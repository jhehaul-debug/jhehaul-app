import os
import uuid
from datetime import datetime
from functools import wraps
from flask import session, redirect, url_for, request, send_from_directory, render_template
from werkzeug.utils import secure_filename
from flask_login import current_user

from app import app, db, UPLOAD_FOLDER, choose_pay_link
from replit_auth import require_login, make_replit_blueprint
from models import User, Job, JobPhoto, Bid
from email_service import notify_customer_new_bid, notify_hauler_bid_accepted

def require_role(role):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                session["next_url"] = request.url
                return redirect(url_for('replit_auth.login'))
            if not current_user.user_type:
                return redirect(url_for('choose_role'))
            if current_user.user_type != role:
                return render_template('403.html'), 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator

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
        if not current_user.user_type:
            return redirect(url_for('choose_role'))
        if current_user.user_type == 'customer':
            return redirect(url_for('customer_jobs'))
        else:
            return redirect(url_for('hauler_jobs'))
    return render_template('landing.html')

@app.route("/choose-role")
@require_login
def choose_role():
    if current_user.user_type:
        if current_user.user_type == 'customer':
            return redirect(url_for('customer_jobs'))
        else:
            return redirect(url_for('hauler_jobs'))
    return render_template('choose_role.html')

@app.route("/set-role", methods=["POST"])
@require_login
def set_role():
    role = request.form.get("role")
    if role in ['customer', 'hauler']:
        current_user.user_type = role
        db.session.commit()
    if role == 'customer':
        return redirect(url_for('customer_jobs'))
    return redirect(url_for('hauler_jobs'))

@app.route("/customer/new", methods=["GET"])
@require_role('customer')
def customer_new():
    return render_template('customer_new.html')

@app.route("/customer/create", methods=["POST"])
@require_role('customer')
def customer_create():
    customer_name = request.form.get("customer_name", "").strip()
    customer_phone = request.form.get("customer_phone", "").strip()
    pickup_address = request.form.get("pickup_address", "").strip()
    job_description = request.form.get("job_description", "").strip()

    if not customer_name or not pickup_address or not job_description:
        return "Missing required fields", 400

    job = Job(
        customer_id=current_user.id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        pickup_address=pickup_address,
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
    return redirect(url_for('customer_job_detail', job_id=job_id))

@app.route("/payment_success/<int:job_id>")
@require_role('customer')
def payment_success(job_id):
    job = Job.query.get_or_404(job_id)
    if job.customer_id != current_user.id:
        return "Access denied", 403
    return render_template('payment_success.html', job=job)

@app.route("/hauler/jobs")
@require_role('hauler')
def hauler_jobs():
    jobs = Job.query.filter_by(status='open').order_by(Job.id.desc()).all()
    return render_template('hauler_jobs.html', jobs=jobs)

@app.route("/hauler/bid/<int:job_id>", methods=["GET"])
@require_role('hauler')
def hauler_bid_form(job_id):
    job = Job.query.get_or_404(job_id)
    return render_template('hauler_bid.html', job=job)

@app.route("/hauler/bid/<int:job_id>", methods=["POST"])
@require_role('hauler')
def hauler_bid_submit(job_id):
    hauler_name = request.form.get("hauler_name", "").strip()
    hauler_phone = request.form.get("hauler_phone", "").strip()
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
    phone = request.form.get("phone", "").strip()
    
    current_user.first_name = first_name
    current_user.last_name = last_name
    current_user.phone = phone
    db.session.commit()
    
    return redirect(url_for('profile'))
