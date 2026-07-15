from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

from sqlalchemy import UniqueConstraint
from flask_login import UserMixin

db = SQLAlchemy()
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.String, primary_key=True)
    email = db.Column(db.String, unique=True, nullable=True)
    first_name = db.Column(db.String, nullable=True)
    last_name = db.Column(db.String, nullable=True)
    profile_image_url = db.Column(db.String, nullable=True)
    phone = db.Column(db.String, nullable=True)
    user_type = db.Column(db.String, nullable=True)
    home_zip = db.Column(db.String, nullable=True)
    max_travel_miles = db.Column(db.Integer, nullable=True)
    notify_new_jobs = db.Column(db.Boolean, default=True)
    notify_sms = db.Column(db.Boolean, default=False)
    sms_consent = db.Column(db.Boolean, default=False)
    sms_consent_at = db.Column(db.DateTime, nullable=True)
    phone_verified = db.Column(db.Boolean, default=False)
    phone_verify_code = db.Column(db.String(6), nullable=True)
    phone_verify_sent_at = db.Column(db.DateTime, nullable=True)
    truck_type = db.Column(db.String, nullable=True)
    trailer_type = db.Column(db.String, nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    agreed_to_hauler_terms = db.Column(db.Boolean, default=False)
    agreed_to_hauler_terms_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    profile_photo_data = db.Column(db.LargeBinary, nullable=True)
    profile_photo_content_type = db.Column(db.String(80), nullable=True)

    jobs = db.relationship('Job', backref='customer', lazy=True, foreign_keys='Job.customer_id')
    bids = db.relationship('Bid', backref='hauler', lazy=True, foreign_keys='Bid.hauler_id')

    @property
    def phone_formatted(self):
        if not self.phone:
            return ''
        digits = ''.join(c for c in self.phone if c.isdigit())
        if len(digits) == 10:
            return f'({digits[:3]}) {digits[3:6]}-{digits[6:]}'
        return self.phone

    


class Job(db.Model):
    __tablename__ = 'jobs'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    customer_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=True)
    customer_name = db.Column(db.String, nullable=False)
    customer_phone = db.Column(db.String, nullable=True)
    pickup_address = db.Column(db.String, nullable=False)
    pickup_zip = db.Column(db.String, nullable=True)
    job_description = db.Column(db.Text, nullable=False)
    service_type = db.Column(db.String, nullable=True)
    status = db.Column(db.String, default='open')
    # Status values:
    #   Legacy: open, bidding, accepted, deposit_paid, completed, cancelled, expired
    #   Portal:  reviewing, quoted, waiting_for_payment, scheduled, in_progress
    accepted_hauler = db.Column(db.String, nullable=True)
    accepted_hauler_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=True)
    accepted_quote = db.Column(db.Float, nullable=True)
    deposit_paid = db.Column(db.Boolean, default=False)
    preferred_date = db.Column(db.String, nullable=True)
    preferred_time = db.Column(db.String, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    expired_at = db.Column(db.DateTime, nullable=True)
    reminder_24h_sent = db.Column(db.Boolean, default=False)
    reminder_48h_sent = db.Column(db.Boolean, default=False)

    photos = db.relationship('JobPhoto', backref='job', lazy=True, cascade='all, delete-orphan')
    bids = db.relationship('Bid', backref='job', lazy=True, cascade='all, delete-orphan')
    completion_photos = db.relationship('CompletionPhoto', backref='job', lazy=True, cascade='all, delete-orphan')
    reviews = db.relationship('Review', backref='job', lazy=True, cascade='all, delete-orphan')

class JobPhoto(db.Model):
    __tablename__ = 'job_photos'
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('jobs.id'), nullable=False)
    filename = db.Column(db.String, nullable=False)
    storage_url = db.Column(db.String, nullable=True)
    data = db.Column(db.LargeBinary, nullable=True)
    content_type = db.Column(db.String(80), nullable=True)

class Bid(db.Model):
    __tablename__ = 'bids'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    job_id = db.Column(db.Integer, db.ForeignKey('jobs.id'), nullable=False)
    hauler_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=True)
    hauler_name = db.Column(db.String, nullable=False)
    hauler_phone = db.Column(db.String, nullable=True)
    quote_amount = db.Column(db.Float, nullable=False)
    message = db.Column(db.Text, nullable=True)
    status = db.Column(db.String, default='active')

    @property
    def hauler_phone_formatted(self):
        if not self.hauler_phone:
            return ''
        digits = ''.join(c for c in self.hauler_phone if c.isdigit())
        if len(digits) == 10:
            return f'({digits[:3]}) {digits[3:6]}-{digits[6:]}'
        return self.hauler_phone

class CompletionPhoto(db.Model):
    __tablename__ = 'completion_photos'
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('jobs.id'), nullable=False)
    filename = db.Column(db.String, nullable=False)
    storage_url = db.Column(db.String, nullable=True)
    data = db.Column(db.LargeBinary, nullable=True)
    content_type = db.Column(db.String(80), nullable=True)
    photo_type = db.Column(db.String, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

class ZipCode(db.Model):
    __tablename__ = 'zip_codes'
    zip = db.Column(db.String, primary_key=True)
    city = db.Column(db.String, nullable=True)
    state = db.Column(db.String, nullable=True)
    lat = db.Column(db.Float, nullable=False)
    lon = db.Column(db.Float, nullable=False)

class Review(db.Model):
    __tablename__ = 'reviews'
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('jobs.id'), nullable=False)
    hauler_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    customer_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class PageView(db.Model):
    __tablename__ = 'page_views'
    id = db.Column(db.Integer, primary_key=True)
    visitor_id = db.Column(db.String(64), nullable=True)
    path = db.Column(db.String(200), nullable=False)
    user_id = db.Column(db.String, nullable=True)
    device_type = db.Column(db.String(20), nullable=True)
    referrer = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class OAuth(db.Model):
    __tablename__ = 'oauth'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    browser_session_key = db.Column(db.String, nullable=False)
    provider = db.Column(db.String, nullable=False)
    token = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class NotificationLog(db.Model):
    __tablename__ = 'notification_logs'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    event_type = db.Column(db.String(100), nullable=False)
    recipient = db.Column(db.String(200), nullable=True)
    subject = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(20), nullable=False)
    sg_status_code = db.Column(db.Integer, nullable=True)
    sg_message_id = db.Column(db.String(200), nullable=True)
    error_msg = db.Column(db.Text, nullable=True)


class HaulerServiceZip(db.Model):
    """Explicit ZIP codes a hauler has opted in to service.

    Works alongside radius matching — a job is visible to a hauler if
    its pickup ZIP is within their radius OR is in this list.
    """
    __tablename__ = 'hauler_service_zips'
    id = db.Column(db.Integer, primary_key=True)
    hauler_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    zip_code = db.Column(db.String(5), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (UniqueConstraint('hauler_id', 'zip_code'),)


class SmsLog(db.Model):
    """One row per SMS send attempt."""
    __tablename__ = 'sms_logs'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    event_type = db.Column(db.String(100), nullable=False)
    recipient_phone = db.Column(db.String(20), nullable=True)
    message_body = db.Column(db.Text, nullable=True)
    twilio_sid = db.Column(db.String(50), nullable=True)
    status = db.Column(db.String(20), nullable=False)  # sent | failed | no_twilio | skipped
    error_msg = db.Column(db.Text, nullable=True)
    retry_count = db.Column(db.Integer, default=0)


class SmsSettings(db.Model):
    """Single-row admin settings for SMS (get-or-create pattern)."""
    __tablename__ = 'sms_settings'
    id = db.Column(db.Integer, primary_key=True)
    sms_globally_enabled = db.Column(db.Boolean, default=True)
    ev_new_bid = db.Column(db.Boolean, default=True)
    ev_bid_accepted = db.Column(db.Boolean, default=True)
    ev_deposit_paid = db.Column(db.Boolean, default=True)
    ev_job_nearby = db.Column(db.Boolean, default=True)
    ev_job_completed = db.Column(db.Boolean, default=True)
    ev_job_cancelled = db.Column(db.Boolean, default=True)
    ev_bid_rejected = db.Column(db.Boolean, default=False)
    ev_admin_alert = db.Column(db.Boolean, default=False)
    ev_quote_received = db.Column(db.Boolean, default=True)
    email_fallback_to_sms = db.Column(db.Boolean, default=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class Quote(db.Model):
    """Admin-created quote for a service request."""
    __tablename__ = 'quotes'
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('jobs.id'), nullable=False)
    price = db.Column(db.Float, nullable=False)
    deposit_amount = db.Column(db.Float, nullable=False)
    admin_notes = db.Column(db.Text, nullable=True)
    customer_notes = db.Column(db.Text, nullable=True)
    estimated_completion = db.Column(db.String, nullable=True)
    status = db.Column(db.String, default='pending')  # pending / accepted / declined
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    job = db.relationship('Job', backref=db.backref('quotes', lazy=True, cascade='all, delete-orphan'))


class Message(db.Model):
    """Customer ↔ admin message thread per job."""
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('jobs.id'), nullable=False)
    sender_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    read_at = db.Column(db.DateTime, nullable=True)

    job = db.relationship('Job', backref=db.backref('messages', lazy=True, cascade='all, delete-orphan'))
    sender = db.relationship('User', foreign_keys=[sender_id])
