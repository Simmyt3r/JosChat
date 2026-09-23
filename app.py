"""
app.py
------
Joschat Flask API — a stateless REST backend deployable as-is on Vercel
(see api/index.py + vercel.json) or run locally with `python app.py`.

Real-time message delivery and WebRTC call signalling are NOT handled
here (see routes/messages.py and routes/calls.py docstrings for why) —
they happen client-side via Supabase Realtime, which is what lets this
API stay fully stateless and serverless-friendly.
"""

import psycopg2
import psycopg2.errors
from flask import Flask, jsonify
from flask_cors import CORS

from config import Config
from extensions import init_cloudinary, get_supabase_auth

from routes.auth import auth_bp
from routes.conversations import conversations_bp
from routes.messages import messages_bp
from routes.media import media_bp
from routes.admin import admin_bp
from routes.calls import calls_bp


def create_app():
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object(Config)

    # Don't let a missing env var crash the entire serverless function at
    # import time — that takes down every route, including /api/health,
    # which makes debugging a deploy much harder than it needs to be (as
    # in: every single request just says "Python process exited with
    # exit status 1" with no indication of which variable is missing).
    # Instead, validate once, remember the result, and surface a clear
    # error on real requests while keeping /api/health always reachable.
    config_error = None
    try:
        Config.validate()
        # Build the auth client without making a network request. Real key
        # validation happens on the first Auth call, keeping cold starts cheap.
        get_supabase_auth()
    except Exception as exc:
        config_error = f"{type(exc).__name__}: {exc}"
        app.logger.error(f"Configuration error: {config_error}")

    CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

    if config_error is None:
        init_cloudinary()

        app.register_blueprint(auth_bp, url_prefix="/api/auth")
        app.register_blueprint(conversations_bp, url_prefix="/api/conversations")
        app.register_blueprint(messages_bp, url_prefix="/api/messages")
        app.register_blueprint(media_bp, url_prefix="/api/media")
        app.register_blueprint(admin_bp, url_prefix="/api/admin")
        app.register_blueprint(calls_bp, url_prefix="/api/calls")
    else:
        @app.before_request
        def _block_until_configured():
            from flask import request as _req
            if _req.path == "/api/health":
                return None
            return jsonify({
                "error": "Server misconfigured — missing environment variables.",
                "detail": config_error,
            }), 500

    @app.route("/api/health", methods=["GET"])
    def health():
        if config_error:
            return jsonify({"status": "misconfigured", "detail": config_error}), 500
        return jsonify({"status": "ok", "service": "joschat-api"}), 200

    @app.route("/api/config", methods=["GET"])
    def public_config():
        # The anon key is safe to expose to the browser: every table it can
        # touch is protected by the Row Level Security policies defined in
        # supabase/schema.sql. The frontend needs this to open its own
        # direct Supabase Realtime subscription for live messages/calls.
        return jsonify({
            "supabase_url": Config.SUPABASE_URL,
            "supabase_anon_key": Config.SUPABASE_ANON_KEY,
            # Lets the client confirm a decrypted media URL actually points at this
            # app's own Cloudinary storage before it is fetched/rendered, now that
            # the server can no longer check the (encrypted) media reference itself.
            "cloudinary_cloud_name": Config.CLOUDINARY_CLOUD_NAME,
        }), 200

    @app.route("/", methods=["GET"])
    def index():
        from flask import render_template
        return render_template("index.html")

    @app.route("/sw.js", methods=["GET"])
    def service_worker():
        # A service worker can only control pages at or below its own URL, so
        # it has to be served from the site root ("/sw.js") — from
        # /static/sw.js it could never control the app at "/". It must also
        # never be cached by the browser/CDN, or updates would not roll out.
        from flask import send_from_directory
        resp = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    @app.errorhandler(404)
    def not_found(_e):
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({"error": "File too large"}), 413

    @app.errorhandler(psycopg2.errors.UndefinedTable)
    def db_schema_missing(e):
        # The most common cause: POSTGRES_URL points at a database that
        # exists, but supabase/schema.sql was never run against it (or was
        # run against a different project than the one these credentials
        # point to). Surface that clearly instead of a bare 500 — this is
        # a setup problem, not a bug to chase in application code.
        app.logger.exception(e)
        payload = {"error": "Database schema not initialized."}
        if Config.DEBUG:
            payload["detail"] = (
                "A query referenced a table that doesn't exist yet "
                f"({e}). Run supabase/schema.sql in your Supabase project's "
                "SQL editor (Project -> SQL Editor -> New query), and make "
                "sure POSTGRES_URL / POSTGRES_URL_NON_POOLING point at that "
                "same project."
            )
        return jsonify(payload), 500

    @app.errorhandler(psycopg2.Error)
    def db_error(e):
        app.logger.exception(e)
        payload = {"error": "Database error."}
        if Config.DEBUG:
            payload["detail"] = str(e).strip()
        return jsonify(payload), 500

    @app.errorhandler(500)
    def server_error(e):
        app.logger.exception(e)
        return jsonify({"error": "Internal server error"}), 500

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=Config.DEBUG)
