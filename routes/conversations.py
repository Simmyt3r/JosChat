"""
routes/conversations.py
------------------------
POST /api/conversations         { participant_ids: [uuid, ...], is_group?, title? }
GET  /api/conversations         -> list conversations the current user belongs to
GET  /api/conversations/<id>    -> single conversation + participant list
"""

from flask import Blueprint, request, jsonify, g

from extensions import db_cursor
from utils.auth_helpers import require_auth

conversations_bp = Blueprint("conversations", __name__)


@conversations_bp.route("", methods=["POST"])
@require_auth
def create_conversation():
    data = request.get_json(silent=True) or {}
    participant_ids = data.get("participant_ids") or []
    is_group = bool(data.get("is_group", False))
    title = data.get("title")

    if not participant_ids:
        return jsonify({"error": "participant_ids must include at least one other user"}), 400
    if not is_group and len(participant_ids) != 1:
        return jsonify({"error": "A direct conversation must have exactly one other participant"}), 400

    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO conversations (is_group, title, created_by)
            VALUES (%s, %s, %s)
            RETURNING *
            """,
            (is_group, title, g.profile["id"]),
        )
        conversation = cur.fetchone()
        conversation_id = conversation["id"]

        all_participant_ids = [g.profile["id"]] + list(participant_ids)
        cur.executemany(
            """
            INSERT INTO conversation_participants (conversation_id, user_id)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            [(conversation_id, uid) for uid in all_participant_ids],
        )

    return jsonify({"conversation": dict(conversation)}), 201


@conversations_bp.route("", methods=["GET"])
@require_auth
def list_conversations():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT c.*
            FROM conversations c
            JOIN conversation_participants cp ON cp.conversation_id = c.id
            WHERE cp.user_id = %s
            ORDER BY c.created_at DESC
            """,
            (g.profile["id"],),
        )
        conversations = cur.fetchall()

    return jsonify({"conversations": [dict(c) for c in conversations]}), 200


@conversations_bp.route("/<int:conversation_id>", methods=["GET"])
@require_auth
def get_conversation(conversation_id):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM conversation_participants
            WHERE conversation_id = %s AND user_id = %s
            """,
            (conversation_id, g.profile["id"]),
        )
        if not cur.fetchone():
            return jsonify({"error": "Not a participant in this conversation"}), 403

        cur.execute("SELECT * FROM conversations WHERE id = %s", (conversation_id,))
        conversation = cur.fetchone()

        cur.execute(
            """
            SELECT cp.user_id, cp.joined_at, p.username
            FROM conversation_participants cp
            JOIN profiles p ON p.id = cp.user_id
            WHERE cp.conversation_id = %s
            """,
            (conversation_id,),
        )
        participants = cur.fetchall()

    return jsonify({
        "conversation": dict(conversation) if conversation else None,
        "participants": [dict(p) for p in participants],
    }), 200
