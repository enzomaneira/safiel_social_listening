from flask import Flask, jsonify
from flask_cors import CORS
from sqlalchemy import text

from .config import Config
from .extensions import db, migrate
from . import models  # noqa: F401
from .api import api
from .instagram_api import ig_api


# Diferente do lock do worker (741852963), que fica preso enquanto ele roda.
CREATE_TABLES_LOCK_ID = 741852964


def create_tables():
    # Os processos do gunicorn e o worker sobem ao mesmo tempo; dois CREATE TABLE simultâneos
    # colidem no PostgreSQL (pg_type_typname_nsp_index). O lock faz um criar e os outros esperarem.
    if db.engine.dialect.name != "postgresql":
        db.create_all()
        return
    with db.engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": CREATE_TABLES_LOCK_ID})
        db.metadata.create_all(connection)


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)
    migrate.init_app(app, db)
    CORS(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGIN"].split(",")}})
    app.register_blueprint(api, url_prefix="/api")
    app.register_blueprint(ig_api, url_prefix="/api/instagram")
    with app.app_context():
        create_tables()

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    return app
