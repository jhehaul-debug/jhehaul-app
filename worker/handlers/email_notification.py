"""EMAIL_NOTIFICATION job handler.

Payload schema (all fields are plain strings — no secrets):
    {
        "fn":     "<email_service function name>",
        "kwargs": { ... function-specific keyword arguments ... }
    }

Only functions listed in _ALLOWED_FUNCTIONS can be dispatched.
This prevents payload injection from calling arbitrary Python.

Idempotency: callers should pass an idempotency_key when the same
email must not be sent twice (e.g. offer-accepted notification).
The queue layer suppresses duplicate QUEUED/PROCESSING jobs automatically.
"""

import logging
from email_service import (
    notify_seller_new_message,
    notify_buyer_offer_expired,
    notify_buyer_offer_timed_out,
    notify_buyer_listing_pending,
    notify_buyer_delivery_quote_ready,
    # Phase M: Growth Automation emails
    notify_price_drop_alert,
    notify_seller_pending_offers_reminder,
    notify_relist_reminder_email,
    notify_seller_insight_email,
    notify_seller_listing_expiring_soon,
)

log = logging.getLogger('jhe.worker.email')

# ── Safe dispatch table (add functions here as email calls are migrated) ───────
_ALLOWED_FUNCTIONS = {
    # Existing
    'notify_seller_new_message':           notify_seller_new_message,
    'notify_buyer_offer_expired':          notify_buyer_offer_expired,
    'notify_buyer_offer_timed_out':        notify_buyer_offer_timed_out,
    'notify_buyer_listing_pending':        notify_buyer_listing_pending,
    'notify_buyer_delivery_quote_ready':   notify_buyer_delivery_quote_ready,
    # Phase M Growth Automation
    'notify_price_drop_alert':             notify_price_drop_alert,
    'notify_seller_pending_offers_reminder': notify_seller_pending_offers_reminder,
    'notify_relist_reminder_email':        notify_relist_reminder_email,
    'notify_seller_insight_email':         notify_seller_insight_email,
    'notify_seller_listing_expiring_soon': notify_seller_listing_expiring_soon,
}


def handle(payload):
    """Dispatch an EMAIL_NOTIFICATION job to the correct email_service function.

    Raises ValueError for unknown/disallowed function names.
    Raises any exception thrown by the underlying email_service function,
    so the runner can retry on transient SendGrid/network errors.
    """
    fn_name = payload.get('fn', '')
    kwargs  = payload.get('kwargs', {})

    if not fn_name:
        raise ValueError("EMAIL_NOTIFICATION payload missing 'fn' field")

    fn = _ALLOWED_FUNCTIONS.get(fn_name)
    if fn is None:
        raise ValueError(
            f"EMAIL_NOTIFICATION: disallowed or unknown function {fn_name!r}. "
            "Add it to worker/handlers/email_notification._ALLOWED_FUNCTIONS."
        )

    log.info("email_notification.handle: calling %s kwargs_keys=%s",
             fn_name, sorted(kwargs.keys()))

    fn(**kwargs)

    log.info("email_notification.handle: sent fn=%s", fn_name)
