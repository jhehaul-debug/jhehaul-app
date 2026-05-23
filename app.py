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
PAY_LINK_300_500 = os.environ.get("PAY_LINK_300_500", "")
PAY_LINK_OVER_500 = os.environ.get("PAY_LINK_OVER_500", "")


def choose_pay_link(accepted_quote):
    try:
        q = float(accepted_quote or 0)
    except Exception:
        q = 0

    if q < 150:
        var_name, link = "PAY_LINK_UNDER_150", PAY_LINK_UNDER_150
    elif q < 300:
        var_name, link = "PAY_LINK_150_300", PAY_LINK_150_300
    elif q <= 500:
        var_name, link = "PAY_LINK_300_500", PAY_LINK_300_500
    else:
        var_name, link = "PAY_LINK_OVER_500", PAY_LINK_OVER_500

    import logging
    logging.info(
        "choose_pay_link: quote=$%.2f → bracket=%s env_var=%s link_set=%s",
        q, var_name, var_name, bool(link)
    )
    return link


# ---- Timezone filter: UTC naive → America/Chicago ----
def _to_central(dt, fmt='%b %d, %Y'):
    if dt is None:
        return ''
    try:
        from zoneinfo import ZoneInfo
        from datetime import timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ct = dt.astimezone(ZoneInfo('America/Chicago'))
        return ct.strftime(fmt)
    except Exception:
        return dt.strftime(fmt)

app.jinja_env.filters['ct'] = _to_central


# ---- Startup checks ----
_sendgrid_key = os.environ.get("SENDGRID_API_KEY")
if not _sendgrid_key:
    logging.warning(
        "⚠️  SENDGRID_API_KEY is NOT set — email notifications will not be delivered. "
        "Set this environment variable in DigitalOcean App Platform → Settings → Environment Variables."
    )
else:
    logging.info("SendGrid configured (key length: %d)", len(_sendgrid_key))

_admin_email = os.environ.get("ADMIN_EMAIL", "jhehaul@gmail.com")
logging.info("Admin notification email: %s", _admin_email)

_spaces_key = os.environ.get("SPACES_KEY")
if not _spaces_key:
    logging.warning(
        "⚠️  SPACES_KEY is NOT set — uploaded photos will be saved to the LOCAL filesystem only. "
        "On DigitalOcean App Platform this storage is EPHEMERAL: photos will be lost on every deploy or restart. "
        "Set SPACES_KEY, SPACES_SECRET, SPACES_BUCKET (and optionally SPACES_REGION, SPACES_CDN_URL) "
        "in DigitalOcean App Platform → Settings → Environment Variables to enable persistent photo storage."
    )
else:
    logging.info("DigitalOcean Spaces configured for photo storage (bucket: %s)",
                 os.environ.get("SPACES_BUCKET", "unknown"))


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
        from sqlalchemy import text as _text
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS truck_type VARCHAR"))
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS trailer_type VARCHAR"))
        db.session.commit()
        logging.info("Column migration: truck_type/trailer_type ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("ALTER TABLE job_photos ADD COLUMN IF NOT EXISTS storage_url VARCHAR"))
        db.session.execute(_text("ALTER TABLE completion_photos ADD COLUMN IF NOT EXISTS storage_url VARCHAR"))
        db.session.commit()
        logging.info("Column migration: storage_url columns ensured on photo tables")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (storage_url) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("ALTER TABLE job_photos ADD COLUMN IF NOT EXISTS data BYTEA"))
        db.session.execute(_text("ALTER TABLE job_photos ADD COLUMN IF NOT EXISTS content_type VARCHAR(80)"))
        db.session.execute(_text("ALTER TABLE completion_photos ADD COLUMN IF NOT EXISTS data BYTEA"))
        db.session.execute(_text("ALTER TABLE completion_photos ADD COLUMN IF NOT EXISTS content_type VARCHAR(80)"))
        db.session.commit()
        logging.info("Column migration: photo data/content_type columns ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (photo data) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("""
            CREATE TABLE IF NOT EXISTS hauler_service_zips (
                id SERIAL PRIMARY KEY,
                hauler_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                zip_code VARCHAR(5) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(hauler_id, zip_code)
            )
        """))
        db.session.commit()
        logging.info("Table migration: hauler_service_zips ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Table migration (hauler_service_zips) skipped: %s", _e)

    try:
        from models import User
        admin_email = os.environ.get("ADMIN_EMAIL", "jhehaul@gmail.com")
        admin = User.query.filter_by(email=admin_email).first()
        if admin and not admin.is_admin:
            admin.is_admin = True
            db.session.commit()
            logging.info("Admin flag restored for %s", admin_email)
    except Exception as e:
        logging.exception("Admin flag restore skipped: %s", e)
