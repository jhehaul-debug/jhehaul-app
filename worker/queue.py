"""JHE Haul — Phase F job queue interface.

All queue operations go through this module. The web app calls `enqueue()`;
the worker calls `claim_next()`, `complete()`, and `fail()`.

Design principles:
  - Payloads contain ONLY safe identifiers (IDs, non-secret strings). Never
    put API keys, tokens, passwords, or full objects in a payload.
  - Idempotency keys prevent duplicate jobs when the same event fires twice.
  - Exponential backoff on retry: 30 s → 2 min → 5 min → 15 min.
  - Stale-job recovery resets PROCESSING jobs whose worker died.
  - Old completed/failed records are purged on a rolling retention schedule.
"""

import json
import uuid
import logging
from datetime import datetime, timedelta

log = logging.getLogger('jhe.queue')

# ── Priority constants (lower number = higher priority) ───────────────────────
HIGH   = 1
NORMAL = 2
LOW    = 3

# Retry backoff delays in seconds, indexed by retry_count (0-based).
_BACKOFF_SECONDS = [30, 120, 300, 900]

# Retention: how long to keep completed / failed job records.
_COMPLETED_RETAIN_DAYS = 7
_FAILED_RETAIN_DAYS    = 30


def enqueue(job_type, payload, *, priority=NORMAL, idempotency_key=None, max_retries=3):
    """Enqueue a background job. Returns the BackgroundJob instance.

    If ``idempotency_key`` is provided and a job with the same (job_type,
    idempotency_key) is already QUEUED / PROCESSING / RETRYING, the existing
    job is returned and no duplicate is created.

    ``payload`` must be a dict containing only safe identifiers — never secrets.
    """
    from models import db, BackgroundJob

    # ── Idempotency check ────────────────────────────────────────────────────
    if idempotency_key:
        existing = (
            BackgroundJob.query
            .filter_by(job_type=job_type, idempotency_key=idempotency_key)
            .filter(BackgroundJob.status.in_(['QUEUED', 'PROCESSING']))
            .first()
        )
        if existing:
            log.debug(
                "queue.enqueue: suppressed duplicate job_type=%s key=%s existing_id=%s",
                job_type, idempotency_key, existing.id,
            )
            return existing

    job = BackgroundJob(
        id              = str(uuid.uuid4()),
        job_type        = job_type,
        status          = 'QUEUED',
        priority        = priority,
        payload_json    = json.dumps(payload or {}),
        idempotency_key = idempotency_key,
        max_retries     = max_retries,
    )
    db.session.add(job)
    db.session.commit()
    log.info("queue.enqueue: job_type=%s id=%s priority=%s key=%s",
             job_type, job.id, priority, idempotency_key)
    return job


def claim_next(worker_id):
    """Claim the next eligible QUEUED job for this worker. Returns job or None.

    Uses SELECT FOR UPDATE SKIP LOCKED on PostgreSQL for race-condition-safe
    claiming across multiple worker processes. Falls back gracefully on SQLite
    (local dev) where SKIP LOCKED is unavailable.
    """
    from models import db, BackgroundJob
    from sqlalchemy import or_

    now = datetime.now()

    try:
        job = (
            BackgroundJob.query
            .filter(
                BackgroundJob.status == 'QUEUED',
                or_(
                    BackgroundJob.next_retry_after.is_(None),
                    BackgroundJob.next_retry_after <= now,
                ),
            )
            .order_by(BackgroundJob.priority, BackgroundJob.created_at)
            .with_for_update(skip_locked=True)
            .first()
        )
    except Exception:
        # SQLite / DB without SKIP LOCKED support — simple first-come first-served
        job = (
            BackgroundJob.query
            .filter(
                BackgroundJob.status == 'QUEUED',
                or_(
                    BackgroundJob.next_retry_after.is_(None),
                    BackgroundJob.next_retry_after <= now,
                ),
            )
            .order_by(BackgroundJob.priority, BackgroundJob.created_at)
            .first()
        )

    if not job:
        return None

    job.status     = 'PROCESSING'
    job.started_at = now
    job.worker_id  = worker_id
    job.claimed_at = now
    db.session.commit()
    log.info("queue.claim_next: claimed job_type=%s id=%s retry=%d worker=%s",
             job.job_type, job.id, job.retry_count, worker_id)
    return job


def complete(job_id):
    """Mark a job as successfully completed."""
    from models import db, BackgroundJob

    job = BackgroundJob.query.get(job_id)
    if not job:
        return
    job.status       = 'COMPLETED'
    job.completed_at = datetime.now()
    db.session.commit()
    log.info("queue.complete: id=%s type=%s", job_id, job.job_type)


def fail(job_id, error_category='UNKNOWN', error_detail=''):
    """Record a job failure. Re-queues with exponential backoff if retries remain;
    otherwise marks the job FAILED permanently.
    """
    from models import db, BackgroundJob

    job = BackgroundJob.query.get(job_id)
    if not job:
        return

    # Store safe error metadata (truncated — no stack traces or secrets exposed)
    job.error_category = str(error_category)[:64]
    job.error_detail   = str(error_detail)[:500]

    if job.retry_count < job.max_retries:
        delay_s              = _BACKOFF_SECONDS[min(job.retry_count, len(_BACKOFF_SECONDS) - 1)]
        job.retry_count     += 1
        job.status           = 'QUEUED'
        job.next_retry_after = datetime.now() + timedelta(seconds=delay_s)
        job.started_at       = None
        job.worker_id        = None
        log.warning(
            "queue.fail: id=%s retrying (attempt %d/%d) after %ds — %s: %s",
            job_id, job.retry_count, job.max_retries, delay_s,
            error_category, str(error_detail)[:120],
        )
    else:
        job.status       = 'FAILED'
        job.completed_at = datetime.now()
        log.error(
            "queue.fail: id=%s PERMANENTLY FAILED after %d retries — %s: %s",
            job_id, job.retry_count, error_category, str(error_detail)[:120],
        )

    db.session.commit()


def cancel(job_id):
    """Cancel a QUEUED job (no-op if already PROCESSING or terminal)."""
    from models import db, BackgroundJob

    job = BackgroundJob.query.get(job_id)
    if job and job.status == 'QUEUED':
        job.status       = 'CANCELLED'
        job.completed_at = datetime.now()
        db.session.commit()
        log.info("queue.cancel: id=%s", job_id)


def recover_stale_jobs(stale_after_minutes=15):
    """Reset PROCESSING jobs whose worker appears to have died.

    A job is considered stale when it has been in PROCESSING for longer than
    ``stale_after_minutes``. The job is re-queued and its retry counter is NOT
    incremented (the worker death is not the job's fault).
    """
    from models import db, BackgroundJob

    cutoff = datetime.now() - timedelta(minutes=stale_after_minutes)
    stale  = (
        BackgroundJob.query
        .filter(
            BackgroundJob.status    == 'PROCESSING',
            BackgroundJob.claimed_at < cutoff,
        )
        .all()
    )
    for job in stale:
        log.warning(
            "queue.recover_stale: resetting id=%s type=%s claimed_at=%s worker=%s",
            job.id, job.job_type, job.claimed_at, job.worker_id,
        )
        job.status     = 'QUEUED'
        job.worker_id  = None
        job.claimed_at = None
        job.started_at = None

    if stale:
        db.session.commit()

    return len(stale)


def cleanup_old_jobs():
    """Delete job records beyond their retention window.

    Completed jobs: kept for _COMPLETED_RETAIN_DAYS days.
    Failed / cancelled jobs: kept for _FAILED_RETAIN_DAYS days.
    Returns total rows deleted.
    """
    from models import db, BackgroundJob

    now     = datetime.now()
    deleted = 0

    n = (
        BackgroundJob.query
        .filter(
            BackgroundJob.status == 'COMPLETED',
            BackgroundJob.completed_at < now - timedelta(days=_COMPLETED_RETAIN_DAYS),
        )
        .delete(synchronize_session=False)
    )
    deleted += n

    n = (
        BackgroundJob.query
        .filter(
            BackgroundJob.status.in_(['FAILED', 'CANCELLED']),
            BackgroundJob.completed_at < now - timedelta(days=_FAILED_RETAIN_DAYS),
        )
        .delete(synchronize_session=False)
    )
    deleted += n

    if deleted:
        db.session.commit()
        log.info("queue.cleanup_old_jobs: deleted %d old records", deleted)

    return deleted


def stats():
    """Return a dict of job counts per status. Safe for admin display."""
    from models import BackgroundJob
    from sqlalchemy import func

    rows = (
        BackgroundJob.query
        .with_entities(BackgroundJob.status, func.count(BackgroundJob.id))
        .group_by(BackgroundJob.status)
        .all()
    )
    counts = {s: 0 for s in ('QUEUED', 'PROCESSING', 'COMPLETED', 'FAILED', 'CANCELLED')}
    for status, cnt in rows:
        counts[status] = cnt
    return counts
