import os
import logging

from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase
from werkzeug.middleware.proxy_fix import ProxyFix

logging.basicConfig(level=logging.INFO)

class Base(DeclarativeBase):
    pass

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Secret key
app.config["SECRET_KEY"] = os.environ.get("SESSION_SECRET", "dev-secret")

# ---- DATABASE (ONLY ONE CONFIGURATION) ----
database_url = os.environ.get("DATABASE_URL")

# DigitalOcean/Heroku sometimes provide "postgres://"
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

# Fallback for local/dev (keeps app from crashing)
if not database_url:
    database_url = "sqlite:///jhehaul.db"

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app, model_class=Base)

# ---- Startup DB init (safe) ----
try:
    with app.app_context():
        import models  # make sure your models register
        db.create_all()
        logging.info("Database initialized.")
except Exception as e:
    logging.exception("Startup DB init skipped: %s", e)

# ---- Routes ----


@app.route("/")
def home():
    return render_template("marketplace.html")

@app.route("/marketplace")
def marketplace():
    return render_template("marketplace.html")

@app.route("/customer")
def customer_dashboard():
    return render_template("customer_dashboard.html")

@app.route("/hauler")
def hauler_dashboard():
    return render_template("hauler_dashboard.html")
@app.route("/health")
def health():
    return "ok", 200


# Local run only (DigitalOcean uses gunicorn)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)