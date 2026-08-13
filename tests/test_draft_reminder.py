"""
Tests: draft-reminder email fires at the right time and never sends twice.

Verifies:
- A draft 24.5 h old receives exactly one email and gets draft_reminder_sent = True
- A draft < 24 h old receives no reminder
- A draft > 48 h old (past the deletion window) receives no reminder
- A draft whose draft_reminder_sent flag is already True receives no second email

Run with:  python tests/test_draft_reminder.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from unittest.mock import patch
from app import app, db
import routes  # noqa: F401 — registers all routes, required for app context
from models import User, Listing

results = []


def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_seller(uid, email='seller@example.com'):
    u = User(
        id=uid,
        email=email,
        first_name='Draft',
        age_confirmed=True,
        user_type='customer',
    )
    db.session.merge(u)
    db.session.commit()
    return db.session.get(User, uid)


def _make_draft(listing_id, seller_id, age_hours, reminder_sent=False):
    """Create an untitled draft listing aged to *age_hours* ago."""
    lst = Listing(
        id=listing_id,
        seller_id=seller_id,
        title='',            # untitled — required for draft-reminder eligibility
        price=0.0,
        price_type='fixed',
        status='draft',
        draft_reminder_sent=reminder_sent,
        created_at=datetime.now() - timedelta(hours=age_hours),
    )
    db.session.merge(lst)
    db.session.commit()
    return db.session.get(Listing, listing_id)


def _cleanup(*id_pairs):
    """Delete test rows by (Model, pk) pairs."""
    for Model, pk in id_pairs:
        obj = db.session.get(Model, pk)
        if obj:
            db.session.delete(obj)
    db.session.commit()


from draft_cleanup import send_draft_reminders

# ---------------------------------------------------------------------------
# Test 1 — draft aged 24.5 h → one email, flag set to True
# ---------------------------------------------------------------------------

SELLER_1 = 'test-dr-seller-01'
LISTING_1 = 910001
SELLER_EMAIL_1 = 'draft_reminder_1@example.com'

with app.app_context():
    _make_seller(SELLER_1, email=SELLER_EMAIL_1)
    _make_draft(LISTING_1, SELLER_1, age_hours=24.5)

with patch('email_service.notify_seller_draft_expiring', return_value=True) as mock_email1:
    sent = send_draft_reminders(app)

with app.app_context():
    lst1 = db.session.get(Listing, LISTING_1)
    check("24.5 h draft: send_draft_reminders returns 1",
          sent == 1,
          f"returned={sent}")
    check("24.5 h draft: email called exactly once",
          mock_email1.call_count == 1,
          f"call_count={mock_email1.call_count}")
    check("24.5 h draft: draft_reminder_sent set to True",
          lst1 is not None and lst1.draft_reminder_sent is True,
          f"flag={lst1.draft_reminder_sent if lst1 else 'NOT FOUND'}")

    if mock_email1.call_count >= 1:
        call_args = mock_email1.call_args[0]
        check("24.5 h draft: email recipient matches seller email",
              call_args[0] == SELLER_EMAIL_1,
              f"got={call_args[0]!r}")
        check("24.5 h draft: email listing_id matches",
              call_args[1] == LISTING_1,
              f"got={call_args[1]!r}")

    _cleanup((Listing, LISTING_1), (User, SELLER_1))

# ---------------------------------------------------------------------------
# Test 2 — draft aged 23 h (too young) → no email
# ---------------------------------------------------------------------------

SELLER_2 = 'test-dr-seller-02'
LISTING_2 = 910002

with app.app_context():
    _make_seller(SELLER_2, email='draft_reminder_2@example.com')
    _make_draft(LISTING_2, SELLER_2, age_hours=23)

with patch('email_service.notify_seller_draft_expiring', return_value=True) as mock_email2:
    sent2 = send_draft_reminders(app)

with app.app_context():
    lst2 = db.session.get(Listing, LISTING_2)
    check("23 h draft: email NOT called",
          mock_email2.call_count == 0,
          f"call_count={mock_email2.call_count}")
    check("23 h draft: draft_reminder_sent still False",
          lst2 is not None and lst2.draft_reminder_sent is False,
          f"flag={lst2.draft_reminder_sent if lst2 else 'NOT FOUND'}")

    _cleanup((Listing, LISTING_2), (User, SELLER_2))

# ---------------------------------------------------------------------------
# Test 3 — draft aged 49 h (past deletion window) → no email
# ---------------------------------------------------------------------------

SELLER_3 = 'test-dr-seller-03'
LISTING_3 = 910003

with app.app_context():
    _make_seller(SELLER_3, email='draft_reminder_3@example.com')
    _make_draft(LISTING_3, SELLER_3, age_hours=49)

with patch('email_service.notify_seller_draft_expiring', return_value=True) as mock_email3:
    sent3 = send_draft_reminders(app)

with app.app_context():
    lst3 = db.session.get(Listing, LISTING_3)
    check("49 h draft: email NOT called (past deletion window)",
          mock_email3.call_count == 0,
          f"call_count={mock_email3.call_count}")
    check("49 h draft: draft_reminder_sent still False",
          lst3 is not None and lst3.draft_reminder_sent is False,
          f"flag={lst3.draft_reminder_sent if lst3 else 'NOT FOUND'}")

    _cleanup((Listing, LISTING_3), (User, SELLER_3))

# ---------------------------------------------------------------------------
# Test 4 — draft_reminder_sent already True → no second email
# ---------------------------------------------------------------------------

SELLER_4 = 'test-dr-seller-04'
LISTING_4 = 910004

with app.app_context():
    _make_seller(SELLER_4, email='draft_reminder_4@example.com')
    _make_draft(LISTING_4, SELLER_4, age_hours=30, reminder_sent=True)

with patch('email_service.notify_seller_draft_expiring', return_value=True) as mock_email4:
    sent4 = send_draft_reminders(app)

with app.app_context():
    lst4 = db.session.get(Listing, LISTING_4)
    check("already-flagged draft: email NOT called again",
          mock_email4.call_count == 0,
          f"call_count={mock_email4.call_count}")
    check("already-flagged draft: returns 0 sent",
          sent4 == 0,
          f"returned={sent4}")
    check("already-flagged draft: draft_reminder_sent remains True",
          lst4 is not None and lst4.draft_reminder_sent is True,
          f"flag={lst4.draft_reminder_sent if lst4 else 'NOT FOUND'}")

    _cleanup((Listing, LISTING_4), (User, SELLER_4))

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
