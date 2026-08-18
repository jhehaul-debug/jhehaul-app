"""
services/seo.py — SEO helpers for JHE Haul listing pages.

Pure functions — no DB access, no Flask imports.
Reusable by routes, templates, and future AI / semantic-search layers.
"""
import re
import unicodedata


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _slugify(text: str, max_length: int = 60) -> str:
    """Convert arbitrary text to a URL-safe ASCII slug."""
    text = str(text or "")
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "-", text.strip())
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_length].rstrip("-")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def listing_slug(listing) -> str:
    """Stable, human-readable URL slug derived from real listing fields.

    Deterministic for the same listing data — safe to use as a canonical
    path component alongside the numeric ID.
    """
    parts: list[str] = []

    vy = getattr(listing, "vehicle_year", None)
    vm = getattr(listing, "vehicle_make", None)
    vmo = getattr(listing, "vehicle_model", None)

    if vy and vm and vmo:
        parts += [str(vy), vm, vmo]
    else:
        parts.append(listing.title or "listing")

    if listing.city:
        parts.append(listing.city)
    if listing.state:
        parts.append(listing.state)

    return _slugify(" ".join(str(p) for p in parts if p))


def listing_canonical_path(listing) -> str:
    """/listing/<id>-<slug> — canonical path (no host) for a listing."""
    return f"/listing/{listing.id}-{listing_slug(listing)}"


def listing_seo_title(listing) -> str:
    """Descriptive <title> tag value for a listing detail page."""
    loc = ""
    if listing.city and listing.state:
        loc = f" in {listing.city}, {listing.state}"
    elif listing.city:
        loc = f" in {listing.city}"

    lt = (getattr(listing, "listing_type", None) or "item")

    if lt == "rental":
        return f"{listing.title or 'Rental'}{loc} | JHE Haul"

    if lt == "property_sale":
        return f"{listing.title or 'Home for Sale'}{loc} | JHE Haul"

    vy = getattr(listing, "vehicle_year", None)
    vm = getattr(listing, "vehicle_make", None)
    vmo = getattr(listing, "vehicle_model", None)
    if vy and vm and vmo:
        return f"{vy} {vm} {vmo} for Sale{loc} | JHE Haul"

    return f"{listing.title or 'Listing'}{loc} | JHE Haul"


def listing_seo_description(listing) -> str:
    """Concise meta description (≤ 155 chars) from real listing data.

    Never exposes email, phone, or private address information.
    """
    parts: list[str] = []

    vy  = getattr(listing, "vehicle_year",    None)
    vm  = getattr(listing, "vehicle_make",    None)
    vmo = getattr(listing, "vehicle_model",   None)
    vmi = getattr(listing, "vehicle_mileage", None)

    if vy and vm and vmo:
        item_desc = f"{vy} {vm} {vmo}"
        if vmi:
            item_desc += f" with {vmi:,} miles"
        parts.append(item_desc)
    else:
        parts.append(listing.title or "Item")

    price_type = getattr(listing, "price_type", None)
    price      = getattr(listing, "price",      None)
    lt         = (getattr(listing, "listing_type", None) or "item")

    if price_type == "free":
        parts.append("Free")
    elif price:
        try:
            price_str = f"${float(price):,.0f}"
            if lt == "rental":
                price_str += "/mo"
            if price_type == "negotiable":
                price_str += " OBO"
            parts.append(price_str)
        except (TypeError, ValueError):
            pass

    if listing.city and listing.state:
        parts.append(f"in {listing.city}, {listing.state}")
    elif listing.city:
        parts.append(f"in {listing.city}")

    if getattr(listing, "delivery_available", False):
        parts.append("JHE Haul delivery available")

    desc = ". ".join(parts)
    if not desc.endswith("."):
        desc += "."
    if len(desc) > 155:
        desc = desc[:152].rsplit(" ", 1)[0].rstrip(".,") + "..."
    return desc


def listing_jsonld(listing, canonical_url: str, primary_photo_url: str = None) -> dict | None:
    """schema.org JSON-LD dict for a listing detail page.

    Returns None if there is insufficient data to build valid markup.
    Never includes private seller data (email, phone, street address).
    Caller serialises with json.dumps() and injects as application/ld+json.
    """
    if not listing or not listing.title:
        return None

    # ── Offer block ──────────────────────────────────────────────────────────
    offer: dict = {
        "@type": "Offer",
        "url": canonical_url,
        "priceCurrency": "USD",
        "availability": (
            "https://schema.org/InStock"
            if getattr(listing, "status", None) == "active"
            else "https://schema.org/SoldOut"
        ),
        "seller": {"@type": "Organization", "name": "JHE Haul"},
    }
    price_type = getattr(listing, "price_type", None)
    price      = getattr(listing, "price",      None)
    if price_type == "free":
        offer["price"] = "0.00"
    elif price:
        try:
            offer["price"] = f"{float(price):.2f}"
        except (TypeError, ValueError):
            pass

    def _place(city: str, state: str) -> dict:
        return {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": city,
                "addressRegion": state,
                "addressCountry": "US",
            },
        }

    lt  = (getattr(listing, "listing_type", None) or "item")
    vy  = getattr(listing, "vehicle_year",  None)
    vm  = getattr(listing, "vehicle_make",  None)
    vmo = getattr(listing, "vehicle_model", None)

    # ── Vehicle ──────────────────────────────────────────────────────────────
    if vy and vm and vmo:
        ld: dict = {
            "@context": "https://schema.org",
            "@type": "Vehicle",
            "name": listing.title,
            "brand": {"@type": "Brand", "name": vm},
            "model": vmo,
            "vehicleModelDate": str(vy),
            "offers": offer,
        }
        vmi = getattr(listing, "vehicle_mileage", None)
        if vmi:
            ld["mileageFromOdometer"] = {
                "@type": "QuantitativeValue",
                "value": vmi,
                "unitCode": "SMI",
            }
        for attr, key in [
            ("vehicle_body_style",     "bodyType"),
            ("vehicle_transmission",   "vehicleTransmission"),
            ("vehicle_fuel_type",      "fuelType"),
            ("vehicle_exterior_color", "color"),
        ]:
            val = getattr(listing, attr, None)
            if val:
                ld[key] = val
        if primary_photo_url:
            ld["image"] = primary_photo_url
        if listing.city and listing.state:
            ld["availableAtOrFrom"] = _place(listing.city, listing.state)
        return ld

    # ── Real estate ───────────────────────────────────────────────────────────
    if lt in ("property_sale", "rental"):
        schema_type = "Residence" if lt == "rental" else "SingleFamilyResidence"
        ld = {
            "@context": "https://schema.org",
            "@type": schema_type,
            "name": listing.title,
            "offers": offer,
        }
        if listing.city and listing.state:
            ld["address"] = {
                "@type": "PostalAddress",
                "addressLocality": listing.city,
                "addressRegion": listing.state,
                "addressCountry": "US",
            }
        for attr, key in [
            ("bedrooms",  "numberOfRooms"),
            ("bathrooms", "numberOfBathroomsTotal"),
        ]:
            val = getattr(listing, attr, None)
            if val:
                ld[key] = val
        sqft = getattr(listing, "sqft", None)
        if sqft:
            ld["floorSize"] = {
                "@type": "QuantitativeValue",
                "value": sqft,
                "unitCode": "FTK",
            }
        if primary_photo_url:
            ld["image"] = primary_photo_url
        return ld

    # ── General product / item ────────────────────────────────────────────────
    ld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": listing.title,
        "offers": offer,
    }
    desc = getattr(listing, "description", None)
    if desc:
        ld["description"] = desc[:500]
    if primary_photo_url:
        ld["image"] = primary_photo_url
    if listing.city and listing.state:
        offer["availableAtOrFrom"] = _place(listing.city, listing.state)
    return ld
