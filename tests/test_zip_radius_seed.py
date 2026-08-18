"""
Task 216: Confirm radius search still returns results after the ZIP code
database is updated or reloaded.

Runs against an in-memory SQLite database — no live DB required.
DATABASE_URL is forcibly set to 'sqlite:///:memory:' (unconditional
assignment, not setdefault) before any app import so an external
DATABASE_URL in the environment cannot override it and cause data loss.

The startup ZIP-loader in app.py is patched to a no-op during import so
the test never makes real pgeocode HTTP calls.  The production loader
(load_minnesota_zips) is exercised separately in Step 6 with a mocked
geocoder (pgeocode.Nominatim), verifying it inserts the expected rows
and that the radius query still works on loader-seeded data.

Checks
------
1. Both key ZIPs (55431 / 55430) are present and have valid lat/lon after
   manual fixture seeding.
2. The planar-distance approximation places 55430 within 25 mi of 55431
   and Rochester (55901) outside 25 mi.
3. The exact bounding-box formula (copied from routes.py) returns the right
   nearby-ZIP set.
4. The real /marketplace route returns 200 and includes / excludes the
   correct listings when searching by ZIP code at different radii.
5. After the zip_codes table is fully cleared and re-seeded the marketplace
   route still returns the correct results — simulating a re-seed event.
6. load_minnesota_zips (the production seeder) with a mocked geocoder
   inserts the expected ZIPs, and the bounding-box query works on that
   loader-seeded data.

Run with:
    python tests/test_zip_radius_seed.py
"""
import os

# ── MUST come before any app import so the in-memory DB is guaranteed. ──────
# Direct assignment (not setdefault) prevents an existing DATABASE_URL in the
# process environment from overriding this and pointing the test at production.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import sys
import math
import uuid
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Patch the startup ZIP loader *before* importing app.  The in-memory SQLite
# DB is empty, so without this patch app.py would call pgeocode for ~3 800
# MN/WI ZIPs at import time — far too slow for CI.  We test the real function
# separately below with a controlled mock.
_noop_loader = patch("load_zips.load_minnesota_zips", return_value=0)
_noop_loader.start()

import flask_login.utils as _flu  # noqa: E402
from app import app                # noqa: E402  (uses forced SQLite URL)
import routes                      # noqa: E402, F401 — registers all routes

_noop_loader.stop()  # no longer needed; loader tested directly in step 6

from models import db, ZipCode, Listing, User  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture ZIP data — real pgeocode lat/lon values, rounded to 4 dp
# ---------------------------------------------------------------------------
FIXTURE_ZIPS = [
    # zip,    city,                state,  lat,      lon
    ("55431", "Bloomington",      "MN",  44.8547, -93.3853),  # search centre
    ("55430", "Brooklyn Center",  "MN",  45.0697, -93.3292),  # ≈15 mi away
    ("55112", "Arden Hills",      "MN",  45.0867, -93.1613),  # ≈19 mi away
    ("55901", "Rochester",        "MN",  44.0234, -92.4799),  # ≈80 mi away
    ("55344", "Eden Prairie",     "MN",  44.8547, -93.4710),  # ≈4 mi away
]


def _seed_zips(session):
    for z, city, state, lat, lon in FIXTURE_ZIPS:
        if not session.get(ZipCode, z):
            session.add(ZipCode(zip=z, city=city, state=state, lat=lat, lon=lon))
    session.commit()


def _bounding_box_nearby(center_zip, radius_mi, session):
    """Exact copy of the bounding-box formula used in routes.py."""
    center = session.get(ZipCode, center_zip)
    if center is None:
        return []
    dlat = radius_mi / 69.0
    dlon = radius_mi / (69.0 * abs(math.cos(math.radians(center.lat))) + 1e-9)
    rows = (
        session.query(ZipCode.zip)
        .filter(
            ZipCode.lat >= center.lat - dlat,
            ZipCode.lat <= center.lat + dlat,
            ZipCode.lon >= center.lon - dlon,
            ZipCode.lon <= center.lon + dlon,
        )
        .all()
    )
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Test runner helpers
# ---------------------------------------------------------------------------
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    status = "PASS" if cond else "FAIL"
    print(f"{status} - {name}", extra if not cond else "")


PREFIX = f"t216-{uuid.uuid4().hex[:6]}"
SELLER_ID = f"{PREFIX}-seller"

# ---------------------------------------------------------------------------
# Main test body
# ---------------------------------------------------------------------------
with app.app_context():
    db.create_all()

    # Safety: ensure zip_codes table is empty before we begin (in-memory DB
    # should already be empty, but be explicit).
    ZipCode.query.delete()
    db.session.commit()

    # Create a throwaway seller so listings can be committed.
    seller = User(
        id=SELLER_ID,
        email=f"{PREFIX}@example.com",
        first_name="T216",
        user_type="customer",
        age_confirmed=True,
    )
    db.session.add(seller)
    db.session.commit()

    # Patch flask-login so the test client acts as the seller.
    _flu._get_user = lambda: seller
    client = app.test_client()

    created_listing_ids: list = []

    def make_listing(suffix, zip_code, city="Bloomington"):
        lst = Listing(
            seller_id=SELLER_ID,
            title=f"T216-{suffix}",
            description="radius seed test",
            status="active",
            moderation_status="approved",
            price=10.0,
            price_type="fixed",
            listing_type="item",
            city=city,
            state="MN",
            zip_code=zip_code,
        )
        db.session.add(lst)
        db.session.commit()
        created_listing_ids.append(lst.id)
        return lst

    try:
        # ── 1. Seed ZIPs and verify presence / validity ───────────────────
        _seed_zips(db.session)

        check(
            f"zip_codes table populated after seed ({len(FIXTURE_ZIPS)} rows)",
            ZipCode.query.count() == len(FIXTURE_ZIPS),
        )

        zc_center = db.session.get(ZipCode, "55431")
        zc_nearby  = db.session.get(ZipCode, "55430")
        check("55431 is present in seeded table", zc_center is not None)
        check("55430 is present in seeded table", zc_nearby  is not None)

        if zc_center:
            check("55431 has valid lat (non-zero, non-NaN)", zc_center.lat != 0 and not math.isnan(zc_center.lat))
            check("55431 has valid lon (non-zero, non-NaN)", zc_center.lon != 0 and not math.isnan(zc_center.lon))
        if zc_nearby:
            check("55430 has valid lat (non-zero, non-NaN)", zc_nearby.lat != 0 and not math.isnan(zc_nearby.lat))
            check("55430 has valid lon (non-zero, non-NaN)", zc_nearby.lon != 0 and not math.isnan(zc_nearby.lon))

        # ── 2. Distance: 55430 within 25 mi, Rochester outside 25 mi ─────
        if zc_center and zc_nearby:
            dlat = (zc_nearby.lat - zc_center.lat) * 69.0
            dlon = (zc_nearby.lon - zc_center.lon) * 69.0 * abs(
                math.cos(math.radians(zc_center.lat))
            )
            dist_nearby = math.sqrt(dlat ** 2 + dlon ** 2)
            check(
                f"55430 is within 25 mi of 55431 (≈{dist_nearby:.1f} mi)",
                dist_nearby < 25.0,
            )
            check(
                f"55430 is farther than 5 mi from 55431 (≈{dist_nearby:.1f} mi)",
                dist_nearby > 5.0,
            )

        zc_rochester = db.session.get(ZipCode, "55901")
        if zc_center and zc_rochester:
            dlat_r = (zc_rochester.lat - zc_center.lat) * 69.0
            dlon_r = (zc_rochester.lon - zc_center.lon) * 69.0 * abs(
                math.cos(math.radians(zc_center.lat))
            )
            dist_roch = math.sqrt(dlat_r ** 2 + dlon_r ** 2)
            check(
                f"55901 (Rochester) is farther than 25 mi from 55431 (≈{dist_roch:.1f} mi)",
                dist_roch > 25.0,
            )

        # ── 3. Bounding-box query returns the right ZIP set ───────────────
        nearby_25 = _bounding_box_nearby("55431", 25, db.session)
        check("bounding-box radius=25: includes 55431 itself",            "55431" in nearby_25)
        check("bounding-box radius=25: includes adjacent 55430",          "55430" in nearby_25)
        check("bounding-box radius=25: excludes Rochester 55901 (~80 mi)", "55901" not in nearby_25)

        nearby_5 = _bounding_box_nearby("55431", 5, db.session)
        check("bounding-box radius=5: excludes 55430 (~15 mi away)",      "55430" not in nearby_5)

        # Unknown ZIP returns an empty list without raising an exception.
        check(
            "bounding-box on unknown ZIP returns empty list (no crash)",
            _bounding_box_nearby("00001", 25, db.session) == [],
        )

        # ── 4. Real marketplace route — radius includes / excludes correctly
        lst_center  = make_listing("center",  "55431", "Bloomington")
        lst_nearby  = make_listing("nearby",  "55430", "Brooklyn Center")
        lst_distant = make_listing("distant", "55901", "Rochester")

        r = client.get("/marketplace?city_zip=55431")
        check("marketplace: returns 200 for ZIP search (55431)", r.status_code == 200)
        html = r.data.decode()
        check("marketplace: listing in 55431 appears",                  lst_center.title  in html)
        check("marketplace: listing in 55430 appears (within radius)",  lst_nearby.title  in html)
        check("marketplace: listing in 55901 excluded (~80 mi away)",   lst_distant.title not in html)

        # radius=5 — 55430 is ~15 mi away and must be excluded
        r5 = client.get("/marketplace?city_zip=55431&radius=5")
        check("marketplace radius=5: returns 200",                         r5.status_code == 200)
        html5 = r5.data.decode()
        check("marketplace radius=5: listing in 55430 excluded (~15 mi)",  lst_nearby.title not in html5)
        check("marketplace radius=5: listing in 55431 itself still appears", lst_center.title in html5)

        # ── 5. Reseed cycle: clear → fallback behaviour → reseed → works ─
        ZipCode.query.delete()
        db.session.commit()
        check("zip_codes table is empty after clear", ZipCode.query.count() == 0)

        # With an empty table the route must still return 200 (exact-match fallback)
        r_empty = client.get("/marketplace?city_zip=55431")
        check("marketplace: returns 200 when zip_codes table is empty", r_empty.status_code == 200)
        html_empty = r_empty.data.decode()
        # In fallback mode the center listing (exact ZIP match) appears,
        # the nearby listing (different ZIP) does not.
        check("marketplace empty-table fallback: center listing (exact ZIP) appears", lst_center.title in html_empty)
        check("marketplace empty-table fallback: nearby listing (different ZIP) not shown", lst_nearby.title not in html_empty)

        # Re-seed the table
        _seed_zips(db.session)
        check("zip_codes repopulated after reseed", ZipCode.query.count() == len(FIXTURE_ZIPS))

        r_after = client.get("/marketplace?city_zip=55431")
        check("marketplace: returns 200 after reseed",                                       r_after.status_code == 200)
        html_after = r_after.data.decode()
        check("marketplace after reseed: center listing (55431) still appears",             lst_center.title  in html_after)
        check("marketplace after reseed: nearby listing (55430) reappears in radius",       lst_nearby.title  in html_after)
        check("marketplace after reseed: distant listing (55901) still excluded (~80 mi)", lst_distant.title not in html_after)

        # ── 6. Production loader (load_minnesota_zips) with mocked geocoder
        # Clear the table so the loader's guard (`if existing > 0: return`) does not skip.
        ZipCode.query.delete()
        db.session.commit()
        check("zip_codes empty before loader test", ZipCode.query.count() == 0)

        # Build a minimal mock: only 55430 and 55431 return valid data;
        # every other ZIP returns NaN lat so the loader skips it.
        def _mock_query(zip_str):
            DATA = {
                "55431": (44.8547, -93.3853, "MN", "Bloomington"),
                "55430": (45.0697, -93.3292, "MN", "Brooklyn Center"),
            }
            mock_result = MagicMock()
            if zip_str in DATA:
                lat, lon, state, city = DATA[zip_str]
                mock_result.latitude   = lat
                mock_result.longitude  = lon
                mock_result.state_code = state
                mock_result.place_name = city
            else:
                # NaN causes the loader's `if lat != lat` guard to skip the row.
                mock_result.latitude   = float("nan")
                mock_result.longitude  = float("nan")
                mock_result.state_code = "MN"
                mock_result.place_name = "Other"
            return mock_result

        mock_nomi = MagicMock()
        mock_nomi.query_postal_code.side_effect = _mock_query

        from load_zips import load_minnesota_zips  # noqa: E402

        with patch("pgeocode.Nominatim", return_value=mock_nomi):
            added = load_minnesota_zips(db, ZipCode)

        check(
            f"load_minnesota_zips inserted rows via mocked geocoder ({added} added)",
            added >= 2,
        )
        check("55431 seeded by loader (mocked geocoder)", db.session.get(ZipCode, "55431") is not None)
        check("55430 seeded by loader (mocked geocoder)", db.session.get(ZipCode, "55430") is not None)

        # Radius query must still work on the loader-seeded data
        nearby_loaded = _bounding_box_nearby("55431", 25, db.session)
        check("bounding-box on loader-seeded data: includes 55430", "55430" in nearby_loaded)

        # And the marketplace route must return the correct listing
        r_loaded = client.get("/marketplace?city_zip=55431")
        check("marketplace route works on loader-seeded data: returns 200", r_loaded.status_code == 200)
        html_loaded = r_loaded.data.decode()
        check("marketplace on loader-seeded data: center listing appears",       lst_center.title in html_loaded)
        check("marketplace on loader-seeded data: nearby listing (55430) appears", lst_nearby.title in html_loaded)

    finally:
        # ── Cleanup: remove all test rows ────────────────────────────────
        for lid in created_listing_ids:
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
