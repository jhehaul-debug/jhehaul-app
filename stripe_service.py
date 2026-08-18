"""JHE Haul — Phase N Stripe Service (TEST MODE ONLY).

All Stripe operations use the configured STRIPE_SECRET_KEY. If that key
starts with 'sk_live_' this module will refuse to process purchases —
live billing is NOT activated in Phase N.

Responsibilities:
- Stripe Checkout session creation (TEST MODE)
- Webhook signature verification
- Purchase activation on successful checkout
- Subscription lifecycle (created / updated / cancelled)

STRIPE MODE: TEST  (enforced at runtime — see get_stripe_mode())
NO LIVE CHARGES.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("jhe.stripe_service")

# ---------------------------------------------------------------------------
# Mode guard — MUST remain TEST in Phase N
# ---------------------------------------------------------------------------

def get_stripe_mode() -> str:
    """Return 'test' or 'live' based on the current Stripe secret key prefix.

    Phase N policy: if the key is a live key, monetization checkout is blocked.
    """
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if key.startswith("sk_live_"):
        return "live"
    return "test"


def _assert_test_mode() -> None:
    """Raise RuntimeError if live Stripe keys are detected.

    This prevents accidental real charges during Phase N architecture work.
    """
    if get_stripe_mode() == "live":
        raise RuntimeError(
            "Phase N monetization is in TEST MODE ONLY. "
            "Live Stripe billing is not activated. "
            "Do not switch to sk_live_ keys without explicit admin approval."
        )


# ---------------------------------------------------------------------------
# Checkout Session
# ---------------------------------------------------------------------------

def create_checkout_session(
    user,
    product,          # MonetizationProduct instance
    listing_id: int = None,
    success_url: str = "",
    cancel_url:  str = "",
) -> Optional[object]:
    """Create a Stripe Checkout Session for a MonetizationProduct.

    Returns the Stripe Session object, or None on failure.

    STRIPE MODE: TEST — no real charge will occur.
    """
    _assert_test_mode()

    import stripe

    if not product.stripe_price_id:
        log.warning(
            "create_checkout_session: product %s has no stripe_price_id — "
            "cannot create session.", product.id
        )
        return None

    if not product.is_active:
        log.warning(
            "create_checkout_session: product %s is inactive — blocked.", product.id
        )
        return None

    try:
        # Build metadata (safe identifiers only — no PII, no secrets)
        metadata = {
            "jhe_user_id":    str(user.id),
            "jhe_product_id": str(product.id),
            "jhe_product_type": product.product_type,
        }
        if listing_id:
            metadata["jhe_listing_id"] = str(listing_id)

        # Determine mode (one-time vs subscription)
        if product.product_type in ("business_plan", "dealer_plan"):
            mode = "subscription"
        else:
            mode = "payment"

        session = stripe.checkout.Session.create(
            mode=mode,
            line_items=[{"price": product.stripe_price_id, "quantity": 1}],
            success_url=success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=cancel_url,
            metadata=metadata,
            customer_email=getattr(user, "email", None),
            client_reference_id=str(user.id),
        )
        log.info(
            "Stripe TEST checkout session created: %s (product=%s user=%s)",
            session.id, product.id, user.id,
        )
        return session
    except Exception as exc:
        log.error("create_checkout_session error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def verify_webhook_signature(
    payload: bytes,
    sig_header: str,
    webhook_secret: str,
) -> Optional[object]:
    """Verify a Stripe webhook signature and return the event.

    Returns None if verification fails — caller must return HTTP 400.
    Never trusts the payload without verification.
    """
    import stripe

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
        return event
    except stripe.error.SignatureVerificationError as exc:
        log.warning("Stripe webhook signature verification failed: %s", exc)
        return None
    except Exception as exc:
        log.error("Stripe webhook parsing error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Purchase activation (called after verified checkout.session.completed)
# ---------------------------------------------------------------------------

def activate_purchase_from_session(session_obj) -> Optional[object]:
    """Activate a MonetizationPurchase after a verified checkout.session.completed event.

    Creates a pending purchase record if one doesn't exist, then marks it active.
    Returns the MonetizationPurchase, or None on failure.

    Entitlement is only granted AFTER server-side Stripe verification — never
    from a frontend success-page redirect alone.
    """
    try:
        from app import db
        from models import MonetizationProduct, MonetizationPurchase
        from monetization_service import record_audit_event, enqueue_promotion_expire

        meta        = session_obj.get("metadata", {}) or {}
        user_id     = meta.get("jhe_user_id")
        product_id  = meta.get("jhe_product_id")
        listing_id  = meta.get("jhe_listing_id")
        session_id  = session_obj.get("id")
        amount      = session_obj.get("amount_total")  # cents

        if not user_id or not product_id:
            log.error("activate_purchase_from_session: missing metadata user=%s product=%s",
                      user_id, product_id)
            return None

        product = MonetizationProduct.query.get(int(product_id))
        if not product:
            log.error("activate_purchase_from_session: product %s not found", product_id)
            return None

        # Look for existing pending purchase for this session
        purchase = MonetizationPurchase.query.filter_by(
            stripe_checkout_session_id=session_id
        ).first()

        if not purchase:
            purchase = MonetizationPurchase(
                user_id                  = user_id,
                product_id               = product.id,
                listing_id               = int(listing_id) if listing_id else None,
                stripe_checkout_session_id = session_id,
                amount_cents             = amount,
                status                   = "pending",
            )
            db.session.add(purchase)

        now = datetime.now()
        purchase.status     = "active"
        purchase.starts_at  = now
        purchase.amount_cents = amount or purchase.amount_cents

        # Set expiry for time-limited products
        if product.duration_days:
            purchase.expires_at = now + timedelta(days=product.duration_days)

        # Apply listing-level promotion flags
        if listing_id and product.product_type in ("featured_listing", "boost", "promoted_listing"):
            _apply_listing_promotion(int(listing_id), product.product_type, purchase.expires_at)

        # Apply plan upgrade
        if product.product_type in ("business_plan", "dealer_plan"):
            _apply_plan_upgrade(user_id, product.product_type, purchase)

        db.session.commit()

        # Enqueue expiry cleanup if time-limited
        if purchase.expires_at:
            enqueue_promotion_expire(purchase.id, purchase.expires_at)

        record_audit_event(
            "promotion_purchased",
            user_id=user_id,
            purchase_id=purchase.id,
            product_id=product.id,
            listing_id=int(listing_id) if listing_id else None,
            performed_by="stripe_webhook",
            detail={"product_type": product.product_type, "session_id": session_id},
        )

        log.info("Purchase activated: id=%s user=%s product=%s",
                 purchase.id, user_id, product.product_type)
        return purchase

    except Exception as exc:
        log.error("activate_purchase_from_session error: %s", exc)
        return None


def deactivate_purchase_subscription(stripe_subscription_id: str) -> bool:
    """Mark purchases with this Stripe subscription ID as expired.

    Called when Stripe reports customer.subscription.deleted.
    Returns True if at least one purchase was updated.
    """
    try:
        from app import db
        from models import MonetizationPurchase, User
        from monetization_service import record_audit_event

        purchases = MonetizationPurchase.query.filter_by(
            stripe_subscription_id=stripe_subscription_id,
            status="active",
        ).all()

        for p in purchases:
            p.status      = "expired"
            p.cancelled_at = datetime.now()

            # Downgrade user plan if it was a plan purchase
            _downgrade_plan_if_needed(p)

            record_audit_event(
                "subscription_cancelled",
                user_id=p.user_id,
                purchase_id=p.id,
                performed_by="stripe_webhook",
                detail={"stripe_sub_id": stripe_subscription_id},
            )

        db.session.commit()
        return len(purchases) > 0

    except Exception as exc:
        log.error("deactivate_purchase_subscription error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _apply_listing_promotion(listing_id: int, product_type: str, expires_at) -> None:
    """Set promoted fields on a Listing record."""
    try:
        from app import db
        from models import Listing

        listing = Listing.query.get(listing_id)
        if not listing:
            return

        listing.promoted_type = product_type
        if product_type == "featured_listing":
            listing.featured = True
        if product_type == "boost":
            listing.boost_expires_at = expires_at
    except Exception as exc:
        log.warning("_apply_listing_promotion error: %s", exc)


def _apply_plan_upgrade(user_id: str, product_type: str, purchase) -> None:
    """Upgrade user.seller_plan based on product_type."""
    try:
        from app import db
        from models import User

        user = User.query.get(user_id)
        if not user:
            return

        plan_map = {"business_plan": "business", "dealer_plan": "dealer"}
        new_plan = plan_map.get(product_type)
        if new_plan:
            user.seller_plan = new_plan
            user.seller_plan_stripe_sub_id = purchase.stripe_subscription_id
            if purchase.expires_at:
                user.seller_plan_expires_at = purchase.expires_at
    except Exception as exc:
        log.warning("_apply_plan_upgrade error: %s", exc)


def _downgrade_plan_if_needed(purchase) -> None:
    """Downgrade user.seller_plan to 'free' if the subscription was their plan."""
    try:
        from app import db
        from models import User, MonetizationProduct

        product = MonetizationProduct.query.get(purchase.product_id)
        if not product or product.product_type not in ("business_plan", "dealer_plan"):
            return

        user = User.query.get(purchase.user_id)
        if user:
            user.seller_plan            = "free"
            user.seller_plan_expires_at = None
            user.seller_plan_stripe_sub_id = None
    except Exception as exc:
        log.warning("_downgrade_plan_if_needed error: %s", exc)
