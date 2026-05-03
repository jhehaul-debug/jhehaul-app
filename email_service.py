import os
import logging
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail


def send_email(to_email, subject, html_content):
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "noreply@jhehaul.com")

    if not api_key:
        logging.warning("SENDGRID_API_KEY not set, skipping email to %s", to_email)
        return False

    if not to_email:
        logging.warning("No recipient email, skipping send")
        return False

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        html_content=html_content
    )

    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        logging.info("Email sent to %s, status: %s", to_email, response.status_code)
        return True
    except Exception as e:
        logging.error("Failed to send email to %s: %s", to_email, e)
        return False


def notify_customer_new_bid(customer_email, job_id, hauler_name, quote_amount):
    subject = f"New Bid on Your Hauling Job #{job_id}"
    html_content = f"""
    <h2>You have a new bid!</h2>
    <p><strong>{hauler_name}</strong> has submitted a bid of <strong>${quote_amount:.2f}</strong> for your hauling job #{job_id}.</p>
    <p>Log in to JHE Haul to review and accept bids.</p>
    <p>Thank you for using JHE Haul!</p>
    """
    return send_email(customer_email, subject, html_content)


def notify_hauler_bid_accepted(hauler_email, job_id, quote_amount):
    subject = f"Your Bid Was Accepted - Job #{job_id}"
    html_content = f"""
    <h2>Congratulations! Your bid was accepted!</h2>
    <p>The customer has accepted your bid of <strong>${quote_amount:.2f}</strong> for Job #{job_id}.</p>
    <p>The customer will now pay the booking deposit. Once payment is confirmed, you'll receive the full pickup address and directions.</p>
    <p>Log in to JHE Haul to view your accepted jobs.</p>
    <p>Thank you for using JHE Haul!</p>
    """
    return send_email(hauler_email, subject, html_content)


def notify_hauler_new_job_nearby(hauler_email, job_id, job_description, distance_miles):
    subject = f"New Hauling Job #{job_id} Near You!"
    html_content = f"""
    <h2>A new job was just posted in your area!</h2>
    <p>A customer posted a hauling job about <strong>{distance_miles:.0f} miles</strong> from your location.</p>
    <p><strong>Job Description:</strong><br>{job_description[:200]}{'...' if len(job_description) > 200 else ''}</p>
    <p>Log in to JHE Haul to view details and submit your bid!</p>
    <p>Thank you for using JHE Haul!</p>
    """
    return send_email(hauler_email, subject, html_content)


def notify_hauler_deposit_paid(hauler_email, job_id, pickup_address, pickup_zip):
    import urllib.parse
    full_address = f"{pickup_address}, {pickup_zip}"
    maps_url = f"https://www.google.com/maps/dir/?api=1&destination={urllib.parse.quote(full_address)}"

    subject = f"Deposit Paid - Job #{job_id} Ready to Go!"
    html_content = f"""
    <h2>Great news! The deposit has been paid!</h2>
    <p>The customer has paid the deposit for Job #{job_id}. You can now view the pickup address and complete the job.</p>
    <p><strong>Pickup Address:</strong><br>{full_address}</p>
    <p><a href="{maps_url}" style="display: inline-block; background: #007bff; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Get Directions</a></p>
    <p>Log in to JHE Haul to view full job details.</p>
    <p>Thank you for using JHE Haul!</p>
    """
    return send_email(hauler_email, subject, html_content)
