"""
notification_service.py — In-app notification helpers for JHE Haul Marketplace.

All public helpers are fire-and-forget: they catch their own exceptions so a
notification failure never breaks the calling request.
"""
from datetime import datetime
import json
import logging

_log = logging.getLogger(__name__)

# ── Category → type mapping (used by the notifications page filter) ────────────
NOTIF_CATEGORIES = {
    'messages': ['new_message'],
    'offers':   ['new_offer', 'offer_accepted', 'offer_declined',
                 'offer_countered', 'offer_expired', 'offer_withdrawn'],
    'listings': ['listing_expired', 'listing_removed',
                 'listing_sold', 'listing_reserved'],
    'delivery': ['delivery_request', 'delivery_quote_ready',
                 'delivery_status', 'delivery_accepted', 'delivery_declined'],
    'account':  ['admin_notice'],
}

# ── Icon map (also referenced from templates) ──────────────────────────────────
NOTIF_ICONS = {
    'new_message':          '💬',
    'new_offer':            '💰',
    'offer_accepted':       '✅',
    'offer_declined':       '❌',
    'offer_countered':      '🔄',
    'offer_expired':        '⏳',
    'offer_withdrawn':      '↩️',
    'listing_expired':      '⏰',
    'listing_removed':      '🚫',
    'listing_sold':         '🏷️',
    'listing_reserved':     '📌',
    'delivery_request':     '🚛',
    'delivery_quote_ready': '💵',
    'delivery_status':      '🚛',
    'delivery_accepted':    '✅',
    'delivery_declined':    '❌',
    'admin_notice':         '⚙️',
}


# ── Core create / read helpers ─────────────────────────────────────────────────

def create_notification(user_id, notif_type, title, message=None,
                        action_url=None, related_listing_id=None,
                        related_offer_id=None, related_conversation_id=None,
                        related_delivery_request_id=None, related_user_id=None,
                        metadata=None):
    """Persist one in-app notification. Returns the Notification or None on error."""
    try:
        from app import db
        from models import Notification
        n = Notification(
            user_id=str(user_id),
            type=notif_type,
            title=title,
            message=message,
            action_url=action_url,
            related_listing_id=related_listing_id,
            related_offer_id=related_offer_id,
            related_conversation_id=related_conversation_id,
            related_delivery_request_id=related_delivery_request_id,
            related_user_id=str(related_user_id) if related_user_id else None,
            metadata_json=json.dumps(metadata) if metadata else None,
        )
        db.session.add(n)
        db.session.commit()
        return n
    except Exception as exc:
        try:
            from app import db as _db
            _db.session.rollback()
        except Exception:
            pass
        _log.error("create_notification failed (type=%s user=%s): %s",
                   notif_type, user_id, exc)
        return None


def get_unread_count(user_id):
    """Return unread notification count for a user (0 on error)."""
    try:
        from models import Notification
        return Notification.query.filter_by(
            user_id=str(user_id), is_read=False
        ).count()
    except Exception:
        return 0


def mark_read(notification_id, user_id):
    """Mark one notification read. Verifies ownership. Returns Notification or None."""
    try:
        from app import db
        from models import Notification
        n = Notification.query.filter_by(
            id=notification_id, user_id=str(user_id)
        ).first()
        if n and not n.is_read:
            n.is_read = True
            n.read_at = datetime.now()
            db.session.commit()
        return n
    except Exception as exc:
        _log.error("mark_read failed: %s", exc)
        return None


def mark_all_read(user_id):
    """Mark every unread notification for a user as read."""
    try:
        from app import db
        from models import Notification
        Notification.query.filter_by(
            user_id=str(user_id), is_read=False
        ).update({'is_read': True, 'read_at': datetime.now()},
                 synchronize_session=False)
        db.session.commit()
    except Exception as exc:
        _log.error("mark_all_read failed: %s", exc)


# ── Marketplace event helpers ──────────────────────────────────────────────────

def notify_new_message(recipient_id, sender_name, listing_title,
                       listing_id, conversation_id):
    """Notify the other party in a listing conversation that a message arrived."""
    return create_notification(
        user_id=recipient_id,
        notif_type='new_message',
        title=f'New message about "{listing_title[:50]}"',
        message=f"{sender_name} sent you a message.",
        action_url=f"/listing/{listing_id}/message/{conversation_id}",
        related_listing_id=listing_id,
        related_conversation_id=conversation_id,
    )


def notify_new_offer(seller_id, buyer_name, amount, listing_title,
                     listing_id, offer_id):
    """Notify seller that a buyer submitted an offer."""
    return create_notification(
        user_id=seller_id,
        notif_type='new_offer',
        title=f"New offer: ${amount:,.0f}",
        message=f"{buyer_name} made an offer on \"{listing_title[:50]}\".",
        action_url=f"/listing/{listing_id}",
        related_listing_id=listing_id,
        related_offer_id=offer_id,
    )


def notify_offer_accepted(buyer_id, amount, listing_title, listing_id, offer_id):
    """Notify buyer that their offer was accepted."""
    return create_notification(
        user_id=buyer_id,
        notif_type='offer_accepted',
        title=f"Your ${amount:,.0f} offer was accepted!",
        message=f"Contact the seller to arrange pickup for \"{listing_title[:50]}\".",
        action_url=f"/listing/{listing_id}",
        related_listing_id=listing_id,
        related_offer_id=offer_id,
    )


def notify_offer_declined(buyer_id, listing_title, listing_id, offer_id):
    """Notify buyer that their offer was declined."""
    return create_notification(
        user_id=buyer_id,
        notif_type='offer_declined',
        title="Your offer was declined.",
        message=f"The seller declined your offer on \"{listing_title[:50]}\".",
        action_url=f"/listing/{listing_id}",
        related_listing_id=listing_id,
        related_offer_id=offer_id,
    )


def notify_offer_countered(buyer_id, counter_amount, listing_title,
                           listing_id, offer_id):
    """Notify buyer that the seller sent a counteroffer."""
    return create_notification(
        user_id=buyer_id,
        notif_type='offer_countered',
        title=f"Seller countered at ${counter_amount:,.0f}",
        message=f"Respond to the counteroffer on \"{listing_title[:50]}\".",
        action_url=f"/listing/{listing_id}",
        related_listing_id=listing_id,
        related_offer_id=offer_id,
    )


def notify_counter_accepted(seller_id, buyer_name, amount, listing_title,
                            listing_id, offer_id):
    """Notify seller that buyer accepted their counteroffer."""
    return create_notification(
        user_id=seller_id,
        notif_type='offer_accepted',
        title=f"{buyer_name} accepted your ${amount:,.0f} counteroffer!",
        message=f"Contact the buyer to arrange pickup for \"{listing_title[:50]}\".",
        action_url=f"/listing/{listing_id}",
        related_listing_id=listing_id,
        related_offer_id=offer_id,
    )


def notify_listing_expired(seller_id, listing_title, listing_id):
    """Notify seller that their listing expired."""
    return create_notification(
        user_id=seller_id,
        notif_type='listing_expired',
        title="Your listing has expired.",
        message=f"\"{listing_title[:60]}\" has expired. Renew it to keep it visible.",
        action_url=f"/listing/{listing_id}",
        related_listing_id=listing_id,
    )


def notify_listing_removed(seller_id, listing_title, listing_id, reason=None):
    """Notify seller that admin removed their listing."""
    msg = f"\"{listing_title[:60]}\" was removed by a moderator."
    if reason:
        msg += f" Reason: {reason}"
    return create_notification(
        user_id=seller_id,
        notif_type='listing_removed',
        title="Your listing was removed.",
        message=msg,
        action_url="/my-listings",
        related_listing_id=listing_id,
    )


def notify_delivery_request(admin_user_id, buyer_name, listing_title, dr_id):
    """Notify an admin user that a new delivery request was submitted."""
    return create_notification(
        user_id=admin_user_id,
        notif_type='delivery_request',
        title="New JHE Haul delivery request",
        message=f"{buyer_name} requested delivery for \"{listing_title[:50]}\".",
        action_url=f"/delivery/{dr_id}",
        related_delivery_request_id=dr_id,
    )


def notify_delivery_quote_ready(buyer_id, listing_title, dr_id, amount=None):
    """Notify buyer that admin entered a delivery quote."""
    amt_str = f" — ${amount:,.0f}" if amount else ""
    return create_notification(
        user_id=buyer_id,
        notif_type='delivery_quote_ready',
        title=f"Your JHE Haul delivery quote is ready{amt_str}",
        message=f"View and respond to your quote for \"{listing_title[:50]}\".",
        action_url=f"/delivery/{dr_id}",
        related_delivery_request_id=dr_id,
    )


def notify_delivery_status(buyer_id, status_label, listing_title, dr_id):
    """Notify buyer of a delivery status change."""
    return create_notification(
        user_id=buyer_id,
        notif_type='delivery_status',
        title=f"Delivery update: {status_label}",
        message=f"Status change for your delivery of \"{listing_title[:50]}\".",
        action_url=f"/delivery/{dr_id}",
        related_delivery_request_id=dr_id,
    )


def notify_admin_notice(user_id, title, message=None, action_url=None):
    """Send an admin-authored notice to any user."""
    return create_notification(
        user_id=user_id,
        notif_type='admin_notice',
        title=title,
        message=message,
        action_url=action_url,
    )


def notify_listing_reserved_to_watchers(listing_id, listing_title):
    """Send in-app notifications (and emails) to users watching a listing when it is marked Reserved.

    Watchers = users who saved/favorited the listing  +  buyers with an active conversation.
    Deduplicates so no buyer gets two notifications.
    Never raises — notification failure must not break the caller.
    """
    try:
        from models import ListingFavorite, ListingConversation, User
        from email_service import notify_buyer_listing_reserved

        safe_title = (listing_title or f"Listing #{listing_id}")[:60]
        action_url = f"/listing/{listing_id}"
        notified_ids: set = set()

        # ── Saved / favorited buyers ──────────────────────────────────────────
        favorites = (ListingFavorite.query
                     .filter_by(listing_id=listing_id)
                     .all())
        for fav in favorites:
            uid = str(fav.user_id)
            if uid in notified_ids:
                continue
            notified_ids.add(uid)
            create_notification(
                user_id=uid,
                notif_type='listing_reserved',
                title=f'"{safe_title}" is now Reserved',
                message="This item may still become available. Check back or contact the seller.",
                action_url=action_url,
                related_listing_id=listing_id,
            )
            # Also send an email if the user has one
            user = User.query.get(uid)
            if user and user.email:
                try:
                    notify_buyer_listing_reserved(user.email, safe_title, listing_id)
                except Exception as _email_err:
                    _log.warning("Reserved email failed for user %s: %s", uid, _email_err)

        # ── Buyers with active conversations ──────────────────────────────────
        convos = (ListingConversation.query
                  .filter_by(listing_id=listing_id)
                  .all())
        for convo in convos:
            uid = str(convo.buyer_id)
            if uid in notified_ids:
                continue
            notified_ids.add(uid)
            create_notification(
                user_id=uid,
                notif_type='listing_reserved',
                title=f'"{safe_title}" is now Reserved',
                message="This item may still become available. Check back or contact the seller.",
                action_url=action_url,
                related_listing_id=listing_id,
                related_conversation_id=convo.id,
            )
            user = User.query.get(uid)
            if user and user.email:
                try:
                    notify_buyer_listing_reserved(user.email, safe_title, listing_id)
                except Exception as _email_err:
                    _log.warning("Reserved email failed for user %s: %s", uid, _email_err)

        _log.info(
            "notify_listing_reserved_to_watchers: listing=%s notified=%d buyers",
            listing_id, len(notified_ids),
        )
    except Exception as exc:
        _log.error("notify_listing_reserved_to_watchers failed (listing=%s): %s",
                   listing_id, exc)
