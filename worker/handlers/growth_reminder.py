"""GROWTH_REMINDER job handler — Phase M Growth Automation.

Handles scheduled batch reminder checks. Enqueued by growth_automation.py.

Payload schema:
    { "check_type": "<type>" }

Supported check_types:
    unread_message_remind  — remind users with unread messages > 24 h
    offer_remind           — remind sellers/buyers about pending offers > 24 h
    listing_expiry_remind  — remind sellers whose listing expires in ≤ 3 days
    relist_remind          — remind sellers whose listing expired ≤ 7 days ago
    seller_insight         — send milestone alerts (50/100/250 views)

All checks:
  • Respect per-user notification preferences
  • Deduplicate using Notification.dedup_key
  • Never block web requests (run in background)
  • Never send SMS
"""
import logging
from datetime import datetime, timedelta

log = logging.getLogger('jhe.worker.growth_reminder')

# ── Cooldown windows ──────────────────────────────────────────────────────────
# A new dedup_key is only generated after this many days, preventing re-alerts.
_COOLDOWN = {
    'unread_message_remind': 2,   # at most once per 2 days per conversation
    'offer_remind_seller':   2,   # at most once per 2 days per listing
    'offer_remind_buyer':    2,   # at most once per 2 days per offer
    'listing_expiry_remind': 1,   # one per expiry window
    'relist_remind':         3,   # at most once per 3 days per expired listing
    'seller_insight':        7,   # one milestone per listing per 7-day window
}


def handle(payload):
    check_type = payload.get('check_type', '')
    dispatch = {
        'unread_message_remind': _check_unread_messages,
        'offer_remind':          _check_offer_reminders,
        'listing_expiry_remind': _check_expiry_reminders,
        'relist_remind':         _check_relist_reminders,
        'seller_insight':        _check_seller_insight,
    }
    fn = dispatch.get(check_type)
    if fn is None:
        raise ValueError(f"GROWTH_REMINDER: unknown check_type={check_type!r}")
    fn()


# ── Unread message reminders ──────────────────────────────────────────────────

def _check_unread_messages():
    """Remind users who have unread messages older than 24 h (at most once per 2 days)."""
    from app import app, db
    from models import ListingMessage, ListingConversation, Listing, User, Notification

    cutoff_24h = datetime.now() - timedelta(hours=24)
    cooldown_days = _COOLDOWN['unread_message_remind']

    with app.app_context():
        # Find conversations with unread messages sent > 24 h ago
        convos = (
            ListingConversation.query
            .join(ListingMessage,
                  ListingMessage.conversation_id == ListingConversation.id)
            .filter(
                ListingMessage.is_read == False,
                ListingMessage.created_at <= cutoff_24h,
            )
            .with_entities(
                ListingConversation.id,
                ListingConversation.listing_id,
                ListingConversation.buyer_id,
            )
            .distinct()
            .all()
        )

        sent = 0
        for convo_id, listing_id, buyer_id in convos:
            listing = Listing.query.get(listing_id)
            if not listing or listing.status in ('deleted', 'removed'):
                continue

            safe_title = (listing.title or f"Listing #{listing_id}")[:50]

            # Find who has unread messages in this conversation
            unread_msgs = (
                ListingMessage.query
                .filter_by(conversation_id=convo_id, is_read=False)
                .filter(ListingMessage.created_at <= cutoff_24h)
                .all()
            )

            recipients_notified = set()
            for msg in unread_msgs:
                # The recipient is whoever did NOT send the message
                recipient_id = (str(listing.seller_id)
                                if str(msg.sender_id) == str(buyer_id)
                                else str(buyer_id))
                if recipient_id in recipients_notified:
                    continue

                user = User.query.get(recipient_id)
                if not user or not getattr(user, 'notify_offer_reminder', True):
                    continue

                dedup_window = (datetime.now() - timedelta(
                    days=datetime.now().toordinal() % cooldown_days
                )).strftime('%Y-W%W')
                dedup_key = f"unread_msg:{convo_id}:{recipient_id}:{dedup_window}"

                existing = Notification.query.filter_by(
                    user_id=recipient_id, dedup_key=dedup_key
                ).first()
                if existing:
                    continue

                try:
                    n = Notification(
                        user_id=recipient_id,
                        type='unread_message_reminder',
                        title=f'Unread message about "{safe_title}"',
                        message="You have an unread message. Reply to keep the conversation going.",
                        action_url=f"/listing/{listing_id}/message/{convo_id}",
                        related_listing_id=listing_id,
                        related_conversation_id=convo_id,
                        dedup_key=dedup_key,
                    )
                    db.session.add(n)
                    db.session.commit()
                    recipients_notified.add(recipient_id)
                    sent += 1
                except Exception as exc:
                    db.session.rollback()
                    log.error("unread_message_remind: failed for user=%s: %s", recipient_id, exc)

        log.info("unread_message_remind: sent=%d reminders", sent)


# ── Offer reminders ───────────────────────────────────────────────────────────

def _check_offer_reminders():
    """Remind sellers and buyers about pending offers older than 24 h."""
    from app import app, db
    from models import ListingOffer, Listing, User, Notification

    cutoff_24h = datetime.now() - timedelta(hours=24)
    cooldown_days = _COOLDOWN['offer_remind_seller']

    with app.app_context():
        pending_offers = (
            ListingOffer.query
            .filter(
                ListingOffer.status.in_(['pending', 'countered']),
                ListingOffer.created_at <= cutoff_24h,
            )
            .all()
        )

        sent = 0
        notified_sellers = {}  # seller_id → {listing_id: count}

        for offer in pending_offers:
            listing = Listing.query.get(offer.listing_id)
            if not listing or listing.status in ('sold', 'deleted', 'removed', 'expired'):
                continue

            safe_title = (listing.title or f"Listing #{offer.listing_id}")[:50]
            week_str = datetime.now().strftime('%Y-W%W')

            # ── Seller reminder ───────────────────────────────────────────────
            seller_id = str(listing.seller_id)
            seller = User.query.get(seller_id)
            if seller and getattr(seller, 'notify_offer_reminder', True):
                dedup_key = f"offer_remind_seller:{offer.listing_id}:{week_str}"
                if not Notification.query.filter_by(
                    user_id=seller_id, dedup_key=dedup_key
                ).first():
                    # Count all pending offers for this listing
                    pending_count = ListingOffer.query.filter(
                        ListingOffer.listing_id == offer.listing_id,
                        ListingOffer.status.in_(['pending', 'countered'])
                    ).count()

                    try:
                        n = Notification(
                            user_id=seller_id,
                            type='offer_reminder',
                            title=(f"You have {pending_count} pending "
                                   f"offer{'s' if pending_count != 1 else ''}"),
                            message=f"Respond to offer{'s' if pending_count != 1 else ''} on \"{safe_title}\".",
                            action_url=f"/listing/{offer.listing_id}",
                            related_listing_id=offer.listing_id,
                            related_offer_id=offer.id,
                            dedup_key=dedup_key,
                        )
                        db.session.add(n)
                        db.session.commit()
                        sent += 1

                        # Queue seller email if enabled
                        if seller.email and getattr(seller, 'notify_email_offers', True):
                            try:
                                from worker.queue import enqueue, NORMAL
                                enqueue('EMAIL_NOTIFICATION', {
                                    'fn': 'notify_seller_pending_offers_reminder',
                                    'kwargs': {
                                        'seller_email':  seller.email,
                                        'listing_title': safe_title,
                                        'listing_id':    offer.listing_id,
                                        'offer_count':   pending_count,
                                    },
                                }, priority=NORMAL)
                            except Exception as exc:
                                log.warning("offer_remind seller email failed: %s", exc)
                    except Exception as exc:
                        db.session.rollback()
                        log.error("offer_remind seller failed: %s", exc)

            # ── Buyer reminder (if offer is still 'pending', not countered) ──
            if offer.status == 'pending':
                buyer_id = str(offer.buyer_id)
                buyer = User.query.get(buyer_id)
                if buyer and getattr(buyer, 'notify_offer_reminder', True):
                    dedup_key = f"offer_remind_buyer:{offer.id}:{week_str}"
                    if not Notification.query.filter_by(
                        user_id=buyer_id, dedup_key=dedup_key
                    ).first():
                        try:
                            n = Notification(
                                user_id=buyer_id,
                                type='offer_reminder',
                                title="Your offer is awaiting a response",
                                message=(f"Your ${offer.amount:,.0f} offer on \"{safe_title}\" "
                                         "hasn't been responded to yet."),
                                action_url=f"/listing/{offer.listing_id}",
                                related_listing_id=offer.listing_id,
                                related_offer_id=offer.id,
                                dedup_key=dedup_key,
                            )
                            db.session.add(n)
                            db.session.commit()
                            sent += 1
                        except Exception as exc:
                            db.session.rollback()
                            log.error("offer_remind buyer failed: %s", exc)

        log.info("offer_remind: sent=%d reminders across %d offers", sent, len(pending_offers))


# ── Listing expiry reminders ──────────────────────────────────────────────────

def _check_expiry_reminders():
    """Remind sellers whose active listing expires in ≤ 3 days."""
    from app import app, db
    from models import Listing, User, Notification

    now = datetime.now()
    window_3d = now + timedelta(days=3)

    with app.app_context():
        expiring = (
            Listing.query
            .filter(
                Listing.status.in_(['active', 'approved']),
                Listing.expires_at.isnot(None),
                Listing.expires_at > now,
                Listing.expires_at <= window_3d,
            )
            .all()
        )

        sent = 0
        for listing in expiring:
            seller_id = str(listing.seller_id)
            user = User.query.get(seller_id)
            if not user:
                continue
            if not getattr(user, 'notify_listing_expiry_reminder', True):
                continue

            days_left = max(1, int((listing.expires_at - now).total_seconds() / 86400))
            safe_title = (listing.title or f"Listing #{listing.id}")[:50]
            dedup_key = f"expiry_remind:{listing.id}:{listing.expires_at.strftime('%Y-%m-%d')}"

            if Notification.query.filter_by(
                user_id=seller_id, dedup_key=dedup_key
            ).first():
                continue

            try:
                n = Notification(
                    user_id=seller_id,
                    type='listing_expiry_reminder',
                    title=f"Your listing expires in {days_left} day{'s' if days_left != 1 else ''}",
                    message=f'"{safe_title}" will expire soon. Edit it to extend the deadline.',
                    action_url=f"/listing/{listing.id}/edit",
                    related_listing_id=listing.id,
                    dedup_key=dedup_key,
                )
                db.session.add(n)
                db.session.commit()
                sent += 1

                # Email if enabled (uses existing email_service function)
                if user.email and getattr(user, 'notify_email_listing_expiry', True):
                    try:
                        from worker.queue import enqueue, NORMAL
                        enqueue('EMAIL_NOTIFICATION', {
                            'fn': 'notify_seller_listing_expiring_soon',
                            'kwargs': {
                                'seller_email': user.email,
                                'listing_id':   listing.id,
                                'title':        safe_title,
                                'expires_at':   listing.expires_at.isoformat(),
                            },
                        }, priority=NORMAL)
                    except Exception as exc:
                        log.warning("expiry_remind email queue failed for listing=%s: %s",
                                    listing.id, exc)
            except Exception as exc:
                db.session.rollback()
                log.error("expiry_remind failed for listing=%s: %s", listing.id, exc)

        log.info("expiry_remind: sent=%d reminders out of %d expiring soon", sent, len(expiring))


# ── Relist reminders ──────────────────────────────────────────────────────────

def _check_relist_reminders():
    """Remind sellers whose listing expired in the past 7 days to relist."""
    from app import app, db
    from models import Listing, User, Notification

    now = datetime.now()
    window_start = now - timedelta(days=7)

    with app.app_context():
        expired = (
            Listing.query
            .filter(
                Listing.status == 'expired',
                Listing.expires_at.isnot(None),
                Listing.expires_at >= window_start,
                Listing.expires_at <= now,
            )
            .all()
        )

        sent = 0
        for listing in expired:
            seller_id = str(listing.seller_id)
            user = User.query.get(seller_id)
            if not user:
                continue
            if not getattr(user, 'notify_listing_expiry_reminder', True):
                continue

            safe_title = (listing.title or f"Listing #{listing.id}")[:50]
            cooldown_window = (now - timedelta(
                days=_COOLDOWN['relist_remind']
            )).strftime('%Y-%m-%d')
            dedup_key = f"relist_remind:{listing.id}:{cooldown_window}"

            if Notification.query.filter_by(
                user_id=seller_id, dedup_key=dedup_key
            ).first():
                continue

            try:
                n = Notification(
                    user_id=seller_id,
                    type='relist_reminder',
                    title=f'Relist "{safe_title[:35]}"?',
                    message="Your listing expired. Relist it to make it visible again.",
                    action_url=f"/listing/{listing.id}/edit",
                    related_listing_id=listing.id,
                    dedup_key=dedup_key,
                )
                db.session.add(n)
                db.session.commit()
                sent += 1

                if user.email and getattr(user, 'notify_email_listing_expiry', True):
                    try:
                        from worker.queue import enqueue, NORMAL
                        enqueue('EMAIL_NOTIFICATION', {
                            'fn': 'notify_relist_reminder_email',
                            'kwargs': {
                                'seller_email':  user.email,
                                'listing_title': safe_title,
                                'listing_id':    listing.id,
                            },
                        }, priority=NORMAL)
                    except Exception as exc:
                        log.warning("relist_remind email queue failed: %s", exc)
            except Exception as exc:
                db.session.rollback()
                log.error("relist_remind failed for listing=%s: %s", listing.id, exc)

        log.info("relist_remind: sent=%d reminders out of %d expired", sent, len(expired))


# ── Seller insight / milestone notifications ──────────────────────────────────

_VIEW_MILESTONES = [25, 50, 100, 250, 500]


def _check_seller_insight():
    """Send milestone view-count notifications to sellers (e.g. "50 views!")."""
    from app import app, db
    from models import Listing, ListingView, User, Notification
    from sqlalchemy import func

    with app.app_context():
        # Get all active listings that have views
        view_counts = (
            db.session.query(
                ListingView.listing_id,
                func.count(ListingView.id).label('view_count')
            )
            .join(Listing, Listing.id == ListingView.listing_id)
            .filter(Listing.status.in_(['active', 'approved']))
            .group_by(ListingView.listing_id)
            .all()
        )

        sent = 0
        for listing_id, view_count in view_counts:
            # Find the highest milestone this listing has crossed
            milestone = None
            for m in reversed(_VIEW_MILESTONES):
                if view_count >= m:
                    milestone = m
                    break
            if not milestone:
                continue

            listing = Listing.query.get(listing_id)
            if not listing:
                continue

            seller_id = str(listing.seller_id)
            user = User.query.get(seller_id)
            if not user:
                continue

            safe_title = (listing.title or f"Listing #{listing_id}")[:50]
            week_str = datetime.now().strftime('%Y-W%W')
            dedup_key = f"seller_insight:{listing_id}:views{milestone}:{week_str}"

            if Notification.query.filter_by(
                user_id=seller_id, dedup_key=dedup_key
            ).first():
                continue

            try:
                n = Notification(
                    user_id=seller_id,
                    type='seller_insight',
                    title=f'"{safe_title[:35]}" has {view_count:,} views!',
                    message=(f"Your listing hit {milestone} views. "
                             "Strong interest — consider responding to messages quickly."),
                    action_url=f"/listing/{listing_id}",
                    related_listing_id=listing_id,
                    dedup_key=dedup_key,
                )
                db.session.add(n)
                db.session.commit()
                sent += 1
            except Exception as exc:
                db.session.rollback()
                log.error("seller_insight failed for listing=%s: %s", listing_id, exc)

        log.info("seller_insight: sent=%d milestone notifications", sent)
