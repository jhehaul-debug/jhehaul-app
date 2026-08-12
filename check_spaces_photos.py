#!/usr/bin/env python3
"""
check_spaces_photos.py
──────────────────────
Post-migration smoke test: verify that every storage_url stored in the photo
tables returns HTTP 200 from the public internet.

Covers four tables:
  • job_photos
  • completion_photos
  • listing_photos
  • gallery_photos

Usage:
  python check_spaces_photos.py [--timeout SECONDS] [--workers N]

Exit codes:
  0  — all storage_urls reachable (or no rows to check)
  1  — one or more URLs returned a non-200 response or connection error
  2  — configuration / startup error

Flags reported per row:
  OK           HTTP 200
  BROKEN       Non-200 response
  ERROR        Network / timeout error
  NO_URL       storage_url is NULL (not yet migrated)
"""

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("check_spaces")

# ── CLI ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Smoke-test Spaces photo URLs")
parser.add_argument(
    "--timeout",
    type=float,
    default=10.0,
    metavar="SECONDS",
    help="Per-request HTTP timeout (default: 10 s)",
)
parser.add_argument(
    "--workers",
    type=int,
    default=10,
    metavar="N",
    help="Concurrent HTTP workers (default: 10)",
)
args = parser.parse_args()

# ── HTTP client ───────────────────────────────────────────────────────────────

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    log.error("requests is not installed. Run: pip install requests")
    sys.exit(2)

session = requests.Session()
retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry)
session.mount("https://", adapter)
session.mount("http://", adapter)


def check_url(table: str, row_id, url: str) -> dict:
    """Return a result dict for a single URL check."""
    try:
        resp = session.head(url, timeout=args.timeout, allow_redirects=True)
        if resp.status_code == 200:
            return {"table": table, "id": row_id, "url": url, "status": "OK", "code": 200}
        else:
            return {
                "table": table,
                "id": row_id,
                "url": url,
                "status": "BROKEN",
                "code": resp.status_code,
            }
    except Exception as exc:
        return {"table": table, "id": row_id, "url": url, "status": "ERROR", "error": str(exc)}


# ── Bootstrap Flask app context ───────────────────────────────────────────────

try:
    from app import app  # noqa: E402
    from models import db, JobPhoto, CompletionPhoto, ListingPhoto, GalleryPhoto  # noqa: E402
except Exception as exc:
    log.error("Failed to import Flask app or models: %s", exc)
    sys.exit(2)

# ── Gather rows ───────────────────────────────────────────────────────────────

TABLES = [
    ("job_photos", JobPhoto),
    ("completion_photos", CompletionPhoto),
    ("listing_photos", ListingPhoto),
    ("gallery_photos", GalleryPhoto),
]

counters = {
    "ok": 0,
    "broken": 0,
    "error": 0,
    "no_url": 0,
}

broken_rows: list[dict] = []

with app.app_context():
    jobs: list[tuple[str, int, str]] = []  # (table, id, url)
    no_url_rows: list[tuple[str, int]] = []

    for label, model in TABLES:
        total = model.query.count()
        with_url = model.query.filter(model.storage_url.isnot(None)).count()
        without_url = total - with_url
        log.info(
            "%-20s  total=%-5d  with_storage_url=%-5d  missing_url=%d",
            label, total, with_url, without_url,
        )

        # Collect rows without storage_url for reporting
        null_rows = (
            model.query.filter(model.storage_url.is_(None))
            .with_entities(model.id)
            .all()
        )
        for (row_id,) in null_rows:
            no_url_rows.append((label, row_id))

        # Collect rows with storage_url for HTTP check
        url_rows = (
            model.query.filter(model.storage_url.isnot(None))
            .with_entities(model.id, model.storage_url)
            .all()
        )
        for row_id, url in url_rows:
            jobs.append((label, row_id, url))

# ── Log NO_URL rows ───────────────────────────────────────────────────────────

for table, row_id in no_url_rows:
    log.warning("NO_URL  %-20s  id=%-6s  (not yet migrated to Spaces)", table, row_id)
    counters["no_url"] += 1

# ── HTTP checks (concurrent) ──────────────────────────────────────────────────

if not jobs:
    log.info("No rows with storage_url to check.")
else:
    log.info("")
    log.info("Checking %d URL(s) with %d worker(s) …", len(jobs), args.workers)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(check_url, table, row_id, url): (table, row_id, url)
            for table, row_id, url in jobs
        }
        for future in as_completed(futures):
            result = future.result()
            status = result["status"]
            counters[status.lower()] = counters.get(status.lower(), 0) + 1

            if status == "OK":
                log.debug("OK      %-20s  id=%-6s  %s", result["table"], result["id"], result["url"])
            elif status == "BROKEN":
                log.error(
                    "BROKEN  %-20s  id=%-6s  HTTP %s  %s",
                    result["table"], result["id"], result["code"], result["url"],
                )
                broken_rows.append(result)
            else:  # ERROR
                log.error(
                    "ERROR   %-20s  id=%-6s  %s  %s",
                    result["table"], result["id"], result.get("error", ""), result["url"],
                )
                broken_rows.append(result)

# ── Summary ───────────────────────────────────────────────────────────────────

log.info("")
log.info("══ Photo URL health check complete ════════════════")
log.info("  OK (HTTP 200)      : %d", counters["ok"])
log.info("  BROKEN (non-200)   : %d", counters["broken"])
log.info("  ERROR (network)    : %d", counters["error"])
log.info("  NO_URL (unmigrated): %d", counters["no_url"])

if broken_rows:
    log.info("")
    log.info("── Broken / unreachable URLs (re-upload candidates) ──")
    for row in broken_rows:
        detail = f"HTTP {row['code']}" if "code" in row else row.get("error", "")
        log.info("  %-20s  id=%-6s  %s  %s", row["table"], row["id"], detail, row["url"])

total_failures = counters["broken"] + counters["error"]
if total_failures:
    log.error(
        "%d URL(s) are broken or unreachable — re-run migrate_photos_to_spaces.py "
        "to retry failed rows, or investigate the URLs above.",
        total_failures,
    )
    sys.exit(1)

log.info("All storage URLs are reachable. ✓")
