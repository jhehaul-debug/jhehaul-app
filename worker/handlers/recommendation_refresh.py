"""RECOMMENDATION_REFRESH job handler — Phase K.

Triggered by:
  • Listing published / re-approved  (idempotency_key: "rec-{listing_id}-{ts}")
  • User saves a listing             (idempotency_key: "rec-user-{user_id}-{ts}")

Current tasks performed in the background:
  1. Saved-search in-app notification — when a new listing matches a buyer's
     saved search (alerts_on=True), create an in-app Notification so the
     buyer sees it the next time they open the site, in addition to the
     email already sent by SAVED_SEARCH_MATCH.
  2. Recommendation cache invalidation — evict stale per-user caches so
     the next page load returns fresh rankings.

Phase K note: actual re-ranking / embedding generation is deferred.
The worker currently focuses on cache busting and in-app notifications.
"""

import json
import logging
from datetime import datetime

log = logging.getLogger('jhe.worker.recommendation_refresh')


def handle(payload):
    """Process a RECOMMENDATION_REFRESH job."""
    from models import db, Listing, SavedSearch, User, Notification
    from ai.recommendations import _cache_invalidate

    listing_id = payload.get('listing_id')
    user_id    = payload.get('user_id')     # optional: bust a single user's cache

    # ── 1. Per-user cache bust ────────────────────────────────────────────
    if user_id:
        _cache_invalidate(str(user_id))
        log.info("recommendation_refresh: cache busted for user=%s", user_id)

    # ── 2. In-app saved-search notifications for a new listing ────────────
    if listing_id:
        listing = Listing.query.get(listing_id)
        if not listing or listing.status != 'active' or not listing.is_approved:
            log.info("recommendation_refresh: listing %s not active/approved — skip", listing_id)
            return

        # Avoid import cycle — import matching logic from worker
        try:
            from worker.handlers.saved_search_match import _matches
        except ImportError:
            log.warning("recommendation_refresh: could not import _matches")
            return

        saved_searches = (
            SavedSearch.query
            .filter_by(alerts_on=True)
            .filter(SavedSearch.filters_json.isnot(None))
            .all()
        )

        notified = 0
        for ss in saved_searches:
            if str(ss.user_id) == str(listing.seller_id):
                continue
            try:
                filters = json.loads(ss.filters_json or '{}')
            except Exception:
                continue
            if not _matches(listing, filters):
                continue

            buyer = User.query.get(ss.user_id) if ss.user_id else None
            if not buyer:
                continue

            # Check user's notification preference
            if not getattr(buyer, 'personalization_enabled', True):
                continue

            # Avoid duplicate in-app notifications: check for recent identical one
            from sqlalchemy import and_
            existing = (Notification.query
                        .filter(
                            Notification.user_id == buyer.id,
                            Notification.type == 'saved_search_match',
                            Notification.related_listing_id == listing.id,
                        )
                        .first())
            if existing:
                continue

            try:
                notif = Notification(
                    user_id=buyer.id,
                    type='saved_search_match',
                    title='New match for your saved search',
                    message=(
                        f"{listing.title}"
                        + (f" — ${listing.price:,.0f}" if listing.price else '')
                        + (f" in {listing.city}" if listing.city else '')
                    ),
                    action_url=f'/listing/{listing.id}',
                    related_listing_id=listing.id,
                )
                db.session.add(notif)
                notified += 1
            except Exception as exc:
                log.warning("recommendation_refresh: notif failed for user %s: %s",
                            buyer.id, exc)

        if notified:
            db.session.commit()

        log.info("recommendation_refresh: listing=%s in-app notifications=%d checks=%d",
                 listing_id, notified, len(saved_searches))
