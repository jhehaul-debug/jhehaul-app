"""
Task 112 validation: property wizard draft-expiry banner and beforeunload prompt.

Run with:  python tests/test_property_wizard_draft_banner.py

Verifies:
1.  Banner (draft-expiry-banner) appears on steps 1–5 when the listing has no title.
2.  Banner is absent on step 6 (preview) even when no title.
3.  beforeunload script block is present on steps 1–5 for an untitled draft.
4.  beforeunload script block is absent on step 6 for an untitled draft.
5.  data-wizard-nav forms are present on steps 1–5 so the JS can suppress the
    prompt when the seller clicks Next/Back.
6.  Neither banner nor beforeunload script appears on steps 1–5 when the listing
    already has a title (seller has committed enough; no nag needed).
7.  Step 6 also shows no banner / no beforeunload for a titled listing.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flask_login.utils as _flu
from app import app
import routes  # noqa: F401 — registers all routes on app
from models import db, User, Listing

results = []


def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


def _make_user(uid):
    u = User(
        id=uid,
        email=f'{uid}@example.com',
        first_name='Test',
        is_admin=False,
        age_confirmed=True,
    )
    u.user_type = 'customer'
    db.session.merge(u)
    db.session.commit()
    return db.session.get(User, uid)


def _make_property_draft(listing_id, seller_id, title=''):
    """Create (or replace) a property_sale draft with the given title."""
    existing = Listing.query.get(listing_id)
    if existing:
        db.session.delete(existing)
        db.session.commit()
    lst = Listing(
        id=listing_id,
        seller_id=seller_id,
        title=title,
        status='draft',
        listing_type='property_sale',
        moderation_status='approved',
    )
    db.session.add(lst)
    db.session.commit()
    return db.session.get(Listing, listing_id)


UNTITLED_ID = 99001
TITLED_ID   = 99002
SELLER_UID  = 't112-seller'


with app.app_context():
    client = app.test_client()

    seller = _make_user(SELLER_UID)
    _flu._get_user = lambda: seller   # mock flask-login current_user

    # ── Create both test listings ─────────────────────────────────────────────
    untitled = _make_property_draft(UNTITLED_ID, SELLER_UID, title='')
    titled   = _make_property_draft(TITLED_ID,   SELLER_UID, title='123 Main Street')

    # ─────────────────────────────────────────────────────────────────────────
    # Section A: UNTITLED draft — banner and beforeunload present on steps 1–5
    # ─────────────────────────────────────────────────────────────────────────
    for step in range(1, 6):
        url = f'/listing/{UNTITLED_ID}/step/{step}'
        r = client.get(url)
        check(
            f'untitled draft step {step}: returns 200',
            r.status_code == 200,
            f'status={r.status_code}',
        )
        check(
            f'untitled draft step {step}: draft-expiry-banner present',
            b'Unsaved draft' in r.data,
            'banner text "Unsaved draft" missing from rendered HTML',
        )
        check(
            f'untitled draft step {step}: beforeunload script present',
            b'beforeunload' in r.data,
            'beforeunload listener not found in rendered HTML',
        )
        check(
            f'untitled draft step {step}: data-wizard-nav form present',
            b'data-wizard-nav' in r.data,
            'data-wizard-nav attribute not found — JS cannot suppress the prompt on nav clicks',
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Section B: UNTITLED draft — step 6 (preview) has NO banner / NO script
    # ─────────────────────────────────────────────────────────────────────────
    r6_untitled = client.get(f'/listing/{UNTITLED_ID}/step/6')
    check(
        'untitled draft step 6: returns 200',
        r6_untitled.status_code == 200,
        f'status={r6_untitled.status_code}',
    )
    check(
        'untitled draft step 6: draft-expiry-banner absent',
        b'Unsaved draft' not in r6_untitled.data,
        'banner text unexpectedly present on preview step',
    )
    check(
        'untitled draft step 6: beforeunload script absent',
        b'beforeunload' not in r6_untitled.data,
        'beforeunload listener should not fire on preview step',
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Section C: TITLED listing — no banner or beforeunload on steps 1–5
    # ─────────────────────────────────────────────────────────────────────────
    for step in range(1, 6):
        url = f'/listing/{TITLED_ID}/step/{step}'
        r = client.get(url)
        check(
            f'titled listing step {step}: returns 200',
            r.status_code == 200,
            f'status={r.status_code}',
        )
        check(
            f'titled listing step {step}: draft-expiry-banner absent',
            b'Unsaved draft' not in r.data,
            'banner text shown for listing that already has a title',
        )
        check(
            f'titled listing step {step}: beforeunload script absent',
            b'beforeunload' not in r.data,
            'beforeunload listener present even though listing already has a title',
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Section D: TITLED listing — step 6 also clean
    # ─────────────────────────────────────────────────────────────────────────
    r6_titled = client.get(f'/listing/{TITLED_ID}/step/6')
    check(
        'titled listing step 6: returns 200',
        r6_titled.status_code == 200,
        f'status={r6_titled.status_code}',
    )
    check(
        'titled listing step 6: draft-expiry-banner absent',
        b'Unsaved draft' not in r6_titled.data,
        'banner text present on step 6 for titled listing',
    )
    check(
        'titled listing step 6: beforeunload script absent',
        b'beforeunload' not in r6_titled.data,
        'beforeunload present on step 6 for titled listing',
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Clean up test listings so subsequent runs stay idempotent
    # ─────────────────────────────────────────────────────────────────────────
    for lid in (UNTITLED_ID, TITLED_ID):
        lst = Listing.query.get(lid)
        if lst:
            db.session.delete(lst)
    db.session.commit()


# ── Summary ───────────────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f'\n{len(results) - len(failed)}/{len(results)} passed')
sys.exit(1 if failed else 0)
