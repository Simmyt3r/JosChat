#!/usr/bin/env python3
"""Repeat JosChat's browser-based WebRTC integration test with per-trial evidence.

This exercises real Chromium RTCPeerConnections and synthetic media through
a local mock that STANDS IN FOR Supabase Realtime. These are NOT field calls.
"""
import argparse
import csv
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = ROOT / "tests/e2e/run_webrtc.py"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--output", default="call-evidence")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if not 1 <= args.trials <= 40:
        parser.error("--trials must be 1 to 40")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    outcomes = []
    for n in range(1, args.trials + 1):
        start = time.monotonic()
        try:
            result = subprocess.run([sys.executable, str(TEST)], cwd=ROOT, capture_output=True,
                                    text=True, timeout=args.timeout, check=False)
            log = (result.stdout or "") + "\nSTDERR:\n" + (result.stderr or "")
            video = "Alice: RTCPeerConnection.connectionState === 'connected'" in log and (
                "Bob: RTCPeerConnection.connectionState === 'connected'" in log)
            voice = "second call (voice-only) connects" in log
            success = result.returncode == 0 and video and voice
            error = None if success else "Nonzero exit or browser-level media checks failed"
            rc = result.returncode
        except subprocess.TimeoutExpired as e:
            log = "Test timed out after {0} seconds\n".format(args.timeout)
            if e.stdout:
                log += e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else e.stdout
            success, video, voice, error, rc = False, False, False, "Timeout", None
        seconds = round(time.monotonic() - start, 3)
        (output / "trial_{0:02d}.log".format(n)).write_text(log)
        outcomes.append({"trial": n, "passed": int(success), "video_connected": int(video),
                         "voice_connected": int(voice), "script_duration_seconds": seconds,
                         "exit_code": rc if rc is not None else "", "error": error or ""})
        print("WebRTC browser harness trial {0}/{1}: {2}".format(
            n, args.trials, "PASS" if success else "FAIL"), flush=True)
    with (output / "call_trials.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(outcomes[0]))
        writer.writeheader()
        writer.writerows(outcomes)
    passes = sum(case["passed"] for case in outcomes)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit": os.getenv("GITHUB_SHA", "unknown"),
        "environment": "local Chromium / fake camera and microphone / mock Supabase relay",
        "method": "run tests/e2e/run_webrtc.py in a fresh subprocess per trial",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "harness_trials": args.trials,
        "harness_passed": passes,
        "harness_failed": args.trials - passes,
        "harness_pass_percent": round(passes * 100.0 / args.trials, 2),
        "not_measured": [
            "Real Supabase signalling reliability",
            "Actual geographically separated devices or carrier networks",
            "Real-world call connection success including NAT and TURN",
            "True call setup latency (script duration includes deliberate sleeps)",
            "Historical 94.2% dissertation outcome",
        ],
    }
    (output / "call_evidence.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        "# WebRTC browser integration evidence",
        "",
        "Commit: " + report["commit"],
        "",
        "Repeated full browser-test passes: **{0}/{1} ({2}%)**".format(
            passes, args.trials, report["harness_pass_percent"]),
        "",
        "A passing trial runs two simulated users, establishes real video and "
        "audio RTCPeerConnections in Chromium with fake media, verifies RTP and teardown, "
        "then establishes a second voice-only call. Signalling is served by a mock.",
        "",
        "**Not a real-world call success rate.** Do not compare this harness percentage "
        "directly with the dissertation's 94.2% figure.",
        "",
        "No cross-network, cellular, remote Supabase, NAT/TURN, or live-user experiment "
        "is performed. See trial logs for actual browser observations.",
        "",
    ]
    report_md = "\n".join(lines)
    (output / "REPORT.md").write_text(report_md)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(report_md)
    print(report_md)
    if passes != args.trials:
        raise SystemExit("One or more WebRTC integration trials failed; see artifact logs")


if __name__ == "__main__":
    main()
