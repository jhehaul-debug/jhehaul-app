import os
import logging
from app import app, db
import routes

def seed_zipcodes_once():
    try:
        with app.app_context():
            from models import ZipCode
            count = ZipCode.query.count()

            if count == 0 and os.getenv("SEED_ZIPS", "0") == "1":
                from load_zips import load_minnesota_zips
                logging.info("Loading ZIP codes into database...")
                added = load_minnesota_zips(db, ZipCode)
                logging.info(f"Loaded {added} ZIP codes")
            else:
                logging.info(f"ZIP seed skipped. Current ZIP count: {count}")

    except Exception as e:
        logging.exception(f"ZIP seed check failed: {e}")

seed_zipcodes_once()

application = app