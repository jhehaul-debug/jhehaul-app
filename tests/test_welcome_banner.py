"""
Task 85 validation: welcome banner appears exactly once for new members.

Run with:  python tests/test_welcome_banner.py

Background:
  set_role() sets session['new_member'] = True AND user.user_type = 'customer'
  then redirects to /marketplace.  marketplace() (non-search path) and home()
  pop the flag with session.pop('new_member', False), so it fires exactly once.

Verifies:
- A brand-new user sees show_welcome=True on their first marketplace render.
- Re-visiting /marketplace immediately after does NOT show the banner (flag consumed).
- Re-visiting / (home) also does NOT show the banner after flag is consumed.
- An existing user (user_type set before login, no flag ever placed) never sees it.
- A returning user after session restart (flag absent) never sees the banner.
- The welcome HTML block is actually present/absent in the rendered output.
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
    """Upsert a test user and return the live model instance."""
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
    # Re-query so SQLAlchemy tracks it in the current session
    return db.session.get(User, uid)


with app.app_context():
    client = app.test_client()

    # ── 1 & 2: brand-new user — banner appears once, then never again ─────────
    # Simulate the state immediately after set_role() completes:
    #   • user.user_type = 'customer'  (already persisted)
    #   • session['new_member'] = True  (set by set_role)
    new_user = _make_user('t85-new-user', user_type='customer')
    _flu._get_user = lambda: new_user

    with client.session_transaction() as sess:
        sess['new_member'] = True

    r1 = client.get('/marketplace')
    check('new member: first /marketplace returns 200',
          r1.status_code == 200,
          f'status={r1.status_code}')
    check('new member: welcome banner rendered on first visit',
          b'welcome-banner' in r1.data,
          'banner HTML block not found in response')

    # ── 2: immediately re-visit — flag must be consumed ───────────────────────
    r2 = client.get('/marketplace')
    check('new member: second /marketplace returns 200',
          r2.status_code == 200,
          f'status={r2.status_code}')
    check('new member: welcome banner absent on second visit',
          b'welcome-banner' not in r2.data,
          'banner still rendered after flag should be consumed')

    # ── 3: / (home) also does not show banner — flag already gone ─────────────
    r3 = client.get('/')
    check('new member: / (home) after flag consumed returns 200',
          r3.status_code == 200,
          f'status={r3.status_code}')
    check('new member: welcome banner absent on / after flag consumed',
          b'welcome-banner' not in r3.data,
          'banner unexpectedly present on home after flag consumed')

    # ── 4: existing user — user_type was already set; flag never placed ────────
    existing_user = _make_user('t85-existing-user', user_type='customer')
    _flu._get_user = lambda: existing_user

    with client.session_transaction() as sess:
        sess.pop('new_member', None)   # ensure no stale flag

    r4 = client.get('/marketplace')
    check('existing user: /marketplace returns 200',
          r4.status_code == 200,
          f'status={r4.status_code}')
    check('existing user: no welcome banner',
          b'welcome-banner' not in r4.data,
          'banner shown to existing user who should never see it')

    r4h = client.get('/')
    check('existing user: / (home) returns 200',
          r4h.status_code == 200,
          f'status={r4h.status_code}')
    check('existing user: no welcome banner on home',
          b'welcome-banner' not in r4h.data,
          'banner shown on home for existing user')

    # ── 5: session-restart simulation — flag absent → no banner ───────────────
    # Covers the race: set_role set the flag but session was lost before the
    # first marketplace render (server restart, cookie expiry, etc.).
    restart_user = _make_user('t85-restart-user', user_type='customer')
    _flu._get_user = lambda: restart_user

    with client.session_transaction() as sess:
        sess.pop('new_member', None)   # no flag — session was wiped

    r5 = client.get('/marketplace')
    check('restart simulation: /marketplace returns 200',
          r5.status_code == 200,
          f'status={r5.status_code}')
    check('restart simulation: no welcome banner when flag absent',
          b'welcome-banner' not in r5.data,
          'banner shown after simulated session restart with no flag')

    # ── 6: one-shot — flag consumed on /marketplace, absent on / ──────────────
    oneshot_user = _make_user('t85-oneshot-user', user_type='customer')
    _flu._get_user = lambda: oneshot_user

    with client.session_transaction() as sess:
        sess['new_member'] = True

    r6a = client.get('/marketplace')
    r6b = client.get('/')
    check('one-shot: first /marketplace shows banner',
          b'welcome-banner' in r6a.data,
          'banner missing on first visit')
    check('one-shot: subsequent / does not show banner',
          b'welcome-banner' not in r6b.data,
          'banner persisted to / after being consumed on /marketplace')

    # ── 7: Cache-Control: no-store prevents back-button replay ────────────────
    # Both / and /marketplace must set Cache-Control: no-store so the browser
    # never serves a stale cached response that still contains the banner HTML.
    # These checks apply even when the flag is absent (the header must always
    # be present on the homepage path, not only when the banner fires).
    cache_user = _make_user('t85-cache-user', user_type='customer')
    _flu._get_user = lambda: cache_user

    with client.session_transaction() as sess:
        sess.pop('new_member', None)  # no flag — normal returning user

    r7a = client.get('/marketplace')
    cc_mp = r7a.headers.get('Cache-Control', '')
    check('cache-control: /marketplace has no-store header',
          'no-store' in cc_mp,
          f'Cache-Control was: {cc_mp!r}')

    r7b = client.get('/')
    cc_home = r7b.headers.get('Cache-Control', '')
    check('cache-control: / has no-store header',
          'no-store' in cc_home,
          f'Cache-Control was: {cc_home!r}')

    # Also verify the header is present when the banner DOES fire (flag set).
    with client.session_transaction() as sess:
        sess['new_member'] = True

    r7c = client.get('/marketplace')
    cc_mp_banner = r7c.headers.get('Cache-Control', '')
    check('cache-control: /marketplace has no-store when banner fires',
          'no-store' in cc_mp_banner,
          f'Cache-Control was: {cc_mp_banner!r}')

    with client.session_transaction() as sess:
        sess['new_member'] = True

    r7d = client.get('/')
    cc_home_banner = r7d.headers.get('Cache-Control', '')
    check('cache-control: / has no-store when banner fires',
          'no-store' in cc_home_banner,
          f'Cache-Control was: {cc_home_banner!r}')


# ── Summary ───────────────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
