# JHE Haul - Job Posting and Payment Platform

## Overview

JHE Haul is a web-based junk hauling marketplace built with Flask. Customers can post hauling jobs with photos, receive bids from haulers, and process payments through Stripe. Haulers can browse open jobs, submit bids, and view accepted job details after customers pay deposits.

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Backend Framework
- **Flask** serves as the web framework with modular file structure
- **Flask-SQLAlchemy** for ORM database operations
- **Flask-Login** for session management
- **Replit Auth** for user authentication (email/password, Google, GitHub, etc.)

### File Structure
- `app.py` - Flask app initialization and database configuration
- `models.py` - SQLAlchemy database models
- `routes.py` - All route handlers with role-based access control
- `replit_auth.py` - Authentication blueprint and helpers
- `main.py` - Application entry point
- `templates/` - Jinja2 HTML templates

### Database
- **PostgreSQL** database (Neon-backed via Replit)
- Models: User, OAuth, Job, JobPhoto, Bid, CompletionPhoto, Review
- Users have `user_type` field (customer or hauler)
- Jobs linked to customers via `customer_id`
- Bids linked to haulers via `hauler_id`
- Reviews linked to jobs, haulers, and customers
- CompletionPhotos for before/after job documentation

### Authentication
- Uses Replit Auth (OpenID Connect)
- Supports email/password, Google, GitHub, X, Apple login
- Role selection after first login (customer or hauler)
- Protected routes with `@require_role('customer')` or `@require_role('hauler')`

### File Storage
- Local file uploads stored in `uploads/` directory
- Files served via Flask's `send_from_directory`
- Photo filenames are UUID-generated for security

### Payment Processing
- **Stripe** payment links for deposit collection
- Four-tier pricing based on quote amounts:
  - Under $150 (static payment link)
  - $150-$299 (static payment link)
  - $300-$499 (static payment link)
  - $500 exactly (static payment link, $49.99 deposit)
  - Over $500: Dynamic Stripe Checkout Session
    - Fee formula: $49.99 + (quote - 500) × 0.10
    - Route: `/checkout/over500/<bid_id>` creates session, redirects to Stripe
    - Success: `/checkout/over500/success` auto-confirms payment (no manual button)
    - Uses `STRIPE_SECRET_KEY` for API calls
- Payment confirmation redirects back to app
- Address unlocks for hauler only after deposit confirmed

### Security Features
- Role-based access control (customers can't access hauler routes and vice versa)
- Job ownership verification (customers only see their own jobs)
- Pickup address hidden from haulers until deposit paid
- Session-based authentication with database storage

## External Dependencies

### Third-Party Services
| Service | Purpose | Configuration |
|---------|---------|---------------|
| Stripe | Payment processing | Static links: `PAY_LINK_UNDER_150`, `PAY_LINK_150_300`, `PAY_LINK_OVER_300`, `PAY_LINK_OVER_500`; Dynamic checkout: `STRIPE_SECRET_KEY` |
| Replit Auth | User authentication | Automatic via REPL_ID |
| SendGrid | Email notifications | Via Replit Connectors |
| Twilio | SMS notifications | Via Replit Connectors (optional) |

### Python Packages
| Package | Purpose |
|---------|---------|
| Flask | Web framework |
| Flask-SQLAlchemy | ORM |
| Flask-Login | Session management |
| Flask-Dance | OAuth integration |
| PyJWT | Token handling |
| psycopg2-binary | PostgreSQL driver |
| pgeocode | Postal code geolocation |
| stripe | Stripe API client |
| sendgrid | Email API client |
| twilio | SMS API client |

### Environment Variables Required
- `DATABASE_URL` - PostgreSQL connection string (auto-configured by Replit)
- `SESSION_SECRET` - Session encryption key (auto-configured by Replit)
- `PAY_LINK_UNDER_150` - Stripe payment link for quotes under $150
- `PAY_LINK_150_300` - Stripe payment link for quotes $150-$300
- `PAY_LINK_OVER_300` - Stripe payment link for quotes $301-$500
- `PAY_LINK_OVER_500` - Stripe payment link for quotes over $500 ($49.99)

### Email Service
- **SendGrid** integration via Replit Connectors
- `email_service.py` - Email helper functions
- Automatic notifications for:
  - Customers when they receive a new bid
  - Haulers when their bid is accepted
  - Haulers when deposit is paid (includes pickup address + directions link)
  - Haulers when a new job is posted within their travel range

### SMS Service (Optional)
- **Twilio** integration via Replit Connectors
- `sms_service.py` - SMS helper functions
- Users can opt-in to SMS notifications in their profile
- Same notification events as email, sent via text message

### Job Lifecycle Features
- **Preferred Pickup Date/Time** - Customers can specify when they want pickup
- **Job Completion** - Customers mark jobs as complete when done
- **Job Cancellation** - Customers can cancel jobs before bid acceptance
- **Before/After Photos** - Haulers can upload proof of work
- **Hauler Reviews** - Customers rate haulers after job completion (1-5 stars)
- **Earnings Dashboard** - Haulers see total earnings, job count, and average rating

### Phone Number Formatting
- All phone fields use masked input: `(###) ###-####` with placeholder `(555) 555-5555`
- Phone numbers stored as digits only (10 digits) in the database
- `User.phone_formatted` property returns display format `(XXX) XXX-XXXX`
- `strip_phone()` helper in routes.py strips formatting before saving

### ZIP Code & Distance Matching
- **ZIP Code Database**: `zip_codes` table with 1,888 MN/WI ZIP codes (lat/lon coordinates)
- **Distance Calculation**: `distance.py` uses Haversine formula for accurate mile calculations
- **Customer Job Posting**: Required `pickup_zip` field (5 digits, validated against ZIP table)
- **Hauler Profile**: `home_zip` (5 digits) + `max_travel_miles` (max distance willing to drive)
- **Matching Rule**: Haulers only see jobs where `pickup_zip` is within their `max_travel_miles` radius from `home_zip`
- **UI Display**: "Approx. X.X miles away" badge shown on job listings and bid pages
- **Privacy**: Only ZIP code shown to haulers; full address locked until deposit is paid
- **Validation**: ZIP must be exactly 5 digits; unsupported ZIPs show "ZIP not supported yet" error (no crash)
- **Notifications**: New job alerts only sent to haulers within their travel radius

### Account Management
- **Account Deletion** - Both customers and haulers can delete their accounts from the profile page
- Customers must complete or cancel all active jobs before deleting
- Haulers must complete all accepted jobs before deleting
- Deletes all associated data (jobs, bids, reviews, photos)

## Recent Changes

- **Feb 2026**: Migrated from SQLite to PostgreSQL
- **Feb 2026**: Added Replit Auth for user authentication
- **Feb 2026**: Implemented customer/hauler role system
- **Feb 2026**: Added role-based access control on all routes
- **Feb 2026**: Restructured from single main.py to modular file structure
- **Feb 2026**: Added user profile page for editing name/phone
- **Feb 2026**: Integrated SendGrid for email notifications
- **Feb 2026**: Added Google Maps directions button for haulers
- **Feb 2026**: Added hauler travel preferences (home ZIP, max miles)
- **Feb 2026**: Added job completion workflow with customer confirmation
- **Feb 2026**: Added hauler ratings and reviews system
- **Feb 2026**: Added preferred pickup date/time to job posting
- **Feb 2026**: Added earnings dashboard for haulers
- **Feb 2026**: Added before/after photos upload for haulers
- **Feb 2026**: Added SMS notifications via Twilio (optional)
- **Feb 2026**: Added job cancellation feature for customers
- **Feb 2026**: Added account deletion feature for customers and haulers
- **Feb 2026**: Added invite/share links for customers, haulers, and admins to invite new users
- **Feb 2026**: Built true mile radius matching with custom ZIP code database (1,888 MN/WI ZIPs) and Haversine formula
- **Feb 2026**: Added masked phone input (###) ###-#### across all forms with digits-only storage
- **Feb 2026**: Added 5-digit ZIP validation with "ZIP not supported yet" error for missing ZIPs
