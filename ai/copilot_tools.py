"""JHE Haul — Phase G Copilot Tool Layer.

All tools enforce normal application authorization. The AI model NEVER receives
raw DB access — every call goes through this controlled layer.
"""

from __future__ import annotations
import logging
from typing import Optional

log = logging.getLogger("jhe.copilot_tools")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_listing(l) -> dict:
    """Return only safe public fields from a Listing ORM object."""
    from models import ListingPhoto
    thumb = None
    try:
        ph = (ListingPhoto.query
              .filter_by(listing_id=l.id, is_primary=True)
              .first())
        if ph and ph.storage_url:
            thumb = ph.storage_url
        elif ph and ph.photo_data:
            thumb = f"/listing-photo/{ph.id}"
    except Exception:
        pass
    has_jhe_delivery = bool(
        l.delivery_option and "jhe_haul" in (l.delivery_option or "").lower()
    )
    return {
        "id": l.id,
        "title": l.title,
        "price": l.price,
        "price_type": l.price_type,
        "condition": l.condition,
        "city": l.city,
        "state": l.state,
        "listing_type": l.listing_type,
        "status": l.status,
        "has_jhe_delivery": has_jhe_delivery,
        "view_count": l.view_count or 0,
        "thumbnail": thumb,
        "url": f"/listing/{l.id}",
        "vehicle_make": l.vehicle_make,
        "vehicle_model": l.vehicle_model,
        "vehicle_year": l.vehicle_year,
        "vehicle_mileage": l.vehicle_mileage,
        "description_snippet": (l.description or "")[:300] if l.description else None,
    }


def _active_listings_query():
    from models import Listing
    return Listing.query.filter(
        Listing.status == "active",
        Listing.moderation_status == "approved",
    )


# ---------------------------------------------------------------------------
# Tool 1 — search_listings  (PUBLIC)
# ---------------------------------------------------------------------------

def search_listings(
    keywords: Optional[str] = None,
    listing_type: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    city_zip: Optional[str] = None,
    vehicle_make: Optional[str] = None,
    vehicle_model: Optional[str] = None,
    condition: Optional[str] = None,
    delivery_available: Optional[bool] = None,
    category_slug: Optional[str] = None,
    limit: int = 6,
) -> dict:
    """Search active public listings."""
    try:
        from models import Listing, Category
        from sqlalchemy import or_
        q = _active_listings_query()

        if listing_type and listing_type in ("item", "property_sale", "rental"):
            q = q.filter(Listing.listing_type == listing_type)

        if min_price is not None:
            q = q.filter(Listing.price >= float(min_price))
        if max_price is not None:
            q = q.filter(Listing.price <= float(max_price))

        if condition and condition in ("new", "like_new", "good", "fair", "for_parts"):
            q = q.filter(Listing.condition == condition)

        if delivery_available:
            q = q.filter(Listing.delivery_option.ilike("%jhe_haul%"))

        if vehicle_make:
            q = q.filter(Listing.vehicle_make.ilike(f"%{vehicle_make}%"))
        if vehicle_model:
            q = q.filter(Listing.vehicle_model.ilike(f"%{vehicle_model}%"))

        if category_slug:
            cat = Category.query.filter_by(slug=category_slug).first()
            if cat:
                q = q.filter(Listing.category_id == cat.id)

        if city_zip:
            city_zip = city_zip.strip()
            if city_zip.isdigit():
                q = q.filter(Listing.zip_code == city_zip)
            else:
                q = q.filter(Listing.city.ilike(f"%{city_zip}%"))

        if keywords:
            kw = f"%{keywords}%"
            q = q.filter(
                or_(
                    Listing.title.ilike(kw),
                    Listing.description.ilike(kw),
                    Listing.vehicle_make.ilike(kw),
                    Listing.vehicle_model.ilike(kw),
                )
            )

        limit = max(1, min(int(limit), 12))
        listings = q.order_by(Listing.created_at.desc()).limit(limit).all()
        return {
            "count": len(listings),
            "listings": [_safe_listing(l) for l in listings],
        }
    except Exception as e:
        log.error("search_listings error: %s", e)
        return {"error": "search unavailable", "listings": []}


# ---------------------------------------------------------------------------
# Tool 2 — get_listing  (PUBLIC)
# ---------------------------------------------------------------------------

def get_listing(listing_id: int) -> dict:
    """Return safe public details of a single listing."""
    try:
        from models import Listing
        l = Listing.query.get(int(listing_id))
        if not l or l.status not in ("active", "reserved", "sold") or l.moderation_status != "approved":
            return {"error": "Listing not found or not public."}
        return {"listing": _safe_listing(l), "full_description": (l.description or "")[:2000]}
    except Exception as e:
        log.error("get_listing error: %s", e)
        return {"error": "Could not retrieve listing."}


# ---------------------------------------------------------------------------
# Tool 3 — get_similar_listings  (PUBLIC)
# ---------------------------------------------------------------------------

def get_similar_listings(listing_id: int, limit: int = 5) -> dict:
    """Return similar listings based on category and price range."""
    try:
        from models import Listing
        base = Listing.query.get(int(listing_id))
        if not base:
            return {"error": "Listing not found.", "listings": []}
        q = _active_listings_query().filter(Listing.id != base.id)
        if base.category_id:
            q = q.filter(Listing.category_id == base.category_id)
        elif base.listing_type:
            q = q.filter(Listing.listing_type == base.listing_type)
        if base.price and base.price > 0:
            lo = base.price * 0.6
            hi = base.price * 1.4
            q = q.filter(Listing.price.between(lo, hi))
        limit = max(1, min(int(limit), 8))
        results = q.order_by(Listing.created_at.desc()).limit(limit).all()
        return {"count": len(results), "listings": [_safe_listing(l) for l in results]}
    except Exception as e:
        log.error("get_similar_listings error: %s", e)
        return {"error": "Could not retrieve similar listings.", "listings": []}


# ---------------------------------------------------------------------------
# Tool 4 — get_marketplace_categories  (PUBLIC)
# ---------------------------------------------------------------------------

def get_marketplace_categories() -> dict:
    """Return available marketplace categories."""
    try:
        from models import Category
        cats = Category.query.filter_by(parent_id=None, is_active=True).order_by(Category.display_order).all()
        result = []
        for c in cats:
            result.append({
                "id": c.id,
                "name": c.name,
                "slug": c.slug,
                "icon": getattr(c, "icon", None),
            })
        return {"categories": result}
    except Exception as e:
        log.error("get_marketplace_categories error: %s", e)
        return {"error": "Could not retrieve categories.", "categories": []}


# ---------------------------------------------------------------------------
# Tool 5 — get_user_listings  (AUTH REQUIRED)
# ---------------------------------------------------------------------------

def get_user_listings(current_user) -> dict:
    """Return the authenticated seller's own listings only."""
    if not current_user or not current_user.is_authenticated:
        return {"error": "Sign in to view your listings.", "listings": []}
    try:
        from models import Listing
        listings = (Listing.query
                    .filter_by(seller_id=current_user.id)
                    .order_by(Listing.created_at.desc())
                    .limit(20).all())
        result = []
        for l in listings:
            result.append({
                "id": l.id,
                "title": l.title,
                "price": l.price,
                "status": l.status,
                "moderation_status": l.moderation_status,
                "view_count": l.view_count or 0,
                "favorite_count": l.favorite_count or 0,
                "created_at": l.created_at.strftime("%Y-%m-%d") if l.created_at else None,
                "listing_type": l.listing_type,
                "url": f"/listing/{l.id}",
            })
        active = [r for r in result if r["status"] == "active"]
        return {
            "total": len(result),
            "active_count": len(active),
            "listings": result,
        }
    except Exception as e:
        log.error("get_user_listings error: %s", e)
        return {"error": "Could not retrieve your listings.", "listings": []}


# ---------------------------------------------------------------------------
# Tool 6 — get_saved_items  (AUTH REQUIRED)
# ---------------------------------------------------------------------------

def get_saved_items(current_user) -> dict:
    """Return the authenticated user's saved/favorited listings."""
    if not current_user or not current_user.is_authenticated:
        return {"error": "Sign in to view saved items.", "listings": []}
    try:
        from models import ListingFavorite, Listing
        favs = (ListingFavorite.query
                .filter_by(user_id=current_user.id)
                .order_by(ListingFavorite.created_at.desc())
                .limit(20).all())
        result = []
        for fav in favs:
            l = Listing.query.get(fav.listing_id)
            if l:
                result.append({**_safe_listing(l), "saved_at": fav.created_at.strftime("%Y-%m-%d") if fav.created_at else None})
        return {"count": len(result), "listings": result}
    except Exception as e:
        log.error("get_saved_items error: %s", e)
        return {"error": "Could not retrieve saved items.", "listings": []}


# ---------------------------------------------------------------------------
# Tool 7 — get_user_offers  (AUTH REQUIRED)
# ---------------------------------------------------------------------------

def get_user_offers(current_user) -> dict:
    """Return offer status for the authenticated user (as buyer or seller)."""
    if not current_user or not current_user.is_authenticated:
        return {"error": "Sign in to view offers.", "offers": []}
    try:
        from models import ListingOffer, Listing
        from sqlalchemy import or_
        offers = (ListingOffer.query
                  .filter(or_(
                      ListingOffer.buyer_id == current_user.id,
                      ListingOffer.seller_id == current_user.id,
                  ))
                  .order_by(ListingOffer.created_at.desc())
                  .limit(20).all())
        result = []
        for o in offers:
            listing = Listing.query.get(o.listing_id)
            role = "buyer" if o.buyer_id == current_user.id else "seller"
            result.append({
                "id": o.id,
                "listing_title": listing.title if listing else "Unknown",
                "listing_url": f"/listing/{o.listing_id}" if listing else None,
                "amount": o.amount,
                "counter_amount": o.counter_amount,
                "status": o.status,
                "role": role,
                "expires_at": o.expires_at.strftime("%Y-%m-%d") if o.expires_at else None,
                "created_at": o.created_at.strftime("%Y-%m-%d") if o.created_at else None,
            })
        pending = [r for r in result if r["status"] == "pending"]
        return {
            "total": len(result),
            "pending_count": len(pending),
            "offers": result,
        }
    except Exception as e:
        log.error("get_user_offers error: %s", e)
        return {"error": "Could not retrieve offers.", "offers": []}


# ---------------------------------------------------------------------------
# Tool 8 — get_user_messages_summary  (AUTH REQUIRED)
# ---------------------------------------------------------------------------

def get_user_messages_summary(current_user) -> dict:
    """Return a safe summary of the user's message conversations (no full text)."""
    if not current_user or not current_user.is_authenticated:
        return {"error": "Sign in to view messages.", "summary": {}}
    try:
        from models import ListingConversation, ListingMessage, Listing
        from sqlalchemy import or_
        convos = (ListingConversation.query
                  .filter(or_(
                      ListingConversation.buyer_id == current_user.id,
                      ListingConversation.seller_id == current_user.id,
                  ))
                  .order_by(ListingConversation.updated_at.desc())
                  .limit(20).all())
        unread = 0
        threads = []
        for c in convos:
            # Count unread: messages NOT from current_user that are unread
            unread_count = (ListingMessage.query
                            .filter_by(conversation_id=c.id, is_read=False)
                            .filter(ListingMessage.sender_id != current_user.id)
                            .count())
            unread += unread_count
            listing = Listing.query.get(c.listing_id)
            threads.append({
                "conversation_id": c.id,
                "listing_title": listing.title if listing else "Unknown",
                "listing_url": f"/listing/{c.listing_id}" if listing else None,
                "unread": unread_count,
                "last_updated": c.updated_at.strftime("%Y-%m-%d") if c.updated_at else None,
            })
        return {
            "unread_total": unread,
            "conversation_count": len(threads),
            "threads": threads[:10],
            "messages_url": "/messages",
        }
    except Exception as e:
        log.error("get_user_messages_summary error: %s", e)
        return {"error": "Could not retrieve messages.", "summary": {}}


# ---------------------------------------------------------------------------
# Tool 9 — get_delivery_status  (AUTH REQUIRED)
# ---------------------------------------------------------------------------

def get_delivery_status(current_user) -> dict:
    """Return authorized delivery requests for the current user."""
    if not current_user or not current_user.is_authenticated:
        return {"error": "Sign in to view delivery status.", "deliveries": []}
    try:
        from models import DeliveryRequest, Listing
        from sqlalchemy import or_
        deliveries = (DeliveryRequest.query
                      .filter(or_(
                          DeliveryRequest.buyer_id == current_user.id,
                          DeliveryRequest.seller_id == current_user.id,
                      ))
                      .order_by(DeliveryRequest.created_at.desc())
                      .limit(10).all())
        STATUS_LABELS = {
            "pending": "Pending — waiting for a hauler",
            "quoted": "Quoted — you have a quote to review",
            "accepted": "Accepted — hauler confirmed",
            "declined": "Declined",
            "completed": "Completed",
            "cancelled": "Cancelled",
        }
        result = []
        for d in deliveries:
            listing = Listing.query.get(d.listing_id) if d.listing_id else None
            result.append({
                "id": d.id,
                "item_description": d.item_description,
                "listing_title": listing.title if listing else None,
                "status": d.status,
                "status_label": STATUS_LABELS.get(d.status, d.status),
                "pickup_city": d.pickup_city,
                "delivery_city": d.delivery_city,
                "quote_amount": d.quote_amount,
                "created_at": d.created_at.strftime("%Y-%m-%d") if d.created_at else None,
                "role": "buyer" if d.buyer_id == current_user.id else "seller",
            })
        return {"count": len(result), "deliveries": result, "delivery_url": "/delivery"}
    except Exception as e:
        log.error("get_delivery_status error: %s", e)
        return {"error": "Could not retrieve delivery status.", "deliveries": []}


# ---------------------------------------------------------------------------
# Tool 10 — get_seller_performance  (AUTH REQUIRED)
# ---------------------------------------------------------------------------

def get_seller_performance(current_user) -> dict:
    """Return safe seller stats: view counts, favorites, offer counts."""
    if not current_user or not current_user.is_authenticated:
        return {"error": "Sign in to view your seller stats.", "stats": {}}
    try:
        from models import Listing, ListingOffer
        listings = Listing.query.filter_by(seller_id=current_user.id).all()
        active = [l for l in listings if l.status == "active"]
        total_views = sum(l.view_count or 0 for l in listings)
        total_faves = sum(l.favorite_count or 0 for l in listings)
        pending_offers = ListingOffer.query.filter_by(seller_id=current_user.id, status="pending").count()
        top_listings = sorted(active, key=lambda l: l.view_count or 0, reverse=True)[:3]
        return {
            "total_listings": len(listings),
            "active_listings": len(active),
            "total_views": total_views,
            "total_favorites": total_faves,
            "pending_offers": pending_offers,
            "top_listings": [
                {"id": l.id, "title": l.title, "view_count": l.view_count or 0, "url": f"/listing/{l.id}"}
                for l in top_listings
            ],
            "dashboard_url": "/selling",
        }
    except Exception as e:
        log.error("get_seller_performance error: %s", e)
        return {"error": "Could not retrieve seller stats.", "stats": {}}


# ---------------------------------------------------------------------------
# Tool 11 — get_account_navigation_help  (PUBLIC)
# ---------------------------------------------------------------------------

_NAV_MAP = {
    "sell":         {"text": "Go to **Selling / My Listings** to create, edit, and manage your listings.",      "label": "Go to Selling",       "url": "/selling"},
    "selling":      {"text": "Go to **Selling / My Listings** to create, edit, and manage your listings.",      "label": "Go to Selling",       "url": "/selling"},
    "create":       {"text": "Tap **+ Sell** in the navigation or visit **Selling** to create a new listing.",  "label": "Create a Listing",    "url": "/selling/new"},
    "messages":     {"text": "Visit **Messages** to view all your buyer and seller conversations.",              "label": "Go to Messages",      "url": "/messages"},
    "offers":       {"text": "Visit **Offers** to see offers you've made or received.",                         "label": "Go to Offers",        "url": "/offers"},
    "saved":        {"text": "Visit **Saved Items** to see listings you've bookmarked.",                        "label": "View Saved Items",    "url": "/saved"},
    "favorites":    {"text": "Visit **Saved Items** to see listings you've bookmarked.",                        "label": "View Saved Items",    "url": "/saved"},
    "delivery":     {"text": "Visit **Deliveries** to request or track JHE Haul delivery.",                     "label": "Go to Deliveries",    "url": "/delivery"},
    "account":      {"text": "Visit **Account** to update your profile, photo, and notification settings.",     "label": "My Account",          "url": "/account"},
    "profile":      {"text": "Visit **Account** to update your profile, photo, and notification settings.",     "label": "My Account",          "url": "/account"},
    "password":     {"text": "Go to **Account → Change Password** to update your password.",                    "label": "Account Settings",    "url": "/account"},
    "notifications":{"text": "Go to **Account** to manage your email and notification preferences.",             "label": "Account Settings",    "url": "/account"},
    "marketplace":  {"text": "The **Marketplace** is the main browsing page for all active listings.",          "label": "Go to Marketplace",   "url": "/marketplace"},
    "search":       {"text": "Use the search bar on the **Marketplace** page or tap ✨ Ask JHE Haul.",          "label": "Browse Marketplace",  "url": "/marketplace"},
    "help":         {"text": "JHE Haul is a local marketplace. Browse items, vehicles, and properties; message sellers; make offers; or request JHE Haul delivery.", "label": "Browse Marketplace", "url": "/marketplace"},
    "default":      {"text": "I can help you navigate JHE Haul, find listings, check your offers or messages, and more.", "label": "Browse Marketplace", "url": "/marketplace"},
}

def get_account_navigation_help(topic: str = "default") -> dict:
    """Return navigation guidance for a JHE Haul section."""
    key = (topic or "default").lower().strip()
    for k in _NAV_MAP:
        if k in key:
            return _NAV_MAP[k]
    return _NAV_MAP["default"]


# ---------------------------------------------------------------------------
# Tool dispatcher  (called by copilot.py)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase H — Write action tools (return pending action, NO DB writes)
# ---------------------------------------------------------------------------

def save_listing(listing_id: int, current_user) -> dict:
    """Prepare to save a listing to the user's saved items. Shows confirmation before saving."""
    from ai.copilot_actions import prepare_save_listing
    return prepare_save_listing(int(listing_id), current_user)


def unsave_listing(listing_id: int, current_user) -> dict:
    """Prepare to remove a listing from the user's saved items. Shows confirmation before removing."""
    from ai.copilot_actions import prepare_unsave_listing
    return prepare_unsave_listing(int(listing_id), current_user)


def mark_listing_sold(listing_id: int, current_user) -> dict:
    """Prepare to mark the seller's own listing as Sold. Shows confirmation before changing status."""
    from ai.copilot_actions import prepare_mark_listing_sold
    return prepare_mark_listing_sold(int(listing_id), current_user)


def prepare_message(listing_id: int, message_text: str, current_user) -> dict:
    """Draft a message to the seller. NOT sent automatically — user reviews and sends from the message page."""
    from ai.copilot_actions import prepare_message_draft
    return prepare_message_draft(int(listing_id), message_text, current_user)


def start_delivery_request(listing_id: int, current_user) -> dict:
    """Start the JHE Haul delivery request flow for a listing."""
    from ai.copilot_actions import prepare_delivery_request_start
    return prepare_delivery_request_start(int(listing_id), current_user)


def prepare_listing_edit(listing_id: int, field: str, new_value: str, current_user) -> dict:
    """Preview a proposed change to the seller's listing (price or description). Requires confirmation."""
    from ai.copilot_actions import prepare_listing_edit as _prep
    return _prep(int(listing_id), field, new_value, current_user)


_TOOL_REGISTRY = {
    # Phase G — read-only
    "search_listings":              search_listings,
    "get_listing":                  get_listing,
    "get_similar_listings":         get_similar_listings,
    "get_marketplace_categories":   get_marketplace_categories,
    "get_user_listings":            get_user_listings,
    "get_saved_items":              get_saved_items,
    "get_user_offers":              get_user_offers,
    "get_user_messages_summary":    get_user_messages_summary,
    "get_delivery_status":          get_delivery_status,
    "get_seller_performance":       get_seller_performance,
    "get_account_navigation_help":  get_account_navigation_help,
    # Phase H — controlled write actions (return pending, not executed)
    "save_listing":                 save_listing,
    "unsave_listing":               unsave_listing,
    "mark_listing_sold":            mark_listing_sold,
    "prepare_message":              prepare_message,
    "start_delivery_request":       start_delivery_request,
    "prepare_listing_edit":         prepare_listing_edit,
}

# Tools that require authentication
_AUTH_REQUIRED_TOOLS = {
    "get_user_listings",
    "get_saved_items",
    "get_user_offers",
    "get_user_messages_summary",
    "get_delivery_status",
    "get_seller_performance",
    "save_listing",
    "unsave_listing",
    "mark_listing_sold",
    "prepare_message",
    "start_delivery_request",
    "prepare_listing_edit",
}

def dispatch_tool(name: str, args: dict, current_user) -> dict:
    """Call a whitelisted tool with validated inputs.  No arbitrary dispatch."""
    if name not in _TOOL_REGISTRY:
        return {"error": f"Unknown tool: {name}"}
    if name in _AUTH_REQUIRED_TOOLS:
        return _TOOL_REGISTRY[name](current_user=current_user, **args)
    return _TOOL_REGISTRY[name](**args)
