"""
WSGI entry point for production (Gunicorn / DigitalOcean App Platform).

Startup health check
────────────────────
After every deploy Gunicorn forks N workers, each of which imports this
module once.  We hook into that import to:

1. Detect fatal startup failures (missing package, DB unreachable at
   db.create_all time) — handled in app.py's init block and here for
   auth/routes import failures.
2. Run a post-import liveness check (package imports + DB SELECT 1) to
   catch subtler breakage that doesn't prevent the module from loading.
3. Notify admin via SMS + email on any failure.

Rate-limit: one alert per deploy, cross-worker.
   /tmp is a fresh tmpfs on every DigitalOcean container deploy.  We use
   os.open(O_CREAT|O_EXCL) — atomic on Linux — to claim the alert slot
   *after* errors are confirmed so a passing worker cannot block a failing
   one from notifying.
"""
import os
import logging

# ── Shared alert sentinel (same path used by app.py's DB-init handler) ────────
_ALERT_SENTINEL = "/tmp/jhe_health_alert_sent"


# ── Standalone notification helper ────────────────────────────────────────────
# This function must NOT depend on `routes`, `auth`, or any module that may
# have failed to import — it only uses sms_service and email_service, which
# are independently loadable.

def _write_health_log(errors, source, notified=False):
    """Persist a HealthCheckLog row so failures survive container restarts.

    Safe to call both inside and outside an active app context — creates one
    when needed.  Swallows all exceptions so a broken DB never masks the alert.
    """
    try:
        import json as _json
        from models import HealthCheckLog as _HCL
        row = _HCL(
            source=source,
            errors_json=_json.dumps(errors),
            notified=notified,
        )
        # Reuse the active context when available; open a temporary one otherwise.
        try:
            from flask import has_app_context as _hac
            if _hac():
                db.session.add(row)
                db.session.commit()
            else:
                with app.app_context():
                    db.session.add(row)
                    db.session.commit()
        except Exception as _ctx_exc:
            logging.error("startup health log DB write failed: %s", _ctx_exc)
    except Exception as _exc:
        logging.error("startup health log unexpected error: %s", _exc)


def _notify_admin(errors):
    """
    Send SMS + email to admin listing startup failures.
    Catches all exceptions internally so a broken notification channel can
    never suppress the alert on the other channel.
    """
    error_lines = "\n".join(f"• {e}" for e in errors)
    short_lines  = "; ".join(errors)[:300]

    try:
        from sms_service import send_sms
        admin_phone = os.environ.get("ADMIN_PHONE")
        if admin_phone:
            send_sms(admin_phone,
                     f"[JHE Haul] Deploy health check FAILED:\n{short_lines}",
                     event_type="admin_health_alert")
        else:
            logging.warning("startup health alert: ADMIN_PHONE not set — SMS skipped")
    except Exception as exc:
        logging.error("startup health alert SMS error: %s", exc)

    try:
        from email_service import notify_admin, _html
        body_html = (
            "<p>The deploy-time health check detected the following failures:</p>"
            "<div class='info-box'>"
            f"<pre style='margin:0;white-space:pre-wrap;font-size:0.88rem;"
            f"color:#b91c1c;'>{error_lines}</pre></div>"
            "<p>The DigitalOcean container may restart automatically — please "
            "verify the app is serving correctly before users encounter errors.</p>"
        )
        notify_admin(
            "[JHE Haul] 🚨 Deploy health check FAILED",
            _html("Deploy Health Check Failed",
                  "One or more startup checks did not pass.",
                  "🚨 Health Alert", body_html),
            event_type="admin_health_alert",
        )
    except Exception as exc:
        logging.error("startup health alert email error: %s", exc)


def _claim_and_notify(errors):
    """
    Atomically claim the alert slot (O_EXCL); if we win, call _notify_admin.
    Returns True if this worker sent the alert, False if already sent.
    """
    try:
        fd = os.open(_ALERT_SENTINEL, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
    except FileExistsError:
        logging.info(
            "Startup health check FAILED but alert already sent by another "
            "worker — skipping duplicate notification."
        )
        return False
    except OSError as exc:
        # Unexpected FS error — attempt to notify anyway rather than go silent.
        logging.warning("startup health check: sentinel open error (%s) — notifying anyway", exc)
        _notify_admin(errors)
        return True

    _notify_admin(errors)
    return True


# ── Step 1: import the application (covers app.py init + DB create_all) ───────
# app.py already wraps db.create_all() in its own try/except + alert; we just
# propagate the exception if it re-raises.
from app import app, db  # noqa: E402  (may raise if DB unreachable)

# ── Step 2: import auth and routes — catch fatal import failures here ──────────
_import_errors = []

try:
    import auth   # noqa: F401 - registers Google/GitHub OAuth blueprints
except Exception as _auth_exc:
    _import_errors.append(f"failed to import auth: {_auth_exc}")
    logging.critical("wsgi: failed to import auth: %s", _auth_exc)

try:
    import routes  # noqa: F401 - registers all routes with the app
except Exception as _routes_exc:
    _import_errors.append(f"failed to import routes: {_routes_exc}")
    logging.critical("wsgi: failed to import routes: %s", _routes_exc)

if _import_errors:
    # Critical imports failed — alert admin, then crash so DigitalOcean can
    # restart the container (it won't serve correctly without these modules).
    logging.error("Startup import failures detected: %s", _import_errors)
    _write_health_log(_import_errors, source='startup_import', notified=False)
    notified = _claim_and_notify(_import_errors)
    # Update the DB row's notified flag now that we know whether the alert went out.
    try:
        with app.app_context():
            from models import HealthCheckLog as _HCL
            last = _HCL.query.filter_by(source='startup_import').order_by(_HCL.id.desc()).first()
            if last:
                last.notified = notified
                db.session.commit()
    except Exception as _upd_exc:
        logging.error("startup health log notified-flag update failed: %s", _upd_exc)
    raise ImportError(f"Startup aborted due to import failures: {_import_errors}")

# ── Step 3: post-import liveness check (packages + DB SELECT 1) ───────────────
# Only runs if auth/routes loaded successfully.  Catches subtler breakage such
# as a DB connection that worked during db.create_all() but became unavailable.

def _run_startup_health_check():
    errors = []
    try:
        from routes import _run_health_checks
        errors = _run_health_checks()
    except Exception as exc:
        errors = [f"health check raised an unexpected exception: {exc}"]
        logging.error("_run_health_checks raised: %s", exc)

    if not errors:
        logging.info("Startup health check passed — all systems ok.")
        return

    logging.error("Startup health check FAILED. Errors: %s", errors)
    _write_health_log(errors, source='startup_liveness', notified=False)
    notified = _claim_and_notify(errors)
    # Update notified flag on the row we just wrote.
    try:
        from models import HealthCheckLog as _HCL2
        last = _HCL2.query.filter_by(source='startup_liveness').order_by(_HCL2.id.desc()).first()
        if last:
            last.notified = notified
            db.session.commit()
    except Exception as _upd2_exc:
        logging.error("startup health log notified-flag update (liveness) failed: %s", _upd2_exc)


try:
    with app.app_context():
        _run_startup_health_check()
except Exception as _hc_exc:
    logging.error("Startup health check wrapper raised an unexpected error: %s", _hc_exc)

# ── WSGI application object (used by Gunicorn) ────────────────────────────────
application = app
