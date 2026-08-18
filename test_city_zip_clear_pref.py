"""
test_city_zip_clear_pref.py — Confirm that the saved City/ZIP session preference
is cleared correctly when a buyer uses any of the Clear affordances.

Covers:
  - Pill-row "✕ Clear" (GET /marketplace?city_zip=) removes the session preference
  - Filters-panel "✕ Clear" (GET /marketplace?city_zip=&listing_type=...) clears pref
  - Submitting the area bar with an empty city_zip value clears the preference
  - After clearing, a follow-up visit to /marketplace does NOT restore city_zip
  - Session preference is set when a non-empty city_zip is submitted
  - Visiting /marketplace without city_zip in URL restores the saved preference
  - After clearing, the homepage area bar is blank (city_zip_filter passed as '')
"""

import sys
from unittest.mock import patch, MagicMock

from flask import session as flask_session

from app import app, db
import routes  # registers all URL rules


# ── Helpers ────────────────────────────────────────────────────────────────────

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


def _anon_client():
    """Return a test client configured to store cookies (session) between requests."""
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app.test_client()


def _patch_login():
    """Patch flask_login so routes that touch current_user don't crash."""
    mock_user = MagicMock()
    mock_user.is_authenticated = False
    mock_user.is_active = False
    mock_user.is_anonymous = True
    mock_user.is_admin = False
    mock_user.get_id.return_value = None
    return patch("flask_login.utils._get_user", return_value=mock_user)


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_pill_clear_removes_session_pref():
    """Pill-row ✕ Clear: GET /marketplace?city_zip= pops city_zip_pref from session."""
    with _patch_login():
        client = _anon_client()
        with client.session_transaction() as sess:
            sess["city_zip_pref"] = "Edina"

        # Simulate clicking the pill-row Clear link
        resp = client.get("/marketplace?city_zip=", follow_redirects=False)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

        with client.session_transaction() as sess:
            assert "city_zip_pref" not in sess, (
                f"city_zip_pref should have been cleared; got '{sess.get('city_zip_pref')}'"
            )


def test_filters_panel_clear_removes_session_pref():
    """Filters panel ✕ Clear: GET /marketplace?city_zip=&listing_type=item clears pref."""
    with _patch_login():
        client = _anon_client()
        with client.session_transaction() as sess:
            sess["city_zip_pref"] = "55416"

        # Simulate the filters-panel Clear link (preserves other params, clears city_zip)
        resp = client.get(
            "/marketplace?city_zip=&listing_type=item", follow_redirects=False
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

        with client.session_transaction() as sess:
            assert "city_zip_pref" not in sess, (
                f"city_zip_pref should be absent; got '{sess.get('city_zip_pref')}'"
            )


def test_area_bar_empty_submit_clears_pref():
    """Submitting the area bar form with an empty city_zip value clears the preference."""
    with _patch_login():
        client = _anon_client()
        with client.session_transaction() as sess:
            sess["city_zip_pref"] = "Minneapolis"

        # The area bar is a GET form: name="city_zip" with empty value → city_zip= in URL
        resp = client.get("/marketplace?city_zip=", follow_redirects=False)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

        with client.session_transaction() as sess:
            assert "city_zip_pref" not in sess, (
                f"Submitting empty area bar should clear pref; got '{sess.get('city_zip_pref')}'"
            )


def test_next_visit_after_clear_has_no_city_zip():
    """After clearing, a follow-up visit to /marketplace without city_zip in URL
    does NOT restore a city_zip filter (session is empty)."""
    with _patch_login():
        client = _anon_client()
        # Seed a preference then clear it
        with client.session_transaction() as sess:
            sess["city_zip_pref"] = "Edina"

        client.get("/marketplace?city_zip=")  # clear

        # Follow-up bare visit — city_zip_pref must NOT come back
        resp = client.get("/marketplace", follow_redirects=False)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

        with client.session_transaction() as sess:
            pref = sess.get("city_zip_pref", "")
            assert pref == "", (
                f"city_zip_pref should be absent after clear; got '{pref}'"
            )

        # The response body should NOT contain the cleared city name in the
        # city_zip pill/filter position.  A simple text check is enough.
        body = resp.data.decode("utf-8", errors="replace")
        # The filter label "Filters: Edina" should not appear
        assert "Filters: Edina" not in body, (
            "Cleared city 'Edina' should not appear in filter labels after clear"
        )


def test_submitting_nonempty_city_zip_saves_pref():
    """Setting a city/ZIP via the filter bar persists it to the session."""
    with _patch_login():
        client = _anon_client()
        resp = client.get("/marketplace?city_zip=Edina", follow_redirects=False)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

        with client.session_transaction() as sess:
            pref = sess.get("city_zip_pref", "")
            assert pref == "Edina", (
                f"city_zip_pref should be 'Edina'; got '{pref}'"
            )


def test_session_pref_restored_when_city_zip_absent_from_url():
    """When city_zip is NOT in the URL, the saved preference is restored as the filter."""
    with _patch_login():
        client = _anon_client()
        # Inject preference directly into session
        with client.session_transaction() as sess:
            sess["city_zip_pref"] = "Bloomington"

        # Visit without city_zip in URL — route should restore from session
        resp = client.get("/marketplace", follow_redirects=False)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

        body = resp.data.decode("utf-8", errors="replace")
        # The Filters pill should show the saved preference label
        assert "Filters: Bloomington" in body, (
            "Saved city_zip_pref 'Bloomington' should appear in the Filters pill label"
        )


def test_homepage_area_bar_blank_after_clear():
    """After clearing the preference, the homepage city_zip_filter is empty
    so the area bar input renders blank (not pre-filled with old value)."""
    with _patch_login():
        client = _anon_client()
        # Set and then clear
        with client.session_transaction() as sess:
            sess["city_zip_pref"] = "Stillwater"

        client.get("/marketplace?city_zip=")  # clear

        # Visit /marketplace (home route redirects anon users to landing,
        # so we test the marketplace route directly)
        resp = client.get("/marketplace", follow_redirects=False)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

        body = resp.data.decode("utf-8", errors="replace")
        # Area bar should exist (no active city_zip_filter) with no pre-filled value
        assert "mp-homepage-area-bar" in body, (
            "Area bar should be visible when no city_zip_filter is active"
        )
        # The old city should not appear anywhere as a filter label
        assert "Filters: Stillwater" not in body, (
            "Cleared city 'Stillwater' must not appear in the filter pill after clear"
        )
        # The area bar input must NOT have value="Stillwater" pre-filled
        assert 'value="Stillwater"' not in body, (
            "Area bar input must not be pre-filled with the cleared city"
        )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nRunning City/ZIP preference clear tests...\n")

    run("pill-row Clear removes session pref",
        test_pill_clear_removes_session_pref)
    run("filters-panel Clear removes session pref",
        test_filters_panel_clear_removes_session_pref)
    run("area bar empty submit clears pref",
        test_area_bar_empty_submit_clears_pref)
    run("next visit after clear has no city_zip",
        test_next_visit_after_clear_has_no_city_zip)
    run("non-empty city_zip submission saves pref",
        test_submitting_nonempty_city_zip_saves_pref)
    run("session pref restored when city_zip absent from URL",
        test_session_pref_restored_when_city_zip_absent_from_url)
    run("homepage area bar blank after clear",
        test_homepage_area_bar_blank_after_clear)

    print(f"\n{'='*60}")
    print(f"Results: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nFailures:")
        for name, exc in FAIL:
            print(f"  {name}: {exc}")
        sys.exit(1)
    else:
        print("All tests passed.")
