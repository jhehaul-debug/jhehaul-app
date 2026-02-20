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
@app.route("/")
def index():
    return "JHE HAUL IS LIVE"
app.secret_key = os.environ.get("SESSION_SECRET")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

import os

app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "sqlite:///data/jhe_haul.db"
)

uri = app.config["SQLALCHEMY_DATABASE_URI"]
if uri and uri.startswith("postgres://"):
    app.config["SQLALCHEMY_DATABASE_URI"] = uri.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    'pool_pre_ping': True,
    "pool_recycle": 300,
}

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

with app.app_context():
    import models
    db.create_all()
    logging.info("Database tables created")

    from models import User
    admin_user = db.session.get(User, '53919193')
    if admin_user and not admin_user.is_admin:
        admin_user.is_admin = True
        db.session.commit()
        logging.info("Admin flag restored for admin user")

    from models import ZipCode
    from load_zips import load_minnesota_zips
    count = ZipCode.query.count()
    if count == 0:
        logging.info("Loading ZIP codes into database...")
        added = load_minnesota_zips(db, ZipCode)
        logging.info(f"Loaded {added} ZIP codes")
    else:
        logging.info(f"ZIP codes already loaded: {count}")
    @app.route("/")
    def home():
        return "JHE HAUL SERVER RUNNING"
        @app.route("/health")
        def health():
            return "ok", 200