"""ANALYTICS_EVENT job handler — Phase F stub.

Moves non-critical analytics writes off the web request path.

Payload schema:
    {
        "event":      "<event name>",       # e.g. "listing_view", "search_executed"
        "listing_id": "<listing id>",       # optional
        "user_id":    "<user id>",          # optional
        "meta":       { ... safe scalars }  # optional, non-PII metadata
    }

Phase F: infrastructure registered, logic deferred to a future phase.
When implemented, this handler should:
  1. Write to an analytics_events table or send to an analytics service.
  2. Never log raw PII (email addresses, phone numbers, real names).
  3. Use aggregate/anonymized identifiers where possible.

Important: do NOT move business-critical transaction data here.
Offer acceptance, payment confirmation, and delivery status must
remain synchronous and in the primary database.
"""

import logging

log = logging.getLogger('jhe.worker.analytics_event')


def handle(payload):
    """Analytics event write — stub implementation."""
    event      = payload.get('event', 'unknown')
    listing_id = payload.get('listing_id')
    user_id    = payload.get('user_id')

    log.info(
        "analytics_event.handle: event=%s listing_id=%s user_id=%s — "
        "stub only; no action taken in Phase F",
        event, listing_id, user_id,
    )
    # Phase F: no-op. Implement analytics persistence in a future phase.
