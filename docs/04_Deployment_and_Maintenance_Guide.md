# Joschat Deployment, Operations and Maintenance Guide
**Document ID:** JC-OPS-004 · **Revision:** 2.0 · **Baseline:** October 2026

## 1. Operational ownership and boundaries
Application: `Simmyt3r/JosChat`; production domain: https://joschat.vercel.app; Vercel project: `joschat`; verified Supabase project: `fruldgczvyvumgmwmbqj`. Cloudinary is used for media. The release candidate freeze is recorded in `RELEASE_FREEZE_2026-10-16.md`.

## 2. Deployment topology
```mermaid
flowchart TD
 G[GitHub main / reviewed commit] --> CI[CI tests and checks]
 CI --> V[Vercel build / Flask + PWA]
 V --> DOMAIN[joschat.vercel.app]
 DOMAIN --> API[Flask route/API]
 API --> DB[(Supabase Postgres)]
 API --> AUTH[Supabase Auth]
 API --> C[Cloudinary]
 DOMAIN --> RT[Supabase Realtime]
```

## 3. Environment inventory (names only)
Production secrets and configuration must be stored in Vercel/Supabase, never in documentation or source control. Inspect `config.py`, `.env.example` and Vercel settings for authoritative keys. Typical categories include:
- Supabase URL and browser publishable/anon key;
- backend PostgreSQL connection URL;
- Cloudinary storage credentials;
- Flask application security configuration;
- any configured push, cron, signing or notification keys.

**Never** copy production credential values into test reports, GitHub issues or user-facing screenshots.

## 4. Pre-deployment checklist
- [ ] Confirm specific release SHA and freeze scope.
- [ ] CI Python, JavaScript and integration tests pass.
- [ ] Run negative authorization and live two-user call tests.
- [ ] Backup Supabase tables / confirm restore method and recovery owner.
- [ ] Compare schema migration history with repository and resolve drift.
- [ ] Confirm production Vercel environment points to the intended Supabase project.
- [ ] Verify no privileged key is exposed to browser variables.
- [ ] Confirm Supabase Auth security warnings are reviewed.
- [ ] Prepare rollback candidate and staff communication.

## 5. Deployment sequence
```mermaid
sequenceDiagram
 autonumber
 actor Operator
 participant GH as GitHub CI
 participant SB as Supabase
 participant V as Vercel
 participant QA as QA
 Operator->>GH: Select frozen commit
 GH-->>Operator: Checks / test outcome
 Operator->>SB: Snapshot and inspect migration history
 Operator->>SB: Apply reviewed missing migration
 SB-->>Operator: Schema verification result
 Operator->>V: Deploy selected commit
 V-->>Operator: Build status and deployment SHA
 Operator->>QA: Request smoke and two-browser validation
 QA-->>Operator: Evidence and go/no-go decision
```

### Applying migrations safely
1. Inspect `supabase/migrations/` and database migration history first.
2. Review every DDL statement for irreversibility or changes to existing rows.
3. Apply through the approved Supabase migration workflow to the **verified** project.
4. Query indexes, policies, row counts and functions after applying.
5. Do not copy a full historical `schema.sql` blindly into a populated project.
6. Preserve message, call, and block evidence. Existing ledger rows must not be deleted to repair a failure.

The call-history release hardening migration was previously recorded and verified; reapplying it as a new migration is not needed.

## 6. Smoke test checklist
| Endpoint/workflow | Expected result |
|---|---|
| `/` and `/app` | Landing and app render |
| `/admin` | Page renders; privileges validated via API |
| `GET /api/auth/me` with valid token | Correct account/profile and role |
| `GET /api/admin/stats` with ordinary token | 403 |
| Direct/group messages | User can send/receive, outsiders denied |
| WebRTC and call history | Two-party call + stored outcome |
| Offline outbox | Retry without duplicate delivery |
| Integrity audit | Expected validation result |

## 7. Monitoring and diagnosis
Check Vercel deployment inspector, build status, runtime error logs within plan retention, Supabase auth activity, database health, and Cloudinary errors. Correlate by request time, deployment ID and path. A READY build alone never certifies the application.

**Admin-access diagnostic:** confirm the signed-in `/api/auth/me` profile role; verify same-tab session; verify `/api/admin/stats` HTTP response; check 401 vs 403 vs 5xx; avoid bypassing `require_admin`.

## 8. Incident response
```mermaid
flowchart TD
 ALERT[Incident detected] --> CLASS[Classify security / availability / data integrity]
 CLASS --> CONTAIN[Contain exposure or limit writes]
 CONTAIN --> EVIDENCE[Capture logs / IDs safely]
 EVIDENCE --> DECIDE{Deployment-only defect?}
 DECIDE -- Yes --> ROLLBACK[Promote previously verified Vercel deployment]
 DECIDE -- No --> DBREC[Review DB changes; use verified backup / surgical fix]
 ROLLBACK --> VERIFY[Regression smoke]
 DBREC --> VERIFY
 VERIFY --> POST[Document incident and preventive test]
```

## 9. Maintenance calendar
| Frequency | Action |
|---|---|
| Each release | CI, database backup, deployment SHA and smoke evidence |
| Weekly | Security advisor, auth warnings, error log review |
| Monthly | Restore drill in isolation; dependency and permissions audit |
| Quarterly | Key rotation plan, disaster recovery and admin access review |
| After incidents | Root-cause analysis, added regression and corrective action |

## 10. Backup and restoration principles
Back up before schema changes and verify the restoration destination; test restores on an isolated environment. Define RPO/RTO with the owner rather than inventing figures. Rotate compromised keys and invalidate sessions as necessary. Restoration of immutable audit or block history requires specialist review of chain integrity.

## 11. Security release findings
Supabase previously warned leaked-password protection was disabled; enabling it is an Auth project setting, not a schema migration. Several RLS-enabled private tables intentionally lacked browser policies. Evaluate GRANTs/RLS together before adding any broad access. Never weaken policies just to silence an advisor.

## 12. Change management and signoff
All post-freeze modifications must be release-blocker fixes with tests, review, documented migration and rollback. Production certification requires signoff against the QA matrix. Do not describe a commit or deployment as “validated” solely because it built.
