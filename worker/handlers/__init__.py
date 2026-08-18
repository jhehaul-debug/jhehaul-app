"""Handler registry for Phase F background worker.

Each handler is a callable that accepts a payload dict and raises on failure.
The runner catches exceptions and calls queue.fail() with the error details.

To add a new job type:
  1. Create worker/handlers/<name>.py with a handle(payload) function.
  2. Import it here and add an entry to _REGISTRY.
"""

from worker.handlers.email_notification import handle as _email_handle
from worker.handlers.saved_search_match import handle as _ssm_handle
from worker.handlers.fraud_scan         import handle as _fraud_handle
from worker.handlers.analytics_event    import handle as _analytics_handle

# ── Job type → handler function ───────────────────────────────────────────────
# None = job type is registered but not yet implemented (will raise ValueError)
_REGISTRY = {
    # ── Email ────────────────────────────────────────────────────────────────
    'EMAIL_NOTIFICATION':    _email_handle,

    # ── Marketplace ─────────────────────────────────────────────────────────
    'SAVED_SEARCH_MATCH':    _ssm_handle,
    'LISTING_EXPIRATION':    None,  # handled by existing expiry thread; stub only

    # ── AI (interactive AI remains synchronous; these are for future batch use)
    'AI_LISTING_TITLE':       None,
    'AI_LISTING_DESCRIPTION': None,
    'AI_CATEGORY_SUGGESTION': None,
    'AI_SEARCH_PROCESSING':   None,

    # ── Safety ──────────────────────────────────────────────────────────────
    'FRAUD_SCAN':            _fraud_handle,

    # ── Analytics / enrichment ───────────────────────────────────────────────
    'ANALYTICS_EVENT':       _analytics_handle,

    # ── Future ──────────────────────────────────────────────────────────────
    'FUTURE_IMAGE_ANALYSIS': None,
    'MARKETPLACE_NOTIFICATION': None,
}


def get_handler(job_type):
    """Return the handler callable for a job type, or raise ValueError.

    Returns None for stub job types that are registered but not yet implemented.
    The runner should log a warning and call fail() for unimplemented stubs.
    """
    if job_type not in _REGISTRY:
        raise ValueError(f"Unknown job_type={job_type!r}. Register it in worker/handlers/__init__.py")
    handler = _REGISTRY[job_type]
    if handler is None:
        raise NotImplementedError(
            f"Job type {job_type!r} is registered but has no handler yet. "
            "Implement worker/handlers/<name>.py and register it."
        )
    return handler


def registered_types():
    """Return sorted list of all registered job type names."""
    return sorted(_REGISTRY.keys())
