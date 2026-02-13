from app import app, db
from models import Job

with app.app_context():
    job = Job.query.order_by(Job.id.desc()).first()

    if not job:
        print("No jobs found")
    else:
        print("Before:", job.status)

        job.deposit_paid = True
        job.status = "completed"

        db.session.commit()

        print("After:", job.status)
        print("SUCCESS -> Job", job.id, "is now COMPLETED")