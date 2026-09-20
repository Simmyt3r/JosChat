"""
routes/messages.py
-------------------
POST /api/messages/send                     -> hash-then-chain-then-store a message
GET  /api/messages/<conversation_id>        -> fetch a conversation's message history
GET  /api/messages/verify/<conversation_id> -> per-message blockchain verification

Design note: the client is responsible for end-to-end encryption. This
route only ever sees `encrypted_content` (ciphertext) — it hashes that
ciphertext and calls the `add_block` Postgres function (see
supabase/schema.sql) to atomically append a block. That function holds
an exclusive table lock for the duration of one insert, so two messages
sent at the same instant still get correctly ordered, unbroken block
indices — something a naive "read last block in Python, then insert"
approach cannot guarantee under concurrent requests.
"""

from datetime import datetime

from flask import Blueprint, request, jsonify, g

from config import Config
from extensions import db_cursor
from blockchain import hash_message
from utils.auth_helpers import require_auth

messages_bp = Blueprint("messages", __name__)

MAX_CIPHERTEXT_CHARS = 200_000

def _cloudinary_prefix() -> str:
    return f"https://res.cloudinary.com/{Config.CLOUDINARY_CLOUD_NAME}/"


def _is_participant(cur, conversation_id, user_id) -> bool:
    cur.execute(
        """
        SELECT 1 FROM conversation_participants
        WHERE conversation_id = %s AND user_id = %s
        """,
        (conversation_id, user_id),
    )
    return cur.fetchone() is not None


@messages_bp.route("/send", methods=["POST"])
@require_auth
def send_message():
    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversation_id")
    encrypted_content = data.get("encrypted_content")
    media_url = data.get("media_url")
    media_public_id = data.get("media_public_id")

    if not conversation_id or not encrypted_content:
        return jsonify({"error": "conversation_id and encrypted_content are required"}), 400
    if not isinstance(conversation_id, int) or isinstance(conversation_id, bool):
        return jsonify({"error": "conversation_id must be an integer"}), 400
    if not isinstance(encrypted_content, str) or len(encrypted_content) > MAX_CIPHERTEXT_CHARS:
        return jsonify({"error": "encrypted_content must be a string under 200,000 characters"}), 400
    # Only ever store links that our own Cloudinary upload route can produce,
    # so a message can't smuggle in an arbitrary/malicious URL for other
    # participants' clients to render.
    if media_url is not None and not (
        isinstance(media_url, str) and media_url.startswith(_cloudinary_prefix())
    ):
        return jsonify({"error": "media_url must be a URL returned by /api/media/upload"}), 400

    message_hash = hash_message(encrypted_content)

    with db_cursor(commit=True) as cur:
        if not _is_participant(cur, conversation_id, g.profile["id"]):
            return jsonify({"error": "Not a participant in this conversation"}), 403

        # Atomically append a block for this message's hash. See
        # add_block() in supabase/schema.sql — a single locked
        # transaction, not a read-then-write from Python, so it is safe
        # under concurrent requests.
        cur.execute("SELECT * FROM add_block(%s)", (message_hash,))
        block = cur.fetchone()
        if not block:
            return jsonify({"error": "Failed to append blockchain block"}), 500

        cur.execute(
            """
            INSERT INTO messages
                (conversation_id, sender_id, encrypted_content, media_url,
                 media_public_id, block_index, block_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                conversation_id, g.profile["id"], encrypted_content, media_url,
                media_public_id, block["idx"], block["block_hash"],
            ),
        )
        message = cur.fetchone()

    # No Flask-side push needed: the `messages` table is registered with
    # Supabase Realtime (see schema.sql), so any client subscribed to
    # `postgres_changes` for this conversation_id receives this insert
    # directly from Supabase within milliseconds.
    return jsonify({
        "message": dict(message),
        "block_hash": block["block_hash"],
        "block_index": block["idx"],
    }), 201


@messages_bp.route("/<int:conversation_id>", methods=["GET"])
@require_auth
def get_messages(conversation_id):
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    before = request.args.get("before")  # ISO timestamp, for pagination
    if before:
        try:
            datetime.fromisoformat(before.replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"error": "before must be an ISO-8601 timestamp"}), 400

    with db_cursor() as cur:
        if not _is_participant(cur, conversation_id, g.profile["id"]):
            return jsonify({"error": "Not a participant in this conversation"}), 403

        if before:
            cur.execute(
                """
                SELECT * FROM messages
                WHERE conversation_id = %s AND created_at < %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (conversation_id, before, limit),
            )
        else:
            cur.execute(
                """
                SELECT * FROM messages
                WHERE conversation_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (conversation_id, limit),
            )
        rows = cur.fetchall()

    return jsonify({"messages": [dict(r) for r in reversed(rows)]}), 200


@messages_bp.route("/verify/<int:conversation_id>", methods=["GET"])
@require_auth
def verify_conversation(conversation_id):
    with db_cursor() as cur:
        if not _is_participant(cur, conversation_id, g.profile["id"]):
            return jsonify({"error": "Not a participant in this conversation"}), 403

        cur.execute("SELECT * FROM validate_conversation(%s)", (conversation_id,))
        results = cur.fetchall()

    all_verified = all(row["verified"] for row in results) if results else True
    return jsonify({
        "conversation_id": conversation_id,
        "all_verified": all_verified,
        "results": [dict(r) for r in results],
    }), 200
