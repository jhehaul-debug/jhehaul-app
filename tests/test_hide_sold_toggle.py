"""
Task 98 validation: hide-sold toggle persists correctly on the homepage after reload.

Run with:  python tests/test_hide_sold_toggle.py

Verifies:
1. Clicking hide_sold=1 on the homepage (/) sets session and filters listings.
2. Reloading / without query params still shows the toggle active (reads session).
3. Clicking hide_sold=0 resets the preference (session cleared, all shown).
4. Reloading / after reset shows toggle inactive again.
5. Toggling on /marketplace homepage (non-search) behaves the same way.
6. Toggling hide_sold in a search result (/marketplace?q=x&hide_sold=1) writes
   to the same session key, so navigating back to / shows the updated preference.
7. Toggling hide_sold=0 in search also clears the session key.
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


def _make_user(uid, user_type='customer', is_admin=False):
    u = User(
        id=uid,
        email=f'{uid}@example.com',
        first_name='Test',
        is_admin=is_admin,
        age_confirmed=True,
    )
    u.user_type = user_type
    db.session.merge(u)
    db.session.commit()
    return db.session.get(User, uid)


with app.app_context():
    client = app.test_client()

    # ── Set up a plain buyer user ─────────────────────────────────────────────
    user = _make_user('t98-buyer', user_type='customer')
    _flu._get_user = lambda: user

    # Start with a clean session (no hide_sold preference)
    with client.session_transaction() as sess:
        sess.pop('hide_sold', None)

    # ── 1. Activate hide_sold on / ────────────────────────────────────────────
    r1 = client.get('/?hide_sold=1')
    check('activate on /: returns 200', r1.status_code == 200,
          f'status={r1.status_code}')
    check('activate on /: toggle button shows "Hiding sold & reserved"',
          b'Hiding sold' in r1.data,
          'active toggle label not found in page')
    check('activate on /: toggle button shows "Show active only" absent',
          b'Show active only' not in r1.data,
          'inactive toggle still rendered after activation')

    # Confirm session was set
    with client.session_transaction() as sess:
        check('activate on /: session[hide_sold] is True',
              sess.get('hide_sold') is True,
              f"session value={sess.get('hide_sold')!r}")

    # ── 2. Reload / without any query param — preference must persist ──────────
    r2 = client.get('/')
    check('persist on reload /: returns 200', r2.status_code == 200,
          f'status={r2.status_code}')
    check('persist on reload /: toggle still shows "Hiding sold & reserved"',
          b'Hiding sold' in r2.data,
          'preference was lost after page reload (no query param)')

    # ── 3. Deactivate via hide_sold=0 on / ───────────────────────────────────
    r3 = client.get('/?hide_sold=0')
    check('deactivate on /: returns 200', r3.status_code == 200,
          f'status={r3.status_code}')
    check('deactivate on /: toggle shows "Show active only"',
          b'Show active only' in r3.data,
          'inactive toggle label not found after deactivation')
    check('deactivate on /: "Hiding sold & reserved" absent',
          b'Hiding sold' not in r3.data,
          'active toggle still rendered after deactivation')

    with client.session_transaction() as sess:
        check('deactivate on /: session[hide_sold] is False',
              sess.get('hide_sold') is False,
              f"session value={sess.get('hide_sold')!r}")

    # ── 4. Reload / again — reset must persist ────────────────────────────────
    r4 = client.get('/')
    check('persist reset on reload /: "Show active only" still shown',
          b'Show active only' in r4.data,
          'reset preference was lost after reload')

    # ── 5. Same flow on /marketplace (homepage branch, no search params) ──────
    with client.session_transaction() as sess:
        sess.pop('hide_sold', None)

    r5a = client.get('/marketplace?hide_sold=1')
    check('activate on /marketplace: returns 200', r5a.status_code == 200,
          f'status={r5a.status_code}')
    check('activate on /marketplace: "Hiding sold & reserved" present',
          b'Hiding sold' in r5a.data,
          'toggle not activated on /marketplace')

    r5b = client.get('/marketplace')
    check('persist on /marketplace reload: "Hiding sold & reserved" present',
          b'Hiding sold' in r5b.data,
          'preference lost after /marketplace reload')

    r5c = client.get('/marketplace?hide_sold=0')
    check('deactivate on /marketplace: "Show active only" present',
          b'Show active only' in r5c.data,
          'toggle not deactivated on /marketplace')

    # ── 6. Toggle in search syncs back to homepage ────────────────────────────
    with client.session_transaction() as sess:
        sess.pop('hide_sold', None)

    # Perform a search with hide_sold=1
    r6a = client.get('/marketplace?q=chair&hide_sold=1')
    check('search with hide_sold=1: returns 200', r6a.status_code == 200,
          f'status={r6a.status_code}')

    with client.session_transaction() as sess:
        check('search hide_sold=1: session[hide_sold] is True',
              sess.get('hide_sold') is True,
              f"session value after search={sess.get('hide_sold')!r}")

    # Navigate back to / — should reflect session preference set by search
    r6b = client.get('/')
    check('homepage after search toggle: "Hiding sold & reserved" present',
          b'Hiding sold' in r6b.data,
          'search toggle did not sync to homepage (session not updated by search branch)')

    # ── 7. Deactivate in search also clears session ───────────────────────────
    r7a = client.get('/marketplace?q=chair&hide_sold=0')
    check('search with hide_sold=0: returns 200', r7a.status_code == 200,
          f'status={r7a.status_code}')

    with client.session_transaction() as sess:
        check('search hide_sold=0: session[hide_sold] is False',
              sess.get('hide_sold') is False,
              f"session value after search reset={sess.get('hide_sold')!r}")

    r7b = client.get('/')
    check('homepage after search reset: "Show active only" present',
          b'Show active only' in r7b.data,
          'search reset did not sync back to homepage')


# ── Summary ───────────────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
