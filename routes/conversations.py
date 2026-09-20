"""
routes/conversations.py
------------------------
POST /api/conversations         { participant_usernames: [name, ...] | participant_ids: [uuid, ...],
                                  is_group?, title? }
GET  /api/conversations         -> list conversations the current user belongs to
GET  /api/conversations/<id>    -> single conversation + participant list

A direct (non-group) conversation is unique per pair of users: asking to
start one that already exists returns the existing conversation (200)
instead of creating a duplicate (201).
"""

import uuid

from flask import Blueprint, request, jsonify, g

from extensions import db_cursor
from utils.auth_helpers import require_auth

conversations_bp = Blueprint("conversations", __name__)


def _as_str_list(value):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return [v.strip() for v in value if v.strip()]


@conversations_bp.route("", methods=["POST"])
@require_auth
def create_conversation():
    data = request.get_json(silent=True) or {}
    is_group = bool(data.get("is_group", False))
    title = data.get("title")
    me = str(g.profile["id"])

    raw_ids = _as_str_list(data.get("participant_ids"))
    raw_names = _as_str_list(data.get("participant_usernames"))
    if raw_ids is None or raw_names is None:
        return jsonify({"error": "participant_ids / participant_usernames must be lists of strings"}), 400

    try:
        ids = {str(uuid.UUID(v)) for v in raw_ids}
    except ValueError:
        return jsonify({"error": "participant_ids must be valid user ids"}), 400

    with db_cursor() as cur:
        if raw_names:
            cur.execute(
                "SELECT id, username FROM profiles WHERE username = ANY(%s) AND status = 'active'",
                (raw_names,),
            )
            found = cur.fetchall()
            missing = set(raw_names) - {r["username"] for r in found}
            if missing:
                return jsonify({"error": f"No such user: {', '.join(sorted(missing))}"}), 404
            ids |= {str(r["id"]) for r in found}

        ids.discard(me)  # the caller is always added automatically
        if not ids:
            return jsonify({"error": "Choose at least one other user"}), 400
        if not is_group and len(ids) != 1:
            return jsonify({"error": "A direct conversation must have exactly one other participant"}), 400

        cur.execute(
            "SELECT id FROM profiles WHERE id = ANY(%s::uuid[]) AND status = 'active'",
            (list(ids),),
        )
        if len(cur.fetchall()) != len(ids):
            return jsonify({"error": "One or more participants do not exist"}), 404

        if not is_group:
            (other,) = ids
            cur.execute(
                """
                SELECT c.* FROM conversations c
                WHERE NOT c.is_group
                  AND (SELECT count(*) FROM conversation_participants cp
                       WHERE cp.conversation_id = c.id) = 2
                  AND EXISTS (SELECT 1 FROM conversation_participants cp
                              WHERE cp.conversation_id = c.id AND cp.user_id = %s)
                  AND EXISTS (SELECT 1 FROM conversation_participants cp
                              WHERE cp.conversation_id = c.id AND cp.user_id = %s)
                ORDER BY c.created_at ASC
                LIMIT 1
                """,
                (me, other),
            )
            existing = cur.fetchone()
            if existing:
                return jsonify({"conversation": dict(existing), "existing": True}), 200

    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO conversations (is_group, title, created_by)
            VALUES (%s, %s, %s)
            RETURNING *
            """,
            (is_group, title, me),
        )
        conversation = cur.fetchone()
        conversation_id = conversation["id"]

        cur.executemany(
            """
            INSERT INTO conversation_participants (conversation_id, user_id)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            [(conversation_id, uid) for uid in [me, *ids]],
        )

    return jsonify({"conversation": dict(conversation), "existing": False}), 201


@conversations_bp.route("", methods=["GET"])
@require_auth
def list_conversations():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT c.*,
                   COALESCE((
                       SELECT array_agg(pr.username ORDER BY pr.username)
                       FROM conversation_participants cp2
                       JOIN profiles pr ON pr.id = cp2.user_id
                       WHERE cp2.conversation_id = c.id AND cp2.user_id <> %s
                   ), ARRAY[]::text[]) AS other_usernames
            FROM conversations c
            JOIN conversation_participants cp ON cp.conversation_id = c.id
            WHERE cp.user_id = %s
            ORDER BY c.created_at DESC
            """,
            (g.profile["id"], g.profile["id"]),
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
