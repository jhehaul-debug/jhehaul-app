import os
import logging

from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_login import LoginManager, UserMixin, login_required, current_user, login_user



logging.basicConfig(level=logging.INFO)

class Base(DeclarativeBase):
    pass

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)
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
from models import User

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))
# ---- Startup DB init (safe) ----
try:
    with app.app_context():
        import models  # make sure your models register
        db.create_all()
        logging.info("Database initialized.")
except Exception as e:
    logging.exception("Startup DB init skipped: %s", e)

# ---- Routes ----



@app.route("/marketplace")
def marketplace():
        return render_template("marketplace.html")

@app.route("/customer")
def customer_dashboard():
        return render_template("customer_dashboard.html", current_user=current_user)

@app.route("/hauler")
def hauler_dashboard():
        return render_template("hauler_dashboard.html", current_user=current_user)

@app.route("/")
def home():
        return render_template("marketplace.html")
@app.route("/about")
def about():
        return render_template("about.html")
@app.route("/health")
def health():
    return "ok", 200


# Local run only (DigitalOcean uses gunicorn