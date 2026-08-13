"""
Task 142 validation: reserved-listing watcher notifications fire correctly end-to-end.

Verifies:
- A user who favorited a listing receives a 'listing_reserved' in-app notification
  when the listing is marked reserved via notify_listing_reserved_to_watchers.
- A buyer with an active conversation also receives the notification.
- A buyer with a pending offer (but no favorite / conversation) receives the notification.
- A buyer with a countered offer (but no favorite / conversation) receives the notification.
- A buyer who both favorited AND has a conversation is only notified ONCE (deduplication).
- A buyer who has an offer AND is a favorite/conversation watcher is only notified ONCE.
- The seller themselves is NOT notified.
- The route-level guard prevents a second call when prior_status == 'reserved'.
- The active→reserved route passes offer_buyer_ids to the notifier BEFORE expiring offers.
- Email is attempted for each unique watcher.

IDs: all test user/listing/conversation IDs are UUID-prefixed strings unlikely to
collide with production data.

Run with:  python tests/test_reserved_watcher_notifications.py
"""
import sys
import os
import uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, call as mock_call
import flask_login.utils as _flu
from app import app, db
import routes  # noqa: F401 — registers all routes on app
from models import (
    User, Listing, ListingFavorite, ListingConversation,
    ListingOffer, Notification,
)

results = []

# Collision-safe prefix for all IDs created by this test run
_PFX = 't142-' + uuid.uuid4().hex[:8]


def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uid(label):
    return f'{_PFX}-{label}'


def _make_user(uid, email=None):
    u = User(
        id=uid,
        email=email or f'{uid}@test.example',
        first_name='T142',
        age_confirmed=True,
        user_type='customer',
    )
    db.session.merge(u)
    db.session.commit()
    return db.session.get(User, uid)


def _make_listing(listing_id, seller_id, status='active'):
    from datetime import datetime, timedelta
    lst = Listing(
        id=listing_id,
        seller_id=seller_id,
        title='T142 Reserved Oak Desk',
        price=300.0,
        price_type='fixed',
        status=status,
        moderation_status='approved',
        listing_type='item',
        expires_at=datetime.now() + timedelta(days=30),
    )
    db.session.merge(lst)
    db.session.commit()
    return db.session.get(Listing, listing_id)


def _make_favorite(user_id, listing_id):
    from sqlalchemy.exc import IntegrityError
    try:
        fav = ListingFavorite(user_id=str(user_id), listing_id=listing_id)
        db.session.add(fav)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()


def _make_convo(convo_id, listing_id, buyer_id, seller_id):
    from datetime import datetime
    from sqlalchemy.exc import IntegrityError
    try:
        c = ListingConversation(
            id=convo_id,
            listing_id=listing_id,
            buyer_id=str(buyer_id),
            seller_id=str(seller_id),
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        db.session.merge(c)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
    return db.session.get(ListingConversation, convo_id)


def _make_offer(offer_id, listing_id, buyer_id, seller_id, status='pending'):
    from sqlalchemy.exc import IntegrityError
    try:
        o = ListingOffer(
            id=offer_id,
            listing_id=listing_id,
            buyer_id=str(buyer_id),
            seller_id=str(seller_id),
            amount=150.0,
            status=status,
        )
        db.session.merge(o)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
    return db.session.get(ListingOffer, offer_id)


def _reserved_notifs(user_id):
    return (Notification.query
            .filter_by(user_id=str(user_id), type='listing_reserved')
            .all())


def _cleanup_notifs(*user_ids):
    for uid in user_ids:
        Notification.query.filter_by(user_id=str(uid)).delete()
    db.session.commit()


# ---------------------------------------------------------------------------
# Actor IDs (all collision-safe)
# ---------------------------------------------------------------------------
SELLER_ID      = _uid('seller')
FAV_ID         = _uid('fav')        # favorited only
CONVO_ID       = _uid('convo')      # conversation only
BOTH_ID        = _uid('both')       # favorited + conversation
OFFER_PEND_ID  = _uid('offerpend')  # pending offer only
OFFER_CNTR_ID  = _uid('offercntr')  # countered offer only
OFFER_FAV_ID   = _uid('offerfav')   # offer + favorite (dedup check)

# Integer IDs — fixed large values within PostgreSQL INTEGER range (max 2,147,483,647)
# The t142 prefix in the hex makes collision with real sequential IDs extremely unlikely.
LISTING_ID  = 1_974_200_001
CONVO_PK    = 1_974_200_002
BOTH_PK     = 1_974_200_003
OFFER_PEND  = 1_974_200_004
OFFER_CNTR  = 1_974_200_005
OFFER_FAV_O = 1_974_200_006  # the offer row for OFFER_FAV_ID

ALL_BUYER_IDS = [FAV_ID, CONVO_ID, BOTH_ID, OFFER_PEND_ID, OFFER_CNTR_ID, OFFER_FAV_ID]
ALL_IDS       = [SELLER_ID] + ALL_BUYER_IDS

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
with app.app_context():
    for uid in ALL_IDS:
        _make_user(uid)

    _make_listing(LISTING_ID, SELLER_ID)

    _make_favorite(FAV_ID, LISTING_ID)
    _make_favorite(BOTH_ID, LISTING_ID)
    _make_favorite(OFFER_FAV_ID, LISTING_ID)      # offer buyer also favorited

    _make_convo(CONVO_PK, LISTING_ID, CONVO_ID, SELLER_ID)
    _make_convo(BOTH_PK,  LISTING_ID, BOTH_ID,  SELLER_ID)

    _make_offer(OFFER_PEND, LISTING_ID, OFFER_PEND_ID, SELLER_ID, status='pending')
    _make_offer(OFFER_CNTR, LISTING_ID, OFFER_CNTR_ID, SELLER_ID, status='countered')
    _make_offer(OFFER_FAV_O, LISTING_ID, OFFER_FAV_ID, SELLER_ID, status='pending')

    _cleanup_notifs(*ALL_IDS)

# ---------------------------------------------------------------------------
# Import the function under test after app context is set up
# ---------------------------------------------------------------------------
from notification_service import notify_listing_reserved_to_watchers


# ---------------------------------------------------------------------------
# Test 1: favorited buyer gets a listing_reserved notification
# ---------------------------------------------------------------------------
with app.app_context():
    _cleanup_notifs(*ALL_IDS)

    with patch('email_service.notify_buyer_listing_reserved', return_value=True):
        notify_listing_reserved_to_watchers(LISTING_ID, 'T142 Reserved Oak Desk')

    notifs = _reserved_notifs(FAV_ID)
    check('1: favorited buyer gets listing_reserved notification',
          len(notifs) == 1, f'count={len(notifs)}')
    if notifs:
        n = notifs[0]
        check('1: notification type is listing_reserved',
              n.type == 'listing_reserved', f'type={n.type!r}')
        check('1: notification title mentions the listing title',
              'T142 Reserved Oak Desk' in n.title, f'title={n.title!r}')
        check('1: action_url points to the listing',
              n.action_url == f'/listing/{LISTING_ID}', f'url={n.action_url!r}')
        check('1: related_listing_id set correctly',
              n.related_listing_id == LISTING_ID,
              f'related_listing_id={n.related_listing_id!r}')


# ---------------------------------------------------------------------------
# Test 2: conversation buyer gets a listing_reserved notification
# ---------------------------------------------------------------------------
with app.app_context():
    _cleanup_notifs(*ALL_IDS)

    with patch('email_service.notify_buyer_listing_reserved', return_value=True):
        notify_listing_reserved_to_watchers(LISTING_ID, 'T142 Reserved Oak Desk')

    notifs = _reserved_notifs(CONVO_ID)
    check('2: conversation buyer gets listing_reserved notification',
          len(notifs) == 1, f'count={len(notifs)}')
    if notifs:
        n = notifs[0]
        check('2: notification type is listing_reserved',
              n.type == 'listing_reserved', f'type={n.type!r}')
        check('2: conversation notification links the conversation',
              n.related_conversation_id == CONVO_PK,
              f'related_conversation_id={n.related_conversation_id!r}')


# ---------------------------------------------------------------------------
# Test 3: buyer with a pending offer (no favorite / conversation) gets notified
# ---------------------------------------------------------------------------
with app.app_context():
    _cleanup_notifs(*ALL_IDS)

    with patch('email_service.notify_buyer_listing_reserved', return_value=True):
        notify_listing_reserved_to_watchers(
            LISTING_ID, 'T142 Reserved Oak Desk',
            offer_buyer_ids=[str(OFFER_PEND_ID)],
        )

    notifs = _reserved_notifs(OFFER_PEND_ID)
    check('3: pending-offer buyer gets listing_reserved notification',
          len(notifs) == 1, f'count={len(notifs)}')


# ---------------------------------------------------------------------------
# Test 4: buyer with a countered offer (no favorite / conversation) gets notified
# ---------------------------------------------------------------------------
with app.app_context():
    _cleanup_notifs(*ALL_IDS)

    with patch('email_service.notify_buyer_listing_reserved', return_value=True):
        notify_listing_reserved_to_watchers(
            LISTING_ID, 'T142 Reserved Oak Desk',
            offer_buyer_ids=[str(OFFER_CNTR_ID)],
        )

    notifs = _reserved_notifs(OFFER_CNTR_ID)
    check('4: countered-offer buyer gets listing_reserved notification',
          len(notifs) == 1, f'count={len(notifs)}')


# ---------------------------------------------------------------------------
# Test 5: buyer who favorited + has conversation is notified exactly ONCE
# ---------------------------------------------------------------------------
with app.app_context():
    _cleanup_notifs(*ALL_IDS)

    with patch('email_service.notify_buyer_listing_reserved', return_value=True):
        notify_listing_reserved_to_watchers(LISTING_ID, 'T142 Reserved Oak Desk')

    notifs = _reserved_notifs(BOTH_ID)
    check('5: fav+convo buyer is notified exactly once (dedup)',
          len(notifs) == 1, f'count={len(notifs)}')


# ---------------------------------------------------------------------------
# Test 6: offer buyer who also favorited is notified exactly ONCE
# ---------------------------------------------------------------------------
with app.app_context():
    _cleanup_notifs(*ALL_IDS)

    with patch('email_service.notify_buyer_listing_reserved', return_value=True):
        notify_listing_reserved_to_watchers(
            LISTING_ID, 'T142 Reserved Oak Desk',
            offer_buyer_ids=[str(OFFER_FAV_ID)],
        )

    notifs = _reserved_notifs(OFFER_FAV_ID)
    check('6: offer+fav buyer is notified exactly once (dedup)',
          len(notifs) == 1, f'count={len(notifs)}')


# ---------------------------------------------------------------------------
# Test 7: seller is NOT notified
# ---------------------------------------------------------------------------
with app.app_context():
    _cleanup_notifs(*ALL_IDS)

    with patch('email_service.notify_buyer_listing_reserved', return_value=True):
        notify_listing_reserved_to_watchers(LISTING_ID, 'T142 Reserved Oak Desk')

    notifs = _reserved_notifs(SELLER_ID)
    check('7: seller does NOT receive a listing_reserved notification',
          len(notifs) == 0, f'count={len(notifs)}')


# ---------------------------------------------------------------------------
# Test 8: email is attempted for each unique watcher
# ---------------------------------------------------------------------------
with app.app_context():
    _cleanup_notifs(*ALL_IDS)

    email_calls = []

    def _fake_email(email, title, listing_id):
        email_calls.append(email)
        return True

    with patch('email_service.notify_buyer_listing_reserved', side_effect=_fake_email):
        # Pass all offer buyers too; OFFER_FAV_ID is both offer and fav (dedup → 1 email)
        notify_listing_reserved_to_watchers(
            LISTING_ID, 'T142 Reserved Oak Desk',
            offer_buyer_ids=[str(OFFER_PEND_ID), str(OFFER_CNTR_ID), str(OFFER_FAV_ID)],
        )

    expected_emails = {
        f'{FAV_ID}@test.example',
        f'{CONVO_ID}@test.example',
        f'{BOTH_ID}@test.example',
        f'{OFFER_PEND_ID}@test.example',
        f'{OFFER_CNTR_ID}@test.example',
        f'{OFFER_FAV_ID}@test.example',
    }
    check('8: email attempted exactly once per unique watcher (6 buyers)',
          len(email_calls) == 6, f'email_calls={len(email_calls)}')
    check('8: each watcher email address is correct',
          set(email_calls) == expected_emails,
          f'got={set(email_calls)!r}  expected={expected_emails!r}')


# ---------------------------------------------------------------------------
# Test 9: route guard — reserved→reserved does NOT call the notifier
# ---------------------------------------------------------------------------
with app.app_context():
    lst = db.session.get(Listing, LISTING_ID)
    lst.status = 'reserved'
    db.session.commit()

    _cleanup_notifs(*ALL_IDS)
    seller_obj = db.session.get(User, SELLER_ID)

    with (
        patch('routes._check_listing_csrf', return_value=None),
        patch('notification_service.notify_listing_reserved_to_watchers') as mock_notify,
        patch('routes.expire_pending_offers'),   # patch at its import site in routes
    ):
        _flu._get_user = lambda: seller_obj
        client = app.test_client()
        r = client.post(
            f'/listing/{LISTING_ID}/status',
            data={'status': 'reserved'},
            follow_redirects=False,
        )

    check('9: reserved→reserved route redirects (302)',
          r.status_code == 302, f'status={r.status_code}')
    check('9: notify_listing_reserved_to_watchers NOT called when prior_status==reserved',
          mock_notify.call_count == 0, f'call_count={mock_notify.call_count}')


# ---------------------------------------------------------------------------
# Test 10: route — active→reserved fires the notifier with correct args,
#           INCLUDING offer_buyer_ids captured before expiry
# ---------------------------------------------------------------------------
with app.app_context():
    from datetime import datetime, timedelta

    # Reset listing to active
    lst = db.session.get(Listing, LISTING_ID)
    lst.status = 'active'
    lst.expires_at = datetime.now() + timedelta(days=30)
    db.session.commit()

    # Ensure offers are still pending/countered so the route captures them
    o_pend = db.session.get(ListingOffer, OFFER_PEND)
    o_cntr = db.session.get(ListingOffer, OFFER_CNTR)
    o_fav  = db.session.get(ListingOffer, OFFER_FAV_O)
    if o_pend: o_pend.status = 'pending'
    if o_cntr: o_cntr.status = 'countered'
    if o_fav:  o_fav.status  = 'pending'
    db.session.commit()

    _cleanup_notifs(*ALL_IDS)
    seller_obj = db.session.get(User, SELLER_ID)

    captured_args = {}

    def _capture_notify(listing_id, listing_title, offer_buyer_ids=None):
        captured_args['listing_id']      = listing_id
        captured_args['listing_title']   = listing_title
        captured_args['offer_buyer_ids'] = list(offer_buyer_ids or [])

    with (
        patch('routes._check_listing_csrf', return_value=None),
        patch('notification_service.notify_listing_reserved_to_watchers',
              side_effect=_capture_notify) as mock_notify2,
        patch('routes.expire_pending_offers'),   # prevent real DB expiry in route
    ):
        _flu._get_user = lambda: seller_obj
        client = app.test_client()
        r = client.post(
            f'/listing/{LISTING_ID}/status',
            data={'status': 'reserved'},
            follow_redirects=False,
        )

    check('10: active→reserved route redirects (302)',
          r.status_code == 302, f'status={r.status_code}')
    check('10: notifier called exactly once on active→reserved',
          mock_notify2.call_count == 1, f'call_count={mock_notify2.call_count}')
    check('10: called with correct listing_id',
          captured_args.get('listing_id') == LISTING_ID,
          f'got={captured_args.get("listing_id")!r}')
    check('10: called with correct listing title',
          'T142 Reserved Oak Desk' in (captured_args.get('listing_title') or ''),
          f'got={captured_args.get("listing_title")!r}')
    # The route must have forwarded the open-offer buyers captured before expiry
    passed_ids = set(captured_args.get('offer_buyer_ids', []))
    expected_offer_buyers = {str(OFFER_PEND_ID), str(OFFER_CNTR_ID), str(OFFER_FAV_ID)}
    check('10: all open-offer buyer IDs passed to notifier before expiry',
          expected_offer_buyers.issubset(passed_ids),
          f'passed={passed_ids!r}  expected_subset={expected_offer_buyers!r}')


# ---------------------------------------------------------------------------
# Teardown — remove all test fixtures in FK-safe order
# ---------------------------------------------------------------------------
with app.app_context():
    _cleanup_notifs(*ALL_IDS)

    for Model, pk in [
        (ListingOffer,        OFFER_PEND),
        (ListingOffer,        OFFER_CNTR),
        (ListingOffer,        OFFER_FAV_O),
        (ListingConversation, CONVO_PK),
        (ListingConversation, BOTH_PK),
    ]:
        obj = db.session.get(Model, pk)
        if obj:
            db.session.delete(obj)
    db.session.commit()

    for fav in ListingFavorite.query.filter_by(listing_id=LISTING_ID).all():
        db.session.delete(fav)
    db.session.commit()

    lst = db.session.get(Listing, LISTING_ID)
    if lst:
        db.session.delete(lst)
    db.session.commit()

    for uid in ALL_IDS:
        u = db.session.get(User, uid)
        if u:
            db.session.delete(u)
    db.session.commit()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
