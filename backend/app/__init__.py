from flask import Flask, jsonify
from flask_cors import CORS

from .config import Config
from .extensions import db, migrate
from . import models  # noqa: F401
from .api import api


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)
    migrate.init_app(app, db)
    CORS(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGIN"].split(",")}})
    app.register_blueprint(api, url_prefix="/api")
    with app.app_context():
        db.create_all()

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    return app
