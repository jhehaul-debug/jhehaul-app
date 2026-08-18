"""
test_seller_new_message_email.py — confirm notify_seller_new_message fires only
on the buyer's *first* message in a conversation, not on subsequent replies.

Covers (sync/fallback path — _queue_email unavailable):
  1. No-convo_id path: first buyer message  → email sent exactly once
  2. No-convo_id path: second buyer message → no email
  3. With-convo_id path: first buyer message  → email sent exactly once
  4. With-convo_id path: second buyer message → no email

Covers (queue path — _queue_email available):
  5. No-convo_id path: first buyer message  → _queue_email enqueued once
  6. No-convo_id path: second buyer message → _queue_email not called
  7. With-convo_id path: first buyer message  → _queue_email enqueued once
  8. With-convo_id path: second buyer message → _queue_email not called
"""

import sys
import uuid
from unittest.mock import patch, MagicMock

from app import app, db
import routes  # registers all URL rules


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _create_user(prefix="user"):
    from models import User
    u = User()
    u.id = str(uuid.uuid4())
    u.email = f"{prefix}_{u.id[:8]}@test.local"
    u.first_name = prefix.title()
    u.user_type = "customer"
    u.is_admin = False
    u.age_confirmed = True
    u.profile_nudge_dismissed = True
    db.session.add(u)
    db.session.commit()
    return u.id


def _create_listing(seller_id, status="active"):
    from models import Listing
    l = Listing()
    l.seller_id = seller_id
    l.title = "Test listing for message email"
    l.status = status
    l.listing_type = "item"
    l.moderation_status = "approved"
    l.price_type = "fixed"
    l.price = 75.00
    db.session.add(l)
    db.session.commit()
    return l.id


def _create_convo(listing_id, buyer_id, seller_id):
    from models import ListingConversation
    c = ListingConversation(
        listing_id=listing_id,
        buyer_id=buyer_id,
        seller_id=seller_id,
    )
    db.session.add(c)
    db.session.commit()
    return c.id


def _add_message(convo_id, sender_id, body="Hello"):
    from models import ListingMessage
    m = ListingMessage(
        conversation_id=convo_id,
        sender_id=sender_id,
        body=body,
    )
    db.session.add(m)
    db.session.commit()
    return m.id


def _mock_buyer(buyer_id, buyer_email="buyer@test.local"):
    """Return a MagicMock that satisfies flask_login and route checks for a buyer."""
    u = MagicMock()
    u.is_authenticated = True
    u.is_admin = False
    u.is_active = True
    u.user_type = "customer"
    u.id = buyer_id
    u.email = buyer_email
    u.first_name = "Buyer"
    u.age_confirmed = True
    u.profile_nudge_dismissed = True
    u.profile_image_url = None
    u.profile_photo_data = None
    u.hide_sold_pref = False
    return u


def _cleanup(*model_id_pairs):
    with app.app_context():
        for Model, row_id in model_id_pairs:
            if row_id is None:
                continue
            obj = Model.query.get(row_id)
            if obj:
                db.session.delete(obj)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


def _delete_messages_for(convo_id):
    """Delete all listing messages for a conversation (before deleting convo)."""
    if not convo_id:
        return
    with app.app_context():
        from models import ListingMessage
        ListingMessage.query.filter_by(
            conversation_id=convo_id
        ).delete(synchronize_session=False)
        db.session.commit()


def _get_convo_id_for(listing_id, buyer_id):
    """Look up the conversation created by the route POST."""
    with app.app_context():
        from models import ListingConversation
        convo = ListingConversation.query.filter_by(
            listing_id=listing_id, buyer_id=buyer_id
        ).first()
        return convo.id if convo else None


# ── Test runner ────────────────────────────────────────────────────────────────

PASS = []
FAIL = []


def run(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  PASS  {name}")
    except Exception as exc:
        import traceback
        FAIL.append((name, exc))
        print(f"  FAIL  {name}: {exc}")
        traceback.print_exc()


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_no_convo_id_first_message_sends_email():
    """No-convo_id path: buyer's first message triggers exactly one seller email."""
    seller_id = buyer_id = listing_id = convo_id = None
    with app.app_context():
        seller_id  = _create_user("seller")
        buyer_id   = _create_user("buyer")
        listing_id = _create_listing(seller_id)

    mock_buyer = _mock_buyer(buyer_id)

    try:
        mock_email = MagicMock(return_value=True)
        with patch("flask_login.utils._get_user", return_value=mock_buyer), \
             patch("flask_wtf.csrf.validate_csrf"), \
             patch("routes._queue_email", return_value=False), \
             patch("routes.notify_seller_new_message", mock_email), \
             patch("notification_service.notify_new_message", MagicMock()):
            client = app.test_client()
            resp = client.post(
                f"/listing/{listing_id}/message",
                data={"body": "Hi, is this still available?",
                      "csrf_token": "dummy"},
                follow_redirects=False,
            )

        assert resp.status_code in (302, 303), (
            f"Expected redirect, got {resp.status_code}"
        )
        assert mock_email.call_count == 1, (
            f"Expected notify_seller_new_message called once on first message, "
            f"got {mock_email.call_count} calls"
        )
    finally:
        convo_id = _get_convo_id_for(listing_id, buyer_id)
        _delete_messages_for(convo_id)
        from models import Listing, ListingConversation, User
        _cleanup(
            (ListingConversation, convo_id),
            (Listing, listing_id),
            (User, buyer_id),
            (User, seller_id),
        )


def test_no_convo_id_second_message_no_email():
    """No-convo_id path: a second buyer message in the same thread sends no email."""
    seller_id = buyer_id = listing_id = convo_id = first_msg_id = None
    with app.app_context():
        seller_id    = _create_user("seller2")
        buyer_id     = _create_user("buyer2")
        listing_id   = _create_listing(seller_id)
        convo_id     = _create_convo(listing_id, buyer_id, seller_id)
        first_msg_id = _add_message(convo_id, buyer_id, "First message already sent")

    mock_buyer = _mock_buyer(buyer_id)

    try:
        mock_email = MagicMock(return_value=True)
        with patch("flask_login.utils._get_user", return_value=mock_buyer), \
             patch("flask_wtf.csrf.validate_csrf"), \
             patch("routes._queue_email", return_value=False), \
             patch("routes.notify_seller_new_message", mock_email), \
             patch("notification_service.notify_new_message", MagicMock()):
            client = app.test_client()
            resp = client.post(
                f"/listing/{listing_id}/message",
                data={"body": "Any update on this?", "csrf_token": "dummy"},
                follow_redirects=False,
            )

        assert resp.status_code in (302, 303), (
            f"Expected redirect, got {resp.status_code}"
        )
        assert mock_email.call_count == 0, (
            f"Expected no email on second buyer message, "
            f"got {mock_email.call_count} calls"
        )
    finally:
        _delete_messages_for(convo_id)
        from models import Listing, ListingConversation, User
        _cleanup(
            (ListingConversation, convo_id),
            (Listing, listing_id),
            (User, buyer_id),
            (User, seller_id),
        )


def test_with_convo_id_first_message_sends_email():
    """With-convo_id path: buyer's first message in the thread sends exactly one email."""
    seller_id = buyer_id = listing_id = convo_id = None
    with app.app_context():
        seller_id  = _create_user("seller3")
        buyer_id   = _create_user("buyer3")
        listing_id = _create_listing(seller_id)
        convo_id   = _create_convo(listing_id, buyer_id, seller_id)
        # No messages yet in this conversation

    mock_buyer = _mock_buyer(buyer_id)

    try:
        mock_email = MagicMock(return_value=True)
        with patch("flask_login.utils._get_user", return_value=mock_buyer), \
             patch("flask_wtf.csrf.validate_csrf"), \
             patch("routes._queue_email", return_value=False), \
             patch("routes.notify_seller_new_message", mock_email), \
             patch("notification_service.notify_new_message", MagicMock()):
            client = app.test_client()
            resp = client.post(
                f"/listing/{listing_id}/message/{convo_id}",
                data={"body": "Is this available to pick up today?",
                      "csrf_token": "dummy"},
                follow_redirects=False,
            )

        assert resp.status_code in (302, 303), (
            f"Expected redirect, got {resp.status_code}"
        )
        assert mock_email.call_count == 1, (
            f"Expected notify_seller_new_message called once on first message "
            f"(convo_id path), got {mock_email.call_count} calls"
        )
    finally:
        _delete_messages_for(convo_id)
        from models import Listing, ListingConversation, User
        _cleanup(
            (ListingConversation, convo_id),
            (Listing, listing_id),
            (User, buyer_id),
            (User, seller_id),
        )


def test_with_convo_id_second_message_no_email():
    """With-convo_id path: buyer's second message in the thread sends no email."""
    seller_id = buyer_id = listing_id = convo_id = first_msg_id = None
    with app.app_context():
        seller_id    = _create_user("seller4")
        buyer_id     = _create_user("buyer4")
        listing_id   = _create_listing(seller_id)
        convo_id     = _create_convo(listing_id, buyer_id, seller_id)
        first_msg_id = _add_message(convo_id, buyer_id, "Already messaged once")

    mock_buyer = _mock_buyer(buyer_id)

    try:
        mock_email = MagicMock(return_value=True)
        with patch("flask_login.utils._get_user", return_value=mock_buyer), \
             patch("flask_wtf.csrf.validate_csrf"), \
             patch("routes._queue_email", return_value=False), \
             patch("routes.notify_seller_new_message", mock_email), \
             patch("notification_service.notify_new_message", MagicMock()):
            client = app.test_client()
            resp = client.post(
                f"/listing/{listing_id}/message/{convo_id}",
                data={"body": "Just following up!", "csrf_token": "dummy"},
                follow_redirects=False,
            )

        assert resp.status_code in (302, 303), (
            f"Expected redirect, got {resp.status_code}"
        )
        assert mock_email.call_count == 0, (
            f"Expected no email on second buyer message (convo_id path), "
            f"got {mock_email.call_count} calls"
        )
    finally:
        _delete_messages_for(convo_id)
        from models import Listing, ListingConversation, User
        _cleanup(
            (ListingConversation, convo_id),
            (Listing, listing_id),
            (User, buyer_id),
            (User, seller_id),
        )


# ── Queue-path tests (when _queue_email is available) ─────────────────────────

def test_no_convo_id_first_message_enqueues_job():
    """No-convo_id path: first buyer message enqueues exactly one email job when queue is up."""
    seller_id = buyer_id = listing_id = convo_id = None
    with app.app_context():
        seller_id  = _create_user("seller5")
        buyer_id   = _create_user("buyer5")
        listing_id = _create_listing(seller_id)

    mock_buyer = _mock_buyer(buyer_id)

    try:
        mock_queue = MagicMock(return_value=True)  # queue available
        with patch("flask_login.utils._get_user", return_value=mock_buyer), \
             patch("flask_wtf.csrf.validate_csrf"), \
             patch("routes._queue_email", mock_queue), \
             patch("notification_service.notify_new_message", MagicMock()):
            client = app.test_client()
            resp = client.post(
                f"/listing/{listing_id}/message",
                data={"body": "Is this available?", "csrf_token": "dummy"},
                follow_redirects=False,
            )

        assert resp.status_code in (302, 303), (
            f"Expected redirect, got {resp.status_code}"
        )
        # _queue_email must have been called exactly once with notify_seller_new_message
        assert mock_queue.call_count == 1, (
            f"Expected _queue_email called once on first message, "
            f"got {mock_queue.call_count} calls"
        )
        first_call_fn = mock_queue.call_args[0][0]
        assert first_call_fn == "notify_seller_new_message", (
            f"Expected fn='notify_seller_new_message', got '{first_call_fn}'"
        )
    finally:
        convo_id = _get_convo_id_for(listing_id, buyer_id)
        _delete_messages_for(convo_id)
        from models import Listing, ListingConversation, User
        _cleanup(
            (ListingConversation, convo_id),
            (Listing, listing_id),
            (User, buyer_id),
            (User, seller_id),
        )


def test_no_convo_id_second_message_no_queue_call():
    """No-convo_id path: second buyer message does not enqueue any email job."""
    seller_id = buyer_id = listing_id = convo_id = first_msg_id = None
    with app.app_context():
        seller_id    = _create_user("seller6")
        buyer_id     = _create_user("buyer6")
        listing_id   = _create_listing(seller_id)
        convo_id     = _create_convo(listing_id, buyer_id, seller_id)
        first_msg_id = _add_message(convo_id, buyer_id, "Already sent first message")

    mock_buyer = _mock_buyer(buyer_id)

    try:
        mock_queue = MagicMock(return_value=True)
        with patch("flask_login.utils._get_user", return_value=mock_buyer), \
             patch("flask_wtf.csrf.validate_csrf"), \
             patch("routes._queue_email", mock_queue), \
             patch("notification_service.notify_new_message", MagicMock()):
            client = app.test_client()
            resp = client.post(
                f"/listing/{listing_id}/message",
                data={"body": "Following up!", "csrf_token": "dummy"},
                follow_redirects=False,
            )

        assert resp.status_code in (302, 303), (
            f"Expected redirect, got {resp.status_code}"
        )
        assert mock_queue.call_count == 0, (
            f"Expected _queue_email not called on second buyer message, "
            f"got {mock_queue.call_count} calls"
        )
    finally:
        _delete_messages_for(convo_id)
        from models import Listing, ListingConversation, User
        _cleanup(
            (ListingConversation, convo_id),
            (Listing, listing_id),
            (User, buyer_id),
            (User, seller_id),
        )


def test_with_convo_id_first_message_enqueues_job():
    """With-convo_id path: first buyer message enqueues exactly one email job when queue is up."""
    seller_id = buyer_id = listing_id = convo_id = None
    with app.app_context():
        seller_id  = _create_user("seller7")
        buyer_id   = _create_user("buyer7")
        listing_id = _create_listing(seller_id)
        convo_id   = _create_convo(listing_id, buyer_id, seller_id)

    mock_buyer = _mock_buyer(buyer_id)

    try:
        mock_queue = MagicMock(return_value=True)
        with patch("flask_login.utils._get_user", return_value=mock_buyer), \
             patch("flask_wtf.csrf.validate_csrf"), \
             patch("routes._queue_email", mock_queue), \
             patch("notification_service.notify_new_message", MagicMock()):
            client = app.test_client()
            resp = client.post(
                f"/listing/{listing_id}/message/{convo_id}",
                data={"body": "Hi, can I pick this up tomorrow?",
                      "csrf_token": "dummy"},
                follow_redirects=False,
            )

        assert resp.status_code in (302, 303), (
            f"Expected redirect, got {resp.status_code}"
        )
        assert mock_queue.call_count == 1, (
            f"Expected _queue_email called once on first message (convo_id path), "
            f"got {mock_queue.call_count} calls"
        )
        first_call_fn = mock_queue.call_args[0][0]
        assert first_call_fn == "notify_seller_new_message", (
            f"Expected fn='notify_seller_new_message', got '{first_call_fn}'"
        )
    finally:
        _delete_messages_for(convo_id)
        from models import Listing, ListingConversation, User
        _cleanup(
            (ListingConversation, convo_id),
            (Listing, listing_id),
            (User, buyer_id),
            (User, seller_id),
        )


def test_with_convo_id_second_message_no_queue_call():
    """With-convo_id path: second buyer message does not enqueue any email job."""
    seller_id = buyer_id = listing_id = convo_id = first_msg_id = None
    with app.app_context():
        seller_id    = _create_user("seller8")
        buyer_id     = _create_user("buyer8")
        listing_id   = _create_listing(seller_id)
        convo_id     = _create_convo(listing_id, buyer_id, seller_id)
        first_msg_id = _add_message(convo_id, buyer_id, "First message already sent")

    mock_buyer = _mock_buyer(buyer_id)

    try:
        mock_queue = MagicMock(return_value=True)
        with patch("flask_login.utils._get_user", return_value=mock_buyer), \
             patch("flask_wtf.csrf.validate_csrf"), \
             patch("routes._queue_email", mock_queue), \
             patch("notification_service.notify_new_message", MagicMock()):
            client = app.test_client()
            resp = client.post(
                f"/listing/{listing_id}/message/{convo_id}",
                data={"body": "Just checking in!", "csrf_token": "dummy"},
                follow_redirects=False,
            )

        assert resp.status_code in (302, 303), (
            f"Expected redirect, got {resp.status_code}"
        )
        assert mock_queue.call_count == 0, (
            f"Expected _queue_email not called on second buyer message (convo_id path), "
            f"got {mock_queue.call_count} calls"
        )
    finally:
        _delete_messages_for(convo_id)
        from models import Listing, ListingConversation, User
        _cleanup(
            (ListingConversation, convo_id),
            (Listing, listing_id),
            (User, buyer_id),
            (User, seller_id),
        )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nRunning seller new-message email guard tests...\n")

    # Sync/fallback path (queue unavailable → direct email call)
    run("no-convo_id path: first buyer message sends email",
        test_no_convo_id_first_message_sends_email)
    run("no-convo_id path: second buyer message sends no email",
        test_no_convo_id_second_message_no_email)
    run("with-convo_id path: first buyer message sends email",
        test_with_convo_id_first_message_sends_email)
    run("with-convo_id path: second buyer message sends no email",
        test_with_convo_id_second_message_no_email)

    # Queue path (queue available → _queue_email enqueues the job)
    run("no-convo_id path: first buyer message enqueues job",
        test_no_convo_id_first_message_enqueues_job)
    run("no-convo_id path: second buyer message enqueues nothing",
        test_no_convo_id_second_message_no_queue_call)
    run("with-convo_id path: first buyer message enqueues job",
        test_with_convo_id_first_message_enqueues_job)
    run("with-convo_id path: second buyer message enqueues nothing",
        test_with_convo_id_second_message_no_queue_call)

    print(f"\n{'='*60}")
    print(f"Results: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nFailures:")
        for name, exc in FAIL:
            print(f"  {name}: {exc}")
        sys.exit(1)
    else:
        print("All tests passed.")
