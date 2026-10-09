"""Real Chromium + real service worker/IndexedDB, with an in-memory API fixture.
Run: python tests/e2e/run_messaging_ui.py (no Supabase or live accounts required).
The backend SQL and authorization are separately covered by pytest/CI.
"""
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import threading
import uuid

from flask import jsonify, request
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.update(SUPABASE_URL="https://example.supabase.co", SUPABASE_ANON_KEY="dummy",
                  POSTGRES_URL="postgres://u:p@localhost:5432/postgres",
                  CLOUDINARY_CLOUD_NAME="demo", CLOUDINARY_API_KEY="k", CLOUDINARY_API_SECRET="s")
from app import create_app
from utils.phone import normalize_phone

USERS, CONVS, MESSAGES = {}, {}, []
STATE = {"fail_send": False, "lose_response": False, "expired": False, "verified_requests": 0, "refresh_fail": False}
BASE = "http://localhost:5056"
app = create_app()
logging.getLogger("werkzeug").setLevel(logging.ERROR)


def profile(u):
    return {"id": u["id"], "username": u["username"], "public_key": u.get("public_key"),
            "phone_number": u["phone_number"], "role": "user", "status": "active"}


def verified(m):
    return hashlib.sha256(m["encrypted_content"].encode()).hexdigest() == m["digest"]


def message(m):
    return {k: v for k, v in m.items() if k != "digest"} | {"verified": verified(m)}


@app.before_request
def fixture_api():
    path = request.path
    if not path.startswith("/api/"):
        return None
    data = request.get_json(silent=True) or {}
    if path == "/api/config":
        return jsonify({"cloudinary_cloud_name": "demo"})
    if path == "/api/auth/register":
        phone, error = normalize_phone(data.get("phone_number"), required=True)
        if error: return jsonify({"error": error, "field": "phone_number"}), 400
        USERS[data["email"]] = {**data, "id": str(uuid.uuid4()), "phone_number": phone}
        return jsonify({"confirmation_required": False}), 201
    if path == "/api/auth/login":
        STATE["expired"] = False
        u = USERS[data["email"]]
        return jsonify({"access_token": "tok-" + u["id"], "refresh_token": u["id"], "profile": profile(u)})
    if path == "/api/auth/refresh":
        if STATE["refresh_fail"]: return jsonify({"error": "Refresh expired"}), 401
        STATE["expired"] = False
        u = next(u for u in USERS.values() if u["id"] == data["refresh_token"])
        return jsonify({"access_token": "tok-" + u["id"], "refresh_token": u["id"], "profile": profile(u)})
    token = request.headers.get("Authorization", "").removeprefix("Bearer tok-")
    u = next((u for u in USERS.values() if u["id"] == token), None)
    if not u or STATE["expired"]: return jsonify({"error": "Session expired"}), 401
    if path == "/api/auth/me": return jsonify({"profile": profile(u)})
    if path == "/api/auth/logout": return jsonify({"message": "ok"})
    if path == "/api/conversations":
        if request.method == "POST":
            names = data["participant_usernames"]
            others = [p for p in USERS.values() if p["username"] in names]
            if len(others) != len(names): return jsonify({"error": "No such user"}), 404
            cid = len(CONVS) + 1
            CONVS[cid] = {"id": cid, "title": data.get("title"), "is_group": data.get("is_group", False),
                          "members": [u["id"], *[o["id"] for o in others]]}
            return jsonify({"conversation": CONVS[cid]}), 201
        return jsonify({"conversations": [{**c, "other_usernames": [p["username"] for p in USERS.values()
                        if p["id"] in c["members"] and p["id"] != u["id"]]} for c in CONVS.values() if u["id"] in c["members"]]})
    if path.startswith("/api/conversations/"):
        c = CONVS[int(path.rsplit("/", 1)[1])]
        return jsonify({"conversation": c, "participants": [{"user_id": p["id"], "username": p["username"], "public_key": p.get("public_key")}
                        for p in USERS.values() if p["id"] in c["members"]]})
    if path == "/api/messages/send":
        if STATE["fail_send"]: return jsonify({"error": "Temporarily unavailable"}), 503
        if request.headers.get("X-Joschat-Owner") != u["id"]: return jsonify({"error": "Wrong account"}), 403
        existing = next((m for m in MESSAGES if m["client_message_id"] == data["client_message_id"]), None)
        if existing: return jsonify({"message": message(existing)}), 200
        m = {**data, "id": len(MESSAGES) + 1, "sender_id": u["id"], "block_index": len(MESSAGES),
             "block_hash": "f" * 64, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
             "digest": hashlib.sha256(data["encrypted_content"].encode()).hexdigest()}
        MESSAGES.append(m)
        if STATE["lose_response"]:
            STATE["lose_response"] = False
            return jsonify({"error": "Response lost after commit"}), 503
        return jsonify({"message": message(m)}), 201
    if path.startswith("/api/messages/verify/"):
        STATE["verified_requests"] += 1
        cid = int(path.rsplit("/", 1)[1])
        ids = request.args.get("message_ids")
        ids = [int(i) for i in ids.split(",")] if ids else None
        return jsonify({"results": [{"message_id": m["id"], "verified": verified(m)} for m in MESSAGES
                         if m["conversation_id"] == cid and (ids is None or m["id"] in ids)]})
    if path.startswith("/api/messages/"):
        cid = int(path.rsplit("/", 1)[1])
        return jsonify({"messages": [message(m) for m in MESSAGES if m["conversation_id"] == cid]})
    if path == "/api/calls/incoming": return jsonify({"calls": []})
    return jsonify({"error": "Unimplemented test API"}), 404


def register(pg, name):
    pg.goto(BASE)
    pg.click("#tab-register")
    pg.fill("#username", name); pg.fill("#email", f"{name}@example.com"); pg.fill("#password", "secret123")
    pg.fill("#phone-number", "bad")
    pg.click("#submit-btn")
    assert pg.get_attribute("#phone-number", "aria-invalid") == "true"
    pg.fill("#phone-number", "09039930006"); pg.click("#submit-btn")
    pg.wait_for_selector("#chat-shell:not([hidden])")
    pg.wait_for_function("navigator.serviceWorker.controller !== null && myKeyStatus === 'ready'")


def send(pg, text):
    pg.fill("#message-text", text); pg.click("#send-btn")
    pg.wait_for_function("!sending && document.getElementById('message-text').value === ''")


def unlock(pg):
    pg.fill("#conv-passphrase", "a long private group phrase"); pg.click("#unlock-btn")
    pg.wait_for_selector("#composer:not([hidden])")


def run():
    server = make_server("localhost", 5056, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    errors = []
    try:
        with sync_playwright() as p:
            br = p.chromium.launch()
            ctx = br.new_context(viewport={"width": 390, "height": 844})
            bob_ctx = br.new_context()
            alice, bob = ctx.new_page(), bob_ctx.new_page()
            for pg in [alice, bob]:
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.route("https://**", lambda route: route.abort())
            register(alice, "alice"); register(bob, "bob")
            USERS["carol@example.com"] = {"id": str(uuid.uuid4()), "username": "carol", "phone_number": "+2348000000001"}
            assert USERS["alice@example.com"]["phone_number"] == "+2349039930006"
            alice.click("#new-group-btn")
            alice.fill("#group-name", "STEM learners")
            alice.fill("#group-usernames", "bob")
            alice.click("#group-submit")
            assert "2–49" in alice.inner_text("#group-error")
            alice.fill("#group-usernames", "@bob, carol, bob")
            alice.click("#group-submit")
            alice.wait_for_selector("#unlock-panel:not([hidden])")
            assert alice.inner_text("#conv-title") == "STEM learners"
            assert "3 members" in alice.inner_text("#group-members")
            assert alice.is_disabled("#voice-call-btn")
            assert not alice.evaluate("document.documentElement.scrollWidth > innerWidth")
            unlock(alice); send(alice, "Welcome to STEM learners")
            alice.wait_for_selector('.seal[data-state="verified"]')
            bob.evaluate("loadConversations()")
            bob.wait_for_selector(".conv"); bob.click(".conv")
            bob.wait_for_selector("#unlock-panel:not([hidden])"); unlock(bob)
            assert "Welcome to STEM learners" in bob.inner_text("#messages")
            assert "@alice" in bob.inner_text("#messages")
            # A Realtime-shaped row has no server verification field: automatic
            # checking must run, without clicking Verify chat.
            row = message(MESSAGES[0]); row.pop("verified")
            bob.evaluate("(row) => { messageStatus.clear(); messageStore = []; return addMessages([row]); }", row)
            bob.wait_for_selector('.seal[data-state="verified"]')
            assert STATE["verified_requests"] > 0
            print("PASS phone registration, group creation/decryption, automatic verification, mobile layout", flush=True)
            # Store encrypted offline messages with a real service worker.
            STATE["fail_send"] = True
            ctx.set_offline(True)
            send(alice, "Offline first message"); send(alice, "Offline second message")
            alice.wait_for_selector('.seal[data-state="queued"]', timeout=3000)
            rows = alice.evaluate("outboxRequest('OUTBOX_LIST')")['rows']
            assert len(rows) == 2 and "Offline first message" not in json.dumps(rows)
            assert "authorization" not in json.dumps(rows)
            # IndexedDB survives a page reload. Queue is delivered on return.
            alice.reload()
            persisted = alice.evaluate("""async () => { await navigator.serviceWorker.ready;
                return await new Promise((resolve,reject) => {const q=indexedDB.open('joschat-outbox',1);
                  q.onsuccess=()=>{const db=q.result;const r=db.transaction('messages').objectStore('messages').getAll();
                    r.onsuccess=()=>{resolve(r.result);db.close()};r.onerror=reject};q.onerror=reject});} """)
            assert len(persisted) == 2
            STATE["fail_send"] = False
            ctx.set_offline(False)
            alice.reload(); alice.wait_for_selector("#chat-shell:not([hidden])")
            alice.wait_for_function("queuedRows.length === 0")
            alice.wait_for_timeout(500)
            assert len(MESSAGES) == 3
            alice.wait_for_selector(".conv"); alice.click(".conv"); alice.wait_for_selector("#composer:not([hidden])")
            assert "Offline first message" in alice.inner_text("#messages")
            print("PASS encrypted offline queue survives reload and sends on reconnect", flush=True)
            # Commit succeeded but response was lost: replay must not duplicate.
            STATE["lose_response"] = True
            send(alice, "Exactly once")
            assert len(MESSAGES) == 4
            alice.evaluate("syncOutbox(true)")
            alice.wait_for_function("queuedRows.length === 0")
            assert len(MESSAGES) == 4
            STATE["expired"] = True
            send(alice, "Refresh session before delivery")
            alice.wait_for_function("queuedRows.length === 0")
            assert len(MESSAGES) == 5
            print("PASS lost-response idempotency and expired-token refresh", flush=True)
            # An expired refresh token returns to login, retaining ciphertext
            # until the same owner reauthenticates.
            STATE["fail_send"] = True
            send(alice, "Keep me through session expiry")
            STATE["fail_send"] = False; STATE["expired"] = True; STATE["refresh_fail"] = True
            alice.evaluate("syncOutbox(true)")
            alice.wait_for_selector("#auth-view:not([hidden])")
            retained = alice.evaluate("(owner) => outboxRequest('OUTBOX_LIST', {owner_id: owner})", USERS['alice@example.com']['id'])['rows']
            assert len(retained) == 1
            STATE["refresh_fail"] = False
            alice.fill("#email", "alice@example.com"); alice.fill("#password", "secret123"); alice.click("#submit-btn")
            alice.wait_for_selector("#chat-shell:not([hidden])")
            alice.wait_for_function("queuedRows.length === 0")
            assert len(MESSAGES) == 6
            alice.click(".conv"); alice.wait_for_selector("#composer:not([hidden])")
            print("PASS session expiry preserves the queue until owner reauthentication", flush=True)
            # Failing integrity does not leave a green badge behind.
            MESSAGES[0]["encrypted_content"] = "tampered"
            bob.evaluate("refreshMessages()")
            bob.wait_for_selector('.seal[data-state="tampered"]')
            # Logout removes only this account's pending messages.
            STATE["fail_send"] = True
            ctx.set_offline(True); send(alice, "Remove me on logout")
            alice.evaluate("signOut()")
            alice.wait_for_selector("#auth-view:not([hidden])")
            assert alice.evaluate("(owner) => outboxRequest('OUTBOX_LIST', {owner_id: owner})", USERS['alice@example.com']['id'])['rows'] == []
            print("PASS tamper badge and logout queue cleanup", flush=True)
            assert not errors, errors
            br.close()
    finally:
        server.shutdown()


if __name__ == "__main__":
    run()
