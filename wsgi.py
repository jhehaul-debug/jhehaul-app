from app import app, db
import auth    # noqa: F401 - registers Google/GitHub OAuth blueprints
import routes  # noqa: F401 - registers all routes with the app

application = app
