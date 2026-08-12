"""
Task 77 validation: /health endpoint catches broken dependencies and DB failures.

Run with:  python tests/test_health_endpoint.py

Verifies:
- Happy path: 200 {"status": "ok"} when all imports succeed and DB is up
- Import failure: 503 with an "errors" list when a critical module is missing
- DB failure: 503 with an "errors" list when the DB raises OperationalError
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


# ── 1. Happy path ─────────────────────────────────────────────────────────────
with app.app_context():
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


# ── Summary ───────────────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
