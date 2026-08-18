"""
Task 212 validation: City/ZIP bar visibility after type-filter tab clicks.

Run with:  python tests/test_area_bar_tab_visibility.py

Verifies:
1. /marketplace (All tab)           → City/ZIP bar IS shown
2. /marketplace?listing_type=item&no_vehicles=1 (Items tab)
                                    → City/ZIP bar IS shown
3. /marketplace?listing_type=housing (Housing tab)
                                    → City/ZIP bar IS shown
4. /marketplace?city_zip=Edina      → City/ZIP bar is HIDDEN
                                      (Filters pill shows the value instead)
5. /marketplace?q=sofa              → City/ZIP bar is HIDDEN (text search active)
6. /marketplace?area=twin-cities    → City/ZIP bar is HIDDEN (area pill active)
7. /marketplace?listing_type=item&no_vehicles=1&city_zip=Edina
                                    → City/ZIP bar is HIDDEN even on Items tab

The bar HTML marker is the class 'mp-homepage-area-bar'.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flask_login.utils as _flu
from app import app
import routes  # noqa: F401 — registers routes on app
from models import db, User

results = []


def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


# The CSS class name '.mp-homepage-area-bar' appears in the <style> block on
# every page load, so we must match the actual <form> element, not just the
# class name string.
BAR_MARKER = '<form method="get" action="/marketplace" class="mp-homepage-area-bar"'


def _make_user(uid, user_type='customer'):
    u = db.session.get(User, uid)
    if not u:
        u = User(
            id=uid,
            email=f'{uid}@example.com',
            first_name='T212',
            user_type=user_type,
            age_confirmed=True,
        )
        db.session.add(u)
        db.session.commit()
    return u


with app.app_context():
    buyer = _make_user('t212-buyer')
    _flu._get_user = lambda: buyer

    client = app.test_client()

    # Ensure no stale city_zip_pref in session before each group
    with client.session_transaction() as sess:
        sess.pop('city_zip_pref', None)

    # ── 1. All tab (no params) ─────────────────────────────────────────────────
    r = client.get('/marketplace')
    html = r.data.decode()
    check('All tab: route returns 200', r.status_code == 200)
    check('All tab: City/ZIP bar is shown', BAR_MARKER in html)

    # ── 2. Items tab ──────────────────────────────────────────────────────────
    r = client.get('/marketplace?listing_type=item&no_vehicles=1')
    html = r.data.decode()
    check('Items tab: route returns 200', r.status_code == 200)
    check('Items tab: City/ZIP bar is shown', BAR_MARKER in html,
          '(bar disappeared after tab click — regression)')

    # ── 3. Housing tab ────────────────────────────────────────────────────────
    r = client.get('/marketplace?listing_type=housing')
    html = r.data.decode()
    check('Housing tab: route returns 200', r.status_code == 200)
    check('Housing tab: City/ZIP bar is shown', BAR_MARKER in html)

    # ── 4. city_zip active → bar hidden, Filters pill shows value ─────────────
    r = client.get('/marketplace?city_zip=Edina')
    html = r.data.decode()
    check('city_zip=Edina: route returns 200', r.status_code == 200)
    check('city_zip=Edina: City/ZIP bar is hidden', BAR_MARKER not in html,
          '(bar should be absent when a location filter is set)')
    check('city_zip=Edina: value appears in Filters pill', 'Edina' in html)

    # ── 5. Text search active → bar hidden ───────────────────────────────────
    with client.session_transaction() as sess:
        sess.pop('city_zip_pref', None)
    r = client.get('/marketplace?q=sofa')
    html = r.data.decode()
    check('q=sofa: route returns 200', r.status_code == 200)
    check('q=sofa: City/ZIP bar is hidden', BAR_MARKER not in html)

    # ── 6. Area filter active → bar hidden ────────────────────────────────────
    with client.session_transaction() as sess:
        sess.pop('city_zip_pref', None)
    r = client.get('/marketplace?area=twin-cities')
    html = r.data.decode()
    check('area=twin-cities: route returns 200', r.status_code == 200)
    check('area=twin-cities: City/ZIP bar is hidden', BAR_MARKER not in html)

    # ── 7. Items tab + city_zip → bar hidden ──────────────────────────────────
    r = client.get('/marketplace?listing_type=item&no_vehicles=1&city_zip=Edina')
    html = r.data.decode()
    check('Items tab + city_zip: route returns 200', r.status_code == 200)
    check('Items tab + city_zip: City/ZIP bar is hidden', BAR_MARKER not in html)

    # ── cleanup ───────────────────────────────────────────────────────────────
    u = db.session.get(User, 't212-buyer')
    if u:
        db.session.delete(u)
        db.session.commit()


failed = [n for n, ok in results if not ok]
print(f'\n{len(results) - len(failed)}/{len(results)} passed')
sys.exit(1 if failed else 0)
