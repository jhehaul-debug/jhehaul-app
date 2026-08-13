"""
Task 90 + 145 validation: offer accept / decline / counter work end-to-end,
plus seller email notifications on buyer-respond path.

Verifies:
- Only the listing seller can call offer_seller_respond (403 for others)
- accept  → offer.status == 'accepted' (SMS disabled; in-app + email used)
- decline → offer.status == 'declined'
- counter → offer.status == 'countered', counter_amount saved
- offer_buyer_respond: buyer accept_counter → status 'accepted', amount updated,
                       seller email sent
- offer_buyer_respond: buyer decline_counter → status 'declined', seller email sent
- offer_buyer_respond: decline_counter on pending offer → rejected (no mutation, no email)
- offer_buyer_respond: non-buyer cannot respond (403)

Run with:  python tests/test_offer_respond.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock
import flask_login.utils as _flu
from app import app, db
import routes  # noqa: F401 — registers routes on app
from models import User, Listing, ListingOffer

results = []


def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_user(uid, notify_sms=False, sms_consent=False, phone=None):
    u = User(
        id=uid,
        email=f'{uid}@example.com',
        first_name='Test',
        age_confirmed=True,
        user_type='customer',
        notify_sms=notify_sms,
        sms_consent=sms_consent,
        phone=phone,
    )
    db.session.merge(u)
    db.session.commit()
    return db.session.get(User, uid)


def _make_listing(listing_id, seller_id):
    lst = Listing(
        id=listing_id,
        seller_id=seller_id,
        title='Test Vintage Couch',
        price=200.0,
        price_type='negotiable',
        status='active',
        moderation_status='approved',
        listing_type='item',
    )
    db.session.merge(lst)
    db.session.commit()
    return db.session.get(Listing, listing_id)


def _make_offer(offer_id, listing_id, buyer_id, seller_id, status='pending', amount=150.0):
    offer = ListingOffer(
        id=offer_id,
        listing_id=listing_id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        amount=amount,
        status=status,
        expires_at=None,  # no time-based expiry for these tests
    )
    db.session.merge(offer)
    db.session.commit()
    return db.session.get(ListingOffer, offer_id)


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

with app.app_context():
    client = app.test_client()

    # Seed users
    seller = _make_user('t90-seller')
    buyer  = _make_user('t90-buyer', notify_sms=True, sms_consent=True, phone='+16125550100')
    other  = _make_user('t90-other')  # neither seller nor buyer

    # Seed listing
    listing = _make_listing(9001, 't90-seller')

    # We patch _check_listing_csrf globally for all POST calls in these tests
    with patch('routes._check_listing_csrf', return_value=None):

        # ── 1: Non-seller gets 403 ─────────────────────────────────────────
        offer1 = _make_offer(9901, 9001, 't90-buyer', 't90-seller', status='pending')
        _flu._get_user = lambda: other
        r = client.post(
            f'/listing/9001/offer/9901/respond',
            data={'action': 'accept'},
            follow_redirects=False,
        )
        check('non-seller gets 403', r.status_code == 403, f'status={r.status_code}')

        # ── 2: accept → status 'accepted' (SMS disabled; email+in-app used) ─
        offer2 = _make_offer(9902, 9001, 't90-buyer', 't90-seller', status='pending')
        _flu._get_user = lambda: seller

        with patch('email_service.send_email') as mock_email2:
            r = client.post(
                f'/listing/9001/offer/9902/respond',
                data={'action': 'accept'},
                follow_redirects=False,
            )

        check('accept: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer2 = db.session.get(ListingOffer, 9902)
        check('accept: offer.status is accepted',
              offer2.status == 'accepted', f'status={offer2.status!r}')
        # SMS is disabled for marketplace; verify the buyer gets an email instead
        check('accept: buyer email sent (SMS disabled)',
              mock_email2.call_count >= 1, f'call_count={mock_email2.call_count}')

        # ── 3: accept → buyer email sent even when buyer has no SMS opt-in ──
        buyer_no_sms = _make_user('t90-buyer-nosms', notify_sms=False, sms_consent=False)
        offer3 = _make_offer(9903, 9001, 't90-buyer-nosms', 't90-seller', status='pending')
        _flu._get_user = lambda: seller

        with patch('email_service.send_email') as mock_email3:
            r = client.post(
                f'/listing/9001/offer/9903/respond',
                data={'action': 'accept'},
                follow_redirects=False,
            )

        check('accept no-sms user: buyer email still sent',
              mock_email3.call_count >= 1, f'call_count={mock_email3.call_count}')

        # ── 4: decline → status 'declined' ────────────────────────────────
        offer4 = _make_offer(9904, 9001, 't90-buyer', 't90-seller', status='pending')
        _flu._get_user = lambda: seller

        with patch('sms_service.send_sms'):
            r = client.post(
                f'/listing/9001/offer/9904/respond',
                data={'action': 'decline'},
                follow_redirects=False,
            )

        check('decline: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer4 = db.session.get(ListingOffer, 9904)
        check('decline: offer.status is declined',
              offer4.status == 'declined', f'status={offer4.status!r}')

        # ── 5: counter → status 'countered', counter_amount saved ─────────
        offer5 = _make_offer(9905, 9001, 't90-buyer', 't90-seller', status='pending', amount=100.0)
        _flu._get_user = lambda: seller

        with patch('sms_service.send_sms'):
            r = client.post(
                f'/listing/9001/offer/9905/respond',
                data={'action': 'counter', 'counter_amount': '175'},
                follow_redirects=False,
            )

        check('counter: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer5 = db.session.get(ListingOffer, 9905)
        check('counter: offer.status is countered',
              offer5.status == 'countered', f'status={offer5.status!r}')
        check('counter: counter_amount saved as 175.0',
              offer5.counter_amount == 175.0, f'counter_amount={offer5.counter_amount!r}')

        # ── 6: counter with bad amount → redirect, status unchanged ───────
        offer6 = _make_offer(9906, 9001, 't90-buyer', 't90-seller', status='pending')
        _flu._get_user = lambda: seller

        with patch('sms_service.send_sms'):
            r = client.post(
                f'/listing/9001/offer/9906/respond',
                data={'action': 'counter', 'counter_amount': 'not-a-number'},
                follow_redirects=False,
            )

        check('counter bad amount: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer6 = db.session.get(ListingOffer, 9906)
        check('counter bad amount: status unchanged (still pending)',
              offer6.status == 'pending', f'status={offer6.status!r}')

        # ── 7: buyer accept_counter → seller gets email ───────────────────
        offer7 = _make_offer(9907, 9001, 't90-buyer', 't90-seller', status='countered', amount=100.0)
        offer7.counter_amount = 175.0
        db.session.commit()
        _flu._get_user = lambda: buyer

        with patch('email_service.send_email') as mock_accept_email:
            r = client.post(
                f'/listing/9001/offer/9907/buyer-respond',
                data={'action': 'accept_counter'},
                follow_redirects=False,
            )

        check('buyer accept_counter: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer7 = db.session.get(ListingOffer, 9907)
        check('buyer accept_counter: status is accepted',
              offer7.status == 'accepted', f'status={offer7.status!r}')
        check('buyer accept_counter: amount updated to counter_amount',
              offer7.amount == 175.0, f'amount={offer7.amount!r}')
        check('buyer accept_counter: seller email sent',
              mock_accept_email.call_count >= 1, f'call_count={mock_accept_email.call_count}')
        if mock_accept_email.call_count >= 1:
            # Find the call that went to the seller (seller_offer_accepted event)
            seller_calls = [
                c for c in mock_accept_email.call_args_list
                if len(c.args) >= 4 and c.args[3] == 'seller_offer_accepted'
            ]
            check('buyer accept_counter: email goes to seller address',
                  any(c.args[0] == 't90-seller@example.com' for c in seller_calls),
                  f'calls={[(c.args[0], c.args[3]) for c in mock_accept_email.call_args_list]}')
            check('buyer accept_counter: event type is seller_offer_accepted',
                  bool(seller_calls),
                  f'event_types={[c.args[3] for c in mock_accept_email.call_args_list if len(c.args) >= 4]}')

        # ── 8: buyer decline_counter → seller gets email ──────────────────
        offer8 = _make_offer(9908, 9001, 't90-buyer', 't90-seller', status='countered', amount=100.0)
        offer8.counter_amount = 175.0
        db.session.commit()
        _flu._get_user = lambda: buyer

        with patch('email_service.send_email') as mock_decline_email:
            r = client.post(
                f'/listing/9001/offer/9908/buyer-respond',
                data={'action': 'decline_counter'},
                follow_redirects=False,
            )

        check('buyer decline_counter: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer8 = db.session.get(ListingOffer, 9908)
        check('buyer decline_counter: status is declined',
              offer8.status == 'declined', f'status={offer8.status!r}')
        check('buyer decline_counter: seller email sent',
              mock_decline_email.call_count >= 1, f'call_count={mock_decline_email.call_count}')
        if mock_decline_email.call_count >= 1:
            seller_calls_d = [
                c for c in mock_decline_email.call_args_list
                if len(c.args) >= 4 and c.args[3] == 'seller_offer_declined'
            ]
            check('buyer decline_counter: email goes to seller address',
                  any(c.args[0] == 't90-seller@example.com' for c in seller_calls_d),
                  f'calls={[(c.args[0], c.args[3]) for c in mock_decline_email.call_args_list]}')
            check('buyer decline_counter: event type is seller_offer_declined',
                  bool(seller_calls_d),
                  f'event_types={[c.args[3] for c in mock_decline_email.call_args_list if len(c.args) >= 4]}')

        # ── 8b: decline_counter on pending offer → rejected, no mutation ──
        offer8b = _make_offer(9908, 9001, 't90-buyer', 't90-seller', status='pending', amount=100.0)
        db.session.commit()
        _flu._get_user = lambda: buyer

        with patch('email_service.send_email') as mock_no_email:
            r = client.post(
                f'/listing/9001/offer/9908/buyer-respond',
                data={'action': 'decline_counter'},
                follow_redirects=False,
            )

        check('decline_counter on pending: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer8b = db.session.get(ListingOffer, 9908)
        check('decline_counter on pending: status unchanged (still pending)',
              offer8b.status == 'pending', f'status={offer8b.status!r}')
        check('decline_counter on pending: no seller email sent',
              mock_no_email.call_count == 0, f'call_count={mock_no_email.call_count}')

        # ── 9: non-buyer gets 403 on buyer-respond ─────────────────────────
        offer9 = _make_offer(9909, 9001, 't90-buyer', 't90-seller', status='countered', amount=100.0)
        offer9.counter_amount = 175.0
        db.session.commit()
        _flu._get_user = lambda: other

        r = client.post(
            f'/listing/9001/offer/9909/buyer-respond',
            data={'action': 'accept_counter'},
            follow_redirects=False,
        )
        check('non-buyer gets 403 on buyer-respond', r.status_code == 403, f'status={r.status_code}')

        # ── 10: buyer withdraw ─────────────────────────────────────────────
        offer10 = _make_offer(9910, 9001, 't90-buyer', 't90-seller', status='pending', amount=100.0)
        _flu._get_user = lambda: buyer

        with patch('sms_service.send_sms'):
            r = client.post(
                f'/listing/9001/offer/9910/buyer-respond',
                data={'action': 'withdraw'},
                follow_redirects=False,
            )

        check('buyer withdraw: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer10 = db.session.get(ListingOffer, 9910)
        check('buyer withdraw: status is withdrawn',
              offer10.status == 'withdrawn', f'status={offer10.status!r}')

        # ── 11: listing status is NOT altered by accept/decline/counter ────
        # The accept/decline/counter routes should not touch listing.status
        listing_fresh = db.session.get(Listing, 9001)
        check('listing status unchanged after respond actions',
              listing_fresh.status == 'active', f'listing.status={listing_fresh.status!r}')


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
