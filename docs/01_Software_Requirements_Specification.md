# Joschat: Software Requirements Specification
**Status:** Release candidate, October 2026. Not a declaration of production acceptance.

## Purpose
Joschat is a progressive web app for authenticated direct and group messaging, encrypted messages and permitted media, integrity verification, voice/video calls, and call-history viewing.

## Users and roles
- Visitor: landing page and entry to authentication.
- Registered active user: own conversations, messages, calls and settings.
- Administrator: privileged dashboard, user moderation, statistics, conversation and blockchain integrity audit.
- Suspended user: denied authenticated application operations.

## Core functional requirements
1. Registration, login, refresh, logout and profile setup with username and phone number.
2. Create/list direct and group conversations and enforce participant authorization on reads and writes.
3. Encrypt message content on the client; transmit ciphertext, maintain delivery history and prevent duplicate offline replays.
4. Offer encrypted media sharing within configured file-size and format limits.
5. Expose automatic message integrity verification and explicit audit operations. A verified badge validates message integrity, not sender identity.
6. Support voice/video call initiation, answer, decline, ending and appropriately scoped historical records.
7. Provide offline-capable PWA shell and reliable message outbox with reconciliation on reconnection.
8. Allow approved admins to review users, stats, conversation audits, and suspend/reinstate users, never self-promote.
9. Maintain secure navigation to /admin for authorized users.

## Non-functional requirements
Role-based API authorization; Supabase RLS; no service role secrets in browser; HTTPS; resilient token refresh; server-side authorization; accessible mobile UI; observable errors; idempotent migrations; backups and rollback procedures.

## Acceptance
Passing CI and multi-user browser scenarios is required before production certification. See 03_Test_Plan_and_QA_Report.md.
