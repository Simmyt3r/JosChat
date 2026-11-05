# Security revision and deployment

This change hardens call signalling, adds configurable STUN/TURN servers, checks
hash-chain structure more strictly, and supports comparison against a separately
retained historical checkpoint. It does not encrypt attachment bytes or provide
distributed consensus. Existing dissertation measurements refer to their recorded
source commit and must not be silently relabelled as results for this revision.

## Deploy in this order

1. Back up the database, then apply these migrations through the Supabase SQL
   editor or your normal migration tooling:
   - `supabase/migrations/20261028120000_private_call_signalling.sql`
   - `supabase/migrations/20261028120100_chain_index_validation.sql`
2. In Supabase Realtime settings, disable **Allow public access**. Private-channel
   RLS does not protect a public channel. Disconnect existing clients when making
   this change. Old clients need to reload the updated frontend to call again.
3. Deploy the updated backend and frontend together. Calls now join private
   `call-<id>` channels after supplying the authenticated user's Realtime token.
   Channel rejection or timeout ends setup with an error; it does not fall back
   to a public channel.
4. Optionally set `WEBRTC_ICE_SERVERS` to a JSON array of your STUN/TURN entries.
   TURN entries require `username` and `credential`. Use short-lived credentials
   from your relay provider and rotate them before expiry. They are delivered
   only through authenticated `GET /api/calls/ice-servers`, never `/api/config`.
   Any active signed-in user can read relay credentials; these are not a place
   for your provider's master API key. STUN remains the default when unset.
5. Leave `CORS_ORIGINS` unset for the same-origin PWA. If another frontend is
   needed, list its exact origins, separated by commas. Wildcards are rejected.
6. Verify two real accounts can call. A third account, anonymous client,
   suspended account, and a conversation member who is not the caller or callee
   must be unable to join a private call channel. Confirm media on different
   networks with TURN enabled; a local mock does not establish NAT traversal.

The policy helper requires an active profile, current conversation membership,
an active call, and identity matching the caller or callee. Restrictive policies
also block older broad *private-channel* grants from exposing call topics.
Realtime authorizes when channels join: ending a call or suspending an account
is not guaranteed to immediately revoke an already-authorized connection.
A compromised endpoint or allowed call peer remains outside this protection.

## Checkpoints and their trust boundary

An administrator can obtain `GET /api/admin/blockchain/checkpoint`, which returns
`checkpoint_index` and `checkpoint_hash` only when the current chain is
consistent. Retain that observation outside the database and its backups, with
an authenticated record of when and how it was captured. It is not signed by an
independent witness.

Later supply both fields to `GET /api/admin/blockchain/validate` as query
parameters. `checkpoint_matches` is false if the historical block has changed
or disappeared, and the audit's `is_valid` becomes false. Later legitimate
appends are allowed. Comparing only to the current server's newly exported
checkpoint would not detect a consistent rewrite. An attacker controlling the
backend can falsify its responses; use independent verification and authenticated
external witnesses for that stronger threat model.

The Python reference now validates the first block, consecutive indices and
sender/conversation field pairing, and accepts an optional `(index, hash)`
checkpoint in `validate_chain`. Explicitly importing an empty list is invalid
rather than silently creating a new genesis block. Python and SQL still have
different timestamp encodings; Python snapshots are not production SQL exports.
The production SQL validator now checks consecutive indices from zero. Hashes
are not rewritten. Final-block removal remains indistinguishable from a valid
prefix without a trusted expected checkpoint that the removal crosses.

## Response and diagnostic protection

API responses, including authentication errors and configuration, use
`Cache-Control: no-store`. Responses also set `nosniff`, deny framing and omit
referrer information. Missing configuration details remain in server logs and
are returned publicly only when debug mode is explicitly enabled. These headers
are not a comprehensive CSP, XSS audit or rate limiter.

## Validation

Run `python -m pytest -q` with an isolated `TEST_DATABASE_URL` whose database name
contains `test`. The fixtures recreate schemas and must never target production.
The SQL tests exercise permissions as browser roles, including an older broad
Realtime policy, suspension, conversation removal, ended calls, migration replay
and rehashed index gaps. Without a test database those tests are skipped, not
passed. CI provides PostgreSQL 16 for them.

Run `node --test tests/js/*.test.js`. The call tests execute the actual setup
function with controlled boundaries and cover private-channel selection, relay
configuration, rejected setup, hang-up during configuration loading and channel
authorization failure. They do not validate the live Supabase service.

If a local PostgreSQL service is unavailable, an optional isolated PostgreSQL-in-
WASM smoke test exercises schema replay, both call peers, outsider and anonymous
denials under a broad policy, suspension, ended calls and a rehashed index gap:

```bash
npm install --prefix /tmp/joschat-sql @electric-sql/pglite
PGLITE_PACKAGE_ROOT=/tmp/joschat-sql/node_modules/@electric-sql/pglite node tests/sql/security_smoke.mjs
```

This checks PostgreSQL SQL/RLS behaviour in PGlite; it is not a live Supabase
Realtime or full psycopg2 integration test.
