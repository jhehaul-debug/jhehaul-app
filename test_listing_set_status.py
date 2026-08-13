"""
Tests for the /listing/<id>/status route (Reserve and Reactivate quick-actions).

Covers:
  - active → reserved   (Reserve button)
  - reserved → active   (Reactivate button, expires_at refreshed)
  - sold    → active    (Reactivate button, sold_at cleared)
  - draft   → reserved  (invalid transition — rejected)
  - arbitrary status value (rejected)
  - missing CSRF token  (400)
  - non-owner           (403)
"""

import sys
import uuid
import datetime
from unittest.mock import patch

from app import app, db
import routes  # registers all URL rules

# ── Fixtures ───────────────────────────────────────────────────────────────────

def _create_seller():
    from models import User
    u = User()
    u.id                    = str(uuid.uuid4())
    u.email                 = f"seller_{u.id[:8]}@test.local"
    u.user_type             = "customer"
    u.is_admin              = False
    u.age_confirmed         = True
    u.profile_nudge_dismissed = True
    db.session.add(u)
    db.session.commit()
    return u.id


def _create_listing(seller_id, status="active"):
    from models import Listing
    l = Listing()
    l.seller_id         = seller_id
    l.title             = "Test listing"
    l.status            = status
    l.listing_type      = "item"
    l.moderation_status = "approved"
    l.price_type        = "fixed"
    l.price             = 25.00
    l.expires_at        = datetime.datetime.now() + datetime.timedelta(days=10)
    db.session.add(l)
    db.session.commit()
    return l.id


def _cleanup(seller_id, listing_id):
    from models import User, Listing
    with app.app_context():
        if listing_id:
            l = Listing.query.get(listing_id)
            if l:
                db.session.delete(l)
        if seller_id:
            u = User.query.get(seller_id)
            if u:
                db.session.delete(u)
        db.session.commit()


def _post_status(client, listing_id, new_status, token="dummy-token"):
    return client.post(
        f"/listing/{listing_id}/status",
        data={"status": new_status, "csrf_token": token},
        follow_redirects=False,
    )


# ── Test helpers ───────────────────────────────────────────────────────────────

PASS = []
FAIL = []


def run(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  PASS  {name}")
    except Exception as exc:
        FAIL.append((name, exc))
        print(f"  FAIL  {name}: {exc}")


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_active_to_reserved():
    """Seller can mark an active listing as reserved."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id  = _create_seller()
        listing_id = _create_listing(seller_id, status="active")
        from models import User
        seller = User.query.get(seller_id)

    try:
        with app.test_client() as client:
            with patch("flask_login.utils._get_user", return_value=seller), \
                 patch("flask_wtf.csrf.validate_csrf"):
                resp = _post_status(client, listing_id, "reserved")

            assert resp.status_code in (302, 303), (
                f"Expected redirect, got {resp.status_code}"
            )

            with app.app_context():
                from models import Listing
                updated = Listing.query.get(listing_id)
                assert updated.status == "reserved", (
                    f"Expected 'reserved', got '{updated.status}'"
                )
    finally:
        _cleanup(seller_id, listing_id)


def test_reserved_to_active_refreshes_expiry():
    """Reactivating a reserved listing sets status=active and refreshes expires_at."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id  = _create_seller()
        listing_id = _create_listing(seller_id, status="reserved")
        # Set expiry in the past so the refresh branch is exercised
        from models import Listing, User
        l = Listing.query.get(listing_id)
        l.expires_at = datetime.datetime.now() - datetime.timedelta(days=5)
        db.session.commit()
        seller = User.query.get(seller_id)

    try:
        with app.test_client() as client:
            with patch("flask_login.utils._get_user", return_value=seller), \
                 patch("flask_wtf.csrf.validate_csrf"):
                resp = _post_status(client, listing_id, "active")

            assert resp.status_code in (302, 303), (
                f"Expected redirect, got {resp.status_code}"
            )

            with app.app_context():
                from models import Listing
                updated = Listing.query.get(listing_id)
                assert updated.status == "active", (
                    f"Expected 'active', got '{updated.status}'"
                )
                assert updated.expires_at is not None, "expires_at should be set"
                assert updated.expires_at > datetime.datetime.now(), (
                    "expires_at should be in the future after reactivation"
                )
    finally:
        _cleanup(seller_id, listing_id)


def test_sold_to_active_clears_sold_at():
    """Reactivating a sold listing clears sold_at and refreshes expires_at."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id  = _create_seller()
        listing_id = _create_listing(seller_id, status="sold")
        from models import Listing, User
        l = Listing.query.get(listing_id)
        l.sold_at    = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        l.expires_at = None  # force the refresh branch
        db.session.commit()
        seller = User.query.get(seller_id)

    try:
        with app.test_client() as client:
            with patch("flask_login.utils._get_user", return_value=seller), \
                 patch("flask_wtf.csrf.validate_csrf"):
                resp = _post_status(client, listing_id, "active")

            assert resp.status_code in (302, 303), (
                f"Expected redirect, got {resp.status_code}"
            )

            with app.app_context():
                from models import Listing
                updated = Listing.query.get(listing_id)
                assert updated.status == "active", (
                    f"Expected 'active', got '{updated.status}'"
                )
                assert updated.sold_at is None, "sold_at should be cleared on reactivation"
                assert updated.expires_at is not None, "expires_at should be set"
                assert updated.expires_at > datetime.datetime.now(), (
                    "expires_at should be in the future"
                )
    finally:
        _cleanup(seller_id, listing_id)


def test_draft_to_reserved_rejected():
    """A draft listing cannot be marked reserved — invalid transition."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id  = _create_seller()
        listing_id = _create_listing(seller_id, status="draft")
        from models import User
        seller = User.query.get(seller_id)

    try:
        with app.test_client() as client:
            with patch("flask_login.utils._get_user", return_value=seller), \
                 patch("flask_wtf.csrf.validate_csrf"):
                resp = _post_status(client, listing_id, "reserved")

            # Route redirects with an error flash rather than changing status
            assert resp.status_code in (302, 303), (
                f"Expected redirect, got {resp.status_code}"
            )

            with app.app_context():
                from models import Listing
                updated = Listing.query.get(listing_id)
                assert updated.status == "draft", (
                    f"Status should remain 'draft', got '{updated.status}'"
                )
    finally:
        _cleanup(seller_id, listing_id)


def test_invalid_status_value_rejected():
    """Posting an unknown status string is rejected; listing unchanged."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id  = _create_seller()
        listing_id = _create_listing(seller_id, status="active")
        from models import User
        seller = User.query.get(seller_id)

    try:
        with app.test_client() as client:
            with patch("flask_login.utils._get_user", return_value=seller), \
                 patch("flask_wtf.csrf.validate_csrf"):
                resp = _post_status(client, listing_id, "hacked")

            assert resp.status_code in (302, 303), (
                f"Expected redirect, got {resp.status_code}"
            )

            with app.app_context():
                from models import Listing
                updated = Listing.query.get(listing_id)
                assert updated.status == "active", (
                    f"Status should remain 'active', got '{updated.status}'"
                )
    finally:
        _cleanup(seller_id, listing_id)


def test_missing_csrf_token_returns_400():
    """A POST with no CSRF token must return 400 — CSRF check is enforced."""
    seller_id = listing_id = None
    with app.app_context():
        seller_id  = _create_seller()
        listing_id = _create_listing(seller_id, status="active")
        from models import User
        seller = User.query.get(seller_id)

    try:
        with app.test_client() as client:
            with patch("flask_login.utils._get_user", return_value=seller):
                # Do NOT patch validate_csrf — let the real CSRF check run
                resp = client.post(
                    f"/listing/{listing_id}/status",
                    data={"status": "reserved"},  # csrf_token deliberately omitted
                    follow_redirects=False,
                )

        assert resp.status_code == 400, (
            f"Expected 400 for missing CSRF token, got {resp.status_code}"
        )
    finally:
        _cleanup(seller_id, listing_id)


def test_expired_to_active_renews_listing():
    """Renewing an expired listing resets expiry fields and preserves original data."""
    seller_id = listing_id = photo_id = None
    with app.app_context():
        seller_id  = _create_seller()
        listing_id = _create_listing(seller_id, status="expired")
        from models import Listing, ListingPhoto, User
        l = Listing.query.get(listing_id)
        # Set fields that should be cleared/reset on renewal
        l.expired_at             = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        l.expiry_reminder_sent   = True
        l.expires_at             = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        # Capture original data that must survive renewal
        original_title = l.title
        original_price = l.price
        # Attach a photo to confirm it is not removed
        photo = ListingPhoto()
        photo.listing_id   = listing_id
        photo.filename     = "test_photo.jpg"
        photo.content_type = "image/jpeg"
        db.session.add(photo)
        db.session.commit()
        photo_id = photo.id
        seller = User.query.get(seller_id)

    try:
        with app.test_client() as client:
            with patch("flask_login.utils._get_user", return_value=seller), \
                 patch("flask_wtf.csrf.validate_csrf"):
                resp = _post_status(client, listing_id, "active")

        assert resp.status_code in (302, 303), (
            f"Expected redirect, got {resp.status_code}"
        )

        now = datetime.datetime.now()
        with app.app_context():
            from models import Listing, ListingPhoto
            updated = Listing.query.get(listing_id)

            # Status must be active
            assert updated.status == "active", (
                f"Expected 'active', got '{updated.status}'"
            )
            # expires_at must be ~30 days in the future (within a 5-minute window)
            assert updated.expires_at is not None, "expires_at should be set after renewal"
            assert updated.expires_at > now, "expires_at should be in the future"
            expected_expiry = now + datetime.timedelta(days=30)
            delta = abs((updated.expires_at - expected_expiry).total_seconds())
            assert delta < 300, (
                f"expires_at should be ~30 days from now, delta was {delta}s"
            )
            # expired_at must be cleared
            assert updated.expired_at is None, (
                f"expired_at should be None after renewal, got '{updated.expired_at}'"
            )
            # expiry_reminder_sent must be reset to False
            assert updated.expiry_reminder_sent == False, (
                f"expiry_reminder_sent should be False after renewal, got {updated.expiry_reminder_sent}"
            )
            # Original listing data must be unchanged
            assert updated.title == original_title, (
                f"Title changed: expected '{original_title}', got '{updated.title}'"
            )
            assert updated.price == original_price, (
                f"Price changed: expected {original_price}, got {updated.price}"
            )
            # Photos must still exist
            remaining_photos = ListingPhoto.query.filter_by(listing_id=listing_id).all()
            assert len(remaining_photos) == 1, (
                f"Expected 1 photo to survive renewal, found {len(remaining_photos)}"
            )
    finally:
        with app.app_context():
            from models import ListingPhoto
            if photo_id:
                p = ListingPhoto.query.get(photo_id)
                if p:
                    db.session.delete(p)
                    db.session.commit()
        _cleanup(seller_id, listing_id)


def test_non_owner_gets_403():
    """A different authenticated user cannot change another seller's listing."""
    owner_id = attacker_id = listing_id = None
    with app.app_context():
        owner_id    = _create_seller()
        attacker_id = _create_seller()
        listing_id  = _create_listing(owner_id, status="active")
        from models import User
        attacker = User.query.get(attacker_id)

    try:
        with app.test_client() as client:
            with patch("flask_login.utils._get_user", return_value=attacker), \
                 patch("flask_wtf.csrf.validate_csrf"):
                resp = _post_status(client, listing_id, "reserved")

        assert resp.status_code == 403, (
            f"Expected 403 for non-owner, got {resp.status_code}"
        )

        with app.app_context():
            from models import Listing
            unchanged = Listing.query.get(listing_id)
            assert unchanged.status == "active", (
                f"Listing status should be unchanged, got '{unchanged.status}'"
            )
    finally:
        _cleanup(owner_id, listing_id)
        _cleanup(attacker_id, None)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nRunning listing_set_status tests...\n")

    run("active → reserved",                          test_active_to_reserved)
    run("reserved → active (expiry refreshed)",       test_reserved_to_active_refreshes_expiry)
    run("sold → active (sold_at cleared)",            test_sold_to_active_clears_sold_at)
    run("expired → active (renewal preserves data)",  test_expired_to_active_renews_listing)
    run("draft → reserved rejected",                  test_draft_to_reserved_rejected)
    run("invalid status value rejected",              test_invalid_status_value_rejected)
    run("missing CSRF token → 400",                   test_missing_csrf_token_returns_400)
    run("non-owner → 403",                            test_non_owner_gets_403)

    print(f"\n{'='*50}")
    print(f"Results: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nFailures:")
        for name, exc in FAIL:
            print(f"  {name}: {exc}")
        sys.exit(1)
    else:
        print("All tests passed.")
