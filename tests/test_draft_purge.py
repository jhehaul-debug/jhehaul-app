"""
Tests: purge_abandoned_drafts uses a 48-hour threshold that is strictly wider
than the 24-hour reminder window, so a draft cannot be deleted before its
reminder has a chance to fire.

Verifies:
- The constants confirm DRAFT_MAX_AGE_HOURS (48) > REMINDER_MIN_HOURS (24)
- A draft ~24.5 h old is NOT deleted by purge_abandoned_drafts (still inside
  the reminder window; purge cutoff is 48 h)
- A draft 48 h + 1 minute old IS deleted by purge_abandoned_drafts

Isolation strategy:
  A separate Flask app instance is created for each test, backed by an
  isolated SQLite in-memory database.  Flask-SQLAlchemy's app-factory pattern
  allows `db.init_app(test_app)` to bind the shared `db` object to a fresh
  connection pool per app.  All tables are created with db.create_all() and
  torn down automatically when the in-memory DB is discarded.  Because the
  test DB contains only our fixture rows, both the real SQL query and exact
  count assertions are safe.  storage.delete_file is patched to prevent any
  filesystem side-effects.

Run with:  python tests/test_draft_purge.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from unittest.mock import patch
from flask import Flask

# Import db and models BEFORE creating the test apps so all model classes
# are registered on the shared SQLAlchemy metadata.
from models import db, User, Listing

results = []


def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


# ---------------------------------------------------------------------------
# Factory: create an isolated in-memory SQLite Flask app for a single test
# ---------------------------------------------------------------------------

def _make_isolated_app():
    """Return a fresh Flask app with an empty in-memory SQLite database."""
    iso_app = Flask(__name__)
    iso_app.config.update({
        # Each call uses a distinct URI so multiple apps don't share the same
        # in-memory database.  Adding check_same_thread=False avoids SQLite
        # threading errors that occur when the background purge reads from a
        # thread-local session opened by the test thread.
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:?check_same_thread=False',
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'SECRET_KEY': 'test-only-not-secret',
        'TESTING': True,
        'WTF_CSRF_ENABLED': False,
    })
    db.init_app(iso_app)
    with iso_app.app_context():
        db.create_all()
    return iso_app


def _make_seller(app_ctx, uid, email='seller@example.com'):
    with app_ctx.app_context():
        u = User(
            id=uid,
            email=email,
            first_name='PurgeSeller',
            age_confirmed=True,
            user_type='customer',
        )
        db.session.merge(u)
        db.session.commit()


def _make_draft(app_ctx, listing_id, seller_id, age_hours, title=''):
    """Create an untitled draft listing aged to *age_hours* ago."""
    with app_ctx.app_context():
        lst = Listing(
            id=listing_id,
            seller_id=seller_id,
            title=title,
            price=0.0,
            price_type='fixed',
            status='draft',
            draft_reminder_sent=False,
            created_at=datetime.now() - timedelta(hours=age_hours),
        )
        db.session.merge(lst)
        db.session.commit()


from draft_cleanup import purge_abandoned_drafts, DRAFT_MAX_AGE_HOURS, REMINDER_MIN_HOURS

# ---------------------------------------------------------------------------
# Sanity check — threshold relationship is correct in the constants themselves
# ---------------------------------------------------------------------------

check(
    "DRAFT_MAX_AGE_HOURS (purge) > REMINDER_MIN_HOURS (reminder): thresholds are compatible",
    DRAFT_MAX_AGE_HOURS > REMINDER_MIN_HOURS,
    f"DRAFT_MAX_AGE_HOURS={DRAFT_MAX_AGE_HOURS}, REMINDER_MIN_HOURS={REMINDER_MIN_HOURS}",
)

# ---------------------------------------------------------------------------
# Test 1 — draft ~24.5 h old → NOT purged (still in reminder window)
#
# The real SQL filter runs against the isolated DB.  The only row present is
# our 24.5 h fixture; it should NOT satisfy `created_at < now() - 48h`, so
# the candidate set is empty and deleted == 0 is exact.
# ---------------------------------------------------------------------------

SELLER_1 = 'test-dp-seller-01'
LISTING_1 = 920001

test_app_1 = _make_isolated_app()
_make_seller(test_app_1, SELLER_1, email='draft_purge_1@example.com')
_make_draft(test_app_1, LISTING_1, SELLER_1, age_hours=24.5)

with patch('storage.delete_file', return_value=None):
    deleted1 = purge_abandoned_drafts(test_app_1)

with test_app_1.app_context():
    lst1 = db.session.get(Listing, LISTING_1)
    check(
        "Draft at 24.5 h old: still exists after purge (not yet eligible)",
        lst1 is not None,
        f"listing={'found' if lst1 else 'DELETED — should have been kept'}",
    )
    check(
        "Draft at 24.5 h old: purge returns 0 deleted",
        deleted1 == 0,
        f"returned={deleted1}",
    )
    check(
        "Draft at 24.5 h old: draft_reminder_sent flag unchanged (still False)",
        lst1 is not None and lst1.draft_reminder_sent is False,
        f"flag={lst1.draft_reminder_sent if lst1 else 'N/A'}",
    )

# ---------------------------------------------------------------------------
# Test 2 — draft 48 h + 1 minute old → IS purged
#
# The real SQL filter runs; the fixture satisfies `created_at < now() - 48h`.
# Because the isolated DB contains only our one fixture row, deleted == 1 is
# also exact.
# ---------------------------------------------------------------------------

SELLER_2 = 'test-dp-seller-02'
LISTING_2 = 920002

# 48 hours + 1 minute expressed as fractional hours
AGE_JUST_OVER_48H = 48 + (1 / 60)

test_app_2 = _make_isolated_app()
_make_seller(test_app_2, SELLER_2, email='draft_purge_2@example.com')
_make_draft(test_app_2, LISTING_2, SELLER_2, age_hours=AGE_JUST_OVER_48H)

with patch('storage.delete_file', return_value=None):
    deleted2 = purge_abandoned_drafts(test_app_2)

with test_app_2.app_context():
    lst2 = db.session.get(Listing, LISTING_2)
    check(
        "Draft at 48 h + 1 min old: deleted by purge",
        lst2 is None,
        f"listing={'STILL EXISTS — should have been purged' if lst2 else 'correctly deleted'}",
    )
    check(
        "Draft at 48 h + 1 min old: purge returns 1 deleted",
        deleted2 == 1,
        f"returned={deleted2}",
    )

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
