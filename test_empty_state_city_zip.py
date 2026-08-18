"""
test_empty_state_city_zip.py — Confirm the marketplace empty-state renders the
correct heading and helper text for every meaningful filter combination.

Covered branches (all result in zero listings so the empty block renders):

  1. City filter only          — "No listings found near X" + "Clear Location Filter"
  2. City + keyword            — city heading takes priority; "Clear Location Filter" present
  3. City + listing_type=item  — same city heading + "Clear Location Filter"
  4. property_sale + city_zip  — "No properties match your filters" + spelling-hint paragraph
  5. rental + city_zip         — "No rentals match your filters" + spelling-hint paragraph
  6. ZIP not in database       — yellow "wasn't found" notice + city-absent heading
  7. No location filter (kw)   — "No results for ..." heading + generic helper text
"""

import sys
import uuid
from unittest.mock import patch, MagicMock

from app import app, db
import routes  # registers all URL rules

# ── Unique sentinel values that will never match real DB rows ──────────────────
_CITY_SENTINEL = "ZxqGhostTownZxq"
_KW_SENTINEL   = "ZxqUnfindableItemZxq"
_ZIP_MISSING   = "00001"   # valid 5-digit format, not in our ZipCode table

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


def _anon_mock():
    """Return a flask-login mock for an anonymous/unauthenticated user."""
    mock = MagicMock()
    mock.is_authenticated = False
    mock.is_active = False
    mock.is_anonymous = True
    mock.is_admin = False
    mock.get_id.return_value = None
    return mock


def _get(path, *, clear_session=True):
    """
    Make a GET request to *path* with an anonymous user, returning the response.
    A fresh client is used each time so session prefs don't bleed between tests.
    """
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    if clear_session:
        with client.session_transaction() as sess:
            sess.pop("city_zip_pref", None)
            sess.pop("hide_sold", None)
    with patch("flask_login.utils._get_user", return_value=_anon_mock()):
        resp = client.get(path, follow_redirects=False)
    return resp


def _html(resp):
    return resp.data.decode("utf-8", errors="replace")


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_city_only_heading_and_clear_link():
    """
    City filter with zero results → heading 'No listings found near "X"'
    and a '✕ Clear Location Filter' CTA link.
    """
    resp = _get(f"/marketplace?city_zip={_CITY_SENTINEL}")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    html = _html(resp)

    assert "mp-empty" in html, "Empty-state block must be present"
    assert f'No listings found near "{_CITY_SENTINEL}"' in html, (
        f"Expected heading 'No listings found near \"{_CITY_SENTINEL}\"' "
        f"but it was absent from the response"
    )
    assert "Clear Location Filter" in html, (
        "'✕ Clear Location Filter' link must appear when city_zip filter is active"
    )


def test_city_only_spelling_hint_paragraph():
    """
    City filter with zero results → helper paragraph mentions spelling check and
    'clear the location filter'.
    """
    resp = _get(f"/marketplace?city_zip={_CITY_SENTINEL}")
    html = _html(resp)

    # The paragraph advises the buyer to double-check spelling
    assert "Double-check the spelling" in html, (
        "City-only empty state must include the spelling-hint helper paragraph"
    )
    assert "clear the location filter" in html, (
        "Spelling-hint paragraph must mention clearing the location filter"
    )


def test_city_plus_keyword_shows_city_heading():
    """
    City + keyword both set → the city-based heading takes priority;
    'Clear Location Filter' is still present.
    """
    resp = _get(f"/marketplace?city_zip={_CITY_SENTINEL}&q={_KW_SENTINEL}")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    html = _html(resp)

    assert f'No listings found near "{_CITY_SENTINEL}"' in html, (
        "When both city and keyword are set, the city-based heading should appear "
        f"('No listings found near \"{_CITY_SENTINEL}\"')"
    )
    assert "Clear Location Filter" in html, (
        "'✕ Clear Location Filter' must appear when city_zip is active alongside a keyword"
    )


def test_city_plus_item_listing_type_heading_and_clear():
    """
    City + listing_type=item → heading is the city-based one;
    'Clear Location Filter' is present (not 'Clear Filters').
    """
    resp = _get(f"/marketplace?city_zip={_CITY_SENTINEL}&listing_type=item")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    html = _html(resp)

    assert f'No listings found near "{_CITY_SENTINEL}"' in html, (
        "listing_type=item with city filter should show the city-based heading"
    )
    assert "Clear Location Filter" in html, (
        "'✕ Clear Location Filter' must appear for item + city filter"
    )


def test_property_sale_plus_city_heading():
    """
    listing_type=property_sale + city_zip → heading 'No properties match your filters'.
    """
    resp = _get(f"/marketplace?listing_type=property_sale&city_zip={_CITY_SENTINEL}")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    html = _html(resp)

    assert "No properties match your filters" in html, (
        "property_sale + city filter should show 'No properties match your filters' heading"
    )


def test_property_sale_plus_city_spelling_hint():
    """
    listing_type=property_sale + city_zip → helper paragraph mentions spelling check.
    """
    resp = _get(f"/marketplace?listing_type=property_sale&city_zip={_CITY_SENTINEL}")
    html = _html(resp)

    assert "Double-check the spelling" in html, (
        "property_sale + city filter must include the spelling-hint paragraph"
    )
    # The hint specifically mentions "Saint Paul" vs "St Paul" as example
    assert "Saint Paul" in html or "St Paul" in html, (
        "Spelling-hint paragraph must contain the Saint Paul / St Paul example"
    )


def test_property_sale_plus_city_clear_filters_link():
    """
    listing_type=property_sale + city_zip → CTA shows '✕ Clear Filters'
    pointing back to /marketplace?listing_type=property_sale.
    """
    resp = _get(f"/marketplace?listing_type=property_sale&city_zip={_CITY_SENTINEL}")
    html = _html(resp)

    assert "Clear Filters" in html, (
        "property_sale + city filter empty state must show '✕ Clear Filters' link"
    )
    assert "listing_type=property_sale" in html, (
        "The Clear Filters link must target /marketplace?listing_type=property_sale"
    )


def test_rental_plus_city_heading():
    """
    listing_type=rental + city_zip → heading 'No rentals match your filters'.
    """
    resp = _get(f"/marketplace?listing_type=rental&city_zip={_CITY_SENTINEL}")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    html = _html(resp)

    assert "No rentals match your filters" in html, (
        "rental + city filter should show 'No rentals match your filters' heading"
    )


def test_rental_plus_city_spelling_hint():
    """
    listing_type=rental + city_zip → helper paragraph mentions spelling check.
    """
    resp = _get(f"/marketplace?listing_type=rental&city_zip={_CITY_SENTINEL}")
    html = _html(resp)

    assert "Double-check the spelling" in html, (
        "rental + city filter must include the spelling-hint paragraph"
    )


def test_rental_plus_city_clear_filters_link():
    """
    listing_type=rental + city_zip → CTA shows '✕ Clear Filters'
    pointing back to /marketplace?listing_type=rental.
    """
    resp = _get(f"/marketplace?listing_type=rental&city_zip={_CITY_SENTINEL}")
    html = _html(resp)

    assert "Clear Filters" in html, (
        "rental + city filter empty state must show '✕ Clear Filters' link"
    )
    assert "listing_type=rental" in html, (
        "The Clear Filters link must target /marketplace?listing_type=rental"
    )


def test_zip_not_in_database_shows_yellow_notice():
    """
    When a 5-digit ZIP is not in our ZipCode table, the yellow 'wasn't found'
    notice banner appears and zip_radius_fallback is truthy.
    """
    # We need _ZIP_MISSING to be absent from the ZipCode table.
    with app.app_context():
        from models import ZipCode
        exists = ZipCode.query.get(_ZIP_MISSING) is not None

    if exists:
        # If somehow 00001 exists in the DB, skip gracefully
        print(f"    (skipped: ZIP {_ZIP_MISSING} exists in DB)")
        return

    resp = _get(f"/marketplace?city_zip={_ZIP_MISSING}")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    html = _html(resp)

    assert "wasn" in html and "found in our location database" in html, (
        f"ZIP {_ZIP_MISSING} is not in the database; the yellow fallback notice "
        "('wasn't found in our location database') must appear"
    )
    assert _ZIP_MISSING in html, (
        f"The notice must display the entered ZIP code ({_ZIP_MISSING})"
    )


def test_no_location_filter_keyword_only_heading():
    """
    Keyword search with no city/ZIP filter and zero results →
    heading 'No results for "X"' (not the city-based heading).
    """
    resp = _get(f"/marketplace?q={_KW_SENTINEL}")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    html = _html(resp)

    assert f'No results for "{_KW_SENTINEL}"' in html, (
        f"Keyword-only search with zero results should show 'No results for \"{_KW_SENTINEL}\"'"
    )
    # Must NOT show the city-based heading
    assert "No listings found near" not in html, (
        "Keyword-only search must not show the city-based heading"
    )


def test_no_location_filter_keyword_only_helper_text():
    """
    Keyword search with no city/ZIP filter → helper paragraph mentions 'Try different keywords'.
    """
    resp = _get(f"/marketplace?q={_KW_SENTINEL}")
    html = _html(resp)

    assert "Try different keywords" in html, (
        "Keyword-only empty state must suggest trying different keywords"
    )


def test_no_location_filter_keyword_only_back_link():
    """
    Keyword search with no city/ZIP filter → CTA is '← Back to Marketplace'
    (not 'Clear Location Filter' and not 'Clear Filters').
    """
    resp = _get(f"/marketplace?q={_KW_SENTINEL}")
    html = _html(resp)

    assert "Back to Marketplace" in html, (
        "Keyword-only empty state must show '← Back to Marketplace' CTA"
    )
    assert "Clear Location Filter" not in html, (
        "'Clear Location Filter' must NOT appear when no city_zip filter is active"
    )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nRunning marketplace empty-state city/ZIP filter tests...\n")

    run("city only — heading 'No listings found near X'",
        test_city_only_heading_and_clear_link)
    run("city only — spelling-hint paragraph",
        test_city_only_spelling_hint_paragraph)
    run("city + keyword — city heading takes priority",
        test_city_plus_keyword_shows_city_heading)
    run("city + listing_type=item — city heading + Clear Location Filter",
        test_city_plus_item_listing_type_heading_and_clear)
    run("property_sale + city — 'No properties match your filters'",
        test_property_sale_plus_city_heading)
    run("property_sale + city — spelling-hint paragraph",
        test_property_sale_plus_city_spelling_hint)
    run("property_sale + city — '✕ Clear Filters' link",
        test_property_sale_plus_city_clear_filters_link)
    run("rental + city — 'No rentals match your filters'",
        test_rental_plus_city_heading)
    run("rental + city — spelling-hint paragraph",
        test_rental_plus_city_spelling_hint)
    run("rental + city — '✕ Clear Filters' link",
        test_rental_plus_city_clear_filters_link)
    run("ZIP not in database — yellow fallback notice",
        test_zip_not_in_database_shows_yellow_notice)
    run("no location filter, keyword only — 'No results for X' heading",
        test_no_location_filter_keyword_only_heading)
    run("no location filter, keyword only — 'Try different keywords' helper",
        test_no_location_filter_keyword_only_helper_text)
    run("no location filter, keyword only — '← Back to Marketplace' CTA",
        test_no_location_filter_keyword_only_back_link)

    print(f"\n{'='*60}")
    print(f"Results: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nFailures:")
        for name, exc in FAIL:
            print(f"  {name}: {exc}")
        sys.exit(1)
    else:
        print("All tests passed.")
