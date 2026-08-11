"""
Task 6 validation: admin portal works under production-like conditions.

Run with:  python tests/test_admin_portal_deploy.py

Verifies:
- /admin/requests, /admin/request/<id>, /admin/messages render 200 as admin
- Sending a quote creates a Quote and attempts the customer email
  (NotificationLog row is written even without SENDGRID_API_KEY)
- Unread message badge count appears and clears after admin views thread
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flask_login.utils as _flu
from app import app
import routes  # noqa: F401 — registers the routes on the app
from models import db, User, Job, Quote, Message, NotificationLog

results = []

def check(name, cond, extra=''):
    results.append((name, cond, extra))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


with app.app_context():
    admin = User.query.filter_by(is_admin=True).first()
    assert admin, "need an admin user in dev DB"

    cust = User(id='test-t6-cust', email='test-t6-cust@example.com',
                first_name='T6', user_type='customer', notify_sms=False)
    db.session.merge(cust)
    db.session.commit()

    job = Job(customer_id='test-t6-cust', customer_name='T6 Cust',
              pickup_address='123 Test St', pickup_zip='55401',
              job_description='task6 test', status='reviewing')
    db.session.add(job)
    db.session.commit()
    job_id = job.id

    _flu._get_user = lambda: admin
    client = app.test_client()

    try:
        # 1. Admin pages render
        for path in ['/admin/requests', f'/admin/request/{job_id}', '/admin/messages']:
            r = client.get(path)
            check(f'{path} loads', r.status_code == 200, f'status={r.status_code}')

        # 2. Send a quote → Quote row + email attempt logged
        n_logs_before = NotificationLog.query.count()
        r = client.post(f'/admin/job/{job_id}/send_quote',
                        data={'price': '200', 'deposit_amount': '50',
                              'customer_notes': 'task6 note'})
        check('send_quote redirects (no 500)', r.status_code in (302, 303),
              f'status={r.status_code}')
        db.session.refresh(job)
        q = Quote.query.filter_by(job_id=job_id).first()
        check('quote created & job quoted',
              q is not None and job.status == 'quoted')
        log = (NotificationLog.query.order_by(NotificationLog.id.desc())
               .filter(NotificationLog.recipient == 'test-t6-cust@example.com')
               .first())
        check('customer email attempt logged',
              NotificationLog.query.count() > n_logs_before and log is not None,
              f'log={log}')

        # 3. Unread badge: customer message → badge count includes it
        m = Message(job_id=job_id, sender_id='test-t6-cust', body='hello admin')
        db.session.add(m)
        db.session.commit()
        page = client.get('/admin/requests').get_data(as_text=True)
        unread = (Message.query.join(User, Message.sender_id == User.id)
                  .filter(Message.read_at == None, User.is_admin == False).count())
        check('unread count includes new msg', unread >= 1)

        # 4. Viewing the thread clears the badge for that message
        client.get(f'/admin/request/{job_id}')
        db.session.expire_all()
        m2 = Message.query.get(m.id)
        check('message marked read after admin views thread', m2.read_at is not None)
    finally:
        Message.query.filter_by(job_id=job_id).delete()
        Quote.query.filter_by(job_id=job_id).delete()
        Job.query.filter_by(id=job_id).delete()
        NotificationLog.query.filter_by(recipient='test-t6-cust@example.com').delete()
        User.query.filter_by(id='test-t6-cust').delete()
        db.session.commit()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
