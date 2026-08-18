"""
test_gallery_pin_delete.py — confirm GalleryPhoto rows (item_type='listing') are
hard-deleted (not merely deactivated) when a listing is removed.

Covers:
  1. Seller deletes a listing via the /listing/<id>/delete route →
     the GalleryPhoto pin is gone from the DB.
  2. Seller deletes their account via /account/delete →
     GalleryPhoto pins for all their listings are gone from the DB.
"""

import sys
import uuid
from unittest.mock import patch, MagicMock

from app import app, db
import routes  # registers all URL rules


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _create_user(email_prefix="seller"):
    from models import User
    u = User()
    u.id = str(uuid.uuid4())
    u.email = f"{email_prefix}_{u.id[:8]}@test.local"
    u.user_type = "customer"
    u.is_admin = False
    u.age_confirmed = True
    u.profile_nudge_dismissed = True
    db.session.add(u)
    db.session.commit()
    return u.id


def _create_listing(seller_id, status="active"):
    from models import Listing
    l = Listing()
    l.seller_id = seller_id
    l.title = "Gallery pin test listing"
    l.status = status
    l.listing_type = "item"
    l.moderation_status = "approved"
    l.price_type = "fixed"
    l.price = 50.00
    db.session.add(l)
    db.session.commit()
    return l.id


def _create_gallery_pin(listing_id, is_active=True):
    from models import GalleryPhoto
    gp = GalleryPhoto()
    gp.item_type = "listing"
    gp.listing_id = listing_id
    gp.is_active = is_active
    gp.display_order = 0
    gp.filename = ""
    db.session.add(gp)
    db.session.commit()
    return gp.id


def _cleanup(*model_id_pairs):
    """Delete rows by (Model, id) pairs; silently skip missing rows."""
    with app.app_context():
        for Model, row_id in model_id_pairs:
            if row_id is None:
                continue
            obj = Model.query.get(row_id)
            if obj:
                db.session.delete(obj)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


# ── Test runner ────────────────────────────────────────────────────────────────

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


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_gallery_pin_deleted_when_listing_deleted():
    """Deleting a listing via the seller delete route hard-deletes its GalleryPhoto pin."""
    seller_id = listing_id = gp_id = None
    with app.app_context():
        seller_id  = _create_user("seller")
        listing_id = _create_listing(seller_id, status="active")
        gp_id      = _create_gallery_pin(listing_id, is_active=True)

        # Confirm the pin exists before deletion
        from models import GalleryPhoto
        assert GalleryPhoto.query.get(gp_id) is not None, \
            "GalleryPhoto pin should exist before listing deletion"

        seller = __import__("models").User.query.get(seller_id)

    try:
        with app.test_client() as client:
            with patch("flask_login.utils._get_user", return_value=seller), \
                 patch("flask_wtf.csrf.validate_csrf"), \
                 patch("routes.notify_buyer_offer_expired", MagicMock()), \
                 patch("storage.delete_file", MagicMock()):
                resp = client.post(
                    f"/listing/{listing_id}/delete",
                    data={"csrf_token": "dummy"},
                    follow_redirects=False,
                )

        assert resp.status_code in (302, 303), (
            f"Expected redirect after delete, got {resp.status_code}"
        )

        with app.app_context():
            from models import GalleryPhoto, Listing
            # Listing row must be gone
            assert Listing.query.get(listing_id) is None, \
                "Listing row should be deleted from the database"
            # Gallery pin must be hard-deleted (not just deactivated)
            pin = GalleryPhoto.query.get(gp_id)
            assert pin is None, (
                f"GalleryPhoto pin (id={gp_id}) should be deleted, "
                f"but it still exists with is_active={getattr(pin, 'is_active', '?')}"
            )
        # IDs are now gone; nothing to clean up
        listing_id = gp_id = None
    finally:
        from models import GalleryPhoto, Listing, User
        _cleanup(
            (GalleryPhoto, gp_id),
            (Listing, listing_id),
            (User, seller_id),
        )


def test_gallery_pin_deleted_when_account_self_deleted():
    """Account self-delete hard-deletes GalleryPhoto pins for every seller listing."""
    seller_id = listing_id_a = listing_id_b = gp_id_a = gp_id_b = None
    with app.app_context():
        seller_id    = _create_user("acct_seller")
        listing_id_a = _create_listing(seller_id, status="active")
        listing_id_b = _create_listing(seller_id, status="reserved")
        gp_id_a      = _create_gallery_pin(listing_id_a, is_active=True)
        gp_id_b      = _create_gallery_pin(listing_id_b, is_active=False)

        # Confirm both pins exist before deletion
        from models import GalleryPhoto
        assert GalleryPhoto.query.get(gp_id_a) is not None, \
            "Gallery pin A should exist before account deletion"
        assert GalleryPhoto.query.get(gp_id_b) is not None, \
            "Gallery pin B should exist before account deletion"

        seller = __import__("models").User.query.get(seller_id)

    try:
        with app.test_client() as client:
            with patch("flask_login.utils._get_user", return_value=seller), \
                 patch("flask_wtf.csrf.validate_csrf"), \
                 patch("routes.notify_admin_user_deleted", MagicMock()), \
                 patch("storage.delete_file", MagicMock()):
                resp = client.post(
                    "/account/delete",
                    data={"confirm_delete": "DELETE", "csrf_token": "dummy"},
                    follow_redirects=False,
                )

        assert resp.status_code in (302, 303), (
            f"Expected redirect after account delete, got {resp.status_code}"
        )

        with app.app_context():
            from models import GalleryPhoto, Listing, User
            # User and both listings should be gone
            assert User.query.get(seller_id) is None, \
                "Seller User row should be deleted from the database"
            assert Listing.query.get(listing_id_a) is None, \
                "Listing A should be deleted along with the account"
            assert Listing.query.get(listing_id_b) is None, \
                "Listing B should be deleted along with the account"

            # Both gallery pins must be hard-deleted
            pin_a = GalleryPhoto.query.get(gp_id_a)
            assert pin_a is None, (
                f"GalleryPhoto pin A (id={gp_id_a}) should be deleted, "
                f"but it still exists (is_active={getattr(pin_a, 'is_active', '?')})"
            )
            pin_b = GalleryPhoto.query.get(gp_id_b)
            assert pin_b is None, (
                f"GalleryPhoto pin B (id={gp_id_b}) should be deleted, "
                f"but it still exists (is_active={getattr(pin_b, 'is_active', '?')})"
            )
        # All rows are gone; skip cleanup
        seller_id = listing_id_a = listing_id_b = gp_id_a = gp_id_b = None
    finally:
        from models import GalleryPhoto, Listing, User
        _cleanup(
            (GalleryPhoto, gp_id_a),
            (GalleryPhoto, gp_id_b),
            (Listing, listing_id_a),
            (Listing, listing_id_b),
            (User, seller_id),
        )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nRunning gallery pin deletion tests...\n")

    run("gallery pin hard-deleted when seller deletes listing",
        test_gallery_pin_deleted_when_listing_deleted)
    run("gallery pins hard-deleted when seller deletes account",
        test_gallery_pin_deleted_when_account_self_deleted)

    print(f"\n{'='*60}")
    print(f"Results: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nFailures:")
        for name, exc in FAIL:
            print(f"  {name}: {exc}")
        sys.exit(1)
    else:
        print("All tests passed.")
