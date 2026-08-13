#!/usr/bin/env python3
"""
migrate_photos_to_spaces.py
───────────────────────────
One-time (idempotent) migration: upload locally-stored photos to
DigitalOcean Spaces and record the public URL in the DB.

Covers four tables:
  • job_photos
  • completion_photos
  • listing_photos
  • gallery_photos

Only rows where storage_url IS NULL are processed, so the script is
safe to re-run at any time.

Usage:
  python migrate_photos_to_spaces.py [--dry-run]
  python migrate_photos_to_spaces.py --cleanup [--dry-run]

Flags:
  --dry-run   Scan and report without uploading, writing to the DB, or
              deleting any files.  Safe to run at any time.
  --cleanup   After migration, delete local uploads/ files whose DB row
              already has storage_url set (i.e. they have been confirmed
              uploaded to Spaces).  Files that are not referenced in the
              DB are never touched, so future local-mode files are safe.
              Combine with --dry-run to preview what would be deleted.

Required env vars (same ones that storage.py uses):
  SPACES_KEY       Spaces access key
  SPACES_SECRET    Spaces secret key
  SPACES_BUCKET    Bucket / Space name
  SPACES_REGION    (optional, default nyc3)
  SPACES_ENDPOINT  (optional, derived from region)
  SPACES_CDN_URL   (optional, preferred CDN origin)
  DATABASE_URL     PostgreSQL connection string (already set in app)
"""

import argparse
import io
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate_photos")

# ── Parse CLI args ────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Migrate local photos to DO Spaces")
parser.add_argument(
    "--dry-run",
    action="store_true",
    help="Scan and report without uploading, updating the DB, or deleting files",
)
parser.add_argument(
    "--cleanup",
    action="store_true",
    help=(
        "Delete local uploads/ files whose DB row already has storage_url set. "
        "Files not referenced by any DB row are left untouched. "
        "Use --dry-run together with --cleanup to preview deletions."
    ),
)
args = parser.parse_args()
DRY_RUN = args.dry_run
CLEANUP = args.cleanup

if DRY_RUN:
    log.info("DRY-RUN mode — no uploads, DB writes, or deletions will happen")

# ── Validate Spaces config ────────────────────────────────────────────────────

SPACES_KEY      = os.environ.get("SPACES_KEY")
SPACES_SECRET   = os.environ.get("SPACES_SECRET")
SPACES_BUCKET   = os.environ.get("SPACES_BUCKET")
SPACES_REGION   = os.environ.get("SPACES_REGION", "nyc3")
SPACES_ENDPOINT = os.environ.get(
    "SPACES_ENDPOINT",
    f"https://{SPACES_REGION}.digitaloceanspaces.com",
)
SPACES_CDN_URL  = os.environ.get("SPACES_CDN_URL", "").rstrip("/")

if not (SPACES_KEY and SPACES_SECRET and SPACES_BUCKET):
    log.error(
        "SPACES_KEY, SPACES_SECRET, and SPACES_BUCKET must all be set. "
        "Aborting — nothing has been changed."
    )
    sys.exit(1)

# ── Build Spaces client ───────────────────────────────────────────────────────

try:
    import boto3
    from botocore.client import Config
except ImportError:
    log.error("boto3 is not installed. Run: pip install boto3")
    sys.exit(1)

s3 = boto3.session.Session().client(
    "s3",
    region_name=SPACES_REGION,
    endpoint_url=SPACES_ENDPOINT,
    aws_access_key_id=SPACES_KEY,
    aws_secret_access_key=SPACES_SECRET,
    config=Config(signature_version="s3v4"),
)

_CONTENT_TYPES = {
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "png":  "image/png",
    "gif":  "image/gif",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
    "bmp":  "image/bmp",
    "tiff": "image/tiff",
}


def _content_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def _spaces_url(filename: str) -> str:
    if SPACES_CDN_URL:
        return f"{SPACES_CDN_URL}/uploads/{filename}"
    return f"{SPACES_ENDPOINT.rstrip('/')}/{SPACES_BUCKET}/uploads/{filename}"


def upload_local_file(local_path: str, filename: str) -> str:
    """Upload *local_path* to Spaces as uploads/{filename} and return the public URL."""
    ct = _content_type(filename)
    with open(local_path, "rb") as fh:
        data = fh.read()

    s3.upload_fileobj(
        io.BytesIO(data),
        SPACES_BUCKET,
        f"uploads/{filename}",
        ExtraArgs={"ACL": "public-read", "ContentType": ct},
    )
    return _spaces_url(filename)


# ── Bootstrap Flask app context ───────────────────────────────────────────────

from app import app, UPLOAD_FOLDER  # noqa: E402  (after sys.path is clean)
from models import db, JobPhoto, CompletionPhoto, ListingPhoto, GalleryPhoto  # noqa: E402
from migrate_cleanup import run_cleanup  # noqa: E402

# ── Migration logic ───────────────────────────────────────────────────────────

TABLES = [
    ("job_photos",        JobPhoto),
    ("completion_photos", CompletionPhoto),
    ("listing_photos",    ListingPhoto),
    ("gallery_photos",    GalleryPhoto),
]

counters = {
    "scanned":    0,
    "uploaded":   0,
    "skipped_no_file": 0,
    "already_done": 0,
    "errors":     0,
}


def migrate_table(label: str, model):
    log.info("── %s ──────────────────────────────────", label)
    rows = model.query.filter(model.storage_url.is_(None)).all()
    log.info("  %d row(s) with storage_url = NULL", len(rows))

    for row in rows:
        counters["scanned"] += 1
        filename = row.filename

        if not filename:
            log.warning("  [id=%s] empty filename — skipping", row.id)
            counters["skipped_no_file"] += 1
            continue

        local_path = os.path.join(UPLOAD_FOLDER, filename)

        if not os.path.isfile(local_path):
            log.warning(
                "  [id=%s] local file not found: uploads/%s — skipping",
                row.id, filename,
            )
            counters["skipped_no_file"] += 1
            continue

        if DRY_RUN:
            log.info("  [id=%s] DRY-RUN would upload uploads/%s", row.id, filename)
            counters["uploaded"] += 1
            continue

        try:
            url = upload_local_file(local_path, filename)
            row.storage_url = url
            db.session.add(row)
            log.info("  [id=%s] ✓ %s", row.id, url)
            counters["uploaded"] += 1
        except Exception as exc:
            log.error("  [id=%s] upload failed: %s", row.id, exc)
            counters["errors"] += 1
            db.session.rollback()


with app.app_context():
    for label, model in TABLES:
        # Count already-migrated rows (informational)
        done = model.query.filter(model.storage_url.isnot(None)).count()
        counters["already_done"] += done

        migrate_table(label, model)

        if not DRY_RUN:
            try:
                db.session.commit()
            except Exception as exc:
                log.error("DB commit failed for %s: %s", label, exc)
                db.session.rollback()
                counters["errors"] += 1

    # ── Cleanup: remove local files already migrated to Spaces ───────────────
    if CLEANUP:
        cleanup_counters = run_cleanup(TABLES, UPLOAD_FOLDER, dry_run=DRY_RUN)
        if cleanup_counters["errors"]:
            counters["errors"] += cleanup_counters["errors"]

# ── Summary ───────────────────────────────────────────────────────────────────

log.info("")
log.info("══ Migration complete ══════════════════════════════")
log.info("  Already had storage_url : %d", counters["already_done"])
log.info("  Rows scanned (NULL)     : %d", counters["scanned"])
log.info("  Uploaded to Spaces      : %d", counters["uploaded"])
log.info("  Skipped (no local file) : %d", counters["skipped_no_file"])
log.info("  Errors                  : %d", counters["errors"])
if DRY_RUN:
    log.info("  (DRY-RUN — nothing written or deleted)")

if counters["errors"]:
    sys.exit(1)
