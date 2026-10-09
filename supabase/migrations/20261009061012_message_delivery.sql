-- Joschat delivery and integrity upgrade. Additive: existing rows/blocks are preserved.
alter table public.messages add column if not exists client_message_id uuid;
create unique index if not exists idx_messages_sender_client_id
    on public.messages (sender_id, client_message_id) where client_message_id is not null;

-- Verify the actual ciphertext, sender/conversation binding, block hash and
-- immediate predecessor link. These functions are backend-only, SECURITY INVOKER.
-- A Verified badge is an integrity check, not identity or factual verification.
create or replace function validate_message(p_message_id bigint)
returns boolean
language sql stable
set timezone to 'UTC'
set search_path = pg_catalog, public, extensions
as $$
    select coalesce((
        select encode(digest(m.encrypted_content, 'sha256'), 'hex') = b.message_hash
           and m.block_hash = b.block_hash
           and b.block_hash = compute_block_hash(
               b.index, b.created_at, b.message_hash,
               b.sender_id, b.conversation_id, b.previous_hash)
           and (b.sender_id is null or
                (b.sender_id = m.sender_id and b.conversation_id = m.conversation_id))
           and case when b.index = 0 then b.previous_hash = repeat('0', 64)
               else prev.block_hash = b.previous_hash
                    and prev.block_hash = compute_block_hash(
                        prev.index, prev.created_at, prev.message_hash,
                        prev.sender_id, prev.conversation_id, prev.previous_hash)
               end
        from messages m
        join blocks b on b.index = m.block_index
        left join blocks prev on prev.index = b.index - 1
        where m.id = p_message_id
    ), false);
$$;

create or replace function validate_conversation(p_conversation_id bigint)
returns table (message_id bigint, verified boolean)
language sql stable
set timezone to 'UTC'
set search_path = pg_catalog, public, extensions
as $$
    select m.id, validate_message(m.id)
    from messages m where m.conversation_id = p_conversation_id
    order by m.created_at, m.id;
$$;

revoke all on function validate_message(bigint) from public, anon, authenticated;
revoke all on function validate_conversation(bigint) from public, anon, authenticated;

-- Pin the namespace of the existing backend ledger functions as well.
alter function public.compute_block_hash(bigint, timestamptz, text, uuid, bigint, text)
    set search_path = pg_catalog, public, extensions;
alter function public.add_block(text, uuid, bigint)
    set search_path = pg_catalog, public, extensions;
alter function public.validate_chain() set search_path = pg_catalog, public, extensions;
