"""
Task 144: Confirm accept, decline, and counter emails reach buyers end-to-end.

Exercises offer_seller_respond (routes.py) for each of the three actions and
verifies that the correct email helper is called with the right arguments.
Also confirms the send is silently suppressed when the buyer has no email.

Run with:  python tests/test_seller_respond_emails.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock
import flask_login.utils as _flu
from app import app, db
import routes  # noqa: F401 — registers all routes on app
from models import User, Listing, ListingOffer

results = []


def check(name, cond, extra=""):
    results.append((name, cond))
    print(("PASS" if cond else "FAIL"), "-", name, (extra if not cond else ""))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_UNSET = object()  # sentinel: tells fixture "use generated email" vs "store NULL"


def _make_user(uid, email=_UNSET, **kwargs):
    actual_email = f"{uid}@example.com" if email is _UNSET else email
    u = User(
        id=uid,
        email=actual_email,
        first_name="Test",
        age_confirmed=True,
        user_type="customer",
        **kwargs,
    )
    db.session.merge(u)
    db.session.commit()
    return db.session.get(User, uid)


def _make_listing(listing_id, seller_id):
    lst = Listing(
        id=listing_id,
        seller_id=seller_id,
        title="Mid-Century Dresser",
        price=200.0,
        price_type="negotiable",
        status="active",
        moderation_status="approved",
        listing_type="item",
    )
    db.session.merge(lst)
    db.session.commit()
    return db.session.get(Listing, listing_id)


def _make_offer(offer_id, listing_id, buyer_id, seller_id,
                status="pending", amount=150.0):
    offer = ListingOffer(
        id=offer_id,
        listing_id=listing_id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        amount=amount,
        status=status,
        expires_at=None,   # no time-based expiry for these tests
    )
    db.session.merge(offer)
    db.session.commit()
    return db.session.get(ListingOffer, offer_id)


# ---------------------------------------------------------------------------
# Run tests inside app context
# ---------------------------------------------------------------------------

with app.app_context():
    client = app.test_client()

    seller = _make_user("t144-seller")
    buyer  = _make_user("t144-buyer")   # has email by default
    nobuy  = _make_user("t144-nobuy", email=None)  # buyer with no email
    listing = _make_listing(14401, "t144-seller")

    # Suppress CSRF and in-app/SMS side-effects for all tests
    with (
        patch("routes._check_listing_csrf", return_value=None),
        patch("routes.notify_offer_accepted",  MagicMock(), create=True),
        patch("routes.notify_offer_declined",  MagicMock(), create=True),
        patch("routes.notify_offer_countered", MagicMock(), create=True),
    ):

        # ── 1: accept → notify_buyer_offer_accepted called with correct args ──
        offer1 = _make_offer(14901, 14401, "t144-buyer", "t144-seller",
                             status="pending", amount=120.0)
        _flu._get_user = lambda: seller

        with patch("email_service.notify_buyer_offer_accepted") as mock_accept:
            r = client.post(
                "/listing/14401/offer/14901/respond",
                data={"action": "accept"},
                follow_redirects=False,
            )

        check("accept: route redirects (302)", r.status_code == 302,
              f"status={r.status_code}")
        offer1 = db.session.get(ListingOffer, 14901)
        check("accept: offer status is 'accepted'",
              offer1.status == "accepted", f"status={offer1.status!r}")
        check("accept: notify_buyer_offer_accepted called once",
              mock_accept.call_count == 1,
              f"call_count={mock_accept.call_count}")
        if mock_accept.call_count:
            call_args = mock_accept.call_args[0]
            check("accept: email sent to buyer address",
                  call_args[0] == buyer.email,
                  f"got={call_args[0]!r}")
            check("accept: listing title passed to email helper",
                  call_args[1] == listing.title,
                  f"got={call_args[1]!r}")
            check("accept: listing id passed to email helper",
                  call_args[2] == 14401,
                  f"got={call_args[2]!r}")
            check("accept: offer amount passed to email helper",
                  call_args[3] == 120.0,
                  f"got={call_args[3]!r}")

        # ── 2: decline → notify_buyer_offer_declined called with correct args ─
        offer2 = _make_offer(14902, 14401, "t144-buyer", "t144-seller",
                             status="pending", amount=85.0)
        _flu._get_user = lambda: seller

        with patch("email_service.notify_buyer_offer_declined") as mock_decline:
            r = client.post(
                "/listing/14401/offer/14902/respond",
                data={"action": "decline"},
                follow_redirects=False,
            )

        check("decline: route redirects (302)", r.status_code == 302,
              f"status={r.status_code}")
        offer2 = db.session.get(ListingOffer, 14902)
        check("decline: offer status is 'declined'",
              offer2.status == "declined", f"status={offer2.status!r}")
        check("decline: notify_buyer_offer_declined called once",
              mock_decline.call_count == 1,
              f"call_count={mock_decline.call_count}")
        if mock_decline.call_count:
            call_args = mock_decline.call_args[0]
            check("decline: email sent to buyer address",
                  call_args[0] == buyer.email,
                  f"got={call_args[0]!r}")
            check("decline: listing title passed to email helper",
                  call_args[1] == listing.title,
                  f"got={call_args[1]!r}")
            check("decline: listing id passed to email helper",
                  call_args[2] == 14401,
                  f"got={call_args[2]!r}")
            check("decline: offer amount passed to email helper",
                  call_args[3] == 85.0,
                  f"got={call_args[3]!r}")

        # ── 3: counter → notify_buyer_offer_countered called with correct args ─
        offer3 = _make_offer(14903, 14401, "t144-buyer", "t144-seller",
                             status="pending", amount=100.0)
        _flu._get_user = lambda: seller

        with patch("email_service.notify_buyer_offer_countered") as mock_counter:
            r = client.post(
                "/listing/14401/offer/14903/respond",
                data={"action": "counter", "counter_amount": "130"},
                follow_redirects=False,
            )

        check("counter: route redirects (302)", r.status_code == 302,
              f"status={r.status_code}")
        offer3 = db.session.get(ListingOffer, 14903)
        check("counter: offer status is 'countered'",
              offer3.status == "countered", f"status={offer3.status!r}")
        check("counter: counter_amount saved correctly",
              offer3.counter_amount == 130.0,
              f"counter_amount={offer3.counter_amount!r}")
        check("counter: notify_buyer_offer_countered called once",
              mock_counter.call_count == 1,
              f"call_count={mock_counter.call_count}")
        if mock_counter.call_count:
            call_args = mock_counter.call_args[0]
            check("counter: email sent to buyer address",
                  call_args[0] == buyer.email,
                  f"got={call_args[0]!r}")
            check("counter: listing title passed to email helper",
                  call_args[1] == listing.title,
                  f"got={call_args[1]!r}")
            check("counter: listing id passed to email helper",
                  call_args[2] == 14401,
                  f"got={call_args[2]!r}")
            check("counter: original offer amount passed (positional arg 3)",
                  call_args[3] == 100.0,
                  f"got={call_args[3]!r}")
            check("counter: counter amount passed (positional arg 4)",
                  call_args[4] == 130.0,
                  f"got={call_args[4]!r}")

        # ── 4: accept with buyer who has no email → email helper NOT called ──
        listing2 = _make_listing(14402, "t144-seller")
        offer4 = _make_offer(14904, 14402, "t144-nobuy", "t144-seller",
                             status="pending", amount=50.0)
        _flu._get_user = lambda: seller

        with patch("email_service.notify_buyer_offer_accepted") as mock_no_email:
            r = client.post(
                "/listing/14402/offer/14904/respond",
                data={"action": "accept"},
                follow_redirects=False,
            )

        check("no-email accept: route still redirects (302)",
              r.status_code == 302, f"status={r.status_code}")
        offer4 = db.session.get(ListingOffer, 14904)
        check("no-email accept: offer status is still 'accepted'",
              offer4.status == "accepted", f"status={offer4.status!r}")
        check("no-email accept: notify_buyer_offer_accepted NOT called",
              mock_no_email.call_count == 0,
              f"call_count={mock_no_email.call_count}")

        # ── 5: decline with buyer who has no email → email helper NOT called ─
        offer5 = _make_offer(14905, 14402, "t144-nobuy", "t144-seller",
                             status="pending", amount=50.0)
        _flu._get_user = lambda: seller

        with patch("email_service.notify_buyer_offer_declined") as mock_no_email_d:
            r = client.post(
                "/listing/14402/offer/14905/respond",
                data={"action": "decline"},
                follow_redirects=False,
            )

        check("no-email decline: route still redirects (302)",
              r.status_code == 302, f"status={r.status_code}")
        check("no-email decline: notify_buyer_offer_declined NOT called",
              mock_no_email_d.call_count == 0,
              f"call_count={mock_no_email_d.call_count}")

        # ── 6: counter with buyer who has no email → email helper NOT called ─
        offer6 = _make_offer(14906, 14402, "t144-nobuy", "t144-seller",
                             status="pending", amount=50.0)
        _flu._get_user = lambda: seller

        with patch("email_service.notify_buyer_offer_countered") as mock_no_email_c:
            r = client.post(
                "/listing/14402/offer/14906/respond",
                data={"action": "counter", "counter_amount": "75"},
                follow_redirects=False,
            )

        check("no-email counter: route still redirects (302)",
              r.status_code == 302, f"status={r.status_code}")
        check("no-email counter: notify_buyer_offer_countered NOT called",
              mock_no_email_c.call_count == 0,
              f"call_count={mock_no_email_c.call_count}")

        # ── 7: email helper raising an exception does not crash the route ──
        offer7 = _make_offer(14907, 14401, "t144-buyer", "t144-seller",
                             status="pending", amount=60.0)
        _flu._get_user = lambda: seller

        with patch("email_service.notify_buyer_offer_accepted",
                   side_effect=Exception("SMTP down")):
            r = client.post(
                "/listing/14401/offer/14907/respond",
                data={"action": "accept"},
                follow_redirects=False,
            )

        check("email-exception: route still redirects (302) despite email failure",
              r.status_code == 302, f"status={r.status_code}")
        offer7 = db.session.get(ListingOffer, 14907)
        check("email-exception: offer is still marked accepted",
              offer7.status == "accepted", f"status={offer7.status!r}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
