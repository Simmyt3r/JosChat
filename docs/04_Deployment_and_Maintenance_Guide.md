# Joschat: Deployment and Maintenance Guide
**Status:** Controlled release-candidate operations.

## Systems
- GitHub: https://github.com/Simmyt3r/JosChat
- Vercel: project `joschat`, https://joschat.vercel.app
- Supabase: project `fruldgczvyvumgmwmbqj`
- Cloudinary: media provider.

## Deploy procedure
1. Back up relevant Supabase data and verify rollback path before migrating.
2. Review commit/PR, run tests and security checks.
3. Inspect existing applied migrations; apply only missing reviewed migrations in order. Never replay production data-destructive SQL.
4. Confirm Vercel environment keys match the verified Supabase instance and media provider. Do not commit credentials.
5. Deploy from the reviewed GitHub commit.
6. Verify production domain, /app, /admin, /api/auth/me under authorization, chat, offline behaviour, two-user calls, and admin restrictions.
7. Document deployment SHA and test evidence before declaring release.

## Security operations
Keep backend secrets out of browser-exposed variables. Preserve ownership checks and restrictive RLS, deny non-admin admin API access, log unexpected privileged actions. Revisit Supabase warning about leaked-password protection in Auth settings.

## Recovery
For app regressions, promote a previously verified deployment. For database issues, stop writes as appropriate, inspect schema and migration history, restore only from verified backups with a documented recovery procedure. Do not delete ledger records as an ad hoc fix.

## Freeze
Feature freeze documented in `RELEASE_FREEZE_2026-10-16.md`. Only scoped fixes with regression evidence should follow the release candidate.
