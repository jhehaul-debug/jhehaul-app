"""
Tests: buyer offer email notifications (accepted / declined / countered).

Verifies:
- notify_buyer_offer_accepted / declined / countered call send_email with the
  correct recipient, event_type, and amounts.
- HTML-hostile listing titles are escaped in the body and do NOT appear raw.
- Subject lines have CR/LF stripped.
- Missing buyer email silently skips the send.

Run with:  python tests/test_buyer_offer_emails.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, call as _call

results = []


def check(name, cond, extra=""):
    results.append((name, cond, extra))
    print(("PASS" if cond else "FAIL"), "-", name, (extra if not cond else ""))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(func, *args, send_return=True, **kwargs):
    """Run an email_service helper with send_email patched; return (mock, result)."""
    with patch("email_service.send_email", return_value=send_return) as mock:
        from email_service import notify_buyer_offer_accepted  # noqa – ensure module loaded
        result = func(*args, **kwargs)
    return mock, result


# ---------------------------------------------------------------------------
# Import the three helpers once
# ---------------------------------------------------------------------------
from email_service import (
    notify_buyer_offer_accepted,
    notify_buyer_offer_declined,
    notify_buyer_offer_countered,
)

BUYER_EMAIL = "buyer@example.com"
TITLE = "Vintage Bicycle"
LISTING_ID = 42
OFFER_AMT = 75.00
COUNTER_AMT = 90.00

# ── Accepted ─────────────────────────────────────────────────────────────────

with patch("email_service.send_email", return_value=True) as mock_send:
    res = notify_buyer_offer_accepted(BUYER_EMAIL, TITLE, LISTING_ID, OFFER_AMT)

check("accepted: send_email called once", mock_send.call_count == 1,
      f"call_count={mock_send.call_count}")
args_a = mock_send.call_args[0]  # positional: (to, subject, html, event_type)
check("accepted: recipient is buyer email", args_a[0] == BUYER_EMAIL,
      f"got={args_a[0]!r}")
check("accepted: event_type is 'buyer_offer_accepted'", args_a[3] == 'buyer_offer_accepted',
      f"got={args_a[3]!r}")
check("accepted: subject contains listing title", TITLE in args_a[1],
      f"subject={args_a[1]!r}")
check("accepted: body contains formatted offer amount", "75.00" in args_a[2],
      f"body snippet={args_a[2][:200]!r}")
check("accepted: helper returns True on success", res is True, f"got={res!r}")

# ── Declined ─────────────────────────────────────────────────────────────────

with patch("email_service.send_email", return_value=True) as mock_send:
    res = notify_buyer_offer_declined(BUYER_EMAIL, TITLE, LISTING_ID, OFFER_AMT)

check("declined: send_email called once", mock_send.call_count == 1,
      f"call_count={mock_send.call_count}")
args_d = mock_send.call_args[0]
check("declined: recipient is buyer email", args_d[0] == BUYER_EMAIL,
      f"got={args_d[0]!r}")
check("declined: event_type is 'buyer_offer_declined'", args_d[3] == 'buyer_offer_declined',
      f"got={args_d[3]!r}")
check("declined: subject contains listing title", TITLE in args_d[1],
      f"subject={args_d[1]!r}")
check("declined: body contains formatted offer amount", "75.00" in args_d[2],
      f"body snippet={args_d[2][:200]!r}")

# ── Countered ────────────────────────────────────────────────────────────────

with patch("email_service.send_email", return_value=True) as mock_send:
    res = notify_buyer_offer_countered(BUYER_EMAIL, TITLE, LISTING_ID, OFFER_AMT, COUNTER_AMT)

check("countered: send_email called once", mock_send.call_count == 1,
      f"call_count={mock_send.call_count}")
args_c = mock_send.call_args[0]
check("countered: recipient is buyer email", args_c[0] == BUYER_EMAIL,
      f"got={args_c[0]!r}")
check("countered: event_type is 'buyer_offer_countered'", args_c[3] == 'buyer_offer_countered',
      f"got={args_c[3]!r}")
check("countered: subject contains listing title", TITLE in args_c[1],
      f"subject={args_c[1]!r}")
check("countered: subject contains counter amount", "90.00" in args_c[1],
      f"subject={args_c[1]!r}")
check("countered: body contains original offer amount", "75.00" in args_c[2],
      f"body snippet={args_c[2][:300]!r}")
check("countered: body contains counter amount", "90.00" in args_c[2],
      f"body snippet={args_c[2][:300]!r}")

# ── HTML injection escaping ───────────────────────────────────────────────────

HOSTILE_TITLE = '<script>alert("xss")</script> & "Bargain" <img src=x>'

with patch("email_service.send_email", return_value=True) as mock_send:
    notify_buyer_offer_accepted(BUYER_EMAIL, HOSTILE_TITLE, LISTING_ID, OFFER_AMT)

html_body = mock_send.call_args[0][2]
check("accepted: raw <script> tag absent from HTML body",
      "<script>" not in html_body,
      f"body contains raw script tag")
check("accepted: escaped &lt;script&gt; present in HTML body",
      "&lt;script&gt;" in html_body,
      f"escaped tag not found in body")
check("accepted: & in title escaped as &amp; in body",
      "&amp;" in html_body,
      f"& not escaped in body")

with patch("email_service.send_email", return_value=True) as mock_send:
    notify_buyer_offer_declined(BUYER_EMAIL, HOSTILE_TITLE, LISTING_ID, OFFER_AMT)

html_body_d = mock_send.call_args[0][2]
check("declined: raw <script> tag absent from HTML body",
      "<script>" not in html_body_d,
      "body contains raw script tag")

with patch("email_service.send_email", return_value=True) as mock_send:
    notify_buyer_offer_countered(BUYER_EMAIL, HOSTILE_TITLE, LISTING_ID, OFFER_AMT, COUNTER_AMT)

html_body_c = mock_send.call_args[0][2]
check("countered: raw <script> tag absent from HTML body",
      "<script>" not in html_body_c,
      "body contains raw script tag")

# ── CRLF injection in subject stripped ───────────────────────────────────────

CRLF_TITLE = "Legit Item\r\nBcc: evil@bad.com"

with patch("email_service.send_email", return_value=True) as mock_send:
    notify_buyer_offer_accepted(BUYER_EMAIL, CRLF_TITLE, LISTING_ID, OFFER_AMT)

subject_crlf = mock_send.call_args[0][1]
check("accepted: subject has CR stripped", "\r" not in subject_crlf,
      f"subject={subject_crlf!r}")
check("accepted: subject has LF stripped", "\n" not in subject_crlf,
      f"subject={subject_crlf!r}")

# ── None listing title falls back gracefully ──────────────────────────────────

with patch("email_service.send_email", return_value=True) as mock_send:
    notify_buyer_offer_accepted(BUYER_EMAIL, None, LISTING_ID, OFFER_AMT)

args_none = mock_send.call_args[0]
check("accepted: None title falls back to 'Listing #42'",
      f"Listing #{LISTING_ID}" in args_none[1],
      f"subject={args_none[1]!r}")

# ── Summary ───────────────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
