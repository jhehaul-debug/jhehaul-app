---
name: AI Search Intelligence — Phase E
description: Architecture and constraints for the natural-language marketplace search feature.
---

## Structure
- `ai/search_intelligence.py` — `parse_marketplace_search(query, ip)` → validated filters dict
- `routes.py` — `POST /api/ai/search-parse` (open to all visitors, IP rate-limited)
- `templates/marketplace.html` — "✨ Ask JHE Haul" button in hero + mobile compact header; panel with textarea, filter chips, confirm → redirect
- `static/css/jhe.css` — `.jhe-ais-*` styles
- `models.py` — `SavedSearch` table (foundation only; no alerts UI yet)

## Marketplace route new params (Phase E)
Added to existing `/marketplace` GET route: `vehicle_make`, `vehicle_model`, `vehicle_year_min`, `vehicle_year_max`, `vehicle_mileage_max`, `condition`, `sort` (newest|price_asc|price_desc), `delivery_available`, `recency` (today|week|month).

## Cost controls in search_intelligence.py
- In-memory cache: 5 min TTL, keyed by MD5(normalized query)
- IP rate limit: 30 requests/hour (tracked in module-level `_ip_log` dict — resets on dyno restart)
- 10s timeout, max 300 output tokens

## Key constraints
- `OPENAI_API_KEY` required (shared with Phase D) — absent → graceful `{"ok":false,"error":"unavailable"}`
- Buyer query goes into OpenAI `user` role only (prompt-injection guard)
- No user identity or PII sent to OpenAI — only raw query text
- Category name → slug resolution happens server-side in the endpoint (not client-side)
- `delivery_available` maps to `Listing.delivery_option.ilike('%jhe_haul%')`

**Why:** Spec required natural-language → structured filter translation without replacing normal search.

**How to apply:** Any new marketplace filter param must be added to both the param-reading block and the `is_search` bool check in the marketplace route, or it won't trigger the search branch.
