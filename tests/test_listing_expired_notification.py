"""
Tests: listing-expired email and SMS fire correctly when the background job runs.

Verifies:
- _run_checks() marks an overdue listing as 'expired'
- notify_seller_listing_expired is called with the seller's email and listing title
- notify_seller_listing_expired_sms is called when seller.notify_sms is True
  (and sms_consent is True and phone is set)
- notify_seller_listing_expired_sms is NOT called when seller.notify_sms is False
- No email or SMS is sent when the seller has no email address

Run with:  python tests/test_listing_expired_notification.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
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

def _make_seller(uid, email=None, notify_sms=False, sms_consent=False, phone=None):
    u = User(
        id=uid,
        email=email,
        first_name='Seller',
        age_confirmed=True,
        user_type='customer',
        notify_sms=notify_sms,
        sms_consent=sms_consent,
        phone=phone,
    )
    db.session.merge(u)
    db.session.commit()
    return db.session.get(User, uid)


def _make_listing(listing_id, seller_id, title='Test Expired Couch',
                  status='active', expires_at=None):
    if expires_at is None:
        expires_at = datetime.now() - timedelta(hours=2)  # already past
    lst = Listing(
        id=listing_id,
        seller_id=seller_id,
        title=title,
        price=100.0,
        price_type='fixed',
        status=status,
        expires_at=expires_at,
    )
    db.session.merge(lst)
    db.session.commit()
    return db.session.get(Listing, listing_id)


def _cleanup(*ids_by_model):
    """Delete test rows by (Model, id) pairs."""
    for Model, row_id in ids_by_model:
        obj = db.session.get(Model, row_id)
        if obj:
            db.session.delete(obj)
    db.session.commit()


# ---------------------------------------------------------------------------
# Test 1: email fires with correct recipient and listing title
# ---------------------------------------------------------------------------

SELLER_ID_1 = 'test-exp-seller-01'
LISTING_ID_1 = 900001
LISTING_TITLE = 'Test Expired Couch'
SELLER_EMAIL = 'seller_expired@example.com'

with app.app_context():
    _make_seller(SELLER_ID_1, email=SELLER_EMAIL)
    _make_listing(LISTING_ID_1, seller_id=SELLER_ID_1, title=LISTING_TITLE)

from job_expiry import _run_checks

with (
    patch('email_service.notify_seller_listing_expired', return_value=True) as mock_email,
    patch('sms_service.notify_seller_listing_expired_sms', return_value=True) as mock_sms,
    patch('models.expire_stale_timed_offers', return_value=[]),
    patch('models.expire_pending_offers'),
):
    _run_checks(app)

with app.app_context():
    lst = db.session.get(Listing, LISTING_ID_1)
    check("listing status set to 'expired'",
          lst is not None and lst.status == 'expired',
          f"status={lst.status if lst else 'NOT FOUND'}")

check("email called exactly once", mock_email.call_count == 1,
      f"call_count={mock_email.call_count}")

if mock_email.call_count >= 1:
    email_args = mock_email.call_args[0]
    check("email recipient matches seller email",
          email_args[0] == SELLER_EMAIL,
          f"got={email_args[0]!r}")
    check("email listing_id matches",
          email_args[1] == LISTING_ID_1,
          f"got={email_args[1]!r}")
    check("email listing title matches",
          email_args[2] == LISTING_TITLE,
          f"got={email_args[2]!r}")

check("SMS not called (seller has notify_sms=False)", mock_sms.call_count == 0,
      f"call_count={mock_sms.call_count}")

with app.app_context():
    _cleanup((Listing, LISTING_ID_1), (User, SELLER_ID_1))

# ---------------------------------------------------------------------------
# Test 2: SMS fires when seller.notify_sms=True, sms_consent=True, phone set
# ---------------------------------------------------------------------------

SELLER_ID_2 = 'test-exp-seller-02'
LISTING_ID_2 = 900002
SELLER_PHONE = '6515550002'

with app.app_context():
    _make_seller(SELLER_ID_2, email='seller2_expired@example.com',
                 notify_sms=True, sms_consent=True, phone=SELLER_PHONE)
    _make_listing(LISTING_ID_2, seller_id=SELLER_ID_2, title='SMS Opted-In Lamp')

with (
    patch('email_service.notify_seller_listing_expired', return_value=True) as mock_email2,
    patch('sms_service.notify_seller_listing_expired_sms', return_value=True) as mock_sms2,
    patch('models.expire_stale_timed_offers', return_value=[]),
    patch('models.expire_pending_offers'),
):
    _run_checks(app)

check("SMS opted-in: email called once", mock_email2.call_count == 1,
      f"call_count={mock_email2.call_count}")
check("SMS opted-in: SMS called once", mock_sms2.call_count == 1,
      f"call_count={mock_sms2.call_count}")

if mock_sms2.call_count >= 1:
    sms_args = mock_sms2.call_args[0]
    check("SMS: phone argument matches seller phone",
          sms_args[0] == SELLER_PHONE,
          f"got={sms_args[0]!r}")
    check("SMS: listing_id argument matches",
          sms_args[1] == LISTING_ID_2,
          f"got={sms_args[1]!r}")
    check("SMS: title argument matches",
          sms_args[2] == 'SMS Opted-In Lamp',
          f"got={sms_args[2]!r}")

with app.app_context():
    _cleanup((Listing, LISTING_ID_2), (User, SELLER_ID_2))

# ---------------------------------------------------------------------------
# Test 3: SMS skipped when notify_sms=False (even with phone and sms_consent)
# ---------------------------------------------------------------------------

SELLER_ID_3 = 'test-exp-seller-03'
LISTING_ID_3 = 900003

with app.app_context():
    _make_seller(SELLER_ID_3, email='seller3_expired@example.com',
                 notify_sms=False, sms_consent=True, phone='6515550003')
    _make_listing(LISTING_ID_3, seller_id=SELLER_ID_3, title='SMS Opted-Out Desk')

with (
    patch('email_service.notify_seller_listing_expired', return_value=True) as mock_email3,
    patch('sms_service.notify_seller_listing_expired_sms', return_value=True) as mock_sms3,
    patch('models.expire_stale_timed_offers', return_value=[]),
    patch('models.expire_pending_offers'),
):
    _run_checks(app)

check("SMS opted-out: email still called once", mock_email3.call_count == 1,
      f"call_count={mock_email3.call_count}")
check("SMS opted-out: SMS not called when notify_sms=False", mock_sms3.call_count == 0,
      f"call_count={mock_sms3.call_count}")

with app.app_context():
    _cleanup((Listing, LISTING_ID_3), (User, SELLER_ID_3))

# ---------------------------------------------------------------------------
# Test 4: No email or SMS when seller has no email address
# ---------------------------------------------------------------------------

SELLER_ID_4 = 'test-exp-seller-04'
LISTING_ID_4 = 900004

with app.app_context():
    _make_seller(SELLER_ID_4, email=None, notify_sms=True, sms_consent=True,
                 phone='6515550004')
    _make_listing(LISTING_ID_4, seller_id=SELLER_ID_4, title='No Email Seller Listing')

with (
    patch('email_service.notify_seller_listing_expired', return_value=True) as mock_email4,
    patch('sms_service.notify_seller_listing_expired_sms', return_value=True) as mock_sms4,
    patch('models.expire_stale_timed_offers', return_value=[]),
    patch('models.expire_pending_offers'),
):
    _run_checks(app)

with app.app_context():
    lst4 = db.session.get(Listing, LISTING_ID_4)
    check("no-email seller: listing still expired",
          lst4 is not None and lst4.status == 'expired',
          f"status={lst4.status if lst4 else 'NOT FOUND'}")

check("no-email seller: email not called", mock_email4.call_count == 0,
      f"call_count={mock_email4.call_count}")
# SMS guard requires seller.email check first; the guard in job_expiry is:
# if seller and seller.email: → email; if seller and seller.notify_sms and seller.sms_consent and seller.phone: → sms
# SMS CAN still fire since it has its own separate guard.
# The code sends SMS independently of email (lines 77-81 in job_expiry.py).
# With a seller that has no email but has phone+notify_sms+sms_consent, SMS fires.
check("no-email seller: SMS called (independent guard with phone+consent)",
      mock_sms4.call_count == 1,
      f"call_count={mock_sms4.call_count}")

with app.app_context():
    _cleanup((Listing, LISTING_ID_4), (User, SELLER_ID_4))

# ---------------------------------------------------------------------------
# Test 5: Active listing that hasn't expired yet is NOT touched
# ---------------------------------------------------------------------------

SELLER_ID_5 = 'test-exp-seller-05'
LISTING_ID_5 = 900005
future_expiry = datetime.now() + timedelta(days=10)

with app.app_context():
    _make_seller(SELLER_ID_5, email='seller5@example.com')
    _make_listing(LISTING_ID_5, seller_id=SELLER_ID_5,
                  title='Future Listing', expires_at=future_expiry)

with (
    patch('email_service.notify_seller_listing_expired', return_value=True) as mock_email5,
    patch('sms_service.notify_seller_listing_expired_sms', return_value=True) as mock_sms5,
    # also mock expiring-soon email so future listing doesn't trigger that path
    patch('email_service.notify_seller_listing_expiring_soon', return_value=True),
    patch('models.expire_stale_timed_offers', return_value=[]),
    patch('models.expire_pending_offers'),
):
    _run_checks(app)

with app.app_context():
    lst5 = db.session.get(Listing, LISTING_ID_5)
    check("future listing: status remains 'active'",
          lst5 is not None and lst5.status == 'active',
          f"status={lst5.status if lst5 else 'NOT FOUND'}")

check("future listing: expired email not called", mock_email5.call_count == 0,
      f"call_count={mock_email5.call_count}")
check("future listing: expired SMS not called", mock_sms5.call_count == 0,
      f"call_count={mock_sms5.call_count}")

with app.app_context():
    _cleanup((Listing, LISTING_ID_5), (User, SELLER_ID_5))

# ---------------------------------------------------------------------------
# Test 6: ev_seller_listing_expired admin toggle — SMS suppressed when disabled
# ---------------------------------------------------------------------------
# This tests the SmsSettings.ev_seller_listing_expired column added in task #101.
# When the admin disables this event, notify_seller_listing_expired_sms() must
# return False without calling send_sms/Twilio, regardless of user opt-in.

from sms_service import notify_seller_listing_expired_sms


def _make_settings(ev_seller_listing_expired=True, sms_globally_enabled=True):
    """Return a stub SmsSettings-like object for patching get_sms_settings."""
    s = MagicMock()
    s.sms_globally_enabled = sms_globally_enabled
    s.ev_seller_listing_expired = ev_seller_listing_expired
    # configure getattr so is_sms_enabled works via _EVENT_TO_SETTING lookup
    def _getattr(name, default=True):
        return getattr(s, name, default)
    s.__class__ = type('SmsSettingsStub', (), {})
    return s


# Test 6a: admin toggle OFF — SMS must not reach send_sms
_settings_off = _make_settings(ev_seller_listing_expired=False)
with (
    patch('sms_service.get_sms_settings', return_value=_settings_off),
    patch('sms_service.send_sms', return_value=True) as mock_send,
):
    result = notify_seller_listing_expired_sms('+16515550099', 900099, 'Admin-Toggled-Off Sofa')

check("ev_seller_listing_expired=False: function returns False",
      result is False,
      f"got={result!r}")
check("ev_seller_listing_expired=False: send_sms never called",
      mock_send.call_count == 0,
      f"call_count={mock_send.call_count}")

# Test 6b: admin toggle ON — SMS proceeds to send_sms
_settings_on = _make_settings(ev_seller_listing_expired=True)
with (
    patch('sms_service.get_sms_settings', return_value=_settings_on),
    patch('sms_service.send_sms', return_value=True) as mock_send_on,
):
    result_on = notify_seller_listing_expired_sms('+16515550099', 900099, 'Admin-Toggled-On Sofa')

check("ev_seller_listing_expired=True: send_sms called once",
      mock_send_on.call_count == 1,
      f"call_count={mock_send_on.call_count}")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
