"""
Tests: 3-day expiry reminder email fires once and is guarded against repeats.

Verifies:
- _run_checks() calls notify_seller_listing_expiring_soon with correct args
  (email, listing_id, title, expires_at) for a listing expiring in ~2 days
- expiry_reminder_sent is set to True on the listing after the run
- Running _run_checks() a second time does NOT call the email again
- A listing whose expiry_reminder_sent is already True is skipped from the start

Run with:  python tests/test_expiry_reminder_guard.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from unittest.mock import patch
from app import app, db
import routes  # noqa: F401 — registers all routes on app
from models import User, Listing

results = []


def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_seller(uid, email='seller_reminder@example.com'):
    u = User(
        id=uid,
        email=email,
        first_name='ReminderSeller',
        age_confirmed=True,
        user_type='customer',
    )
    db.session.merge(u)
    db.session.commit()
    return db.session.get(User, uid)


def _make_listing(listing_id, seller_id, title='Expiring Soon Couch',
                  expires_at=None, expiry_reminder_sent=False):
    lst = Listing(
        id=listing_id,
        seller_id=seller_id,
        title=title,
        price=100.0,
        price_type='fixed',
        status='active',
        expires_at=expires_at,
        expiry_reminder_sent=expiry_reminder_sent,
    )
    db.session.merge(lst)
    db.session.commit()
    return db.session.get(Listing, listing_id)


def _cleanup(*ids_by_model):
    for Model, row_id in ids_by_model:
        obj = db.session.get(Model, row_id)
        if obj:
            db.session.delete(obj)
    db.session.commit()


from job_expiry import _run_checks

# ---------------------------------------------------------------------------
# Test 1: Email fires with correct args; expiry_reminder_sent becomes True
# ---------------------------------------------------------------------------

SELLER_ID_1 = 'test-reminder-seller-01'
LISTING_ID_1 = 980001
LISTING_TITLE_1 = 'Expiring Soon Couch'
SELLER_EMAIL_1 = 'seller_reminder01@example.com'
EXPIRES_AT_1 = datetime.now() + timedelta(days=2)

with app.app_context():
    _make_seller(SELLER_ID_1, email=SELLER_EMAIL_1)
    _make_listing(LISTING_ID_1, seller_id=SELLER_ID_1, title=LISTING_TITLE_1,
                  expires_at=EXPIRES_AT_1, expiry_reminder_sent=False)

with (
    patch('email_service.notify_seller_listing_expiring_soon', return_value=True) as mock_remind,
    patch('email_service.notify_seller_listing_expired', return_value=True),
    patch('sms_service.notify_seller_listing_expired_sms', return_value=True),
    patch('models.expire_stale_timed_offers', return_value=[]),
    patch('models.expire_pending_offers', return_value=[]),
):
    _run_checks(app)

check("reminder email called exactly once on first run",
      mock_remind.call_count == 1,
      f"call_count={mock_remind.call_count}")

if mock_remind.call_count >= 1:
    args = mock_remind.call_args[0]
    check("reminder: email arg matches seller email",
          args[0] == SELLER_EMAIL_1,
          f"got={args[0]!r}")
    check("reminder: listing_id arg matches",
          args[1] == LISTING_ID_1,
          f"got={args[1]!r}")
    check("reminder: title arg matches",
          args[2] == LISTING_TITLE_1,
          f"got={args[2]!r}")
    # expires_at may be passed as a datetime — check it's close to the expected value
    check("reminder: expires_at arg is set (not None)",
          args[3] is not None,
          f"got={args[3]!r}")

with app.app_context():
    lst1 = db.session.get(Listing, LISTING_ID_1)
    check("expiry_reminder_sent is True after first run",
          lst1 is not None and lst1.expiry_reminder_sent is True,
          f"expiry_reminder_sent={lst1.expiry_reminder_sent if lst1 else 'NOT FOUND'}")

# ---------------------------------------------------------------------------
# Test 2: Second _run_checks() call does NOT fire the email again
# ---------------------------------------------------------------------------

with (
    patch('email_service.notify_seller_listing_expiring_soon', return_value=True) as mock_remind2,
    patch('email_service.notify_seller_listing_expired', return_value=True),
    patch('sms_service.notify_seller_listing_expired_sms', return_value=True),
    patch('models.expire_stale_timed_offers', return_value=[]),
    patch('models.expire_pending_offers', return_value=[]),
):
    _run_checks(app)

check("reminder email NOT called on second run (guard works)",
      mock_remind2.call_count == 0,
      f"call_count={mock_remind2.call_count}")

with app.app_context():
    _cleanup((Listing, LISTING_ID_1), (User, SELLER_ID_1))

# ---------------------------------------------------------------------------
# Test 3: Listing with expiry_reminder_sent=True is skipped from the start
# ---------------------------------------------------------------------------

SELLER_ID_3 = 'test-reminder-seller-03'
LISTING_ID_3 = 980003
EXPIRES_AT_3 = datetime.now() + timedelta(days=1)

with app.app_context():
    _make_seller(SELLER_ID_3, email='seller_reminder03@example.com')
    _make_listing(LISTING_ID_3, seller_id=SELLER_ID_3,
                  title='Already Reminded Dresser',
                  expires_at=EXPIRES_AT_3,
                  expiry_reminder_sent=True)  # already sent

with (
    patch('email_service.notify_seller_listing_expiring_soon', return_value=True) as mock_remind3,
    patch('email_service.notify_seller_listing_expired', return_value=True),
    patch('sms_service.notify_seller_listing_expired_sms', return_value=True),
    patch('models.expire_stale_timed_offers', return_value=[]),
    patch('models.expire_pending_offers', return_value=[]),
):
    _run_checks(app)

check("pre-flagged listing: reminder email NOT called (already sent guard)",
      mock_remind3.call_count == 0,
      f"call_count={mock_remind3.call_count}")

with app.app_context():
    _cleanup((Listing, LISTING_ID_3), (User, SELLER_ID_3))

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
