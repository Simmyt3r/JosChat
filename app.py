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

from flask import Flask, jsonify
from flask_cors import CORS

from config import Config
from extensions import init_cloudinary

from routes.auth import auth_bp
from routes.conversations import conversations_bp
from routes.messages import messages_bp
from routes.media import media_bp
from routes.admin import admin_bp
from routes.calls import calls_bp


def create_app():
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object(Config)

    # Fail fast and loudly if required env vars are missing, rather than
    # producing confusing errors deep inside a Supabase/Cloudinary call.
    Config.validate()

    CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)
    init_cloudinary()

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(conversations_bp, url_prefix="/api/conversations")
    app.register_blueprint(messages_bp, url_prefix="/api/messages")
    app.register_blueprint(media_bp, url_prefix="/api/media")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(calls_bp, url_prefix="/api/calls")

    @app.route("/api/health", methods=["GET"])
    def health():
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
        }), 200

    @app.route("/", methods=["GET"])
    def index():
        from flask import render_template
        return render_template("index.html")

    @app.errorhandler(404)
    def not_found(_e):
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({"error": "File too large"}), 413

    @app.errorhandler(500)
    def server_error(e):
        app.logger.exception(e)
        return jsonify({"error": "Internal server error"}), 500

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=Config.DEBUG)
