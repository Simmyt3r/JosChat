"""Private generic web push; durable retries never contain message plaintext."""
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from pywebpush import webpush, WebPushException
from config import Config
from extensions import db_cursor


def valid_subscription(data):
    if not isinstance(data, dict): return False
    endpoint = data.get("endpoint", "")
    if not isinstance(endpoint, str) or len(endpoint) > 2048: return False
    url = urlparse(endpoint)
    host = url.hostname or ""
    allowed = host in ("fcm.googleapis.com", "web.push.apple.com", "updates.push.services.mozilla.com") or host.endswith((".push.services.mozilla.com", ".notify.windows.com"))
    try:
        if not allowed or url.scheme != "https" or url.username or url.password or url.port not in (None,443): return False
    except ValueError: return False
    try:
        keys = data["keys"]
        raw = lambda v: base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))
        return len(raw(keys["p256dh"])) == 65 and len(raw(keys["auth"])) == 16
    except (KeyError, TypeError, ValueError): return False


def queue_notification(cur, users, event_key, conversation_id, kind="message"):
    if not Config.VAPID_PRIVATE_KEY: return
    for uid in users:
        payload = {"owner_id": str(uid), "kind": kind, "conversation_id": conversation_id}
        cur.execute("INSERT INTO private.push_deliveries(user_id,payload,event_key,expires_at) "
                    "SELECT %s,%s::jsonb,%s,now() + interval '1 hour' WHERE EXISTS "
                    "(SELECT 1 FROM private.push_subscriptions WHERE user_id = %s) ON CONFLICT DO NOTHING",
                    (uid, json.dumps(payload), event_key, uid))


def dispatch_notifications(limit=20):
    if not Config.VAPID_PRIVATE_KEY: return {"sent": 0, "pending": 0}
    sent = 0
    # A transaction lock prevents request-time and scheduled workers racing.
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM private.push_deliveries WHERE expires_at < now() OR attempts >= 5")
        cur.execute("DELETE FROM private.push_deliveries d WHERE NOT EXISTS "
                    "(SELECT 1 FROM profiles p WHERE p.id = d.user_id AND p.status = 'active') "
                    "OR (d.payload->>'kind' <> 'audit' AND NOT EXISTS "
                    "(SELECT 1 FROM conversation_participants cp WHERE cp.user_id = d.user_id "
                    "AND cp.conversation_id::text = d.payload->>'conversation_id'))")
        cur.execute("SELECT * FROM private.push_deliveries ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED", (limit,))
        jobs = cur.fetchall()
        tasks = []
        retries = set()
        for job in jobs:
            cur.execute("SELECT endpoint,subscription FROM private.push_subscriptions WHERE user_id = %s", (job["user_id"],))
            subscriptions = cur.fetchall()
            for sub in subscriptions:
                tasks.append((job, sub))

        def deliver(item):
            job, sub = item
            try:
                webpush(subscription_info=sub["subscription"], data=json.dumps(job["payload"]),
                        vapid_private_key=Config.VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": Config.VAPID_SUBJECT}, ttl=300, timeout=3)
                return job, sub, "sent"
            except WebPushException as exc:
                return job, sub, "expired" if getattr(exc.response, "status_code", None) in (404,410) else "retry"
            except Exception:
                return job, sub, "retry"

        with ThreadPoolExecutor(max_workers=8) as pool:
            for job, sub, outcome in pool.map(deliver, tasks):
                if outcome == "sent": sent += 1
                elif outcome == "expired":
                    cur.execute("DELETE FROM private.push_subscriptions WHERE endpoint = %s", (sub["endpoint"],))
                else: retries.add(job["id"])
        for job in jobs:
            if job["id"] in retries:
                cur.execute("UPDATE private.push_deliveries SET attempts = attempts+1 WHERE id = %s", (job["id"],))
            else:
                cur.execute("DELETE FROM private.push_deliveries WHERE id = %s", (job["id"],))
    return {"sent": sent}


def dispatch_safely():
    try: return dispatch_notifications(limit=3)
    except Exception:
        # Delivery failure must never turn a committed message into a send error.
        from flask import current_app
        current_app.logger.warning("Push dispatch deferred")
        return {"deferred": True}
