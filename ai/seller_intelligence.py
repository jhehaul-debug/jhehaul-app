"""JHE Haul — Phase I: AI Seller Intelligence Service.

Architecture:
  - All numeric metrics (views, counts, age) computed in Python — NO AI for numbers.
  - AI model (GPT-4o-mini) called ONLY for natural-language recommendation narrative.
  - 5-minute in-memory cache per seller to avoid repeated DB queries and AI calls.
  - Every function enforces seller_id ownership — no cross-seller data leakage.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("jhe.seller_intelligence")

# ---------------------------------------------------------------------------
# 5-minute per-seller cache
# ---------------------------------------------------------------------------
_OVERVIEW_CACHE: dict[str, tuple[float, dict]] = {}
_LISTING_CACHE:  dict[tuple, tuple[float, dict]] = {}
_CACHE_TTL = 300   # seconds


def _cache_get(store: dict, key) -> Optional[dict]:
    entry = store.get(key)
    if entry and time.time() - entry[0] < _CACHE_TTL:
        return entry[1]
    return None


def _cache_set(store: dict, key, value: dict) -> None:
    store[key] = (time.time(), value)


# ---------------------------------------------------------------------------
# Quality Score — deterministic, no AI
# ---------------------------------------------------------------------------

def _photo_count(l) -> int:
    try:
        photos = l.photos
        if photos is None:
            return 0
        return len(list(photos))
    except Exception:
        return 0


_QUALITY_CRITERIA = [
    ("title",        lambda l: 2 if len(l.title or "") > 15 else (1 if len(l.title or "") > 4 else 0)),
    ("description",  lambda l: 3 if len(l.description or "") > 150 else (2 if len(l.description or "") > 30 else (1 if len(l.description or "") > 0 else 0))),
    ("photos",       lambda l: 2 if _photo_count(l) >= 4 else (1 if _photo_count(l) >= 1 else 0)),
    ("price",        lambda l: 1 if (l.price is not None or getattr(l, 'price_type', None) == "free") else 0),
    ("location",     lambda l: 1 if l.city else 0),
    ("category",     lambda l: 1 if l.category_id else 0),
    ("delivery",     lambda l: 1 if l.delivery_option else 0),
    ("video",        lambda l: 1 if _has_video(l) else 0),
    ("condition",    lambda l: 1 if l.condition else 0),
    ("vehicle_fields", lambda l: 1 if _vehicle_complete(l) else 0),
]
_MAX_QUALITY_PTS = sum(
    max(2, 3, 2, 1, 1, 1, 1, 1, 1, 1)  # approximate max per criterion
    for _ in range(1)
) or 13   # actual max = 2+3+2+1+1+1+1+1+1+1 = 14

def _has_video(listing) -> bool:
    try:
        return bool(listing.videos and len(listing.videos) > 0)
    except Exception:
        return False

def _vehicle_complete(listing) -> bool:
    if listing.listing_type != "vehicle":
        return True   # not applicable → doesn't penalize
    return bool(listing.vehicle_make and listing.vehicle_model and listing.vehicle_year)


def compute_listing_quality_score(listing) -> dict:
    """Return {score: int 1-10, label: str, missing: [criteria names]}."""
    raw = 0
    missing = []
    for name, fn in _QUALITY_CRITERIA:
        pts = fn(listing)
        max_pts = {"title": 2, "description": 3, "photos": 2}.get(name, 1)
        raw += pts
        if pts < max_pts:
            missing.append(name)

    # Normalize: max raw = 14 → 10
    score = max(1, min(10, round(raw / 14 * 10)))
    if score >= 8:
        label = "Excellent"
    elif score >= 5:
        label = "Good"
    else:
        label = "Needs Improvement"

    return {"score": score, "label": label, "missing": missing}


# ---------------------------------------------------------------------------
# Attention signals
# ---------------------------------------------------------------------------

_HIGH_VIEWS_THRESHOLD     = 25    # views with 0 offers → surface
_LOW_ENGAGEMENT_DAYS      = 14    # days active
_LOW_ENGAGEMENT_MAX_VIEWS = 5     # below this is low
_EXPIRING_DAYS            = 5     # within N days → surface
_QUALITY_ATTENTION_SCORE  = 4     # quality score ≤ this → surface


def _listing_age_days(listing) -> int:
    try:
        created = listing.created_at
        if created is None:
            return 0
        if created.tzinfo is not None:
            now = datetime.now(timezone.utc)
        else:
            now = datetime.utcnow()
        return (now - created).days
    except Exception:
        return 0


def detect_attention_signals(seller_id: str, active_listings: list) -> list[dict]:
    """Return sorted attention-signal dicts for the seller's active listings.
    Ownership verified: only uses listings already filtered to seller_id.
    """
    from models import ListingOffer, ListingMessage, ListingConversation

    signals = []

    # --- Pending offers (global, not per-listing) ---
    try:
        pending_count = ListingOffer.query.filter(
            ListingOffer.seller_id == seller_id,
            ListingOffer.status == "pending",
        ).count()
        if pending_count > 0:
            signals.append({
                "type":     "pending_offers",
                "priority": 1,
                "label":    "Pending Offers",
                "message":  f"You have {pending_count} pending offer{'s' if pending_count != 1 else ''} waiting for a response.",
                "url":      "/seller/offers",
                "count":    pending_count,
            })
    except Exception as e:
        log.warning("attention_signals: pending offers query failed: %s", e)

    # --- Unread messages ---
    try:
        unread_count = (
            ListingMessage.query
            .join(ListingConversation, ListingMessage.conversation_id == ListingConversation.id)
            .filter(
                ListingConversation.seller_id == seller_id,
                ListingMessage.sender_id != seller_id,
                ListingMessage.read_at.is_(None),
            ).count()
        )
        if unread_count > 0:
            signals.append({
                "type":     "unread_messages",
                "priority": 1,
                "label":    "Unread Messages",
                "message":  f"You have {unread_count} unread buyer message{'s' if unread_count != 1 else ''}.",
                "url":      "/messages",
                "count":    unread_count,
            })
    except Exception as e:
        log.warning("attention_signals: unread messages query failed: %s", e)

    # --- Per-listing signals ---
    for lst in active_listings:
        age_days  = _listing_age_days(lst)
        views     = lst.view_count or 0
        listing_url = f"/listing/{lst.id}"

        # Expiring soon
        try:
            if lst.expires_at:
                exp = lst.expires_at
                if exp.tzinfo is not None:
                    now = datetime.now(timezone.utc)
                else:
                    now = datetime.utcnow()
                days_left = (exp - now).days
                if 0 <= days_left <= _EXPIRING_DAYS:
                    signals.append({
                        "type":       "expiring_soon",
                        "priority":   2,
                        "label":      "Expiring Soon",
                        "message":    f"\"{lst.title}\" expires in {days_left} day{'s' if days_left != 1 else ''}.",
                        "url":        listing_url,
                        "listing_id": lst.id,
                        "days_left":  days_left,
                    })
        except Exception:
            pass

        # High views, no offers
        try:
            offer_count = ListingOffer.query.filter(
                ListingOffer.listing_id == lst.id,
                ListingOffer.status.in_(["pending", "countered", "accepted"]),
            ).count()
            if views >= _HIGH_VIEWS_THRESHOLD and offer_count == 0:
                signals.append({
                    "type":       "high_views_no_offers",
                    "priority":   3,
                    "label":      "High Views, No Offers",
                    "message":    f"\"{lst.title}\" has {views} views but no offers yet. Consider reviewing the price or listing details.",
                    "url":        listing_url,
                    "listing_id": lst.id,
                    "views":      views,
                })
        except Exception:
            pass

        # Low engagement
        if age_days >= _LOW_ENGAGEMENT_DAYS and views <= _LOW_ENGAGEMENT_MAX_VIEWS:
            signals.append({
                "type":       "low_engagement",
                "priority":   4,
                "label":      "Low Engagement",
                "message":    f"\"{lst.title}\" has been active {age_days} days with only {views} view{'s' if views != 1 else ''}.",
                "url":        listing_url,
                "listing_id": lst.id,
                "views":      views,
                "days_active": age_days,
            })

        # Incomplete listing
        try:
            quality = compute_listing_quality_score(lst)
            if quality["score"] <= _QUALITY_ATTENTION_SCORE:
                missing_display = ", ".join(quality["missing"][:3]) if quality["missing"] else "details"
                signals.append({
                    "type":       "incomplete_listing",
                    "priority":   5,
                    "label":      "Listing Needs More Detail",
                    "message":    f"\"{lst.title}\" is missing: {missing_display}. Improve it to attract more buyers.",
                    "url":        f"/listing/{lst.id}/edit",
                    "listing_id": lst.id,
                    "quality_score": quality["score"],
                })
        except Exception:
            pass

    # Sort by priority then listing
    signals.sort(key=lambda s: (s["priority"], s.get("listing_id", 0)))
    return signals[:12]   # cap at 12 attention cards


# ---------------------------------------------------------------------------
# Top / low-engagement listings
# ---------------------------------------------------------------------------

def _listing_engagement_score(lst) -> float:
    """Simple ranking score: views + 5*offers + 3*convos (no AI)."""
    views = lst.view_count or 0
    favs  = lst.favorite_count or 0
    return views + favs * 2


def get_top_listings(active_listings: list, n: int = 3) -> list[dict]:
    ranked = sorted(active_listings, key=_listing_engagement_score, reverse=True)
    return [_safe_listing_summary(lst) for lst in ranked[:n]]


def get_low_engagement_listings(active_listings: list, n: int = 5) -> list[dict]:
    old = [lst for lst in active_listings if _listing_age_days(lst) >= _LOW_ENGAGEMENT_DAYS]
    ranked = sorted(old, key=_listing_engagement_score)
    return [_safe_listing_summary(lst) for lst in ranked[:n]]


def _safe_listing_summary(lst) -> dict:
    quality = compute_listing_quality_score(lst)
    photo_count = _photo_count(lst)
    return {
        "id":            lst.id,
        "title":         lst.title,
        "price":         float(lst.price) if lst.price is not None else None,
        "price_type":    lst.price_type,
        "views":         lst.view_count or 0,
        "favorites":     lst.favorite_count or 0,
        "status":        lst.status,
        "days_active":   _listing_age_days(lst),
        "photo_count":   photo_count,
        "quality_score": quality["score"],
        "quality_label": quality["label"],
        "quality_missing": quality["missing"],
        "url":           f"/listing/{lst.id}",
        "edit_url":      f"/listing/{lst.id}/edit",
        "delivery":      bool(lst.delivery_option),
        "listing_type":  lst.listing_type,
    }


# ---------------------------------------------------------------------------
# Comparable price range (real data only)
# ---------------------------------------------------------------------------
_COMPARABLE_MIN_SAMPLES = 3   # need at least this many to show range


def get_comparable_price_range(listing) -> Optional[dict]:
    """Return price range of comparable active JHE listings, or None if insufficient data."""
    from models import Listing
    try:
        query = Listing.query.filter(
            Listing.status == "active",
            Listing.moderation_status == "approved",
            Listing.id != listing.id,
            Listing.price.isnot(None),
            Listing.price > 0,
        )
        if listing.category_id:
            query = query.filter(Listing.category_id == listing.category_id)
        elif listing.listing_type:
            query = query.filter(Listing.listing_type == listing.listing_type)
        prices = [float(l.price) for l in query.all()]
        if len(prices) < _COMPARABLE_MIN_SAMPLES:
            return None
        prices.sort()
        # Use 20th–80th percentile range
        lo = prices[max(0, int(len(prices) * 0.2))]
        hi = prices[min(len(prices) - 1, int(len(prices) * 0.8))]
        return {"min": lo, "max": hi, "sample_count": len(prices)}
    except Exception as e:
        log.warning("comparable_price_range failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Per-listing intelligence
# ---------------------------------------------------------------------------

def get_listing_intel(listing_id: int, seller_id: str) -> dict:
    """Return detailed intelligence for one seller-owned listing.
    Enforces ownership: returns error if seller doesn't own it.
    """
    cache_key = (listing_id, seller_id)
    cached = _cache_get(_LISTING_CACHE, cache_key)
    if cached:
        return cached

    from models import Listing, ListingOffer, ListingConversation
    listing = Listing.query.get(int(listing_id))
    if not listing:
        return {"error": "Listing not found."}
    if listing.seller_id != seller_id:
        return {"error": "You do not have permission to view intelligence for this listing."}

    quality   = compute_listing_quality_score(listing)
    age_days  = _listing_age_days(listing)
    photo_count = _photo_count(listing)
    price_range = get_comparable_price_range(listing)

    try:
        pending_offers = ListingOffer.query.filter(
            ListingOffer.listing_id == listing_id,
            ListingOffer.status == "pending",
        ).count()
    except Exception:
        pending_offers = 0

    try:
        convos = ListingConversation.query.filter_by(listing_id=listing_id).count()
    except Exception:
        convos = 0

    # Recommendations: purely logic-based, plain strings
    recommendations = _build_listing_recommendations(listing, quality, photo_count)

    # AI narrative for this listing — only if OPENAI_API_KEY set
    ai_summary = _ai_listing_narrative(listing, quality, pending_offers, convos, age_days, photo_count)

    result = {
        "listing_id":       listing_id,
        "title":            listing.title,
        "status":           listing.status,
        "views":            listing.view_count or 0,
        "favorites":        listing.favorite_count or 0,
        "days_active":      age_days,
        "photo_count":      photo_count,
        "has_video":        _has_video(listing),
        "has_delivery":     bool(listing.delivery_option),
        "pending_offers":   pending_offers,
        "conversations":    convos,
        "quality":          quality,
        "recommendations":  recommendations,
        "price_range":      price_range,
        "ai_summary":       ai_summary,
        "url":              f"/listing/{listing_id}",
        "edit_url":         f"/listing/{listing_id}/edit",
    }
    _cache_set(_LISTING_CACHE, cache_key, result)
    return result


def _build_listing_recommendations(listing, quality: dict, photo_count: int) -> list[dict]:
    """Build recommendation list with reason. Deterministic, no AI."""
    recs = []
    missing = quality["missing"]

    if photo_count == 0:
        recs.append({"rec": "Add at least one photo.", "reason": "This listing has no photos. Listings with photos receive significantly more views."})
    elif photo_count < 4:
        recs.append({"rec": "Add more photos.", "reason": f"This listing has {photo_count} photo{'s' if photo_count != 1 else ''}. Listings with 4+ photos tend to attract more buyers."})

    if not _has_video(listing):
        recs.append({"rec": "Add a video.", "reason": "A short video helps buyers understand the item better."})

    if "description" in missing:
        if len(listing.description or "") == 0:
            recs.append({"rec": "Add a description.", "reason": "This listing has no description."})
        else:
            recs.append({"rec": "Expand the description.", "reason": f"The description is short ({len(listing.description or '')} characters). More detail helps buyers feel confident."})

    if not listing.condition:
        recs.append({"rec": "Specify the item condition.", "reason": "Buyers often filter by condition."})

    if not listing.delivery_option:
        recs.append({"rec": "Consider enabling JHE Haul delivery.", "reason": "Listings with delivery options often attract more buyers."})

    if listing.listing_type == "vehicle":
        if not listing.vehicle_mileage:
            recs.append({"rec": "Add vehicle mileage.", "reason": "Mileage is a top factor for vehicle buyers."})
        if not listing.vehicle_make or not listing.vehicle_model:
            recs.append({"rec": "Complete vehicle details (make, model, year).", "reason": "Missing vehicle details reduce visibility in search."})

    if "price" in missing:
        recs.append({"rec": "Set a price.", "reason": "Listings without a price receive fewer inquiries."})

    if "location" in missing:
        recs.append({"rec": "Add a city or location.", "reason": "Buyers filter by location. Without it, your listing won't appear in area searches."})

    return recs[:5]   # top 5 recommendations


def _ai_listing_narrative(listing, quality, pending_offers, convos, age_days, photo_count) -> Optional[str]:
    """Call GPT-4o-mini to produce a 2-3 sentence natural-language listing summary.
    Returns None if OPENAI_API_KEY is not set or call fails — always graceful.
    """
    import os
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    try:
        from openai import OpenAI
        client = OpenAI(timeout=10.0)
        prompt = (
            f"You are a helpful seller coach for JHE Haul marketplace. "
            f"Given the following listing data, write 2-3 concise, honest sentences that summarize "
            f"how this listing is performing and what the seller should focus on. "
            f"Never invent metrics. Never claim causation without evidence.\n\n"
            f"Listing: {listing.title}\n"
            f"Status: {listing.status}\n"
            f"Views: {listing.view_count or 0}\n"
            f"Pending offers: {pending_offers}\n"
            f"Buyer conversations: {convos}\n"
            f"Days active: {age_days}\n"
            f"Photos: {photo_count}\n"
            f"Quality score: {quality['score']}/10 ({quality['label']})\n"
            f"Missing: {', '.join(quality['missing']) if quality['missing'] else 'nothing major'}\n"
        )
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("_ai_listing_narrative failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Seller overview
# ---------------------------------------------------------------------------

def get_seller_overview(seller_id: str) -> dict:
    """Return full seller intelligence overview.
    All metrics computed from real DB data. Ownership strictly enforced.
    """
    cached = _cache_get(_OVERVIEW_CACHE, seller_id)
    if cached:
        return cached

    from models import Listing, ListingOffer, ListingMessage, ListingConversation

    # Load seller's active listings
    try:
        active_listings = Listing.query.filter(
            Listing.seller_id == seller_id,
            Listing.status == "active",
            Listing.moderation_status == "approved",
        ).all()
    except Exception as e:
        log.error("get_seller_overview: query failed: %s", e)
        return {"error": "Could not load seller data."}

    all_listings = Listing.query.filter(Listing.seller_id == seller_id).all()
    total_views = sum(lst.view_count or 0 for lst in all_listings)
    sold_count  = sum(1 for lst in all_listings if lst.status == "sold")

    try:
        pending_offers = ListingOffer.query.filter(
            ListingOffer.seller_id == seller_id,
            ListingOffer.status == "pending",
        ).count()
    except Exception:
        pending_offers = 0

    try:
        unread_messages = (
            ListingMessage.query
            .join(ListingConversation, ListingMessage.conversation_id == ListingConversation.id)
            .filter(
                ListingConversation.seller_id == seller_id,
                ListingMessage.sender_id != seller_id,
                ListingMessage.read_at.is_(None),
            ).count()
        )
    except Exception:
        unread_messages = 0

    attention_signals  = detect_attention_signals(seller_id, active_listings)
    top_listings       = get_top_listings(active_listings)
    low_eng_listings   = get_low_engagement_listings(active_listings)
    listing_summaries  = [_safe_listing_summary(l) for l in active_listings]

    # Per-listing quality distribution
    quality_dist = {"Excellent": 0, "Good": 0, "Needs Improvement": 0}
    for s in listing_summaries:
        quality_dist[s["quality_label"]] += 1

    # AI narrative for overview — only if key set
    ai_narrative = _ai_overview_narrative(
        seller_id, active_listings, total_views, pending_offers, unread_messages,
        attention_signals, sold_count
    )

    result = {
        "seller_id":        seller_id,
        "active_count":     len(active_listings),
        "total_views":      total_views,
        "pending_offers":   pending_offers,
        "unread_messages":  unread_messages,
        "sold_count":       sold_count,
        "attention_signals": attention_signals,
        "top_listings":     top_listings,
        "low_eng_listings": low_eng_listings,
        "listing_summaries": listing_summaries,
        "quality_dist":     quality_dist,
        "ai_narrative":     ai_narrative,
        "generated_at":     datetime.utcnow().isoformat(),
    }
    _cache_set(_OVERVIEW_CACHE, seller_id, result)
    return result


def _ai_overview_narrative(seller_id, active_listings, total_views, pending_offers,
                            unread_messages, attention_signals, sold_count) -> Optional[str]:
    """1-2 sentence narrative for the overview panel. Graceful if no key."""
    import os
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    if not active_listings and sold_count == 0:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(timeout=10.0)
        top_signal_msgs = "; ".join(s["message"] for s in attention_signals[:3]) or "None"
        prompt = (
            "You are a helpful seller coach for JHE Haul marketplace. "
            "Write 1-2 concise, honest sentences summarizing this seller's current situation and top priority. "
            "Never invent data. Never claim causation without evidence.\n\n"
            f"Active listings: {len(active_listings)}\n"
            f"Total views across all listings: {total_views}\n"
            f"Pending offers: {pending_offers}\n"
            f"Unread buyer messages: {unread_messages}\n"
            f"Items sold: {sold_count}\n"
            f"Top attention signals: {top_signal_msgs}\n"
        )
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("_ai_overview_narrative failed: %s", e)
        return None


def invalidate_seller_cache(seller_id: str) -> None:
    """Invalidate cached insights when seller data changes."""
    _OVERVIEW_CACHE.pop(seller_id, None)
    # Remove listing cache entries for this seller
    keys = [k for k in _LISTING_CACHE if k[1] == seller_id]  # type: ignore[index]
    for k in keys:
        _LISTING_CACHE.pop(k, None)
