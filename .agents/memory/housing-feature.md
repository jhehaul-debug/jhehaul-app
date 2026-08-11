---
name: Housing & Real Estate feature architecture
description: How property listings (for sale / rental) are integrated into the marketplace
---

## Core design decision: listing_type column

`Listing.listing_type` is the single source of truth — `'item'` | `'property_sale'` | `'rental'`.
`Listing.is_property` (Python property) returns True when listing_type is property_sale or rental.
All branching (wizard template, field saving, card display, CTA labels, filters) keys off this.

**Why:** Avoids a parallel model tree; keeps marketplace queries simple (one Listing table, one filter).

## Category tree (max 2 levels — enforced by model)

Housing & Real Estate (slug='housing', parent_id=None)
  └─ For Sale (slug='housing-for-sale', parent_id=housing.id)
       └─ Houses for Sale, Condos, Multi-Family, Land, Commercial, Manufactured, Other (parent_id=for_sale.id)
  └─ For Rent (slug='housing-for-rent', parent_id=housing.id)
       └─ Apartments, Houses, Rooms, Commercial, Short-term (parent_id=for_rent.id)

Category nesting is max 2 levels. Do NOT add a 3rd level — the model/UI don't support it.
Property type is stored in `Listing.property_type` field, not as a 3rd category level.

## Wizard templates

- Items → `listing_wizard.html` (existing 6-step wizard)
- Properties (sale/rental) → `property_wizard.html` (new, 6-step: Photos/Details/Price/Location/Extras/Preview)

`listing_step` route uses `listing.is_property` to pick template and branch POST handlers.

## Step 2 branching in listing_step POST

- Property: saves property_type, listed_by, bedrooms, bathrooms, sqft, lot_size, year_built,
  garage_parking, hoa_fee, property_tax_annual, amenities; rental-only: rent_terms, pets_allowed, utilities_included
- Item: saves category_id, subcategory_id, condition (unchanged)

## Step 4: property_address

Properties also save `property_address` (optional, publicly visible) in step 4.

## Step 5 branching

- Property: saves open_house_dt (for sale) or available-from date (rental)
- Item: saves delivery_option checkboxes

## Marketplace filters

`?listing_type=item|property_sale|rental|housing` — filters by type
`?area=twin-cities` — filters by state=MN AND city in `_TWIN_CITIES_CITIES` frozenset (routes.py)
`?min_price`, `?max_price`, `?min_beds`, `?open_house=1` — property-specific numeric filters

Twin Cities city list is in `_TWIN_CITIES_CITIES` frozenset in routes.py. Expansion-ready: add cities there.

## No Stripe for real estate

Property listings explicitly must NOT go through Stripe/payment flow.
Disclaimer shown on property wizard step 5 and listing detail page.
CTA on listing detail changes to "Contact Owner / Request Showing" or "Contact Landlord" for properties.

## sell_choose.html

`/sell` now renders `sell_choose.html` (3 cards: Sell Item / For Sale / Rental).
Each card links to `/listing/new?type=item|property_sale|rental`.
`listing_new` reads the `?type=` param and sets `listing_type` on the draft.
