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

## Project layout

```
joschat_app/
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
├── utils/auth_helpers.py       @require_auth / @require_admin decorators
├── supabase/schema.sql         Tables, RLS policies, add_block()/validate_chain()
├── templates/index.html         Minimal demo UI
└── static/
    ├── js/app.js                 Demo client (auth, messaging, calling)
    ├── manifest.json              PWA manifest
    └── sw.js                       Service worker (offline caching)
```

## Setup

### 1. Create a Supabase project and run the schema

1. Go to [supabase.com](https://supabase.com) → New Project.
2. Open **SQL Editor** → New query, paste the entire contents of
   `supabase/schema.sql`, and run it. This creates all tables, the
   `add_block`/`validate_chain`/`validate_conversation` functions, RLS
   policies, and enables Realtime on `messages`.

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

### 5. Promote your first admin user

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
| POST | `/api/auth/login` | – | `{email, password}` → `{access_token, profile}` |
| POST | `/api/auth/logout` | Bearer | Invalidate the current session |
| GET | `/api/auth/me` | Bearer | Current user's profile |
| POST | `/api/conversations` | Bearer | `{participant_ids: [...], is_group?, title?}` |
| GET | `/api/conversations` | Bearer | List the caller's conversations |
| GET | `/api/conversations/<id>` | Bearer | Conversation + participants |
| POST | `/api/messages/send` | Bearer | `{conversation_id, encrypted_content, media_url?}` |
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

- **Encryption**: `static/js/app.js` uses one shared demo AES-GCM key
  per browser (stored in `localStorage`) rather than a full public-key
  key-exchange protocol. The `profiles.public_key` column is provisioned
  for a real X25519-based E2EE handshake, which is the natural next step
  before handling real sensitive traffic.
- **Blockchain decentralisation**: the chain is a single, private,
  application-layer hash chain hosted by your Supabase project, not a
  multi-node consensus network — see Chapter 2 of the project report for
  why that trade-off is appropriate here.
- **Database privileges**: Flask connects to Postgres directly with full
  privileges (no service key was provisioned in this environment). RLS
  policies still apply to anything the browser touches directly
  (Realtime), but Flask itself is the trust boundary — keep your
  `POSTGRES_*` credentials as secret as you would a service-role key.
- **Call quality**: only a public STUN server is configured; a production
  deployment behind restrictive NATs/firewalls will need a TURN server
  (e.g. via Twilio or a self-hosted coturn instance) as a fallback.
