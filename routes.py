import os
import uuid
import stripe
from datetime import datetime
from functools import wraps
from flask import session, redirect, url_for, request, send_from_directory, render_template, flash, make_response, g, abort, jsonify
from werkzeug.utils import secure_filename
from flask_login import current_user

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

from app import app, db, UPLOAD_FOLDER, choose_pay_link
from auth import require_login
from models import User, Job, JobPhoto, Bid, CompletionPhoto, Review, PageView, HaulerServiceZip, Quote, Message, Category, Listing, ListingPhoto, ListingReport, DeliveryRequest
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
    notify_customer_quote_received, notify_customer_deposit_confirmed,
    notify_customer_appointment_confirmed,
    notify_admin_new_request,
)
from sms_service import (
    notify_hauler_new_job_sms, notify_hauler_bid_accepted_sms,
    notify_hauler_deposit_paid_sms, notify_hauler_bid_rejected_sms,
    notify_hauler_job_cancelled_sms,
    notify_customer_new_bid_sms, notify_customer_job_completed_sms,
    notify_customer_quote_received_sms, notify_customer_deposit_confirmed_sms,
    notify_customer_appointment_confirmed_sms,
    notify_admin_sms, send_sms, send_verification_sms, get_sms_settings,
    notify_admin_new_customer_sms, notify_admin_new_hauler_sms,
    notify_admin_new_job_sms, notify_admin_bid_accepted_sms, notify_admin_new_bid_sms,
    notify_admin_new_request_sms,
)

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

@app.context_processor
def inject_globals():
    result = {'admin_unread_count': 0, 'customer_unread_count': 0,
              'admin_phone': os.environ.get('ADMIN_PHONE', '')}
    if current_user.is_authenticated:
        try:
            if current_user.is_admin:
                result['admin_unread_count'] = (Message.query
                    .join(User, Message.sender_id == User.id)
                    .filter(Message.read_at == None, User.is_admin == False)
                    .count())
            elif current_user.user_type == 'customer':
                job_unread = (Message.query
                    .join(Job, Message.job_id == Job.id)
                    .filter(Job.customer_id == current_user.id,
                            Message.sender_id != current_user.id,
                            Message.read_at == None)
                    .count())
                from models import ListingConversation, ListingMessage
                marketplace_unread = (ListingMessage.query
                    .join(ListingConversation,
                          ListingMessage.conversation_id == ListingConversation.id)
                    .filter(
                        db.or_(
                            ListingConversation.buyer_id == current_user.id,
                            ListingConversation.seller_id == current_user.id,
                        ),
                        ListingMessage.sender_id != current_user.id,
                        ListingMessage.read_at == None,
                    )
                    .count())
                result['customer_unread_count'] = job_unread + marketplace_unread
        except Exception:
            pass
    return result


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
    if ext not in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif', '.bmp', '.tiff'}:
        flash("Please upload a valid image file (JPG, PNG, WebP, or GIF).", "error")
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

def _marketplace_categories():
    from models import Category
    return (Category.query
            .filter_by(is_active=True, parent_id=None)
            .order_by(Category.display_order, Category.name)
            .all())


def _marketplace_homepage_ctx():
    """Build context dict for the marketplace homepage (no active filters)."""
    from models import Listing
    _base = Listing.query.filter(
        Listing.status.in_(['active', 'sold', 'reserved']),
        Listing.moderation_status == 'approved'
    )
    _active = Listing.query.filter_by(status='active', moderation_status='approved')
    recent = (_base.filter(Listing.listing_type == 'item')
              .order_by(Listing.created_at.desc()).limit(8).all())
    free_items = (_active.filter_by(price_type='free')
                  .filter(Listing.listing_type == 'item')
                  .order_by(Listing.created_at.desc()).limit(8).all())
    featured = (_active.filter_by(featured=True)
                .order_by(Listing.created_at.desc()).limit(8).all())
    for_sale = (_base.filter(Listing.listing_type == 'property_sale')
                .order_by(Listing.created_at.desc()).limit(6).all())
    rentals  = (_base.filter(Listing.listing_type == 'rental')
                .order_by(Listing.created_at.desc()).limit(6).all())
    return dict(recent_listings=recent, free_listings=free_items,
                featured_listings=featured, for_sale_listings=for_sale,
                rental_listings=rentals)


@app.route("/")
def home():
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for('admin_dashboard'))
        if not current_user.user_type:
            return redirect(url_for('choose_role'))
        categories = _marketplace_categories()
        ctx = _marketplace_homepage_ctx()
        return render_template('marketplace.html', categories=categories, is_search=False, **ctx)
    # Logged-out visitors see the marketing landing page
    return redirect(url_for('landing'))


# Twin Cities metro cities (expansion-ready: add more cities later)
_TWIN_CITIES_CITIES = frozenset({
    'minneapolis', 'saint paul', 'st. paul', 'bloomington', 'plymouth',
    'brooklyn park', 'maple grove', 'eagan', 'woodbury', 'coon rapids',
    'apple valley', 'edina', 'burnsville', 'minnetonka', 'saint louis park',
    'st. louis park', 'shakopee', 'maplewood', 'roseville', 'blaine',
    'eden prairie', 'lakeville', 'richfield', 'inver grove heights', 'fridley',
    'shoreview', 'brooklyn center', 'crystal', 'robbinsdale', 'hopkins',
    'golden valley', 'new hope', 'columbia heights', 'new brighton', 'arden hills',
    'rosemount', 'farmington', 'prior lake', 'chanhassen', 'chaska', 'savage',
    'stillwater', 'white bear lake', 'cottage grove', 'oakdale', 'mendota heights',
    'vadnais heights', 'mounds view', 'little canada', 'champlin', 'andover',
    'ham lake', 'circle pines', 'hugo', 'south st. paul', 'west st. paul',
    'north st. paul', 'newport', 'falcon heights', 'lauderdale', 'elk river',
    'rogers', 'osseo', 'dayton', 'medina', 'wayzata', 'excelsior',
    'tonka bay', 'deephaven', 'shorewood', 'victoria', 'carver', 'jordan',
    'spring lake park', 'st. anthony', 'mahtomedi', 'lake elmo', 'bayport',
    'st. paul park', 'lilydale', 'mendota', 'sunfish lake', 'pine springs',
})


@app.route("/marketplace")
def marketplace():
    from models import Category, Listing
    categories = _marketplace_categories()

    q                  = request.args.get('q',            '').strip()
    category_slug      = request.args.get('category',     '').strip()
    price_type_filter  = request.args.get('price_type',   '').strip()
    featured_filter    = request.args.get('featured',     '').strip()
    listing_type_filter= request.args.get('listing_type', '').strip()
    area_filter        = request.args.get('area',         '').strip()
    min_price_raw      = request.args.get('min_price',    '').strip()
    max_price_raw      = request.args.get('max_price',    '').strip()
    min_beds_raw       = request.args.get('min_beds',     '').strip()
    open_house_only    = request.args.get('open_house',   '').strip()
    hide_sold          = request.args.get('hide_sold',    '').strip()

    try: min_price = float(min_price_raw) if min_price_raw else None
    except ValueError: min_price = None
    try: max_price = float(max_price_raw) if max_price_raw else None
    except ValueError: max_price = None
    try: min_beds = float(min_beds_raw) if min_beds_raw else None
    except ValueError: min_beds = None

    is_search = bool(q or category_slug or price_type_filter or featured_filter
                     or listing_type_filter or area_filter or min_price is not None
                     or max_price is not None or min_beds is not None or open_house_only
                     or hide_sold)

    if is_search:
        if hide_sold:
            qobj = Listing.query.filter_by(status='active', moderation_status='approved')
        else:
            qobj = Listing.query.filter(
                Listing.status.in_(['active', 'sold', 'reserved']),
                Listing.moderation_status == 'approved'
            )

        if q:
            qobj = qobj.filter(
                db.or_(Listing.title.ilike(f'%{q}%'),
                       Listing.description.ilike(f'%{q}%'))
            )
        if category_slug:
            cat = Category.query.filter_by(slug=category_slug, is_active=True).first()
            if cat:
                # Match category OR any of its subcategory children
                child_ids = [c.id for c in cat.subcategories]
                if child_ids:
                    qobj = qobj.filter(
                        db.or_(Listing.category_id == cat.id,
                               Listing.category_id.in_(child_ids))
                    )
                else:
                    qobj = qobj.filter(Listing.category_id == cat.id)

        if price_type_filter in ('free', 'fixed', 'negotiable'):
            qobj = qobj.filter(Listing.price_type == price_type_filter)
        if featured_filter:
            qobj = qobj.filter(Listing.featured == True)

        # Listing type filter (item | property_sale | rental | housing)
        if listing_type_filter == 'housing':
            qobj = qobj.filter(Listing.listing_type.in_(['property_sale', 'rental']))
        elif listing_type_filter in ('item', 'property_sale', 'rental'):
            qobj = qobj.filter(Listing.listing_type == listing_type_filter)

        # Twin Cities area filter (MN + metro city list)
        if area_filter == 'twin-cities':
            qobj = qobj.filter(Listing.state == 'MN').filter(
                db.func.lower(Listing.city).in_(_TWIN_CITIES_CITIES)
            )

        # Property-specific numeric filters
        if min_price is not None:
            qobj = qobj.filter(Listing.price >= min_price)
        if max_price is not None:
            qobj = qobj.filter(Listing.price <= max_price)
        if min_beds is not None:
            qobj = qobj.filter(Listing.bedrooms >= min_beds)
        if open_house_only:
            from datetime import datetime as _now_dt
            qobj = qobj.filter(Listing.open_house_dt >= _now_dt.utcnow())

        search_results = qobj.order_by(Listing.featured.desc(), Listing.created_at.desc()).limit(48).all()
        active_category = Category.query.filter_by(slug=category_slug).first() if category_slug else None
        return render_template('marketplace.html',
                               categories=categories,
                               is_search=True,
                               search_query=q,
                               search_results=search_results,
                               active_category=active_category,
                               price_type_filter=price_type_filter,
                               featured_filter=featured_filter,
                               listing_type_filter=listing_type_filter,
                               area_filter=area_filter,
                               min_price=min_price, max_price=max_price,
                               min_beds=min_beds, open_house_only=open_house_only,
                               hide_sold=hide_sold,
                               recent_listings=[], free_listings=[], featured_listings=[],
                               for_sale_listings=[], rental_listings=[])
    else:
        ctx = _marketplace_homepage_ctx()
        return render_template('marketplace.html', categories=categories, is_search=False,
                               listing_type_filter='', area_filter='', hide_sold='', **ctx)


@app.route("/sell")
@require_login
def sell():
    """Entry point for selling — shows listing type chooser."""
    if not current_user.user_type and not current_user.is_admin:
        return redirect(url_for('choose_role'))
    return render_template('sell_choose.html')


# ── Listing / Sell Flow (Phase 4) ──────────────────────────────────────────────

# Whitelisted enum values — never accept arbitrary strings from form POSTs
_VALID_PRICE_TYPES   = frozenset({'fixed', 'negotiable', 'free'})
_VALID_CONDITIONS    = frozenset({'new', 'like_new', 'good', 'fair', 'for_parts'})
_VALID_DELIVERY_OPTS = frozenset({'pickup', 'seller_delivers', 'jhe_haul'})


def _check_listing_csrf():
    """Validate CSRF token for listing management POST endpoints.

    Checks the form field 'csrf_token' or the 'X-CSRFToken' request header.
    Aborts 400 on failure.  Not applied app-wide — existing routes are unaffected.
    """
    from flask_wtf.csrf import validate_csrf, ValidationError
    token = request.form.get('csrf_token') or request.headers.get('X-CSRFToken', '')
    try:
        validate_csrf(token)
    except ValidationError:
        abort(400, description="CSRF validation failed.")


def _listing_owner_or_403(listing_id):
    """Return listing record; abort 403 if requester isn't the seller or admin."""
    from models import Listing
    listing = Listing.query.get_or_404(listing_id)
    if not current_user.is_admin and listing.seller_id != current_user.id:
        abort(403)
    return listing


def _apply_listing_fields(listing, form):
    """
    Set listing fields from a submitted form, enforcing whitelists.
    Does NOT commit — caller decides when to commit or rollback.
    """
    listing.title = form.get('title', '').strip()[:200]
    listing.description = form.get('description', '').strip()

    cat_id = form.get('category_id', '').strip()
    listing.category_id = int(cat_id) if cat_id.isdigit() else None
    sub_id = form.get('subcategory_id', '').strip()
    raw_sub = int(sub_id) if sub_id.isdigit() else None

    # Validate subcategory belongs to selected category
    if raw_sub and listing.category_id:
        from models import Category as _Cat
        sub_obj = _Cat.query.get(raw_sub)
        listing.subcategory_id = raw_sub if (sub_obj and sub_obj.parent_id == listing.category_id) else None
    else:
        listing.subcategory_id = None

    price_type = form.get('price_type', 'fixed')
    listing.price_type = price_type if price_type in _VALID_PRICE_TYPES else 'fixed'

    if listing.price_type == 'free':
        listing.price = None
    else:
        price_str = form.get('price', '').strip()
        try:
            p = float(price_str)
            listing.price = p if p >= 0 else None
        except (ValueError, TypeError):
            listing.price = None

    listing.city     = form.get('city',     '').strip()[:100] or None
    listing.state    = form.get('state',    '').strip()[:50]  or None
    listing.zip_code = form.get('zip_code', '').strip()[:10]  or None
    if listing.zip_code:
        from models import ZipCode as _ZC
        zc = _ZC.query.get(listing.zip_code)
        if zc:
            listing.latitude  = zc.lat
            listing.longitude = zc.lon
            if not listing.city:  listing.city  = zc.city
            if not listing.state: listing.state = zc.state

    # Optional seller-set expiry date
    # Active/reserved listings must always have a future expiry to prevent perpetual listings.
    _exp = form.get('expires_at', '').strip()
    from datetime import datetime as _dt_exp, timedelta as _td_exp
    _now_exp = _dt_exp.now()
    if _exp:
        try:
            _parsed_exp = _dt_exp.strptime(_exp, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
            # Accept dates in the future and at most 365 days out
            if _now_exp < _parsed_exp <= _now_exp + _td_exp(days=365):
                listing.expires_at = _parsed_exp
            # Out-of-range: fall back to 30-day default rather than clearing
            elif listing.expires_at is None or listing.expires_at <= _now_exp:
                listing.expires_at = _now_exp + _td_exp(days=30)
        except ValueError:
            if listing.expires_at is None or listing.expires_at <= _now_exp:
                listing.expires_at = _now_exp + _td_exp(days=30)
    else:
        # Blank — enforce a 30-day default so active listings are never perpetual
        if listing.expires_at is None or listing.expires_at <= _now_exp:
            listing.expires_at = _now_exp + _td_exp(days=30)

    if listing.is_property:
        # Property-specific fields
        listing.property_type   = form.get('property_type',   '').strip() or None
        _lb = form.get('listed_by', 'owner').strip()
        listing.listed_by = _lb if _lb in ('owner', 'agent', 'builder') else 'owner'
        listing.property_address = form.get('property_address', '').strip()[:200] or None
        for _fld in ('bedrooms', 'bathrooms'):
            _v = form.get(_fld, '').strip()
            try:    setattr(listing, _fld, float(_v) if _v else None)
            except: setattr(listing, _fld, None)
        _sqft = form.get('sqft', '').strip()
        listing.sqft = int(_sqft) if _sqft.isdigit() else None
        _lot = form.get('lot_size', '').strip()
        listing.lot_size = _lot[:50] if _lot else None
        _yr = form.get('year_built', '').strip()
        try:
            _yr_int = int(_yr)
            listing.year_built = _yr_int if 1800 <= _yr_int <= 2030 else None
        except (ValueError, TypeError):
            listing.year_built = None
        listing.garage_parking = form.get('garage_parking', '').strip()[:100] or None
        for _fld in ('hoa_fee', 'property_tax_annual'):
            _v = form.get(_fld, '').strip()
            try:    setattr(listing, _fld, float(_v) if _v else None)
            except: setattr(listing, _fld, None)
        listing.amenities = form.get('amenities', '').strip() or None
        if listing.listing_type == 'rental':
            _rt = form.get('rent_terms', '').strip()
            listing.rent_terms = _rt if _rt in ('monthly', 'annual', 'weekly', 'short_term') else None
            listing.pets_allowed = form.get('pets_allowed') == '1'
            listing.utilities_included = form.get('utilities_included', '').strip()[:200] or None
        _odt = form.get('open_house_dt', '').strip()
        if _odt:
            try:
                from datetime import datetime as _dt3
                listing.open_house_dt = _dt3.fromisoformat(_odt)
            except ValueError:
                listing.open_house_dt = None
        else:
            listing.open_house_dt = None
    else:
        # Item-specific fields
        raw_condition = form.get('condition', '').strip()
        listing.condition = raw_condition if raw_condition in _VALID_CONDITIONS else None
        raw_opts = form.getlist('delivery_option')
        valid_opts = [o for o in raw_opts if o in _VALID_DELIVERY_OPTS]
        listing.delivery_option = ','.join(valid_opts) if valid_opts else None


def _validate_listing(listing, require_photos=False):
    """
    Validate a listing's current field values.
    Returns list of (human_message, wizard_step) tuples; empty = valid.
    """
    errors = []
    if not listing.title:
        errors.append(("Title is required.", 2))
    if listing.price_type not in _VALID_PRICE_TYPES:
        errors.append(("Invalid price type selected.", 3))
    elif listing.price_type in ('fixed', 'negotiable'):
        if listing.price is None or not (isinstance(listing.price, (int, float)) and listing.price >= 0):
            errors.append(("A valid price is required for fixed and negotiable listings.", 3))
    if require_photos:
        from models import ListingPhoto as _LP
        if not _LP.query.filter_by(listing_id=listing.id).first():
            errors.append(("At least one photo is required.", 1))
    return errors


@app.route("/listing/new")
@require_login
def listing_new():
    """Create a new draft listing and enter the wizard at step 1."""
    if not current_user.user_type and not current_user.is_admin:
        return redirect(url_for('choose_role'))
    from models import Listing
    lt = request.args.get('type', 'item')
    if lt not in ('item', 'property_sale', 'rental'):
        lt = 'item'
    draft = Listing(seller_id=current_user.id, title='', status='draft',
                    moderation_status='approved', listing_type=lt)
    db.session.add(draft)
    db.session.commit()
    return redirect(url_for('listing_step', listing_id=draft.id, step=1))


@app.route("/listing/<int:listing_id>/step/<int:step>", methods=["GET", "POST"])
@require_login
def listing_step(listing_id, step):
    """Multi-step listing creation wizard (steps 1–6)."""
    from models import Listing, Category
    listing = _listing_owner_or_403(listing_id)

    TOTAL_STEPS = 6
    step = max(1, min(step, TOTAL_STEPS))
    categories = (Category.query
                  .filter_by(is_active=True, parent_id=None)
                  .order_by(Category.display_order, Category.name)
                  .all())

    if request.method == "POST":
        _check_listing_csrf()
        if step == 1:
            # Photos are managed via AJAX; just advance.
            pass

        elif step == 2:
            listing.title = request.form.get('title', '').strip()[:200]
            listing.description = request.form.get('description', '').strip()

            if listing.is_property:
                # Property-specific details
                listing.property_type = request.form.get('property_type', '').strip() or None
                _lb = request.form.get('listed_by', 'owner').strip()
                listing.listed_by = _lb if _lb in ('owner', 'agent', 'builder') else 'owner'
                for _fld in ('bedrooms', 'bathrooms'):
                    _v = request.form.get(_fld, '').strip()
                    try:
                        setattr(listing, _fld, float(_v) if _v else None)
                    except (ValueError, TypeError):
                        setattr(listing, _fld, None)
                _sqft = request.form.get('sqft', '').strip()
                listing.sqft = int(_sqft) if _sqft.isdigit() else None
                _lot = request.form.get('lot_size', '').strip()
                listing.lot_size = _lot[:50] if _lot else None
                _yr = request.form.get('year_built', '').strip()
                try:
                    _yr_int = int(_yr)
                    listing.year_built = _yr_int if 1800 <= _yr_int <= 2030 else None
                except (ValueError, TypeError):
                    listing.year_built = None
                listing.garage_parking = request.form.get('garage_parking', '').strip()[:100] or None
                for _fld in ('hoa_fee', 'property_tax_annual'):
                    _v = request.form.get(_fld, '').strip()
                    try:
                        setattr(listing, _fld, float(_v) if _v else None)
                    except (ValueError, TypeError):
                        setattr(listing, _fld, None)
                listing.amenities = request.form.get('amenities', '').strip() or None
                if listing.listing_type == 'rental':
                    _rt = request.form.get('rent_terms', '').strip()
                    listing.rent_terms = _rt if _rt in ('monthly', 'annual', 'weekly', 'short_term') else None
                    listing.pets_allowed = request.form.get('pets_allowed') == '1'
                    listing.utilities_included = request.form.get('utilities_included', '').strip()[:200] or None
            else:
                # Item-specific details
                cat_id = request.form.get('category_id', '').strip()
                listing.category_id = int(cat_id) if cat_id.isdigit() else None
                sub_id = request.form.get('subcategory_id', '').strip()
                raw_sub = int(sub_id) if sub_id.isdigit() else None
                if raw_sub and listing.category_id:
                    from models import Category as _Cat2
                    sub_obj = _Cat2.query.get(raw_sub)
                    listing.subcategory_id = raw_sub if (sub_obj and sub_obj.parent_id == listing.category_id) else None
                else:
                    listing.subcategory_id = None
                raw_cond = request.form.get('condition', '').strip()
                listing.condition = raw_cond if raw_cond in _VALID_CONDITIONS else None

            if not listing.title:
                flash("A title is required.", "error")
                _tpl = 'property_wizard.html' if listing.is_property else 'listing_wizard.html'
                return render_template(_tpl, listing=listing,
                                       step=step, total_steps=TOTAL_STEPS, categories=categories)

        elif step == 3:
            price_type = request.form.get('price_type', 'fixed')
            listing.price_type = price_type if price_type in _VALID_PRICE_TYPES else 'fixed'
            if listing.price_type == 'free':
                listing.price = None
            else:
                price_str = request.form.get('price', '').strip()
                try:
                    p = float(price_str)
                    listing.price = p if p >= 0 else None
                except (ValueError, TypeError):
                    listing.price = None
                if listing.price is None:
                    flash("Please enter a valid price (0 or higher).", "error")
                    _tpl = 'property_wizard.html' if listing.is_property else 'listing_wizard.html'
                    return render_template(_tpl, listing=listing,
                                           step=step, total_steps=TOTAL_STEPS, categories=categories)

        elif step == 4:
            listing.city     = request.form.get('city',     '').strip()[:100] or None
            listing.state    = request.form.get('state',    '').strip()[:50]  or None
            listing.zip_code = request.form.get('zip_code', '').strip()[:10]  or None
            if listing.zip_code:
                from models import ZipCode as _ZC2
                zc = _ZC2.query.get(listing.zip_code)
                if zc:
                    listing.latitude  = zc.lat
                    listing.longitude = zc.lon
                    if not listing.city:  listing.city  = zc.city
                    if not listing.state: listing.state = zc.state
            if listing.is_property:
                listing.property_address = request.form.get('property_address', '').strip()[:200] or None

        elif step == 5:
            if listing.is_property:
                _odt = request.form.get('open_house_dt', '').strip()
                if _odt:
                    try:
                        from datetime import datetime as _dt2
                        listing.open_house_dt = _dt2.fromisoformat(_odt)
                    except ValueError:
                        listing.open_house_dt = None
                else:
                    listing.open_house_dt = None
            else:
                raw_opts = request.form.getlist('delivery_option')
                valid_opts = [o for o in raw_opts if o in _VALID_DELIVERY_OPTS]
                listing.delivery_option = ','.join(valid_opts) if valid_opts else None
            # Parse optional seller-set expiry date (shared by both item and property flows)
            _exp5 = request.form.get('expires_at', '').strip()
            if _exp5:
                try:
                    from datetime import datetime as _dt5, timedelta as _td5
                    _parsed5 = _dt5.strptime(_exp5, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
                    # Must be in the future and no more than 365 days out
                    _now5 = _dt5.now()
                    if _now5 < _parsed5 <= _now5 + _td5(days=365):
                        listing.expires_at = _parsed5
                    # else silently ignore out-of-range values
                except ValueError:
                    pass  # ignore malformed date
            else:
                listing.expires_at = None

        elif step == 6:
            # Publish — full server-side validation including photos
            pub_errors = _validate_listing(listing, require_photos=True)
            if pub_errors:
                first_step = min(s for _, s in pub_errors)
                for msg, _ in pub_errors:
                    flash(msg, "error")
                return redirect(url_for('listing_step', listing_id=listing_id, step=first_step))
            listing.status = 'active'
            # Set a default 30-day expiry if the seller didn't choose one
            if not listing.expires_at:
                from datetime import datetime as _dt_pub, timedelta as _td_pub
                listing.expires_at = _dt_pub.now() + _td_pub(days=30)
            db.session.commit()
            flash("Your listing is now live! 🎉", "success")
            return redirect(url_for('my_listings'))

        db.session.commit()

        if step < TOTAL_STEPS:
            return redirect(url_for('listing_step', listing_id=listing_id, step=step + 1))
        return redirect(url_for('listing_step', listing_id=listing_id, step=6))

    _tpl = 'property_wizard.html' if listing.is_property else 'listing_wizard.html'
    from datetime import datetime as _dt_now
    return render_template(_tpl,
                           listing=listing,
                           step=step,
                           total_steps=TOTAL_STEPS,
                           categories=categories,
                           now=_dt_now.now())


@app.route("/listing/<int:listing_id>/photo/upload", methods=["POST"])
@require_login
def listing_photo_upload(listing_id):
    """AJAX: upload a photo to a listing (max 10)."""
    _check_listing_csrf()
    from models import Listing, ListingPhoto
    listing = _listing_owner_or_403(listing_id)

    current_count = ListingPhoto.query.filter_by(listing_id=listing_id).count()
    if current_count >= 10:
        return jsonify(error='Maximum 10 photos allowed.'), 400

    photo = request.files.get('photo')
    if not photo or not photo.filename:
        return jsonify(error='No file received.'), 400

    ext = os.path.splitext(photo.filename)[1].lower()
    allowed = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif', '.bmp', '.tiff'}
    if ext not in allowed:
        return jsonify(error='Unsupported file type.'), 400

    data, ct = _read_photo_bytes(photo, ext)
    if len(data) > 15 * 1024 * 1024:
        return jsonify(error='Photo must be under 15 MB.'), 400

    from storage import upload_file as _upload_file
    filename, storage_url = _upload_file(photo, ext)

    is_primary = (current_count == 0)
    lp = ListingPhoto(
        listing_id=listing_id,
        filename=filename,
        storage_url=storage_url,
        data=data if not storage_url else None,
        content_type=ct,
        display_order=current_count,
        is_primary=is_primary,
    )
    db.session.add(lp)
    db.session.commit()

    photo_url = storage_url or url_for('serve_listing_photo', photo_id=lp.id)
    return jsonify(id=lp.id, url=photo_url, is_primary=lp.is_primary), 200


@app.route("/listing/<int:listing_id>/photo/<int:photo_id>/delete", methods=["POST"])
@require_login
def listing_photo_delete(listing_id, photo_id):
    """AJAX: remove a photo from a listing (DB row + stored file)."""
    _check_listing_csrf()
    from models import Listing, ListingPhoto
    _listing_owner_or_403(listing_id)
    photo = ListingPhoto.query.filter_by(id=photo_id, listing_id=listing_id).first_or_404()
    was_primary = photo.is_primary
    filename_to_delete = photo.filename
    db.session.delete(photo)
    db.session.flush()
    if was_primary:
        remaining = (ListingPhoto.query
                     .filter_by(listing_id=listing_id)
                     .order_by(ListingPhoto.display_order)
                     .first())
        if remaining:
            remaining.is_primary = True
    db.session.commit()
    # Delete the stored file after DB commit so we don't orphan it on rollback
    try:
        from storage import delete_file as _delete_file
        _delete_file(filename_to_delete)
    except Exception as exc:
        app.logger.warning("listing_photo_delete: storage cleanup failed: %s", exc)
    return jsonify(ok=True), 200


@app.route("/listing/<int:listing_id>/photo/<int:photo_id>/primary", methods=["POST"])
@require_login
def listing_photo_set_primary(listing_id, photo_id):
    """AJAX: set a photo as the primary/cover image."""
    _check_listing_csrf()
    from models import Listing, ListingPhoto
    _listing_owner_or_403(listing_id)
    ListingPhoto.query.filter_by(listing_id=listing_id).update({'is_primary': False})
    photo = ListingPhoto.query.filter_by(id=photo_id, listing_id=listing_id).first_or_404()
    photo.is_primary = True
    db.session.commit()
    return jsonify(ok=True), 200


@app.route("/listing/<int:listing_id>/photo/reorder", methods=["POST"])
@require_login
def listing_photo_reorder(listing_id):
    """AJAX: update display_order for listing photos."""
    _check_listing_csrf()
    from models import Listing, ListingPhoto
    _listing_owner_or_403(listing_id)
    order = (request.get_json() or {}).get('order', [])
    for i, pid in enumerate(order):
        ListingPhoto.query.filter_by(id=int(pid), listing_id=listing_id).update({'display_order': i})
    db.session.commit()
    return jsonify(ok=True), 200


@app.route("/listing/<int:listing_id>/edit", methods=["GET", "POST"])
@require_login
def listing_edit(listing_id):
    """Edit all fields of an existing listing on one page."""
    from models import Listing, Category
    listing = _listing_owner_or_403(listing_id)
    categories = (Category.query
                  .filter_by(is_active=True, parent_id=None)
                  .order_by(Category.display_order, Category.name)
                  .all())

    if request.method == "POST":
        _check_listing_csrf()
        # Apply all fields through whitelist helper (not yet committed)
        _apply_listing_fields(listing, request.form)

        # Validate — re-render the form (not redirect) to retain submitted values
        edit_errors = _validate_listing(listing, require_photos=False)
        if edit_errors:
            for msg, _ in edit_errors:
                flash(msg, "error")
            # listing object already has submitted values → template shows user's input
            db.session.rollback()
            # Reload with submitted values from form (rollback cleared in-session changes)
            _apply_listing_fields(listing, request.form)
            return render_template('listing_edit.html', listing=listing, categories=categories)

        db.session.commit()
        flash("Listing updated.", "success")
        return redirect(url_for('my_listings'))

    return render_template('listing_edit.html', listing=listing, categories=categories)


@app.route("/listing/<int:listing_id>/delete", methods=["POST"])
@require_login
def listing_delete(listing_id):
    """Delete a listing and all its photos (DB rows + stored files)."""
    _check_listing_csrf()
    from models import Listing, ListingPhoto
    listing = _listing_owner_or_403(listing_id)
    # Collect filenames before cascade-delete removes them from the session
    filenames = [p.filename for p in listing.photos if p.filename]
    db.session.delete(listing)
    db.session.commit()
    # Delete stored files after DB commit
    try:
        from storage import delete_file as _delete_file
        for fname in filenames:
            try:
                _delete_file(fname)
            except Exception as exc:
                app.logger.warning("listing_delete: storage cleanup failed for %s: %s", fname, exc)
    except Exception as exc:
        app.logger.warning("listing_delete: storage import failed: %s", exc)
    flash("Listing deleted.", "success")
    return redirect(url_for('my_listings'))


@app.route("/listing/<int:listing_id>/status", methods=["POST"])
@require_login
def listing_set_status(listing_id):
    """Allow a seller to mark their listing as sold, reserved, or active."""
    _check_listing_csrf()
    from models import Listing
    import datetime
    listing = _listing_owner_or_403(listing_id)
    new_status = request.form.get('status', '').strip()
    allowed = ('sold', 'reserved', 'active')
    if new_status not in allowed:
        flash("Invalid status.", "error")
        return redirect(url_for('my_listings'))
    # Only allow transitioning from sensible states
    if new_status == 'active' and listing.status not in ('sold', 'reserved', 'expired'):
        flash("Cannot reactivate a listing that is not sold, reserved, or expired.", "error")
        return redirect(url_for('my_listings'))
    if new_status in ('sold', 'reserved') and listing.status not in ('active', 'reserved', 'sold'):
        flash("Only active or sold/reserved listings can be updated.", "error")
        return redirect(url_for('my_listings'))
    prior_status = listing.status   # capture before overwriting
    listing.status = new_status
    if new_status == 'sold':
        listing.sold_at = datetime.datetime.utcnow()
    elif new_status == 'active':
        listing.sold_at = None
        if prior_status == 'expired':
            # Renewing from expired: always give a fresh 30-day window
            listing.expires_at = datetime.datetime.now() + datetime.timedelta(days=30)
            listing.expired_at = None
        elif not listing.expires_at or listing.expires_at <= datetime.datetime.now():
            # Reactivating from sold/reserved with no valid future expiry — reset to 30 days
            listing.expires_at = datetime.datetime.now() + datetime.timedelta(days=30)
    db.session.commit()
    labels = {'sold': 'Listing marked as sold.', 'reserved': 'Listing marked as reserved.', 'active': 'Listing reactivated.'}
    flash(labels[new_status], "success")
    return redirect(url_for('my_listings'))


@app.route("/my-listings")
@require_login
def my_listings():
    """Seller's dashboard: view and manage their own listings."""
    from models import Listing

    DRAFT_DISPLAY_CAP = 10  # show at most this many draft entries

    # Non-draft listings: show all
    non_drafts = (Listing.query
                  .filter(Listing.seller_id == current_user.id,
                          Listing.status != 'draft')
                  .order_by(Listing.created_at.desc())
                  .all())

    # Drafts: cap to the most recent N, track how many are hidden
    all_drafts = (Listing.query
                  .filter_by(seller_id=current_user.id, status='draft')
                  .order_by(Listing.created_at.desc())
                  .all())
    hidden_draft_count = max(0, len(all_drafts) - DRAFT_DISPLAY_CAP)
    visible_drafts = all_drafts[:DRAFT_DISPLAY_CAP]

    # Interleave: drafts first (most recent work-in-progress), then the rest
    listings = visible_drafts + non_drafts

    return render_template('my_listings.html', listings=listings,
                           hidden_draft_count=hidden_draft_count)


@app.route("/listing/<int:listing_id>")
def listing_detail(listing_id):
    """Individual listing detail page (Phase 5–6)."""
    from models import Listing, ListingFavorite

    listing = Listing.query.get_or_404(listing_id)

    # Access control: active/sold/reserved + approved listings are public.
    # Seller and admin can see any status.
    is_owner = current_user.is_authenticated and current_user.id == listing.seller_id
    is_admin = current_user.is_authenticated and current_user.is_admin
    is_public = (listing.status in ('active', 'sold', 'reserved') and listing.moderation_status == 'approved')
    if not (is_public or is_owner or is_admin):
        abort(404)

    # Increment view_count once per session (deduplicated)
    viewed_key = 'viewed_listings'
    viewed = session.get(viewed_key, [])
    if listing_id not in viewed:
        try:
            listing.view_count = (listing.view_count or 0) + 1
            db.session.commit()
            viewed.append(listing_id)
            session[viewed_key] = viewed
        except Exception:
            db.session.rollback()

    # Seller stats for sidebar
    from models import Listing as _L
    seller_active_count = _L.query.filter_by(
        seller_id=listing.seller_id, status='active', moderation_status='approved'
    ).count()
    seller_sold_count = _L.query.filter_by(
        seller_id=listing.seller_id, status='sold'
    ).count()

    # Favorite status
    is_favorited = False
    if current_user.is_authenticated:
        is_favorited = ListingFavorite.query.filter_by(
            user_id=current_user.id, listing_id=listing_id
        ).first() is not None

    # Ordered photos (primary first)
    photos = sorted(listing.photos, key=lambda p: (0 if p.is_primary else 1, p.display_order))

    # Buyer's existing offer on this listing (most recent)
    buyer_offer = None
    if current_user.is_authenticated and not is_owner and not is_admin:
        from models import ListingOffer
        buyer_offer = (ListingOffer.query
                       .filter_by(listing_id=listing_id, buyer_id=current_user.id)
                       .order_by(ListingOffer.created_at.desc())
                       .first())

    # Seller's incoming offers (owner view only, negotiable listings)
    seller_offers = []
    if is_owner and listing.price_type == 'negotiable':
        from models import ListingOffer
        seller_offers = (ListingOffer.query
                         .filter_by(listing_id=listing_id)
                         .filter(ListingOffer.status.in_(['pending', 'countered', 'accepted']))
                         .order_by(ListingOffer.created_at.desc())
                         .all())

    return render_template(
        'listing_detail.html',
        listing=listing,
        photos=photos,
        is_favorited=is_favorited,
        is_owner=is_owner,
        is_admin=is_admin,
        seller_active_count=seller_active_count,
        seller_sold_count=seller_sold_count,
        buyer_offer=buyer_offer,
        seller_offers=seller_offers,
    )


@app.route("/saved")
@require_login
def saved_items():
    """Show all listings the current user has saved/favorited."""
    from models import ListingFavorite, Listing
    favorites = (ListingFavorite.query
                 .filter_by(user_id=current_user.id)
                 .order_by(ListingFavorite.created_at.desc())
                 .all())
    items = []
    for fav in favorites:
        listing = Listing.query.get(fav.listing_id)
        if listing is None:
            continue
        # Apply the same visibility rule as listing_detail
        is_public = (listing.status in ('active', 'sold', 'reserved') and
                     listing.moderation_status == 'approved')
        is_owner  = current_user.id == listing.seller_id
        visible   = is_public or is_owner or current_user.is_admin
        items.append({'listing': listing, 'visible': visible, 'fav': fav})
    return render_template('saved_items.html', items=items)


@app.route("/listing/<int:listing_id>/favorite", methods=["POST"])
def listing_favorite_toggle(listing_id):
    """Toggle save/favorite for a listing. Requires login."""
    if not current_user.is_authenticated:
        return redirect(url_for('invite', role='customer'))
    _check_listing_csrf()

    from models import Listing, ListingFavorite
    listing = Listing.query.get_or_404(listing_id)

    # Check if the current user already has this listing favorited
    existing = ListingFavorite.query.filter_by(
        user_id=current_user.id, listing_id=listing_id
    ).first()

    # Always allow a user to remove their own existing favorite, even if the
    # listing is no longer publicly visible — otherwise stuck entries pile up.
    # Only enforce visibility when *adding* a new favorite.
    if not existing:
        is_public = listing.status in ('active', 'sold', 'reserved') and listing.moderation_status == 'approved'
        is_owner  = current_user.id == listing.seller_id
        if not (is_public or is_owner or current_user.is_admin):
            abort(404)

    if existing:
        db.session.delete(existing)
        listing.favorite_count = max(0, (listing.favorite_count or 1) - 1)
        db.session.commit()
        favorited = False
    else:
        fav = ListingFavorite(user_id=current_user.id, listing_id=listing_id)
        db.session.add(fav)
        listing.favorite_count = (listing.favorite_count or 0) + 1
        db.session.commit()
        favorited = True

    # AJAX support
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or \
       request.accept_mimetypes.best == 'application/json':
        return jsonify(favorited=favorited, count=listing.favorite_count)

    # Allow caller to redirect back to a safe local page (e.g. /saved)
    next_url = request.form.get('next', '').strip()
    if next_url and next_url.startswith('/') and not next_url.startswith('//'):
        return redirect(next_url)
    return redirect(url_for('listing_detail', listing_id=listing_id))


@app.route("/listing/<int:listing_id>/report", methods=["POST"])
def listing_report(listing_id):
    """Submit a report for a listing. Requires login."""
    if not current_user.is_authenticated:
        return redirect(url_for('invite', role='customer'))
    _check_listing_csrf()

    from models import Listing, ListingReport
    listing = Listing.query.get_or_404(listing_id)

    # Enforce the same visibility rule as listing_detail
    is_public = listing.status in ('active', 'sold', 'reserved') and listing.moderation_status == 'approved'
    is_owner  = current_user.id == listing.seller_id
    if not (is_public or is_owner or current_user.is_admin):
        abort(404)

    reason = request.form.get('reason', '').strip()[:100]
    details = request.form.get('details', '').strip()[:1000]
    if not reason:
        flash("Please select a reason for the report.", "error")
        return redirect(url_for('listing_detail', listing_id=listing_id))

    report = ListingReport(
        listing_id=listing_id,
        reporter_id=current_user.id,
        reason=reason,
        details=details,
    )
    db.session.add(report)
    db.session.commit()
    flash("Thank you — your report has been submitted for review.", "success")
    return redirect(url_for('listing_detail', listing_id=listing_id))


@app.route("/listing/<int:listing_id>/offer", methods=["POST"])
@require_login
def listing_make_offer(listing_id):
    """Submit or update a buyer offer on a negotiable listing."""
    _check_listing_csrf()

    from models import Listing, ListingOffer
    listing = Listing.query.get_or_404(listing_id)

    # Only active, approved, negotiable listings accept offers
    if listing.status != 'active' or listing.moderation_status != 'approved':
        abort(404)
    if listing.price_type != 'negotiable':
        flash("This listing does not accept offers.", "error")
        return redirect(url_for('listing_detail', listing_id=listing_id))

    # Sellers cannot offer on their own listing
    if current_user.id == listing.seller_id:
        flash("You cannot make an offer on your own listing.", "error")
        return redirect(url_for('listing_detail', listing_id=listing_id))

    # Parse amount — reject non-numeric, non-finite, and non-positive values
    import math as _math
    try:
        amount = float(request.form.get('amount', '').strip())
        if not _math.isfinite(amount) or amount <= 0 or amount > 999_999_999:
            raise ValueError
    except (ValueError, AttributeError):
        flash("Please enter a valid offer amount.", "error")
        return redirect(url_for('listing_detail', listing_id=listing_id))

    message = (request.form.get('message', '') or '').strip()[:1000]

    # Upsert: one pending offer per buyer per listing (update if already pending)
    existing = (ListingOffer.query
                .filter_by(listing_id=listing_id, buyer_id=current_user.id, status='pending')
                .first())
    if existing:
        existing.amount = amount
        existing.message = message or existing.message
        existing.updated_at = datetime.now()
        offer = existing
    else:
        offer = ListingOffer(
            listing_id=listing_id,
            buyer_id=current_user.id,
            seller_id=listing.seller_id,
            amount=amount,
            message=message or None,
            status='pending',
        )
        db.session.add(offer)

    db.session.commit()

    # Notify seller via SMS if they have SMS enabled
    # Uses the 'customer_new_bid' event toggle (ev_new_bid) — closest semantic match
    seller = listing.seller
    try:
        from sms_service import send_sms, is_sms_enabled
        if (is_sms_enabled('customer_new_bid') and seller.notify_sms
                and seller.sms_consent and seller.phone):
            buyer_name = current_user.first_name or 'A buyer'
            sms_body = (
                f"{buyer_name} made a ${amount:,.0f} offer on your listing "
                f'"{listing.title[:40]}". Log in to respond.'
            )
            send_sms(seller.phone, sms_body, 'customer_new_bid')
    except Exception:
        pass

    flash("Your offer has been sent to the seller!", "success")
    return redirect(url_for('listing_detail', listing_id=listing_id))


# ── Offer: Seller respond (accept / decline / counter) ─────────────────────

@app.route("/listing/<int:listing_id>/offer/<int:offer_id>/respond", methods=["POST"])
@require_login
def offer_seller_respond(listing_id, offer_id):
    """Seller accepts, declines, or counters a buyer offer."""
    _check_listing_csrf()
    from models import ListingOffer
    offer = ListingOffer.query.get_or_404(offer_id)
    listing = offer.listing

    if listing.id != listing_id or str(current_user.id) != str(listing.seller_id):
        abort(403)
    if offer.status not in ('pending', 'countered'):
        flash("This offer is no longer open.", "error")
        return redirect(url_for('listing_detail', listing_id=listing_id))

    action = request.form.get('action', '').strip()
    import math as _math

    if action == 'accept':
        offer.status = 'accepted'
        offer.updated_at = datetime.now()
        db.session.commit()
        try:
            from sms_service import send_sms
            buyer = offer.buyer
            if buyer and buyer.notify_sms and buyer.sms_consent and buyer.phone:
                send_sms(buyer.phone,
                         f"✅ Your ${offer.amount:,.0f} offer on \"{listing.title[:40]}\" was accepted! "
                         f"Message the seller to arrange pickup.",
                         'customer_new_bid')
        except Exception:
            pass
        flash("Offer accepted! The buyer has been notified.", "success")

    elif action == 'decline':
        offer.status = 'declined'
        offer.updated_at = datetime.now()
        db.session.commit()
        try:
            from sms_service import send_sms
            buyer = offer.buyer
            if buyer and buyer.notify_sms and buyer.sms_consent and buyer.phone:
                send_sms(buyer.phone,
                         f"Your offer on \"{listing.title[:40]}\" was declined. "
                         f"Browse more listings at JHEHaul.com.",
                         'customer_new_bid')
        except Exception:
            pass
        flash("Offer declined.", "success")

    elif action == 'counter':
        try:
            counter_amount = float(request.form.get('counter_amount', '').strip())
            if not _math.isfinite(counter_amount) or counter_amount <= 0 or counter_amount > 999_999_999:
                raise ValueError
        except (ValueError, AttributeError):
            flash("Please enter a valid counter offer amount.", "error")
            return redirect(url_for('listing_detail', listing_id=listing_id))
        offer.counter_amount = counter_amount
        offer.status = 'countered'
        offer.updated_at = datetime.now()
        db.session.commit()
        try:
            from sms_service import send_sms
            buyer = offer.buyer
            if buyer and buyer.notify_sms and buyer.sms_consent and buyer.phone:
                send_sms(buyer.phone,
                         f"💬 The seller countered your offer on \"{listing.title[:40]}\" "
                         f"at ${counter_amount:,.0f}. Log in to respond.",
                         'customer_new_bid')
        except Exception:
            pass
        flash(f"Counteroffer of ${counter_amount:,.0f} sent to the buyer.", "success")

    else:
        flash("Invalid action.", "error")

    return redirect(url_for('listing_detail', listing_id=listing_id))


# ── Offer: Buyer respond to counter (accept / decline / withdraw) ────────────

@app.route("/listing/<int:listing_id>/offer/<int:offer_id>/buyer-respond", methods=["POST"])
@require_login
def offer_buyer_respond(listing_id, offer_id):
    """Buyer accepts a counteroffer, declines it, or withdraws their offer."""
    _check_listing_csrf()
    from models import ListingOffer
    offer = ListingOffer.query.get_or_404(offer_id)

    if offer.listing_id != listing_id or str(current_user.id) != str(offer.buyer_id):
        abort(403)

    action = request.form.get('action', '').strip()

    if action == 'accept_counter':
        if offer.status != 'countered' or not offer.counter_amount:
            flash("No active counteroffer to accept.", "error")
            return redirect(url_for('listing_detail', listing_id=listing_id))
        offer.amount = offer.counter_amount
        offer.status = 'accepted'
        offer.updated_at = datetime.now()
        db.session.commit()
        try:
            from sms_service import send_sms
            seller = offer.seller
            if seller and seller.notify_sms and seller.sms_consent and seller.phone:
                send_sms(seller.phone,
                         f"✅ {current_user.first_name or 'A buyer'} accepted your ${offer.amount:,.0f} "
                         f"counteroffer on \"{offer.listing.title[:40]}\".",
                         'customer_new_bid')
        except Exception:
            pass
        flash("You accepted the counteroffer! Contact the seller to arrange pickup.", "success")

    elif action == 'decline_counter':
        offer.status = 'declined'
        offer.updated_at = datetime.now()
        db.session.commit()
        flash("Counteroffer declined.", "success")

    elif action == 'withdraw':
        if offer.status not in ('pending', 'countered'):
            flash("This offer can no longer be withdrawn.", "error")
            return redirect(url_for('listing_detail', listing_id=listing_id))
        offer.status = 'withdrawn'
        offer.updated_at = datetime.now()
        db.session.commit()
        flash("Your offer has been withdrawn.", "success")

    else:
        flash("Invalid action.", "error")

    return redirect(url_for('listing_detail', listing_id=listing_id))


# ── My Offers (buyer dashboard) ──────────────────────────────────────────────

@app.route("/my-offers")
@require_login
def my_offers():
    """Buyer's full offer history."""
    from models import ListingOffer
    offers = (ListingOffer.query
              .filter_by(buyer_id=current_user.id)
              .order_by(ListingOffer.updated_at.desc())
              .all())
    return render_template('my_offers.html', offers=offers)


# ── Seller Profile (public) ──────────────────────────────────────────────────

@app.route("/seller/<user_id>")
def seller_profile(user_id):
    """Public seller profile — name, city, active listings, sold count."""
    from models import Listing as _L
    seller = User.query.get_or_404(user_id)

    listings = (_L.query
                .filter_by(seller_id=user_id, status='active', moderation_status='approved')
                .order_by(_L.created_at.desc())
                .limit(24)
                .all())
    active_count = (_L.query
                    .filter_by(seller_id=user_id, status='active', moderation_status='approved')
                    .count())
    sold_count = _L.query.filter_by(seller_id=user_id, status='sold').count()
    featured_listing = listings[0] if listings else None

    return render_template('seller_profile.html',
                           seller=seller,
                           listings=listings,
                           active_count=active_count,
                           sold_count=sold_count,
                           featured_listing=featured_listing)


# ── Report User ──────────────────────────────────────────────────────────────

@app.route("/report-user/<user_id>", methods=["POST"])
@require_login
def report_user(user_id):
    """Submit a report against another user."""
    from models import UserReport
    _check_listing_csrf()
    target = User.query.get_or_404(user_id)
    if str(target.id) == str(current_user.id):
        flash("You cannot report yourself.", "error")
        return redirect(url_for('seller_profile', user_id=user_id))

    reason = (request.form.get('reason', '') or '').strip()[:100]
    details = (request.form.get('details', '') or '').strip()[:1000]
    if not reason:
        flash("Please select a reason.", "error")
        return redirect(url_for('seller_profile', user_id=user_id))

    report = UserReport(
        reported_user_id=str(user_id),
        reporter_id=str(current_user.id),
        reason=reason,
        details=details or None,
    )
    db.session.add(report)
    db.session.commit()
    flash("Report submitted. Our team will review it shortly.", "success")
    return redirect(url_for('seller_profile', user_id=user_id))


@app.route("/marketplace/messages")
@require_login
def marketplace_messages():
    """Unified inbox: all ListingConversation rows where the user is buyer or seller."""
    from models import ListingConversation, ListingMessage

    def _enrich(convos, viewer_id):
        out = []
        for convo in convos:
            msgs = convo.messages  # already ordered by created_at via relationship
            last_msg = msgs[-1] if msgs else None
            unread = sum(1 for m in msgs
                         if m.sender_id != viewer_id and m.read_at is None)
            # Thumbnail: primary photo of the listing
            thumb_url = None
            if convo.listing and convo.listing.primary_photo:
                p = convo.listing.primary_photo
                if p.storage_url:
                    thumb_url = p.storage_url
                else:
                    thumb_url = url_for('serve_listing_photo', photo_id=p.id)
            out.append({
                'convo': convo,
                'listing': convo.listing,
                'last_message': last_msg,
                'unread_count': unread,
                'thumb_url': thumb_url,
                'sort_ts': last_msg.created_at if last_msg else convo.created_at,
            })
        out.sort(key=lambda x: x['sort_ts'], reverse=True)
        return out

    uid = current_user.id
    buying = (ListingConversation.query
              .filter_by(buyer_id=uid)
              .order_by(ListingConversation.updated_at.desc())
              .all())
    selling = (ListingConversation.query
               .filter_by(seller_id=uid)
               .order_by(ListingConversation.updated_at.desc())
               .all())

    return render_template(
        'marketplace_messages.html',
        buying_convos=_enrich(buying, uid),
        selling_convos=_enrich(selling, uid),
    )


@app.route("/listing/<int:listing_id>/message", methods=["GET", "POST"])
@app.route("/listing/<int:listing_id>/message/<int:convo_id>", methods=["GET", "POST"])
def listing_message(listing_id, convo_id=None):
    """Start or continue a buyer↔seller conversation thread.

    Buyers:  GET/POST /listing/<id>/message              — create or resume their thread.
             GET/POST /listing/<id>/message/<convo_id>   — resume a specific thread.
    Sellers: GET      /listing/<id>/message              — inbox of all buyer threads.
             GET/POST /listing/<id>/message/<convo_id>   — reply to a specific thread.
    Admins:  same as sellers.

    Listing access mirrors the detail page (public-or-owner/admin) so sellers can
    still reach conversations after marking the listing as reserved/sold.
    New buyer threads are only allowed on active+approved listings.
    """
    if not current_user.is_authenticated:
        session['next_url'] = request.url
        return redirect(url_for('invite', role='customer'))

    from models import Listing, ListingConversation, ListingMessage

    # Same public-or-owner/admin access as listing_detail
    listing = Listing.query.get_or_404(listing_id)
    is_listing_owner = current_user.id == listing.seller_id
    is_admin = current_user.is_admin
    is_public = listing.status == 'active' and listing.moderation_status == 'approved'
    if not (is_public or is_listing_owner or is_admin):
        abort(404)

    # ── convo_id given: resolve first, then authorize, then branch ────────────
    if convo_id:
        convo = ListingConversation.query.filter_by(
            id=convo_id, listing_id=listing_id
        ).first_or_404()
        is_participant = current_user.id in (convo.buyer_id, convo.seller_id)
        if not (is_admin or is_participant):
            abort(403)

        # Determine whether the current user is acting as the seller side
        is_seller_view = is_admin or (current_user.id == convo.seller_id)

        if request.method == "POST":
            _check_listing_csrf()
            body = request.form.get('body', '').strip()
            if body:
                msg = ListingMessage(
                    conversation_id=convo.id,
                    sender_id=current_user.id,
                    body=body[:2000],
                )
                db.session.add(msg)
                convo.updated_at = datetime.now()
                db.session.commit()
                flash("Message sent!", "success")
            return redirect(url_for('listing_message',
                                    listing_id=listing_id, convo_id=convo_id))

        # Mark messages from the other party as read
        try:
            for m in convo.messages:
                if m.sender_id != current_user.id and m.read_at is None:
                    m.read_at = datetime.now()
            db.session.commit()
        except Exception:
            db.session.rollback()

        return render_template(
            'listing_message.html',
            listing=listing,
            convo=convo,
            messages=convo.messages,
            is_seller=is_seller_view,
        )

    # ── No convo_id: seller/admin → inbox; buyer → own thread ────────────────
    if is_listing_owner or is_admin:
        convos = (ListingConversation.query
                  .filter_by(listing_id=listing_id)
                  .order_by(ListingConversation.updated_at.desc())
                  .all())
        return render_template(
            'listing_seller_inbox.html',
            listing=listing,
            convos=convos,
        )

    # Buyer path — only allowed to start new threads on active+approved listing
    if not is_public:
        if listing.status in ('sold', 'reserved'):
            label = 'sold' if listing.status == 'sold' else 'reserved'
            flash(f"This listing is already {label} and is no longer accepting messages.", "error")
        else:
            flash("This listing is not available for messages.", "error")
        return redirect(url_for('listing_detail', listing_id=listing_id))

    convo = ListingConversation.query.filter_by(
        listing_id=listing_id, buyer_id=current_user.id
    ).first()
    if not convo:
        convo = ListingConversation(
            listing_id=listing_id,
            buyer_id=current_user.id,
            seller_id=listing.seller_id,
        )
        db.session.add(convo)
        db.session.flush()

    if request.method == "POST":
        _check_listing_csrf()
        body = request.form.get('body', '').strip()
        if body:
            msg = ListingMessage(
                conversation_id=convo.id,
                sender_id=current_user.id,
                body=body[:2000],
            )
            db.session.add(msg)
            convo.updated_at = datetime.now()
        db.session.commit()
        if body:
            flash("Message sent!", "success")
        return redirect(url_for('listing_message', listing_id=listing_id))

    db.session.commit()  # persist new convo if just created

    # Mark seller's messages as read
    try:
        for m in convo.messages:
            if m.sender_id != current_user.id and m.read_at is None:
                m.read_at = datetime.now()
        db.session.commit()
    except Exception:
        db.session.rollback()

    return render_template(
        'listing_message.html',
        listing=listing,
        convo=convo,
        messages=convo.messages,
        is_seller=False,
    )


@app.route("/uploads/listing/db/<int:photo_id>")
def serve_listing_photo(photo_id):
    """Serve a listing photo stored as binary in the database.

    Public access is restricted to photos whose parent listing is active and
    approved.  The listing seller and site admins may also access photos for
    listings in other states (draft, pending, removed, etc.).
    """
    from models import ListingPhoto, Listing
    photo = ListingPhoto.query.get(photo_id)
    if not photo or not photo.data:
        return "", 404
    listing = Listing.query.get(photo.listing_id)
    if not listing:
        return "", 404

    is_public = (listing.status in ('active', 'sold', 'reserved') and listing.moderation_status == 'approved')
    is_owner = current_user.is_authenticated and current_user.id == listing.seller_id
    is_admin = current_user.is_authenticated and current_user.is_admin
    if not (is_public or is_owner or is_admin):
        return "", 404

    from flask import Response
    r = Response(photo.data, mimetype=photo.content_type or 'image/jpeg')
    cache = "public, max-age=3600" if is_public else "private, no-cache"
    r.headers["Cache-Control"] = cache
    return r

@app.route("/invite")
@app.route("/invite/<role>")
def invite(role=None):
    if role == 'hauler':
        return redirect(url_for('home'))
    if role == 'customer':
        session['invited_role'] = role
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for('admin_dashboard'))
        if not current_user.user_type:
            return redirect(url_for('choose_role'))
        return redirect(url_for('customer_dashboard'))
    return render_template('invite_landing.html', role=role)

@app.route("/choose-role")
@require_login
def choose_role():
    if current_user.is_admin:
        session.pop("invited_role", None)
        return redirect(url_for("admin_dashboard"))
    if current_user.user_type == "customer":
        session.pop("invited_role", None)
        return redirect(url_for("marketplace"))
    session.pop("invited_role", None)
    current_user.user_type = 'customer'
    db.session.commit()
    try:
        _name = f"{current_user.first_name or ''} {current_user.last_name or ''}".strip() or current_user.email
        notify_admin_new_customer(_name, current_user.email)
        notify_admin_new_customer_sms(_name, current_user.email)
    except Exception as e:
        app.logger.error("Admin notify failed (new customer): %s", e)
    return redirect(url_for("marketplace"))

@app.route("/set-role", methods=["POST"])
@require_login
def set_role():
    if current_user.is_admin:
        return redirect(url_for('admin_dashboard'))
    current_user.user_type = 'customer'
    db.session.commit()
    try:
        _name = f"{current_user.first_name or ''} {current_user.last_name or ''}".strip() or current_user.email
        notify_admin_new_customer(_name, current_user.email)
        notify_admin_new_customer_sms(_name, current_user.email)
    except Exception as e:
        app.logger.error("Admin notify failed (new customer): %s", e)
    return redirect(url_for('marketplace'))

@app.route("/hauler/setup")
@app.route("/hauler/setup", methods=["POST"])
def hauler_setup():
    return redirect(url_for('home'))

def hauler_setup_save():
    return redirect(url_for('home'))

@app.route("/about")
def about():
    return render_template('about.html')

@app.route("/how-it-works")
def how_it_works():
    return render_template('how_it_works.html')

@app.route("/safety")
def safety():
    return render_template('safety.html')

@app.route("/guidelines")
def guidelines():
    return render_template('guidelines.html')

@app.route("/privacy")
def privacy():
    return render_template('privacy.html')

@app.route("/hauler-agreement")
def hauler_agreement():
    return redirect(url_for('home'))

@app.route("/customer-terms")
def customer_terms():
    return render_template('customer_terms.html')

@app.route("/customer/new", methods=["GET"])
def customer_new():
    return redirect(url_for('customer_request'))

@app.route("/customer/request", methods=["GET"])
@require_role('customer')
def customer_request():
    return render_template('customer_request.html')

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
    service_type = request.form.get("service_type", "").strip()

    agree_terms = request.form.get("agree_terms")
    if not agree_terms:
        flash("You must certify that you own or have legal authority over the property before submitting a request.", "error")
        return redirect(url_for('customer_request'))

    if not customer_name or not pickup_address or not job_description or not pickup_zip:
        return "Missing required fields", 400

    import re
    if not re.match(r'^\d{5}$', pickup_zip):
        flash("Please enter a valid 5-digit ZIP code.", "error")
        return redirect(url_for('customer_request'))

    from models import ZipCode
    if not ZipCode.query.get(pickup_zip):
        flash("That ZIP code is not supported yet. We currently cover Minnesota and Wisconsin.", "error")
        return redirect(url_for('customer_request'))

    from launch_zone import in_launch_zone
    allowed, _ = in_launch_zone(pickup_zip)
    if not allowed:
        app.logger.warning("launch_zone: job posting rejected ZIP %s for user %s", pickup_zip, current_user.id)
        flash("JHE Haul is currently launching in select Minnesota areas. "
              "We're not in your area just yet — check back soon as we expand!", "error")
        return redirect(url_for('customer_request'))

    job = Job(
        customer_id=current_user.id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        pickup_address=pickup_address,
        pickup_zip=pickup_zip,
        preferred_date=preferred_date if preferred_date else None,
        preferred_time=preferred_time if preferred_time else None,
        job_description=job_description,
        service_type=service_type if service_type else None,
        status='reviewing'
    )
    db.session.add(job)
    db.session.commit()

    try:
        notify_admin_new_request(job.id, customer_name, service_type, pickup_zip, job_description)
        notify_admin_new_request_sms(job.id, customer_name, service_type, pickup_zip)
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

    flash("Your service request has been submitted! We'll review it and send you a quote soon.", "success")
    return redirect(url_for("customer_jobs"))

@app.route("/customer/jobs")
@require_role('customer')
def customer_jobs():
    jobs = Job.query.filter_by(customer_id=current_user.id).order_by(Job.id.desc()).all()
    unread_counts = {}
    for job in jobs:
        unread_counts[job.id] = Message.query.filter_by(job_id=job.id).filter(
            Message.sender_id != current_user.id,
            Message.read_at.is_(None)
        ).count()
    return render_template('customer_jobs.html', jobs=jobs, unread_counts=unread_counts)


@app.route("/customer/messages")
@require_role('customer')
def customer_messages():
    jobs = Job.query.filter_by(customer_id=current_user.id).order_by(Job.id.desc()).all()
    conversations = []
    for job in jobs:
        msgs = Message.query.filter_by(job_id=job.id).order_by(Message.created_at.asc()).all()
        if not msgs:
            continue
        last_msg = msgs[-1]
        unread = sum(1 for m in msgs if m.sender_id != current_user.id and m.read_at is None)
        conversations.append({
            'job': job,
            'last_message': last_msg,
            'unread_count': unread,
        })
    conversations.sort(key=lambda c: c['last_message'].created_at or 0, reverse=True)
    return render_template('customer_messages.html', conversations=conversations)


@app.route("/customer/job/<int:job_id>")
def customer_job_detail_legacy(job_id):
    return redirect(url_for('customer_job_detail', job_id=job_id), code=301)

@app.route("/customer/request/<int:job_id>")
@require_role('customer')
def customer_job_detail(job_id):
    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id and not current_user.is_admin:
        return "Access denied", 403

    quotes = Quote.query.filter_by(job_id=job_id).order_by(Quote.created_at.desc()).all()
    active_quote = (
        next((q for q in quotes if q.status == 'accepted'), None) or
        next((q for q in quotes if q.status == 'pending'), None)
    )

    messages = Message.query.filter_by(job_id=job_id).order_by(Message.created_at.asc()).all()
    from datetime import datetime as _dt
    changed = False
    for msg in messages:
        if msg.sender_id != current_user.id and not msg.read_at:
            msg.read_at = _dt.now()
            changed = True
    if changed:
        db.session.commit()

    portal_checkout_url = None
    pay_link = None
    checkout_over500_url = None
    pay_link_missing = False

    if job.status == 'waiting_for_payment' and not job.deposit_paid and active_quote:
        portal_checkout_url = url_for('checkout_quote', quote_id=active_quote.id)
    elif job.status == "accepted" and not job.deposit_paid:
        accepted_bid = Bid.query.filter_by(job_id=job_id, status='accepted').first()
        quote_amt = float(job.accepted_quote or 0)
        if quote_amt > 500 and accepted_bid:
            checkout_over500_url = url_for('checkout_over500', bid_id=accepted_bid.id)
        else:
            pay_link = choose_pay_link(job.accepted_quote)
            if pay_link:
                base_url = os.environ.get("APP_BASE_URL", "https://jhehaul.com").rstrip("/")
                success_url = f"{base_url}/payment_success/{job.id}"
                pay_link = f"{pay_link}?success_url={success_url}"
            else:
                pay_link_missing = True

    bids = Bid.query.filter_by(job_id=job_id).order_by(Bid.quote_amount.asc()).all()
    hauler_map = {}
    for bid in bids:
        if bid.hauler_id and bid.hauler_id not in hauler_map:
            h = User.query.get(bid.hauler_id)
            if h:
                hauler_map[bid.hauler_id] = h
    scheduled_date_nice = None
    scheduled_time_nice = None
    if job.scheduled_date:
        try:
            scheduled_date_nice = _dt.strptime(job.scheduled_date, '%Y-%m-%d').strftime('%A, %B %d, %Y')
        except ValueError:
            scheduled_date_nice = job.scheduled_date
        if job.scheduled_time:
            try:
                scheduled_time_nice = _dt.strptime(job.scheduled_time, '%H:%M').strftime('%I:%M %p').lstrip('0')
            except ValueError:
                scheduled_time_nice = job.scheduled_time

    return render_template('customer_job_detail.html', job=job, bids=bids,
                           scheduled_date_nice=scheduled_date_nice,
                           scheduled_time_nice=scheduled_time_nice,
                           pay_link=pay_link,
                           checkout_over500_url=checkout_over500_url,
                           pay_link_missing=pay_link_missing,
                           portal_checkout_url=portal_checkout_url,
                           active_quote=active_quote,
                           quotes=quotes,
                           messages=messages,
                           hauler_map=hauler_map)

@app.route("/customer/upload_photos/<int:job_id>", methods=["POST"])
@require_role('customer')
def customer_upload_photos(job_id):
    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    if job.status not in ['open', 'bidding', 'accepted', 'deposit_paid',
                          'reviewing', 'quoted', 'waiting_for_payment', 'scheduled', 'in_progress']:
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
            if rb_hauler and rb_hauler.notify_sms and rb_hauler.phone:
                notify_hauler_bid_rejected_sms(rb_hauler.phone, job.id)
    except Exception as e:
        app.logger.error("Rejected-bid notifications failed (job #%s): %s", job.id, e)

    try:
        _cname = f"{current_user.first_name or ''} {current_user.last_name or ''}".strip() or current_user.email
        notify_admin_bid_accepted(job.id, _cname, bid.hauler_name, float(bid.quote_amount))
        notify_admin_bid_accepted_sms(job.id, _cname, bid.hauler_name, float(bid.quote_amount))
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
            cancel_url=f"{domain}/customer/request/{job.id}",
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


@app.route("/customer/quote/<int:quote_id>/accept", methods=["POST"])
@require_role('customer')
def customer_accept_quote(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    job = Job.query.get_or_404(quote.job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    if job.status != 'quoted' or quote.status != 'pending':
        flash("This quote is no longer available to accept.", "error")
        return redirect(url_for('customer_job_detail', job_id=job.id))
    for other in Quote.query.filter_by(job_id=job.id).filter(Quote.id != quote.id, Quote.status == 'pending').all():
        other.status = 'declined'
    quote.status = 'accepted'
    job.status = 'waiting_for_payment'
    db.session.commit()
    flash("Quote accepted! Please pay the deposit below to confirm your booking.", "success")
    return redirect(url_for('customer_job_detail', job_id=job.id))


@app.route("/customer/quote/<int:quote_id>/decline", methods=["POST"])
@require_role('customer')
def customer_decline_quote(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    job = Job.query.get_or_404(quote.job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    if job.status != 'quoted' or quote.status != 'pending':
        flash("This quote can no longer be declined.", "error")
        return redirect(url_for('customer_job_detail', job_id=job.id))
    decline_note = request.form.get("decline_note", "").strip()
    quote.status = 'declined'
    if decline_note:
        quote.customer_notes = decline_note
    job.status = 'reviewing'
    db.session.commit()
    flash("Quote declined. We'll review your request and follow up with you soon.", "success")
    return redirect(url_for('customer_job_detail', job_id=job.id))


@app.route("/customer/message/<int:job_id>", methods=["POST"])
@require_role('customer')
def customer_send_message(job_id):
    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    body = request.form.get("body", "").strip()
    if not body:
        flash("Message cannot be empty.", "error")
        return redirect(url_for('customer_job_detail', job_id=job_id))
    msg = Message(job_id=job_id, sender_id=current_user.id, body=body)
    db.session.add(msg)
    db.session.commit()
    try:
        from email_service import send_email, _html
        admin_email = os.environ.get("ADMIN_EMAIL", "jhehaul@gmail.com")
        app_url = os.environ.get("APP_BASE_URL", "https://jhehaul.com")
        subject = f"[JHE Haul] Customer message on Request #{job_id} — {current_user.first_name or 'Customer'}"
        body_html = f"""
        <p><strong>{current_user.first_name or current_user.email or 'Customer'}</strong> sent a message about Request #{job_id}.</p>
        <div style="background:#f1f5f9;border:1px solid #e2e8f0;border-radius:8px;padding:14px 18px;margin:14px 0;">
          <p style="margin:0;color:#1a202c;white-space:pre-wrap;">{body}</p>
        </div>
        <a href="{app_url}/admin/request/{job_id}" class="btn">Reply in Admin →</a>"""
        send_email(admin_email, subject,
                   _html("Customer Message", f"Re: Request #{job_id}",
                         "💬 New Customer Message", body_html),
                   'admin_customer_message')
    except Exception as e:
        app.logger.error("Admin message notify failed (job #%s): %s", job_id, e)
    try:
        notify_admin_sms(
            f"Customer msg on Request #{job_id}: {body[:80]}{'…' if len(body) > 80 else ''}"
        )
    except Exception as e:
        app.logger.error("Admin message SMS failed (job #%s): %s", job_id, e)
    return redirect(url_for('customer_job_detail', job_id=job_id) + '#messages')


@app.route("/checkout/quote/<int:quote_id>")
@require_role('customer')
def checkout_quote(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    job = Job.query.get_or_404(quote.job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    if job.status != 'waiting_for_payment' or job.deposit_paid:
        return redirect(url_for('customer_job_detail', job_id=job.id))
    if quote.status != 'accepted':
        flash("This quote has not been accepted. Please accept the quote before paying.", "error")
        return redirect(url_for('customer_job_detail', job_id=job.id))
    deposit = float(quote.deposit_amount or 0)
    if deposit <= 0:
        flash("Invalid deposit amount. Please contact support.", "error")
        return redirect(url_for('customer_job_detail', job_id=job.id))
    fee_cents = int(round(deposit * 100))
    domain = os.environ.get("APP_BASE_URL", "https://jhehaul.com").rstrip("/")
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'JHE Haul — Deposit (Request #{job.id})',
                        'description': f'{job.service_type or "Service"} — total quote ${quote.price:.2f}',
                    },
                    'unit_amount': fee_cents,
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=(f"{domain}/checkout/quote/success"
                         f"?session_id={{CHECKOUT_SESSION_ID}}&job_id={job.id}&quote_id={quote.id}"),
            cancel_url=f"{domain}/customer/request/{job.id}",
            metadata={'job_id': str(job.id), 'quote_id': str(quote.id), 'deposit_amount': str(deposit)},
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        app.logger.error("Stripe checkout error (quote #%s): %s", quote_id, e)
        flash("Payment error. Please try again.", "error")
        return redirect(url_for('customer_job_detail', job_id=job.id))


@app.route("/checkout/quote/success")
@require_role('customer')
def checkout_quote_success():
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
        cs = stripe.checkout.Session.retrieve(session_id)
        # Validate metadata to prevent session replay on a different request
        meta = cs.metadata or {}
        meta_job_id = int(meta.get('job_id', job_id))
        if meta_job_id != job_id:
            app.logger.warning("Stripe session job_id mismatch: expected %s, got %s", job_id, meta_job_id)
            flash("Payment session does not match this request. Please contact support.", "error")
            return redirect(url_for('customer_jobs'))
        meta_quote_id = int(meta.get('quote_id', 0))
        if meta_quote_id:
            auth_quote = Quote.query.get(meta_quote_id)
            if not auth_quote or auth_quote.job_id != job_id or auth_quote.status != 'accepted':
                app.logger.warning("Stripe quote auth failed (job #%s, quote #%s)", job_id, meta_quote_id)
                flash("Payment session does not match an authorized quote. Please contact support.", "error")
                return redirect(url_for('customer_jobs'))
        if cs.payment_status == 'paid':
            job.deposit_paid = True
            job.status = 'scheduled'
            db.session.commit()
            try:
                _cname = (f"{current_user.first_name or ''} {current_user.last_name or ''}".strip()
                          or current_user.email)
                notify_admin_deposit_paid(job.id, _cname, None, job.accepted_quote)
            except Exception as e:
                app.logger.error("Admin notify failed (quote deposit job #%s): %s", job.id, e)
            try:
                if current_user.email:
                    notify_customer_deposit_confirmed(current_user.email, job.id, job.service_type)
            except Exception as e:
                app.logger.error("Customer deposit notify failed (job #%s): %s", job.id, e)
            try:
                if current_user.notify_sms and current_user.phone:
                    notify_customer_deposit_confirmed_sms(current_user.phone, job.id, job.service_type)
            except Exception as e:
                app.logger.error("Customer deposit SMS failed (job #%s): %s", job.id, e)
            flash("Deposit paid! Your service is now scheduled.", "success")
            return redirect(url_for('customer_job_detail', job_id=job.id))
        else:
            flash("Payment not completed. Please try again.", "error")
            return redirect(url_for('customer_job_detail', job_id=job.id))
    except Exception as e:
        app.logger.error("Stripe verify error (quote checkout): %s", e)
        flash("Could not verify payment. Please contact support.", "error")
        return redirect(url_for('customer_job_detail', job_id=job.id))


@app.route("/services")
def services():
    return render_template('services.html')


@app.route("/admin/job/<int:job_id>/send_quote", methods=["POST"])
@app.route("/admin/quote/create/<int:job_id>", methods=["POST"])
@require_login
def admin_send_quote(job_id):
    if not current_user.is_admin:
        return "Access denied", 403
    job = Job.query.get_or_404(job_id)
    try:
        price = float(request.form.get("price", 0))
        deposit = float(request.form.get("deposit_amount", 0))
    except (ValueError, TypeError):
        flash("Invalid price values.", "error")
        return redirect(url_for('admin_job_detail', job_id=job_id) if 'admin_job_detail' in app.view_functions else url_for('home'))
    admin_notes = request.form.get("admin_notes", "").strip() or None
    customer_notes = request.form.get("customer_notes", "").strip() or None
    estimated_completion = request.form.get("estimated_completion", "").strip() or None
    if price <= 0 or deposit <= 0:
        flash("Price and deposit must be greater than zero.", "error")
        return redirect(url_for('admin_request_detail', job_id=job_id))
    pending_quote = Quote.query.filter_by(job_id=job.id, status='pending').first()
    if pending_quote and request.form.get("confirm_resend") != "1":
        flash("A quote is already pending — the customer hasn't responded yet. "
              "Confirm resend to send another quote.", "error")
        return redirect(url_for('admin_request_detail', job_id=job_id))
    quote = Quote(
        job_id=job.id,
        price=price,
        deposit_amount=deposit,
        admin_notes=admin_notes,
        customer_notes=customer_notes,
        estimated_completion=estimated_completion,
        status='pending',
    )
    db.session.add(quote)
    job.status = 'quoted'
    db.session.commit()
    customer = User.query.get(job.customer_id)
    if customer:
        try:
            if customer.email:
                notify_customer_quote_received(
                    customer.email, job.id, job.service_type or 'Service Request',
                    price, deposit, customer_notes, estimated_completion
                )
        except Exception as e:
            app.logger.error("notify_customer_quote_received failed (job #%s): %s", job.id, e)
        try:
            if customer.notify_sms and customer.phone:
                notify_customer_quote_received_sms(customer.phone, job.id, job.service_type or 'Service Request', price)
        except Exception as e:
            app.logger.error("notify_customer_quote_received_sms failed (job #%s): %s", job.id, e)
    flash(f"Quote sent to customer for Request #{job.id}.", "success")
    return redirect(url_for('admin_request_detail', job_id=job_id))


@app.route("/admin/quote/<int:quote_id>/withdraw", methods=["POST"])
@require_login
def admin_withdraw_quote(quote_id):
    if not current_user.is_admin:
        return "Access denied", 403
    quote = Quote.query.get_or_404(quote_id)
    job = Job.query.get_or_404(quote.job_id)
    if quote.status != 'pending':
        flash("Only pending quotes can be withdrawn.", "error")
        return redirect(url_for('admin_request_detail', job_id=job.id))
    quote.status = 'withdrawn'
    # Revert job status to 'reviewing' if no other pending quote remains
    other_pending = Quote.query.filter(
        Quote.job_id == job.id,
        Quote.id != quote.id,
        Quote.status == 'pending'
    ).first()
    if not other_pending and job.status == 'quoted':
        job.status = 'reviewing'
    db.session.commit()
    flash(f"Quote #{quote.id} has been withdrawn. The customer can no longer act on it.", "success")
    return redirect(url_for('admin_request_detail', job_id=job.id))


# ── ADMIN PORTAL ROUTES ────────────────────────────────────────────────────────

_PORTAL_STATUSES = ['reviewing', 'quoted', 'waiting_for_payment', 'scheduled', 'in_progress', 'completed', 'cancelled']
_LEGACY_STATUSES = ['open', 'bidding', 'accepted', 'deposit_paid', 'expired']
_ALL_STATUSES_ORDERED = _PORTAL_STATUSES + _LEGACY_STATUSES
_STATUS_LABELS = {
    'reviewing': ('🔍 Reviewing', '#3b82f6'),
    'quoted': ('💬 Quoted', '#8b5cf6'),
    'waiting_for_payment': ('💳 Awaiting Payment', '#f59e0b'),
    'scheduled': ('📅 Scheduled', '#10b981'),
    'in_progress': ('🚛 In Progress', '#f97316'),
    'completed': ('✅ Completed', '#16a34a'),
    'cancelled': ('❌ Cancelled', '#ef4444'),
    'open': ('📂 Open (Legacy)', '#64748b'),
    'bidding': ('🏷 Bidding (Legacy)', '#64748b'),
    'accepted': ('🤝 Accepted (Legacy)', '#64748b'),
    'deposit_paid': ('💰 Deposit Paid (Legacy)', '#64748b'),
    'expired': ('⏰ Expired (Legacy)', '#64748b'),
}


@app.route("/admin/requests")
@require_admin
def admin_requests():
    jobs = Job.query.order_by(Job.id.desc()).all()
    grouped = {}
    for job in jobs:
        grouped.setdefault(job.status, []).append(job)
    present_statuses = [s for s in _ALL_STATUSES_ORDERED if grouped.get(s)]
    for s in grouped:
        if s not in present_statuses:
            present_statuses.append(s)
    unread_by_job = {}
    msgs = (Message.query
            .join(User, Message.sender_id == User.id)
            .filter(Message.read_at == None, User.is_admin == False)
            .all())
    for m in msgs:
        unread_by_job[m.job_id] = unread_by_job.get(m.job_id, 0) + 1
    return render_template('admin_requests.html',
                           grouped=grouped,
                           all_statuses=present_statuses,
                           status_labels=_STATUS_LABELS,
                           unread_by_job=unread_by_job)


@app.route("/admin/request/<int:job_id>", methods=["GET"])
@require_admin
def admin_request_detail(job_id):
    job = Job.query.get_or_404(job_id)
    customer = User.query.get(job.customer_id) if job.customer_id else None
    messages = (Message.query
                .filter_by(job_id=job_id)
                .order_by(Message.created_at.asc())
                .all())
    now = datetime.now()
    for msg in messages:
        if not msg.read_at and msg.sender_id != current_user.id:
            msg.read_at = now
    db.session.commit()
    active_quote = (Quote.query
                    .filter_by(job_id=job_id, status='pending')
                    .order_by(Quote.created_at.desc())
                    .first())
    all_quotes = Quote.query.filter_by(job_id=job_id).order_by(Quote.created_at.desc()).all()
    completion_photos = CompletionPhoto.query.filter_by(job_id=job_id).all()
    return render_template('admin_request_detail.html',
                           job=job,
                           customer=customer,
                           messages=messages,
                           active_quote=active_quote,
                           all_quotes=all_quotes,
                           completion_photos=completion_photos,
                           status_labels=_STATUS_LABELS,
                           portal_statuses=_PORTAL_STATUSES)


_VALID_TRANSITIONS = {
    'reviewing':           {'quoted', 'cancelled'},
    'quoted':              {'scheduled', 'cancelled'},
    'waiting_for_payment': {'scheduled', 'cancelled'},
    'scheduled':           {'in_progress', 'cancelled'},
    'in_progress':         {'completed', 'cancelled'},
    'completed':           {'cancelled'},
    'cancelled':           {'reviewing'},
    'open':                {'reviewing', 'cancelled'},
    'bidding':             {'reviewing', 'cancelled'},
    'accepted':            {'reviewing', 'scheduled', 'cancelled'},
    'deposit_paid':        {'reviewing', 'scheduled', 'cancelled'},
    'expired':             {'reviewing', 'cancelled'},
}


@app.route("/admin/request/<int:job_id>/status", methods=["POST"])
@require_admin
def admin_request_status(job_id):
    job = Job.query.get_or_404(job_id)
    new_status = request.form.get("status", "").strip()
    if new_status not in _PORTAL_STATUSES:
        flash("Invalid status.", "error")
        return redirect(url_for('admin_request_detail', job_id=job_id))
    old_status = job.status
    allowed = _VALID_TRANSITIONS.get(old_status, set())
    if new_status not in allowed:
        if new_status == old_status:
            flash("Status is already set to that value.", "error")
        else:
            flash(f"Cannot move from '{old_status.replace('_',' ')}' to '{new_status.replace('_',' ')}'. "
                  f"Allowed next steps: {', '.join(s.replace('_',' ') for s in sorted(allowed)) or 'none'}.", "error")
        return redirect(url_for('admin_request_detail', job_id=job_id))
    if new_status == 'scheduled':
        accepted_quote = Quote.query.filter_by(job_id=job_id, status='accepted').first()
        if not accepted_quote or not job.deposit_paid:
            flash("Cannot schedule: customer must accept the quote and pay the deposit first.", "error")
            return redirect(url_for('admin_request_detail', job_id=job_id))
    job.status = new_status
    if new_status == 'completed' and not job.completed_at:
        job.completed_at = datetime.now()
    if new_status == 'cancelled' and not job.cancelled_at:
        job.cancelled_at = datetime.now()
    db.session.commit()
    flash(f"Status updated: {old_status.replace('_',' ')} → {new_status.replace('_',' ')}.", "success")
    return redirect(url_for('admin_request_detail', job_id=job_id))


@app.route("/admin/request/<int:job_id>/schedule", methods=["POST"])
@require_admin
def admin_schedule_appointment(job_id):
    job = Job.query.get_or_404(job_id)
    scheduled_date = request.form.get("scheduled_date", "").strip()
    scheduled_time = request.form.get("scheduled_time", "").strip()
    if not scheduled_date or not scheduled_time:
        flash("Please choose both an appointment date and time.", "error")
        return redirect(url_for('admin_request_detail', job_id=job_id))
    try:
        datetime.strptime(scheduled_date, '%Y-%m-%d')
        datetime.strptime(scheduled_time, '%H:%M')
    except ValueError:
        flash("Invalid date or time format.", "error")
        return redirect(url_for('admin_request_detail', job_id=job_id))
    # Only jobs that could legitimately be (or stay) scheduled may get an appointment
    if job.status not in ('quoted', 'waiting_for_payment', 'accepted', 'deposit_paid',
                          'scheduled', 'in_progress'):
        flash(f"Cannot schedule an appointment for a request in '{job.status.replace('_',' ')}' status.", "error")
        return redirect(url_for('admin_request_detail', job_id=job_id))
    # Same business rule as the status route: quote accepted + deposit paid first
    if job.status not in ('scheduled', 'in_progress'):
        accepted_quote = Quote.query.filter_by(job_id=job_id, status='accepted').first()
        if not accepted_quote or not job.deposit_paid:
            flash("Cannot schedule: customer must accept the quote and pay the deposit first.", "error")
            return redirect(url_for('admin_request_detail', job_id=job_id))
    job.scheduled_date = scheduled_date
    job.scheduled_time = scheduled_time
    if job.status != 'in_progress':
        job.status = 'scheduled'
    db.session.commit()

    # Human-friendly date/time for notifications
    try:
        nice_date = datetime.strptime(scheduled_date, '%Y-%m-%d').strftime('%A, %B %d, %Y')
    except ValueError:
        nice_date = scheduled_date
    nice_time = datetime.strptime(scheduled_time, '%H:%M').strftime('%I:%M %p').lstrip('0')

    customer = User.query.get(job.customer_id) if job.customer_id else None
    if customer:
        try:
            if customer.email:
                notify_customer_appointment_confirmed(
                    customer.email, job.id, job.service_type or 'Service Request',
                    nice_date, nice_time)
        except Exception as e:
            app.logger.error("notify_customer_appointment_confirmed failed (job #%s): %s", job.id, e)
        try:
            if customer.notify_sms and customer.phone:
                notify_customer_appointment_confirmed_sms(
                    customer.phone, job.id, job.service_type or 'your service',
                    nice_date, nice_time)
        except Exception as e:
            app.logger.error("notify_customer_appointment_confirmed_sms failed (job #%s): %s", job.id, e)

    flash(f"Appointment confirmed for {nice_date} at {nice_time}. Customer notified.", "success")
    return redirect(url_for('admin_request_detail', job_id=job_id))


@app.route("/admin/request/<int:job_id>/complete", methods=["POST"])
@require_admin
def admin_request_complete(job_id):
    job = Job.query.get_or_404(job_id)
    job.status = 'completed'
    job.completed_at = datetime.now()
    from storage import upload_file as _upload_file
    photos = request.files.getlist("photos")
    for photo in photos:
        if photo and photo.filename:
            ext = os.path.splitext(photo.filename)[1]
            photo_data, photo_ct = _read_photo_bytes(photo, ext)
            filename, storage_url = _upload_file(photo, ext)
            cp = CompletionPhoto(
                job_id=job.id, filename=filename, storage_url=storage_url,
                data=photo_data if not storage_url else None, content_type=photo_ct,
                photo_type='after',
            )
            db.session.add(cp)
    db.session.commit()
    customer = User.query.get(job.customer_id) if job.customer_id else None
    if customer:
        try:
            if customer.email:
                notify_customer_job_completed(customer.email, job.id)
        except Exception as e:
            app.logger.error("notify_customer_job_completed failed (job #%s): %s", job.id, e)
        try:
            if customer.notify_sms and customer.phone:
                from sms_service import notify_customer_job_completed_sms
                notify_customer_job_completed_sms(customer.phone, job.id)
        except Exception as e:
            app.logger.error("notify_customer_job_completed_sms failed (job #%s): %s", job.id, e)
    flash("Job marked complete. Customer has been notified.", "success")
    return redirect(url_for('admin_request_detail', job_id=job_id))


@app.route("/admin/request/<int:job_id>/upload_photo", methods=["POST"])
@require_admin
def admin_upload_completion_photo(job_id):
    job = Job.query.get_or_404(job_id)
    photo_type = request.form.get("photo_type", "after")
    from storage import upload_file as _upload_file
    photos = request.files.getlist("photos")
    count = 0
    for photo in photos:
        if photo and photo.filename:
            ext = os.path.splitext(photo.filename)[1]
            photo_data, photo_ct = _read_photo_bytes(photo, ext)
            filename, storage_url = _upload_file(photo, ext)
            cp = CompletionPhoto(
                job_id=job.id, filename=filename, storage_url=storage_url,
                data=photo_data if not storage_url else None, content_type=photo_ct,
                photo_type=photo_type,
            )
            db.session.add(cp)
            count += 1
    db.session.commit()
    flash(f"{count} photo(s) uploaded.", "success")
    return redirect(url_for('admin_request_detail', job_id=job_id))


@app.route("/admin/request/<int:job_id>/message", methods=["POST"])
@app.route("/admin/message/<int:job_id>", methods=["POST"])
@require_admin
def admin_message_reply(job_id):
    job = Job.query.get_or_404(job_id)
    body = request.form.get("body", "").strip()
    if not body:
        flash("Message cannot be empty.", "error")
        return redirect(url_for('admin_request_detail', job_id=job_id))
    msg = Message(job_id=job.id, sender_id=current_user.id, body=body)
    db.session.add(msg)
    db.session.commit()
    customer = User.query.get(job.customer_id) if job.customer_id else None
    if customer and customer.email:
        try:
            from email_service import send_email, _html
            subject = f"New message about your Request #{job.id}"
            body_html = f"""
            <p>JHE Haul sent you a message about your service request.</p>
            <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px 18px;margin:14px 0;">
              <p style="margin:0;color:#1a202c;">{body}</p>
            </div>
            <a href="{'/customer/request/' + str(job.id)}" class="btn">View &amp; Reply →</a>"""
            send_email(customer.email, subject,
                       _html("Message from JHE Haul", f"Re: Request #{job.id}",
                             "💬 New Message", body_html),
                       'admin_message_sent')
        except Exception as e:
            app.logger.error("admin_message_reply notify failed (job #%s): %s", job.id, e)
    flash("Message sent.", "success")
    return redirect(url_for('admin_request_detail', job_id=job_id))


@app.route("/admin/messages")
@require_admin
def admin_messages():
    unread_items = (db.session.query(Message, Job, User)
                    .join(Job, Message.job_id == Job.id)
                    .join(User, Message.sender_id == User.id)
                    .filter(Message.read_at == None, User.is_admin == False)
                    .order_by(Message.created_at.desc())
                    .all())
    messages = [{'msg': msg, 'job': job, 'sender': sender} for msg, job, sender in unread_items]
    return render_template('admin_messages.html', messages=messages)


# ── END ADMIN PORTAL ROUTES ────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# MARKETPLACE DELIVERY SCHEDULING
# ══════════════════════════════════════════════════════════════════════════════

_DELIVERY_STATUS_META = {
    'requested':       ('🔵', 'Requested',       '#3b82f6'),
    'offers_received': ('📬', 'Offers Received',  '#8b5cf6'),
    'hauler_selected': ('✅', 'Hauler Selected',  '#059669'),
    'scheduled':       ('📅', 'Scheduled',        '#2563eb'),
    'picked_up':       ('📦', 'Picked Up',        '#d97706'),
    'in_transit':      ('🚛', 'In Transit',       '#ea580c'),
    'delivered':       ('🏁', 'Delivered',        '#16a34a'),
    'cancelled':       ('❌', 'Cancelled',        '#dc2626'),
}


def _delivery_status_meta(status):
    return _DELIVERY_STATUS_META.get(status, ('⚪', status.replace('_', ' ').title(), '#6b7280'))


@app.route("/listing/<int:listing_id>/request-delivery", methods=["GET", "POST"])
@require_login
def listing_request_delivery(listing_id):
    listing = Listing.query.get_or_404(listing_id)
    if listing.is_property:
        abort(404)
    is_public = listing.status in ('active', 'sold', 'reserved') and listing.moderation_status == 'approved'
    if not is_public:
        abort(404)
    if current_user.id == listing.seller_id:
        flash("You can't request delivery for your own listing.", "error")
        return redirect(url_for('listing_detail', listing_id=listing_id))

    photos = sorted(listing.photos, key=lambda p: (0 if p.is_primary else 1, p.display_order))

    if request.method == 'GET':
        return render_template('delivery_request_form.html', listing=listing, photos=photos)

    # ── POST: create delivery request ───────────────────────────────────────
    pickup_address  = request.form.get('pickup_address', '').strip()
    pickup_city     = request.form.get('pickup_city', '').strip()
    pickup_state    = request.form.get('pickup_state', 'MN').strip()
    pickup_zip      = request.form.get('pickup_zip', '').strip()
    delivery_address = request.form.get('delivery_address', '').strip()
    delivery_city   = request.form.get('delivery_city', '').strip()
    delivery_state  = request.form.get('delivery_state', 'MN').strip()
    delivery_zip    = request.form.get('delivery_zip', '').strip()
    preferred_date  = request.form.get('preferred_date', '').strip() or None
    preferred_time  = request.form.get('preferred_time', '').strip() or None
    item_count      = max(1, int(request.form.get('item_count', 1) or 1))
    approx_dimensions = request.form.get('approx_dimensions', '').strip() or None
    pickup_stairs   = request.form.get('pickup_stairs') == '1'
    delivery_stairs = request.form.get('delivery_stairs') == '1'
    need_loading    = request.form.get('need_loading') == '1'
    need_unloading  = request.form.get('need_unloading') == '1'
    special_instructions = request.form.get('special_instructions', '').strip() or None

    if not pickup_zip or not delivery_zip:
        flash("Pickup and delivery ZIP codes are required.", "error")
        return render_template('delivery_request_form.html', listing=listing, photos=photos)

    # Build public job description (no street addresses)
    extras = []
    if approx_dimensions: extras.append(f"Size: {approx_dimensions}")
    extras.append(f"{item_count} item(s)")
    if pickup_stairs:   extras.append("stairs at pickup")
    if delivery_stairs: extras.append("stairs at delivery")
    if need_loading:    extras.append("loading help needed")
    if need_unloading:  extras.append("unloading help needed")
    job_desc = f"Marketplace delivery: {listing.title}" + (" | " + " | ".join(extras) if extras else "")
    if special_instructions:
        job_desc += f"\nNotes: {special_instructions}"

    buyer_name = ((current_user.first_name or '') + ' ' + (current_user.last_name or '')).strip() or current_user.email

    # Create Job so haulers can bid through existing infrastructure
    job = Job(
        customer_id=current_user.id,
        customer_name=buyer_name,
        customer_phone=current_user.phone or '',
        pickup_address=f"{pickup_city}, {pickup_state} {pickup_zip}".strip(', '),
        pickup_zip=pickup_zip,
        preferred_date=preferred_date,
        preferred_time=preferred_time,
        job_description=job_desc,
        service_type='marketplace_delivery',
        status='open',
    )
    db.session.add(job)
    db.session.flush()

    dr = DeliveryRequest(
        listing_id=listing_id,
        buyer_id=current_user.id,
        seller_id=listing.seller_id,
        pickup_address=pickup_address,
        pickup_city=pickup_city,
        pickup_state=pickup_state,
        pickup_zip=pickup_zip,
        pickup_stairs=pickup_stairs,
        delivery_address=delivery_address,
        delivery_city=delivery_city,
        delivery_state=delivery_state,
        delivery_zip=delivery_zip,
        delivery_stairs=delivery_stairs,
        item_description=listing.title,
        approx_dimensions=approx_dimensions,
        item_count=item_count,
        preferred_date=preferred_date,
        preferred_time=preferred_time,
        special_instructions=special_instructions,
        need_loading=need_loading,
        need_unloading=need_unloading,
        job_id=job.id,
        status='requested',
    )
    db.session.add(dr)
    db.session.commit()

    # Notify haulers via SMS
    try:
        haulers = User.query.filter_by(user_type='hauler', notify_sms=True).filter(User.phone.isnot(None)).limit(30).all()
        for h in haulers:
            try:
                notify_hauler_new_job_sms(h.phone, job.id, 0)
            except Exception:
                pass
    except Exception as _e:
        app.logger.error("Delivery hauler notify failed: %s", _e)

    try:
        notify_admin_new_request_sms(job.id, buyer_name, 'marketplace_delivery', pickup_zip)
    except Exception as _e:
        app.logger.error("Delivery admin notify failed: %s", _e)

    flash("Delivery request submitted! Registered haulers will be notified.", "success")
    return redirect(url_for('delivery_detail', dr_id=dr.id))


@app.route("/delivery/<int:dr_id>")
@require_login
def delivery_detail(dr_id):
    dr = DeliveryRequest.query.get_or_404(dr_id)

    is_buyer  = current_user.id == dr.buyer_id
    is_seller = current_user.id == dr.seller_id
    is_admin  = current_user.is_admin

    # Gather bids on the linked job
    hauler_bids = []
    selected_bid = None
    hauler_own_bid = None

    if dr.job_id:
        hauler_bids = Bid.query.filter_by(job_id=dr.job_id).order_by(Bid.created_at.asc()).all()
        selected_bid = next((b for b in hauler_bids if b.status == 'accepted'), None)
        hauler_own_bid = next((b for b in hauler_bids if b.hauler_id == current_user.id), None)

    is_selected_hauler = bool(selected_bid and selected_bid.hauler_id == current_user.id)

    # Access control
    if not (is_buyer or is_seller or is_admin or hauler_own_bid or current_user.user_type == 'hauler'):
        abort(403)

    # Haulers browsing available deliveries can see the masked view
    if current_user.user_type == 'hauler' and not (is_buyer or is_seller or is_admin):
        if dr.status in ('cancelled', 'delivered'):
            abort(403)

    reveal_addresses = is_buyer or is_seller or is_admin or is_selected_hauler

    # Listing photo
    listing_photo = None
    if dr.listing:
        lp = sorted(dr.listing.photos, key=lambda p: (0 if p.is_primary else 1, p.display_order))
        listing_photo = lp[0] if lp else None

    icon, label, color = _delivery_status_meta(dr.status)
    return render_template('delivery_detail.html',
        dr=dr, is_buyer=is_buyer, is_seller=is_seller,
        is_admin=is_admin, is_selected_hauler=is_selected_hauler,
        reveal_addresses=reveal_addresses,
        hauler_bids=hauler_bids, selected_bid=selected_bid,
        hauler_own_bid=hauler_own_bid,
        listing_photo=listing_photo,
        status_icon=icon, status_label=label, status_color=color,
    )


@app.route("/delivery/<int:dr_id>/offer", methods=["POST"])
@require_login
def delivery_offer(dr_id):
    dr = DeliveryRequest.query.get_or_404(dr_id)
    if current_user.user_type != 'hauler':
        flash("Only registered haulers can submit delivery offers.", "error")
        return redirect(url_for('delivery_detail', dr_id=dr_id))
    if dr.status in ('delivered', 'cancelled', 'hauler_selected'):
        flash("This delivery request is no longer accepting offers.", "error")
        return redirect(url_for('delivery_detail', dr_id=dr_id))
    if not dr.job_id:
        flash("Unable to submit offer — delivery request configuration error.", "error")
        return redirect(url_for('delivery_detail', dr_id=dr_id))

    existing = Bid.query.filter_by(job_id=dr.job_id, hauler_id=current_user.id).first()
    if existing:
        flash("You already submitted an offer. Contact the buyer through the listing if you need to update it.", "info")
        return redirect(url_for('delivery_detail', dr_id=dr_id))

    try:
        quote_amount = float(request.form.get('quote_amount', '').strip())
    except (ValueError, TypeError):
        flash("Please enter a valid delivery price.", "error")
        return redirect(url_for('delivery_detail', dr_id=dr_id))

    message      = request.form.get('message', '').strip()
    availability = request.form.get('availability', '').strip()
    full_message = (f"Available: {availability}\n" if availability else '') + message

    hauler_name = ((current_user.first_name or '') + ' ' + (current_user.last_name or '')).strip() or current_user.email
    bid = Bid(
        job_id=dr.job_id,
        hauler_id=current_user.id,
        hauler_name=hauler_name,
        hauler_phone=current_user.phone or '',
        quote_amount=quote_amount,
        message=full_message,
        status='active',
    )
    db.session.add(bid)
    if dr.status == 'requested':
        dr.status = 'offers_received'
    db.session.commit()

    try:
        buyer = User.query.get(dr.buyer_id)
        if buyer and buyer.phone and buyer.notify_sms:
            send_sms(buyer.phone,
                f"JHE Haul: {hauler_name} offered ${quote_amount:.0f} to deliver your item. "
                f"View offers: {request.host_url}delivery/{dr.id}",
                event_type='delivery_offer')
    except Exception as _e:
        app.logger.error("Delivery offer SMS: %s", _e)

    flash("Delivery offer submitted! The buyer will be notified.", "success")
    return redirect(url_for('delivery_detail', dr_id=dr_id))


@app.route("/delivery/<int:dr_id>/select/<int:bid_id>", methods=["POST"])
@require_login
def delivery_select_hauler(dr_id, bid_id):
    dr = DeliveryRequest.query.get_or_404(dr_id)
    if current_user.id != dr.buyer_id and not current_user.is_admin:
        abort(403)
    if dr.status in ('delivered', 'cancelled'):
        flash("This delivery cannot be modified.", "error")
        return redirect(url_for('delivery_detail', dr_id=dr_id))

    selected = Bid.query.get_or_404(bid_id)
    if selected.job_id != dr.job_id:
        abort(400)

    # Reject all other bids
    if dr.job_id:
        Bid.query.filter_by(job_id=dr.job_id).filter(Bid.id != bid_id).update({'status': 'not_selected'})

    selected.status = 'accepted'
    dr.status = 'hauler_selected'
    db.session.commit()

    try:
        hauler = User.query.get(selected.hauler_id)
        if hauler and hauler.phone and hauler.notify_sms:
            send_sms(hauler.phone,
                f"JHE Haul: Your delivery offer was selected! Full pickup/drop-off details are now visible. "
                f"View: {request.host_url}delivery/{dr.id}",
                event_type='delivery_selected')
    except Exception as _e:
        app.logger.error("Delivery select SMS: %s", _e)

    flash("Hauler selected! They'll be notified and can now see the full addresses.", "success")
    return redirect(url_for('delivery_detail', dr_id=dr_id))


@app.route("/delivery/<int:dr_id>/update-status", methods=["POST"])
@require_login
def delivery_update_status(dr_id):
    dr = DeliveryRequest.query.get_or_404(dr_id)
    is_admin = current_user.is_admin
    is_buyer = current_user.id == dr.buyer_id

    selected_bid = None
    if dr.job_id:
        selected_bid = Bid.query.filter_by(job_id=dr.job_id, status='accepted').first()
    is_selected_hauler = bool(selected_bid and selected_bid.hauler_id == current_user.id)

    if not (is_admin or is_selected_hauler or is_buyer):
        abort(403)

    new_status = request.form.get('status', '').strip()
    valid = list(_DELIVERY_STATUS_META.keys())
    if new_status not in valid:
        flash("Invalid status.", "error")
        return redirect(url_for('delivery_detail', dr_id=dr_id))

    if is_selected_hauler and not is_admin:
        if new_status not in ('scheduled', 'picked_up', 'in_transit', 'delivered', 'cancelled'):
            abort(403)
    if is_buyer and not is_admin:
        if new_status not in ('cancelled',):
            abort(403)

    dr.status = new_status
    db.session.commit()

    _buyer_msgs = {
        'scheduled':  "Your delivery is scheduled.",
        'picked_up':  "Your item has been picked up and is on its way!",
        'in_transit': "Your item is in transit.",
        'delivered':  "Your item has been delivered! Please confirm receipt.",
        'cancelled':  "Your delivery request has been cancelled.",
    }
    try:
        if new_status in _buyer_msgs:
            buyer = User.query.get(dr.buyer_id)
            if buyer and buyer.phone and buyer.notify_sms:
                send_sms(buyer.phone,
                    f"JHE Haul Delivery: {_buyer_msgs[new_status]} "
                    f"Details: {request.host_url}delivery/{dr.id}",
                    event_type='delivery_status')
    except Exception as _e:
        app.logger.error("Delivery status SMS: %s", _e)

    flash(f"Status updated to: {new_status.replace('_', ' ').title()}", "success")
    return redirect(url_for('delivery_detail', dr_id=dr_id))


@app.route("/my-deliveries")
@require_login
def my_deliveries():
    deliveries = (DeliveryRequest.query
                  .filter_by(buyer_id=current_user.id)
                  .order_by(DeliveryRequest.created_at.desc())
                  .all())
    return render_template('my_deliveries.html', deliveries=deliveries,
                           status_meta=_DELIVERY_STATUS_META)


@app.route("/hauler/deliveries")
@require_login
def hauler_deliveries_page():
    if current_user.user_type != 'hauler':
        flash("This page is for registered haulers only.", "error")
        return redirect(url_for('home'))

    open_statuses = ('requested', 'offers_received')
    available_drs = (DeliveryRequest.query
                     .filter(DeliveryRequest.status.in_(open_statuses))
                     .order_by(DeliveryRequest.created_at.desc())
                     .all())

    # Map job_id → my bid for quick lookup
    all_job_ids = [dr.job_id for dr in available_drs if dr.job_id]
    my_bids_map = {}
    if all_job_ids:
        my_bids = Bid.query.filter(Bid.job_id.in_(all_job_ids),
                                   Bid.hauler_id == current_user.id).all()
        my_bids_map = {b.job_id: b for b in my_bids}

    # Active deliveries this hauler was selected for
    my_active_bids = Bid.query.filter_by(hauler_id=current_user.id, status='accepted').all()
    my_active_drs = []
    if my_active_bids:
        act_job_ids = [b.job_id for b in my_active_bids]
        my_active_drs = (DeliveryRequest.query
                         .filter(DeliveryRequest.job_id.in_(act_job_ids),
                                 DeliveryRequest.status.notin_(['delivered', 'cancelled']))
                         .all())

    return render_template('hauler_deliveries.html',
        available_drs=available_drs,
        my_bids_map=my_bids_map,
        my_active_drs=my_active_drs,
        status_meta=_DELIVERY_STATUS_META,
    )


@app.route("/admin/deliveries")
@require_admin
def admin_deliveries():
    status_filter = request.args.get('status', '')
    q = DeliveryRequest.query
    if status_filter:
        q = q.filter_by(status=status_filter)
    deliveries = q.order_by(DeliveryRequest.created_at.desc()).all()

    stats = {}
    for s in _DELIVERY_STATUS_META:
        stats[s] = DeliveryRequest.query.filter_by(status=s).count()
    stats['total'] = DeliveryRequest.query.count()

    return render_template('admin_deliveries.html',
        deliveries=deliveries, stats=stats,
        status_filter=status_filter,
        status_meta=_DELIVERY_STATUS_META,
    )


# ── end MARKETPLACE DELIVERY ──────────────────────────────────────────────────

@app.route("/hauler/jobs")
def hauler_jobs():
    return redirect(url_for('home'))

@app.route("/hauler/bid/<int:job_id>", methods=["GET", "POST"])
def hauler_bid_form(job_id):
    return redirect(url_for('home'))

def hauler_bid_submit(job_id):
    return redirect(url_for('home'))

@app.route("/hauler/dashboard")
def hauler_dashboard():
    return redirect(url_for('home'))

@app.route("/profile")
@require_login
def profile():
    from models import Listing as _Listing
    listing_count = _Listing.query.filter_by(
        seller_id=current_user.id
    ).filter(_Listing.status != 'draft').count()
    invite_url = request.host_url.rstrip('/') + '/invite'
    return render_template('profile.html',
                           listing_count=listing_count,
                           invite_url=invite_url)

@app.route("/hauler/service-zips/add", methods=["POST"])
def hauler_service_zip_add():
    return redirect(url_for('home'))


@app.route("/hauler/service-zips/remove", methods=["POST"])
def hauler_service_zip_remove():
    return redirect(url_for('home'))


@app.route("/profile/update", methods=["POST"])
@require_login
def profile_update():
    import re as _re
    first_name = request.form.get("first_name", "").strip()
    last_name  = request.form.get("last_name",  "").strip()
    phone      = strip_phone(request.form.get("phone", ""))
    city_raw   = request.form.get("city", "").strip()[:100] or None
    zip_raw    = request.form.get("zip_code", "").strip()

    current_user.first_name = first_name
    current_user.last_name  = last_name
    current_user.phone      = phone or None
    current_user.city       = city_raw

    # ZIP validation — uses existing ZipCode service-area table
    if zip_raw:
        if not _re.match(r'^\d{5}$', zip_raw):
            flash("Please enter a valid 5-digit ZIP code.", "error")
            return redirect(url_for('profile'))
        from models import ZipCode as _ZC
        if not _ZC.query.get(zip_raw):
            flash("That ZIP code isn't in our service area yet (MN & WI only).", "error")
            return redirect(url_for('profile'))
        current_user.zip_code = zip_raw
    else:
        current_user.zip_code = None

    # SMS notifications — available to all users
    notify_sms = request.form.get("notify_sms") == "1"
    current_user.notify_sms = notify_sms

    # SMS consent
    from datetime import datetime as _dt
    sms_consent_form = request.form.get("sms_consent") == "1"
    if sms_consent_form and not current_user.sms_consent:
        current_user.sms_consent = True
        current_user.sms_consent_at = _dt.now()
    elif not sms_consent_form:
        current_user.sms_consent = False

    # If phone was cleared, reset verification
    if not phone:
        current_user.phone_verified = False
        current_user.phone_verify_code = None

    db.session.commit()
    flash("Profile updated successfully!", "success")
    return redirect(url_for('profile'))

@app.route("/account/delete", methods=["POST"])
@require_login
def delete_account():
    from models import (OAuth, Bid, Review, CompletionPhoto,
                        Listing, ListingFavorite, ListingConversation,
                        ListingMessage, ListingOffer, DeliveryRequest,
                        UserBlock, ListingReport)
    user_id   = current_user.id
    user_type = current_user.user_type

    # Confirm typed "DELETE" from the form
    if request.form.get("confirm_delete", "").strip().upper() != "DELETE":
        flash("Type DELETE in the confirmation box to delete your account.", "error")
        return redirect(url_for('profile'))

    # ── Legacy hauling data cleanup ──────────────────────────────────────────
    if user_type == 'customer':
        jobs = Job.query.filter_by(customer_id=user_id).all()
        active_jobs = [j for j in jobs if j.status in [
            'open', 'bidding', 'accepted', 'deposit_paid',
            'reviewing', 'quoted', 'waiting_for_payment', 'scheduled', 'in_progress'
        ]]
        if active_jobs:
            flash("You have active jobs. Please complete or cancel them before deleting your account.", "error")
            return redirect(url_for('profile'))
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
                flash("You have active accepted jobs. Please complete them before deleting your account.", "error")
                return redirect(url_for('profile'))
        Bid.query.filter_by(hauler_id=user_id).delete()
        Review.query.filter_by(hauler_id=user_id).delete()
        CompletionPhoto.query.filter_by(hauler_id=user_id).delete()

    # ── Marketplace data cleanup ─────────────────────────────────────────────
    # Seller listings (cascade: photos, favorites, offers, conversations, reports)
    for listing in Listing.query.filter_by(seller_id=user_id).all():
        db.session.delete(listing)
    db.session.flush()  # run cascades before buyer-side deletes

    # Buyer-side records on other sellers' listings
    ListingFavorite.query.filter_by(buyer_id=user_id).delete(synchronize_session=False)
    ListingOffer.query.filter_by(buyer_id=user_id).delete(synchronize_session=False)
    ListingConversation.query.filter_by(buyer_id=user_id).delete(synchronize_session=False)
    DeliveryRequest.query.filter_by(buyer_id=user_id).delete(synchronize_session=False)
    ListingReport.query.filter_by(reporter_id=user_id).delete(synchronize_session=False)
    ListingMessage.query.filter_by(sender_id=user_id).delete(synchronize_session=False)
    UserBlock.query.filter(
        (UserBlock.blocker_id == user_id) | (UserBlock.blocked_id == user_id)
    ).delete(synchronize_session=False)

    # ── Core account removal ─────────────────────────────────────────────────
    _del_name  = (((current_user.first_name or '') + ' ' + (current_user.last_name or '')).strip()
                  or current_user.email or 'Unknown')
    _del_email = current_user.email or ''
    OAuth.query.filter_by(user_id=user_id).delete()
    db.session.delete(current_user)
    db.session.commit()

    try:
        notify_admin_user_deleted(_del_name, _del_email, user_type or '')
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
    if job.status not in ('deposit_paid', 'scheduled', 'in_progress'):
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
        if current_user.notify_sms and current_user.phone:
            notify_customer_job_completed_sms(current_user.phone, job.id)
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
    if job.status not in ['open', 'bidding', 'reviewing', 'quoted', 'waiting_for_payment']:
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
            if bh and bh.notify_sms and bh.phone:
                notify_hauler_job_cancelled_sms(bh.phone, job.id)
    except Exception as e:
        app.logger.error("Hauler cancel notify failed (job #%s): %s", job.id, e)

    return redirect(url_for('customer_jobs'))

@app.route("/customer/job/<int:job_id>/reactivate", methods=["POST"])
@require_role('customer')
def customer_reactivate_job(job_id):
    from models import Bid as _Bid
    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    if job.status != 'expired':
        return "Job is not expired", 400
    bid_count = _Bid.query.filter_by(job_id=job.id).count()
    job.status = 'bidding' if bid_count > 0 else 'open'
    job.expired_at = None
    job.reminder_24h_sent = False
    job.reminder_48h_sent = False
    db.session.commit()
    return redirect(url_for('customer_job_detail', job_id=job_id))

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
def hauler_earnings():
    return redirect(url_for('home'))

@app.route("/hauler/upload_photos/<int:job_id>", methods=["GET", "POST"])
def hauler_upload_photos(job_id):
    return redirect(url_for('home'))


@app.route("/customer/dashboard")
@require_login
def customer_dashboard():
    if current_user.is_admin:
        return redirect(url_for('admin_dashboard'))
    return redirect(url_for('customer_jobs'))

@app.route("/admin")
@require_admin
def admin_dashboard():
    import os as _os
    from models import SmsLog as _SmsLog

    # Marketplace stats
    total_users = User.query.count()
    active_listings = Listing.query.filter_by(status='active').count()
    pending_listings = Listing.query.filter(
        db.or_(Listing.status == 'pending', Listing.moderation_status == 'pending')
    ).count()
    sold_items = Listing.query.filter_by(status='sold').count()
    reported_listings = ListingReport.query.filter_by(status='pending').count()
    total_listings = Listing.query.count()
    homes_for_sale = Listing.query.filter_by(listing_type='property_sale').count()
    rental_listings = Listing.query.filter_by(listing_type='rental').count()
    housing_listings = homes_for_sale + rental_listings

    # Recent activity
    recent_listings = Listing.query.order_by(Listing.created_at.desc()).limit(8).all()
    recent_users = User.query.order_by(User.created_at.desc()).limit(5).all()
    recent_sold = (Listing.query.filter_by(status='sold')
                   .order_by(Listing.sold_at.desc()).limit(5).all())
    pending_reports = (ListingReport.query.filter_by(status='pending')
                       .order_by(ListingReport.created_at.desc()).limit(5).all())

    categories_count = Category.query.count()

    # SMS/infra stats
    sms_sent_total = _SmsLog.query.filter_by(status='sent').count()
    sms_failed_total = _SmsLog.query.filter_by(status='failed').count()
    twilio_configured = bool(_os.environ.get("TWILIO_ACCOUNT_SID"))
    spaces_configured = bool(_os.environ.get("SPACES_KEY"))

    return render_template('admin_dashboard.html',
                           total_users=total_users,
                           active_listings=active_listings,
                           pending_listings=pending_listings,
                           sold_items=sold_items,
                           reported_listings=reported_listings,
                           total_listings=total_listings,
                           homes_for_sale=homes_for_sale,
                           rental_listings=rental_listings,
                           housing_listings=housing_listings,
                           recent_listings=recent_listings,
                           recent_users=recent_users,
                           recent_sold=recent_sold,
                           pending_reports=pending_reports,
                           categories_count=categories_count,
                           sms_sent_total=sms_sent_total,
                           sms_failed_total=sms_failed_total,
                           twilio_configured=twilio_configured,
                           spaces_configured=spaces_configured)

@app.route("/admin/customers")
@require_admin
def admin_customers():
    from models import Listing as _Listing
    members = User.query.filter(User.is_admin == False).order_by(User.created_at.desc()).all()
    total = len(members)
    jobs_map = {}
    for m in members:
        jobs_map[m.id] = _Listing.query.filter(
            _Listing.seller_id == m.id,
            _Listing.status != 'draft'
        ).count()
    return render_template('admin_customers.html',
                           members=members,
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
    elif notification_type == "admin_job_expired":
        from email_service import notify_admin_job_expired
        success = notify_admin_job_expired(999, "Test Customer", 3)
    elif notification_type == "customer_bid_reminder_24h":
        from email_service import notify_customer_pending_bids_reminder
        success = notify_customer_pending_bids_reminder(email, 999, 2)
    elif notification_type == "customer_bid_reminder_48h":
        from email_service import notify_customer_job_expiring_soon
        success = notify_customer_job_expiring_soon(email, 999)

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
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "noreply@jhehaul.com")
    type_labels = {
        # Admin notifications
        'admin_new_customer':    'Admin — New Customer Signup',
        'admin_new_hauler':      'Admin — New Hauler Signup',
        'admin_new_job':         'Admin — New Job Posted',
        'admin_new_bid':         'Admin — New Bid Submitted',
        'admin_bid_accepted':    'Admin — Bid Accepted',
        'admin_deposit_paid':    'Admin — Deposit Paid',
        'admin_job_completed':   'Admin — Job Completed',
        'admin_job_cancelled':   'Admin — Job Cancelled',
        'admin_job_expired':     'Admin — Job Auto-Expired',
        'admin_user_deleted':    'Admin — Account Deleted',
        # Customer notifications
        'customer_new_bid':              'Customer — New Bid Received',
        'customer_bid_accepted_confirm': 'Customer — Bid Accepted (Pay Deposit)',
        'customer_job_completed':        'Customer — Job Complete (Review Request)',
        'customer_bid_reminder_24h':     'Customer — Reminder: Bids Waiting (24h)',
        'customer_bid_reminder_48h':     'Customer — Job Expiring Soon (48h)',
        # Hauler notifications
        'hauler_new_job_nearby':  'Hauler — New Job Nearby',
        'hauler_bid_accepted':    'Hauler — Bid Accepted',
        'hauler_bid_rejected':    'Hauler — Bid Not Selected',
        'hauler_deposit_paid':    'Hauler — Deposit Paid (Address Unlocked)',
        'hauler_job_cancelled':   'Hauler — Job Cancelled by Customer',
        'hauler_new_review':      'Hauler — New Review Received',
        # Legacy / test
        'email':                  'General Email',
        'admin':                  'Admin (general)',
    }
    return render_template('admin_notifications.html',
                           logs=logs, sent=sent, failed=failed,
                           sendgrid_configured=sendgrid_configured,
                           from_email=from_email,
                           type_labels=type_labels)


@app.route("/admin/suppression-check")
@require_admin
def admin_suppression_check():
    """Query SendGrid suppression lists for a specific email address."""
    import urllib.request, urllib.parse, urllib.error, json as _json
    email = request.args.get("email", "").strip()
    if not email:
        return jsonify({"error": "No email provided"}), 400

    api_key = os.environ.get("SENDGRID_API_KEY")
    if not api_key:
        return jsonify({"error": "SENDGRID_API_KEY is not configured"}), 500

    encoded = urllib.parse.quote(email, safe='')
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    checks = {
        "bounces":       f"https://api.sendgrid.com/v3/suppression/bounces/{encoded}",
        "blocks":        f"https://api.sendgrid.com/v3/suppression/blocks/{encoded}",
        "spam_reports":  f"https://api.sendgrid.com/v3/suppression/spam_reports/{encoded}",
        "invalid_emails":f"https://api.sendgrid.com/v3/suppression/invalid_emails/{encoded}",
        "unsubscribes":  f"https://api.sendgrid.com/v3/asm/suppressions/global/{encoded}",
    }

    results = {}
    suppressed_in = []

    for name, url in checks.items():
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as r:
                data = _json.loads(r.read())
                # bounces/blocks/spam/invalid return a list; unsubscribes returns {"recipient_unsubscribes":[...]}
                if isinstance(data, list):
                    results[name] = data
                    if data:
                        suppressed_in.append(name)
                elif isinstance(data, dict):
                    inner = data.get("recipient_unsubscribes") or data.get("suppressions") or []
                    results[name] = inner
                    if inner:
                        suppressed_in.append(name)
                else:
                    results[name] = data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                results[name] = []   # 404 = not on this list
            else:
                body = ""
                try:
                    body = e.read().decode()[:300]
                except Exception:
                    pass
                results[name] = {"error": f"HTTP {e.code}", "detail": body}
        except Exception as ex:
            results[name] = {"error": str(ex)}

    return jsonify({
        "email": email,
        "is_suppressed": len(suppressed_in) > 0,
        "suppressed_in": suppressed_in,
        "details": results,
    })


@app.route("/admin/delete-job/<int:job_id>", methods=["POST"])
@require_admin
def admin_delete_job(job_id):
    job = Job.query.get_or_404(job_id)
    db.session.delete(job)
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route("/admin/job/<int:job_id>/reactivate", methods=["POST"])
@require_admin
def admin_reactivate_job(job_id):
    job = Job.query.get_or_404(job_id)
    bid_count = Bid.query.filter_by(job_id=job.id).count()
    job.status = 'bidding' if bid_count > 0 else 'open'
    job.expired_at = None
    job.reminder_24h_sent = False
    job.reminder_48h_sent = False
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
    return redirect(url_for('admin_users'))


# ── Admin: Listings ────────────────────────────────────────────────────────

@app.route("/admin/listings")
@require_admin
def admin_listings():
    q = request.args.get('q', '').strip()
    status_filter   = request.args.get('status', '')
    category_filter = request.args.get('category', '')
    lt_filter       = request.args.get('listing_type', '')

    query = Listing.query
    if q:
        query = query.filter(Listing.title.ilike(f'%{q}%'))
    if status_filter:
        query = query.filter_by(status=status_filter)
    if category_filter:
        try:
            query = query.filter_by(category_id=int(category_filter))
        except (ValueError, TypeError):
            pass
    if lt_filter in ('item', 'property_sale', 'rental'):
        query = query.filter(Listing.listing_type == lt_filter)

    listings = query.order_by(Listing.created_at.desc()).limit(200).all()
    categories = Category.query.order_by(Category.display_order).all()
    total = Listing.query.count()
    return render_template('admin_listings.html',
                           listings=listings, categories=categories,
                           total=total, q=q, status_filter=status_filter,
                           category_filter=category_filter)


@app.route("/admin/listings/<int:listing_id>/approve", methods=["POST"])
@require_admin
def admin_listing_approve(listing_id):
    _check_listing_csrf()
    listing = Listing.query.get_or_404(listing_id)
    listing.moderation_status = 'approved'
    if listing.status == 'pending':
        listing.status = 'active'
    db.session.commit()
    flash(f'Listing "{listing.title}" approved.', 'success')
    return redirect(request.referrer or url_for('admin_listings'))


@app.route("/admin/listings/<int:listing_id>/hide", methods=["POST"])
@require_admin
def admin_listing_hide(listing_id):
    _check_listing_csrf()
    listing = Listing.query.get_or_404(listing_id)
    listing.moderation_status = 'flagged'
    listing.status = 'removed'
    db.session.commit()
    flash(f'Listing "{listing.title}" hidden.', 'success')
    return redirect(request.referrer or url_for('admin_listings'))


@app.route("/admin/listings/<int:listing_id>/remove", methods=["POST"])
@require_admin
def admin_listing_remove(listing_id):
    _check_listing_csrf()
    listing = Listing.query.get_or_404(listing_id)
    listing.moderation_status = 'removed'
    listing.status = 'removed'
    db.session.commit()
    flash(f'Listing "{listing.title}" removed.', 'success')
    return redirect(request.referrer or url_for('admin_listings'))


@app.route("/admin/listings/<int:listing_id>/sold", methods=["POST"])
@require_admin
def admin_listing_mark_sold(listing_id):
    _check_listing_csrf()
    listing = Listing.query.get_or_404(listing_id)
    listing.status = 'sold'
    listing.sold_at = datetime.now()
    db.session.commit()
    flash(f'Listing "{listing.title}" marked as sold.', 'success')
    return redirect(request.referrer or url_for('admin_listings'))


@app.route("/admin/listings/<int:listing_id>/restore", methods=["POST"])
@require_admin
def admin_listing_restore(listing_id):
    _check_listing_csrf()
    listing = Listing.query.get_or_404(listing_id)
    if listing.moderation_status not in ('removed', 'flagged') and listing.status != 'removed':
        flash('Listing cannot be restored from its current state.', 'error')
        return redirect(request.referrer or url_for('admin_listings'))
    listing.moderation_status = 'approved'
    listing.status = 'active'
    db.session.commit()
    flash(f'Listing "{listing.title}" restored to active.', 'success')
    return redirect(request.referrer or url_for('admin_listings'))


@app.route("/admin/listings/cleanup-drafts", methods=["POST"])
@require_admin
def admin_cleanup_drafts():
    """Manually trigger purge of abandoned draft listings older than 48 h with no title/photos."""
    from draft_cleanup import purge_abandoned_drafts
    try:
        deleted = purge_abandoned_drafts(app)
        flash(f"Draft cleanup complete — {deleted} abandoned draft(s) removed.", "success")
    except Exception as exc:
        app.logger.error("admin_cleanup_drafts error: %s", exc)
        flash("Draft cleanup failed — check server logs.", "error")
    return redirect(request.referrer or url_for('admin_listings'))


@app.route("/admin/listings/<int:listing_id>/toggle_featured", methods=["POST"])
@require_admin
def admin_listing_toggle_featured(listing_id):
    _check_listing_csrf()
    listing = Listing.query.get_or_404(listing_id)
    listing.featured = not listing.featured
    db.session.commit()
    state = 'featured' if listing.featured else 'unfeatured'
    flash(f'Listing "{listing.title}" {state}.', 'success')
    return redirect(request.referrer or url_for('admin_listings'))


@app.route("/admin/listings/<int:listing_id>")
@require_admin
def admin_listing_detail(listing_id):
    listing = Listing.query.get_or_404(listing_id)
    return render_template('admin_listing_detail.html', listing=listing)


# ── Admin: Users ───────────────────────────────────────────────────────────

@app.route("/admin/users")
@require_admin
def admin_users():
    q = request.args.get('q', '').strip()
    query = User.query
    if q:
        query = query.filter(
            db.or_(
                User.first_name.ilike(f'%{q}%'),
                User.last_name.ilike(f'%{q}%'),
                User.email.ilike(f'%{q}%')
            )
        )
    users = query.order_by(User.created_at.desc()).all()
    listing_counts = {u.id: Listing.query.filter_by(seller_id=u.id).count() for u in users}
    total = len(users)
    return render_template('admin_users.html', users=users, total=total,
                           listing_counts=listing_counts, q=q)


@app.route("/admin/users/<string:user_id>/suspend", methods=["POST"])
@require_admin
def admin_user_suspend(user_id):
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash("Admin accounts cannot be suspended.", "error")
        return redirect(url_for('admin_users'))
    user.is_suspended = True
    db.session.commit()
    name = ((user.first_name or '') + ' ' + (user.last_name or '')).strip() or user.email
    flash(f"{name}'s account suspended.", "success")
    return redirect(url_for('admin_users'))


@app.route("/admin/users/<string:user_id>/restore", methods=["POST"])
@require_admin
def admin_user_restore(user_id):
    user = User.query.get_or_404(user_id)
    user.is_suspended = False
    db.session.commit()
    name = ((user.first_name or '') + ' ' + (user.last_name or '')).strip() or user.email
    flash(f"{name}'s account restored.", "success")
    return redirect(url_for('admin_users'))


@app.route("/admin/users/<user_id>/detail")
@require_admin
def admin_user_detail(user_id):
    """Admin: view a single user's profile, listings, and reports."""
    from models import Listing as _L, UserReport, ListingReport
    seller = User.query.get_or_404(user_id)
    listings = _L.query.filter_by(seller_id=user_id).order_by(_L.created_at.desc()).all()
    user_reports = (UserReport.query
                    .filter_by(reported_user_id=str(user_id))
                    .order_by(UserReport.created_at.desc())
                    .all())
    listing_reports = (db.session.query(ListingReport)
                       .join(_L, ListingReport.listing_id == _L.id)
                       .filter(_L.seller_id == user_id)
                       .order_by(ListingReport.created_at.desc())
                       .all())
    return render_template('admin_user_detail.html',
                           seller=seller,
                           listings=listings,
                           user_reports=user_reports,
                           listing_reports=listing_reports)


# ── Admin: Reports ─────────────────────────────────────────────────────────

@app.route("/admin/reports")
@require_admin
def admin_reports():
    status_filter = request.args.get('status', 'pending')
    query = ListingReport.query
    if status_filter and status_filter != 'all':
        query = query.filter_by(status=status_filter)
    reports = query.order_by(ListingReport.created_at.desc()).all()
    pending_count = ListingReport.query.filter_by(status='pending').count()
    return render_template('admin_reports.html', reports=reports,
                           status_filter=status_filter, pending_count=pending_count)


@app.route("/admin/reports/<int:report_id>/dismiss", methods=["POST"])
@require_admin
def admin_report_dismiss(report_id):
    report = ListingReport.query.get_or_404(report_id)
    report.status = 'resolved'
    db.session.commit()
    flash("Report dismissed.", "success")
    return redirect(url_for('admin_reports'))


@app.route("/admin/reports/<int:report_id>/remove-listing", methods=["POST"])
@require_admin
def admin_report_remove_listing(report_id):
    report = ListingReport.query.get_or_404(report_id)
    listing = Listing.query.get(report.listing_id)
    if listing:
        listing.status = 'removed'
        listing.moderation_status = 'removed'
    report.status = 'resolved'
    db.session.commit()
    flash("Listing removed and report resolved.", "success")
    return redirect(url_for('admin_reports'))


@app.route("/admin/reports/<int:report_id>/restore-listing", methods=["POST"])
@require_admin
def admin_report_restore_listing(report_id):
    report = ListingReport.query.get_or_404(report_id)
    listing = Listing.query.get(report.listing_id)
    if listing:
        listing.status = 'active'
        listing.moderation_status = 'approved'
    report.status = 'resolved'
    db.session.commit()
    flash("Listing restored and report resolved.", "success")
    return redirect(url_for('admin_reports'))


@app.route("/admin/reports/<int:report_id>/suspend-seller", methods=["POST"])
@require_admin
def admin_report_suspend_seller(report_id):
    report = ListingReport.query.get_or_404(report_id)
    listing = Listing.query.get(report.listing_id)
    if listing:
        seller = User.query.get(listing.seller_id)
        if seller and not seller.is_admin:
            seller.is_suspended = True
            report.status = 'resolved'
            db.session.commit()
            flash("Seller suspended and report resolved.", "success")
        else:
            flash("Cannot suspend this seller.", "error")
    else:
        flash("Listing not found.", "error")
    return redirect(url_for('admin_reports'))


# ── Admin: User Reports ─────────────────────────────────────────────────────

@app.route("/admin/user-reports")
@require_admin
def admin_user_reports():
    """Admin view of user-against-user reports."""
    from models import UserReport
    status_filter = request.args.get('status', 'pending')
    query = UserReport.query
    if status_filter and status_filter != 'all':
        query = query.filter_by(status=status_filter)
    reports = query.order_by(UserReport.created_at.desc()).all()
    pending_count = UserReport.query.filter_by(status='pending').count()
    return render_template('admin_user_reports.html', reports=reports,
                           status_filter=status_filter, pending_count=pending_count)


@app.route("/admin/user-reports/<int:report_id>/dismiss", methods=["POST"])
@require_admin
def admin_user_report_dismiss(report_id):
    from models import UserReport
    report = UserReport.query.get_or_404(report_id)
    report.status = 'resolved'
    db.session.commit()
    flash("Report dismissed.", "success")
    return redirect(url_for('admin_user_reports'))


@app.route("/admin/user-reports/<int:report_id>/suspend", methods=["POST"])
@require_admin
def admin_user_report_suspend(report_id):
    from models import UserReport
    report = UserReport.query.get_or_404(report_id)
    user = User.query.get(report.reported_user_id)
    if user and not user.is_admin:
        user.is_suspended = True
        report.status = 'resolved'
        db.session.commit()
        flash("User suspended and report resolved.", "success")
    else:
        flash("Cannot suspend this user.", "error")
    return redirect(url_for('admin_user_reports'))


# ── Admin: Categories ──────────────────────────────────────────────────────

@app.route("/admin/categories")
@require_admin
def admin_categories():
    categories = Category.query.order_by(Category.display_order, Category.name).all()
    listing_counts = {c.id: Listing.query.filter_by(category_id=c.id).count() for c in categories}
    return render_template('admin_categories.html', categories=categories,
                           listing_counts=listing_counts)


@app.route("/admin/categories/add", methods=["POST"])
@require_admin
def admin_category_add():
    import re
    name = request.form.get('name', '').strip()
    icon = request.form.get('icon', '').strip()
    display_order = request.form.get('display_order', 0)
    if not name:
        flash("Category name is required.", "error")
        return redirect(url_for('admin_categories'))
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    base_slug = slug
    i = 1
    while Category.query.filter_by(slug=slug).first():
        slug = f"{base_slug}-{i}"
        i += 1
    cat = Category(name=name, slug=slug, icon=icon or None,
                   display_order=int(display_order) if display_order else 0)
    db.session.add(cat)
    db.session.commit()
    flash(f'Category "{name}" added.', 'success')
    return redirect(url_for('admin_categories'))


@app.route("/admin/categories/<int:cat_id>/edit", methods=["POST"])
@require_admin
def admin_category_edit(cat_id):
    cat = Category.query.get_or_404(cat_id)
    name = request.form.get('name', '').strip()
    icon = request.form.get('icon', '').strip()
    display_order = request.form.get('display_order', 0)
    if name:
        cat.name = name
    if icon:
        cat.icon = icon
    cat.display_order = int(display_order) if display_order else 0
    db.session.commit()
    flash(f'Category "{cat.name}" updated.', 'success')
    return redirect(url_for('admin_categories'))


@app.route("/admin/categories/<int:cat_id>/toggle", methods=["POST"])
@require_admin
def admin_category_toggle(cat_id):
    cat = Category.query.get_or_404(cat_id)
    cat.is_active = not cat.is_active
    db.session.commit()
    state = 'enabled' if cat.is_active else 'disabled'
    flash(f'Category "{cat.name}" {state}.', 'success')
    return redirect(url_for('admin_categories'))


# ── Admin: Analytics ───────────────────────────────────────────────────────

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
    total_users     = User.query.count()
    total_customers = User.query.filter_by(user_type='customer').count()
    total_haulers   = User.query.filter_by(user_type='hauler').count()
    new_today = User.query.filter(User.created_at >= today).count()
    new_week  = User.query.filter(User.created_at >= week_ago).count()
    active_users = db.session.execute(
        db.text("SELECT COUNT(DISTINCT user_id) FROM page_views WHERE user_id IS NOT NULL AND created_at >= :s"),
        {"s": week_ago}
    ).scalar() or 0
    active_haulers = db.session.execute(
        db.text("""SELECT COUNT(DISTINCT u.id) FROM users u
                   JOIN bids b ON b.hauler_id = u.id
                   WHERE u.user_type = 'hauler' AND b.created_at >= :s"""),
        {"s": week_ago}
    ).scalar() or 0

    _ds = db.session.execute(
        db.text("SELECT DATE(created_at) AS d, COUNT(*) AS c FROM users WHERE created_at >= :s GROUP BY DATE(created_at) ORDER BY d"),
        {"s": month_ago}
    ).fetchall()
    daily_signup_labels = [str(r[0]) for r in _ds]
    daily_signup_values = [r[1] for r in _ds]

    # ── Marketplace listing stats ──────────────────────────────────────────────
    total_listings_a     = Listing.query.count()
    active_listings_a    = Listing.query.filter_by(status='active').count()
    sold_listings_a      = Listing.query.filter_by(status='sold').count()
    pending_listings_a   = Listing.query.filter(
        db.or_(Listing.status == 'pending', Listing.moderation_status == 'pending')
    ).count()
    removed_listings_a   = Listing.query.filter_by(status='removed').count()
    # Listing type breakdown
    items_for_sale_a  = Listing.query.filter_by(listing_type='item').count()
    homes_for_sale_a  = Listing.query.filter_by(listing_type='property_sale').count()
    rentals_a         = Listing.query.filter_by(listing_type='rental').count()
    # Housing sub-stats
    active_housing_a  = Listing.query.filter(
        Listing.listing_type.in_(['property_sale', 'rental']),
        Listing.status == 'active'
    ).count()
    pending_housing_a = Listing.query.filter(
        Listing.listing_type.in_(['property_sale', 'rental']),
        db.or_(Listing.status == 'pending', Listing.moderation_status == 'pending')
    ).count()
    total_housing_a   = homes_for_sale_a + rentals_a

    # Listings by category
    listings_by_cat = db.session.execute(db.text("""
        SELECT c.name, COUNT(l.id) AS cnt
        FROM categories c
        LEFT JOIN listings l ON l.category_id = c.id AND l.status = 'active'
        WHERE c.is_active = TRUE
        GROUP BY c.id, c.name
        ORDER BY cnt DESC
        LIMIT 15
    """)).fetchall()
    cat_labels = json.dumps([r[0] for r in listings_by_cat])
    cat_values = json.dumps([r[1] for r in listings_by_cat])

    # Daily listings last 30 days
    _dl = db.session.execute(
        db.text("SELECT DATE(created_at) AS d, COUNT(*) AS c FROM listings WHERE created_at >= :s GROUP BY DATE(created_at) ORDER BY d"),
        {"s": month_ago}
    ).fetchall()
    daily_listing_labels = [str(r[0]) for r in _dl]
    daily_listing_values = [r[1] for r in _dl]

    # Listing status chart
    listing_status_labels = json.dumps(['Active', 'Pending', 'Sold', 'Removed'])
    listing_status_values = json.dumps([active_listings_a, pending_listings_a, sold_listings_a, removed_listings_a])

    # ── Legacy job stats (kept for historical reference) ───────────────────────
    total_jobs     = Job.query.count()
    open_jobs      = Job.query.filter_by(status='open').count()
    active_jobs    = Job.query.filter(Job.status.in_(['accepted','deposit_paid'])).count()
    completed_jobs = Job.query.filter_by(status='completed').count()
    cancelled_jobs = Job.query.filter_by(status='cancelled').count()
    expired_jobs   = Job.query.filter_by(status='expired').count()
    total_bids     = Bid.query.count()
    bids_accepted  = db.session.execute(
        db.text("SELECT COUNT(*) FROM jobs WHERE status NOT IN ('open','cancelled') AND accepted_hauler_id IS NOT NULL")
    ).scalar() or 0
    total_revenue = db.session.query(db.func.sum(Job.accepted_quote)).filter(Job.status=='completed').scalar() or 0

    job_status_labels = ['Open','Active','Completed','Cancelled','Expired']
    job_status_values = [open_jobs, active_jobs, completed_jobs, cancelled_jobs, expired_jobs]

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

    # Top sellers by active listing count
    top_sellers = db.session.execute(db.text("""
        SELECT u.first_name, u.last_name, u.email,
               COUNT(DISTINCT l.id) AS total_listings,
               COUNT(DISTINCT CASE WHEN l.status='active' THEN l.id END) AS active_listings,
               COUNT(DISTINCT CASE WHEN l.status='sold'   THEN l.id END) AS sold_listings
        FROM users u
        JOIN listings l ON l.seller_id = u.id AND l.status != 'draft'
        GROUP BY u.id, u.first_name, u.last_name, u.email
        ORDER BY active_listings DESC, total_listings DESC
        LIMIT 10
    """)).fetchall()

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
        total_users=total_users,
        total_customers=total_customers, total_haulers=total_haulers,
        new_today=new_today, new_week=new_week, active_users=active_users,
        active_haulers=active_haulers,
        daily_signup_labels=json.dumps(daily_signup_labels),
        daily_signup_values=json.dumps(daily_signup_values),
        top_sellers=top_sellers,
        # Marketplace listing stats
        total_listings_a=total_listings_a,
        active_listings_a=active_listings_a,
        sold_listings_a=sold_listings_a,
        pending_listings_a=pending_listings_a,
        removed_listings_a=removed_listings_a,
        items_for_sale_a=items_for_sale_a,
        homes_for_sale_a=homes_for_sale_a,
        rentals_a=rentals_a,
        active_housing_a=active_housing_a,
        pending_housing_a=pending_housing_a,
        total_housing_a=total_housing_a,
        cat_labels=cat_labels, cat_values=cat_values,
        daily_listing_labels=json.dumps(daily_listing_labels),
        daily_listing_values=json.dumps(daily_listing_values),
        listing_status_labels=listing_status_labels,
        listing_status_values=listing_status_values,
        # Legacy job stats
        total_jobs=total_jobs, open_jobs=open_jobs, active_jobs=active_jobs,
        completed_jobs=completed_jobs, cancelled_jobs=cancelled_jobs, expired_jobs=expired_jobs,
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
    _today_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    _week_dt  = _today_dt - timedelta(days=7)
    w.writerow(['Total Users', User.query.count()])
    w.writerow(['Total Members (customer role)', User.query.filter_by(user_type='customer').count()])
    w.writerow(['New Today', User.query.filter(User.created_at >= _today_dt).count()])
    w.writerow(['New This Week', User.query.filter(User.created_at >= _week_dt).count()])
    w.writerow(['Active (7d)', db.session.execute(db.text("SELECT COUNT(DISTINCT user_id) FROM page_views WHERE user_id IS NOT NULL AND created_at >= :s"), {"s": _week_dt}).scalar() or 0])
    w.writerow([])

    w.writerow(['MARKETPLACE LISTING ANALYTICS', ''])
    w.writerow(['Metric', 'Value'])
    w.writerow(['Total Listings', Listing.query.count()])
    w.writerow(['Active Listings', Listing.query.filter_by(status='active').count()])
    w.writerow(['Sold Items', Listing.query.filter_by(status='sold').count()])
    w.writerow(['Pending Listings', Listing.query.filter(db.or_(Listing.status=='pending', Listing.moderation_status=='pending')).count()])
    w.writerow(['Removed Listings', Listing.query.filter_by(status='removed').count()])
    w.writerow(['Items for Sale (item type)', Listing.query.filter_by(listing_type='item').count()])
    w.writerow(['Homes for Sale', Listing.query.filter_by(listing_type='property_sale').count()])
    w.writerow(['Rental Listings', Listing.query.filter_by(listing_type='rental').count()])
    w.writerow([])

    w.writerow(['HAUL & DELIVERY ANALYTICS', ''])
    w.writerow(['Metric', 'Value'])
    w.writerow(['Registered Haulers', User.query.filter_by(user_type='hauler').count()])
    w.writerow(['Total Haul Requests', Job.query.count()])
    w.writerow(['Total Bids', Bid.query.count()])
    w.writerow(['Completed Hauls', Job.query.filter_by(status='completed').count()])
    w.writerow(['Total Haul Revenue', f"${db.session.query(db.func.sum(Job.accepted_quote)).filter(Job.status=='completed').scalar() or 0:.2f}"])
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


# ── Phone verification ────────────────────────────────────────────────────────

@app.route("/profile/send-phone-verify", methods=["POST"])
@require_login
def send_phone_verify():
    from datetime import datetime as _dt
    phone = current_user.phone
    if not phone:
        flash("Save your phone number first, then request a verification code.", "error")
        return redirect(url_for('profile'))
    code = send_verification_sms(phone)
    if code:
        current_user.phone_verify_code = code
        current_user.phone_verify_sent_at = _dt.now()
        db.session.commit()
        flash("Verification code sent! Enter the 6-digit code below.", "success")
    else:
        flash("Could not send the verification SMS. Check that Twilio is configured or try again.", "error")
    return redirect(url_for('profile'))


@app.route("/profile/verify-phone", methods=["POST"])
@require_login
def verify_phone():
    from datetime import datetime as _dt, timedelta
    code = request.form.get("verify_code", "").strip()
    if not current_user.phone_verify_code:
        flash("No verification code is pending. Please request one first.", "error")
        return redirect(url_for('profile'))
    if current_user.phone_verify_sent_at:
        expires = current_user.phone_verify_sent_at + timedelta(minutes=10)
        if _dt.now() > expires:
            current_user.phone_verify_code = None
            current_user.phone_verify_sent_at = None
            db.session.commit()
            flash("That code has expired. Please request a new verification code.", "error")
            return redirect(url_for('profile'))
    if code == current_user.phone_verify_code:
        current_user.phone_verified = True
        current_user.phone_verify_code = None
        current_user.phone_verify_sent_at = None
        db.session.commit()
        flash("Phone number verified! SMS notifications are now active.", "success")
    else:
        flash("Incorrect code — please try again.", "error")
    return redirect(url_for('profile'))


# ── Admin SMS settings ────────────────────────────────────────────────────────

@app.route("/admin/sms-settings")
@require_admin
def admin_sms_settings():
    from models import SmsSettings, SmsLog
    settings = SmsSettings.query.first()
    if not settings:
        settings = SmsSettings()
        db.session.add(settings)
        db.session.commit()
    sms_sent = SmsLog.query.filter_by(status='sent').count()
    sms_failed = SmsLog.query.filter_by(status='failed').count()
    sms_total = SmsLog.query.count()
    twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_tok = os.environ.get("TWILIO_AUTH_TOKEN")
    from_num = os.environ.get("TWILIO_PHONE_NUMBER") or os.environ.get("TWILIO_FROM_NUMBER") or ""
    twilio_configured = bool(twilio_sid and twilio_tok and from_num)
    masked = ('*' * max(0, len(from_num) - 4) + from_num[-4:]) if from_num else ""
    return render_template('admin_sms_settings.html',
                           settings=settings,
                           twilio_configured=twilio_configured,
                           from_number_masked=masked,
                           sms_sent=sms_sent,
                           sms_failed=sms_failed,
                           sms_total=sms_total)


@app.route("/admin/sms-settings/update", methods=["POST"])
@require_admin
def admin_sms_settings_update():
    from models import SmsSettings
    settings = SmsSettings.query.first()
    if not settings:
        settings = SmsSettings()
        db.session.add(settings)
    settings.sms_globally_enabled = request.form.get("sms_globally_enabled") == "1"
    settings.ev_new_bid          = request.form.get("ev_new_bid") == "1"
    settings.ev_bid_accepted     = request.form.get("ev_bid_accepted") == "1"
    settings.ev_deposit_paid     = request.form.get("ev_deposit_paid") == "1"
    settings.ev_job_nearby       = request.form.get("ev_job_nearby") == "1"
    settings.ev_job_completed    = request.form.get("ev_job_completed") == "1"
    settings.ev_job_cancelled    = request.form.get("ev_job_cancelled") == "1"
    settings.ev_bid_rejected     = request.form.get("ev_bid_rejected") == "1"
    settings.ev_admin_alert      = request.form.get("ev_admin_alert") == "1"
    settings.ev_quote_received   = request.form.get("ev_quote_received") == "1"
    settings.email_fallback_to_sms = request.form.get("email_fallback_to_sms") == "1"
    db.session.commit()
    flash("SMS settings saved.", "success")
    return redirect(url_for('admin_sms_settings'))


@app.route("/admin/sms-settings/test", methods=["POST"])
@require_admin
def admin_sms_test():
    phone = strip_phone(request.form.get("phone", ""))
    if not phone:
        flash("Enter a phone number to send the test to.", "error")
        return redirect(url_for('admin_sms_settings'))
    ok = send_sms(phone,
                  "JHE Haul: This is a test SMS from the admin dashboard. "
                  "If you received this, SMS is working correctly!",
                  'admin_test')
    if ok:
        flash(f"Test SMS sent to ({phone[:3]}) {phone[3:6]}-{phone[6:]}! Check the SMS Logs for delivery details.", "success")
    else:
        flash("Test SMS failed. Check Twilio credentials and the SMS Logs for the error.", "error")
    return redirect(url_for('admin_sms_settings'))


@app.route("/admin/sms-logs")
@require_admin
def admin_sms_logs():
    from models import SmsLog
    logs = SmsLog.query.order_by(SmsLog.created_at.desc()).limit(500).all()
    sent    = sum(1 for l in logs if l.status == 'sent')
    failed  = sum(1 for l in logs if l.status == 'failed')
    skipped = sum(1 for l in logs if l.status in ('no_twilio', 'skipped'))
    type_labels = {
        'hauler_new_job_nearby':  'Hauler — New Job Nearby',
        'customer_new_bid':       'Customer — New Bid',
        'hauler_bid_accepted':    'Hauler — Bid Accepted',
        'hauler_bid_rejected':    'Hauler — Bid Not Selected',
        'hauler_deposit_paid':    'Hauler — Deposit Paid',
        'customer_job_completed': 'Customer — Job Complete',
        'hauler_job_cancelled':   'Hauler — Job Cancelled',
        'admin_alert':            'Admin Alert',
        'admin_test':             'Admin Test',
        'phone_verification':     'Phone Verification',
        'sms':                    'General SMS',
        'admin_new_customer':     'Admin — New Customer',
        'admin_new_hauler':       'Admin — New Hauler',
        'admin_new_job':          'Admin — New Job',
        'admin_bid_accepted':     'Admin — Bid Accepted',
        'admin_new_bid':          'Admin — New Bid',
        'fallback_hauler_new_job_nearby':  'Fallback — Hauler New Job',
        'fallback_customer_new_bid':       'Fallback — Customer Bid',
        'fallback_hauler_bid_accepted':    'Fallback — Hauler Bid Accepted',
        'fallback_hauler_deposit_paid':    'Fallback — Hauler Deposit Paid',
    }
    return render_template('admin_sms_logs.html',
                           logs=logs, sent=sent, failed=failed, skipped=skipped,
                           type_labels=type_labels)


@app.route("/admin/sms/resend/<int:log_id>", methods=["POST"])
@require_admin
def admin_sms_resend(log_id):
    from models import SmsLog
    log = SmsLog.query.get_or_404(log_id)
    if log.status not in ('failed', 'no_twilio'):
        flash("Only failed SMS messages can be resent.", "error")
        return redirect(url_for('admin_sms_logs'))
    ok = send_sms(log.recipient_phone, log.message_body, log.event_type)
    if ok:
        phone_fmt = log.recipient_phone or '?'
        flash(f"SMS resent to {phone_fmt}.", "success")
    else:
        flash("Resend failed. Check Twilio configuration and SMS logs for the error.", "error")
    return redirect(url_for('admin_sms_logs'))


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

def _gallery_photos():
    from models import GalleryPhoto
    return GalleryPhoto.query.order_by(GalleryPhoto.display_order, GalleryPhoto.id).all()


_GALLERY_MAX_PX    = 1600              # longest-edge cap in pixels
_GALLERY_QUALITY   = 80               # JPEG compression quality (0–95)
_GALLERY_MAX_BYTES = 10 * 1024 * 1024  # 10 MB input limit


def _compress_gallery_image(raw_bytes: bytes, ext: str):
    """
    Resize and compress an uploaded gallery image with Pillow.

    - HEIC / HEIF files are rejected — browsers cannot render them natively.
    - All other formats are converted to JPEG, capped at _GALLERY_MAX_PX on
      the longest edge, and saved at _GALLERY_QUALITY.
    - Returns (compressed_bytes, out_ext, content_type) or raises ValueError
      with a user-friendly message on unsupported input.
    """
    ext_clean = ext.lstrip('.').lower()
    if ext_clean in ('heic', 'heif'):
        raise ValueError(
            "HEIC/HEIF photos cannot be uploaded — please convert to JPG or PNG first "
            "(on iPhone: Settings → Camera → Formats → Most Compatible)."
        )

    import io as _io
    from PIL import Image, ExifTags

    img = Image.open(_io.BytesIO(raw_bytes))

    # Auto-rotate using EXIF orientation so portrait shots aren't sideways
    try:
        exif = img._getexif()
        if exif:
            orient_key = next(
                (k for k, v in ExifTags.TAGS.items() if v == 'Orientation'), None
            )
            if orient_key and orient_key in exif:
                orient = exif[orient_key]
                _ROTATIONS = {3: 180, 6: 270, 8: 90}
                if orient in _ROTATIONS:
                    img = img.rotate(_ROTATIONS[orient], expand=True)
    except Exception:
        pass  # EXIF failures are non-fatal

    # Ensure mode is compatible with JPEG output
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')

    # Shrink so the longest edge ≤ _GALLERY_MAX_PX; never enlarge
    w, h = img.size
    if max(w, h) > _GALLERY_MAX_PX:
        scale = _GALLERY_MAX_PX / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = _io.BytesIO()
    img.save(buf, format='JPEG', quality=_GALLERY_QUALITY, optimize=True)
    return buf.getvalue(), '.jpg', 'image/jpeg'


@app.route("/admin/gallery/upload", methods=["POST"])
@require_admin
def admin_gallery_upload():
    from models import GalleryPhoto
    files = request.files.getlist("photos")
    files = [f for f in files if f and f.filename]
    if not files:
        flash("No files selected.", "error")
        return redirect(url_for('admin_gallery'))

    caption = (request.form.get("caption") or "").strip()[:200] or None
    allowed = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif', '.bmp', '.tiff'}
    from sqlalchemy import func as _func
    max_order = db.session.query(_func.coalesce(_func.max(GalleryPhoto.display_order), 0)).scalar() or 0

    from storage import upload_bytes as _upload_bytes
    added, skipped = 0, 0
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in allowed:
            skipped += 1
            continue
        f.stream.seek(0)
        raw_data = f.stream.read()
        if not raw_data or len(raw_data) > _GALLERY_MAX_BYTES:
            skipped += 1
            continue
        try:
            compressed_data, out_ext, out_ct = _compress_gallery_image(raw_data, ext)
        except ValueError as ve:
            flash(str(ve), "error")
            skipped += 1
            continue
        except Exception as exc:
            app.logger.warning("admin_gallery_upload: compression failed for %s: %s", f.filename, exc)
            skipped += 1
            continue
        filename, storage_url = _upload_bytes(compressed_data, out_ext)
        max_order += 1
        db.session.add(GalleryPhoto(
            caption=caption,
            filename=filename,
            storage_url=storage_url,
            data=compressed_data if not storage_url else None,
            content_type=out_ct if not storage_url else None,
            display_order=max_order,
        ))
        added += 1
    db.session.commit()

    if added:
        flash(f"Uploaded {added} photo{'s' if added != 1 else ''} to the gallery.", "success")
    if skipped:
        flash(f"{skipped} file{'s were' if skipped != 1 else ' was'} skipped (unsupported type, over 10 MB, or conversion error).", "error")
    return redirect(url_for('admin_gallery'))

@app.route("/landing")
def landing():
    """Public marketing landing page with the current gallery photo set."""
    return render_template('landing.html', gallery_photos=_gallery_photos())

@app.route("/admin/gallery/<int:photo_id>/move", methods=["POST"])
@require_admin
def admin_gallery_move(photo_id):
    """Move a photo up or down in display order."""
    from models import GalleryPhoto
    direction = request.form.get("direction")
    photos = _gallery_photos()
    # Normalize display_order to a clean sequence first
    for i, p in enumerate(photos):
        p.display_order = i + 1
    idx = next((i for i, p in enumerate(photos) if p.id == photo_id), None)
    if idx is None:
        db.session.commit()
        return redirect(url_for('admin_gallery'))
    if direction == "up" and idx > 0:
        photos[idx].display_order, photos[idx - 1].display_order = \
            photos[idx - 1].display_order, photos[idx].display_order
    elif direction == "down" and idx < len(photos) - 1:
        photos[idx].display_order, photos[idx + 1].display_order = \
            photos[idx + 1].display_order, photos[idx].display_order
    db.session.commit()
    return redirect(url_for('admin_gallery'))

@app.route("/uploads/gallery/db/<int:photo_id>")
def serve_gallery_photo(photo_id):
    """Serve a gallery photo stored as binary in the database."""
    from models import GalleryPhoto
    photo = GalleryPhoto.query.get(photo_id)
    if not photo or not photo.data:
        return "", 404
    from flask import Response
    r = Response(photo.data, mimetype=photo.content_type or 'image/jpeg')
    r.headers["Cache-Control"] = "public, max-age=3600"
    return r

@app.route("/admin/gallery")
@require_admin
def admin_gallery():
    return render_template('admin_gallery.html', photos=_gallery_photos())

@app.route("/admin/gallery/<int:photo_id>/caption", methods=["POST"])
@require_admin
def admin_gallery_caption(photo_id):
    from models import GalleryPhoto
    photo = GalleryPhoto.query.get_or_404(photo_id)
    photo.caption = (request.form.get("caption") or "").strip()[:200] or None
    db.session.commit()
    flash("Caption updated.", "success")
    return redirect(url_for('admin_gallery'))

@app.route("/admin/gallery/<int:photo_id>/delete", methods=["POST"])
@require_admin
def admin_gallery_delete(photo_id):
    from models import GalleryPhoto
    photo = GalleryPhoto.query.get_or_404(photo_id)
    filename_to_delete = photo.filename
    db.session.delete(photo)
    db.session.commit()
    # Delete the stored file (Spaces or local) after DB commit so we don't orphan it on rollback
    if filename_to_delete:
        try:
            from storage import delete_file as _delete_file
            _delete_file(filename_to_delete)
        except Exception as exc:
            app.logger.warning("admin_gallery_delete: storage cleanup failed: %s", exc)
    flash("Photo removed from the gallery.", "success")
    return redirect(url_for('admin_gallery'))
