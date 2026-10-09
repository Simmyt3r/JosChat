"""
Shared mock for the browser-level calling tests (run_calls_ui.py, run_webrtc.py).

A mocked Flask API (the same shapes routes/*.py return) plus a tiny HTTP-polling
relay that stands in for Supabase Realtime Broadcast + Presence. Real Supabase
Realtime isn't reachable from a sandbox, so fake_supabase.js replaces supabase-js
in the page and talks to this relay instead. Everything else — app.js's own
channel/broadcast/presence calls, and the WebRTC stack — is real.
"""
import pathlib, re, sys, tempfile, uuid, datetime as dt
HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SHOTS = pathlib.Path(tempfile.gettempdir()) / "joschat-shots"   # screenshots land here
SHOTS.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))
from utils.keys import validate_public_key
from playwright.sync_api import sync_playwright

BASE = "http://localhost:5055"
now = dt.datetime.now(dt.timezone.utc)
iso = lambda m=0: (now - dt.timedelta(minutes=m)).isoformat().replace("+00:00", "Z")

USERS = {}
CONVS = {}
CALLS = {}
NEXT = {"id": 100}

def user_by_token(req):
    tok = (req.headers.get("authorization") or "").replace("Bearer ", "")
    return next((u for u in USERS.values() if tok == "tok-" + u["id"]), None)

def public(u): return {"id": u["id"], "username": u["username"], "public_key": u["public_key"], "role": "user", "status": "active"}

def api(route, request):
    path = request.url.replace(BASE, "").split("?")[0]; m = request.method
    j = lambda body, st=200: route.fulfill(status=st, content_type="application/json", body=__import__("json").dumps(body))
    try:
        body = request.post_data_json if request.post_data else {}
    except Exception:
        body = {}
    if path == "/api/config": return j({"supabase_url": "https://x.supabase.co", "supabase_anon_key": "x", "cloudinary_cloud_name": "demo"})
    if path == "/api/auth/register":
        key, err = validate_public_key(body.get("public_key"))
        if err: return j({"error": err}, 400)
        uid = str(uuid.uuid4())
        USERS[body["email"]] = {"id": uid, "username": body["username"], "password": body["password"], "public_key": key}
        return j({"confirmation_required": False}, 201)
    if path == "/api/auth/login":
        u = USERS.get(body["email"])
        if not u or u["password"] != body["password"]: return j({"error": "Invalid email or password"}, 401)
        return j({"access_token": "tok-" + u["id"], "refresh_token": "r", "profile": public(u)})
    if path.startswith("/api/__relay/"):
        result = relay_handle(path, m, body, request.url)
        return j(result if result is not None else {"ok": True})
    me = user_by_token(request)
    if not me: return j({"error": "unauthorized"}, 401)
    if path == "/api/auth/me": return j({"profile": public(me)})
    if path == "/api/auth/logout": return j({"message": "ok"})
    if path == "/api/auth/public-key" and m == "PUT":
        key, err = validate_public_key(body.get("public_key"))
        if err or not key: return j({"error": err or "public_key is required"}, 400)
        me["public_key"] = key; return j({"profile": public(me)})
    if path == "/api/conversations" and m == "GET":
        out = []
        for c in CONVS.values():
            if me["id"] in c["members"]:
                others = sorted(u["username"] for u in USERS.values() if u["id"] in c["members"] and u["id"] != me["id"])
                out.append({"id": c["id"], "title": None, "is_group": len(c["members"]) > 2, "other_usernames": others, "created_at": iso(50)})
        return j({"conversations": out})
    if path == "/api/conversations" and m == "POST":
        others = [next((u for u in USERS.values() if u["username"] == n), None) for n in body["participant_usernames"]]
        if not all(others): return j({"error": "No such user"}, 404)
        cid = max(CONVS, default=0) + 1
        CONVS[cid] = {"id": cid, "members": [me["id"]] + [o["id"] for o in others]}
        return j({"conversation": {"id": cid, "title": None}, "existing": False}, 201)
    mm = re.match(r"^/api/conversations/(\d+)$", path)
    if mm:
        c = CONVS.get(int(mm[1]))
        if not c: return j({"error": "not found"}, 404)
        return j({"conversation": {"id": c["id"]}, "participants": [
            {"user_id": uid, "username": next(u["username"] for u in USERS.values() if u["id"] == uid),
             "public_key": next(u["public_key"] for u in USERS.values() if u["id"] == uid)} for uid in c["members"]]})
    mm = re.match(r"^/api/messages/(\d+)$", path)
    if mm: return j({"messages": []})
    if path == "/api/calls/start":
        conv = CONVS.get(body.get("conversation_id"))
        if not conv or me["id"] not in conv["members"] or body.get("callee_id") not in conv["members"]:
            return j({"error": "not a participant"}, 403)
        active = [c for c in CALLS.values() if c["conversation_id"] == body["conversation_id"] and c["status"] in ("initiated", "connected")]
        if active: return j({"error": "There's already an active call in this conversation"}, 409)
        NEXT["id"] += 1
        call = {"id": NEXT["id"], "conversation_id": body["conversation_id"], "caller_id": me["id"],
                "callee_id": body["callee_id"], "call_type": body.get("call_type", "voice"), "status": "initiated"}
        CALLS[call["id"]] = call
        return j({"call": call}, 201)
    mm = re.match(r"^/api/calls/(\d+)/accept$", path)
    if mm:
        call = CALLS.get(int(mm[1]))
        if not call or call["callee_id"] != me["id"] or call["status"] != "initiated": return j({"error": "not found"}, 404)
        call["status"] = "connected"; return j({"call": call})
    mm = re.match(r"^/api/calls/(\d+)/end$", path)
    if mm:
        call = CALLS.get(int(mm[1]))
        if not call or me["id"] not in (call["caller_id"], call["callee_id"]): return j({"error": "not found"}, 404)
        call["status"] = body.get("status", "ended"); return j({"call": call})
    return j({"error": "unmocked " + path}, 404)


RELAY_TOPICS = {}
RELAY_SEQ = {"n": 0}

def relay_handle(path, method, body, url):
    import json as _json
    from urllib.parse import urlparse, parse_qs
    if path in ("/api/__relay/join", "/api/__relay/leave"):
        return None   # handled by fulfilling below in api(); see wrapper
    if path == "/api/__relay/publish":
        RELAY_SEQ["n"] += 1
        RELAY_TOPICS.setdefault(body["topic"], []).append({"seq": RELAY_SEQ["n"], **{k: v for k, v in body.items() if k != "topic"}})
        return {"ok": True}
    if path == "/api/__relay/poll":
        q = parse_qs(urlparse(url).query)
        topic, since = q["topic"][0], int(q.get("since", ["0"])[0])
        msgs = [m for m in RELAY_TOPICS.get(topic, []) if m["seq"] > since]
        cursor = msgs[-1]["seq"] if msgs else since
        return {"messages": msgs, "cursor": cursor}
    return {"ok": True}


FAKE_SUPABASE_JS = (HERE / "fake_supabase.js").read_text()

def new_page(browser, **kw):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, service_workers="block", color_scheme="dark", **kw)
    ctx.add_init_script(FAKE_SUPABASE_JS)
    pg = ctx.new_page(); errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda mm: mm.type == "error" and "Failed to load resource" not in mm.text and errs.append(mm.text))
    pg.route(re.compile(r"^https?://(?!localhost)"), lambda r: r.abort())
    pg.route(BASE + "/api/**", api)
    return ctx, pg, errs

def register(pg, username):
    pg.goto(BASE); pg.wait_for_selector("#auth-view:not([hidden])"); pg.click("#tab-register")
    pg.fill("#username", username); pg.fill("#phone-number", "09039930006"); pg.fill("#email", f"{username}@x.com"); pg.fill("#password", "secret12"); pg.click("#submit-btn")
    pg.wait_for_selector("#chat-shell:not([hidden])"); pg.wait_for_selector(".conv, .side-empty")
    pg.wait_for_function("myKeyStatus === 'ready'")

def open_chat(pg, nth=0):
    pg.click(f".conv >> nth={nth}"); pg.wait_for_function("keyInfo !== null && document.querySelector('#thread-view').hidden === false"); pg.wait_for_timeout(200)

def ok(m): print("  ok:", m)

FAKE_MEDIA_ARGS = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream", "--mute-audio"]
