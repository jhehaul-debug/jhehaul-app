import os
import uuid
from datetime import datetime
from functools import wraps
from flask import session, redirect, url_for, request, send_from_directory, render_template, flash
from werkzeug.utils import secure_filename
from flask_login import current_user

from app import app, db, UPLOAD_FOLDER, choose_pay_link
from replit_auth import require_login, make_replit_blueprint
from models import User, Job, JobPhoto, Bid, CompletionPhoto, Review
from email_service import notify_customer_new_bid, notify_hauler_bid_accepted, notify_hauler_deposit_paid, notify_hauler_new_job_nearby
from sms_service import notify_hauler_new_job_sms, notify_hauler_bid_accepted_sms, notify_hauler_deposit_paid_sms, notify_customer_new_bid_sms

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
                return redirect(url_for('replit_auth.login'))
            if not current_user.user_type:
                return redirect(url_for('choose_role'))
            if current_user.user_type != role and not current_user.is_admin:
                return render_template('403.html'), 403
            if current_user.user_type == 'hauler' and not current_user.is_admin and not current_user.home_zip and request.endpoint not in ('hauler_setup', 'hauler_setup_save'):
                return redirect(url_for('hauler_setup'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def require_admin(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            session["next_url"] = request.url
            return redirect(url_for('replit_auth.login'))
        if not current_user.is_admin:
            return render_template('403.html'), 403
        return f(*args, **kwargs)
    return decorated_function

app.register_blueprint(make_replit_blueprint(), url_prefix="/auth")

@app.before_request
def make_session_permanent():
    session.permanent = True

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

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
        if not current_user.user_type:
            return redirect(url_for('choose_role'))
        if current_user.user_type == 'customer':
            return redirect(url_for('customer_jobs'))
        return redirect(url_for('hauler_jobs'))
    return render_template('invite_landing.html', role=role)

@app.route("/choose-role")
@require_login
def choose_role():
    if current_user.user_type:
        if current_user.user_type == 'customer':
            return redirect(url_for('customer_jobs'))
        else:
            return redirect(url_for('hauler_jobs'))
    invited_role = session.pop('invited_role', None)
    return render_template('choose_role.html', invited_role=invited_role)

@app.route("/set-role", methods=["POST"])
@require_login
def set_role():
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
        return redirect(url_for('customer_jobs'))
    return redirect(url_for('choose_role'))

@app.route("/hauler/setup")
@require_role('hauler')
def hauler_setup():
    if current_user.home_zip and current_user.max_travel_miles:
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
    db.session.commit()

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

    photos = request.files.getlist("photos")
    for photo in photos:
        if photo and photo.filename:
            ext = os.path.splitext(photo.filename)[1]
            filename = f"{uuid.uuid4().hex}{ext}"
            photo.save(os.path.join(UPLOAD_FOLDER, filename))
            photo_record = JobPhoto(job_id=job.id, filename=filename)
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
    if job.customer_id != current_user.id:
        return "Access denied", 403
    
    bids = Bid.query.filter_by(job_id=job_id).order_by(Bid.quote_amount.asc()).all()
    
    pay_link = None
    if job.status == "accepted" and not job.deposit_paid:
        pay_link = choose_pay_link(job.accepted_quote)
        if pay_link:
            domain = os.environ.get("REPLIT_DEV_DOMAIN", "")
            success_url = f"https://{domain}/payment_success/{job.id}"
            pay_link = f"{pay_link}?success_url={success_url}"
    
    return render_template('customer_job_detail.html', job=job, bids=bids, pay_link=pay_link)

@app.route("/customer/upload_photos/<int:job_id>", methods=["POST"])
@require_role('customer')
def customer_upload_photos(job_id):
    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    if job.status not in ['open', 'bidding', 'accepted', 'deposit_paid']:
        return "Cannot upload photos at this stage", 400

    photos = request.files.getlist("photos")
    for photo in photos:
        if photo and photo.filename:
            ext = os.path.splitext(photo.filename)[1]
            filename = f"{uuid.uuid4().hex}{ext}"
            photo.save(os.path.join(UPLOAD_FOLDER, filename))
            photo_record = JobPhoto(job_id=job.id, filename=filename)
            db.session.add(photo_record)

    db.session.commit()
    return redirect(url_for('customer_job_detail', job_id=job.id))

@app.route("/customer/accept_bid/<int:bid_id>", methods=["POST"])
@require_role('customer')
def customer_accept_bid(bid_id):
    bid = Bid.query.get_or_404(bid_id)
    job = Job.query.get_or_404(bid.job_id)
    
    if job.customer_id != current_user.id:
        return "Access denied", 403

    job.status = 'accepted'
    job.accepted_hauler = bid.hauler_name
    job.accepted_hauler_id = bid.hauler_id
    job.accepted_quote = bid.quote_amount
    
    bid.status = 'accepted'
    Bid.query.filter(Bid.job_id == job.id, Bid.id != bid_id).update({'status': 'rejected'})
    
    db.session.commit()
    
    hauler = User.query.get(bid.hauler_id)
    if hauler and hauler.email:
        notify_hauler_bid_accepted(hauler.email, job.id, bid.quote_amount)
        if hauler.notify_sms and hauler.phone:
            notify_hauler_bid_accepted_sms(hauler.phone, job.id)
    
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
    
    return redirect(url_for('customer_job_detail', job_id=job_id))

@app.route("/payment_success/<int:job_id>")
@require_role('customer')
def payment_success(job_id):
    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    return redirect(url_for('customer_job_detail', job_id=job_id))

@app.route("/hauler/jobs")
@require_role('hauler')
def hauler_jobs():
    from models import ZipCode
    from distance import haversine_miles

    all_jobs = Job.query.filter(Job.status.in_(['open', 'bidding'])).order_by(Job.id.desc()).all()

    job_distances = {}

    if current_user.home_zip and current_user.max_travel_miles:
        hauler_zip = ZipCode.query.get(current_user.home_zip)

        if hauler_zip:
            filtered_jobs = []
            for job in all_jobs:
                if job.pickup_zip:
                    job_zip = ZipCode.query.get(job.pickup_zip)
                    if job_zip:
                        miles = haversine_miles(hauler_zip.lat, hauler_zip.lon, job_zip.lat, job_zip.lon)
                        if miles <= current_user.max_travel_miles:
                            job_distances[job.id] = round(miles, 1)
                            filtered_jobs.append(job)
                    else:
                        filtered_jobs.append(job)
                else:
                    filtered_jobs.append(job)
            jobs = filtered_jobs
        else:
            jobs = all_jobs
    else:
        if current_user.home_zip:
            hauler_zip = ZipCode.query.get(current_user.home_zip)
            if hauler_zip:
                for job in all_jobs:
                    if job.pickup_zip:
                        job_zip = ZipCode.query.get(job.pickup_zip)
                        if job_zip:
                            miles = haversine_miles(hauler_zip.lat, hauler_zip.lon, job_zip.lat, job_zip.lon)
                            job_distances[job.id] = round(miles, 1)
        jobs = all_jobs

    return render_template('hauler_jobs.html', jobs=jobs, job_distances=job_distances)

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

    return render_template('bid_success.html')

@app.route("/hauler/dashboard")
@require_role('hauler')
def hauler_dashboard():
    jobs = Job.query.filter(
        Job.accepted_hauler_id == current_user.id,
        Job.status.in_(['accepted', 'deposit_paid', 'completed'])
    ).order_by(Job.id.desc()).all()
    return render_template('hauler_dashboard.html', jobs=jobs)

@app.route("/profile")
@require_login
def profile():
    return render_template('profile.html')

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
    
    OAuth.query.filter_by(user_id=user_id).delete()
    db.session.delete(current_user)
    db.session.commit()
    
    return redirect(url_for('index'))

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
    if job.status != 'deposit_paid':
        return "Cannot upload photos at this stage", 400
    
    if request.method == "POST":
        before_photos = request.files.getlist("before_photos")
        after_photos = request.files.getlist("after_photos")
        
        for photo in before_photos:
            if photo and photo.filename:
                ext = os.path.splitext(photo.filename)[1]
                filename = f"{uuid.uuid4().hex}{ext}"
                photo.save(os.path.join(UPLOAD_FOLDER, filename))
                photo_record = CompletionPhoto(job_id=job.id, filename=filename, photo_type='before')
                db.session.add(photo_record)
        
        for photo in after_photos:
            if photo and photo.filename:
                ext = os.path.splitext(photo.filename)[1]
                filename = f"{uuid.uuid4().hex}{ext}"
                photo.save(os.path.join(UPLOAD_FOLDER, filename))
                photo_record = CompletionPhoto(job_id=job.id, filename=filename, photo_type='after')
                db.session.add(photo_record)
        
        db.session.commit()
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
    total_revenue = db.session.query(db.func.sum(Job.accepted_quote)).filter(Job.status == 'completed').scalar() or 0

    jobs = Job.query.order_by(Job.id.desc()).all()
    users = User.query.order_by(User.created_at.desc()).all()

    return render_template('admin_dashboard.html',
                           total_users=total_users,
                           total_customers=total_customers,
                           total_haulers=total_haulers,
                           total_jobs=total_jobs,
                           open_jobs=open_jobs,
                           active_jobs=active_jobs,
                           completed_jobs=completed_jobs,
                           cancelled_jobs=cancelled_jobs,
                           total_bids=total_bids,
                           total_revenue=total_revenue,
                           jobs=jobs,
                           users=users)

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
    elif notification_type == "bid_accepted":
        success = notify_hauler_bid_accepted(email, 999, 150.00)
    elif notification_type == "deposit_paid":
        success = notify_hauler_deposit_paid(email, 999, "123 Test Street", "12345")
    elif notification_type == "new_job_nearby":
        success = notify_hauler_new_job_nearby(email, 999, "Test junk removal job", 5.0)

    if success:
        flash(f"Test email sent successfully to {email}!", "success")
    else:
        flash(f"Failed to send test email to {email}.", "error")

    return redirect(url_for('admin_dashboard'))

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
        return "Cannot delete admin", 400
    user_jobs = Job.query.filter_by(customer_id=user_id).all()
    for job in user_jobs:
        db.session.delete(job)
    hauler_jobs = Job.query.filter_by(accepted_hauler_id=user_id).all()
    for job in hauler_jobs:
        job.accepted_hauler_id = None
        job.accepted_hauler = None
        job.accepted_quote = None
        job.status = 'open'
        job.deposit_paid = False
    Bid.query.filter_by(hauler_id=user_id).delete()
    Review.query.filter_by(hauler_id=user_id).delete()
    Review.query.filter_by(customer_id=user_id).delete()
    from models import OAuth
    OAuth.query.filter_by(user_id=user_id).delete()
    db.session.delete(user)
    db.session.commit()
    return redirect(url_for('admin_dashboard'))
