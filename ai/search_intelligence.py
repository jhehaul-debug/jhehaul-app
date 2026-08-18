"""
ai/search_intelligence.py — Natural-language marketplace search parser (Phase E).

Converts a buyer's conversational query into validated structured search filters
that the existing /marketplace route can execute directly.

Privacy contract:
  Only the buyer's raw query text (≤300 chars) is sent to OpenAI.
  No user identity, session data, purchase history, or private records is shared.
  Buyer text is isolated in the 'user' role only (prompt-injection guard).

Cost controls:
  - In-memory result cache: 5 minutes per unique query (prevents repeat API calls)
  - Per-IP rate limit: 30 requests per hour
  - Hard 10-second timeout per call
  - Max 300 input chars, 300 output tokens

Fallback:
  Returns {"ok": False, "error": "unavailable"} on any failure so normal
  marketplace search continues uninterrupted.
"""

import os
import re
import json
import time
import math
import logging
import hashlib

log = logging.getLogger(__name__)

_TIMEOUT_SECS       = 10
_MAX_QUERY_CHARS    = 300
_MAX_OUTPUT_TOKENS  = 300
_CACHE_TTL_SECS     = 300   # 5 minutes
_RATE_LIMIT_MAX     = 30    # per IP per hour
_RATE_LIMIT_WINDOW  = 3600

# Simple in-memory stores (reset on dyno restart — acceptable for a cache)
_cache: dict   = {}   # {md5(norm_query): (timestamp, result)}
_ip_log: dict  = {}   # {ip: [timestamp, ...]}

_ALLOWED_LISTING_TYPES = {"item", "property_sale", "rental"}
_ALLOWED_CONDITIONS    = {"new", "like_new", "good", "fair", "for_parts"}
_ALLOWED_SORTS         = {"newest", "price_asc", "price_desc"}
_ALLOWED_RECENCY       = {"today", "week", "month"}

_KNOWN_CATEGORIES = [
    "Vehicles", "Furniture", "Electronics", "Appliances",
    "Tools & Equipment", "Clothing & Accessories", "Books & Media",
    "Sports & Outdoors", "Toys & Games", "Home & Garden",
    "Collectibles & Antiques", "Musical Instruments",
    "Baby & Kids", "Jewelry & Watches", "Art", "Other",
]

_SYSTEM_PROMPT = (
    "You are a search assistant for JHE Haul Marketplace, a local buy/sell "
    "platform in Minnesota. Convert the buyer's search request into structured "
    "filter JSON. Only use what the buyer explicitly stated — do not invent "
    "details, vehicle history, or location data they didn't mention. "
    "Respond ONLY with a single valid JSON object. No markdown, no commentary."
)

_USER_TEMPLATE = (
    'Search request: "{query}"\n\n'
    "Return ONLY a JSON object with exactly these keys (use null for anything not mentioned):\n"
    "  keywords        array of strings — general search terms not covered by other fields\n"
    "  category        string|null — one of: {cats}\n"
    "  listing_type    string|null — one of: item, property_sale, rental\n"
    "  min_price       number|null\n"
    "  max_price       number|null\n"
    "  condition       array of strings — zero or more of: new, like_new, good, fair, for_parts\n"
    "  city_zip        string|null — city name or 5-digit ZIP; only if explicitly mentioned\n"
    "  vehicle         object with keys: make (str|null), model (str|null), "
    "year_min (int|null), year_max (int|null), mileage_max (int|null)\n"
    "  delivery_available  boolean|null — true only if buyer asks for JHE Haul delivery\n"
    "  sort            string|null — one of: newest, price_asc, price_desc\n"
    "  recency         string|null — one of: today, week, month\n"
    "  summary         string — 1-sentence plain-English restatement of what you understood (max 120 chars)"
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize(query: str) -> str:
    return re.sub(r'\s+', ' ', query.lower().strip())


def _cache_key(query: str) -> str:
    return hashlib.md5(_normalize(query).encode()).hexdigest()


def _check_rate_limit(ip: str) -> bool:
    """Return True (blocked) if the IP has exceeded its hourly limit."""
    if not ip:
        return False
    now = time.time()
    cutoff = now - _RATE_LIMIT_WINDOW
    recent = [t for t in _ip_log.get(ip, []) if t > cutoff]
    _ip_log[ip] = recent
    if len(recent) >= _RATE_LIMIT_MAX:
        return True
    _ip_log[ip] = recent + [now]
    return False


def _clean(v, max_len: int = 120) -> str | None:
    if not v:
        return None
    return re.sub(r"<[^>]+>", "", str(v)).strip()[:max_len] or None


def _safe_num(v, min_v=0, max_v=10_000_000) -> float | None:
    try:
        n = float(v)
        return n if min_v <= n <= max_v else None
    except (TypeError, ValueError):
        return None


def _validate(raw: dict) -> dict:
    """Server-side validation and sanitisation of every AI output field."""
    keywords = [
        re.sub(r"<[^>]+>", "", str(k)).strip()[:80]
        for k in (raw.get("keywords") or [])
        if str(k).strip()
    ][:6]

    category = _clean(raw.get("category"), 80)

    lt = raw.get("listing_type")
    listing_type = lt if lt in _ALLOWED_LISTING_TYPES else None

    min_price = _safe_num(raw.get("min_price"))
    max_price = _safe_num(raw.get("max_price"))

    raw_conds = raw.get("condition") or []
    condition = [c for c in raw_conds if c in _ALLOWED_CONDITIONS]

    city_zip = _clean(raw.get("city_zip"), 80)

    vraw = raw.get("vehicle") or {}
    vehicle = {
        "make":        _clean(vraw.get("make"), 60),
        "model":       _clean(vraw.get("model"), 60),
        "year_min":    int(y) if (y := _safe_num(vraw.get("year_min"), 1900, 2030)) else None,
        "year_max":    int(y) if (y := _safe_num(vraw.get("year_max"), 1900, 2030)) else None,
        "mileage_max": int(m) if (m := _safe_num(vraw.get("mileage_max"), 0, 1_000_000)) else None,
    }

    da = raw.get("delivery_available")
    delivery_available = bool(da) if isinstance(da, bool) else None

    sort_v = raw.get("sort")
    sort    = sort_v if sort_v in _ALLOWED_SORTS else None

    rec_v   = raw.get("recency")
    recency = rec_v if rec_v in _ALLOWED_RECENCY else None

    summary = _clean(raw.get("summary"), 160)

    return {
        "keywords":          keywords,
        "category":          category,
        "listing_type":      listing_type,
        "min_price":         min_price,
        "max_price":         max_price,
        "condition":         condition,
        "city_zip":          city_zip,
        "vehicle":           vehicle,
        "delivery_available": delivery_available,
        "sort":              sort,
        "recency":           recency,
        "summary":           summary,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def parse_marketplace_search(query: str, ip: str = "") -> dict:
    """
    Parse a natural-language buyer search into validated marketplace filters.

    Args:
        query: raw buyer query (truncated to _MAX_QUERY_CHARS).
        ip:    caller IP for per-IP rate limiting ("" skips IP check).

    Returns:
        {"ok": True, "filters": {...}, "summary": str, "cached": bool,
         "response_ms": int}
        or {"ok": False, "error": "unavailable"|"rate_limited"|"empty"}
    """
    query = (query or "").strip()[:_MAX_QUERY_CHARS]
    if not query:
        return {"ok": False, "error": "empty"}

    if _check_rate_limit(ip):
        log.info("ai/search_intelligence: rate-limited IP=%s", ip[:20])
        return {"ok": False, "error": "rate_limited"}

    # Cache hit
    ck  = _cache_key(query)
    now = time.time()
    if ck in _cache:
        ts, cached = _cache[ck]
        if now - ts < _CACHE_TTL_SECS:
            log.debug("ai/search_intelligence: cache hit len=%d", len(query))
            return {**cached, "cached": True}

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        log.info("ai/search_intelligence: OPENAI_API_KEY not set")
        return {"ok": False, "error": "unavailable"}

    try:
        import openai as _oai
    except ImportError:
        return {"ok": False, "error": "unavailable"}

    user_msg = _USER_TEMPLATE.format(
        query=query,
        cats=", ".join(_KNOWN_CATEGORIES),
    )

    t0 = time.time()
    try:
        client = _oai.OpenAI(api_key=api_key, timeout=_TIMEOUT_SECS)
        resp   = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            max_tokens=_MAX_OUTPUT_TOKENS,
            temperature=0.1,
        )
        response_ms = int((time.time() - t0) * 1000)
        raw_text = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        log.warning("ai/search_intelligence: API call failed: %s", exc)
        return {"ok": False, "error": "unavailable"}

    try:
        clean_text = re.sub(r"```(?:json)?", "", raw_text).strip().rstrip("`").strip()
        raw_data   = json.loads(clean_text)
    except (ValueError, TypeError):
        log.warning("ai/search_intelligence: JSON parse failed; raw=%s", raw_text[:200])
        return {"ok": False, "error": "unavailable"}

    validated = _validate(raw_data)
    summary   = validated.pop("summary", None) or ""

    result = {
        "ok":          True,
        "filters":     validated,
        "summary":     summary,
        "cached":      False,
        "response_ms": response_ms,
    }
    _cache[ck] = (now, result)
    return result
