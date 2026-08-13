"""
Task 90 + 145 + 148 validation: offer accept / decline / counter work end-to-end,
plus seller email notifications on buyer-respond path, plus the one-accepted-offer guard.

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
- Task 148: accepting one offer auto-declines all other pending/countered offers
- Task 148: seller blocked from accepting a second offer when one is already accepted
- Task 148: buyer blocked from accepting a counteroffer when another offer is already accepted
- Task 148: buyer accept_counter auto-declines sibling pending/countered offers

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

    # Each independent test group gets its own listing so accepted-offer state
    # from one test cannot bleed into the next.
    listing_a = _make_listing(9001, 't90-seller')   # tests 1 & 2 (non-seller 403, accept)
    listing_b = _make_listing(9003, 't90-seller')   # test 3 (no-sms accept)
    listing_c = _make_listing(9004, 't90-seller')   # tests 4-6 (decline / counter)
    listing_d = _make_listing(9005, 't90-seller')   # test 7 (buyer accept_counter)
    listing_e = _make_listing(9006, 't90-seller')   # tests 8-10 (decline_counter / withdraw)
    listing_f = _make_listing(9007, 't90-seller')   # test 11 (listing status unchanged)

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
        # Uses a fresh listing (9003) so the accepted offer on listing 9001 doesn't block it.
        buyer_no_sms = _make_user('t90-buyer-nosms', notify_sms=False, sms_consent=False)
        offer3 = _make_offer(9903, 9003, 't90-buyer-nosms', 't90-seller', status='pending')
        _flu._get_user = lambda: seller

        with patch('email_service.send_email') as mock_email3:
            r = client.post(
                f'/listing/9003/offer/9903/respond',
                data={'action': 'accept'},
                follow_redirects=False,
            )

        check('accept no-sms user: buyer email still sent',
              mock_email3.call_count >= 1, f'call_count={mock_email3.call_count}')

        # ── 4: decline → status 'declined' ────────────────────────────────
        offer4 = _make_offer(9904, 9004, 't90-buyer', 't90-seller', status='pending')
        _flu._get_user = lambda: seller

        with patch('sms_service.send_sms'):
            r = client.post(
                f'/listing/9004/offer/9904/respond',
                data={'action': 'decline'},
                follow_redirects=False,
            )

        check('decline: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer4 = db.session.get(ListingOffer, 9904)
        check('decline: offer.status is declined',
              offer4.status == 'declined', f'status={offer4.status!r}')

        # ── 5: counter → status 'countered', counter_amount saved ─────────
        offer5 = _make_offer(9905, 9004, 't90-buyer', 't90-seller', status='pending', amount=100.0)
        _flu._get_user = lambda: seller

        with patch('sms_service.send_sms'):
            r = client.post(
                f'/listing/9004/offer/9905/respond',
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
        offer6 = _make_offer(9906, 9004, 't90-buyer', 't90-seller', status='pending')
        _flu._get_user = lambda: seller

        with patch('sms_service.send_sms'):
            r = client.post(
                f'/listing/9004/offer/9906/respond',
                data={'action': 'counter', 'counter_amount': 'not-a-number'},
                follow_redirects=False,
            )

        check('counter bad amount: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer6 = db.session.get(ListingOffer, 9906)
        check('counter bad amount: status unchanged (still pending)',
              offer6.status == 'pending', f'status={offer6.status!r}')

        # ── 7: buyer accept_counter → seller gets email ───────────────────
        # Uses a fresh listing (9005) so it starts with no accepted offer.
        offer7 = _make_offer(9907, 9005, 't90-buyer', 't90-seller', status='countered', amount=100.0)
        offer7.counter_amount = 175.0
        db.session.commit()
        _flu._get_user = lambda: buyer

        with patch('email_service.send_email') as mock_accept_email:
            r = client.post(
                f'/listing/9005/offer/9907/buyer-respond',
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
        offer8 = _make_offer(9908, 9006, 't90-buyer', 't90-seller', status='countered', amount=100.0)
        offer8.counter_amount = 175.0
        db.session.commit()
        _flu._get_user = lambda: buyer

        with patch('email_service.send_email') as mock_decline_email:
            r = client.post(
                f'/listing/9006/offer/9908/buyer-respond',
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
        offer8b = _make_offer(9918, 9006, 't90-buyer', 't90-seller', status='pending', amount=100.0)
        db.session.commit()
        _flu._get_user = lambda: buyer

        with patch('email_service.send_email') as mock_no_email:
            r = client.post(
                f'/listing/9006/offer/9918/buyer-respond',
                data={'action': 'decline_counter'},
                follow_redirects=False,
            )

        check('decline_counter on pending: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer8b = db.session.get(ListingOffer, 9918)
        check('decline_counter on pending: status unchanged (still pending)',
              offer8b.status == 'pending', f'status={offer8b.status!r}')
        check('decline_counter on pending: no seller email sent',
              mock_no_email.call_count == 0, f'call_count={mock_no_email.call_count}')

        # ── 9: non-buyer gets 403 on buyer-respond ─────────────────────────
        offer9 = _make_offer(9909, 9006, 't90-buyer', 't90-seller', status='countered', amount=100.0)
        offer9.counter_amount = 175.0
        db.session.commit()
        _flu._get_user = lambda: other

        r = client.post(
            f'/listing/9006/offer/9909/buyer-respond',
            data={'action': 'accept_counter'},
            follow_redirects=False,
        )
        check('non-buyer gets 403 on buyer-respond', r.status_code == 403, f'status={r.status_code}')

        # ── 10: buyer withdraw ─────────────────────────────────────────────
        offer10 = _make_offer(9910, 9006, 't90-buyer', 't90-seller', status='pending', amount=100.0)
        _flu._get_user = lambda: buyer

        with patch('sms_service.send_sms'):
            r = client.post(
                f'/listing/9006/offer/9910/buyer-respond',
                data={'action': 'withdraw'},
                follow_redirects=False,
            )

        check('buyer withdraw: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer10 = db.session.get(ListingOffer, 9910)
        check('buyer withdraw: status is withdrawn',
              offer10.status == 'withdrawn', f'status={offer10.status!r}')

        # ── 11: listing status is NOT altered by accept/decline/counter ────
        # Check listing_f which had decline/counter actions (not accept)
        listing_c_fresh = db.session.get(Listing, 9004)
        check('listing status unchanged after respond actions',
              listing_c_fresh.status == 'active', f'listing.status={listing_c_fresh.status!r}')

        # ── 12: accepting one offer auto-declines other pending/countered ──
        # Uses listing 9002 (fresh, no prior offers)
        listing2 = _make_listing(9002, 't90-seller')
        buyer_b = _make_user('t90-buyer-b')
        offer_a = _make_offer(9920, 9002, 't90-buyer', 't90-seller', status='pending', amount=80.0)
        offer_b = _make_offer(9921, 9002, 't90-buyer-b', 't90-seller', status='pending', amount=90.0)
        offer_c = _make_offer(9922, 9002, 't90-buyer', 't90-seller', status='countered', amount=70.0)
        _flu._get_user = lambda: seller

        with patch('email_service.send_email'):
            r = client.post(
                f'/listing/9002/offer/9920/respond',
                data={'action': 'accept'},
                follow_redirects=False,
            )

        check('auto-decline: accept redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer_a = db.session.get(ListingOffer, 9920)
        offer_b = db.session.get(ListingOffer, 9921)
        offer_c = db.session.get(ListingOffer, 9922)
        check('auto-decline: accepted offer is accepted',
              offer_a.status == 'accepted', f'status={offer_a.status!r}')
        check('auto-decline: other pending offer is declined',
              offer_b.status == 'declined', f'status={offer_b.status!r}')
        check('auto-decline: other countered offer is declined',
              offer_c.status == 'declined', f'status={offer_c.status!r}')

        # ── 13: seller blocked from accepting a second offer (same listing) ─
        # listing 9002 now has offer_a accepted; trying to accept offer_d should fail
        offer_d = _make_offer(9923, 9002, 't90-buyer-b', 't90-seller', status='pending', amount=95.0)
        _flu._get_user = lambda: seller

        with patch('email_service.send_email'):
            r = client.post(
                f'/listing/9002/offer/9923/respond',
                data={'action': 'accept'},
                follow_redirects=False,
            )

        check('double-accept seller: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer_d = db.session.get(ListingOffer, 9923)
        check('double-accept seller: second offer stays pending (not accepted)',
              offer_d.status == 'pending', f'status={offer_d.status!r}')
        offer_a_again = db.session.get(ListingOffer, 9920)
        check('double-accept seller: original accepted offer unchanged',
              offer_a_again.status == 'accepted', f'status={offer_a_again.status!r}')

        # ── 14: buyer accept_counter auto-declines sibling offers ──────────
        listing3 = _make_listing(9008, 't90-seller')
        offer_e  = _make_offer(9930, 9008, 't90-buyer', 't90-seller', status='countered', amount=60.0)
        offer_e.counter_amount = 80.0
        offer_f  = _make_offer(9931, 9008, 't90-buyer-b', 't90-seller', status='pending', amount=55.0)
        db.session.commit()
        _flu._get_user = lambda: buyer

        with patch('email_service.send_email'):
            r = client.post(
                f'/listing/9008/offer/9930/buyer-respond',
                data={'action': 'accept_counter'},
                follow_redirects=False,
            )

        check('buyer accept_counter auto-decline: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer_e = db.session.get(ListingOffer, 9930)
        offer_f = db.session.get(ListingOffer, 9931)
        check('buyer accept_counter auto-decline: counter accepted',
              offer_e.status == 'accepted', f'status={offer_e.status!r}')
        check('buyer accept_counter auto-decline: sibling declined',
              offer_f.status == 'declined', f'status={offer_f.status!r}')

        # ── 15: buyer blocked from accept_counter when another offer accepted ─
        # listing 9008 now has offer_e accepted; trying to accept offer_g should fail
        offer_g = _make_offer(9932, 9008, 't90-buyer-b', 't90-seller', status='countered', amount=50.0)
        offer_g.counter_amount = 75.0
        db.session.commit()
        _flu._get_user = lambda: buyer_b

        with patch('email_service.send_email'):
            r = client.post(
                f'/listing/9008/offer/9932/buyer-respond',
                data={'action': 'accept_counter'},
                follow_redirects=False,
            )

        check('buyer double-accept: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer_g = db.session.get(ListingOffer, 9932)
        check('buyer double-accept: second offer stays countered (not accepted)',
              offer_g.status == 'countered', f'status={offer_g.status!r}')
        offer_e_still = db.session.get(ListingOffer, 9930)
        check('buyer double-accept: original accepted offer unchanged',
              offer_e_still.status == 'accepted', f'status={offer_e_still.status!r}')

        # ── 16: partial unique index rejects a second accepted offer directly ─
        # This verifies the database-level safety net that guards against the
        # concurrent race: even if two requests both pass the in-memory check,
        # the index on listing_offers(listing_id) WHERE status='accepted'
        # ensures only one commit succeeds.
        from sqlalchemy.exc import IntegrityError as _IntegrityError
        listing4 = _make_listing(9009, 't90-seller')
        offer_h = _make_offer(9950, 9009, 't90-buyer', 't90-seller', status='pending', amount=120.0)
        offer_i = _make_offer(9951, 9009, 't90-buyer-b', 't90-seller', status='pending', amount=130.0)

        # Accept the first offer normally (via the route guard)
        _flu._get_user = lambda: seller
        with patch('email_service.send_email'):
            client.post(
                f'/listing/9009/offer/9950/respond',
                data={'action': 'accept'},
                follow_redirects=False,
            )
        offer_h = db.session.get(ListingOffer, 9950)
        check('index guard setup: first offer accepted',
              offer_h.status == 'accepted', f'status={offer_h.status!r}')

        # Now bypass the route guard and directly set a second offer to accepted —
        # simulating the concurrent-race scenario.  The DB constraint must catch it.
        caught_index_error = False
        try:
            offer_i.status = 'accepted'
            db.session.commit()
        except _IntegrityError:
            db.session.rollback()
            caught_index_error = True

        # Reload from DB to confirm invariant holds
        offer_i = db.session.get(ListingOffer, 9951)
        check('index guard: partial unique index rejects second accepted offer',
              caught_index_error,
              f'IntegrityError not raised — offer_i.status={offer_i.status!r}')
        check('index guard: only one accepted offer exists on listing',
              ListingOffer.query.filter_by(listing_id=9009, status='accepted').count() == 1,
              f"count={ListingOffer.query.filter_by(listing_id=9009, status='accepted').count()}")

        # ── 17: seller POSTing to an expired offer → 302, status stays 'expired' ─
        # Task 149: verify the time-based expiry guard in offer_seller_respond.
        from datetime import datetime as _dt
        listing_exp_s = _make_listing(9010, 't90-seller')
        offer_exp_s = _make_offer(9960, 9010, 't90-buyer', 't90-seller', status='pending', amount=120.0)
        # Backdate expires_at to force the expiry guard to trigger
        offer_exp_s.expires_at = _dt(2020, 1, 1)
        db.session.commit()
        _flu._get_user = lambda: seller

        with patch('email_service.send_email'):
            r = client.post(
                f'/listing/9010/offer/9960/respond',
                data={'action': 'accept'},
                follow_redirects=False,
            )

        check('seller expired offer: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer_exp_s = db.session.get(ListingOffer, 9960)
        check('seller expired offer: status is expired (not accepted)',
              offer_exp_s.status == 'expired', f'status={offer_exp_s.status!r}')

        # ── 18: buyer POSTing to an expired offer → 302, status stays 'expired' ─
        # Task 149: verify the time-based expiry guard in offer_buyer_respond.
        listing_exp_b = _make_listing(9011, 't90-seller')
        offer_exp_b = _make_offer(9961, 9011, 't90-buyer', 't90-seller', status='countered', amount=100.0)
        offer_exp_b.counter_amount = 130.0
        # Backdate expires_at to force the expiry guard to trigger
        offer_exp_b.expires_at = _dt(2020, 1, 1)
        db.session.commit()
        _flu._get_user = lambda: buyer

        with patch('email_service.send_email'):
            r = client.post(
                f'/listing/9011/offer/9961/buyer-respond',
                data={'action': 'accept_counter'},
                follow_redirects=False,
            )

        check('buyer expired offer: redirects (302)', r.status_code == 302, f'status={r.status_code}')
        offer_exp_b = db.session.get(ListingOffer, 9961)
        check('buyer expired offer: status is expired (not accepted)',
              offer_exp_b.status == 'expired', f'status={offer_exp_b.status!r}')


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
