-- ============================================================================
-- Joschat — Supabase schema
-- Run this in the Supabase SQL editor (Project -> SQL Editor -> New query).
-- Requires the pgcrypto extension for SHA-256 hashing inside Postgres.
--
-- This script is idempotent: it is safe to run again on a database that
-- already has an older version of it (tables are left untouched; functions,
-- policies and grants are re-applied).
-- ============================================================================

create extension if not exists pgcrypto;

-- ----------------------------------------------------------------------------
-- 1. Profiles (extends Supabase's built-in auth.users)
-- ----------------------------------------------------------------------------
create table if not exists profiles (
    id              uuid primary key references auth.users (id) on delete cascade,
    username        text unique not null,
    phone_number    text unique,
    public_key      text,                      -- client-generated E2EE public key
    role            text not null default 'user' check (role in ('user', 'admin')),
    status          text not null default 'active' check (status in ('active', 'suspended')),
    created_at      timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- 2. Conversations
-- ----------------------------------------------------------------------------
create table if not exists conversations (
    id          bigint generated always as identity primary key,
    is_group    boolean not null default false,
    title       text,                          -- optional, for group chats
    created_by  uuid references profiles (id),
    created_at  timestamptz not null default now()
);

create table if not exists conversation_participants (
    conversation_id  bigint references conversations (id) on delete cascade,
    user_id          uuid references profiles (id) on delete cascade,
    joined_at        timestamptz not null default now(),
    primary key (conversation_id, user_id)
);

-- ----------------------------------------------------------------------------
-- 3. Blocks — the append-only hash chain (the "blockchain" integrity layer)
-- ----------------------------------------------------------------------------
create table if not exists blocks (
    index            bigint primary key,
    created_at       timestamptz not null default now(),
    message_hash     text not null,           -- SHA-256 of the ciphertext, never the plaintext
    sender_id        uuid,                    -- who sent the message  } both NULL only on blocks written
    conversation_id  bigint,                  -- which conversation    } before these columns existed
    previous_hash    text not null,
    block_hash       text not null unique
);

-- Upgrading a database created before sender_id / conversation_id were recorded.
-- Existing blocks keep NULLs and keep validating under the original hash formula
-- (see compute_block_hash below), so history is never rewritten. There are
-- deliberately no foreign keys: the ledger must survive a deleted conversation
-- or account, and a cascading delete would tear holes in the chain.
alter table blocks add column if not exists sender_id uuid;
alter table blocks add column if not exists conversation_id bigint;

do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'blocks_ids_together') then
        alter table blocks add constraint blocks_ids_together
            check ((sender_id is null) = (conversation_id is null));
    end if;
end $$;

-- ----------------------------------------------------------------------------
-- 4. Messages
-- ----------------------------------------------------------------------------
create table if not exists messages (
    id                  bigint generated always as identity primary key,
    conversation_id     bigint not null references conversations (id) on delete cascade,
    sender_id           uuid not null references profiles (id),
    encrypted_content    text not null,          -- ciphertext produced client-side (E2EE)
    media_url           text,                    -- Cloudinary secure_url, if any
    media_public_id     text,                    -- Cloudinary public_id, for later management
    block_index         bigint not null references blocks (index),
    block_hash          text not null,
    created_at          timestamptz not null default now()
);

create index if not exists idx_messages_conversation on messages (conversation_id, created_at);

-- ----------------------------------------------------------------------------
-- 5. Calls — lightweight call-session log (signalling itself happens over
--    Supabase Realtime Broadcast on the client; this table is just a record).
-- ----------------------------------------------------------------------------
create table if not exists calls (
    id               bigint generated always as identity primary key,
    conversation_id  bigint references conversations (id),
    caller_id        uuid references profiles (id),
    callee_id        uuid references profiles (id),
    call_type        text not null check (call_type in ('voice', 'video')),
    status           text not null default 'initiated'
                     check (status in ('initiated', 'connected', 'missed', 'ended', 'failed')),
    started_at       timestamptz not null default now(),
    ended_at         timestamptz
);

-- ============================================================================
-- Atomic blockchain functions
-- All hashing happens inside Postgres so there is exactly one authoritative
-- implementation of "how a block hash is computed" — Flask never computes a
-- hash itself, it only asks Postgres to do it, inside a single locked
-- transaction, which removes the race condition you'd get from two
-- concurrent messages both reading "the last block" in application code.
-- ============================================================================


-- NOTE: every function below pins `timezone` to UTC. Block hashes include
-- created_at::text, and timestamptz -> text depends on the *session* time zone.
-- Without the pin, a block hashed in one session (say UTC) and re-checked from
-- another (say Africa/Lagos) hashes differently and validate_chain() reports
-- tampering that never happened.

-- The one place the block hash is defined. add_block, validate_chain and
-- validate_conversation all call it, so they can never disagree.
--   New blocks:    sha256(index|created_at|message_hash|sender_id|conversation_id|previous_hash)
--   Older blocks:  sha256(index || created_at || message_hash || previous_hash)
--                  (no sender/conversation recorded; kept so existing history stays valid)
create or replace function compute_block_hash(
    p_index            bigint,
    p_created_at       timestamptz,
    p_message_hash     text,
    p_sender_id        uuid,
    p_conversation_id  bigint,
    p_previous_hash    text
)
returns text
language sql
stable
set timezone to 'UTC'
as $$
    select encode(
        digest(
            case
                when p_sender_id is null and p_conversation_id is null then
                    p_index::text || p_created_at::text || p_message_hash || p_previous_hash
                else
                    p_index::text || '|' || p_created_at::text || '|' || p_message_hash || '|' ||
                    p_sender_id::text || '|' || p_conversation_id::text || '|' || p_previous_hash
            end,
            'sha256'
        ),
        'hex'
    );
$$;

create or replace function add_block(
    p_message_hash     text,
    p_sender_id        uuid,
    p_conversation_id  bigint
)
returns table (
    idx            bigint,
    block_hash     text,
    previous_hash  text,
    created_at     timestamptz
)
language plpgsql
set timezone to 'UTC'
as $$
declare
    v_last_index  bigint;
    v_last_hash   text;
    v_new_index   bigint;
    v_ts          timestamptz := now();
    v_hash        text;
begin
    if p_sender_id is null or p_conversation_id is null then
        raise exception 'add_block requires both sender_id and conversation_id';
    end if;

    -- Serialize block creation: only one transaction can hold this lock
    -- at a time, so "read last block, then append" is effectively atomic.
    lock table blocks in exclusive mode;

    select b.index, b.block_hash into v_last_index, v_last_hash
    from blocks b
    order by b.index desc
    limit 1;

    if v_last_index is null then
        v_new_index := 0;
        v_last_hash := repeat('0', 64);   -- genesis previous-hash
    else
        v_new_index := v_last_index + 1;
    end if;

    v_hash := compute_block_hash(
        v_new_index, v_ts, p_message_hash, p_sender_id, p_conversation_id, v_last_hash
    );

    insert into blocks (index, created_at, message_hash, sender_id, conversation_id, previous_hash, block_hash)
    values (v_new_index, v_ts, p_message_hash, p_sender_id, p_conversation_id, v_last_hash, v_hash);

    return query select v_new_index, v_hash, v_last_hash, v_ts;
end;
$$;

create or replace function validate_chain()
returns table (
    is_valid       boolean,
    invalid_index  bigint,
    blocks_checked bigint
)
language plpgsql
set timezone to 'UTC'
as $$
declare
    rec            record;
    v_prev_hash    text := repeat('0', 64);
    v_computed     text;
    v_count        bigint := 0;
begin
    for rec in select * from blocks order by index asc loop
        v_computed := compute_block_hash(
            rec.index, rec.created_at, rec.message_hash,
            rec.sender_id, rec.conversation_id, rec.previous_hash
        );
        v_count := v_count + 1;

        if v_computed <> rec.block_hash or rec.previous_hash <> v_prev_hash then
            return query select false, rec.index, v_count;
            return;
        end if;

        v_prev_hash := rec.block_hash;
    end loop;

    return query select true, null::bigint, v_count;
end;
$$;

-- Validate only the segment of the chain touched by one conversation.
-- Cross-checks each message's stored block against a full recomputation, and
-- (for blocks that record them) that the block names the same sender and
-- conversation as the message row it is attached to.
create or replace function validate_conversation(p_conversation_id bigint)
returns table (
    message_id  bigint,
    verified    boolean
)
language plpgsql
set timezone to 'UTC'
as $$
declare
    rec         record;
    v_computed  text;
    v_bound     boolean;
begin
    for rec in
        select m.id            as msg_id,
               m.sender_id     as msg_sender_id,
               m.conversation_id as msg_conversation_id,
               m.block_index,
               m.block_hash    as msg_block_hash,
               b.message_hash,
               b.sender_id     as blk_sender_id,
               b.conversation_id as blk_conversation_id,
               b.previous_hash,
               b.created_at
        from messages m
        join blocks b on b.index = m.block_index
        where m.conversation_id = p_conversation_id
        order by m.created_at asc
    loop
        v_computed := compute_block_hash(
            rec.block_index, rec.created_at, rec.message_hash,
            rec.blk_sender_id, rec.blk_conversation_id, rec.previous_hash
        );
        -- Blocks written before sender/conversation were recorded have NULLs and
        -- cannot be bound to their message; every newer block must match exactly.
        v_bound := rec.blk_sender_id is null
                   or (rec.blk_sender_id = rec.msg_sender_id
                       and rec.blk_conversation_id = rec.msg_conversation_id);
        return query select rec.msg_id, (v_computed = rec.msg_block_hash and v_bound);
    end loop;
end;
$$;

-- ============================================================================
-- Row Level Security
-- ============================================================================
alter table profiles enable row level security;
alter table conversations enable row level security;
alter table conversation_participants enable row level security;
alter table messages enable row level security;
alter table blocks enable row level security;
alter table calls enable row level security;

-- Profiles: anyone authenticated can read basic profile info; a user can
-- only update their own row; admins can read/update everything via the
-- Flask backend using the service-role key (which bypasses RLS entirely).
drop policy if exists "Profiles are viewable by authenticated users" on profiles;
create policy "Profiles are viewable by authenticated users"
    on profiles for select
    using (auth.role() = 'authenticated');

drop policy if exists "Users can update their own profile" on profiles;
create policy "Users can update their own profile"
    on profiles for update
    using (auth.uid() = id);

-- Conversations / participants: only participants can see a conversation.
drop policy if exists "Participants can view their conversations" on conversations;
create policy "Participants can view their conversations"
    on conversations for select
    using (
        exists (
            select 1 from conversation_participants cp
            where cp.conversation_id = conversations.id
              and cp.user_id = auth.uid()
        )
    );

drop policy if exists "Participants can view participant lists" on conversation_participants;
create policy "Participants can view participant lists"
    on conversation_participants for select
    using (
        exists (
            select 1 from conversation_participants cp2
            where cp2.conversation_id = conversation_participants.conversation_id
              and cp2.user_id = auth.uid()
        )
    );

-- Messages: only participants of the conversation can read/insert messages.
drop policy if exists "Participants can view messages" on messages;
create policy "Participants can view messages"
    on messages for select
    using (
        exists (
            select 1 from conversation_participants cp
            where cp.conversation_id = messages.conversation_id
              and cp.user_id = auth.uid()
        )
    );

drop policy if exists "Participants can send messages" on messages;
create policy "Participants can send messages"
    on messages for insert
    with check (
        auth.uid() = sender_id
        and exists (
            select 1 from conversation_participants cp
            where cp.conversation_id = messages.conversation_id
              and cp.user_id = auth.uid()
        )
    );

-- Blocks: readable by any authenticated user (needed for independent
-- chain-integrity verification); never writable directly by clients —
-- only the add_block() function (called by the Flask backend with the
-- service-role key) may insert.
drop policy if exists "Authenticated users can read blocks" on blocks;
create policy "Authenticated users can read blocks"
    on blocks for select
    using (auth.role() = 'authenticated');

-- Calls: participants only.
drop policy if exists "Call participants can view their calls" on calls;
create policy "Call participants can view their calls"
    on calls for select
    using (auth.uid() = caller_id or auth.uid() = callee_id);

drop policy if exists "Call participants can insert their calls" on calls;
create policy "Call participants can insert their calls"
    on calls for insert
    with check (auth.uid() = caller_id);

-- ============================================================================
-- Function privileges
-- Supabase exposes every function in the public schema to the browser through
-- PostgREST (/rest/v1/rpc/...), and by default grants EXECUTE on them to the
-- `anon` and `authenticated` roles. Left as is, ANY visitor could call
-- add_block() and append junk to the chain (or hammer validate_chain(), which
-- scans the whole table). Only the backend — which connects as `postgres`, the
-- owner of these functions — should be able to run them.
-- ============================================================================
-- The pre-upgrade one-argument add_block(text) may still exist on a database that
-- has not yet had it dropped (see README, "Upgrading an existing database").
do $$
begin
    if to_regprocedure('add_block(text)') is not null then
        revoke all on function add_block(text) from public, anon, authenticated;
    end if;
end $$;
revoke all on function add_block(text, uuid, bigint) from public, anon, authenticated;
revoke all on function compute_block_hash(bigint, timestamptz, text, uuid, bigint, text) from public, anon, authenticated;
revoke all on function validate_chain() from public, anon, authenticated;
revoke all on function validate_conversation(bigint) from public, anon, authenticated;

-- ============================================================================
-- Realtime
-- Enable Realtime on `messages` so clients can subscribe to new rows
-- (INSERT events) for a given conversation_id directly via supabase-js,
-- without needing a persistent Flask/Socket.IO server.
-- ============================================================================
do $$
begin
    if not exists (
        select 1 from pg_publication_tables
        where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'messages'
    ) then
        alter publication supabase_realtime add table messages;
    end if;
end
$$;
