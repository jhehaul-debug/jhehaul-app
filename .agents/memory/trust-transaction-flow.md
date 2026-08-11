---
name: Trust & Transaction Flow
description: Seller profiles, offer respond flow, report user, admin user detail — added in marketplace trust pass.
---

## Key architecture decisions

- **User.id is a String** (Replit Auth UUID like `53919193`). All FK columns referencing users.id use `db.Column(db.String, ...)`. Route converters for user_id use `<user_id>` (no type prefix) not `<int:user_id>`.
- **Offer respond routes use int offer_id** — ListingOffer.id is integer, so `<int:offer_id>` is correct.
- **Seller profile at `/seller/<user_id>`** — public, no login required. Shows first name + last initial, city, member since, active listings grid, sold count.
- **UserReport model** — new table `user_reports`; created by `db.create_all()` on startup (no explicit migration needed). Use `str(user_id)` when storing since reported_user_id is String FK.
- **routes.py must be imported separately** — `from app import app` alone doesn't register routes. Tests must `import routes` after app import or url_for/endpoints are missing.

## Offer status flow
pending → accepted | declined | countered | withdrawn
countered → accepted (buyer accepts counter) | declined | withdrawn

**Why:** `counter_amount` field on ListingOffer stores seller's counter. Status `countered` triggers buyer-respond UI.

## Admin user detail route
Path: `/admin/users/<user_id>/detail` (string user_id, `/detail` suffix to avoid conflict with suspend/restore routes at `/admin/users/<string:user_id>/suspend`).

## What was NOT done (for Task #36 coordination)
- Saved Items nav link in base.html — wait for Task #36 merge, then pull and add.
- Saved Items link in profile.html quick links — same reason.
- Heart/save button on marketplace listing cards (Task #65 is proposed).
