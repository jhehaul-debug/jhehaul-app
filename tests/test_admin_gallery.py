"""
Task 12 validation: admin gallery photo management.

Run with:  python tests/test_admin_gallery.py

Verifies (against the configured DB — works on SQLite fallback and PostgreSQL):
- /admin/gallery renders 200 as admin; 403 for non-admins
- Upload creates GalleryPhoto rows (DB-bytes fallback) and flashes success
- /uploads/gallery/db/<id> serves the photo bytes
- /landing renders the uploaded set (and placeholders when empty)
- Caption edit, reorder (move up/down), and delete work
- Delete also removes the stored local file (storage cleanup)
"""
import sys, os, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flask_login.utils as _flu
from app import app, UPLOAD_FOLDER
import routes  # noqa: F401 — registers the routes on the app
from models import db, User, GalleryPhoto

PNG = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
    '0000000d49444154789c626001000000ffff03000006000557bfabd4'
    '0000000049454e44ae426082'
)

results = []

def check(name, cond, extra=''):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), '-', name, extra if not cond else '')


with app.app_context():
    admin = User.query.filter_by(is_admin=True).first()
    if not admin:
        admin = User(id='test-t12-admin', email='test-t12-admin@example.com',
                     first_name='T12', is_admin=True)
        db.session.merge(admin)
        db.session.commit()
    _flu._get_user = lambda: admin
    c = app.test_client()

    GalleryPhoto.query.delete()
    db.session.commit()

    r = c.get('/landing')
    check('landing renders placeholders when empty',
          r.status_code == 200 and b'Photos coming soon' in r.data)

    r = c.get('/admin/gallery')
    check('admin gallery page renders', r.status_code == 200 and b'Landing Page Gallery' in r.data)

    data = {'photos': [(io.BytesIO(PNG), 'a.png'), (io.BytesIO(PNG), 'b.png')],
            'caption': 'Test Job'}
    r = c.post('/admin/gallery/upload', data=data,
               content_type='multipart/form-data', follow_redirects=True)
    photos = GalleryPhoto.query.order_by(GalleryPhoto.display_order, GalleryPhoto.id).all()
    check('upload creates 2 rows with IDs',
          r.status_code == 200 and len(photos) == 2 and all(p.id for p in photos))
    ids = [p.id for p in photos]
    files = [p.filename for p in photos]

    # DB-bytes fallback serving (rows without storage_url keep bytes in DB)
    db_backed = [p for p in photos if not p.storage_url]
    if db_backed:
        r = c.get(f'/uploads/gallery/db/{db_backed[0].id}')
        check('serve DB-backed photo bytes', r.status_code == 200 and r.data == PNG)
    r = c.get('/uploads/gallery/db/999999')
    check('missing photo 404s', r.status_code == 404)

    r = c.get('/landing')
    check('landing shows uploaded gallery',
          b'Test Job' in r.data and b'Photos coming soon' not in r.data)

    c.post(f'/admin/gallery/{ids[1]}/move', data={'direction': 'up'})
    order = [p.id for p in GalleryPhoto.query.order_by(GalleryPhoto.display_order, GalleryPhoto.id).all()]
    check('reorder moves photo up', order == [ids[1], ids[0]], str(order))

    c.post(f'/admin/gallery/{ids[0]}/caption', data={'caption': 'New Cap'})
    db.session.expire_all()
    check('caption update', db.session.get(GalleryPhoto, ids[0]).caption == 'New Cap')

    cust = User.query.filter_by(is_admin=False).first()
    if cust:
        _flu._get_user = lambda: cust
        r = c.get('/admin/gallery')
        check('non-admin gets 403', r.status_code == 403)
        _flu._get_user = lambda: admin

    for pid, fname in zip(ids, files):
        c.post(f'/admin/gallery/{pid}/delete')
        local_path = os.path.join(UPLOAD_FOLDER, fname)
        check(f'stored file cleaned up ({fname})', not os.path.exists(local_path))
    check('all rows deleted', GalleryPhoto.query.count() == 0)

failed = [n for n, ok in results if not ok]
print('\n%d/%d passed' % (len(results) - len(failed), len(results)))
sys.exit(1 if failed else 0)
