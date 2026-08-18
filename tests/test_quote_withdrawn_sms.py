"""
Tests: notify_customer_quote_withdrawn_sms respects the admin SMS kill-switch.

Verifies:
- SMS is sent when globally enabled AND ev_quote_withdrawn is True
- SMS is skipped when ev_quote_withdrawn is False
- SMS is skipped when sms_globally_enabled is False
- _EVENT_TO_SETTING contains the 'customer_quote_withdrawn' → 'ev_quote_withdrawn' mapping
- Admin can POST to /admin/sms-settings/update to disable ev_quote_withdrawn,
  and the withdrawal notification is then suppressed end-to-end

Run with:  python tests/test_quote_withdrawn_sms.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock

results = []


def check(name, cond, extra=""):
    results.append((name, cond, extra))
    print(("PASS" if cond else "FAIL"), "-", name, (extra if not cond else ""))


# ── Unit tests: is_sms_enabled kill-switch ────────────────────────────────────

def _make_settings(globally_enabled=True, ev_quote_withdrawn=True):
    s = MagicMock()
    s.sms_globally_enabled = globally_enabled
    s.ev_quote_withdrawn = ev_quote_withdrawn
    return s


def _run_notify(settings_obj, send_sms_mock):
    with patch("sms_service.get_sms_settings", return_value=settings_obj), \
         patch("sms_service.send_sms", send_sms_mock):
        from sms_service import notify_customer_quote_withdrawn_sms
        return notify_customer_quote_withdrawn_sms(
            phone="+16515550001",
            job_id=42,
            service_type="Junk Removal",
            withdrawal_note="Price changed.",
        )


# Test 1: sends when enabled
send_mock_1 = MagicMock(return_value=True)
result_1 = _run_notify(_make_settings(globally_enabled=True, ev_quote_withdrawn=True), send_mock_1)
check(
    "SMS sent when globally enabled AND ev_quote_withdrawn=True",
    send_mock_1.called and result_1 is True,
    f"called={send_mock_1.called}, result={result_1!r}",
)
check(
    "send_sms called with correct event_type",
    send_mock_1.call_args is not None
    and send_mock_1.call_args[0][2] == "customer_quote_withdrawn",
    f"call_args={send_mock_1.call_args}",
)

# Test 2: skips when ev_quote_withdrawn disabled
send_mock_2 = MagicMock(return_value=True)
result_2 = _run_notify(_make_settings(globally_enabled=True, ev_quote_withdrawn=False), send_mock_2)
check(
    "SMS skipped when ev_quote_withdrawn=False",
    not send_mock_2.called and result_2 is False,
    f"called={send_mock_2.called}, result={result_2!r}",
)

# Test 3: skips when globally disabled
send_mock_3 = MagicMock(return_value=True)
result_3 = _run_notify(_make_settings(globally_enabled=False, ev_quote_withdrawn=True), send_mock_3)
check(
    "SMS skipped when sms_globally_enabled=False",
    not send_mock_3.called and result_3 is False,
    f"called={send_mock_3.called}, result={result_3!r}",
)

# Test 4: mapping is correct
from sms_service import _EVENT_TO_SETTING
check(
    "'customer_quote_withdrawn' is in _EVENT_TO_SETTING",
    "customer_quote_withdrawn" in _EVENT_TO_SETTING,
    f"keys={list(_EVENT_TO_SETTING.keys())}",
)
check(
    "_EVENT_TO_SETTING['customer_quote_withdrawn'] == 'ev_quote_withdrawn'",
    _EVENT_TO_SETTING.get("customer_quote_withdrawn") == "ev_quote_withdrawn",
    f"mapped to={_EVENT_TO_SETTING.get('customer_quote_withdrawn')!r}",
)


# ── Integration test: admin route persists ev_quote_withdrawn ─────────────────

import flask_login.utils as _flu
from app import app
import routes  # noqa: F401

with app.app_context():
    from models import db, User, SmsSettings

    # Ensure an admin user exists
    admin = User.query.filter_by(is_admin=True).first()
    assert admin, "Integration test requires an admin user in the dev DB"

    # Reset settings to known state: both globally enabled and ev_quote_withdrawn=True
    settings = SmsSettings.query.first()
    if not settings:
        settings = SmsSettings()
        db.session.add(settings)
    settings.sms_globally_enabled = True
    settings.ev_quote_withdrawn = True
    db.session.commit()

    _flu._get_user = lambda: admin
    client = app.test_client()

    # POST to disable ev_quote_withdrawn (omitting the checkbox = unchecked = "0")
    form_data = {
        "sms_globally_enabled": "1",
        "ev_new_bid": "1",
        "ev_bid_accepted": "1",
        "ev_deposit_paid": "1",
        "ev_job_nearby": "1",
        "ev_job_completed": "1",
        "ev_job_cancelled": "1",
        "ev_bid_rejected": "0",
        "ev_admin_alert": "0",
        "ev_quote_received": "1",
        # ev_quote_withdrawn intentionally omitted → treated as "0"
        "email_fallback_to_sms": "0",
    }
    r = client.post("/admin/sms-settings/update", data=form_data)
    check(
        "POST /admin/sms-settings/update redirects without error",
        r.status_code in (302, 303),
        f"status={r.status_code}",
    )

    db.session.expire_all()
    settings = SmsSettings.query.first()
    check(
        "ev_quote_withdrawn persisted as False after admin unchecks it",
        settings is not None and settings.ev_quote_withdrawn is False,
        f"ev_quote_withdrawn={getattr(settings, 'ev_quote_withdrawn', 'MISSING')}",
    )
    check(
        "global SMS still enabled (unrelated toggle not clobbered)",
        settings is not None and settings.sms_globally_enabled is True,
        f"sms_globally_enabled={getattr(settings, 'sms_globally_enabled', 'MISSING')}",
    )

    # Confirm is_sms_enabled now returns False for customer_quote_withdrawn
    from sms_service import is_sms_enabled
    check(
        "is_sms_enabled('customer_quote_withdrawn') returns False when ev_quote_withdrawn=False",
        is_sms_enabled("customer_quote_withdrawn") is False,
        "",
    )

    # Confirm notify function is suppressed end-to-end with real DB state
    send_calls = []
    with patch("sms_service.send_sms", side_effect=lambda *a, **kw: send_calls.append(a) or True):
        from sms_service import notify_customer_quote_withdrawn_sms
        result = notify_customer_quote_withdrawn_sms(
            phone="+16515550002", job_id=99, service_type="Hauling"
        )
    check(
        "notify_customer_quote_withdrawn_sms returns False when ev_quote_withdrawn disabled in DB",
        result is False and len(send_calls) == 0,
        f"result={result!r}, send_calls={send_calls}",
    )

    # Re-enable and confirm it fires again
    settings.ev_quote_withdrawn = True
    db.session.commit()

    send_calls_2 = []
    with patch("sms_service.send_sms", side_effect=lambda *a, **kw: send_calls_2.append(a) or True):
        result2 = notify_customer_quote_withdrawn_sms(
            phone="+16515550002", job_id=99, service_type="Hauling"
        )
    check(
        "notify_customer_quote_withdrawn_sms fires when ev_quote_withdrawn re-enabled in DB",
        result2 is True and len(send_calls_2) == 1,
        f"result={result2!r}, send_calls={send_calls_2}",
    )


# ── Route integration tests: admin_withdraw_quote → SMS ──────────────────────
#
# These tests POST directly to /admin/quote/<id>/withdraw and verify that
# notify_customer_quote_withdrawn_sms is called (or skipped) depending on the
# ev_quote_withdrawn setting.
#
# Both the email notifier (routes.notify_customer_quote_withdrawn) and the SMS
# notifier (routes.notify_customer_quote_withdrawn_sms) are always patched so
# no real SendGrid or Twilio call is ever made.
#
# SMS settings are saved before each test and restored in finally-blocks so
# the test suite is order-independent.
#

import uuid as _uuid
import contextlib as _cl

with app.app_context():
    from models import db, User, Job, Quote, SmsSettings

    # Patch targets — both live in the routes module namespace
    _EMAIL_PATCH = "routes.notify_customer_quote_withdrawn"
    _SMS_PATCH   = "routes.notify_customer_quote_withdrawn_sms"

    # ── Helper: create a disposable customer + job + pending quote ────────────
    def _make_test_fixtures(tag):
        customer = User(
            id=str(_uuid.uuid4()),
            email=f"route_test_{tag}_{_uuid.uuid4().hex[:6]}@example.invalid",
            first_name="Route",
            last_name="Test",
            user_type="customer",
            phone="+16515559900",
            notify_sms=True,
            sms_consent=True,
            age_confirmed=True,
        )
        db.session.add(customer)
        db.session.flush()

        job = Job(
            customer_id=customer.id,
            customer_name="Route Test",
            pickup_address="123 Test St",
            pickup_zip="55401",
            job_description="Test description",
            service_type="Junk Removal",
            status="quoted",
        )
        db.session.add(job)
        db.session.flush()

        quote = Quote(
            job_id=job.id,
            price=150.0,
            deposit_amount=50.0,
            status="pending",
        )
        db.session.add(quote)
        db.session.commit()
        return customer, job, quote

    def _cleanup_fixtures(*objs):
        """Delete test DB rows, ignoring rows that were already removed."""
        db.session.expire_all()
        for obj in objs:
            try:
                merged = db.session.merge(obj)
                db.session.delete(merged)
            except Exception:
                pass
        db.session.commit()

    @_cl.contextmanager
    def _sms_settings_saved():
        """Context manager: save ev_quote_withdrawn + global flag, restore on exit."""
        s = SmsSettings.query.first()
        if not s:
            s = SmsSettings()
            db.session.add(s)
            db.session.commit()
        orig_global = s.sms_globally_enabled
        orig_ev     = s.ev_quote_withdrawn
        try:
            yield s
        finally:
            db.session.expire_all()
            s2 = SmsSettings.query.first()
            if s2:
                s2.sms_globally_enabled = orig_global
                s2.ev_quote_withdrawn   = orig_ev
                db.session.commit()

    admin = User.query.filter_by(is_admin=True).first()
    assert admin, "Route integration tests require an admin user in the dev DB"

    _flu._get_user = lambda: admin
    client = app.test_client()

    # ── Test A: SMS IS called when ev_quote_withdrawn=True ───────────────────
    with _sms_settings_saved() as _s_a:
        _s_a.sms_globally_enabled = True
        _s_a.ev_quote_withdrawn   = True
        db.session.commit()

        _cust_a, _job_a, _quote_a = _make_test_fixtures("a")
        try:
            with patch(_EMAIL_PATCH) as _email_mock_a, \
                 patch(_SMS_PATCH)   as sms_mock_a:
                _email_mock_a.return_value = True
                sms_mock_a.return_value    = True
                resp_a = client.post(
                    f"/admin/quote/{_quote_a.id}/withdraw",
                    data={"withdrawal_note": "Price changed"},
                )

            check(
                "Route: POST /admin/quote/<id>/withdraw redirects (ev_quote_withdrawn=True)",
                resp_a.status_code in (302, 303),
                f"status={resp_a.status_code}",
            )
            check(
                "Route: notify_customer_quote_withdrawn_sms IS called when ev_quote_withdrawn=True",
                sms_mock_a.called,
                f"called={sms_mock_a.called}, call_args={sms_mock_a.call_args}",
            )
            # Verify the phone, job_id, and withdrawal_note are passed correctly
            _sms_kw_a  = sms_mock_a.call_args[1] if sms_mock_a.called else {}
            _sms_pos_a = sms_mock_a.call_args[0] if sms_mock_a.called else ()
            _got_phone  = _sms_kw_a.get("phone")  or (len(_sms_pos_a) > 0 and _sms_pos_a[0])
            _got_job_id = _sms_kw_a.get("job_id") or (len(_sms_pos_a) > 1 and _sms_pos_a[1])
            _got_note   = _sms_kw_a.get("withdrawal_note")
            check(
                "Route: SMS call passes correct phone, job_id and withdrawal_note",
                sms_mock_a.called
                    and _got_phone  == _cust_a.phone
                    and _got_job_id == _job_a.id
                    and _got_note   == "Price changed",
                f"phone={_got_phone!r} (want {_cust_a.phone!r}), "
                f"job_id={_got_job_id!r} (want {_job_a.id!r}), "
                f"note={_got_note!r}",
            )
            # Verify the quote was actually withdrawn in the DB
            db.session.expire_all()
            _qt_a = db.session.get(Quote, _quote_a.id)
            check(
                "Route: quote status set to 'withdrawn' in DB after POST",
                _qt_a is not None and _qt_a.status == "withdrawn",
                f"status={getattr(_qt_a, 'status', 'MISSING')}",
            )
        finally:
            _cleanup_fixtures(_quote_a, _job_a, _cust_a)

    # ── Test B: route still invokes the SMS fn when ev_quote_withdrawn=False ──
    # (The fn itself gate-keeps; unit tests above confirm it returns False then.)
    with _sms_settings_saved() as _s_b:
        _s_b.sms_globally_enabled = True
        _s_b.ev_quote_withdrawn   = False
        db.session.commit()

        _cust_b, _job_b, _quote_b = _make_test_fixtures("b")
        try:
            with patch(_EMAIL_PATCH) as _email_mock_b, \
                 patch(_SMS_PATCH)   as sms_mock_b:
                _email_mock_b.return_value = True
                sms_mock_b.return_value    = False
                resp_b = client.post(
                    f"/admin/quote/{_quote_b.id}/withdraw",
                    data={"withdrawal_note": ""},
                )

            check(
                "Route: POST /admin/quote/<id>/withdraw redirects (ev_quote_withdrawn=False)",
                resp_b.status_code in (302, 303),
                f"status={resp_b.status_code}",
            )
            # The route invokes the fn; the fn's own gate returns False (tested in unit tests above).
            check(
                "Route: notify_customer_quote_withdrawn_sms IS invoked regardless of toggle "
                "(toggle check lives inside the fn, not the route)",
                sms_mock_b.called,
                f"called={sms_mock_b.called}",
            )
        finally:
            _cleanup_fixtures(_quote_b, _job_b, _cust_b)

    # ── Test C: SMS fn NOT called when customer has no phone ─────────────────
    with _sms_settings_saved() as _s_c:
        _s_c.sms_globally_enabled = True
        _s_c.ev_quote_withdrawn   = True
        db.session.commit()

        cust_c = User(
            id=str(_uuid.uuid4()),
            email=f"route_test_c_{_uuid.uuid4().hex[:6]}@example.invalid",
            first_name="No",
            last_name="Phone",
            user_type="customer",
            phone=None,        # no phone → route guard skips SMS fn entirely
            notify_sms=True,
            sms_consent=True,
            age_confirmed=True,
        )
        db.session.add(cust_c)
        db.session.flush()
        job_c = Job(
            customer_id=cust_c.id,
            customer_name="No Phone",
            pickup_address="123 Test St",
            pickup_zip="55401",
            job_description="Test",
            service_type="Junk Removal",
            status="quoted",
        )
        db.session.add(job_c)
        db.session.flush()
        quote_c = Quote(
            job_id=job_c.id,
            price=100.0,
            deposit_amount=30.0,
            status="pending",
        )
        db.session.add(quote_c)
        db.session.commit()

        try:
            with patch(_EMAIL_PATCH) as _email_mock_c, \
                 patch(_SMS_PATCH)   as sms_mock_c:
                _email_mock_c.return_value = True
                resp_c = client.post(f"/admin/quote/{quote_c.id}/withdraw", data={})

            check(
                "Route: notify_customer_quote_withdrawn_sms NOT called when customer has no phone",
                not sms_mock_c.called,
                f"called={sms_mock_c.called}",
            )
        finally:
            _cleanup_fixtures(quote_c, job_c, cust_c)


# ── Summary ───────────────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
