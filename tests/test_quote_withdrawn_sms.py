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


# ── Summary ───────────────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
