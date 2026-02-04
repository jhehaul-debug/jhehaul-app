# JHE Haul - Job Posting and Payment Platform

## Overview

JHE Haul is a web-based job posting and payment platform built with Flask. The application allows customers to post hauling jobs, receive quotes, and process payments through Stripe. It features a multi-tier pricing structure based on quote amounts and manages job workflows from submission through payment.

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Backend Framework
- **Flask** serves as the web framework, handling routing, form submissions, and template rendering
- Uses Werkzeug utilities for secure file handling

### Database
- **SQLite** database stored in `data/jhe_haul.db`
- Uses Python's built-in `sqlite3` module with row factory for dict-like row access
- Database is auto-initialized with required tables on startup

### File Storage
- Local file uploads stored in `uploads/` directory
- Files served via Flask's `send_from_directory` for uploaded content

### Payment Processing
- **Stripe** integration for payment handling
- Three-tier payment link system based on quote amounts:
  - Under $150
  - $150-$300
  - Over $300
- Payment links stored in environment variables (Replit Secrets)

### Geolocation
- **pgeocode** library included for postal code/geographic functionality (likely for distance calculations or service area verification)

### Template System
- Jinja2 templates (Flask default) for HTML rendering
- Templates stored in `templates/` directory

## External Dependencies

### Third-Party Services
| Service | Purpose | Configuration |
|---------|---------|---------------|
| Stripe | Payment processing | Environment variables: `PAY_LINK_UNDER_150`, `PAY_LINK_150_300`, `PAY_LINK_OVER_300` |

### Python Packages
| Package | Purpose |
|---------|---------|
| Flask | Web framework |
| pgeocode | Postal code geolocation |
| stripe | Stripe API client |

### Environment Variables Required
- `PAY_LINK_UNDER_150` - Stripe payment link for quotes under $150
- `PAY_LINK_150_300` - Stripe payment link for quotes $150-$300
- `PAY_LINK_OVER_300` - Stripe payment link for quotes over $300