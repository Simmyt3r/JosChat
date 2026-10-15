import json
from flask import Blueprint, jsonify, request, g
from extensions import db_cursor
from config import Config
from utils.auth_helpers import require_auth
from utils.notifications import valid_subscription

notifications_bp = Blueprint("notifications", __name__)


@notifications_bp.route("/subscriptions", methods=["POST", "DELETE"])
@require_auth
def subscription():
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")
    if request.method == "DELETE":
        if not isinstance(endpoint, str): return jsonify({"error": "endpoint is required"}), 400
        with db_cursor(commit=True) as cur:
            cur.execute("DELETE FROM private.push_subscriptions WHERE endpoint = %s AND user_id = %s", (endpoint, g.profile["id"]))
        return jsonify({"disabled": True})
    if not Config.VAPID_PRIVATE_KEY: return jsonify({"error": "Push is not configured"}), 503
    if not valid_subscription(data): return jsonify({"error": "Invalid push subscription"}), 400
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(g.profile["id"]),))
        cur.execute("SELECT user_id FROM private.push_subscriptions WHERE endpoint = %s", (endpoint,))
        old = cur.fetchone()
        if old and str(old["user_id"]) != str(g.profile["id"]):
            return jsonify({"error": "Disable the previous account's subscription on this device first"}), 409
        if not old:
            cur.execute("SELECT count(*) AS n FROM private.push_subscriptions WHERE user_id = %s", (g.profile["id"],))
            if cur.fetchone()["n"] >= 3:
                return jsonify({"error": "Notifications are enabled on three devices. Disable one before adding another."}), 409
        cur.execute("INSERT INTO private.push_subscriptions(endpoint,user_id,subscription) VALUES (%s,%s,%s::jsonb) "
                    "ON CONFLICT (endpoint) DO UPDATE SET subscription = EXCLUDED.subscription WHERE private.push_subscriptions.user_id = EXCLUDED.user_id", (endpoint, g.profile["id"], json.dumps(data)))
        if cur.rowcount == 0:
            return jsonify({"error": "This device's subscription belongs to another account"}), 409
    return jsonify({"enabled": True}), 201
