"""
Task 115 validation: admin listings Housing category filter + startup backfill.

Run with:  python tests/test_housing_category_filter.py

Verifies (against the configured DB):
- A property listing with category_id=NULL appears in /admin/listings when the
  Housing category filter is selected (matched via listing_type fallback in the route).
- A rental listing with category_id=NULL also appears under that filter.
- A property listing with category_id already set to housing appears.
- Non-property (item) listings do NOT appear under the housing filter.
- The startup backfill function (app.backfill_housing_category_ids) sets category_id
  on property rows that had NULL, and emits the expected log message.
"""
import sys
import os
import uuid
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flask_login.utils as _flu
from app import app, backfill_housing_category_ids
import routes  # noqa: F401 — registers routes on the app
from models import db, User, Category, Listing

# ── helpers ───────────────────────────────────────────────────────────────────

results = []


def check(name, cond, extra=""):
    results.append((name, cond))
    status = "PASS" if cond else "FAIL"
    print(f"{status} - {name}", extra if not cond else "")


# ── fixtures ──────────────────────────────────────────────────────────────────

PREFIX = f"t115-{uuid.uuid4().hex[:6]}"
SELLER_ID = f"{PREFIX}-seller"

null_cat_listing_id = None
null_cat_rental_id = None
cat_set_listing_id = None
item_listing_id = None


def cleanup():
    """Remove all test rows; called in finally so it always runs."""
    with app.app_context():
        for lid in [null_cat_listing_id, null_cat_rental_id, cat_set_listing_id, item_listing_id]:
            if lid is not None:
                row = db.session.get(Listing, lid)
                if row:
                    db.session.delete(row)
        seller = db.session.get(User, SELLER_ID)
        if seller:
            db.session.delete(seller)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


with app.app_context():
    admin = User.query.filter_by(is_admin=True).first()
    assert admin, "Need an admin user in the dev DB to run these tests"

    # Create a throwaway seller
    seller = db.session.get(User, SELLER_ID)
    if not seller:
        seller = User(
            id=SELLER_ID,
            email=f"{PREFIX}@example.com",
            first_name="T115Test",
            user_type="customer",
            age_confirmed=True,
        )
        db.session.add(seller)
        db.session.commit()

    housing_cat = Category.query.filter_by(slug="housing").first()
    assert housing_cat, "Housing category must exist (seeded at startup)"

    try:
        # ── Create test listings ─────────────────────────────────────────────

        # 1. property_sale with category_id=NULL (simulates a legacy row)
        null_cat_listing = Listing(
            seller_id=SELLER_ID,
            title=f"{PREFIX} PropSale NullCat",
            description="Test property listing, no category_id set",
            listing_type="property_sale",
            category_id=None,
            status="active",
            price=250000,
            moderation_status="approved",
        )
        db.session.add(null_cat_listing)

        # 2. rental with category_id=NULL
        null_cat_rental = Listing(
            seller_id=SELLER_ID,
            title=f"{PREFIX} Rental NullCat",
            description="Test rental listing, no category_id set",
            listing_type="rental",
            category_id=None,
            status="active",
            price=1500,
            moderation_status="approved",
        )
        db.session.add(null_cat_rental)

        # 3. property_sale with category_id already set to housing
        cat_set_listing = Listing(
            seller_id=SELLER_ID,
            title=f"{PREFIX} PropSale WithCat",
            description="Test property listing with housing category_id already set",
            listing_type="property_sale",
            category_id=housing_cat.id,
            status="active",
            price=300000,
            moderation_status="approved",
        )
        db.session.add(cat_set_listing)

        # 4. regular item listing — must NOT appear under housing filter
        item_listing = Listing(
            seller_id=SELLER_ID,
            title=f"{PREFIX} Item NoCat",
            description="Test item listing, should not match housing filter",
            listing_type="item",
            category_id=None,
            status="active",
            price=50,
            moderation_status="approved",
        )
        db.session.add(item_listing)

        db.session.flush()

        null_cat_listing_id = null_cat_listing.id
        null_cat_rental_id  = null_cat_rental.id
        cat_set_listing_id  = cat_set_listing.id
        item_listing_id     = item_listing.id

        db.session.commit()

        # Patch flask-login so requests run as admin
        _flu._get_user = lambda: admin
        client = app.test_client()

        # ── Test 1: NULL-category property_sale appears under housing filter ──
        r = client.get(f"/admin/listings?category={housing_cat.id}")
        check("admin/listings returns 200", r.status_code == 200)
        body = r.data.decode("utf-8", errors="replace")

        check(
            "property_sale with NULL category_id appears under housing filter",
            null_cat_listing.title in body,
            f"title={null_cat_listing.title!r} not found",
        )

        # ── Test 2: NULL-category rental appears under housing filter ─────────
        check(
            "rental with NULL category_id appears under housing filter",
            null_cat_rental.title in body,
            f"title={null_cat_rental.title!r} not found",
        )

        # ── Test 3: property_sale WITH category_id set also appears ───────────
        check(
            "property_sale with category_id set appears under housing filter",
            cat_set_listing.title in body,
            f"title={cat_set_listing.title!r} not found",
        )

        # ── Test 4: item listing NOT matched by housing filter ────────────────
        check(
            "item listing with NULL category_id is excluded from housing filter",
            item_listing.title not in body,
            f"item title={item_listing.title!r} wrongly appeared under housing filter",
        )

        # ── Test 5: unfiltered admin listings includes all four ───────────────
        r_all = client.get("/admin/listings")
        body_all = r_all.data.decode("utf-8", errors="replace")
        check(
            "unfiltered admin/listings shows all test listings",
            all(t in body_all for t in [
                null_cat_listing.title,
                null_cat_rental.title,
                cat_set_listing.title,
                item_listing.title,
            ]),
        )

        # ── Tests 6-9: real backfill function updates NULL-category rows ───────
        # Reset to NULL to simulate pre-backfill state
        null_cat_listing.category_id = None
        null_cat_rental.category_id  = None
        db.session.commit()

        # Capture logs produced by the actual backfill implementation
        log_records = []

        class _LogCapture(logging.Handler):
            def emit(self, record):
                log_records.append(record.getMessage())

        handler = _LogCapture()
        logging.getLogger().addHandler(handler)
        try:
            n_updated = backfill_housing_category_ids(Category, Listing)
        finally:
            logging.getLogger().removeHandler(handler)

        check(
            "backfill updates at least 2 NULL-category property rows",
            n_updated >= 2,
            f"returned n_updated={n_updated}",
        )

        db.session.expire_all()
        refreshed_null   = db.session.get(Listing, null_cat_listing_id)
        refreshed_rental = db.session.get(Listing, null_cat_rental_id)

        check(
            "backfill sets housing category_id on property_sale row",
            refreshed_null.category_id == housing_cat.id,
            f"got category_id={refreshed_null.category_id}",
        )
        check(
            "backfill sets housing category_id on rental row",
            refreshed_rental.category_id == housing_cat.id,
            f"got category_id={refreshed_rental.category_id}",
        )

        # ── Test 10: backfill emits the expected log line ─────────────────────
        # Reset one row to NULL so the backfill has something to report, then
        # confirm the log message matches the format in the production code.
        null_cat_listing.category_id = None
        db.session.commit()

        log_records2 = []

        class _LogCapture2(logging.Handler):
            def emit(self, record):
                log_records2.append(record.getMessage())

        handler2 = _LogCapture2()
        logging.getLogger().addHandler(handler2)
        try:
            backfill_housing_category_ids(Category, Listing)
        finally:
            logging.getLogger().removeHandler(handler2)

        backfill_logged = any(
            "Backfill: set category_id=" in msg and "housing" in msg and "existing property listings" in msg
            for msg in log_records2
        )
        check(
            "backfill emits log line when NULL-category property listings exist",
            backfill_logged,
            f"captured log messages: {log_records2}",
        )

    finally:
        cleanup()

# ── Summary ───────────────────────────────────────────────────────────────────
failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
sys.exit(1 if failed else 0)
