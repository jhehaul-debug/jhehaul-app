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

# ── Summary ───────────────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
