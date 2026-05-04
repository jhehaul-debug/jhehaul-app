from app import app, db
import auth
import routes
import logging

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)