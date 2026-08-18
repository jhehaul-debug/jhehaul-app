---
name: AI Listing Assistant — Phase D
description: Architecture and constraints for the GPT-4o-mini listing suggestion feature.
---

## Structure
- `ai/__init__.py` — package marker
- `ai/listing_assistant.py` — single `suggest(listing_data)` function; one OpenAI call returns title + description + category + quality score + tips
- `models.py` — `AIUsageLog` table (`__tablename__ = 'ai_usage_logs'`); FK → `users.id` (NOT `user.id` — table is `users`)
- `routes.py` — `POST /api/ai/listing-assist` (auth-gated, 10/hr rate limit, ownership check); `GET /admin/ai-usage`
- `templates/listing_wizard.html` — AI panel inserted in step 2 (Details), between description textarea and category select
- `templates/admin_ai_usage.html` — usage stats + last 200 requests
- `static/css/jhe.css` — `.jhe-ai-*` style classes appended at end

## Key constraints
- `OPENAI_API_KEY` must be set in **both** Replit Secrets (local) and DigitalOcean App Platform env vars (production). Without it, `suggest()` immediately returns `{"ok": False, "error": "unavailable"}` — the listing flow continues normally.
- Rate limit: 10 AI calls per seller per hour, enforced by counting `AIUsageLog` rows in the last 60 min.
- Seller text goes into the OpenAI `user` role only — never the `system` prompt (prompt-injection guard).
- No PII sent to OpenAI: only title, description, category, condition, vehicle specs, photo count, has_price flag, has_location flag.
- AI never auto-applies — seller must click "Use This" per suggestion.

**Why:** User confirmed Phase D spec; graceful degradation was required so listing flow works without the key.

**How to apply:** Any change to `suggest()` response shape should stay backward-compatible with the JS in `listing_wizard.html` that reads `data.title`, `data.description`, `data.category`, `data.tips[]`, `data.score`.
