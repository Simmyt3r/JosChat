# Joschat: Test Plan and QA Report
**Status:** Test plan and known gaps, not a claim of a complete test run.

## Test environments
Repository `Simmyt3r/JosChat`; Vercel project `joschat`; Supabase project `fruldgczvyvumgmwmbqj`. Use two non-production test identities plus an admin and standard user, isolated test conversations and media.

## Priority verification matrix
| Area | Test | Gate |
| --- | --- | --- |
| Authentication | Register, login, refresh, logout, revoked/expired token | Pending end-to-end |
| Admin | Admin @Ayeson/@simmyt3r access; ordinary user 403; navigation in same tab | Pending browser confirmation |
| Conversation | Direct/group create, add participants, prohibit outsider access | Pending live test |
| Messaging | Encrypt/decrypt, verify integrity, duplicate replay, offline retry | Pending live test |
| Media | Authorized upload/read, disallowed type, size ceiling | Pending live test |
| Calls | Incoming/outgoing voice/video, accept/decline, missed, disconnect | Pending two-browser test |
| Call history | Pagination, membership, timestamps and status | Endpoint implemented; regression execution pending |
| Data security | RLS outsider access, no privilege escalation, secret hygiene | Review required |
| Deployment | CI tests, final production commit, smoke test | Pending |

## Evidence required
Record date, commit SHA, environment, result, expected/actual response, and screenshots/logs with personal data redacted. No unrun case is 'passed'.

## Known risks
Supabase Auth leaked-password protection warning; confirm final Vercel SHA and final admin login. Database migration was previously checked preserving call, message and block counts. A READY deployment is not proof of successful workflows.

## Release rule
Freeze features and approve production only after the complete security, functional and two-browser regression matrix passes.
