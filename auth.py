import os
import logging
from functools import wraps
from sqlalchemy.exc import IntegrityError

from flask import Blueprint, session, redirect, request, url_for, render_template
from flask_dance.contrib.google import make_google_blueprint
from flask_dance.contrib.github import make_github_blueprint
from flask_dance.consumer import oauth_authorized, oauth_error
from flask_login import LoginManager, login_user, logout_user, current_user

from app import app, db
from models import User

login_manager = LoginManager(app)
login_manager.login_view = 'auth.login'


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, user_id)


# Google OAuth blueprint
google_bp = make_google_blueprint(
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    scope=["openid",
           "https://www.googleapis.com/auth/userinfo.email",
           "https://www.googleapis.com/auth/userinfo.profile"],
)
app.register_blueprint(google_bp, url_prefix="/auth/google")

# GitHub OAuth blueprint
github_bp = make_github_blueprint(
    client_id=os.environ.get("GITHUB_CLIENT_ID"),
    client_secret=os.environ.get("GITHUB_CLIENT_SECRET"),
)
app.register_blueprint(github_bp, url_prefix="/auth/github")

# Auth routes blueprint (login page, logout, error)
auth_bp = Blueprint('auth', __name__, url_prefix='/auth')


def _save_user(provider, user_id, email, first_name, last_name, profile_image_url):
    """Find or create a user. Matches by email first for migration continuity with existing accounts."""
    if email:
        existing = User.query.filter_by(email=email).first()
        if existing:
            if not existing.first_name and first_name:
                existing.first_name = first_name
            if not existing.last_name and last_name:
                existing.last_name = last_name
            existing.profile_image_url = profile_image_url
            db.session.commit()
            return existing

    new_id = f"{provider}_{user_id}"
    existing = db.session.get(User, new_id)
    if existing:
        existing.profile_image_url = profile_image_url
        db.session.commit()
        return existing

    user = User(
        id=new_id,
        email=email,
        first_name=first_name,
        last_name=last_name,
        profile_image_url=profile_image_url,
    )
    try:
        db.session.add(user)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if email:
            user = User.query.filter_by(email=email).first()
    return user


@oauth_authorized.connect_via(google_bp)
def google_logged_in(blueprint, token):
    if not token:
        return redirect(url_for('auth.error'))
    resp = blueprint.session.get("/oauth2/v2/userinfo")
    if not resp.ok:
        return redirect(url_for('auth.error'))
    info = resp.json()
    user = _save_user(
        provider='google',
        user_id=info.get('id'),
        email=info.get('email'),
        first_name=info.get('given_name'),
        last_name=info.get('family_name'),
        profile_image_url=info.get('picture'),
    )
    if user:
        login_user(user)
    next_url = session.pop('next_url', None)
    return redirect(next_url or url_for('home'))


@oauth_authorized.connect_via(github_bp)
def github_logged_in(blueprint, token):
    if not token:
        return redirect(url_for('auth.error'))
    resp = blueprint.session.get("/user")
    if not resp.ok:
        return redirect(url_for('auth.error'))
    info = resp.json()
    email = info.get('email')
    if not email:
        email_resp = blueprint.session.get("/user/emails")
        if email_resp.ok:
            emails = email_resp.json()
            email = next(
                (e['email'] for e in emails if e.get('primary') and e.get('verified')),
                None
            )
    name_parts = (info.get('name') or '').split(' ', 1)
    first_name = name_parts[0] if name_parts else ''
    last_name = name_parts[1] if len(name_parts) > 1 else ''
    user = _save_user(
        provider='github',
        user_id=str(info.get('id')),
        email=email,
        first_name=first_name,
        last_name=last_name,
        profile_image_url=info.get('avatar_url'),
    )
    if user:
        login_user(user)
    next_url = session.pop('next_url', None)
    return redirect(next_url or url_for('home'))


@oauth_error.connect_via(google_bp)
@oauth_error.connect_via(github_bp)
def oauth_error_handler(blueprint, error, error_description=None, error_uri=None):
    logging.error(f"OAuth error from {blueprint.name}: {error} - {error_description}")
    return redirect(url_for('auth.error'))


@auth_bp.route('/login')
def login():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    next_url = request.args.get('next') or request.args.get('next_url')
    if next_url:
        session['next_url'] = next_url
    return render_template('login.html')


@auth_bp.route('/logout')
def logout():
    logout_user()
    session.clear()
    return redirect(url_for('home'))


@auth_bp.route('/error')
def error():
    return render_template('403.html'), 403


app.register_blueprint(auth_bp)


def require_login(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            session['next_url'] = request.url
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function
