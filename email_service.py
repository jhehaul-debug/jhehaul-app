import os
import logging
import requests
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

def get_sendgrid_credentials():
    hostname = os.environ.get('REPLIT_CONNECTORS_HOSTNAME')
    repl_identity = os.environ.get('REPL_IDENTITY')
    web_repl_renewal = os.environ.get('WEB_REPL_RENEWAL')
    
    if repl_identity:
        x_replit_token = 'repl ' + repl_identity
    elif web_repl_renewal:
        x_replit_token = 'depl ' + web_repl_renewal
    else:
        logging.warning("No Replit token found for SendGrid")
        return None, None
    
    try:
        response = requests.get(
            f'https://{hostname}/api/v2/connection?include_secrets=true&connector_names=sendgrid',
            headers={
                'Accept': 'application/json',
                'X_REPLIT_TOKEN': x_replit_token
            }
        )
        data = response.json()
        connection = data.get('items', [{}])[0]
        settings = connection.get('settings', {})
        api_key = settings.get('api_key')
        from_email = settings.get('from_email')
        
        if not api_key or not from_email:
            logging.warning("SendGrid credentials not found")
            return None, None
            
        return api_key, from_email
    except Exception as e:
        logging.error(f"Failed to get SendGrid credentials: {e}")
        return None, None

def send_email(to_email, subject, html_content):
    api_key, from_email = get_sendgrid_credentials()
    
    if not api_key or not from_email:
        logging.warning("SendGrid not configured, skipping email")
        return False
    
    if not to_email:
        logging.warning("No recipient email, skipping")
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
        logging.info(f"Email sent to {to_email}, status: {response.status_code}")
        return True
    except Exception as e:
        logging.error(f"Failed to send email: {e}")
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
    <p>Once the customer pays the deposit, you'll be able to see the pickup address in your dashboard.</p>
    <p>Log in to JHE Haul to view your accepted jobs.</p>
    <p>Thank you for using JHE Haul!</p>
    """
    return send_email(hauler_email, subject, html_content)

def notify_hauler_deposit_paid(hauler_email, job_id, pickup_address, pickup_zip):
    subject = f"Deposit Paid - Job #{job_id} Ready to Go!"
    html_content = f"""
    <h2>Great news! The deposit has been paid!</h2>
    <p>The customer has paid the deposit for Job #{job_id}. You can now view the pickup address and complete the job.</p>
    <p><strong>Pickup Address:</strong><br>{pickup_address}<br>{pickup_zip}</p>
    <p>Log in to JHE Haul to view full job details.</p>
    <p>Thank you for using JHE Haul!</p>
    """
    return send_email(hauler_email, subject, html_content)
