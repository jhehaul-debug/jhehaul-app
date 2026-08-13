"""
Task 107 validation: heart/save buttons stay in sync across card and detail page.

Run with:  python tests/test_heart_sync.py

Verifies:
1. AJAX toggle (card-style) adds a favorite — returns favorited=True, count increments.
2. AJAX toggle a second time removes the favorite — returns favorited=False, count decrements.
3. Non-AJAX POST (detail-page form submit) adds and removes correctly; redirects back.
4. Saved Items page lists the listing after a save, and no longer lists it after an unsave.
5. Unauthenticated request redirects to invite/login page, not 401 or 500.
6. Toggling with an Accept: application/json header also returns JSON (detail-page JS path).
7. Count floor: removing when stored count is already 0 keeps count at 0 (no underflow).
8. Cross-surface: save via card AJAX → detail page renders "Saved"/"favorited" class.
9. Cross-surface: unsave via detail AJAX → marketplace card renders aria-pressed=false/🤍.
10. Idempotency: card + detail toggling converges correctly (exactly one DB row, then zero).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch
import flask_login.utils as _flu
from app import app, db
import routes  # noqa: F401 — registers routes on app
from models import User, Listing, ListingFavorite

results = []


def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_user(uid, user_type='customer'):
    u = User(
        id=uid,
        email=f'{uid}@example.com',
        first_name='Test',
        age_confirmed=True,
        user_type=user_type,
    )
    db.session.merge(u)
    db.session.commit()
    return db.session.get(User, uid)


def _make_listing(lid, seller_id, status='active'):
    lst = Listing(
        id=lid,
        seller_id=seller_id,
        title=f'Heart Sync Test Listing {lid}',
        price=50.0,
        price_type='fixed',
        status=status,
        moderation_status='approved',
        listing_type='item',
    )
    db.session.merge(lst)
    lst_obj = db.session.get(Listing, lid)
    lst_obj.favorite_count = 0
    db.session.commit()
    return db.session.get(Listing, lid)


def _clear_favorites(user_id, listing_id):
    ListingFavorite.query.filter_by(user_id=user_id, listing_id=listing_id).delete()
    db.session.commit()


def _seed_favorite(user_id, listing_id):
    """Insert a ListingFavorite row directly without going through the endpoint."""
    fav = ListingFavorite(user_id=user_id, listing_id=listing_id)
    db.session.merge(fav)
    db.session.commit()


AJAX_HEADERS = {
    'X-Requested-With': 'XMLHttpRequest',
    'Content-Type': 'application/x-www-form-urlencoded',
}
FORM_HEADERS = {
    'Content-Type': 'application/x-www-form-urlencoded',
}
JSON_ACCEPT_HEADERS = {
    'Accept': 'application/json',
    'Content-Type': 'application/x-www-form-urlencoded',
}

# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

with app.app_context():
    client = app.test_client()

    seller = _make_user('t107-seller')
    buyer  = _make_user('t107-buyer')

    listing  = _make_listing(10701, 't107-seller')
    listing2 = _make_listing(10702, 't107-seller')
    listing3 = _make_listing(10703, 't107-seller')

    # Patch CSRF for all POST requests in these tests
    with patch('routes._check_listing_csrf', return_value=None):

        # ── 1. AJAX toggle: add favorite ──────────────────────────────────────
        _clear_favorites('t107-buyer', 10701)
        listing.favorite_count = 0
        db.session.commit()

        _flu._get_user = lambda: buyer
        r1 = client.post(
            f'/listing/{listing.id}/favorite',
            headers=AJAX_HEADERS,
            data='csrf_token=test',
        )
        check('AJAX add: returns 200', r1.status_code == 200,
              f'status={r1.status_code}')
        check('AJAX add: Content-Type is JSON', 'application/json' in r1.content_type,
              f'content_type={r1.content_type}')
        data1 = r1.get_json()
        check('AJAX add: favorited=True', data1 and data1.get('favorited') is True,
              f'response={data1}')
        check('AJAX add: count=1', data1 and data1.get('count') == 1,
              f'count={data1.get("count") if data1 else "N/A"}')

        fav_row = ListingFavorite.query.filter_by(
            user_id='t107-buyer', listing_id=10701).first()
        check('AJAX add: ListingFavorite row created in DB', fav_row is not None,
              'no row found in DB after AJAX save')

        # ── 2. AJAX toggle: remove favorite ───────────────────────────────────
        r2 = client.post(
            f'/listing/{listing.id}/favorite',
            headers=AJAX_HEADERS,
            data='csrf_token=test',
        )
        check('AJAX remove: returns 200', r2.status_code == 200,
              f'status={r2.status_code}')
        data2 = r2.get_json()
        check('AJAX remove: favorited=False', data2 and data2.get('favorited') is False,
              f'response={data2}')
        check('AJAX remove: count=0', data2 and data2.get('count') == 0,
              f'count={data2.get("count") if data2 else "N/A"}')

        fav_row2 = ListingFavorite.query.filter_by(
            user_id='t107-buyer', listing_id=10701).first()
        check('AJAX remove: ListingFavorite row deleted from DB', fav_row2 is None,
              'row still present in DB after AJAX unsave')

        # ── 3. Non-AJAX (detail-page form) POST: add then remove ──────────────
        _clear_favorites('t107-buyer', 10701)
        listing.favorite_count = 0
        db.session.commit()

        _flu._get_user = lambda: buyer
        r3a = client.post(
            f'/listing/{listing.id}/favorite',
            headers=FORM_HEADERS,
            data='csrf_token=test',
            follow_redirects=False,
        )
        check('Form add: redirects (3xx)', r3a.status_code in (301, 302, 303),
              f'status={r3a.status_code}')
        loc3a = r3a.headers.get('Location', '')
        check('Form add: redirect location is local',
              loc3a.startswith('/') or 'localhost' in loc3a or 'listing' in loc3a,
              f'Location={loc3a}')

        fav_row3 = ListingFavorite.query.filter_by(
            user_id='t107-buyer', listing_id=10701).first()
        check('Form add: ListingFavorite row created', fav_row3 is not None,
              'no DB row after non-AJAX form POST')

        # Remove via form
        r3b = client.post(
            f'/listing/{listing.id}/favorite',
            headers=FORM_HEADERS,
            data='csrf_token=test',
            follow_redirects=False,
        )
        check('Form remove: redirects (3xx)', r3b.status_code in (301, 302, 303),
              f'status={r3b.status_code}')

        fav_row3b = ListingFavorite.query.filter_by(
            user_id='t107-buyer', listing_id=10701).first()
        check('Form remove: ListingFavorite row deleted', fav_row3b is None,
              'row still present after non-AJAX form remove')

        # ── 4. Saved Items page reflects save/unsave ───────────────────────────
        _clear_favorites('t107-buyer', 10701)
        listing.favorite_count = 0
        db.session.commit()

        _flu._get_user = lambda: buyer

        # Add via AJAX
        client.post(f'/listing/{listing.id}/favorite',
                    headers=AJAX_HEADERS, data='csrf_token=test')

        r4a = client.get('/saved', follow_redirects=False)
        check('Saved Items after save: returns 200', r4a.status_code == 200,
              f'status={r4a.status_code}')
        html4a = r4a.data.decode('utf-8', errors='replace')
        check('Saved Items after save: listing title present',
              'Heart Sync Test Listing 10701' in html4a,
              'listing title not found in Saved Items after save')

        # Remove via AJAX
        client.post(f'/listing/{listing.id}/favorite',
                    headers=AJAX_HEADERS, data='csrf_token=test')

        r4b = client.get('/saved', follow_redirects=False)
        check('Saved Items after unsave: returns 200', r4b.status_code == 200,
              f'status={r4b.status_code}')
        html4b = r4b.data.decode('utf-8', errors='replace')
        check('Saved Items after unsave: listing title absent',
              'Heart Sync Test Listing 10701' not in html4b,
              'listing still appears in Saved Items after unsave')

        # ── 5. Unauthenticated request redirects (not 401 / 500) ──────────────
        from flask_login import AnonymousUserMixin
        _flu._get_user = lambda: AnonymousUserMixin()

        r5 = client.post(
            f'/listing/{listing.id}/favorite',
            headers=FORM_HEADERS,
            data='csrf_token=test',
            follow_redirects=False,
        )
        check('Unauthenticated: redirects (3xx)', r5.status_code in (301, 302, 303),
              f'status={r5.status_code} — expected redirect to login/invite')
        check('Unauthenticated: not a 401/500', r5.status_code not in (401, 500),
              f'status={r5.status_code}')
        loc5 = r5.headers.get('Location', '')
        check('Unauthenticated: redirects to invite or login',
              'invite' in loc5 or 'login' in loc5,
              f'Location={loc5}')

        # ── 6. Accept: application/json header also returns JSON ───────────────
        _clear_favorites('t107-buyer', 10701)
        listing.favorite_count = 0
        db.session.commit()

        _flu._get_user = lambda: buyer
        r6 = client.post(
            f'/listing/{listing.id}/favorite',
            headers=JSON_ACCEPT_HEADERS,
            data='csrf_token=test',
        )
        check('JSON Accept header: returns 200', r6.status_code == 200,
              f'status={r6.status_code}')
        data6 = r6.get_json()
        check('JSON Accept header: returns JSON with favorited key',
              data6 is not None and 'favorited' in data6,
              f'response={data6}')

        _clear_favorites('t107-buyer', 10701)

        # ── 7. True count-floor: remove when stored count is already 0 ────────
        # Seed a ListingFavorite row directly but leave favorite_count=0 to simulate
        # an inconsistent/corrupt state. The route must clamp at 0, not go negative.
        _clear_favorites('t107-buyer', 10701)
        listing.favorite_count = 0
        db.session.commit()
        _seed_favorite('t107-buyer', 10701)  # row exists but count stays at 0

        _flu._get_user = lambda: buyer
        r7 = client.post(
            f'/listing/{listing.id}/favorite',
            headers=AJAX_HEADERS,
            data='csrf_token=test',
        )
        data7 = r7.get_json()
        check('Count floor: returns 200', r7.status_code == 200,
              f'status={r7.status_code}')
        check('Count floor: favorited=False (row removed)', data7 and data7.get('favorited') is False,
              f'response={data7}')
        check('Count floor: count >= 0 (never negative)',
              data7 is not None and data7.get('count', -1) >= 0,
              f'count={data7.get("count") if data7 else "N/A"}')

        db.session.refresh(listing)
        check('Count floor: persisted favorite_count >= 0',
              (listing.favorite_count or 0) >= 0,
              f'persisted count={listing.favorite_count}')

        _clear_favorites('t107-buyer', 10701)

        # ── 8. Cross-surface: save via card AJAX → detail page shows "Saved" ──
        # Simulate the card heart-click path: buyer saves from the marketplace grid.
        _clear_favorites('t107-buyer', 10702)
        listing2.favorite_count = 0
        db.session.commit()

        _flu._get_user = lambda: buyer

        # Card save (AJAX POST, same as mpToggleSave JS in marketplace.html)
        r8_save = client.post(
            f'/listing/{listing2.id}/favorite',
            headers=AJAX_HEADERS,
            data='csrf_token=test',
        )
        data8_save = r8_save.get_json()
        check('Cross-surface (card→detail): AJAX save returns favorited=True',
              data8_save and data8_save.get('favorited') is True,
              f'response={data8_save}')

        # Now GET the detail page — server renders is_favorited based on DB state.
        r8_detail = client.get(f'/listing/{listing2.id}', follow_redirects=False)
        check('Cross-surface (card→detail): detail page loads 200',
              r8_detail.status_code == 200,
              f'status={r8_detail.status_code}')
        html8_detail = r8_detail.data.decode('utf-8', errors='replace')

        # Detail page renders `class="btn-fav favorited"` and label "Saved"
        check('Cross-surface (card→detail): detail page shows "favorited" CSS class',
              'btn-fav favorited' in html8_detail,
              'class "btn-fav favorited" not found — detail page does not reflect card save')
        check('Cross-surface (card→detail): detail page shows "Saved" label',
              '>Saved<' in html8_detail,
              '"Saved" label not found in detail page after card save')

        # ── 9. Cross-surface: unsave via detail AJAX → marketplace renders unsaved card ──
        # The detail page uses the same /listing/<id>/favorite endpoint with AJAX.
        r9_unsave = client.post(
            f'/listing/{listing2.id}/favorite',
            headers=AJAX_HEADERS,
            data='csrf_token=test',
        )
        data9_unsave = r9_unsave.get_json()
        check('Cross-surface (detail→card): AJAX unsave returns favorited=False',
              data9_unsave and data9_unsave.get('favorited') is False,
              f'response={data9_unsave}')

        # GET the marketplace page — the card should render aria-pressed="false"
        r9_mp = client.get('/marketplace', follow_redirects=False)
        check('Cross-surface (detail→card): marketplace loads 200',
              r9_mp.status_code == 200,
              f'status={r9_mp.status_code}')
        html9_mp = r9_mp.data.decode('utf-8', errors='replace')

        # When listing2 is NOT saved, the card must NOT have aria-pressed="true"
        # for listing2's id. We check the specific button markup for that listing.
        # The heart button includes data-listing-id="{{ listing.id }}" and
        # aria-pressed="{{ 'true' if is_saved else 'false' }}"
        btn_marker_unsaved = f'data-listing-id="{listing2.id}"'
        if btn_marker_unsaved in html9_mp:
            # Find the snippet around that button and assert aria-pressed is false
            idx = html9_mp.find(btn_marker_unsaved)
            snippet = html9_mp[max(0, idx-200):idx+200]
            check('Cross-surface (detail→card): card aria-pressed is false after unsave',
                  'aria-pressed="true"' not in snippet,
                  f'snippet around button: {snippet[:300]!r}')
            check('Cross-surface (detail→card): card shows 🤍 (not ❤️) after unsave',
                  '🤍' in snippet or 'aria-pressed="false"' in snippet,
                  f'snippet: {snippet[:300]!r}')
        else:
            # Listing may not appear on homepage if it's filtered; verify via search
            r9_search = client.get(f'/marketplace?q=Heart+Sync+Test+Listing+{listing2.id}',
                                   follow_redirects=False)
            html9_s = r9_search.data.decode('utf-8', errors='replace')
            btn_marker_unsaved_s = f'data-listing-id="{listing2.id}"'
            if btn_marker_unsaved_s in html9_s:
                idx_s = html9_s.find(btn_marker_unsaved_s)
                snippet_s = html9_s[max(0, idx_s-200):idx_s+200]
                check('Cross-surface (detail→card): search card aria-pressed is false',
                      'aria-pressed="true"' not in snippet_s,
                      f'snippet: {snippet_s[:300]!r}')
            else:
                # Card not rendered on this page; rely on DB truth
                db_row = ListingFavorite.query.filter_by(
                    user_id='t107-buyer', listing_id=listing2.id).first()
                check('Cross-surface (detail→card): DB confirms listing is unsaved',
                      db_row is None,
                      'DB still has a favorite row after unsave')

        _clear_favorites('t107-buyer', 10702)

        # ── 10. Idempotency: card save + detail page save converge correctly ───
        _clear_favorites('t107-buyer', 10703)
        listing3.favorite_count = 0
        db.session.commit()

        _flu._get_user = lambda: buyer

        # First save (card AJAX)
        client.post(f'/listing/{listing3.id}/favorite',
                    headers=AJAX_HEADERS, data='csrf_token=test')

        row_count_after_first = ListingFavorite.query.filter_by(
            user_id='t107-buyer', listing_id=10703).count()
        check('Idempotency: exactly 1 row after first save', row_count_after_first == 1,
              f'row_count={row_count_after_first}')

        # Second toggle (detail page AJAX — same endpoint, same user)
        r10 = client.post(
            f'/listing/{listing3.id}/favorite',
            headers=AJAX_HEADERS,
            data='csrf_token=test',
        )
        data10 = r10.get_json()
        check('Idempotency: second toggle returns favorited=False',
              data10 and data10.get('favorited') is False,
              f'response={data10}')
        rows10 = ListingFavorite.query.filter_by(
            user_id='t107-buyer', listing_id=10703).count()
        check('Idempotency: 0 rows in DB after toggle off', rows10 == 0,
              f'row count={rows10}')
        check('Idempotency: count returned >= 0',
              data10 is not None and data10.get('count', -1) >= 0,
              f'count={data10.get("count") if data10 else "N/A"}')

        _clear_favorites('t107-buyer', 10703)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
