import os
import uuid
import stripe
from datetime import datetime
from functools import wraps
from flask import session, redirect, url_for, request, send_from_directory, render_template, flash, make_response, g
from werkzeug.utils import secure_filename
from flask_login import current_user

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

from app import app, db, UPLOAD_FOLDER, choose_pay_link
from auth import require_login
from models import User, Job, JobPhoto, Bid, CompletionPhoto, Review, PageView, HaulerServiceZip
from email_service import (
    notify_customer_new_bid, notify_customer_bid_accepted_confirm,
    notify_customer_job_completed,
    notify_hauler_bid_accepted, notify_hauler_bid_rejected,
    notify_hauler_deposit_paid, notify_hauler_new_job_nearby,
    notify_hauler_job_cancelled, notify_hauler_new_review,
    notify_admin_new_customer, notify_admin_new_hauler,
    notify_admin_new_job, notify_admin_new_bid,
    notify_admin_bid_accepted, notify_admin_deposit_paid,
    notify_admin_job_completed, notify_admin_job_cancelled,
    notify_admin_user_deleted,
)
from sms_service import notify_hauler_new_job_sms, notify_hauler_bid_accepted_sms, notify_hauler_deposit_paid_sms, notify_customer_new_bid_sms

def get_badges(user, reviews=None, completed_count=0):
    badges = []
    if reviews is None:
        reviews = []
    if user.user_type == 'hauler':
        if len(reviews) >= 5:
            avg = sum(r.rating for r in reviews) / len(reviews)
            if avg >= 4.5:
                badges.append({'label': 'Top Hauler', 'icon': '⭐', 'color': '#f59e0b', 'desc': 'Avg rating 4.5+ with 5+ reviews'})
        if completed_count >= 10:
            badges.append({'label': 'Experienced', 'icon': '🏆', 'color': '#16a34a', 'desc': '10+ completed jobs'})
        elif completed_count >= 5:
            badges.append({'label': 'Reliable', 'icon': '✅', 'color': '#2563eb', 'desc': '5+ completed jobs'})
    elif user.user_type == 'customer':
        if completed_count >= 10:
            badges.append({'label': 'Community Builder', 'icon': '🌟', 'color': '#7c3aed', 'desc': '10+ completed jobs'})
        elif completed_count >= 3:
            badges.append({'label': 'Trusted Customer', 'icon': '🤝', 'color': '#9c27b0', 'desc': '3+ completed jobs'})
    return badges


def strip_phone(phone_str):
    if not phone_str:
        return ''
    return ''.join(c for c in phone_str if c.isdigit())

def require_role(role):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                session["next_url"] = request.url
                return redirect(url_for('auth.login'))
            if not current_user.user_type and not current_user.is_admin:
                return redirect(url_for('choose_role'))
            if current_user.user_type != role and not current_user.is_admin:
                return render_template('403.html'), 403
            if current_user.user_type == 'hauler' and not current_user.is_admin and not (current_user.home_zip and current_user.max_travel_miles) and request.endpoint not in ('hauler_setup', 'hauler_setup_save'):
                return redirect(url_for('hauler_setup'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def require_admin(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            session["next_url"] = request.url
            return redirect(url_for('auth.login'))
        if not current_user.is_admin:
            return render_template('403.html'), 403
        return f(*args, **kwargs)
    return decorated_function

@app.before_request
def make_session_permanent():
    session.permanent = True

_SKIP_TRACKING = {'/health', '/robots.txt', '/sitemap.xml', '/favicon.ico'}

@app.before_request
def track_page_view():
    if request.method == 'OPTIONS':
        return
    path = request.path
    if path in _SKIP_TRACKING or path.startswith('/static/') or path.startswith('/uploads/'):
        return

    visitor_id = request.cookies.get('jhe_vid')
    g.pv_new_visitor = False
    if not visitor_id:
        visitor_id = str(uuid.uuid4())[:20]
        g.pv_new_visitor = True
    g.pv_visitor_id = visitor_id

    ua = (request.user_agent.string or '').lower()
    device = 'mobile' if any(x in ua for x in ('mobile', 'android', 'iphone', 'ipad', 'tablet')) else 'desktop'

    referrer = request.referrer or None
    if referrer:
        try:
            from urllib.parse import urlparse
            base_host = urlparse(os.environ.get('APP_BASE_URL', 'https://jhehaul.com')).netloc
            if urlparse(referrer).netloc == base_host:
                referrer = None
        except Exception:
            referrer = None

    try:
        uid = current_user.id if current_user.is_authenticated else None
    except Exception:
        uid = None

    try:
        pv = PageView(
            visitor_id=visitor_id,
            path=path[:200],
            user_id=uid,
            device_type=device,
            referrer=referrer[:500] if referrer else None,
        )
        db.session.add(pv)
        db.session.commit()
    except Exception as _e:
        db.session.rollback()
        app.logger.debug("PageView record skipped: %s", _e)

@app.after_request
def set_visitor_cookie(response):
    if getattr(g, 'pv_new_visitor', False):
        vid = getattr(g, 'pv_visitor_id', None)
        if vid:
            response.set_cookie('jhe_vid', vid, max_age=365*24*3600, httponly=True, samesite='Lax')
    return response

_PHOTO_CONTENT_TYPES = {
    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
    'gif': 'image/gif', 'webp': 'image/webp', 'heic': 'image/heic',
    'heif': 'image/heif', 'bmp': 'image/bmp', 'tiff': 'image/tiff',
}

def _read_photo_bytes(file_obj, ext):
    """Read photo bytes from an uploaded file; rewind stream for subsequent save."""
    ct = _PHOTO_CONTENT_TYPES.get(ext.lstrip('.').lower(), 'image/jpeg')
    file_obj.stream.seek(0)
    data = file_obj.stream.read()
    file_obj.stream.seek(0)
    return data, ct


@app.route("/uploads/db/<int:photo_id>")
def uploaded_file_db(photo_id):
    """Serve a job photo stored as binary in the database."""
    from models import JobPhoto
    photo = JobPhoto.query.get(photo_id)
    if not photo or not photo.data:
        return "", 404
    from flask import Response
    r = Response(photo.data, mimetype=photo.content_type or 'image/jpeg')
    r.headers["Cache-Control"] = "no-cache, max-age=0"
    return r


@app.route("/uploads/completion/db/<int:photo_id>")
def uploaded_completion_file_db(photo_id):
    """Serve a completion photo stored as binary in the database."""
    from models import CompletionPhoto
    photo = CompletionPhoto.query.get(photo_id)
    if not photo or not photo.data:
        return "", 404
    from flask import Response
    r = Response(photo.data, mimetype=photo.content_type or 'image/jpeg')
    r.headers["Cache-Control"] = "no-cache, max-age=0"
    return r


@app.route("/uploads/profile/<user_id>")
def serve_profile_photo(user_id):
    """Serve a user's profile photo stored as binary in the database."""
    user = User.query.get(user_id)
    if not user or not user.profile_photo_data:
        return "", 404
    from flask import Response
    r = Response(user.profile_photo_data, mimetype=user.profile_photo_content_type or 'image/jpeg')
    r.headers["Cache-Control"] = "public, max-age=3600"
    return r


@app.route("/profile/photo/upload", methods=["POST"])
@require_login
def profile_photo_upload():
    photo = request.files.get("profile_photo")
    if not photo or not photo.filename:
        flash("No file selected.", "error")
        return redirect(url_for('profile'))
    ext = os.path.splitext(photo.filename)[1].lower()
    if ext not in {'.jpg', '.jpeg', '.png', '.gif', '.webp'}:
        flash("Please upload a JPG, PNG, GIF, or WebP image.", "error")
        return redirect(url_for('profile'))
    photo_data, photo_ct = _read_photo_bytes(photo, ext)
    if len(photo_data) > 5 * 1024 * 1024:
        flash("Profile photo must be under 5 MB.", "error")
        return redirect(url_for('profile'))
    from storage import upload_file as _upload_file
    _filename, storage_url = _upload_file(photo, ext)
    if storage_url:
        current_user.profile_image_url = storage_url
        current_user.profile_photo_data = None
        current_user.profile_photo_content_type = None
    else:
        current_user.profile_photo_data = photo_data
        current_user.profile_photo_content_type = photo_ct
        current_user.profile_image_url = url_for('serve_profile_photo', user_id=current_user.id)
    db.session.commit()
    flash("Profile picture updated!", "success")
    return redirect(url_for('profile'))


@app.route("/profile/photo/remove", methods=["POST"])
@require_login
def profile_photo_remove():
    current_user.profile_image_url = None
    current_user.profile_photo_data = None
    current_user.profile_photo_content_type = None
    db.session.commit()
    flash("Profile picture removed.", "success")
    return redirect(url_for('profile'))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    """Serve a photo from the local filesystem (fallback for old records without DB data)."""
    import os as _os
    file_path = _os.path.join(UPLOAD_FOLDER, filename)
    if not _os.path.isfile(file_path):
        app.logger.warning("uploaded_file: file not found on disk: %s", filename)
        return "", 404
    response = send_from_directory(UPLOAD_FOLDER, filename)
    response.headers["Cache-Control"] = "no-cache, max-age=0"
    return response

@app.route("/")
def home():
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for('admin_dashboard'))
        if not current_user.user_type:
            return redirect(url_for('choose_role'))
        if current_user.user_type == 'customer':
            return redirect(url_for('customer_jobs'))
        else:
            return redirect(url_for('hauler_jobs'))
    return render_template('landing.html')

@app.route("/invite")
@app.route("/invite/<role>")
def invite(role=None):
    if role in ['customer', 'hauler']:
        session['invited_role'] = role
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for('admin_dashboard'))
        if not current_user.user_type:
            return redirect(url_for('choose_role'))
        if current_user.user_type == 'hauler':
            return redirect(url_for('hauler_jobs'))
        return redirect(url_for('customer_jobs'))
    return render_template('invite_landing.html', role=role)

@app.route("/choose-role")
@require_login
def choose_role():
    if current_user.is_admin:
        session.pop("invited_role", None)
        return redirect(url_for("admin_dashboard"))
    if current_user.user_type == "hauler":
        session.pop("invited_role", None)
        return redirect(url_for("hauler_jobs"))
    if current_user.user_type == "customer":
        session.pop("invited_role", None)
        return redirect(url_for("customer_jobs"))
    invited_role = session.pop("invited_role", None)
    return render_template("choose_role.html", invited_role=invited_role)

@app.route("/set-role", methods=["POST"])
@require_login
def set_role():
    if current_user.is_admin:
        return redirect(url_for('admin_dashboard'))
    role = request.form.get("role")
    if role == 'hauler':
        agree_terms = request.form.get("agree_terms")
        if not agree_terms:
            flash("You must read and agree to the JHE HAUL Hauler Agreement before creating a hauler account.", "error")
            return redirect(url_for('choose_role'))
        current_user.user_type = role
        current_user.agreed_to_hauler_terms = True
        current_user.agreed_to_hauler_terms_at = datetime.now()
        db.session.commit()
        return redirect(url_for('hauler_setup'))
    elif role == 'customer':
        current_user.user_type = role
        db.session.commit()
        try:
            _name = f"{current_user.first_name or ''} {current_user.last_name or ''}".strip() or current_user.email
            notify_admin_new_customer(_name, current_user.email)
        except Exception as e:
            app.logger.error("Admin notify failed (new customer): %s", e)
        return redirect(url_for('customer_jobs'))
    return redirect(url_for('choose_role'))

@app.route("/hauler/setup")
@require_role('hauler')
def hauler_setup():
    if current_user.home_zip and current_user.max_travel_miles and current_user.truck_type:
        return redirect(url_for('hauler_jobs'))
    return render_template('hauler_setup.html')

@app.route("/hauler/setup", methods=["POST"])
@require_role('hauler')
def hauler_setup_save():
    import re
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    phone = strip_phone(request.form.get("phone", ""))
    home_zip = request.form.get("home_zip", "").strip()
    max_travel_miles = request.form.get("max_travel_miles", "").strip()
    notify_new_jobs = request.form.get("notify_new_jobs") == "1"
    notify_sms = request.form.get("notify_sms") == "1"
    truck_type = request.form.get("truck_type", "").strip()
    trailer_type = request.form.get("trailer_type", "").strip()

    if not first_name or not last_name:
        flash("Please enter your first and last name.", "error")
        return redirect(url_for('hauler_setup'))

    if not home_zip or not re.match(r'^\d{5}$', home_zip):
        flash("Please enter a valid 5-digit ZIP code.", "error")
        return redirect(url_for('hauler_setup'))

    from models import ZipCode
    if not ZipCode.query.get(home_zip):
        flash("That ZIP code is not supported yet. We currently cover Minnesota and Wisconsin.", "error")
        return redirect(url_for('hauler_setup'))

    from launch_zone import in_launch_zone
    allowed, _ = in_launch_zone(home_zip)
    if not allowed:
        app.logger.warning("launch_zone: hauler setup rejected ZIP %s for user %s", home_zip, current_user.id)
        flash("JHE Haul is currently launching in select Minnesota areas. "
              "We're not in your area just yet — check back soon as we expand!", "error")
        return redirect(url_for('hauler_setup'))

    if not max_travel_miles:
        flash("Please enter how far you're willing to drive.", "error")
        return redirect(url_for('hauler_setup'))

    current_user.first_name = first_name
    current_user.last_name = last_name
    current_user.phone = phone if phone else None
    current_user.home_zip = home_zip
    current_user.max_travel_miles = int(max_travel_miles)
    current_user.notify_new_jobs = notify_new_jobs
    current_user.notify_sms = notify_sms
    current_user.truck_type = truck_type if truck_type else None
    current_user.trailer_type = trailer_type if trailer_type else None
    db.session.commit()

    try:
        notify_admin_new_hauler(
            f"{current_user.first_name} {current_user.last_name}".strip() or current_user.email,
            current_user.email,
            home_zip,
            truck_type
        )
    except Exception as e:
        app.logger.error("Admin notify failed (new hauler): %s", e)

    flash("You're all set! Browse open jobs below.", "success")
    return redirect(url_for('hauler_jobs'))

@app.route("/about")
def about():
    return render_template('about.html')

@app.route("/hauler-agreement")
def hauler_agreement():
    return render_template('hauler_agreement.html')

@app.route("/customer-terms")
def customer_terms():
    return render_template('customer_terms.html')

@app.route("/customer/new", methods=["GET"])
@require_role('customer')
def customer_new():
    return render_template('customer_new.html')

@app.route("/customer/create", methods=["POST"])
@require_role('customer')
def customer_create():
    customer_name = request.form.get("customer_name", "").strip()
    customer_phone = strip_phone(request.form.get("customer_phone", ""))
    pickup_address = request.form.get("pickup_address", "").strip()
    pickup_zip = request.form.get("pickup_zip", "").strip()
    preferred_date = request.form.get("preferred_date", "").strip()
    preferred_time = request.form.get("preferred_time", "").strip()
    job_description = request.form.get("job_description", "").strip()

    agree_terms = request.form.get("agree_terms")
    if not agree_terms:
        flash("You must certify that you own or have legal authority over the property before posting a job.", "error")
        return redirect(url_for('customer_new'))

    if not customer_name or not pickup_address or not job_description or not pickup_zip:
        return "Missing required fields", 400

    import re
    if not re.match(r'^\d{5}$', pickup_zip):
        flash("Please enter a valid 5-digit ZIP code.", "error")
        return redirect(url_for('customer_new'))

    from models import ZipCode
    if not ZipCode.query.get(pickup_zip):
        flash("That ZIP code is not supported yet. We currently cover Minnesota and Wisconsin.", "error")
        return redirect(url_for('customer_new'))

    from launch_zone import in_launch_zone
    allowed, _ = in_launch_zone(pickup_zip)
    if not allowed:
        app.logger.warning("launch_zone: job posting rejected ZIP %s for user %s", pickup_zip, current_user.id)
        flash("JHE Haul is currently launching in select Minnesota areas. "
              "We're not in your area just yet — check back soon as we expand!", "error")
        return redirect(url_for('customer_new'))

    job = Job(
        customer_id=current_user.id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        pickup_address=pickup_address,
        pickup_zip=pickup_zip,
        preferred_date=preferred_date if preferred_date else None,
        preferred_time=preferred_time if preferred_time else None,
        job_description=job_description,
        status='open'
    )
    db.session.add(job)
    db.session.commit()

    try:
        notify_admin_new_job(job.id, customer_name, pickup_zip, job_description)
    except Exception as e:
        app.logger.error("Admin notify failed (new job #%s): %s", job.id, e)

    from storage import upload_file as _upload_file
    photos = request.files.getlist("photos")
    for photo in photos:
        if photo and photo.filename:
            ext = os.path.splitext(photo.filename)[1]
            photo_data, photo_ct = _read_photo_bytes(photo, ext)
            filename, storage_url = _upload_file(photo, ext)
            photo_record = JobPhoto(
                job_id=job.id, filename=filename, storage_url=storage_url,
                data=photo_data if not storage_url else None, content_type=photo_ct,
            )
            db.session.add(photo_record)
    db.session.commit()

    if pickup_zip:
        from models import ZipCode
        from distance import haversine_miles
        job_zip_loc = ZipCode.query.get(pickup_zip)
        if job_zip_loc:
            # All haulers who have radius configured
            radius_haulers = User.query.filter(
                User.user_type == 'hauler',
                User.home_zip.isnot(None),
                User.max_travel_miles.isnot(None),
                User.email.isnot(None),
                User.notify_new_jobs == True
            ).all()

            # Hauler IDs who explicitly service this ZIP
            explicit_ids = set(
                sz.hauler_id for sz in
                HaulerServiceZip.query.filter_by(zip_code=pickup_zip).all()
            )

            # Build (hauler, distance_miles) pairs for all qualifying haulers
            to_notify = []   # list of (hauler, distance_miles)
            notified_ids = set()

            for hauler in radius_haulers:
                try:
                    hauler_zip_loc = ZipCode.query.get(hauler.home_zip)
                    if not hauler_zip_loc:
                        continue
                    dist = haversine_miles(
                        hauler_zip_loc.lat, hauler_zip_loc.lon,
                        job_zip_loc.lat,   job_zip_loc.lon
                    )
                    in_radius   = dist <= hauler.max_travel_miles
                    in_explicit = hauler.id in explicit_ids
                    if in_radius or in_explicit:
                        to_notify.append((hauler, dist))
                        notified_ids.add(hauler.id)
                except Exception:
                    pass

            # Also pick up haulers who only have explicit ZIPs but no radius set
            extra_haulers = User.query.filter(
                User.id.in_(explicit_ids - notified_ids),
                User.email.isnot(None),
                User.notify_new_jobs == True
            ).all()
            for hauler in extra_haulers:
                try:
                    dist = 0.0
                    if hauler.home_zip:
                        hauler_zip_loc = ZipCode.query.get(hauler.home_zip)
                        if hauler_zip_loc:
                            dist = haversine_miles(
                                hauler_zip_loc.lat, hauler_zip_loc.lon,
                                job_zip_loc.lat,   job_zip_loc.lon
                            )
                    to_notify.append((hauler, dist))
                    notified_ids.add(hauler.id)
                except Exception:
                    pass

            # Sort closest-first so nearby haulers are contacted first
            to_notify.sort(key=lambda x: x[1])

            for hauler, distance_miles in to_notify:
                try:
                    notify_hauler_new_job_nearby(hauler.email, job.id, job_description, distance_miles)
                    if hauler.notify_sms and hauler.phone:
                        notify_hauler_new_job_sms(hauler.phone, job.id, distance_miles)
                except Exception:
                    pass

    return redirect(url_for("customer_jobs"))

@app.route("/customer/jobs")
@require_role('customer')
def customer_jobs():
    jobs = Job.query.filter_by(customer_id=current_user.id).order_by(Job.id.desc()).all()
    return render_template('customer_jobs.html', jobs=jobs)

@app.route("/customer/job/<int:job_id>")
@require_role('customer')
def customer_job_detail(job_id):
    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id and not current_user.is_admin:
        return "Access denied", 403

    bids = Bid.query.filter_by(job_id=job_id).order_by(Bid.quote_amount.asc()).all()
    
    pay_link = None
    checkout_over500_url = None
    pay_link_missing = False
    accepted_bid = None
    if job.status == "accepted" and not job.deposit_paid:
        accepted_bid = Bid.query.filter_by(job_id=job_id, status='accepted').first()
        quote = float(job.accepted_quote or 0)
        if quote > 500 and accepted_bid:
            checkout_over500_url = url_for('checkout_over500', bid_id=accepted_bid.id)
        else:
            pay_link = choose_pay_link(job.accepted_quote)
            app.logger.info(
                "Payment bracket resolved: job=%s quote=$%.2f pay_link_present=%s",
                job_id, quote, bool(pay_link)
            )
            if pay_link:
                base_url = os.environ.get("APP_BASE_URL", "https://jhehaul.com").rstrip("/")
                success_url = f"{base_url}/payment_success/{job.id}"
                pay_link = f"{pay_link}?success_url={success_url}"
            else:
                pay_link_missing = True
                app.logger.warning(
                    "No pay link configured: job=%s quote=$%.2f — "
                    "check PAY_LINK_UNDER_150 / PAY_LINK_150_300 / PAY_LINK_300_500 env vars",
                    job_id, quote
                )

    hauler_map = {}
    for bid in bids:
        if bid.hauler_id and bid.hauler_id not in hauler_map:
            h = User.query.get(bid.hauler_id)
            if h:
                hauler_map[bid.hauler_id] = h
    return render_template('customer_job_detail.html', job=job, bids=bids, pay_link=pay_link,
                           checkout_over500_url=checkout_over500_url, pay_link_missing=pay_link_missing,
                           hauler_map=hauler_map)

@app.route("/customer/upload_photos/<int:job_id>", methods=["POST"])
@require_role('customer')
def customer_upload_photos(job_id):
    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    if job.status not in ['open', 'bidding', 'accepted', 'deposit_paid']:
        return "Cannot upload photos at this stage", 400

    from storage import upload_file as _upload_file
    photos = request.files.getlist("photos")
    for photo in photos:
        if photo and photo.filename:
            ext = os.path.splitext(photo.filename)[1]
            photo_data, photo_ct = _read_photo_bytes(photo, ext)
            filename, storage_url = _upload_file(photo, ext)
            photo_record = JobPhoto(
                job_id=job.id, filename=filename, storage_url=storage_url,
                data=photo_data if not storage_url else None, content_type=photo_ct,
            )
            db.session.add(photo_record)

    db.session.commit()
    return redirect(url_for('customer_job_detail', job_id=job.id))

@app.route("/customer/accept_bid/<int:bid_id>", methods=["POST"])
@require_role('customer')
def customer_accept_bid(bid_id):
    bid = Bid.query.get_or_404(bid_id)
    job = Job.query.get_or_404(bid.job_id)

    if job.customer_id != current_user.id and not current_user.is_admin:
        app.logger.warning("Access denied: user %s tried to accept bid %s on job %s owned by %s",
                           current_user.id, bid_id, job.id, job.customer_id)
        return "Access denied", 403

    try:
        job.status = 'accepted'
        job.accepted_hauler = bid.hauler_name
        job.accepted_hauler_id = bid.hauler_id
        job.accepted_quote = bid.quote_amount

        bid.status = 'accepted'
        Bid.query.filter(
            Bid.job_id == job.id, Bid.id != bid_id
        ).update({'status': 'rejected'}, synchronize_session=False)

        db.session.commit()
        app.logger.info("Bid %s accepted for job %s, quote=$%.2f by user %s",
                        bid_id, job.id, float(bid.quote_amount), current_user.id)
        flash("Bid accepted! Pay the deposit below to confirm your booking.", "success")
    except Exception as e:
        db.session.rollback()
        app.logger.error("Error accepting bid %s for job %s: %s", bid_id, bid.job_id, e)
        flash("Something went wrong accepting the bid. Please try again.", "error")
        return redirect(url_for('customer_job_detail', job_id=bid.job_id))

    try:
        hauler = User.query.get(bid.hauler_id)
        if hauler and hauler.email:
            notify_hauler_bid_accepted(hauler.email, job.id, bid.quote_amount)
        if hauler and hauler.notify_sms and hauler.phone:
            notify_hauler_bid_accepted_sms(hauler.phone, job.id)
    except Exception as e:
        app.logger.error("Notification failed after accepting bid %s: %s", bid_id, e)

    try:
        # Confirm acceptance back to customer
        if current_user.email:
            notify_customer_bid_accepted_confirm(
                current_user.email, job.id, bid.hauler_name, float(bid.quote_amount)
            )
    except Exception as e:
        app.logger.error("Customer bid-accepted confirm failed (job #%s): %s", job.id, e)

    try:
        # Notify other haulers their bids were not chosen
        rejected_bids = Bid.query.filter(
            Bid.job_id == job.id, Bid.id != bid_id, Bid.status == 'rejected'
        ).all()
        for rb in rejected_bids:
            rb_hauler = User.query.get(rb.hauler_id) if rb.hauler_id else None
            if rb_hauler and rb_hauler.email:
                notify_hauler_bid_rejected(rb_hauler.email, job.id)
    except Exception as e:
        app.logger.error("Rejected-bid notifications failed (job #%s): %s", job.id, e)

    try:
        _cname = f"{current_user.first_name or ''} {current_user.last_name or ''}".strip() or current_user.email
        notify_admin_bid_accepted(job.id, _cname, bid.hauler_name, float(bid.quote_amount))
    except Exception as e:
        app.logger.error("Admin notify failed (bid accepted job #%s): %s", job.id, e)

    return redirect(url_for('customer_job_detail', job_id=job.id))

@app.route("/customer/mark_paid/<int:job_id>", methods=["POST"])
@require_role('customer')
def customer_mark_paid(job_id):
    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    job.deposit_paid = True
    job.status = 'deposit_paid'
    db.session.commit()
    
    if job.accepted_hauler_id:
        hauler = User.query.get(job.accepted_hauler_id)
        if hauler and hauler.email:
            notify_hauler_deposit_paid(hauler.email, job.id, job.pickup_address, job.pickup_zip)
            if hauler.notify_sms and hauler.phone:
                notify_hauler_deposit_paid_sms(hauler.phone, job.id)
    try:
        _cname = f"{current_user.first_name or ''} {current_user.last_name or ''}".strip() or current_user.email
        notify_admin_deposit_paid(job.id, _cname, job.accepted_hauler, job.accepted_quote)
    except Exception as e:
        app.logger.error("Admin notify failed (deposit paid job #%s): %s", job.id, e)

    return redirect(url_for('customer_job_detail', job_id=job_id))

@app.route("/payment_success/<int:job_id>")
@require_role('customer')
def payment_success(job_id):
    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    return redirect(url_for('customer_job_detail', job_id=job_id))

@app.route("/checkout/over500/<int:bid_id>")
@require_role('customer')
def checkout_over500(bid_id):
    bid = Bid.query.get_or_404(bid_id)
    job = Job.query.get_or_404(bid.job_id)

    if job.customer_id != current_user.id:
        return "Access denied", 403
    if job.status != 'accepted' or job.deposit_paid:
        return redirect(url_for('customer_job_detail', job_id=job.id))

    quote_amount = float(bid.quote_amount or 0)
    if quote_amount <= 500:
        return redirect(url_for('customer_job_detail', job_id=job.id))

    platform_fee = 49.99 + (quote_amount - 500) * 0.10
    fee_cents = int(round(platform_fee * 100))

    domain = os.environ.get("APP_BASE_URL", "https://jhehaul.com").rstrip("/")

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'JHE Haul - Platform Fee (Job #{job.id})',
                        'description': f'Deposit for hauling quote of ${quote_amount:.2f}',
                    },
                    'unit_amount': fee_cents,
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=f"{domain}/checkout/over500/success?session_id={{CHECKOUT_SESSION_ID}}&job_id={job.id}",
            cancel_url=f"{domain}/customer/job/{job.id}",
            metadata={
                'job_id': str(job.id),
                'bid_id': str(bid.id),
                'quote_amount': str(quote_amount),
                'platform_fee': str(platform_fee),
            },
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        import logging
        logging.error(f"Stripe checkout error: {e}")
        flash("Payment error. Please try again.", "error")
        return redirect(url_for('customer_job_detail', job_id=job.id))

@app.route("/checkout/over500/success")
@require_role('customer')
def checkout_over500_success():
    session_id = request.args.get('session_id')
    job_id = request.args.get('job_id', type=int)

    if not session_id or not job_id:
        return redirect(url_for('customer_jobs'))

    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403

    if job.deposit_paid:
        return redirect(url_for('customer_job_detail', job_id=job.id))

    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
        if checkout_session.payment_status == 'paid':
            job.deposit_paid = True
            job.status = 'deposit_paid'
            db.session.commit()

            if job.accepted_hauler_id:
                hauler = User.query.get(job.accepted_hauler_id)
                if hauler and hauler.email:
                    notify_hauler_deposit_paid(hauler.email, job.id, job.pickup_address, job.pickup_zip)
                    if hauler.notify_sms and hauler.phone:
                        notify_hauler_deposit_paid_sms(hauler.phone, job.id)
            try:
                _cname = f"{current_user.first_name or ''} {current_user.last_name or ''}".strip() or current_user.email
                notify_admin_deposit_paid(job.id, _cname, job.accepted_hauler, job.accepted_quote)
            except Exception as e:
                app.logger.error("Admin notify failed (over500 deposit paid job #%s): %s", job.id, e)

            return redirect(url_for('customer_job_detail', job_id=job.id))
        else:
            flash("Payment not completed. Please try again.", "error")
            return redirect(url_for('customer_job_detail', job_id=job.id))
    except Exception as e:
        import logging
        logging.error(f"Stripe session verify error: {e}")
        flash("Could not verify payment. Please contact support.", "error")
        return redirect(url_for('customer_job_detail', job_id=job.id))

@app.route("/hauler/jobs")
@require_role('hauler')
def hauler_jobs():
    from models import ZipCode
    from distance import haversine_miles

    all_jobs = Job.query.filter(Job.status.in_(['open', 'bidding'])).order_by(Job.id.desc()).all()

    job_distances = {}
    filtered_jobs = []
    seen_ids = set()
    max_miles = current_user.max_travel_miles or 0
    hauler_zip_rec = ZipCode.query.get(current_user.home_zip) if current_user.home_zip else None

    # Explicit service ZIPs this hauler has added
    explicit_zips = set(
        sz.zip_code for sz in
        HaulerServiceZip.query.filter_by(hauler_id=current_user.id).all()
    )

    for job in all_jobs:
        if job.id in seen_ids:
            continue

        in_explicit = job.pickup_zip and job.pickup_zip in explicit_zips

        if hauler_zip_rec and job.pickup_zip:
            job_zip_rec = ZipCode.query.get(job.pickup_zip)
            if job_zip_rec:
                miles = round(haversine_miles(
                    hauler_zip_rec.lat, hauler_zip_rec.lon,
                    job_zip_rec.lat, job_zip_rec.lon
                ), 1)
                in_radius = miles <= max_miles
                included  = in_radius or in_explicit
                app.logger.info(
                    "Job filter — #%s: zip=%s dist=%.1f mi max=%s explicit=%s → %s",
                    job.id, job.pickup_zip, miles, max_miles, in_explicit,
                    "INCLUDED" if included else "FILTERED OUT"
                )
                if included:
                    job_distances[job.id] = miles
                    filtered_jobs.append(job)
                    seen_ids.add(job.id)
            else:
                # ZIP not in table — include if explicit, else include as fallback
                app.logger.info(
                    "Job filter — #%s: pickup_zip=%s not in ZIP table explicit=%s → INCLUDED",
                    job.id, job.pickup_zip, in_explicit
                )
                filtered_jobs.append(job)
                seen_ids.add(job.id)
        elif in_explicit:
            # Hauler has no home ZIP but explicitly services this ZIP
            filtered_jobs.append(job)
            seen_ids.add(job.id)
        elif not hauler_zip_rec and not job.pickup_zip:
            # No coords on either side — include as fallback
            app.logger.info(
                "Job filter — #%s: missing hauler_zip or job pickup_zip → INCLUDED (no coords)",
                job.id
            )
            filtered_jobs.append(job)
            seen_ids.add(job.id)

    app.logger.info(
        "hauler_jobs result: user=%s home_zip=%s max=%s mi explicit_zips=%d — showing %d of %d open jobs",
        current_user.id, current_user.home_zip, max_miles,
        len(explicit_zips), len(filtered_jobs), len(all_jobs)
    )

    return render_template('hauler_jobs.html', jobs=filtered_jobs,
                           job_distances=job_distances)

@app.route("/hauler/bid/<int:job_id>", methods=["GET"])
@require_role('hauler')
def hauler_bid_form(job_id):
    from models import ZipCode
    from distance import haversine_miles
    job = Job.query.get_or_404(job_id)
    approx_miles = None
    if current_user.home_zip and job.pickup_zip:
        hauler_zip = ZipCode.query.get(current_user.home_zip)
        job_zip = ZipCode.query.get(job.pickup_zip)
        if hauler_zip and job_zip:
            approx_miles = round(haversine_miles(hauler_zip.lat, hauler_zip.lon, job_zip.lat, job_zip.lon), 1)
    return render_template('hauler_bid.html', job=job, approx_miles=approx_miles)

@app.route("/hauler/bid/<int:job_id>", methods=["POST"])
@require_role('hauler')
def hauler_bid_submit(job_id):
    hauler_name = request.form.get("hauler_name", "").strip()
    hauler_phone = strip_phone(request.form.get("hauler_phone", ""))
    quote_amount = request.form.get("quote_amount", "").strip()
    message = request.form.get("message", "").strip()

    if not hauler_name or not quote_amount:
        return "Missing required fields", 400

    try:
        quote_amount = float(quote_amount)
    except ValueError:
        return "Invalid quote amount", 400

    job = Job.query.get_or_404(job_id)
    if job.status == 'open':
        job.status = 'bidding'

    bid = Bid(
        job_id=job_id,
        hauler_id=current_user.id,
        hauler_name=hauler_name,
        hauler_phone=hauler_phone,
        quote_amount=quote_amount,
        message=message,
        status='active'
    )
    db.session.add(bid)
    db.session.commit()

    customer = User.query.get(job.customer_id)
    if customer and customer.email:
        notify_customer_new_bid(customer.email, job_id, hauler_name, quote_amount)
    if customer and customer.notify_sms and customer.phone:
        notify_customer_new_bid_sms(customer.phone, job_id, hauler_name, quote_amount)

    try:
        notify_admin_new_bid(job_id, hauler_name, quote_amount)
    except Exception as e:
        app.logger.error("Admin notify failed (new bid on job #%s): %s", job_id, e)

    return render_template('bid_success.html')

@app.route("/hauler/dashboard")
@require_role('hauler')
def hauler_dashboard():
    jobs = Job.query.filter(
        Job.accepted_hauler_id == current_user.id,
        Job.status.in_(['accepted', 'deposit_paid', 'completed'])
    ).order_by(Job.id.desc()).all()
    all_reviews = Review.query.filter_by(hauler_id=current_user.id).all()
    hauler_review_count = len(all_reviews)
    hauler_avg_rating = sum(r.rating for r in all_reviews) / hauler_review_count if hauler_review_count else 0.0
    completed_count = sum(1 for j in jobs if j.status == 'completed')
    hauler_badges = get_badges(current_user, all_reviews, completed_count)
    return render_template('hauler_dashboard.html', jobs=jobs,
                           hauler_avg_rating=hauler_avg_rating,
                           hauler_review_count=hauler_review_count,
                           hauler_badges=hauler_badges)

@app.route("/profile")
@require_login
def profile():
    reviews = []
    avg_rating = 0.0
    review_count = 0
    completed_count = 0
    badges = []

    if current_user.user_type == 'hauler':
        all_reviews = Review.query.filter_by(hauler_id=current_user.id).order_by(Review.created_at.desc()).all()
        review_count = len(all_reviews)
        avg_rating = sum(r.rating for r in all_reviews) / review_count if review_count else 0.0
        reviews = all_reviews[:5]
        completed_count = Job.query.filter_by(accepted_hauler_id=current_user.id, status='completed').count()
        badges = get_badges(current_user, all_reviews, completed_count)
    elif current_user.user_type == 'customer':
        completed_count = Job.query.filter_by(customer_id=current_user.id, status='completed').count()
        badges = get_badges(current_user, [], completed_count)

    service_zips = []
    if current_user.user_type == 'hauler':
        from models import ZipCode as _ZC
        raw = HaulerServiceZip.query.filter_by(hauler_id=current_user.id).order_by(HaulerServiceZip.zip_code).all()
        service_zips = []
        for sz in raw:
            zc = _ZC.query.get(sz.zip_code)
            service_zips.append({
                'zip': sz.zip_code,
                'city': zc.city if zc else '',
                'state': zc.state if zc else '',
            })

    return render_template('profile.html',
                           reviews=reviews,
                           avg_rating=avg_rating,
                           review_count=review_count,
                           completed_count=completed_count,
                           badges=badges,
                           service_zips=service_zips)

@app.route("/hauler/service-zips/add", methods=["POST"])
@require_role('hauler')
def hauler_service_zip_add():
    import re
    from models import ZipCode as _ZC
    zip_code = request.form.get("zip_code", "").strip()
    if not re.match(r'^\d{5}$', zip_code):
        flash("Please enter a valid 5-digit ZIP code.", "error")
        return redirect(url_for('profile'))
    if not _ZC.query.get(zip_code):
        flash("That ZIP code isn't in our database yet. We currently cover Minnesota and Wisconsin.", "error")
        return redirect(url_for('profile'))
    existing = HaulerServiceZip.query.filter_by(
        hauler_id=current_user.id, zip_code=zip_code
    ).first()
    if existing:
        flash(f"ZIP {zip_code} is already in your service list.", "info")
        return redirect(url_for('profile'))
    count = HaulerServiceZip.query.filter_by(hauler_id=current_user.id).count()
    if count >= 25:
        flash("You can add up to 25 specific ZIP codes. Remove some to add more.", "error")
        return redirect(url_for('profile'))
    sz = HaulerServiceZip(hauler_id=current_user.id, zip_code=zip_code)
    db.session.add(sz)
    db.session.commit()
    zc = _ZC.query.get(zip_code)
    city_str = f" ({zc.city})" if zc and zc.city else ""
    flash(f"Added {zip_code}{city_str} to your service area.", "success")
    return redirect(url_for('profile'))


@app.route("/hauler/service-zips/remove", methods=["POST"])
@require_role('hauler')
def hauler_service_zip_remove():
    zip_code = request.form.get("zip_code", "").strip()
    sz = HaulerServiceZip.query.filter_by(
        hauler_id=current_user.id, zip_code=zip_code
    ).first()
    if sz:
        db.session.delete(sz)
        db.session.commit()
        flash(f"Removed {zip_code} from your service area.", "success")
    return redirect(url_for('profile'))


@app.route("/profile/update", methods=["POST"])
@require_login
def profile_update():
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    phone = strip_phone(request.form.get("phone", ""))
    
    current_user.first_name = first_name
    current_user.last_name = last_name
    current_user.phone = phone if phone else None
    
    if current_user.user_type == 'customer':
        notify_sms = request.form.get("notify_sms") == "1"
        current_user.notify_sms = notify_sms
    
    if current_user.user_type == 'hauler':
        import re
        home_zip = request.form.get("home_zip", "").strip()
        max_travel_miles = request.form.get("max_travel_miles", "").strip()
        notify_new_jobs = request.form.get("notify_new_jobs") == "1"
        notify_sms = request.form.get("notify_sms") == "1"
        
        if home_zip:
            if not re.match(r'^\d{5}$', home_zip):
                flash("Please enter a valid 5-digit ZIP code.", "error")
                return redirect(url_for('profile'))
            from models import ZipCode
            if not ZipCode.query.get(home_zip):
                flash("That ZIP code is not supported yet. We currently cover Minnesota and Wisconsin.", "error")
                return redirect(url_for('profile'))
        
        current_user.home_zip = home_zip if home_zip else None
        current_user.max_travel_miles = int(max_travel_miles) if max_travel_miles else None
        current_user.notify_new_jobs = notify_new_jobs
        current_user.notify_sms = notify_sms
    
    db.session.commit()
    flash("Profile updated successfully!", "success")
    return redirect(url_for('profile'))

@app.route("/account/delete", methods=["POST"])
@require_login
def delete_account():
    from models import OAuth, Bid, Review, CompletionPhoto
    user_id = current_user.id
    user_type = current_user.user_type
    
    if user_type == 'customer':
        jobs = Job.query.filter_by(customer_id=user_id).all()
        active_jobs = [j for j in jobs if j.status in ['open', 'bidding', 'accepted', 'deposit_paid']]
        if active_jobs:
            return "Cannot delete account with active jobs. Please complete or cancel all jobs first.", 400
        for job in jobs:
            JobPhoto.query.filter_by(job_id=job.id).delete()
            Bid.query.filter_by(job_id=job.id).delete()
            Review.query.filter_by(job_id=job.id).delete()
            CompletionPhoto.query.filter_by(job_id=job.id).delete()
            db.session.delete(job)
    
    if user_type == 'hauler':
        active_bids = Bid.query.filter_by(hauler_id=user_id, status='accepted').all()
        for bid in active_bids:
            job = Job.query.get(bid.job_id)
            if job and job.status in ['accepted', 'deposit_paid']:
                return "Cannot delete account with active accepted jobs. Please complete all jobs first.", 400
        Bid.query.filter_by(hauler_id=user_id).delete()
        Review.query.filter_by(hauler_id=user_id).delete()
        CompletionPhoto.query.filter_by(hauler_id=user_id).delete()
    
    _del_name = (((current_user.first_name or '') + ' ' + (current_user.last_name or '')).strip()
                 or current_user.email or 'Unknown')
    _del_email = current_user.email or ''
    _del_type = user_type or ''
    OAuth.query.filter_by(user_id=user_id).delete()
    db.session.delete(current_user)
    db.session.commit()

    try:
        notify_admin_user_deleted(_del_name, _del_email, _del_type)
    except Exception as e:
        app.logger.error("Admin notify failed (user deleted %s): %s", _del_email, e)

    return redirect(url_for('home'))

@app.route("/customer/complete/<int:job_id>", methods=["POST"])
@require_role('customer')
def customer_complete_job(job_id):
    from datetime import datetime
    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    if job.status != 'deposit_paid':
        return "Job cannot be completed yet", 400
    job.status = 'completed'
    job.completed_at = datetime.now()
    db.session.commit()

    try:
        notify_admin_job_completed(
            job.id, job.customer_name, job.accepted_hauler, job.accepted_quote
        )
    except Exception as e:
        app.logger.error("Admin notify failed (job #%s completed by customer): %s", job.id, e)

    try:
        if current_user.email:
            notify_customer_job_completed(current_user.email, job.id)
    except Exception as e:
        app.logger.error("Customer job-completed notify failed (job #%s): %s", job.id, e)

    return redirect(url_for('customer_job_detail', job_id=job_id))

@app.route("/customer/cancel/<int:job_id>", methods=["POST"])
@require_role('customer')
def customer_cancel_job(job_id):
    from datetime import datetime
    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    if job.status not in ['open', 'bidding']:
        return "Job cannot be cancelled at this stage", 400
    job.status = 'cancelled'
    job.cancelled_at = datetime.now()
    db.session.commit()

    try:
        notify_admin_job_cancelled(job.id, job.customer_name)
    except Exception as e:
        app.logger.error("Admin notify failed (job #%s cancelled): %s", job.id, e)

    try:
        # Notify all haulers who bid on this job
        active_bids = Bid.query.filter_by(job_id=job.id).all()
        for b in active_bids:
            bh = User.query.get(b.hauler_id) if b.hauler_id else None
            if bh and bh.email:
                notify_hauler_job_cancelled(bh.email, job.id, job.customer_name)
    except Exception as e:
        app.logger.error("Hauler cancel notify failed (job #%s): %s", job.id, e)

    return redirect(url_for('customer_jobs'))

@app.route("/customer/review/<int:job_id>", methods=["GET", "POST"])
@require_role('customer')
def customer_review(job_id):
    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    if job.status != 'completed':
        return "Job must be completed before reviewing", 400
    
    existing_review = Review.query.filter_by(job_id=job_id).first()
    if existing_review:
        return redirect(url_for('customer_job_detail', job_id=job_id))
    
    if request.method == "POST":
        rating = int(request.form.get("rating", 5))
        if rating < 1 or rating > 5:
            rating = 5
        comment = request.form.get("comment", "").strip()
        if not job.accepted_hauler_id:
            return "No hauler to review", 400
        review = Review(
            job_id=job_id,
            hauler_id=job.accepted_hauler_id,
            customer_id=current_user.id,
            rating=rating,
            comment=comment
        )
        db.session.add(review)
        db.session.commit()

        try:
            hauler = User.query.get(job.accepted_hauler_id)
            if hauler and hauler.email:
                cname = f"{current_user.first_name or ''} {current_user.last_name or ''}".strip() or current_user.email
                notify_hauler_new_review(hauler.email, job_id, cname, rating, comment)
        except Exception as e:
            app.logger.error("Review notification failed for job #%s: %s", job_id, e)

        flash("Review submitted! Thank you for your feedback.", "success")
        return redirect(url_for('customer_job_detail', job_id=job_id))
    
    return render_template('customer_review.html', job=job)

@app.route("/hauler/earnings")
@require_role('hauler')
def hauler_earnings():
    completed_jobs = Job.query.filter(
        Job.accepted_hauler_id == current_user.id,
        Job.status == 'completed'
    ).order_by(Job.completed_at.desc()).all()
    
    total_earnings = sum(job.accepted_quote or 0 for job in completed_jobs)
    job_count = len(completed_jobs)
    
    reviews = Review.query.filter_by(hauler_id=current_user.id).all()
    avg_rating = sum(r.rating for r in reviews) / len(reviews) if reviews else 0
    
    return render_template('hauler_earnings.html', 
                           jobs=completed_jobs, 
                           total_earnings=total_earnings,
                           job_count=job_count,
                           avg_rating=avg_rating,
                           review_count=len(reviews))

@app.route("/hauler/upload_photos/<int:job_id>", methods=["GET", "POST"])
@require_role('hauler')
def hauler_upload_photos(job_id):
    job = Job.query.get_or_404(job_id)
    if job.accepted_hauler_id != current_user.id:
        return "Access denied", 403
    if job.status not in ['deposit_paid', 'completed']:
        return "Cannot upload photos at this stage", 400

    if request.method == "POST":
        before_photos = request.files.getlist("before_photos")
        after_photos = request.files.getlist("after_photos")

        from storage import upload_file as _upload_file
        saved_after = 0
        for photo in before_photos:
            if photo and photo.filename:
                ext = os.path.splitext(photo.filename)[1]
                photo_data, photo_ct = _read_photo_bytes(photo, ext)
                filename, storage_url = _upload_file(photo, ext)
                photo_record = CompletionPhoto(
                    job_id=job.id, filename=filename, storage_url=storage_url,
                    data=photo_data if not storage_url else None, content_type=photo_ct,
                    photo_type='before'
                )
                db.session.add(photo_record)

        for photo in after_photos:
            if photo and photo.filename:
                ext = os.path.splitext(photo.filename)[1]
                photo_data, photo_ct = _read_photo_bytes(photo, ext)
                filename, storage_url = _upload_file(photo, ext)
                photo_record = CompletionPhoto(
                    job_id=job.id, filename=filename, storage_url=storage_url,
                    data=photo_data if not storage_url else None, content_type=photo_ct,
                    photo_type='after'
                )
                db.session.add(photo_record)
                saved_after += 1

        if saved_after == 0:
            db.session.rollback()
            flash("You must upload at least one AFTER photo to submit completion proof.", "error")
            return render_template('hauler_upload_photos.html', job=job)

        job.status = 'completed'
        job.completed_at = datetime.now()
        db.session.commit()

        try:
            notify_admin_job_completed(
                job.id,
                job.customer_name,
                job.accepted_hauler,
                job.accepted_quote
            )
        except Exception as e:
            app.logger.error("Admin notify failed (job #%s completed by hauler): %s", job.id, e)

        flash("Completion proof submitted. Customer has been notified and payment can now be released.", "success")
        return redirect(url_for('hauler_dashboard'))

    return render_template('hauler_upload_photos.html', job=job)

@app.route("/admin")
@require_admin
def admin_dashboard():
    total_users = User.query.count()
    total_customers = User.query.filter_by(user_type='customer').count()
    total_haulers = User.query.filter_by(user_type='hauler').count()
    total_jobs = Job.query.count()
    open_jobs = Job.query.filter_by(status='open').count()
    active_jobs = Job.query.filter(Job.status.in_(['accepted', 'deposit_paid'])).count()
    completed_jobs = Job.query.filter_by(status='completed').count()
    cancelled_jobs = Job.query.filter_by(status='cancelled').count()
    total_bids = Bid.query.count()
    accepted_bids = Bid.query.filter_by(status='accepted').count()
    pending_bids = (Bid.query
                    .join(Job, Bid.job_id == Job.id)
                    .filter(Job.status.in_(['open', 'bidding']))
                    .count())
    total_revenue = db.session.query(db.func.sum(Job.accepted_quote)).filter(Job.status == 'completed').scalar() or 0

    pending_users = User.query.filter(
        User.user_type == None, User.is_admin == False
    ).order_by(User.created_at.desc()).all()
    jobs = Job.query.order_by(Job.id.desc()).all()
    recent_accepted = (Job.query
                       .filter(Job.accepted_hauler_id != None)
                       .order_by(Job.id.desc())
                       .limit(8)
                       .all())

    import os as _os
    spaces_configured = bool(_os.environ.get("SPACES_KEY"))

    return render_template('admin_dashboard.html',
                           spaces_configured=spaces_configured,
                           total_users=total_users,
                           total_customers=total_customers,
                           total_haulers=total_haulers,
                           total_jobs=total_jobs,
                           open_jobs=open_jobs,
                           active_jobs=active_jobs,
                           completed_jobs=completed_jobs,
                           cancelled_jobs=cancelled_jobs,
                           total_bids=total_bids,
                           accepted_bids=accepted_bids,
                           pending_bids=pending_bids,
                           total_revenue=total_revenue,
                           jobs=jobs,
                           recent_accepted=recent_accepted,
                           pending_users=pending_users)

@app.route("/admin/customers")
@require_admin
def admin_customers():
    customers = User.query.filter_by(user_type='customer').order_by(User.created_at.desc()).all()
    total = len(customers)
    jobs_map = {}
    for c in customers:
        jobs_map[c.id] = Job.query.filter_by(customer_id=c.id).count()
    return render_template('admin_customers.html',
                           customers=customers,
                           total=total,
                           jobs_map=jobs_map)


@app.route("/admin/haulers")
@require_admin
def admin_haulers():
    haulers = User.query.filter_by(user_type='hauler').order_by(User.created_at.desc()).all()
    total = len(haulers)
    setup_count = sum(1 for h in haulers if h.home_zip and h.max_travel_miles)
    completed_map = {}
    bid_map = {}
    rating_map = {}
    for h in haulers:
        completed_map[h.id] = Job.query.filter_by(accepted_hauler_id=h.id, status='completed').count()
        bid_map[h.id] = Bid.query.filter_by(hauler_id=h.id).count()
        revs = Review.query.filter_by(hauler_id=h.id).all()
        rating_map[h.id] = round(sum(r.rating for r in revs) / len(revs), 1) if revs else None
    return render_template('admin_haulers.html',
                           haulers=haulers,
                           total=total,
                           setup_count=setup_count,
                           completed_map=completed_map,
                           bid_map=bid_map,
                           rating_map=rating_map)


@app.route("/admin/test-job", methods=["POST"])
@require_admin
def admin_test_job():
    customer_name = request.form.get("customer_name", "").strip()
    pickup_address = request.form.get("pickup_address", "").strip()
    pickup_zip = request.form.get("pickup_zip", "").strip()
    preferred_date = request.form.get("preferred_date", "").strip()
    preferred_time = request.form.get("preferred_time", "").strip()
    job_description = request.form.get("job_description", "").strip()

    if not customer_name or not pickup_address or not job_description or not pickup_zip:
        flash("Missing required fields for test job.", "error")
        return redirect(url_for('admin_dashboard'))

    job = Job(
        customer_id=current_user.id,
        customer_name=customer_name,
        pickup_address=pickup_address,
        pickup_zip=pickup_zip,
        preferred_date=preferred_date if preferred_date else None,
        preferred_time=preferred_time if preferred_time else None,
        job_description=job_description,
        status='open'
    )
    db.session.add(job)
    db.session.commit()

    from storage import upload_file as _upload_file
    photos = request.files.getlist("photos")
    for photo in photos:
        if photo and photo.filename:
            ext = os.path.splitext(photo.filename)[1]
            photo_data, photo_ct = _read_photo_bytes(photo, ext)
            filename, storage_url = _upload_file(photo, ext)
            photo_record = JobPhoto(
                job_id=job.id, filename=filename, storage_url=storage_url,
                data=photo_data if not storage_url else None, content_type=photo_ct,
            )
            db.session.add(photo_record)
    db.session.commit()

    if pickup_zip:
        from models import ZipCode
        from distance import haversine_miles
        job_zip_loc = ZipCode.query.get(pickup_zip)
        if job_zip_loc:
            haulers = User.query.filter(
                User.user_type == 'hauler',
                User.home_zip.isnot(None),
                User.max_travel_miles.isnot(None),
                User.email.isnot(None),
                User.notify_new_jobs == True
            ).all()

            for hauler in haulers:
                try:
                    hauler_zip_loc = ZipCode.query.get(hauler.home_zip)
                    if hauler_zip_loc:
                        distance_miles = haversine_miles(hauler_zip_loc.lat, hauler_zip_loc.lon, job_zip_loc.lat, job_zip_loc.lon)
                        if distance_miles <= hauler.max_travel_miles:
                            notify_hauler_new_job_nearby(hauler.email, job.id, job_description, distance_miles)
                            if hauler.notify_sms and hauler.phone:
                                notify_hauler_new_job_sms(hauler.phone, job.id, distance_miles)
                except:
                    pass

    flash("Test job posted successfully!", "success")
    return redirect(url_for('admin_dashboard'))

@app.route("/admin/test-bid/<int:job_id>", methods=["POST"])
@require_admin
def admin_test_bid(job_id):
    job = Job.query.get_or_404(job_id)
    if job.status not in ['open', 'bidding']:
        flash("Can only bid on open jobs.", "error")
        return redirect(url_for('admin_dashboard'))

    hauler_name = request.form.get("hauler_name", "Test Hauler").strip()
    quote_amount = request.form.get("quote_amount", "150").strip()
    message = request.form.get("message", "").strip()

    try:
        quote_amount = float(quote_amount)
    except ValueError:
        flash("Invalid quote amount.", "error")
        return redirect(url_for('admin_dashboard'))

    if job.status == 'open':
        job.status = 'bidding'

    bid = Bid(
        job_id=job_id,
        hauler_id=current_user.id,
        hauler_name=hauler_name,
        hauler_phone=current_user.phone or '',
        quote_amount=quote_amount,
        message=message if message else None,
        status='active'
    )
    db.session.add(bid)
    db.session.commit()

    if job.customer and job.customer.email:
        notify_customer_new_bid(job.customer.email, job.id, hauler_name, quote_amount)

    flash(f"Test bid of ${quote_amount:.2f} submitted on Job #{job_id}!", "success")
    return redirect(url_for('admin_dashboard'))

@app.route("/admin/test-email", methods=["POST"])
@require_admin
def admin_test_email():
    email = request.form.get("email", "").strip()
    notification_type = request.form.get("notification_type", "").strip()

    if not email or not notification_type:
        flash("Email and notification type are required.", "error")
        return redirect(url_for('admin_dashboard'))

    success = False
    if notification_type == "new_bid":
        success = notify_customer_new_bid(email, 999, "Test Hauler", 150.00)
    elif notification_type == "bid_accepted_confirm":
        success = notify_customer_bid_accepted_confirm(email, 999, "Test Hauler", 150.00)
    elif notification_type == "customer_job_completed":
        success = notify_customer_job_completed(email, 999)
    elif notification_type == "bid_accepted":
        success = notify_hauler_bid_accepted(email, 999, 150.00)
    elif notification_type == "bid_rejected":
        success = notify_hauler_bid_rejected(email, 999)
    elif notification_type == "deposit_paid":
        success = notify_hauler_deposit_paid(email, 999, "123 Test Street, Minneapolis", "55401")
    elif notification_type == "hauler_job_cancelled":
        success = notify_hauler_job_cancelled(email, 999, "Test Customer")
    elif notification_type == "new_job_nearby":
        success = notify_hauler_new_job_nearby(email, 999, "Old couch, dresser, and misc junk removal", 5.0)
    elif notification_type == "admin_new_customer":
        success = notify_admin_new_customer("Test Customer", email)
    elif notification_type == "admin_new_hauler":
        success = notify_admin_new_hauler("Test Hauler", email, "55401", "Pickup Truck")
    elif notification_type == "admin_new_job":
        success = notify_admin_new_job(999, "Test Customer", "55401", "Old couch, mattress, and misc junk")
    elif notification_type == "admin_new_bid":
        success = notify_admin_new_bid(999, "Test Hauler", 175.00)
    elif notification_type == "admin_bid_accepted":
        success = notify_admin_bid_accepted(999, "Test Customer", "Test Hauler", 175.00)
    elif notification_type == "admin_deposit_paid":
        success = notify_admin_deposit_paid(999, "Test Customer", "Test Hauler", 175.00)
    elif notification_type == "admin_job_completed":
        success = notify_admin_job_completed(999, "Test Customer", "Test Hauler", 175.00)
    elif notification_type == "admin_job_cancelled":
        success = notify_admin_job_cancelled(999, "Test Customer")
    elif notification_type == "admin_user_deleted":
        success = notify_admin_user_deleted("Test User", email, "customer")

    if success:
        flash(f"Test email sent to {email}! Check the Notification Log to confirm delivery.", "success")
    else:
        flash(f"Failed to send to {email}. SENDGRID_API_KEY may not be set — check Notification Log for details.", "error")

    return redirect(url_for('admin_dashboard'))

@app.route("/admin/notifications")
@require_admin
def admin_notifications():
    from models import NotificationLog
    logs = NotificationLog.query.order_by(NotificationLog.created_at.desc()).limit(500).all()
    sent = sum(1 for l in logs if l.status == 'sent')
    failed = sum(1 for l in logs if l.status == 'failed')
    import os
    sendgrid_configured = bool(os.environ.get("SENDGRID_API_KEY"))
    return render_template('admin_notifications.html',
                           logs=logs, sent=sent, failed=failed,
                           sendgrid_configured=sendgrid_configured)


@app.route("/admin/delete-job/<int:job_id>", methods=["POST"])
@require_admin
def admin_delete_job(job_id):
    job = Job.query.get_or_404(job_id)
    db.session.delete(job)
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route("/admin/delete-user/<string:user_id>", methods=["POST"])
@require_admin
def admin_delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash("Admin accounts cannot be deleted.", "error")
        return redirect(url_for('admin_dashboard'))
    from models import OAuth, JobPhoto, CompletionPhoto
    user_name = (((user.first_name or '') + ' ' + (user.last_name or '')).strip()
                 or user.email or 'User')
    user_type = user.user_type or 'customer'
    # Clean up customer jobs and their photos
    customer_jobs = Job.query.filter_by(customer_id=user_id).all()
    for job in customer_jobs:
        JobPhoto.query.filter_by(job_id=job.id).delete()
        CompletionPhoto.query.filter_by(job_id=job.id).delete()
        Bid.query.filter_by(job_id=job.id).delete()
        Review.query.filter_by(job_id=job.id).delete()
        db.session.delete(job)
    # Detach hauler from any jobs they were assigned to
    hauler_jobs = Job.query.filter_by(accepted_hauler_id=user_id).all()
    for job in hauler_jobs:
        job.accepted_hauler_id = None
        job.accepted_quote = None
        job.status = 'open'
        job.deposit_paid = False
    # Clean up hauler-specific records
    Bid.query.filter_by(hauler_id=user_id).delete()
    Review.query.filter_by(hauler_id=user_id).delete()
    Review.query.filter_by(customer_id=user_id).delete()
    OAuth.query.filter_by(user_id=user_id).delete()
    db.session.delete(user)
    db.session.commit()
    flash(f"{user_name}'s account has been deleted.", "success")
    if user_type == 'hauler':
        return redirect(url_for('admin_haulers'))
    return redirect(url_for('admin_customers'))


@app.route("/admin/analytics")
@require_admin
def admin_analytics():
    from datetime import timedelta
    import json
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    # ── Visitor stats ──────────────────────────────────────────────────────────
    total_views = PageView.query.count()
    unique_visitors = db.session.execute(
        db.text("SELECT COUNT(DISTINCT visitor_id) FROM page_views")
    ).scalar() or 0
    returning_visitors = db.session.execute(
        db.text("SELECT COUNT(*) FROM (SELECT visitor_id FROM page_views GROUP BY visitor_id HAVING COUNT(*) > 3) x")
    ).scalar() or 0
    today_views = PageView.query.filter(PageView.created_at >= today).count()
    week_views  = PageView.query.filter(PageView.created_at >= week_ago).count()
    month_views = PageView.query.filter(PageView.created_at >= month_ago).count()

    # Daily traffic last 30 days
    _dt = db.session.execute(
        db.text("SELECT DATE(created_at) AS d, COUNT(*) AS c FROM page_views WHERE created_at >= :s GROUP BY DATE(created_at) ORDER BY d"),
        {"s": month_ago}
    ).fetchall()
    daily_traffic_labels = [str(r[0]) for r in _dt]
    daily_traffic_values = [r[1] for r in _dt]

    # Top pages
    page_traffic = db.session.execute(
        db.text("SELECT path, COUNT(*) AS c FROM page_views GROUP BY path ORDER BY c DESC LIMIT 15")
    ).fetchall()

    # Device split
    _dev = db.session.execute(
        db.text("SELECT COALESCE(device_type,'unknown'), COUNT(*) FROM page_views GROUP BY device_type")
    ).fetchall()
    device_labels = [r[0] for r in _dev]
    device_values = [r[1] for r in _dev]

    # Referrers
    referrers = db.session.execute(
        db.text("""SELECT referrer, COUNT(*) AS c FROM page_views
                   WHERE referrer IS NOT NULL AND referrer <> ''
                   GROUP BY referrer ORDER BY c DESC LIMIT 10""")
    ).fetchall()

    # ── User stats ─────────────────────────────────────────────────────────────
    total_customers = User.query.filter_by(user_type='customer').count()
    total_haulers   = User.query.filter_by(user_type='hauler').count()
    new_today = User.query.filter(User.created_at >= today).count()
    new_week  = User.query.filter(User.created_at >= week_ago).count()
    active_users = db.session.execute(
        db.text("SELECT COUNT(DISTINCT user_id) FROM page_views WHERE user_id IS NOT NULL AND created_at >= :s"),
        {"s": week_ago}
    ).scalar() or 0

    _ds = db.session.execute(
        db.text("SELECT DATE(created_at) AS d, COUNT(*) AS c FROM users WHERE created_at >= :s GROUP BY DATE(created_at) ORDER BY d"),
        {"s": month_ago}
    ).fetchall()
    daily_signup_labels = [str(r[0]) for r in _ds]
    daily_signup_values = [r[1] for r in _ds]

    # ── Marketplace stats ──────────────────────────────────────────────────────
    total_jobs     = Job.query.count()
    open_jobs      = Job.query.filter_by(status='open').count()
    active_jobs    = Job.query.filter(Job.status.in_(['accepted','deposit_paid'])).count()
    completed_jobs = Job.query.filter_by(status='completed').count()
    cancelled_jobs = Job.query.filter_by(status='cancelled').count()
    total_bids     = Bid.query.count()
    bids_accepted  = db.session.execute(
        db.text("SELECT COUNT(*) FROM jobs WHERE status NOT IN ('open','cancelled') AND accepted_hauler_id IS NOT NULL")
    ).scalar() or 0
    total_revenue = db.session.query(db.func.sum(Job.accepted_quote)).filter(Job.status=='completed').scalar() or 0

    # Job status chart
    job_status_labels = ['Open','Active','Completed','Cancelled']
    job_status_values = [open_jobs, active_jobs, completed_jobs, cancelled_jobs]

    # Daily jobs last 30 days
    _dj = db.session.execute(
        db.text("SELECT DATE(created_at) AS d, COUNT(*) AS c FROM jobs WHERE created_at >= :s GROUP BY DATE(created_at) ORDER BY d"),
        {"s": month_ago}
    ).fetchall()
    daily_job_labels  = [str(r[0]) for r in _dj]
    daily_job_values  = [r[1] for r in _dj]

    # Top haulers
    top_haulers = db.session.execute(
        db.text("""
            SELECT u.first_name, u.last_name, u.email,
                   COUNT(DISTINCT b.id)                                          AS bids,
                   COUNT(DISTINCT CASE WHEN j.status='completed' THEN j.id END) AS completed,
                   ROUND(AVG(r.rating)::numeric, 1)                             AS avg_rating
            FROM users u
            LEFT JOIN bids   b ON b.hauler_id = u.id
            LEFT JOIN jobs   j ON j.accepted_hauler_id = u.id
            LEFT JOIN reviews r ON r.hauler_id = u.id
            WHERE u.user_type = 'hauler'
            GROUP BY u.id, u.first_name, u.last_name, u.email
            ORDER BY completed DESC, bids DESC
            LIMIT 10
        """)
    ).fetchall()

    # Top areas
    top_areas = db.session.execute(
        db.text("""
            SELECT j.pickup_zip, z.city, z.state, COUNT(*) AS cnt
            FROM jobs j
            LEFT JOIN zip_codes z ON z.zip = j.pickup_zip
            WHERE j.pickup_zip IS NOT NULL
            GROUP BY j.pickup_zip, z.city, z.state
            ORDER BY cnt DESC LIMIT 10
        """)
    ).fetchall()

    # Activity feed
    recent_users = User.query.order_by(User.created_at.desc()).limit(8).all()
    recent_jobs  = Job.query.order_by(Job.id.desc()).limit(8).all()
    recent_bids  = Bid.query.order_by(Bid.id.desc()).limit(8).all()

    # ── Service Area Analytics ──────────────────────────────────────────────────
    sa_days = request.args.get('days', '30')
    try:
        sa_days_int = int(sa_days)
    except (ValueError, TypeError):
        sa_days_int = 30
        sa_days = '30'
    sa_since = (today - timedelta(days=sa_days_int)) if sa_days_int > 0 else None

    # ZIP-level area stats: jobs, bids, accepted, completed, avg quote
    area_zip_stats = db.session.execute(db.text("""
        SELECT
            j.pickup_zip,
            COALESCE(z.city, 'Unknown')  AS city,
            COALESCE(z.state, '')        AS state,
            COUNT(DISTINCT j.id)         AS total_jobs,
            COUNT(DISTINCT b.id)         AS total_bids,
            COUNT(DISTINCT CASE WHEN j.status NOT IN ('open','bidding','cancelled')
                                 AND j.accepted_hauler_id IS NOT NULL
                            THEN j.id END)                                           AS accepted_jobs,
            COUNT(DISTINCT CASE WHEN j.status='completed' THEN j.id END)            AS completed_jobs,
            ROUND(AVG(CASE WHEN j.status='completed' AND j.accepted_quote IS NOT NULL
                           THEN j.accepted_quote END)::numeric, 0)                  AS avg_quote
        FROM jobs j
        LEFT JOIN zip_codes z ON z.zip = j.pickup_zip
        LEFT JOIN bids b      ON b.job_id = j.id
        WHERE j.pickup_zip IS NOT NULL
          AND (:since IS NULL OR j.created_at >= :since)
        GROUP BY j.pickup_zip, z.city, z.state
        ORDER BY total_jobs DESC
        LIMIT 50
    """), {"since": sa_since}).fetchall()

    # City-level stats
    area_city_stats = db.session.execute(db.text("""
        SELECT
            COALESCE(z.city, 'Unknown')  AS city,
            COALESCE(z.state, '')        AS state,
            COUNT(DISTINCT j.id)         AS total_jobs,
            COUNT(DISTINCT b.id)         AS total_bids,
            COUNT(DISTINCT CASE WHEN j.status='completed' THEN j.id END) AS completed,
            ROUND(AVG(CASE WHEN j.status='completed' AND j.accepted_quote IS NOT NULL
                           THEN j.accepted_quote END)::numeric, 0) AS avg_quote
        FROM jobs j
        LEFT JOIN zip_codes z ON z.zip = j.pickup_zip
        LEFT JOIN bids b      ON b.job_id = j.id
        WHERE j.pickup_zip IS NOT NULL
          AND (:since IS NULL OR j.created_at >= :since)
        GROUP BY z.city, z.state
        ORDER BY total_jobs DESC
        LIMIT 20
    """), {"since": sa_since}).fetchall()

    # Underserved areas: jobs with fewer than 2 bids
    underserved_areas = db.session.execute(db.text("""
        SELECT
            j.pickup_zip,
            COALESCE(z.city, 'Unknown')  AS city,
            COALESCE(z.state, '')        AS state,
            COUNT(DISTINCT j.id)         AS total_jobs,
            COUNT(DISTINCT b.id)         AS total_bids
        FROM jobs j
        LEFT JOIN zip_codes z ON z.zip = j.pickup_zip
        LEFT JOIN bids b      ON b.job_id = j.id
        WHERE j.pickup_zip IS NOT NULL AND j.status != 'cancelled'
          AND (:since IS NULL OR j.created_at >= :since)
        GROUP BY j.pickup_zip, z.city, z.state
        HAVING COUNT(DISTINCT b.id) < 2
        ORDER BY total_jobs DESC, total_bids ASC
        LIMIT 25
    """), {"since": sa_since}).fetchall()

    # Hauler coverage: where each hauler is based and their radius (all-time)
    hauler_coverage = db.session.execute(db.text("""
        SELECT
            TRIM(COALESCE(u.first_name,'') || ' ' || COALESCE(u.last_name,'')) AS name,
            u.email,
            COALESCE(u.home_zip, '—')            AS home_zip,
            COALESCE(z.city, '—')                AS home_city,
            COALESCE(u.max_travel_miles, 0)      AS miles,
            COUNT(DISTINCT b.id)                 AS total_bids,
            COUNT(DISTINCT CASE WHEN j.status='completed' THEN j.id END) AS completed
        FROM users u
        LEFT JOIN zip_codes z ON z.zip = u.home_zip
        LEFT JOIN bids b      ON b.hauler_id = u.id
        LEFT JOIN jobs j      ON j.accepted_hauler_id = u.id
        WHERE u.user_type = 'hauler'
        GROUP BY u.id, u.first_name, u.last_name, u.email,
                 u.home_zip, z.city, u.max_travel_miles
        ORDER BY total_bids DESC, miles DESC
    """)).fetchall()

    # Where haulers are concentrated (ZIP clusters)
    hauler_zip_dist = db.session.execute(db.text("""
        SELECT
            u.home_zip,
            COALESCE(z.city, 'Unknown')                                    AS city,
            COUNT(*)                                                        AS hauler_count,
            ROUND(AVG(COALESCE(u.max_travel_miles, 0))::numeric, 0)       AS avg_miles
        FROM users u
        LEFT JOIN zip_codes z ON z.zip = u.home_zip
        WHERE u.user_type = 'hauler' AND u.home_zip IS NOT NULL
        GROUP BY u.home_zip, z.city
        ORDER BY hauler_count DESC
        LIMIT 15
    """)).fetchall()

    # Summary metrics for stat cards
    active_zip_count   = len(area_zip_stats)
    active_city_count  = len([r for r in area_city_stats if r[0] and r[0] != 'Unknown'])
    underserved_count  = len(underserved_areas)
    haulers_no_zone    = sum(1 for r in hauler_coverage if r[2] == '—' or r[4] == 0)
    covered_zip_count  = len(set(r[2] for r in hauler_coverage if r[2] and r[2] != '—'))

    # Chart data: top 10 ZIPs
    top_zip_chart  = area_zip_stats[:10]
    sa_zip_labels  = json.dumps([r[0] for r in top_zip_chart])
    sa_zip_jobs    = json.dumps([r[3] for r in top_zip_chart])
    sa_zip_bids    = json.dumps([r[4] for r in top_zip_chart])
    sa_zip_done    = json.dumps([r[6] for r in top_zip_chart])

    # Chart data: top 8 cities
    top_city_chart = area_city_stats[:8]
    sa_city_labels = json.dumps([r[0] for r in top_city_chart])
    sa_city_jobs   = json.dumps([r[2] for r in top_city_chart])
    sa_city_done   = json.dumps([r[4] for r in top_city_chart])

    # ── Explicit ZIP coverage (hauler_service_zips table) ───────────────────────
    explicit_zip_coverage = db.session.execute(db.text("""
        SELECT
            hsz.zip_code,
            COALESCE(z.city, 'Unknown')                                       AS city,
            COALESCE(z.state, '')                                             AS state,
            COUNT(DISTINCT hsz.hauler_id)                                     AS hauler_count,
            COUNT(DISTINCT j.id)                                              AS job_count,
            COUNT(DISTINCT b.id)                                              AS bid_count,
            COUNT(DISTINCT CASE WHEN j.status='completed' THEN j.id END)     AS completed
        FROM hauler_service_zips hsz
        LEFT JOIN zip_codes z ON z.zip = hsz.zip_code
        LEFT JOIN jobs j      ON j.pickup_zip = hsz.zip_code
        LEFT JOIN bids b      ON b.job_id = j.id
        GROUP BY hsz.zip_code, z.city, z.state
        ORDER BY hauler_count DESC, job_count DESC
        LIMIT 30
    """)).fetchall()

    # Supply surplus: ZIPs haulers explicitly cover but with low/no customer demand
    supply_surplus = [r for r in explicit_zip_coverage if r[3] >= 1 and r[4] < 2]

    # Total unique ZIPs in the explicit list
    total_explicit_zips = db.session.execute(
        db.text("SELECT COUNT(DISTINCT zip_code) FROM hauler_service_zips")
    ).scalar() or 0

    return render_template('admin_analytics.html',
        total_views=total_views, unique_visitors=unique_visitors,
        returning_visitors=returning_visitors, today_views=today_views,
        week_views=week_views, month_views=month_views,
        daily_traffic_labels=json.dumps(daily_traffic_labels),
        daily_traffic_values=json.dumps(daily_traffic_values),
        page_traffic=page_traffic,
        device_labels=json.dumps(device_labels),
        device_values=json.dumps(device_values),
        referrers=referrers,
        total_customers=total_customers, total_haulers=total_haulers,
        new_today=new_today, new_week=new_week, active_users=active_users,
        daily_signup_labels=json.dumps(daily_signup_labels),
        daily_signup_values=json.dumps(daily_signup_values),
        total_jobs=total_jobs, open_jobs=open_jobs, active_jobs=active_jobs,
        completed_jobs=completed_jobs, cancelled_jobs=cancelled_jobs,
        total_bids=total_bids, bids_accepted=bids_accepted,
        total_revenue=total_revenue,
        job_status_labels=json.dumps(job_status_labels),
        job_status_values=json.dumps(job_status_values),
        daily_job_labels=json.dumps(daily_job_labels),
        daily_job_values=json.dumps(daily_job_values),
        top_haulers=top_haulers, top_areas=top_areas,
        recent_users=recent_users, recent_jobs=recent_jobs, recent_bids=recent_bids,
        sa_days=sa_days, sa_days_int=sa_days_int,
        area_zip_stats=area_zip_stats,
        area_city_stats=area_city_stats,
        hauler_coverage=hauler_coverage,
        hauler_zip_dist=hauler_zip_dist,
        underserved_areas=underserved_areas,
        active_zip_count=active_zip_count,
        active_city_count=active_city_count,
        underserved_count=underserved_count,
        haulers_no_zone=haulers_no_zone,
        covered_zip_count=covered_zip_count,
        sa_zip_labels=sa_zip_labels, sa_zip_jobs=sa_zip_jobs,
        sa_zip_bids=sa_zip_bids, sa_zip_done=sa_zip_done,
        sa_city_labels=sa_city_labels, sa_city_jobs=sa_city_jobs,
        sa_city_done=sa_city_done,
        explicit_zip_coverage=explicit_zip_coverage,
        supply_surplus=supply_surplus,
        total_explicit_zips=total_explicit_zips,
    )


@app.route("/admin/analytics/export")
@require_admin
def admin_analytics_export():
    import csv, io
    from datetime import timedelta
    now = datetime.now()
    month_ago = (now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=30))

    out = io.StringIO()
    w = csv.writer(out)

    w.writerow(['JHE HAUL ANALYTICS EXPORT'])
    w.writerow([f'Generated: {now.strftime("%Y-%m-%d %H:%M")}'])
    w.writerow([])

    w.writerow(['VISITOR ANALYTICS'])
    w.writerow(['Metric', 'Value'])
    w.writerow(['Total Page Views', PageView.query.count()])
    w.writerow(['Unique Visitors', db.session.execute(db.text("SELECT COUNT(DISTINCT visitor_id) FROM page_views")).scalar() or 0])
    w.writerow(['Views Today', PageView.query.filter(PageView.created_at >= now.replace(hour=0,minute=0,second=0,microsecond=0)).count()])
    w.writerow(['Views This Week', PageView.query.filter(PageView.created_at >= now.replace(hour=0,minute=0,second=0,microsecond=0) - timedelta(days=7)).count()])
    w.writerow([])

    w.writerow(['DAILY TRAFFIC (Last 30 Days)', ''])
    w.writerow(['Date', 'Page Views'])
    for r in db.session.execute(db.text("SELECT DATE(created_at), COUNT(*) FROM page_views WHERE created_at >= :s GROUP BY DATE(created_at) ORDER BY 1"), {"s": month_ago}).fetchall():
        w.writerow([r[0], r[1]])
    w.writerow([])

    w.writerow(['TOP PAGES', ''])
    w.writerow(['Path', 'Views'])
    for r in db.session.execute(db.text("SELECT path, COUNT(*) AS c FROM page_views GROUP BY path ORDER BY c DESC LIMIT 20")).fetchall():
        w.writerow([r[0], r[1]])
    w.writerow([])

    w.writerow(['DEVICE SPLIT', ''])
    w.writerow(['Device', 'Views'])
    for r in db.session.execute(db.text("SELECT COALESCE(device_type,'unknown'), COUNT(*) FROM page_views GROUP BY device_type")).fetchall():
        w.writerow([r[0], r[1]])
    w.writerow([])

    w.writerow(['USER ANALYTICS', ''])
    w.writerow(['Metric', 'Value'])
    w.writerow(['Total Customers', User.query.filter_by(user_type='customer').count()])
    w.writerow(['Total Haulers', User.query.filter_by(user_type='hauler').count()])
    w.writerow(['New Today', User.query.filter(User.created_at >= now.replace(hour=0,minute=0,second=0,microsecond=0)).count()])
    w.writerow([])

    w.writerow(['MARKETPLACE ANALYTICS', ''])
    w.writerow(['Metric', 'Value'])
    w.writerow(['Total Jobs', Job.query.count()])
    w.writerow(['Total Bids', Bid.query.count()])
    w.writerow(['Completed Jobs', Job.query.filter_by(status='completed').count()])
    w.writerow(['Total Revenue', f"${db.session.query(db.func.sum(Job.accepted_quote)).filter(Job.status=='completed').scalar() or 0:.2f}"])
    w.writerow([])

    w.writerow(['TOP HAULERS', ''])
    w.writerow(['Name', 'Email', 'Bids', 'Completed', 'Avg Rating'])
    for r in db.session.execute(db.text("""
        SELECT u.first_name||' '||COALESCE(u.last_name,''), u.email,
               COUNT(DISTINCT b.id), COUNT(DISTINCT CASE WHEN j.status='completed' THEN j.id END),
               ROUND(AVG(r.rating)::numeric,1)
        FROM users u
        LEFT JOIN bids b ON b.hauler_id=u.id
        LEFT JOIN jobs j ON j.accepted_hauler_id=u.id
        LEFT JOIN reviews r ON r.hauler_id=u.id
        WHERE u.user_type='hauler'
        GROUP BY u.id, u.first_name, u.last_name, u.email
        ORDER BY 4 DESC LIMIT 20
    """)).fetchall():
        w.writerow([r[0], r[1], r[2], r[3], r[4] or 'N/A'])
    w.writerow([])

    w.writerow(['TOP AREAS', ''])
    w.writerow(['ZIP', 'City', 'State', 'Jobs'])
    for r in db.session.execute(db.text("""
        SELECT j.pickup_zip, z.city, z.state, COUNT(*) AS cnt
        FROM jobs j LEFT JOIN zip_codes z ON z.zip=j.pickup_zip
        WHERE j.pickup_zip IS NOT NULL
        GROUP BY j.pickup_zip,z.city,z.state ORDER BY cnt DESC LIMIT 20
    """)).fetchall():
        w.writerow([r[0], r[1] or '', r[2] or '', r[3]])

    out.seek(0)
    resp = make_response(out.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename=jhehaul_analytics_{now.strftime("%Y%m%d")}.csv'
    return resp


@app.route("/health")
def health():
    return "ok", 200


@app.route("/robots.txt")
def robots_txt():
    base = os.environ.get("APP_BASE_URL", "https://jhehaul.com").rstrip("/")
    content = "\n".join([
        "User-agent: *",
        "Allow: /",
        "Allow: /invite",
        "Allow: /invite/customer",
        "Allow: /invite/hauler",
        "Allow: /about",
        "Allow: /hauler-agreement",
        "Allow: /customer-terms",
        "",
        "Disallow: /admin",
        "Disallow: /admin/",
        "Disallow: /customer/",
        "Disallow: /hauler/",
        "Disallow: /auth/",
        "Disallow: /profile",
        "Disallow: /profile/",
        "Disallow: /checkout/",
        "Disallow: /uploads/",
        "Disallow: /choose-role",
        "Disallow: /set-role",
        "Disallow: /payment_success/",
        "Disallow: /account/",
        "",
        f"Sitemap: {base}/sitemap.xml",
    ])
    resp = make_response(content)
    resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.route("/sitemap.xml")
def sitemap_xml():
    root = os.path.abspath(os.path.dirname(__file__))
    return send_from_directory(root, "sitemap.xml", mimetype="application/xml")
