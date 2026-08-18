---
name: Monetization Architecture — Phase N
description: Architecture decisions and constraints for Phase N marketplace monetization foundation.
---

## Rule
All monetization logic is centralized in monetization_service.py. Never scatter pricing or entitlement checks across templates or routes. Use has_feature(user, feature) for every server-side entitlement check — never trust frontend assertions.

**Why:** Phase N spec explicitly prohibits live billing activation, autonomous purchases, and hard-coded prices. Centralized service + product DB table + all-OFF defaults enforce this.

## Key Architecture Decisions

### Products Default OFF
- `MonetizationProduct.is_active = False` by default. Cannot activate without a Stripe Price ID.
- Admin toggle at `/admin/monetization/products/<id>/toggle` — guarded + audit-logged.
- Activation blocked if `stripe_price_id` is NULL.

### Stripe TEST MODE Only
- `stripe_service.get_stripe_mode()` checks key prefix: `sk_test_` → 'test', `sk_live_` → 'live'.
- `_assert_test_mode()` raises RuntimeError if live key detected — called inside `create_checkout_session()`.
- `STRIPE_WEBHOOK_SECRET` env var required for webhook handler — rejects unsigned payloads.

### Entitlement Hierarchy
```
PLAN_FREE < PLAN_BUSINESS < PLAN_DEALER
```
- Plan-level features: checked against User.seller_plan + seller_plan_expires_at.
- Purchase-level features (featured_listing, boost, promoted_listing): checked against MonetizationPurchase with status='active' and expires_at > now.
- `has_feature(user, feature, listing_id=None)` — fail-closed: returns False on DB error or unknown feature.

### New Models
- `MonetizationProduct` — product catalog (all OFF by default)
- `MonetizationPurchase` — purchase records (no card data; Stripe IDs only)
- `SellerStorefront` — /store/<slug> public page (no PII exposed)
- `MonetizationAuditLog` — audit trail (no secrets; safe metadata only)

### User columns added (Phase N)
- `stripe_customer_id`, `seller_plan` (default 'free'), `seller_plan_expires_at`, `seller_plan_stripe_sub_id`

### Listing columns added (Phase N)
- `boost_expires_at`, `promoted_type`

### Routes
- `/monetize` — seller monetization dashboard
- `/monetize/storefront` — create/edit storefront
- `/store/<slug>` — public storefront
- `/monetization/success` + `/monetization/cancel` — post-checkout pages
- `/api/monetization/checkout` — POST, requires auth, TEST MODE only
- `/api/stripe/webhook` — POST, signature-verified only; `STRIPE_WEBHOOK_SECRET` required
- `/admin/monetization` — admin overview (is_admin required)
- `/admin/monetization/products/<id>/toggle` — POST (is_admin required)

### Worker
- `PROMOTION_EXPIRE` registered (15th type); clears featured/boost/promoted_type flags on expiry; listing never deleted.

### Admin Copilot
- Now 20 tools (was 17): added get_revenue_analytics, get_promotion_summary, get_monetization_product_catalog.

### Key Gotcha
- Login URL is `url_for('auth.login')` not `url_for('login')` — Phase N routes learned this the hard way.

## How to Apply
- New product type: add to `MonetizationProduct` DB record + `_FEATURE_MAP` in monetization_service.py + admin copilot tool schema if admin-queryable.
- New revenue stream: add to `get_revenue_analytics()` in admin_ops.py.
- Activating billing: set `stripe_price_id` on product + admin toggles `is_active` + set `STRIPE_WEBHOOK_SECRET` env var.
- Never activate live Stripe without explicit approval file.
