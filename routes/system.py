"""Existing Vercel functions run daily maintenance using CRON_SECRET."""
import hmac
from flask import Blueprint, jsonify, request
from config import Config
from extensions import db_cursor
from utils.integrity import run_audit, seal_message
from utils.notifications import dispatch_notifications, queue_notification

system_bp = Blueprint("system", __name__)


def cron_authorized():
    return bool(Config.CRON_SECRET and hmac.compare_digest(request.headers.get("Authorization", "").encode(), f"Bearer {Config.CRON_SECRET}".encode()))


@system_bp.route("/seal-history", methods=["POST"])
def seal_history():
    """One-time seal of the fixed pre-deployment history; never reseals a receipt."""
    if not cron_authorized(): return jsonify({"error": "Unauthorized"}), 401
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT * FROM validate_chain()")
        if not cur.fetchone()["is_valid"]: return jsonify({"error": "History failed validation; cannot seal"}), 409
        cur.execute("SELECT m.* FROM messages m LEFT JOIN private.message_receipts r ON r.message_id = m.id "
                    "WHERE m.block_index < %s AND r.message_id IS NULL ORDER BY m.block_index", (Config.LEDGER_LEGACY_END_INDEX,))
        messages = cur.fetchall()
        for m in messages: seal_message(cur, m)
    return jsonify({"sealed": len(messages)})


@system_bp.route("/maintenance", methods=["GET"])
def maintenance():
    if not cron_authorized(): return jsonify({"error": "Unauthorized"}), 401
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(731018) AS locked")
        if not cur.fetchone()["locked"]: return jsonify({"running": True}), 202
        report = run_audit(cur, "scheduled")
        cur.execute("UPDATE calls SET status = 'missed', ended_at = now() WHERE status IN ('initiated','accepted') AND started_at < now()-interval '2 minutes'")
        if not report["is_valid"]:
            cur.execute("SELECT id FROM profiles WHERE role = 'admin' AND status = 'active'")
            queue_notification(cur, [r["id"] for r in cur.fetchall()], f"audit:{report['head_hash']}", None, "audit")
    push = dispatch_notifications(limit=5)
    return jsonify({"integrity": report, "notifications": push})


@system_bp.route("/notifications", methods=["GET"])
def retry_notifications():
    if not cron_authorized(): return jsonify({"error": "Unauthorized"}), 401
    return jsonify(dispatch_notifications(limit=20))
