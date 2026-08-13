"""
Task 137 validation: profile-completion nudge appears for new members and
disappears permanently after dismissal.

Run with:  python tests/test_profile_nudge.py

Verifies:
- A new member with no photo and no phone sees the nudge on /marketplace
  after the welcome banner is consumed.
- The nudge is NOT shown on the same page load as the welcome banner.
- Clicking × (POST /profile/nudge/dismiss) sets the DB flag and removes
  the banner from all subsequent responses.
- A user who already has a profile photo never sees the nudge.
- A user who already has a phone number never sees the nudge.
- The nudge also works correctly on / (home route).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flask_login.utils as _flu
from app import app
import routes  # noqa: F401 — registers all routes on app
from models import db, User

results = []


def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, (extra if not cond else ''))


def _make_user(uid, phone=None, profile_image_url=None, profile_photo_data=None,
               nudge_dismissed=False, user_type='customer'):
    """Upsert a test user; return the live model instance."""
    u = User(
        id=uid,
        email=f'{uid}@example.com',
        first_name='Test',
        age_confirmed=True,
        user_type=user_type,
        phone=phone,
        profile_image_url=profile_image_url,
        profile_photo_data=profile_photo_data,
        profile_nudge_dismissed=nudge_dismissed,
    )
    db.session.merge(u)
    db.session.commit()
    return db.session.get(User, uid)


with app.app_context():
    client = app.test_client()

    # ── 1: First login — welcome banner present, nudge must NOT appear ────────
    first_login_user = _make_user('t137-first-login')
    _flu._get_user = lambda: first_login_user

    with client.session_transaction() as sess:
        sess['new_member'] = True

    r1 = client.get('/marketplace')
    check('first login /marketplace returns 200',
          r1.status_code == 200, f'status={r1.status_code}')
    check('first login: welcome banner is present',
          b'welcome-banner' in r1.data,
          'welcome-banner block missing')
    check('first login: nudge is NOT shown alongside welcome banner',
          b'profile-nudge-banner' not in r1.data,
          'nudge incorrectly co-rendered with welcome banner')

    # ── 2: Second visit — welcome flag consumed, nudge must appear ────────────
    r2 = client.get('/marketplace')
    check('second visit /marketplace returns 200',
          r2.status_code == 200, f'status={r2.status_code}')
    check('second visit: welcome banner is gone',
          b'welcome-banner' not in r2.data,
          'welcome banner still present after flag consumed')
    check('second visit: profile nudge is shown',
          b'profile-nudge-banner' in r2.data,
          'nudge not rendered for incomplete-profile user')

    # ── 3: Dismiss endpoint marks DB flag, nudge absent afterwards ────────────
    r_dismiss = client.post('/profile/nudge/dismiss',
                            headers={'X-Requested-With': 'XMLHttpRequest'})
    check('dismiss endpoint returns 204',
          r_dismiss.status_code == 204, f'status={r_dismiss.status_code}')

    # Reload user from DB to check the flag was persisted
    db.session.expire_all()
    updated = db.session.get(User, 't137-first-login')
    check('dismiss: profile_nudge_dismissed set to True in DB',
          updated.profile_nudge_dismissed is True,
          f'flag={updated.profile_nudge_dismissed}')

    # Re-load login context with refreshed user
    _flu._get_user = lambda: updated

    r3 = client.get('/marketplace')
    check('after dismiss /marketplace returns 200',
          r3.status_code == 200, f'status={r3.status_code}')
    check('after dismiss: nudge no longer shown',
          b'profile-nudge-banner' not in r3.data,
          'nudge reappeared after dismissal')

    # ── 4: Nudge also absent on / (home) after dismissal ─────────────────────
    r4 = client.get('/')
    check('after dismiss / (home) returns 200',
          r4.status_code == 200, f'status={r4.status_code}')
    check('after dismiss: nudge absent on home too',
          b'profile-nudge-banner' not in r4.data,
          'nudge reappeared on home after dismissal')

    # ── 5: User with profile photo never sees nudge ───────────────────────────
    photo_user = _make_user('t137-has-photo',
                            profile_image_url='https://example.com/photo.jpg')
    _flu._get_user = lambda: photo_user

    with client.session_transaction() as sess:
        sess.pop('new_member', None)

    r5 = client.get('/marketplace')
    check('user with photo /marketplace returns 200',
          r5.status_code == 200, f'status={r5.status_code}')
    check('user with photo: nudge absent',
          b'profile-nudge-banner' not in r5.data,
          'nudge shown to user who already has a photo')

    # ── 6: User with phone number never sees nudge ────────────────────────────
    phone_user = _make_user('t137-has-phone', phone='6125551234')
    _flu._get_user = lambda: phone_user

    with client.session_transaction() as sess:
        sess.pop('new_member', None)

    r6 = client.get('/marketplace')
    check('user with phone /marketplace returns 200',
          r6.status_code == 200, f'status={r6.status_code}')
    check('user with phone: nudge absent',
          b'profile-nudge-banner' not in r6.data,
          'nudge shown to user who already has a phone')

    # ── 7: Same welcome-banner / nudge invariant holds on / (home) ───────────
    home_new = _make_user('t137-home-new')
    _flu._get_user = lambda: home_new

    with client.session_transaction() as sess:
        sess['new_member'] = True

    r7a = client.get('/')
    check('home first login returns 200',
          r7a.status_code == 200, f'status={r7a.status_code}')
    check('home first login: nudge absent alongside welcome banner',
          b'profile-nudge-banner' not in r7a.data,
          'nudge co-rendered with welcome banner on home route')

    r7b = client.get('/')
    check('home second visit: nudge shown after welcome flag consumed',
          b'profile-nudge-banner' in r7b.data,
          'nudge not rendered on home for incomplete-profile user')


# ── Summary ───────────────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
