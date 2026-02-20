            import os
            import logging

            from flask import Flask
            from werkzeug.middleware.proxy_fix import ProxyFix
            from flask_sqlalchemy import SQLAlchemy

            app = Flask(__name__)

            # Secret key (give DO an env var SESSION_SECRET later)
            app.secret_key = os.environ.get("SESSION_SECRET", "dev-secret")

            # So Flask works correctly behind DigitalOcean proxy
            app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

            # Database (TEMP safe default for App Platform)
            # App Platform filesystem is not reliably writable except /tmp unless you add a DB or persistent storage.
            db_url = os.environ.get("DATABASE_URL", "sqlite:////tmp/jhe_haul.db")

            # Fix old postgres:// URLs if you ever use Postgres
            if db_url.startswith("postgres://"):
                db_url = db_url.replace("postgres://", "postgresql://", 1)

            app.config["SQLALCHEMY_DATABASE_URI"] = db_url
            app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

            db = SQLAlchemy(app)

            @app.get("/")
            def home():
                return "JHE HAUL SERVER RUNNING"

            @app.get("/health")
            def health():
                return "ok", 200


            # Create tables (only for SQLite/testing)
            with app.app_context():
                db.create_all()