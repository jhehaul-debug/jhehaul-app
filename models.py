from datetime import datetime
from app import db
from flask_dance.consumer.storage.sqla import OAuthConsumerMixin
from flask_login import UserMixin
from sqlalchemy import UniqueConstraint

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
    is_admin = db.Column(db.Boolean, default=False)
    agreed_to_hauler_terms = db.Column(db.Boolean, default=False)
    agreed_to_hauler_terms_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

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

class OAuth(OAuthConsumerMixin, db.Model):
    user_id = db.Column(db.String, db.ForeignKey(User.id))
    browser_session_key = db.Column(db.String, nullable=False)
    user = db.relationship(User)
    __table_args__ = (UniqueConstraint(
        'user_id',
        'browser_session_key',
        'provider',
        name='uq_user_browser_session_key_provider',
    ),)

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
    status = db.Column(db.String, default='open')
    accepted_hauler = db.Column(db.String, nullable=True)
    accepted_hauler_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=True)
    accepted_quote = db.Column(db.Float, nullable=True)
    deposit_paid = db.Column(db.Boolean, default=False)
    preferred_date = db.Column(db.String, nullable=True)
    preferred_time = db.Column(db.String, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)

    photos = db.relationship('JobPhoto', backref='job', lazy=True, cascade='all, delete-orphan')
    bids = db.relationship('Bid', backref='job', lazy=True, cascade='all, delete-orphan')
    completion_photos = db.relationship('CompletionPhoto', backref='job', lazy=True, cascade='all, delete-orphan')
    reviews = db.relationship('Review', backref='job', lazy=True, cascade='all, delete-orphan')

class JobPhoto(db.Model):
    __tablename__ = 'job_photos'
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('jobs.id'), nullable=False)
    filename = db.Column(db.String, nullable=False)

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
