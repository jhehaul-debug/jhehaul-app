"""
test_startup_health_alert.py — confirm that the startup health alert in wsgi.py
reaches the admin (SMS + email) when a health check fails.

Covers:
  - _notify_admin sends SMS via send_sms when ADMIN_PHONE is set
  - _notify_admin sends email via notify_admin (email_service) when health fails
  - _notify_admin skips SMS (logs warning) when ADMIN_PHONE is not set
  - _notify_admin still sends email even if SMS raises an exception
  - _notify_admin still sends SMS even if email raises an exception
  - _claim_and_notify sends alert when sentinel file does not yet exist
  - _claim_and_notify skips duplicate alert when sentinel file already exists
  - _run_startup_health_check calls _claim_and_notify when _run_health_checks returns errors
  - _run_startup_health_check does nothing when _run_health_checks returns no errors
  - _run_startup_health_check wraps an unexpected exception from _run_health_checks

IMPORT-TIME SAFETY
------------------
`import wsgi` executes the module-level startup code (including
_run_startup_health_check) immediately.  In the test environment the health
check can detect a genuinely missing variable (e.g. SENDGRID_API_KEY) and try
to send a real SMS/email before any test mocks are in place.

We prevent this by pre-creating the global sentinel file before the wsgi import
so that _claim_and_notify returns False ("already sent") and the notification
path is never reached.  The sentinel is removed after the import so each test
class starts with a clean slate.
"""

import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock, call

# ── Bootstrap the Flask app and routes before anything else ───────────────────
from app import app, db
import routes  # noqa: F401  registers all URL rules

# ── Safe wsgi import ──────────────────────────────────────────────────────────
# Pre-create the sentinel so the module-level _run_startup_health_check does not
# fire real notifications regardless of the environment's health-check result.
_GLOBAL_SENTINEL = "/tmp/jhe_health_alert_sent"
_sentinel_created_by_us = False
if not os.path.exists(_GLOBAL_SENTINEL):
    try:
        open(_GLOBAL_SENTINEL, "w").close()
        _sentinel_created_by_us = True
    except OSError:
        pass

import wsgi as _wsgi  # noqa: E402  (module-level startup now blocked by sentinel)

# Remove the sentinel only if we created it so individual tests start clean.
if _sentinel_created_by_us:
    try:
        os.remove(_GLOBAL_SENTINEL)
    except OSError:
        pass


# ── Test classes ──────────────────────────────────────────────────────────────

class TestNotifyAdmin(unittest.TestCase):
    """_notify_admin sends SMS + email; each channel is independent."""

    FAKE_ERRORS = ["missing package foo: No module named 'foo'",
                   "missing required environment variable: DATABASE_URL"]

    def _call(self, env_overrides=None):
        """Call _notify_admin with FAKE_ERRORS inside a patched environment."""
        env = {**os.environ, "ADMIN_PHONE": "+16515550001"}
        if env_overrides:
            env.update(env_overrides)
        with patch.dict(os.environ, env, clear=True):
            _wsgi._notify_admin(self.FAKE_ERRORS)

    # ── SMS ────────────────────────────────────────────────────────────────────

    def test_sms_sent_to_admin_phone(self):
        """send_sms is called with ADMIN_PHONE and a message containing the errors."""
        with patch("sms_service.send_sms") as mock_sms, \
             patch("email_service.notify_admin"), \
             patch("email_service._html", return_value="<html/>"):
            self._call({"ADMIN_PHONE": "+16515550001"})

        mock_sms.assert_called_once()
        args = mock_sms.call_args[0]
        phone_arg = args[0]
        msg_arg = args[1]
        self.assertEqual(phone_arg, "+16515550001")
        self.assertIn("FAILED", msg_arg)
        self.assertIn("missing package foo", msg_arg)

    def test_sms_event_type_is_admin_health_alert(self):
        """send_sms is called with event_type='admin_health_alert'."""
        with patch("sms_service.send_sms") as mock_sms, \
             patch("email_service.notify_admin"), \
             patch("email_service._html", return_value="<html/>"):
            self._call({"ADMIN_PHONE": "+16515550001"})

        positional = mock_sms.call_args[0]
        # send_sms(to_phone, message, event_type)
        event_arg = positional[2] if len(positional) > 2 else mock_sms.call_args[1].get("event_type")
        self.assertEqual(event_arg, "admin_health_alert")

    def test_sms_skipped_when_admin_phone_not_set(self):
        """send_sms is NOT called when ADMIN_PHONE is absent; a warning is logged instead."""
        env_no_phone = {k: v for k, v in os.environ.items() if k != "ADMIN_PHONE"}
        with patch.dict(os.environ, env_no_phone, clear=True), \
             patch("sms_service.send_sms") as mock_sms, \
             patch("email_service.notify_admin"), \
             patch("email_service._html", return_value="<html/>"), \
             patch("logging.warning") as mock_warn:
            _wsgi._notify_admin(self.FAKE_ERRORS)

        mock_sms.assert_not_called()
        warning_msgs = " ".join(
            str(a) for c in mock_warn.call_args_list for a in c[0]
        )
        self.assertIn("ADMIN_PHONE", warning_msgs)

    # ── Email ──────────────────────────────────────────────────────────────────

    def test_email_sent_on_failure(self):
        """notify_admin (email) is called with the right subject and event_type."""
        with patch("sms_service.send_sms"), \
             patch("email_service.notify_admin") as mock_email, \
             patch("email_service._html", return_value="<html/>"):
            self._call()

        mock_email.assert_called_once()
        args = mock_email.call_args[0]
        kwargs = mock_email.call_args[1]
        subject_arg = args[0]
        event_arg = kwargs.get("event_type") or (args[2] if len(args) > 2 else None)
        self.assertIn("FAILED", subject_arg)
        self.assertEqual(event_arg, "admin_health_alert")

    def test_email_body_contains_all_errors(self):
        """The HTML body passed to notify_admin contains every error string."""
        captured_html = {}

        def fake_html(header_title, header_sub, tag, body_html):
            captured_html["body"] = body_html
            return f"<html>{body_html}</html>"

        with patch("sms_service.send_sms"), \
             patch("email_service.notify_admin"), \
             patch("email_service._html", side_effect=fake_html):
            self._call()

        body = captured_html.get("body", "")
        for err in self.FAKE_ERRORS:
            self.assertIn(err, body,
                          f"Error message not found in email body: {err!r}")

    # ── Channel independence ───────────────────────────────────────────────────

    def test_email_sent_even_if_sms_raises(self):
        """If send_sms raises, email is still attempted."""
        with patch("sms_service.send_sms", side_effect=RuntimeError("twilio down")), \
             patch("email_service.notify_admin") as mock_email, \
             patch("email_service._html", return_value="<html/>"):
            # Must not propagate the exception
            _wsgi._notify_admin(self.FAKE_ERRORS)

        mock_email.assert_called_once()

    def test_sms_sent_even_if_email_raises(self):
        """If notify_admin raises, SMS was still attempted beforehand."""
        with patch("sms_service.send_sms") as mock_sms, \
             patch("email_service.notify_admin", side_effect=RuntimeError("sg down")), \
             patch("email_service._html", return_value="<html/>"):
            # Must not propagate the exception
            self._call()

        mock_sms.assert_called_once()


class TestClaimAndNotify(unittest.TestCase):
    """_claim_and_notify uses an O_EXCL sentinel to fire at most one alert per deploy."""

    FAKE_ERRORS = ["database unreachable: connection refused"]

    def test_first_caller_sends_alert(self):
        """When the sentinel does not exist, _notify_admin is called and True is returned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sentinel = os.path.join(tmpdir, "jhe_health_alert_sent")
            with patch.object(_wsgi, "_ALERT_SENTINEL", sentinel), \
                 patch.object(_wsgi, "_notify_admin") as mock_notify:
                result = _wsgi._claim_and_notify(self.FAKE_ERRORS)

        self.assertTrue(result)
        mock_notify.assert_called_once_with(self.FAKE_ERRORS)

    def test_second_caller_skips_alert(self):
        """When the sentinel already exists, _notify_admin is NOT called and False is returned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sentinel = os.path.join(tmpdir, "jhe_health_alert_sent")
            # Simulate a previous worker having already claimed the alert slot.
            open(sentinel, "w").close()
            with patch.object(_wsgi, "_ALERT_SENTINEL", sentinel), \
                 patch.object(_wsgi, "_notify_admin") as mock_notify:
                result = _wsgi._claim_and_notify(self.FAKE_ERRORS)

        self.assertFalse(result)
        mock_notify.assert_not_called()

    def test_sentinel_file_is_created(self):
        """After _claim_and_notify, the sentinel file exists on disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sentinel = os.path.join(tmpdir, "jhe_health_alert_sent")
            with patch.object(_wsgi, "_ALERT_SENTINEL", sentinel), \
                 patch.object(_wsgi, "_notify_admin"):
                _wsgi._claim_and_notify(self.FAKE_ERRORS)

            self.assertTrue(os.path.exists(sentinel))

    def test_concurrent_workers_notify_exactly_once(self):
        """N threads racing to call _claim_and_notify invoke _notify_admin exactly once.

        This exercises the O_CREAT|O_EXCL atomic-claim guarantee under real
        concurrency — multiple threads hit the sentinel open at the same instant,
        only one should win the race and fire the notification.
        """
        NUM_WORKERS = 16

        notify_call_count = []
        count_lock = threading.Lock()

        def counting_notify(errors):
            with count_lock:
                notify_call_count.append(1)

        barrier = threading.Barrier(NUM_WORKERS)
        results = []
        results_lock = threading.Lock()

        def worker(sentinel):
            barrier.wait()  # all threads released simultaneously
            result = _wsgi._claim_and_notify(self.FAKE_ERRORS)
            with results_lock:
                results.append(result)

        with tempfile.TemporaryDirectory() as tmpdir:
            sentinel = os.path.join(tmpdir, "jhe_health_alert_sent")
            with patch.object(_wsgi, "_ALERT_SENTINEL", sentinel), \
                 patch.object(_wsgi, "_notify_admin", side_effect=counting_notify):

                threads = [
                    threading.Thread(target=worker, args=(sentinel,))
                    for _ in range(NUM_WORKERS)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

        # Exactly one worker should have claimed the slot and fired the alert.
        self.assertEqual(
            sum(notify_call_count), 1,
            f"_notify_admin was called {sum(notify_call_count)} times; expected exactly 1"
        )
        # Exactly one worker returns True; all others return False.
        self.assertEqual(
            results.count(True), 1,
            f"Expected exactly 1 True result from _claim_and_notify, got {results.count(True)}"
        )
        self.assertEqual(
            results.count(False), NUM_WORKERS - 1,
            f"Expected {NUM_WORKERS - 1} False results, got {results.count(False)}"
        )


class TestRunStartupHealthCheck(unittest.TestCase):
    """_run_startup_health_check calls _claim_and_notify only when errors exist."""

    def test_alert_fired_when_health_checks_return_errors(self):
        """_claim_and_notify is invoked when _run_health_checks returns a non-empty list."""
        fake_errors = ["missing package pgeocode: No module named 'pgeocode'"]
        with app.app_context():
            with patch("routes._run_health_checks", return_value=fake_errors), \
                 patch.object(_wsgi, "_claim_and_notify") as mock_claim:
                _wsgi._run_startup_health_check()

        mock_claim.assert_called_once_with(fake_errors)

    def test_no_alert_when_health_checks_pass(self):
        """_claim_and_notify is NOT invoked when _run_health_checks returns an empty list."""
        with app.app_context():
            with patch("routes._run_health_checks", return_value=[]), \
                 patch.object(_wsgi, "_claim_and_notify") as mock_claim:
                _wsgi._run_startup_health_check()

        mock_claim.assert_not_called()

    def test_alert_fired_when_health_checks_raise(self):
        """If _run_health_checks itself raises, an error is collected and _claim_and_notify fires."""
        with app.app_context():
            with patch("routes._run_health_checks",
                       side_effect=RuntimeError("unexpected crash")), \
                 patch.object(_wsgi, "_claim_and_notify") as mock_claim:
                _wsgi._run_startup_health_check()

        mock_claim.assert_called_once()
        errors_passed = mock_claim.call_args[0][0]
        self.assertTrue(any("unexpected crash" in e for e in errors_passed))

    def test_error_list_passed_to_claim_matches_health_check_output(self):
        """The exact error list from _run_health_checks reaches _claim_and_notify."""
        fake_errors = [
            "missing required environment variable: DATABASE_URL",
            "database unreachable: FATAL: password authentication failed",
        ]
        with app.app_context():
            with patch("routes._run_health_checks", return_value=fake_errors), \
                 patch.object(_wsgi, "_claim_and_notify") as mock_claim:
                _wsgi._run_startup_health_check()

        mock_claim.assert_called_once_with(fake_errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
