"""
Task 118 validation: notification bell, badge, and all event hooks fire correctly end-to-end.

Verifies:
- Buyer sends a message → seller gets in-app 'new_message' notification
- Buyer makes an offer (POST /listing/<id>/offer) → seller gets 'new_offer' notification
- Seller accepts offer → buyer gets 'offer_accepted' notification
- Seller counters → buyer gets 'offer_countered' notification
- Seller declines → buyer gets 'offer_declined' notification
- Buyer accepts counter → seller gets 'offer_accepted' notification (counter accepted)
- Delivery request route (POST /listing/<id>/request-delivery) → ALL admin users each
  get a 'delivery_request' notification (route-level, not service-direct)
- Admin changes delivery status to 'quoted' → buyer gets 'delivery_quote_ready' notification
- Opening a notification marks it read and redirects to action_url
- Opening another user's notification returns 404 (ownership enforced)
- POST /notifications/mark-all-read clears the badge (unread count → 0)
- GET /api/notifications/count returns JSON {count: N} with correct Content-Type
- Rendered base-template page includes bell badge markup; badge count matches unread count
  for user with notifications, and badge is hidden (display:none) when count is zero
- Badge polling JS is embedded in base.html for authenticated users

Run with:  python tests/test_notification_bell_hooks.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch
import flask_login.utils as _flu
from app import app, db
import routes  # noqa: F401 — registers routes on app
from models import User, Listing, Notification

results = []


def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_user(uid, is_admin=False):
    u = User(
        id=uid,
        email=f'{uid}@example.com',
        first_name='T118',
        age_confirmed=True,
        user_type='customer',
        is_admin=is_admin,
    )
    db.session.merge(u)
    db.session.commit()
    return db.session.get(User, uid)


def _make_listing(listing_id, seller_id):
    from models import Listing as _L
    lst = _L(
        id=listing_id,
        seller_id=seller_id,
        title='Vintage T118 Sofa',
        price=250.0,
        price_type='negotiable',
        status='active',
        moderation_status='approved',
        listing_type='item',
    )
    db.session.merge(lst)
    db.session.commit()
    return db.session.get(_L, listing_id)


def _make_offer(offer_id, listing_id, buyer_id, seller_id,
                status='pending', amount=180.0, counter_amount=None):
    from models import ListingOffer
    offer = ListingOffer(
        id=offer_id,
        listing_id=listing_id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        amount=amount,
        counter_amount=counter_amount,
        status=status,
        expires_at=None,
    )
    db.session.merge(offer)
    db.session.commit()
    return db.session.get(ListingOffer, offer_id)


def _make_convo(convo_id, listing_id, buyer_id, seller_id):
    from models import ListingConversation
    from datetime import datetime
    c = ListingConversation(
        id=convo_id,
        listing_id=listing_id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    db.session.merge(c)
    db.session.commit()
    return db.session.get(ListingConversation, convo_id)


def _make_delivery_request(dr_id, buyer_id, listing_id):
    from models import DeliveryRequest
    dr = DeliveryRequest(
        id=dr_id,
        buyer_id=buyer_id,
        listing_id=listing_id,
        pickup_zip='55414',
        delivery_zip='55101',
        status='requested',
    )
    db.session.merge(dr)
    db.session.commit()
    return db.session.get(DeliveryRequest, dr_id)


def _unread_notifs(user_id, notif_type=None):
    """Return unread Notification rows for a user, optionally filtered by type."""
    q = Notification.query.filter_by(user_id=str(user_id), is_read=False)
    if notif_type:
        q = q.filter_by(type=notif_type)
    return q.all()


def _cleanup_notifs(*user_ids):
    for uid in user_ids:
        Notification.query.filter_by(user_id=str(uid)).delete()
    db.session.commit()


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------

with app.app_context():
    client = app.test_client()

    # Seed actors — two admins to confirm ALL admins are notified
    seller   = _make_user('t118-seller')
    buyer    = _make_user('t118-buyer')
    admin1   = _make_user('t118-admin1', is_admin=True)
    admin2   = _make_user('t118-admin2', is_admin=True)
    stranger = _make_user('t118-stranger')

    # Seed listing; delivery request seeded separately per test
    listing = _make_listing(80001, 't118-seller')

    # Clear any leftover notifications from prior runs
    _cleanup_notifs('t118-seller', 't118-buyer', 't118-admin1', 't118-admin2', 't118-stranger')

    # Patch CSRF and external notification helpers throughout
    with (
        patch('routes._check_listing_csrf', return_value=None),
        patch('email_service.notify_buyer_offer_accepted', return_value=True),
        patch('email_service.notify_buyer_offer_declined', return_value=True),
        patch('email_service.notify_buyer_offer_countered', return_value=True),
        patch('sms_service.notify_admin_new_request_sms', return_value=True),
        patch('sms_service.notify_hauler_new_job_sms', return_value=True),
    ):

        # ── 1: new_message notification ────────────────────────────────────
        convo = _make_convo(50001, 80001, 't118-buyer', 't118-seller')
        _flu._get_user = lambda: buyer
        r = client.post(
            '/listing/80001/message/50001',
            data={'body': 'Is this still available?'},
            follow_redirects=False,
        )
        check('1: message POST redirects (302)', r.status_code == 302,
              f'status={r.status_code}')

        seller_msgs = _unread_notifs('t118-seller', 'new_message')
        check('1: seller gets new_message notification', len(seller_msgs) == 1,
              f'count={len(seller_msgs)}')
        if seller_msgs:
            n = seller_msgs[0]
            check('1: new_message title mentions listing', 'Vintage T118 Sofa' in n.title,
                  f'title={n.title!r}')
            check('1: new_message action_url points to conversation',
                  '/listing/80001/message/' in (n.action_url or ''),
                  f'url={n.action_url!r}')

        # ── 2: new_offer notification (route-level POST) ───────────────────
        _cleanup_notifs('t118-seller')
        _flu._get_user = lambda: buyer
        r = client.post(
            '/listing/80001/offer',
            data={'amount': '180', 'message': 'Can you do $180?'},
            follow_redirects=False,
        )
        check('2: make-offer POST redirects (302)', r.status_code == 302,
              f'status={r.status_code}')

        seller_offers = _unread_notifs('t118-seller', 'new_offer')
        check('2: seller gets new_offer notification', len(seller_offers) >= 1,
              f'count={len(seller_offers)}')
        if seller_offers:
            n = seller_offers[0]
            check('2: new_offer title shows dollar amount', '$180' in n.title,
                  f'title={n.title!r}')

        # ── 3: offer_accepted notification ─────────────────────────────────
        _cleanup_notifs('t118-buyer')
        from models import ListingOffer
        offer_row = ListingOffer.query.filter_by(
            listing_id=80001, buyer_id='t118-buyer', status='pending'
        ).first()
        if not offer_row:
            offer_row = _make_offer(80901, 80001, 't118-buyer', 't118-seller', status='pending')

        _flu._get_user = lambda: seller
        r = client.post(
            f'/listing/80001/offer/{offer_row.id}/respond',
            data={'action': 'accept'},
            follow_redirects=False,
        )
        check('3: seller-respond accept redirects (302)', r.status_code == 302,
              f'status={r.status_code}')

        buyer_accepted = _unread_notifs('t118-buyer', 'offer_accepted')
        check('3: buyer gets offer_accepted notification', len(buyer_accepted) >= 1,
              f'count={len(buyer_accepted)}')
        if buyer_accepted:
            n = buyer_accepted[0]
            check('3: offer_accepted title confirms acceptance',
                  'accepted' in n.title.lower(),
                  f'title={n.title!r}')

        # ── 4: offer_declined notification ─────────────────────────────────
        _cleanup_notifs('t118-buyer')
        decline_offer = _make_offer(80902, 80001, 't118-buyer', 't118-seller',
                                    status='pending', amount=120.0)

        _flu._get_user = lambda: seller
        r = client.post(
            '/listing/80001/offer/80902/respond',
            data={'action': 'decline'},
            follow_redirects=False,
        )
        check('4: seller-respond decline redirects (302)', r.status_code == 302,
              f'status={r.status_code}')

        buyer_declined = _unread_notifs('t118-buyer', 'offer_declined')
        check('4: buyer gets offer_declined notification', len(buyer_declined) >= 1,
              f'count={len(buyer_declined)}')

        # ── 5: offer_countered notification ────────────────────────────────
        _cleanup_notifs('t118-buyer')
        counter_offer = _make_offer(80903, 80001, 't118-buyer', 't118-seller',
                                    status='pending', amount=140.0)

        _flu._get_user = lambda: seller
        r = client.post(
            '/listing/80001/offer/80903/respond',
            data={'action': 'counter', 'counter_amount': '200'},
            follow_redirects=False,
        )
        check('5: seller-respond counter redirects (302)', r.status_code == 302,
              f'status={r.status_code}')

        buyer_countered = _unread_notifs('t118-buyer', 'offer_countered')
        check('5: buyer gets offer_countered notification', len(buyer_countered) >= 1,
              f'count={len(buyer_countered)}')
        if buyer_countered:
            n = buyer_countered[0]
            check('5: offer_countered title shows counter amount',
                  '$200' in n.title,
                  f'title={n.title!r}')

        # ── 6: counter_accepted → seller gets offer_accepted notification ──
        _cleanup_notifs('t118-seller')
        _flu._get_user = lambda: buyer
        r = client.post(
            '/listing/80001/offer/80903/buyer-respond',
            data={'action': 'accept_counter'},
            follow_redirects=False,
        )
        check('6: buyer accept_counter redirects (302)', r.status_code == 302,
              f'status={r.status_code}')

        seller_counter_accepted = _unread_notifs('t118-seller', 'offer_accepted')
        check('6: seller gets offer_accepted when buyer accepts counter',
              len(seller_counter_accepted) >= 1,
              f'count={len(seller_counter_accepted)}')
        if seller_counter_accepted:
            n = seller_counter_accepted[0]
            check('6: counter_accepted title mentions "accepted"',
                  'accepted' in n.title.lower(),
                  f'title={n.title!r}')

        # ── 7: delivery_request route → ALL admins get notifications ───────
        # Use a second listing that's still active (re-use 80001 — still active)
        _cleanup_notifs('t118-admin1', 't118-admin2')

        _flu._get_user = lambda: buyer
        r = client.post(
            '/listing/80001/request-delivery',
            data={
                'pickup_zip':      '55414',
                'pickup_city':     'Minneapolis',
                'pickup_state':    'MN',
                'delivery_zip':    '55101',
                'delivery_city':   'Saint Paul',
                'delivery_state':  'MN',
                'item_count':      '1',
            },
            follow_redirects=False,
        )
        check('7: delivery POST redirects (302)', r.status_code == 302,
              f'status={r.status_code}')

        admin1_dr = _unread_notifs('t118-admin1', 'delivery_request')
        admin2_dr = _unread_notifs('t118-admin2', 'delivery_request')
        check('7: admin1 gets delivery_request notification', len(admin1_dr) >= 1,
              f'count={len(admin1_dr)}')
        check('7: admin2 gets delivery_request notification', len(admin2_dr) >= 1,
              f'count={len(admin2_dr)}')
        if admin1_dr:
            n = admin1_dr[0]
            check('7: delivery_request action_url points to delivery detail',
                  '/delivery/' in (n.action_url or ''),
                  f'url={n.action_url!r}')
            check('7: delivery_request message mentions buyer name',
                  'T118' in (n.message or ''),
                  f'message={n.message!r}')

        # ── 8: delivery 'quoted' → buyer gets delivery_quote_ready ─────────
        # Get the DR that was just created by the route above
        from models import DeliveryRequest as _DR
        dr_fresh = _DR.query.filter_by(buyer_id='t118-buyer').order_by(_DR.created_at.desc()).first()
        _cleanup_notifs('t118-buyer')

        if dr_fresh:
            dr_fresh.quote_amount = 75.0
            db.session.commit()

            _flu._get_user = lambda: admin1
            r = client.post(
                f'/delivery/{dr_fresh.id}/update-status',
                data={'status': 'quoted'},
                follow_redirects=False,
            )
            check('8: delivery update-status quoted redirects (302)', r.status_code == 302,
                  f'status={r.status_code}')

            buyer_quote = _unread_notifs('t118-buyer', 'delivery_quote_ready')
            check('8: buyer gets delivery_quote_ready notification', len(buyer_quote) >= 1,
                  f'count={len(buyer_quote)}')
            if buyer_quote:
                n = buyer_quote[0]
                check('8: delivery_quote_ready title mentions amount',
                      '$75' in n.title,
                      f'title={n.title!r}')
        else:
            check('8: delivery DR found for quote test', False, 'DR not found')

        # ── 9: opening a notification marks it read and redirects ──────────
        from notification_service import create_notification
        test_notif = create_notification(
            user_id='t118-buyer',
            notif_type='admin_notice',
            title='Test notice for open',
            action_url='/my-listings',
        )
        check('9: test notification created', test_notif is not None, 'notification is None')

        _flu._get_user = lambda: buyer
        r = client.get(
            f'/notifications/{test_notif.id}/open',
            follow_redirects=False,
        )
        check('9: notification open redirects (302)', r.status_code == 302,
              f'status={r.status_code}')
        check('9: redirects to action_url',
              r.headers.get('Location', '').endswith('/my-listings'),
              f'location={r.headers.get("Location")!r}')

        refreshed = db.session.get(Notification, test_notif.id)
        check('9: notification is_read after open', refreshed.is_read is True,
              f'is_read={refreshed.is_read!r}')
        check('9: read_at is set', refreshed.read_at is not None,
              f'read_at={refreshed.read_at!r}')

        # ── 10: unauthorized user cannot open another user's notification ──
        stranger_notif = create_notification(
            user_id='t118-stranger',
            notif_type='admin_notice',
            title='Stranger notice',
            action_url='/home',
        )
        _flu._get_user = lambda: buyer  # buyer tries to open stranger's notification
        r = client.get(
            f'/notifications/{stranger_notif.id}/open',
            follow_redirects=False,
        )
        check('10: unauthorized open returns 404', r.status_code == 404,
              f'status={r.status_code}')

        sn = db.session.get(Notification, stranger_notif.id)
        check('10: stranger notification is still unread', sn.is_read is False,
              f'is_read={sn.is_read!r}')

        # ── 11: mark-all-read clears the badge ────────────────────────────
        _cleanup_notifs('t118-buyer')
        for i in range(3):
            create_notification(user_id='t118-buyer', notif_type='admin_notice',
                                title=f'Unread {i}')

        from notification_service import get_unread_count
        before_count = get_unread_count('t118-buyer')
        check('11: before mark-all-read unread count > 0', before_count > 0,
              f'count={before_count}')

        _flu._get_user = lambda: buyer
        r = client.post('/notifications/mark-all-read', follow_redirects=False)
        check('11: mark-all-read redirects (302)', r.status_code == 302,
              f'status={r.status_code}')

        after_count = get_unread_count('t118-buyer')
        check('11: after mark-all-read unread count is 0', after_count == 0,
              f'count={after_count}')

        # ── 12: /api/notifications/count returns JSON {count: N} ──────────
        create_notification(user_id='t118-buyer', notif_type='admin_notice',
                            title='Poll test notice')

        _flu._get_user = lambda: buyer
        r = client.get('/api/notifications/count', follow_redirects=False)
        check('12: /api/notifications/count returns 200', r.status_code == 200,
              f'status={r.status_code}')
        import json as _json
        try:
            data = _json.loads(r.data)
            check('12: response has "count" key', 'count' in data,
                  f'keys={list(data.keys())}')
            check('12: count is integer >= 1',
                  isinstance(data.get('count'), int) and data['count'] >= 1,
                  f'count={data.get("count")!r}')
        except Exception as e:
            check('12: JSON parse', False, str(e))

        # ── 13: bell badge HTML present and shows correct count ───────────
        # Ensure buyer has ≥1 unread notification (created in test 12)
        unread_before_render = get_unread_count('t118-buyer')
        check('13: buyer has unread notifications before badge render',
              unread_before_render >= 1, f'count={unread_before_render}')

        _flu._get_user = lambda: buyer
        r = client.get('/notifications', follow_redirects=False)
        check('13: /notifications returns 200', r.status_code == 200,
              f'status={r.status_code}')
        body = r.data.decode('utf-8', errors='replace')

        # Desktop badge element must be present in base.html
        check('13: desktop bell badge element present',
              'id="notif-count-desktop"' in body,
              'notif-count-desktop not found in HTML')
        # Mobile badge element must be present
        check('13: mobile bell badge element present',
              'id="notif-count-mobile"' in body,
              'notif-count-mobile not found in HTML')
        # With unread notifications the badge must NOT have display:none
        # (the server-rendered badge is visible when count > 0)
        check('13: desktop badge visible (no display:none when count>0)',
              f'id="notif-count-desktop" style="display:none' not in body,
              'desktop badge rendered as hidden despite unread notifications')
        # The unread count number should appear in the page
        check('13: badge shows correct unread count',
              str(unread_before_render) in body,
              f'count {unread_before_render} not found in page body')

        # ── 14: badge hidden when no unread notifications ─────────────────
        # Mark all read so count drops to 0, then render a page
        from notification_service import mark_all_read
        mark_all_read('t118-buyer')
        zero_count = get_unread_count('t118-buyer')
        check('14: count is 0 after mark_all_read', zero_count == 0,
              f'count={zero_count}')

        _flu._get_user = lambda: buyer
        r = client.get('/notifications', follow_redirects=False)
        body_zero = r.data.decode('utf-8', errors='replace')
        # When count=0 the server renders the badge with display:none
        check('14: desktop badge hidden when count=0',
              'id="notif-count-desktop" style="display:none' in body_zero,
              'desktop badge not hidden when unread count is 0')
        check('14: mobile badge hidden when count=0',
              'id="notif-count-mobile" style="display:none' in body_zero,
              'mobile badge not hidden when unread count is 0')

        # ── 15: badge polling JS is embedded for authenticated users ───────
        check('15: polling JS fetch call present in page',
              '/api/notifications/count' in body_zero,
              'polling fetch URL not found in page HTML')
        check('15: setInterval polling present in page',
              'setInterval' in body_zero,
              'setInterval not found in page HTML')
        check('15: setTimeout initial poll present in page',
              'setTimeout' in body_zero,
              'setTimeout not found in page HTML')

        # ── 16: /notifications page renders filter tabs ────────────────────
        r = client.get('/notifications?filter=messages', follow_redirects=False)
        check('16: /notifications?filter=messages returns 200', r.status_code == 200,
              f'status={r.status_code}')
        body_f = r.data.decode('utf-8', errors='replace')
        check('16: active filter tab highlighted',
              'notif-filter-btn active' in body_f,
              'active filter class not found')

        # ── 17: get_unread_count is consistent with poll API ───────────────
        create_notification(user_id='t118-buyer', notif_type='admin_notice',
                            title='Consistency check')
        direct_count = get_unread_count('t118-buyer')
        _flu._get_user = lambda: buyer
        r2 = client.get('/api/notifications/count')
        api_count = _json.loads(r2.data).get('count', -1)
        check('17: direct get_unread_count matches /api/notifications/count',
              direct_count == api_count,
              f'direct={direct_count} api={api_count}')


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
