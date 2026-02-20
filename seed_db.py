from app import app, db
from models import ZipCode
from load_zips import load_minnesota_zips

with app.app_context():
    print("Loading Minnesota ZIP codes...")
    load_minnesota_zips(db, ZipCode)
    print("Done.")