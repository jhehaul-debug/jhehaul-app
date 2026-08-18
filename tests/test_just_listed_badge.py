"""
Task 190 validation: 'Just Listed' badge appears exactly when expected.

Run with:  python tests/test_just_listed_badge.py

Verifies:
1. A listing created within 48 h shows the badge on the marketplace page.
2. A listing created 49+ h ago does NOT show the badge.
3. Sold listings never show the badge, even if brand-new.
4. Reserved listings never show the badge, even if brand-new.
5. Pending listings never show the badge, even if brand-new.
6. Listing.created_at defaults to datetime.utcnow, consistent with the
   utcnow() template global used in the badge comparison.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from unittest.mock import patch
import flask_login.utils as _flu
from app import app, db
import routes  # noqa: F401 — registers routes on app
from models import User, Listing

results = []


def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


# ── Helpers ──────────────────────────────────────────────────────────────────

def _card_html(full_html, listing_id):
    """
    Extract the HTML for exactly one listing card by slicing from the anchor
    tag that links to /listing/<id> through the heart button that carries
    data-listing-id="<id>".  Returns empty string if the listing is absent.
    """
    start_marker = f'href="/listing/{listing_id}"'
    end_marker   = f'data-listing-id="{listing_id}"'
    start = full_html.find(start_marker)
    if start == -1:
        return ''
    end = full_html.find(end_marker, start)
    if end == -1:
        return full_html[start:start + 3000]   # fallback: wide window
    return full_html[start:end + len(end_marker) + 50]


BADGE_TEXT = 'mp-badge-new'      # CSS class present only on the badge element
_SELLER_ID = 'badge-test-seller-190'
NOW = datetime.utcnow()


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _make_user(uid):
    u = User(
        id=uid,
        email=f'{uid}@example.com',
        first_name='Test',
        age_confirmed=True,
        user_type='customer',
    )
    db.session.merge(u)
    db.session.commit()
    return db.session.get(User, uid)


def _make_listing(lid, seller_id, status='active', created_at=None):
    lst = Listing(
        id=lid,
        seller_id=seller_id,
        title=f'Badge Test Listing {lid}',
        price=50.0,
        price_type='fixed',
        status=status,
        moderation_status='approved',
        listing_type='item',
    )
    db.session.merge(lst)
    db.session.commit()
    if created_at is not None:
        db.session.execute(
            db.text('UPDATE listings SET created_at = :ts WHERE id = :lid'),
            {'ts': created_at, 'lid': lid}
        )
        db.session.commit()
    return db.session.get(Listing, lid)


def _marketplace_html(client, user):
    with patch.object(_flu, '_get_user', return_value=user):
        resp = client.get('/marketplace')
    return resp.data.decode('utf-8', errors='replace')


# ── Tests ────────────────────────────────────────────────────────────────────

with app.app_context():
    client = app.test_client()
    seller = _make_user(_SELLER_ID)

    # ── 1. Fresh listing (1 h old) shows badge ───────────────────────────────
    _make_listing(19010, _SELLER_ID, status='active',
                  created_at=NOW - timedelta(hours=1))
    html = _marketplace_html(client, seller)
    card = _card_html(html, 19010)
    check(
        'Fresh active listing (1 h old) shows Just Listed badge',
        BADGE_TEXT in card,
        f'card excerpt: {card[:400]}',
    )

    # ── 2. Old listing (49 h ago) does NOT show badge ────────────────────────
    _make_listing(19011, _SELLER_ID, status='active',
                  created_at=NOW - timedelta(hours=49))
    html = _marketplace_html(client, seller)
    card_old = _card_html(html, 19011)
    check(
        'Old active listing (49 h) does NOT show Just Listed badge',
        BADGE_TEXT not in card_old,
        f'card excerpt: {card_old[:400]}',
    )

    # ── 3. Sold listing (brand-new) does NOT show badge ──────────────────────
    _make_listing(19012, _SELLER_ID, status='sold',
                  created_at=NOW - timedelta(minutes=5))
    html = _marketplace_html(client, seller)
    card_sold = _card_html(html, 19012)
    check(
        'Sold listing (brand-new) does NOT show Just Listed badge',
        BADGE_TEXT not in card_sold,
        f'card excerpt: {card_sold[:400]}',
    )

    # ── 4. Reserved listing (brand-new) does NOT show badge ─────────────────
    _make_listing(19013, _SELLER_ID, status='reserved',
                  created_at=NOW - timedelta(minutes=5))
    html = _marketplace_html(client, seller)
    card_res = _card_html(html, 19013)
    check(
        'Reserved listing (brand-new) does NOT show Just Listed badge',
        BADGE_TEXT not in card_res,
        f'card excerpt: {card_res[:400]}',
    )

    # ── 5. Pending listing (brand-new) does NOT show badge ───────────────────
    _make_listing(19014, _SELLER_ID, status='pending',
                  created_at=NOW - timedelta(minutes=5))
    html = _marketplace_html(client, seller)
    card_pend = _card_html(html, 19014)
    check(
        'Pending listing (brand-new) does NOT show Just Listed badge',
        BADGE_TEXT not in card_pend,
        f'card excerpt: {card_pend[:400]}',
    )

    # ── 6. Listing.created_at defaults to datetime.utcnow ───────────────────
    col = Listing.__table__.c.created_at
    col_default = col.default
    check(
        'Listing.created_at column has a default',
        col_default is not None,
    )
    fn = col_default.arg if col_default else None
    fn_name = getattr(fn, '__name__', '') if fn else ''
    check(
        'Listing.created_at default is datetime.utcnow (not datetime.now)',
        fn_name == 'utcnow',
        f'got default callable name={fn_name!r}; expected "utcnow"',
    )

    # ── Tear-down ────────────────────────────────────────────────────────────
    for lid in [19010, 19011, 19012, 19013, 19014]:
        Listing.query.filter_by(id=lid).delete()
    db.session.commit()


# ── Summary ──────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
print(f'\n{passed} passed, {failed} failed')
if failed:
    sys.exit(1)
