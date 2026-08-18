"""
Task 235 validation: sold items disappear from the authenticated home page (/)
immediately after being marked sold — same guarantee verified for /landing in Task #136.

Run with:  python tests/test_home_sold_visibility.py

Verifies (using _marketplace_homepage_ctx and rendered GET /):
1. An active, approved item listing appears in recent_listings on GET /.
2. After marking it sold, the listing IS still in recent_listings when
   hide_sold=False (default — active, sold, reserved, pending all shown).
3. After marking it sold, the listing is ABSENT from recent_listings when
   hide_sold=True (active-only mode).
4. GET /?hide_sold=0  → sold listing appears on the rendered page.
5. GET /?hide_sold=1  → sold listing is absent from the rendered page.
6. Preference persists: GET / after hide_sold=1 still hides the sold listing.
7. Re-activating the listing makes it reappear with hide_sold=True.

The test resets buyer.hide_sold_pref to False before starting and restores it
afterward so each run is fully deterministic.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flask_login.utils as _flu
from app import app
import routes  # noqa: F401 — registers routes on app
from models import db, User, Category, Listing
from routes import _marketplace_homepage_ctx

results = []


def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, (extra if not cond else ''))


def _make_user(uid, user_type='customer'):
    u = User(
        id=uid,
        email=f'{uid}@example.com',
        first_name='Test',
        age_confirmed=True,
    )
    u.user_type = user_type
    db.session.merge(u)
    db.session.commit()
    return db.session.get(User, uid)


with app.app_context():
    client = app.test_client()

    # ── Setup: buyer user with explicit hide_sold_pref=False ─────────────────
    buyer = _make_user('t235-buyer', user_type='customer')
    buyer.hide_sold_pref = False   # reset so test is deterministic across runs
    db.session.commit()
    _flu._get_user = lambda: buyer

    # Ensure at least one root category exists
    cat = Category.query.filter_by(parent_id=None, is_active=True).first()
    if cat is None:
        cat = Category(name='Test Category T235', is_active=True, display_order=0)
        db.session.add(cat)
        db.session.commit()

    # Remove any leftover test listings from prior runs
    Listing.query.filter(
        Listing.title == 'Active Widget T235',
        Listing.seller_id == buyer.id,
    ).delete(synchronize_session=False)
    db.session.commit()

    # ── Create an active listing ──────────────────────────────────────────────
    listing = Listing(
        seller_id=buyer.id,
        title='Active Widget T235',
        status='active',
        moderation_status='approved',
        price_type='fixed',
        price=25.00,
        listing_type='item',
        category_id=cat.id,
    )
    db.session.add(listing)
    db.session.commit()
    listing_id = listing.id

    # ── 1. Active listing appears in ctx and on GET / ─────────────────────────
    with client.session_transaction() as sess:
        sess.pop('hide_sold', None)   # start with clean session

    ctx1 = _marketplace_homepage_ctx(hide_sold=False)
    check(
        '_marketplace_homepage_ctx(hide_sold=False): active listing in recent_listings',
        listing_id in {l.id for l in ctx1['recent_listings']},
        f'listing id {listing_id} not found in recent_listings',
    )

    r1 = client.get('/?hide_sold=0')
    check('GET /?hide_sold=0 returns 200', r1.status_code == 200,
          f'status={r1.status_code}')
    check(
        'Active listing appears on / (hide_sold=False)',
        b'Active Widget T235' in r1.data,
        'active listing title not found in rendered homepage',
    )

    # ── 2. Mark listing sold ──────────────────────────────────────────────────
    listing.status = 'sold'
    db.session.commit()

    # ── 3. hide_sold=False: sold listing IS included in ctx ───────────────────
    ctx2 = _marketplace_homepage_ctx(hide_sold=False)
    check(
        '_marketplace_homepage_ctx(hide_sold=False): sold listing in recent_listings (default shows all statuses)',
        listing_id in {l.id for l in ctx2['recent_listings']},
        f'sold listing {listing_id} missing — default mode must include sold items',
    )

    # ── 4. hide_sold=True: sold listing absent from ctx ───────────────────────
    ctx3 = _marketplace_homepage_ctx(hide_sold=True)
    check(
        '_marketplace_homepage_ctx(hide_sold=True): sold listing ABSENT from recent_listings',
        listing_id not in {l.id for l in ctx3['recent_listings']},
        f'sold listing {listing_id} still in recent_listings with hide_sold=True',
    )

    # ── 5. Rendered page: GET /?hide_sold=0 shows sold listing ───────────────
    with client.session_transaction() as sess:
        sess.pop('hide_sold', None)

    r2 = client.get('/?hide_sold=0')
    check('GET /?hide_sold=0 after sell returns 200', r2.status_code == 200,
          f'status={r2.status_code}')
    check(
        'Sold listing visible on / with hide_sold=0 (default mode shows all statuses)',
        b'Active Widget T235' in r2.data,
        'sold listing absent from rendered page in default mode — should appear with sold badge',
    )

    # ── 6. Rendered page: GET /?hide_sold=1 hides sold listing immediately ────
    with client.session_transaction() as sess:
        sess.pop('hide_sold', None)
    buyer.hide_sold_pref = False   # ensure DB preference doesn't interfere
    db.session.commit()

    r3 = client.get('/?hide_sold=1')
    check('GET /?hide_sold=1 returns 200', r3.status_code == 200,
          f'status={r3.status_code}')
    check(
        'Sold listing absent from / immediately after marking sold (hide_sold=1)',
        b'Active Widget T235' not in r3.data,
        'sold listing still rendered on homepage when hide_sold=True',
    )

    # ── 7. Preference persists on reload ─────────────────────────────────────
    r4 = client.get('/')
    check('GET / reload (no param) after hide_sold=1 returns 200',
          r4.status_code == 200, f'status={r4.status_code}')
    check(
        'Sold listing absent on / reload with persisted hide_sold preference',
        b'Active Widget T235' not in r4.data,
        'sold listing reappeared after reload — session preference not persisted',
    )

    # ── 8. Re-activate listing — reappears immediately with hide_sold=True ────
    listing.status = 'active'
    db.session.commit()

    ctx4 = _marketplace_homepage_ctx(hide_sold=True)
    check(
        '_marketplace_homepage_ctx(hide_sold=True): reactivated listing reappears',
        listing_id in {l.id for l in ctx4['recent_listings']},
        f'listing {listing_id} absent after reactivation',
    )

    # ── Clean up ──────────────────────────────────────────────────────────────
    db.session.delete(listing)
    buyer.hide_sold_pref = False
    db.session.commit()


# ── Summary ───────────────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
