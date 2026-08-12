import os
import logging

from flask import Flask
from flask_wtf.csrf import generate_csrf  # required; install Flask-WTF from requirements.txt
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

# CSRF helpers — scoped to listing endpoints only (not app-wide).
# Global enforcement requires all existing forms to carry tokens first.
@app.context_processor
def _csrf_context():
    """Make csrf_token() callable in all templates without global enforcement."""
    return dict(csrf_token=generate_csrf)

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
    try:
        db.create_all()
        logging.info("Database tables created")
    except Exception as _db_init_exc:
        # Database is unreachable — the process cannot start.  Alert admin
        # before crashing so the failure doesn't go unnoticed.
        logging.critical(
            "FATAL: db.create_all() failed — database unreachable: %s", _db_init_exc
        )
        try:
            _ALERT_SENTINEL = "/tmp/jhe_health_alert_sent"
            _fd = os.open(_ALERT_SENTINEL, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(_fd)
            # This worker owns the alert slot — send SMS and email.
            _err_msg = f"DB unreachable at startup: {str(_db_init_exc)[:250]}"
            try:
                from sms_service import send_sms as _send_sms
                _admin_phone = os.environ.get("ADMIN_PHONE")
                if _admin_phone:
                    _send_sms(
                        _admin_phone,
                        f"[JHE Haul] FATAL: {_err_msg}",
                        event_type="admin_health_alert",
                    )
            except Exception as _sms_exc:
                logging.error("startup DB-fail SMS error: %s", _sms_exc)
            try:
                from email_service import send_email as _send_email, _html as _email_html
                _admin_email = os.environ.get("ADMIN_EMAIL", "jhehaul@gmail.com")
                _send_email(
                    _admin_email,
                    "[JHE Haul] 🚨 FATAL: Database unreachable at startup",
                    _email_html(
                        "Fatal Startup Failure",
                        "The database was unreachable when the app tried to initialize.",
                        "🚨 Health Alert",
                        "<p>The app cannot start. DigitalOcean will restart the container.</p>"
                        f"<div class='info-box'><pre style='margin:0;white-space:pre-wrap;"
                        f"font-size:0.88rem;color:#b91c1c;'>{_err_msg}</pre></div>",
                    ),
                    event_type="admin_health_alert",
                )
            except Exception as _email_exc:
                logging.error("startup DB-fail email error: %s", _email_exc)
        except FileExistsError:
            # Another worker already sent the alert for this deploy.
            logging.info("DB-fail alert already sent by another worker — skipping duplicate.")
        except Exception as _sentinel_exc:
            logging.error("startup DB-fail sentinel error: %s", _sentinel_exc)
        raise  # Re-raise — DB is required; let the process crash and restart.

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
        from sqlalchemy import text as _text
        db.session.execute(_text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS scheduled_date VARCHAR"))
        db.session.execute(_text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS scheduled_time VARCHAR"))
        db.session.commit()
        logging.info("Column migration: scheduled_date/scheduled_time ensured on jobs table")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (scheduled appointment) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_photo_data BYTEA"))
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_photo_content_type VARCHAR(80)"))
        db.session.commit()
        logging.info("Column migration: profile photo columns ensured on users table")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (profile photo) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS sms_consent BOOLEAN DEFAULT FALSE"))
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS sms_consent_at TIMESTAMP"))
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_verified BOOLEAN DEFAULT FALSE"))
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_verify_code VARCHAR(6)"))
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_verify_sent_at TIMESTAMP"))
        db.session.commit()
        logging.info("Column migration: users SMS consent + phone verification columns ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (users SMS) skipped: %s", _e)

    # gallery_photos table is created portably (SQLite + PostgreSQL) by
    # db.create_all() above via the GalleryPhoto model in models.py.

    # city / zip_code must be migrated before any User query below
    try:
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS city VARCHAR(100)"))
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS zip_code VARCHAR(10)"))
        db.session.commit()
        logging.info("Column migration: users.city / users.zip_code ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (users.city/zip_code) skipped: %s", _e)

    # ── User safety columns (must run before ANY User query below) ───────────
    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE"))
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS age_confirmed BOOLEAN DEFAULT FALSE"))
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS marketplace_warning_count INTEGER DEFAULT 0"))
        # Existing users are pre-confirmed
        db.session.execute(_text("UPDATE users SET age_confirmed = TRUE WHERE age_confirmed IS NOT TRUE"))
        db.session.commit()
        logging.info("Column migration: users safety columns (is_banned, age_confirmed, warning_count) ensured (early)")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (users safety early) skipped: %s", _e)

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

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS expired_at TIMESTAMP"))
        db.session.execute(_text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS reminder_24h_sent BOOLEAN DEFAULT FALSE"))
        db.session.execute(_text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS reminder_48h_sent BOOLEAN DEFAULT FALSE"))
        db.session.execute(_text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS pickup_reminder_sent BOOLEAN DEFAULT FALSE"))
        db.session.commit()
        logging.info("Column migration: job expiry columns ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (job expiry) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("ALTER TABLE notification_logs ADD COLUMN IF NOT EXISTS sg_status_code INTEGER"))
        db.session.execute(_text("ALTER TABLE notification_logs ADD COLUMN IF NOT EXISTS sg_message_id VARCHAR(200)"))
        db.session.commit()
        logging.info("Column migration: notification_logs SendGrid detail columns ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (notification_logs sg columns) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS service_type VARCHAR"))
        db.session.commit()
        logging.info("Column migration: jobs.service_type ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (jobs.service_type) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("""
            CREATE TABLE IF NOT EXISTS quotes (
                id SERIAL PRIMARY KEY,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                price FLOAT NOT NULL,
                deposit_amount FLOAT NOT NULL,
                admin_notes TEXT,
                customer_notes TEXT,
                estimated_completion VARCHAR,
                status VARCHAR DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """))
        db.session.commit()
        logging.info("Table migration: quotes ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Table migration (quotes) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text(
            "ALTER TABLE quotes ADD COLUMN IF NOT EXISTS withdrawal_note TEXT"
        ))
        db.session.commit()
        logging.info("Column migration: quotes.withdrawal_note ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (quotes.withdrawal_note) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                sender_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                read_at TIMESTAMP
            )
        """))
        db.session.commit()
        logging.info("Table migration: messages ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Table migration (messages) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text(
            "ALTER TABLE sms_settings ADD COLUMN IF NOT EXISTS ev_quote_received BOOLEAN DEFAULT TRUE"
        ))
        db.session.commit()
        logging.info("Column migration: sms_settings.ev_quote_received ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (sms_settings.ev_quote_received) skipped: %s", _e)

    # ── Marketplace Phase 2 migrations ───────────────────────────────────────

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("""
            CREATE TABLE IF NOT EXISTS categories (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                slug VARCHAR(100) NOT NULL UNIQUE,
                icon VARCHAR(100),
                display_order INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                parent_id INTEGER REFERENCES categories(id),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        db.session.commit()
        logging.info("Table migration: categories ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Table migration (categories) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("""
            CREATE TABLE IF NOT EXISTS listings (
                id SERIAL PRIMARY KEY,
                seller_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title VARCHAR(200) NOT NULL,
                description TEXT,
                category_id INTEGER REFERENCES categories(id),
                subcategory_id INTEGER REFERENCES categories(id),
                price FLOAT,
                price_type VARCHAR(20) DEFAULT 'fixed',
                condition VARCHAR(20),
                city VARCHAR(100),
                state VARCHAR(50),
                zip_code VARCHAR(10),
                latitude FLOAT,
                longitude FLOAT,
                status VARCHAR(20) DEFAULT 'active',
                delivery_option VARCHAR(100),
                view_count INTEGER DEFAULT 0,
                favorite_count INTEGER DEFAULT 0,
                featured BOOLEAN DEFAULT FALSE,
                moderation_status VARCHAR(20) DEFAULT 'approved',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                sold_at TIMESTAMP,
                expired_at TIMESTAMP
            )
        """))
        db.session.commit()
        logging.info("Table migration: listings ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Table migration (listings) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("""
            CREATE TABLE IF NOT EXISTS listing_photos (
                id SERIAL PRIMARY KEY,
                listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
                filename VARCHAR NOT NULL,
                storage_url VARCHAR,
                data BYTEA,
                content_type VARCHAR(80),
                display_order INTEGER DEFAULT 0,
                is_primary BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        db.session.commit()
        logging.info("Table migration: listing_photos ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Table migration (listing_photos) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("""
            CREATE TABLE IF NOT EXISTS listing_favorites (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, listing_id)
            )
        """))
        db.session.commit()
        logging.info("Table migration: listing_favorites ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Table migration (listing_favorites) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("""
            CREATE TABLE IF NOT EXISTS listing_offers (
                id SERIAL PRIMARY KEY,
                listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
                buyer_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                seller_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                amount FLOAT NOT NULL,
                counter_amount FLOAT,
                message TEXT,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                expires_at TIMESTAMP
            )
        """))
        db.session.commit()
        logging.info("Table migration: listing_offers ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Table migration (listing_offers) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("""
            CREATE TABLE IF NOT EXISTS listing_conversations (
                id SERIAL PRIMARY KEY,
                listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
                buyer_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                seller_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(listing_id, buyer_id)
            )
        """))
        db.session.commit()
        logging.info("Table migration: listing_conversations ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Table migration (listing_conversations) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("""
            CREATE TABLE IF NOT EXISTS listing_messages (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES listing_conversations(id) ON DELETE CASCADE,
                sender_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                read_at TIMESTAMP
            )
        """))
        db.session.commit()
        logging.info("Table migration: listing_messages ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Table migration (listing_messages) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("""
            CREATE TABLE IF NOT EXISTS delivery_requests (
                id SERIAL PRIMARY KEY,
                listing_id INTEGER REFERENCES listings(id),
                buyer_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                seller_id VARCHAR REFERENCES users(id),
                pickup_city VARCHAR(100),
                pickup_state VARCHAR(50),
                pickup_zip VARCHAR(10),
                pickup_stairs BOOLEAN DEFAULT FALSE,
                delivery_city VARCHAR(100),
                delivery_state VARCHAR(50),
                delivery_zip VARCHAR(10),
                delivery_stairs BOOLEAN DEFAULT FALSE,
                elevator_available BOOLEAN DEFAULT FALSE,
                item_description TEXT,
                approx_dimensions VARCHAR(200),
                item_count INTEGER DEFAULT 1,
                preferred_date VARCHAR(50),
                preferred_time VARCHAR(50),
                special_instructions TEXT,
                quote_amount FLOAT,
                admin_notes TEXT,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """))
        db.session.commit()
        logging.info("Table migration: delivery_requests ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Table migration (delivery_requests) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        for _col in [
            "ALTER TABLE delivery_requests ADD COLUMN IF NOT EXISTS pickup_address TEXT",
            "ALTER TABLE delivery_requests ADD COLUMN IF NOT EXISTS delivery_address TEXT",
            "ALTER TABLE delivery_requests ADD COLUMN IF NOT EXISTS need_loading BOOLEAN DEFAULT FALSE",
            "ALTER TABLE delivery_requests ADD COLUMN IF NOT EXISTS need_unloading BOOLEAN DEFAULT FALSE",
            "ALTER TABLE delivery_requests ADD COLUMN IF NOT EXISTS job_id INTEGER REFERENCES jobs(id)",
        ]:
            db.session.execute(_text(_col))
        db.session.commit()
        logging.info("Column migration: delivery_requests extended fields ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (delivery_requests extended) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("""
            CREATE TABLE IF NOT EXISTS listing_reports (
                id SERIAL PRIMARY KEY,
                listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
                reporter_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reason VARCHAR(100) NOT NULL,
                details TEXT,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        db.session.commit()
        logging.info("Table migration: listing_reports ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Table migration (listing_reports) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("""
            CREATE TABLE IF NOT EXISTS user_blocks (
                id SERIAL PRIMARY KEY,
                blocker_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                blocked_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(blocker_id, blocked_id)
            )
        """))
        db.session.commit()
        logging.info("Table migration: user_blocks ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Table migration (user_blocks) skipped: %s", _e)

    try:
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_suspended BOOLEAN DEFAULT FALSE"))
        db.session.commit()
        logging.info("Column migration: users.is_suspended ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (users.is_suspended) skipped: %s", _e)

    # ── Housing & Real Estate property columns ────────────────────────────────
    try:
        from sqlalchemy import text as _text
        _prop_cols = [
            ('listing_type',        "VARCHAR(20) DEFAULT 'item'"),
            ('property_type',       'VARCHAR(50)'),
            ('property_address',    'VARCHAR(200)'),
            ('bedrooms',            'FLOAT'),
            ('bathrooms',           'FLOAT'),
            ('sqft',                'INTEGER'),
            ('lot_size',            'VARCHAR(50)'),
            ('year_built',          'INTEGER'),
            ('garage_parking',      'VARCHAR(100)'),
            ('hoa_fee',             'FLOAT'),
            ('property_tax_annual', 'FLOAT'),
            ('open_house_dt',       'TIMESTAMP'),
            ('amenities',           'TEXT'),
            ('listed_by',           'VARCHAR(20)'),
            ('rent_terms',          'VARCHAR(20)'),
            ('pets_allowed',        'BOOLEAN'),
            ('utilities_included',  'VARCHAR(200)'),
        ]
        for _col, _defn in _prop_cols:
            db.session.execute(_text(
                f"ALTER TABLE listings ADD COLUMN IF NOT EXISTS {_col} {_defn}"
            ))
        db.session.commit()
        logging.info("Column migration: listings property fields ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (listings property fields) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text(
            "ALTER TABLE listings ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP"
        ))
        db.session.commit()
        logging.info("Column migration: listings.expires_at ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (listings.expires_at) skipped: %s", _e)

    try:
        from sqlalchemy import inspect as _inspect, text as _text
        _insp = _inspect(db.engine)
        _cols = [c['name'] for c in _insp.get_columns('listings')]
        if 'expiry_reminder_sent' not in _cols:
            db.session.execute(_text(
                "ALTER TABLE listings ADD COLUMN expiry_reminder_sent BOOLEAN DEFAULT FALSE"
            ))
            db.session.commit()
        logging.info("Column migration: listings.expiry_reminder_sent ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (listings.expiry_reminder_sent) skipped: %s", _e)

    try:
        from sqlalchemy import inspect as _inspect, text as _text
        _insp = _inspect(db.engine)
        _cols = [c['name'] for c in _insp.get_columns('listings')]
        if 'draft_reminder_sent' not in _cols:
            db.session.execute(_text(
                "ALTER TABLE listings ADD COLUMN draft_reminder_sent BOOLEAN DEFAULT FALSE"
            ))
            db.session.commit()
        logging.info("Column migration: listings.draft_reminder_sent ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (listings.draft_reminder_sent) skipped: %s", _e)

    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("ALTER TABLE gallery_photos ADD COLUMN IF NOT EXISTS item_type VARCHAR(20) DEFAULT 'custom'"))
        db.session.execute(_text("ALTER TABLE gallery_photos ADD COLUMN IF NOT EXISTS listing_id INTEGER REFERENCES listings(id)"))
        db.session.execute(_text("ALTER TABLE gallery_photos ADD COLUMN IF NOT EXISTS headline VARCHAR(200)"))
        db.session.execute(_text("ALTER TABLE gallery_photos ADD COLUMN IF NOT EXISTS description VARCHAR(500)"))
        db.session.execute(_text("ALTER TABLE gallery_photos ADD COLUMN IF NOT EXISTS button_text VARCHAR(100)"))
        db.session.execute(_text("ALTER TABLE gallery_photos ADD COLUMN IF NOT EXISTS button_link VARCHAR(500)"))
        db.session.execute(_text("ALTER TABLE gallery_photos ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"))
        db.session.execute(_text("UPDATE gallery_photos SET item_type='custom', is_active=TRUE WHERE item_type IS NULL"))
        db.session.commit()
        logging.info("Column migration: gallery_photos featured content fields ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (gallery_photos featured content) skipped: %s", _e)

    # ── Notifications table ───────────────────────────────────────────────────
    try:
        from models import Notification as _Notif  # noqa: F401
        db.create_all()  # creates notifications table if it doesn't exist
        logging.info("Table migration: notifications ensured")
    except Exception as _e:
        logging.info("Table migration (notifications) skipped: %s", _e)

    # ── Safety & media tables (listing_videos, moderation_audit_logs) ─────────
    try:
        from models import ListingVideo as _LV, ModerationAuditLog as _MAL  # noqa: F401
        db.create_all()
        logging.info("Table migration: listing_videos + moderation_audit_logs ensured")
    except Exception as _e:
        logging.info("Table migration (safety/media tables) skipped: %s", _e)

    # ── User safety columns ────────────────────────────────────────────────────
    try:
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE"))
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS age_confirmed BOOLEAN DEFAULT FALSE"))
        db.session.execute(_text("ALTER TABLE users ADD COLUMN IF NOT EXISTS marketplace_warning_count INTEGER DEFAULT 0"))
        # Existing users are pre-confirmed — they already agreed to terms before this feature
        db.session.execute(_text("UPDATE users SET age_confirmed = TRUE WHERE age_confirmed IS NOT TRUE AND created_at < NOW()"))
        db.session.commit()
        logging.info("Column migration: users safety columns (is_banned, age_confirmed, warning_count) ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (users safety) skipped: %s", _e)

    # ── ListingReport safety columns ───────────────────────────────────────────
    try:
        db.session.execute(_text("ALTER TABLE listing_reports ADD COLUMN IF NOT EXISTS evidence_url VARCHAR(500)"))
        db.session.execute(_text("ALTER TABLE listing_reports ADD COLUMN IF NOT EXISTS admin_notes TEXT"))
        db.session.execute(_text("ALTER TABLE listing_reports ADD COLUMN IF NOT EXISTS investigation_flag BOOLEAN DEFAULT FALSE"))
        db.session.commit()
        logging.info("Column migration: listing_reports safety columns ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (listing_reports safety) skipped: %s", _e)

    # ── UserReport safety columns ──────────────────────────────────────────────
    try:
        db.session.execute(_text("ALTER TABLE user_reports ADD COLUMN IF NOT EXISTS admin_notes TEXT"))
        db.session.execute(_text("ALTER TABLE user_reports ADD COLUMN IF NOT EXISTS investigation_flag BOOLEAN DEFAULT FALSE"))
        db.session.execute(_text("ALTER TABLE user_reports ADD COLUMN IF NOT EXISTS related_listing_id INTEGER"))
        db.session.commit()
        logging.info("Column migration: user_reports safety columns ensured")
    except Exception as _e:
        db.session.rollback()
        logging.info("Column migration (user_reports safety) skipped: %s", _e)

    # ── Seed default marketplace categories ──────────────────────────────────
    try:
        from models import Category
        if Category.query.count() == 0:
            default_categories = [
                ('Furniture', 'furniture', '🛋️', 1),
                ('Appliances', 'appliances', '🍳', 2),
                ('Electronics', 'electronics', '📱', 3),
                ('Vehicles', 'vehicles', '🚗', 4),
                ('Auto Parts', 'auto-parts', '🔧', 5),
                ('Tools', 'tools', '🔨', 6),
                ('Home & Garden', 'home-garden', '🏡', 7),
                ('Clothing & Accessories', 'clothing', '👗', 8),
                ('Restaurant Equipment', 'restaurant-equipment', '🍽️', 9),
                ('Business Equipment', 'business-equipment', '💼', 10),
                ('Kids & Baby', 'kids-baby', '🧸', 11),
                ('Sports & Outdoors', 'sports-outdoors', '⚽', 12),
                ('Collectibles', 'collectibles', '🏆', 13),
                ('Free Items', 'free-items', '🎁', 14),
                ('Other', 'other', '📦', 15),
            ]
            for name, slug, icon, order in default_categories:
                db.session.add(Category(name=name, slug=slug, icon=icon, display_order=order))
            db.session.commit()
            logging.info("Seeded %d default marketplace categories", len(default_categories))
    except Exception as _e:
        db.session.rollback()
        logging.info("Category seed skipped: %s", _e)

    # ── Ensure Housing & Real Estate category tree ────────────────────────────
    try:
        from models import Category as _HC
        if not _HC.query.filter_by(slug='housing').first():
            _housing = _HC(name='Housing & Real Estate', slug='housing',
                           icon='🏠', display_order=16, is_active=True)
            db.session.add(_housing)
            db.session.flush()

            _for_sale = _HC(name='For Sale', slug='housing-for-sale',
                            icon='🏷️', display_order=1, is_active=True,
                            parent_id=_housing.id)
            db.session.add(_for_sale)
            db.session.flush()
            for _i, (_nm, _sl) in enumerate([
                ('Houses for Sale',           'houses-for-sale'),
                ('Condos & Townhomes',         'condos-townhomes'),
                ('Multi-Family Properties',    'multi-family'),
                ('Land & Lots',                'land-lots'),
                ('Commercial Property',        'commercial-property'),
                ('Manufactured/Mobile Homes',  'manufactured-homes'),
                ('Other Real Estate',          'other-real-estate'),
            ], 1):
                db.session.add(_HC(name=_nm, slug=_sl, display_order=_i,
                                   is_active=True, parent_id=_for_sale.id))

            _for_rent = _HC(name='For Rent', slug='housing-for-rent',
                            icon='🔑', display_order=2, is_active=True,
                            parent_id=_housing.id)
            db.session.add(_for_rent)
            db.session.flush()
            for _i, (_nm, _sl) in enumerate([
                ('Apartments for Rent',           'apartments-rent'),
                ('Houses for Rent',               'houses-rent'),
                ('Rooms for Rent',                'rooms-rent'),
                ('Commercial Space for Rent',     'commercial-rent'),
                ('Short-term / Vacation Rental',  'short-term-rental'),
            ], 1):
                db.session.add(_HC(name=_nm, slug=_sl, display_order=_i,
                                   is_active=True, parent_id=_for_rent.id))

            db.session.commit()
            logging.info("Category migration: Housing & Real Estate seeded")
    except Exception as _e:
        db.session.rollback()
        logging.info("Category migration (housing) skipped: %s", _e)

    # ── Backfill Housing category on existing property listings ──────────────
    # Listings created before category_id was auto-assigned have NULL category_id.
    # Set it now so the admin /admin/listings category filter finds them correctly.
    try:
        from models import Category as _BHC, Listing as _BL
        _housing_cat = _BHC.query.filter_by(slug='housing').first()
        if _housing_cat:
            updated = (_BL.query
                       .filter(_BL.listing_type.in_(['property_sale', 'rental']),
                               _BL.category_id.is_(None))
                       .update({'category_id': _housing_cat.id},
                               synchronize_session=False))
            db.session.commit()
            if updated:
                logging.info("Backfill: set category_id=%d (housing) on %d existing property listings",
                             _housing_cat.id, updated)
            else:
                logging.info("Backfill: no property listings needed category_id update")
        else:
            logging.info("Backfill: housing category not found yet, skipping")
    except Exception as _e:
        db.session.rollback()
        logging.info("Backfill (housing category_id) skipped: %s", _e)

from job_expiry import start_expiry_thread
start_expiry_thread(app)

from draft_cleanup import start_draft_cleanup_thread
start_draft_cleanup_thread(app)

# Deactivate any pinned gallery listings whose listing is no longer active.
try:
    with app.app_context():
        from routes import _deactivate_stale_gallery_pins
        n = _deactivate_stale_gallery_pins()
        if n:
            logging.info("Startup: deactivated %d stale gallery pin(s)", n)
except Exception as _e:
    logging.warning("Startup gallery pin cleanup skipped: %s", _e)
