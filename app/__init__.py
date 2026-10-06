"""ProxySQL Web UI - Flask application factory."""
import datetime
import os

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from . import db
from .auth import bp as auth_bp, csrf_protect
from .api import bp as api_bp
from .config import Config
from .proxysql import ProxySQLError

__version__ = "1.0.0"


def create_app(config_path=None):
    cfg = Config(config_path)
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")
    app.config.update(
        WEBUI=cfg,
        SECRET_KEY=cfg.secret_key,
        SESSION_COOKIE_NAME="psqlwebui",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=cfg.secure_cookies,
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(minutes=cfg.session_minutes),
        JSON_SORT_KEYS=False,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    app.json.sort_keys = False
    if cfg.behind_proxy:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.init_db(cfg.db_path)

    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)
    app.teardown_appcontext(db.close_db)

    @app.before_request
    def _csrf():
        if request.path.startswith("/api/"):
            return csrf_protect()
        return None

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(ProxySQLError)
    def _proxysql_error(e):
        return jsonify(error=str(e)), 502

    @app.errorhandler(ValueError)
    def _value_error(e):
        return jsonify(error=str(e)), 400

    @app.errorhandler(HTTPException)
    def _http_error(e):
        if request.path.startswith("/api/"):
            return jsonify(error=e.description), e.code
        return e

    @app.route("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.route("/healthz")
    def healthz():
        return jsonify(ok=True, version=__version__)

    return app
