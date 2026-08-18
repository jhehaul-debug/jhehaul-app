"""PRICE_DROP_NOTIFY job handler — Phase M Growth Automation.

Payload schema:
    {
        "listing_id": <int>,
        "old_price":  <float>,
        "new_price":  <float>
    }

Notifies every user who saved/favorited the listing that the price dropped.
Each user receives at most one in-app notification per (listing, price drop event).
Email is queued separately and respects notify_email_price_drop preference.
Never sends if price did not actually drop or if listing is not active/approved.
"""
import logging
from datetime import datetime, timedelta

log = logging.getLogger('jhe.worker.price_drop')


def handle(payload):
    from app import app, db
    from models import Listing, ListingFavorite, User, Notification

    listing_id = payload.get('listing_id')
    try:
        old_price = float(payload.get('old_price', 0))
        new_price = float(payload.get('new_price', 0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"PRICE_DROP_NOTIFY: invalid price payload: {exc}") from exc

    if not listing_id:
        raise ValueError("PRICE_DROP_NOTIFY: missing listing_id")
    if new_price >= old_price:
        log.info("PRICE_DROP_NOTIFY: listing=%s price did not drop (%.2f→%.2f), skipping",
                 listing_id, old_price, new_price)
        return

    with app.app_context():
        listing = Listing.query.get(listing_id)
        if not listing:
            raise ValueError(f"PRICE_DROP_NOTIFY: listing {listing_id} not found")
        if listing.status not in ('active', 'approved'):
            log.info("PRICE_DROP_NOTIFY: listing=%s status=%s, skipping", listing_id, listing.status)
            return
        if getattr(listing, 'is_draft', False):
            return

        safe_title = (listing.title or f"Listing #{listing_id}")[:60]
        action_url = f"/listing/{listing_id}"
        dedup_window = datetime.now().strftime('%Y-%m-%d')  # one alert per listing per day
        dedup_key = f"price_drop:{listing_id}:{dedup_window}"

        favorites = ListingFavorite.query.filter_by(listing_id=listing_id).all()
        notified = 0

        for fav in favorites:
            uid = str(fav.user_id)
            # Skip the seller themselves
            if listing.seller_id and str(listing.seller_id) == uid:
                continue

            user = User.query.get(uid)
            if not user:
                continue

            # Check in-app preference (default True)
            if not getattr(user, 'notify_price_drop', True):
                continue

            # Dedup: skip if already sent this dedup_key for this user today
            existing = Notification.query.filter_by(
                user_id=uid, dedup_key=dedup_key
            ).first()
            if existing:
                continue

            # Create in-app notification
            try:
                n = Notification(
                    user_id=uid,
                    type='price_drop',
                    title=f"Price dropped on \"{safe_title[:40]}\"",
                    message=(f"Price dropped from ${old_price:,.0f} to ${new_price:,.0f}. "
                             "Grab it before it's gone."),
                    action_url=action_url,
                    related_listing_id=listing_id,
                    dedup_key=dedup_key,
                )
                db.session.add(n)
                db.session.commit()
                notified += 1
            except Exception as exc:
                db.session.rollback()
                log.error("PRICE_DROP_NOTIFY: in-app failed for user=%s: %s", uid, exc)
                continue

            # Queue email if user has email prefs enabled
            if user.email and getattr(user, 'notify_email_price_drop', True):
                try:
                    from worker.queue import enqueue, NORMAL
                    enqueue('EMAIL_NOTIFICATION', {
                        'fn': 'notify_price_drop_alert',
                        'kwargs': {
                            'buyer_email':    user.email,
                            'listing_title':  safe_title,
                            'listing_id':     listing_id,
                            'old_price':      old_price,
                            'new_price':      new_price,
                        },
                    }, priority=NORMAL)
                except Exception as exc:
                    log.warning("PRICE_DROP_NOTIFY: email queue failed for user=%s: %s", uid, exc)

        log.info("PRICE_DROP_NOTIFY: listing=%s notified=%d/%d savers",
                 listing_id, notified, len(favorites))
