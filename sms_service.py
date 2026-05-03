import os
import logging


def send_sms(to_phone, message):
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_phone = os.environ.get("TWILIO_FROM_NUMBER")

    if not account_sid or not auth_token or not from_phone:
        logging.warning("Twilio not configured (missing env vars), skipping SMS")
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
        logging.info("SMS sent to %s, SID: %s", to_phone, sms.sid)
        return True
    except Exception as e:
        logging.error("Failed to send SMS to %s: %s", to_phone, e)
        return False


def notify_hauler_new_job_sms(phone, job_id, distance_miles):
    message = f"JHE Haul: New job #{job_id} posted {distance_miles:.0f} miles from you! Log in to view and bid."
    return send_sms(phone, message)


def notify_hauler_bid_accepted_sms(phone, job_id):
    message = f"JHE Haul: Your bid on job #{job_id} was accepted! Waiting for customer deposit. You'll get the address once payment is confirmed."
    return send_sms(phone, message)


def notify_hauler_deposit_paid_sms(phone, job_id):
    message = f"JHE Haul: Deposit paid for job #{job_id}! Log in to see the pickup address and get directions."
    return send_sms(phone, message)


def notify_customer_new_bid_sms(phone, job_id, hauler_name, quote_amount):
    message = f"JHE Haul: {hauler_name} bid ${quote_amount:.2f} on your job #{job_id}. Log in to review bids!"
    return send_sms(phone, message)
