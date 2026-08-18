---
name: SEO foundation — Phase C
description: Canonical URL pattern, seo.py helpers, dynamic sitemap, category routes, base.html blocks — all decisions worth preserving for future SEO or AI phases.
---

## Canonical URL pattern
`/listing/<id>-<slug>` — ID is the routing key; slug is computed by `listing_slug()` and ignored by Flask but used by crawlers.
Both `/listing/<id>` and `/listing/<id>-<slug>` are served by the same `listing_detail()` view (two `@app.route` decorators, `slug=None` default). No redirect — canonical tag signals the preferred URL.

**Why:** Redirect from `/listing/<id>` would break internal `url_for('listing_detail', listing_id=...)` calls unless all callers were updated. Canonical tag achieves the same Google consolidation without touching existing links.

**How to apply:** When generating outbound listing links (email, SMS, OG), use `listing_canonical_path(listing)` from `services/seo.py`. For internal app links (edit, offer, message), keep using `url_for('listing_detail', listing_id=...)`.

## services/seo.py
Pure functions — no DB access, no Flask imports. Safe to call in routes, Jinja, and future AI layers.
Key exports: `listing_slug`, `listing_canonical_path`, `listing_seo_title`, `listing_seo_description`, `listing_jsonld`.
JSON-LD: Vehicle for year/make/model listings; SingleFamilyResidence/Residence for properties; Product for everything else. Offer block always included. No email/phone/street address ever emitted.

## base.html blocks added (Phase C)
Three new empty blocks placed after og_meta, before favicon:
- `{% block canonical %}` — canonical link tag
- `{% block noindex %}` — robots noindex meta (draft/removed/expired/sold listings)
- `{% block structured_data %}` — JSON-LD script tag

**Why:** Keeps base.html generic; per-page overrides live in the template that needs them.

## Dynamic sitemap
`/sitemap.xml` route now generates XML in-memory. Includes static pages + all active/approved listings with `listing_canonical_path()` URLs. Limit: 45,000 rows. Old `static/sitemap.xml` still exists but is no longer served (route overrides it).

## Category discovery routes
`/marketplace/<cat_page>` with whitelist: `vehicles`, `items`, `homes-for-sale`, `rentals`.
Config in `_CATEGORY_PAGE_CFG` dict. Uses `_marketplace_categories()` and `_saved_listing_ids()` helpers already in routes.py. Returns 404 for unknown slugs.
marketplace.html now reads `seo_page_title`, `seo_page_desc`, `seo_canonical_path` from context — safe Jinja fallbacks for base marketplace route which doesn't pass them.

## robots.txt additions (Phase C)
Added Disallow: /selling, /my-listings, /notifications, /saved, /my-offers, /seller/
These are login-redirect pages that were crawlable by the previous Allow: / rule.

## View count / bot inflation
Not addressed in Phase C per spec. Session-dedup (`viewed_listings` in session) prevents repeat-session inflation but not first-hit bot inflation. Document for Phase D analytics work.
