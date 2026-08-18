"""FRAUD_SCAN job handler — Phase J implementation.

Payload schema:
    {
        "listing_id": <int>,    # optional — analyze a specific listing
        "user_id":    "<str>",  # optional — analyze account activity
        "trigger":    "<str>"   # e.g. "new_listing", "report_threshold", "new_user"
    }

This handler:
  1. Fetches listing/user from DB by ID (never trusts payload values for auth).
  2. Delegates to ai.fraud_safety.calculate_risk_and_flag().
  3. Creates a FraudFlag DB record for MEDIUM+ risk.
  4. Logs outcome. Never exposes PII or raw AI output in logs.
  5. NEVER permanently bans users or permanently removes listings automatically.
"""

import logging

log = logging.getLogger('jhe.worker.fraud_scan')


def handle(payload: dict):
    """Fraud/safety scan — Phase J full implementation."""
    listing_id = payload.get('listing_id')
    user_id    = payload.get('user_id')
    trigger    = payload.get('trigger', 'auto')

    if not listing_id and not user_id:
        log.warning("fraud_scan.handle: payload has neither listing_id nor user_id — skipping")
        return

    log.info("fraud_scan.handle: trigger=%s listing_id=%s user_id=%s", trigger, listing_id, user_id)

    from ai.fraud_safety import calculate_risk_and_flag

    result = calculate_risk_and_flag(
        listing_id=int(listing_id) if listing_id else None,
        user_id=str(user_id)       if user_id    else None,
        trigger=trigger,
    )

    if 'error' in result:
        log.error("fraud_scan error: %s", result['error'])
        raise RuntimeError(f"fraud_scan failed: {result['error']}")

    risk  = result.get('risk_level', 'LOW')
    sigs  = len(result.get('signals', []))
    flagged = 'flag_id' in result

    log.info(
        "fraud_scan.complete: trigger=%s listing=%s user=%s risk=%s signals=%d flagged=%s flag_id=%s",
        trigger, listing_id, user_id, risk, sigs, flagged, result.get('flag_id'),
    )
