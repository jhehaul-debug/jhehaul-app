"""
migrate_cleanup.py
──────────────────
Standalone cleanup helper extracted from migrate_photos_to_spaces.py so that
it can be imported and unit-tested independently of the migration script's
top-level argument parsing and Spaces credential checks.

Public API
----------
run_cleanup(tables, upload_folder, dry_run=False) -> dict
    Delete local uploads/ files whose DB row already has storage_url set.
    Files that have no DB row, or whose row has storage_url=NULL, are left
    untouched.

    Parameters
    ----------
    tables : list of (label: str, model: SQLAlchemy model)
        Same list used by the migration script (TABLES).
    upload_folder : str
        Absolute or relative path to the local uploads directory.
    dry_run : bool
        When True, log what would be deleted but do not remove anything.

    Returns
    -------
    dict with keys:
        deleted, would_delete, skipped_no_db, skipped_not_file, errors
"""

import logging
import os

log = logging.getLogger("migrate_photos")


def run_cleanup(tables, upload_folder: str, dry_run: bool = False) -> dict:
    """
    Scan *upload_folder* and delete files that have been confirmed migrated.

    A file is "confirmed migrated" when at least one DB row across *tables*
    references that filename AND has storage_url set to a non-NULL value.

    Files that are not referenced by any DB row (orphans) are left alone.
    Files whose DB row has storage_url=NULL (not yet migrated) are left alone.
    """
    log.info("")
    log.info("── Cleanup ─────────────────────────────────────────")

    if dry_run:
        log.info("  DRY-RUN mode — no files will actually be deleted")

    # Build a set of filenames confirmed migrated (storage_url IS NOT NULL).
    migrated_filenames: set = set()
    for _label, model in tables:
        rows = model.query.filter(model.storage_url.isnot(None)).all()
        for row in rows:
            if row.filename:
                migrated_filenames.add(row.filename)

    log.info(
        "  DB rows with storage_url set : %d unique filenames",
        len(migrated_filenames),
    )

    counters = {
        "deleted":          0,
        "would_delete":     0,
        "skipped_no_db":    0,
        "skipped_not_file": 0,
        "errors":           0,
    }

    try:
        local_files = os.listdir(upload_folder)
    except FileNotFoundError:
        local_files = []
        log.warning("  uploads/ folder not found — nothing to clean up")

    for fname in sorted(local_files):
        fpath = os.path.join(upload_folder, fname)

        if not os.path.isfile(fpath):
            counters["skipped_not_file"] += 1
            continue  # skip subdirectories, etc.

        if fname not in migrated_filenames:
            # Orphan or not-yet-migrated — leave alone.
            log.debug("  KEEP  %s  (not in DB or not yet migrated)", fname)
            counters["skipped_no_db"] += 1
            continue

        # Confirmed migrated — safe to remove.
        if dry_run:
            log.info("  DRY-RUN would delete  uploads/%s", fname)
            counters["would_delete"] += 1
        else:
            try:
                os.remove(fpath)
                log.info("  Deleted  uploads/%s", fname)
                counters["deleted"] += 1
            except Exception as exc:
                log.error("  Failed to delete uploads/%s : %s", fname, exc)
                counters["errors"] += 1

    log.info("")
    log.info("  Local files scanned        : %d", len(local_files))
    if dry_run:
        log.info("  Would delete               : %d", counters["would_delete"])
    else:
        log.info("  Deleted                    : %d", counters["deleted"])
    log.info("  Kept (not in DB / pending) : %d", counters["skipped_no_db"])
    log.info("  Errors                     : %d", counters["errors"])

    return counters
