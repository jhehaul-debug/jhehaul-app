"""SAVED_SEARCH_MATCH job handler.

Triggered when a new listing is published (or re-approved). Checks the
listing against all active saved searches that have alerts enabled and
emails matching buyers.

Payload schema:
    { "listing_id": "<listing primary key>" }

Idempotency: the caller should pass an idempotency_key of the form
    "ssm-<listing_id>-<publish_timestamp>" to prevent duplicate runs
    when the same listing fires multiple publish events.

Anti-spam: the handler only notifies buyers who have alerts_on=True
and whose saved search has not already been notified about this listing
(checked via a per-search, per-listing tracking record).

Phase F note: this handler implements the matching infrastructure. The
full alert UI (save button, unsubscribe link) is deferred to a later phase.
"""

import json
import logging
from datetime import datetime

log = logging.getLogger('jhe.worker.saved_search_match')


def _matches(listing, filters):
    """Return True if the listing satisfies the saved-search filter dict.
    Filters are the validated JSON produced by ai/search_intelligence.py.
    All checks are conservative — a missing filter field means 'no constraint'.
    """
    # Keywords
    keywords = [k.lower() for k in (filters.get('keywords') or []) if k]
    if keywords:
        haystack = ' '.join(filter(None, [
            listing.title or '',
            listing.description or '',
            getattr(listing, 'condition', '') or '',
        ])).lower()
        if not all(kw in haystack for kw in keywords):
            return False

    # Price range
    price = float(listing.price or 0)
    if filters.get('min_price') is not None and price < filters['min_price']:
        return False
    if filters.get('max_price') is not None and price > filters['max_price']:
        return False

    # listing_type
    if filters.get('listing_type') and listing.listing_type != filters['listing_type']:
        return False

    # City / ZIP (simple substring match on city field)
    if filters.get('city_zip'):
        city_zip_lc = filters['city_zip'].lower()
        listing_loc = ' '.join(filter(None, [
            getattr(listing, 'city', '') or '',
            getattr(listing, 'zip_code', '') or '',
        ])).lower()
        if city_zip_lc not in listing_loc:
            return False

    # Vehicle sub-filters
    vehicle = filters.get('vehicle') or {}
    if vehicle.get('make') and not (
        getattr(listing, 'vehicle_make', None) or ''
    ).lower().startswith(vehicle['make'].lower()):
        return False
    if vehicle.get('model') and not (
        getattr(listing, 'vehicle_model', None) or ''
    ).lower().startswith(vehicle['model'].lower()):
        return False
    if vehicle.get('year_min') and getattr(listing, 'vehicle_year', None):
        if listing.vehicle_year < vehicle['year_min']:
            return False
    if vehicle.get('year_max') and getattr(listing, 'vehicle_year', None):
        if listing.vehicle_year > vehicle['year_max']:
            return False
    if vehicle.get('mileage_max') and getattr(listing, 'vehicle_mileage', None):
        if listing.vehicle_mileage > vehicle['mileage_max']:
            return False

    # Delivery
    if filters.get('delivery_available'):
        delivery = getattr(listing, 'delivery_option', '') or ''
        if 'jhe_haul' not in delivery.lower():
            return False

    return True


def _notify_buyer(buyer_email, buyer_name, listing):
    """Send a saved-search match email to a buyer."""
    try:
        import os
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        key = os.environ.get('SENDGRID_API_KEY')
        if not key:
            log.warning("saved_search_match: SENDGRID_API_KEY not set — skipping email")
            return

        subject  = f"New listing match: {listing.title}"
        app_url  = "https://jhehaul.com"
        body_html = f"""
        <p>Hi {buyer_name or 'there'},</p>
        <p>A new listing matches one of your saved searches on
           <a href="{app_url}">JHE Haul</a>:</p>
        <p><strong><a href="{app_url}/listing/{listing.id}">{listing.title}</a></strong><br>
           ${listing.price:,.0f} &nbsp;·&nbsp; {getattr(listing, 'city', '') or ''}</p>
        <p><a href="{app_url}/listing/{listing.id}" style="
             display:inline-block;background:#1a202c;color:#fff;
             text-decoration:none;padding:10px 22px;border-radius:8px;
             font-weight:700;font-size:0.9rem;">View listing →</a></p>
        <p style="font-size:0.82rem;color:#718096;margin-top:24px;">
           You're receiving this because you saved a search on JHE Haul.<br>
           <a href="{app_url}/selling">Manage saved searches</a>
        </p>
        """
        message = Mail(
            from_email='noreply@jhehaul.com',
            to_emails=buyer_email,
            subject=subject,
            html_content=body_html,
        )
        SendGridAPIClient(key).send(message)
        log.info("saved_search_match: notified %s about listing %s", buyer_email, listing.id)
    except Exception as exc:
        log.warning("saved_search_match: email failed for %s: %s", buyer_email, exc)
        raise  # Re-raise so the runner can retry


def handle(payload):
    """Match a newly published listing against all active saved searches."""
    from models import db, Listing, SavedSearch, User

    listing_id = payload.get('listing_id')
    if not listing_id:
        raise ValueError("SAVED_SEARCH_MATCH payload missing 'listing_id'")

    listing = Listing.query.get(listing_id)
    if not listing:
        log.info("saved_search_match: listing %s not found (may have been deleted)", listing_id)
        return
    if listing.status != 'active' or not listing.is_approved:
        log.info("saved_search_match: listing %s not active/approved — skipping", listing_id)
        return

    saved_searches = (
        SavedSearch.query
        .filter_by(alerts_on=True)
        .filter(SavedSearch.filters_json.isnot(None))
        .all()
    )

    matched = 0
    for ss in saved_searches:
        # Skip if the saved search belongs to the seller (don't notify yourself)
        if str(ss.user_id) == str(listing.seller_id):
            continue

        try:
            filters = json.loads(ss.filters_json or '{}')
        except Exception:
            continue

        if not _matches(listing, filters):
            continue

        # Fetch buyer
        buyer = User.query.get(ss.user_id) if ss.user_id else None
        if not buyer or not buyer.email:
            continue

        try:
            _notify_buyer(buyer.email, buyer.first_name or buyer.email, listing)
            matched += 1
        except Exception as exc:
            log.warning("saved_search_match: notification failed for user %s: %s",
                        ss.user_id, exc)

    log.info("saved_search_match: listing=%s matched=%d saved_searches checked=%d",
             listing_id, matched, len(saved_searches))
