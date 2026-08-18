---
name: Admin Ops Layer — Phase L
description: Architecture decisions and constraints for the Phase L admin AI operations assistant.
---

## Rule
The admin copilot (ai/admin_copilot.py) and ops tool layer (ai/admin_ops.py) are completely separate from the buyer/seller copilot (ai/copilot.py). Never merge them.

**Why:** Admin tools expose aggregate marketplace data not appropriate for buyers/sellers. The systems share GPT-4o-mini but use separate rate limiters, system prompts, tool schemas, and audit logging.

## How to apply
- New admin intelligence tools go in `ai/admin_ops.py` (read-only functions) + schema in `ai/admin_copilot.py:ADMIN_COPILOT_TOOLS`.
- New buyer/seller tools go in `ai/copilot_tools.py` + schema in `ai/copilot.py:COPILOT_TOOLS`.
- Admin ops functions must NEVER contain db.session.add/commit/delete — read-only contract.
- Admin copilot rate limit: 60/hr per admin_id. Buyer copilot: 20/hr per IP.
- Both endpoints log to AIUsageLog (tool_name='admin_copilot' vs feature-specific names).
- `require_admin` decorator returns 302 for unauthenticated (redirect to login) and 403 for non-admin. CSRF fires before it on POSTs, so JSON endpoints from unauthenticated clients may return 400 before 302.
