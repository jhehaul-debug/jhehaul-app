"""
Tests: draft-reminder email fires at the right time and never sends twice.

Verifies:
- A draft 24.5 h old with no recent activity receives exactly one email and
  gets draft_reminder_sent = True
- A draft < 24 h old receives no reminder
- A draft > 48 h old (past the deletion window) receives no reminder
- A draft whose draft_reminder_sent flag is already True receives no second email
- A draft touched within the grace window (draft_activity_at recent) receives no email
- A draft whose draft_activity_at is older than the grace window still gets the email

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


def _make_draft(listing_id, seller_id, age_hours, reminder_sent=False,
                activity_hours=None, title=''):
    """Create a draft listing aged to *age_hours* ago.

    title — defaults to '' (untitled, eligible for reminder).  Pass a non-empty
    string to simulate a seller who has already started filling in the wizard.

    activity_hours — if given, sets draft_activity_at to that many hours ago,
    simulating a seller who recently touched the draft.  None (default) leaves
    draft_activity_at as NULL, which means "untouched since creation" and is
    always eligible for the reminder.
    """
    activity_at = (
        datetime.now() - timedelta(hours=activity_hours)
        if activity_hours is not None
        else None
    )
    lst = Listing(
        id=listing_id,
        seller_id=seller_id,
        title=title,
        price=0.0,
        price_type='fixed',
        status='draft',
        draft_reminder_sent=reminder_sent,
        created_at=datetime.now() - timedelta(hours=age_hours),
        draft_activity_at=activity_at,
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
# Test 5 — draft_activity_at set via ORM (mimics listing_step field-edit save),
#           2 h ago → recency guard fires, no email
# ---------------------------------------------------------------------------

SELLER_5A = 'test-dr-seller-05a'
LISTING_5A = 910007

with app.app_context():
    _make_seller(SELLER_5A, email='draft_reminder_5a@example.com')
    # Create the draft with NO draft_activity_at (simulates old row)
    _make_draft(LISTING_5A, SELLER_5A, age_hours=24.5)
    # Now simulate listing_step stamping draft_activity_at via the ORM (2 h ago)
    lst5a = db.session.get(Listing, LISTING_5A)
    lst5a.draft_activity_at = datetime.now() - timedelta(hours=2)
    db.session.commit()

with patch('email_service.notify_seller_draft_expiring', return_value=True) as mock_email5a:
    sent5a = send_draft_reminders(app)

with app.app_context():
    lst5a = db.session.get(Listing, LISTING_5A)
    check("wizard-step ORM stamp (2 h ago): email NOT called",
          mock_email5a.call_count == 0,
          f"call_count={mock_email5a.call_count}")
    check("wizard-step ORM stamp (2 h ago): draft_reminder_sent still False",
          lst5a is not None and lst5a.draft_reminder_sent is False,
          f"flag={lst5a.draft_reminder_sent if lst5a else 'NOT FOUND'}")
    check("wizard-step ORM stamp (2 h ago): returns 0 sent",
          sent5a == 0,
          f"returned={sent5a}")

    _cleanup((Listing, LISTING_5A), (User, SELLER_5A))

# ---------------------------------------------------------------------------
# Test 6 — draft 24.5 h old but recently touched (draft_activity_at = 2 h ago)
#           → recency guard fires, no email
# ---------------------------------------------------------------------------

SELLER_5 = 'test-dr-seller-05'
LISTING_5 = 910005

with app.app_context():
    _make_seller(SELLER_5, email='draft_reminder_5@example.com')
    _make_draft(LISTING_5, SELLER_5, age_hours=24.5, activity_hours=2)

with patch('email_service.notify_seller_draft_expiring', return_value=True) as mock_email5:
    sent5 = send_draft_reminders(app)

with app.app_context():
    lst5 = db.session.get(Listing, LISTING_5)
    check("recently-touched draft (activity 2 h ago): email NOT called",
          mock_email5.call_count == 0,
          f"call_count={mock_email5.call_count}")
    check("recently-touched draft (activity 2 h ago): draft_reminder_sent still False",
          lst5 is not None and lst5.draft_reminder_sent is False,
          f"flag={lst5.draft_reminder_sent if lst5 else 'NOT FOUND'}")
    check("recently-touched draft (activity 2 h ago): returns 0 sent",
          sent5 == 0,
          f"returned={sent5}")

    _cleanup((Listing, LISTING_5), (User, SELLER_5))

# ---------------------------------------------------------------------------
# Test 6 — draft 24.5 h old, draft_activity_at = 12 h ago (outside grace window)
#           → recency guard does NOT fire, email is sent
# ---------------------------------------------------------------------------

SELLER_6 = 'test-dr-seller-06'
LISTING_6 = 910006
SELLER_EMAIL_6 = 'draft_reminder_6@example.com'

with app.app_context():
    _make_seller(SELLER_6, email=SELLER_EMAIL_6)
    _make_draft(LISTING_6, SELLER_6, age_hours=24.5, activity_hours=12)

with patch('email_service.notify_seller_draft_expiring', return_value=True) as mock_email6:
    sent6 = send_draft_reminders(app)

with app.app_context():
    lst6 = db.session.get(Listing, LISTING_6)
    check("stale-activity draft (activity 12 h ago): email called once",
          mock_email6.call_count == 1,
          f"call_count={mock_email6.call_count}")
    check("stale-activity draft (activity 12 h ago): draft_reminder_sent set to True",
          lst6 is not None and lst6.draft_reminder_sent is True,
          f"flag={lst6.draft_reminder_sent if lst6 else 'NOT FOUND'}")
    check("stale-activity draft (activity 12 h ago): returns 1 sent",
          sent6 == 1,
          f"returned={sent6}")

    _cleanup((Listing, LISTING_6), (User, SELLER_6))

# ---------------------------------------------------------------------------
# Test 7 — draft with a non-empty title (seller is mid-wizard) → no email
#           The eligibility filter requires title IS NULL or title == ''.
#           A draft that already has a title is excluded so the seller isn't
#           interrupted while they're actively filling in the form.
# ---------------------------------------------------------------------------

SELLER_7 = 'test-dr-seller-07'
LISTING_7 = 910008
SELLER_EMAIL_7 = 'draft_reminder_7@example.com'

with app.app_context():
    _make_seller(SELLER_7, email=SELLER_EMAIL_7)
    # Seller has already typed a title — they are mid-wizard; draft is 24.5 h old
    _make_draft(LISTING_7, SELLER_7, age_hours=24.5, title='My Old Lawnmower')

with patch('email_service.notify_seller_draft_expiring', return_value=True) as mock_email7:
    sent7 = send_draft_reminders(app)

with app.app_context():
    lst7 = db.session.get(Listing, LISTING_7)
    check("mid-wizard draft (title set): email NOT called",
          mock_email7.call_count == 0,
          f"call_count={mock_email7.call_count}")
    check("mid-wizard draft (title set): draft_reminder_sent still False",
          lst7 is not None and lst7.draft_reminder_sent is False,
          f"flag={lst7.draft_reminder_sent if lst7 else 'NOT FOUND'}")
    check("mid-wizard draft (title set): returns 0 sent",
          sent7 == 0,
          f"returned={sent7}")

    _cleanup((Listing, LISTING_7), (User, SELLER_7))

# ---------------------------------------------------------------------------
# Test 8 — draft with an empty title (24.5 h old) still qualifies
#           Mirrors Test 1 but is an explicit companion to Test 7 so that
#           both sides of the title guard are captured together.
# ---------------------------------------------------------------------------

SELLER_8 = 'test-dr-seller-08'
LISTING_8 = 910009
SELLER_EMAIL_8 = 'draft_reminder_8@example.com'

with app.app_context():
    _make_seller(SELLER_8, email=SELLER_EMAIL_8)
    # title='' (default) — seller never reached the title field; reminder expected
    _make_draft(LISTING_8, SELLER_8, age_hours=24.5)

with patch('email_service.notify_seller_draft_expiring', return_value=True) as mock_email8:
    sent8 = send_draft_reminders(app)

with app.app_context():
    lst8 = db.session.get(Listing, LISTING_8)
    check("untitled draft (empty title): email called once",
          mock_email8.call_count == 1,
          f"call_count={mock_email8.call_count}")
    check("untitled draft (empty title): draft_reminder_sent set to True",
          lst8 is not None and lst8.draft_reminder_sent is True,
          f"flag={lst8.draft_reminder_sent if lst8 else 'NOT FOUND'}")
    check("untitled draft (empty title): returns 1 sent",
          sent8 == 1,
          f"returned={sent8}")

    _cleanup((Listing, LISTING_8), (User, SELLER_8))

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
