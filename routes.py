import os
import uuid
import math
import stripe
from datetime import datetime
from functools import wraps
from flask import session, redirect, url_for, request, send_from_directory, render_template, flash, make_response, g, abort, jsonify
from werkzeug.utils import secure_filename
from flask_login import current_user, login_user, logout_user

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

from app import app, db, UPLOAD_FOLDER, choose_pay_link
from auth import require_login
from models import User, Job, JobPhoto, Bid, CompletionPhoto, Review, PageView, HaulerServiceZip, Quote, Message, Category, Listing, ListingPhoto, ListingReport, DeliveryRequest, FraudFlag, ListingView, RecommendationEvent, expire_pending_offers, expire_stale_timed_offers
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
    notify_customer_quote_received, notify_customer_quote_withdrawn,
    notify_customer_deposit_confirmed,
    notify_customer_appointment_confirmed,
    notify_admin_new_request,
    notify_buyer_offer_expired,
    notify_buyer_offer_timed_out,
    notify_buyer_listing_pending,
    notify_seller_new_message,
    notify_buyer_delivery_quote_ready,
)
from sms_service import (
    notify_hauler_new_job_sms, notify_hauler_bid_accepted_sms,
    notify_hauler_deposit_paid_sms, notify_hauler_bid_rejected_sms,
    notify_hauler_job_cancelled_sms,
    notify_customer_new_bid_sms, notify_customer_job_completed_sms,
    notify_customer_quote_received_sms, notify_customer_quote_withdrawn_sms,
    notify_customer_deposit_confirmed_sms,
    notify_customer_appointment_confirmed_sms,
    notify_admin_sms, send_sms, send_verification_sms, get_sms_settings,
    notify_admin_new_customer_sms, notify_admin_new_hauler_sms,
    notify_admin_new_job_sms, notify_admin_bid_accepted_sms, notify_admin_new_bid_sms,
    notify_admin_new_request_sms,
)
# notify_seller_new_offer_sms removed — marketplace SMS disabled; in-app + email used instead

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
        # Session-version check: only enforced for local-password admin sessions.
        # OAuth sessions never set '_admin_sv', so they pass through untouched.
        sess_ver = session.get('_admin_sv')
        if sess_ver is not None:
            db_ver = current_user.admin_session_version or 0
            if sess_ver != db_ver:
                logout_user()
                session.clear()
                flash("Your admin session has expired. Please sign in again.", "info")
                return redirect(url_for('admin_local_login'))
        return f(*args, **kwargs)
    return decorated_function


def _mask_email(email):
    """Partially mask an email for display: j*****@example.com"""
    if not email or '@' not in email:
        return email or ''
    local, domain = email.split('@', 1)
    if len(local) <= 1:
        masked_local = local
    else:
        masked_local = local[0] + '*' * min(len(local) - 1, 5)
    return f"{masked_local}@{domain}"

@app.context_processor
def inject_globals():
    result = {'admin_unread_count': 0, 'customer_unread_count': 0,
              'notif_unread_count': 0,
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
        # In-app notification unread count (all user types)
        try:
            from notification_service import get_unread_count as _notif_count
            result['notif_unread_count'] = _notif_count(current_user.id)
        except Exception:
            pass
    return result


import json as _json
from vehicle_data import VEHICLE_MAKES_MODELS as _VMM, VEHICLE_MAKES as _VMAKES
from services.seo import (
    listing_slug,
    listing_canonical_path,
    listing_seo_title,
    listing_seo_description,
    listing_jsonld as _listing_jsonld,
)
from ai.listing_assistant import suggest as _ai_suggest
from ai.search_intelligence import parse_marketplace_search as _ai_search_parse


def _queue_email(fn_name, **kwargs):
    """Enqueue an EMAIL_NOTIFICATION background job.

    Returns True when the job was queued successfully; False when the queue is
    unavailable (caller should fall back to a direct synchronous send).
    Never raises — callers rely on try/except for resilience.

    Payload contains only non-secret kwargs (email addresses, IDs, titles).
    Secrets are never placed in job payloads per Phase F design rules.
    """
    try:
        from worker.queue import enqueue, NORMAL
        enqueue('EMAIL_NOTIFICATION', {'fn': fn_name, 'kwargs': kwargs}, priority=NORMAL)
        return True
    except Exception as _qe:
        app.logger.warning("_queue_email: could not enqueue %s — %s", fn_name, _qe)
        return False
_VEHICLE_MAKES_MODELS_JSON = _json.dumps(_VMM)

@app.context_processor
def _inject_vehicle_data():
    return dict(VEHICLE_MAKES=_VMAKES, VEHICLE_MAKES_MODELS_JSON=_VEHICLE_MAKES_MODELS_JSON)


@app.before_request
def make_session_permanent():
    session.permanent = True


# ── Age confirmation gate ─────────────────────────────────────────────────────
_AGE_GATE_EXEMPT = {
    'confirm_age', 'auth.logout', 'auth.switch_account', 'auth.login',
    'auth.error', 'static', 'serve_listing_photo',
    'api_notification_count',  # polling must not redirect
    # Listing AJAX endpoints — must never redirect to age gate mid-upload
    'listing_photo_upload', 'listing_photo_upload_lazy',
    'listing_photo_delete', 'listing_photo_primary',
    'listing_photo_reorder',
    'listing_video_upload', 'listing_video_delete',
    'csrf_refresh',  # token refresh endpoint
    # Admin local-auth routes — accessible without age confirmation
    'admin_local_login', 'admin_forgot_password', 'admin_reset_password',
    'admin_verify_recovery_email',
}

@app.before_request
def enforce_age_confirmation():
    """Redirect authenticated users who have not confirmed 18+ to the age gate."""
    if not current_user.is_authenticated:
        return
    if getattr(current_user, 'age_confirmed', True):
        return
    endpoint = request.endpoint or ''
    if endpoint in _AGE_GATE_EXEMPT:
        return
    if request.path.startswith('/static/') or request.path.startswith('/api/'):
        return
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return
    return redirect(url_for('confirm_age'))


@app.route('/confirm-age', methods=['GET', 'POST'])
@require_login
def confirm_age():
    """18+ age confirmation gate shown to new users after OAuth sign-in."""
    if current_user.age_confirmed:
        return redirect(url_for('home'))
    if request.method == 'POST':
        _check_listing_csrf()
        if request.form.get('age_cert') == '1':
            current_user.age_confirmed = True
            db.session.commit()
            next_url = session.pop('next_url', None)
            return redirect(next_url or url_for('home'))
        else:
            flash("You must confirm that you are at least 18 years old to use JHE Haul Marketplace.", "error")
    return render_template('confirm_age.html')


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
    """Read photo bytes from an uploaded file; convert HEIC/HEIF to JPEG for browser compatibility."""
    ct = _PHOTO_CONTENT_TYPES.get(ext.lstrip('.').lower(), 'image/jpeg')
    file_obj.stream.seek(0)
    data = file_obj.stream.read()
    file_obj.stream.seek(0)

    # Convert HEIC/HEIF (iPhone format) → JPEG so all browsers can display it
    if ext.lower() in ('.heic', '.heif'):
        try:
            try:
                import pillow_heif as _ph
                _ph.register_heif_opener()
            except ImportError:
                pass
            from PIL import Image as _PILImage
            import io as _io
            img = _PILImage.open(_io.BytesIO(data))
            buf = _io.BytesIO()
            img.convert('RGB').save(buf, format='JPEG', quality=90)
            converted = buf.getvalue()
            if converted:
                data = converted
                ct = 'image/jpeg'
                file_obj.stream = _io.BytesIO(data)  # so upload_file gets JPEG too
        except Exception as _conv_err:
            app.logger.warning("HEIC/HEIF→JPEG conversion failed (%s); storing original bytes", _conv_err)

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


@app.route("/profile/nudge/dismiss", methods=["POST"])
@require_login
def dismiss_profile_nudge():
    """Permanently dismiss the profile-completion nudge banner for this user."""
    current_user.profile_nudge_dismissed = True
    db.session.commit()
    return ('', 204)


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

def _expire_offers_and_notify(listing_id, listing_title=None):
    """Expire pending/countered offers on a listing and email each affected buyer.

    Must be called BEFORE db.session.commit() so the offer rows can be queried
    while still in their current state.
    """
    from models import ListingOffer
    # Fetch affected offers (with buyer id and email) before the bulk-update wipes the status
    affected = (ListingOffer.query
                .filter(
                    ListingOffer.listing_id == listing_id,
                    ListingOffer.status.in_(['pending', 'countered'])
                )
                .join(User, ListingOffer.buyer_id == User.id)
                .with_entities(ListingOffer.amount, User.email, User.id)
                .all())
    # Perform the bulk status update
    expire_pending_offers(listing_id)
    # Notify each buyer via email + in-app alert (silently swallow errors so they don't break the route)
    safe_title = (listing_title or f"Listing #{listing_id}")[:60]
    for offer_amount, buyer_email, buyer_id in affected:
        if buyer_email:
            try:
                notify_buyer_offer_expired(buyer_email, listing_title, listing_id, offer_amount)
            except Exception as _e:
                app.logger.warning("notify_buyer_offer_expired failed for listing %s: %s", listing_id, _e)
        if buyer_id:
            try:
                from notification_service import create_notification
                create_notification(
                    user_id=buyer_id,
                    notif_type='offer_expired',
                    title="Your offer is no longer active.",
                    message=f"Your offer on \"{safe_title}\" is no longer active — the listing was sold or removed.",
                    action_url="/marketplace",
                    related_listing_id=listing_id,
                )
            except Exception as _e:
                app.logger.warning("in-app offer_expired notification failed for listing %s buyer %s: %s", listing_id, buyer_id, _e)


def _marketplace_categories():
    from models import Category
    return (Category.query
            .filter_by(is_active=True, parent_id=None)
            .order_by(Category.display_order, Category.name)
            .all())


def _saved_listing_ids():
    """Return a set of listing IDs saved by the current user (empty set if logged out)."""
    if not current_user.is_authenticated:
        return set()
    try:
        from models import ListingFavorite
        rows = ListingFavorite.query.filter_by(user_id=current_user.id).with_entities(ListingFavorite.listing_id).all()
        return {r.listing_id for r in rows}
    except Exception:
        return set()


def _marketplace_homepage_ctx(hide_sold=False):
    """Build context dict for the marketplace homepage (no active filters)."""
    from models import Listing
    _base = Listing.query.filter(
        Listing.status.in_(['active', 'sold', 'reserved', 'pending']),
        Listing.moderation_status == 'approved'
    )
    _active = Listing.query.filter_by(status='active', moderation_status='approved')
    # When hide_sold is on, all sections show only active listings
    _recent_base = _active if hide_sold else _base
    _prop_base   = _active if hide_sold else _base
    recent = (_recent_base.filter(Listing.listing_type == 'item')
              .order_by(Listing.created_at.desc()).limit(8).all())
    # NOTE: free_items intentionally always uses _active (status='active') regardless of the
    # hide_sold preference.  A free item that is sold or reserved has nothing left to give
    # away, so showing it in the "Free Items" section would be misleading.  _active already
    # guarantees only live, available listings appear here — hide_sold adds nothing extra.
    free_items = (_active.filter_by(price_type='free')
                  .filter(Listing.listing_type == 'item')
                  .order_by(Listing.created_at.desc()).limit(8).all())
    featured = (_active.filter_by(featured=True)
                .order_by(Listing.created_at.desc()).limit(8).all())
    for_sale = (_prop_base.filter(Listing.listing_type == 'property_sale')
                .order_by(Listing.created_at.desc()).limit(6).all())
    rentals  = (_prop_base.filter(Listing.listing_type == 'rental')
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
        show_welcome = session.pop('new_member', False)
        categories = _marketplace_categories()
        # Respect hide_sold query param; persist choice in session and DB (for logged-in users)
        hs_param = request.args.get('hide_sold', None)
        if hs_param is not None:
            _hs_bool = bool(hs_param and hs_param != '0')
            session['hide_sold'] = _hs_bool
            # Persist to DB so the preference survives session expiry
            if current_user.is_authenticated and current_user.hide_sold_pref != _hs_bool:
                current_user.hide_sold_pref = _hs_bool
                db.session.commit()
        elif 'hide_sold' not in session and current_user.is_authenticated:
            # First visit after session expiry — seed from stored preference
            session['hide_sold'] = bool(current_user.hide_sold_pref)
        hide_sold_pref = session.get('hide_sold', False)
        ctx = _marketplace_homepage_ctx(hide_sold=hide_sold_pref)
        # Show profile nudge if profile is incomplete and user hasn't dismissed it
        profile_incomplete = (not current_user.profile_image_url and
                              not current_user.profile_photo_data and
                              not current_user.phone)
        show_profile_nudge = (profile_incomplete and
                              not getattr(current_user, 'profile_nudge_dismissed', False) and
                              not show_welcome)
        _home_resp = make_response(render_template(
            'marketplace.html', categories=categories, is_search=False,
            hide_sold='1' if hide_sold_pref else '',
            no_vehicles_filter='',
            listing_type_filter='', active_category=None,
            city_zip_filter='', area_filter='',
            show_welcome=show_welcome,
            show_profile_nudge=show_profile_nudge,
            gallery_photos=_gallery_photos(active_only=True),
            saved_listing_ids=_saved_listing_ids(), **ctx))
        # Prevent browser back-button from replaying a cached response that still
        # contains the welcome banner HTML (the session flag is already consumed).
        _home_resp.headers['Cache-Control'] = 'no-store'
        return _home_resp
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
    # Authenticated users who haven't completed onboarding must see the welcome screen first
    if current_user.is_authenticated and not current_user.user_type and not current_user.is_admin:
        return redirect(url_for('choose_role'))

    from models import Category, Listing
    categories = _marketplace_categories()

    q                  = request.args.get('q',            '').strip()
    category_slug      = request.args.get('category',     '').strip()
    price_type_filter  = request.args.get('price_type',   '').strip()
    featured_filter    = request.args.get('featured',     '').strip()
    listing_type_filter= request.args.get('listing_type', '').strip()
    no_vehicles_filter = request.args.get('no_vehicles',  '').strip()
    area_filter        = request.args.get('area',         '').strip()
    _city_zip_in_url   = 'city_zip' in request.args
    city_zip_filter    = request.args.get('city_zip',     '').strip()
    # Persist buyer's preferred area in session so it pre-fills on future visits.
    # Only update the session when city_zip was explicitly present in this request
    # (including an empty value, which means the buyer cleared it).
    if _city_zip_in_url:
        if city_zip_filter:
            session['city_zip_pref'] = city_zip_filter
        else:
            session.pop('city_zip_pref', None)
    else:
        # Not in URL — restore from saved preference so the filter pre-fills
        city_zip_filter = session.get('city_zip_pref', '')
    min_price_raw      = request.args.get('min_price',    '').strip()
    max_price_raw      = request.args.get('max_price',    '').strip()
    min_beds_raw       = request.args.get('min_beds',     '').strip()
    open_house_only    = request.args.get('open_house',   '').strip()
    # ── Phase E: extended search params ──────────────────────────────────
    vehicle_make_f    = request.args.get('vehicle_make',        '').strip()
    vehicle_model_f   = request.args.get('vehicle_model',       '').strip()
    veh_yr_min_raw    = request.args.get('vehicle_year_min',    '').strip()
    veh_yr_max_raw    = request.args.get('vehicle_year_max',    '').strip()
    veh_mile_raw      = request.args.get('vehicle_mileage_max', '').strip()
    condition_f       = request.args.get('condition',           '').strip()
    sort_f            = request.args.get('sort',                '').strip()
    delivery_f        = request.args.get('delivery_available',  '').strip()
    recency_f         = request.args.get('recency',             '').strip()
    _hide_sold_param   = request.args.get('hide_sold',    None)
    # Persist the hide_sold choice to session so it stays in sync with the homepage toggle.
    # Normalise to '1' / '' so Jinja truthiness works correctly (avoid '0' being truthy).
    if _hide_sold_param is not None:
        _hs_bool = bool(_hide_sold_param and _hide_sold_param.strip() != '0')
        session['hide_sold'] = _hs_bool
        # Persist to DB so the preference survives session expiry
        if current_user.is_authenticated and current_user.hide_sold_pref != _hs_bool:
            current_user.hide_sold_pref = _hs_bool
            db.session.commit()
        hide_sold = '1' if _hs_bool else ''
    else:
        if 'hide_sold' not in session and current_user.is_authenticated:
            # First visit after session expiry — seed from stored preference
            session['hide_sold'] = bool(current_user.hide_sold_pref)
        hide_sold = '1' if session.get('hide_sold') else ''

    try: min_price = float(min_price_raw) if min_price_raw else None
    except ValueError: min_price = None
    try: max_price = float(max_price_raw) if max_price_raw else None
    except ValueError: max_price = None
    try: min_beds = float(min_beds_raw) if min_beds_raw else None
    except ValueError: min_beds = None
    try: veh_yr_min = int(veh_yr_min_raw)    if veh_yr_min_raw else None
    except ValueError: veh_yr_min = None
    try: veh_yr_max = int(veh_yr_max_raw)    if veh_yr_max_raw else None
    except ValueError: veh_yr_max = None
    try: veh_mileage_max = int(veh_mile_raw) if veh_mile_raw   else None
    except ValueError: veh_mileage_max = None

    is_search = bool(q or category_slug or price_type_filter or featured_filter
                     or listing_type_filter or area_filter or city_zip_filter
                     or min_price is not None or max_price is not None
                     or min_beds is not None or open_house_only or hide_sold
                     or vehicle_make_f or vehicle_model_f
                     or veh_yr_min is not None or veh_yr_max is not None
                     or veh_mileage_max is not None or condition_f
                     or sort_f or delivery_f or recency_f)

    if is_search:
        if hide_sold:
            qobj = Listing.query.filter_by(status='active', moderation_status='approved')
        else:
            qobj = Listing.query.filter(
                Listing.status.in_(['active', 'sold', 'reserved', 'pending']),
                Listing.moderation_status == 'approved'
            )

        if q:
            qobj = qobj.filter(
                db.or_(
                    Listing.title.ilike(f'%{q}%'),
                    Listing.description.ilike(f'%{q}%'),
                    Listing.vehicle_make.ilike(f'%{q}%'),
                    Listing.vehicle_model.ilike(f'%{q}%'),
                    Listing.city.ilike(f'%{q}%'),
                    db.cast(Listing.vehicle_year, db.String).ilike(f'%{q}%'),
                )
            )
        if category_slug:
            cat = Category.query.filter_by(slug=category_slug, is_active=True).first()
            if cat:
                # Match category OR any of its subcategory children
                child_ids = [c.id for c in cat.subcategories]
                if child_ids:
                    cat_filter = db.or_(Listing.category_id == cat.id,
                                        Listing.category_id.in_(child_ids))
                else:
                    cat_filter = (Listing.category_id == cat.id)
                # Housing chip: also capture property listings by listing_type so
                # existing listings created before category_id was auto-assigned
                # still appear when buyers click the chip.
                if category_slug == 'housing':
                    qobj = qobj.filter(
                        db.or_(cat_filter,
                               Listing.listing_type.in_(['property_sale', 'rental']))
                    )
                else:
                    qobj = qobj.filter(cat_filter)

        if price_type_filter in ('free', 'fixed', 'negotiable'):
            qobj = qobj.filter(Listing.price_type == price_type_filter)
        if featured_filter:
            qobj = qobj.filter(Listing.featured == True)

        # Listing type filter (item | property_sale | rental | housing)
        if listing_type_filter == 'housing':
            qobj = qobj.filter(Listing.listing_type.in_(['property_sale', 'rental']))
        elif listing_type_filter in ('item', 'property_sale', 'rental'):
            qobj = qobj.filter(Listing.listing_type == listing_type_filter)

        # no_vehicles=1 with listing_type=item → exclude the Vehicles category
        if no_vehicles_filter and listing_type_filter == 'item':
            _vcats = Category.query.filter(
                db.or_(Category.slug == 'vehicles', Category.name.ilike('vehicle%'))
            ).all()
            _excl_ids = []
            for _vc in _vcats:
                _excl_ids.append(_vc.id)
                _excl_ids.extend([c.id for c in _vc.subcategories])
            if _excl_ids:
                qobj = qobj.filter(
                    db.or_(Listing.category_id.is_(None),
                           Listing.category_id.notin_(_excl_ids))
                )
            else:
                qobj = qobj.filter(Listing.vehicle_make.is_(None))

        # Twin Cities area filter — radius-based (40 mi from Minneapolis centre)
        if area_filter == 'twin-cities':
            _TC_LAT, _TC_LON = 44.9778, -93.2650
            # Degree offsets: 1° lat ≈ 69 mi; 1° lon ≈ 49 mi at 45 °N
            _TC_DLAT = 40.0 / 69.0   # ≈ 0.580 °
            _TC_DLON = 40.0 / 49.0   # ≈ 0.816 °
            # Primary path: listings with coordinates → bounding-box pre-filter
            coords_filter = db.and_(
                Listing.latitude.isnot(None),
                Listing.longitude.isnot(None),
                Listing.latitude  >= _TC_LAT - _TC_DLAT,
                Listing.latitude  <= _TC_LAT + _TC_DLAT,
                Listing.longitude >= _TC_LON - _TC_DLON,
                Listing.longitude <= _TC_LON + _TC_DLON,
            )
            # Fallback: older listings with no lat/lon → city-name list
            no_coords_filter = db.and_(
                db.or_(Listing.latitude.is_(None), Listing.longitude.is_(None)),
                Listing.state == 'MN',
                db.func.lower(Listing.city).in_(_TWIN_CITIES_CITIES),
            )
            qobj = qobj.filter(db.or_(coords_filter, no_coords_filter))

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

        # City / ZIP filter — applies to any listing type
        zip_radius_fallback = False
        _ZIP_RADIUS_MI = 25  # configurable search radius in miles
        if city_zip_filter:
            _czf = city_zip_filter.strip()
            _is_zip = _czf.isdigit() and len(_czf) == 5
            if _is_zip:
                from models import ZipCode as _ZipCode
                _center = _ZipCode.query.get(_czf)
                if _center:
                    # Bounding-box radius lookup (1° lat ≈ 69 mi; lon shrinks by cos(lat))
                    _dlat = _ZIP_RADIUS_MI / 69.0
                    _dlon = _ZIP_RADIUS_MI / (69.0 * abs(math.cos(math.radians(_center.lat))) + 1e-9)
                    _nearby_zips = [
                        row[0] for row in _ZipCode.query.filter(
                            _ZipCode.lat >= _center.lat - _dlat,
                            _ZipCode.lat <= _center.lat + _dlat,
                            _ZipCode.lon >= _center.lon - _dlon,
                            _ZipCode.lon <= _center.lon + _dlon,
                        ).with_entities(_ZipCode.zip).all()
                    ]
                    qobj = qobj.filter(Listing.zip_code.in_(_nearby_zips))
                else:
                    # ZIP not in our database — fall back to exact match with a notice
                    zip_radius_fallback = True
                    qobj = qobj.filter(Listing.zip_code == _czf)
            else:
                # City name search (case-insensitive partial match)
                qobj = qobj.filter(Listing.city.ilike(f'%{_czf}%'))

        # ── Phase E extended filters ──────────────────────────────────────
        _VALID_CONDITIONS = {'new', 'like_new', 'good', 'fair', 'for_parts'}
        if vehicle_make_f:
            qobj = qobj.filter(Listing.vehicle_make.ilike(f'%{vehicle_make_f}%'))
        if vehicle_model_f:
            qobj = qobj.filter(Listing.vehicle_model.ilike(f'%{vehicle_model_f}%'))
        if veh_yr_min is not None:
            qobj = qobj.filter(Listing.vehicle_year >= veh_yr_min)
        if veh_yr_max is not None:
            qobj = qobj.filter(Listing.vehicle_year <= veh_yr_max)
        if veh_mileage_max is not None:
            qobj = qobj.filter(Listing.vehicle_mileage <= veh_mileage_max)
        if condition_f in _VALID_CONDITIONS:
            qobj = qobj.filter(Listing.condition == condition_f)
        if delivery_f:
            qobj = qobj.filter(Listing.delivery_option.ilike('%jhe_haul%'))
        if recency_f:
            from datetime import datetime as _rdt, timedelta as _rtd
            _recency_days = {'today': 1, 'week': 7, 'month': 30}.get(recency_f)
            if _recency_days:
                qobj = qobj.filter(Listing.created_at >= _rdt.now() - _rtd(days=_recency_days))
        # ── End Phase E filters ───────────────────────────────────────────

        _limit = min(max(int(request.args.get('limit', 24) or 24), 1), 192)
        if sort_f == 'price_asc':
            _mp_order = [Listing.featured.desc(), Listing.price.asc()]
        elif sort_f == 'price_desc':
            _mp_order = [Listing.featured.desc(), Listing.price.desc()]
        else:
            _mp_order = [Listing.featured.desc(), Listing.created_at.desc()]
        _all   = qobj.order_by(*_mp_order).limit(_limit + 1).all()
        has_more = len(_all) > _limit
        search_results = _all[:_limit]
        active_category = Category.query.filter_by(slug=category_slug).first() if category_slug else None
        _mp_profile_incomplete = (
            current_user.is_authenticated and
            not current_user.profile_image_url and
            not current_user.profile_photo_data and
            not current_user.phone
        )
        _mp_show_nudge = _mp_profile_incomplete and not getattr(current_user, 'profile_nudge_dismissed', False)
        from urllib.parse import urlencode as _urlencode
        _lm_args = {k: v for k, v in request.args.items() if k != 'limit'}
        _lm_qs = _urlencode(_lm_args)
        _load_more_base_url = '/marketplace?' + (_lm_qs + '&' if _lm_qs else '')
        return render_template('marketplace.html',
                               categories=categories,
                               is_search=True,
                               search_query=q,
                               search_results=search_results,
                               active_category=active_category,
                               price_type_filter=price_type_filter,
                               featured_filter=featured_filter,
                               listing_type_filter=listing_type_filter,
                               no_vehicles_filter=no_vehicles_filter,
                               area_filter=area_filter,
                               city_zip_filter=city_zip_filter,
                               min_price=min_price, max_price=max_price,
                               min_beds=min_beds, open_house_only=open_house_only,
                               hide_sold=hide_sold,
                               search_limit=_limit,
                               has_more=has_more,
                               load_more_base_url=_load_more_base_url,
                               saved_listing_ids=_saved_listing_ids(),
                               show_welcome=False,
                               show_profile_nudge=_mp_show_nudge,
                               zip_radius_fallback=zip_radius_fallback,
                               recent_listings=[], free_listings=[], featured_listings=[],
                               for_sale_listings=[], rental_listings=[])
    else:
        show_welcome = session.pop('new_member', False)
        # Respect hide_sold query param; persist choice in session and DB (for logged-in users)
        hs_param = request.args.get('hide_sold', None)
        if hs_param is not None:
            _hs_bool = bool(hs_param and hs_param != '0')
            session['hide_sold'] = _hs_bool
            # Persist to DB so the preference survives session expiry
            if current_user.is_authenticated and current_user.hide_sold_pref != _hs_bool:
                current_user.hide_sold_pref = _hs_bool
                db.session.commit()
        elif 'hide_sold' not in session and current_user.is_authenticated:
            # First visit after session expiry — seed from stored preference
            session['hide_sold'] = bool(current_user.hide_sold_pref)
        hide_sold_pref = session.get('hide_sold', False)
        ctx = _marketplace_homepage_ctx(hide_sold=hide_sold_pref)
        _mp_profile_incomplete = (
            current_user.is_authenticated and
            not current_user.profile_image_url and
            not current_user.profile_photo_data and
            not current_user.phone
        )
        _mp_show_nudge = (_mp_profile_incomplete and
                          not getattr(current_user, 'profile_nudge_dismissed', False) and
                          not show_welcome)
        _mp_resp = make_response(render_template(
            'marketplace.html', categories=categories, is_search=False,
            listing_type_filter='', area_filter='', city_zip_filter='',
            hide_sold='1' if hide_sold_pref else '',
            show_welcome=show_welcome,
            show_profile_nudge=_mp_show_nudge,
            gallery_photos=_gallery_photos(active_only=True),
            saved_listing_ids=_saved_listing_ids(), **ctx))
        # Prevent browser back-button from replaying a cached response that still
        # contains the welcome banner HTML (the session flag is already consumed).
        _mp_resp.headers['Cache-Control'] = 'no-store'
        return _mp_resp


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
    For AJAX requests returns JSON 403; for browser form submits flashes an
    error and redirects back so the user never sees a bare 400 page.
    """
    from flask_wtf.csrf import validate_csrf, ValidationError
    token = request.form.get('csrf_token') or request.headers.get('X-CSRFToken', '')
    try:
        validate_csrf(token)
    except ValidationError:
        is_ajax = (
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or request.is_json
            or bool(request.headers.get('X-CSRFToken'))  # explicit AJAX CSRF header
        )
        if is_ajax:
            # Build a 403 JSON response and raise it via abort()
            from flask import make_response as _mkr
            _r = _mkr(jsonify(error='Session expired — please refresh the page and try again.'), 403)
            abort(_r)
        # Browser form submit: redirect back with a friendly flash so the
        # seller never sees a raw "Bad Request" page.  Photos are already saved.
        flash('Your session expired mid-upload. Your photos are saved — please try again.', 'warning')
        referrer = request.referrer or url_for('home')
        abort(redirect(referrer))


@app.route('/listing/csrf-refresh', methods=['GET'])
@require_login
def csrf_refresh():
    """Return a fresh CSRF token so the wizard can keep its hidden field current."""
    from flask_wtf.csrf import generate_csrf as _gen
    return jsonify(token=_gen())


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
        # Vehicle-specific fields (applied/cleared based on category)
        _apply_vehicle_fields(listing, form)


_VALID_TRANSMISSIONS  = frozenset({'automatic', 'manual', 'cvt', 'other'})
_VALID_FUEL_TYPES     = frozenset({'gasoline', 'diesel', 'electric', 'hybrid', 'plug_in_hybrid', 'other'})
_VALID_DRIVETRAINS    = frozenset({'fwd', 'rwd', 'awd', '4wd', 'other'})
_VALID_TITLE_STATUSES = frozenset({'clean', 'salvage', 'rebuilt', 'lien', 'other'})


def _apply_vehicle_fields(listing, form):
    """Save vehicle-specific fields from a submitted form onto a Listing instance.

    Safe to call for any listing — fields are cleared when category != Vehicles.
    Does NOT commit.
    """
    from models import Category as _VC
    _vcat = _VC.query.filter_by(name='Vehicles').first()
    if not (_vcat and listing.category_id == _vcat.id):
        # Category changed away from Vehicles — wipe stale data
        for _f in ('vehicle_year', 'vehicle_make', 'vehicle_model', 'vehicle_trim',
                   'vehicle_body_style', 'vehicle_mileage', 'vehicle_exterior_color',
                   'vehicle_transmission', 'vehicle_fuel_type', 'vehicle_drivetrain',
                   'vehicle_vin', 'vehicle_title_status'):
            setattr(listing, _f, None)
        return

    _yr = form.get('vehicle_year', '').strip()
    try:
        _yr_int = int(_yr)
        listing.vehicle_year = _yr_int if 1886 <= _yr_int <= 2030 else None
    except (ValueError, TypeError):
        listing.vehicle_year = None

    _make = form.get('vehicle_make', '').strip()
    if _make == 'Other':
        _other = form.get('vehicle_make_other', '').strip()
        listing.vehicle_make = _other[:50] if _other else 'Other'
    else:
        listing.vehicle_make = _make[:50] if _make else None

    _vmodel = form.get('vehicle_model', '').strip()
    if _vmodel == 'Other':
        _vmodel_other = form.get('vehicle_model_other', '').strip()
        listing.vehicle_model = _vmodel_other[:100] if _vmodel_other else None
    else:
        listing.vehicle_model = _vmodel[:100] if _vmodel else None
    listing.vehicle_trim           = (form.get('vehicle_trim', '').strip()[:100] or None)
    listing.vehicle_body_style     = (form.get('vehicle_body_style', '').strip()[:50] or None)
    listing.vehicle_exterior_color = (form.get('vehicle_exterior_color', '').strip()[:50] or None)

    _mi = form.get('vehicle_mileage', '').strip()
    try:
        listing.vehicle_mileage = int(float(_mi)) if _mi else None
    except (ValueError, TypeError):
        listing.vehicle_mileage = None

    _tr = form.get('vehicle_transmission', '').strip()
    listing.vehicle_transmission = _tr if _tr in _VALID_TRANSMISSIONS else None
    _fu = form.get('vehicle_fuel_type', '').strip()
    listing.vehicle_fuel_type = _fu if _fu in _VALID_FUEL_TYPES else None
    _dr = form.get('vehicle_drivetrain', '').strip()
    listing.vehicle_drivetrain = _dr if _dr in _VALID_DRIVETRAINS else None
    _ts = form.get('vehicle_title_status', '').strip()
    listing.vehicle_title_status = _ts if _ts in _VALID_TITLE_STATUSES else None
    # VIN stored but NEVER displayed publicly
    listing.vehicle_vin = (form.get('vehicle_vin', '').strip()[:50] or None)


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


@app.route("/listing/new", methods=["GET", "POST"])
@require_login
def listing_new():
    """Step 1 of the listing wizard — stateless on GET; no DB row created until step 2."""
    if not current_user.user_type and not current_user.is_admin:
        return redirect(url_for('choose_role'))
    from models import Listing, Category
    lt = request.args.get('type', 'item')
    if lt not in ('item', 'property_sale', 'rental'):
        lt = 'item'

    if request.method == "POST":
        _check_listing_csrf()
        # Step 1 submitted — listing may already exist if the seller uploaded photos.
        listing_id_raw = request.form.get('listing_id', '').strip()
        listing_lt = request.form.get('listing_type', lt)
        if listing_lt not in ('item', 'property_sale', 'rental'):
            listing_lt = 'item'
        if listing_id_raw and listing_id_raw.isdigit():
            lid = int(listing_id_raw)
            existing = Listing.query.filter_by(id=lid, seller_id=current_user.id,
                                               status='draft').first()
            if existing:
                return redirect(url_for('listing_step', listing_id=lid, step=2))
        # No listing yet (no photos uploaded) — create a minimal draft now so step 2
        # can set the title.  The listing is created here rather than on GET so that
        # sellers who visit /listing/new and abandon immediately leave no trace.
        draft = Listing(seller_id=current_user.id, title='', status='draft',
                        moderation_status='approved', listing_type=listing_lt)
        # Auto-assign the Housing & Real Estate category for property listings so
        # the category chip filter on the marketplace returns them correctly.
        if listing_lt in ('property_sale', 'rental'):
            _housing_cat = Category.query.filter_by(slug='housing', is_active=True).first()
            if _housing_cat:
                draft.category_id = _housing_cat.id
        db.session.add(draft)
        db.session.commit()
        return redirect(url_for('listing_step', listing_id=draft.id, step=2))

    # GET — render step 1 without touching the database
    TOTAL_STEPS = 6
    labels = ['Photos', 'Details', 'Price', 'Location', 'Options', 'Review']
    return render_template('listing_wizard.html',
                           listing=None,
                           listing_type=lt,
                           step=1,
                           total_steps=TOTAL_STEPS,
                           labels=labels,
                           categories=[])


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
                # Auto-assign the Housing & Real Estate category so the marketplace
                # category chip filter returns this listing correctly.
                _housing_cat2 = Category.query.filter_by(slug='housing', is_active=True).first()
                if _housing_cat2:
                    listing.category_id = _housing_cat2.id
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
                # Vehicle-specific fields (only saved when Vehicles category selected)
                _apply_vehicle_fields(listing, request.form)

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
                else:
                    listing.latitude  = None
                    listing.longitude = None
                    flash(
                        f"ZIP code {listing.zip_code} wasn't found in our location database. "
                        "Your listing will still be saved, but it may not appear in location-based "
                        "searches. Double-check the ZIP and update it if needed.",
                        "warning"
                    )
            else:
                listing.latitude  = None
                listing.longitude = None
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
            return redirect(url_for('selling'))

        # Stamp draft_activity_at so the reminder system knows the seller
        # actively returned to this draft.  Only applies to steps 1-5 (step 6
        # sets status='active' and returns early before reaching this line).
        if listing.status == 'draft':
            listing.draft_activity_at = datetime.now()
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


def _touch_draft_activity(listing_id):
    """Stamp draft_activity_at on a draft listing whenever any content is mutated.

    Photo and video endpoints only write to ListingPhoto / ListingVideo rows, so
    the Listing.updated_at column is never bumped.  This helper gives the draft-
    reminder logic a reliable "seller was here recently" signal regardless of which
    sub-resource changed.  It is a no-op for non-draft listings (the WHERE clause
    limits to status='draft') and silently swallows errors so it never breaks the
    surrounding request.

    IMPORTANT: do NOT call db.session.commit() here — the caller owns the
    transaction and will commit after all their writes are done.
    """
    try:
        from sqlalchemy import text as _sq_text
        db.session.execute(
            _sq_text(
                "UPDATE listings SET draft_activity_at = :now "
                "WHERE id = :lid AND status = 'draft'"
            ),
            {"now": datetime.now(), "lid": listing_id},
        )
    except Exception as _e:
        app.logger.debug(
            "_touch_draft_activity: update skipped for listing #%s: %s", listing_id, _e
        )


@app.route("/listing/<int:listing_id>/photo/upload", methods=["POST"])
@require_login
def listing_photo_upload(listing_id):
    """AJAX: upload a photo to a listing (max 20)."""
    _check_listing_csrf()
    from models import Listing, ListingPhoto
    listing = _listing_owner_or_403(listing_id)

    current_count = ListingPhoto.query.filter_by(listing_id=listing_id).count()
    if current_count >= 20:
        return jsonify(error='Maximum 20 photos allowed per listing.'), 400

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
    _touch_draft_activity(listing_id)
    db.session.commit()

    photo_url = storage_url or url_for('serve_listing_photo', photo_id=lp.id)
    return jsonify(id=lp.id, url=photo_url, is_primary=lp.is_primary), 200


@app.route("/listing/new/photo/upload", methods=["POST"])
@require_login
def listing_new_photo_upload():
    """AJAX: lazily create a draft listing then upload its first photo.

    Called from the stateless step-1 page when the seller uploads a photo before
    any Listing row exists.  Returns JSON with ``listing_id`` so the client can
    use the regular upload endpoint for subsequent photos in the same session.
    """
    if not current_user.user_type and not current_user.is_admin:
        return jsonify(error='Not authorized'), 403
    _check_listing_csrf()
    from models import Listing, ListingPhoto

    lt = request.args.get('type', 'item')
    if lt not in ('item', 'property_sale', 'rental'):
        lt = 'item'

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

    # Create the draft listing now that the seller has shown real intent.
    # Set draft_activity_at to now so the recency guard in send_draft_reminders
    # recognises this as a freshly-touched draft from the very beginning.
    draft = Listing(seller_id=current_user.id, title='', status='draft',
                    moderation_status='approved', listing_type=lt,
                    draft_activity_at=datetime.now())
    db.session.add(draft)
    db.session.flush()  # get draft.id without a full commit yet

    from storage import upload_file as _upload_file
    filename, storage_url = _upload_file(photo, ext)

    lp = ListingPhoto(
        listing_id=draft.id,
        filename=filename,
        storage_url=storage_url,
        data=data if not storage_url else None,
        content_type=ct,
        display_order=0,
        is_primary=True,
    )
    db.session.add(lp)
    db.session.commit()

    photo_url = storage_url or url_for('serve_listing_photo', photo_id=lp.id)
    return jsonify(listing_id=draft.id, id=lp.id, url=photo_url, is_primary=True), 200


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
    _touch_draft_activity(listing_id)
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
    _touch_draft_activity(listing_id)
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
    _touch_draft_activity(listing_id)
    db.session.commit()
    return jsonify(ok=True), 200


# ── Listing Video Upload / Delete ────────────────────────────────────────────

VIDEO_ALLOWED_EXTS = {'.mp4', '.mov', '.webm', '.avi', '.m4v'}
VIDEO_ALLOWED_CT   = {'video/mp4', 'video/quicktime', 'video/webm',
                      'video/avi', 'video/x-msvideo', 'video/x-m4v'}
VIDEO_MAX_BYTES    = 150 * 1024 * 1024  # 150 MB

@app.route("/listing/<int:listing_id>/video/upload", methods=["POST"])
@require_login
def listing_video_upload(listing_id):
    """AJAX: attach one video to a listing (max 1, max 150 MB, max 60 s client-reported)."""
    _check_listing_csrf()
    from models import Listing, ListingVideo
    _listing_owner_or_403(listing_id)

    # Only one video allowed
    if ListingVideo.query.filter_by(listing_id=listing_id).count() >= 1:
        return jsonify(error='Only one video per listing is allowed.'), 400

    vid = request.files.get('video')
    if not vid or not vid.filename:
        return jsonify(error='No video file received.'), 400

    ext = os.path.splitext(vid.filename)[1].lower()
    if ext not in VIDEO_ALLOWED_EXTS:
        return jsonify(error='Unsupported video type. Use MP4, MOV, WebM, or AVI.'), 400

    # Size check (read once — stream to memory only to measure; real projects stream to disk)
    raw = vid.read()
    if len(raw) > VIDEO_MAX_BYTES:
        return jsonify(error='Video must be under 150 MB.'), 400
    vid.seek(0)

    # MIME sniff
    ct = vid.content_type or ''
    if ct and ct not in VIDEO_ALLOWED_CT:
        # Allow extension-only match as fallback
        if not any(ext == e for e in VIDEO_ALLOWED_EXTS):
            return jsonify(error='Invalid video content type.'), 400

    # Try to upload to storage (DO Spaces / S3); fall back gracefully
    storage_url = None
    filename = None
    try:
        from storage import upload_file as _uf
        filename, storage_url = _uf(vid, ext)
    except Exception:
        filename = f'video_{listing_id}_{int(datetime.now().timestamp())}{ext}'

    duration = None
    try:
        duration = float(request.form.get('duration_seconds', 0)) or None
    except (ValueError, TypeError):
        pass

    # Server-side duration guard (uses client-reported value; rejects > 60s)
    if duration and duration > 60:
        return jsonify(error='Videos must be 60 seconds or less.'), 400

    lv = ListingVideo(
        listing_id=listing_id,
        filename=filename,
        storage_url=storage_url,
        content_type=ct or f'video/{ext.lstrip(".")}',
        file_size_bytes=len(raw),
        duration_seconds=duration,
    )
    db.session.add(lv)
    _touch_draft_activity(listing_id)
    db.session.commit()
    return jsonify(id=lv.id, url=storage_url or '', ok=True), 200


@app.route("/listing/<int:listing_id>/video/<int:video_id>/delete", methods=["POST"])
@require_login
def listing_video_delete(listing_id, video_id):
    """AJAX: remove the video from a listing."""
    _check_listing_csrf()
    from models import ListingVideo
    _listing_owner_or_403(listing_id)
    lv = ListingVideo.query.filter_by(id=video_id, listing_id=listing_id).first_or_404()
    if lv.filename:
        try:
            from storage import delete_file as _df
            _df(lv.filename)
        except Exception:
            pass
    db.session.delete(lv)
    _touch_draft_activity(listing_id)
    db.session.commit()
    return jsonify(ok=True), 200


# ─────────────────────────────────────────────────────────────────────────────

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
        # Capture current price before edits for price-drop detection (Phase M)
        _price_before_edit = listing.price
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

        # If the seller extended the expiry date beyond the 3-day reminder window,
        # reset the flag so they will receive the reminder again for the new deadline.
        from datetime import datetime as _dt_now, timedelta as _td_now
        if (listing.expires_at and
                listing.expires_at > _dt_now.now() + _td_now(days=3)):
            listing.expiry_reminder_sent = False
        db.session.commit()

        # Phase M: trigger price-drop notifications if price dropped for active listing
        try:
            if (_price_before_edit is not None and listing.price is not None and
                    listing.price < _price_before_edit and
                    listing.status in ('active', 'approved')):
                from notification_service import notify_price_drop as _notify_pd
                _notify_pd(listing.id, float(_price_before_edit), float(listing.price))
        except Exception as _pd_err:
            app.logger.warning("listing_edit: price-drop notification failed: %s", _pd_err)

        flash("Listing updated.", "success")
        return redirect(url_for('selling'))

    return render_template('listing_edit.html', listing=listing, categories=categories)


@app.route("/listing/<int:listing_id>/delete", methods=["POST"])
@require_login
def listing_delete(listing_id):
    """Delete a listing and all its photos (DB rows + stored files)."""
    _check_listing_csrf()
    from models import Listing, ListingPhoto, ListingOffer
    listing = _listing_owner_or_403(listing_id)
    # Capture pending-offer buyers BEFORE cascade-delete wipes the rows
    _saved_title = listing.title
    _pending_buyers = (ListingOffer.query
                       .filter(
                           ListingOffer.listing_id == listing_id,
                           ListingOffer.status.in_(['pending', 'countered'])
                       )
                       .join(User, ListingOffer.buyer_id == User.id)
                       .with_entities(ListingOffer.amount, User.email)
                       .all())
    # Collect filenames before cascade-delete removes them from the session
    filenames = [p.filename for p in listing.photos if p.filename]
    # Remove any gallery pins pointing to this listing before deleting it
    from models import GalleryPhoto
    GalleryPhoto.query.filter_by(item_type='listing', listing_id=listing_id).delete(synchronize_session=False)
    db.session.delete(listing)
    db.session.commit()
    # Notify affected buyers now that the listing is gone
    for _offer_amount, _buyer_email in _pending_buyers:
        if _buyer_email:
            try:
                notify_buyer_offer_expired(_buyer_email, _saved_title, listing_id, _offer_amount)
            except Exception as _e:
                app.logger.warning("listing_delete: notify_buyer_offer_expired failed: %s", _e)
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
    return redirect(url_for('selling'))


@app.route("/listing/<int:listing_id>/discard", methods=["POST"])
@require_login
def listing_discard(listing_id):
    """Immediately discard an in-progress draft listing, deleting all stored media."""
    _check_listing_csrf()
    from models import Listing
    listing = Listing.query.filter_by(id=listing_id, seller_id=current_user.id,
                                     status='draft').first()
    if not listing:
        # Not found or not a draft — silently redirect to sell chooser
        return redirect(url_for('sell'))
    try:
        from storage import delete_file as _delete_file
        for photo in list(listing.photos):
            if photo.filename:
                try:
                    _delete_file(photo.filename)
                except Exception as _pe:
                    app.logger.warning("listing_discard: photo %s: %s", photo.filename, _pe)
        for video in list(getattr(listing, 'videos', [])):
            if video.filename:
                try:
                    _delete_file(video.filename)
                except Exception as _ve:
                    app.logger.warning("listing_discard: video %s: %s", video.filename, _ve)
    except Exception as _se:
        app.logger.warning("listing_discard: storage import failed: %s", _se)
    db.session.delete(listing)
    db.session.commit()
    return redirect(url_for('sell'))


@app.route("/listing/<int:listing_id>/status", methods=["POST"])
@require_login
def listing_set_status(listing_id):
    """Allow a seller to mark their listing as sold, reserved, or active."""
    _check_listing_csrf()
    from models import Listing
    import datetime
    listing = _listing_owner_or_403(listing_id)
    new_status = request.form.get('status', '').strip()
    allowed = ('sold', 'reserved', 'active', 'pending')
    if new_status not in allowed:
        flash("Invalid status.", "error")
        return redirect(url_for('selling'))
    # Only allow transitioning from sensible states
    if new_status == 'active' and listing.status not in ('sold', 'reserved', 'expired', 'pending'):
        flash("Cannot reactivate a listing that is not sold, reserved, pending, or expired.", "error")
        return redirect(url_for('selling'))
    if new_status in ('sold', 'reserved', 'pending') and listing.status not in ('active', 'reserved', 'sold', 'pending'):
        flash("Only active or sold/reserved/pending listings can be updated.", "error")
        return redirect(url_for('selling'))
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
            listing.expiry_reminder_sent = False  # new expiry cycle — reset reminder
        elif not listing.expires_at or listing.expires_at <= datetime.datetime.now():
            # Reactivating from sold/reserved with no valid future expiry — reset to 30 days
            listing.expires_at = datetime.datetime.now() + datetime.timedelta(days=30)
            listing.expiry_reminder_sent = False  # new expiry cycle — reset reminder
    # Expire any open offers when the listing is no longer available.
    # For sold, also email buyers ("no longer available").
    # For reserved, expire silently — the watcher notification below handles buyer comms.
    # Capture open offer buyers BEFORE expiring so we can notify them afterwards.
    offer_buyer_ids: list = []
    if new_status == 'sold':
        _expire_offers_and_notify(listing_id, listing.title)
    elif new_status == 'reserved':
        from models import ListingOffer as _LO
        offer_buyer_ids = [
            str(o.buyer_id)
            for o in _LO.query.filter(
                _LO.listing_id == listing_id,
                _LO.status.in_(['pending', 'countered'])
            ).all()
        ]
        expire_pending_offers(listing_id)
    db.session.commit()
    # Deactivate any gallery pins for this listing if it's no longer active;
    # re-activate auto-deactivated pins if the listing is back to active+approved.
    if new_status != 'active':
        try:
            _deactivate_stale_gallery_pins()
        except Exception as _gp_err:
            app.logger.warning("listing_set_status: gallery pin cleanup failed: %s", _gp_err)
    elif listing.moderation_status == 'approved':
        try:
            _reactivate_gallery_pins(listing.id)
        except Exception as _gp_err:
            app.logger.warning("listing_set_status: gallery pin reactivation failed: %s", _gp_err)
    labels = {'sold': 'Listing marked as sold.', 'reserved': 'Listing marked as reserved.', 'active': 'Listing reactivated.', 'pending': 'Listing marked as pending sale.'}
    flash(labels[new_status], "success")
    # Notify buyers watching this listing when it transitions to reserved
    if new_status == 'reserved' and prior_status != 'reserved':
        try:
            from notification_service import notify_listing_reserved_to_watchers
            notify_listing_reserved_to_watchers(listing_id, listing.title,
                                                offer_buyer_ids=offer_buyer_ids)
        except Exception as _notif_err:
            app.logger.warning("listing_set_status: reserved notification failed: %s", _notif_err)
    # Notify buyers with active offers when the listing moves to Pending Sale
    if new_status == 'pending' and prior_status != 'pending':
        try:
            from notification_service import notify_buyers_listing_pending
            notify_buyers_listing_pending(listing_id, listing.title)
        except Exception as _pending_err:
            app.logger.warning("listing_set_status: pending sale notifications failed: %s", _pending_err)
    return redirect(url_for('selling'))


@app.route("/listing/<int:listing_id>/renew", methods=["POST"])
@require_login
def listing_renew(listing_id):
    """Allow a seller to renew an expired listing, resetting its expiry to 30 days from now."""
    _check_listing_csrf()
    import datetime
    listing = _listing_owner_or_403(listing_id)
    if listing.status != 'expired':
        flash("Only expired listings can be renewed.", "error")
        return redirect(url_for('selling'))
    listing.status = 'active'
    listing.expires_at = datetime.datetime.now() + datetime.timedelta(days=30)
    listing.expired_at = None
    listing.expiry_reminder_sent = False  # start a fresh expiry cycle
    db.session.commit()
    if listing.moderation_status == 'approved':
        try:
            _reactivate_gallery_pins(listing.id)
        except Exception as _gp_err:
            app.logger.warning("listing_renew: gallery pin reactivation failed: %s", _gp_err)
    flash("Your listing has been renewed and is active for 30 more days.", "success")
    return redirect(url_for('selling'))


@app.route("/my-listings")
def my_listings_redirect():
    """Backward-compat redirect — canonical seller dashboard is now /selling."""
    from flask import redirect
    return redirect(url_for('selling'), 301)


@app.route("/selling")
@require_login
def selling():
    """Seller dashboard: overview stats, status counts, filtered listing list."""
    from models import Listing, ListingOffer, ListingConversation
    from sqlalchemy import func
    from datetime import datetime as _dt, timedelta

    status_filter = request.args.get('filter', '').lower().strip()

    # All non-draft listings for this seller (no-draft policy)
    all_listings = (Listing.query
                    .filter(Listing.seller_id == current_user.id,
                            Listing.status != 'draft')
                    .order_by(Listing.created_at.desc())
                    .all())

    # Find draft listings that are close to being auto-deleted — these are the
    # ones whose expiry warning will be displayed on this page.  Only drafts in
    # the same window used by the reminder email (≥ 24 h old, < 48 h old, no
    # title) qualify.  We touch draft_last_seen_at on exactly those listings so
    # the reminder email is skipped — the seller has already seen the warning.
    from draft_cleanup import REMINDER_MIN_HOURS, DRAFT_MAX_AGE_HOURS
    now_ts = _dt.now()
    _reminder_cutoff = now_ts - timedelta(hours=REMINDER_MIN_HOURS)
    _delete_cutoff   = now_ts - timedelta(hours=DRAFT_MAX_AGE_HOURS)
    expiring_drafts = (Listing.query
                       .filter(
                           Listing.seller_id == current_user.id,
                           Listing.status == 'draft',
                           db.or_(Listing.title == None, Listing.title == ''),
                           Listing.created_at <= _reminder_cutoff,
                           Listing.created_at > _delete_cutoff,
                       )
                       .order_by(Listing.created_at.asc())
                       .all())
    if expiring_drafts:
        try:
            expiring_ids = [d.id for d in expiring_drafts]
            (
                Listing.query
                .filter(Listing.id.in_(expiring_ids))
                .update({Listing.draft_last_seen_at: now_ts}, synchronize_session=False)
            )
            db.session.commit()
        except Exception as _dls_err:
            db.session.rollback()
            app.logger.debug("selling: draft_last_seen_at touch failed: %s", _dls_err)

    # Per-status counts for the dashboard overview cards
    status_counts = {s: 0 for s in ('active', 'sold', 'reserved', 'pending', 'expired', 'removed')}
    for lst in all_listings:
        if lst.status in status_counts:
            status_counts[lst.status] += 1
    status_counts['total'] = len(all_listings)

    # Seller performance stats
    total_views = sum(lst.view_count or 0 for lst in all_listings)
    if all_listings:
        listing_ids = [lst.id for lst in all_listings]
        total_conversations = (db.session.query(func.count(ListingConversation.id))
                               .filter(ListingConversation.listing_id.in_(listing_ids))
                               .scalar() or 0)
    else:
        total_conversations = 0

    # Pending offers count (for the "Offers" badge)
    pending_offers_count = (ListingOffer.query
                            .filter_by(seller_id=current_user.id)
                            .filter(ListingOffer.status.in_(['pending', 'countered']))
                            .count())

    # Total offers ever received (all statuses) — for Performance card
    total_offers_count = (ListingOffer.query
                          .filter_by(seller_id=current_user.id)
                          .count())

    # Apply status filter for the listing list below the dashboard
    valid_filters = ('active', 'sold', 'reserved', 'pending', 'expired', 'removed')
    if status_filter in valid_filters:
        listings = [lst for lst in all_listings if lst.status == status_filter]
    else:
        status_filter = 'all'
        listings = all_listings

    return render_template('selling.html',
                           listings=listings,
                           status_counts=status_counts,
                           total_views=total_views,
                           total_conversations=total_conversations,
                           pending_offers_count=pending_offers_count,
                           total_offers_count=total_offers_count,
                           status_filter=status_filter,
                           hidden_draft_count=0,
                           expiring_drafts=expiring_drafts,
                           now=_dt.now())


@app.route("/listing/<int:listing_id>")
@app.route("/listing/<int:listing_id>-<slug>")
def listing_detail(listing_id, slug=None):
    """Individual listing detail page (Phase 5–6)."""
    from models import Listing, ListingFavorite

    listing = Listing.query.get_or_404(listing_id)

    # Access control: active/sold/reserved + approved listings are public.
    # Seller and admin can see any status.
    is_owner = current_user.is_authenticated and current_user.id == listing.seller_id
    is_admin = current_user.is_authenticated and current_user.is_admin
    is_public = (listing.status in ('active', 'sold', 'reserved', 'pending') and listing.moderation_status == 'approved')
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
        # On-read expiry check: mark stale pending/countered offers as expired
        if (buyer_offer
                and buyer_offer.status in ('pending', 'countered')
                and buyer_offer.expires_at
                and buyer_offer.expires_at < datetime.now()):
            buyer_offer.status = 'expired'
            buyer_offer.updated_at = datetime.now()
            try:
                db.session.commit()
                # Notify the buyer that their own offer window closed
                try:
                    notify_buyer_offer_timed_out(
                        current_user.email,
                        listing.title,
                        listing_id,
                        buyer_offer.amount,
                    )
                except Exception as _ne:
                    app.logger.warning(
                        "on-read timed-offer notify failed (listing #%s): %s",
                        listing_id, _ne,
                    )
            except Exception:
                db.session.rollback()

    # Seller's incoming offers (owner view only, negotiable listings)
    seller_offers = []
    if is_owner and listing.price_type == 'negotiable':
        from models import ListingOffer
        # Sweep time-expired offers before rendering so the seller sees accurate statuses
        try:
            timed_out_targets = expire_stale_timed_offers()
            if timed_out_targets:
                db.session.commit()
                for _t in timed_out_targets:
                    if not _t.get('buyer_email'):
                        continue
                    try:
                        notify_buyer_offer_timed_out(
                            _t['buyer_email'],
                            _t['listing_title'],
                            _t['listing_id'],
                            _t['offer_amount'],
                        )
                    except Exception as _ne:
                        app.logger.warning(
                            "listing detail sweep: timed-offer notify failed "
                            "(offer #%s): %s", _t['offer_id'], _ne,
                        )
        except Exception:
            db.session.rollback()
        seller_offers = (ListingOffer.query
                         .filter_by(listing_id=listing_id)
                         .filter(ListingOffer.status.in_(['pending', 'countered', 'accepted']))
                         .order_by(ListingOffer.created_at.desc())
                         .all())

    # Similar listings — shown to non-owners for all non-draft/removed statuses
    similar_listings = []
    similar_fallback = False
    if listing.status not in ('draft', 'removed') and not is_owner:
        try:
            from ai.recommendations import get_similar_listings as _get_sim
            similar_listings, similar_fallback = _get_sim(
                listing,
                user=current_user if current_user.is_authenticated else None,
                limit=6,
            )
        except Exception as _sim_err:
            app.logger.debug("similar_listings fallback: %s", _sim_err)
            # Hard fallback: same category, random order
            from models import Listing as _SL
            from sqlalchemy import func as _sim_func
            sim_q = _SL.query.filter(
                _SL.id != listing_id,
                _SL.status == 'active',
                _SL.moderation_status == 'approved',
            )
            if listing.category_id:
                sim_q = sim_q.filter(_SL.category_id == listing.category_id)
            else:
                similar_fallback = True
            similar_listings = sim_q.order_by(_sim_func.random()).limit(6).all()

    # ── SEO context ────────────────────────────────────────────────────────────
    _seo_base = os.environ.get("APP_BASE_URL", "https://jhehaul.com").rstrip("/")
    _seo_path = listing_canonical_path(listing)
    _seo_url  = _seo_base + _seo_path
    _primary_photo_url = None
    if photos:
        _pp = photos[0]
        _primary_photo_url = (_pp.storage_url
                              or url_for("serve_listing_photo", photo_id=_pp.id, _external=True))
    _seo_ld_dict = _listing_jsonld(listing, _seo_url, _primary_photo_url)
    _seo_ld_json = _json.dumps(_seo_ld_dict, ensure_ascii=False) if _seo_ld_dict else None

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
        similar_listings=similar_listings,
        similar_fallback=similar_fallback,
        seo_title=listing_seo_title(listing),
        seo_description=listing_seo_description(listing),
        seo_canonical_path=_seo_path,
        seo_canonical_url=_seo_url,
        seo_primary_photo_url=_primary_photo_url,
        seo_jsonld_json=_seo_ld_json,
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
        is_public = listing.status in ('active', 'sold', 'reserved', 'pending') and listing.moderation_status == 'approved'
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
    is_public = listing.status in ('active', 'sold', 'reserved', 'pending') and listing.moderation_status == 'approved'
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
        from datetime import timedelta as _offer_td
        offer = ListingOffer(
            listing_id=listing_id,
            buyer_id=current_user.id,
            seller_id=listing.seller_id,
            amount=amount,
            message=message or None,
            status='pending',
            expires_at=datetime.now() + _offer_td(days=7),
        )
        db.session.add(offer)

    db.session.commit()

    # SMS notifications disabled for marketplace — in-app + email are used instead
    seller = listing.seller

    # In-app notification → seller
    try:
        from notification_service import notify_new_offer as _nno
        _nno(seller_id=listing.seller_id,
             buyer_name=current_user.first_name or current_user.email or 'A buyer',
             amount=amount,
             listing_title=listing.title,
             listing_id=listing_id,
             offer_id=offer.id)
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

    # Honour a safe local next-URL so the seller can be sent back to the inbox
    _next_raw = request.form.get('next', '').strip()
    _next_url = (_next_raw
                 if _next_raw and _next_raw.startswith('/') and not _next_raw.startswith('//')
                 else None)

    def _done_redirect():
        return redirect(_next_url or url_for('listing_detail', listing_id=listing_id))

    # Enforce time-based expiry before any action
    if (offer.status in ('pending', 'countered')
            and offer.expires_at and offer.expires_at < datetime.now()):
        offer.status = 'expired'
        offer.updated_at = datetime.now()
        _expired_buyer = offer.buyer
        _expired_listing_title = listing.title
        _expired_listing_id = listing_id
        _expired_amount = offer.amount
        _expired_offer_id = offer.id
        try:
            db.session.commit()
            # Notify the buyer that their offer window closed
            if _expired_buyer and _expired_buyer.email:
                try:
                    notify_buyer_offer_timed_out(
                        _expired_buyer.email,
                        _expired_listing_title,
                        _expired_listing_id,
                        _expired_amount,
                    )
                except Exception as _ne:
                    app.logger.warning(
                        "offer_seller_respond: timed-offer notify failed "
                        "(offer #%s): %s", _expired_offer_id, _ne,
                    )
        except Exception:
            db.session.rollback()
        flash("This offer has expired and can no longer be accepted, declined, or countered.", "error")
        return _done_redirect()

    if offer.status not in ('pending', 'countered'):
        flash("This offer is no longer open.", "error")
        return _done_redirect()

    action = request.form.get('action', '').strip()
    import math as _math

    if action == 'accept':
        # ── Atomicity: lock the Listing row so concurrent accept requests
        # serialize behind a single write lock.  Locking the Listing row (which
        # always exists) prevents the lost-update race that locking only
        # ListingOffer rows (which may not yet exist) cannot avoid.
        # PostgreSQL honours FOR UPDATE; SQLite raises CompileError at
        # compile-time, so we fall back gracefully — the partial unique index
        # on listing_offers (listing_id) WHERE status='accepted' is then the
        # final safety net in both environments.
        from models import Listing as _ListingModel
        try:
            _ListingModel.query.filter_by(id=listing_id).with_for_update().one_or_none()
        except Exception:
            db.session.rollback()
            # Re-fetch offer after rollback (session objects are expired)
            offer = ListingOffer.query.get_or_404(offer_id)

        # Check for an already-accepted sibling offer
        already_accepted = ListingOffer.query.filter(
            ListingOffer.listing_id == listing_id,
            ListingOffer.id != offer_id,
            ListingOffer.status == 'accepted',
        ).first()
        if already_accepted:
            flash("An offer on this listing has already been accepted. Only one offer can be accepted.", "error")
            return _done_redirect()

        offer.status = 'accepted'
        offer.updated_at = datetime.now()

        # Mark the listing as reserved immediately so other buyers see the
        # correct status and cannot submit new offers.
        prior_listing_status = listing.status
        listing.status = 'reserved'

        # Extend expires_at so the background job cannot silently expire a
        # listing while its accepted offer is still active.  We push it out
        # to at least 30 days from now; if the existing expiry is already
        # further in the future we leave it alone.
        from datetime import timedelta as _timedelta
        _reserve_min_expiry = datetime.now() + _timedelta(days=30)
        if listing.expires_at is None or listing.expires_at < _reserve_min_expiry:
            listing.expires_at = _reserve_min_expiry
            listing.expiry_reminder_sent = False  # re-arm the 3-day reminder

        # Auto-decline all other open offers on the same listing so no
        # second buyer is left waiting on a deal that won't happen.
        other_open = ListingOffer.query.filter(
            ListingOffer.listing_id == listing_id,
            ListingOffer.id != offer_id,
            ListingOffer.status.in_(['pending', 'countered']),
        ).all()
        # Capture declined buyer info before the commit for notifications below.
        _declined_buyers = [(o.id, o.buyer_id, o.buyer, o.amount) for o in other_open]
        _now = datetime.now()
        for _other in other_open:
            _other.status = 'declined'
            _other.updated_at = _now

        try:
            db.session.commit()
        except Exception as _ie:
            db.session.rollback()
            app.logger.warning("offer accept IntegrityError (concurrent race) listing=%s: %s", listing_id, _ie)
            flash("An offer on this listing was just accepted simultaneously. Please refresh and try again.", "error")
            return _done_redirect()
        buyer = offer.buyer
        # SMS disabled for marketplace — in-app + email used instead
        # Email notification → buyer
        try:
            from email_service import notify_buyer_offer_accepted as _eboa
            if buyer and buyer.email:
                _eboa(buyer.email, listing.title, listing_id, offer.amount)
        except Exception:
            pass
        # In-app notification → buyer
        try:
            from notification_service import notify_offer_accepted as _noa
            _noa(buyer_id=offer.buyer_id,
                 amount=offer.amount,
                 listing_title=listing.title,
                 listing_id=listing_id,
                 offer_id=offer.id)
        except Exception:
            pass

        # Notify each auto-declined buyer (email + in-app)
        for _oid, _bid, _obuyer, _oamount in _declined_buyers:
            try:
                from email_service import notify_buyer_offer_declined as _ebod_auto
                if _obuyer and _obuyer.email:
                    _ebod_auto(_obuyer.email, listing.title, listing_id, _oamount)
            except Exception as _e:
                app.logger.warning("auto-decline email failed offer=%s: %s", _oid, _e)
            try:
                from notification_service import notify_offer_declined as _nod_auto
                _nod_auto(buyer_id=_bid,
                          listing_title=listing.title,
                          listing_id=listing_id,
                          offer_id=_oid)
            except Exception as _e:
                app.logger.warning("auto-decline in-app notif failed offer=%s: %s", _oid, _e)

        # Notify listing watchers (saved / favorited) that the listing is now reserved.
        if prior_listing_status != 'reserved':
            try:
                from notification_service import notify_listing_reserved_to_watchers
                _declined_buyer_ids = [str(_bid) for _, _bid, _, _ in _declined_buyers]
                notify_listing_reserved_to_watchers(listing_id, listing.title,
                                                    offer_buyer_ids=_declined_buyer_ids)
            except Exception as _notif_err:
                app.logger.warning("offer accept: reserved watcher notification failed: %s", _notif_err)

        flash("Offer accepted! The buyer has been notified and your listing is now marked as reserved.", "success")

    elif action == 'decline':
        offer.status = 'declined'
        offer.updated_at = datetime.now()
        db.session.commit()
        buyer = offer.buyer
        # SMS disabled for marketplace — in-app + email used instead
        # Email notification → buyer
        try:
            from email_service import notify_buyer_offer_declined as _ebod
            if buyer and buyer.email:
                _ebod(buyer.email, listing.title, listing_id, offer.amount)
        except Exception:
            pass
        # In-app notification → buyer
        try:
            from notification_service import notify_offer_declined as _nod
            _nod(buyer_id=offer.buyer_id,
                 listing_title=listing.title,
                 listing_id=listing_id,
                 offer_id=offer.id)
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
            return _done_redirect()
        original_amount = offer.amount
        offer.counter_amount = counter_amount
        offer.status = 'countered'
        offer.updated_at = datetime.now()
        db.session.commit()
        buyer = offer.buyer
        # SMS disabled for marketplace — in-app + email used instead
        # Email notification → buyer
        try:
            from email_service import notify_buyer_offer_countered as _eboc
            if buyer and buyer.email:
                _eboc(buyer.email, listing.title, listing_id, original_amount, counter_amount)
        except Exception:
            pass
        # In-app notification → buyer
        try:
            from notification_service import notify_offer_countered as _noc
            _noc(buyer_id=offer.buyer_id,
                 counter_amount=counter_amount,
                 listing_title=listing.title,
                 listing_id=listing_id,
                 offer_id=offer.id)
        except Exception:
            pass
        flash(f"Counteroffer of ${counter_amount:,.0f} sent to the buyer.", "success")

    else:
        flash("Invalid action.", "error")

    return _done_redirect()


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

    # Enforce time-based expiry before any action
    if (offer.status in ('pending', 'countered')
            and offer.expires_at and offer.expires_at < datetime.now()):
        _expired_listing = offer.listing
        _expired_listing_title = _expired_listing.title if _expired_listing else None
        _expired_amount = offer.amount
        _expired_offer_id = offer.id
        offer.status = 'expired'
        offer.updated_at = datetime.now()
        try:
            db.session.commit()
            # Notify the buyer (current_user) that their offer window closed
            if current_user.email:
                try:
                    notify_buyer_offer_timed_out(
                        current_user.email,
                        _expired_listing_title,
                        listing_id,
                        _expired_amount,
                    )
                except Exception as _ne:
                    app.logger.warning(
                        "offer_buyer_respond: timed-offer notify failed "
                        "(offer #%s): %s", _expired_offer_id, _ne,
                    )
        except Exception:
            db.session.rollback()
        flash("This offer has expired. You can make a fresh offer if the listing is still active.", "error")
        return redirect(url_for('listing_detail', listing_id=listing_id))

    if action == 'accept_counter':
        if offer.status != 'countered' or not offer.counter_amount:
            flash("No active counteroffer to accept.", "error")
            return redirect(url_for('listing_detail', listing_id=listing_id))

        # ── Atomicity: lock the Listing row (same pattern as offer_seller_respond)
        from models import Listing as _ListingModelBR
        try:
            _ListingModelBR.query.filter_by(id=listing_id).with_for_update().one_or_none()
        except Exception:
            db.session.rollback()
            offer = ListingOffer.query.get_or_404(offer_id)

        # Check for an already-accepted sibling offer
        _ac_already = ListingOffer.query.filter(
            ListingOffer.listing_id == listing_id,
            ListingOffer.id != offer_id,
            ListingOffer.status == 'accepted',
        ).first()
        if _ac_already:
            flash("An offer on this listing has already been accepted.", "error")
            return redirect(url_for('listing_detail', listing_id=listing_id))

        offer.amount = offer.counter_amount
        offer.status = 'accepted'
        offer.updated_at = datetime.now()

        # Auto-decline all other open offers on the same listing
        _ac_open = ListingOffer.query.filter(
            ListingOffer.listing_id == listing_id,
            ListingOffer.id != offer_id,
            ListingOffer.status.in_(['pending', 'countered']),
        ).all()
        _ac_now = datetime.now()
        for _ac_other in _ac_open:
            _ac_other.status = 'declined'
            _ac_other.updated_at = _ac_now

        try:
            db.session.commit()
        except Exception as _ie:
            db.session.rollback()
            app.logger.warning("accept_counter IntegrityError (concurrent race) listing=%s: %s", listing_id, _ie)
            flash("An offer on this listing was just accepted simultaneously. Please refresh and try again.", "error")
            return redirect(url_for('listing_detail', listing_id=listing_id))
        # SMS disabled for marketplace — in-app notification used instead
        # Email notification → seller
        try:
            from email_service import notify_seller_offer_accepted as _esoa
            seller = offer.seller
            if seller and seller.email:
                _esoa(seller.email, offer.listing.title, listing_id, offer.amount)
        except Exception:
            pass
        # In-app notification → seller
        try:
            from notification_service import notify_counter_accepted as _nca
            _nca(seller_id=offer.seller_id,
                 buyer_name=current_user.first_name or current_user.email or 'The buyer',
                 amount=offer.amount,
                 listing_title=offer.listing.title,
                 listing_id=listing_id,
                 offer_id=offer.id)
        except Exception:
            pass
        flash("You accepted the counteroffer! Contact the seller to arrange pickup.", "success")

    elif action == 'decline_counter':
        if offer.status != 'countered' or not offer.counter_amount:
            flash("No active counteroffer to decline.", "error")
            return redirect(url_for('listing_detail', listing_id=listing_id))
        offer.status = 'declined'
        offer.updated_at = datetime.now()
        db.session.commit()
        # Email notification → seller
        try:
            from email_service import notify_seller_offer_declined as _esod
            seller = offer.seller
            if seller and seller.email:
                _esod(seller.email, offer.listing.title, listing_id, offer.counter_amount)
        except Exception:
            pass
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


@app.route("/seller/offers")
@require_login
def seller_offers():
    """Seller's aggregated inbox — all incoming offers across all their listings."""
    from models import ListingOffer
    # Sweep time-expired offers before rendering so counts are accurate
    try:
        timed_out_targets = expire_stale_timed_offers()
        if timed_out_targets:
            db.session.commit()
            for _t in timed_out_targets:
                if not _t.get('buyer_email'):
                    continue
                try:
                    notify_buyer_offer_timed_out(
                        _t['buyer_email'],
                        _t['listing_title'],
                        _t['listing_id'],
                        _t['offer_amount'],
                    )
                except Exception as _ne:
                    app.logger.warning(
                        "seller inbox sweep: timed-offer notify failed "
                        "(offer #%s): %s", _t['offer_id'], _ne,
                    )
    except Exception:
        db.session.rollback()
    offers = (ListingOffer.query
              .filter_by(seller_id=current_user.id)
              .order_by(ListingOffer.updated_at.desc())
              .all())
    pending_count = sum(1 for o in offers if o.status in ('pending', 'countered'))
    return render_template('seller_offers.html', offers=offers, pending_count=pending_count)


# ── In-App Notifications ─────────────────────────────────────────────────────

_NOTIF_ICONS = {
    'new_message':          '💬',
    'new_offer':            '💰',
    'offer_accepted':       '✅',
    'offer_declined':       '❌',
    'offer_countered':      '🔄',
    'offer_expired':        '⏳',
    'offer_withdrawn':      '↩️',
    'listing_expired':      '⏰',
    'listing_removed':      '🚫',
    'listing_sold':         '🏷️',
    'listing_reserved':     '📌',
    'delivery_request':     '🚛',
    'delivery_quote_ready': '💵',
    'delivery_status':      '🚛',
    'delivery_accepted':    '✅',
    'delivery_declined':    '❌',
    'admin_notice':         '⚙️',
}

_NOTIF_CATEGORIES = {
    'messages': ['new_message'],
    'offers':   ['new_offer', 'offer_accepted', 'offer_declined',
                 'offer_countered', 'offer_expired', 'offer_withdrawn'],
    'listings': ['listing_expired', 'listing_removed',
                 'listing_sold', 'listing_reserved'],
    'delivery': ['delivery_request', 'delivery_quote_ready',
                 'delivery_status', 'delivery_accepted', 'delivery_declined'],
    'account':  ['admin_notice'],
}


@app.route("/notifications")
@require_login
def notifications_page():
    from models import Notification
    filter_key = request.args.get('filter', 'all').lower()
    query = Notification.query.filter_by(user_id=str(current_user.id))
    if filter_key == 'unread':
        query = query.filter_by(is_read=False)
    elif filter_key in _NOTIF_CATEGORIES:
        query = query.filter(Notification.type.in_(_NOTIF_CATEGORIES[filter_key]))
    notifications = query.order_by(Notification.created_at.desc()).limit(100).all()
    unread_count = Notification.query.filter_by(
        user_id=str(current_user.id), is_read=False).count()
    return render_template('notifications.html',
                           notifications=notifications,
                           notif_icons=_NOTIF_ICONS,
                           filter_key=filter_key,
                           unread_count=unread_count)


@app.route("/notifications/mark-all-read", methods=["POST"])
@require_login
def notifications_mark_all_read():
    from notification_service import mark_all_read as _mark_all
    _mark_all(current_user.id)
    return redirect(url_for('notifications_page'))


@app.route("/notifications/<int:notif_id>/open")
@require_login
def notification_open(notif_id):
    """Mark one notification as read and redirect to its action_url."""
    from models import Notification
    n = Notification.query.filter_by(
        id=notif_id, user_id=str(current_user.id)
    ).first_or_404()
    if not n.is_read:
        n.is_read = True
        n.read_at = datetime.now()
        db.session.commit()
    if n.action_url:
        return redirect(n.action_url)
    return redirect(url_for('notifications_page'))


@app.route("/api/zip_lookup")
def api_zip_lookup():
    """Return lat/lon/city/state for a given ZIP code, or {found: false} if not in the database."""
    from models import ZipCode as _ZC
    zip_code = request.args.get('zip', '').strip()[:10]
    if not zip_code:
        return jsonify({'found': False})
    zc = _ZC.query.get(zip_code)
    if zc:
        return jsonify({'found': True, 'lat': zc.lat, 'lon': zc.lon,
                        'city': zc.city or '', 'state': zc.state or ''})
    return jsonify({'found': False})


@app.route("/api/search-suggestions")
def api_search_suggestions():
    """Return typeahead suggestions for the marketplace search box."""
    from models import Listing, Category
    q = request.args.get('q', '').strip()
    if not q or len(q) < 2:
        return jsonify(suggestions=[])

    ql = q.lower()
    suggestions = []
    seen_text = set()

    def _add(text, stype, url):
        key = text.lower()
        if key not in seen_text and len(suggestions) < 8:
            seen_text.add(key)
            suggestions.append({'text': text, 'type': stype, 'url': url})

    # ── 1. Synonym / keyword map ─────────────────────────────────────
    # Each entry: (set_of_trigger_words_or_prefixes, [(label, type, url), ...])
    # A suggestion fires when any trigger word starts with ql OR ql starts with it (min 2 chars).
    _SYNONYM_MAP = [
        ({'car','cars','auto','autos','automobile','automobiles','vehicle','vehicles'}, [
            ('Cars & Trucks',       'category', '/marketplace?category=vehicles'),
            ('Vehicles',            'category', '/marketplace?category=vehicles'),
            ('Auto Parts',          'category', '/marketplace?category=auto-parts'),
        ]),
        ({'truck','trucks','pickup','pickups'}, [
            ('Trucks & Pickups',    'category', '/marketplace?q=truck&category=vehicles'),
            ('Cars & Trucks',       'category', '/marketplace?category=vehicles'),
        ]),
        ({'van','vans','minivan','minivans'}, [
            ('Vans & Minivans',     'category', '/marketplace?q=van&category=vehicles'),
            ('Cars & Trucks',       'category', '/marketplace?category=vehicles'),
        ]),
        ({'suv','crossover','crossovers','4wd','awd'}, [
            ('SUVs & Crossovers',   'category', '/marketplace?q=suv&category=vehicles'),
            ('Cars & Trucks',       'category', '/marketplace?category=vehicles'),
        ]),
        ({'part','parts','autopart','autoparts'}, [
            ('Auto Parts',          'category', '/marketplace?category=auto-parts'),
            ('Cars & Trucks',       'category', '/marketplace?category=vehicles'),
        ]),
        ({'apartment','apartments','apt','studio','studios','flat','flats'}, [
            ('Apartments for Rent', 'housing',  '/marketplace?listing_type=rental&q=apartment'),
            ('Rentals',             'housing',  '/marketplace?listing_type=rental'),
            ('Housing & Real Estate','housing', '/marketplace?category=housing'),
        ]),
        ({'house','houses','home','homes','residential'}, [
            ('Homes for Sale',      'housing',  '/marketplace?listing_type=property_sale'),
            ('Houses for Rent',     'housing',  '/marketplace?listing_type=rental&q=house'),
            ('Housing & Real Estate','housing', '/marketplace?category=housing'),
        ]),
        ({'rent','rental','rentals','renting','lease','leasing','tenant'}, [
            ('Rentals',             'housing',  '/marketplace?listing_type=rental'),
            ('Apartments for Rent', 'housing',  '/marketplace?listing_type=rental&q=apartment'),
            ('Houses for Rent',     'housing',  '/marketplace?listing_type=rental&q=house'),
        ]),
        ({'condo','condos','townhome','townhomes','townhouse','townhouses','duplex'}, [
            ('Condos & Townhomes',  'housing',  '/marketplace?listing_type=property_sale&q=condo'),
            ('Homes for Sale',      'housing',  '/marketplace?listing_type=property_sale'),
        ]),
        ({'property','properties','realestate','realty','forsale','for sale'}, [
            ('Homes for Sale',      'housing',  '/marketplace?listing_type=property_sale'),
            ('Housing & Real Estate','housing', '/marketplace?category=housing'),
            ('Rentals',             'housing',  '/marketplace?listing_type=rental'),
        ]),
        ({'refrigerator','refrigerators','fridge','fridges','freezer','freezers'}, [
            ('Refrigerators',       'category', '/marketplace?q=refrigerator'),
            ('Appliances',          'category', '/marketplace?category=appliances'),
        ]),
        ({'washer','washers','dryer','dryers','laundry'}, [
            ('Washers & Dryers',    'category', '/marketplace?q=washer+dryer'),
            ('Appliances',          'category', '/marketplace?category=appliances'),
        ]),
        ({'stove','stoves','oven','ovens','range','ranges','microwave','microwaves','dishwasher','dishwashers'}, [
            ('Kitchen Appliances',  'category', '/marketplace?q=kitchen+appliances'),
            ('Appliances',          'category', '/marketplace?category=appliances'),
        ]),
        ({'appliance','appliances'}, [
            ('Appliances',          'category', '/marketplace?category=appliances'),
            ('Refrigerators',       'category', '/marketplace?q=refrigerator'),
            ('Washers & Dryers',    'category', '/marketplace?q=washer+dryer'),
        ]),
        ({'couch','couches','sofa','sofas','loveseat','loveseats','sectional','sectionals'}, [
            ('Sofas & Couches',     'category', '/marketplace?q=couch+sofa'),
            ('Furniture',           'category', '/marketplace?category=furniture'),
        ]),
        ({'furniture','chair','chairs','table','tables','desk','desks','dresser','dressers','bed','beds','mattress'}, [
            ('Furniture',           'category', '/marketplace?category=furniture'),
            ('Sofas & Couches',     'category', '/marketplace?q=couch+sofa'),
        ]),
        ({'tv','television','televisions','monitor','monitors','screen'}, [
            ('TVs & Monitors',      'category', '/marketplace?q=tv+television'),
            ('Electronics',         'category', '/marketplace?category=electronics'),
        ]),
        ({'phone','phones','iphone','android','smartphone','smartphones','tablet','tablets','laptop','laptops','computer','computers'}, [
            ('Electronics',         'category', '/marketplace?category=electronics'),
            ('Phones & Tablets',    'category', '/marketplace?q=phone+tablet'),
        ]),
        ({'electronic','electronics','gaming','game','games','console','consoles'}, [
            ('Electronics',         'category', '/marketplace?category=electronics'),
        ]),
        ({'drill','drills','saw','saws','hammer','hammers','wrench','wrenches','screwdriver'}, [
            ('Power Tools',         'category', '/marketplace?q=power+tools'),
            ('Tools',               'category', '/marketplace?category=tools'),
        ]),
        ({'tool','tools','toolbox','toolboxes','equipment'}, [
            ('Tools',               'category', '/marketplace?category=tools'),
            ('Power Tools',         'category', '/marketplace?q=power+tools'),
        ]),
        ({'clothing','clothes','shirt','shirts','pants','jacket','jackets','shoes','boots','dress','dresses','coat','coats'}, [
            ('Clothing & Accessories','category','/marketplace?category=clothing'),
        ]),
        ({'sport','sports','outdoor','outdoors','bicycle','bicycles','bike','bikes','gym','fitness','exercise'}, [
            ('Sports & Outdoors',   'category', '/marketplace?category=sports-outdoors'),
        ]),
        ({'kid','kids','baby','babies','toy','toys','stroller','strollers','crib','cribs'}, [
            ('Kids & Baby',         'category', '/marketplace?category=kids-baby'),
        ]),
        ({'garden','gardening','lawn','lawnmower','patio','outdoor furniture'}, [
            ('Home & Garden',       'category', '/marketplace?category=home-garden'),
        ]),
        ({'restaurant','commercial kitchen','industrial'}, [
            ('Restaurant Equipment','category', '/marketplace?category=restaurant-equipment'),
        ]),
        ({'business','office','commercial'}, [
            ('Business Equipment',  'category', '/marketplace?category=business-equipment'),
        ]),
        ({'free','freebie','freebies','giveaway'}, [
            ('Free Items',          'category', '/marketplace?category=free-items'),
        ]),
        ({'collectible','collectibles','antique','antiques','vintage','comic','coins'}, [
            ('Collectibles',        'category', '/marketplace?category=collectibles'),
        ]),
    ]

    for triggers, labels in _SYNONYM_MAP:
        matched = any(
            t.startswith(ql) or ql.startswith(t)
            for t in triggers
            if len(t) >= 2
        )
        if matched:
            for label, stype, url in labels:
                _add(label, stype, url)
            if len(suggestions) >= 5:
                break

    # ── 2. Vehicle makes (with common nicknames/aliases) ────────────
    _MAKES = ['Acura','Audi','BMW','Buick','Cadillac','Chevrolet','Chrysler',
              'Dodge','Ford','GMC','Honda','Hyundai','Infiniti','Jeep','Kia',
              'Lexus','Lincoln','Mazda','Mercedes-Benz','Mitsubishi','Nissan',
              'Pontiac','RAM','Subaru','Tesla','Toyota','Volkswagen','Volvo']
    _MAKE_ALIASES = {
        'chevy':'Chevrolet','chev':'Chevrolet',
        'vw':'Volkswagen','merc':'Mercedes-Benz','benz':'Mercedes-Benz',
        'mercedesbenz':'Mercedes-Benz','mercedes':'Mercedes-Benz',
        'beemer':'BMW','beamer':'BMW',
        'mopar':'Dodge','ram':'RAM',
        'caddy':'Cadillac','lincoln':'Lincoln',
    }
    # Check aliases first
    for alias, make in _MAKE_ALIASES.items():
        if alias.startswith(ql) or ql.startswith(alias):
            _add(make + ' vehicles', 'vehicle',
                 f'/marketplace?category=vehicles&q={make}')

    # Standard make prefix match
    for make in _MAKES:
        if make.lower().startswith(ql):
            _add(make + ' vehicles', 'vehicle',
                 f'/marketplace?category=vehicles&q={make}')

    # Multi-word query: try "make model" pattern (e.g. "honda accord")
    _parts = ql.split()
    if len(_parts) >= 2:
        _first = _parts[0]
        _rest  = ' '.join(_parts[1:])
        # Resolve alias for first word
        _resolved_make = _MAKE_ALIASES.get(_first)
        if not _resolved_make:
            for make in _MAKES:
                if make.lower().startswith(_first):
                    _resolved_make = make
                    break
        if _resolved_make:
            _add(f'{_resolved_make} {_rest.title()} vehicles', 'vehicle',
                 f'/marketplace?category=vehicles&q={_resolved_make}+{_rest}')
            _add(f'{_resolved_make} vehicles', 'vehicle',
                 f'/marketplace?category=vehicles&q={_resolved_make}')

    # ── 3. Matching DB categories (name contains query) ──────────────
    if len(suggestions) < 5:
        cats = Category.query.filter(
            Category.name.ilike(f'%{q}%'),
            Category.is_active == True
        ).order_by(Category.parent_id.asc().nullsfirst()).limit(4).all()
        for c in cats:
            _add(c.name, 'category',
                 f'/marketplace?category={c.slug}' if c.parent_id is None
                 else f'/marketplace?category={c.parent.slug if c.parent else c.slug}')

    # ── 4. Vehicle make+model combos from live listings ──────────────
    if len(suggestions) < 6:
        vm_rows = (Listing.query
            .with_entities(Listing.vehicle_make, Listing.vehicle_model)
            .filter(
                Listing.status == 'active',
                Listing.moderation_status == 'approved',
                Listing.vehicle_make.isnot(None),
                db.or_(
                    Listing.vehicle_make.ilike(f'{q}%'),
                    Listing.vehicle_model.ilike(f'%{q}%'),
                )
            )
            .distinct().limit(5).all())
        for make, model in vm_rows:
            if make and model:
                _add(f'{make} {model}', 'vehicle',
                     f'/marketplace?category=vehicles&q={make}+{model}')
            elif make:
                _add(make, 'vehicle',
                     f'/marketplace?category=vehicles&q={make}')

    # ── 5. Sellers whose display name matches the query ─────────────
    if len(suggestions) < 7:
        from models import User as _User
        # Only surface sellers who have at least one active, approved listing
        seller_sub = (db.session.query(Listing.seller_id)
            .filter(
                Listing.status == 'active',
                Listing.moderation_status == 'approved',
            )
            .distinct()
            .subquery())
        sellers = (_User.query
            .filter(
                _User.id.in_(seller_sub),
                _User.is_banned == False,
                db.or_(
                    _User.first_name.ilike(f'%{q}%'),
                    _User.last_name.ilike(f'%{q}%'),
                )
            )
            .limit(3).all())
        for seller in sellers:
            first = (seller.first_name or '').strip()
            last  = (seller.last_name  or '').strip()
            # Use "First L." public display convention — never expose full last name
            if first and last:
                name = f'{first} {last[0]}.'
            elif first:
                name = first
            elif last:
                name = f'{last[0]}.'
            else:
                continue  # skip users with no name at all
            _add(name, 'seller', f'/seller/{seller.id}')

    # ── 6. Active listing titles that contain the query ──────────────
    if len(suggestions) < 8:
        listings = (Listing.query
            .filter(
                Listing.status == 'active',
                Listing.moderation_status == 'approved',
                Listing.title.ilike(f'%{q}%')
            )
            .order_by(Listing.created_at.desc())
            .limit(4).all())
        for l in listings:
            _add(l.title, 'listing', f'/listing/{l.id}')

    # ── 7. Always end with "Search 'X' in all Marketplace" ──────────
    _add(f'Search "{q}" in all Marketplace', 'search_all',
         f'/marketplace?q={q}')

    return jsonify(suggestions=suggestions[:8])


@app.route("/api/ai/search-parse", methods=["POST"])
def api_ai_search_parse():
    """Convert a natural-language buyer query into validated marketplace filters.
    Rate-limited per IP (30/hr). Results cached 5 min per unique query.
    No auth required — open to all visitors. No PII or private data sent to AI.
    Prompt-injection guard: buyer text is isolated in the 'user' role only.
    """
    from models import AIUsageLog
    import time as _time

    data  = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()[:300]
    if not query:
        return jsonify({"ok": False, "error": "empty"}), 400

    ip = request.remote_addr or ""
    t0 = _time.time()
    result = _ai_search_parse(query, ip=ip)
    response_ms = result.get("response_ms") or int((_time.time() - t0) * 1000)

    # Resolve category name → slug for the client to use in the URL
    if result.get("ok") and result.get("filters", {}).get("category"):
        _cat_name = result["filters"]["category"]
        try:
            _cat = Category.query.filter(
                Category.name.ilike(_cat_name),
                Category.is_active == True,
                Category.parent_id.is_(None),
            ).first()
            if not _cat:
                # Fuzzy fallback
                _all_cats = Category.query.filter_by(is_active=True, parent_id=None).all()
                _cn_lower = _cat_name.lower()
                for _c in _all_cats:
                    if _cn_lower in _c.name.lower() or _c.name.lower() in _cn_lower:
                        _cat = _c
                        break
            if _cat:
                result["filters"]["category_slug"] = _cat.slug
        except Exception:
            pass

    # Log (aggregate only — no query text stored)
    if not result.get("cached"):
        try:
            _uid = current_user.id if current_user.is_authenticated else None
            db.session.add(AIUsageLog(
                user_id=_uid,
                tool_name="search_parse",
                success=bool(result.get("ok")),
                response_ms=response_ms,
            ))
            db.session.commit()
        except Exception as _le:
            db.session.rollback()
            app.logger.warning("AIUsageLog (search) write failed: %s", _le)

    return jsonify(result)


@app.route("/api/ai/listing-assist", methods=["POST"])
def api_ai_listing_assist():
    """AI listing assistant — title, description, category, and quality tips
    in one GPT-4o-mini call.  Rate-limited to 10 requests/seller/hour.
    No PII is sent to the AI provider.
    """
    if not current_user.is_authenticated:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    from models import AIUsageLog
    import time as _time

    data = request.get_json(silent=True) or {}
    listing_id = data.get("listing_id")
    if not listing_id:
        return jsonify({"ok": False, "error": "missing_listing_id"}), 400

    listing = Listing.query.get(listing_id)
    if not listing or listing.seller_id != current_user.id:
        return jsonify({"ok": False, "error": "unauthorized"}), 403

    # ── Rate limit: 10 AI calls per seller per hour ───────────────────────────
    from datetime import datetime as _dt, timedelta as _td
    one_hour_ago = _dt.now() - _td(hours=1)
    recent_count = AIUsageLog.query.filter(
        AIUsageLog.user_id == current_user.id,
        AIUsageLog.created_at >= one_hour_ago,
    ).count()
    if recent_count >= 10:
        return jsonify({"ok": False, "error": "rate_limited"}), 429

    # ── Collect safe listing data — no PII ───────────────────────────────────
    category_name = ""
    if listing.category_id:
        cat = Category.query.get(listing.category_id)
        if cat:
            category_name = cat.name

    listing_data = {
        "listing_type":         listing.listing_type or "item",
        "category":             category_name,
        "title":                (data.get("title") or listing.title or "")[:400],
        "description":          (data.get("description") or listing.description or "")[:800],
        "condition":            data.get("condition") or listing.condition or "",
        "vehicle_year":         listing.vehicle_year,
        "vehicle_make":         listing.vehicle_make,
        "vehicle_model":        listing.vehicle_model,
        "vehicle_mileage":      listing.vehicle_mileage,
        "vehicle_trim":         listing.vehicle_trim,
        "vehicle_fuel_type":    listing.vehicle_fuel_type,
        "vehicle_transmission": listing.vehicle_transmission,
        "photo_count":          len(listing.photos),
        "has_price":            bool(listing.price),
        "has_location":         bool(listing.city),
    }

    t0 = _time.time()
    result = _ai_suggest(listing_data)
    response_ms = result.get("response_ms") or int((_time.time() - t0) * 1000)

    # ── Log the attempt (no sensitive content) ───────────────────────────────
    try:
        log_entry = AIUsageLog(
            user_id=current_user.id,
            tool_name="listing_assist",
            success=bool(result.get("ok")),
            response_ms=response_ms,
        )
        db.session.add(log_entry)
        db.session.commit()
    except Exception as _le:
        db.session.rollback()
        app.logger.warning("AIUsageLog write failed: %s", _le)

    return jsonify(result)


@app.route("/api/notifications/count")
@require_login
def api_notification_count():
    """JSON endpoint for badge polling. Returns {count: N}.

    Uses jsonify so the response always carries Content-Type: application/json,
    which satisfies strict fetch() consumers and avoids browser sniffing.
    """
    from notification_service import get_unread_count as _gc
    return jsonify({'count': _gc(current_user.id)})


@app.route("/admin/send-notice/<user_id>", methods=["POST"])
@require_admin
def admin_send_notice(user_id):
    """Send an admin notice notification to any user."""
    from models import User as _U
    target = _U.query.get_or_404(user_id)
    title   = (request.form.get('notice_title',   '') or '').strip()
    message = (request.form.get('notice_message', '') or '').strip() or None
    action  = (request.form.get('notice_url',     '') or '').strip() or None
    if not title:
        flash("Notice title is required.", "error")
        return redirect(request.referrer or url_for('admin_dashboard'))
    from notification_service import notify_admin_notice as _nan
    _nan(user_id, title, message, action)
    flash(f"Notice sent to {target.first_name or target.email}.", "success")
    return redirect(request.referrer or url_for('admin_dashboard'))


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
                # Check first-message condition BEFORE adding the new message
                _is_buyer_sender = not is_seller_view
                _existing_count = len(convo.messages) if _is_buyer_sender else 1
                msg = ListingMessage(
                    conversation_id=convo.id,
                    sender_id=current_user.id,
                    body=body[:2000],
                )
                db.session.add(msg)
                convo.updated_at = datetime.now()
                db.session.commit()
                # Email seller on buyer's first message in this conversation
                if _is_buyer_sender and _existing_count == 0:
                    try:
                        seller = User.query.get(convo.seller_id)
                        if seller and seller.email:
                            buyer_name = (current_user.first_name or
                                          current_user.email or 'A buyer')
                            # Phase F: queue the email; fall back to sync if queue unavailable
                            if not _queue_email(
                                'notify_seller_new_message',
                                seller_email=seller.email,
                                listing_title=listing.title,
                                listing_id=listing_id,
                                buyer_name=buyer_name,
                                conversation_id=convo.id,
                            ):
                                notify_seller_new_message(
                                    seller.email, listing.title, listing_id,
                                    buyer_name, convo.id)
                    except Exception:
                        pass
                # In-app notification → the other participant
                try:
                    from notification_service import notify_new_message as _nnm
                    recipient_id = (convo.seller_id
                                    if str(current_user.id) == str(convo.buyer_id)
                                    else convo.buyer_id)
                    sender_name = current_user.first_name or current_user.email or 'Someone'
                    _nnm(recipient_id=recipient_id,
                         sender_name=sender_name,
                         listing_title=listing.title,
                         listing_id=listing_id,
                         conversation_id=convo.id)
                except Exception:
                    pass
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
            # Check first-message condition BEFORE adding the new message
            _is_first_buyer_msg = len(convo.messages) == 0
            msg = ListingMessage(
                conversation_id=convo.id,
                sender_id=current_user.id,
                body=body[:2000],
            )
            db.session.add(msg)
            convo.updated_at = datetime.now()
            db.session.commit()
            # Email seller on buyer's first message in this conversation
            if _is_first_buyer_msg:
                try:
                    seller = User.query.get(listing.seller_id)
                    if seller and seller.email:
                        buyer_name = (current_user.first_name or
                                      current_user.email or 'A buyer')
                        # Phase F: queue the email; fall back to sync if queue unavailable
                        if not _queue_email(
                            'notify_seller_new_message',
                            seller_email=seller.email,
                            listing_title=listing.title,
                            listing_id=listing_id,
                            buyer_name=buyer_name,
                            conversation_id=convo.id,
                        ):
                            notify_seller_new_message(
                                seller.email, listing.title, listing_id,
                                buyer_name, convo.id)
                except Exception:
                    pass
        else:
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

    is_public = (listing.status in ('active', 'sold', 'reserved', 'pending') and listing.moderation_status == 'approved')
    is_owner = current_user.is_authenticated and current_user.id == listing.seller_id
    is_admin = current_user.is_authenticated and current_user.is_admin
    if not (is_public or is_owner or is_admin):
        return "", 404

    # On-the-fly HEIC/HEIF → JPEG conversion for legacy stored photos.
    # Also persists the converted bytes back to the DB row so subsequent
    # requests are served directly as JPEG without re-converting.
    if photo.content_type in ('image/heic', 'image/heif'):
        _heif_available = False
        try:
            import pillow_heif as _ph2
            _ph2.register_heif_opener()
            _heif_available = True
        except ImportError:
            app.logger.error(
                "serve_listing_photo: pillow-heif is not installed — cannot convert "
                "ListingPhoto #%s from HEIC/HEIF to JPEG. Add 'pillow-heif' to "
                "requirements.txt to fix broken images.",
                photo_id,
            )
        if _heif_available:
            try:
                from PIL import Image as _PILImg2
                import io as _io2
                _img2 = _PILImg2.open(_io2.BytesIO(photo.data))
                _buf2 = _io2.BytesIO()
                _img2.convert('RGB').save(_buf2, format='JPEG', quality=90)
                _jpg2 = _buf2.getvalue()
                if _jpg2:
                    photo.data = _jpg2
                    photo.content_type = 'image/jpeg'
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
            except Exception as _conv_err2:
                app.logger.warning(
                    "serve_listing_photo: HEIC→JPEG conversion failed for photo %s: %s",
                    photo_id, _conv_err2
                )

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
        # Admin check must come first — admins may have a null user_type which would
        # otherwise fall into the choose_role redirect, creating a silent dead-end.
        if current_user.is_admin:
            return render_template('invite_landing.html', role=role, authenticated_non_customer=True,
                                   viewer_type='admin')
        if current_user.user_type == 'customer':
            # Actual customers go straight to the request form
            return redirect(url_for('customer_request'))
        if not current_user.user_type:
            return redirect(url_for('choose_role'))
        # Sellers and haulers see the landing page with a helpful explanation
        return render_template('invite_landing.html', role=role, authenticated_non_customer=True,
                               viewer_type=current_user.user_type)
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
    # Render the welcome screen — role is assigned when they click "Get Started"
    return render_template("choose_role.html")

@app.route("/set-role", methods=["POST"])
@require_login
def set_role():
    if current_user.is_admin:
        return redirect(url_for('admin_dashboard'))
    # Only assign the role the first time
    is_new = not current_user.user_type
    current_user.user_type = 'customer'
    db.session.commit()
    if is_new:
        session['new_member'] = True
        try:
            _name = f"{current_user.first_name or ''} {current_user.last_name or ''}".strip() or current_user.email
            notify_admin_new_customer(_name, current_user.email)
            notify_admin_new_customer_sms(_name, current_user.email)
        except Exception as e:
            app.logger.error("Admin notify failed (new customer): %s", e)
    return redirect(url_for('marketplace'))


@app.route("/hauler/apply", methods=["GET", "POST"])
@require_login
def hauler_apply():
    """Let a logged-in customer apply to become a marketplace hauler."""
    if current_user.user_type == 'hauler':
        return redirect(url_for('hauler_dashboard'))
    if request.method == 'POST':
        current_user.user_type = 'hauler'
        current_user.hauler_status = 'pending'
        db.session.commit()
        # Notify admins of the new application
        try:
            from notification_service import create_notification as _cn
            _name = f"{current_user.first_name or ''} {current_user.last_name or ''}".strip() or current_user.email
            for _adm in User.query.filter_by(is_admin=True).all():
                _cn(user_id=_adm.id, type='hauler_application',
                    title='New Hauler Application',
                    message=f'{_name} applied to be a marketplace hauler.',
                    action_url='/admin/haulers')
        except Exception:
            pass
        flash("Application submitted! We'll review it and notify you when approved.", "success")
        return redirect(url_for('hauler_dashboard'))
    return render_template('hauler_apply.html')


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
    withdrawal_note = request.form.get('withdrawal_note', '').strip() or None
    quote.status = 'withdrawn'
    quote.withdrawal_note = withdrawal_note
    # Revert job status to 'reviewing' if no other pending quote remains
    other_pending = Quote.query.filter(
        Quote.job_id == job.id,
        Quote.id != quote.id,
        Quote.status == 'pending'
    ).first()
    if not other_pending and job.status == 'quoted':
        job.status = 'reviewing'
    db.session.commit()

    # Notify the customer
    customer = User.query.get(job.customer_id)
    if customer:
        try:
            if customer.email:
                notify_customer_quote_withdrawn(
                    customer.email, job.id, job.service_type or 'Service Request',
                    withdrawal_note=withdrawal_note
                )
        except Exception as e:
            app.logger.warning("Failed to send quote-withdrawn email: %s", e)
        try:
            if customer.notify_sms and customer.sms_consent and customer.phone:
                notify_customer_quote_withdrawn_sms(
                    customer.phone, job.id, job.service_type or 'Service Request',
                    withdrawal_note=withdrawal_note
                )
        except Exception as e:
            app.logger.warning("Failed to send quote-withdrawn SMS: %s", e)

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
    'quoted':          ('💵', 'Quoted',           '#8b5cf6'),
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
    is_public = listing.status in ('active', 'sold', 'reserved', 'pending') and listing.moderation_status == 'approved'
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

    # In-app notification → all admin users
    try:
        from notification_service import notify_delivery_request as _ndr
        listing_title = listing.title if listing else 'marketplace item'
        _buyer_name = buyer_name
        admin_users = User.query.filter_by(is_admin=True).all()
        for _admin in admin_users:
            _ndr(admin_user_id=_admin.id,
                 buyer_name=_buyer_name,
                 listing_title=listing_title,
                 dr_id=dr.id)
    except Exception:
        pass

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
        status_meta=_DELIVERY_STATUS_META,
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

    # SMS disabled for marketplace — in-app notifications used instead

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

    # SMS disabled for marketplace — in-app notifications used instead

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

    # In-app notification → buyer on any status change (all statuses)
    try:
        from notification_service import notify_delivery_status as _nds
        from models import Listing as _DL
        _dl = _DL.query.get(dr.listing_id) if dr.listing_id else None
        _item_title = _dl.title if _dl else 'your item'
        _status_label = new_status.replace('_', ' ').title()
        _nds(buyer_id=dr.buyer_id,
             status_label=_status_label,
             listing_title=_item_title,
             dr_id=dr_id)
        # Separate "quote ready" notification for the buyer when admin marks quoted
        if new_status == 'quoted' and is_admin:
            from notification_service import notify_delivery_quote_ready as _ndqr
            _ndqr(buyer_id=dr.buyer_id,
                  listing_title=_item_title,
                  dr_id=dr_id,
                  amount=dr.quote_amount)
    except Exception:
        pass
    # Email buyer when a delivery quote is ready
    if new_status == 'quoted' and is_admin:
        try:
            buyer = User.query.get(dr.buyer_id)
            if buyer and buyer.email:
                from models import Listing as _QL
                _ql = _QL.query.get(dr.listing_id) if dr.listing_id else None
                _qtitle = _ql.title if _ql else 'your item'
                notify_buyer_delivery_quote_ready(
                    buyer.email, _qtitle, dr_id, dr.quote_amount)
        except Exception:
            pass

    _buyer_msgs = {
        'scheduled':  "Your delivery is scheduled.",
        'picked_up':  "Your item has been picked up and is on its way!",
        'in_transit': "Your item is in transit.",
        'delivered':  "Your item has been delivered! Please confirm receipt.",
        'cancelled':  "Your delivery request has been cancelled.",
    }
    # SMS disabled for marketplace — in-app notifications used instead

    flash(f"Status updated to: {new_status.replace('_', ' ').title()}", "success")
    return redirect(url_for('delivery_detail', dr_id=dr_id))


@app.route("/request-delivery", methods=["GET", "POST"])
@require_login
def standalone_request_delivery():
    """Standalone delivery request — not tied to a listing."""
    prefill_name  = ((current_user.first_name or '') + ' ' + (current_user.last_name or '')).strip() or ''
    prefill_phone = current_user.phone or ''

    if request.method == 'GET':
        return render_template('standalone_delivery_request_form.html',
                               prefill_name=prefill_name,
                               prefill_phone=prefill_phone)

    # ── POST: parse form ──────────────────────────────────────────────────────
    contact_name    = request.form.get('contact_name', '').strip() or prefill_name
    contact_phone   = request.form.get('contact_phone', '').strip() or prefill_phone
    item_description = request.form.get('item_description', '').strip()
    approx_dimensions = request.form.get('approx_dimensions', '').strip() or None
    approx_weight   = request.form.get('approx_weight', '').strip() or None
    item_count      = max(1, int(request.form.get('item_count', 1) or 1))
    pickup_address  = request.form.get('pickup_address', '').strip()
    pickup_city     = request.form.get('pickup_city', '').strip()
    pickup_state    = request.form.get('pickup_state', 'MN').strip()
    pickup_zip      = request.form.get('pickup_zip', '').strip()
    delivery_address = request.form.get('delivery_address', '').strip()
    delivery_city   = request.form.get('delivery_city', '').strip()
    delivery_state  = request.form.get('delivery_state', 'MN').strip()
    delivery_zip    = request.form.get('delivery_zip', '').strip()
    pickup_stairs   = request.form.get('pickup_stairs') == '1'
    delivery_stairs = request.form.get('delivery_stairs') == '1'
    need_loading    = request.form.get('need_loading') == '1'
    need_unloading  = request.form.get('need_unloading') == '1'
    preferred_date  = request.form.get('preferred_date', '').strip() or None
    preferred_time  = request.form.get('preferred_time', '').strip() or None
    special_instructions = request.form.get('special_instructions', '').strip() or None

    if not item_description:
        flash("Please describe what needs to be delivered.", "error")
        return render_template('standalone_delivery_request_form.html',
                               prefill_name=prefill_name, prefill_phone=prefill_phone)
    if not pickup_zip or not delivery_zip:
        flash("Pickup and drop-off ZIP codes are required.", "error")
        return render_template('standalone_delivery_request_form.html',
                               prefill_name=prefill_name, prefill_phone=prefill_phone)

    # Build public job description (no street addresses)
    extras = []
    if approx_dimensions: extras.append(f"Size: {approx_dimensions}")
    if approx_weight:     extras.append(f"Weight: {approx_weight}")
    extras.append(f"{item_count} item(s)")
    if pickup_stairs:   extras.append("stairs at pickup")
    if delivery_stairs: extras.append("stairs at delivery")
    if need_loading:    extras.append("loading help needed")
    if need_unloading:  extras.append("unloading help needed")
    job_desc = item_description + (" | " + " | ".join(extras) if extras else "")
    if special_instructions:
        job_desc += f"\nNotes: {special_instructions}"

    # Combine size + weight for the dimensions field
    dim_parts = [p for p in [approx_dimensions, (f"~{approx_weight}" if approx_weight else None)] if p]
    combined_dimensions = " / ".join(dim_parts) if dim_parts else None

    # Create Job so the request surfaces in admin/hauler queues
    job = Job(
        customer_id=current_user.id,
        customer_name=contact_name,
        customer_phone=contact_phone,
        pickup_address=f"{pickup_city}, {pickup_state} {pickup_zip}".strip(', '),
        pickup_zip=pickup_zip,
        preferred_date=preferred_date,
        preferred_time=preferred_time,
        job_description=job_desc,
        service_type='standalone_delivery',
        status='open',
    )
    db.session.add(job)
    db.session.flush()

    dr = DeliveryRequest(
        listing_id=None,
        buyer_id=current_user.id,
        seller_id=None,
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
        item_description=item_description,
        approx_dimensions=combined_dimensions,
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

    # Notify admin
    try:
        notify_admin_new_request_sms(job.id, contact_name, 'standalone_delivery', pickup_zip)
    except Exception as _e:
        app.logger.error("Standalone delivery admin notify failed: %s", _e)

    try:
        from notification_service import notify_delivery_request as _ndr
        admin_users = User.query.filter_by(is_admin=True).all()
        for _admin in admin_users:
            _ndr(admin_user_id=_admin.id,
                 buyer_name=contact_name,
                 listing_title=item_description,
                 dr_id=dr.id)
    except Exception:
        pass

    flash("Delivery request submitted! JHE Haul will review your request and send you a quote.", "success")
    return redirect(url_for('delivery_detail', dr_id=dr.id))


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
    _hs = getattr(current_user, 'hauler_status', None)
    if _hs != 'approved':
        if _hs == 'pending':
            flash("Your hauler application is under review. You'll be notified when approved.", "info")
        elif _hs == 'suspended':
            flash("Your hauler access is currently suspended. Contact support for assistance.", "error")
        elif _hs == 'rejected':
            flash("Your hauler application was not approved.", "error")
        else:
            flash("Hauler access requires admin approval.", "info")
        return redirect(url_for('hauler_dashboard'))

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
@require_login
def hauler_dashboard():
    if current_user.user_type != 'hauler':
        return redirect(url_for('home'))
    _hs = getattr(current_user, 'hauler_status', None)

    # Deliveries this hauler has been selected for (via accepted bid)
    my_bids = Bid.query.filter_by(hauler_id=current_user.id, status='accepted').all()
    my_job_ids = [b.job_id for b in my_bids]

    active_drs = []
    completed_drs = []
    if my_job_ids:
        _all_my = (DeliveryRequest.query
                   .filter(DeliveryRequest.job_id.in_(my_job_ids))
                   .order_by(DeliveryRequest.created_at.desc()).all())
        for _dr in _all_my:
            if _dr.status in ('delivered', 'cancelled'):
                completed_drs.append(_dr)
            else:
                active_drs.append(_dr)

    available_count = 0
    if _hs == 'approved':
        available_count = (DeliveryRequest.query
                           .filter(DeliveryRequest.status.in_(('requested', 'offers_received')))
                           .count())

    return render_template('hauler_dashboard.html',
                           hauler_status=_hs,
                           active_drs=active_drs,
                           completed_drs=completed_drs,
                           available_count=available_count,
                           status_meta=_DELIVERY_STATUS_META)

@app.route("/account")
@require_login
def account():
    return redirect(url_for('profile'))


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
                           invite_url=invite_url,
                           is_primary_admin=_is_primary_admin(current_user))

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

    db.session.commit()
    flash("Profile updated successfully!", "success")
    return redirect(url_for('profile'))


@app.route("/account/notification-prefs", methods=["POST"])
@require_login
def profile_notification_prefs():
    """Save notification preference toggles from the profile page."""
    current_user.notify_listing_status_changes = bool(request.form.get("notify_listing_status_changes"))
    db.session.commit()
    flash("Notification preferences saved.", "success")
    return redirect(url_for('profile') + '#notification-prefs')


def _is_primary_admin(user):
    """Return True if user is the designated primary admin (is_admin + configured ADMIN_EMAIL)."""
    primary_email = os.environ.get("ADMIN_EMAIL", "jhehaul@gmail.com")
    return bool(user and user.is_admin and user.email and
                user.email.strip().lower() == primary_email.strip().lower())


@app.route("/account/delete", methods=["POST"])
@require_login
def delete_account():
    from models import (OAuth, Bid, Review, CompletionPhoto,
                        Listing, ListingFavorite, ListingConversation,
                        ListingMessage, ListingOffer, DeliveryRequest,
                        UserBlock, ListingReport)
    user_id   = current_user.id
    user_type = current_user.user_type

    # ── Primary Admin and last-admin protection ──────────────────────────────
    if current_user.is_admin:
        if _is_primary_admin(current_user):
            flash("The primary admin account cannot be deleted.", "error")
            return redirect(url_for('profile'))
        # Block deletion if this is the last active admin account
        remaining_admins = User.query.filter_by(is_admin=True).count()
        if remaining_admins <= 1:
            flash("Cannot delete the last active admin account.", "error")
            return redirect(url_for('profile'))

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
    from models import GalleryPhoto as _GalleryPhoto
    for listing in Listing.query.filter_by(seller_id=user_id).all():
        # Remove any gallery pins for this listing before deleting it
        _GalleryPhoto.query.filter_by(item_type='listing', listing_id=listing.id).delete(synchronize_session=False)
        db.session.delete(listing)
    db.session.flush()  # run cascades before buyer-side deletes

    # Buyer-side records on other sellers' listings
    ListingFavorite.query.filter_by(user_id=user_id).delete(synchronize_session=False)
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
    from models import SmsLog as _SmsLog, NotificationLog as _NotificationLog

    # Marketplace stats
    total_users = User.query.count()
    active_listings = Listing.query.filter_by(status='active').count()
    pending_listings = Listing.query.filter(
        db.or_(Listing.status == 'pending', Listing.moderation_status == 'pending')
    ).count()
    sold_items = Listing.query.filter_by(status='sold').count()
    reported_listings = ListingReport.query.filter_by(status='pending').count()
    total_listings = Listing.query.count()
    draft_listings = Listing.query.filter_by(status='draft').count()
    reserved_listings = Listing.query.filter_by(status='reserved').count()
    expired_listings = Listing.query.filter_by(status='expired').count()
    removed_listings = Listing.query.filter_by(status='removed').count()
    homes_for_sale = Listing.query.filter_by(listing_type='property_sale').count()
    rental_listings = Listing.query.filter_by(listing_type='rental').count()
    housing_listings = homes_for_sale + rental_listings

    # Recent activity — exclude drafts so empty/abandoned drafts don't clutter the feed
    recent_listings = (Listing.query
                       .filter(Listing.status != 'draft')
                       .order_by(Listing.created_at.desc()).limit(8).all())
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

    # Deploy health-alert events (email + SMS logs combined, most recent first)
    _email_alerts = (_NotificationLog.query
                     .filter_by(event_type='admin_health_alert')
                     .order_by(_NotificationLog.created_at.desc())
                     .limit(20).all())
    _sms_alerts = (_SmsLog.query
                   .filter_by(event_type='admin_health_alert')
                   .order_by(_SmsLog.created_at.desc())
                   .limit(20).all())
    # Build unified list: dicts with keys: ts, channel, status, detail
    _health_events_raw = []
    for _e in _email_alerts:
        _health_events_raw.append({
            'ts': _e.created_at,
            'channel': 'email',
            'status': _e.status,
            'detail': _e.error_msg or _e.subject or '',
        })
    for _s in _sms_alerts:
        _health_events_raw.append({
            'ts': _s.created_at,
            'channel': 'sms',
            'status': _s.status,
            'detail': _s.error_msg or (_s.message_body[:200] if _s.message_body else ''),
        })
    _health_events_raw.sort(key=lambda x: x['ts'], reverse=True)
    health_alert_events = _health_events_raw[:20]

    return render_template('admin_dashboard.html',
                           total_users=total_users,
                           active_listings=active_listings,
                           pending_listings=pending_listings,
                           sold_items=sold_items,
                           reported_listings=reported_listings,
                           total_listings=total_listings,
                           draft_listings=draft_listings,
                           reserved_listings=reserved_listings,
                           expired_listings=expired_listings,
                           removed_listings=removed_listings,
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
                           spaces_configured=spaces_configured,
                           health_alert_events=health_alert_events)

@app.route("/admin/photo-health")
@require_admin
def admin_photo_health():
    """Run photo URL health checks across all photo tables and render results."""
    import requests as _req
    from requests.adapters import HTTPAdapter as _HTTPAdapter
    from urllib3.util.retry import Retry as _Retry
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from models import JobPhoto, CompletionPhoto, ListingPhoto, GalleryPhoto

    TABLES = [
        ("job_photos", JobPhoto),
        ("completion_photos", CompletionPhoto),
        ("listing_photos", ListingPhoto),
        ("gallery_photos", GalleryPhoto),
    ]

    # ── Gather DB stats ───────────────────────────────────────────────────────
    table_stats = []
    jobs = []           # (table_label, row_id, url)
    no_url_rows = []    # (table_label, row_id)

    for label, model in TABLES:
        total = model.query.count()
        with_url = model.query.filter(model.storage_url.isnot(None)).count()
        without_url = total - with_url

        null_rows = (model.query
                     .filter(model.storage_url.is_(None))
                     .with_entities(model.id)
                     .all())
        for (row_id,) in null_rows:
            no_url_rows.append({"table": label, "id": row_id})

        url_rows = (model.query
                    .filter(model.storage_url.isnot(None))
                    .with_entities(model.id, model.storage_url)
                    .all())
        for row_id, url in url_rows:
            jobs.append((label, row_id, url))

        table_stats.append({
            "label": label,
            "total": total,
            "with_url": with_url,
            "without_url": without_url,
        })

    # ── HTTP checks ───────────────────────────────────────────────────────────
    _session = _req.Session()
    _retry = _Retry(total=2, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    _adapter = _HTTPAdapter(max_retries=_retry)
    _session.mount("https://", _adapter)
    _session.mount("http://", _adapter)

    def _check_url(table, row_id, url):
        try:
            resp = _session.head(url, timeout=8, allow_redirects=True)
            if resp.status_code == 200:
                return {"table": table, "id": row_id, "url": url, "status": "OK", "code": 200}
            return {"table": table, "id": row_id, "url": url, "status": "BROKEN", "code": resp.status_code}
        except Exception as exc:
            return {"table": table, "id": row_id, "url": url, "status": "ERROR", "error": str(exc)[:200]}

    counters = {"ok": 0, "broken": 0, "error": 0, "no_url": len(no_url_rows)}
    broken_rows = []

    if jobs:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_check_url, t, rid, u): (t, rid, u) for t, rid, u in jobs}
            for future in as_completed(futures):
                result = future.result()
                status = result["status"].lower()
                counters[status] = counters.get(status, 0) + 1
                if result["status"] in ("BROKEN", "ERROR"):
                    broken_rows.append(result)

    broken_rows.sort(key=lambda r: (r["table"], r["id"]))

    return render_template(
        "admin_photo_health.html",
        table_stats=table_stats,
        counters=counters,
        broken_rows=broken_rows,
        no_url_rows=no_url_rows,
        total_checked=len(jobs),
    )


@app.route("/admin/jobs")
def admin_jobs():
    """Phase F: Admin background job monitoring dashboard."""
    if not current_user.is_authenticated or not getattr(current_user, 'is_admin', False):
        return jsonify({"error": "forbidden"}), 403

    from models import BackgroundJob
    from sqlalchemy import func
    from worker.queue import stats as _job_stats

    # ── Summary counts ───────────────────────────────────────────────────────
    try:
        counts = _job_stats()
    except Exception:
        counts = {s: 0 for s in ('QUEUED', 'PROCESSING', 'COMPLETED', 'FAILED', 'CANCELLED')}

    # ── Recent failures (last 50 — no secrets, no full payloads) ─────────────
    failed_jobs = (
        BackgroundJob.query
        .filter(BackgroundJob.status.in_(['FAILED', 'QUEUED']))
        .filter(BackgroundJob.error_category.isnot(None))
        .order_by(BackgroundJob.created_at.desc())
        .limit(50)
        .all()
    )

    # ── Average processing time for recently completed jobs ───────────────────
    try:
        from datetime import timedelta as _td
        _recent_done = (
            BackgroundJob.query
            .filter(
                BackgroundJob.status == 'COMPLETED',
                BackgroundJob.started_at.isnot(None),
            )
            .order_by(BackgroundJob.completed_at.desc())
            .limit(200)
            .all()
        )
        if _recent_done:
            durations = [
                (j.completed_at - j.started_at).total_seconds()
                for j in _recent_done
                if j.completed_at and j.started_at
            ]
            avg_ms = int(sum(durations) / len(durations) * 1000) if durations else 0
        else:
            avg_ms = 0
    except Exception:
        avg_ms = 0

    # ── Most recent 200 jobs (all statuses) ───────────────────────────────────
    recent_jobs = (
        BackgroundJob.query
        .order_by(BackgroundJob.created_at.desc())
        .limit(200)
        .all()
    )

    return render_template(
        'admin_jobs.html',
        counts=counts,
        failed_jobs=failed_jobs,
        recent_jobs=recent_jobs,
        avg_ms=avg_ms,
    )


@app.route("/admin/ai-usage")
def admin_ai_usage():
    """Admin view of AI listing-assistant usage statistics."""
    if not (current_user.is_authenticated and current_user.is_admin):
        abort(403)
    from models import AIUsageLog
    from datetime import datetime as _dt, timedelta as _td
    logs        = AIUsageLog.query.order_by(AIUsageLog.created_at.desc()).limit(200).all()
    total       = AIUsageLog.query.count()
    success_cnt = AIUsageLog.query.filter_by(success=True).count()
    failed_cnt  = total - success_cnt
    recent_24h  = AIUsageLog.query.filter(
        AIUsageLog.created_at >= _dt.now() - _td(hours=24)
    ).count()
    return render_template(
        "admin_ai_usage.html",
        logs=logs, total=total,
        success=success_cnt, failed=failed_cnt,
        recent_24h=recent_24h,
    )


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


@app.route("/admin/hauler/<user_id>/set-status", methods=["POST"])
@require_admin
def admin_set_hauler_status(user_id):
    hauler = User.query.filter_by(id=user_id, user_type='hauler').first_or_404()
    new_status = request.form.get('status', '').strip()
    if new_status not in ('pending', 'approved', 'suspended', 'rejected'):
        flash("Invalid status.", "error")
        return redirect(url_for('admin_haulers'))
    hauler.hauler_status = new_status
    db.session.commit()
    # In-app notification to the hauler
    try:
        from notification_service import create_notification as _cn2
        _status_msgs = {
            'approved':  ('✅ Hauler Application Approved',
                          'Your hauler application has been approved. You can now accept marketplace deliveries.',
                          '/hauler/deliveries'),
            'rejected':  ('❌ Hauler Application Declined',
                          'Your hauler application was not approved at this time. Contact support for more information.',
                          '/profile'),
            'suspended': ('⚠️ Hauler Access Suspended',
                          'Your hauler access has been temporarily suspended. Contact support for assistance.',
                          '/profile'),
            'pending':   ('🔄 Hauler Status Updated',
                          'Your hauler account status has been reset to pending review.',
                          '/hauler/dashboard'),
        }
        _t, _m, _u = _status_msgs[new_status]
        _cn2(user_id=hauler.id, type='hauler_status', title=_t, message=_m, action_url=_u)
    except Exception:
        pass
    _hname = f"{hauler.first_name or ''} {hauler.last_name or ''}".strip() or hauler.email
    flash(f"Hauler {_hname} status set to {new_status}.", "success")
    return redirect(url_for('admin_haulers'))


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
        import os as _os
        if not _os.environ.get("SENDGRID_API_KEY"):
            flash(f"Email not sent: SENDGRID_API_KEY is not set in this environment. "
                  f"Add it in DigitalOcean → App → web service component → Environment Variables.", "error")
        else:
            flash(f"Email send failed. SENDGRID_API_KEY is present but SendGrid rejected the request "
                  f"(likely invalid or missing Mail Send permission). "
                  f"Check the Notification Log for the exact HTTP status.", "error")

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
        # Infrastructure / health
        'admin_health_alert':    'Admin — Deploy Health Check Alert',
        # Legacy / test
        'email':                  'General Email',
        'admin':                  'Admin (general)',
    }
    return render_template('admin_notifications.html',
                           logs=logs, sent=sent, failed=failed,
                           sendgrid_configured=sendgrid_configured,
                           from_email=from_email,
                           type_labels=type_labels)


@app.route("/admin/health-alerts")
@require_admin
def admin_health_alerts():
    """Full audit log of deploy health-check alert events (email + SMS combined)."""
    from models import NotificationLog as _NL, SmsLog as _SL
    email_alerts = (_NL.query
                    .filter_by(event_type='admin_health_alert')
                    .order_by(_NL.created_at.desc())
                    .limit(200).all())
    sms_alerts = (_SL.query
                  .filter_by(event_type='admin_health_alert')
                  .order_by(_SL.created_at.desc())
                  .limit(200).all())
    events = []
    for e in email_alerts:
        events.append({
            'ts': e.created_at,
            'channel': 'email',
            'status': e.status,
            'detail': e.error_msg or e.subject or '',
        })
    for s in sms_alerts:
        events.append({
            'ts': s.created_at,
            'channel': 'sms',
            'status': s.status,
            'detail': s.error_msg or (s.message_body or ''),
        })
    events.sort(key=lambda x: x['ts'], reverse=True)
    return render_template('admin_health_alerts.html', events=events)


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
        # Extra guard: never allow deleting the primary admin via this route
        if _is_primary_admin(user):
            flash("The primary admin account cannot be deleted.", "error")
            return redirect(url_for('admin_user_detail', user_id=user_id))
        # Never allow deleting the last admin account
        remaining_admins = User.query.filter_by(is_admin=True).count()
        if remaining_admins <= 1:
            flash("Cannot delete the last active admin account.", "error")
            return redirect(url_for('admin_user_detail', user_id=user_id))
        flash("Admin accounts cannot be deleted.", "error")
        return redirect(url_for('admin_dashboard'))
    from models import OAuth, JobPhoto, CompletionPhoto, GalleryPhoto
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
    # Clean up seller listings — remove gallery pins first, then the listings themselves
    # (prevents both FK constraint violations and orphaned GalleryPhoto rows)
    for listing in Listing.query.filter_by(seller_id=user_id).all():
        GalleryPhoto.query.filter_by(item_type='listing', listing_id=listing.id).delete(synchronize_session=False)
        db.session.delete(listing)
    db.session.flush()  # run listing cascades before removing the user row
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
            cat_id = int(category_filter)
            # For the Housing category, also capture property listings matched by
            # listing_type so legacy rows with NULL category_id still appear.
            housing_cat = Category.query.filter_by(slug='housing').first()
            if housing_cat and cat_id == housing_cat.id:
                housing_child_ids = [c.id for c in housing_cat.subcategories]
                cat_filter_expr = db.or_(
                    Listing.category_id == cat_id,
                    Listing.category_id.in_(housing_child_ids) if housing_child_ids else db.false(),
                    Listing.listing_type.in_(['property_sale', 'rental']),
                )
                query = query.filter(cat_filter_expr)
            else:
                query = query.filter_by(category_id=cat_id)
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
    was_moderation_pending = (listing.moderation_status == 'pending')
    listing.moderation_status = 'approved'
    # Only auto-activate if the listing was genuinely awaiting moderation review
    if listing.status == 'pending' and was_moderation_pending:
        listing.status = 'active'
    db.session.commit()
    # Re-activate any gallery pins if the listing is now fully live
    if listing.status == 'active' and listing.moderation_status == 'approved':
        try:
            _reactivate_gallery_pins(listing.id)
        except Exception as _gp_err:
            app.logger.warning("admin_listing_approve: gallery pin reactivation failed: %s", _gp_err)
    # Phase J: enqueue background fraud scan for newly approved listing
    try:
        from worker.queue import enqueue, NORMAL, LOW
        enqueue('FRAUD_SCAN', {'listing_id': listing.id, 'trigger': 'listing_approved'}, priority=NORMAL)
    except Exception as _fe:
        app.logger.warning("admin_listing_approve: could not enqueue fraud scan: %s", _fe)
    # Phase K: enqueue saved-search in-app notification + cache bust
    try:
        from worker.queue import enqueue as _enqueue_k, LOW as _LOW_K
        import time as _time_k
        _enqueue_k(
            'RECOMMENDATION_REFRESH',
            {'listing_id': listing.id},
            priority=_LOW_K,
            idempotency_key=f"rec-{listing.id}-{int(_time_k.time()//300)}",
        )
    except Exception as _re:
        app.logger.debug("admin_listing_approve: rec refresh enqueue skipped: %s", _re)
    flash(f'Listing "{listing.title}" approved.', 'success')
    return redirect(request.referrer or url_for('admin_listings'))


@app.route("/admin/listings/<int:listing_id>/hide", methods=["POST"])
@require_admin
def admin_listing_hide(listing_id):
    _check_listing_csrf()
    listing = Listing.query.get_or_404(listing_id)
    listing.moderation_status = 'flagged'
    listing.status = 'removed'
    _expire_offers_and_notify(listing.id, listing.title)
    db.session.commit()
    try:
        _deactivate_stale_gallery_pins()
    except Exception as _gp_err:
        app.logger.warning("admin_listing_hide: gallery pin cleanup failed: %s", _gp_err)
    flash(f'Listing "{listing.title}" hidden.', 'success')
    return redirect(request.referrer or url_for('admin_listings'))


@app.route("/admin/listings/<int:listing_id>/remove", methods=["POST"])
@require_admin
def admin_listing_remove(listing_id):
    _check_listing_csrf()
    listing = Listing.query.get_or_404(listing_id)
    listing.moderation_status = 'removed'
    listing.status = 'removed'
    _expire_offers_and_notify(listing.id, listing.title)
    db.session.commit()
    try:
        _deactivate_stale_gallery_pins()
    except Exception as _gp_err:
        app.logger.warning("admin_listing_remove: gallery pin cleanup failed: %s", _gp_err)
    flash(f'Listing "{listing.title}" removed.', 'success')
    return redirect(request.referrer or url_for('admin_listings'))


@app.route("/admin/listings/<int:listing_id>/sold", methods=["POST"])
@require_admin
def admin_listing_mark_sold(listing_id):
    _check_listing_csrf()
    listing = Listing.query.get_or_404(listing_id)
    listing.status = 'sold'
    listing.sold_at = datetime.now()
    _expire_offers_and_notify(listing.id, listing.title)
    db.session.commit()
    try:
        _deactivate_stale_gallery_pins()
    except Exception as _gp_err:
        app.logger.warning("admin_listing_mark_sold: gallery pin cleanup failed: %s", _gp_err)
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
    try:
        _reactivate_gallery_pins(listing.id)
    except Exception as _gp_err:
        app.logger.warning("admin_listing_restore: gallery pin reactivation failed: %s", _gp_err)
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


# ── Admin: Security (local password login, forgot/reset, recovery email) ─────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_local_login():
    """Local email+password admin login — alternative to OAuth."""
    if current_user.is_authenticated and current_user.is_admin:
        return redirect(url_for('admin_dashboard'))

    error = None
    if request.method == "POST":
        _check_listing_csrf()
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        admin = User.query.filter_by(email=email, is_admin=True).first()

        # Generic failure path (don't reveal whether email exists)
        if not admin or not admin.admin_password_hash:
            error = "Invalid credentials."
        else:
            if admin.admin_lockout_until and admin.admin_lockout_until > datetime.now():
                mins = max(1, int((admin.admin_lockout_until - datetime.now()).total_seconds() // 60) + 1)
                error = f"Account temporarily locked. Try again in {mins} minute(s)."
            else:
                from werkzeug.security import check_password_hash
                if check_password_hash(admin.admin_password_hash, password):
                    admin.admin_login_attempts = 0
                    admin.admin_lockout_until  = None
                    db.session.commit()
                    login_user(admin)
                    session['_admin_sv'] = admin.admin_session_version or 0
                    next_url = session.pop("next_url", None)
                    return redirect(next_url or url_for('admin_dashboard'))
                else:
                    admin.admin_login_attempts = (admin.admin_login_attempts or 0) + 1
                    if admin.admin_login_attempts >= 5:
                        from datetime import timedelta
                        admin.admin_lockout_until = datetime.now() + timedelta(minutes=15)
                        db.session.commit()
                        try:
                            from email_service import notify_admin_login_alert
                            ip = (request.headers.get('X-Forwarded-For', '')
                                  or request.remote_addr or 'unknown').split(',')[0].strip()
                            notify_admin_login_alert(admin.email, ip, admin.admin_login_attempts)
                        except Exception:
                            pass
                        error = "Too many failed attempts. Account locked for 15 minutes."
                    else:
                        db.session.commit()
                        remaining = 5 - admin.admin_login_attempts
                        error = f"Invalid credentials. {remaining} attempt(s) remaining before lockout."

    return render_template("admin_login.html", error=error)


@app.route("/admin/forgot-password", methods=["GET", "POST"])
def admin_forgot_password():
    """Step 1 of password recovery: request a reset link by email."""
    sent = False
    if request.method == "POST":
        _check_listing_csrf()
        email = request.form.get("email", "").strip().lower()
        sent  = True   # always show generic confirmation

        admin = User.query.filter(
            User.is_admin == True,
            db.or_(
                db.func.lower(User.email) == email,
                db.func.lower(User.admin_recovery_email) == email
            )
        ).first()

        if admin:
            import secrets, hashlib
            from datetime import timedelta
            raw_token  = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            admin.admin_reset_token    = token_hash
            admin.admin_reset_token_at = datetime.now()
            db.session.commit()
            reset_link = url_for('admin_reset_password', token=raw_token, _external=True)
            try:
                from email_service import notify_admin_password_reset_request
                notify_admin_password_reset_request(email, reset_link)
            except Exception as _e:
                app.logger.warning("admin_forgot_password: email failed: %s", _e)

    return render_template("admin_forgot_password.html", sent=sent)


@app.route("/admin/reset-password/<token>", methods=["GET", "POST"])
def admin_reset_password(token):
    """Step 2 of password recovery: set a new password via the token link."""
    import hashlib
    from datetime import timedelta

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now        = datetime.now()

    admin = User.query.filter(
        User.is_admin == True,
        User.admin_reset_token == token_hash,
        User.admin_reset_token_at != None
    ).first()

    expired = admin is not None and (now - admin.admin_reset_token_at) > timedelta(minutes=30)
    invalid = admin is None or expired

    if invalid:
        return render_template("admin_reset_password.html", invalid=True, expired=expired, error=None)

    error = None
    if request.method == "POST":
        _check_listing_csrf()
        new_pw  = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if len(new_pw) < 12:
            error = "Password must be at least 12 characters."
        elif new_pw != confirm:
            error = "Passwords do not match."
        else:
            from werkzeug.security import generate_password_hash
            admin.admin_password_hash   = generate_password_hash(new_pw)
            admin.admin_reset_token     = None
            admin.admin_reset_token_at  = None
            admin.admin_session_version = (admin.admin_session_version or 0) + 1
            admin.admin_login_attempts  = 0
            admin.admin_lockout_until   = None
            db.session.commit()
            if current_user.is_authenticated:
                logout_user()
            try:
                from email_service import (notify_admin_successful_recovery,
                                           notify_admin_password_changed)
                notify_admin_successful_recovery(admin.email)
                if admin.admin_recovery_email:
                    notify_admin_password_changed(admin.admin_recovery_email)
            except Exception as _e:
                app.logger.warning("admin_reset_password: notify failed: %s", _e)
            flash("Password reset successfully. Please sign in.", "success")
            return redirect(url_for('admin_local_login'))

    return render_template("admin_reset_password.html", invalid=False, expired=False, error=error)


@app.route("/admin/security", methods=["GET", "POST"])
@require_admin
def admin_security_settings():
    """Admin security settings: set/change password, set/verify recovery email, test email."""
    import secrets, hashlib
    from datetime import timedelta

    admin = User.query.get(current_user.id)
    error         = None
    success       = None
    sg_test_result = None   # populated only when send_test_email action runs

    if request.method == "POST":
        _check_listing_csrf()
        action = request.form.get("action", "")

        if action == "set_password":
            new_pw     = request.form.get("new_password", "")
            confirm_pw = request.form.get("confirm_password", "")
            current_pw = request.form.get("current_password", "")

            if len(new_pw) < 12:
                error = "Password must be at least 12 characters."
            elif new_pw != confirm_pw:
                error = "Passwords do not match."
            elif admin.admin_password_hash:
                from werkzeug.security import check_password_hash
                if not check_password_hash(admin.admin_password_hash, current_pw):
                    error = "Current password is incorrect."

            if not error:
                from werkzeug.security import generate_password_hash
                admin.admin_password_hash   = generate_password_hash(new_pw)
                admin.admin_session_version = (admin.admin_session_version or 0) + 1
                db.session.commit()
                # Keep the current OAuth session alive (update version in session)
                session['_admin_sv'] = admin.admin_session_version
                try:
                    from email_service import notify_admin_password_changed
                    notify_admin_password_changed(admin.email)
                    if admin.admin_recovery_email:
                        notify_admin_password_changed(admin.admin_recovery_email)
                except Exception as _e:
                    app.logger.warning("admin_security set_password notify: %s", _e)
                success = "Admin password set successfully. A confirmation email has been sent."

        elif action == "set_recovery_email":
            new_recovery = request.form.get("recovery_email", "").strip().lower()
            confirm_pw   = request.form.get("confirm_password", "")

            if not new_recovery or '@' not in new_recovery:
                error = "Please enter a valid email address."
            elif admin.email and new_recovery == admin.email.lower():
                error = "Recovery email must differ from your primary email."
            elif not admin.admin_password_hash:
                error = "Set an admin password before adding a recovery email."
            else:
                from werkzeug.security import check_password_hash
                if not check_password_hash(admin.admin_password_hash, confirm_pw):
                    error = "Incorrect admin password."

            if not error:
                raw_token  = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
                admin.admin_recovery_email_pending  = new_recovery
                admin.admin_recovery_email_token    = token_hash
                admin.admin_recovery_email_token_at = datetime.now()
                db.session.commit()
                verify_link = url_for('admin_verify_recovery_email', token=raw_token, _external=True)
                masked = _mask_email(new_recovery)
                try:
                    from email_service import (notify_admin_recovery_email_verify,
                                               notify_admin_recovery_email_changed)
                    notify_admin_recovery_email_verify(new_recovery, verify_link)
                    notify_admin_recovery_email_changed(admin.email, masked)
                except Exception as _e:
                    app.logger.warning("admin_security set_recovery_email notify: %s", _e)
                success = f"Verification email sent to {masked}. Click the link in that email to activate it."

        elif action == "send_test_email":
            _sg_key  = os.environ.get("SENDGRID_API_KEY")
            _sg_from = os.environ.get("SENDGRID_FROM_EMAIL", "noreply@jhehaul.com")
            if not _sg_key:
                sg_test_result = {
                    "attempted": True,
                    "status_code": None,
                    "accepted": False,
                    "msg_id": None,
                    "error": "SENDGRID_API_KEY is not set in this environment.",
                    "body": None,
                }
                error = "SENDGRID_API_KEY is not set — email not sent."
            else:
                try:
                    from sendgrid import SendGridAPIClient
                    from sendgrid.helpers.mail import Mail
                    _msg = Mail(
                        from_email=(_sg_from, "JHE Haul"),
                        to_emails=admin.email,
                        subject="JHE Haul Email Test",
                        html_content="<p>This is a diagnostic test email from JHE Haul admin security settings.</p>",
                    )
                    _resp = SendGridAPIClient(_sg_key).send(_msg)
                    _mid  = (_resp.headers.get("X-Message-Id", "") or "") if _resp.headers else ""
                    sg_test_result = {
                        "attempted": True,
                        "status_code": _resp.status_code,
                        "accepted": _resp.status_code == 202,
                        "msg_id": _mid,
                        "error": None,
                        "body": None,
                    }
                    if _resp.status_code == 202:
                        success = f"SendGrid accepted the test email (HTTP 202) — sent to {admin.email}."
                    else:
                        error = f"SendGrid returned HTTP {_resp.status_code} — not accepted."
                except Exception as _e:
                    _sg_code = getattr(_e, "status_code", None)
                    _sg_body = ""
                    try:
                        _sg_body = str(getattr(_e, "body", "") or "")[:600]
                    except Exception:
                        pass
                    sg_test_result = {
                        "attempted": True,
                        "status_code": _sg_code,
                        "accepted": False,
                        "msg_id": None,
                        "error": str(_e)[:400],
                        "body": _sg_body,
                    }
                    error = f"SendGrid error (HTTP {_sg_code or '?'}) — see diagnostic panel below."

    _sg_from_val = os.environ.get("SENDGRID_FROM_EMAIL", "")
    return render_template(
        "admin_security.html",
        admin=admin,
        has_password=bool(admin.admin_password_hash),
        recovery_email=_mask_email(admin.admin_recovery_email) if admin.admin_recovery_email else None,
        recovery_pending=_mask_email(admin.admin_recovery_email_pending) if admin.admin_recovery_email_pending else None,
        sendgrid_ok=bool(os.environ.get("SENDGRID_API_KEY")),
        sg_api_key_present=bool(os.environ.get("SENDGRID_API_KEY")),
        sg_from_email_present=bool(_sg_from_val),
        sg_from_email_value=_sg_from_val or "noreply@jhehaul.com (default fallback — SENDGRID_FROM_EMAIL not set)",
        sg_test_result=sg_test_result,
        error=error,
        success=success,
    )


@app.route("/admin/verify-recovery-email/<token>")
def admin_verify_recovery_email(token):
    """Clicked from the verification email sent to the new recovery address."""
    import hashlib
    from datetime import timedelta

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now        = datetime.now()

    admin = User.query.filter(
        User.is_admin == True,
        User.admin_recovery_email_token == token_hash,
        User.admin_recovery_email_token_at != None
    ).first()

    if not admin or (now - admin.admin_recovery_email_token_at) > timedelta(hours=24):
        flash("This verification link is invalid or has expired. "
              "Please request a new one from Admin Security settings.", "error")
        dest = (url_for('admin_security_settings')
                if current_user.is_authenticated and current_user.is_admin
                else url_for('admin_local_login'))
        return redirect(dest)

    new_recovery = admin.admin_recovery_email_pending
    admin.admin_recovery_email         = new_recovery
    admin.admin_recovery_email_pending = None
    admin.admin_recovery_email_token   = None
    admin.admin_recovery_email_token_at= None
    db.session.commit()

    masked = _mask_email(new_recovery)
    try:
        from email_service import notify_admin_recovery_email_verified
        notify_admin_recovery_email_verified(admin.email, masked)
    except Exception as _e:
        app.logger.warning("admin_verify_recovery_email: notify failed: %s", _e)

    flash(f"Recovery email {masked} verified and activated.", "success")
    dest = (url_for('admin_security_settings')
            if current_user.is_authenticated and current_user.is_admin
            else url_for('admin_local_login'))
    return redirect(dest)


# ── Admin: Users ───────────────────────────────────────────────────────────

@app.route("/admin/users")
@require_admin
def admin_users():
    q = request.args.get('q', '').strip()
    status_filter = request.args.get('status', '')
    type_filter = request.args.get('type', '')
    query = User.query
    if q:
        query = query.filter(
            db.or_(
                User.first_name.ilike(f'%{q}%'),
                User.last_name.ilike(f'%{q}%'),
                User.email.ilike(f'%{q}%'),
                User.id.ilike(f'%{q}%'),
            )
        )
    if status_filter == 'suspended':
        query = query.filter(User.is_suspended == True, User.is_banned == False)
    elif status_filter == 'banned':
        query = query.filter(User.is_banned == True)
    elif status_filter == 'active':
        query = query.filter(User.is_suspended == False, User.is_banned == False)
    if type_filter == 'admin':
        query = query.filter(User.is_admin == True)
    elif type_filter == 'hauler':
        query = query.filter(User.user_type == 'hauler')
    elif type_filter == 'customer':
        query = query.filter(User.user_type == 'customer', User.is_admin == False)
    users = query.order_by(User.created_at.desc()).all()
    listing_counts = {
        u.id: Listing.query.filter_by(seller_id=u.id).filter(Listing.status != 'draft').count()
        for u in users
    }
    total = len(users)
    return render_template('admin_users.html', users=users, total=total,
                           listing_counts=listing_counts, q=q,
                           status_filter=status_filter, type_filter=type_filter)


@app.route("/admin/users/<string:user_id>/suspend", methods=["POST"])
@require_admin
def admin_user_suspend(user_id):
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash("Admin accounts cannot be suspended.", "error")
        return redirect(url_for('admin_users'))
    user.is_suspended = True
    _notes = request.form.get('notes', '').strip() or f'Suspended by admin {current_user.email}'
    _log_mod_action('suspend', 'user', user_id, notes=_notes)
    db.session.commit()
    name = ((user.first_name or '') + ' ' + (user.last_name or '')).strip() or user.email
    flash(f"{name}'s account suspended.", "success")
    return redirect(request.referrer or url_for('admin_users'))


@app.route("/admin/users/<string:user_id>/restore", methods=["POST"])
@require_admin
def admin_user_restore(user_id):
    user = User.query.get_or_404(user_id)
    user.is_suspended = False
    _log_mod_action('unsuspend', 'user', user_id,
                    notes=f'Unsuspended by admin {current_user.email}')
    db.session.commit()
    name = ((user.first_name or '') + ' ' + (user.last_name or '')).strip() or user.email
    flash(f"{name}'s account restored.", "success")
    return redirect(request.referrer or url_for('admin_users'))


@app.route("/admin/listings/<int:listing_id>/moderate", methods=["POST"])
@require_admin
def admin_listing_moderate(listing_id):
    """Remove or restore a listing from admin user-detail page."""
    _check_listing_csrf()
    listing = Listing.query.get_or_404(listing_id)
    action = request.form.get('action', '')
    if action == 'remove':
        listing.status = 'removed'
        listing.moderation_status = 'removed'
        _log_mod_action('remove_listing', 'listing', listing_id,
                        notes=f'Removed by {current_user.email}')
        db.session.commit()
        try:
            _deactivate_stale_gallery_pins()
        except Exception as _gp_err:
            app.logger.warning("admin_listing_moderate: gallery pin cleanup failed: %s", _gp_err)
        flash(f'Listing "{listing.title}" removed.', 'success')
    elif action == 'restore':
        listing.status = 'active'
        listing.moderation_status = 'approved'
        _log_mod_action('restore_listing', 'listing', listing_id,
                        notes=f'Restored by {current_user.email}')
        db.session.commit()
        try:
            _reactivate_gallery_pins(listing.id)
        except Exception as _gp_err:
            app.logger.warning("admin_listing_moderate restore: gallery pin reactivation failed: %s", _gp_err)
        flash(f'Listing "{listing.title}" restored.', 'success')
    else:
        flash('Unknown action.', 'error')
    return redirect(request.referrer or url_for('admin_users'))


@app.route("/admin/users/<user_id>/detail")
@require_admin
def admin_user_detail(user_id):
    """Admin: view a single user's profile, listings, reports, and moderation log."""
    from models import Listing as _L, UserReport, ListingReport, ModerationAuditLog, Notification
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
    mod_logs = (ModerationAuditLog.query
                .filter(
                    ModerationAuditLog.target_type == 'user',
                    ModerationAuditLog.target_id == str(user_id)
                )
                .order_by(ModerationAuditLog.created_at.desc())
                .limit(50).all())
    _admin_ids = {log.admin_id for log in mod_logs if log.admin_id}
    admin_map = {a.id: a for a in User.query.filter(User.id.in_(_admin_ids)).all()} if _admin_ids else {}
    sent_notices = (Notification.query
                    .filter_by(user_id=str(user_id), type='admin_notice')
                    .order_by(Notification.created_at.desc())
                    .limit(50).all())
    return render_template('admin_user_detail.html',
                           seller=seller,
                           listings=listings,
                           user_reports=user_reports,
                           listing_reports=listing_reports,
                           mod_logs=mod_logs,
                           admin_map=admin_map,
                           sent_notices=sent_notices)


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
        _expire_offers_and_notify(listing.id, listing.title)
    report.status = 'resolved'
    db.session.commit()
    if listing:
        try:
            _deactivate_stale_gallery_pins()
        except Exception as _gp_err:
            app.logger.warning("admin_report_remove_listing: gallery pin cleanup failed: %s", _gp_err)
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
    if listing:
        try:
            _reactivate_gallery_pins(listing.id)
        except Exception as _gp_err:
            app.logger.warning("admin_report_restore_listing: gallery pin reactivation failed: %s", _gp_err)
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


# ── Admin: Moderation actions (ban, unban, warn, flag, notes) ──────────────

def _log_mod_action(action, target_type, target_id, report_id=None, notes=None):
    """Record a moderation action in the audit log."""
    from models import ModerationAuditLog
    entry = ModerationAuditLog(
        admin_id=current_user.id,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        report_id=report_id,
        notes=notes,
    )
    db.session.add(entry)


@app.route("/admin/users/<user_id>/ban", methods=["POST"])
@require_admin
def admin_ban_user(user_id):
    _check_listing_csrf()
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash("Cannot ban an admin account.", "error")
        return redirect(request.referrer or url_for('admin_user_reports'))
    report_id = request.form.get('report_id')
    notes = request.form.get('notes', '')
    user.is_banned = True
    user.is_suspended = True
    _log_mod_action('ban', 'user', user_id,
                    report_id=int(report_id) if report_id else None,
                    notes=notes or f'Banned by admin {current_user.email}')
    db.session.commit()
    flash(f"Account for {user.email or user.id} has been permanently banned.", "success")
    return redirect(request.referrer or url_for('admin_user_reports'))


@app.route("/admin/users/<user_id>/unban", methods=["POST"])
@require_admin
def admin_unban_user(user_id):
    _check_listing_csrf()
    user = User.query.get_or_404(user_id)
    user.is_banned = False
    user.is_suspended = False
    _log_mod_action('unban', 'user', user_id,
                    notes=f'Unbanned by admin {current_user.email}')
    db.session.commit()
    flash(f"Account for {user.email or user.id} has been unbanned.", "success")
    return redirect(request.referrer or url_for('admin_user_reports'))


@app.route("/admin/users/<user_id>/warn", methods=["POST"])
@require_admin
def admin_warn_user(user_id):
    _check_listing_csrf()
    user = User.query.get_or_404(user_id)
    report_id = request.form.get('report_id')
    notes = request.form.get('notes', '')
    user.marketplace_warning_count = (user.marketplace_warning_count or 0) + 1
    _log_mod_action('warn', 'user', user_id,
                    report_id=int(report_id) if report_id else None,
                    notes=notes or f'Warning #{user.marketplace_warning_count} issued by {current_user.email}')
    db.session.commit()
    flash(f"Warning issued to {user.email or user.id} (total: {user.marketplace_warning_count}).", "success")
    return redirect(request.referrer or url_for('admin_user_reports'))


@app.route("/admin/reports/<int:report_id>/flag-investigate", methods=["POST"])
@require_admin
def admin_report_flag_investigate(report_id):
    _check_listing_csrf()
    report = ListingReport.query.get_or_404(report_id)
    report.investigation_flag = True
    report.status = 'under_investigation'
    _log_mod_action('flag_investigate', 'report', report_id)
    db.session.commit()
    flash("Report flagged for investigation.", "success")
    return redirect(url_for('admin_reports'))


@app.route("/admin/reports/<int:report_id>/add-note", methods=["POST"])
@require_admin
def admin_report_add_note(report_id):
    _check_listing_csrf()
    report = ListingReport.query.get_or_404(report_id)
    notes = request.form.get('admin_notes', '').strip()
    if notes:
        existing = (report.admin_notes or '').strip()
        ts = datetime.now().strftime('%b %d %H:%M')
        report.admin_notes = f"{existing}\n[{ts} — {current_user.email}] {notes}".strip()
        _log_mod_action('add_note', 'report', report_id, notes=notes)
        db.session.commit()
        flash("Note saved.", "success")
    return redirect(url_for('admin_reports'))


@app.route("/admin/user-reports/<int:report_id>/flag-investigate", methods=["POST"])
@require_admin
def admin_user_report_flag_investigate(report_id):
    _check_listing_csrf()
    from models import UserReport
    report = UserReport.query.get_or_404(report_id)
    report.investigation_flag = True
    report.status = 'under_investigation'
    _log_mod_action('flag_investigate', 'report', report_id)
    db.session.commit()
    flash("Report flagged for investigation.", "success")
    return redirect(url_for('admin_user_reports'))


@app.route("/admin/user-reports/<int:report_id>/add-note", methods=["POST"])
@require_admin
def admin_user_report_add_note(report_id):
    _check_listing_csrf()
    from models import UserReport
    report = UserReport.query.get_or_404(report_id)
    notes = request.form.get('admin_notes', '').strip()
    if notes:
        existing = (report.admin_notes or '').strip()
        ts = datetime.now().strftime('%b %d %H:%M')
        report.admin_notes = f"{existing}\n[{ts} — {current_user.email}] {notes}".strip()
        _log_mod_action('add_note', 'report', report_id, notes=notes)
        db.session.commit()
        flash("Note saved.", "success")
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

    # Check whether Twilio account is in Trial mode (trial = only verified numbers can receive SMS)
    twilio_trial = False
    if twilio_configured:
        try:
            from twilio.rest import Client as _TwilioClient
            _tc = _TwilioClient(twilio_sid, twilio_tok)
            acct = _tc.api.accounts(twilio_sid).fetch()
            twilio_trial = (acct.type == 'Trial')
        except Exception:
            pass  # If check fails, don't block the page

    return render_template('admin_sms_settings.html',
                           settings=settings,
                           twilio_configured=twilio_configured,
                           twilio_trial=twilio_trial,
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
    settings.ev_quote_withdrawn  = request.form.get("ev_quote_withdrawn") == "1"
    settings.ev_seller_listing_expired = request.form.get("ev_seller_listing_expired") == "1"
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

def _run_health_checks():
    """
    Run all startup / liveness checks and return a list of error strings.
    An empty list means everything is healthy.
    Called both by the /health route and by the wsgi.py startup notifier.
    """
    errors = []

    # 1. Verify critical third-party packages are importable
    critical_modules = {
        "flask": "Flask",
        "flask_sqlalchemy": "Flask-SQLAlchemy",
        "flask_login": "Flask-Login",
        "stripe": "stripe",
        "sendgrid": "sendgrid",
        "twilio": "twilio",
        "boto3": "boto3",
        "psycopg2": "psycopg2-binary",
        "pgeocode": "pgeocode",
        "PIL": "Pillow",
        "flask_wtf": "Flask-WTF",
    }
    for module, pkg in critical_modules.items():
        try:
            __import__(module)
        except ImportError as exc:
            errors.append(f"missing package {pkg}: {exc}")

    # 2. Verify required environment variables are set
    # These must be present for the site to function correctly in production.
    strictly_required = [
        "DATABASE_URL",
        "APP_BASE_URL",
        "STRIPE_SECRET_KEY",
        "SENDGRID_API_KEY",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        # Stripe payment-link tiers — missing any tier breaks payment for that price range
        "PAY_LINK_UNDER_150",
        "PAY_LINK_150_300",
        "PAY_LINK_300_500",
        "PAY_LINK_OVER_500",
    ]
    for var in strictly_required:
        if not os.environ.get(var):
            errors.append(f"missing required environment variable: {var}")

    # Session secret: app accepts either SESSION_SECRET or the legacy SECRET_KEY alias
    if not (os.environ.get("SESSION_SECRET") or os.environ.get("SECRET_KEY")):
        errors.append("missing required environment variable: SESSION_SECRET (or SECRET_KEY)")

    # Twilio is optional (SMS disabled when not configured). But if TWILIO_ACCOUNT_SID
    # is set, the remaining credentials must also be present.
    if os.environ.get("TWILIO_ACCOUNT_SID"):
        if not os.environ.get("TWILIO_AUTH_TOKEN"):
            errors.append(
                "missing required environment variable: TWILIO_AUTH_TOKEN "
                "(required when TWILIO_ACCOUNT_SID is set)"
            )
        if not (os.environ.get("TWILIO_PHONE_NUMBER") or os.environ.get("TWILIO_FROM_NUMBER")):
            errors.append(
                "missing required environment variable: TWILIO_PHONE_NUMBER or TWILIO_FROM_NUMBER "
                "(required when TWILIO_ACCOUNT_SID is set)"
            )

    # 3. Verify database is reachable with a cheap query
    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("SELECT 1"))
    except Exception as exc:
        errors.append(f"database unreachable: {exc}")

    return errors
@app.route("/health")
def health():
    """
    Health-check endpoint used by DigitalOcean App Platform (see .do/app.yaml).
    Returns 200 when critical imports and the DB are reachable; 503 otherwise.
    Lightweight and unauthenticated — safe to poll frequently.
    """
    errors = _run_health_checks()
    if errors:
        return jsonify({"status": "error", "errors": errors}), 503
    return jsonify({"status": "ok"}), 200


# ─────────────────────────────────────────────────────────────────────────────
# Category discovery pages — stable, indexable URLs for SEO (Phase C)
# ─────────────────────────────────────────────────────────────────────────────
_CATEGORY_PAGE_CFG = {
    "vehicles": {
        "listing_type": "item",
        "category_slug": "vehicles",
        "page_title": "Vehicles for Sale | JHE Haul Marketplace",
        "page_desc":  "Browse cars, trucks, motorcycles, and more for sale near you on JHE Haul.",
    },
    "items": {
        "listing_type": "item",
        "category_slug": None,
        "page_title": "Items for Sale | JHE Haul Marketplace",
        "page_desc":  "Browse furniture, electronics, tools, collectibles, and more for sale locally on JHE Haul.",
    },
    "homes-for-sale": {
        "listing_type": "property_sale",
        "category_slug": None,
        "page_title": "Homes for Sale | JHE Haul Real Estate",
        "page_desc":  "Browse homes, condos, and properties for sale near you on JHE Haul Marketplace.",
    },
    "rentals": {
        "listing_type": "rental",
        "category_slug": None,
        "page_title": "Rentals | JHE Haul Marketplace",
        "page_desc":  "Browse apartments, houses, and rooms for rent near you on JHE Haul.",
    },
}

@app.route("/marketplace/<cat_page>")
def marketplace_category(cat_page):
    """Stable, indexable category discovery pages for SEO."""
    cfg = _CATEGORY_PAGE_CFG.get(cat_page)
    if not cfg:
        abort(404)

    lt       = cfg["listing_type"]
    cat_slug = cfg["category_slug"]

    categories = _marketplace_categories()

    q = Listing.query.filter(
        Listing.status == "active",
        Listing.moderation_status == "approved",
        Listing.listing_type == lt,
    )
    cat_obj = None
    if cat_slug:
        cat_obj = Category.query.filter_by(slug=cat_slug, is_active=True).first()
        if cat_obj:
            child_ids = [c.id for c in Category.query.filter_by(parent_id=cat_obj.id, is_active=True).all()]
            id_set = [cat_obj.id] + child_ids
            q = q.filter(Listing.category_id.in_(id_set))

    _limit      = min(max(int(request.args.get('limit', 48) or 48), 1), 192)
    all_results = q.order_by(Listing.created_at.desc()).limit(_limit + 1).all()
    has_more    = len(all_results) > _limit
    results     = all_results[:_limit]

    from urllib.parse import urlencode as _urlencode
    _lm_args2 = {k: v for k, v in request.args.items() if k != 'limit'}
    _lm_qs2 = _urlencode(_lm_args2)
    _load_more_base_url2 = f'/marketplace/{cat_page}?' + (_lm_qs2 + '&' if _lm_qs2 else '')
    return render_template(
        "marketplace.html",
        categories=categories,
        is_search=True,
        search_query="",
        search_results=results,
        active_category=cat_obj,
        price_type_filter="",
        featured_filter=False,
        listing_type_filter=lt,
        no_vehicles_filter=False,
        area_filter="",
        city_zip_filter="",
        min_price=None,
        max_price=None,
        min_beds=None,
        open_house_only=False,
        hide_sold=False,
        search_limit=_limit,
        has_more=has_more,
        load_more_base_url=_load_more_base_url2,
        saved_listing_ids=_saved_listing_ids(),
        show_welcome=False,
        show_profile_nudge=False,
        zip_radius_fallback=False,
        recent_listings=[],
        free_listings=[],
        featured_listings=[],
        for_sale_listings=[],
        rental_listings=[],
        # SEO overrides consumed by marketplace.html blocks
        seo_page_title=cfg["page_title"],
        seo_page_desc=cfg["page_desc"],
        seo_canonical_path=f"/marketplace/{cat_page}",
    )


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
        "Disallow: /selling",
        "Disallow: /my-listings",
        "Disallow: /notifications",
        "Disallow: /saved",
        "Disallow: /my-offers",
        "Disallow: /seller/",
        "",
        f"Sitemap: {base}/sitemap.xml",
    ])
    resp = make_response(content)
    resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.route("/sitemap.xml")
def sitemap_xml():
    """Dynamically generated XML sitemap — active listings + public pages."""
    base = os.environ.get("APP_BASE_URL", "https://jhehaul.com").rstrip("/")

    # Static public pages
    static_pages = [
        ("",                        "1.0",  "daily"),
        ("/marketplace",            "0.9",  "hourly"),
        ("/marketplace/vehicles",   "0.8",  "daily"),
        ("/marketplace/items",      "0.8",  "daily"),
        ("/marketplace/homes-for-sale", "0.7", "daily"),
        ("/marketplace/rentals",    "0.7",  "daily"),
        ("/about",                  "0.5",  "monthly"),
        ("/invite",                 "0.5",  "monthly"),
    ]

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]

    for path, priority, changefreq in static_pages:
        lines += [
            "  <url>",
            f"    <loc>{base}{path}</loc>",
            f"    <changefreq>{changefreq}</changefreq>",
            f"    <priority>{priority}</priority>",
            "  </url>",
        ]

    # Active, approved listings
    try:
        active_listings = (Listing.query
                           .filter(Listing.status == "active",
                                   Listing.moderation_status == "approved")
                           .order_by(Listing.created_at.desc())
                           .limit(45000)
                           .all())
        for lst in active_listings:
            loc = base + listing_canonical_path(lst)
            lastmod = (lst.updated_at or lst.created_at)
            lastmod_str = lastmod.strftime("%Y-%m-%d") if lastmod else ""
            lines += [
                "  <url>",
                f"    <loc>{loc}</loc>",
            ]
            if lastmod_str:
                lines.append(f"    <lastmod>{lastmod_str}</lastmod>")
            lines += [
                "    <changefreq>weekly</changefreq>",
                "    <priority>0.8</priority>",
                "  </url>",
            ]
    except Exception as _e:
        app.logger.warning("sitemap_xml: listing query failed: %s", _e)

    lines.append("</urlset>")

    from flask import Response
    return Response("\n".join(lines), mimetype="application/xml")

def _gallery_photos(active_only=False):
    from models import GalleryPhoto
    q = GalleryPhoto.query
    if active_only:
        q = q.filter(GalleryPhoto.is_active == True)
    return q.order_by(GalleryPhoto.display_order, GalleryPhoto.id).all()

def _deactivate_stale_gallery_pins():
    """Deactivate pinned listing gallery entries whose listing is no longer active.

    A listing pin is considered stale when:
    - The listing no longer exists (orphaned foreign key)
    - The listing status is anything other than 'active'  (sold, reserved, expired, removed, draft, …)
    - The listing's moderation_status is not 'approved'

    Stale pins are deactivated (is_active set to False) rather than deleted so
    the admin Featured Content page shows the badge flip to Inactive.
    Public-facing pages use active_only=True, so deactivated pins are never
    shown to visitors.  Runs at startup and on every admin gallery page visit.
    Returns the count of rows deactivated.
    """
    from models import GalleryPhoto
    all_listing_pins = (GalleryPhoto.query
                        .filter_by(item_type='listing', is_active=True)
                        .all())
    deactivated = 0
    for pin in all_listing_pins:
        listing = pin.listing_rel
        if (listing is None
                or listing.status != 'active'
                or listing.moderation_status != 'approved'):
            pin.is_active = False
            pin.auto_deactivated = True   # provenance: deactivated by stale-cleanup, not admin
            deactivated += 1
    if deactivated:
        try:
            db.session.commit()
            app.logger.info("gallery cleanup: deactivated %d stale pinned listing(s)", deactivated)
        except Exception as _e:
            db.session.rollback()
            app.logger.warning("gallery cleanup: commit failed: %s", _e)
    return deactivated


def _reactivate_gallery_pins(listing_id):
    """Re-activate gallery pins for a listing that has returned to active+approved.

    ONLY reactivates pins whose is_active was set to False by
    _deactivate_stale_gallery_pins() (identified by auto_deactivated=True).
    Pins an admin intentionally disabled via the Featured Content toggle have
    auto_deactivated=False and are left untouched.

    Call this *after* db.session.commit() when a listing transitions back to
    status='active' and moderation_status='approved'.
    Returns the count of rows re-activated.
    """
    from models import GalleryPhoto
    inactive_pins = (GalleryPhoto.query
                     .filter_by(item_type='listing', listing_id=listing_id,
                                is_active=False, auto_deactivated=True)
                     .all())
    reactivated = 0
    for pin in inactive_pins:
        pin.is_active = True
        pin.auto_deactivated = False   # clear provenance flag now that pin is live again
        reactivated += 1
    if reactivated:
        try:
            db.session.commit()
            app.logger.info(
                "gallery pins: re-activated %d pin(s) for listing %s",
                reactivated, listing_id,
            )
        except Exception as _e:
            db.session.rollback()
            app.logger.warning("gallery pins: re-activation commit failed: %s", _e)
    return reactivated


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
    headline = (request.form.get("headline") or "").strip()[:200] or None
    description = (request.form.get("description") or "").strip()[:500] or None
    button_text = (request.form.get("button_text") or "").strip()[:100] or None
    button_link = (request.form.get("button_link") or "").strip()[:500] or None
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
            item_type='custom',
            caption=caption,
            headline=headline or caption,
            description=description,
            button_text=button_text,
            button_link=button_link,
            is_active=True,
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
    """Public marketing landing page — featured listings for the homepage."""
    from models import Listing
    _active_approved = Listing.query.filter_by(status='active', moderation_status='approved')
    preview_listings = (
        _active_approved
        .filter(Listing.listing_type == 'item')
        .order_by(Listing.created_at.desc())
        .limit(6)
        .all()
    )
    for_sale_listings = (
        _active_approved
        .filter(Listing.listing_type == 'property_sale')
        .order_by(Listing.created_at.desc())
        .limit(6)
        .all()
    )
    rental_listings = (
        _active_approved
        .filter(Listing.listing_type == 'rental')
        .order_by(Listing.created_at.desc())
        .limit(6)
        .all()
    )
    return render_template(
        'landing.html',
        gallery_photos=_gallery_photos(active_only=True),
        preview_listings=preview_listings,
        for_sale_listings=for_sale_listings,
        rental_listings=rental_listings,
    )

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
    _deactivate_stale_gallery_pins()
    return render_template('admin_gallery.html', photos=_gallery_photos(active_only=False))

@app.route("/admin/gallery/listing-search")
@require_admin
def admin_gallery_listing_search():
    """JSON search of active listings for the feature-a-listing UI."""
    from models import Listing, ListingPhoto
    from flask import jsonify
    q = request.args.get("q", "").strip()
    results = []
    if q:
        listings = (Listing.query
                    .filter(Listing.status == 'active', Listing.title.ilike(f"%{q}%"))
                    .order_by(Listing.created_at.desc())
                    .limit(10).all())
        for lst in listings:
            first_photo = (ListingPhoto.query
                           .filter_by(listing_id=lst.id)
                           .order_by(ListingPhoto.is_primary.desc(), ListingPhoto.display_order)
                           .first())
            if first_photo:
                thumb = first_photo.storage_url or url_for('serve_listing_photo', photo_id=first_photo.id)
            else:
                thumb = None
            results.append({
                "id": lst.id,
                "title": lst.title,
                "price": lst.price,
                "price_type": lst.price_type,
                "city": lst.city or "",
                "thumb": thumb,
            })
    return jsonify(results)

@app.route("/admin/gallery/feature-listing", methods=["POST"])
@require_admin
def admin_gallery_feature_listing():
    """Pin an existing active listing to the homepage featured section."""
    from models import GalleryPhoto, Listing
    from sqlalchemy import func as _func
    listing_id = request.form.get("listing_id", type=int)
    if not listing_id:
        flash("No listing selected.", "error")
        return redirect(url_for('admin_gallery'))
    listing = Listing.query.get(listing_id)
    if not listing or listing.status != 'active':
        flash("Listing not found or not active.", "error")
        return redirect(url_for('admin_gallery'))
    max_order = db.session.query(_func.coalesce(_func.max(GalleryPhoto.display_order), 0)).scalar() or 0
    gp = GalleryPhoto(
        item_type='listing',
        listing_id=listing_id,
        headline=listing.title,
        filename='',
        display_order=max_order + 1,
        is_active=True,
    )
    db.session.add(gp)
    db.session.commit()
    flash(f"\u201c{listing.title}\u201d added to featured content.", "success")
    return redirect(url_for('admin_gallery'))

@app.route("/admin/gallery/<int:photo_id>/toggle", methods=["POST"])
@require_admin
def admin_gallery_toggle(photo_id):
    """Activate or deactivate a featured item without removing it."""
    from models import GalleryPhoto
    photo = GalleryPhoto.query.get_or_404(photo_id)
    photo.is_active = not photo.is_active
    photo.auto_deactivated = False   # manual toggle always takes ownership; auto-reactivation must not override this
    db.session.commit()
    flash(f"Item {'activated' if photo.is_active else 'deactivated'}.", "success")
    return redirect(url_for('admin_gallery'))

@app.route("/admin/gallery/<int:photo_id>/edit", methods=["POST"])
@require_admin
def admin_gallery_edit(photo_id):
    """Update the headline, description, and button for a custom banner."""
    from models import GalleryPhoto
    photo = GalleryPhoto.query.get_or_404(photo_id)
    photo.headline = (request.form.get("headline") or "").strip()[:200] or None
    photo.caption = photo.headline  # keep legacy field in sync
    photo.description = (request.form.get("description") or "").strip()[:500] or None
    photo.button_text = (request.form.get("button_text") or "").strip()[:100] or None
    photo.button_link = (request.form.get("button_link") or "").strip()[:500] or None
    db.session.commit()
    flash("Featured item updated.", "success")
    return redirect(url_for('admin_gallery'))

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


# ══════════════════════════════════════════════════════════════════════════════
# PHASE K — RECOMMENDATION & PERSONALIZATION INTELLIGENCE
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/listing/<int:listing_id>/view", methods=["POST"])
def api_record_listing_view(listing_id):
    """Record a listing view for recommendation personalisation.

    Called by listing_detail.html JS on page load.
    No auth required — anonymous sessions are tracked via session_key.
    Never exposes user data; safe to call without CSRF (no state mutation visible
    to other users; worst-case an extra ListingView row is written).
    """
    from ai.recommendations import record_view
    user_id = None
    if current_user.is_authenticated:
        user_id = current_user.id
    session_key = session.get('rec_session_key')
    record_view(user_id, listing_id, session_key)
    return jsonify({'ok': True})


@app.route("/api/recommendations", methods=["GET"])
@require_login
def api_recommendations():
    """Return personalised listing recommendations for the authenticated user.

    Query params:
      ?source=recommended_for_you  (default) | new_near_you
      ?limit=12
    """
    from ai.recommendations import get_recommended_for_you, get_new_near_user
    source = request.args.get('source', 'recommended_for_you')
    limit  = min(int(request.args.get('limit', 12)), 24)

    if source == 'new_near_you':
        zip_code = getattr(current_user, 'home_zip', None)
        city     = getattr(current_user, 'city', None)
        listings = get_new_near_user(zip_code, city, limit=limit,
                                     exclude_seller_id=current_user.id)
        return jsonify({
            'source': 'new_near_you',
            'is_personalised': bool(zip_code or city),
            'listings': [_listing_card_json(l) for l in listings],
        })

    result = get_recommended_for_you(current_user, limit=limit)
    return jsonify({
        'source': 'recommended_for_you',
        'is_personalised': result['is_personalised'],
        'fallback_label':  result.get('fallback_label'),
        'listings': [
            {**_listing_card_json(l),
             'reason': result['reasons'].get(l.id)}
            for l in result['listings']
        ],
    })


def _listing_card_json(listing) -> dict:
    """Minimal safe JSON representation of a Listing for the frontend."""
    primary_photo = listing.primary_photo
    photo_url = None
    if primary_photo:
        photo_url = (primary_photo.storage_url
                     or url_for('serve_listing_photo', photo_id=primary_photo.id))
    price_display = None
    if listing.price_type == 'free':
        price_display = 'Free'
    elif listing.price:
        price_display = f'${listing.price:,.0f}'
    return {
        'id':            listing.id,
        'title':         listing.title,
        'price_display': price_display,
        'price_type':    listing.price_type,
        'city':          listing.city,
        'state':         listing.state,
        'photo_url':     photo_url,
        'url':           url_for('listing_detail', listing_id=listing.id),
        'is_property':   listing.is_property,
        'listing_type':  listing.listing_type,
    }


@app.route("/api/listing/<int:listing_id>/similar", methods=["GET"])
def api_listing_similar(listing_id):
    """Return similar listings for a given listing (JSON, public)."""
    from models import Listing as _LSim
    from ai.recommendations import get_similar_listings as _get_sim
    listing = _LSim.query.get_or_404(listing_id)
    user = current_user if current_user.is_authenticated else None
    limit = min(int(request.args.get('limit', 6)), 12)
    similar, is_fallback = _get_sim(listing, user=user, limit=limit)
    return jsonify({
        'listing_id': listing_id,
        'is_fallback': is_fallback,
        'listings': [_listing_card_json(l) for l in similar],
    })


@app.route("/api/recently-viewed", methods=["GET"])
def api_recently_viewed():
    """Return recently viewed active listings for the current user or session."""
    from ai.recommendations import get_recently_viewed as _get_rv
    user_id     = current_user.id if current_user.is_authenticated else None
    session_key = session.get('rec_session_key')
    limit = min(int(request.args.get('limit', 10)), 20)
    listings = _get_rv(user_id=user_id, session_key=session_key, limit=limit)
    return jsonify({'listings': [_listing_card_json(l) for l in listings]})


@app.route("/api/recommendations/event", methods=["POST"])
def api_recommendations_event():
    """Track a recommendation interaction event (click, save, etc.)."""
    from ai.recommendations import record_event as _rec_event
    data = request.get_json(silent=True) or {}
    listing_id = data.get('listing_id')
    event_type = data.get('event_type', 'click')
    source     = data.get('source', 'recommended_for_you')
    if not listing_id:
        return jsonify({'ok': False, 'error': 'listing_id required'}), 400
    user_id = current_user.id if current_user.is_authenticated else None
    session_key = session.get('rec_session_key')
    _rec_event(user_id, int(listing_id), event_type, source, session_key)
    return jsonify({'ok': True})


@app.route("/settings/notifications", methods=["GET", "POST"])
@require_login
def settings_notifications():
    """Notification Preference Center — Phase M Growth Automation."""
    BOOL_PREFS = [
        # (form_field, model_attr, label, category)
        ('notify_saved_search_match',    'notify_saved_search_match',    'Saved search matches',       'discover'),
        ('notify_price_drop',            'notify_price_drop',            'Price drops on saved items', 'discover'),
        ('notify_offer_reminder',        'notify_offer_reminder',        'Pending offer reminders',    'offers'),
        ('notify_listing_expiry_reminder','notify_listing_expiry_reminder','Listing expiry reminders', 'listings'),
        ('notify_listing_status_changes','notify_listing_status_changes', 'Listing status changes',    'listings'),
        ('notify_recommendations',       'notify_recommendations',       'Personalised recommendations','discover'),
        ('notify_email_price_drop',      'notify_email_price_drop',      'Price drop emails',          'email'),
        ('notify_email_offers',          'notify_email_offers',          'Offer reminder emails',      'email'),
        ('notify_email_listing_expiry',  'notify_email_listing_expiry',  'Listing expiry emails',      'email'),
        ('notify_email_recommendations', 'notify_email_recommendations', 'Recommendation emails',      'email'),
    ]
    if request.method == "POST":
        _check_listing_csrf()
        for field, attr, *_ in BOOL_PREFS:
            val = request.form.get(field) == 'on'
            setattr(current_user, attr, val)
        db.session.commit()
        flash("Notification preferences saved.", "success")
        return redirect(url_for('settings_notifications'))
    return render_template(
        'settings_notifications.html',
        prefs=BOOL_PREFS,
        user=current_user,
    )


@app.route("/api/settings/personalization", methods=["POST"])
@require_login
def api_settings_personalization():
    """Toggle personalisation settings for the authenticated user."""
    from models import db
    data = request.get_json(silent=True) or {}
    if 'personalization_enabled' in data:
        val = bool(data['personalization_enabled'])
        try:
            current_user.personalization_enabled = val
            db.session.commit()
            from ai.recommendations import _cache_invalidate
            _cache_invalidate(current_user.id)
        except Exception:
            db.session.rollback()
    return jsonify({'ok': True,
                    'personalization_enabled': getattr(current_user, 'personalization_enabled', True)})


# Ensure anonymous sessions have a stable rec_session_key
@app.before_request
def _ensure_rec_session_key():
    import uuid as _uuid
    if 'rec_session_key' not in session:
        session['rec_session_key'] = _uuid.uuid4().hex


# ══════════════════════════════════════════════════════════════════════════════
# PHASE L — AI ADMIN + MARKETPLACE OPERATIONS ASSISTANT
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/admin/operations")
@require_admin
def admin_operations():
    """Admin-only AI Operations Assistant page — Phase L."""
    from datetime import timedelta as _td_ops
    from models import (Listing, ListingReport, FraudFlag,
                        BackgroundJob, NotificationLog)

    today = datetime.utcnow() - _td_ops(hours=24)

    new_users_today    = User.query.filter(User.created_at >= today).count()
    new_listings_today = Listing.query.filter(Listing.created_at >= today,
                                              Listing.status != 'draft').count()
    active_listings    = Listing.query.filter_by(status='active',
                                                 moderation_status='approved').count()
    pending_mod        = Listing.query.filter(
        db.or_(Listing.status == 'pending',
               Listing.moderation_status == 'pending')
    ).count()
    open_reports       = ListingReport.query.filter_by(status='pending').count()
    high_risk_flags    = FraudFlag.query.filter(
        FraudFlag.risk_level.in_(['HIGH', 'CRITICAL']),
        FraudFlag.status == 'pending',
    ).count()
    failed_jobs        = BackgroundJob.query.filter_by(status='FAILED').count()
    failed_emails_24h  = NotificationLog.query.filter(
        NotificationLog.status == 'failed',
        NotificationLog.created_at >= today,
    ).count()

    # Attention items for the page (deterministic, no AI call on load)
    attention_items = []
    if FraudFlag.query.filter(FraudFlag.risk_level == 'CRITICAL',
                              FraudFlag.status == 'pending').count():
        n = FraudFlag.query.filter(FraudFlag.risk_level == 'CRITICAL',
                                   FraudFlag.status == 'pending').count()
        attention_items.append({'severity': 'CRITICAL',
                                'label': f'{n} critical fraud flag(s) pending',
                                'url': '/admin/fraud-queue'})
    if FraudFlag.query.filter(FraudFlag.risk_level == 'HIGH',
                              FraudFlag.status == 'pending').count():
        n = FraudFlag.query.filter(FraudFlag.risk_level == 'HIGH',
                                   FraudFlag.status == 'pending').count()
        attention_items.append({'severity': 'HIGH',
                                'label': f'{n} high-risk fraud flag(s) pending',
                                'url': '/admin/fraud-queue'})
    if open_reports:
        attention_items.append({'severity': 'MEDIUM',
                                'label': f'{open_reports} unresolved listing report(s)',
                                'url': '/admin/reports'})
    if pending_mod:
        attention_items.append({'severity': 'LOW',
                                'label': f'{pending_mod} listing(s) awaiting moderation',
                                'url': '/admin/listings'})
    if failed_jobs:
        attention_items.append({'severity': 'MEDIUM',
                                'label': f'{failed_jobs} failed background job(s)',
                                'url': '/admin'})
    if failed_emails_24h >= 3:
        attention_items.append({'severity': 'LOW',
                                'label': f'{failed_emails_24h} email failure(s) in last 24 h',
                                'url': '/admin/notifications'})

    return render_template(
        'admin_operations.html',
        new_users_today=new_users_today,
        new_listings_today=new_listings_today,
        active_listings=active_listings,
        pending_mod=pending_mod,
        open_reports=open_reports,
        high_risk_flags=high_risk_flags,
        failed_jobs=failed_jobs,
        failed_emails_24h=failed_emails_24h,
        attention_items=attention_items,
    )


@app.route("/api/admin/copilot/chat", methods=["POST"])
@require_admin
def api_admin_copilot_chat():
    """Admin-only AI Operations copilot chat endpoint — Phase L.

    Accepts JSON: { message, history, page_context }
    Returns JSON: { reply, nav_links, tokens_in, tokens_out, error, rate_limited }

    Security:
    - require_admin enforced (double-checked inside run_admin_copilot)
    - CSRF enforced via standard Flask-WTF before_request
    - No raw DB access exposed to the AI model
    - User-generated content delimited in system prompt
    - Admin AI usage logged (AIUsageLog tool_name='admin_copilot')
    """
    import time as _time
    data = request.get_json(silent=True) or {}

    # Sanitise inputs
    message      = str(data.get('message', ''))[:1200]
    history      = data.get('history', [])
    page_context = str(data.get('page_context', ''))[:80]

    if not isinstance(history, list):
        history = []

    # Log this admin AI usage
    t0 = _time.time()
    try:
        from ai.admin_copilot import run_admin_copilot
        result = run_admin_copilot(
            message=message,
            history=history,
            page_context=page_context,
            current_user=current_user,
        )
    except Exception as exc:
        log.error("admin copilot endpoint error: %s", exc)
        result = {
            'reply': 'AI Operations Assistant is temporarily unavailable. All normal admin controls continue to work.',
            'nav_links': [],
            'tokens_in': 0,
            'tokens_out': 0,
            'error': str(exc),
            'rate_limited': False,
        }

    elapsed_ms = int((_time.time() - t0) * 1000)

    # Persist usage log
    try:
        from models import AIUsageLog
        log_entry = AIUsageLog(
            user_id=current_user.id,
            tool_name='admin_copilot',
            success=not bool(result.get('error')),
            response_ms=elapsed_ms,
        )
        db.session.add(log_entry)
        db.session.commit()
    except Exception:
        db.session.rollback()

    return jsonify({
        'reply':        result.get('reply', ''),
        'nav_links':    result.get('nav_links', []),
        'tokens_in':    result.get('tokens_in', 0),
        'tokens_out':   result.get('tokens_out', 0),
        'error':        result.get('error'),
        'rate_limited': result.get('rate_limited', False),
    })


# ══════════════════════════════════════════════════════════════════════════════
# PHASE G — AI MARKETPLACE COPILOT
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/copilot/chat", methods=["POST"])
def copilot_chat():
    """Main Copilot endpoint.  Read-only.  No write/delete/payment actions allowed.

    Request JSON:
      { "message": str, "history": [{role, content}, ...], "context": {page_type, listing_id} }

    Response JSON:
      { "reply": str, "cards": [...], "nav_links": [...], "error": str|null }
    """
    import time as _time
    import hashlib
    from models import CopilotSession

    t0 = _time.time()
    # Use remote_addr, which ProxyFix has already normalised using the trusted
    # proxy chain.  Never read X-Forwarded-For here: callers can spoof it to
    # bypass per-IP rate limits or inject arbitrary strings into the key store.
    client_ip = request.remote_addr or "unknown"
    ip_hash   = hashlib.sha256(client_ip.encode()).hexdigest()[:16]

    # CSRF validation — the frontend sends X-CSRFToken on every POST.
    # A cross-site attacker cannot read or set this header, so this prevents
    # third-party pages from silently consuming rate quota and API spend.
    try:
        from flask_wtf.csrf import validate_csrf, ValidationError as _CSRFValidationError
        _csrf_token = request.headers.get("X-CSRFToken", "")
        validate_csrf(_csrf_token)
    except Exception:
        return jsonify({"reply": "", "cards": [], "nav_links": [], "error": "Invalid request."}), 403

    data = request.get_json(silent=True) or {}
    message  = (data.get("message") or "").strip()[:800]
    history  = (data.get("history") or [])[:16]   # cap at 16 items before copilot trims further
    context  = data.get("context") or {}

    # Sanitise context — only accept known safe keys
    safe_context = {
        "page_type":  str(context.get("page_type") or "general")[:50],
        "listing_id": int(context["listing_id"]) if context.get("listing_id") and str(context.get("listing_id", "")).isdigit() else None,
    }

    if not message:
        return jsonify({"reply": "Please ask me something!", "cards": [], "nav_links": [], "error": None})

    try:
        from ai.copilot import run_copilot
        result = run_copilot(
            message=message,
            history=history,
            context=safe_context,
            current_user=current_user,
            client_ip=client_ip,
        )
    except Exception as e:
        app.logger.error("copilot_chat unhandled error: %s", e)
        result = {
            "reply": "Ask JHE Haul is temporarily unavailable. You can continue using marketplace search and navigation.",
            "cards": [], "nav_links": [], "tokens_in": 0, "tokens_out": 0,
            "error": str(e), "rate_limited": False,
        }

    elapsed_ms = int((_time.time() - t0) * 1000)

    # Log analytics (no private content stored)
    try:
        log_entry = CopilotSession(
            user_id=current_user.id if current_user.is_authenticated else None,
            ip_hash=ip_hash,
            page_type=safe_context.get("page_type"),
            success=not bool(result.get("error")) and not result.get("rate_limited"),
            rate_limited=result.get("rate_limited", False),
            error_type=str(result.get("error") or "")[:100] or None,
            tokens_in=result.get("tokens_in", 0),
            tokens_out=result.get("tokens_out", 0),
            response_ms=elapsed_ms,
        )
        db.session.add(log_entry)
        db.session.commit()
    except Exception as e:
        app.logger.warning("copilot analytics log failed: %s", e)
        try:
            db.session.rollback()
        except Exception:
            pass

    return jsonify({
        "reply":      result.get("reply", ""),
        "cards":      result.get("cards", []),
        "nav_links":  result.get("nav_links", []),
        "error":      result.get("error"),
    })


@app.route("/api/copilot/action/execute", methods=["POST"])
@require_login
def copilot_action_execute():
    """Execute a Copilot action that the user has explicitly confirmed.

    Request JSON:
      { "action_type": str, "params": {...}, "cancelled": bool }

    Response JSON:
      { "success": bool, "message": str, "nav_links": [...], "error": str|null }
    """
    import time as _time
    from models import CopilotActionLog

    t0 = _time.time()
    data = request.get_json(silent=True) or {}
    action_type = (data.get("action_type") or "").strip()[:50]
    params      = data.get("params") or {}
    cancelled   = bool(data.get("cancelled", False))

    # Whitelist action types
    from ai.copilot_actions import execute_action, EXECUTE_ACTIONS
    if action_type not in EXECUTE_ACTIONS:
        return jsonify({
            "success": False,
            "message": f"Action '{action_type}' is not available.",
            "nav_links": [],
            "error": "unknown_action",
        }), 400

    log_entry = CopilotActionLog(
        user_id=current_user.id,
        action_type=action_type,
        listing_id=int(params.get("listing_id")) if params.get("listing_id") else None,
        field_name=params.get("field"),
        confirmed=not cancelled,
        cancelled=cancelled,
    )

    if cancelled:
        # User pressed Cancel — log and return without executing
        db.session.add(log_entry)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return jsonify({"success": True, "message": "Action cancelled.", "nav_links": [], "error": None})

    try:
        result = execute_action(action_type, params, current_user)
    except Exception as e:
        app.logger.error("copilot_action_execute error: %s", e)
        result = {"success": False, "message": "Something went wrong. Please use the marketplace UI instead."}

    log_entry.success = result.get("success", False)
    log_entry.error_message = (str(result.get("message", "")) if not result.get("success") else None)
    db.session.add(log_entry)
    try:
        db.session.commit()
    except Exception as e:
        app.logger.warning("copilot action log failed: %s", e)
        try:
            db.session.rollback()
        except Exception:
            pass

    return jsonify({
        "success":   result.get("success", False),
        "message":   result.get("message", ""),
        "nav_links": result.get("nav_links", []),
        "error":     None if result.get("success") else result.get("message", "Unknown error"),
    })


@app.route("/api/seller/insights")
@require_login
def seller_insights_api():
    """Return seller intelligence JSON for the current authenticated seller.
    Used by the Seller Dashboard AJAX load and the Copilot overview tool.
    Result is cached 5 minutes per seller server-side.
    """
    from ai.seller_intelligence import get_seller_overview
    try:
        data = get_seller_overview(current_user.id)
        if "error" in data:
            return jsonify({"error": data["error"]}), 500
        return jsonify(data)
    except Exception as e:
        app.logger.error("seller_insights_api error: %s", e)
        return jsonify({"error": "Could not load seller insights."}), 500


@app.route("/api/seller/insights/listing/<int:listing_id>")
@require_login
def seller_listing_insights_api(listing_id):
    """Return per-listing seller intelligence. Enforces ownership."""
    from ai.seller_intelligence import get_listing_intel
    try:
        data = get_listing_intel(listing_id, current_user.id)
        if "error" in data:
            return jsonify({"error": data["error"]}), 403 if "permission" in data["error"] else 404
        return jsonify(data)
    except Exception as e:
        app.logger.error("seller_listing_insights_api error: %s", e)
        return jsonify({"error": "Could not load listing insights."}), 500


# ── Phase J: Admin Fraud & Safety Queue ──────────────────────────────────────

@app.route("/admin/fraud-queue")
@require_admin
def admin_fraud_queue():
    """Admin fraud & safety review queue with risk-level filtering."""
    from models import FraudFlag
    from sqlalchemy import case as sa_case

    status_filter = request.args.get('status', '').strip()
    risk_filter   = request.args.get('risk',   '').strip()
    q             = request.args.get('q',      '').strip()
    page          = max(1, int(request.args.get('page', 1) or 1))
    per_page      = 20

    query = FraudFlag.query
    if status_filter:
        query = query.filter(FraudFlag.status == status_filter)
    if risk_filter:
        query = query.filter(FraudFlag.risk_level == risk_filter)
    if q:
        from models import User as _U, Listing as _L
        uid_hits = [u.id for u in _U.query.filter(
            db.or_(_U.email.ilike(f'%{q}%'), _U.first_name.ilike(f'%{q}%'), _U.last_name.ilike(f'%{q}%'))
        ).limit(30)]
        lid_hits = [l.id for l in _L.query.filter(_L.title.ilike(f'%{q}%')).limit(30)]
        query = query.filter(db.or_(
            FraudFlag.user_id.in_(uid_hits),
            FraudFlag.listing_id.in_(lid_hits),
        ))

    risk_order = sa_case({'CRITICAL': 4, 'HIGH': 3, 'MEDIUM': 2, 'LOW': 1}, value=FraudFlag.risk_level)
    query = query.order_by(risk_order.desc(), FraudFlag.created_at.desc())

    total       = query.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    flags       = query.offset((page - 1) * per_page).limit(per_page).all()

    open_q = FraudFlag.status.in_(['pending', 'reviewing'])
    pending_count  = FraudFlag.query.filter_by(status='pending').count()
    critical_count = FraudFlag.query.filter(open_q, FraudFlag.risk_level == 'CRITICAL').count()
    high_count     = FraudFlag.query.filter(open_q, FraudFlag.risk_level == 'HIGH').count()
    medium_count   = FraudFlag.query.filter(open_q, FraudFlag.risk_level == 'MEDIUM').count()

    return render_template('admin_fraud_queue.html',
        flags=flags, page=page, total_pages=total_pages,
        status_filter=status_filter, risk_filter=risk_filter, q=q,
        pending_count=pending_count, critical_count=critical_count,
        high_count=high_count, medium_count=medium_count,
    )


@app.route("/admin/fraud-queue/<int:flag_id>")
@require_admin
def admin_fraud_flag_detail(flag_id):
    """Detail view for one fraud flag including moderation history."""
    from models import FraudFlag, ModerationAuditLog
    flag = FraudFlag.query.get_or_404(flag_id)
    mod_logs = []
    filters = []
    if flag.user_id:
        filters.append(db.and_(
            ModerationAuditLog.target_type == 'user',
            ModerationAuditLog.target_id == str(flag.user_id),
        ))
    if flag.listing_id:
        filters.append(db.and_(
            ModerationAuditLog.target_type == 'listing',
            ModerationAuditLog.target_id == str(flag.listing_id),
        ))
    if filters:
        mod_logs = (ModerationAuditLog.query
                    .filter(db.or_(*filters))
                    .order_by(ModerationAuditLog.created_at.desc())
                    .limit(20).all())
    return render_template('admin_fraud_detail.html', flag=flag, mod_logs=mod_logs)


@app.route("/admin/fraud-queue/<int:flag_id>/action", methods=["POST"])
@require_admin
def admin_fraud_flag_action(flag_id):
    """Handle admin actions on a fraud flag."""
    from models import FraudFlag
    _check_listing_csrf()
    flag   = FraudFlag.query.get_or_404(flag_id)
    action = request.form.get('action', '').strip()
    note   = request.form.get('note',   '').strip()
    now    = datetime.now()

    if action == 'dismiss':
        flag.status = 'dismissed'
        flag.resolved_at = now; flag.resolved_by_id = current_user.id
        _log_mod_action('dismiss', 'fraud_flag', flag.id, notes=f"Flag #{flag.id} dismissed")
        flash('Flag dismissed.', 'success')

    elif action == 'false_positive':
        flag.status = 'false_positive'; flag.is_false_positive = True
        flag.resolved_at = now; flag.resolved_by_id = current_user.id
        _log_mod_action('dismiss', 'fraud_flag', flag.id, notes=f"Flag #{flag.id} marked false positive")
        flash('Marked as false positive.', 'success')

    elif action == 'mark_reviewing':
        flag.status = 'reviewing'
        _log_mod_action('flag_investigate', 'fraud_flag', flag.id, notes=f"Flag #{flag.id} under review")
        flash('Flag moved to Reviewing.', 'info')

    elif action == 'reopen':
        flag.status = 'pending'; flag.is_false_positive = False
        flag.resolved_at = None; flag.resolved_by_id = None
        _log_mod_action('flag_investigate', 'fraud_flag', flag.id, notes=f"Flag #{flag.id} reopened")
        flash('Flag reopened.', 'info')

    elif action == 'add_note':
        if note:
            stamp  = now.strftime('%b %-d')
            prefix = flag.admin_note or ''
            flag.admin_note = (prefix + f"\n[{stamp} {current_user.first_name or '?'}]: {note}").strip()
            _log_mod_action('add_note', 'fraud_flag', flag.id, notes=note[:200])
            flash('Note added.', 'success')

    elif action == 'warn_user' and flag.flagged_user:
        u = flag.flagged_user
        u.marketplace_warning_count = (u.marketplace_warning_count or 0) + 1
        flag.status = 'actioned'; flag.resolved_at = now; flag.resolved_by_id = current_user.id
        _log_mod_action('warn', 'user', u.id, notes=f"Warning via fraud flag #{flag.id}")
        flash(f'Warning issued to {u.first_name or u.email or u.id}.', 'warning')

    elif action == 'suspend_listing' and flag.listing:
        lst = flag.listing
        lst.moderation_status = 'flagged'; lst.status = 'removed'
        try:
            _expire_offers_and_notify(lst.id, lst.title)
            _deactivate_stale_gallery_pins()
        except Exception as _ge:
            app.logger.warning("fraud_flag suspend_listing cleanup: %s", _ge)
        flag.status = 'actioned'; flag.resolved_at = now; flag.resolved_by_id = current_user.id
        _log_mod_action('suspend_listing', 'listing', lst.id, notes=f"Suspended via fraud flag #{flag.id}")
        flash(f'Listing "{lst.title[:40]}" suspended.', 'warning')

    elif action == 'remove_listing' and flag.listing:
        lst = flag.listing
        lst.moderation_status = 'removed'; lst.status = 'removed'
        try:
            _expire_offers_and_notify(lst.id, lst.title)
            _deactivate_stale_gallery_pins()
        except Exception as _ge:
            app.logger.warning("fraud_flag remove_listing cleanup: %s", _ge)
        flag.status = 'actioned'; flag.resolved_at = now; flag.resolved_by_id = current_user.id
        _log_mod_action('remove_listing', 'listing', lst.id, notes=f"Removed via fraud flag #{flag.id}")
        flash(f'Listing "{lst.title[:40]}" removed.', 'danger')

    elif action == 'restore_listing' and flag.listing:
        lst = flag.listing
        lst.moderation_status = 'approved'; lst.status = 'active'
        try:
            _reactivate_gallery_pins(lst.id)
        except Exception as _ge:
            app.logger.warning("fraud_flag restore_listing: %s", _ge)
        flag.status = 'dismissed'; flag.is_false_positive = True
        flag.resolved_at = now; flag.resolved_by_id = current_user.id
        _log_mod_action('restore_listing', 'listing', lst.id, notes=f"Restored (false positive) via fraud flag #{flag.id}")
        flash(f'Listing "{lst.title[:40]}" restored.', 'success')

    elif action == 'suspend_account' and flag.flagged_user:
        u = flag.flagged_user; u.is_suspended = True
        flag.status = 'actioned'; flag.resolved_at = now; flag.resolved_by_id = current_user.id
        _log_mod_action('suspend', 'user', u.id, notes=f"Suspended via fraud flag #{flag.id}")
        flash(f'Account for {u.email or u.id} suspended.', 'warning')

    elif action == 'ban_account' and flag.flagged_user:
        u = flag.flagged_user; u.is_banned = True; u.is_suspended = True
        flag.status = 'actioned'; flag.resolved_at = now; flag.resolved_by_id = current_user.id
        _log_mod_action('ban', 'user', u.id, notes=f"Banned via fraud flag #{flag.id}")
        flash(f'Account for {u.email or u.id} permanently banned.', 'danger')

    else:
        flash('Unknown or invalid action.', 'warning')
        return redirect(url_for('admin_fraud_flag_detail', flag_id=flag_id))

    db.session.commit()
    return redirect(url_for('admin_fraud_flag_detail', flag_id=flag_id))


@app.route("/admin/fraud-queue/scan", methods=["POST"])
@require_admin
def admin_fraud_manual_scan():
    """Trigger an immediate (synchronous) fraud scan for a listing or user."""
    _check_listing_csrf()
    listing_id = request.form.get('listing_id', '').strip()
    user_id    = request.form.get('user_id',    '').strip()
    if not listing_id and not user_id:
        flash('Provide a listing ID or user ID to scan.', 'warning')
        return redirect(url_for('admin_fraud_queue'))
    try:
        from ai.fraud_safety import calculate_risk_and_flag
        result = calculate_risk_and_flag(
            listing_id=int(listing_id) if listing_id else None,
            user_id=str(user_id)       if user_id    else None,
            trigger='manual',
        )
        if 'error' in result:
            flash(f'Scan error: {result["error"]}', 'danger')
        elif 'flag_id' in result:
            risk = result.get('risk_level', '?')
            sigs = len(result.get('signals', []))
            flash(f'Scan complete: {risk} risk, {sigs} signal(s). Flag #{result["flag_id"]} created/updated.',
                  'warning' if risk in ('HIGH', 'CRITICAL') else 'info')
            return redirect(url_for('admin_fraud_flag_detail', flag_id=result['flag_id']))
        else:
            risk = result.get('risk_level', 'LOW')
            flash(f'Scan complete: {risk} risk — no flag created (below MEDIUM threshold).', 'success')
    except Exception as _e:
        app.logger.error("admin_fraud_manual_scan error: %s", _e)
        flash(f'Scan failed: {_e}', 'danger')
    return redirect(url_for('admin_fraud_queue'))


@app.route("/admin/copilot-analytics")
@require_admin
def admin_copilot_analytics():
    """Admin view: Copilot usage analytics (no private conversation content)."""
    from models import CopilotSession
    from sqlalchemy import func
    total       = CopilotSession.query.count()
    successful  = CopilotSession.query.filter_by(success=True).count()
    rate_limited= CopilotSession.query.filter_by(rate_limited=True).count()
    errored     = CopilotSession.query.filter(CopilotSession.error_type.isnot(None)).count()
    avg_ms_row  = db.session.query(func.avg(CopilotSession.response_ms)).scalar()
    avg_ms      = int(avg_ms_row or 0)
    total_tok_in  = db.session.query(func.sum(CopilotSession.tokens_in)).scalar() or 0
    total_tok_out = db.session.query(func.sum(CopilotSession.tokens_out)).scalar() or 0
    # Approximate cost: gpt-4o-mini $0.15/1M in, $0.60/1M out
    approx_cost = (total_tok_in / 1_000_000 * 0.15) + (total_tok_out / 1_000_000 * 0.60)
    page_counts = (db.session.query(CopilotSession.page_type, func.count())
                   .group_by(CopilotSession.page_type)
                   .order_by(func.count().desc()).all())
    recent = (CopilotSession.query
              .order_by(CopilotSession.created_at.desc())
              .limit(50).all())
    return render_template(
        "admin_copilot_analytics.html",
        total=total, successful=successful, rate_limited=rate_limited,
        errored=errored, avg_ms=avg_ms,
        total_tok_in=total_tok_in, total_tok_out=total_tok_out,
        approx_cost=round(approx_cost, 4),
        page_counts=page_counts,
        recent=recent,
    )
