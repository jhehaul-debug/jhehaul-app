"""
test_photo_health_check.py — confirm the /admin/photo-health route returns
accurate results when some photo URLs are broken, cause network errors, or
have no storage_url at all.

Strategy
--------
Each test patches ``routes.render_template`` with a side-effect that captures
the exact template context (counters, table_stats, broken_rows, no_url_rows)
before delegating to the real renderer.  This lets assertions target specific
dict values rather than parsing the HTML string.

HTTP is intercepted by patching ``requests.Session`` (the object constructed
inside the route function) so no real network calls are made.

Isolation: every test uses unique URLs (containing a UUID prefix) so that
pre-existing DB rows cannot accidentally satisfy the per-URL assertions.

Covers:
  1. Rows with storage_url=None appear in no_url_rows and are NOT checked over HTTP.
  2. A 404 URL → BROKEN entry in broken_rows with the correct status code.
  3. A network-error URL → ERROR entry in broken_rows.
  4. A healthy 200 URL → HTTP HEAD called for that URL; not in broken_rows.
  5. Per-table summary counts (total / with_url / without_url) match seeded data.
  6. counters['no_url'] equals the true DB count of null-URL rows across all tables.
"""

import sys
import uuid
from unittest.mock import patch, MagicMock, call as _call

import flask
from app import app, db
import routes  # registers all URL rules


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _make_admin():
    from models import User
    u = User()
    u.id = str(uuid.uuid4())
    u.email = f"admin_{u.id[:8]}@test.local"
    u.user_type = "customer"
    u.is_admin = True
    u.age_confirmed = True
    u.profile_nudge_dismissed = True
    db.session.add(u)
    db.session.commit()
    return u.id


def _make_job():
    from models import Job
    j = Job()
    j.customer_name = "Test Customer"
    j.pickup_address = "123 Test St"
    j.job_description = "Test job"
    db.session.add(j)
    db.session.commit()
    return j.id


def _make_job_photo(job_id, storage_url):
    from models import JobPhoto
    p = JobPhoto()
    p.job_id = job_id
    p.filename = f"test_{uuid.uuid4().hex[:8]}.jpg"
    p.storage_url = storage_url
    db.session.add(p)
    db.session.commit()
    return p.id


def _make_seller():
    from models import User
    u = User()
    u.id = str(uuid.uuid4())
    u.email = f"seller_{u.id[:8]}@test.local"
    u.user_type = "customer"
    u.is_admin = False
    u.age_confirmed = True
    u.profile_nudge_dismissed = True
    db.session.add(u)
    db.session.commit()
    return u.id


def _make_listing(seller_id):
    from models import Listing
    l = Listing()
    l.seller_id = seller_id
    l.title = "Photo health test listing"
    l.status = "active"
    l.listing_type = "item"
    l.moderation_status = "approved"
    l.price_type = "fixed"
    l.price = 10.00
    db.session.add(l)
    db.session.commit()
    return l.id


def _make_listing_photo(listing_id, storage_url):
    from models import ListingPhoto
    p = ListingPhoto()
    p.listing_id = listing_id
    p.filename = f"test_{uuid.uuid4().hex[:8]}.jpg"
    p.storage_url = storage_url
    p.is_primary = False
    p.display_order = 0
    db.session.add(p)
    db.session.commit()
    return p.id


def _cleanup(*model_id_pairs):
    with app.app_context():
        for Model, row_id in model_id_pairs:
            if row_id is None:
                continue
            obj = db.session.get(Model, row_id)
            if obj:
                db.session.delete(obj)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


def _mock_admin_user(admin_id):
    """Return a MagicMock that satisfies flask-login + require_admin checks."""
    m = MagicMock()
    m.is_authenticated = True
    m.is_admin = True
    m.user_type = "customer"
    m.id = admin_id
    m.profile_image_url = None
    m.profile_photo_data = None
    m.phone = "5550000001"
    m.profile_nudge_dismissed = True
    m.admin_session_version = 0
    return m


def _mock_http_session(url_responses):
    """
    Return a mock requests.Session whose .head() returns a mock response.

    url_responses: dict mapping URL → int status code (e.g. 200, 404)
                   or an Exception instance to raise.
    Any URL not in the dict defaults to 200.
    """
    mock_session = MagicMock()

    def _head(url, **kwargs):
        outcome = url_responses.get(url, 200)
        if isinstance(outcome, Exception):
            raise outcome
        resp = MagicMock()
        resp.status_code = outcome
        return resp

    mock_session.head.side_effect = _head
    return mock_session


def _run_health_route(admin_id, url_responses):
    """
    Execute GET /admin/photo-health with mocked HTTP, capturing the template
    context.  Returns (response, captured_ctx_dict).
    """
    import requests as real_requests
    mock_session = _mock_http_session(url_responses)
    captured = {}

    def _capture_render(template_name, **ctx):
        captured.update(ctx)
        return flask.render_template(template_name, **ctx)

    admin = db.session.get(__import__("models").User, admin_id)

    with app.test_client() as client:
        with patch("flask_login.utils._get_user", return_value=admin), \
             patch.object(real_requests, "Session", return_value=mock_session), \
             patch("routes.render_template", side_effect=_capture_render):
            resp = client.get("/admin/photo-health", follow_redirects=False)

    return resp, captured, mock_session


# ── Test runner ────────────────────────────────────────────────────────────────

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


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_no_url_rows_listed_and_not_http_checked():
    """
    Rows with storage_url=None appear in no_url_rows (by table+id) and HTTP HEAD
    is NOT called for them.
    """
    admin_id = job_id = jp_null_id = seller_id = listing_id = lp_null_id = None
    try:
        with app.app_context():
            admin_id    = _make_admin()
            job_id      = _make_job()
            jp_null_id  = _make_job_photo(job_id, storage_url=None)
            seller_id   = _make_seller()
            listing_id  = _make_listing(seller_id)
            lp_null_id  = _make_listing_photo(listing_id, storage_url=None)

        with app.app_context():
            resp, ctx, mock_sess = _run_health_route(admin_id, url_responses={})

        assert resp.status_code == 200, \
            f"Expected 200, got {resp.status_code}"

        no_url_rows = ctx.get("no_url_rows", [])

        # Our null job_photo must appear in no_url_rows
        jp_entry = next(
            (r for r in no_url_rows if r["table"] == "job_photos" and r["id"] == jp_null_id),
            None,
        )
        assert jp_entry is not None, (
            f"job_photos row id={jp_null_id} missing from no_url_rows; got: {no_url_rows}"
        )

        # Our null listing_photo must appear in no_url_rows
        lp_entry = next(
            (r for r in no_url_rows if r["table"] == "listing_photos" and r["id"] == lp_null_id),
            None,
        )
        assert lp_entry is not None, (
            f"listing_photos row id={lp_null_id} missing from no_url_rows; got: {no_url_rows}"
        )

        # HTTP HEAD must NOT have been called (no URLs to check)
        assert mock_sess.head.call_count == 0, (
            f"HTTP HEAD should not be called for null storage_url rows, "
            f"but was called {mock_sess.head.call_count} time(s)"
        )

    finally:
        from models import JobPhoto, ListingPhoto, Listing, Job, User
        _cleanup(
            (ListingPhoto, lp_null_id),
            (Listing,      listing_id),
            (User,         seller_id),
            (JobPhoto,     jp_null_id),
            (Job,          job_id),
            (User,         admin_id),
        )


def test_broken_url_appears_in_broken_rows():
    """
    A non-200 HTTP response → the row is classified as BROKEN with the correct
    status code in broken_rows.
    """
    uid        = uuid.uuid4().hex[:12]
    GOOD_URL   = f"https://cdn.example.com/{uid}/ok.jpg"
    BAD_URL    = f"https://cdn.example.com/{uid}/missing.jpg"
    admin_id = job_id = jp_ok_id = jp_bad_id = None
    try:
        with app.app_context():
            admin_id  = _make_admin()
            job_id    = _make_job()
            jp_ok_id  = _make_job_photo(job_id, storage_url=GOOD_URL)
            jp_bad_id = _make_job_photo(job_id, storage_url=BAD_URL)

        with app.app_context():
            resp, ctx, _ = _run_health_route(
                admin_id,
                url_responses={GOOD_URL: 200, BAD_URL: 404},
            )

        assert resp.status_code == 200, \
            f"Expected 200, got {resp.status_code}"

        broken_rows = ctx.get("broken_rows", [])

        # The bad URL must appear as BROKEN
        bad_entry = next(
            (r for r in broken_rows if r.get("url") == BAD_URL),
            None,
        )
        assert bad_entry is not None, (
            f"BAD_URL not found in broken_rows; broken_rows: {broken_rows}"
        )
        assert bad_entry["status"] == "BROKEN", \
            f"Expected status=BROKEN, got {bad_entry['status']}"
        assert bad_entry["code"] == 404, \
            f"Expected code=404, got {bad_entry.get('code')}"
        assert bad_entry["table"] == "job_photos", \
            f"Expected table=job_photos, got {bad_entry.get('table')}"
        assert bad_entry["id"] == jp_bad_id, \
            f"Expected id={jp_bad_id}, got {bad_entry.get('id')}"

        # The good URL must NOT appear in broken_rows
        good_entry = next(
            (r for r in broken_rows if r.get("url") == GOOD_URL),
            None,
        )
        assert good_entry is None, \
            f"GOOD_URL should not be in broken_rows but found: {good_entry}"

        # broken counter must include our one broken row
        assert ctx["counters"]["broken"] >= 1, \
            f"counters['broken'] should be >= 1, got {ctx['counters']['broken']}"

    finally:
        from models import JobPhoto, Job, User
        _cleanup(
            (JobPhoto, jp_bad_id),
            (JobPhoto, jp_ok_id),
            (Job,      job_id),
            (User,     admin_id),
        )


def test_network_error_appears_as_error_row():
    """A URL that raises a connection error is classified as ERROR in broken_rows."""
    uid     = uuid.uuid4().hex[:12]
    ERR_URL = f"https://cdn.example.com/{uid}/unreachable.jpg"
    admin_id = job_id = jp_err_id = None
    try:
        with app.app_context():
            admin_id  = _make_admin()
            job_id    = _make_job()
            jp_err_id = _make_job_photo(job_id, storage_url=ERR_URL)

        import requests as real_requests
        conn_err = real_requests.exceptions.ConnectionError("connection refused")

        with app.app_context():
            resp, ctx, _ = _run_health_route(
                admin_id,
                url_responses={ERR_URL: conn_err},
            )

        assert resp.status_code == 200, \
            f"Expected 200, got {resp.status_code}"

        broken_rows = ctx.get("broken_rows", [])

        err_entry = next(
            (r for r in broken_rows if r.get("url") == ERR_URL),
            None,
        )
        assert err_entry is not None, (
            f"ERR_URL not found in broken_rows; broken_rows: {broken_rows}"
        )
        assert err_entry["status"] == "ERROR", \
            f"Expected status=ERROR, got {err_entry['status']}"
        assert err_entry["table"] == "job_photos", \
            f"Expected table=job_photos, got {err_entry.get('table')}"
        assert err_entry["id"] == jp_err_id, \
            f"Expected id={jp_err_id}, got {err_entry.get('id')}"
        assert "connection refused" in err_entry.get("error", "").lower(), \
            f"Expected error message to mention connection; got {err_entry.get('error')}"

        # error counter must include our row
        assert ctx["counters"]["error"] >= 1, \
            f"counters['error'] should be >= 1, got {ctx['counters']['error']}"

    finally:
        from models import JobPhoto, Job, User
        _cleanup(
            (JobPhoto, jp_err_id),
            (Job,      job_id),
            (User,     admin_id),
        )


def test_healthy_url_is_checked_and_not_in_broken_rows():
    """
    A 200 URL: HTTP HEAD is called for that URL, ok counter increases, and the
    URL does not appear in broken_rows.
    """
    uid    = uuid.uuid4().hex[:12]
    OK_URL = f"https://cdn.example.com/{uid}/healthy.jpg"
    admin_id = job_id = jp_ok_id = None
    try:
        with app.app_context():
            admin_id = _make_admin()
            job_id   = _make_job()
            jp_ok_id = _make_job_photo(job_id, storage_url=OK_URL)

            # Count how many storage_url rows exist BEFORE the request; all get
            # 200 from the mock (default), so counters['ok'] must equal this.
            from models import JobPhoto, CompletionPhoto, ListingPhoto, GalleryPhoto
            expected_ok = (
                JobPhoto.query.filter(JobPhoto.storage_url.isnot(None)).count()
                + CompletionPhoto.query.filter(CompletionPhoto.storage_url.isnot(None)).count()
                + ListingPhoto.query.filter(ListingPhoto.storage_url.isnot(None)).count()
                + GalleryPhoto.query.filter(GalleryPhoto.storage_url.isnot(None)).count()
            )

        with app.app_context():
            resp, ctx, mock_sess = _run_health_route(
                admin_id,
                url_responses={},  # default → 200 for all URLs
            )

        assert resp.status_code == 200, \
            f"Expected 200, got {resp.status_code}"

        # HTTP HEAD must have been called with OUR URL
        called_urls = [c.args[0] for c in mock_sess.head.call_args_list]
        assert OK_URL in called_urls, (
            f"HTTP HEAD was not called for OK_URL={OK_URL!r}; called: {called_urls[:5]}"
        )

        # Our URL must NOT appear in broken_rows
        broken_rows = ctx.get("broken_rows", [])
        bad_entry = next(
            (r for r in broken_rows if r.get("url") == OK_URL),
            None,
        )
        assert bad_entry is None, \
            f"OK_URL should not be in broken_rows but found: {bad_entry}"

        # ok counter must equal the total rows with storage_url (all mocked to 200)
        assert ctx["counters"]["ok"] == expected_ok, (
            f"counters['ok']={ctx['counters']['ok']} != expected {expected_ok}"
        )

    finally:
        from models import JobPhoto, Job, User
        _cleanup(
            (JobPhoto, jp_ok_id),
            (Job,      job_id),
            (User,     admin_id),
        )


def test_per_table_summary_counts_match_seeded_data():
    """
    Per-table summary (total / with_url / without_url) in table_stats reflects
    the true DB state at query time.
    """
    uid = uuid.uuid4().hex[:12]
    URL_A = f"https://cdn.example.com/{uid}/a.jpg"
    URL_B = f"https://cdn.example.com/{uid}/b.jpg"
    admin_id = job_id = seller_id = listing_id = None
    jp_ids = []
    lp_ids = []
    try:
        with app.app_context():
            admin_id = _make_admin()
            job_id   = _make_job()
            # 2 job_photos: 1 with URL, 1 without
            jp_ids.append(_make_job_photo(job_id, storage_url=URL_A))
            jp_ids.append(_make_job_photo(job_id, storage_url=None))

            seller_id  = _make_seller()
            listing_id = _make_listing(seller_id)
            # 3 listing_photos: 2 with URL, 1 without
            lp_ids.append(_make_listing_photo(listing_id, storage_url=URL_B))
            lp_ids.append(_make_listing_photo(listing_id, storage_url=URL_B + "2"))
            lp_ids.append(_make_listing_photo(listing_id, storage_url=None))

            # Capture the exact DB counts that the route will query
            from models import JobPhoto, ListingPhoto
            exp_jp_total    = JobPhoto.query.count()
            exp_jp_with     = JobPhoto.query.filter(JobPhoto.storage_url.isnot(None)).count()
            exp_jp_without  = exp_jp_total - exp_jp_with

            exp_lp_total    = ListingPhoto.query.count()
            exp_lp_with     = ListingPhoto.query.filter(ListingPhoto.storage_url.isnot(None)).count()
            exp_lp_without  = exp_lp_total - exp_lp_with

        with app.app_context():
            resp, ctx, _ = _run_health_route(
                admin_id,
                url_responses={URL_A: 200, URL_B: 200, URL_B + "2": 200},
            )

        assert resp.status_code == 200, \
            f"Expected 200, got {resp.status_code}"

        table_stats = ctx.get("table_stats", [])
        stats_by_label = {row["label"]: row for row in table_stats}

        # job_photos assertions
        assert "job_photos" in stats_by_label, \
            f"job_photos missing from table_stats; labels: {list(stats_by_label)}"
        jp_stat = stats_by_label["job_photos"]
        assert jp_stat["total"]       == exp_jp_total,   \
            f"job_photos total: got {jp_stat['total']}, expected {exp_jp_total}"
        assert jp_stat["with_url"]    == exp_jp_with,    \
            f"job_photos with_url: got {jp_stat['with_url']}, expected {exp_jp_with}"
        assert jp_stat["without_url"] == exp_jp_without, \
            f"job_photos without_url: got {jp_stat['without_url']}, expected {exp_jp_without}"

        # listing_photos assertions
        assert "listing_photos" in stats_by_label, \
            f"listing_photos missing from table_stats; labels: {list(stats_by_label)}"
        lp_stat = stats_by_label["listing_photos"]
        assert lp_stat["total"]       == exp_lp_total,   \
            f"listing_photos total: got {lp_stat['total']}, expected {exp_lp_total}"
        assert lp_stat["with_url"]    == exp_lp_with,    \
            f"listing_photos with_url: got {lp_stat['with_url']}, expected {exp_lp_with}"
        assert lp_stat["without_url"] == exp_lp_without, \
            f"listing_photos without_url: got {lp_stat['without_url']}, expected {exp_lp_without}"

    finally:
        from models import JobPhoto, ListingPhoto, Listing, Job, User
        for lid in lp_ids:
            _cleanup((ListingPhoto, lid))
        _cleanup(
            (Listing, listing_id),
            (User,    seller_id),
        )
        for jid in jp_ids:
            _cleanup((JobPhoto, jid))
        _cleanup(
            (Job,  job_id),
            (User, admin_id),
        )


def test_no_url_counter_equals_total_null_rows_in_db():
    """counters['no_url'] equals the DB count of null storage_url rows across all 4 tables."""
    admin_id = job_id = jp_null1_id = jp_null2_id = None
    try:
        with app.app_context():
            admin_id     = _make_admin()
            job_id       = _make_job()
            jp_null1_id  = _make_job_photo(job_id, storage_url=None)
            jp_null2_id  = _make_job_photo(job_id, storage_url=None)

            # Count ALL null rows that the route will count
            from models import JobPhoto, CompletionPhoto, ListingPhoto, GalleryPhoto
            expected_no_url = (
                JobPhoto.query.filter(JobPhoto.storage_url.is_(None)).count()
                + CompletionPhoto.query.filter(CompletionPhoto.storage_url.is_(None)).count()
                + ListingPhoto.query.filter(ListingPhoto.storage_url.is_(None)).count()
                + GalleryPhoto.query.filter(GalleryPhoto.storage_url.is_(None)).count()
            )

        with app.app_context():
            resp, ctx, _ = _run_health_route(admin_id, url_responses={})

        assert resp.status_code == 200, \
            f"Expected 200, got {resp.status_code}"

        actual_no_url = ctx["counters"]["no_url"]
        assert actual_no_url == expected_no_url, (
            f"counters['no_url']={actual_no_url} != expected {expected_no_url}"
        )

    finally:
        from models import JobPhoto, Job, User
        _cleanup(
            (JobPhoto, jp_null2_id),
            (JobPhoto, jp_null1_id),
            (Job,      job_id),
            (User,     admin_id),
        )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nRunning photo health check tests...\n")

    run("null storage_url rows appear in no_url_rows and are not HTTP-checked",
        test_no_url_rows_listed_and_not_http_checked)
    run("404 URL classified as BROKEN with correct code in broken_rows",
        test_broken_url_appears_in_broken_rows)
    run("connection error URL classified as ERROR in broken_rows",
        test_network_error_appears_as_error_row)
    run("healthy 200 URL: HEAD called, not in broken_rows, ok counter accurate",
        test_healthy_url_is_checked_and_not_in_broken_rows)
    run("per-table summary counts match seeded DB state",
        test_per_table_summary_counts_match_seeded_data)
    run("counters[no_url] equals total null-URL rows across all 4 tables",
        test_no_url_counter_equals_total_null_rows_in_db)

    print(f"\n{'='*60}")
    print(f"Results: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nFailures:")
        for name, exc in FAIL:
            print(f"  {name}: {exc}")
        sys.exit(1)
    else:
        print("All tests passed.")
