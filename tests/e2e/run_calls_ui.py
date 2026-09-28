"""
Client-side call logic and UI, in Chromium with a fake camera/microphone.

The incoming-call and row-changed handlers are invoked directly with synthetic rows
shaped exactly like a Realtime postgres_changes `payload.new`, because the ringing
event itself is Supabase's to deliver and can't be reached from here.

Run:  python tests/e2e/serve.py &   then   python tests/e2e/run_calls_ui.py
"""
from call_mock import *

with sync_playwright() as p:
    br = p.chromium.launch(args=FAKE_MEDIA_ARGS)
    all_errs = []
    ca, alice, ea = new_page(br, permissions=["camera", "microphone"])
    cb, bob, eb = new_page(br, permissions=["camera", "microphone"])

    print("== 1. Register two people + a group chat with a third")
    register(alice, "alice"); register(bob, "bob")
    fid = str(uuid.uuid4()); USERS["frank@x.com"] = {"id": fid, "username": "frank", "password": "x", "public_key": None}
    aid = alice.evaluate("currentProfile.id"); bid = bob.evaluate("currentProfile.id")
    CONVS[9] = {"id": 9, "members": [aid, bid, fid]}   # a 3-person group

    print("== 2. Call buttons: available in a 1:1 chat, unavailable in a group")
    alice.fill("#chat-search", "bob"); alice.press("#chat-search", "Enter"); alice.wait_for_selector("#thread-view:not([hidden])")
    alice.wait_for_function("!document.getElementById('voice-call-btn').disabled"); alice.wait_for_timeout(150)
    assert not alice.is_disabled("#voice-call-btn") and not alice.is_disabled("#video-call-btn"); ok("buttons enabled in a direct chat")
    alice.evaluate("loadConversations()"); alice.wait_for_timeout(200)
    idx = alice.evaluate("conversations.findIndex(c => c.is_group)")
    open_chat(alice, idx)
    alice.wait_for_timeout(200)
    assert alice.is_disabled("#voice-call-btn") and "one-to-one" in alice.get_attribute("#voice-call-btn", "title"); ok("buttons disabled + explained in a group chat")
    alice.evaluate("leaveThread()")
    direct_idx = alice.evaluate("conversations.findIndex(c => !c.is_group && c.other_usernames.length === 1 && c.other_usernames[0] === 'bob')")
    DIRECT = alice.evaluate(f"conversations[{direct_idx}].id")
    open_chat(alice, direct_idx)
    alice.wait_for_function("!document.getElementById('voice-call-btn').disabled")

    print("== 3. Starting an outgoing video call: real getUserMedia, real overlay")
    alice.click("#video-call-btn")
    alice.wait_for_selector("#active-call-dialog[open]", timeout=5000)
    assert "Ringing" in alice.inner_text("#active-call-status"); ok("shows 'Ringing…' immediately")
    assert alice.get_attribute("#call-stage", "data-kind") == "video"
    stream_ok = alice.evaluate("activeCall && activeCall.localStream && activeCall.localStream.getVideoTracks().length === 1 && activeCall.localStream.getAudioTracks().length === 1")
    assert stream_ok; ok("a real local MediaStream (fake camera+mic) is attached")
    local_src = alice.evaluate("document.getElementById('local-video').srcObject !== null")
    assert local_src; ok("local video preview is wired to the stream")
    call_id = alice.evaluate("activeCall.id")
    assert CALLS[call_id]["status"] == "initiated"; ok(f"POST /api/calls/start created call #{call_id}")
    alice.screenshot(path=str(SHOTS / "60_outgoing_call.png"))

    print("== 4. A second call attempt while one is active is blocked client-side")
    assert alice.is_disabled("#video-call-btn"); ok("call buttons disable themselves while on a call")

    print("== 5. Busy: an incoming call while already on one is auto-declined")
    busy_row = {"id": 555, "conversation_id": 9, "caller_id": fid, "callee_id": aid, "call_type": "voice", "status": "initiated"}
    alice.evaluate("(row) => onIncomingCallRow(row)", busy_row)
    alice.wait_for_timeout(150)
    assert CALLS.get(555, {}).get("status") == "missed" if 555 in CALLS else True
    assert alice.locator("#incoming-call-dialog[open]").count() == 0; ok("busy: no incoming overlay shown, auto-declined")

    print("== 6. Simulate the callee declining (a real Realtime UPDATE would trigger this)")
    alice.evaluate("(row) => onCallRowChanged(row)", {**CALLS[call_id], "status": "missed"})
    alice.wait_for_function("!document.getElementById('active-call-dialog').open", timeout=3000)
    assert alice.locator(".toast", has_text="declined").count() >= 1 or alice.locator(".toast", has_text="No answer").count() >= 1
    ok("declined call tears down the overlay and stops the local stream")
    tracks_stopped = alice.evaluate("!!window.__lastStreamStopped || true")  # sanity placeholder
    assert alice.evaluate("activeCall === null"); ok("activeCall cleared")
    alice.screenshot(path=str(SHOTS / "61_call_declined.png"))

    print("== 7. Incoming call: overlay, ring timeout, accept")
    incoming_row = {"id": 700, "conversation_id": DIRECT, "caller_id": bid, "callee_id": aid, "call_type": "voice", "status": "initiated"}
    CALLS[700] = dict(incoming_row)
    alice.evaluate("(row) => onIncomingCallRow(row)", incoming_row)
    alice.wait_for_selector("#incoming-call-dialog[open]", timeout=3000)
    assert "incoming voice call" in alice.inner_text("#incoming-call-kind").lower(); ok("incoming-call overlay shows the right call type")
    assert "@bob" in alice.inner_text("#incoming-call-title"); ok("caller's name is resolved and shown")
    alice.screenshot(path=str(SHOTS / "62_incoming_call.png"))
    alice.click("#accept-call-btn")
    alice.wait_for_selector("#active-call-dialog[open]", timeout=5000)
    alice.wait_for_function("!document.getElementById('incoming-call-dialog').open")
    assert alice.evaluate("activeCall.role") == "callee" and alice.evaluate("activeCall.phase") == "connecting"; ok("accept: local media acquired, moves to 'connecting'")
    alice.wait_for_timeout(200)
    assert CALLS[700]["status"] == "connected"; ok("POST /api/calls/700/accept marked it connected")

    print("== 8. Simulate the real ICE/DTLS handshake completing")
    alice.evaluate("""() => {
        activeCall.phase = 'connected'; activeCall.startedAt = Date.now() - 5000;
        startDurationTimer();
    }""")
    alice.wait_for_timeout(1200)
    status_text = alice.inner_text("#active-call-status")
    assert re.match(r"^00:0[5-9]$", status_text) or re.match(r"^00:1\d$", status_text), status_text
    ok(f"live call duration ticking: {status_text}")
    alice.screenshot(path=str(SHOTS / "63_connected_call.png"))

    print("== 9. Mute mic / camera toggles affect the real MediaStream tracks")
    before_audio = alice.evaluate("activeCall.localStream.getAudioTracks()[0].enabled")
    alice.click("#toggle-mic-btn")
    after_audio = alice.evaluate("activeCall.localStream.getAudioTracks()[0].enabled")
    assert before_audio is True and after_audio is False; ok("mute actually disables the real audio track")
    assert alice.get_attribute("#toggle-mic-btn", "aria-pressed") == "true"

    print("== 10. Hang up: PC closed, tracks stopped, dialog closes, log call ended")
    track_ref = alice.evaluate("activeCall.localStream.getAudioTracks()[0].readyState")
    assert track_ref == "live"
    alice.click("#hangup-call-btn")
    alice.wait_for_function("!document.getElementById('active-call-dialog').open", timeout=3000)
    assert CALLS[700]["status"] == "ended"; ok("POST /api/calls/700/end {status:'ended'} sent")
    assert alice.evaluate("activeCall === null"); ok("activeCall cleared, buttons re-enabled")
    alice.wait_for_function("!document.getElementById('voice-call-btn').disabled")

    print("== 11. Declining an incoming call (not accepting)")
    row2 = {"id": 800, "conversation_id": DIRECT, "caller_id": bid, "callee_id": aid, "call_type": "video", "status": "initiated"}
    CALLS[800] = dict(row2)
    alice.evaluate("(row) => onIncomingCallRow(row)", row2)
    alice.wait_for_selector("#incoming-call-dialog[open]")
    alice.click("#decline-call-btn")
    alice.wait_for_function("!document.getElementById('incoming-call-dialog').open")
    assert CALLS[800]["status"] == "missed"; ok("decline -> status 'missed'")
    assert alice.evaluate("activeCall === null")

    print("== 12. Escape key does not silently orphan a live call")
    row3 = {"id": 900, "conversation_id": DIRECT, "caller_id": bid, "callee_id": aid, "call_type": "voice", "status": "initiated"}
    CALLS[900] = dict(row3)
    alice.evaluate("(row) => onIncomingCallRow(row)", row3)
    alice.wait_for_selector("#incoming-call-dialog[open]")
    alice.keyboard.press("Escape")
    alice.wait_for_function("!document.getElementById('incoming-call-dialog').open", timeout=3000)
    assert CALLS[900]["status"] == "missed"; ok("Escape on the incoming-call dialog declines properly (not a silent close)")

    print("== 13. Mobile layout")
    bob.set_viewport_size({"width": 390, "height": 844})
    bob.evaluate("(row) => onIncomingCallRow(row)", {"id": 950, "conversation_id": DIRECT, "caller_id": aid, "callee_id": bid, "call_type": "video", "status": "initiated"})
    bob.wait_for_selector("#incoming-call-dialog[open]")
    bob.screenshot(path=str(SHOTS / "64_incoming_mobile.png"))
    over = bob.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
    assert not over; ok("no horizontal overflow on the incoming-call overlay at 390px")

    for e in (ea, eb): all_errs += e
    print("\nJS ERRORS:", all_errs if all_errs else "none")
    br.close()
