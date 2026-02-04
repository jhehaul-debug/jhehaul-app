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
- Three-tier pricing based on quote amounts:
  - Under $150
  - $150-$300
  - Over $300
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
| Stripe | Payment processing | Environment variables: `PAY_LINK_UNDER_150`, `PAY_LINK_150_300`, `PAY_LINK_OVER_300` |
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
- `PAY_LINK_OVER_300` - Stripe payment link for quotes over $300

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
