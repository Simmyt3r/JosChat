"""
Run:  python tests/e2e/serve.py &   then   python tests/e2e/run_webrtc.py

Two real Chromium browsers, fake camera/mic, REAL RTCPeerConnection negotiation.
Only the signalling *transport* is stood in for (an HTTP-polling relay behind the
mocked API, see tests/e2e/fake_supabase.js) because real Supabase Realtime is not
reachable from a sandbox. app.js's own channel/broadcast/presence calls, the
offer/answer/ICE logic, and the WebRTC stack are all real.
"""
import json, re, sys, time, uuid
from call_mock import *

def relay_msgs(kind=None, event=None):
    out = []
    for topic, msgs in RELAY_TOPICS.items():
        if not topic.startswith("call-"): continue
        for m in msgs:
            if (kind is None or m["kind"] == kind) and (event is None or m.get("event") == event):
                out.append(m)
    return out

def stats(pg):
    return pg.evaluate("""async () => {
        const r = await activeCall.pc.getStats(); const out = {};
        r.forEach((s) => { if (s.type === 'inbound-rtp') out[s.kind] = { bytes: s.bytesReceived, packets: s.packetsReceived }; });
        return out; }""")

with sync_playwright() as p:
    br = p.chromium.launch(args=FAKE_MEDIA_ARGS)
    ca, alice, ea = new_page(br, permissions=["camera", "microphone"])
    cb, bob, eb = new_page(br, permissions=["camera", "microphone"])

    print("== 1. Two people, one direct chat")
    register(alice, "alice"); register(bob, "bob")
    aid, bid = alice.evaluate("currentProfile.id"), bob.evaluate("currentProfile.id")
    alice.fill("#chat-search", "bob"); alice.press("#chat-search", "Enter")
    alice.wait_for_function("!document.getElementById('video-call-btn').disabled")
    DIRECT = alice.evaluate("currentConversationId")
    ok(f"direct chat #{DIRECT}, call buttons enabled")

    print("== 2. Alice starts a video call; Bob does NOT answer for 3 seconds")
    alice.click("#video-call-btn")
    alice.wait_for_selector("#active-call-dialog[open]")
    call_id = alice.evaluate("activeCall.id")
    row = dict(CALLS[call_id])
    bob.evaluate("(row) => onIncomingCallRow(row)", row)      # what Realtime's INSERT event would deliver
    bob.wait_for_selector("#incoming-call-dialog[open]")
    alice.wait_for_timeout(3000)
    offers_before = len(relay_msgs("bc", "offer"))
    assert offers_before == 0, offers_before
    ok("while Bob hasn't picked up, Alice has NOT sent an offer (nothing to lose)")
    assert alice.evaluate("activeCall.phase") == "ringing-out"
    ok("Alice is still 'ringing-out'")

    print("== 3. Bob accepts late — the connection must still complete")
    bob.click("#accept-call-btn")
    bob.wait_for_selector("#active-call-dialog[open]")
    for who, pg in (("Alice", alice), ("Bob", bob)):
        pg.wait_for_function("activeCall && activeCall.pc && activeCall.pc.connectionState === 'connected'", timeout=25000)
        ok(f"{who}: RTCPeerConnection.connectionState === 'connected'")
    assert alice.evaluate("activeCall.phase") == "connected" and bob.evaluate("activeCall.phase") == "connected"
    ok("both sides moved to phase 'connected'")

    print("== 4. Glare-free signalling: exactly one offer, one answer")
    n_off, n_ans = len(relay_msgs("bc", "offer")), len(relay_msgs("bc", "answer"))
    assert (n_off, n_ans) == (1, 1), (n_off, n_ans)
    ok("1 offer (from the caller only), 1 answer (from the callee only)")
    off = relay_msgs("bc", "offer")[0]; ans = relay_msgs("bc", "answer")[0]
    assert off["sender"] != ans["sender"]; ok("offer and answer came from different peers")
    n_ice = len(relay_msgs("bc", "ice-candidate")); assert n_ice >= 2; ok(f"{n_ice} ICE candidates exchanged")

    print("== 5. Remote media really arrives (ontrack + real RTP bytes)")
    for who, pg in (("Alice", alice), ("Bob", bob)):
        n_tracks = pg.evaluate("document.getElementById('remote-video').srcObject.getTracks().length")
        assert n_tracks == 2, n_tracks
        assert pg.get_attribute("#call-stage", "data-remote-live") == "true"
        ok(f"{who}: remote stream has {n_tracks} tracks (audio+video), stage marked live")
    alice.wait_for_timeout(2500)
    for who, pg in (("Alice", alice), ("Bob", bob)):
        st = stats(pg)
        assert st.get("video", {}).get("bytes", 0) > 0 and st.get("audio", {}).get("bytes", 0) > 0, st
        ok(f"{who}: receiving real RTP — video {st['video']['bytes']} B, audio {st['audio']['bytes']} B")
    v_alice = alice.evaluate("(() => { const v = document.getElementById('remote-video'); return { w: v.videoWidth, h: v.videoHeight, t: v.currentTime }; })()")
    assert v_alice["w"] > 0 and v_alice["t"] > 0; ok(f"Alice's <video> is decoding frames: {v_alice['w']}x{v_alice['h']}, currentTime={v_alice['t']:.1f}s")

    print("== 6. Timers and status")
    alice.wait_for_timeout(1200)
    assert re.match(r"^\d\d:\d\d$", alice.inner_text("#active-call-status")) and re.match(r"^\d\d:\d\d$", bob.inner_text("#active-call-status"))
    ok(f"both show a running call timer: Alice {alice.inner_text('#active-call-status')}, Bob {bob.inner_text('#active-call-status')}")
    assert CALLS[call_id]["status"] == "connected"; ok("call log says 'connected'")
    alice.screenshot(path=str(SHOTS / "70_video_call_alice.png")); bob.screenshot(path=str(SHOTS / "71_video_call_bob.png"))

    print("== 7. Controls act on the real tracks, and the other side keeps its connection")
    alice.click("#toggle-mic-btn"); assert alice.evaluate("activeCall.localStream.getAudioTracks()[0].enabled") is False; ok("Alice muted: local audio track disabled")
    alice.click("#toggle-camera-btn"); assert alice.evaluate("activeCall.localStream.getVideoTracks()[0].enabled") is False
    assert alice.get_attribute("#call-stage", "data-camera-off") == "true"; ok("Alice camera off: track disabled, local preview hidden")
    assert bob.evaluate("activeCall.pc.connectionState") == "connected"; ok("Bob's connection unaffected")
    alice.click("#toggle-mic-btn"); alice.click("#toggle-camera-btn")
    assert alice.evaluate("activeCall.localStream.getAudioTracks()[0].enabled") and alice.evaluate("activeCall.localStream.getVideoTracks()[0].enabled"); ok("both toggle back on")

    print("== 8. Alice hangs up; Bob's side is told and cleans up completely")
    a_tracks = alice.evaluate("activeCall.localStream.getTracks().map(t => t.readyState)")
    assert set(a_tracks) == {"live"}
    alice.evaluate("window.__pc = activeCall.pc; window.__stream = activeCall.localStream")
    alice.click("#hangup-call-btn")
    alice.wait_for_function("!document.getElementById('active-call-dialog').open")
    assert alice.evaluate("activeCall === null")
    assert alice.evaluate("window.__pc.connectionState") == "closed"; ok("Alice's RTCPeerConnection is closed")
    assert alice.evaluate("window.__stream.getTracks().every(t => t.readyState === 'ended')"); ok("Alice's camera and microphone are released")
    assert CALLS[call_id]["status"] == "ended"; ok("call log says 'ended'")
    bob.evaluate("window.__pc = activeCall.pc; window.__stream = activeCall.localStream")
    bob.evaluate("(row) => onCallRowChanged(row)", dict(CALLS[call_id]))       # the Realtime UPDATE Bob would receive
    bob.wait_for_function("!document.getElementById('active-call-dialog').open")
    assert bob.evaluate("activeCall === null")
    assert bob.evaluate("window.__pc.connectionState") == "closed"
    assert bob.evaluate("window.__stream.getTracks().every(t => t.readyState === 'ended')"); ok("Bob is told the call ended and releases his camera/mic and connection too")
    assert bob.locator(".toast", has_text="Call ended").count() >= 1; ok("Bob sees a 'Call ended' notice")

    print("== 9. A fresh call works right after (no stale state)")
    alice.wait_for_function("!document.getElementById('voice-call-btn').disabled")
    alice.click("#voice-call-btn"); alice.wait_for_selector("#active-call-dialog[open]")
    call2 = alice.evaluate("activeCall.id"); assert call2 != call_id
    bob.evaluate("(row) => onIncomingCallRow(row)", dict(CALLS[call2])); bob.wait_for_selector("#incoming-call-dialog[open]")
    bob.click("#accept-call-btn")
    for pg in (alice, bob): pg.wait_for_function("activeCall && activeCall.pc && activeCall.pc.connectionState === 'connected'", timeout=25000)
    assert alice.get_attribute("#call-stage", "data-kind") == "voice" and alice.evaluate("activeCall.localStream.getVideoTracks().length") == 0
    ok("second call (voice-only) connects; no camera was requested")
    assert len(relay_msgs("bc", "offer")) == 2 and len(relay_msgs("bc", "answer")) == 2; ok("still exactly one offer/answer per call")
    bob.click("#hangup-call-btn")
    bob.wait_for_function("!document.getElementById('active-call-dialog').open")
    assert CALLS[call2]["status"] == "ended"; ok("callee hanging up also logs 'ended'")
    alice.evaluate("(row) => onCallRowChanged(row)", dict(CALLS[call2])); alice.wait_for_function("!document.getElementById('active-call-dialog').open")

    errs = ea + eb
    print("\nJS ERRORS:", errs if errs else "none")
    br.close()
