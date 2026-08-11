"""
Targeted route tests for the admin appointment-scheduling feature.

Run with:  python tests/test_schedule_appointment.py

Uses the real app + dev database; creates temporary rows and cleans them up.
Authentication is bypassed by patching flask_login's current-user lookup.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flask_login.utils as _flu
from app import app
import routes  # noqa: F401 — registers the routes on the app
from models import db, User, Job, Quote

results = []

def check(name, cond, extra=''):
    results.append((name, cond, extra))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


with app.app_context():
    # -- fixtures ------------------------------------------------------------
    admin = User.query.filter_by(is_admin=True).first()
    assert admin, "need an admin user in dev DB"

    cust = User(id='test-sched-cust', email='test-sched-cust@example.com',
                first_name='Test', user_type='customer', notify_sms=False)
    db.session.merge(cust)
    db.session.commit()

    def make_job(status, deposit_paid=False, accepted_quote=False):
        j = Job(customer_id='test-sched-cust', customer_name='Test Cust',
                pickup_address='123 Test St', pickup_zip='55401',
                job_description='test', status=status, deposit_paid=deposit_paid)
        db.session.add(j)
        db.session.commit()
        if accepted_quote:
            q = Quote(job_id=j.id, price=100, deposit_amount=25, status='accepted')
            db.session.add(q)
            db.session.commit()
        return j

    _flu._get_user = lambda: admin  # bypass login as admin
    client = app.test_client()

    job_ids = []
    try:
        # 1. Missing time is rejected
        j = make_job('deposit_paid', deposit_paid=True, accepted_quote=True); job_ids.append(j.id)
        client.post(f'/admin/request/{j.id}/schedule',
                    data={'scheduled_date': '2026-09-01', 'scheduled_time': ''})
        db.session.refresh(j)
        check('missing time rejected', j.scheduled_date is None and j.status == 'deposit_paid')

        # 2. Unpaid / no accepted quote is rejected
        j2 = make_job('quoted'); job_ids.append(j2.id)
        client.post(f'/admin/request/{j2.id}/schedule',
                    data={'scheduled_date': '2026-09-01', 'scheduled_time': '14:00'})
        db.session.refresh(j2)
        check('unpaid job cannot be scheduled', j2.status == 'quoted' and j2.scheduled_date is None)

        # 3. Happy path: paid + accepted quote → scheduled with date/time stored
        j3 = make_job('deposit_paid', deposit_paid=True, accepted_quote=True); job_ids.append(j3.id)
        client.post(f'/admin/request/{j3.id}/schedule',
                    data={'scheduled_date': '2026-09-01', 'scheduled_time': '14:00'})
        db.session.refresh(j3)
        check('paid job becomes scheduled',
              j3.status == 'scheduled' and j3.scheduled_date == '2026-09-01' and j3.scheduled_time == '14:00')

        # 4. In-progress job keeps status but stores appointment; customer page shows it
        j4 = make_job('in_progress', deposit_paid=True, accepted_quote=True); job_ids.append(j4.id)
        client.post(f'/admin/request/{j4.id}/schedule',
                    data={'scheduled_date': '2026-09-02', 'scheduled_time': '09:30'})
        db.session.refresh(j4)
        check('in-progress keeps status, stores appointment',
              j4.status == 'in_progress' and j4.scheduled_date == '2026-09-02')
        page = client.get(f'/customer/request/{j4.id}').get_data(as_text=True)
        check('customer page shows appointment for in-progress job',
              'September 02, 2026' in page and '9:30 AM' in page)

        # 5. Customer page shows prominent confirmation for scheduled job
        page3 = client.get(f'/customer/request/{j3.id}').get_data(as_text=True)
        check('customer page shows confirmed appointment banner',
              'Your appointment is confirmed' in page3 and 'September 01, 2026' in page3 and '2:00 PM' in page3)

        # 6. Completed job cannot be scheduled
        j5 = make_job('completed', deposit_paid=True); job_ids.append(j5.id)
        client.post(f'/admin/request/{j5.id}/schedule',
                    data={'scheduled_date': '2026-09-01', 'scheduled_time': '14:00'})
        db.session.refresh(j5)
        check('completed job cannot be scheduled', j5.status == 'completed' and j5.scheduled_date is None)
    finally:
        for jid in job_ids:
            j = Job.query.get(jid)
            if j:
                db.session.delete(j)
        u = User.query.get('test-sched-cust')
        if u:
            db.session.delete(u)
        db.session.commit()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
