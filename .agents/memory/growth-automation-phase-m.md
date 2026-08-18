---
name: Growth Automation — Phase M
description: Architecture decisions and constraints for Phase M growth automation and re-engagement system.
---

## Rule
All growth notification logic is centralized in notification_service.py. Never scatter it across routes or templates. Background jobs run in the existing Phase F worker queue — never block web requests.

**Why:** Phase M spec explicitly prohibits notification loops, duplicate emails, and mass generic blasts. Centralized service + dedup_key + cooldown windows enforce this.

## Key Architecture Decisions

### Deduplication
- `Notification.dedup_key` column (VARCHAR 200) — application-level dedup, no DB unique constraint.
- Format: `{type}:{resource_id}:{window}` (e.g. `price_drop:123:2026-08-18`)
- `create_notification_deduped()` checks for existing row before inserting. Returns None if dedup hit.

### User Preferences (9 new User columns)
- In-app: `notify_saved_search_match`, `notify_price_drop`, `notify_offer_reminder`, `notify_listing_expiry_reminder`, `notify_recommendations`
- Email: `notify_email_price_drop`, `notify_email_offers`, `notify_email_listing_expiry`, `notify_email_recommendations`
- All default True except `notify_recommendations` and `notify_email_recommendations` (opt-in marketing, default False).
- Managed at `/settings/notifications` (GET/POST, @require_login).

### Background Thread
- `growth_automation.py` → `start_growth_automation_thread(app)` — started in app.py after draft_cleanup.
- Interval: 3600s. Enqueues GROWTH_REMINDER jobs for 5 check_types at LOW priority.
- No-op on subsequent calls (singleton guard).

### Workers
- `PRICE_DROP_NOTIFY` — triggered event-driven from listing_edit route when price drops.
- `GROWTH_REMINDER` — batch scheduled: unread_message_remind, offer_remind, listing_expiry_remind, relist_remind, seller_insight.
- Both registered in worker/handlers/__init__.py.

### Email Allowlist
- 5 new functions added to worker/handlers/email_notification.py allowlist.
- `notify_seller_listing_expiring_soon` was already in email_service.py — now also allowlisted for queue dispatch.

### Admin Analytics
- `get_growth_operations_summary(days)` added to ai/admin_ops.py — 15th tool.
- Admin copilot now has 17 tool schemas (was 16).

### No SMS
- All Phase M files confirmed SMS-free. Only in-app + email channels per spec.

### Python 3.11 Gotcha
- Backslash escape inside f-string expression is a SyntaxError in Python < 3.12.
- Pattern: use a variable (`_var = "haven't"`) outside the f-string instead of `'haven\'t'` inside `{...}`.

## How to Apply
- New growth notification type: add to NOTIF_CATEGORIES, NOTIF_ICONS, NOTIF_PRIORITY in notification_service.py; add handler or extend GROWTH_REMINDER check_type; add email fn + allowlist entry; add user pref column + migration.
- Price drop trigger is in listing_edit route — captures `_price_before_edit` before `_apply_listing_fields`, fires after commit.
- Cooldown windows are in `_COOLDOWN` dict at top of growth_reminder.py.
