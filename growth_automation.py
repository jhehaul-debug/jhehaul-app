"""growth_automation.py — Phase M scheduled growth reminder thread.

Runs a background thread (interval=3600s) that enqueues GROWTH_REMINDER
jobs for each check type. Jobs execute in the worker queue and never block
web requests.

Safe to start multiple times — subsequent calls are no-ops.
"""
import logging
import threading
import time

_log = logging.getLogger('growth_automation')
_started = False
_lock = threading.Lock()

# Interval between full reminder sweeps (seconds). 1 hour keeps volume low.
_INTERVAL_SECONDS = 3600

# Check types to enqueue on each sweep
_CHECK_TYPES = [
    'unread_message_remind',
    'offer_remind',
    'listing_expiry_remind',
    'relist_remind',
    'seller_insight',
]


def start_growth_automation_thread(app):
    """Start the growth automation thread once. No-op on subsequent calls."""
    global _started
    with _lock:
        if _started:
            return
        _started = True

    def _run():
        _log.info("Growth automation thread started (interval=%ds)", _INTERVAL_SECONDS)
        while True:
            time.sleep(_INTERVAL_SECONDS)
            try:
                with app.app_context():
                    _enqueue_reminders(app)
            except Exception as exc:
                _log.error("Growth automation sweep failed: %s", exc)

    t = threading.Thread(target=_run, name='growth_automation', daemon=True)
    t.start()
    _log.info("Growth automation background thread started (check every %ds)", _INTERVAL_SECONDS)


def _enqueue_reminders(app):
    """Enqueue one GROWTH_REMINDER job per check type."""
    try:
        from worker.queue import enqueue, LOW
        for check_type in _CHECK_TYPES:
            try:
                enqueue('GROWTH_REMINDER', {'check_type': check_type}, priority=LOW)
                _log.debug("Growth automation: enqueued GROWTH_REMINDER check_type=%s", check_type)
            except Exception as exc:
                _log.warning("Growth automation: failed to enqueue %s: %s", check_type, exc)
    except Exception as exc:
        _log.error("Growth automation: queue import failed: %s", exc)
