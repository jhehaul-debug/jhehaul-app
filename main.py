import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)
STRIPE_LINK = os.environ.get("STRIPE_PAYMENT_LINK", "")
# Database file path
DB_PATH = os.path.join("data", "jhe_haul.db")

def init_db():
    # Ensure /data folder exists
    os.makedirs("data", exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            customer_name TEXT NOT NULL,
            pickup_address TEXT NOT NULL,
            job_description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        )
    """)
    conn.commit()
    conn.close()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.route("/")
def home():
    return """
    <h1>JHE Haul App</h1>
    <p>If you see this, the app is running ✅</p>
    <ul>
      <li><a href="/customer/new">Customer: Post a Job</a></li>
      <li><a href="/hauler/jobs">Hauler: View Open Jobs</a></li>
    </ul>
    """

# ✅ Show customer form
@app.route("/customer/new", methods=["GET"])
def customer_new():
    return render_template("customer_new.html")

# ✅ Handle form submit (THIS is what you were missing)
@app.route("/customer/create", methods=["POST"])
def customer_create():
    customer_name = request.form.get("customer_name", "").strip()
    pickup_address = request.form.get("pickup_address", "").strip()
    job_description = request.form.get("job_description", "").strip()

    if not customer_name or not pickup_address or not job_description:
        return "Missing required fields", 400

    conn = db()
    conn.execute(
        "INSERT INTO jobs (created_at, customer_name, pickup_address, job_description, status) VALUES (?, ?, ?, ?, 'open')",
        (datetime.utcnow().isoformat(), customer_name, pickup_address, job_description)
    )
    conn.commit()
    conn.close()

    return redirect(url_for("customer_posted"))

@app.route("/customer/posted")
def customer_posted():
    return """
    <h2>Job Posted ✅</h2>
    <p>Your job is now visible to haulers.</p>
    <p><a href="/hauler/jobs">View Open Jobs (Hauler View)</a></p>
    <p><a href="/">Back Home</a></p>
    """

# ✅ Hauler view (shows jobs)
@app.route("/hauler/jobs")
def hauler_jobs():
    conn = db()
    rows = conn.execute(
        "SELECT id, created_at, customer_name, pickup_address, job_description, status FROM jobs WHERE status='open' ORDER BY id DESC"
    ).fetchall()
    conn.close()

    if not rows:
        return """
        <h2>Hauler: Open Jobs</h2>
        <p>No open jobs yet.</p>
        <p><a href="/">Back Home</a></p>
        """

    html = "<h2>Hauler: Open Jobs</h2><ul>"
    for r in rows:
        html += f"""
        <li>
          <b>Job #{r['id']}</b><br>
          <b>Name:</b> {r['customer_name']}<br>
          <b>Pickup:</b> {r['pickup_address']}<br>
          <b>Details:</b> {r['job_description']}<br>
        </li><hr>
        """
    html += "</ul><p><a href='/'>Back Home</a></p>"
    return html

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=3000)