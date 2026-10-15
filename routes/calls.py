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

POST /api/calls/start    { conversation_id, callee_id, call_type }
POST /api/calls/<id>/accept
POST /api/calls/<id>/end   { status? }   -- status one of connected|missed|ended|failed (default "ended")
GET  /api/calls/<conversation_id>

`status` on the `calls` row is a durable log, not the live signal: the callee's
Realtime INSERT event is what actually rings the caller (an "initiated" row
appearing for them), and a live Broadcast+Presence channel carries the
SDP offer/answer and ICE candidates once a call is being set up — see the
"WebRTC calling" section of static/js/app.js.
"""

import uuid
import base64
import hashlib
import hmac
import time

from flask import Blueprint, request, jsonify, g

from extensions import db_cursor
from config import Config
from utils.auth_helpers import require_auth
from utils.notifications import queue_notification, dispatch_safely

calls_bp = Blueprint("calls", __name__)


@calls_bp.route("/incoming", methods=["GET"])
@require_auth
def pending_incoming():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM calls WHERE callee_id = %s AND status = 'initiated' AND ended_at IS NULL "
                    "AND started_at > now()-interval '2 minutes' ORDER BY started_at DESC LIMIT 1", (g.profile["id"],))
        call = cur.fetchone()
    return jsonify({"call": dict(call) if call else None})


@calls_bp.route("/ice", methods=["GET"])
@require_auth
def ice_servers():
    servers = [{"urls": "stun:stun.l.google.com:19302"}]
    urls = [u.strip() for u in Config.TURN_URLS.split(',') if u.strip().startswith(('turn:', 'turns:'))]
    if urls and Config.TURN_SHARED_SECRET:
        username = f"{int(time.time()) + 3600}:{g.profile['id']}"
        credential = base64.b64encode(hmac.new(Config.TURN_SHARED_SECRET.encode(), username.encode(), hashlib.sha1).digest()).decode()
        servers.append({"urls": urls, "username": username, "credential": credential})
    elif urls and Config.TURN_USERNAME and Config.TURN_PASSWORD:
        servers.append({"urls": urls, "username": Config.TURN_USERNAME, "credential": Config.TURN_PASSWORD})
    return jsonify({"ice_servers": servers, "relay_available": len(servers) > 1})


@calls_bp.route("/<int:call_id>/connected", methods=["POST"])
@require_auth
def connected_call(call_id):
    # Reported by RTCPeerConnection.connectionState, never by the Accept button.
    with db_cursor(commit=True) as cur:
        cur.execute("UPDATE calls SET status = 'connected', connected_at = coalesce(connected_at, now()) "
                    "WHERE id = %s AND (caller_id = %s OR callee_id = %s) AND status IN ('accepted','connected') RETURNING *",
                    (call_id, g.profile["id"], g.profile["id"]))
        call = cur.fetchone()
    if not call: return jsonify({"error": "Call is not accepted or has ended"}), 409
    return jsonify({"call": dict(call)})


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
    if callee_id == str(g.profile["id"]):
        return jsonify({"error": "Choose another person to call"}), 400

    with db_cursor(commit=True) as cur:
        cur.execute("SELECT is_group FROM conversations WHERE id = %s FOR UPDATE", (conversation_id,))
        conversation = cur.fetchone()
        if not conversation or conversation["is_group"]:
            return jsonify({"error": "Calling is available in direct conversations"}), 400
        # Both ends of a call must actually belong to the conversation.
        for uid in (g.profile["id"], callee_id):
            cur.execute(
                "SELECT 1 FROM conversation_participants cp JOIN profiles p ON p.id = cp.user_id AND p.status = 'active' WHERE conversation_id = %s AND user_id = %s",
                (conversation_id, uid),
            )
            if not cur.fetchone():
                return jsonify({"error": "Caller and callee must both be participants in the conversation"}), 403

        # One call at a time per conversation — otherwise a double-click (or two
        # people calling each other at once) leaves a stray "ringing" row neither
        # side is actually listening to, since a client only tracks one call.
        cur.execute("UPDATE calls SET status = 'missed', ended_at = now() WHERE conversation_id = %s AND status IN ('initiated','accepted') AND started_at < now() - interval '2 minutes'", (conversation_id,))
        cur.execute(
            "SELECT 1 FROM calls WHERE conversation_id = %s AND status IN ('initiated', 'accepted', 'connected') AND ended_at IS NULL",
            (conversation_id,),
        )
        if cur.fetchone():
            return jsonify({"error": "There's already an active call in this conversation"}), 409

        cur.execute(
            """
            INSERT INTO calls (conversation_id, caller_id, callee_id, call_type, status)
            VALUES (%s, %s, %s, %s, 'initiated')
            RETURNING *
            """,
            (conversation_id, g.profile["id"], callee_id, call_type),
        )
        call = cur.fetchone()
        queue_notification(cur, [callee_id], f"call:{call['id']}", conversation_id, "call")

    dispatch_safely()
    return jsonify({"call": dict(call)}), 201


@calls_bp.route("/<int:call_id>/accept", methods=["POST"])
@require_auth
def accept_call(call_id):
    """The callee picking up. Marks the call connected; does not touch ended_at."""
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE calls SET status = 'accepted', accepted_at = now()
            WHERE id = %s AND callee_id = %s AND status = 'initiated'
            RETURNING *
            """,
            (call_id, g.profile["id"]),
        )
        call = cur.fetchone()

    if not call:
        return jsonify({"error": "Call not found, already answered, or you're not the callee"}), 404
    return jsonify({"call": dict(call)}), 200


@calls_bp.route("/<int:call_id>/end", methods=["POST"])
@require_auth
def end_call(call_id):
    data = request.get_json(silent=True) or {}
    status = data.get("status", "ended")
    if status not in ("missed", "ended", "failed"):
        status = "ended"

    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE calls SET status = %s, ended_at = now()
            WHERE id = %s AND (caller_id = %s OR callee_id = %s) AND ended_at IS NULL
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
