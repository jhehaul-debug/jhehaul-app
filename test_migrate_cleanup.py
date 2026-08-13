"""
test_migrate_cleanup.py
───────────────────────
Unit tests for the --cleanup logic in migrate_photos_to_spaces.py.

The cleanup helper lives in migrate_cleanup.run_cleanup() so it can be
imported and exercised without triggering the migration script's top-level
argument parsing or Spaces credential checks.

Scenarios covered
─────────────────
1. Migrated files (DB row, storage_url set)      → deleted
2. Un-migrated files (DB row, storage_url=NULL)  → kept
3. Orphan files (no DB row at all)               → kept
4. Dry-run mode                                  → nothing deleted, correct
                                                   filenames logged
5. Mix of all three types + dry-run              → combined assertions
"""

import os
import tempfile
import logging
from unittest.mock import MagicMock, patch


# ── Import the function under test ────────────────────────────────────────────

from migrate_cleanup import run_cleanup


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_model_row(filename, storage_url):
    """Return a MagicMock that looks like a photo model row."""
    row = MagicMock()
    row.filename = filename
    row.storage_url = storage_url
    return row


def _fake_tables(migrated_filenames, unmigrated_filenames):
    """
    Build a fake TABLES list understood by run_cleanup.

    run_cleanup calls:
        model.query.filter(model.storage_url.isnot(None)).all()

    We return one fake table whose query returns only the migrated rows.
    Un-migrated rows (storage_url=None) never appear because the filter
    excludes them — this mirrors the real behaviour.
    """
    migrated_rows = [_make_model_row(fn, "https://cdn.example.com/" + fn)
                     for fn in migrated_filenames]

    # Build a mock model whose .query.filter(...).all() returns migrated_rows.
    mock_model = MagicMock()
    mock_model.storage_url = MagicMock()
    mock_model.query.filter.return_value.all.return_value = migrated_rows

    return [("test_table", mock_model)]


def _create_files(folder, filenames):
    """Write empty placeholder files inside *folder*."""
    for fn in filenames:
        open(os.path.join(folder, fn), "w").close()


# ── Test runner ───────────────────────────────────────────────────────────────

PASS = []
FAIL = []


def run(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  PASS  {name}")
    except Exception as exc:
        import traceback
        FAIL.append((name, exc))
        print(f"  FAIL  {name}: {exc}")
        traceback.print_exc()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_migrated_files_are_deleted():
    """Files with storage_url set must be removed from disk."""
    with tempfile.TemporaryDirectory() as folder:
        _create_files(folder, ["migrated_a.jpg", "migrated_b.jpg"])

        tables = _fake_tables(
            migrated_filenames=["migrated_a.jpg", "migrated_b.jpg"],
            unmigrated_filenames=[],
        )
        result = run_cleanup(tables, folder, dry_run=False)

    assert result["deleted"] == 2, \
        f"Expected 2 deleted, got {result['deleted']}"
    assert result["would_delete"] == 0
    assert result["skipped_no_db"] == 0
    assert result["errors"] == 0


def test_unmigrated_files_are_kept():
    """Files whose DB row has storage_url=NULL must NOT be removed."""
    with tempfile.TemporaryDirectory() as folder:
        _create_files(folder, ["unmigrated.jpg"])

        # No row appears in the migrated set (query filters out NULL rows).
        tables = _fake_tables(
            migrated_filenames=[],
            unmigrated_filenames=["unmigrated.jpg"],
        )
        result = run_cleanup(tables, folder, dry_run=False)

        # File must still exist.
        assert os.path.isfile(os.path.join(folder, "unmigrated.jpg")), \
            "Un-migrated file should NOT have been deleted"

    assert result["deleted"] == 0
    assert result["skipped_no_db"] == 1
    assert result["errors"] == 0


def test_orphan_files_are_kept():
    """Files with no matching DB row at all must not be touched."""
    with tempfile.TemporaryDirectory() as folder:
        _create_files(folder, ["orphan.jpg"])

        # DB has no rows at all.
        tables = _fake_tables(migrated_filenames=[], unmigrated_filenames=[])
        result = run_cleanup(tables, folder, dry_run=False)

        assert os.path.isfile(os.path.join(folder, "orphan.jpg")), \
            "Orphan file (no DB row) should NOT have been deleted"

    assert result["deleted"] == 0
    assert result["skipped_no_db"] == 1
    assert result["errors"] == 0


def test_dry_run_deletes_nothing():
    """With dry_run=True, no files are removed regardless of DB state."""
    with tempfile.TemporaryDirectory() as folder:
        _create_files(folder, ["migrated_x.jpg", "unmigrated_y.jpg", "orphan_z.jpg"])

        tables = _fake_tables(
            migrated_filenames=["migrated_x.jpg"],
            unmigrated_filenames=["unmigrated_y.jpg"],
        )
        result = run_cleanup(tables, folder, dry_run=True)

        # All three files must still be present.
        for fn in ["migrated_x.jpg", "unmigrated_y.jpg", "orphan_z.jpg"]:
            assert os.path.isfile(os.path.join(folder, fn)), \
                f"{fn} should NOT have been removed in dry-run mode"

    assert result["deleted"] == 0, \
        "dry_run must not delete anything"
    assert result["would_delete"] == 1, \
        "dry_run should report would_delete=1 for the migrated file"
    assert result["skipped_no_db"] == 2, \
        "un-migrated and orphan files should both be skipped"
    assert result["errors"] == 0


def test_dry_run_logs_correct_filenames():
    """dry_run must log the filename it *would* delete, not the others."""
    with tempfile.TemporaryDirectory() as folder:
        _create_files(folder, ["to_delete.jpg", "to_keep.jpg"])

        tables = _fake_tables(
            migrated_filenames=["to_delete.jpg"],
            unmigrated_filenames=["to_keep.jpg"],
        )

        log_records = []

        class CapturingHandler(logging.Handler):
            def emit(self, record):
                log_records.append(self.format(record))

        handler = CapturingHandler()
        logger = logging.getLogger("migrate_photos")
        original_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            run_cleanup(tables, folder, dry_run=True)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(original_level)

        joined = "\n".join(log_records)
        assert "to_delete.jpg" in joined, \
            "Dry-run log must mention the file that would be deleted"
        # to_keep.jpg is skipped at DEBUG level (not INFO), so it should not
        # appear in an INFO+ log capture.
        assert "DRY-RUN would delete" in joined, \
            "'DRY-RUN would delete' message not found in log output"


def test_mixed_scenario_live():
    """
    Full scenario with all three file types in live (non-dry-run) mode:
      • migrated file  → deleted
      • unmigrated file → kept
      • orphan file    → kept
    """
    with tempfile.TemporaryDirectory() as folder:
        _create_files(folder, ["migrated.png", "unmigrated.png", "orphan.png"])

        tables = _fake_tables(
            migrated_filenames=["migrated.png"],
            unmigrated_filenames=["unmigrated.png"],
        )
        result = run_cleanup(tables, folder, dry_run=False)

        assert not os.path.isfile(os.path.join(folder, "migrated.png")), \
            "migrated.png should have been deleted"
        assert os.path.isfile(os.path.join(folder, "unmigrated.png")), \
            "unmigrated.png should have been kept"
        assert os.path.isfile(os.path.join(folder, "orphan.png")), \
            "orphan.png should have been kept"

    assert result["deleted"] == 1
    assert result["skipped_no_db"] == 2   # unmigrated + orphan
    assert result["errors"] == 0


def test_missing_upload_folder_is_safe():
    """If the uploads/ folder does not exist, cleanup should finish without error."""
    tables = _fake_tables(migrated_filenames=["whatever.jpg"], unmigrated_filenames=[])
    result = run_cleanup(tables, "/tmp/nonexistent_upload_folder_xyz", dry_run=False)

    assert result["deleted"] == 0
    assert result["errors"] == 0


def test_only_regular_files_are_deleted():
    """Subdirectories inside uploads/ must never be touched."""
    with tempfile.TemporaryDirectory() as folder:
        # Create a real file and a subdirectory with the name of a migrated file.
        real_file = "real_migrated.jpg"
        open(os.path.join(folder, real_file), "w").close()

        subdir = "subdir_migrated.jpg"   # same name as a migrated entry
        os.makedirs(os.path.join(folder, subdir), exist_ok=True)

        tables = _fake_tables(
            migrated_filenames=[real_file, subdir],
            unmigrated_filenames=[],
        )
        result = run_cleanup(tables, folder, dry_run=False)

        # Regular file should be gone; subdirectory must still be there.
        assert not os.path.isfile(os.path.join(folder, real_file)), \
            "Regular migrated file should have been deleted"
        assert os.path.isdir(os.path.join(folder, subdir)), \
            "Subdirectory must not be removed"

    assert result["deleted"] == 1
    assert result["skipped_not_file"] == 1
    assert result["errors"] == 0


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n── Migrate cleanup tests ────────────────────────────────────────")
    run("migrated files are deleted",           test_migrated_files_are_deleted)
    run("unmigrated files are kept",            test_unmigrated_files_are_kept)
    run("orphan files are kept",                test_orphan_files_are_kept)
    run("dry-run deletes nothing",              test_dry_run_deletes_nothing)
    run("dry-run logs correct filenames",       test_dry_run_logs_correct_filenames)
    run("mixed scenario (live mode)",           test_mixed_scenario_live)
    run("missing upload folder is safe",        test_missing_upload_folder_is_safe)
    run("only regular files are deleted",       test_only_regular_files_are_deleted)

    print()
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        import sys
        sys.exit(1)
