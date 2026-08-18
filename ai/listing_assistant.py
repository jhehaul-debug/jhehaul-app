"""
ai/listing_assistant.py — AI listing suggestion service for JHE Haul.

One OpenAI call per request returns title, description, category suggestion,
quality score, and completeness tips together, minimising latency and cost.

Privacy contract:
  Only sends public listing fields (type, category, title, description,
  condition, vehicle specs). Never sends email, phone, address, messages,
  offers, or payment data.  Seller-supplied text goes into the *user* role
  only, so it can never override system instructions (prompt-injection guard).

Fallback: if OPENAI_API_KEY is absent or the call fails for any reason,
  returns {"ok": False, "error": "unavailable"} so listing creation
  continues normally without interruption.
"""

import os
import re
import json
import time
import logging

log = logging.getLogger(__name__)

_TIMEOUT_SECS   = 15
_MAX_INPUT_CHARS = 600   # truncate user text before sending
_MAX_TITLE_LEN   = 120
_MAX_DESC_LEN    = 1200

_ALLOWED_CATEGORIES = [
    "Vehicles", "Furniture", "Electronics", "Appliances",
    "Tools & Equipment", "Clothing & Accessories", "Books & Media",
    "Sports & Outdoors", "Toys & Games", "Home & Garden",
    "Collectibles & Antiques", "Musical Instruments",
    "Baby & Kids", "Jewelry & Watches", "Art", "Other",
]

_SYSTEM_PROMPT = (
    "You are a helpful listing assistant for JHE Haul Marketplace, "
    "a local buy/sell platform in Minnesota. "
    "Help sellers write clear, honest, buyer-friendly listings. "
    "Never invent facts: if a detail is not provided, omit it. "
    "Do not add warranties, legal claims, or ownership history. "
    "Respond ONLY with a single valid JSON object — no markdown, no extra text."
)


def _sanitize(text: str, max_len: int = _MAX_INPUT_CHARS) -> str:
    return (text or "").strip()[:max_len]


def _clean_output(text: str, max_len: int = 2000) -> str:
    """Strip HTML tags and truncate AI output to prevent injection."""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = text.strip()
    return text[:max_len]


def suggest(listing_data: dict) -> dict:
    """
    Call OpenAI GPT-4o mini to generate listing suggestions.

    Args:
        listing_data: dict with safe, non-PII listing fields.
            Keys used: listing_type, category, title, description,
            condition, vehicle_year, vehicle_make, vehicle_model,
            vehicle_mileage, vehicle_trim, vehicle_fuel_type,
            vehicle_transmission, photo_count, has_price, has_location.

    Returns:
        On success:
            {"ok": True, "title": str, "description": str,
             "category": str, "tips": [str], "score": int,
             "response_ms": int}
        On failure:
            {"ok": False, "error": "unavailable" | "timeout"}
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        log.info("ai/listing_assistant: OPENAI_API_KEY not configured — unavailable")
        return {"ok": False, "error": "unavailable"}

    try:
        import openai as _oai
    except ImportError:
        log.warning("ai/listing_assistant: openai package not installed")
        return {"ok": False, "error": "unavailable"}

    # ── Build safe user message (seller content isolated in user role) ────────
    lt       = listing_data.get("listing_type") or "item"
    category = _sanitize(listing_data.get("category") or "")
    title    = _sanitize(listing_data.get("title") or "")
    desc     = _sanitize(listing_data.get("description") or "")
    cond     = listing_data.get("condition") or ""
    vy       = listing_data.get("vehicle_year") or ""
    vmake    = _sanitize(listing_data.get("vehicle_make") or "")
    vmodel   = _sanitize(listing_data.get("vehicle_model") or "")
    vmile    = listing_data.get("vehicle_mileage") or ""
    vtrim    = _sanitize(listing_data.get("vehicle_trim") or "")
    vfuel    = listing_data.get("vehicle_fuel_type") or ""
    vtrans   = listing_data.get("vehicle_transmission") or ""
    photo_count  = int(listing_data.get("photo_count") or 0)
    has_price    = bool(listing_data.get("has_price"))
    has_location = bool(listing_data.get("has_location"))

    lines = [f"Listing type: {lt}"]
    if category:
        lines.append(f"Category: {category}")
    if vy and vmake and vmodel:
        vline = f"Vehicle: {vy} {vmake} {vmodel}"
        if vtrim:
            vline += f" {vtrim}"
        if vmile:
            try:
                vline += f", {int(vmile):,} miles"
            except (ValueError, TypeError):
                pass
        if vfuel:
            vline += f", {vfuel}"
        if vtrans:
            vline += f", {vtrans}"
        lines.append(vline)
    if title:
        lines.append(f"Current title: {title}")
    if desc:
        lines.append(f"Current description: {desc}")
    if cond:
        cond_labels = {
            "new": "New/Unused", "like_new": "Like New", "good": "Good",
            "fair": "Fair", "for_parts": "For Parts/As-Is",
        }
        lines.append(f"Condition: {cond_labels.get(cond, cond)}")
    lines.append(f"Photos uploaded: {photo_count}")
    lines.append(f"Price set: {'yes' if has_price else 'no'}")
    lines.append(f"Location set: {'yes' if has_location else 'no'}")

    cat_list = ", ".join(_ALLOWED_CATEGORIES)
    lines.append(
        f"\n\nReturn ONLY a JSON object with exactly these keys:\n"
        f"  suggested_title (string ≤ 120 chars)\n"
        f"  suggested_description (string ≤ 800 chars, plain text, no HTML)\n"
        f"  suggested_category (one category name from: {cat_list})\n"
        f"  tips (array of up to 4 short strings — only list genuinely missing "
        f"things that would help buyers, e.g. 'Add more photos', "
        f"'Include mileage', 'Describe the condition in more detail')\n"
        f"  quality_score (integer 1–10: title 0-2, description 0-3, "
        f"photos 0-2, price 0-1, location 0-1, category 0-1)"
    )

    user_message = "\n".join(lines)

    # ── OpenAI call ───────────────────────────────────────────────────────────
    t0 = time.time()
    try:
        client   = _oai.OpenAI(api_key=api_key, timeout=_TIMEOUT_SECS)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
            max_tokens=500,
            temperature=0.3,
        )
        response_ms = int((time.time() - t0) * 1000)
        raw = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        log.warning("ai/listing_assistant: API call failed: %s", exc)
        return {"ok": False, "error": "unavailable"}

    # ── Parse and sanitise output ─────────────────────────────────────────────
    try:
        clean_raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        data = json.loads(clean_raw)
    except (ValueError, TypeError):
        log.warning("ai/listing_assistant: JSON parse failed; raw=%s", raw[:200])
        return {"ok": False, "error": "unavailable"}

    s_title = _clean_output(str(data.get("suggested_title") or ""), _MAX_TITLE_LEN)
    s_desc  = _clean_output(str(data.get("suggested_description") or ""), _MAX_DESC_LEN)
    s_cat   = _clean_output(str(data.get("suggested_category") or ""), 80)
    raw_tips = data.get("tips") or []
    tips = [_clean_output(str(t), 120) for t in raw_tips if str(t).strip()][:4]
    try:
        score = max(1, min(10, int(data.get("quality_score") or 5)))
    except (ValueError, TypeError):
        score = 5

    return {
        "ok": True,
        "title": s_title,
        "description": s_desc,
        "category": s_cat,
        "tips": tips,
        "score": score,
        "response_ms": response_ms,
    }
