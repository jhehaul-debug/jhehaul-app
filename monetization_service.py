"""JHE Haul — Phase N Monetization Service.

Centralized entitlement and product registry. All products default to
is_active=False and NO live charges are activated. Pricing, plan activation,
and production billing are gated behind explicit admin approval.

Public API
----------
has_feature(user, feature, listing_id=None)  → bool  (server-side entitlement)
get_seller_plan_info(user)                   → dict
get_active_products(product_type=None)       → list[MonetizationProduct]
can_promote_listing(listing, user)           → (bool, reason_str)
record_audit_event(event_type, **kwargs)     → None
get_delivery_revenue_summary(days)           → dict
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger("jhe.monetization")

# ---------------------------------------------------------------------------
# Plan constants
# ---------------------------------------------------------------------------

PLAN_FREE     = "free"
PLAN_BUSINESS = "business"
PLAN_DEALER   = "dealer"

# Higher number = higher plan tier
PLAN_HIERARCHY: dict[str, int] = {
    PLAN_FREE:     0,
    PLAN_BUSINESS: 1,
    PLAN_DEALER:   2,
}

# ---------------------------------------------------------------------------
# Feature map: feature_name → (minimum_plan, product_type_for_purchase_check)
#
# product_type=None  means the feature is plan-level only (no per-purchase check).
# product_type=str   means the feature also requires an active MonetizationPurchase
#                    of that product_type (for listing-level or one-off purchases).
# ---------------------------------------------------------------------------

_FEATURE_MAP: dict[str, tuple[str, Optional[str]]] = {
    # ── Listing-level (purchasable by any plan, product_type check required) ──
    "featured_listing":       (PLAN_FREE,     "featured_listing"),
    "boost_listing":          (PLAN_FREE,     "boost"),
    "promoted_listing":       (PLAN_FREE,     "promoted_listing"),

    # ── Business plan features ────────────────────────────────────────────────
    "storefront":             (PLAN_BUSINESS, None),
    "business_profile":       (PLAN_BUSINESS, None),
    "advanced_analytics":     (PLAN_BUSINESS, None),
    "ai_seller_tools":        (PLAN_BUSINESS, None),
    "bulk_listing":           (PLAN_BUSINESS, None),
    "priority_support":       (PLAN_BUSINESS, None),
    "more_active_listings":   (PLAN_BUSINESS, None),

    # ── Dealer plan features ──────────────────────────────────────────────────
    "dealer_profile":         (PLAN_DEALER,   None),
    "vehicle_inventory":      (PLAN_DEALER,   None),
    "ai_inventory_assistant": (PLAN_DEALER,   None),
    "dealership_storefront":  (PLAN_DEALER,   None),
    "lead_management":        (PLAN_DEALER,   None),
    "bulk_vehicle_management":(PLAN_DEALER,   None),

    # ── Future buyer premium features (architecture only, not activated) ───────
    "advanced_saved_searches": (PLAN_FREE,    "buyer_premium"),
    "priority_alerts":         (PLAN_FREE,    "buyer_premium"),
    "advanced_buyer_copilot":  (PLAN_FREE,    "buyer_premium"),
}


def has_feature(user, feature: str, listing_id: int = None) -> bool:
    """Server-side entitlement check. Never trust frontend assertions.

    Plan-level features: checks user.seller_plan against PLAN_HIERARCHY.
    Purchase-level features: also requires an active MonetizationPurchase
    of the matching product_type (optionally scoped to listing_id).

    Returns False for any unknown feature or on DB error (fail-closed).
    """
    if feature not in _FEATURE_MAP:
        return False

    required_plan, product_type = _FEATURE_MAP[feature]

    # ── Plan-level check ─────────────────────────────────────────────────────
    plan           = getattr(user, "seller_plan", PLAN_FREE) or PLAN_FREE
    plan_expires   = getattr(user, "seller_plan_expires_at", None)
    plan_rank      = PLAN_HIERARCHY.get(plan, 0)
    required_rank  = PLAN_HIERARCHY.get(required_plan, 0)

    # If plan has an expiry and it has passed, treat as FREE
    if plan_expires and plan_expires < datetime.now():
        plan_rank = 0

    plan_ok = plan_rank >= required_rank

    # Plan-level only (no per-purchase check needed)
    if product_type is None:
        return plan_ok

    # Purchase-level: free tier can still buy individual promotions
    if required_rank > 0 and not plan_ok:
        return False

    # ── Purchase-level check ─────────────────────────────────────────────────
    try:
        from app import db
        from models import MonetizationPurchase, MonetizationProduct

        now = datetime.now()
        q = (
            MonetizationPurchase.query
            .join(MonetizationProduct,
                  MonetizationPurchase.product_id == MonetizationProduct.id)
            .filter(
                MonetizationPurchase.user_id == user.id,
                MonetizationPurchase.status  == "active",
                MonetizationProduct.product_type == product_type,
                db.or_(
                    MonetizationPurchase.expires_at == None,
                    MonetizationPurchase.expires_at > now,
                ),
            )
        )
        if listing_id:
            q = q.filter(
                db.or_(
                    MonetizationPurchase.listing_id == listing_id,
                    MonetizationPurchase.listing_id == None,
                )
            )
        return q.first() is not None
    except Exception as exc:
        log.warning("has_feature DB error (feature=%s): %s", feature, exc)
        return False


def get_seller_plan_info(user) -> dict:
    """Return a safe, serialisable summary of the seller's current plan."""
    plan         = getattr(user, "seller_plan", PLAN_FREE) or PLAN_FREE
    plan_expires = getattr(user, "seller_plan_expires_at", None)

    # Treat as free if expired
    if plan_expires and plan_expires < datetime.now():
        plan = PLAN_FREE

    plan_rank = PLAN_HIERARCHY.get(plan, 0)

    features_available = [
        feat for feat, (req_plan, _) in _FEATURE_MAP.items()
        if PLAN_HIERARCHY.get(req_plan, 0) <= plan_rank
        and _ is None  # plan-level only; purchase features excluded from this list
    ]

    return {
        "plan":           plan,
        "plan_rank":      plan_rank,
        "plan_expires_at": plan_expires.isoformat() if plan_expires else None,
        "is_business":    plan_rank >= PLAN_HIERARCHY[PLAN_BUSINESS],
        "is_dealer":      plan_rank >= PLAN_HIERARCHY[PLAN_DEALER],
        "plan_features":  features_available,
    }


def get_active_products(product_type: str = None):
    """Return active MonetizationProduct records, optionally filtered by type.

    Returns [] if the table doesn't exist yet or on any error.
    """
    try:
        from models import MonetizationProduct
        q = MonetizationProduct.query.filter_by(is_active=True)
        if product_type:
            q = q.filter_by(product_type=product_type)
        return q.order_by(MonetizationProduct.display_order).all()
    except Exception as exc:
        log.warning("get_active_products error: %s", exc)
        return []


def can_promote_listing(listing, user) -> tuple[bool, str]:
    """Check whether a listing is eligible for paid promotion.

    Returns (True, "") on success or (False, reason) when blocked.
    Integrates Phase J safety status checks.
    """
    if listing is None:
        return False, "Listing not found."

    # Ownership check
    if str(listing.seller_id) != str(user.id):
        return False, "You can only promote your own listings."

    # Status checks (Phase J safety integration)
    ineligible_statuses = {"sold", "removed", "expired", "reserved"}
    if (listing.status or "").lower() in ineligible_statuses:
        return False, f"Listings with status '{listing.status}' cannot be promoted."

    # Fraud / suspension check
    if getattr(user, "is_banned", False) or getattr(user, "is_suspended", False):
        return False, "Your account is not eligible for paid promotion."

    fraud_score = getattr(user, "fraud_score", 0) or 0
    if fraud_score >= 80:
        return False, "Your account is not currently eligible for paid promotion."

    # Private listing check (if applicable)
    if getattr(listing, "is_private", False):
        return False, "Private listings cannot be publicly promoted."

    return True, ""


def record_audit_event(
    event_type: str,
    user_id: str = None,
    purchase_id: int = None,
    product_id: int = None,
    listing_id: int = None,
    performed_by: str = "system",
    detail: dict = None,
) -> None:
    """Write a monetization audit log entry.

    Never logs payment secrets, card data, or auth tokens.
    """
    try:
        from app import db
        from models import MonetizationAuditLog

        entry = MonetizationAuditLog(
            event_type   = event_type,
            user_id      = user_id,
            purchase_id  = purchase_id,
            product_id   = product_id,
            listing_id   = listing_id,
            performed_by = performed_by,
            detail_json  = json.dumps(detail or {}),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as exc:
        log.error("record_audit_event failed (event=%s): %s", event_type, exc)


def get_delivery_revenue_summary(days: int = 30) -> dict:
    """Summarise delivery request revenue for admin analytics.

    Counts requests, accepted, completed, and sums quote amounts.
    Returns $0 totals if no revenue data exists — never mixes estimated
    with collected revenue.
    """
    try:
        from app import db
        from models import DeliveryRequest
        from sqlalchemy import func
        from datetime import timedelta

        since = datetime.now() - timedelta(days=days)

        # All counts and revenue exclude is_test=True records
        total   = DeliveryRequest.query.filter(
            DeliveryRequest.created_at >= since,
            DeliveryRequest.is_test == False,
        ).count()
        pending = DeliveryRequest.query.filter(
            DeliveryRequest.created_at >= since,
            DeliveryRequest.status.in_(["pending", "quoted"]),
            DeliveryRequest.is_test == False,
        ).count()
        accepted = DeliveryRequest.query.filter(
            DeliveryRequest.created_at >= since,
            DeliveryRequest.status == "accepted",
            DeliveryRequest.is_test == False,
        ).count()
        completed = DeliveryRequest.query.filter(
            DeliveryRequest.created_at >= since,
            DeliveryRequest.status == "completed",
            DeliveryRequest.is_test == False,
        ).count()

        # Sum accepted quote amounts — collected revenue only, test quotes excluded
        revenue_row = (
            db.session.query(func.sum(DeliveryRequest.quote_amount))
            .filter(
                DeliveryRequest.created_at >= since,
                DeliveryRequest.status.in_(["accepted", "completed"]),
                DeliveryRequest.quote_amount != None,
                DeliveryRequest.is_test == False,
            )
            .scalar()
        )
        revenue_cents = int((revenue_row or 0) * 100)  # convert dollars → cents

        return {
            "period_days":        days,
            "total_requests":     total,
            "pending":            pending,
            "accepted":           accepted,
            "completed":          completed,
            "collected_cents":    revenue_cents,
            "collected_dollars":  round(revenue_cents / 100, 2),
            "revenue_type":       "collected",  # never estimated
            "note": "Delivery revenue from accepted/completed quotes only.",
        }
    except Exception as exc:
        log.error("get_delivery_revenue_summary error: %s", exc)
        return {
            "period_days": days, "total_requests": 0, "pending": 0,
            "accepted": 0, "completed": 0, "collected_cents": 0,
            "collected_dollars": 0.0, "revenue_type": "collected",
            "error": str(exc),
        }


def enqueue_promotion_expire(purchase_id: int, expires_at: datetime) -> None:
    """Enqueue a PROMOTION_EXPIRE job so the worker cleans up when the promotion ends."""
    try:
        from worker.queue import enqueue
        enqueue(
            job_type="PROMOTION_EXPIRE",
            payload={"purchase_id": purchase_id},
            priority=3,  # LOW — cleanup task
            idempotency_key=f"promo_expire:{purchase_id}",
            run_after=expires_at,
        )
    except Exception as exc:
        log.warning("enqueue_promotion_expire failed (purchase=%s): %s", purchase_id, exc)
