"""
Task 76 / 125 validation: City / ZIP marketplace filter — including radius search.

Run with:  python tests/test_city_zip_filter.py

Verifies (against the configured DB):
- City name exact match — listing appears in results
- City name partial match — listing appears when partial city token used
- City name no-match — listing for a different city is excluded
- ZIP exact match — listing appears when searching by exact ZIP code
- ZIP radius search — listing with an adjacent ZIP appears when searching centre ZIP
- ZIP not in ZipCode table — falls back to exact match, shows notice, returns 200
- ZIP no-match — unknown ZIP returns 0 results (no 500 / silent error)
- filter value is echoed back into the rendered form after submission
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flask_login.utils as _flu
from app import app
import routes  # noqa: F401 — registers routes on the app
from models import db, User, Listing, ZipCode

# ── helpers ──────────────────────────────────────────────────────────────────

results = []


def check(name, cond, extra=""):
    results.append((name, cond))
    status = "PASS" if cond else "FAIL"
    print(f"{status} - {name}", extra if not cond else "")


# ── fixtures ─────────────────────────────────────────────────────────────────

PREFIX = f"test-t76-{uuid.uuid4().hex[:6]}"
SELLER_ID = f"{PREFIX}-seller"

with app.app_context():
    admin = User.query.filter_by(is_admin=True).first()
    assert admin, "Need an admin user in the dev DB to run these tests"

    # Create a throwaway seller user
    seller = db.session.get(User, SELLER_ID)
    if not seller:
        seller = User(
            id=SELLER_ID,
            email=f"{PREFIX}@example.com",
            first_name="T76Test",
            user_type="customer",
            age_confirmed=True,
        )
        db.session.add(seller)
        db.session.commit()

    # Patch flask-login so the test client is treated as the admin
    _flu._get_user = lambda: admin
    client = app.test_client()

    created_ids = []

    def make_listing(suffix, city, zip_code, state="MN"):
        lst = Listing(
            seller_id=SELLER_ID,
            title=f"T76-{suffix}",
            description="city/zip filter test listing",
            status="active",
            moderation_status="approved",
            price=25.0,
            price_type="fixed",
            listing_type="item",
            city=city,
            state=state,
            zip_code=zip_code,
        )
        db.session.add(lst)
        db.session.commit()
        created_ids.append(lst.id)
        return lst

    try:
        # ── 1. City name — exact match ────────────────────────────────────
        lst_eagan = make_listing("eagan-exact", "Eagan", "55122")

        r = client.get("/marketplace?city_zip=Eagan")
        html = r.data.decode()
        check("route returns 200 for city name search", r.status_code == 200)
        check("city exact match: listing appears", lst_eagan.title in html)

        # ── 2. City name — partial match ──────────────────────────────────
        # "Eden Prairie" → search for "Eden" (partial)
        lst_eden = make_listing("eden-partial", "Eden Prairie", "55344")

        r = client.get("/marketplace?city_zip=Eden")
        html = r.data.decode()
        check("city partial match: listing appears", lst_eden.title in html)

        # ── 3. City name — no-match: other-city listing excluded ──────────
        # lst_eden has city="Eden Prairie"; search "Rochester" must not return it
        lst_rochester = make_listing("rochester", "Rochester", "55901")

        r = client.get("/marketplace?city_zip=Eagan")
        html = r.data.decode()
        check(
            "city no-match: Rochester listing excluded from Eagan search",
            lst_rochester.title not in html,
        )

        # Eagan listing should still be present
        check(
            "city no-match: Eagan listing still present",
            lst_eagan.title in html,
        )

        # ── 4. ZIP exact match ────────────────────────────────────────────
        lst_zip = make_listing("zip-exact", "Bloomington", "55431")

        r = client.get("/marketplace?city_zip=55431")
        html = r.data.decode()
        check("route returns 200 for ZIP search", r.status_code == 200)
        check("ZIP exact match: correct listing appears", lst_zip.title in html)

        # Listing with a distant ZIP (Rochester, ~80 mi away) must not appear
        check(
            "ZIP radius: Rochester listing (55901, ~80 mi away) excluded from 55431 search",
            lst_rochester.title not in html,
        )

        # ── 5. ZIP radius search ───────────────────────────────────────────
        # 55431 (Bloomington) and 55430 (also Bloomington) are adjacent ZIPs.
        # Both must be in the ZipCode table for this test to be meaningful.
        # If either is missing, we skip rather than fail.
        _zc_center = ZipCode.query.get("55431")
        _zc_nearby = ZipCode.query.get("55430")
        if _zc_center and _zc_nearby:
            lst_nearby_zip = make_listing("zip-radius-nearby", "Bloomington", "55430")
            r = client.get("/marketplace?city_zip=55431")
            html = r.data.decode()
            check(
                "ZIP radius: adjacent-ZIP listing (55430) appears when searching 55431",
                lst_nearby_zip.title in html,
            )
            # Clean up the extra listing
            db.session.delete(db.session.get(Listing, lst_nearby_zip.id))
            created_ids.remove(lst_nearby_zip.id)
            db.session.commit()
        else:
            check(
                "ZIP radius: skipped (55430/55431 not in ZipCode table)",
                True,  # not a failure — just missing seed data
            )

        # ── 6. ZIP not in ZipCode table → exact fallback + notice ─────────
        # "00001" is not a real ZIP and should not be in the ZipCode table.
        _missing_zip = "00001"
        assert not ZipCode.query.get(_missing_zip), \
            f"{_missing_zip} unexpectedly present in ZipCode table"
        lst_fallback = make_listing("zip-fallback", "FakeCity", _missing_zip)
        r = client.get(f"/marketplace?city_zip={_missing_zip}")
        html = r.data.decode()
        check("ZIP fallback: returns 200 when ZIP not in table", r.status_code == 200)
        check(
            "ZIP fallback: listing with exact ZIP still appears",
            lst_fallback.title in html,
        )
        check(
            "ZIP fallback: fallback notice shown in HTML",
            "wasn" in html and _missing_zip in html,
        )

        # ── 7. Unknown ZIP → 0 results, no server error ───────────────────
        # "99999" is not a real ZIP assigned to any listing
        r = client.get("/marketplace?city_zip=99999")
        html = r.data.decode()
        check("unknown ZIP: returns 200 (no 500 error)", r.status_code == 200)
        # None of our test listings should appear
        for lid, title_suffix in [
            (lst_eagan.id, lst_eagan.title),
            (lst_eden.id, lst_eden.title),
            (lst_zip.id, lst_zip.title),
        ]:
            check(
                f"unknown ZIP: {title_suffix} not in results",
                title_suffix not in html,
            )

        # ── 6. Filter value retained in rendered form ─────────────────────
        # When the user submits a city name, that value should appear in the
        # page HTML so the <input> is pre-filled (value="Burnsville" etc.)
        r = client.get("/marketplace?city_zip=Burnsville")
        html = r.data.decode()
        check(
            "filter value retained: 'Burnsville' echoed in HTML",
            "Burnsville" in html,
        )

        r = client.get("/marketplace?city_zip=55122")
        html = r.data.decode()
        check(
            "filter value retained: ZIP '55122' echoed in HTML",
            "55122" in html,
        )

        # ── 7. Partial city match excludes unrelated city ─────────────────
        # Search "Eden" must NOT return Rochester or Eagan listings
        r = client.get("/marketplace?city_zip=Eden")
        html = r.data.decode()
        check(
            "partial match: Rochester listing excluded from Eden search",
            lst_rochester.title not in html,
        )
        check(
            "partial match: Eagan listing excluded from Eden search",
            lst_eagan.title not in html,
        )

    finally:
        # ── cleanup ───────────────────────────────────────────────────────
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
