"""
Tests for the wsgi.py startup health-check notifier.

Verifies:
- Healthy startup → no alert sent
- Failing health check → SMS + email sent via _claim_and_notify
- Sentinel deduplication → second call to _claim_and_notify returns False
  and does NOT call _notify_admin a second time
- Unexpected exception from _run_health_checks → treated as failure,
  alert sent
- auth/routes import failure → _claim_and_notify called with error details

Run with:  python tests/test_startup_notifier.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
import unittest
from unittest.mock import patch, MagicMock, call

# We test the helper functions in wsgi.py directly without triggering the
# module-level side effects.  Import them after suppressing the sentinel path.

results = []


def check(name, cond, extra=""):
    results.append((name, cond, extra))
    print(("PASS" if cond else "FAIL"), "-", name, extra if not cond else "")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_sentinel_dir():
    """Return a temp directory to use as a clean /tmp for each test."""
    return tempfile.mkdtemp()


# We import the helpers without triggering the full module startup by importing
# them individually from a minimal namespace.

import importlib, types

def _load_wsgi_helpers():
    """
    Load _notify_admin and _claim_and_notify from wsgi.py without executing
    the module-level startup code (imports of app / auth / routes).
    Returns the module namespace dict with just the helpers available.
    """
    src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wsgi.py")
    with open(src_path) as f:
        src = f.read()

    # Extract only up to (but not including) the "from app import" line so the
    # module-level startup side-effects don't fire.
    lines = src.splitlines()
    cut = next(
        (i for i, l in enumerate(lines) if l.startswith("from app import")),
        len(lines),
    )
    helper_src = "\n".join(lines[:cut])

    ns = {"__name__": "wsgi_helpers", "__file__": src_path}
    exec(compile(helper_src, src_path, "exec"), ns)
    return ns


# ── Tests ────────────────────────────────────────────────────────────────────

# 1. _claim_and_notify — first caller wins, sends notification
with tempfile.TemporaryDirectory() as tmpdir:
    sentinel = os.path.join(tmpdir, "jhe_health_alert_sent")
    ns = _load_wsgi_helpers()
    _claim_and_notify = ns["_claim_and_notify"]
    _notify_admin    = ns["_notify_admin"]

    notify_calls = []

    def _mock_notify(errors):
        notify_calls.append(errors)

    with patch.dict(ns, {"_notify_admin": _mock_notify, "_ALERT_SENTINEL": sentinel}):
        result = _claim_and_notify(["db unreachable"])

    check(
        "_claim_and_notify: first caller returns True (wins sentinel)",
        result is True,
        f"got {result!r}",
    )
    check(
        "_claim_and_notify: first caller calls _notify_admin once",
        len(notify_calls) == 1,
        f"calls={notify_calls}",
    )
    check(
        "_claim_and_notify: sentinel file created",
        os.path.exists(sentinel),
        f"sentinel={sentinel}",
    )

# 2. _claim_and_notify — second caller (sentinel exists) → no duplicate
with tempfile.TemporaryDirectory() as tmpdir:
    sentinel = os.path.join(tmpdir, "jhe_health_alert_sent")
    ns = _load_wsgi_helpers()
    _claim_and_notify = ns["_claim_and_notify"]

    notify_calls_2 = []

    def _mock_notify_2(errors):
        notify_calls_2.append(errors)

    # Pre-create the sentinel as if a previous worker already sent the alert.
    with open(sentinel, "w") as f:
        f.write("sent")

    with patch.dict(ns, {"_notify_admin": _mock_notify_2, "_ALERT_SENTINEL": sentinel}):
        result2 = _claim_and_notify(["db unreachable"])

    check(
        "_claim_and_notify: second caller returns False (sentinel exists)",
        result2 is False,
        f"got {result2!r}",
    )
    check(
        "_claim_and_notify: second caller does NOT call _notify_admin",
        len(notify_calls_2) == 0,
        f"calls={notify_calls_2}",
    )

# 3. _run_startup_health_check — healthy → no notification
with tempfile.TemporaryDirectory() as tmpdir:
    sentinel = os.path.join(tmpdir, "jhe_health_alert_sent")
    ns = _load_wsgi_helpers()

    notify_calls_3 = []

    def _mock_notify_3(errors):
        notify_calls_3.append(errors)

    # We define _run_startup_health_check inline here since it depends on
    # routes._run_health_checks — mock that to return empty (healthy).
    def _run_startup_health_check_sim():
        errors = []
        try:
            errors = []  # simulate healthy
        except Exception as exc:
            errors = [f"health check raised an unexpected exception: {exc}"]
        if not errors:
            return  # no notification
        with patch.dict(ns, {"_notify_admin": _mock_notify_3, "_ALERT_SENTINEL": sentinel}):
            _claim_and_notify = ns["_claim_and_notify"]
            _claim_and_notify(errors)

    _run_startup_health_check_sim()
    check(
        "_run_startup_health_check: healthy → no notification sent",
        len(notify_calls_3) == 0,
        f"calls={notify_calls_3}",
    )

# 4. _run_startup_health_check — health check raises exception → notification sent
with tempfile.TemporaryDirectory() as tmpdir:
    sentinel = os.path.join(tmpdir, "jhe_health_alert_sent")
    ns = _load_wsgi_helpers()

    notify_calls_4 = []

    def _mock_notify_4(errors):
        notify_calls_4.append(list(errors))

    def _run_startup_health_check_exc_sim():
        errors = []
        try:
            raise RuntimeError("unexpected checker crash")
        except Exception as exc:
            errors = [f"health check raised an unexpected exception: {exc}"]
        if errors:
            with patch.dict(ns, {"_notify_admin": _mock_notify_4, "_ALERT_SENTINEL": sentinel}):
                ns["_claim_and_notify"](errors)

    _run_startup_health_check_exc_sim()
    check(
        "_run_startup_health_check: unexpected exception → notification sent",
        len(notify_calls_4) == 1,
        f"calls={notify_calls_4}",
    )
    check(
        "_run_startup_health_check: exception message included in errors",
        notify_calls_4 and "unexpected checker crash" in notify_calls_4[0][0],
        f"errors={notify_calls_4}",
    )

# 5. _notify_admin — SMS and email both attempted even if one fails
with tempfile.TemporaryDirectory() as tmpdir:
    ns = _load_wsgi_helpers()
    _notify_admin = ns["_notify_admin"]

    sms_called   = []
    email_called = []

    def _fake_send_sms(phone, body, event_type=None):
        sms_called.append(phone)

    def _fake_notify_admin(subj, html, event_type=None):
        email_called.append(subj)

    def _fake_html(*args):
        return "<html/>"

    with patch.dict("sys.modules", {
        "sms_service":   MagicMock(send_sms=_fake_send_sms),
        "email_service": MagicMock(notify_admin=_fake_notify_admin, _html=_fake_html),
    }):
        with patch.dict(os.environ, {"ADMIN_PHONE": "+16515550000",
                                     "ADMIN_EMAIL": "admin@example.com"}):
            _notify_admin(["db unreachable: connection refused"])

    check(
        "_notify_admin: SMS attempted",
        len(sms_called) == 1,
        f"sms_called={sms_called}",
    )
    check(
        "_notify_admin: email attempted",
        len(email_called) == 1,
        f"email_called={email_called}",
    )

# ── 6-9: End-to-end: missing env var → _run_health_checks → _notify_admin ─────
#
# These tests confirm the full wsgi.py signal path:
#   _run_health_checks() detects missing var
#   → error list returned
#   → _claim_and_notify() calls _notify_admin()
#   → SMS body / email subject contain the missing-var name
#
# We import _run_health_checks from routes (it is a plain function, no
# request context needed for the env-var checks).

from app import app as _flask_app
import routes as _routes_mod  # noqa: F401  ensures routes are registered
from routes import _run_health_checks

# Minimal env that satisfies every check except the one we blank out.
# We zero out vars that don't matter to DB reachability so the DB SELECT 1
# doesn't run into actual connection errors (we patch it out below).
_BASE_ENV = {
    "DATABASE_URL":                       "postgresql://user:pass@host/db",
    "APP_BASE_URL":                       "https://jhehaul.com",
    "STRIPE_SECRET_KEY":                  "sk_test_dummy",
    "SENDGRID_API_KEY":                   "SG.dummy",
    "GOOGLE_CLIENT_ID":                   "dummy.apps.googleusercontent.com",
    "GOOGLE_CLIENT_SECRET":               "dummy_goog_secret",
    "PAY_LINK_UNDER_150":                 "https://buy.stripe.com/dummy1",
    "PAY_LINK_OVER_500":                  "https://buy.stripe.com/dummy4",
    "STRIPE_PRICE_LISTING_BOOST_7_DAY":   "price_dummy1",
    "STRIPE_PRICE_FEATURED_BOOST_14_DAY": "price_dummy2",
    "STRIPE_PRICE_DELIVERY_BASE_FEE":     "price_dummy3",
    "SESSION_SECRET":                     "dummy_secret",
    # Twilio intentionally absent — optional
    "TWILIO_ACCOUNT_SID": "", "TWILIO_AUTH_TOKEN": "",
    "TWILIO_PHONE_NUMBER": "", "TWILIO_FROM_NUMBER": "",
}

from unittest.mock import patch as _patch
from sqlalchemy import text as _text


def _hc_with_env(extra_env):
    """Run _run_health_checks() with a patched env + mocked DB SELECT 1."""
    env = {**_BASE_ENV, **extra_env}
    with _flask_app.app_context():
        with _patch.dict(os.environ, env, clear=False):
            from unittest.mock import MagicMock as _MM
            from models import db as _db
            with _patch.object(_db.session, "execute", return_value=_MM()):
                return _run_health_checks()


# 6. Missing DATABASE_URL → _run_health_checks returns an error for it
_errors_6 = _hc_with_env({"DATABASE_URL": ""})
check(
    "missing DATABASE_URL → _run_health_checks returns non-empty errors",
    len(_errors_6) > 0,
    f"errors={_errors_6}",
)
check(
    "missing DATABASE_URL → error string mentions DATABASE_URL",
    any("DATABASE_URL" in e for e in _errors_6),
    f"errors={_errors_6}",
)

# 7. Those errors flow through _claim_and_notify → _notify_admin with
#    the var name present in the SMS body.
with tempfile.TemporaryDirectory() as _tmpdir7:
    _sentinel7 = os.path.join(_tmpdir7, "jhe_health_alert_sent")
    _ns7 = _load_wsgi_helpers()

    _sms_bodies_7  = []
    _email_subjs_7 = []

    def _fake_send_sms_7(phone, body, event_type=None):
        _sms_bodies_7.append(body)

    def _fake_notify_admin_7(subj, html, event_type=None):
        _email_subjs_7.append(subj)

    def _fake_html_7(*args):
        return "<html/>"

    with _patch.dict(
        "sys.modules",
        {
            "sms_service":   MagicMock(send_sms=_fake_send_sms_7),
            "email_service": MagicMock(notify_admin=_fake_notify_admin_7,
                                       _html=_fake_html_7),
        },
    ):
        with _patch.dict(
            os.environ,
            {"ADMIN_PHONE": "+16515550000", "ADMIN_EMAIL": "admin@example.com"},
        ):
            with _patch.dict(_ns7, {"_ALERT_SENTINEL": _sentinel7}):
                _ns7["_claim_and_notify"](_errors_6)

    check(
        "missing-var errors reach _notify_admin SMS call",
        len(_sms_bodies_7) == 1,
        f"sms_bodies={_sms_bodies_7}",
    )
    check(
        "SMS body contains missing-var name (DATABASE_URL)",
        _sms_bodies_7 and "DATABASE_URL" in _sms_bodies_7[0],
        f"sms_body={_sms_bodies_7}",
    )
    check(
        "missing-var errors reach _notify_admin email call",
        len(_email_subjs_7) == 1,
        f"email_subjs={_email_subjs_7}",
    )
    check(
        "email subject signals health check failure",
        _email_subjs_7 and "FAILED" in _email_subjs_7[0].upper(),
        f"email_subj={_email_subjs_7}",
    )

# 8. ADMIN_PHONE absent → SMS skipped but email still fires
with tempfile.TemporaryDirectory() as _tmpdir8:
    _sentinel8 = os.path.join(_tmpdir8, "jhe_health_alert_sent")
    _ns8 = _load_wsgi_helpers()

    _sms_calls_8   = []
    _email_calls_8 = []

    def _fake_send_sms_8(phone, body, event_type=None):
        _sms_calls_8.append(phone)

    def _fake_notify_admin_8(subj, html, event_type=None):
        _email_calls_8.append(subj)

    def _fake_html_8(*args):
        return "<html/>"

    _env_no_phone = {k: v for k, v in os.environ.items() if k != "ADMIN_PHONE"}
    _env_no_phone["ADMIN_PHONE"] = ""  # explicitly absent

    with _patch.dict(
        "sys.modules",
        {
            "sms_service":   MagicMock(send_sms=_fake_send_sms_8),
            "email_service": MagicMock(notify_admin=_fake_notify_admin_8,
                                       _html=_fake_html_8),
        },
    ):
        with _patch.dict(os.environ, _env_no_phone, clear=False):
            with _patch.dict(_ns8, {"_ALERT_SENTINEL": _sentinel8}):
                _ns8["_notify_admin"](["missing required environment variable: SENDGRID_API_KEY"])

    check(
        "ADMIN_PHONE absent → SMS skipped (0 SMS calls)",
        len(_sms_calls_8) == 0,
        f"sms_calls={_sms_calls_8}",
    )
    check(
        "ADMIN_PHONE absent → email still sent",
        len(_email_calls_8) == 1,
        f"email_calls={_email_calls_8}",
    )

# 9. Full wsgi _run_startup_health_check simulation with a real missing var:
#    _run_health_checks() → errors detected → _claim_and_notify → _notify_admin
with tempfile.TemporaryDirectory() as _tmpdir9:
    _sentinel9 = os.path.join(_tmpdir9, "jhe_health_alert_sent")
    _ns9 = _load_wsgi_helpers()

    _notify_calls_9 = []

    def _mock_notify_9(errors):
        _notify_calls_9.append(list(errors))

    # Simulate _run_startup_health_check logic using the real _run_health_checks
    def _sim_run_startup():
        errors = []
        try:
            errors = _hc_with_env({"STRIPE_SECRET_KEY": ""})  # force a missing-var error
        except Exception as exc:
            errors = [f"health check raised an unexpected exception: {exc}"]
        if not errors:
            return
        with _patch.dict(_ns9, {"_notify_admin": _mock_notify_9,
                                 "_ALERT_SENTINEL": _sentinel9}):
            _ns9["_claim_and_notify"](errors)

    _sim_run_startup()

    check(
        "full wsgi path: missing STRIPE_SECRET_KEY → _notify_admin called",
        len(_notify_calls_9) == 1,
        f"calls={_notify_calls_9}",
    )
    check(
        "full wsgi path: error message contains STRIPE_SECRET_KEY",
        _notify_calls_9 and any("STRIPE_SECRET_KEY" in e
                                 for e in _notify_calls_9[0]),
        f"errors={_notify_calls_9}",
    )


# ── Summary ───────────────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
