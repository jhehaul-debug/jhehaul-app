import os
import logging
import requests

def get_twilio_credentials():
    hostname = os.environ.get('REPLIT_CONNECTORS_HOSTNAME')
    repl_identity = os.environ.get('REPL_IDENTITY')
    web_repl_renewal = os.environ.get('WEB_REPL_RENEWAL')
    
    if repl_identity:
        x_replit_token = 'repl ' + repl_identity
    elif web_repl_renewal:
        x_replit_token = 'depl ' + web_repl_renewal
    else:
        logging.warning("No Replit token found for Twilio")
        return None, None, None
    
    try:
        response = requests.get(
            f'https://{hostname}/api/v2/connection?include_secrets=true&connector_names=twilio',
            headers={
                'Accept': 'application/json',
                'X_REPLIT_TOKEN': x_replit_token
            }
        )
        data = response.json()
        connection = data.get('items', [{}])[0]
        settings = connection.get('settings', {})
        account_sid = settings.get('account_sid')
        auth_token = settings.get('auth_token')
        phone_number = settings.get('phone_number')
        
        if not account_sid or not auth_token or not phone_number:
            logging.warning("Twilio credentials not found")
            return None, None, None
            
        return account_sid, auth_token, phone_number
    except Exception as e:
        logging.error(f"Failed to get Twilio credentials: {e}")
        return None, None, None

def send_sms(to_phone, message):
    account_sid, auth_token, from_phone = get_twilio_credentials()
    
    if not account_sid or not auth_token or not from_phone:
        logging.warning("Twilio not configured, skipping SMS")
        return False
    
    if not to_phone:
        logging.warning("No recipient phone, skipping SMS")
        return False
    
    formatted_phone = to_phone.strip()
    if not formatted_phone.startswith('+'):
        formatted_phone = '+1' + ''.join(filter(str.isdigit, formatted_phone))
    
    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)
        
        sms = client.messages.create(
            body=message,
            from_=from_phone,
            to=formatted_phone
        )
        logging.info(f"SMS sent to {to_phone}, SID: {sms.sid}")
        return True
    except Exception as e:
        logging.error(f"Failed to send SMS: {e}")
        return False

def notify_hauler_new_job_sms(phone, job_id, distance_miles):
    message = f"JHE Haul: New job #{job_id} posted {distance_miles:.0f} miles from you! Log in to view and bid."
    return send_sms(phone, message)

def notify_hauler_bid_accepted_sms(phone, job_id, pickup_address=None, pickup_zip=None):
    import urllib.parse
    if pickup_address and pickup_zip:
        full_address = f"{pickup_address}, {pickup_zip}"
        maps_url = f"https://www.google.com/maps/dir/?api=1&destination={urllib.parse.quote(full_address)}"
        message = f"JHE Haul: Your bid on job #{job_id} was accepted! Pickup: {full_address}. Directions: {maps_url}"
    else:
        message = f"JHE Haul: Your bid on job #{job_id} was accepted! Log in to see details."
    return send_sms(phone, message)

def notify_hauler_deposit_paid_sms(phone, job_id):
    message = f"JHE Haul: Deposit paid for job #{job_id}! Log in to see the pickup address and get directions."
    return send_sms(phone, message)

def notify_customer_new_bid_sms(phone, job_id, hauler_name, quote_amount):
    message = f"JHE Haul: {hauler_name} bid ${quote_amount:.2f} on your job #{job_id}. Log in to review bids!"
    return send_sms(phone, message)
