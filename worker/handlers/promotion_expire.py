"""Phase N — PROMOTION_EXPIRE worker handler.

Payload: { "purchase_id": int }

Actions:
  1. Load MonetizationPurchase; if already expired or not found → no-op.
  2. Mark purchase.status = 'expired'.
  3. Clear listing promotion flags (featured, boost_expires_at, promoted_type).
  4. Listing itself is never deleted.
  5. Write MonetizationAuditLog entry.
"""

from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger("jhe.promotion_expire")


def handle(payload: dict) -> None:
    purchase_id = payload.get("purchase_id")
    if not purchase_id:
        raise ValueError("PROMOTION_EXPIRE: missing purchase_id in payload")

    from app import db
    from models import MonetizationPurchase, MonetizationProduct, Listing
    from monetization_service import record_audit_event

    purchase = MonetizationPurchase.query.get(int(purchase_id))
    if not purchase:
        log.warning("PROMOTION_EXPIRE: purchase %s not found — skipping", purchase_id)
        return

    if purchase.status != "active":
        log.info("PROMOTION_EXPIRE: purchase %s already %s — skipping",
                 purchase_id, purchase.status)
        return

    product = MonetizationProduct.query.get(purchase.product_id)

    # Mark purchase expired
    purchase.status = "expired"
    log.info("PROMOTION_EXPIRE: purchase %s → expired (product_type=%s listing=%s)",
             purchase_id,
             product.product_type if product else "?",
             purchase.listing_id)

    # Clear listing promotion flags — listing itself stays active
    if purchase.listing_id:
        listing = Listing.query.get(purchase.listing_id)
        if listing:
            product_type = product.product_type if product else ""

            if product_type == "featured_listing":
                listing.featured = False
                listing.promoted_type = None
            elif product_type == "boost":
                listing.boost_expires_at = None
                # Only clear promoted_type if it was set to 'boost'
                if listing.promoted_type == "boost":
                    listing.promoted_type = None
            elif product_type == "promoted_listing":
                if listing.promoted_type == "promoted_listing":
                    listing.promoted_type = None
            else:
                # Fallback: clear all promotion flags
                listing.featured = False
                listing.boost_expires_at = None
                listing.promoted_type = None

            log.info("PROMOTION_EXPIRE: cleared promotion flags on listing %s", listing.id)

    db.session.commit()

    record_audit_event(
        "promotion_expired",
        user_id     = purchase.user_id,
        purchase_id = purchase.id,
        product_id  = purchase.product_id,
        listing_id  = purchase.listing_id,
        performed_by= "system",
        detail      = {"product_type": product.product_type if product else "unknown"},
    )
