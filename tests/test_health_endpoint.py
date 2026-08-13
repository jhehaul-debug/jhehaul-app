"""
Task 77 / 127 validation: /health endpoint catches broken dependencies, DB failures,
and missing required environment variables.

Run with:  python tests/test_health_endpoint.py

Verifies:
- Happy path: 200 {"status": "ok"} when all imports succeed, env vars are set, and DB is up
- Import failure: 503 with an "errors" list when a critical module is missing
- DB failure: 503 with an "errors" list when the DB raises OperationalError
- Missing strictly-required env var (DATABASE_URL): 503 with a descriptive error
- Missing payment-link var: 503 with a descriptive error
- Missing session secret (neither SESSION_SECRET nor SECRET_KEY): 503
- Twilio optional: no Twilio vars → still passes (Twilio is optional)
- Twilio partial: TWILIO_ACCOUNT_SID set but auth token absent → 503
- Twilio legacy phone alias: TWILIO_FROM_NUMBER accepted in place of TWILIO_PHONE_NUMBER
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import builtins
import json
from unittest.mock import patch, MagicMock

import flask_login.utils as _flu
from app import app
import routes  # noqa: F401 — registers the routes on the app
from models import db

results = []


def check(name, cond, extra=''):
    results.append((name, cond, extra))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


client = app.test_client()

# All required env vars supplied with dummy values for a "green" baseline
_ALL_REQUIRED = {
    'DATABASE_URL':          'postgresql://user:pass@host/db',
    'APP_BASE_URL':          'https://jhehaul.com',
    'STRIPE_SECRET_KEY':     'sk_test_dummy',
    'SENDGRID_API_KEY':      'SG.dummy',
    'GOOGLE_CLIENT_ID':      'dummy.apps.googleusercontent.com',
    'GOOGLE_CLIENT_SECRET':  'dummy_goog_secret',
    'PAY_LINK_UNDER_150':    'https://buy.stripe.com/dummy1',
    'PAY_LINK_150_300':      'https://buy.stripe.com/dummy2',
    'PAY_LINK_300_500':      'https://buy.stripe.com/dummy3',
    'PAY_LINK_OVER_500':     'https://buy.stripe.com/dummy4',
    'SESSION_SECRET':        'dummy_secret',
    # Twilio intentionally absent — it is optional
}


# ── 1. Happy path ─────────────────────────────────────────────────────────────
with app.app_context():
    with patch.dict(os.environ, _ALL_REQUIRED, clear=False):
        r = client.get('/health')
    data = json.loads(r.data)
    check('happy path returns 200', r.status_code == 200,
          f'status={r.status_code}')
    check('happy path body has status ok', data.get('status') == 'ok',
          f'body={data}')
    check('happy path has no errors key', 'errors' not in data,
          f'body={data}')


# ── 2. Import failure → 503 with errors list ──────────────────────────────────
_real_import = builtins.__import__

def _failing_import(name, *args, **kwargs):
    if name == 'stripe':
        raise ImportError("No module named 'stripe'")
    return _real_import(name, *args, **kwargs)

with app.app_context():
    with patch.dict(os.environ, _ALL_REQUIRED, clear=False):
        with patch('builtins.__import__', side_effect=_failing_import):
            r = client.get('/health')
    data = json.loads(r.data)
    check('import failure returns 503', r.status_code == 503,
          f'status={r.status_code}')
    check('import failure body has errors list', isinstance(data.get('errors'), list) and len(data['errors']) > 0,
          f'body={data}')
    check('import failure status is error', data.get('status') == 'error',
          f'body={data}')
    check('import failure errors mention missing package',
          any('stripe' in e for e in data.get('errors', [])),
          f'errors={data.get("errors")}')


# ── 3. DB failure → 503 with errors list ──────────────────────────────────────
from sqlalchemy.exc import OperationalError

with app.app_context():
    with patch.dict(os.environ, _ALL_REQUIRED, clear=False):
        with patch.object(db.session, 'execute',
                          side_effect=OperationalError("SELECT 1", {}, Exception("connection refused"))):
            r = client.get('/health')
    data = json.loads(r.data)
    check('db failure returns 503', r.status_code == 503,
          f'status={r.status_code}')
    check('db failure body has errors list', isinstance(data.get('errors'), list) and len(data['errors']) > 0,
          f'body={data}')
    check('db failure status is error', data.get('status') == 'error',
          f'body={data}')
    check('db failure errors mention database',
          any('database' in e for e in data.get('errors', [])),
          f'errors={data.get("errors")}')


# ── 4. Missing DATABASE_URL → 503 ─────────────────────────────────────────────
_missing_db = {k: v for k, v in _ALL_REQUIRED.items() if k != 'DATABASE_URL'}

with app.app_context():
    with patch.dict(os.environ, {**_missing_db, 'DATABASE_URL': ''}, clear=False):
        r = client.get('/health')
    data = json.loads(r.data)
    check('missing DATABASE_URL returns 503', r.status_code == 503,
          f'status={r.status_code}')
    check('missing DATABASE_URL errors mention DATABASE_URL',
          any('DATABASE_URL' in e for e in data.get('errors', [])),
          f'errors={data.get("errors")}')


# ── 5. Missing payment-link var → 503 ────────────────────────────────────────
_missing_paylink = {k: v for k, v in _ALL_REQUIRED.items() if k != 'PAY_LINK_UNDER_150'}

with app.app_context():
    with patch.dict(os.environ, {**_missing_paylink, 'PAY_LINK_UNDER_150': ''}, clear=False):
        r = client.get('/health')
    data = json.loads(r.data)
    check('missing PAY_LINK_UNDER_150 returns 503', r.status_code == 503,
          f'status={r.status_code}')
    check('missing PAY_LINK_UNDER_150 errors mention the var',
          any('PAY_LINK_UNDER_150' in e for e in data.get('errors', [])),
          f'errors={data.get("errors")}')


# ── 6. Neither SESSION_SECRET nor SECRET_KEY set → 503 ───────────────────────
_no_session = {k: v for k, v in _ALL_REQUIRED.items()
               if k not in ('SESSION_SECRET', 'SECRET_KEY')}

with app.app_context():
    with patch.dict(os.environ, {**_no_session, 'SESSION_SECRET': '', 'SECRET_KEY': ''}, clear=False):
        r = client.get('/health')
    data = json.loads(r.data)
    check('missing session secret returns 503', r.status_code == 503,
          f'status={r.status_code}')
    check('missing session secret errors mention SESSION_SECRET',
          any('SESSION_SECRET' in e for e in data.get('errors', [])),
          f'errors={data.get("errors")}')


# ── 7. Twilio absent entirely → still 200 (Twilio is optional) ───────────────
_no_twilio = {**_ALL_REQUIRED}
# Ensure none of the Twilio vars are set
_no_twilio_env = {**_no_twilio,
                  'TWILIO_ACCOUNT_SID': '', 'TWILIO_AUTH_TOKEN': '',
                  'TWILIO_PHONE_NUMBER': '', 'TWILIO_FROM_NUMBER': ''}

with app.app_context():
    with patch.dict(os.environ, _no_twilio_env, clear=False):
        r = client.get('/health')
    data = json.loads(r.data)
    check('no Twilio vars still returns 200 (Twilio optional)', r.status_code == 200,
          f'status={r.status_code} body={data}')


# ── 8. Twilio SID set but AUTH_TOKEN absent → 503 ────────────────────────────
_partial_twilio = {**_ALL_REQUIRED,
                   'TWILIO_ACCOUNT_SID': 'ACdummy',
                   'TWILIO_AUTH_TOKEN': '',
                   'TWILIO_PHONE_NUMBER': '+15550000000'}

with app.app_context():
    with patch.dict(os.environ, _partial_twilio, clear=False):
        r = client.get('/health')
    data = json.loads(r.data)
    check('Twilio SID set but no AUTH_TOKEN returns 503', r.status_code == 503,
          f'status={r.status_code}')
    check('Twilio partial error mentions TWILIO_AUTH_TOKEN',
          any('TWILIO_AUTH_TOKEN' in e for e in data.get('errors', [])),
          f'errors={data.get("errors")}')


# ── 9. TWILIO_FROM_NUMBER accepted as legacy alias for phone ──────────────────
_legacy_phone = {**_ALL_REQUIRED,
                 'TWILIO_ACCOUNT_SID': 'ACdummy',
                 'TWILIO_AUTH_TOKEN': 'dummy_token',
                 'TWILIO_PHONE_NUMBER': '',        # absent
                 'TWILIO_FROM_NUMBER': '+15550000001'}  # legacy alias set

with app.app_context():
    with patch.dict(os.environ, _legacy_phone, clear=False):
        r = client.get('/health')
    data = json.loads(r.data)
    check('TWILIO_FROM_NUMBER accepted as legacy phone alias (200)', r.status_code == 200,
          f'status={r.status_code} body={data}')


# ── Summary ───────────────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
