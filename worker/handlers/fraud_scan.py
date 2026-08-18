"""FRAUD_SCAN job handler — Phase F stub.

Payload schema:
    {
        "listing_id": "<listing id>",   # optional
        "user_id":    "<user id>",      # optional
        "trigger":    "<event name>"    # e.g. "new_listing", "new_user"
    }

Phase F: infrastructure registered, logic deferred to a future phase.
When implemented, this handler should:
  1. Fetch the listing/user by ID (never from payload directly — always DB).
  2. Apply heuristic checks (duplicate photos, price anomalies, phone patterns).
  3. Create a ListingReport or flag the user for admin review.
  4. Never expose raw AI responses or PII in logs.
"""

import logging

log = logging.getLogger('jhe.worker.fraud_scan')


def handle(payload):
    """Fraud/safety scan — stub implementation."""
    listing_id = payload.get('listing_id')
    user_id    = payload.get('user_id')
    trigger    = payload.get('trigger', 'unknown')

    log.info(
        "fraud_scan.handle: trigger=%s listing_id=%s user_id=%s — "
        "stub only; no action taken in Phase F",
        trigger, listing_id, user_id,
    )
    # Phase F: no-op. Implement heuristic checks in a future phase.
