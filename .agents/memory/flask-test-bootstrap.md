---
name: Flask test bootstrap
description: How to write route tests against this Flask app without pytest
---

Routes are registered only when `routes` is imported (main.py does `import routes`).
**Why:** Test scripts that import only `app` get 404s on every endpoint — POSTs silently "pass" negative assertions.
**How to apply:** In any test/verification script, do `from app import app` then `import routes` before creating a test client. Bypass auth by patching `flask_login.utils._get_user` to return an admin user. Tests run against the real dev DB — create and clean up temp rows.
