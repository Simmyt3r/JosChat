# JosChat Dissertation — reproducible research evidence

**Status:** CI-based experimental evidence collection, not a claim that the dissertation's
historical measurements were reproduced. All runs are tied to the tested Git commit.
The repository's ledger is a **centrally operated PostgreSQL SHA-256 hash chain**,
not a multi-node distributed consensus blockchain.

## Run the evidence workflow

GitHub → Actions → **Dissertation Research Evidence** → **Run workflow** (or open
a pull request that edits the scripts, tests or the hash-chain implementation).

The workflow has two separate jobs. Each publishes a ZIP on its run page under
**Artifacts**. For a dissertation, record the run URL, commit SHA, date, runner
specifications, method, and raw rows from the artifacts.

### 1. PostgreSQL integrity and timings

Run scripts/collect_research_evidence.py against a **disposable** PostgreSQL
database containing 'test' in its name. The script **drops and recreates schemas**.

It executes the repository's actual supabase/schema.sql hash-chain functions.
The Python reference implementation is **not** substituted for the database.
A deterministic 498-record dataset is generated with opaque synthetic strings.
150 records are selected by a recorded seed and their ciphertext is modified.
348 records are left unchanged.

For each message it calls validate_message() and records both expected tampering
status and observed detection. Confusion matrix:

- **TP:** tampered record flagged.
- **FN:** tampered record not flagged.
- **TN:** unchanged record accepted.
- **FP:** unchanged record incorrectly flagged.
- **Sensitivity (TPR):** TP / (TP + FN).
- **Specificity (TNR):** TN / (TN + FP).
- **False-positive rate:** FP / (FP + TN).
- **Overall accuracy:** (TP + TN) / (TP + TN + FP + FN).

Reproducing **150 TP, 348 TN, 0 FP and 0 FN** shows that the *present implementation*
correctly identifies this *specific synthetic test set* in the runner environment.
It does **not** prove that the original dissertation study performed exactly
these trials, or establish universal protection against attackers.

The script also probes modified block hashes, broken predecessor links,
reassigned ciphertext, deleted message rows, deleted chain tails and privileged
complete-chain rewriting. **Known limitations must be reported, not concealed**:
with no independently protected commitment to the chain tip, a privileged
attacker can rewrite all blocks and their hashes; deleting message rows or
truncating the newest block can also escape these checks. The report records
both detected and undetected probes.

For chain timing, the script runs validate_chain() on 100, 500, 1,000 and 5,000
generated blocks, 15 times each. It stores individual milliseconds plus mean,
median, standard deviation and p95. Each timed sample includes query round
trip on the runner. These values may differ from **0.38 seconds for 500 blocks**
due to hardware, PostgreSQL, workload, and test method. Do not change the tests
simply to match a target number.

Artifact: joschat-postgres-research-evidence, containing evidence.json,
integrity_cases.csv (all 498 cases), attack_probes.csv, timing_trials.csv,
and REPORT.md.

### 2. WebRTC browser-level regression

The second job runs real Chromium with two browser sessions, fake camera and
microphone, RTCPeerConnection, RTP exchange and call cleanup, repeated five
times. It runs tests/e2e/run_webrtc.py without altering its pass criteria.
**Supabase Realtime is mocked**, so this specifically validates browser-side
WebRTC and local signalling logic, not real-world cross-network reliability.

Artifact: joschat-chromium-webrtc-harness, containing per-trial CSV,
individual logs, a JSON report, and the mock server's log. The pass percentage
for this harness is **not** a field call success percentage and cannot
independently validate the dissertation's reported **94.2%**.

## Measurements that still need original evidence

| Dissertation statement | Supporting evidence still needed |
|---|---|
| 150 TP, 348 TN, no FP/FN | If claiming a historical experiment: original logs, modification procedure, scripts and environment. A new CI run is a distinct reproduction. |
| Mean message delivery 1.6 s | Sender and receiver timestamps from real clients; trials, definition of delivery, networks, sizes, failures, distribution. |
| 500-block validation 0.38 s | Original timing samples, hardware, algorithm revision and measurement method; fresh runner timings are separate. |
| WebRTC call connection success 94.2% | Original call attempt log with successful and failed counts, total, network, devices and establishment criteria. Do not guess the denominator. |
| User acceptance / reduction of insecurity | Participant, task, consent or field study evidence; unit tests do not substantiate these claims. |

Automated PASS results are not a replacement for human trial records. Clearly
distinguish historical results, CI reproductions and work still proposed.

## Local reproduction

Create a **throwaway PostgreSQL 16** database called joschat_test, then:

~~~bash
pip install -r requirements.txt pytest
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/joschat_test
python -m pytest tests/test_schema_integration.py -q
export JOSCHAT_EVIDENCE_ALLOW_RESET=1
python scripts/collect_research_evidence.py --output research-evidence --repeats 15 --seed 20261103
~~~

The schema integration tests and evidence script both reset the test database;
never use a production URL. Python/SQL versions and runner details are captured
in artifacts. For browser tests, install Playwright and Chromium, start
tests/e2e/serve.py, and run scripts/collect_call_evidence.py.
See tests/e2e/README.md.

## Security interpretation

- Client-side direct-chat E2EE uses browser-held ECDH P-256 keys and AES-GCM,
  while other modes may use passphrases; forward secrecy is not guaranteed.
- Cloudinary attachment **references** are encrypted; actual file bytes are
  uploaded as-is. Avoid claiming all media is end-to-end encrypted.
- CI runner localhost testing does not simulate real cellular packet loss,
  intermittent service, TURN relay behavior or provider availability.
- Use the **scope-qualified** term "tamper evidence"; do not claim immutable
  history against a privileged administrator without external anchoring.
