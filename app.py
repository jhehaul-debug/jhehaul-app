import os
import logging

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET") or os.environ.get("SECRET_KEY") or "dev-secret-change-me"
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ---- Database ----
database_url = os.environ.get("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
if not database_url:
    database_url = "sqlite:////tmp/jhehaul.db"

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}

# Import db from models (single source of truth) and bind to this app
from models import db  # noqa: E402
db.init_app(app)

# ---- Upload folder ----
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# ---- Stripe payment links ----
PAY_LINK_UNDER_150 = os.environ.get("PAY_LINK_UNDER_150", "")
PAY_LINK_150_300 = os.environ.get("PAY_LINK_150_300", "")
PAY_LINK_300_500 = os.environ.get("PAY_LINK_OVER_300", "")
PAY_LINK_OVER_500 = os.environ.get("PAY_LINK_OVER_500", "")


def choose_pay_link(accepted_quote):
    try:
        q = float(accepted_quote or 0)
    except Exception:
        q = 0
    if q < 150:
        return PAY_LINK_UNDER_150
    elif q < 300:
        return PAY_LINK_150_300
    elif q < 500:
        return PAY_LINK_300_500
    else:
        return PAY_LINK_OVER_500


# ---- Initialize tables and load ZIP codes ----
with app.app_context():
    import models as _models  # noqa: F401
    db.create_all()
    logging.info("Database tables created")

    try:
        from models import ZipCode
        from load_zips import load_minnesota_zips
        count = ZipCode.query.count()
        if count == 0:
            logging.info("Loading ZIP codes into database...")
            added = load_minnesota_zips(db, ZipCode)
            logging.info(f"Loaded {added} ZIP codes")
        else:
            logging.info(f"ZIP codes already loaded: {count}")
    except Exception as e:
        logging.exception("ZIP code load skipped: %s", e)

    try:
        from models import User
        admin_email = os.environ.get("ADMIN_EMAIL", "incorporateiq@gmail.com")
        admin = User.query.filter_by(email=admin_email).first()
        if admin and not admin.is_admin:
            admin.is_admin = True
            db.session.commit()
            logging.info("Admin flag restored for %s", admin_email)
    except Exception as e:
        logging.exception("Admin flag restore skipped: %s", e)
