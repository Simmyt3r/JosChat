"""
routes/calls.py
----------------
WebRTC signalling (SDP offers/answers, ICE candidates) happens directly
between clients over a Supabase Realtime *Broadcast* channel — see
static/js/app.js `startCall()` / `listenForIncomingCalls()` — because
that is a low-latency pub/sub channel Supabase already runs for us, and
it avoids needing a persistent Flask server (which doesn't fit Vercel's
serverless model). Flask's only job here is to keep a durable log of
call sessions for the admin dashboard and call-history features.

POST /api/calls/start   { conversation_id, callee_id, call_type }
POST /api/calls/<id>/end
GET  /api/calls/<conversation_id>
"""

import uuid

from flask import Blueprint, request, jsonify, g

from extensions import db_cursor
from utils.auth_helpers import require_auth

calls_bp = Blueprint("calls", __name__)


@calls_bp.route("/start", methods=["POST"])
@require_auth
def start_call():
    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversation_id")
    callee_id = data.get("callee_id")
    call_type = data.get("call_type", "voice")

    if not conversation_id or not callee_id:
        return jsonify({"error": "conversation_id and callee_id are required"}), 400
    if not isinstance(conversation_id, int) or isinstance(conversation_id, bool):
        return jsonify({"error": "conversation_id must be an integer"}), 400
    try:
        callee_id = str(uuid.UUID(str(callee_id)))
    except ValueError:
        return jsonify({"error": "callee_id must be a valid user id"}), 400
    if call_type not in ("voice", "video"):
        return jsonify({"error": "call_type must be 'voice' or 'video'"}), 400

    with db_cursor(commit=True) as cur:
        # Both ends of a call must actually belong to the conversation.
        for uid in (g.profile["id"], callee_id):
            cur.execute(
                "SELECT 1 FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
                (conversation_id, uid),
            )
            if not cur.fetchone():
                return jsonify({"error": "Caller and callee must both be participants in the conversation"}), 403

        cur.execute(
            """
            INSERT INTO calls (conversation_id, caller_id, callee_id, call_type, status)
            VALUES (%s, %s, %s, %s, 'initiated')
            RETURNING *
            """,
            (conversation_id, g.profile["id"], callee_id, call_type),
        )
        call = cur.fetchone()

    return jsonify({"call": dict(call)}), 201


@calls_bp.route("/<int:call_id>/end", methods=["POST"])
@require_auth
def end_call(call_id):
    data = request.get_json(silent=True) or {}
    status = data.get("status", "ended")
    if status not in ("connected", "missed", "ended", "failed"):
        status = "ended"

    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE calls SET status = %s, ended_at = now()
            WHERE id = %s AND (caller_id = %s OR callee_id = %s)
            RETURNING *
            """,
            (status, call_id, g.profile["id"], g.profile["id"]),
        )
        call = cur.fetchone()

    if not call:
        return jsonify({"error": "Call not found"}), 404
    return jsonify({"call": dict(call)}), 200


@calls_bp.route("/<int:conversation_id>", methods=["GET"])
@require_auth
def call_history(conversation_id):
    with db_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
            (conversation_id, g.profile["id"]),
        )
        if not cur.fetchone():
            return jsonify({"error": "Not a participant in this conversation"}), 403

        cur.execute(
            """
            SELECT * FROM calls
            WHERE conversation_id = %s
            ORDER BY started_at DESC
            """,
            (conversation_id,),
        )
        calls = cur.fetchall()

    return jsonify({"calls": [dict(c) for c in calls]}), 200
