import os
from flask import Flask

app = Flask(__name__)

            # Database URL (works for DigitalOcean Postgres)
database_url = os.environ.get("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
                database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url or "sqlite:///jhe_haul.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

@app.route("/")
def home():
                return "JHE HAUL SERVER RUNNING", 200

@app.route("/health")
def health():
                return "ok", 200