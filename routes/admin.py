"""
routes/admin.py
----------------
All routes here require an authenticated user with role='admin' on their
profiles row (see utils/auth_helpers.require_admin). Promote a user to
admin manually in Supabase (Table Editor -> profiles -> role -> 'admin'),
or via a one-off SQL statement — there is deliberately no self-service
"become admin" endpoint.

GET    /api/admin/users                 -> list all users
POST   /api/admin/users/<id>/suspend    -> suspend a user
POST   /api/admin/users/<id>/reinstate  -> reinstate a suspended user
GET    /api/admin/blockchain/validate   -> full chain integrity audit
GET    /api/admin/stats                 -> basic platform counters
"""

from flask import Blueprint, jsonify

from extensions import db_cursor
from utils.auth_helpers import require_auth, require_admin

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/users", methods=["GET"])
@require_auth
@require_admin
def list_users():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM profiles ORDER BY created_at DESC")
        users = cur.fetchall()
    return jsonify({"users": [dict(u) for u in users]}), 200


@admin_bp.route("/users/<uuid:user_id>/suspend", methods=["POST"])
@require_auth
@require_admin
def suspend_user(user_id):
    with db_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE profiles SET status = 'suspended' WHERE id = %s RETURNING *",
            (str(user_id),),
        )
        user = cur.fetchone()
    return jsonify({"user": dict(user) if user else None}), 200


@admin_bp.route("/users/<uuid:user_id>/reinstate", methods=["POST"])
@require_auth
@require_admin
def reinstate_user(user_id):
    with db_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE profiles SET status = 'active' WHERE id = %s RETURNING *",
            (str(user_id),),
        )
        user = cur.fetchone()
    return jsonify({"user": dict(user) if user else None}), 200


@admin_bp.route("/blockchain/validate", methods=["GET"])
@require_auth
@require_admin
def validate_blockchain():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM validate_chain()")
        result = cur.fetchone()
    report = dict(result) if result else {"is_valid": True, "invalid_index": None, "blocks_checked": 0}
    return jsonify(report), 200


@admin_bp.route("/stats", methods=["GET"])
@require_auth
@require_admin
def platform_stats():
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM profiles")
        total_users = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM messages")
        total_messages = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM conversations")
        total_conversations = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM calls")
        total_calls = cur.fetchone()["n"]

    return jsonify({
        "total_users": total_users,
        "total_messages": total_messages,
        "total_conversations": total_conversations,
        "total_calls": total_calls,
    }), 200
