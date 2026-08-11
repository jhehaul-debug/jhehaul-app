"""
draft_cleanup.py — background daemon thread that purges abandoned draft listings.

A draft is considered abandoned when ALL of the following are true:
  • status == 'draft'
  • title is empty (None or '')
  • no ListingPhoto rows exist for it
  • created_at is older than DRAFT_MAX_AGE_HOURS

The thread runs once at startup (after a short delay) and then every
CLEANUP_INTERVAL seconds.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

DRAFT_MAX_AGE_HOURS = 48          # delete drafts older than this
CLEANUP_INTERVAL    = 6 * 60 * 60 # run every 6 hours


def purge_abandoned_drafts(app):
    """
    Delete abandoned draft listings inside an app context.
    Returns the number of listings deleted.
    """
    with app.app_context():
        from models import db, Listing, ListingPhoto

        cutoff = datetime.now() - timedelta(hours=DRAFT_MAX_AGE_HOURS)

        # Find draft listings older than the cutoff with no title
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
            photo_count = ListingPhoto.query.filter_by(listing_id=listing.id).count()
            if photo_count > 0:
                # Seller started uploading photos — keep the draft
                continue
            try:
                db.session.delete(listing)
                db.session.commit()
                deleted += 1
                log.info("Purged abandoned draft listing #%s (created %s)", listing.id, listing.created_at)
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
                purge_abandoned_drafts(app)
            except Exception as exc:
                log.error("Draft cleanup loop error: %s", exc)
            time.sleep(CLEANUP_INTERVAL)

    t = threading.Thread(target=_loop, daemon=True, name="draft-cleanup")
    t.start()
    log.info(
        "Draft cleanup background thread started (interval=%ds, max_age=%dh)",
        CLEANUP_INTERVAL,
        DRAFT_MAX_AGE_HOURS,
    )
