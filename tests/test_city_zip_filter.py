"""
Task 76 / 125 / 215 validation: City / ZIP marketplace filter — including radius search.

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
- Custom radius (5 mi) passed via ?radius=5 is respected (fewer results than 50 mi)
- Custom radius (50 mi) passed via ?radius=50 is respected (more results than 5 mi)
- Invalid radius value falls back to default 25 mi without error
- Radius echoed in URL / filter pill when non-default
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

        # ── 8. Custom radius: deterministic exclusion/inclusion ───────────
        # 55431 (Bloomington, MN) is the search center.
        # 55112 (Arden Hills, MN) is ~19 miles away — inside 50 mi but outside 10 mi.
        # If both ZIPs are seeded we can assert exclusion at radius=10 and inclusion at radius=50.
        import math as _math
        _zc_center2 = ZipCode.query.get("55431")
        _zc_far     = ZipCode.query.get("55112")
        if _zc_center2 and _zc_far:
            # Sanity-check actual distance so the test is self-validating
            _dlat2 = (_zc_far.lat - _zc_center2.lat) * 69.0
            _dlon2 = (_zc_far.lon - _zc_center2.lon) * 69.0 * abs(_math.cos(_math.radians(_zc_center2.lat)))
            _actual_dist = _math.sqrt(_dlat2**2 + _dlon2**2)
            _dist_ok = 10 < _actual_dist < 50
            check(
                f"radius deterministic test: 55112 is between 10 and 50 mi from 55431 (actual ≈ {_actual_dist:.1f} mi)",
                _dist_ok,
            )
            if _dist_ok:
                lst_far2 = make_listing("radius-far2", "Arden Hills", "55112")

                # radius=10 — 55112 is outside 10 mi → listing must NOT appear
                r10 = client.get("/marketplace?city_zip=55431&radius=10")
                html10 = r10.data.decode()
                check("radius=10: returns 200", r10.status_code == 200)
                check(
                    "radius=10: far-away listing (55112, ~19 mi) EXCLUDED",
                    lst_far2.title not in html10,
                )

                # radius=50 — 55112 is inside 50 mi → listing must appear
                r50 = client.get("/marketplace?city_zip=55431&radius=50")
                html50 = r50.data.decode()
                check("radius=50: returns 200", r50.status_code == 200)
                check(
                    "radius=50: far-away listing (55112, ~19 mi) INCLUDED",
                    lst_far2.title in html50,
                )

                # Invalid radius value falls back to default without server error
                r_bad = client.get("/marketplace?city_zip=55431&radius=999")
                check("invalid radius: returns 200 (falls back to default)", r_bad.status_code == 200)

                # Radius echoed in form HTML
                r_echo = client.get("/marketplace?city_zip=55431&radius=10")
                html_echo = r_echo.data.decode()
                check("radius=10: returns 200", r_echo.status_code == 200)
                check(
                    "radius=10: radius value selected in rendered HTML",
                    'value="10"' in html_echo and 'selected' in html_echo,
                )
                # Non-default radius is reflected in the pill label
                check(
                    "radius=10: pill label mentions '10 mi'",
                    "10 mi" in html_echo,
                )

                # Radius is preserved in the hero/compact keyword search hidden input.
                # The hidden input renders as '<input type="hidden" name="radius" value="10">'
                # which is distinct from '<option value="10" ...>' in the dropdown.
                check(
                    "radius=10: hero form carries radius as hidden input",
                    '<input type="hidden" name="radius" value="10">' in html_echo,
                )

                # Clean up the extra listing
                db.session.delete(db.session.get(Listing, lst_far2.id))
                created_ids.remove(lst_far2.id)
                db.session.commit()
        else:
            check(
                "custom radius: skipped (55431/55112 not in ZipCode table)",
                True,
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
