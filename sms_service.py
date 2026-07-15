"""
Twilio SMS service for JHE Haul.

Features:
- Per-event-type enable/disable via SmsSettings (admin-controlled)
- Full logging to SmsLog table (SID, status, retry count, errors)
- One automatic retry on transient Twilio failures (2-second delay)
- Phone verification (6-digit code, 10-minute expiry)
- Email-fallback SMS helper
- Supports TWILIO_PHONE_NUMBER or legacy TWILIO_FROM_NUMBER env var
"""
import os
import logging
import time
import random
import string

_APP_URL = os.environ.get("APP_BASE_URL", "https://jhehaul.com")

# Maps event_type string → SmsSettings column name
_EVENT_TO_SETTING = {
    'hauler_new_job_nearby':   'ev_job_nearby',
    'customer_new_bid':        'ev_new_bid',
    'hauler_bid_accepted':     'ev_bid_accepted',
    'hauler_bid_rejected':     'ev_bid_rejected',
    'hauler_deposit_paid':     'ev_deposit_paid',
    'customer_job_completed':  'ev_job_completed',
    'hauler_job_cancelled':    'ev_job_cancelled',
    'admin_alert':             'ev_admin_alert',
    'customer_quote_received': 'ev_quote_received',
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _format_phone(phone):
    """Normalize a phone number to E.164 (+1XXXXXXXXXX). Returns None if invalid."""
    if not phone:
        return None
    p = phone.strip()
    if p.startswith('+'):
        return p
    digits = ''.join(c for c in p if c.isdigit())
    if len(digits) == 10:
        return '+1' + digits
    if len(digits) == 11 and digits.startswith('1'):
        return '+' + digits
    return None


def _twilio_client():
    """Return (Client, from_number) or (None, None) if not configured."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_PHONE_NUMBER") or os.environ.get("TWILIO_FROM_NUMBER")
    if not (sid and tok and frm):
        return None, None
    try:
        from twilio.rest import Client
        return Client(sid, tok), frm
    except Exception as e:
        logging.error("Twilio client init failed: %s", e)
        return None, None


def get_sms_settings():
    """
    Get or create the single SmsSettings row.
    Safe to call from routes (existing app context) and background threads
    (wraps its own context via current_app).
    Returns a SmsSettings ORM object or a defaults stub on error.
    """
    try:
        from flask import current_app
        from models import db, SmsSettings
        with current_app.app_context():
            s = SmsSettings.query.first()
            if not s:
                s = SmsSettings()
                db.session.add(s)
                db.session.commit()
            return s
    except Exception as e:
        logging.warning("Could not load SmsSettings: %s", e)
        class _Defaults:
            sms_globally_enabled = True
            ev_new_bid = True
            ev_bid_accepted = True
            ev_deposit_paid = True
            ev_job_nearby = True
            ev_job_completed = True
            ev_job_cancelled = True
            ev_bid_rejected = False
            ev_admin_alert = False
            email_fallback_to_sms = False
        return _Defaults()


def is_sms_enabled(event_type=None):
    """
    Return True if SMS is globally enabled AND the given event type is enabled.
    If event_type is None or unknown, only checks the global flag.
    """
    s = get_sms_settings()
    if not s.sms_globally_enabled:
        return False
    if event_type and event_type in _EVENT_TO_SETTING:
        return bool(getattr(s, _EVENT_TO_SETTING[event_type], True))
    return True


def _log_sms(event_type, phone, message, status,
             twilio_sid=None, error_msg=None, retry_count=0):
    """Write a row to sms_logs. Never raises — SMS delivery is unaffected."""
    try:
        from flask import current_app
        from models import db, SmsLog
        with current_app.app_context():
            log = SmsLog(
                event_type=event_type,
                recipient_phone=phone or '',
                message_body=(message or '')[:500],
                twilio_sid=twilio_sid,
                status=status,
                error_msg=str(error_msg)[:500] if error_msg else None,
                retry_count=retry_count,
            )
            db.session.add(log)
            db.session.commit()
    except Exception as e:
        logging.warning("SmsLog write failed: %s", e)


# ── Core send ──────────────────────────────────────────────────────────────────

def _friendly_twilio_error(err_str):
    """
    Convert raw Twilio exception strings into short, human-readable messages.
    Twilio embeds the HTTP status + error code in the string, e.g.:
      HTTP 400 error: Unable to create record: ... code 21608
    """
    s = str(err_str)
    if '21608' in s:
        return ('Trial account restriction: the destination number is not a verified '
                'Twilio number. Upgrade your Twilio account or add the number at '
                'console.twilio.com → Phone Numbers → Verified Caller IDs.')
    if '21211' in s:
        return 'Invalid "To" phone number — check the number is a valid US number in E.164 format.'
    if '21214' in s:
        return '"To" number is not a mobile number capable of receiving SMS.'
    if '20003' in s or 'authenticate' in s.lower():
        return 'Twilio authentication failed — check TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.'
    if '21606' in s:
        return 'The "From" number is not SMS-capable. Check TWILIO_PHONE_NUMBER.'
    if 'connect' in s.lower() or 'timeout' in s.lower():
        return f'Network error connecting to Twilio: {s[:120]}'
    # Default: trim to 300 chars so the log column doesn't overflow
    return s[:300]


def send_sms(to_phone, message, event_type='sms'):
    """
    Send an SMS via Twilio.
    - Always logs the E.164-formatted phone number (never raw digits)
    - Logs every attempt to sms_logs table (SID, status, friendly error)
    - Retries once automatically on transient failure (2-second delay)
    - Returns True on success, False on failure
    - Does NOT enforce per-user opt-in (callers handle that)
    - Does NOT check per-event settings (call is_sms_enabled() upstream)
    """
    # Format to E.164 first so every log entry shows the canonical number
    formatted = _format_phone(to_phone) if to_phone else None
    log_phone = formatted or to_phone or ''   # fall back to raw if unparseable

    client, from_phone = _twilio_client()
    if not client:
        logging.warning("Twilio not configured — SMS skipped (event=%s)", event_type)
        _log_sms(event_type, log_phone, message, 'no_twilio',
                 error_msg='TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_PHONE_NUMBER not set')
        return False

    if not to_phone:
        logging.warning("send_sms: no recipient phone (event=%s)", event_type)
        return False

    if not formatted:
        logging.warning("send_sms: cannot format phone %r (event=%s)", to_phone, event_type)
        _log_sms(event_type, log_phone, message, 'failed',
                 error_msg=f'Cannot parse phone number into E.164: {to_phone!r}')
        return False

    for attempt in range(2):
        try:
            msg = client.messages.create(body=message, from_=from_phone, to=formatted)
            logging.info("SMS sent → %s | SID=%s | event=%s | attempt=%d",
                         formatted, msg.sid, event_type, attempt + 1)
            # Log E.164 number and Twilio SID + status
            _log_sms(event_type, formatted, message, 'sent',
                     twilio_sid=msg.sid, retry_count=attempt)
            return True
        except Exception as e:
            friendly = _friendly_twilio_error(e)
            if attempt == 0:
                logging.warning("SMS attempt 1 failed — retrying in 2s (event=%s): %s",
                                event_type, friendly)
                time.sleep(2)
            else:
                logging.error("SMS failed after retry (event=%s → %s): %s",
                              event_type, formatted, friendly)
                _log_sms(event_type, formatted, message, 'failed',
                         error_msg=friendly, retry_count=attempt)
                return False
    return False


def send_verification_sms(phone):
    """
    Generate a 6-digit verification code, send it, and return the code.
    Returns the code string on success, None on failure.
    The caller must save the code to user.phone_verify_code.
    """
    code = ''.join(random.choices(string.digits, k=6))
    msg = f"JHE Haul verification code: {code}. Expires in 10 minutes. Do not share this code."
    ok = send_sms(phone, msg, 'phone_verification')
    return code if ok else None


def sms_fallback(email_sent_ok, user, event_type, sms_message):
    """
    If an email failed AND admin has enabled email-fallback-to-SMS,
    send an SMS to the user.  Never raises.
    """
    if email_sent_ok:
        return
    try:
        s = get_sms_settings()
        if not s.email_fallback_to_sms:
            return
        if user and user.phone and user.notify_sms:
            send_sms(user.phone, sms_message, f'fallback_{event_type}')
    except Exception as e:
        logging.warning("sms_fallback error: %s", e)


# ── Hauler notifications ───────────────────────────────────────────────────────

def notify_hauler_new_job_sms(phone, job_id, distance_miles):
    if not is_sms_enabled('hauler_new_job_nearby'):
        return False
    msg = (f"JHE Haul: New job #{job_id} posted ~{distance_miles:.0f} mi away! "
           f"Log in to view & bid: {_APP_URL}/hauler/jobs")
    return send_sms(phone, msg, 'hauler_new_job_nearby')


def notify_hauler_bid_accepted_sms(phone, job_id):
    if not is_sms_enabled('hauler_bid_accepted'):
        return False
    msg = (f"JHE Haul: Your bid on job #{job_id} was ACCEPTED! "
           f"Waiting for the customer deposit — you'll get the pickup address once paid. "
           f"{_APP_URL}/hauler/jobs")
    return send_sms(phone, msg, 'hauler_bid_accepted')


def notify_hauler_deposit_paid_sms(phone, job_id):
    if not is_sms_enabled('hauler_deposit_paid'):
        return False
    msg = (f"JHE Haul: Deposit paid for job #{job_id}! "
           f"Log in to see the pickup address & get directions: {_APP_URL}/hauler/jobs")
    return send_sms(phone, msg, 'hauler_deposit_paid')


def notify_hauler_bid_rejected_sms(phone, job_id):
    if not is_sms_enabled('hauler_bid_rejected'):
        return False
    msg = (f"JHE Haul: The customer chose another hauler for job #{job_id}. "
           f"New jobs post daily — keep bidding! {_APP_URL}/hauler/jobs")
    return send_sms(phone, msg, 'hauler_bid_rejected')


def notify_hauler_job_cancelled_sms(phone, job_id):
    if not is_sms_enabled('hauler_job_cancelled'):
        return False
    msg = (f"JHE Haul: Job #{job_id} was cancelled by the customer. "
           f"Browse other open jobs: {_APP_URL}/hauler/jobs")
    return send_sms(phone, msg, 'hauler_job_cancelled')


# ── Customer notifications ─────────────────────────────────────────────────────

def notify_customer_new_bid_sms(phone, job_id, hauler_name, quote_amount):
    if not is_sms_enabled('customer_new_bid'):
        return False
    msg = (f"JHE Haul: {hauler_name} bid ${quote_amount:.2f} on your job #{job_id}. "
           f"Log in to review bids: {_APP_URL}/customer/request/{job_id}")
    return send_sms(phone, msg, 'customer_new_bid')


def notify_customer_quote_received_sms(phone, job_id, service_type, price):
    if not is_sms_enabled('customer_quote_received'):
        return False
    service_label = service_type or 'your service'
    msg = (f"JHE Haul: Your quote for {service_label} is ready — ${price:.2f}. "
           f"Log in to review and confirm: {_APP_URL}/customer/request/{job_id}")
    return send_sms(phone, msg, 'customer_quote_received')


def notify_customer_deposit_confirmed_sms(phone, job_id, service_type):
    service_label = service_type or 'your service request'
    msg = (f"JHE Haul: Deposit received for {service_label} #{job_id}. "
           f"We'll be in touch to confirm your appointment! {_APP_URL}/customer/request/{job_id}")
    return send_sms(phone, msg, 'customer_deposit_confirmed')


def notify_customer_job_completed_sms(phone, job_id):
    if not is_sms_enabled('customer_job_completed'):
        return False
    msg = (f"JHE Haul: Job #{job_id} is complete! "
           f"Please log in to leave a review for your hauler: {_APP_URL}/customer/request/{job_id}")
    return send_sms(phone, msg, 'customer_job_completed')


# ── Admin notifications ────────────────────────────────────────────────────────

def notify_admin_sms(message):
    """Send an SMS alert to ADMIN_PHONE. Off by default; enable in SMS settings."""
    if not is_sms_enabled('admin_alert'):
        return False
    admin_phone = os.environ.get("ADMIN_PHONE")
    if not admin_phone:
        logging.debug("ADMIN_PHONE not set — admin SMS skipped")
        return False
    return send_sms(admin_phone, f"[JHE Admin] {message}", 'admin_alert')


# ── Admin event-specific SMS helpers ──────────────────────────────────────────

def notify_admin_new_customer_sms(name, email):
    """Admin SMS when a new customer registers. Controlled by ev_admin_alert toggle."""
    return notify_admin_sms(f"New customer: {name} ({email})")


def notify_admin_new_hauler_sms(name, email, home_zip, truck_type):
    """Admin SMS when a new hauler completes setup. Controlled by ev_admin_alert toggle."""
    return notify_admin_sms(
        f"New hauler: {name} ({email}) | {truck_type or 'N/A'} | ZIP {home_zip}"
    )


def notify_admin_new_job_sms(job_id, customer_name, pickup_zip, description):
    """Admin SMS when a new job is posted. Controlled by ev_admin_alert toggle."""
    short = (description or '')[:60]
    return notify_admin_sms(
        f"Job #{job_id} posted | {customer_name} | ZIP {pickup_zip} | {short}"
    )


def notify_admin_bid_accepted_sms(job_id, customer_name, hauler_name, quote):
    """Admin SMS when a bid is accepted. Controlled by ev_admin_alert toggle."""
    return notify_admin_sms(
        f"Bid accepted: Job #{job_id} | ${float(quote):.0f} | {customer_name} → {hauler_name}"
    )


def notify_admin_new_bid_sms(job_id, hauler_name, quote):
    """Admin SMS when a new bid is submitted. Controlled by ev_admin_alert toggle."""
    return notify_admin_sms(
        f"New bid: Job #{job_id} | {hauler_name} | ${float(quote):.0f}"
    )
