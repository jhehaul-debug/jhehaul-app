import os
import logging
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# ── Admin security email helpers (used by routes.py admin security routes) ──

_ADMIN_EMAIL = None
_APP_URL = "https://jhehaul.com"

_EMAIL_STYLE = """
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background:#f7f8fa; margin:0; padding:0; }
  .wrap { max-width:580px; margin:32px auto; background:#fff;
          border-radius:12px; overflow:hidden;
          box-shadow:0 2px 12px rgba(0,0,0,0.08); }
  .header { background:#1a202c; padding:24px 32px; }
  .header h1 { color:#fff; margin:0; font-size:1.3rem; font-weight:700; }
  .header p  { color:#a0aec0; margin:4px 0 0; font-size:0.88rem; }
  .body { padding:28px 32px; color:#2d3748; }
  .body h2 { margin:0 0 14px; font-size:1.1rem; color:#1a202c; }
  .body p  { margin:0 0 10px; line-height:1.6; font-size:0.94rem; color:#4a5568; }
  .tag { display:inline-block; background:#ebf8ff; color:#2b6cb0;
         font-size:0.78rem; font-weight:700; padding:3px 10px;
         border-radius:20px; margin-bottom:16px; }
  .pill { display:inline-block; padding:4px 12px; border-radius:20px;
          font-weight:700; font-size:0.84rem; }
  .pill-green  { background:#dcfce7; color:#15803d; }
  .pill-orange { background:#fff7ed; color:#ea580c; }
  .pill-blue   { background:#dbeafe; color:#1d4ed8; }
  .pill-red    { background:#fee2e2; color:#b91c1c; }
  .info-box { background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px;
              padding:14px 18px; margin:16px 0; }
  .info-box p { margin:4px 0; color:#4a5568; }
  .info-box strong { color:#1a202c; }
  .btn { display:inline-block; background:#1a202c; color:#fff !important;
         text-decoration:none; padding:12px 24px; border-radius:8px;
         font-weight:700; font-size:0.9rem; margin-top:14px; }
  .footer { background:#f8fafc; padding:16px 32px; border-top:1px solid #e2e8f0;
            font-size:0.78rem; color:#a0aec0; text-align:center; }
</style>
"""

def _html(header_title, header_sub, tag, body_html):
    return f"""<!DOCTYPE html><html><head>{_EMAIL_STYLE}</head><body>
<div class="wrap">
  <div class="header">
    <h1>JHE Haul</h1>
    <p>Local Marketplace</p>
  </div>
  <div class="body">
    <div class="tag">{tag}</div>
    <h2>{header_title}</h2>
    <p style="font-size:0.88rem;color:#718096;margin-bottom:18px;">{header_sub}</p>
    {body_html}
  </div>
  <div class="footer">
    JHE Haul · Local Marketplace · <a href="{_APP_URL}" style="color:#3498db;">jhehaul.com</a><br>
    You received this because you have an account with JHE Haul.
  </div>
</div></body></html>"""


def _log_notification(event_type, recipient, subject, status,
                       error_msg=None, sg_status_code=None, sg_message_id=None):
    """Write a row to notification_logs. Never raises — email delivery is unaffected."""
    try:
        from flask import current_app
        from models import db, NotificationLog
        with current_app.app_context():
            log = NotificationLog(
                event_type=event_type,
                recipient=recipient or '',
                subject=subject or '',
                status=status,
                sg_status_code=sg_status_code,
                sg_message_id=sg_message_id,
                error_msg=str(error_msg)[:1000] if error_msg else None,
            )
            db.session.add(log)
            db.session.commit()
    except Exception as e:
        logging.warning("Notification log write failed: %s", e)


def send_email(to_email, subject, html_content, event_type='email'):
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "noreply@jhehaul.com")

    if not api_key:
        logging.error(
            "SENDGRID_API_KEY not set — email NOT sent to %s | subject: %s",
            to_email, subject
        )
        _log_notification(event_type, to_email, subject, 'failed',
                          'SENDGRID_API_KEY environment variable is not set')
        return False

    if not to_email:
        logging.error("send_email called with no recipient (subject: %s)", subject)
        return False

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        html_content=html_content,
    )
    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        status_code = response.status_code
        # X-Message-Id is the SendGrid tracking ID for this specific send
        msg_id = response.headers.get('X-Message-Id', '') if response.headers else ''
        logging.info(
            "SendGrid ACCEPTED → %s | HTTP %s | msg_id=%s | %s",
            to_email, status_code, msg_id, subject
        )
        # 202 = SendGrid accepted for delivery (does NOT guarantee inbox arrival)
        _log_notification(event_type, to_email, subject, 'sent',
                          sg_status_code=status_code, sg_message_id=msg_id)
        return True
    except Exception as e:
        # Parse HTTP status code out of exception if available
        err_str = str(e)
        sg_code = None
        try:
            if hasattr(e, 'status_code'):
                sg_code = e.status_code
            elif hasattr(e, 'body'):
                pass  # body already in str(e)
        except Exception:
            pass
        logging.error(
            "SendGrid ERROR → %s | HTTP %s | %s | %s",
            to_email, sg_code or '???', subject, err_str
        )
        _log_notification(event_type, to_email, subject, 'failed',
                          error_msg=err_str, sg_status_code=sg_code)
        return False


def notify_admin(subject, html_content, event_type='admin'):
    admin_email = os.environ.get("ADMIN_EMAIL", "jhehaul@gmail.com")
    return send_email(admin_email, subject, html_content, event_type)


# ── ADMIN NOTIFICATIONS ────────────────────────────────────────────────────────

def notify_admin_new_customer(user_name, user_email):
    body = f"""
    <div class="info-box">
      <p><strong>Name:</strong> {user_name}</p>
      <p><strong>Email:</strong> {user_email}</p>
    </div>
    <a href="{_APP_URL}/admin/customers" class="btn">View Customers →</a>"""
    return notify_admin(
        f"[JHE Haul] New Customer: {user_name}",
        _html("New Customer Signed Up", "A new customer just created an account.",
              "👤 New Customer", body),
        'admin_new_customer'
    )


def notify_admin_new_hauler(user_name, user_email, home_zip, truck_type):
    body = f"""
    <div class="info-box">
      <p><strong>Name:</strong> {user_name}</p>
      <p><strong>Email:</strong> {user_email}</p>
      <p><strong>Home ZIP:</strong> {home_zip or '—'}</p>
      <p><strong>Truck Type:</strong> {truck_type or 'Not specified'}</p>
    </div>
    <a href="{_APP_URL}/admin/haulers" class="btn">View Haulers →</a>"""
    return notify_admin(
        f"[JHE Haul] New Hauler: {user_name}",
        _html("New Hauler Signed Up", "A new hauler just created an account.",
              "🚛 New Hauler", body),
        'admin_new_hauler'
    )


def notify_admin_new_job(job_id, customer_name, pickup_zip, description):
    body = f"""
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Customer:</strong> {customer_name}</p>
      <p><strong>Pickup ZIP:</strong> {pickup_zip}</p>
      <p><strong>Description:</strong><br>{description[:300]}{'…' if len(description) > 300 else ''}</p>
    </div>
    <a href="{_APP_URL}/admin" class="btn">View Admin Dashboard →</a>"""
    return notify_admin(
        f"[JHE Haul] New Job #{job_id} Posted",
        _html("New Job Posted", f"Job #{job_id} is ready for hauler bids.",
              "📦 New Job", body),
        'admin_new_job'
    )


def notify_admin_new_bid(job_id, hauler_name, quote_amount):
    body = f"""
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Hauler:</strong> {hauler_name}</p>
      <p><strong>Quote:</strong> <span class="pill pill-blue">${quote_amount:.2f}</span></p>
    </div>
    <a href="{_APP_URL}/admin" class="btn">View in Admin →</a>"""
    return notify_admin(
        f"[JHE Haul] New Bid on Job #{job_id} — ${quote_amount:.2f}",
        _html("New Bid Submitted", f"{hauler_name} submitted a bid on Job #{job_id}.",
              "🏷 New Bid", body),
        'admin_new_bid'
    )


def notify_admin_bid_accepted(job_id, customer_name, hauler_name, quote_amount):
    body = f"""
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Customer:</strong> {customer_name}</p>
      <p><strong>Hauler:</strong> {hauler_name}</p>
      <p><strong>Accepted Quote:</strong> <span class="pill pill-orange">${quote_amount:.2f}</span></p>
    </div>
    <p>Deposit payment from customer is next. You'll receive another alert once paid.</p>
    <a href="{_APP_URL}/admin" class="btn">View in Admin →</a>"""
    return notify_admin(
        f"[JHE Haul] Bid Accepted — Job #{job_id} — ${quote_amount:.2f}",
        _html("Bid Accepted!", f"Customer accepted a bid on Job #{job_id}.",
              "🤝 Bid Accepted", body),
        'admin_bid_accepted'
    )


def notify_admin_deposit_paid(job_id, customer_name, hauler_name, quote_amount):
    try:
        q = float(quote_amount or 0)
    except Exception:
        q = 0
    body = f"""
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Customer:</strong> {customer_name}</p>
      <p><strong>Hauler:</strong> {hauler_name or '—'}</p>
      <p><strong>Quote:</strong> <span class="pill pill-green">${q:.2f}</span></p>
    </div>
    <p>The deposit has been collected. The hauler now has access to the pickup address.</p>
    <a href="{_APP_URL}/admin" class="btn">View in Admin →</a>"""
    return notify_admin(
        f"[JHE Haul] 💳 Deposit Paid — Job #{job_id}",
        _html("Deposit Paid!", f"Customer paid the deposit for Job #{job_id}.",
              "💳 Deposit Paid", body),
        'admin_deposit_paid'
    )


def notify_admin_job_completed(job_id, customer_name, hauler_name, quote_amount):
    try:
        q = float(quote_amount or 0)
    except Exception:
        q = 0
    body = f"""
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Customer:</strong> {customer_name}</p>
      <p><strong>Hauler:</strong> {hauler_name or '—'}</p>
      <p><strong>Quote:</strong> <span class="pill pill-green">${q:.2f}</span></p>
    </div>
    <a href="{_APP_URL}/admin" class="btn">View in Admin →</a>"""
    return notify_admin(
        f"[JHE Haul] ✅ Job #{job_id} Completed",
        _html("Job Completed!", f"Job #{job_id} has been marked as complete.",
              "✅ Completed", body),
        'admin_job_completed'
    )


def notify_admin_job_cancelled(job_id, customer_name):
    body = f"""
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Customer:</strong> {customer_name}</p>
      <p><strong>Status:</strong> <span class="pill pill-red">Cancelled</span></p>
    </div>
    <a href="{_APP_URL}/admin" class="btn">View in Admin →</a>"""
    return notify_admin(
        f"[JHE Haul] ❌ Job #{job_id} Cancelled",
        _html("Job Cancelled", f"Job #{job_id} was cancelled by the customer.",
              "❌ Cancelled", body),
        'admin_job_cancelled'
    )


def notify_admin_user_deleted(user_name, user_email, user_type):
    body = f"""
    <div class="info-box">
      <p><strong>Name:</strong> {user_name}</p>
      <p><strong>Email:</strong> {user_email}</p>
      <p><strong>Role:</strong> {user_type or 'Unknown'}</p>
    </div>
    <p>Their account and all associated data has been removed from the database.</p>
    <a href="{_APP_URL}/admin" class="btn">View Admin →</a>"""
    return notify_admin(
        f"[JHE Haul] Account Deleted: {user_name} ({user_type})",
        _html("User Account Deleted", f"{user_name} deleted their account.",
              "🗑 Account Deleted", body),
        'admin_user_deleted'
    )


# ── CUSTOMER NOTIFICATIONS ─────────────────────────────────────────────────────

def notify_customer_new_bid(customer_email, job_id, hauler_name, quote_amount):
    body = f"""
    <p><strong>{hauler_name}</strong> submitted a bid of
       <span class="pill pill-blue">${quote_amount:.2f}</span>
       on your hauling job.</p>
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Hauler:</strong> {hauler_name}</p>
      <p><strong>Quote:</strong> ${quote_amount:.2f}</p>
    </div>
    <p>Log in to review all bids and accept the best one!</p>
    <a href="{_APP_URL}/customer/request/{job_id}" class="btn">Review Bids →</a>"""
    return send_email(
        customer_email,
        f"New Bid on Your Job #{job_id} — ${quote_amount:.2f}",
        _html("You Have a New Bid!", "A hauler has submitted a quote on your job.",
              "🏷 New Bid", body),
        'customer_new_bid'
    )


def notify_customer_bid_accepted_confirm(customer_email, job_id, hauler_name, quote_amount):
    body = f"""
    <p>You accepted the bid from <strong>{hauler_name}</strong> for
       <span class="pill pill-orange">${quote_amount:.2f}</span>.</p>
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Hauler:</strong> {hauler_name}</p>
      <p><strong>Accepted Quote:</strong> ${quote_amount:.2f}</p>
    </div>
    <p><strong>Next step:</strong> Pay the booking deposit to confirm your job and
       release the pickup address to your hauler.</p>
    <a href="{_APP_URL}/customer/request/{job_id}" class="btn">Pay Deposit Now →</a>"""
    return send_email(
        customer_email,
        f"Bid Accepted — Now Pay Your Deposit (Job #{job_id})",
        _html("Bid Accepted!", "You've chosen your hauler — pay the deposit to confirm.",
              "🤝 Bid Accepted", body),
        'customer_bid_accepted_confirm'
    )


def notify_customer_job_completed(customer_email, job_id):
    body = f"""
    <p>Your hauling job has been marked as <strong>complete</strong>. Great work getting it done!</p>
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Status:</strong> <span class="pill pill-green">Completed</span></p>
    </div>
    <p>Please take a moment to leave a review for your hauler — it helps them get more work!</p>
    <a href="{_APP_URL}/customer/request/{job_id}" class="btn">Leave a Review →</a>"""
    return send_email(
        customer_email,
        f"Job #{job_id} Complete — Please Leave a Review!",
        _html("Job Complete! 🎉", "Your hauling job has been completed successfully.",
              "✅ Completed", body),
        'customer_job_completed'
    )


# ── HAULER NOTIFICATIONS ───────────────────────────────────────────────────────

def notify_hauler_new_job_nearby(hauler_email, job_id, job_description, distance_miles):
    body = f"""
    <p>A customer just posted a hauling job <strong>{distance_miles:.0f} miles</strong> from you.</p>
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Distance:</strong> ~{distance_miles:.0f} miles away</p>
      <p><strong>Description:</strong><br>{job_description[:200]}{'…' if len(job_description) > 200 else ''}</p>
    </div>
    <p>Log in quickly — first haulers to bid often win the job!</p>
    <a href="{_APP_URL}/hauler/jobs" class="btn">View &amp; Bid on This Job →</a>"""
    return send_email(
        hauler_email,
        f"New Job #{job_id} Near You — {distance_miles:.0f} miles away",
        _html("New Job In Your Area!", "A hauling job was just posted near you.",
              "📍 Job Nearby", body),
        'hauler_new_job_nearby'
    )


def notify_hauler_bid_accepted(hauler_email, job_id, quote_amount):
    body = f"""
    <p>The customer accepted your bid of
       <span class="pill pill-green">${quote_amount:.2f}</span>
       on Job #{job_id}!</p>
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Your Quote:</strong> ${quote_amount:.2f}</p>
      <p><strong>Status:</strong> Waiting for customer deposit</p>
    </div>
    <p>The customer needs to pay a booking deposit. Once they do, you'll automatically receive the full pickup address and directions.</p>
    <a href="{_APP_URL}/hauler/jobs" class="btn">View Your Jobs →</a>"""
    return send_email(
        hauler_email,
        f"🎉 Your Bid Was Accepted — Job #{job_id}",
        _html("Your Bid Was Accepted!", "Congratulations — the customer chose you!",
              "🤝 Bid Accepted", body),
        'hauler_bid_accepted'
    )


def notify_hauler_bid_rejected(hauler_email, job_id):
    body = f"""
    <p>The customer on Job #{job_id} chose a different hauler.</p>
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Status:</strong> <span class="pill pill-red">Not Selected</span></p>
    </div>
    <p>Don't worry — new jobs are posted regularly. Keep bidding!</p>
    <a href="{_APP_URL}/hauler/jobs" class="btn">Browse Open Jobs →</a>"""
    return send_email(
        hauler_email,
        f"Job #{job_id} — Another Hauler Was Selected",
        _html("Another Hauler Was Chosen", "Your bid on this job wasn't selected this time.",
              "📋 Not Selected", body),
        'hauler_bid_rejected'
    )


def notify_hauler_deposit_paid(hauler_email, job_id, pickup_address, pickup_zip):
    import urllib.parse
    full_address = f"{pickup_address}, {pickup_zip}"
    maps_url = f"https://www.google.com/maps/dir/?api=1&destination={urllib.parse.quote(full_address)}"
    body = f"""
    <p>The customer paid their deposit for Job #{job_id}. You now have access to the full pickup address.</p>
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Pickup Address:</strong><br><strong>{full_address}</strong></p>
    </div>
    <a href="{maps_url}" class="btn" style="background:#27ae60;">Get Directions →</a>
    <a href="{_APP_URL}/hauler/jobs" class="btn" style="margin-left:10px;">View Job Details →</a>"""
    return send_email(
        hauler_email,
        f"💳 Deposit Paid — Job #{job_id} Ready to Go!",
        _html("Deposit Received — You're On!", "The pickup address is now unlocked for you.",
              "💳 Deposit Paid", body),
        'hauler_deposit_paid'
    )


def notify_hauler_job_cancelled(hauler_email, job_id, customer_name):
    body = f"""
    <p>Unfortunately, Job #{job_id} has been cancelled by the customer.</p>
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Customer:</strong> {customer_name}</p>
      <p><strong>Status:</strong> <span class="pill pill-red">Cancelled</span></p>
    </div>
    <p>Plenty of new jobs are posted daily. Check the board for other opportunities!</p>
    <a href="{_APP_URL}/hauler/jobs" class="btn">Browse Open Jobs →</a>"""
    return send_email(
        hauler_email,
        f"Job #{job_id} Has Been Cancelled",
        _html("Job Cancelled", f"A job you bid on has been cancelled.",
              "❌ Job Cancelled", body),
        'hauler_job_cancelled'
    )


def notify_buyer_offer_accepted(buyer_email, listing_title, listing_id, offer_amount):
    """Email the buyer when the seller accepts their offer."""
    import html as _html_mod
    raw_title = listing_title or f"Listing #{listing_id}"
    safe_title = _html_mod.escape(raw_title)
    # Subject uses a plain-text version (no HTML tags, newlines stripped)
    subject_title = raw_title.replace('\n', ' ').replace('\r', ' ')
    listing_url = f"{_APP_URL}/listing/{listing_id}"
    body = f"""
    <p>Great news — the seller has <strong>accepted</strong> your offer of
       <span class="pill pill-green">${offer_amount:,.2f}</span>
       on <strong>{safe_title}</strong>!</p>
    <div class="info-box">
      <p><strong>Listing:</strong> {safe_title}</p>
      <p><strong>Your Offer:</strong> ${offer_amount:,.2f}</p>
      <p><strong>Status:</strong> <span class="pill pill-green">Accepted ✅</span></p>
    </div>
    <p>Log in to message the seller and arrange pickup or delivery.</p>
    <a href="{listing_url}" class="btn">View Listing &amp; Message Seller →</a>"""
    return send_email(
        buyer_email,
        f"Your offer was accepted — \"{subject_title}\"",
        _html(
            "Your Offer Was Accepted! 🎉",
            "The seller said yes — reach out to arrange the details.",
            "✅ Offer Accepted",
            body,
        ),
        'buyer_offer_accepted',
    )


def notify_buyer_offer_declined(buyer_email, listing_title, listing_id, offer_amount):
    """Email the buyer when the seller declines their offer."""
    import html as _html_mod
    raw_title = listing_title or f"Listing #{listing_id}"
    safe_title = _html_mod.escape(raw_title)
    subject_title = raw_title.replace('\n', ' ').replace('\r', ' ')
    listing_url = f"{_APP_URL}/listing/{listing_id}"
    body = f"""
    <p>Unfortunately, the seller has <strong>declined</strong> your offer of
       <span class="pill pill-red">${offer_amount:,.2f}</span>
       on <strong>{safe_title}</strong>.</p>
    <div class="info-box">
      <p><strong>Listing:</strong> {safe_title}</p>
      <p><strong>Your Offer:</strong> ${offer_amount:,.2f}</p>
      <p><strong>Status:</strong> <span class="pill pill-red">Declined</span></p>
    </div>
    <p>You can browse the listing to see if the seller's asking price works for you, or
       explore other items in the marketplace.</p>
    <a href="{listing_url}" class="btn">View Listing →</a>"""
    return send_email(
        buyer_email,
        f"Your offer was declined — \"{subject_title}\"",
        _html(
            "Your Offer Was Declined",
            "The seller passed on this one — browse more great listings.",
            "❌ Offer Declined",
            body,
        ),
        'buyer_offer_declined',
    )


def notify_buyer_offer_countered(buyer_email, listing_title, listing_id,
                                  original_amount, counter_amount):
    """Email the buyer when the seller counters their offer."""
    import html as _html_mod
    raw_title = listing_title or f"Listing #{listing_id}"
    safe_title = _html_mod.escape(raw_title)
    subject_title = raw_title.replace('\n', ' ').replace('\r', ' ')
    listing_url = f"{_APP_URL}/listing/{listing_id}"
    body = f"""
    <p>The seller has made a <strong>counteroffer</strong> on <strong>{safe_title}</strong>.</p>
    <div class="info-box">
      <p><strong>Listing:</strong> {safe_title}</p>
      <p><strong>Your Original Offer:</strong> ${original_amount:,.2f}</p>
      <p><strong>Seller's Counter:</strong> <span class="pill pill-orange">${counter_amount:,.2f}</span></p>
      <p><strong>Status:</strong> <span class="pill pill-orange">Counteroffer</span></p>
    </div>
    <p>Log in to accept or decline the counteroffer.</p>
    <a href="{listing_url}" class="btn">Respond to Counteroffer →</a>"""
    return send_email(
        buyer_email,
        f"Counteroffer received — \"{subject_title}\" (${counter_amount:,.2f})",
        _html(
            "The Seller Made a Counteroffer",
            "Log in to accept or decline their counteroffer.",
            "💬 Counteroffer",
            body,
        ),
        'buyer_offer_countered',
    )


def notify_buyer_listing_reserved(buyer_email, listing_title, listing_id):
    """Notify a buyer (saved/watching) that a listing has been marked Reserved."""
    safe_title = listing_title or f"Listing #{listing_id}"
    listing_url = f"{_APP_URL}/listing/{listing_id}"
    body = f"""
    <p>An item you were watching has been marked as <strong>Reserved</strong>.</p>
    <div class="info-box">
      <p><strong>Listing:</strong> {safe_title}</p>
      <p><strong>Status:</strong> <span class="pill pill-orange">Reserved</span></p>
    </div>
    <p>This means the seller is in talks with another buyer, but the item may still become
       available if the deal falls through. You can still view the listing and message the
       seller to express your interest.</p>
    <a href="{listing_url}" class="btn">View Listing →</a>"""
    return send_email(
        buyer_email,
        f"Item you're watching is now Reserved — \"{safe_title}\"",
        _html(
            "This Item Is Now Reserved",
            "It may still become available — stay in touch with the seller.",
            "📌 Reserved",
            body,
        ),
        'buyer_listing_reserved',
    )


def notify_buyer_offer_expired_listing(buyer_email, listing_title, listing_id, offer_amount):
    """Email the buyer when their offer expired because the listing itself auto-expired.

    This is intentionally distinct from the time-based offer-expiry notification
    (offer ran out its own timer). The messaging here explains that the *listing*
    expired underneath the offer, not that the offer window closed.
    """
    import html as _html_mod
    raw_title = listing_title or f"Listing #{listing_id}"
    safe_title = _html_mod.escape(raw_title)
    subject_title = raw_title.replace('\n', ' ').replace('\r', ' ')
    marketplace_url = f"{_APP_URL}/marketplace"
    body = f"""
    <p>Your offer of <span class="pill pill-orange">${offer_amount:,.2f}</span> on
       <strong>{safe_title}</strong> has expired because the listing itself
       has expired and is no longer available in the marketplace.</p>
    <div class="info-box">
      <p><strong>Listing:</strong> {safe_title}</p>
      <p><strong>Your Offer:</strong> ${offer_amount:,.2f}</p>
      <p><strong>Reason:</strong> The listing expired before the seller could respond.</p>
      <p><strong>Status:</strong> <span class="pill pill-red">Offer Expired</span></p>
    </div>
    <p>Browse the marketplace to find other great items — new listings are added every day!</p>
    <a href="{marketplace_url}" class="btn">Browse the Marketplace →</a>"""
    return send_email(
        buyer_email,
        f"Your offer expired — \"{subject_title}\" is no longer available",
        _html(
            "Your Offer Has Expired",
            "The listing expired before the seller could respond to your offer.",
            "⏰ Offer Expired",
            body,
        ),
        'buyer_offer_expired_listing',
    )


def notify_seller_listing_expired(seller_email, listing_id, title):
    """Notify the seller that their listing has auto-expired."""
    safe_title = title or f"Listing #{listing_id}"
    body = f"""
    <p>Your listing <strong>{safe_title}</strong> has expired and is no longer visible
       in the marketplace.</p>
    <div class="info-box">
      <p><strong>Listing:</strong> {safe_title}</p>
      <p><strong>Status:</strong> <span class="pill pill-red">Expired</span></p>
    </div>
    <p>You can renew the listing from your <strong>My Listings</strong> page — it takes
       just a few seconds to make it active again.</p>
    <a href="{_APP_URL}/my-listings" class="btn">Go to My Listings →</a>"""
    return send_email(
        seller_email,
        f"Your listing \"{safe_title}\" has expired",
        _html(
            "Your Listing Has Expired",
            "Renew it from My Listings to keep selling.",
            "⏰ Listing Expired",
            body,
        ),
        'seller_listing_expired',
    )


def notify_seller_listing_expiring_soon(seller_email, listing_id, title, expires_at):
    """Notify the seller that their listing expires in ~3 days so they can extend it."""
    import datetime as _dt
    safe_title = title or f"Listing #{listing_id}"
    if isinstance(expires_at, _dt.datetime):
        expires_str = expires_at.strftime("%B %-d, %Y")
    else:
        expires_str = str(expires_at)
    edit_url = f"{_APP_URL}/listing/{listing_id}/edit"
    body = f"""
    <p>Your listing <strong>{safe_title}</strong> is set to expire on
       <strong>{expires_str}</strong>.</p>
    <div class="info-box">
      <p><strong>Listing:</strong> {safe_title}</p>
      <p><strong>Expires:</strong> {expires_str}</p>
      <p><strong>Status:</strong> <span class="pill pill-orange">Expiring Soon</span></p>
    </div>
    <p>Head to your listing's edit page to push the expiry date out — it only takes a
       few seconds.</p>
    <a href="{edit_url}" class="btn">Extend My Listing →</a>"""
    return send_email(
        seller_email,
        f"Your listing \"{safe_title}\" expires on {expires_str} — extend it now",
        _html(
            "Your Listing Is Expiring Soon",
            "Extend the expiry date so buyers can still find your item.",
            "⏳ Expiring Soon",
            body,
        ),
        'seller_listing_expiring_soon',
    )


def notify_seller_draft_expiring(seller_email, listing_id):
    """Notify the seller that their unfinished draft will be auto-deleted in ~24 hours."""
    # Link to step 2 of the wizard so the seller can fill in details and publish,
    # not the edit page (which only saves changes, never transitions draft → active).
    finish_url = f"{_APP_URL}/listing/{listing_id}/step/2"
    body = f"""
    <p>You started listing an item but never finished it. Your draft will be
       <strong>automatically deleted in approximately 24 hours</strong> if left incomplete.</p>
    <div class="info-box">
      <p><strong>Draft ID:</strong> #{listing_id}</p>
      <p><strong>Status:</strong> <span class="pill pill-orange">Incomplete Draft</span></p>
      <p><strong>Action needed:</strong> Complete the listing wizard to publish your item, or your photos will be lost.</p>
    </div>
    <p>It only takes a minute — click below to pick up where you left off and publish your listing.</p>
    <a href="{finish_url}" class="btn">Finish &amp; Publish My Listing →</a>"""
    return send_email(
        seller_email,
        "Your draft listing will be deleted in 24 hours — finish it now",
        _html(
            "Your Draft Is About to Be Deleted",
            "Complete it now before your photos are removed.",
            "⚠️ Draft Expiring",
            body,
        ),
        'seller_draft_expiring',
    )


def notify_customer_pending_bids_reminder(customer_email, job_id, bid_count):
    """24-hour inactivity reminder — bids waiting for review."""
    body = f"""
    <p>You have <strong>{bid_count} bid{'s' if bid_count != 1 else ''}</strong> waiting on your hauling job that haven't been reviewed yet.</p>
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Bids Received:</strong> {bid_count}</p>
      <p><strong>Action needed:</strong> Review and accept a bid to move forward.</p>
    </div>
    <p style="color:#b45309;font-weight:600;">⚠️ If no action is taken, your job will be removed from active bidding in 48 hours.</p>
    <a href="{_APP_URL}/customer/request/{job_id}" class="btn">Review Bids Now →</a>"""
    return send_email(
        customer_email,
        f"⏰ Reminder: {bid_count} bid{'s' if bid_count != 1 else ''} waiting on Job #{job_id}",
        _html("You Have Pending Bids!", "Haulers are waiting — review your bids now.",
              "⏰ Action Needed", body),
        'customer_bid_reminder_24h'
    )


def notify_customer_job_expiring_soon(customer_email, job_id):
    """48-hour inactivity reminder — job expiring in 24 hours."""
    body = f"""
    <p>Your hauling job is about to expire because no bid has been accepted yet.</p>
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Status:</strong> <span class="pill pill-orange">Expiring Soon</span></p>
    </div>
    <p style="color:#b91c1c;font-weight:600;">🚨 This job will be automatically closed in approximately 24 hours if no bid is accepted.</p>
    <p>Accept a bid now to lock in your hauler and keep your job active. You can always reactivate an expired job, but acting now is faster.</p>
    <a href="{_APP_URL}/customer/request/{job_id}" class="btn" style="background:#dc2626;">Accept a Bid Now →</a>"""
    return send_email(
        customer_email,
        f"🚨 Job #{job_id} Will Expire Soon — Action Required",
        _html("Your Job Is About to Expire!", "Accept a bid now to keep your job active.",
              "🚨 Expiring Soon", body),
        'customer_bid_reminder_48h'
    )


def notify_admin_job_expired(job_id, customer_name, bid_count):
    """Admin notification when a job is auto-expired."""
    body = f"""
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Customer:</strong> {customer_name}</p>
      <p><strong>Bids that were waiting:</strong> {bid_count}</p>
      <p><strong>Status:</strong> <span class="pill pill-red">Expired</span></p>
    </div>
    <p>This job was automatically expired after 72 hours of inactivity (no bid accepted). The customer can reactivate the job from their dashboard.</p>
    <a href="{_APP_URL}/admin" class="btn">View in Admin →</a>"""
    return notify_admin(
        f"[JHE Haul] Job #{job_id} Auto-Expired ({bid_count} bids)",
        _html("Job Auto-Expired", f"Job #{job_id} expired after 72 hours of bid inactivity.",
              "⏰ Job Expired", body),
        'admin_job_expired'
    )


def notify_admin_listing_expired_no_email(listing_id, listing_title, seller_id, seller_name, seller_phone):
    """Admin alert: a listing expired but the seller has no email address, so they received no notification."""
    phone_html = (
        f'<p><strong>Phone:</strong> {seller_phone}</p>' if seller_phone
        else '<p><strong>Phone:</strong> <em>None on file</em></p>'
    )
    body = f"""
    <div class="info-box">
      <p><strong>Listing #:</strong> {listing_id}</p>
      <p><strong>Title:</strong> {listing_title}</p>
      <p><strong>Seller ID:</strong> {seller_id}</p>
      <p><strong>Seller Name:</strong> {seller_name or 'Unknown'}</p>
      {phone_html}
      <p><strong>Status:</strong> <span class="pill pill-red">Expired</span></p>
    </div>
    <p>This listing expired automatically, but the seller has <strong>no email address on file</strong>
       and did not receive an expiry notification. Please follow up with the seller manually if possible.</p>
    <a href="{_APP_URL}/admin/listings/{listing_id}" class="btn">View Listing in Admin →</a>"""
    return notify_admin(
        f"[JHE Haul] ACTION NEEDED — Listing #{listing_id} expired, seller has no email",
        _html(
            "Listing Expired — Seller Not Notified",
            f"Listing #{listing_id} auto-expired but no expiry email could be sent (seller has no email address).",
            "⚠️ No-Email Expiry",
            body,
        ),
        'admin_listing_expired_no_email',
    )


def notify_hauler_new_review(hauler_email, job_id, customer_name, rating, comment):
    stars = '★' * rating + '☆' * (5 - rating)
    comment_html = (
        f'<p style="font-style:italic;color:#4a5568;border-left:3px solid #e2e8f0;'
        f'padding-left:12px;margin:10px 0;">"{comment}"</p>'
        if comment else ""
    )
    body = f"""
    <div class="info-box">
      <p><strong>Job #:</strong> {job_id}</p>
      <p><strong>Customer:</strong> {customer_name}</p>
      <p><strong>Rating:</strong> <span style="color:#f6c90e;font-size:1.2em;">{stars}</span> ({rating}/5)</p>
    </div>
    {comment_html}
    <a href="{_APP_URL}/hauler/earnings" class="btn">View Your Reviews →</a>"""
    return send_email(
        hauler_email,
        f"New Review: {stars} for Job #{job_id}",
        _html("You Got a New Review!", f"{customer_name} left you a {rating}-star review.",
              "⭐ New Review", body),
        'hauler_new_review'
    )


# ── PORTAL NOTIFICATIONS ───────────────────────────────────────────────────────

def notify_customer_quote_received(customer_email, job_id, service_type, price, deposit_amount,
                                    admin_notes=None, estimated_completion=None):
    service_label = service_type or 'Service'
    notes_html = (f'<p><strong>Note from JHE Haul:</strong><br><em>{admin_notes}</em></p>'
                  if admin_notes else '')
    est_html = (f'<p><strong>Estimated Completion:</strong> {estimated_completion}</p>'
                if estimated_completion else '')
    body = f"""
    <p>Great news — we've reviewed your request and have a quote ready for you.</p>
    <div class="info-box">
      <p><strong>Request #:</strong> {job_id}</p>
      <p><strong>Service:</strong> {service_label}</p>
      <p><strong>Total Price:</strong> <span class="pill pill-blue">${price:.2f}</span></p>
      <p><strong>Deposit Due Now:</strong> <span class="pill pill-orange">${deposit_amount:.2f}</span></p>
      {est_html}
    </div>
    {notes_html}
    <p>Log in to review the full quote, ask any questions, and confirm your booking.</p>
    <a href="{_APP_URL}/customer/request/{job_id}" class="btn">Review Your Quote →</a>"""
    return send_email(
        customer_email,
        f"Your Quote Is Ready — {service_label} (Request #{job_id})",
        _html("Your Quote Is Ready!", "Log in to review your quote and confirm your booking.",
              "🏷 Quote Ready", body),
        'customer_quote_received'
    )


def notify_customer_appointment_confirmed(customer_email, job_id, service_type,
                                           scheduled_date, scheduled_time):
    """Email the customer their confirmed pickup appointment date/time."""
    service_label = service_type or 'Service'
    when = f"{scheduled_date}" + (f" at {scheduled_time}" if scheduled_time else "")
    body = f"""
    <p>Your appointment is confirmed! We'll see you then.</p>
    <div class="info-box">
      <p><strong>Request #:</strong> {job_id}</p>
      <p><strong>Service:</strong> {service_label}</p>
      <p><strong>Appointment:</strong> <span class="pill pill-green">📅 {when}</span></p>
    </div>
    <p>If you need to make changes, message us anytime through your request page.</p>
    <a href="{_APP_URL}/customer/request/{job_id}" class="btn">View Your Request →</a>"""
    return send_email(
        customer_email,
        f"Appointment Confirmed — {when} ({service_label}, Request #{job_id})",
        _html("Your Appointment Is Confirmed! 📅",
              "Your pickup date and time are locked in.",
              "📅 Appointment Set", body),
        'customer_appointment_confirmed'
    )


def notify_customer_appointment_reminder(customer_email, job_id, service_type,
                                          scheduled_date, scheduled_time):
    """Email the customer a day-before reminder about their pickup appointment."""
    service_label = service_type or 'Service'
    when = f"{scheduled_date}" + (f" at {scheduled_time}" if scheduled_time else "")
    body = f"""
    <p>Just a friendly reminder — your pickup is <strong>tomorrow</strong>!</p>
    <div class="info-box">
      <p><strong>Request #:</strong> {job_id}</p>
      <p><strong>Service:</strong> {service_label}</p>
      <p><strong>Appointment:</strong> <span class="pill pill-green">📅 {when}</span></p>
    </div>
    <p>Please make sure your items are ready and accessible. If you need to make any changes, message us as soon as possible through your request page.</p>
    <a href="{_APP_URL}/customer/request/{job_id}" class="btn">View Your Request →</a>"""
    return send_email(
        customer_email,
        f"Reminder: Your Pickup Is Tomorrow — {when} ({service_label}, Request #{job_id})",
        _html("Your Pickup Is Tomorrow! 🚛",
              "Don't forget — we'll see you tomorrow.",
              "📅 Pickup Reminder", body),
        'customer_appointment_reminder'
    )


def notify_admin_new_request(job_id, customer_name, service_type, pickup_zip, description):
    """Admin email when a new service request is submitted."""
    body = f"""
    <div class="info-box">
      <p><strong>Request #:</strong> {job_id}</p>
      <p><strong>Customer:</strong> {customer_name}</p>
      <p><strong>Service Type:</strong> {service_type or '—'}</p>
      <p><strong>Pickup ZIP:</strong> {pickup_zip or '—'}</p>
      <p><strong>Description:</strong><br>{description[:300]}{'…' if len(description) > 300 else ''}</p>
    </div>
    <a href="{_APP_URL}/admin/request/{job_id}" class="btn">Review Request →</a>"""
    return notify_admin(
        f"[JHE Haul] New Service Request #{job_id} — {customer_name}",
        _html("New Service Request", f"Request #{job_id} needs your review.",
              "📋 New Request", body),
        'admin_new_request'
    )


def notify_customer_quote_withdrawn(customer_email, job_id, service_type, withdrawal_note=None):
    """Email the customer when admin withdraws a pending quote."""
    service_label = service_type or 'Service'
    _note = (withdrawal_note or '').strip()
    note_html = (
        f'<div class="info-box" style="border-left:3px solid #f59e0b;">'
        f'<p><strong>Note from our team:</strong></p>'
        f'<p style="white-space:pre-wrap;">{_note}</p>'
        f'</div>'
        if _note else ''
    )
    body = f"""
    <p>We wanted to let you know that the quote for your recent service request has been withdrawn by our team.</p>
    <div class="info-box">
      <p><strong>Request #:</strong> {job_id}</p>
      <p><strong>Service:</strong> {service_label}</p>
      <p><strong>Status:</strong> <span class="pill pill-orange">Quote Withdrawn</span></p>
    </div>
    {note_html}
    <p>Our team is reviewing your request and will follow up with you shortly. If you have any questions, you can message us directly through your request page.</p>
    <a href="{_APP_URL}/customer/request/{job_id}" class="btn">View Your Request →</a>"""
    return send_email(
        customer_email,
        f"Quote Update — {service_label} (Request #{job_id})",
        _html("Quote Update", "Our team will follow up with you shortly.",
              "📋 Quote Withdrawn", body),
        'customer_quote_withdrawn'
    )


def notify_buyer_offer_timed_out(buyer_email, listing_title, listing_id, offer_amount):
    """Email the buyer when their offer expires because the offer's own response window elapsed.

    This is intentionally distinct from notify_buyer_offer_expired (listing sold/removed)
    and notify_buyer_offer_expired_listing (listing itself auto-expired). Here the listing
    may still be active — the seller simply did not respond before the offer deadline.
    """
    import html as _html_mod
    raw_title = listing_title or f"Listing #{listing_id}"
    safe_title = _html_mod.escape(raw_title)
    subject_title = raw_title.replace('\n', ' ').replace('\r', ' ')
    listing_url = f"{_APP_URL}/listing/{listing_id}"
    body = f"""
    <p>Your offer of <span class="pill pill-orange">${offer_amount:,.2f}</span> on
       <strong>{safe_title}</strong> has expired because the seller didn't respond
       within the offer window.</p>
    <div class="info-box">
      <p><strong>Listing:</strong> {safe_title}</p>
      <p><strong>Your Offer:</strong> ${offer_amount:,.2f}</p>
      <p><strong>Status:</strong> <span class="pill pill-red">Offer Timed Out</span></p>
    </div>
    <p>The listing may still be available. You can visit the listing to make a new offer
       or contact the seller directly.</p>
    <a href="{listing_url}" class="btn">View Listing →</a>"""
    return send_email(
        buyer_email,
        f"Your offer timed out — \"{subject_title}\"",
        _html(
            "Your Offer Window Has Closed",
            "The seller didn't respond in time — the listing may still be available.",
            "⏰ Offer Timed Out",
            body,
        ),
        'buyer_offer_timed_out',
    )


def notify_buyer_listing_pending(buyer_email, listing_title, listing_id, offer_amount):
    """Email the buyer when a seller marks their listing as Pending Sale.

    The buyer's offer is not expired — it stays on hold — so the message
    conveys uncertainty rather than finality.
    """
    import html as _html_mod
    raw_title = listing_title or f"Listing #{listing_id}"
    safe_title = _html_mod.escape(raw_title)
    subject_title = raw_title.replace('\n', ' ').replace('\r', ' ')
    listing_url = f"{_APP_URL}/listing/{listing_id}"
    body = f"""
    <p>We wanted to give you a quick update on a listing you made an offer on:</p>
    <div class="info-box">
      <p><strong>Listing:</strong> {safe_title}</p>
      <p><strong>Your Offer:</strong> ${offer_amount:,.2f}</p>
      <p><strong>Status:</strong> <span class="pill pill-orange">Pending Sale</span></p>
    </div>
    <p>The seller is currently completing a sale with another buyer. Your offer is
       <strong>on hold</strong> — no action is needed from you right now.</p>
    <p>If the deal falls through, the seller may reactivate the listing and your offer
       could still be considered. We'll notify you if that happens.</p>
    <a href="{listing_url}" class="btn">View Listing →</a>"""
    return send_email(
        buyer_email,
        f"Update on your offer for \"{subject_title}\"",
        _html(
            "Listing Is Pending Sale",
            "The seller is completing a sale — your offer is on hold.",
            "⏳ Pending Sale",
            body,
        ),
        'buyer_listing_pending',
    )


def notify_buyer_offer_expired(buyer_email, listing_title, listing_id, offer_amount):
    """Email the buyer when their pending offer expires because the listing was sold or removed."""
    import html as _html_mod
    raw_title = listing_title or f"Listing #{listing_id}"
    safe_title = _html_mod.escape(raw_title)
    subject_title = raw_title.replace('\n', ' ').replace('\r', ' ')
    browse_url = f"{_APP_URL}/marketplace"
    body = f"""
    <p>Unfortunately, the listing you made an offer on is <strong>no longer available</strong>.</p>
    <div class="info-box">
      <p><strong>Listing:</strong> {safe_title}</p>
      <p><strong>Your Offer:</strong> ${offer_amount:,.2f}</p>
      <p><strong>Status:</strong> <span class="pill pill-red">No Longer Available</span></p>
    </div>
    <p>The listing was sold or removed before your offer could be acted on. Browse the
       marketplace to find similar items — great deals are posted every day!</p>
    <a href="{browse_url}" class="btn">Browse Similar Items →</a>"""
    return send_email(
        buyer_email,
        f"Your offer on \"{subject_title}\" is no longer active",
        _html(
            "This Listing Is No Longer Available",
            "The item was sold or removed — browse for something similar.",
            "🚫 Offer Expired",
            body,
        ),
        'buyer_offer_expired',
    )


def notify_seller_new_message(seller_email, listing_title, listing_id, buyer_name, conversation_id):
    """Email the seller when a buyer sends their first message in a conversation."""
    import html as _html_mod
    raw_title = listing_title or f"Listing #{listing_id}"
    safe_title = _html_mod.escape(raw_title)
    subject_title = raw_title.replace('\n', ' ').replace('\r', ' ')
    safe_buyer = _html_mod.escape(buyer_name or 'A buyer')
    convo_url = f"{_APP_URL}/listing/{listing_id}/message/{conversation_id}"
    body = f"""
    <p><strong>{safe_buyer}</strong> just sent you a message about your listing
       <strong>{safe_title}</strong>.</p>
    <div class="info-box">
      <p><strong>Listing:</strong> {safe_title}</p>
      <p><strong>From:</strong> {safe_buyer}</p>
    </div>
    <p>Log in to read their message and reply — buyers who get fast responses are much
       more likely to complete a purchase!</p>
    <a href="{convo_url}" class="btn">Read &amp; Reply →</a>"""
    return send_email(
        seller_email,
        f"New message about your listing — \"{subject_title}\"",
        _html(
            "You Have a New Message!",
            "A buyer reached out about your listing.",
            "💬 New Message",
            body,
        ),
        'seller_new_message',
    )


def notify_seller_offer_accepted(seller_email, listing_title, listing_id, offer_amount):
    """Email the seller when a buyer accepts their counteroffer."""
    import html as _html_mod
    raw_title = listing_title or f"Listing #{listing_id}"
    safe_title = _html_mod.escape(raw_title)
    subject_title = raw_title.replace('\n', ' ').replace('\r', ' ')
    listing_url = f"{_APP_URL}/listing/{listing_id}"
    body = f"""
    <p>Great news — the buyer has <strong>accepted</strong> your counteroffer of
       <span class="pill pill-green">${offer_amount:,.2f}</span>
       on <strong>{safe_title}</strong>!</p>
    <div class="info-box">
      <p><strong>Listing:</strong> {safe_title}</p>
      <p><strong>Agreed Amount:</strong> ${offer_amount:,.2f}</p>
      <p><strong>Status:</strong> <span class="pill pill-green">Accepted ✅</span></p>
    </div>
    <p>Log in to message the buyer and arrange pickup or delivery.</p>
    <a href="{listing_url}" class="btn">View Listing &amp; Message Buyer →</a>"""
    return send_email(
        seller_email,
        f"Your counteroffer was accepted — \"{subject_title}\"",
        _html(
            "Counteroffer Accepted! 🎉",
            "The buyer said yes — reach out to arrange the details.",
            "✅ Counteroffer Accepted",
            body,
        ),
        'seller_offer_accepted',
    )


def notify_seller_offer_declined(seller_email, listing_title, listing_id, offer_amount):
    """Email the seller when a buyer declines their counteroffer."""
    import html as _html_mod
    raw_title = listing_title or f"Listing #{listing_id}"
    safe_title = _html_mod.escape(raw_title)
    subject_title = raw_title.replace('\n', ' ').replace('\r', ' ')
    listing_url = f"{_APP_URL}/listing/{listing_id}"
    body = f"""
    <p>The buyer has <strong>declined</strong> your counteroffer of
       <span class="pill pill-red">${offer_amount:,.2f}</span>
       on <strong>{safe_title}</strong>.</p>
    <div class="info-box">
      <p><strong>Listing:</strong> {safe_title}</p>
      <p><strong>Your Counter:</strong> ${offer_amount:,.2f}</p>
      <p><strong>Status:</strong> <span class="pill pill-red">Declined</span></p>
    </div>
    <p>Your listing is still active. Other buyers can still make offers.</p>
    <a href="{listing_url}" class="btn">View Listing →</a>"""
    return send_email(
        seller_email,
        f"Your counteroffer was declined — \"{subject_title}\"",
        _html(
            "Counteroffer Declined",
            "The buyer passed — your listing is still active.",
            "❌ Counteroffer Declined",
            body,
        ),
        'seller_offer_declined',
    )


def notify_buyer_delivery_quote_ready(buyer_email, listing_title, dr_id, quote_amount):
    """Email the buyer when admin has set a delivery quote and it's ready to review."""
    import html as _html_mod
    raw_title = listing_title or 'your item'
    safe_title = _html_mod.escape(raw_title)
    subject_title = raw_title.replace('\n', ' ').replace('\r', ' ')
    dr_url = f"{_APP_URL}/delivery/{dr_id}"
    amount_html = (
        f'<span class="pill pill-blue">${quote_amount:,.2f}</span>'
        if quote_amount is not None
        else '<span class="pill pill-orange">See details</span>'
    )
    body = f"""
    <p>Your delivery quote for <strong>{safe_title}</strong> is ready!</p>
    <div class="info-box">
      <p><strong>Item:</strong> {safe_title}</p>
      <p><strong>Delivery Quote:</strong> {amount_html}</p>
      <p><strong>Status:</strong> <span class="pill pill-blue">Quote Ready</span></p>
    </div>
    <p>Log in to review the quote and confirm your delivery.</p>
    <a href="{dr_url}" class="btn">Review Delivery Quote →</a>"""
    return send_email(
        buyer_email,
        f"Your delivery quote is ready — \"{subject_title}\"",
        _html(
            "Your Delivery Quote Is Ready!",
            "Review the quote and confirm your delivery.",
            "🚚 Quote Ready",
            body,
        ),
        'buyer_delivery_quote_ready',
    )


def notify_customer_deposit_confirmed(customer_email, job_id, service_type, estimated_completion=None):
    service_label = service_type or 'Service'
    est_html = (f'<p><strong>Estimated Completion:</strong> {estimated_completion}</p>'
                if estimated_completion else '')
    body = f"""
    <p>Your deposit has been received and your service is now confirmed and scheduled.</p>
    <div class="info-box">
      <p><strong>Request #:</strong> {job_id}</p>
      <p><strong>Service:</strong> {service_label}</p>
      <p><strong>Status:</strong> <span class="pill pill-green">Scheduled</span></p>
      {est_html}
    </div>
    <p>We'll be in touch to coordinate the exact timing. You can message us anytime through your request page.</p>
    <a href="{_APP_URL}/customer/request/{job_id}" class="btn">View Your Request →</a>"""
    return send_email(
        customer_email,
        f"Booking Confirmed — {service_label} (Request #{job_id})",
        _html("You're All Set! 🎉", "Your service is scheduled. We'll be in touch soon.",
              "✅ Confirmed", body),
        'customer_deposit_confirmed'
    )


# ── ADMIN SECURITY EMAILS ─────────────────────────────────────────────────────

def notify_admin_password_reset_request(to_email, reset_link):
    """Send a password-reset link to the admin (primary or recovery email)."""
    body = f"""
    <div class="info-box">
      <p>A password reset was requested for the JHE Haul admin account.</p>
      <p>This link expires in <strong>30 minutes</strong> and is single-use.</p>
      <p>If you did not request this, your account is still secure — ignore this email.</p>
    </div>
    <a href="{reset_link}" class="btn">Reset Admin Password &rarr;</a>
    <p style="margin-top:16px;font-size:0.82rem;color:#718096;">
      If the button does not work, copy and paste this link:<br>
      <code style="word-break:break-all;font-size:0.78rem;">{reset_link}</code>
    </p>"""
    return send_email(
        to_email,
        "[JHE Haul] Admin Password Reset Request",
        _html("Admin Password Reset", "Reset your JHE Haul admin password.",
              "🔐 Password Reset Requested", body),
        'admin_security'
    )


def notify_admin_password_changed(admin_email):
    """Notify admin that their password was successfully changed."""
    body = f"""
    <div class="info-box">
      <p>Your JHE Haul admin password was successfully changed.</p>
      <p>If you did not make this change, secure your admin account immediately and contact your host provider.</p>
    </div>
    <a href="{_APP_URL}/admin" class="btn">Go to Admin Dashboard &rarr;</a>"""
    return send_email(
        admin_email,
        "[JHE Haul] Admin Password Changed",
        _html("Admin Password Changed", "Your admin password was updated.",
              "🔑 Password Changed", body),
        'admin_security'
    )


def notify_admin_recovery_email_verify(recovery_email, verify_link):
    """Send verification link to the new recovery email address."""
    body = f"""
    <div class="info-box">
      <p>This email address is being added as the recovery email for the JHE Haul admin account.</p>
      <p>Click the button below to verify this address. The link expires in <strong>24 hours</strong>.</p>
      <p>If you did not request this, ignore this email — no changes will be made.</p>
    </div>
    <a href="{verify_link}" class="btn">Verify Recovery Email &rarr;</a>
    <p style="margin-top:16px;font-size:0.82rem;color:#718096;">
      If the button does not work, copy and paste:<br>
      <code style="word-break:break-all;font-size:0.78rem;">{verify_link}</code>
    </p>"""
    return send_email(
        recovery_email,
        "[JHE Haul] Verify Your Admin Recovery Email",
        _html("Verify Recovery Email", "Confirm your JHE Haul admin recovery address.",
              "📧 Verify Recovery Email", body),
        'admin_security'
    )


def notify_admin_recovery_email_changed(admin_email, masked_recovery):
    """Security notice to primary admin email when recovery email is changed."""
    body = f"""
    <div class="info-box">
      <p>A new recovery email (<strong>{masked_recovery}</strong>) was added to the JHE Haul admin account.</p>
      <p>The new address must be verified before it becomes active.</p>
      <p>If you did not make this change, secure your admin account immediately.</p>
    </div>
    <a href="{_APP_URL}/admin/security" class="btn">Review Admin Security &rarr;</a>"""
    return send_email(
        admin_email,
        "[JHE Haul] Admin Recovery Email Changed",
        _html("Recovery Email Changed", "A new recovery email address was set.",
              "🔔 Security Notice", body),
        'admin_security'
    )


def notify_admin_recovery_email_verified(admin_email, masked_recovery):
    """Notify admin that their recovery email is now verified and active."""
    body = f"""
    <div class="info-box">
      <p>The recovery email <strong>{masked_recovery}</strong> has been verified and is now active on your JHE Haul admin account.</p>
      <p>You can now use this address to recover your admin account if you forget your password.</p>
    </div>
    <a href="{_APP_URL}/admin/security" class="btn">View Admin Security &rarr;</a>"""
    return send_email(
        admin_email,
        "[JHE Haul] Admin Recovery Email Verified",
        _html("Recovery Email Verified", "Your admin recovery email is now active.",
              "✅ Recovery Email Verified", body),
        'admin_security'
    )


def notify_admin_login_alert(admin_email, ip_address, attempts):
    """Alert admin of repeated failed login attempts and temporary lockout."""
    body = f"""
    <div class="info-box">
      <p><strong>Failed attempts:</strong> {attempts}</p>
      <p><strong>IP address:</strong> {ip_address}</p>
      <p>The admin login has been temporarily locked for 15 minutes.</p>
    </div>
    <a href="{_APP_URL}/admin/forgot-password" class="btn">Reset Password &rarr;</a>"""
    return send_email(
        admin_email,
        "[JHE Haul] \u26a0\ufe0f Repeated Failed Admin Login Attempts",
        _html("Suspicious Login Activity",
              "Multiple failed admin login attempts detected.",
              "🚨 Security Alert", body),
        'admin_security'
    )


def notify_admin_successful_recovery(admin_email):
    """Confirm a completed admin password reset."""
    body = f"""
    <div class="info-box">
      <p>A password reset was successfully completed for the JHE Haul admin account.</p>
      <p>If you did not perform this reset, contact your host provider immediately.</p>
    </div>
    <a href="{_APP_URL}/admin" class="btn">Go to Admin Dashboard &rarr;</a>"""
    return send_email(
        admin_email,
        "[JHE Haul] Admin Password Successfully Reset",
        _html("Password Reset Complete", "Your admin password has been reset.",
              "✅ Password Reset Complete", body),
        'admin_security'
    )
