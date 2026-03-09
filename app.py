import os
import logging

from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_login import LoginManager, UserMixin, login_required, current_user, login_user, logout_user
from sqlalchemy import Column, Integer, String, Text
from werkzeug.security import generate_password_hash, check_password_hash 
logging.basicConfig(level=logging.INFO)

class Base(DeclarativeBase):
    pass

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
# Secret key
app.config["SECRET_KEY"] = os.environ.get("SESSION_SECRET", "dev-secret")

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


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))
# ---- Startup DB init (safe) ----
try:
    with app.app_context():
          # make sure your models registe
        db.create_all()
        logging.info("Database initialized.")
except Exception as e:
    logging.exception("Startup DB init skipped: %s", e)

# ---- Routes ----

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":

        email = request.form.get("email")
        password = request.form.get("password")

        user = db.session.query(User).filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            login_user(user)

            if user.user_type == "customer":
                return redirect(url_for("customer_dashboard"))
            else:
                return redirect(url_for("hauler_dashboard"))

        return render_template("login.html", error="Invalid login", current_user=current_user)

    return render_template("login.html", current_user=current_user)
@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        email = request.form.get("email")
        password = request.form.get("password")
        user_type = request.form.get("user_type")

        new_user = User(
            email=email,
            password_hash=generate_password_hash(password),
            user_type=user_type
        )

        db.session.add(new_user)
        db.session.commit()

        login_user(new_user)

        if user_type == "customer":
            return redirect(url_for("customer_dashboard"))
        else:
            return redirect(url_for("hauler_dashboard"))

    return render_template("register.html", current_user=current_user)
@app.route("/marketplace")
def marketplace():
    return render_template("marketplace.html", current_user=current_user)
@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("marketplace"))
@app.route("/customer")
@login_required
def customer_dashboard():
    return render_template("customer_dashboard.html", current_user=current_user)

@app.route("/hauler")
@login_required
def hauler_dashboard():
    jobs = []
    return render_template("hauler_dashboard.html", current_user=current_user, jobs=jobs)

@app.route("/hauler/jobs")
def hauler_jobs():
    return render_template("hauler_jobs.html", current_user=current_user)

@app.route("/")
def home():
    return render_template("marketplace.html", current_user=current_user)

@app.route("/about")
def about():
    return render_template("about.html", current_user=current_user)

@app.route("/customer/new")
def customer_new():
    return render_template("customer_new.html", current_user=current_user)

@app.route("/customer/terms")
def customer_terms():
    return render_template("customer_terms.html", current_user=current_user)
@app.route("/customer/create", methods=["POST"])
def create_customer_job():
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
    jobs = db.session.query(Job).order_by(Job.id.desc()).all()
    return render_template("customer_jobs.html", current_user=current_user, jobs=jobs)

@app.route("/hauler/earnings")
def hauler_earnings():
    return render_template("hauler_earnings.html", current_user=current_user)

@app.route("/profile")
def profile():
    return render_template("profile.html", current_user=current_user)

@app.route("/admin")
def admin_dashboard():
    return render_template("admin_dashboard.html", current_user=current_user)

@app.route("/health")
def health():
    return "ok", 200