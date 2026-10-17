# Joschat: System Design and Architecture
**Status:** Release candidate, October 2026.

## Components
Browser/PWA (HTML/CSS/JS) -> Flask API on Vercel -> Supabase PostgreSQL; browser uses Supabase Auth and Realtime; Cloudinary provides media object storage. Supabase Realtime Broadcast handles WebRTC SDP/ICE signalling; actual audio/video uses browser WebRTC peer connections.

## Source map
- `app.py`: Flask app, routes for landing, /app, /admin and API.
- `routes/auth.py`: identity and profile endpoints.
- `routes/conversations.py`, `routes/messages.py`: conversations, E2EE messages and verification.
- `routes/calls.py`: call lifecycle and history.
- `routes/admin.py`: privileged APIs.
- `static/js/app.js`, `static/js/admin.js`, `static/sw.js`: client, dashboard, service worker.
- `supabase/schema.sql` and `supabase/migrations/`: schema and changes.

## Data entities
`profiles`, `conversations`, `conversation_participants`, `messages`, `blocks`, `calls`; additional private tables support delivery and security operations.

## Trust boundaries
The browser carries a Supabase access token to Flask; Flask validates the authenticated identity and loads the server-managed profile. Administrator APIs independently enforce role=admin. Supabase RLS restricts browser-accessible rows. The server stores encrypted message bodies. Ledger functions append message hashes atomically and independently verify integrity; a successful hash audit is not proof of identity or content truth.

## Call flow
Caller POSTs call start; recipient receives Realtime signal; recipient accepts or rejects; browsers negotiate WebRTC through Realtime Broadcast; the server persists timestamps/status for call history. Record access is restricted to participants.

## Failure handling
Expired tokens trigger refresh or login; offline messages remain queued for retry; unavailable database/auth/realtime/media services should surface actionable errors without leaking secrets. Prevent repeated terminal call transitions.
