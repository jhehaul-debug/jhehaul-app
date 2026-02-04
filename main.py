from werkzeug.utils import secure_filename
import uuid
import json
import os
import sqlite3
from datetime import datetime
from flask import Flask, request, redirect, url_for

app = Flask(__name__)

DB_PATH = os.path.join("data", "jhe_haul.db")

        # Pull Stripe links from Replit Secrets
PAY_LINK_UNDER_150 = os.environ.get("PAY_LINK_UNDER_150", "")
PAY_LINK_150_300   = os.environ.get("PAY_LINK_150_300", "")
PAY_LINK_OVER_300  = os.environ.get("PAY_LINK_OVER_300", "")

def choose_pay_link(accepted_quote):
            try:
                q = float(accepted_quote or 0)
            except:
                q = 0

            if q < 150:
                return PAY_LINK_UNDER_150
            elif q <= 300:
                return PAY_LINK_150_300
            else:
                return PAY_LINK_OVER_300
def db():
        os.makedirs("data", exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


def init_db():
        conn = db()

        # Jobs table (includes hidden address until deposit paid)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                customer_phone TEXT,
                pickup_address TEXT NOT NULL,
                job_description TEXT NOT NULL,

                status TEXT NOT NULL DEFAULT 'open',     -- open | bidding | accepted | deposit_paid | completed
                accepted_hauler TEXT,                    -- hauler name
                accepted_quote REAL,                     -- accepted quote
                deposit_paid INTEGER NOT NULL DEFAULT 0  -- 0/1
            )
        """)

        # Bids table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                job_id INTEGER NOT NULL,
                hauler_name TEXT NOT NULL,
                hauler_phone TEXT,
                quote_amount REAL NOT NULL,
                message TEXT,
                status TEXT NOT NULL DEFAULT 'active',   -- active | accepted | rejected
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
        """)
    # Job photos table (Number 6)
    
        conn.commit()
        conn.close()


@app.route("/")
def home():
        return """
        <h1>JHE Haul App</h1>
        <p>If you see this, the app is running ✅</p>
        <ul>
          <li><a href="/customer/new">Customer: Post a Job</a></li>
          <li><a href="/customer/jobs">Customer: My Jobs (View Bids)</a></li>
          <li><a href="/hauler/jobs">Hauler: View Open Jobs</a></li>
          <li><a href="/hauler/dashboard">Hauler: Dashboard</a></li>
        </ul>
        """


    # -------------------------
    # CUSTOMER: POST JOB
    # -------------------------
@app.route("/customer/new", methods=["GET"])
def customer_new():
        return """
        <h2>Customer: Post a Job</h2>

        <form method="POST"
              action="/customer/create"
              enctype="multipart/form-data">

            <label>Your Name</label><br>
            <input name="customer_name" required><br><br>

            <label>Your Phone (optional)</label><br>
            <input name="customer_phone"><br><br>

            <label>Pickup Address</label><br>
            <input name="pickup_address" required><br><br>

            <label>What needs hauled?</label><br>
            <textarea name="job_description" required></textarea><br><br>

            <!-- ✅ FILE UPLOAD MUST BE HERE -->
            <label>Upload Photos</label><br>
            <input type="file" name="photos" multiple accept="image/*"><br><br>

            <button type="submit">Submit Job</button>
        </form>

        <p><a href="/">Back Home</a></p>
        """


@app.route("/customer/create", methods=["POST"])
def customer_create():
        customer_name = request.form.get("customer_name", "").strip()
        customer_phone = request.form.get("customer_phone", "").strip()
        pickup_address = request.form.get("pickup_address", "").strip()
        job_description = request.form.get("job_description", "").strip()

        if not customer_name or not pickup_address or not job_description:
            return "Missing required fields", 400

        conn = db()
        conn.execute("""
            INSERT INTO jobs (created_at, customer_name, customer_phone, pickup_address, job_description, status)
            VALUES (?, ?, ?, ?, ?, 'open')
        """, (datetime.utcnow().isoformat(), customer_name, customer_phone, pickup_address, job_description))
        conn.commit()
        conn.close()

        return redirect(url_for("customer_jobs"))


@app.route("/customer/jobs")
def customer_jobs():
        conn = db()
        jobs = conn.execute("""
            SELECT id, created_at, customer_name, job_description, status, deposit_paid, accepted_hauler, accepted_quote
            FROM jobs
            ORDER BY id DESC
        """).fetchall()
        conn.close()

        html = "<h2>Customer: My Jobs</h2>"
        if not jobs:
            html += "<p>No jobs yet.</p><p><a href='/'>Back Home</a></p>"
            return html

        for j in jobs:
            html += f"""
            <hr>
            <h3>Job #{j['id']}</h3>
            <p><b>Status:</b> {j['status']}</p>
            <p><b>Description:</b><br>{j['job_description']}</p>
            <p><a href="/customer/job/{j['id']}">View Bids / Accept</a></p>
            """

        html += "<hr><p><a href='/'>Back Home</a></p>"
        return html


@app.route("/customer/job/<int:job_id>")
def customer_job_detail(job_id):
        conn = db()
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        bids = conn.execute("""
            SELECT * FROM bids
            WHERE job_id=?
            ORDER BY quote_amount ASC
        """, (job_id,)).fetchall()
        conn.close()

        if not job:
            return "Job not found", 404

        html = f"""
        <h2>Customer: Job #{job['id']}</h2>
        <p><b>Status:</b> {job['status']}</p>
        <p><b>Description:</b><br>{job['job_description']}</p>
        <p><b>Address:</b> (hidden from haulers until deposit is paid)</p>
        <hr>
        <h3>Bids</h3>
        """

        if not bids:
            html += "<p>No bids yet.</p>"
        else:
            for b in bids:
                html += f"""
                <div style="border:1px solid #ddd; padding:10px; margin-bottom:10px;">
                  <p><b>Hauler:</b> {b['hauler_name']} ({b['hauler_phone'] or 'no phone'})</p>
                  <p><b>Quote:</b> ${b['quote_amount']:.2f}</p>
                  <p><b>Message:</b> {b['message'] or ''}</p>
                """

                # Only allow accepting if job not already accepted
                if job["status"] in ("open", "bidding"):
                    html += f"""
                      <form method="POST" action="/customer/accept_bid/{b['id']}">
                        <button type="submit">Accept This Bid</button>
                      </form>
                    """

                html += "</div>"

        # If accepted, show pay deposit link
                # If accepted, show pay deposit link
                if job["status"] == "accepted" and not job["deposit_paid"]:
                    pay_link = pay_link = choose_pay_link(job["accepted_quote"])

                    if pay_link:
                        html += f"""
                            <hr>
                            <h3>Deposit Payment</h3>
                            <p>You accepted a hauler. Next step: pay the deposit to unlock the address for the hauler.</p>
                            <p><a href="{pay_link}" target="_blank">Pay Deposit (Stripe)</a></p>

                            <p>After you pay, click:</p>
                            <form method="POST" action="/customer/mark_paid/{job['id']}">
                                <button type="submit">I Paid the Deposit ✅</button>
                            </form>
                        """
                    else:
                        html += """
                            <hr>
                            <h3>Deposit Payment</h3>
                            <p><b>Error:</b> Payment link missing in Secrets.</p>
                        """
            else:
                html += """
                <hr>
                <h3>Deposit Payment</h3>
                <p><b>Missing STRIPE_PAYMENT_LINK</b> in Replit Secrets.</p>
                """

        if job["deposit_paid"]:
            html += """
            <hr>
            <p><b>Deposit Paid ✅</b> Address is now unlocked for the hauler.</p>
            """

        html += "<hr><p><a href='/customer/jobs'>Back to My Jobs</a> | <a href='/'>Back Home</a></p>"
        return html


@app.route("/customer/accept_bid/<int:bid_id>", methods=["POST"])
def customer_accept_bid(bid_id):
        conn = db()
        bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
        if not bid:
            conn.close()
            return "Bid not found", 404

        job_id = bid["job_id"]

        # Mark job accepted
        conn.execute("""
            UPDATE jobs
            SET status='accepted',
                accepted_hauler=?,
                accepted_quote=?
            WHERE id=?
        """, (bid["hauler_name"], bid["quote_amount"], job_id))

        # Mark this bid accepted, others rejected
        conn.execute("UPDATE bids SET status='accepted' WHERE id=?", (bid_id,))
        conn.execute("UPDATE bids SET status='rejected' WHERE job_id=? AND id<>?", (job_id, bid_id))

        conn.commit()
        conn.close()

        return redirect(f"/customer/job/{job_id}")


@app.route("/customer/mark_paid/<int:job_id>", methods=["POST"])
def customer_mark_paid(job_id):
        conn = db()
        conn.execute("UPDATE jobs SET deposit_paid=1, status='deposit_paid' WHERE id=?", (job_id,))
        conn.commit()
        conn.close()
        return redirect(f"/customer/job/{job_id}")


    # -------------------------
    # HAULER: VIEW JOBS (NO ADDRESS)
    # -------------------------
from flask import send_from_directory, request

UPLOAD_FOLDER = "uploads"  # make sure /uploads folder exists

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route("/hauler/bid/<int:job_id>", methods=["GET"])
def hauler_bid_form(job_id):
    conn = db()
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    photos = conn.execute(
        "SELECT filename FROM job_photos WHERE job_id=? ORDER BY id DESC",
        (job_id,)
    ).fetchall()
    conn.close()

    if not job:
        return "<h2>Job not found</h2><p><a href='/hauler/jobs'>Back to Jobs</a></p>", 404

    html = f"<h2>Hauler: Bid on Job #{job_id}</h2>"
    html += "<h3>Job Photos</h3>"

    if not photos:
        html += "<p>No photos uploaded.</p>"
    else:
        for row in photos:
            filename = row["filename"] if hasattr(row, "keys") else row[0]
            html += f"<img src='/uploads/{filename}' style='max-width:300px;margin:10px;border:1px solid #ccc;'>"
    html += f"""
        <hr>
        <form method="POST" action="/hauler/bid/{job_id}">
            <label>Your Name</label><br>
            <input name="hauler_name" required><br><br>

            <label>Your Phone (optional)</label><br>
            <input name="hauler_phone"><br><br>

            <label>Quote Amount ($)</label><br>
            <input name="quote_amount" type="number" step="0.01" required><br><br>

            <label>Message (optional)</label><br>
            <textarea name="message" style="width:95%;height:90px;"></textarea><br><br>

            <button type="submit">Submit Bid</button>
        </form>

        <p><a href="/hauler/jobs">Back to Jobs</a> | <a href="/">Back Home</a></p>
    """
    return html


@app.route("/hauler/bid/<int:job_id>", methods=["POST"])
def hauler_bid_submit_post(job_id):
    hauler_name = request.form.get("hauler_name", "").strip()
    hauler_phone = request.form.get("hauler_phone", "").strip()
    quote_amount = request.form.get("quote_amount", "").strip()
    message = request.form.get("message", "").strip()

    if not hauler_name or not quote_amount:
        return "Missing required fields", 400

    try:
        quote_amount = float(quote_amount)
    except ValueError:
        return "Invalid quote amount", 400

    conn = db()

    # set job to bidding (only if currently open)
    conn.execute(
        "UPDATE jobs SET status='bidding' WHERE id=? AND status='open'",
        (job_id,)
    )

    conn.execute(
        """
        INSERT INTO bids (created_at, job_id, hauler_name, hauler_phone, quote_amount, message, status)
        VALUES (?, ?, ?, ?, ?, ?, 'active')
        """,
        (datetime.utcnow().isoformat(), job_id, hauler_name, hauler_phone, quote_amount, message)
    )

    conn.commit()
    conn.close()

    return """
        <h2>Bid Submitted ✅</h2>
        <p>Your bid was sent to the customer.</p>
        <p><a href="/hauler/jobs">Back to Jobs</a> | <a href="/">Back Home</a></p>
    """

        # Bid form
    html += f"""
        <hr>
        <form method="POST" action="/hauler/bid/{job_id}">
            <label>Your Name</label><br>
            <input name="hauler_name" required><br><br>

            <label>Your Phone (optional)</label><br>
            <input name="hauler_phone"><br><br>

            <label>Quote Amount ($)</label><br>
            <input name="quote_amount" type="number" step="0.01" required><br><br>

            <label>Message (optional)</label><br>
            <textarea name="message" style="width:95%; height:90px;"></textarea><br><br>

            <button type="submit">Submit Bid</button>
        </form>

        <p><a href="/hauler/jobs">Back to Jobs</a> | <a href="/">Back Home</a></p>
        """

    return html


@app.route("/hauler/bid/<int:job_id>", methods=["POST"])
def hauler_bid_submit(job_id):
        hauler_name = request.form.get("hauler_name", "").strip()
        hauler_phone = request.form.get("hauler_phone", "").strip()
        quote_amount = request.form.get("quote_amount", "").strip()
        message = request.form.get("message", "").strip()

        if not hauler_name or not quote_amount:
            return "Missing required fields", 400

        try:
            quote_amount = float(quote_amount)
        except ValueError:
            return "Invalid quote amount", 400

        conn = db()

        # Move job to bidding once bids start
        conn.execute("UPDATE jobs SET status='bidding' WHERE id=? AND status='open'", (job_id,))

        conn.execute("""
            INSERT INTO bids (created_at, job_id, hauler_name, hauler_phone, quote_amount, message, status)
            VALUES (?, ?, ?, ?, ?, ?, 'active')
        """, (datetime.utcnow().isoformat(), job_id, hauler_name, hauler_phone, quote_amount, message))

        conn.commit()
        conn.close()

        return """
        <h2>Bid Submitted ✅</h2>
        <p>Your bid was sent to the customer. If they accept and pay deposit, you’ll get the address.</p>
        <p><a href="/hauler/jobs">Back to Jobs</a> | <a href="/">Back Home</a></p>
        """


# -------------------------
# HAULER: DASHBOARD (shows accepted  +   address only after deposit)
# -------------------------
@app.route("/hauler/dashboard")
def hauler_dashboard():
    conn = db()
    jobs = conn.execute("""
            SELECT id, status, accepted_hauler, accepted_quote, deposit_paid, pickup_address, job_description
            FROM jobs
            WHERE status IN ('accepted','deposit_paid','completed')
            ORDER BY id DESC
        """).fetchall()
    conn.close()

    html = "<h2>Hauler: Dashboard</h2>"
    if not jobs:
            html += "<p>No accepted jobs yet.</p><p><a href='/'>Back Home</a></p>"
            return html

    html += "<p>Note: Address unlocks only after deposit is paid.</p>"

    for j in jobs:
            html += f"<hr><h3>Job #{j['id']}</h3>"
            html += f"<p><b>Status:</b> {j['status']}</p>"
            html += f"<p><b>Accepted Hauler:</b> {j['accepted_hauler'] or '(not set)'}</p>"
            html += f"<p><b>Quote:</b> {('$' + format(j['accepted_quote'], '.2f')) if j['accepted_quote'] else ''}</p>"
            html += f"<p><b>Description:</b><br>{j['job_description']}</p>"

            if j["deposit_paid"]:
                html += f"<p><b>Pickup Address (UNLOCKED ✅):</b><br>{j['pickup_address']}</p>"
            else:
                html += "<p><b>Pickup Address:</b> LOCKED (waiting for deposit)</p>"

    html += "<hr><p><a href='/'>Back Home</a></p>"
    return html


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)