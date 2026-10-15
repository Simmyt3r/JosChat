-- Additive upgrade; historical ciphertext and block hashes are preserved.
alter table conversations add column if not exists key_epoch integer not null default 1;
alter table messages add column if not exists integrity_version integer not null default 1;
create unique index if not exists messages_one_per_block on messages(block_index);
alter table calls add column if not exists accepted_at timestamptz;
alter table calls add column if not exists connected_at timestamptz;
alter table calls drop constraint if exists calls_status_check;
alter table calls add constraint calls_status_check check(status in ('initiated','accepted','connected','missed','ended','failed'));

create table if not exists private.message_receipts (
    block_index bigint primary key,
    message_id bigint not null unique,
    signature text not null,
    payload_hash text not null,
    created_at timestamptz not null default now()
);
create table if not exists private.integrity_audits (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    source text not null,
    report jsonb not null
);
create table if not exists private.push_subscriptions (
    endpoint text primary key,
    user_id uuid not null references profiles(id) on delete cascade,
    subscription jsonb not null,
    created_at timestamptz not null default now()
);
create index if not exists push_subscriptions_user on private.push_subscriptions(user_id);
create table if not exists private.push_deliveries (
    id bigint generated always as identity primary key,
    user_id uuid not null references profiles(id) on delete cascade,
    payload jsonb not null,
    event_key text not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    attempts integer not null default 0,
    unique(user_id, event_key)
);
alter table private.message_receipts enable row level security;
alter table private.integrity_audits enable row level security;
alter table private.push_subscriptions enable row level security;
alter table private.push_deliveries enable row level security;
revoke all on all tables in schema private from public, anon, authenticated;
revoke all on all sequences in schema private from public, anon, authenticated;

create or replace function compute_message_hash(content text, media text, url text, public_id text)
returns text language sql immutable
set search_path = pg_catalog, public, extensions
as $$
    select encode(digest('joschat/message/v2|' ||
        case when content is null then '-' else octet_length(content)::text || ':' || content end || '|' ||
        case when media is null then '-' else octet_length(media)::text || ':' || media end || '|' ||
        case when url is null then '-' else octet_length(url)::text || ':' || url end || '|' ||
        case when public_id is null then '-' else octet_length(public_id)::text || ':' || public_id end,
        'sha256'), 'hex');
$$;

create or replace function validate_message(p_message_id bigint)
returns boolean language sql stable
set timezone to 'UTC'
set search_path = pg_catalog, public, extensions
as $$
    select coalesce((
        select (case when m.integrity_version = 2
                     then compute_message_hash(m.encrypted_content,m.encrypted_media,m.media_url,m.media_public_id)
                     else encode(digest(m.encrypted_content,'sha256'),'hex') end) = b.message_hash
           and m.block_hash = b.block_hash
           and b.block_hash = compute_block_hash(b.index,b.created_at,b.message_hash,b.sender_id,b.conversation_id,b.previous_hash)
           and (b.sender_id is null or (b.sender_id = m.sender_id and b.conversation_id = m.conversation_id))
           and case when b.index = 0 then b.previous_hash = repeat('0',64)
                    else prev.block_hash = b.previous_hash
                         and prev.block_hash = compute_block_hash(prev.index,prev.created_at,prev.message_hash,prev.sender_id,prev.conversation_id,prev.previous_hash) end
        from messages m join blocks b on b.index = m.block_index
        left join blocks prev on prev.index = b.index-1
        where m.id = p_message_id
    ),false);
$$;

create or replace function validate_chain()
returns table(is_valid boolean, invalid_index bigint, blocks_checked bigint)
language plpgsql
set timezone to 'UTC'
set search_path = pg_catalog, public, extensions
as $$
declare
    rec record;
    v_prev text := repeat('0',64);
    v_count bigint := 0;
    v_message_id bigint;
begin
    for rec in select * from blocks order by index loop
        if rec.index <> v_count or rec.previous_hash <> v_prev or
           rec.block_hash <> compute_block_hash(rec.index,rec.created_at,rec.message_hash,rec.sender_id,rec.conversation_id,rec.previous_hash) then
            return query select false,rec.index,v_count+1; return;
        end if;
        -- Pre-binding legacy blocks have no attributable message. Preserve them.
        if rec.conversation_id is not null then
            select m.id into v_message_id from messages m where m.block_index = rec.index;
            if v_message_id is null or not validate_message(v_message_id) then
                return query select false,rec.index,v_count+1; return;
            end if;
        end if;
        v_prev := rec.block_hash; v_count := v_count+1;
    end loop;
    return query select true,null::bigint,v_count;
end;
$$;

-- Even a privileged application query cannot accidentally erase/rewrite history.
-- Database administrators can disable triggers; external-key receipts still detect
-- rewritten sealed blocks. They are not a decentralized consensus mechanism.
create or replace function private.reject_history_change()
returns trigger language plpgsql
set search_path = pg_catalog
as $$ begin raise exception 'Joschat history is append-only' using errcode = '55000'; end; $$;
drop trigger if exists immutable_history on blocks;
create trigger immutable_history before update or delete on blocks for each row execute function private.reject_history_change();
drop trigger if exists immutable_history on messages;
create trigger immutable_history before update or delete on messages for each row execute function private.reject_history_change();
drop trigger if exists immutable_history on private.message_receipts;
create trigger immutable_history before update or delete on private.message_receipts for each row execute function private.reject_history_change();
revoke all on function private.reject_history_change() from public,anon,authenticated;
revoke all on function compute_message_hash(text,text,text,text) from public,anon,authenticated;
revoke all on function validate_chain() from public,anon,authenticated;
revoke all on function validate_message(bigint) from public,anon,authenticated;

-- The topic must correspond to a live call involving the authenticated account.
create or replace function private.is_call_member(topic text)
returns boolean language sql stable security definer
set search_path = pg_catalog, public
as $$
    select exists(select 1 from public.calls c
        join public.profiles p on p.id = auth.uid() and p.status = 'active'
        where topic = 'call-' || c.id::text
          and (c.caller_id = auth.uid() or c.callee_id = auth.uid())
          and c.status in ('initiated','accepted','connected')
          and exists(select 1 from public.conversation_participants cp where cp.conversation_id = c.conversation_id and cp.user_id = auth.uid()));
$$;
revoke all on function private.is_call_member(text) from public,anon;
grant execute on function private.is_call_member(text) to authenticated;
drop policy if exists "Joschat call receive" on realtime.messages;
create policy "Joschat call receive" on realtime.messages for select to authenticated
    using (extension in ('broadcast','presence') and private.is_call_member(realtime.topic()));
drop policy if exists "Joschat call send" on realtime.messages;
create policy "Joschat call send" on realtime.messages for insert to authenticated
    with check (extension in ('broadcast','presence') and private.is_call_member(realtime.topic()));

-- Suspension also revokes row delivery for an otherwise unexpired JWT.
create or replace function private.is_conversation_member(p_conversation_id bigint)
returns boolean language sql stable security definer
set search_path = pg_catalog, public
as $$ select exists(select 1 from public.conversation_participants cp
    join public.profiles p on p.id = cp.user_id and p.status = 'active'
    where cp.conversation_id = p_conversation_id and cp.user_id = auth.uid()); $$;
revoke all on function private.is_conversation_member(bigint) from public,anon;
grant execute on function private.is_conversation_member(bigint) to authenticated;
