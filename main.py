from app import app, db
import routes
import logging

if __name__ == "__main__":
    with app.app_context():
        from models import ZipCode
        from load_zips import load_minnesota_zips
        count = ZipCode.query.count()
        if count == 0:
            logging.info("Loading ZIP codes into database...")
            added = load_minnesota_zips(db, ZipCode)
            logging.info(f"Loaded {added} ZIP codes")
        else:
            logging.info(f"ZIP codes already loaded: {count}")
    app.run(host="0.0.0.0", port=5000, debug=False)
