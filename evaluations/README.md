# Joschat evaluation evidence

These are controlled laboratory results for the five metrics in the supplied
project document, §3.6. They are not field-pilot observations or production SLAs.
The raw JSON files identify their environment and measurements.

| Metric | Observed result | Scope |
| --- | --- | --- |
| Tamper detection | 7/7 deliberate changes detected | Text, attachment reference, missing row, block hash, tail removal, index gap, coherent database rewrite |
| False positives | 0/1000 untampered messages flagged | New full-payload format |
| Send to recipient display | 3961.341 ms mean, 5 samples | Real Chromium/AES-GCM and local HTTP fixture; actual four-second polling fallback |
| Call connections | 2/2 attempts connected | Real voice/video WebRTC and RTP on loopback; fake camera/mic and signalling transport |

Full ledger audit timings include payload and server-signed receipt checks:

| Blocks | Mean validation time | Repeats |
| --- | --- | --- |
| 10 | 12.122 ms | 5 |
| 100 | 43.483 ms | 5 |
| 1000 | 181.671 ms | 5 |

Local SQL engine: PostgreSQL 18.3 (PGlite 0.5.8) on wasm32-unknown-emscripten, compiled by emcc (Emscripten gcc/clang-like replacement + linker emulating GNU ld) 3.1.74 (1092ec30a3fb1d46b1782ff1b4db5094d3d06ae5), 32-bit. These timings characterize this test runtime,
not Supabase production. GitHub CI separately runs all SQL/RLS regression checks and
this evaluator against real Postgres 16.

## Reproduce

Use only a disposable database whose name contains `test`. The evaluator drops and
recreates application schemas.

```bash
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/joschat_test python tests/evaluate_integrity.py --output evaluations/integrity-ci.json
JOSCHAT_EVALUATION_OUTPUT=evaluations/messaging-ci.json python tests/e2e/run_completion_ui.py
JOSCHAT_EVALUATION_OUTPUT=evaluations/calls-ci.json python tests/e2e/run_call_checks.py run_webrtc.py
```

The browser tests prove application behaviour with the transport boundaries stated
above. Notification OS presentation is stubbed in headless shell; endpoint ownership,
subscription validation and durable dispatch/retry use real SQL with external push
transport mocked. Real browser delivery through FCM/APNs/Mozilla, live Supabase
Realtime, restrictive NATs and a Jos user pilot still need deployment/field testing.
TURN support is implemented; a relay provider's credentials are required to use it.
