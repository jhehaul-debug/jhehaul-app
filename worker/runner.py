#!/usr/bin/env python3
"""JHE Haul — Phase F background worker runner.

Usage:
    python -m worker.runner
    python -m worker.runner --worker-id worker-1 --poll 2

This process must run separately from the web app. It connects to the same
PostgreSQL database and claims QUEUED jobs using SELECT FOR UPDATE SKIP LOCKED
so multiple workers can run in parallel safely.

DigitalOcean deployment: configure as a Worker component in the same App,
pointing to the same database. Set all required environment variables
(OPENAI_API_KEY, SENDGRID_API_KEY, DATABASE_URL, etc.) on the Worker component.

Secrets are NEVER read from job payloads — they come from environment variables.
"""

import sys
import os
import time
import logging
import argparse
import uuid

# Allow: python -m worker.runner  (run from project root)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('jhe.worker.runner')

# How often to run maintenance tasks (seconds)
_STALE_RECOVERY_INTERVAL = 300   # 5 minutes
_CLEANUP_INTERVAL        = 3600  # 1 hour


def _dispatch(job):
    """Dispatch a claimed BackgroundJob to its registered handler.

    Raises on any failure so the caller can invoke queue.fail().
    """
    from worker.handlers import get_handler

    payload = job.payload()
    handler = get_handler(job.job_type)   # raises ValueError / NotImplementedError
    handler(payload)


def run(worker_id=None, poll_interval=3.0):
    """Main worker loop. Runs until the process is killed."""
    from app import app
    from worker.queue import claim_next, complete, fail, recover_stale_jobs, cleanup_old_jobs

    if not worker_id:
        worker_id = f"worker-{uuid.uuid4().hex[:8]}"

    log.info("JHE Haul worker started. worker_id=%s poll_interval=%.1fs", worker_id, poll_interval)

    last_stale_check   = 0.0
    last_cleanup_check = 0.0

    while True:
        now_ts = time.time()

        with app.app_context():

            # ── Stale-job recovery ──────────────────────────────────────────
            if now_ts - last_stale_check >= _STALE_RECOVERY_INTERVAL:
                try:
                    n = recover_stale_jobs()
                    if n:
                        log.info("Recovered %d stale PROCESSING jobs", n)
                except Exception as exc:
                    log.warning("Stale-job recovery error: %s", exc)
                last_stale_check = now_ts

            # ── Old-job cleanup ─────────────────────────────────────────────
            if now_ts - last_cleanup_check >= _CLEANUP_INTERVAL:
                try:
                    n = cleanup_old_jobs()
                    if n:
                        log.info("Cleaned up %d old job records", n)
                except Exception as exc:
                    log.warning("Old-job cleanup error: %s", exc)
                last_cleanup_check = now_ts

            # ── Claim next job ──────────────────────────────────────────────
            try:
                job = claim_next(worker_id)
            except Exception as exc:
                log.error("claim_next failed: %s — sleeping %ss", exc, poll_interval)
                time.sleep(poll_interval)
                continue

            if job is None:
                # Queue empty — back off
                time.sleep(poll_interval)
                continue

            # ── Execute ─────────────────────────────────────────────────────
            log.info(
                "Executing job id=%s type=%s priority=%s retry=%d",
                job.id, job.job_type, job.priority, job.retry_count,
            )
            t0 = time.monotonic()

            try:
                _dispatch(job)
                complete(job.id)
                elapsed = time.monotonic() - t0
                log.info("Completed job id=%s type=%s in %.2fs", job.id, job.job_type, elapsed)

            except (ValueError, NotImplementedError) as exc:
                # Permanent failure — no point retrying (misconfigured job type)
                elapsed = time.monotonic() - t0
                log.error(
                    "Permanent failure for job id=%s type=%s after %.2fs: %s",
                    job.id, job.job_type, elapsed, exc,
                )
                fail(job.id,
                     error_category=type(exc).__name__,
                     error_detail=str(exc))

            except Exception as exc:
                # Transient failure — retry with backoff
                elapsed = time.monotonic() - t0
                log.warning(
                    "Transient failure for job id=%s type=%s after %.2fs: %s: %s",
                    job.id, job.job_type, elapsed, type(exc).__name__, exc,
                )
                fail(job.id,
                     error_category=type(exc).__name__,
                     error_detail=str(exc))


def main():
    parser = argparse.ArgumentParser(description="JHE Haul background worker")
    parser.add_argument(
        '--worker-id', default=None,
        help="Unique worker identifier (auto-generated if omitted)",
    )
    parser.add_argument(
        '--poll', type=float, default=3.0,
        help="Seconds to sleep between queue polls when queue is empty (default: 3)",
    )
    args = parser.parse_args()
    run(worker_id=args.worker_id, poll_interval=args.poll)


if __name__ == '__main__':
    main()
