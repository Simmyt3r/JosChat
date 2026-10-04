"""
routes/admin.py
----------------
All routes here require an authenticated user with role='admin' on their
profiles row (see utils/auth_helpers.require_admin). Promote a user to
admin manually in Supabase (Table Editor -> profiles -> role -> 'admin'),
or via a one-off SQL statement — there is deliberately no self-service
"become admin" endpoint.

GET    /api/admin/users                              -> list all users
POST   /api/admin/users/<id>/suspend                 -> suspend a user
POST   /api/admin/users/<id>/reinstate                -> reinstate a suspended user
GET    /api/admin/blockchain/validate                -> full chain integrity audit
GET    /api/admin/conversations                       -> list conversations with
                                                          participant usernames and a
                                                          message count
GET    /api/admin/conversations/<id>/validate         -> integrity audit scoped to
                                                          one conversation
GET    /api/admin/stats                               -> platform counters

These endpoints back templates/admin.html (served at GET /admin in app.py).
That page itself carries no secrets and is not the security boundary — these
API routes are: the page just renders a client-side "not an admin" screen if
the person signed in isn't one, while every actual read happens through
@require_admin here.
"""

from flask import Blueprint, jsonify, g

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
    # An admin suspending their own account would lock themselves out of the
    # one dashboard that could reinstate them.
    if str(user_id) == g.profile["id"]:
        return jsonify({"error": "You can't suspend your own account"}), 400

    with db_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE profiles SET status = 'suspended' WHERE id = %s RETURNING *",
            (str(user_id),),
        )
        user = cur.fetchone()
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({"user": dict(user)}), 200


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
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({"user": dict(user)}), 200


@admin_bp.route("/blockchain/validate", methods=["GET"])
@require_auth
@require_admin
def validate_blockchain():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM validate_chain()")
        result = cur.fetchone()
    report = dict(result) if result else {"is_valid": True, "invalid_index": None, "blocks_checked": 0}
    return jsonify(report), 200


@admin_bp.route("/conversations", methods=["GET"])
@require_auth
@require_admin
def list_conversations():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                c.id,
                c.is_group,
                c.created_at,
                COALESCE(
                    ARRAY_AGG(p.username ORDER BY p.username) FILTER (WHERE p.username IS NOT NULL),
                    ARRAY[]::text[]
                ) AS participants,
                (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count
            FROM conversations c
            LEFT JOIN conversation_participants cp ON cp.conversation_id = c.id
            LEFT JOIN profiles p ON p.id = cp.user_id
            GROUP BY c.id, c.is_group, c.created_at
            ORDER BY c.created_at DESC
            LIMIT 200
            """
        )
        conversations = cur.fetchall()
    return jsonify({"conversations": [dict(c) for c in conversations]}), 200


@admin_bp.route("/conversations/<int:conversation_id>/validate", methods=["GET"])
@require_auth
@require_admin
def validate_conversation_route(conversation_id):
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM conversations WHERE id = %s", (conversation_id,))
        if cur.fetchone() is None:
            return jsonify({"error": "Conversation not found"}), 404

        cur.execute("SELECT * FROM validate_conversation(%s)", (conversation_id,))
        results = cur.fetchall()

    results = [dict(r) for r in results]
    flagged = [r["message_id"] for r in results if not r.get("verified", True)]
    return jsonify({
        "conversation_id": conversation_id,
        "messages_checked": len(results),
        "all_verified": len(flagged) == 0,
        "flagged_message_ids": flagged,
        "results": results,
    }), 200


@admin_bp.route("/stats", methods=["GET"])
@require_auth
@require_admin
def platform_stats():
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM profiles")
        total_users = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM profiles WHERE status = 'active'")
        active_users = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM profiles WHERE status = 'suspended'")
        suspended_users = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM conversations")
        total_conversations = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM messages")
        total_messages = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM blocks")
        total_blocks = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM calls")
        total_calls = cur.fetchone()["n"]
        cur.execute("SELECT status, COUNT(*) AS n FROM calls GROUP BY status")
        calls_by_status = {row["status"]: row["n"] for row in cur.fetchall()}

    return jsonify({
        "total_users": total_users,
        "active_users": active_users,
        "suspended_users": suspended_users,
        "total_conversations": total_conversations,
        "total_messages": total_messages,
        "total_blocks": total_blocks,
        "total_calls": total_calls,
        "calls_by_status": calls_by_status,
    }), 200
