-- ============================================================================
-- Joschat — Supabase schema
-- Run this in the Supabase SQL editor (Project -> SQL Editor -> New query).
-- Requires the pgcrypto extension for SHA-256 hashing inside Postgres.
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
    index          bigint primary key,
    created_at     timestamptz not null default now(),
    message_hash   text not null,
    previous_hash  text not null,
    block_hash     text not null unique
);

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

create or replace function add_block(p_message_hash text)
returns table (
    idx            bigint,
    block_hash     text,
    previous_hash  text,
    created_at     timestamptz
)
language plpgsql
as $$
declare
    v_last_index  bigint;
    v_last_hash   text;
    v_new_index   bigint;
    v_ts          timestamptz := now();
    v_hash        text;
begin
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

    v_hash := encode(
        digest(v_new_index::text || v_ts::text || p_message_hash || v_last_hash, 'sha256'),
        'hex'
    );

    insert into blocks (index, created_at, message_hash, previous_hash, block_hash)
    values (v_new_index, v_ts, p_message_hash, v_last_hash, v_hash);

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
as $$
declare
    rec            record;
    v_prev_hash    text := repeat('0', 64);
    v_computed     text;
    v_count        bigint := 0;
begin
    for rec in select * from blocks order by index asc loop
        v_computed := encode(
            digest(rec.index::text || rec.created_at::text || rec.message_hash || rec.previous_hash, 'sha256'),
            'hex'
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
-- (Cross-checks each message's stored block against a full recomputation.)
create or replace function validate_conversation(p_conversation_id bigint)
returns table (
    message_id  bigint,
    verified    boolean
)
language plpgsql
as $$
declare
    rec         record;
    v_computed  text;
begin
    for rec in
        select m.id, m.block_index, m.block_hash, b.message_hash, b.previous_hash, b.created_at
        from messages m
        join blocks b on b.index = m.block_index
        where m.conversation_id = p_conversation_id
        order by m.created_at asc
    loop
        v_computed := encode(
            digest(rec.block_index::text || rec.created_at::text || rec.message_hash || rec.previous_hash, 'sha256'),
            'hex'
        );
        return query select rec.id, (v_computed = rec.block_hash);
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
create policy "Profiles are viewable by authenticated users"
    on profiles for select
    using (auth.role() = 'authenticated');

create policy "Users can update their own profile"
    on profiles for update
    using (auth.uid() = id);

-- Conversations / participants: only participants can see a conversation.
create policy "Participants can view their conversations"
    on conversations for select
    using (
        exists (
            select 1 from conversation_participants cp
            where cp.conversation_id = conversations.id
              and cp.user_id = auth.uid()
        )
    );

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
create policy "Participants can view messages"
    on messages for select
    using (
        exists (
            select 1 from conversation_participants cp
            where cp.conversation_id = messages.conversation_id
              and cp.user_id = auth.uid()
        )
    );

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
create policy "Authenticated users can read blocks"
    on blocks for select
    using (auth.role() = 'authenticated');

-- Calls: participants only.
create policy "Call participants can view their calls"
    on calls for select
    using (auth.uid() = caller_id or auth.uid() = callee_id);

create policy "Call participants can insert their calls"
    on calls for insert
    with check (auth.uid() = caller_id);

-- ============================================================================
-- Realtime
-- Enable Realtime on `messages` so clients can subscribe to new rows
-- (INSERT events) for a given conversation_id directly via supabase-js,
-- without needing a persistent Flask/Socket.IO server.
-- ============================================================================
alter publication supabase_realtime add table messages;
