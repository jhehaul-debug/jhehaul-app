"""
test_listing_offer_expiry.py — confirm that pending offers are expired when a
listing's auto-expiry fires via the background job (_run_checks in job_expiry.py).

Covers:
  - Listing whose expires_at has passed is transitioned to 'expired'
  - Pending offers on that listing become 'expired'
  - Countered offers on that listing become 'expired'
  - Listing not yet past expires_at is left untouched
  - Listing that is already 'sold' is left untouched
"""

import sys
import uuid
import datetime
from unittest.mock import patch, MagicMock

from app import app, db
import routes  # registers all URL rules


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _create_seller():
    from models import User
    u = User()
    u.id = str(uuid.uuid4())
    u.email = f"seller_{u.id[:8]}@test.local"
    u.user_type = "customer"
    u.is_admin = False
    u.age_confirmed = True
    u.profile_nudge_dismissed = True
    db.session.add(u)
    db.session.commit()
    return u.id


def _create_buyer():
    from models import User
    u = User()
    u.id = str(uuid.uuid4())
    u.email = f"buyer_{u.id[:8]}@test.local"
    u.user_type = "customer"
    u.is_admin = False
    u.age_confirmed = True
    u.profile_nudge_dismissed = True
    db.session.add(u)
    db.session.commit()
    return u.id


def _create_listing(seller_id, status="active", expires_at=None):
    from models import Listing
    l = Listing()
    l.seller_id = seller_id
    l.title = "Auto-expiry test listing"
    l.status = status
    l.listing_type = "item"
    l.moderation_status = "approved"
    l.price_type = "fixed"
    l.price = 50.00
    l.expires_at = expires_at
    db.session.add(l)
    db.session.commit()
    return l.id


def _create_offer(listing_id, buyer_id, seller_id, status="pending", amount=40.00):
    from models import ListingOffer
    o = ListingOffer()
    o.listing_id = listing_id
    o.buyer_id = buyer_id
    o.seller_id = seller_id
    o.amount = amount
    o.status = status
    o.message = "Test offer"
    db.session.add(o)
    db.session.commit()
    return o.id


def _cleanup(*ids_by_model):
    """Delete rows by (Model, id) pairs in order."""
    with app.app_context():
        for Model, row_id in ids_by_model:
            if row_id is None:
                continue
            obj = Model.query.get(row_id)
            if obj:
                db.session.delete(obj)
        db.session.commit()


# ── Test helpers ───────────────────────────────────────────────────────────────

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


# ── Patch targets: suppress all outbound notifications ─────────────────────────
# The notification functions are imported *inside* _run_checks, so we must patch
# them at their source modules (email_service / sms_service), not on job_expiry.

_NOTIFY_PATCHES = [
    "email_service.notify_seller_listing_expired",
    "email_service.notify_seller_listing_expiring_soon",
    "email_service.notify_customer_appointment_reminder",
    "email_service.notify_customer_pending_bids_reminder",
    "email_service.notify_customer_job_expiring_soon",
    "email_service.notify_admin_job_expired",
    "sms_service.notify_seller_listing_expired_sms",
    "sms_service.notify_customer_appointment_reminder_sms",
]


def _run_checks_silent():
    """Call _run_checks with all outbound notifications suppressed."""
    from job_expiry import _run_checks
    patches = [patch(p, MagicMock()) for p in _NOTIFY_PATCHES]
    for p in patches:
        p.start()
    try:
        _run_checks(app)
    finally:
        for p in patches:
            p.stop()


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_expired_listing_transitions_to_expired():
    """A listing whose expires_at has passed is auto-expired by _run_checks."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id = _create_seller()
        past = datetime.datetime.now() - datetime.timedelta(hours=1)
        listing_id = _create_listing(seller_id, status="active", expires_at=past)

    try:
        _run_checks_silent()

        with app.app_context():
            from models import Listing
            lst = Listing.query.get(listing_id)
            assert lst.status == "expired", (
                f"Expected listing status 'expired', got '{lst.status}'"
            )
            assert lst.expired_at is not None, "expired_at should be stamped"
    finally:
        with app.app_context():
            from models import Listing, User
            _cleanup((Listing, listing_id), (User, seller_id))


def test_pending_offers_expired_on_listing_auto_expiry():
    """Pending offers are set to 'expired' when the listing auto-expires."""
    seller_id = buyer_id = listing_id = offer_id = None
    with app.app_context():
        seller_id = _create_seller()
        buyer_id  = _create_buyer()
        past = datetime.datetime.now() - datetime.timedelta(hours=2)
        listing_id = _create_listing(seller_id, status="active", expires_at=past)
        offer_id   = _create_offer(listing_id, buyer_id, seller_id, status="pending")

    try:
        _run_checks_silent()

        with app.app_context():
            from models import Listing, ListingOffer
            lst = Listing.query.get(listing_id)
            offer = ListingOffer.query.get(offer_id)
            assert lst.status == "expired", (
                f"Listing should be 'expired', got '{lst.status}'"
            )
            assert offer.status == "expired", (
                f"Pending offer should be 'expired', got '{offer.status}'"
            )
    finally:
        with app.app_context():
            from models import Listing, ListingOffer, User
            _cleanup(
                (ListingOffer, offer_id),
                (Listing, listing_id),
                (User, buyer_id),
                (User, seller_id),
            )


def test_countered_offers_expired_on_listing_auto_expiry():
    """Countered offers (not just pending) are also expired when the listing auto-expires."""
    seller_id = buyer_id = listing_id = offer_id = None
    with app.app_context():
        seller_id  = _create_seller()
        buyer_id   = _create_buyer()
        past = datetime.datetime.now() - datetime.timedelta(hours=3)
        listing_id = _create_listing(seller_id, status="active", expires_at=past)
        offer_id   = _create_offer(listing_id, buyer_id, seller_id, status="countered")

    try:
        _run_checks_silent()

        with app.app_context():
            from models import Listing, ListingOffer
            lst = Listing.query.get(listing_id)
            offer = ListingOffer.query.get(offer_id)
            assert lst.status == "expired", (
                f"Listing should be 'expired', got '{lst.status}'"
            )
            assert offer.status == "expired", (
                f"Countered offer should be 'expired', got '{offer.status}'"
            )
    finally:
        with app.app_context():
            from models import Listing, ListingOffer, User
            _cleanup(
                (ListingOffer, offer_id),
                (Listing, listing_id),
                (User, buyer_id),
                (User, seller_id),
            )


def test_reserved_listing_with_future_expiry_not_touched():
    """A listing whose expires_at is still in the future is left untouched."""
    seller_id = listing_id = offer_id = buyer_id = None
    with app.app_context():
        seller_id  = _create_seller()
        buyer_id   = _create_buyer()
        future = datetime.datetime.now() + datetime.timedelta(days=5)
        listing_id = _create_listing(seller_id, status="active", expires_at=future)
        offer_id   = _create_offer(listing_id, buyer_id, seller_id, status="pending")

    try:
        _run_checks_silent()

        with app.app_context():
            from models import Listing, ListingOffer
            lst = Listing.query.get(listing_id)
            offer = ListingOffer.query.get(offer_id)
            assert lst.status == "active", (
                f"Listing should remain 'active', got '{lst.status}'"
            )
            assert offer.status == "pending", (
                f"Offer should remain 'pending', got '{offer.status}'"
            )
    finally:
        with app.app_context():
            from models import Listing, ListingOffer, User
            _cleanup(
                (ListingOffer, offer_id),
                (Listing, listing_id),
                (User, buyer_id),
                (User, seller_id),
            )


def test_sold_listing_not_auto_expired_by_job():
    """A listing already marked 'sold' is not touched by the expiry job."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id  = _create_seller()
        past = datetime.datetime.now() - datetime.timedelta(hours=1)
        listing_id = _create_listing(seller_id, status="sold", expires_at=past)

    try:
        _run_checks_silent()

        with app.app_context():
            from models import Listing
            lst = Listing.query.get(listing_id)
            assert lst.status == "sold", (
                f"Sold listing should remain 'sold', got '{lst.status}'"
            )
    finally:
        with app.app_context():
            from models import Listing, User
            _cleanup((Listing, listing_id), (User, seller_id))


def test_multiple_offers_all_expired():
    """All pending/countered offers on an auto-expired listing are bulk-expired."""
    seller_id = buyer_id = listing_id = None
    offer_ids = []
    with app.app_context():
        seller_id  = _create_seller()
        buyer_id   = _create_buyer()
        past = datetime.datetime.now() - datetime.timedelta(hours=1)
        listing_id = _create_listing(seller_id, status="active", expires_at=past)
        offer_ids.append(_create_offer(listing_id, buyer_id, seller_id, status="pending",   amount=30.00))
        offer_ids.append(_create_offer(listing_id, buyer_id, seller_id, status="countered", amount=35.00))
        offer_ids.append(_create_offer(listing_id, buyer_id, seller_id, status="pending",   amount=45.00))

    try:
        _run_checks_silent()

        with app.app_context():
            from models import ListingOffer
            for oid in offer_ids:
                offer = ListingOffer.query.get(oid)
                assert offer.status == "expired", (
                    f"Offer #{oid} should be 'expired', got '{offer.status}'"
                )
    finally:
        with app.app_context():
            from models import Listing, ListingOffer, User
            for oid in offer_ids:
                _cleanup((ListingOffer, oid),)
            _cleanup((Listing, listing_id), (User, buyer_id), (User, seller_id))


def test_accepted_offer_not_expired_by_listing_auto_expiry():
    """An already-accepted offer is not changed when a listing auto-expires."""
    seller_id = buyer_id = listing_id = offer_id = None
    with app.app_context():
        seller_id  = _create_seller()
        buyer_id   = _create_buyer()
        past = datetime.datetime.now() - datetime.timedelta(hours=1)
        listing_id = _create_listing(seller_id, status="active", expires_at=past)
        offer_id   = _create_offer(listing_id, buyer_id, seller_id, status="accepted")

    try:
        _run_checks_silent()

        with app.app_context():
            from models import ListingOffer
            offer = ListingOffer.query.get(offer_id)
            assert offer.status == "accepted", (
                f"Accepted offer should remain 'accepted', got '{offer.status}'"
            )
    finally:
        with app.app_context():
            from models import Listing, ListingOffer, User
            _cleanup(
                (ListingOffer, offer_id),
                (Listing, listing_id),
                (User, buyer_id),
                (User, seller_id),
            )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nRunning listing offer expiry tests...\n")

    run("expired listing transitions to 'expired'",
        test_expired_listing_transitions_to_expired)
    run("pending offers expired on listing auto-expiry",
        test_pending_offers_expired_on_listing_auto_expiry)
    run("countered offers expired on listing auto-expiry",
        test_countered_offers_expired_on_listing_auto_expiry)
    run("listing with future expiry left untouched",
        test_reserved_listing_with_future_expiry_not_touched)
    run("sold listing not touched by expiry job",
        test_sold_listing_not_auto_expired_by_job)
    run("multiple offers all expired on auto-expiry",
        test_multiple_offers_all_expired)
    run("accepted offer not changed by auto-expiry",
        test_accepted_offer_not_expired_by_listing_auto_expiry)

    print(f"\n{'='*60}")
    print(f"Results: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nFailures:")
        for name, exc in FAIL:
            print(f"  {name}: {exc}")
        sys.exit(1)
    else:
        print("All tests passed.")
