"""
draft_cleanup.py — background daemon thread that purges abandoned draft listings.

Since listing creation is now deferred (no DB row is created when a seller merely
visits /listing/new), the only abandoned drafts that can accumulate are listings
where the seller uploaded at least one photo during step 1 but never entered a
title in step 2.

A draft is considered abandoned when ALL of the following are true:
  • status == 'draft'
  • title is empty (None or '')
  • created_at is older than DRAFT_MAX_AGE_HOURS

The thread runs once at startup (after a short delay) and then every
CLEANUP_INTERVAL seconds.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

DRAFT_MAX_AGE_HOURS        = 48      # delete drafts older than this
REMINDER_MIN_HOURS         = 24      # start sending reminders once draft reaches this age
REMINDER_RECENCY_GRACE_HOURS = 6    # skip reminder if the draft was touched within this window
CLEANUP_INTERVAL           = 60 * 60  # run every hour (keeps reminder close to the 24h mark)


def send_draft_reminders(app):
    """
    Send a one-time reminder email to sellers whose untitled draft is at least
    REMINDER_MIN_HOURS old but not yet old enough to be purged.  The window is
    intentionally wide (24h → 48h) so that every 6-hour worker run has a chance
    to catch each draft before it is deleted.  The draft_reminder_sent flag
    prevents duplicate emails across multiple runs.
    Returns the number of reminder emails sent.
    """
    with app.app_context():
        from models import db, Listing
        from email_service import notify_seller_draft_expiring

        now = datetime.now()
        reminder_cutoff = now - timedelta(hours=REMINDER_MIN_HOURS)
        delete_cutoff   = now - timedelta(hours=DRAFT_MAX_AGE_HOURS)
        recency_cutoff  = now - timedelta(hours=REMINDER_RECENCY_GRACE_HOURS)

        # Eligible: old enough to warn (≥ 24h) but not yet deleted (< 48h), reminder
        # not yet sent, AND not touched recently.
        #
        # draft_activity_at tracks the last time the seller mutated any draft content
        # (photo upload/delete/reorder, video upload/delete, field edits).  Photo and
        # video endpoints only write to child rows (ListingPhoto / ListingVideo), so
        # Listing.updated_at would NOT reflect those changes — hence the dedicated column.
        #
        # NULL means the draft predates the column or was never touched after creation;
        # in that case we allow the reminder (the seller has not actively returned to it).
        candidates = (
            Listing.query
            .filter(
                Listing.status == 'draft',
                db.or_(Listing.title == None, Listing.title == ''),
                Listing.created_at <= reminder_cutoff,
                Listing.created_at > delete_cutoff,
                Listing.draft_reminder_sent == False,
                db.or_(
                    Listing.draft_activity_at == None,
                    Listing.draft_activity_at <= recency_cutoff,
                ),
            )
            .all()
        )

        sent = 0
        for listing in candidates:
            seller = listing.seller
            if not seller or not seller.email:
                log.warning("Draft reminder: listing #%s has no seller email, skipping", listing.id)
                continue
            try:
                # Atomically claim this listing with a full eligibility re-check in the
                # WHERE clause so that:
                #   (a) concurrent Gunicorn workers cannot both send the same email, and
                #   (b) a listing that was completed/activated after the SELECT is skipped.
                from sqlalchemy import text as _text
                claim = db.session.execute(
                    _text(
                        "UPDATE listings SET draft_reminder_sent = true "
                        "WHERE id = :lid "
                        "  AND draft_reminder_sent = false "
                        "  AND status = 'draft' "
                        "  AND (title IS NULL OR title = '') "
                        "  AND created_at <= :reminder_cutoff "
                        "  AND created_at > :delete_cutoff "
                        "  AND (draft_activity_at IS NULL OR draft_activity_at <= :recency_cutoff)"
                    ),
                    {
                        "lid": listing.id,
                        "reminder_cutoff": reminder_cutoff,
                        "delete_cutoff": delete_cutoff,
                        "recency_cutoff": recency_cutoff,
                    },
                )
                db.session.commit()
                if claim.rowcount == 0:
                    log.debug("Draft reminder: listing #%s already claimed or no longer eligible", listing.id)
                    continue

                # Send the email.  On any failure reset the flag so the next
                # hourly run can retry — this preserves deduplication while
                # keeping the notification reliable.
                ok = notify_seller_draft_expiring(seller.email, listing.id)
                if ok:
                    sent += 1
                    log.info("Draft reminder sent for listing #%s to %s", listing.id, seller.email)
                else:
                    db.session.execute(
                        _text("UPDATE listings SET draft_reminder_sent = false WHERE id = :lid"),
                        {"lid": listing.id},
                    )
                    db.session.commit()
                    log.warning(
                        "Draft reminder: SendGrid delivery failed for listing #%s — "
                        "flag reset, will retry next run",
                        listing.id,
                    )
            except Exception as exc:
                try:
                    db.session.rollback()
                    # Release the claim so the next run can retry
                    from sqlalchemy import text as _text
                    db.session.execute(
                        _text("UPDATE listings SET draft_reminder_sent = false WHERE id = :lid"),
                        {"lid": listing.id},
                    )
                    db.session.commit()
                except Exception:
                    pass
                log.error("Failed to send draft reminder for listing #%s: %s", listing.id, exc)

        if sent:
            log.info("Draft reminders: sent %d email(s)", sent)
        else:
            log.debug("Draft reminders: no drafts in reminder window")

        return sent


def purge_abandoned_drafts(app):
    """
    Delete abandoned draft listings inside an app context.
    Returns the number of listings deleted.
    """
    with app.app_context():
        from models import db, Listing, ListingPhoto

        cutoff = datetime.now() - timedelta(hours=DRAFT_MAX_AGE_HOURS)

        # Find draft listings older than the cutoff with no title.
        # These are listings where a photo was uploaded (which lazily created the
        # draft) but the seller never completed step 2 (title/details).
        candidates = (
            Listing.query
            .filter(
                Listing.status == 'draft',
                Listing.created_at < cutoff,
                db.or_(Listing.title == None, Listing.title == ''),
            )
            .all()
        )

        deleted = 0
        for listing in candidates:
            # Delete any associated photos from storage before removing the row
            photos = ListingPhoto.query.filter_by(listing_id=listing.id).all()
            try:
                for photo in photos:
                    try:
                        from storage import delete_file as _delete_file
                        _delete_file(photo.filename)
                    except Exception as exc:
                        log.warning("purge_abandoned_drafts: could not delete photo file %s: %s",
                                    photo.filename, exc)
                    db.session.delete(photo)
                db.session.delete(listing)
                db.session.commit()
                deleted += 1
                log.info("Purged abandoned draft listing #%s (created %s, %d photo(s))",
                         listing.id, listing.created_at, len(photos))
            except Exception as exc:
                db.session.rollback()
                log.error("Failed to delete draft listing #%s: %s", listing.id, exc)

        if deleted:
            log.info("Draft cleanup: purged %d abandoned draft(s)", deleted)
        else:
            log.debug("Draft cleanup: no abandoned drafts to purge")

        return deleted


def start_draft_cleanup_thread(app):
    """Spawn a daemon background thread that runs draft cleanup on a schedule."""

    def _loop():
        time.sleep(90)  # Let the app fully start before first run
        while True:
            try:
                send_draft_reminders(app)
            except Exception as exc:
                log.error("Draft reminder loop error: %s", exc)
            try:
                purge_abandoned_drafts(app)
            except Exception as exc:
                log.error("Draft cleanup loop error: %s", exc)
            time.sleep(CLEANUP_INTERVAL)

    t = threading.Thread(target=_loop, daemon=True, name="draft-cleanup")
    t.start()
    log.info(
        "Draft cleanup background thread started (interval=%ds, max_age=%dh, reminder_after=%dh)",
        CLEANUP_INTERVAL,
        DRAFT_MAX_AGE_HOURS,
        REMINDER_MIN_HOURS,
    )
