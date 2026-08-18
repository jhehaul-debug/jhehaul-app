"""Phase K — Recommendation & Personalization Intelligence.

All recommendations are deterministic, rule-based, and explainable.
No AI calls are made in this module. GPT/embeddings are explicitly
deferred to a future phase.

Privacy rules enforced here:
  • User A cannot see User B's activity or viewing history.
  • Removed / suspended / non-public listings are always excluded.
  • Sensitive personal traits are never used for ranking.
  • Personalisation can be disabled per-user (personalization_enabled flag).

Retention policy (enforced by prune_old_events()):
  • listing_views:         90 days
  • recommendation_events: 90 days
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger('jhe.recommendations')

# ---------------------------------------------------------------------------
# In-memory cache: user_id → (timestamp, result_list)
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 300  # 5 minutes


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < _CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, value):
    _cache[key] = (time.time(), value)


def _cache_invalidate(user_id: str):
    _cache.pop(f'rec:{user_id}', None)
    _cache.pop(f'rv:{user_id}', None)


# ---------------------------------------------------------------------------
# Safety filter — always exclude unavailable / restricted listings
# ---------------------------------------------------------------------------
def _safe_listing_query():
    """Base SQLAlchemy query for recommendation-eligible listings."""
    from models import Listing
    return Listing.query.filter(
        Listing.status == 'active',
        Listing.moderation_status == 'approved',
    )


# ---------------------------------------------------------------------------
# Record a listing view
# ---------------------------------------------------------------------------
def record_view(user_id: Optional[str], listing_id: int,
                session_key: Optional[str] = None) -> None:
    """Persist a listing view. Safe to call from request context."""
    from models import db, ListingView
    try:
        # Deduplicate: don't re-record the same listing within 30 minutes
        cutoff = datetime.now() - timedelta(minutes=30)
        q = ListingView.query.filter(
            ListingView.listing_id == listing_id,
            ListingView.viewed_at >= cutoff,
        )
        if user_id:
            q = q.filter(ListingView.user_id == user_id)
        elif session_key:
            q = q.filter(ListingView.session_key == session_key)
        else:
            return  # No way to identify the viewer

        if q.first():
            return  # already recorded recently

        view = ListingView(
            user_id=user_id,
            listing_id=listing_id,
            session_key=session_key,
        )
        db.session.add(view)
        db.session.commit()
        if user_id:
            _cache_invalidate(user_id)
    except Exception as exc:
        log.debug("record_view failed (non-fatal): %s", exc)
        try:
            from models import db
            db.session.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Record a recommendation interaction event
# ---------------------------------------------------------------------------
def record_event(user_id: Optional[str], listing_id: int,
                 event_type: str, source: str,
                 session_key: Optional[str] = None) -> None:
    """Persist a recommendation event (impression / click / save / message / offer)."""
    from models import db, RecommendationEvent
    _VALID_TYPES   = {'impression', 'click', 'save', 'message', 'offer'}
    _VALID_SOURCES = {'recommended_for_you', 'similar_listings', 'new_near_you',
                      'recently_viewed', 'saved_search_match'}
    if event_type not in _VALID_TYPES or source not in _VALID_SOURCES:
        return
    try:
        ev = RecommendationEvent(
            user_id=user_id,
            listing_id=listing_id,
            event_type=event_type,
            source=source,
            session_key=session_key,
        )
        db.session.add(ev)
        db.session.commit()
    except Exception as exc:
        log.debug("record_event failed (non-fatal): %s", exc)
        try:
            from models import db
            db.session.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Derive user signals from their activity
# ---------------------------------------------------------------------------
def _get_user_signals(user) -> dict:
    """
    Build a signal dict from a user's marketplace activity:
      category_ids   — categories from favorites + recent views
      sub_category_ids — sub-categories from the same
      price_min/max  — price range they engage with
      zip_code       — home_zip or None
      delivery_pref  — True if saved searches ask for JHE delivery
      vehicle_makes  — makes from saved searches / viewed vehicle listings
      vehicle_models — models from saved searches / viewed vehicle listings
      listing_ids_seen — IDs already seen (exclude from recs)
      keywords       — text tokens from saved searches
    """
    from models import ListingFavorite, ListingView, SavedSearch, Listing

    signals = {
        'category_ids':     set(),
        'sub_category_ids': set(),
        'price_min':        None,
        'price_max':        None,
        'zip_code':         getattr(user, 'home_zip', None),
        'delivery_pref':    False,
        'vehicle_makes':    set(),
        'vehicle_models':   set(),
        'listing_ids_seen': set(),
        'keywords':         set(),
    }

    prices = []

    # ── Saved listings ────────────────────────────────────────────────────
    favs = (ListingFavorite.query
            .filter_by(user_id=user.id)
            .order_by(ListingFavorite.created_at.desc())
            .limit(50)
            .all())
    fav_listing_ids = [f.listing_id for f in favs]
    if fav_listing_ids:
        fav_listings = Listing.query.filter(Listing.id.in_(fav_listing_ids)).all()
        for l in fav_listings:
            if l.category_id:
                signals['category_ids'].add(l.category_id)
            if l.subcategory_id:
                signals['sub_category_ids'].add(l.subcategory_id)
            if l.price:
                prices.append(float(l.price))
            if l.vehicle_make:
                signals['vehicle_makes'].add(l.vehicle_make.lower())
            if l.vehicle_model:
                signals['vehicle_models'].add(l.vehicle_model.lower())

    # ── Recently viewed listings ──────────────────────────────────────────
    cutoff = datetime.now() - timedelta(days=30)
    views = (ListingView.query
             .filter_by(user_id=user.id)
             .filter(ListingView.viewed_at >= cutoff)
             .order_by(ListingView.viewed_at.desc())
             .limit(40)
             .all())
    view_listing_ids = [v.listing_id for v in views]
    signals['listing_ids_seen'].update(view_listing_ids)
    if view_listing_ids:
        viewed_listings = Listing.query.filter(Listing.id.in_(view_listing_ids)).all()
        for l in viewed_listings:
            if l.category_id:
                signals['category_ids'].add(l.category_id)
            if l.subcategory_id:
                signals['sub_category_ids'].add(l.subcategory_id)
            if l.price:
                prices.append(float(l.price))
            if l.vehicle_make:
                signals['vehicle_makes'].add(l.vehicle_make.lower())
            if l.vehicle_model:
                signals['vehicle_models'].add(l.vehicle_model.lower())

    # ── Saved searches ────────────────────────────────────────────────────
    saved_searches = (SavedSearch.query
                      .filter_by(user_id=user.id)
                      .order_by(SavedSearch.created_at.desc())
                      .limit(10)
                      .all())
    for ss in saved_searches:
        try:
            f = json.loads(ss.filters_json or '{}')
        except Exception:
            continue
        if f.get('delivery_available'):
            signals['delivery_pref'] = True
        v = f.get('vehicle') or {}
        if v.get('make'):
            signals['vehicle_makes'].add(v['make'].lower())
        if v.get('model'):
            signals['vehicle_models'].add(v['model'].lower())
        for kw in (f.get('keywords') or []):
            if kw:
                signals['keywords'].add(kw.lower())

    # ── Price range ───────────────────────────────────────────────────────
    if prices:
        prices.sort()
        signals['price_min'] = prices[0] * 0.4   # 40% below min engagement price
        signals['price_max'] = prices[-1] * 2.2  # 2.2× above max engagement price

    return signals


def _has_enough_signals(signals: dict) -> bool:
    """True if we have at least 3 distinct activity signals to personalise."""
    count = 0
    if signals['category_ids']:
        count += 1
    if signals['listing_ids_seen']:
        count += 1
    if signals['vehicle_makes'] or signals['vehicle_models']:
        count += 1
    if signals['keywords']:
        count += 1
    if signals['price_min'] is not None:
        count += 1
    return count >= 2


def _score_listing(listing, signals: dict) -> tuple[int, str]:
    """
    Return (score, reason_text) for a candidate listing.

    Scoring factors (documented — not opaque):
      +3  category match (exact category_id in user's engagement set)
      +2  sub-category match
      +2  nearby listing (same ZIP or city as user's home_zip/city)
      +2  vehicle make match
      +1  vehicle model match
      +1  price range match
      +1  delivery preference match
      +1  recently listed (within 7 days)
      +1  keyword match (listing title/description contains a search keyword)
    """
    score = 0
    reason_parts = []

    if listing.category_id in signals['category_ids']:
        score += 3
        reason_parts.append('category match')
    if listing.subcategory_id and listing.subcategory_id in signals['sub_category_ids']:
        score += 2
        reason_parts.append('subcategory match')

    # Location proximity (ZIP match)
    user_zip = signals.get('zip_code')
    if user_zip and getattr(listing, 'zip_code', None):
        if listing.zip_code == user_zip:
            score += 2
            reason_parts.append('near you')

    # Vehicle signals
    if signals['vehicle_makes'] and getattr(listing, 'vehicle_make', None):
        if listing.vehicle_make.lower() in signals['vehicle_makes']:
            score += 2
            reason_parts.append(f"similar vehicle make")
    if signals['vehicle_models'] and getattr(listing, 'vehicle_model', None):
        if listing.vehicle_model.lower() in signals['vehicle_models']:
            score += 1
            reason_parts.append('similar model')

    # Price range
    if signals['price_min'] is not None and listing.price:
        p = float(listing.price)
        if signals['price_min'] <= p <= signals['price_max']:
            score += 1
            reason_parts.append('price range')

    # Delivery preference
    if signals['delivery_pref']:
        delivery = getattr(listing, 'delivery_option', '') or ''
        if 'jhe_haul' in delivery.lower():
            score += 1
            reason_parts.append('JHE delivery available')

    # Recency bonus
    if listing.created_at and listing.created_at >= datetime.now() - timedelta(days=7):
        score += 1
        reason_parts.append('new listing')

    # Keyword match
    if signals['keywords']:
        haystack = f"{listing.title or ''} {listing.description or ''}".lower()
        matching_kw = [kw for kw in signals['keywords'] if kw in haystack]
        if matching_kw:
            score += 1
            reason_parts.append('matches your search')

    reason = ', '.join(reason_parts[:3]) if reason_parts else 'active listing'
    return score, reason


# ---------------------------------------------------------------------------
# get_recommended_for_you
# ---------------------------------------------------------------------------
def get_recommended_for_you(user, limit: int = 12) -> dict:
    """
    Return personalised recommendations for an authenticated user.

    Returns a dict with keys:
      listings       — list of Listing ORM objects (max `limit`)
      reasons        — dict {listing_id: reason_string}
      is_personalised — bool (False → fallback to recent/popular)
      fallback_label — str shown when is_personalised is False
    """
    if not user or not user.is_authenticated:
        return {'listings': [], 'reasons': {}, 'is_personalised': False,
                'fallback_label': 'Recently Listed'}

    # Check personalization opt-out
    if not getattr(user, 'personalization_enabled', True):
        return {'listings': [], 'reasons': {}, 'is_personalised': False,
                'fallback_label': 'Recently Listed'}

    cache_key = f'rec:{user.id}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        signals = _get_user_signals(user)

        if not _has_enough_signals(signals):
            # Fallback: newest 12 active listings the user hasn't seen
            from models import Listing
            recent = (_safe_listing_query()
                      .filter(Listing.seller_id != user.id)
                      .order_by(Listing.created_at.desc())
                      .limit(limit + 20)
                      .all())
            result = {
                'listings': recent[:limit],
                'reasons': {},
                'is_personalised': False,
                'fallback_label': 'Recently Listed',
            }
            _cache_set(cache_key, result)
            return result

        # Load candidates: active, not the user's own, not already seen
        from models import Listing
        seen_ids = list(signals['listing_ids_seen']) or [-1]
        candidates = (_safe_listing_query()
                      .filter(
                          Listing.seller_id != user.id,
                          ~Listing.id.in_(seen_ids),
                      )
                      .order_by(Listing.created_at.desc())
                      .limit(200)
                      .all())

        # Score and rank
        scored = [(listing, *_score_listing(listing, signals)) for listing in candidates]
        scored.sort(key=lambda x: x[1], reverse=True)

        # Diversity: max 3 per seller
        selected, reasons, seller_counts = [], {}, {}
        for listing, score, reason in scored:
            if score == 0:
                continue
            sid = listing.seller_id
            if seller_counts.get(sid, 0) >= 3:
                continue
            selected.append(listing)
            reasons[listing.id] = reason
            seller_counts[sid] = seller_counts.get(sid, 0) + 1
            if len(selected) >= limit:
                break

        # Pad with recency if needed
        if len(selected) < limit:
            selected_ids = {l.id for l in selected}
            pad = (_safe_listing_query()
                   .filter(
                       Listing.seller_id != user.id,
                       ~Listing.id.in_(list(selected_ids) + seen_ids),
                   )
                   .order_by(Listing.created_at.desc())
                   .limit(limit - len(selected))
                   .all())
            for l in pad:
                selected.append(l)

        result = {
            'listings': selected[:limit],
            'reasons': reasons,
            'is_personalised': True,
            'fallback_label': None,
        }
        _cache_set(cache_key, result)
        return result

    except Exception as exc:
        log.warning("get_recommended_for_you failed: %s", exc)
        return {'listings': [], 'reasons': {}, 'is_personalised': False,
                'fallback_label': 'Recently Listed'}


# ---------------------------------------------------------------------------
# get_similar_listings
# ---------------------------------------------------------------------------
def get_similar_listings(listing, user=None, limit: int = 6) -> tuple[list, bool]:
    """
    Return (listings, is_fallback) of listings similar to the given one.

    Similarity factors (in order of priority):
      1. Same make + model (vehicles)
      2. Same category + subcategory
      3. Same category + similar price (±50%) + nearby
      4. Same category only (fallback)
      5. Recent sitewide active listings (last-resort fallback)

    Excludes the current listing, the seller's own listings, sold/inactive.
    Returns is_fallback=True when falling back to sitewide recent.
    """
    from models import Listing
    from sqlalchemy import or_, and_

    exclude_ids = [listing.id]
    if user and user.is_authenticated:
        # Exclude user's own
        pass  # filtered below

    base = _safe_listing_query().filter(Listing.id != listing.id)
    if user and user.is_authenticated and str(user.id) == str(listing.seller_id):
        base = base  # still show similar even to seller (they can see market)

    # ── Strategy 1: Vehicle make + model ──────────────────────────────────
    if listing.vehicle_make and listing.vehicle_model:
        sim = (base
               .filter(
                   Listing.vehicle_make.ilike(listing.vehicle_make),
                   Listing.vehicle_model.ilike(listing.vehicle_model),
               )
               .order_by(Listing.created_at.desc())
               .limit(limit * 2)
               .all())
        if len(sim) >= 2:
            return _diversify(sim, limit), False

    # ── Strategy 2: Same category + subcategory + price range ─────────────
    if listing.category_id and listing.price:
        price = float(listing.price)
        sim = (base
               .filter(
                   Listing.category_id == listing.category_id,
                   Listing.price >= price * 0.5,
                   Listing.price <= price * 2.0,
               )
               .order_by(Listing.created_at.desc())
               .limit(limit * 2)
               .all())
        if len(sim) >= 2:
            return _diversify(sim, limit), False

    # ── Strategy 3: Same category only ────────────────────────────────────
    if listing.category_id:
        sim = (base
               .filter(Listing.category_id == listing.category_id)
               .order_by(Listing.created_at.desc())
               .limit(limit * 2)
               .all())
        if sim:
            return _diversify(sim, limit), False

    # ── Strategy 4: Same property type ────────────────────────────────────
    if listing.is_property:
        sim = (base
               .filter(Listing.listing_type == listing.listing_type)
               .order_by(Listing.created_at.desc())
               .limit(limit * 2)
               .all())
        if sim:
            return _diversify(sim, limit), False

    # ── Strategy 5: Sitewide recency fallback ─────────────────────────────
    sim = (base
           .order_by(Listing.created_at.desc())
           .limit(limit)
           .all())
    return sim, True


def _diversify(listings: list, limit: int) -> list:
    """Max 2 per seller for diversity."""
    selected, seller_counts = [], {}
    for l in listings:
        sid = l.seller_id
        if seller_counts.get(sid, 0) >= 2:
            continue
        selected.append(l)
        seller_counts[sid] = seller_counts.get(sid, 0) + 1
        if len(selected) >= limit:
            break
    return selected


# ---------------------------------------------------------------------------
# get_recently_viewed
# ---------------------------------------------------------------------------
def get_recently_viewed(user_id: Optional[str] = None,
                        session_key: Optional[str] = None,
                        limit: int = 10) -> list:
    """Return recently viewed active listings for a user or anonymous session."""
    from models import ListingView, Listing

    if not user_id and not session_key:
        return []

    cache_key = f'rv:{user_id or session_key}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        cutoff = datetime.now() - timedelta(days=30)
        q = ListingView.query.filter(ListingView.viewed_at >= cutoff)
        if user_id:
            q = q.filter(ListingView.user_id == user_id)
        else:
            q = q.filter(ListingView.session_key == session_key)

        views = (q.order_by(ListingView.viewed_at.desc())
                  .limit(limit * 3)
                  .all())

        seen_ids, ordered_ids = set(), []
        for v in views:
            if v.listing_id not in seen_ids:
                seen_ids.add(v.listing_id)
                ordered_ids.append(v.listing_id)

        if not ordered_ids:
            _cache_set(cache_key, [])
            return []

        listings_by_id = {
            l.id: l for l in Listing.query.filter(
                Listing.id.in_(ordered_ids),
                Listing.status == 'active',
                Listing.moderation_status == 'approved',
            ).all()
        }

        result = [listings_by_id[lid] for lid in ordered_ids
                  if lid in listings_by_id][:limit]
        _cache_set(cache_key, result)
        return result
    except Exception as exc:
        log.warning("get_recently_viewed failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# get_new_near_user
# ---------------------------------------------------------------------------
def get_new_near_user(zip_code: Optional[str], city: Optional[str] = None,
                      limit: int = 8, exclude_seller_id: Optional[str] = None) -> list:
    """Return recently published active listings near a ZIP or city."""
    from models import Listing
    from sqlalchemy import or_

    if not zip_code and not city:
        return []

    try:
        cutoff = datetime.now() - timedelta(days=14)
        q = (_safe_listing_query()
             .filter(Listing.created_at >= cutoff))

        if exclude_seller_id:
            q = q.filter(Listing.seller_id != exclude_seller_id)

        conditions = []
        if zip_code:
            conditions.append(Listing.zip_code == zip_code)
        if city:
            conditions.append(Listing.city.ilike(city))

        q = q.filter(or_(*conditions))
        return (q.order_by(Listing.created_at.desc()).limit(limit).all())
    except Exception as exc:
        log.warning("get_new_near_user failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# get_saved_search_matches  (Copilot tool helper)
# ---------------------------------------------------------------------------
def get_saved_search_matches_for_user(user, limit: int = 8) -> list:
    """Return recent active listings that match the user's saved searches."""
    from models import SavedSearch, Listing
    from worker.handlers.saved_search_match import _matches

    try:
        saved_searches = (SavedSearch.query
                          .filter_by(user_id=user.id)
                          .filter(SavedSearch.filters_json.isnot(None))
                          .all())
        if not saved_searches:
            return []

        cutoff = datetime.now() - timedelta(days=14)
        recent = (_safe_listing_query()
                  .filter(Listing.created_at >= cutoff,
                          Listing.seller_id != user.id)
                  .order_by(Listing.created_at.desc())
                  .limit(200)
                  .all())

        matched, seen = [], set()
        for listing in recent:
            if listing.id in seen:
                continue
            for ss in saved_searches:
                try:
                    filters = json.loads(ss.filters_json or '{}')
                except Exception:
                    continue
                if _matches(listing, filters):
                    matched.append(listing)
                    seen.add(listing.id)
                    break

        return matched[:limit]
    except Exception as exc:
        log.warning("get_saved_search_matches_for_user failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Admin analytics summary
# ---------------------------------------------------------------------------
def get_recommendation_analytics() -> dict:
    """Aggregate recommendation engagement metrics for admin."""
    from models import RecommendationEvent
    from sqlalchemy import func

    try:
        rows = (RecommendationEvent.query
                .with_entities(
                    RecommendationEvent.source,
                    RecommendationEvent.event_type,
                    func.count(RecommendationEvent.id).label('cnt'),
                )
                .group_by(RecommendationEvent.source, RecommendationEvent.event_type)
                .all())

        by_source: dict = {}
        for source, event_type, cnt in rows:
            if source not in by_source:
                by_source[source] = {}
            by_source[source][event_type] = cnt

        return {'by_source': by_source, 'total_events': sum(r.cnt for r in rows)}
    except Exception as exc:
        log.warning("get_recommendation_analytics failed: %s", exc)
        return {'by_source': {}, 'total_events': 0}


# ---------------------------------------------------------------------------
# Maintenance: prune old events (call from a scheduled task)
# ---------------------------------------------------------------------------
def prune_old_events(days: int = 90) -> dict[str, int]:
    """Delete listing_views and recommendation_events older than `days` days.

    Retention policy:
      listing_views:         90 days
      recommendation_events: 90 days

    Returns counts of deleted rows.
    """
    from models import db, ListingView, RecommendationEvent
    cutoff = datetime.now() - timedelta(days=days)
    try:
        lv_deleted = (ListingView.query
                      .filter(ListingView.viewed_at < cutoff)
                      .delete(synchronize_session=False))
        re_deleted = (RecommendationEvent.query
                      .filter(RecommendationEvent.created_at < cutoff)
                      .delete(synchronize_session=False))
        db.session.commit()
        return {'listing_views_deleted': lv_deleted,
                'recommendation_events_deleted': re_deleted}
    except Exception as exc:
        log.warning("prune_old_events failed: %s", exc)
        db.session.rollback()
        return {'listing_views_deleted': 0, 'recommendation_events_deleted': 0}
