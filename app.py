import os
import logging

from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Column, Integer, String, Text

from werkzeug.security import generate_password_hash, check_password_hash 
logging.basicConfig(level=logging.INFO)
from flask_login import LoginManager, UserMixin, login_required, current_user, login_user, logout_user

from werkzeug.middleware.proxy_fix import ProxyFix
class Base(DeclarativeBase):
    pass
  # customer or hauler
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")#
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

# Secret key

# ---- DATABASE (ONLY ONE CONFIGURATION) ----
database_url = os.environ.get("DATABASE_URL")

# DigitalOcean/Heroku sometimes provide "postgres://"
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

# Fallback for local/dev (keeps app from crashing)
if not database_url:
    database_url = "sqlite:////tmp/jhehaul.db"

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app, model_class=Base)
from models import User, Job

@login_manager.user_loader
def load_user(user_id):
        return db.session.get(User, user_id)
# ---- Startup DB init (safe) ----
try:
    with app.app_context():
          # make sure your models registe
        db.create_all()
        logging.info("Database initialized.")
except Exception as e:
    logging.exception("Startup DB init skipped: %s", e)

# ---- Routes ----



@app.route("/hauler")
def hauler_dashboard():
        from models import Job

        jobs = db.session.query(Job).filter(
            Job.status.in_(["open", "bidding"])
        ).order_by(Job.id.desc()).all()

        return render_template(
            "hauler_dashboard.html",
            current_user=current_user,
            jobs=jobs
        )

@app.route("/hauler/jobs")
def hauler_jobs():
        from models import Job

        jobs = db.session.query(Job).filter(
            Job.status.in_(["open", "bidding"])
        ).order_by(Job.id.desc()).all()

        return render_template(
            "hauler_jobs.html",
            current_user=current_user,
            jobs=jobs
        )

@app.route("/")
def home():
    return render_template("marketplace.html", current_user=current_user)

@app.route("/about")
def about():
    return render_template("about.html", current_user=current_user)
@app.route("/customer")
def customer_dashboard():
    return render_template("customer_dashboard.html", current_user=current_user)
@app.route("/customer/new")
def customer_new():
    return render_template("customer_new.html", current_user=current_user)

@app.route("/customer/terms")
def customer_terms():
    return render_template("customer_terms.html", current_user=current_user)
@app.route("/customer/create", methods=["POST"])
def create_customer_job():
    from models import Job

    new_job = Job(

        customer_name=request.form.get("customer_name"),
        customer_phone=request.form.get("customer_phone"),
        pickup_address=request.form.get("pickup_address"),
        pickup_zip=request.form.get("pickup_zip"),
        job_description=request.form.get("job_description"),
        preferred_date=request.form.get("preferred_date"),
        preferred_time=request.form.get("preferred_time"),
        status="open"
    )

    db.session.add(new_job)
    db.session.commit()

    return redirect(url_for("customer_jobs"))


@app.route("/customer/jobs")
def customer_jobs():
    from models import Job

    jobs = db.session.query(Job).order_by(Job.id.desc()).all()

    return render_template(
        "customer_jobs.html",
        current_user=current_user,
        jobs=jobs
    )
@app.route("/customer/jobs/<int:job_id>")
def customer_job_detail(job_id):
        from models import Job

        job = db.session.query(Job).get(job_id)
        if not job:
            return "Job not found", 404

        bids = []
        pay_link = None
        checkout_over500_url = None

        return render_template(
            "customer_job_detail.html",
            current_user=current_user,
            job=job,
            bids=bids,
            pay_link=pay_link,
            checkout_over500_url=checkout_over500_url
        )
    # =========================
    # CUSTOMER ACTION ROUTES
    # =========================

@app.route("/customer/upload-photos/<int:job_id>", methods=["POST"])
def customer_upload_photos(job_id):
        # TODO: handle file uploads later
        return redirect(url_for("customer_job_detail", job_id=job_id))


@app.route("/customer/complete-job/<int:job_id>", methods=["POST"])
def customer_complete_job(job_id):
        from models import Job

        job = db.session.query(Job).get(job_id)
        if job:
            job.status = "completed"
            db.session.commit()

        return redirect(url_for("customer_job_detail", job_id=job_id))


@app.route("/customer/cancel-job/<int:job_id>", methods=["POST"])
def customer_cancel_job(job_id):
        from models import Job

        job = db.session.query(Job).get(job_id)
        if job:
            job.status = "cancelled"
            db.session.commit()

        return redirect(url_for("customer_jobs"))


@app.route("/customer/review/<int:job_id>")
def customer_review(job_id):
        # placeholder page for now
        return f"Review page for job {job_id}"


@app.route("/customer/accept-bid/<int:bid_id>", methods=["POST"])
def customer_accept_bid(bid_id):
        # placeholder logic
        return redirect(url_for("customer_jobs"))
@app.route("/hauler/earnings")
def hauler_earnings():
    return render_template("hauler_earnings.html", current_user=current_user)

@app.route("/profile")
def profile():
    return render_template("profile.html", current_user=current_user)
@app.route("/login", methods=["GET", "POST"])
def login():
        from models import User

        if request.method == "POST":
            email = request.form.get("email")

            user = db.session.query(User).filter_by(email=email).first()

            if user:
                login_user(user)
                return redirect(url_for("choose_role"))

        return render_template("login.html")


def choose_role():
        return render_template(
            "choose_role.html",
            current_user=current_user,
            invited_role=None
        )
@app.route("/hauler-agreement")
@login_required
def hauler_agreement():
    return "<h2>Hauler Agreement</h2><p>You agree to terms.</p>"


@app.route("/set-role", methods=["POST"])
@login_required
def set_role():
    role = request.form.get("role")

    if role == "customer":
        return redirect(url_for("customer_dashboard"))

    if role == "hauler":
        if not request.form.get("agree_terms"):
            return redirect(url_for("choose_role"))

        current_user.user_type = "hauler"
        db.session.commit()
        return redirect(url_for("hauler_dashboard"))

    return redirect(url_for("choose_role"))
@app.route("/make-me-admin")
@login_required
def make_me_admin():
        current_user.is_admin = True
        db.session.commit()
        return "You are now admin."

@app.route("/create-user")
def create_user():
        from models import User

        email = "jhehaul@gmail.com"

        existing = db.session.query(User).filter_by(email=email).first()
        if existing:
            return "User already exists."

        new_user = User(
            id=email,
            email=email,
            first_name="Admin",
            last_name="User",
            is_admin=False  # IMPORTANT
        )

        db.session.add(new_user)
        db.session.commit()

        return "User created successfully."

@app.route("/health")
def health():
    return "ok", 200