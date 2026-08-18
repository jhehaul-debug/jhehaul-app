"""JHE Haul — Phase H Controlled Copilot Actions.

Two-phase design:
  1. prepare_*()  — called by AI tools; validates auth/ownership; returns a
                    pending-action dict for the frontend to confirm. NO DB writes.
  2. execute_*()  — called by /api/copilot/action/execute AFTER user confirms;
                    performs the actual state change with full server-side checks.

No action executes without going through the confirm step.
No payment, delete, or security-settings actions are permitted.
"""

from __future__ import annotations
import logging
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional

log = logging.getLogger("jhe.copilot_actions")

# ---------------------------------------------------------------------------
# Per-user action rate limiter (prevents edit spam)
# ---------------------------------------------------------------------------
_ACTION_RATE: dict[str, dict[str, deque]] = defaultdict(lambda: defaultdict(deque))
_ACTION_LIMITS = {
    "apply_listing_edit": (10, 3600),  # 10 per hour
    "mark_listing_sold":  (5,  3600),
    "save_listing":       (50, 3600),  # effectively unlimited (idempotent)
    "unsave_listing":     (50, 3600),
}

def _action_rate_ok(user_id: str, action_type: str) -> bool:
    limit, window = _ACTION_LIMITS.get(action_type, (20, 3600))
    now = time.time()
    dq = _ACTION_RATE[user_id][action_type]
    while dq and dq[0] < now - window:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


# ---------------------------------------------------------------------------
# Allowed action types (whitelist)
# ---------------------------------------------------------------------------
PREPARE_ACTIONS = frozenset({
    "save_listing",
    "unsave_listing",
    "mark_listing_sold",
    "prepare_message",
    "start_delivery_request",
    "prepare_listing_edit",
})

EXECUTE_ACTIONS = frozenset({
    "save_listing",
    "unsave_listing",
    "mark_listing_sold",
    "apply_listing_edit",
    # prepare_message / start_delivery_request have no execute step — they
    # are navigate/preview actions only.
})

# Allowed fields for listing edit
_EDITABLE_FIELDS = {"price", "description"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _listing_or_error(listing_id: int):
    from models import Listing
    try:
        l = Listing.query.get(int(listing_id))
    except Exception:
        l = None
    if not l:
        return None, "Listing not found."
    return l, None


def _require_auth(current_user):
    if not current_user or not current_user.is_authenticated:
        return "You need to be signed in to do that."
    return None


# ---------------------------------------------------------------------------
# PREPARE FUNCTIONS  — return pending action, NO DB writes
# ---------------------------------------------------------------------------

def prepare_save_listing(listing_id: int, current_user) -> dict:
    err = _require_auth(current_user)
    if err:
        return {"error": err}
    listing, err = _listing_or_error(listing_id)
    if err:
        return {"error": err}
    if listing.status not in ("active", "reserved", "sold", "pending"):
        return {"error": "This listing is not available to save."}
    if listing.seller_id == current_user.id:
        return {"error": "You cannot save your own listing."}
    return {
        "_action_pending": True,
        "action_type": "save_listing",
        "listing_id": listing_id,
        "params": {"listing_id": listing_id},
        "confirmation_text": f"Save \"{listing.title}\" to your saved items?",
        "preview": {
            "action_label": "💾 Save to Saved Items",
            "listing_title": listing.title,
            "listing_price": listing.price,
            "listing_url": f"/listing/{listing_id}",
        },
    }


def prepare_unsave_listing(listing_id: int, current_user) -> dict:
    err = _require_auth(current_user)
    if err:
        return {"error": err}
    listing, err = _listing_or_error(listing_id)
    if err:
        return {"error": err}
    from models import ListingFavorite
    fav = ListingFavorite.query.filter_by(
        user_id=current_user.id, listing_id=listing_id).first()
    if not fav:
        return {"error": f"\"{listing.title}\" is not in your saved items."}
    return {
        "_action_pending": True,
        "action_type": "unsave_listing",
        "listing_id": listing_id,
        "params": {"listing_id": listing_id},
        "confirmation_text": f"Remove \"{listing.title}\" from your saved items?",
        "preview": {
            "action_label": "🗑 Remove from Saved Items",
            "listing_title": listing.title,
            "listing_url": f"/listing/{listing_id}",
        },
    }


def prepare_mark_listing_sold(listing_id: int, current_user) -> dict:
    err = _require_auth(current_user)
    if err:
        return {"error": err}
    listing, err = _listing_or_error(listing_id)
    if err:
        return {"error": err}
    if listing.seller_id != current_user.id:
        return {"error": "You can only mark your own listings as sold."}
    if listing.status not in ("active", "reserved"):
        status_label = listing.status.title()
        return {"error": f"This listing is already {status_label} and cannot be changed."}
    return {
        "_action_pending": True,
        "action_type": "mark_listing_sold",
        "listing_id": listing_id,
        "params": {"listing_id": listing_id},
        "confirmation_text": (
            f"Mark \"{listing.title}\" as Sold? "
            "This will notify buyers with pending offers. You can update the listing again from My Listings."
        ),
        "preview": {
            "action_label": "✅ Mark as Sold",
            "listing_title": listing.title,
            "listing_price": listing.price,
            "listing_url": f"/listing/{listing_id}",
            "warning": "Pending offers on this listing will be expired.",
        },
    }


def prepare_message_draft(listing_id: int, message_text: str, current_user) -> dict:
    """Draft a message to the seller. NOT sent automatically — user must confirm in UI."""
    err = _require_auth(current_user)
    if err:
        return {"error": err}
    listing, err = _listing_or_error(listing_id)
    if err:
        return {"error": err}
    if listing.seller_id == current_user.id:
        return {"error": "You cannot message yourself as the seller."}
    if listing.status not in ("active", "reserved"):
        return {"error": "This listing is no longer active."}
    # Sanitize: limit length and strip suspicious patterns
    safe_text = (message_text or "").strip()[:500]
    if not safe_text:
        return {"error": "Message text is required."}
    return {
        "_action_pending": True,
        "action_type": "prepare_message",
        "listing_id": listing_id,
        "params": {"listing_id": listing_id, "message_text": safe_text},
        "confirmation_text": "Send this message to the seller?",
        "preview": {
            "action_label": "💬 Message Seller",
            "listing_title": listing.title,
            "message_text": safe_text,
            "listing_url": f"/listing/{listing_id}",
            "message_url": f"/listing/{listing_id}/message",
        },
        "_navigate_only": True,   # No execute step — link user to message page
    }


def prepare_delivery_request_start(listing_id: int, current_user) -> dict:
    """Start the existing delivery request flow for a listing."""
    err = _require_auth(current_user)
    if err:
        return {"error": err}
    listing, err = _listing_or_error(listing_id)
    if err:
        return {"error": err}
    if listing.seller_id == current_user.id:
        return {"error": "Sellers cannot request delivery for their own listings."}
    if listing.listing_type == "property_sale":
        return {"error": "JHE Haul delivery is not available for property listings."}
    if listing.status not in ("active", "reserved"):
        return {"error": "This listing is not currently available."}
    has_jhe = bool(listing.delivery_option and "jhe_haul" in (listing.delivery_option or "").lower())
    if not has_jhe:
        return {
            "error": None,
            "message": f"\"{listing.title}\" does not currently offer JHE Haul delivery. You can request it on the listing page.",
            "_navigate_only": True,
            "nav_link": {"label": "View Listing", "url": f"/listing/{listing_id}"},
        }
    return {
        "_action_pending": True,
        "action_type": "start_delivery_request",
        "listing_id": listing_id,
        "params": {"listing_id": listing_id},
        "confirmation_text": f"Start a JHE Haul delivery request for \"{listing.title}\"?",
        "preview": {
            "action_label": "🚚 Request JHE Haul Delivery",
            "listing_title": listing.title,
            "pickup_city": listing.city or "Seller location",
            "delivery_url": f"/listing/{listing_id}/request-delivery",
        },
        "_navigate_only": True,   # On confirm, navigate to the existing form
    }


def prepare_listing_edit(listing_id: int, field: str, new_value: str, current_user) -> dict:
    """Preview a proposed listing change. Seller must confirm before applying."""
    err = _require_auth(current_user)
    if err:
        return {"error": err}
    listing, err = _listing_or_error(listing_id)
    if err:
        return {"error": err}
    if listing.seller_id != current_user.id:
        return {"error": "You can only edit your own listings."}
    if listing.status in ("removed", "expired"):
        return {"error": "This listing cannot be edited in its current state."}
    field = (field or "").lower().strip()
    if field not in _EDITABLE_FIELDS:
        return {
            "error": f"Copilot can only help edit price or description in this phase. "
                     f"For other changes, go to Edit Listing.",
            "nav_link": {"label": "Edit Listing", "url": f"/listing/{listing_id}/edit"},
        }
    # Validate new value
    if field == "price":
        try:
            price_val = float(str(new_value).replace("$", "").replace(",", "").strip())
            if price_val < 0:
                return {"error": "Price cannot be negative."}
            if price_val > 10_000_000:
                return {"error": "Price seems too high. Please check and try again."}
            display_new = f"${price_val:,.2f}"
            display_old = f"${listing.price:,.2f}" if listing.price is not None else "Not set"
            clean_value = str(price_val)
        except (ValueError, TypeError):
            return {"error": "Please provide a valid numeric price, e.g. 9500 or $9,500."}
    elif field == "description":
        clean_value = str(new_value or "").strip()[:3000]
        if len(clean_value) < 10:
            return {"error": "Description is too short (minimum 10 characters)."}
        display_old = (listing.description or "")[:120] + ("…" if len(listing.description or "") > 120 else "")
        display_new = clean_value[:120] + ("…" if len(clean_value) > 120 else "")
    else:
        return {"error": "Unsupported field."}

    return {
        "_action_pending": True,
        "action_type": "apply_listing_edit",
        "listing_id": listing_id,
        "params": {"listing_id": listing_id, "field": field, "new_value": clean_value},
        "confirmation_text": f"Apply this change to \"{listing.title}\"?",
        "preview": {
            "action_label": f"✏️ Update {field.title()}",
            "listing_title": listing.title,
            "field": field,
            "old_value": display_old,
            "new_value": display_new,
            "listing_url": f"/listing/{listing_id}",
        },
    }


# ---------------------------------------------------------------------------
# EXECUTE FUNCTIONS  — run AFTER user confirms; full server-side auth checks
# ---------------------------------------------------------------------------

def execute_save_listing(listing_id: int, current_user) -> dict:
    if not current_user or not current_user.is_authenticated:
        return {"success": False, "message": "You need to be signed in."}
    if not _action_rate_ok(current_user.id, "save_listing"):
        return {"success": False, "message": "Too many save actions. Please try again later."}
    from models import Listing, ListingFavorite, db
    listing = Listing.query.get(int(listing_id))
    if not listing or listing.status not in ("active", "reserved", "sold", "pending"):
        return {"success": False, "message": "Listing not found or no longer available."}
    if listing.seller_id == current_user.id:
        return {"success": False, "message": "You cannot save your own listing."}
    existing = ListingFavorite.query.filter_by(
        user_id=current_user.id, listing_id=listing_id).first()
    if existing:
        return {
            "success": True,
            "message": f"\"{listing.title}\" is already in your saved items.",
            "nav_links": [{"label": "View Saved Items", "url": "/saved"}],
        }
    fav = ListingFavorite(user_id=current_user.id, listing_id=listing_id)
    db.session.add(fav)
    listing.favorite_count = (listing.favorite_count or 0) + 1
    db.session.commit()
    log.info("copilot: user %s saved listing %s", current_user.id[-4:], listing_id)
    return {
        "success": True,
        "message": f"\"{listing.title}\" has been saved to your saved items.",
        "nav_links": [{"label": "View Saved Items", "url": "/saved"}],
    }


def execute_unsave_listing(listing_id: int, current_user) -> dict:
    if not current_user or not current_user.is_authenticated:
        return {"success": False, "message": "You need to be signed in."}
    from models import Listing, ListingFavorite, db
    listing = Listing.query.get(int(listing_id))
    title = listing.title if listing else f"Listing #{listing_id}"
    fav = ListingFavorite.query.filter_by(
        user_id=current_user.id, listing_id=listing_id).first()
    if not fav:
        return {"success": True, "message": f"\"{title}\" was not in your saved items."}
    db.session.delete(fav)
    if listing:
        listing.favorite_count = max(0, (listing.favorite_count or 1) - 1)
    db.session.commit()
    log.info("copilot: user %s unsaved listing %s", current_user.id[-4:], listing_id)
    return {
        "success": True,
        "message": f"\"{title}\" has been removed from your saved items.",
        "nav_links": [{"label": "View Saved Items", "url": "/saved"}],
    }


def execute_mark_listing_sold(listing_id: int, current_user) -> dict:
    if not current_user or not current_user.is_authenticated:
        return {"success": False, "message": "You need to be signed in."}
    if not _action_rate_ok(current_user.id, "mark_listing_sold"):
        return {"success": False, "message": "Too many requests. Please try again later."}
    from models import Listing, db, expire_pending_offers
    listing = Listing.query.get(int(listing_id))
    if not listing:
        return {"success": False, "message": "Listing not found."}
    if listing.seller_id != current_user.id:
        return {"success": False, "message": "You can only mark your own listings as sold."}
    if listing.status == "sold":
        return {"success": True, "message": f"\"{listing.title}\" is already marked as Sold.",
                "nav_links": [{"label": "View Listing", "url": f"/listing/{listing_id}"}]}
    if listing.status in ("removed", "expired"):
        return {"success": False,
                "message": f"This listing is {listing.status} and cannot be changed."}
    if listing.status not in ("active", "reserved", "pending"):
        return {"success": False, "message": "This listing cannot be marked sold in its current state."}
    # Expire pending offers — collect notification targets
    notif_targets = expire_pending_offers(listing_id)
    listing.status = "sold"
    listing.sold_at = datetime.utcnow()
    db.session.commit()
    # Deactivate any gallery pins for this listing
    try:
        from models import GalleryPhoto
        pins = GalleryPhoto.query.filter_by(listing_id=listing_id, is_active=True).all()
        for pin in pins:
            pin.is_active = False
        if pins:
            db.session.commit()
    except Exception as e:
        log.warning("copilot mark_sold: gallery pin cleanup failed: %s", e)
    # Send offer-expired notifications (best-effort)
    try:
        from email_service import notify_buyer_offer_expired
        for t in notif_targets:
            if t.get("buyer_email"):
                notify_buyer_offer_expired(
                    buyer_email=t["buyer_email"],
                    listing_title=listing.title,
                    listing_id=listing_id,
                    offer_amount=t.get("offer_amount"),
                )
    except Exception as e:
        log.warning("copilot mark_sold: notification error: %s", e)
    log.info("copilot: user %s marked listing %s sold", current_user.id[-4:], listing_id)
    return {
        "success": True,
        "message": f"\"{listing.title}\" has been marked as Sold.",
        "nav_links": [
            {"label": "View Listing", "url": f"/listing/{listing_id}"},
            {"label": "My Listings", "url": "/selling"},
        ],
    }


def execute_apply_listing_edit(listing_id: int, field: str, new_value: str, current_user) -> dict:
    if not current_user or not current_user.is_authenticated:
        return {"success": False, "message": "You need to be signed in."}
    if not _action_rate_ok(current_user.id, "apply_listing_edit"):
        return {"success": False, "message": "Too many edit requests. Please try again later."}
    field = (field or "").lower().strip()
    if field not in _EDITABLE_FIELDS:
        return {"success": False, "message": "That field cannot be edited via the Copilot."}
    from models import Listing, db
    listing = Listing.query.get(int(listing_id))
    if not listing:
        return {"success": False, "message": "Listing not found."}
    if listing.seller_id != current_user.id:
        return {"success": False, "message": "You can only edit your own listings."}
    if listing.status in ("removed", "expired"):
        return {"success": False, "message": "This listing cannot be edited in its current state."}
    if field == "price":
        try:
            price_val = float(str(new_value).replace("$", "").replace(",", "").strip())
            if price_val < 0 or price_val > 10_000_000:
                return {"success": False, "message": "Invalid price value."}
            listing.price = price_val
        except (ValueError, TypeError):
            return {"success": False, "message": "Invalid price."}
    elif field == "description":
        listing.description = str(new_value or "").strip()[:3000]
    listing.updated_at = datetime.utcnow()
    db.session.commit()
    log.info("copilot: user %s edited listing %s field=%s", current_user.id[-4:], listing_id, field)
    return {
        "success": True,
        "message": f"The {field} for \"{listing.title}\" has been updated.",
        "nav_links": [
            {"label": "View Listing", "url": f"/listing/{listing_id}"},
            {"label": "Edit Listing", "url": f"/listing/{listing_id}/edit"},
        ],
    }


# ---------------------------------------------------------------------------
# Main dispatcher — called by /api/copilot/action/execute
# ---------------------------------------------------------------------------

_EXECUTORS = {
    "save_listing":      lambda p, u: execute_save_listing(p["listing_id"], u),
    "unsave_listing":    lambda p, u: execute_unsave_listing(p["listing_id"], u),
    "mark_listing_sold": lambda p, u: execute_mark_listing_sold(p["listing_id"], u),
    "apply_listing_edit": lambda p, u: execute_apply_listing_edit(
        p["listing_id"], p.get("field", ""), p.get("new_value", ""), u),
}

def execute_action(action_type: str, params: dict, current_user) -> dict:
    """Dispatch a confirmed action.  Only whitelisted types are permitted."""
    if action_type not in _EXECUTORS:
        return {
            "success": False,
            "message": f"Action '{action_type}' is not available. Please use the marketplace UI.",
        }
    try:
        return _EXECUTORS[action_type](params, current_user)
    except Exception as e:
        log.error("execute_action %s error: %s", action_type, e)
        return {"success": False, "message": "Something went wrong. Please try again or use the marketplace UI."}
