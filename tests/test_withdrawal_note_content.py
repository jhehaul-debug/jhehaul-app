"""
Tests: withdrawal note content in email and SMS notifications.

Verifies:
- notify_customer_quote_withdrawn includes the note in the HTML body when provided
- notify_customer_quote_withdrawn omits the note block when no note is given
- notify_customer_quote_withdrawn_sms injects the note into the SMS text when provided
- notify_customer_quote_withdrawn_sms omits the note when none is given
- Blank / whitespace-only notes are treated the same as no note (both functions)

Run with:  python tests/test_withdrawal_note_content.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock

results = []


def check(name, cond, extra=""):
    results.append((name, cond, extra))
    print(("PASS" if cond else "FAIL"), "-", name, (extra if not cond else ""))


# ── Email: notify_customer_quote_withdrawn ─────────────────────────────────────

def _run_email(withdrawal_note, send_return=True):
    """Patch send_email, call notify_customer_quote_withdrawn, return (html_body, result)."""
    captured = {}
    def _fake_send(to, subject, html_content, event_type='email'):
        captured['html'] = html_content
        return send_return

    with patch("email_service.send_email", side_effect=_fake_send):
        from email_service import notify_customer_quote_withdrawn
        result = notify_customer_quote_withdrawn(
            customer_email="customer@example.com",
            job_id=7,
            service_type="Junk Removal",
            withdrawal_note=withdrawal_note,
        )
    return captured.get('html', ''), result


# Test 1: note present → appears in rendered HTML body
html_with_note, _ = _run_email("Price changed — service unavailable in your area.")
check(
    "Email body contains the withdrawal note text when note is provided",
    "Price changed — service unavailable in your area." in html_with_note,
    f"html snippet: {html_with_note[html_with_note.find('Note'):html_with_note.find('Note')+120]!r}",
)
check(
    "Email body contains 'Note from our team' label when note is provided",
    "Note from our team" in html_with_note,
    "",
)

# Test 2: no note (None) → note block absent
html_no_note, _ = _run_email(None)
check(
    "Email body does NOT contain 'Note from our team' when no note given",
    "Note from our team" not in html_no_note,
    f"unexpected text found in: {html_no_note[:300]!r}" if "Note from our team" in html_no_note else "",
)

# Test 3: empty string → treated as no note
html_empty, _ = _run_email("")
check(
    "Email body does NOT contain note block for empty-string note",
    "Note from our team" not in html_empty,
    f"found in: {html_empty[:300]!r}" if "Note from our team" in html_empty else "",
)

# Test 4: whitespace-only → treated as no note
html_ws, _ = _run_email("   ")
check(
    "Email body does NOT contain note block for whitespace-only note",
    "Note from our team" not in html_ws,
    f"found in: {html_ws[:300]!r}" if "Note from our team" in html_ws else "",
)

# Test 5: whitespace note text itself is not injected
check(
    "Email body does not inject bare whitespace into note area",
    # The stripped whitespace note should not appear wrapped in the note info-box
    'white-space:pre-wrap;">   </p>' not in html_ws,
    "",
)

# Test 6: event_type is correct
captured_event = {}
def _capture_event(to, subject, html_content, event_type='email'):
    captured_event['event_type'] = event_type
    return True

with patch("email_service.send_email", side_effect=_capture_event):
    from email_service import notify_customer_quote_withdrawn
    notify_customer_quote_withdrawn("x@example.com", 1, "Hauling", "Some note")

check(
    "notify_customer_quote_withdrawn uses event_type='customer_quote_withdrawn'",
    captured_event.get('event_type') == 'customer_quote_withdrawn',
    f"got: {captured_event.get('event_type')!r}",
)

# Test 7: request link is in the body
check(
    "Email body contains link to customer request page",
    "/customer/request/7" in html_with_note,
    "",
)


# ── SMS: notify_customer_quote_withdrawn_sms ───────────────────────────────────

def _make_sms_settings(globally_enabled=True, ev_quote_withdrawn=True):
    s = MagicMock()
    s.sms_globally_enabled = globally_enabled
    s.ev_quote_withdrawn = ev_quote_withdrawn
    return s


def _run_sms(withdrawal_note, settings=None):
    """Patch send_sms + settings, return (sms_body, result)."""
    if settings is None:
        settings = _make_sms_settings()
    captured = {}
    def _fake_send(to_phone, message, event_type='sms'):
        captured['message'] = message
        return True

    with patch("sms_service.get_sms_settings", return_value=settings), \
         patch("sms_service.send_sms", side_effect=_fake_send):
        from sms_service import notify_customer_quote_withdrawn_sms
        result = notify_customer_quote_withdrawn_sms(
            phone="+16515550099",
            job_id=7,
            service_type="Junk Removal",
            withdrawal_note=withdrawal_note,
        )
    return captured.get('message', ''), result


# Test 8: note present → included in SMS message
sms_with_note, _ = _run_sms("Schedule conflict next month.")
check(
    "SMS message contains the withdrawal note when note is provided",
    "Schedule conflict next month." in sms_with_note,
    f"sms: {sms_with_note!r}",
)
check(
    "SMS message contains 'Note:' label when note is provided",
    "Note:" in sms_with_note,
    f"sms: {sms_with_note!r}",
)

# Test 9: no note (None) → "Note:" absent from SMS
sms_no_note, _ = _run_sms(None)
check(
    "SMS message does NOT contain 'Note:' when no note given",
    "Note:" not in sms_no_note,
    f"unexpected text found: {sms_no_note!r}",
)

# Test 10: empty string → treated as no note in SMS
sms_empty, _ = _run_sms("")
check(
    "SMS message does NOT contain 'Note:' for empty-string note",
    "Note:" not in sms_empty,
    f"unexpected text found: {sms_empty!r}",
)

# Test 11: whitespace-only → treated as no note in SMS
sms_ws, _ = _run_sms("   ")
check(
    "SMS message does NOT contain 'Note:' for whitespace-only note",
    "Note:" not in sms_ws,
    f"unexpected text found: {sms_ws!r}",
)

# Test 12: note is truncated at 120 characters in SMS
long_note = "A" * 200
sms_long, _ = _run_sms(long_note)
check(
    "Long note is truncated to 120 characters in SMS",
    long_note not in sms_long and ("A" * 120) in sms_long,
    f"sms: {sms_long!r}",
)

# Test 13: SMS contains the request link
check(
    "SMS message contains link to customer request page",
    "/customer/request/7" in sms_with_note,
    f"sms: {sms_with_note!r}",
)

# Test 14: SMS event_type is correct
event_check = {}
def _capture_sms_event(to_phone, message, event_type='sms'):
    event_check['event_type'] = event_type
    return True

with patch("sms_service.get_sms_settings", return_value=_make_sms_settings()), \
     patch("sms_service.send_sms", side_effect=_capture_sms_event):
    from sms_service import notify_customer_quote_withdrawn_sms
    notify_customer_quote_withdrawn_sms("+16515550099", 7, "Junk Removal", "Note text")

check(
    "notify_customer_quote_withdrawn_sms uses event_type='customer_quote_withdrawn'",
    event_check.get('event_type') == 'customer_quote_withdrawn',
    f"got: {event_check.get('event_type')!r}",
)


# ── Summary ───────────────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("\nFailed tests:")
    for name, _, extra in failed:
        print(f"  FAIL - {name}", extra)
sys.exit(1 if failed else 0)
