# Joschat — Flask API (Supabase + Cloudinary + Vercel)

Secured messaging and calling backend for the Joschat project. Message
integrity is guaranteed by an append-only, SHA-256 hash chain ("blockchain
layer") implemented as atomic Postgres functions inside Supabase.

## Architecture at a glance

```
 Browser (PWA)
   │
   ├── REST calls ─────────────► Flask API (this repo) ─────► Postgres (direct connection)
   │   (auth, send message,          │                          profiles, conversations,
   │    upload media, admin)         │                          messages, blocks, calls
   │                                 │                          + add_block()/validate_chain()
   │                                 └────────────────────────► Cloudinary (media)
   │
   └── Direct connection ──────► Supabase Auth (sign up/in, token verification)
       (live new-message events,     Supabase Realtime (Postgres change-stream + Broadcast
        WebRTC call signalling)       pub/sub — no Flask/Socket.IO server needed)
```

**Why no Flask-SocketIO?** Vercel's Python runtime executes each request
as a short-lived serverless function — it cannot hold open a persistent
WebSocket connection the way a traditional server can. Supabase already
runs a managed Realtime service, so instead of fighting the platform we
lean on it: the Flask API stays a simple, stateless REST API (ideal for
Vercel), and the browser subscribes to Supabase directly for anything
that needs to be "live" (new messages, call signalling).

**Why direct Postgres instead of the Supabase REST client?** This
project's environment doesn't provision a `service_role`/secret key —
only `SUPABASE_ANON_KEY` (or `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`) plus
direct `POSTGRES_URL` credentials. Rather than fight Row Level Security
with an under-privileged key, Flask connects straight to Postgres with
those credentials (the `postgres` role, which bypasses RLS by design) for
every table operation, and only calls out to Supabase's Auth service for
sign-up/sign-in/token verification, which has no direct-SQL equivalent.
RLS policies in `supabase/schema.sql` remain in force for anything the
*browser* touches directly (Realtime subscriptions, in this app).

**Why is the blockchain logic inside Postgres, not Python?** A serverless
function has no persistent memory between requests, so a pure-Python
`Blockchain` object (see `blockchain.py`) can't safely track "the last
block" across concurrent requests — two messages sent at the same instant
could both read the same "last block" and corrupt the chain. The
`add_block()` SQL function in `supabase/schema.sql` takes an exclusive
table lock and does the read-and-append in one atomic transaction, so it
is safe under concurrency. `blockchain.py` is kept as a small, readable,
directly-runnable reference implementation of the same algorithm.

**What a block contains.** Each block records its `index`, a timestamp, the
SHA-256 of the *encrypted* message (`message_hash` — never the plaintext), the
`sender_id` and `conversation_id` of the message, the previous block's hash,
and its own `block_hash`, computed over all of those with
`compute_block_hash()` (`sha256(index|created_at|message_hash|sender_id|conversation_id|previous_hash)`).
Because the sender and conversation are part of the hash, neither can be
changed afterwards without breaking the chain, and `validate_conversation()`
also checks that a block names the same sender and conversation as the message
row it belongs to.

## Project layout

```
JosChat/
├── app.py                  Flask app factory + routes registration
├── config.py                Environment-variable configuration
├── extensions.py             Postgres connection helper + Supabase Auth + Cloudinary setup
├── blockchain.py             Reference Python implementation (see above)
├── api/index.py               Vercel serverless entry point
├── vercel.json                Vercel routing config
├── requirements.txt
├── .env.example
├── routes/
│   ├── auth.py                Register / login / logout / me
│   ├── conversations.py       Create/list conversations
│   ├── messages.py             Send / fetch / verify (blockchain-backed)
│   ├── media.py                 Cloudinary upload/delete
│   ├── admin.py                  User management + full chain audit
│   └── calls.py                   Call session logging
├── utils/auth_helpers.py       @require_token / @require_auth / @require_admin decorators
├── supabase/schema.sql         Tables, RLS policies, add_block()/validate_chain() (safe to re-run)
├── templates/index.html         Demo UI
├── tests/                       pytest suite (unit tests + optional Postgres schema tests)
└── static/
    ├── js/app.js                 Demo client (auth, chats, messaging; calling is a sketch)
    ├── manifest.json              PWA manifest
    └── sw.js                       Service worker (served at /sw.js; network-first)
```

## Setup

### 1. Create a Supabase project and run the schema

1. Go to [supabase.com](https://supabase.com) → New Project.
2. Open **SQL Editor** → New query, paste the entire contents of
   `supabase/schema.sql`, and run it. This creates all tables, the
   `add_block`/`validate_chain`/`validate_conversation` functions, RLS
   policies, and enables Realtime on `messages`.

> Already ran an older `schema.sql`? Run the current one again — it is
> idempotent, and it applies two fixes to an existing database: the hash
> functions now pin the time zone to UTC (otherwise `validate_chain()` can report
> false "tampering" when checked from a session in a different time zone), and
> `add_block`/`validate_chain`/`validate_conversation` are no longer callable by
> the public `anon`/`authenticated` roles through Supabase's auto-generated
> `/rest/v1/rpc/...` API.

#### Upgrading an existing database (blocks that don't record sender/conversation yet)

Older versions of `add_block` took only the message hash. The current
`schema.sql` adds `sender_id` / `conversation_id` to `blocks` and a three-argument
`add_block(message_hash, sender_id, conversation_id)`. Existing blocks are **not**
rewritten: they keep empty IDs and keep validating under the original hash
formula, so a chain can mix old and new blocks. To upgrade without downtime,
in this order:

1. Run the current `supabase/schema.sql` in the SQL Editor. It is additive, and the
   old one-argument `add_block(text)` keeps working, so the deployed app is unaffected.
2. Deploy the new application code (it calls the three-argument `add_block`).
3. Once the new code is live, remove the old function:
   `drop function if exists add_block(text);`

### 2. Get your environment variables

**If you're using Vercel's Supabase integration** (Vercel Dashboard →
your project → Storage → connect a Supabase database), the following are
injected automatically — you don't need to copy them by hand:

```
SUPABASE_URL
SUPABASE_ANON_KEY                        (or NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY)
POSTGRES_URL
POSTGRES_URL_NON_POOLING
POSTGRES_HOST
POSTGRES_DATABASE
POSTGRES_PASSWORD
```

Run `vercel env pull .env` locally to download them into a `.env` file.

**If you're not using the Vercel integration**, get the equivalents from
the Supabase dashboard: `Project Settings → API` for the URL/anon key,
and `Project Settings → Database → Connection string` for the Postgres
values.

### 3. Create a Cloudinary account

1. Go to [cloudinary.com](https://cloudinary.com) → sign up (free tier is fine).
2. From the Dashboard, copy `Cloud name`, `API Key`, `API Secret` into
   your `.env` (see `.env.example`).

### 4. Install dependencies and run locally

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

The app runs at `http://localhost:5000`. Open it in a browser to use the
demo UI, or hit the JSON API directly (see "API reference" below).

**Using the demo UI:** create an account for each person, log in, and type the other
person's username in the search box to start a chat. When both people have set up
their **secure keys** (this happens automatically when an account is created or first
logs in), the chat is protected with them and no passphrase is needed. Otherwise, or
for group chats, both of you enter the same **shared passphrase** for that chat (agree
on it outside Joschat). Either way, messages are encrypted in the browser and the
server never sees a key or the plaintext.

Running against a local, non-TLS Postgres instead of Supabase? Set
`POSTGRES_SSLMODE=disable` (the default, `require`, is what Supabase needs).

### Running the tests

```bash
pip install pytest
pytest -q                                   # unit tests (no network, no database)

# Also exercise supabase/schema.sql on a real (throw-away!) Postgres:
createdb joschat_test
TEST_DATABASE_URL=postgresql://localhost/joschat_test pytest -q
```

### 5. Supabase Auth settings

Sign-up works whether **Confirm email** (Authentication → Providers → Email) is on
or off. With it **on** (the default), new users must click the link in the
confirmation email before they can log in; the UI says so instead of showing
"invalid password". While testing you can turn it off to skip that step.

### 6. Promote your first admin user

Registration always creates a `role='user'` profile. To promote someone
to admin, run this in the Supabase SQL editor:

```sql
update profiles set role = 'admin' where username = 'your_username';
```

## Deploying to Vercel

```bash
npm install -g vercel     # if you don't already have the CLI
vercel login
vercel
```

If you connected Supabase through Vercel's integration, the environment
variables are already set for you in the project. Otherwise, add them
under **Project Settings → Environment Variables** for both Production
and Preview, then redeploy.

## API reference (all responses are JSON)

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| GET | `/api/health` | – | Liveness check |
| GET | `/api/config` | – | Public Supabase URL/anon key for the frontend |
| POST | `/api/auth/register` | – | `{username, email, password, phone_number?, public_key?}` |
| POST | `/api/auth/login` | – | `{email, password}` → `{access_token, refresh_token, profile}` (`profile` is `null` if the account has no profile row yet) |
| POST | `/api/auth/refresh` | – | `{refresh_token}` → a fresh `{access_token, refresh_token, profile}` |
| POST | `/api/auth/profile` | Bearer | `{username, phone_number?}` — create the profile row for an account that has none |
| PUT | `/api/auth/public-key` | Bearer | `{public_key}` — set or replace your own end-to-end encryption public key (base64 of an uncompressed P-256 point; anything else, including a private key, is rejected) |
| POST | `/api/auth/logout` | Bearer | Invalidate the current session |
| GET | `/api/auth/me` | Bearer | Current user's profile |
| POST | `/api/conversations` | Bearer | `{participant_usernames: [...] \| participant_ids: [...], is_group?, title?}` — returns the existing conversation (200) if the pair already has one |
| GET | `/api/conversations` | Bearer | List the caller's conversations |
| GET | `/api/conversations/<id>` | Bearer | Conversation + participants |
| POST | `/api/messages/send` | Bearer | `{conversation_id, encrypted_content, encrypted_media?}` — `encrypted_media?` is ciphertext of `{url, public_id, ...}`; the plaintext `media_url?`/`media_public_id?` fields are still accepted too (for any older or other client) but the bundled client never sends them |
| GET | `/api/messages/<conversation_id>` | Bearer | Message history (paginated via `?before=`) |
| GET | `/api/messages/verify/<conversation_id>` | Bearer | Per-message blockchain verification |
| POST | `/api/media/upload` | Bearer | multipart `file` (+ `conversation_id`) → Cloudinary URL |
| DELETE | `/api/media/<public_id>` | Bearer | Remove a Cloudinary asset |
| POST | `/api/calls/start` | Bearer | `{conversation_id, callee_id, call_type}` |
| POST | `/api/calls/<id>/end` | Bearer | `{status?}` |
| GET | `/api/calls/<conversation_id>` | Bearer | Call history for a conversation |
| GET | `/api/admin/users` | Admin | List all users |
| POST | `/api/admin/users/<id>/suspend` | Admin | Suspend a user |
| POST | `/api/admin/users/<id>/reinstate` | Admin | Reinstate a user |
| GET | `/api/admin/blockchain/validate` | Admin | Full-chain integrity audit |
| GET | `/api/admin/stats` | Admin | Platform-wide counters |

`Bearer` = `Authorization: Bearer <access_token>` header, using the token
returned by `/api/auth/login`.

## Testing the blockchain logic in isolation

```bash
python blockchain.py
```

This runs a small self-contained demo: builds a 4-block chain, validates
it, then tampers with a block and shows that validation catches it. The
real, persisted chain (used by the API) is validated the same way, but
inside Postgres — trigger it via `GET /api/admin/blockchain/validate`.

## Known simplifications (documented, not hidden)

- **Encryption**: direct chats use public-key end-to-end encryption
  (`static/js/crypto.js`): each account has an ECDH P-256 key pair created in the
  browser, the private key is non-extractable and kept in that browser's IndexedDB,
  and only the public key is sent to the server (and validated there, including an
  on-curve check, by `utils/keys.py`; a private key is refused). Two people derive an
  AES-256-GCM key from their own private key and the other's public key (ECDH +
  HKDF). Messages are marked `e2.` and are bound to their conversation and sender.
  Older passphrase-encrypted chats, group chats and contacts without keys keep using a
  shared passphrase (PBKDF2 → AES-GCM); each message is decrypted by the scheme that
  sealed it. What this does **not** give you: the server distributes public keys, so
  a malicious server could swap one — compare the *safety number* with your contact
  (the app also warns when a contact's key changes); there is no forward secrecy
  (long-lived keys); and keys belong to one browser, so a new device must set up new
  keys and cannot read messages sealed to the old one.
- **Encrypted media**: what's encrypted is the *reference* to an attachment, not the
  file's bytes. Uploading still sends the file to `/api/media/upload` and then
  Cloudinary as-is (so Cloudinary can generate previews/thumbnails and the upload
  step can still be size/type-checked); once Cloudinary returns its URL, the client
  seals `{url, public_id, resource_type, format}` with the same key that seals the
  conversation's text (`encrypted_media` in the `messages` table) and sends only
  that. A message row on its own no longer says where — or in which conversation —
  an attachment lives, and the same tamper-evidence applies (moving a sealed
  reference to another conversation or message fails to decrypt, exactly like text).
  Decrypting an attachment also checks the URL is `https://res.cloudinary.com/<this
  project's cloud name>/…` before it is ever fetched, since the server can no longer
  check that itself once the reference is ciphertext (`isTrustedMediaUrl` in
  `app.js`; the cloud name is served from `/api/config`). What this does **not**
  give you: anyone who obtains a Cloudinary URL — from the decrypted reference, or by
  guessing/scanning `public_id`s — can view that file without being a participant,
  since Cloudinary does not check who is asking. Full end-to-end encryption of the
  bytes themselves (encrypt client-side before upload, decrypt into a blob URL after
  download) is the more thorough alternative; it was left for future work here.
  Messages sent before this existed keep their old, unencrypted `media_url`/
  `media_public_id`, which the client still renders (through the same trust check).
- **Blockchain decentralisation**: the chain is a single, private,
  application-layer hash chain hosted by your Supabase project, not a
  multi-node consensus network — see Chapter 2 of the project report for
  why that trade-off is appropriate here.
- **Database privileges**: Flask connects to Postgres directly with full
  privileges (no service key was provisioned in this environment). RLS
  policies still apply to anything the browser touches directly
  (Realtime), but Flask itself is the trust boundary — keep your
  `POSTGRES_*` credentials as secret as you would a service-role key.
- **What the browser can do to the database**: the browser holds the public
  Supabase key, so whatever the `anon` and `authenticated` roles may do, any
  visitor or registered user can do directly, skipping Flask. `schema.sql`
  therefore gives them the minimum: no INSERT/UPDATE/DELETE on any table (so a
  user cannot edit their own `role`, or write messages that skip the hash
  chain), read-only access to their **own** conversations, messages, blocks and
  calls (blocks name the sender and conversation, so they are not public), and
  only `id`, `username` and `public_key` from profiles (never phone numbers).
  Anonymous visitors get nothing. Membership is checked by
  `private.is_conversation_member()`, a `SECURITY DEFINER` helper in a schema
  PostgREST does not expose; a policy that queried its own table instead
  recursed forever, which also stopped Realtime from delivering messages.
  `tests/test_browser_access.py` checks all of this by acting as those roles.
  After adding a table, re-run `schema.sql` so it is locked down the same way
  (Supabase grants new tables to the browser roles by default).
- **Calling is a sketch**: `startCall()` / `listenForIncomingCalls()` in
  `static/js/app.js` are not wired to any button and are missing pieces (no
  `ontrack` handler to play remote media, no "ringing" handshake). The
  `/api/calls/*` endpoints only log call sessions.
- **Call quality**: only a public STUN server is configured; a production
  deployment behind restrictive NATs/firewalls will need a TURN server
  (e.g. via Twilio or a self-hosted coturn instance) as a fallback.
