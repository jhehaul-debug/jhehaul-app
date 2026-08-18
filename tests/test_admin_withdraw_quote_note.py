"""
Task 229: Confirm the withdrawal note still passes through the admin route end-to-end.

Verifies:
- POST /admin/quote/<id>/withdraw with a withdrawal_note calls notify_customer_quote_withdrawn
  and notify_customer_quote_withdrawn_sms with that exact note value.
- POST with no withdrawal_note calls both helpers with note=None (empty string stripped to None).
- The quote row has withdrawal_note persisted correctly in both cases.

Run with:  python tests/test_admin_withdraw_quote_note.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock, call
import flask_login.utils as _flu
from app import app, db
import routes  # noqa: F401 — registers all routes on the Flask app
from models import User, Job, Quote

results = []


def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


# ── Fixtures ──────────────────────────────────────────────────────────────────

ADMIN_ID   = 'test-admin-229'
CUST_ID    = 'test-cust-229'

def _setup():
    """Create minimal DB rows needed for the route; return (admin, customer, job, quote)."""
    admin = User.query.get(ADMIN_ID)
    if not admin:
        admin = User(
            id=ADMIN_ID,
            email='admin229@example.com',
            first_name='Admin',
            age_confirmed=True,
            is_admin=True,
            user_type='customer',
        )
        db.session.add(admin)

    customer = User.query.get(CUST_ID)
    if not customer:
        customer = User(
            id=CUST_ID,
            email='cust229@example.com',
            first_name='Customer',
            age_confirmed=True,
            user_type='customer',
            notify_sms=True,
            sms_consent=True,
            phone='6515550229',
        )
        db.session.add(customer)

    db.session.flush()

    job = Job(
        customer_id=CUST_ID,
        customer_name='Customer 229',
        pickup_address='123 Test St',
        pickup_zip='55101',
        job_description='Test junk removal for task 229',
        service_type='Junk Removal',
        status='quoted',
    )
    db.session.add(job)
    db.session.flush()

    quote = Quote(
        job_id=job.id,
        price=150.0,
        deposit_amount=50.0,
        status='pending',
    )
    db.session.add(quote)
    db.session.commit()
    return admin, customer, job, quote


def _teardown(job_id, quote_id):
    try:
        Quote.query.filter_by(id=quote_id).delete()
        Job.query.filter_by(id=job_id).delete()
        User.query.filter_by(id=CUST_ID).delete()
        User.query.filter_by(id=ADMIN_ID).delete()
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f'  [teardown error] {e}')


# ── Tests ─────────────────────────────────────────────────────────────────────

with app.app_context():
    admin, customer, job, quote = _setup()
    job_id   = job.id
    quote_id = quote.id

    _flu._get_user = lambda: admin
    client = app.test_client()

    # ------------------------------------------------------------------
    # Case 1: POST with a withdrawal_note — both helpers must receive it
    # ------------------------------------------------------------------
    NOTE = 'Unable to service this area at the moment.'

    email_mock = MagicMock(return_value=True)
    sms_mock   = MagicMock(return_value=True)

    with patch('routes.notify_customer_quote_withdrawn', email_mock), \
         patch('routes.notify_customer_quote_withdrawn_sms', sms_mock):
        r = client.post(
            f'/admin/quote/{quote_id}/withdraw',
            data={'withdrawal_note': NOTE},
            follow_redirects=False,
        )

    check(
        'Case 1: POST redirects (not an error page)',
        r.status_code in (301, 302, 303),
        f'status={r.status_code}',
    )

    check(
        'Case 1: email helper called exactly once',
        email_mock.call_count == 1,
        f'call_count={email_mock.call_count}',
    )

    check(
        'Case 1: email helper received the correct withdrawal_note',
        email_mock.call_count == 1
        and email_mock.call_args.kwargs.get('withdrawal_note') == NOTE,
        f'kwargs={email_mock.call_args.kwargs if email_mock.call_count else "not called"}',
    )

    check(
        'Case 1: SMS helper called exactly once',
        sms_mock.call_count == 1,
        f'call_count={sms_mock.call_count}',
    )

    check(
        'Case 1: SMS helper received the correct withdrawal_note',
        sms_mock.call_count == 1
        and sms_mock.call_args.kwargs.get('withdrawal_note') == NOTE,
        f'kwargs={sms_mock.call_args.kwargs if sms_mock.call_count else "not called"}',
    )

    # Confirm the note was persisted to the DB
    db.session.expire_all()
    saved_quote = Quote.query.get(quote_id)
    check(
        'Case 1: withdrawal_note persisted to the quote row',
        saved_quote is not None and saved_quote.withdrawal_note == NOTE,
        f'withdrawal_note={getattr(saved_quote, "withdrawal_note", "MISSING")}',
    )
    check(
        'Case 1: quote status set to "withdrawn"',
        saved_quote is not None and saved_quote.status == 'withdrawn',
        f'status={getattr(saved_quote, "status", "MISSING")}',
    )

    # ------------------------------------------------------------------
    # Case 2: POST with no note — helpers must receive note=None
    # ------------------------------------------------------------------

    # Reset quote to pending for second test
    saved_quote.status = 'pending'
    saved_quote.withdrawal_note = None
    db.session.commit()

    email_mock2 = MagicMock(return_value=True)
    sms_mock2   = MagicMock(return_value=True)

    with patch('routes.notify_customer_quote_withdrawn', email_mock2), \
         patch('routes.notify_customer_quote_withdrawn_sms', sms_mock2):
        r2 = client.post(
            f'/admin/quote/{quote_id}/withdraw',
            data={'withdrawal_note': ''},   # empty string → stripped to None in route
            follow_redirects=False,
        )

    check(
        'Case 2: POST with no note redirects (not an error page)',
        r2.status_code in (301, 302, 303),
        f'status={r2.status_code}',
    )

    check(
        'Case 2: email helper called exactly once',
        email_mock2.call_count == 1,
        f'call_count={email_mock2.call_count}',
    )

    check(
        'Case 2: email helper received withdrawal_note=None',
        email_mock2.call_count == 1
        and email_mock2.call_args.kwargs.get('withdrawal_note') is None,
        f'kwargs={email_mock2.call_args.kwargs if email_mock2.call_count else "not called"}',
    )

    check(
        'Case 2: SMS helper called exactly once',
        sms_mock2.call_count == 1,
        f'call_count={sms_mock2.call_count}',
    )

    check(
        'Case 2: SMS helper received withdrawal_note=None',
        sms_mock2.call_count == 1
        and sms_mock2.call_args.kwargs.get('withdrawal_note') is None,
        f'kwargs={sms_mock2.call_args.kwargs if sms_mock2.call_count else "not called"}',
    )

    db.session.expire_all()
    saved_quote2 = Quote.query.get(quote_id)
    check(
        'Case 2: withdrawal_note stored as None when no note submitted',
        saved_quote2 is not None and saved_quote2.withdrawal_note is None,
        f'withdrawal_note={getattr(saved_quote2, "withdrawal_note", "MISSING")}',
    )

    _teardown(job_id, quote_id)


# ── Summary ───────────────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f'\n{len(results) - len(failed)}/{len(results)} passed')
sys.exit(1 if failed else 0)
