"""
Task 73 validation: Twin Cities area filter (coordinate bounding-box + city-name fallback).

Run with:  python tests/test_twin_cities_filter.py

Verifies (against the configured DB):
- Listings with coordinates inside the 40-mile bounding box are included
- Listings with coordinates outside the bounding box are excluded
- Listings with no coordinates use the city-name fallback (city in list + state=MN)
- City-name fallback does NOT include outstate MN cities (Duluth, Rochester)
- The route returns HTTP 200 for ?area=twin-cities
"""
import sys, os, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flask_login.utils as _flu
from app import app
import routes  # noqa: F401 — registers routes on the app
from models import db, User, Listing

# ── constants matching routes.py ────────────────────────────────────────────
_TC_LAT, _TC_LON = 44.9778, -93.2650
_TC_DLAT = 40.0 / 69.0   # ≈ 0.5797 °
_TC_DLON = 40.0 / 49.0   # ≈ 0.8163 °

# Twin Cities suburbs — should be INCLUDED
# Coordinates verified inside the bounding box
SUBURBS_INSIDE = [
    # (title_suffix, lat, lon, city)
    ("lakeville",    44.6497, -93.2428, "Lakeville"),
    ("burnsville",   44.7677, -93.2777, "Burnsville"),
    ("eden-prairie", 44.8547, -93.4708, "Eden Prairie"),
    ("woodbury",     44.9239, -92.9594, "Woodbury"),
]

# Outstate MN cities — coordinates clearly outside the bounding box
CITIES_OUTSIDE = [
    # (title_suffix, lat, lon, city)
    # Duluth: lat ~46.79 > upper bound 45.56
    ("duluth",     46.7867, -92.1005, "Duluth"),
    # Rochester: lat ~44.01 < lower bound 44.40
    ("rochester",  44.0121, -92.4802, "Rochester"),
    # St. Cloud: lat ~45.56 just over upper bound; lon ~-94.16 outside west bound
    ("st-cloud",   45.5630, -94.1636, "St. Cloud"),
]

# City-name fallback: no lat/lon set, city name determines inclusion
FALLBACK_INSIDE = [
    # Known TC suburb in the city list
    ("fallback-burnsville", None, None, "Burnsville", "MN"),
    ("fallback-lakeville",  None, None, "Lakeville",  "MN"),
    ("fallback-eagan",      None, None, "Eagan",      "MN"),
]

FALLBACK_OUTSIDE = [
    # Outstate MN — city name not in _TWIN_CITIES_CITIES
    ("fallback-duluth",    None, None, "Duluth",    "MN"),
    ("fallback-rochester", None, None, "Rochester", "MN"),
    # Twin Cities city name but wrong state — must NOT be included
    ("fallback-wrong-state", None, None, "Burnsville", "WI"),
]


results = []

def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


PREFIX = f"test-t73-{uuid.uuid4().hex[:6]}"
SELLER_ID = f"{PREFIX}-seller"

with app.app_context():
    # ── fixtures ────────────────────────────────────────────────────────────
    admin = User.query.filter_by(is_admin=True).first()
    assert admin, "need an admin user in dev DB"

    seller = User.query.get(SELLER_ID)
    if not seller:
        seller = User(id=SELLER_ID, email=f"{PREFIX}@example.com",
                      first_name="T73", user_type="customer")
        db.session.add(seller)
        db.session.commit()

    # Make auth transparent — tests hit the marketplace route as admin
    _flu._get_user = lambda: admin
    client = app.test_client()

    created_ids = []

    def make_listing(suffix, lat, lon, city, state="MN"):
        lst = Listing(
            seller_id=SELLER_ID,
            title=f"T73-{suffix}",
            description="test listing",
            status="active",
            moderation_status="approved",
            price=10.0,
            price_type="fixed",
            listing_type="item",
            city=city,
            state=state,
            latitude=lat,
            longitude=lon,
        )
        db.session.add(lst)
        db.session.commit()
        created_ids.append(lst.id)
        return lst

    try:
        # ── 1. Route responds with 200 ────────────────────────────────────
        r = client.get("/marketplace?area=twin-cities")
        check("route returns 200 for ?area=twin-cities", r.status_code == 200)

        # ── 2. Inside-metro coordinate listings ──────────────────────────
        inside_listings = [make_listing(s, lat, lon, city)
                           for s, lat, lon, city in SUBURBS_INSIDE]

        r = client.get("/marketplace?area=twin-cities")
        html = r.data.decode()
        for lst in inside_listings:
            check(
                f"coord-inside included: {lst.title}",
                lst.title in html,
            )

        # ── 3. Outside-metro coordinate listings ─────────────────────────
        outside_listings = [make_listing(s, lat, lon, city)
                            for s, lat, lon, city in CITIES_OUTSIDE]

        r = client.get("/marketplace?area=twin-cities")
        html = r.data.decode()
        for lst in outside_listings:
            check(
                f"coord-outside excluded: {lst.title}",
                lst.title not in html,
            )

        # ── 4. City-name fallback — should be INCLUDED ───────────────────
        fallback_in = [make_listing(s, lat, lon, city, state)
                       for s, lat, lon, city, state in FALLBACK_INSIDE]

        r = client.get("/marketplace?area=twin-cities")
        html = r.data.decode()
        for lst in fallback_in:
            check(
                f"fallback-inside included: {lst.title}",
                lst.title in html,
            )

        # ── 5. City-name fallback — should be EXCLUDED ───────────────────
        fallback_out = [make_listing(s, lat, lon, city, state)
                        for s, lat, lon, city, state in FALLBACK_OUTSIDE]

        r = client.get("/marketplace?area=twin-cities")
        html = r.data.decode()
        for lst in fallback_out:
            check(
                f"fallback-outside excluded: {lst.title}",
                lst.title not in html,
            )

        # ── 6. Boundary sanity: inside listings still present after outside
        #       listings were added (regression guard) ─────────────────────
        for lst in inside_listings:
            check(
                f"inside still present after adding outside: {lst.title}",
                lst.title in html,
            )

    finally:
        # ── cleanup ─────────────────────────────────────────────────────
        for lid in created_ids:
            lst = db.session.get(Listing, lid)
            if lst:
                db.session.delete(lst)
        sel = db.session.get(User, SELLER_ID)
        if sel:
            db.session.delete(sel)
        db.session.commit()

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
