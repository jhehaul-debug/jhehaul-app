"""
test_landing_preview_updates.py — confirm the /landing page listings preview
reflects live listing status on every load.

Covers:
  1. Active + approved item listing appears in the "Recent Items for Sale" preview
  2. After marking the listing sold, it disappears from /landing on the very next load
  3. After deactivating (status='inactive') a listing, it disappears from /landing
  4. A pending-moderation listing (moderation_status='pending') is never shown
  5. No Cache-Control header on the /landing response enables stale content
  6. Active property-sale listing appears in the "Homes For Sale" preview
  7. Property-sale listing marked sold is absent from /landing on next load
  8. Active rental listing appears in the "Rentals" preview
  9. Rental listing marked sold is absent from /landing on next load
"""

import sys
import uuid
from unittest.mock import patch

from app import app, db
import routes  # registers all URL rules


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_seller():
    from models import User
    u = User()
    u.id = str(uuid.uuid4())
    u.email = f"seller_{u.id[:8]}@test.local"
    u.user_type = "customer"
    u.is_admin = False
    u.age_confirmed = True
    u.profile_nudge_dismissed = True
    u.phone = "5550000000"
    db.session.add(u)
    db.session.commit()
    return u.id


def _make_listing(seller_id, title, listing_type="item",
                  status="active", moderation_status="approved",
                  price=50.0, city="Minneapolis"):
    from models import Listing
    l = Listing()
    l.seller_id = seller_id
    l.title = title
    l.status = status
    l.listing_type = listing_type
    l.moderation_status = moderation_status
    l.price_type = "fixed"
    l.price = price
    l.city = city
    db.session.add(l)
    db.session.commit()
    return l.id


def _set_status(listing_id, new_status):
    """Directly update a listing's status in the DB (bypasses route auth)."""
    from models import Listing
    with app.app_context():
        l = Listing.query.get(listing_id)
        if l:
            l.status = new_status
            db.session.commit()


def _cleanup(*pairs):
    """Delete (Model, id) pairs; ignores None ids."""
    with app.app_context():
        for Model, row_id in pairs:
            if row_id is None:
                continue
            obj = Model.query.get(row_id)
            if obj:
                db.session.delete(obj)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


def _get_landing():
    """Perform an anonymous GET /landing and return the response."""
    with app.test_client() as client:
        # /landing is public — no auth patch needed
        return client.get("/landing", follow_redirects=False)


# ── Test runner ───────────────────────────────────────────────────────────────

PASS = []
FAIL = []


def run(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  PASS  {name}")
    except Exception as exc:
        import traceback
        FAIL.append((name, exc))
        print(f"  FAIL  {name}: {exc}")
        traceback.print_exc()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_active_item_appears_in_preview():
    """An active + approved item listing appears in the /landing preview."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id = _make_seller()
        listing_id = _make_listing(seller_id, "Active Preview Lamp",
                                   listing_type="item",
                                   status="active", moderation_status="approved")

    try:
        resp = _get_landing()
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        html = resp.data.decode()
        assert "Active Preview Lamp" in html, \
            "Active approved item should appear in the landing page preview"
    finally:
        from models import Listing, User
        _cleanup((Listing, listing_id), (User, seller_id))


def test_sold_item_disappears_on_next_load():
    """After marking a listing sold, /landing no longer shows it."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id = _make_seller()
        listing_id = _make_listing(seller_id, "Soon Sold Bookshelf",
                                   listing_type="item",
                                   status="active", moderation_status="approved")

    try:
        # Confirm it appears while active
        resp_before = _get_landing()
        assert resp_before.status_code == 200
        assert "Soon Sold Bookshelf" in resp_before.data.decode(), \
            "Listing should appear on /landing while status=active"

        # Mark sold directly in the DB (simulates seller/admin action)
        _set_status(listing_id, "sold")

        # Confirm it is absent on the very next load — no stale cache
        resp_after = _get_landing()
        assert resp_after.status_code == 200
        assert "Soon Sold Bookshelf" not in resp_after.data.decode(), \
            "Sold listing must not appear in /landing preview on next load"
    finally:
        from models import Listing, User
        _cleanup((Listing, listing_id), (User, seller_id))


def test_deactivated_item_disappears_on_next_load():
    """After setting status='inactive', /landing no longer shows the listing."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id = _make_seller()
        listing_id = _make_listing(seller_id, "Deactivated Coffee Table",
                                   listing_type="item",
                                   status="active", moderation_status="approved")

    try:
        resp_before = _get_landing()
        assert resp_before.status_code == 200
        assert "Deactivated Coffee Table" in resp_before.data.decode(), \
            "Listing should appear while active"

        _set_status(listing_id, "inactive")

        resp_after = _get_landing()
        assert resp_after.status_code == 200
        assert "Deactivated Coffee Table" not in resp_after.data.decode(), \
            "Deactivated listing must not appear in /landing preview"
    finally:
        from models import Listing, User
        _cleanup((Listing, listing_id), (User, seller_id))


def test_pending_moderation_item_never_shown():
    """A listing with moderation_status='pending' is never in the /landing preview."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id = _make_seller()
        listing_id = _make_listing(seller_id, "Pending Moderation Chair",
                                   listing_type="item",
                                   status="active", moderation_status="pending")

    try:
        resp = _get_landing()
        assert resp.status_code == 200
        assert "Pending Moderation Chair" not in resp.data.decode(), \
            "Listing with moderation_status='pending' must not appear on /landing"
    finally:
        from models import Listing, User
        _cleanup((Listing, listing_id), (User, seller_id))


def test_no_caching_header_on_landing_response():
    """The /landing response must not carry a Cache-Control header that allows
    stale content to be served (max-age > 0 without revalidation)."""
    resp = _get_landing()
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    cc = (resp.headers.get("Cache-Control") or "").lower()
    # Either no Cache-Control header, or it must require revalidation / no-cache.
    # A plain "public, max-age=3600" without must-revalidate would be a problem.
    if cc:
        stale_ok = (
            "max-age" in cc
            and "no-cache" not in cc
            and "no-store" not in cc
            and "must-revalidate" not in cc
            and "private" not in cc
        )
        assert not stale_ok, (
            f"Cache-Control '{cc}' allows stale content — /landing must not be "
            "publicly cached without revalidation, or sold listings could linger."
        )


def test_active_property_sale_appears_in_preview():
    """An active + approved property_sale listing appears on /landing."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id = _make_seller()
        listing_id = _make_listing(seller_id, "Active Property For Sale",
                                   listing_type="property_sale",
                                   status="active", moderation_status="approved",
                                   price=250000.0, city="St. Paul")

    try:
        resp = _get_landing()
        assert resp.status_code == 200
        assert "Active Property For Sale" in resp.data.decode(), \
            "Active property_sale listing should appear in /landing Homes For Sale section"
    finally:
        from models import Listing, User
        _cleanup((Listing, listing_id), (User, seller_id))


def test_sold_property_sale_disappears_on_next_load():
    """Marking a property_sale listing sold removes it from /landing immediately."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id = _make_seller()
        listing_id = _make_listing(seller_id, "Soon Sold Property",
                                   listing_type="property_sale",
                                   status="active", moderation_status="approved",
                                   price=300000.0)

    try:
        resp_before = _get_landing()
        assert resp_before.status_code == 200
        assert "Soon Sold Property" in resp_before.data.decode(), \
            "Property listing should appear while active"

        _set_status(listing_id, "sold")

        resp_after = _get_landing()
        assert resp_after.status_code == 200
        assert "Soon Sold Property" not in resp_after.data.decode(), \
            "Sold property listing must not appear in /landing on next load"
    finally:
        from models import Listing, User
        _cleanup((Listing, listing_id), (User, seller_id))


def test_active_rental_appears_in_preview():
    """An active + approved rental listing appears on /landing."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id = _make_seller()
        listing_id = _make_listing(seller_id, "Active Rental Apartment",
                                   listing_type="rental",
                                   status="active", moderation_status="approved",
                                   price=1200.0, city="Duluth")

    try:
        resp = _get_landing()
        assert resp.status_code == 200
        assert "Active Rental Apartment" in resp.data.decode(), \
            "Active rental listing should appear in /landing Rentals section"
    finally:
        from models import Listing, User
        _cleanup((Listing, listing_id), (User, seller_id))


def test_sold_rental_disappears_on_next_load():
    """Marking a rental listing sold removes it from /landing immediately."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id = _make_seller()
        listing_id = _make_listing(seller_id, "Soon Rented House",
                                   listing_type="rental",
                                   status="active", moderation_status="approved",
                                   price=1500.0)

    try:
        resp_before = _get_landing()
        assert resp_before.status_code == 200
        assert "Soon Rented House" in resp_before.data.decode(), \
            "Rental listing should appear while active"

        _set_status(listing_id, "sold")

        resp_after = _get_landing()
        assert resp_after.status_code == 200
        assert "Soon Rented House" not in resp_after.data.decode(), \
            "Sold rental listing must not appear in /landing on next load"
    finally:
        from models import Listing, User
        _cleanup((Listing, listing_id), (User, seller_id))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nRunning landing page preview update tests...\n")

    run("active item appears in preview",               test_active_item_appears_in_preview)
    run("sold item disappears on next load",            test_sold_item_disappears_on_next_load)
    run("deactivated item disappears on next load",     test_deactivated_item_disappears_on_next_load)
    run("pending-moderation item never shown",          test_pending_moderation_item_never_shown)
    run("no stale Cache-Control header on /landing",    test_no_caching_header_on_landing_response)
    run("active property_sale appears in preview",      test_active_property_sale_appears_in_preview)
    run("sold property_sale disappears on next load",   test_sold_property_sale_disappears_on_next_load)
    run("active rental appears in preview",             test_active_rental_appears_in_preview)
    run("sold rental disappears on next load",          test_sold_rental_disappears_on_next_load)

    print(f"\n{'='*60}")
    print(f"Results: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nFailures:")
        for name, exc in FAIL:
            print(f"  {name}: {exc}")
        sys.exit(1)
    else:
        print("All tests passed.")
