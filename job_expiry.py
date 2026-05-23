"""
job_expiry.py — background daemon thread for job inactivity reminders and auto-expiration.

Inactivity clock: time since the latest bid was submitted on a job.
Only jobs in status ['open', 'bidding'] with at least one bid are processed.

Schedule:
  24 h after last bid → first reminder email to customer
  48 h after last bid → second reminder email (job expiring soon)
  72 h after last bid → job status set to 'expired'
"""

import logging
import threading
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

CHECK_INTERVAL = 30 * 60  # 30 minutes between checks


def _run_checks(app):
    """Run one full expiry check cycle inside an app context."""
    with app.app_context():
        from models import db, Job, Bid, User
        from email_service import (
            notify_customer_pending_bids_reminder,
            notify_customer_job_expiring_soon,
            notify_admin_job_expired,
        )

        now = datetime.now()
        cutoff_24h = now - timedelta(hours=24)
        cutoff_48h = now - timedelta(hours=48)
        cutoff_72h = now - timedelta(hours=72)

        jobs = Job.query.filter(Job.status.in_(['open', 'bidding'])).all()

        expired_count = 0
        r24_count = 0
        r48_count = 0

        for job in jobs:
            latest_bid = (Bid.query
                          .filter_by(job_id=job.id)
                          .order_by(Bid.created_at.desc())
                          .first())
            if not latest_bid:
                continue

            clock = latest_bid.created_at
            bid_count = Bid.query.filter_by(job_id=job.id).count()
            customer = User.query.get(job.customer_id) if job.customer_id else None
            customer_email = customer.email if customer else None

            try:
                # ── 72h → expire ────────────────────────────────────────────
                if clock <= cutoff_72h:
                    job.status = 'expired'
                    job.expired_at = now
                    db.session.commit()
                    expired_count += 1
                    log.info("Job #%s auto-expired (last bid: %s)", job.id, clock)
                    try:
                        notify_admin_job_expired(job.id, job.customer_name, bid_count)
                    except Exception as e:
                        log.error("Admin expired notify failed (job #%s): %s", job.id, e)
                    continue

                # ── 48h → second reminder ────────────────────────────────────
                if clock <= cutoff_48h and not job.reminder_48h_sent:
                    job.reminder_48h_sent = True
                    db.session.commit()
                    r48_count += 1
                    log.info("Job #%s — 48h reminder queued", job.id)
                    if customer_email:
                        try:
                            notify_customer_job_expiring_soon(customer_email, job.id)
                        except Exception as e:
                            log.error("48h reminder failed (job #%s): %s", job.id, e)
                    continue

                # ── 24h → first reminder ─────────────────────────────────────
                if clock <= cutoff_24h and not job.reminder_24h_sent:
                    job.reminder_24h_sent = True
                    db.session.commit()
                    r24_count += 1
                    log.info("Job #%s — 24h reminder queued", job.id)
                    if customer_email:
                        try:
                            notify_customer_pending_bids_reminder(customer_email, job.id, bid_count)
                        except Exception as e:
                            log.error("24h reminder failed (job #%s): %s", job.id, e)

            except Exception as e:
                log.error("Expiry error for job #%s: %s", job.id, e)
                db.session.rollback()

        if expired_count or r24_count or r48_count:
            log.info(
                "Expiry run: expired=%d  24h_reminders=%d  48h_reminders=%d",
                expired_count, r24_count, r48_count
            )


def start_expiry_thread(app):
    """Spawn a daemon background thread that runs expiry checks every 30 minutes."""
    def _loop():
        time.sleep(60)  # Let the app fully start before first run
        while True:
            try:
                _run_checks(app)
            except Exception as e:
                log.error("Expiry loop error: %s", e)
            time.sleep(CHECK_INTERVAL)

    t = threading.Thread(target=_loop, daemon=True, name="job-expiry")
    t.start()
    log.info("Job expiry background thread started (check every %ds)", CHECK_INTERVAL)
