# Browser-level tests

These drive real Chromium (via Playwright) against the real front end. They are **not**
part of `pytest` (they need a browser, and the file names deliberately don't match
pytest's patterns), and they are not run in CI.

```bash
pip install playwright && playwright install chromium
python tests/e2e/serve.py &              # the real app on :5055, dummy credentials
python tests/e2e/run_calls_ui.py         # call UI / state machine, 13 scenarios
python tests/e2e/run_webrtc.py           # two real browsers, a real WebRTC call
```

Both use Chromium's fake camera and microphone, so `getUserMedia` succeeds for real.

## What is real, and what is stood in for

Supabase Realtime can't be reached from a sandbox, so `fake_supabase.js` replaces
`supabase-js` in the page and talks to a small HTTP-polling relay inside the mock
(`call_mock.py`). Everything the app itself does is real: `app.js`'s channel,
broadcast and presence calls, the offer/answer/ICE logic, and the browser's
`RTCPeerConnection`, camera, RTP and video decoding.

What that means for what is and isn't proven:

- **Proven:** the caller waits for the callee to join before sending an offer (so a
  late answer loses nothing); exactly one offer and one answer per call; the
  connection reaches `connected`; real audio/video bytes flow both ways; hang-up
  releases the camera, microphone and connection on both sides; a second call works
  straight after.
- **Not proven here:** that Supabase delivers the `calls` INSERT/UPDATE events and the
  Broadcast/Presence messages the way this relay does. `run_calls_ui.py` and
  `run_webrtc.py` hand those events to `onIncomingCallRow` / `onCallRowChanged`
  directly, shaped like a real `postgres_changes` payload. Check this on the deployed
  site with two accounts.
- **Not proven:** calls across different networks. Both peers here are on one machine,
  so no NAT traversal is exercised, and only a public STUN server is configured (no TURN).
