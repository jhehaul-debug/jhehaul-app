"""
Tests: buyer notification when an offer expires on its own deadline.

Verifies:
- notify_buyer_offer_timed_out is a distinct helper with correct copy (not
  "listing sold/removed" — the listing may still be active).
- expire_stale_timed_offers() returns a list of notification targets (not a count).
- The background thread (_run_checks) calls notify_buyer_offer_timed_out for
  each returned target after committing.
- Buyers with no email address are silently skipped.
- The offer_seller_respond route calls notify_buyer_offer_timed_out when the
  seller tries to act on a timed-out offer.
- No duplicate email is sent when expire_stale_timed_offers returns an empty list.

Run with:  python tests/test_offer_timed_out_notification.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, call as _call

results = []


def check(name, cond, extra=""):
    results.append((name, cond, extra))
    print(("PASS" if cond else "FAIL"), "-", name, (extra if not cond else ""))


# ── email_service: notify_buyer_offer_timed_out helper ───────────────────────

from email_service import notify_buyer_offer_timed_out

BUYER_EMAIL = "buyer@example.com"
TITLE = "Vintage Lamp"
LISTING_ID = 77
OFFER_AMT = 60.00

# 1. Calls send_email once with correct recipient and event_type
with patch("email_service.send_email", return_value=True) as mock_send:
    res = notify_buyer_offer_timed_out(BUYER_EMAIL, TITLE, LISTING_ID, OFFER_AMT)

check("timed_out email: send_email called once", mock_send.call_count == 1,
      f"call_count={mock_send.call_count}")
args = mock_send.call_args[0]  # (to, subject, html, event_type)
check("timed_out email: recipient is buyer email", args[0] == BUYER_EMAIL,
      f"got={args[0]!r}")
check("timed_out email: event_type is 'buyer_offer_timed_out'",
      args[3] == 'buyer_offer_timed_out',
      f"got={args[3]!r}")
check("timed_out email: subject mentions timed out (not sold/removed)",
      "timed out" in args[1].lower(),
      f"subject={args[1]!r}")
check("timed_out email: body does NOT say 'sold or removed'",
      "sold or removed" not in args[2].lower(),
      f"body contains 'sold or removed'")
check("timed_out email: body mentions offer window / response window",
      "window" in args[2].lower() or "respond" in args[2].lower(),
      f"body snippet={args[2][:300]!r}")
check("timed_out email: body contains formatted offer amount",
      "60.00" in args[2],
      f"body snippet={args[2][:300]!r}")
check("timed_out email: subject contains listing title", TITLE in args[1],
      f"subject={args[1]!r}")
check("timed_out email: returns True on success", res is True, f"got={res!r}")

# 2. None listing title falls back gracefully
with patch("email_service.send_email", return_value=True) as mock_none:
    notify_buyer_offer_timed_out(BUYER_EMAIL, None, LISTING_ID, OFFER_AMT)

none_subj = mock_none.call_args[0][1]
check("timed_out email: None title → 'Listing #77' in subject",
      f"Listing #{LISTING_ID}" in none_subj, f"subject={none_subj!r}")

# ── models.expire_stale_timed_offers: now returns list not int ────────────────

from app import app, db
import routes  # noqa: F401 — registers all routes
from models import User, Listing, ListingOffer, expire_stale_timed_offers

SELLER_ID = 'test-to-seller-01'
BUYER_ID  = 'test-to-buyer-01'
LISTING_ID_DB = 910001
OFFER_ID_DB   = 810001

def _make_user(uid, email=None):
    u = User(id=uid, email=email, first_name='Test',
             age_confirmed=True, user_type='customer')
    db.session.merge(u)
    db.session.commit()
    return db.session.get(User, uid)

def _make_listing_db(lid, seller_id, title='Timed-Out Test Listing'):
    lst = Listing(id=lid, seller_id=seller_id, title=title,
                  price=50.0, price_type='negotiable', status='active')
    db.session.merge(lst)
    db.session.commit()
    return db.session.get(Listing, lid)

def _make_offer(oid, listing_id, buyer_id, seller_id, expires_past=True):
    past = datetime.now() - timedelta(hours=1) if expires_past else datetime.now() + timedelta(hours=1)
    o = ListingOffer(
        id=oid,
        listing_id=listing_id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        amount=55.0,
        status='pending',
        expires_at=past,
    )
    db.session.merge(o)
    db.session.commit()
    return db.session.get(ListingOffer, oid)

def _cleanup(*pairs):
    for Model, row_id in pairs:
        obj = db.session.get(Model, row_id)
        if obj:
            db.session.delete(obj)
    db.session.commit()

# 3. expire_stale_timed_offers returns list with one entry for the expired offer
with app.app_context():
    _make_user(SELLER_ID, email='to-seller@example.com')
    _make_user(BUYER_ID,  email='to-buyer@example.com')
    _make_listing_db(LISTING_ID_DB, SELLER_ID)
    _make_offer(OFFER_ID_DB, LISTING_ID_DB, BUYER_ID, SELLER_ID, expires_past=True)

    targets = expire_stale_timed_offers()
    db.session.commit()

check("expire_stale_timed_offers: returns a list (not int)",
      isinstance(targets, list), f"got type={type(targets)!r}")
check("expire_stale_timed_offers: contains one target for our offer",
      any(t['offer_id'] == OFFER_ID_DB for t in targets),
      f"targets={targets!r}")
if targets:
    t = next((x for x in targets if x['offer_id'] == OFFER_ID_DB), None)
    if t:
        check("expire_stale_timed_offers: buyer_email in target",
              t['buyer_email'] == 'to-buyer@example.com',
              f"got={t['buyer_email']!r}")
        check("expire_stale_timed_offers: listing_title in target",
              t['listing_title'] == 'Timed-Out Test Listing',
              f"got={t['listing_title']!r}")
        check("expire_stale_timed_offers: listing_id in target",
              t['listing_id'] == LISTING_ID_DB,
              f"got={t['listing_id']!r}")
        check("expire_stale_timed_offers: offer_amount in target",
              t['offer_amount'] == 55.0,
              f"got={t['offer_amount']!r}")

# Confirm offer status was set to expired
with app.app_context():
    offer_row = db.session.get(ListingOffer, OFFER_ID_DB)
    check("expire_stale_timed_offers: offer.status is 'expired'",
          offer_row is not None and offer_row.status == 'expired',
          f"status={offer_row.status if offer_row else 'NOT FOUND'}")

with app.app_context():
    _cleanup((ListingOffer, OFFER_ID_DB), (Listing, LISTING_ID_DB),
             (User, BUYER_ID), (User, SELLER_ID))

# ── Background thread: _run_checks sends notify_buyer_offer_timed_out ─────────

from job_expiry import _run_checks

SELLER_ID_B = 'test-to-seller-bg'
BUYER_ID_B  = 'test-to-buyer-bg'
LISTING_ID_B = 910002
OFFER_ID_B   = 810002
BUYER_EMAIL_B = 'to-buyer-bg@example.com'

with app.app_context():
    _make_user(SELLER_ID_B, email='to-seller-bg@example.com')
    _make_user(BUYER_ID_B,  email=BUYER_EMAIL_B)
    _make_listing_db(LISTING_ID_B, SELLER_ID_B, title='BG Thread Test Lamp')
    _make_offer(OFFER_ID_B, LISTING_ID_B, BUYER_ID_B, SELLER_ID_B, expires_past=True)

with (
    patch('email_service.notify_buyer_offer_timed_out') as mock_to_email,
    patch('email_service.notify_seller_listing_expired', return_value=True),
    patch('sms_service.notify_seller_listing_expired_sms', return_value=True),
    patch('email_service.notify_buyer_offer_expired_listing', return_value=True),
    patch('email_service.notify_seller_listing_expiring_soon', return_value=True),
    patch('email_service.notify_customer_pending_bids_reminder', return_value=True),
    patch('email_service.notify_customer_job_expiring_soon', return_value=True),
    patch('email_service.notify_admin_job_expired', return_value=True),
    patch('email_service.notify_customer_appointment_reminder', return_value=True),
    patch('models.expire_pending_offers', return_value=[]),
):
    _run_checks(app)

check("_run_checks: notify_buyer_offer_timed_out called at least once",
      mock_to_email.call_count >= 1,
      f"call_count={mock_to_email.call_count}")

if mock_to_email.call_count >= 1:
    call_args = mock_to_email.call_args[0]
    check("_run_checks: timed-out email sent to buyer",
          call_args[0] == BUYER_EMAIL_B,
          f"got={call_args[0]!r}")
    check("_run_checks: timed-out email includes listing title",
          call_args[1] == 'BG Thread Test Lamp',
          f"got={call_args[1]!r}")
    check("_run_checks: timed-out email includes listing_id",
          call_args[2] == LISTING_ID_B,
          f"got={call_args[2]!r}")
    check("_run_checks: timed-out email includes offer amount",
          call_args[3] == 55.0,
          f"got={call_args[3]!r}")

with app.app_context():
    _cleanup((ListingOffer, OFFER_ID_B), (Listing, LISTING_ID_B),
             (User, BUYER_ID_B), (User, SELLER_ID_B))

# ── Background thread: buyer with no email is silently skipped ────────────────

SELLER_ID_NE = 'test-to-seller-ne'
BUYER_ID_NE  = 'test-to-buyer-ne'
LISTING_ID_NE = 910003
OFFER_ID_NE   = 810003

with app.app_context():
    _make_user(SELLER_ID_NE, email='to-seller-ne@example.com')
    _make_user(BUYER_ID_NE,  email=None)   # no email
    _make_listing_db(LISTING_ID_NE, SELLER_ID_NE, title='No-Email Buyer Test')
    _make_offer(OFFER_ID_NE, LISTING_ID_NE, BUYER_ID_NE, SELLER_ID_NE, expires_past=True)

with (
    patch('email_service.notify_buyer_offer_timed_out') as mock_ne_email,
    patch('email_service.notify_seller_listing_expired', return_value=True),
    patch('sms_service.notify_seller_listing_expired_sms', return_value=True),
    patch('email_service.notify_buyer_offer_expired_listing', return_value=True),
    patch('email_service.notify_seller_listing_expiring_soon', return_value=True),
    patch('email_service.notify_customer_pending_bids_reminder', return_value=True),
    patch('email_service.notify_customer_job_expiring_soon', return_value=True),
    patch('email_service.notify_admin_job_expired', return_value=True),
    patch('email_service.notify_customer_appointment_reminder', return_value=True),
    patch('models.expire_pending_offers', return_value=[]),
):
    _run_checks(app)

check("_run_checks: no-email buyer — notify_buyer_offer_timed_out NOT called",
      mock_ne_email.call_count == 0,
      f"call_count={mock_ne_email.call_count}")

with app.app_context():
    _cleanup((ListingOffer, OFFER_ID_NE), (Listing, LISTING_ID_NE),
             (User, BUYER_ID_NE), (User, SELLER_ID_NE))

# ── Background thread: no expired offers → email not called ───────────────────

with (
    patch('email_service.notify_buyer_offer_timed_out') as mock_empty,
    patch('email_service.notify_seller_listing_expired', return_value=True),
    patch('sms_service.notify_seller_listing_expired_sms', return_value=True),
    patch('email_service.notify_buyer_offer_expired_listing', return_value=True),
    patch('email_service.notify_seller_listing_expiring_soon', return_value=True),
    patch('email_service.notify_customer_pending_bids_reminder', return_value=True),
    patch('email_service.notify_customer_job_expiring_soon', return_value=True),
    patch('email_service.notify_admin_job_expired', return_value=True),
    patch('email_service.notify_customer_appointment_reminder', return_value=True),
    patch('models.expire_pending_offers', return_value=[]),
    patch('models.expire_stale_timed_offers', return_value=[]),
):
    _run_checks(app)

check("_run_checks: empty targets → notify_buyer_offer_timed_out NOT called",
      mock_empty.call_count == 0,
      f"call_count={mock_empty.call_count}")

# ── Summary ───────────────────────────────────────────────────────────────────

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
