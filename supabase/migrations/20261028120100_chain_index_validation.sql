-- Validate contiguous indexes as well as hash linkage. No hashes are rewritten.
create or replace function validate_chain()
returns table (
    is_valid       boolean,
    invalid_index  bigint,
    blocks_checked bigint
)
language plpgsql
set timezone to 'UTC'
set search_path = pg_catalog, public, extensions
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

        if rec.index <> v_count - 1 or v_computed <> rec.block_hash or rec.previous_hash <> v_prev_hash then
            return query select false, rec.index, v_count;
            return;
        end if;

        v_prev_hash := rec.block_hash;
    end loop;

    return query select true, null::bigint, v_count;
end;
$$;

