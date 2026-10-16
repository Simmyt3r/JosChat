# Joschat release candidate freeze — 2026-10-16

Status: **RC FROZEN FOR FEATURE DEVELOPMENT, NOT YET CERTIFIED FOR PRODUCTION**.

## Scope
- Flask API + PWA; Supabase project `fruldgczvyvumgmwmbqj`; Vercel project `joschat`.
- Authenticated direct and group chat, encrypted messaging, verified-message indicators, offline outbox, voice/video calling and per-user call history.
- No new feature work until release gates pass. Fixes must be narrowly scoped, tested and reviewed.

## Database
Applied Supabase migration `20261016024540_joschat_release_hardening_call_history` on 2026-10-16.
SQL mirrored in `supabase/migrations/20261016024500_joschat_release_hardening_call_history.sql`.
Verified after migration: 13 call records, 14 messages, 14 blocks, 3 history indexes and one call-history SELECT policy. Existing data retained.

## Release gates still open
1. Run all Python unit tests and JS tests in CI against final HEAD.
2. Verify two-user voice and video calls across browsers (answered, declined, missed, reconnect, camera permission failure) and call history.
3. Verify private/group messaging and offline replay against real Supabase with 2 authenticated users.
4. Verify /api admin guards, media permissions, ledger integrity and PWA install/update behavior.
5. Supabase warning: leaked-password protection disabled. Enable in Auth dashboard if supported by plan.
6. Security advisor lists 5 no-policy tables (including `public.joschat`, intentionally denied to browser roles, and private backend-only tables). Check privileges before changing them.
7. Confirm latest Vercel deployment is READY at the final Git SHA and run smoke tests.

## Change policy
- Do not drop, truncate or backfill historical message/call/ledger data.
- No unreviewed production credentials or auth bypasses.
- Follow-up changes must be security/release-blocker fixes, with regression tests.
