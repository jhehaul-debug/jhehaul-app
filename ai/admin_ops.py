"""JHE Haul — Phase L Admin Operations Tool Layer.

All functions return sanitized dicts safe to send to an AI model.
No secrets, passwords, API keys, or authentication tokens are ever included.
No destructive DB writes are performed here — this is a READ-ONLY analytics layer.

Every public function is admin-gated at the call site (admin_copilot.py dispatch).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("jhe.admin_ops")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _since(days: int) -> datetime:
    return _now_utc() - timedelta(days=days)


def _scalar(q) -> int:
    """Return int count from a SQLAlchemy count query."""
    try:
        return q or 0
    except Exception:
        return 0


def _fmt_dt(dt) -> Optional[str]:
    if dt is None:
        return None
    if hasattr(dt, 'strftime'):
        return dt.strftime('%Y-%m-%d %H:%M UTC')
    return str(dt)


# ---------------------------------------------------------------------------
# 1. get_marketplace_overview
# ---------------------------------------------------------------------------

def get_marketplace_overview() -> dict:
    """Full marketplace snapshot — all key metrics in one call.

    Answers: 'What happened today?', 'Give me the overview', 'What's the state of the marketplace?'
    """
    try:
        from models import (User, Listing, ListingReport, UserReport,
                            DeliveryRequest, FraudFlag, BackgroundJob,
                            NotificationLog)
        from app import db

        today = _since(1)

        # Users
        total_users        = User.query.count()
        new_users_today    = User.query.filter(User.created_at >= today).count()
        sellers_count      = User.query.filter(
            User.id.in_(db.session.query(Listing.seller_id).distinct())
        ).count()

        # Listings
        active_listings    = Listing.query.filter_by(status='active', moderation_status='approved').count()
        pending_mod        = Listing.query.filter(
            db.or_(Listing.status == 'pending', Listing.moderation_status == 'pending')
        ).count()
        new_listings_today = Listing.query.filter(Listing.created_at >= today,
                                                  Listing.status != 'draft').count()
        sold_total         = Listing.query.filter_by(status='sold').count()
        sold_today         = Listing.query.filter(Listing.status == 'sold',
                                                  Listing.sold_at >= today).count()

        # Reports
        open_listing_reports = ListingReport.query.filter_by(status='pending').count()
        try:
            open_user_reports = UserReport.query.filter_by(status='pending').count()
        except Exception:
            open_user_reports = 0
        total_open_reports = open_listing_reports + open_user_reports

        # Fraud flags
        high_risk_flags    = FraudFlag.query.filter(
            FraudFlag.risk_level.in_(['HIGH', 'CRITICAL']),
            FraudFlag.status == 'pending'
        ).count()
        pending_fraud_all  = FraudFlag.query.filter_by(status='pending').count()

        # Delivery
        pending_deliveries = DeliveryRequest.query.filter_by(status='pending').count()
        active_deliveries  = DeliveryRequest.query.filter(
            DeliveryRequest.status.in_(['quoted', 'accepted'])
        ).count()

        # Background jobs
        failed_jobs        = BackgroundJob.query.filter_by(status='FAILED').count()
        queued_jobs        = BackgroundJob.query.filter_by(status='QUEUED').count()

        # Email failures (last 24h)
        failed_emails_today = NotificationLog.query.filter(
            NotificationLog.status == 'failed',
            NotificationLog.created_at >= today,
        ).count()

        # Attention level
        attention_items = []
        if high_risk_flags > 0:
            attention_items.append({'severity': 'HIGH',    'label': f'{high_risk_flags} high/critical fraud flag(s) pending'})
        if total_open_reports > 0:
            attention_items.append({'severity': 'MEDIUM',  'label': f'{total_open_reports} unresolved report(s)'})
        if pending_mod > 0:
            attention_items.append({'severity': 'LOW',     'label': f'{pending_mod} listing(s) awaiting moderation'})
        if failed_jobs > 0:
            attention_items.append({'severity': 'MEDIUM',  'label': f'{failed_jobs} failed background job(s)'})
        if failed_emails_today > 0:
            attention_items.append({'severity': 'LOW',     'label': f'{failed_emails_today} failed email(s) in the last 24 h'})
        if pending_deliveries > 0:
            attention_items.append({'severity': 'INFO',    'label': f'{pending_deliveries} delivery request(s) awaiting quote'})

        return {
            'as_of':                _fmt_dt(_now_utc()),
            'users': {
                'total':            total_users,
                'new_today':        new_users_today,
                'with_listings':    sellers_count,
            },
            'listings': {
                'active':           active_listings,
                'pending_moderation': pending_mod,
                'new_today':        new_listings_today,
                'sold_total':       sold_total,
                'sold_today':       sold_today,
            },
            'reports': {
                'open_listing_reports': open_listing_reports,
                'open_user_reports':    open_user_reports,
                'total_open':           total_open_reports,
            },
            'fraud_safety': {
                'pending_flags':        pending_fraud_all,
                'high_critical_pending': high_risk_flags,
            },
            'delivery': {
                'pending_quote':    pending_deliveries,
                'active':           active_deliveries,
            },
            'operations': {
                'failed_jobs':      failed_jobs,
                'queued_jobs':      queued_jobs,
                'failed_emails_24h': failed_emails_today,
            },
            'attention_items':      attention_items,
            'nav_links': [
                {'label': 'Open Fraud Queue',    'url': '/admin/fraud-queue'},
                {'label': 'Open Reports',        'url': '/admin/reports'},
                {'label': 'Open Listings',       'url': '/admin/listings'},
                {'label': 'Open Deliveries',     'url': '/admin/deliveries'},
                {'label': 'Open Notifications',  'url': '/admin/notifications'},
            ],
        }
    except Exception as exc:
        log.error("get_marketplace_overview error: %s", exc)
        return {'error': 'Could not retrieve marketplace overview.'}


# ---------------------------------------------------------------------------
# 2. get_daily_marketplace_summary
# ---------------------------------------------------------------------------

def get_daily_marketplace_summary(days: int = 1) -> dict:
    """Activity totals for the last N days (1=today, 7=week, 30=month).

    Answers: 'Give me the daily summary', 'What happened this week?', 'Monthly report'
    """
    try:
        from models import (User, Listing, ListingOffer, Message,
                            DeliveryRequest, ListingReport, FraudFlag,
                            NotificationLog, BackgroundJob, ListingView)
        days = max(1, min(int(days), 90))
        since = _since(days)
        label = 'today' if days == 1 else f'last {days} days'

        new_users      = User.query.filter(User.created_at >= since).count()
        new_listings   = Listing.query.filter(Listing.created_at >= since,
                                              Listing.status != 'draft').count()
        sold_listings  = Listing.query.filter(Listing.status == 'sold',
                                              Listing.sold_at >= since).count()

        try:
            new_offers = ListingOffer.query.filter(
                ListingOffer.created_at >= since).count()
        except Exception:
            new_offers = None

        try:
            new_messages = Message.query.filter(Message.created_at >= since).count()
        except Exception:
            new_messages = None

        new_deliveries = DeliveryRequest.query.filter(
            DeliveryRequest.created_at >= since).count()
        new_reports    = ListingReport.query.filter(
            ListingReport.created_at >= since).count()

        try:
            new_flags = FraudFlag.query.filter(
                FraudFlag.created_at >= since).count()
        except Exception:
            new_flags = 0

        failed_emails  = NotificationLog.query.filter(
            NotificationLog.status == 'failed',
            NotificationLog.created_at >= since,
        ).count()

        failed_jobs    = BackgroundJob.query.filter(
            BackgroundJob.status == 'FAILED',
            BackgroundJob.created_at >= since,
        ).count()

        try:
            listing_views = ListingView.query.filter(
                ListingView.viewed_at >= since).count()
        except Exception:
            listing_views = None

        summary = {
            'period': label,
            'days': days,
            'new_users':      new_users,
            'new_listings':   new_listings,
            'sold_listings':  sold_listings,
            'new_deliveries': new_deliveries,
            'new_reports':    new_reports,
            'new_fraud_flags': new_flags,
            'failed_emails':  failed_emails,
            'failed_jobs':    failed_jobs,
        }
        if new_offers is not None:
            summary['new_offers'] = new_offers
        if new_messages is not None:
            summary['new_messages'] = new_messages
        if listing_views is not None:
            summary['listing_views'] = listing_views

        return summary
    except Exception as exc:
        log.error("get_daily_marketplace_summary error: %s", exc)
        return {'error': 'Could not retrieve daily summary.'}


# ---------------------------------------------------------------------------
# 3. get_admin_attention_items
# ---------------------------------------------------------------------------

def get_admin_attention_items() -> dict:
    """Prioritised list of things that need the admin's attention right now.

    Answers: 'What needs my attention?', 'What's urgent?', 'What should I look at?'
    """
    try:
        from models import (ListingReport, UserReport, FraudFlag,
                            BackgroundJob, NotificationLog, DeliveryRequest,
                            Listing)
        from app import db

        items = []
        today = _since(1)
        week  = _since(7)

        # CRITICAL / HIGH
        crit_flags = FraudFlag.query.filter(
            FraudFlag.risk_level == 'CRITICAL', FraudFlag.status == 'pending'
        ).count()
        if crit_flags:
            items.append({'severity': 'CRITICAL', 'count': crit_flags,
                          'label': 'Critical fraud/safety flag(s) pending review',
                          'url': '/admin/fraud-queue'})

        high_flags = FraudFlag.query.filter(
            FraudFlag.risk_level == 'HIGH', FraudFlag.status == 'pending'
        ).count()
        if high_flags:
            items.append({'severity': 'HIGH', 'count': high_flags,
                          'label': 'High-risk fraud flag(s) pending review',
                          'url': '/admin/fraud-queue'})

        # Worker failures (recent)
        job_failures = BackgroundJob.query.filter(
            BackgroundJob.status == 'FAILED',
            BackgroundJob.created_at >= week,
        ).count()
        if job_failures >= 5:
            items.append({'severity': 'HIGH', 'count': job_failures,
                          'label': f'Background job failures this week ({job_failures})',
                          'url': '/admin'})

        # MEDIUM
        open_reports = ListingReport.query.filter_by(status='pending').count()
        if open_reports:
            items.append({'severity': 'MEDIUM', 'count': open_reports,
                          'label': 'Unresolved listing report(s)',
                          'url': '/admin/reports'})

        try:
            open_user_reports = UserReport.query.filter_by(status='pending').count()
            if open_user_reports:
                items.append({'severity': 'MEDIUM', 'count': open_user_reports,
                              'label': 'Unresolved user report(s)',
                              'url': '/admin/user-reports'})
        except Exception:
            pass

        email_failures_24h = NotificationLog.query.filter(
            NotificationLog.status == 'failed',
            NotificationLog.created_at >= today,
        ).count()
        if email_failures_24h >= 3:
            items.append({'severity': 'MEDIUM', 'count': email_failures_24h,
                          'label': f'Email delivery failures in the last 24 h ({email_failures_24h})',
                          'url': '/admin/notifications'})

        med_flags = FraudFlag.query.filter(
            FraudFlag.risk_level == 'MEDIUM', FraudFlag.status == 'pending'
        ).count()
        if med_flags:
            items.append({'severity': 'MEDIUM', 'count': med_flags,
                          'label': 'Medium-risk fraud flag(s) pending review',
                          'url': '/admin/fraud-queue'})

        if 0 < job_failures < 5:
            items.append({'severity': 'MEDIUM', 'count': job_failures,
                          'label': f'Background job failure(s) this week ({job_failures})',
                          'url': '/admin'})

        # LOW
        pending_mod = Listing.query.filter(
            db.or_(Listing.status == 'pending',
                   Listing.moderation_status == 'pending')
        ).count()
        if pending_mod:
            items.append({'severity': 'LOW', 'count': pending_mod,
                          'label': 'Listing(s) awaiting moderation',
                          'url': '/admin/listings'})

        pending_deliveries = DeliveryRequest.query.filter_by(status='pending').count()
        if pending_deliveries:
            items.append({'severity': 'LOW', 'count': pending_deliveries,
                          'label': 'Delivery request(s) awaiting quote',
                          'url': '/admin/deliveries'})

        if email_failures_24h and email_failures_24h < 3:
            items.append({'severity': 'LOW', 'count': email_failures_24h,
                          'label': f'Email failure(s) in the last 24 h',
                          'url': '/admin/notifications'})

        if not items:
            items.append({'severity': 'INFO', 'count': 0,
                          'label': 'Nothing urgent right now — marketplace looks healthy.'})

        return {
            'total_items': len(items),
            'items': items,
            'as_of': _fmt_dt(_now_utc()),
        }
    except Exception as exc:
        log.error("get_admin_attention_items error: %s", exc)
        return {'error': 'Could not retrieve attention items.'}


# ---------------------------------------------------------------------------
# 4. get_new_users_summary
# ---------------------------------------------------------------------------

def get_new_users_summary(days: int = 7) -> dict:
    """User registration breakdown for the last N days.

    Answers: 'How many users joined?', 'User growth this week', 'New registrations'
    """
    try:
        from models import User, Listing
        from app import db
        days = max(1, min(int(days), 90))
        since = _since(days)

        total_users   = User.query.count()
        new_users     = User.query.filter(User.created_at >= since).count()
        suspended     = User.query.filter(User.is_suspended == True).count()

        try:
            banned = User.query.filter(User.is_banned == True).count()
        except Exception:
            banned = 0

        # Users with at least one listing (sellers)
        seller_ids = db.session.query(Listing.seller_id).distinct().all()
        sellers_total = len(seller_ids)

        # Haulers
        try:
            haulers = User.query.filter(User.hauler_status.isnot(None)).count()
        except Exception:
            haulers = 0

        # Breakdown by day for sparkline hint (last 7 days max)
        daily = []
        actual_days = min(days, 7)
        for i in range(actual_days - 1, -1, -1):
            day_start = _now_utc().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=i)
            day_end   = day_start + timedelta(days=1)
            count     = User.query.filter(User.created_at >= day_start,
                                          User.created_at < day_end).count()
            daily.append({'date': day_start.strftime('%Y-%m-%d'), 'new_users': count})

        return {
            'period': f'last {days} day(s)',
            'total_users':   total_users,
            'new_users':     new_users,
            'sellers_total': sellers_total,
            'haulers_total': haulers,
            'suspended':     suspended,
            'banned':        banned,
            'daily_trend':   daily,
        }
    except Exception as exc:
        log.error("get_new_users_summary error: %s", exc)
        return {'error': 'Could not retrieve user summary.'}


# ---------------------------------------------------------------------------
# 5. get_listing_activity_summary
# ---------------------------------------------------------------------------

def get_listing_activity_summary(days: int = 7) -> dict:
    """Listing creation, status changes, and engagement for the last N days.

    Answers: 'How many listings were posted?', 'Listing activity', 'What sold?'
    """
    try:
        from models import Listing, ListingView, ListingOffer
        days = max(1, min(int(days), 90))
        since = _since(days)

        new_listings    = Listing.query.filter(Listing.created_at >= since,
                                               Listing.status != 'draft').count()
        new_drafts      = Listing.query.filter(Listing.created_at >= since,
                                               Listing.status == 'draft').count()
        sold_period     = Listing.query.filter(Listing.status == 'sold',
                                               Listing.sold_at >= since).count()
        active_total    = Listing.query.filter_by(status='active',
                                                  moderation_status='approved').count()
        reserved_total  = Listing.query.filter_by(status='reserved').count()
        expired_period  = Listing.query.filter(Listing.status == 'expired',
                                               Listing.updated_at >= since).count()

        # Listing type breakdown
        items_active    = Listing.query.filter_by(status='active',
                                                  listing_type='item',
                                                  moderation_status='approved').count()
        vehicles_active = Listing.query.filter_by(status='active',
                                                  listing_type='vehicle',
                                                  moderation_status='approved').count()
        prop_active     = Listing.query.filter(
            Listing.status == 'active',
            Listing.moderation_status == 'approved',
            Listing.listing_type.in_(['property_sale', 'rental'])
        ).count()

        # Views (Phase K)
        try:
            total_views_period = ListingView.query.filter(
                ListingView.viewed_at >= since).count()
        except Exception:
            total_views_period = None

        # Offers
        try:
            offers_period = ListingOffer.query.filter(
                ListingOffer.created_at >= since).count()
        except Exception:
            offers_period = None

        result = {
            'period':       f'last {days} day(s)',
            'new_listings': new_listings,
            'new_drafts':   new_drafts,
            'sold':         sold_period,
            'expired':      expired_period,
            'active_total': active_total,
            'reserved':     reserved_total,
            'by_type': {
                'items':    items_active,
                'vehicles': vehicles_active,
                'property': prop_active,
            },
        }
        if total_views_period is not None:
            result['listing_views'] = total_views_period
        if offers_period is not None:
            result['offers'] = offers_period

        return result
    except Exception as exc:
        log.error("get_listing_activity_summary error: %s", exc)
        return {'error': 'Could not retrieve listing activity.'}


# ---------------------------------------------------------------------------
# 6. get_top_categories
# ---------------------------------------------------------------------------

def get_top_categories(limit: int = 8) -> dict:
    """Categories ranked by active listing count.

    Answers: 'Which categories are most active?', 'Category breakdown', 'What's selling?'
    """
    try:
        from models import Category, Listing
        from app import db
        limit = max(1, min(int(limit), 20))

        rows = (db.session.query(
                    Category.name, db.func.count(Listing.id).label('cnt')
                )
                .join(Listing, Listing.category_id == Category.id)
                .filter(Listing.status == 'active',
                        Listing.moderation_status == 'approved')
                .group_by(Category.id, Category.name)
                .order_by(db.func.count(Listing.id).desc())
                .limit(limit)
                .all())

        categories = [{'name': r.name, 'active_listings': r.cnt} for r in rows]
        total_categories = Category.query.filter_by(parent_id=None).count()

        return {
            'total_categories': total_categories,
            'top_by_active_listings': categories,
        }
    except Exception as exc:
        log.error("get_top_categories error: %s", exc)
        return {'error': 'Could not retrieve category data.'}


# ---------------------------------------------------------------------------
# 7. get_top_listings
# ---------------------------------------------------------------------------

def get_top_listings(by: str = 'views', limit: int = 8) -> dict:
    """Top listings by views or other engagement signals.

    Answers: 'Most viewed listings', 'Which listings have the most interest?',
    'Top performing listings', 'High views but no offers'
    """
    try:
        from models import Listing, ListingPhoto
        limit = max(1, min(int(limit), 20))
        by    = by if by in ('views', 'recent', 'expiring_soon') else 'views'

        q = Listing.query.filter(Listing.status == 'active',
                                 Listing.moderation_status == 'approved')

        if by == 'views':
            q = q.order_by(Listing.view_count.desc())
        elif by == 'recent':
            q = q.order_by(Listing.created_at.desc())
        elif by == 'expiring_soon':
            q = q.filter(Listing.expires_at.isnot(None),
                         Listing.expires_at > _now_utc()
                         ).order_by(Listing.expires_at.asc())

        listings = q.limit(limit).all()

        def _safe(l):
            return {
                'id':       l.id,
                'title':    l.title,
                'price':    float(l.price) if l.price else None,
                'price_type': l.price_type,
                'city':     l.city,
                'state':    l.state,
                'views':    l.view_count or 0,
                'url':      f'/listing/{l.id}',
                'listing_type': l.listing_type,
                'created_at': _fmt_dt(l.created_at),
                'expires_at': _fmt_dt(getattr(l, 'expires_at', None)),
            }

        return {
            'sorted_by': by,
            'count': len(listings),
            'listings': [_safe(l) for l in listings],
        }
    except Exception as exc:
        log.error("get_top_listings error: %s", exc)
        return {'error': 'Could not retrieve top listings.'}


# ---------------------------------------------------------------------------
# 8. get_open_reports
# ---------------------------------------------------------------------------

def get_open_reports(limit: int = 10) -> dict:
    """All unresolved listing and user reports.

    Answers: 'Show unresolved reports', 'What's been reported?', 'Open moderation items'
    """
    try:
        from models import ListingReport, Listing
        limit = max(1, min(int(limit), 30))

        reports = (ListingReport.query
                   .filter_by(status='pending')
                   .order_by(ListingReport.created_at.desc())
                   .limit(limit).all())

        def _safe_report(r):
            listing_title = None
            try:
                lst = Listing.query.get(r.listing_id)
                listing_title = lst.title if lst else None
            except Exception:
                pass
            return {
                'id':            r.id,
                'listing_id':    r.listing_id,
                'listing_title': listing_title,
                'reason':        r.reason,
                'created_at':    _fmt_dt(r.created_at),
                'url':           f'/admin/reports',
            }

        total_open = ListingReport.query.filter_by(status='pending').count()

        try:
            from models import UserReport
            user_reports_count = UserReport.query.filter_by(status='pending').count()
        except Exception:
            user_reports_count = 0

        return {
            'listing_reports_open':   total_open,
            'user_reports_open':      user_reports_count,
            'total_open':             total_open + user_reports_count,
            'recent_listing_reports': [_safe_report(r) for r in reports],
            'nav_links': [
                {'label': 'Open Reports Queue', 'url': '/admin/reports'},
                {'label': 'User Reports',       'url': '/admin/user-reports'},
            ],
        }
    except Exception as exc:
        log.error("get_open_reports error: %s", exc)
        return {'error': 'Could not retrieve reports.'}


# ---------------------------------------------------------------------------
# 9. get_safety_summary
# ---------------------------------------------------------------------------

def get_safety_summary(days: int = 7) -> dict:
    """Fraud/safety flags summary and accounts with repeated reports.

    Answers: 'Show high-risk listings', 'Summarize today's safety flags',
    'Which accounts have repeated reports?'
    """
    try:
        from models import FraudFlag, ListingReport, Listing
        from app import db
        days  = max(1, min(int(days), 90))
        since = _since(days)

        by_risk = {}
        for level in ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW'):
            by_risk[level.lower()] = FraudFlag.query.filter(
                FraudFlag.risk_level == level,
                FraudFlag.status == 'pending',
            ).count()

        new_flags_period = FraudFlag.query.filter(
            FraudFlag.created_at >= since).count()

        recent_high = (FraudFlag.query
                       .filter(FraudFlag.risk_level.in_(['HIGH', 'CRITICAL']),
                               FraudFlag.status == 'pending')
                       .order_by(FraudFlag.created_at.desc())
                       .limit(5).all())

        def _safe_flag(f):
            listing_title = None
            try:
                lst = Listing.query.get(f.listing_id) if f.listing_id else None
                listing_title = lst.title if lst else None
            except Exception:
                pass
            return {
                'id':            f.id,
                'risk_level':    f.risk_level,
                'listing_id':    f.listing_id,
                'listing_title': listing_title,
                'created_at':    _fmt_dt(f.created_at),
                'url':           f'/admin/fraud-queue',
            }

        # Accounts with 2+ reports
        repeated = (db.session.query(
                        ListingReport.reporter_id,
                        db.func.count(ListingReport.id).label('cnt')
                    )
                    .filter(ListingReport.status == 'pending')
                    .group_by(ListingReport.reporter_id)
                    .having(db.func.count(ListingReport.id) >= 2)
                    .limit(5).all())

        return {
            'period':           f'last {days} day(s)',
            'new_flags_period': new_flags_period,
            'pending_by_risk':  by_risk,
            'total_pending':    sum(by_risk.values()),
            'recent_high_risk': [_safe_flag(f) for f in recent_high],
            'accounts_with_repeated_reports': len(repeated),
            'nav_links': [
                {'label': 'Open Fraud Queue', 'url': '/admin/fraud-queue'},
                {'label': 'Open Reports',     'url': '/admin/reports'},
            ],
        }
    except Exception as exc:
        log.error("get_safety_summary error: %s", exc)
        return {'error': 'Could not retrieve safety summary.'}


# ---------------------------------------------------------------------------
# 10. get_delivery_operations_summary
# ---------------------------------------------------------------------------

def get_delivery_operations_summary(days: int = 30) -> dict:
    """Delivery requests by status and recent activity.

    Answers: 'How many deliveries are pending?', 'Delivery operations',
    'Are there any stuck deliveries?', 'Delivery requests created today'
    """
    try:
        from models import DeliveryRequest
        days  = max(1, min(int(days), 90))
        since = _since(days)

        statuses = {}
        for s in ('pending', 'quoted', 'accepted', 'declined', 'completed', 'cancelled'):
            statuses[s] = DeliveryRequest.query.filter_by(status=s).count()

        new_period    = DeliveryRequest.query.filter(
            DeliveryRequest.created_at >= since).count()

        # "Stuck" = pending for more than 3 days
        stuck_since   = _since(3)
        stuck_pending = DeliveryRequest.query.filter(
            DeliveryRequest.status == 'pending',
            DeliveryRequest.created_at <= stuck_since,
        ).count()

        return {
            'period':         f'last {days} day(s)',
            'by_status':      statuses,
            'total_active':   statuses.get('pending', 0) + statuses.get('quoted', 0) + statuses.get('accepted', 0),
            'new_period':     new_period,
            'stuck_pending':  stuck_pending,
            'note': 'stuck_pending = pending requests older than 3 days without a quote',
            'nav_links': [
                {'label': 'Open Deliveries', 'url': '/admin/deliveries'},
            ],
        }
    except Exception as exc:
        log.error("get_delivery_operations_summary error: %s", exc)
        return {'error': 'Could not retrieve delivery summary.'}


# ---------------------------------------------------------------------------
# 11. get_failed_jobs_summary
# ---------------------------------------------------------------------------

def get_failed_jobs_summary(days: int = 7) -> dict:
    """Background worker job failures and queue health.

    Answers: 'Are workers healthy?', 'How many jobs failed?',
    'What job type is failing?', 'Are jobs backed up?'
    """
    try:
        from models import BackgroundJob
        from app import db
        days  = max(1, min(int(days), 90))
        since = _since(days)

        queued      = BackgroundJob.query.filter_by(status='QUEUED').count()
        processing  = BackgroundJob.query.filter_by(status='PROCESSING').count()
        failed_all  = BackgroundJob.query.filter_by(status='FAILED').count()
        completed_p = BackgroundJob.query.filter(
            BackgroundJob.status == 'COMPLETED',
            BackgroundJob.completed_at >= since,
        ).count()

        failed_period = BackgroundJob.query.filter(
            BackgroundJob.status == 'FAILED',
            BackgroundJob.created_at >= since,
        ).count()

        # Failure breakdown by job type
        type_breakdown = (db.session.query(
                              BackgroundJob.job_type,
                              db.func.count(BackgroundJob.id).label('cnt')
                          )
                          .filter(BackgroundJob.status == 'FAILED',
                                  BackgroundJob.created_at >= since)
                          .group_by(BackgroundJob.job_type)
                          .order_by(db.func.count(BackgroundJob.id).desc())
                          .limit(8).all())

        # Recent failures (safe fields only — no payload)
        recent_failures = (BackgroundJob.query
                           .filter(BackgroundJob.status == 'FAILED',
                                   BackgroundJob.created_at >= since)
                           .order_by(BackgroundJob.created_at.desc())
                           .limit(5).all())

        def _safe_job(j):
            return {
                'job_type':       j.job_type,
                'error_category': j.error_category,
                'error_detail':   (j.error_detail or '')[:200],
                'retry_count':    j.retry_count,
                'created_at':     _fmt_dt(j.created_at),
            }

        # Health assessment
        health = 'HEALTHY'
        if failed_period >= 10:
            health = 'DEGRADED'
        elif failed_period >= 3:
            health = 'WARNING'

        return {
            'period':             f'last {days} day(s)',
            'health':             health,
            'queue': {
                'queued':         queued,
                'processing':     processing,
            },
            'failures': {
                'failed_total':   failed_all,
                'failed_period':  failed_period,
                'completed_period': completed_p,
                'by_type':        [{'job_type': r.job_type, 'count': r.cnt} for r in type_breakdown],
                'recent':         [_safe_job(j) for j in recent_failures],
            },
        }
    except Exception as exc:
        log.error("get_failed_jobs_summary error: %s", exc)
        return {'error': 'Could not retrieve job summary.'}


# ---------------------------------------------------------------------------
# 12. get_email_delivery_summary
# ---------------------------------------------------------------------------

def get_email_delivery_summary(days: int = 7) -> dict:
    """Email / notification delivery health from notification logs.

    Answers: 'How many emails failed?', 'Are notifications working?',
    'Show recent delivery failures', 'Which notification type is failing most?'
    """
    try:
        from models import NotificationLog
        from app import db
        days  = max(1, min(int(days), 90))
        since = _since(days)

        total_sent   = NotificationLog.query.filter(
            NotificationLog.created_at >= since).count()
        total_failed = NotificationLog.query.filter(
            NotificationLog.status == 'failed',
            NotificationLog.created_at >= since,
        ).count()
        total_ok     = NotificationLog.query.filter(
            NotificationLog.status.in_(['sent', 'delivered', 'ok']),
            NotificationLog.created_at >= since,
        ).count()

        delivery_rate = round(100 * total_ok / total_sent, 1) if total_sent else None

        # Failure breakdown by event type (no recipient addresses)
        type_breakdown = (db.session.query(
                              NotificationLog.event_type,
                              db.func.count(NotificationLog.id).label('cnt')
                          )
                          .filter(NotificationLog.status == 'failed',
                                  NotificationLog.created_at >= since)
                          .group_by(NotificationLog.event_type)
                          .order_by(db.func.count(NotificationLog.id).desc())
                          .limit(8).all())

        health = 'HEALTHY'
        if total_failed >= 20:
            health = 'DEGRADED'
        elif total_failed >= 5:
            health = 'WARNING'

        return {
            'period':         f'last {days} day(s)',
            'health':         health,
            'total_sent':     total_sent,
            'total_failed':   total_failed,
            'total_ok':       total_ok,
            'delivery_rate':  f'{delivery_rate}%' if delivery_rate is not None else 'n/a',
            'failures_by_type': [
                {'event_type': r.event_type, 'count': r.cnt}
                for r in type_breakdown
            ],
            'nav_links': [
                {'label': 'Open Notification Log', 'url': '/admin/notifications'},
            ],
        }
    except Exception as exc:
        log.error("get_email_delivery_summary error: %s", exc)
        return {'error': 'Could not retrieve email summary.'}


# ---------------------------------------------------------------------------
# 13. get_ai_usage_summary
# ---------------------------------------------------------------------------

def get_ai_usage_summary(days: int = 30) -> dict:
    """AI feature usage across all Phase D/E/G/I/J/K/L.

    Answers: 'How much AI has been used?', 'What AI features are being used?',
    'AI cost estimate', 'Are AI features working?'
    """
    try:
        from models import AIUsageLog
        from app import db
        days  = max(1, min(int(days), 90))
        since = _since(days)

        total     = AIUsageLog.query.filter(AIUsageLog.created_at >= since).count()
        succeeded = AIUsageLog.query.filter(AIUsageLog.success == True,
                                            AIUsageLog.created_at >= since).count()
        failed    = AIUsageLog.query.filter(AIUsageLog.success == False,
                                            AIUsageLog.created_at >= since).count()

        # By tool name
        by_tool = (db.session.query(
                       AIUsageLog.tool_name,
                       db.func.count(AIUsageLog.id).label('cnt'),
                       db.func.sum(db.case(
                           (AIUsageLog.success == True, 1), else_=0
                       )).label('ok'),
                       db.func.avg(AIUsageLog.response_ms).label('avg_ms'),
                   )
                   .filter(AIUsageLog.created_at >= since)
                   .group_by(AIUsageLog.tool_name)
                   .order_by(db.func.count(AIUsageLog.id).desc())
                   .all())

        # CopilotSession if tracked
        try:
            from models import CopilotSession
            copilot_sessions = CopilotSession.query.filter(
                CopilotSession.created_at >= since).count()
            copilot_tokens_in  = db.session.query(
                db.func.sum(CopilotSession.tokens_in)).filter(
                CopilotSession.created_at >= since).scalar() or 0
            copilot_tokens_out = db.session.query(
                db.func.sum(CopilotSession.tokens_out)).filter(
                CopilotSession.created_at >= since).scalar() or 0
        except Exception:
            copilot_sessions = copilot_tokens_in = copilot_tokens_out = None

        # Rough cost estimate: gpt-4o-mini ~$0.15/$0.60 per 1M tokens
        cost_note = None
        if copilot_tokens_in and copilot_tokens_out:
            est_usd = (copilot_tokens_in / 1_000_000 * 0.15 +
                       copilot_tokens_out / 1_000_000 * 0.60)
            cost_note = f'≈ ${est_usd:.4f} estimated for copilot (gpt-4o-mini rate, {days}d)'

        result = {
            'period':    f'last {days} day(s)',
            'total':     total,
            'succeeded': succeeded,
            'failed':    failed,
            'success_rate': f'{round(100*succeeded/total,1)}%' if total else 'n/a',
            'by_tool': [
                {
                    'tool_name':  r.tool_name,
                    'requests':   r.cnt,
                    'succeeded':  int(r.ok or 0),
                    'avg_ms':     round(float(r.avg_ms), 0) if r.avg_ms else None,
                }
                for r in by_tool
            ],
        }
        if copilot_sessions is not None:
            result['copilot'] = {
                'sessions':   copilot_sessions,
                'tokens_in':  copilot_tokens_in,
                'tokens_out': copilot_tokens_out,
            }
        if cost_note:
            result['cost_estimate_note'] = cost_note

        return result
    except Exception as exc:
        log.error("get_ai_usage_summary error: %s", exc)
        return {'error': 'Could not retrieve AI usage data.'}


# ---------------------------------------------------------------------------
# 14. get_morning_brief  (convenience aggregator)
# ---------------------------------------------------------------------------

def get_morning_brief() -> dict:
    """One-call morning operations brief combining overview + attention.

    Answers: 'Morning brief', 'What happened since yesterday?',
    'Give me the daily operations summary'
    """
    try:
        overview   = get_daily_marketplace_summary(days=1)
        attention  = get_admin_attention_items()
        week_users = get_new_users_summary(days=7)

        return {
            'brief_type': 'morning',
            'generated_at': _fmt_dt(_now_utc()),
            'since_yesterday': overview,
            'attention': attention.get('items', []),
            'user_growth_7d': {
                'new_users': week_users.get('new_users', 0),
                'total_users': week_users.get('total_users', 0),
            },
        }
    except Exception as exc:
        log.error("get_morning_brief error: %s", exc)
        return {'error': 'Could not generate morning brief.'}
