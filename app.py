from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase
import os
from werkzeug.middleware.proxy_fix import ProxyFix
import logging

logging.basicConfig(level=logging.DEBUG)

class Base(DeclarativeBase):
        pass

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SESSION_SECRET", "dev-secret")

# Database configuration (DigitalOcean fix)
# --- DATABASE (ONLY ONE CONFIGURATION) ---

# --- DATABASE (ONLY ONE CONFIGURATION) ---
database_url = os.environ.get("DATABASE_URL")

if database_url:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
else:
    database_url = "sqlite:///jhehaul.db"

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app, model_class=Base)

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

PAY_LINK_UNDER_150 = os.environ.get("PAY_LINK_UNDER_150", "")
PAY_LINK_150_300 = os.environ.get("PAY_LINK_150_300", "")
PAY_LINK_300_500 = os.environ.get("PAY_LINK_OVER_300", "")
PAY_LINK_OVER_500 = os.environ.get("PAY_LINK_OVER_500", "")

def choose_pay_link(accepted_quote):
        try:
            q = float(accepted_quote or 0)
        except:
            q = 0
        if q < 150:
            return PAY_LINK_UNDER_150
        elif q < 300:
            return PAY_LINK_150_300
        elif q < 500:
            return PAY_LINK_300_500
        else:
            return PAY_LINK_OVER_500
    try:
        with app.app_context():
            import models
            db.create_all()
    except Exception as e:
        logging.exception("Startup DB init failed: %s", e)
    logging.info("Database tables created")

from models import User
admin_user = db.session.get(User, '53919193')
if admin_user and not admin_user.is_admin:
            admin_user.is_admin = True
            db.session.commit()
            logging.info("Admin flag restored for admin user")

with app.app_context():
    try:
        db.create_all()
        print("Database initialized")
    except Exception as e:
        print("Database init skipped:", e)

    @app.route("/")
    def home():
        return "JHE HAUL SERVER RUNNING"
@app.route("/health")
def health():
                return "ok", 200
if __name__ == "__main__":
        app.run(host="0.0.0.0", port=8080)