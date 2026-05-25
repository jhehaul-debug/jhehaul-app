---
name: SMS system architecture
description: How the Twilio SMS system is structured alongside SendGrid email, and key gotchas
---

## Key design decisions

**Single-row settings table**: `SmsSettings` is always get-or-created as a single row. Admin can toggle global SMS and per-event-type SMS from `/admin/sms-settings`.

**Migration ordering rule**: New User ORM columns must be migrated via `ALTER TABLE … ADD COLUMN IF NOT EXISTS` BEFORE the admin flag restore block in `app.py`. The admin flag restore does `User.query.filter_by(email=…).first()` which trips on unknown columns. Any new User columns go in the block labeled "users SMS consent" (currently lines ~190-201).

**Why:** Flask-SQLAlchemy maps all model fields into every SELECT. If the DB column doesn't exist yet when the admin query runs at startup, the whole startup context aborts.

**Env vars**: `TWILIO_PHONE_NUMBER` is the canonical name; `TWILIO_FROM_NUMBER` is the legacy fallback (supported in `_twilio_client()`). Don't drop the fallback.

**User opt-in check in routes**: Existing SMS calls only check `user.notify_sms and user.phone`. The new `sms_consent` field is stored for compliance and shown in UI, but not enforced as a hard gate on existing SMS sends (backward compat). New event types added after the consent feature DO check all three.

**Phone verification**: 6-digit code stored in `user.phone_verify_code`, expires 10 min from `phone_verify_sent_at`. Routes: `POST /profile/send-phone-verify` and `POST /profile/verify-phone`.

## New models
- `SmsLog`: one row per send attempt (event_type, phone, message, twilio_sid, status, error_msg, retry_count)
- `SmsSettings`: single-row admin config

## New routes
- `GET/POST /admin/sms-settings` and `/admin/sms-settings/update`
- `POST /admin/sms-settings/test`
- `GET /admin/sms-logs`
- `POST /admin/sms/resend/<log_id>`
- `POST /profile/send-phone-verify`
- `POST /profile/verify-phone`
