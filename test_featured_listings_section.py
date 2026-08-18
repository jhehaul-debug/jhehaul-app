"""
test_featured_listings_section.py — confirm the Featured Listings section on
the marketplace homepage shows and hides correctly.

Covers:
  1. Section appears on / when at least one active gallery_photo exists
  2. Section is hidden when gallery_photos table is empty
  3. Section is hidden when all gallery_photos are inactive (is_active=False)
  4. Section does NOT appear on /marketplace search/filter views (is_search=True)
  5. 'listing' item_type card renders correctly (title, price, city)
  6. 'custom' item_type card renders correctly (headline, description, button)
  7. Inactive listing pin is skipped in the Featured section even if gallery entry is active
  8. Section hidden on category filter view (/marketplace?category=...)
"""

import sys
import uuid
from unittest.mock import patch, MagicMock

from app import app, db
import routes  # registers all URL rules


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _make_user():
    from models import User
    u = User()
    u.id = str(uuid.uuid4())
    u.email = f"testuser_{u.id[:8]}@test.local"
    u.user_type = "customer"
    u.is_admin = False
    u.age_confirmed = True
    u.profile_nudge_dismissed = True
    u.profile_image_url = None
    u.profile_photo_data = None
    u.phone = "5550000000"   # set so no profile nudge
    db.session.add(u)
    db.session.commit()
    return u.id   # return plain id, not the object


def _make_listing(seller_id, title="Test Featured Listing", price=99.00,
                  city="Minneapolis", status="active", moderation_status="approved"):
    from models import Listing
    l = Listing()
    l.seller_id = seller_id
    l.title = title
    l.status = status
    l.listing_type = "item"
    l.moderation_status = moderation_status
    l.price_type = "fixed"
    l.price = price
    l.city = city
    db.session.add(l)
    db.session.commit()
    return l.id   # return plain id


def _make_gallery_listing_pin(listing_id, is_active=True, display_order=0):
    from models import GalleryPhoto
    gp = GalleryPhoto()
    gp.item_type = "listing"
    gp.listing_id = listing_id
    gp.is_active = is_active
    gp.display_order = display_order
    gp.filename = ""
    db.session.add(gp)
    db.session.commit()
    return gp.id   # return plain id


def _make_gallery_custom(headline="Big Sale!", description="Up to 50% off.",
                         button_text="Shop Now", button_link="/marketplace",
                         is_active=True, display_order=0):
    from models import GalleryPhoto
    gp = GalleryPhoto()
    gp.item_type = "custom"
    gp.headline = headline
    gp.description = description
    gp.button_text = button_text
    gp.button_link = button_link
    gp.is_active = is_active
    gp.display_order = display_order
    gp.filename = ""
    db.session.add(gp)
    db.session.commit()
    return gp.id   # return plain id


def _cleanup(*model_id_pairs):
    """Delete rows by (Model, id) pairs. Accepts None ids gracefully."""
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


def _mock_user_for(user_id):
    """Return a MagicMock that passes flask_login checks for the given user_id."""
    mock_user = MagicMock()
    mock_user.is_authenticated = True
    mock_user.is_admin = False
    mock_user.user_type = "customer"
    mock_user.id = user_id
    mock_user.profile_image_url = None
    mock_user.profile_photo_data = None
    mock_user.phone = "5550000000"
    mock_user.profile_nudge_dismissed = True
    return mock_user


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

def test_section_shown_with_active_custom_gallery_item():
    """Featured section appears on / when an active custom gallery item exists."""
    user_id = gp_id = None
    with app.app_context():
        user_id = _make_user()
        gp_id = _make_gallery_custom(headline="Summer Deals!")

    try:
        mock_user = _mock_user_for(user_id)
        with patch("flask_login.utils._get_user", return_value=mock_user):
            client = app.test_client()
            resp = client.get("/")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        html = resp.data.decode()
        assert "Featured Listings" in html, \
            "Expected 'Featured Listings' heading in homepage HTML"
        assert "Summer Deals!" in html, \
            "Custom gallery item headline should appear in featured section"
    finally:
        from models import GalleryPhoto, User
        _cleanup((GalleryPhoto, gp_id), (User, user_id))


def test_section_hidden_when_no_gallery_photos():
    """Featured section is absent when there are no active gallery rows."""
    user_id = None
    with app.app_context():
        user_id = _make_user()

    try:
        mock_user = _mock_user_for(user_id)
        with patch("flask_login.utils._get_user", return_value=mock_user):
            # Patch _gallery_photos to isolate from any real DB rows
            with patch("routes._gallery_photos", return_value=[]):
                client = app.test_client()
                resp = client.get("/")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        html = resp.data.decode()
        assert "⭐ Featured Listings" not in html, \
            "Featured Listings section should NOT appear when gallery_photos is empty"
    finally:
        from models import User
        _cleanup((User, user_id))


def test_section_hidden_when_all_gallery_items_inactive():
    """Featured section is absent when all gallery items have is_active=False."""
    user_id = gp_id = None
    with app.app_context():
        user_id = _make_user()
        gp_id = _make_gallery_custom(headline="Hidden Banner", is_active=False)

    try:
        mock_user = _mock_user_for(user_id)
        with patch("flask_login.utils._get_user", return_value=mock_user):
            client = app.test_client()
            resp = client.get("/")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        html = resp.data.decode()
        assert "Hidden Banner" not in html, \
            "Inactive gallery item's headline should not appear anywhere on homepage"
    finally:
        from models import GalleryPhoto, User
        _cleanup((GalleryPhoto, gp_id), (User, user_id))


def test_section_hidden_on_search_view():
    """Featured section does NOT appear on /marketplace when is_search=True (q=...)."""
    user_id = gp_id = None
    with app.app_context():
        user_id = _make_user()
        gp_id = _make_gallery_custom(headline="Should Not Show In Search")

    try:
        mock_user = _mock_user_for(user_id)
        with patch("flask_login.utils._get_user", return_value=mock_user):
            client = app.test_client()
            resp = client.get("/marketplace?q=chair")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        html = resp.data.decode()
        assert "⭐ Featured Listings" not in html, \
            "Featured Listings section must NOT appear in search/filter views"
        assert "Should Not Show In Search" not in html, \
            "Gallery item content must not leak into search view HTML"
    finally:
        from models import GalleryPhoto, User
        _cleanup((GalleryPhoto, gp_id), (User, user_id))


def test_listing_type_card_renders():
    """A 'listing' item_type gallery pin renders the listing title and price on /."""
    user_id = listing_id = gp_id = None
    with app.app_context():
        user_id = _make_user()
        listing_id = _make_listing(
            seller_id=user_id,
            title="Vintage Couch For Sale",
            price=150.00,
            city="St. Paul",
        )
        gp_id = _make_gallery_listing_pin(listing_id=listing_id)

    try:
        mock_user = _mock_user_for(user_id)
        with patch("flask_login.utils._get_user", return_value=mock_user):
            client = app.test_client()
            resp = client.get("/")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        html = resp.data.decode()
        assert "Featured Listings" in html, \
            "Featured section heading should appear when a pinned listing exists"
        assert "Vintage Couch For Sale" in html, \
            "Pinned listing title should render in the featured section"
        assert "150" in html, \
            "Pinned listing price should render in the featured section"
        assert "St. Paul" in html, \
            "Pinned listing city should render in the featured section"
    finally:
        from models import GalleryPhoto, Listing, User
        _cleanup((GalleryPhoto, gp_id), (Listing, listing_id), (User, user_id))


def test_custom_type_card_renders():
    """A 'custom' item_type gallery card renders headline, description, and button on /."""
    user_id = gp_id = None
    with app.app_context():
        user_id = _make_user()
        gp_id = _make_gallery_custom(
            headline="End-of-Season Clearance",
            description="Huge discounts on furniture.",
            button_text="Browse Deals",
            button_link="/marketplace?q=furniture",
        )

    try:
        mock_user = _mock_user_for(user_id)
        with patch("flask_login.utils._get_user", return_value=mock_user):
            client = app.test_client()
            resp = client.get("/")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        html = resp.data.decode()
        assert "End-of-Season Clearance" in html, \
            "Custom card headline should appear in homepage HTML"
        assert "Huge discounts on furniture." in html, \
            "Custom card description should appear in homepage HTML"
        assert "Browse Deals" in html, \
            "Custom card button text should appear in homepage HTML"
    finally:
        from models import GalleryPhoto, User
        _cleanup((GalleryPhoto, gp_id), (User, user_id))


def test_inactive_pinned_listing_skipped_in_featured_section():
    """A pinned listing whose status is not 'active' is skipped in the card grid."""
    user_id = listing_id = gp_id = None
    with app.app_context():
        user_id = _make_user()
        listing_id = _make_listing(
            seller_id=user_id,
            title="Already Sold Featured Item",
            status="sold",   # not active → template skips this card
        )
        gp_id = _make_gallery_listing_pin(listing_id=listing_id, is_active=True)

    try:
        mock_user = _mock_user_for(user_id)
        with patch("flask_login.utils._get_user", return_value=mock_user):
            client = app.test_client()
            resp = client.get("/")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        html = resp.data.decode()
        # The template renders a listing card only when gp.listing_rel.status == 'active'.
        # The sold listing's title should NOT appear inside the featured section card grid.
        # We locate the section block to limit the check to that region.
        featured_marker = "⭐ Featured Listings"
        section_start = html.find(featured_marker)
        if section_start == -1:
            # Section entirely absent — also acceptable: no visible cards rendered
            pass
        else:
            # The card grid ends before the next top-level section div.
            # Take a 5000-char slice of the featured block to search within.
            featured_block = html[section_start: section_start + 5000]
            assert "Already Sold Featured Item" not in featured_block, \
                "Sold listing should not appear as a featured card in the section"
    finally:
        from models import GalleryPhoto, Listing, User
        _cleanup((GalleryPhoto, gp_id), (Listing, listing_id), (User, user_id))


def test_section_hidden_on_category_filter_view():
    """Featured section does NOT appear on /marketplace?category=... (is_search=True)."""
    user_id = gp_id = None
    with app.app_context():
        user_id = _make_user()
        gp_id = _make_gallery_custom(headline="Category Filter Should Hide Me")

    try:
        mock_user = _mock_user_for(user_id)
        with patch("flask_login.utils._get_user", return_value=mock_user):
            client = app.test_client()
            resp = client.get("/marketplace?category=furniture")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        html = resp.data.decode()
        assert "⭐ Featured Listings" not in html, \
            "Featured Listings section must NOT appear when filtering by category"
    finally:
        from models import GalleryPhoto, User
        _cleanup((GalleryPhoto, gp_id), (User, user_id))


def test_section_shown_on_marketplace_direct_visit():
    """Featured section appears on /marketplace (no query params) when active gallery items exist."""
    user_id = gp_id = None
    with app.app_context():
        user_id = _make_user()
        gp_id = _make_gallery_custom(headline="Marketplace Direct Visit Deal!")

    try:
        mock_user = _mock_user_for(user_id)
        # Explicitly set hide_sold_pref=False so the /marketplace route doesn't treat
        # this as a search request (truthy MagicMock would set hide_sold='1' → is_search=True)
        mock_user.hide_sold_pref = False
        with patch("flask_login.utils._get_user", return_value=mock_user):
            client = app.test_client()
            resp = client.get("/marketplace")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        html = resp.data.decode()
        assert "⭐ Featured Listings" in html, \
            "Featured Listings section should appear on /marketplace when active gallery items exist"
        assert "Marketplace Direct Visit Deal!" in html, \
            "Gallery item headline should appear on direct /marketplace visit"
    finally:
        from models import GalleryPhoto, User
        _cleanup((GalleryPhoto, gp_id), (User, user_id))


def test_featured_items_rendered_in_display_order():
    """Items with lower display_order appear before items with higher display_order in the HTML."""
    user_id = gp_first_id = gp_second_id = None
    with app.app_context():
        user_id = _make_user()
        # display_order=1 should appear first in the featured section
        gp_first_id = _make_gallery_custom(
            headline="FIRST ITEM LOW ORDER",
            display_order=1,
        )
        # display_order=10 should appear after display_order=1
        gp_second_id = _make_gallery_custom(
            headline="SECOND ITEM HIGH ORDER",
            display_order=10,
        )

    try:
        mock_user = _mock_user_for(user_id)
        with patch("flask_login.utils._get_user", return_value=mock_user):
            client = app.test_client()
            resp = client.get("/")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        html = resp.data.decode()
        assert "FIRST ITEM LOW ORDER" in html, \
            "Gallery item with display_order=1 should appear in homepage HTML"
        assert "SECOND ITEM HIGH ORDER" in html, \
            "Gallery item with display_order=10 should appear in homepage HTML"
        pos_first = html.index("FIRST ITEM LOW ORDER")
        pos_second = html.index("SECOND ITEM HIGH ORDER")
        assert pos_first < pos_second, (
            f"Item with display_order=1 (pos {pos_first}) should appear before "
            f"item with display_order=10 (pos {pos_second}) in the rendered HTML"
        )
    finally:
        from models import GalleryPhoto, User
        _cleanup((GalleryPhoto, gp_first_id), (GalleryPhoto, gp_second_id), (User, user_id))


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nRunning Featured Listings section visibility tests...\n")

    run("section shown with active custom gallery item",
        test_section_shown_with_active_custom_gallery_item)
    run("section hidden when no gallery photos",
        test_section_hidden_when_no_gallery_photos)
    run("section hidden when all gallery items are inactive",
        test_section_hidden_when_all_gallery_items_inactive)
    run("section hidden on search view (/marketplace?q=...)",
        test_section_hidden_on_search_view)
    run("'listing' type card renders title, price, and city",
        test_listing_type_card_renders)
    run("'custom' type card renders headline, description, and button",
        test_custom_type_card_renders)
    run("inactive pinned listing skipped in featured section",
        test_inactive_pinned_listing_skipped_in_featured_section)
    run("section hidden on category filter view",
        test_section_hidden_on_category_filter_view)
    run("section shown on /marketplace direct visit (no query params)",
        test_section_shown_on_marketplace_direct_visit)
    run("featured items rendered in display_order sequence",
        test_featured_items_rendered_in_display_order)

    print(f"\n{'='*60}")
    print(f"Results: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nFailures:")
        for name, exc in FAIL:
            print(f"  {name}: {exc}")
        sys.exit(1)
    else:
        print("All tests passed.")
