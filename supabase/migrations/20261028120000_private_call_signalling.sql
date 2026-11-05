-- Apply before deploying private-channel clients. In Supabase Realtime settings,
-- also disable "Allow public access"; public channels bypass authorization.
-- Authorization is evaluated on channel join; existing sessions are not revoked
-- immediately when a profile is suspended or a call ends.
create schema if not exists private;
revoke all on schema private from public, anon;
grant usage on schema private to authenticated;

create or replace function private.can_signal_call(p_topic text)
returns boolean
language sql stable security definer
set search_path = pg_catalog, public
as $$
    select exists (
        select 1 from public.calls c
        join public.profiles p on p.id = auth.uid() and p.status = 'active'
        where p_topic = 'call-' || c.id::text
          and c.status in ('initiated', 'connected')
          and p.id in (c.caller_id, c.callee_id)
          and exists (select 1 from public.conversation_participants cp
                      where cp.conversation_id = c.conversation_id and cp.user_id = p.id)
    );
$$;
revoke all on function private.can_signal_call(text) from public, anon;
grant execute on function private.can_signal_call(text) to authenticated;

-- Vanilla Postgres test setups do not necessarily provide the Realtime table.
-- Supabase already owns and enables its RLS; do not ALTER that managed table.
do $migration$
begin
    if to_regclass('realtime.messages') is not null then
        execute 'drop policy if exists "Call members receive signals" on realtime.messages';
        execute 'create policy "Call members receive signals" on realtime.messages for select to authenticated
                 using (extension in (''broadcast'', ''presence'') and private.can_signal_call((select realtime.topic())))';
        execute 'drop policy if exists "Call members send signals" on realtime.messages';
        execute 'create policy "Call members send signals" on realtime.messages for insert to authenticated
                 with check (extension in (''broadcast'', ''presence'') and private.can_signal_call((select realtime.topic())))';
        -- Restrictive guards prevent an older permissive policy from granting
        -- outsiders access to a private call topic. Leave unrelated topics alone.
        execute 'drop policy if exists "Call topics require membership" on realtime.messages';
        execute 'create policy "Call topics require membership" on realtime.messages as restrictive for all to authenticated
                 using ((select realtime.topic()) not like ''call-%'' or private.can_signal_call((select realtime.topic())))
                 with check ((select realtime.topic()) not like ''call-%'' or private.can_signal_call((select realtime.topic())))';
        execute 'drop policy if exists "Anonymous users cannot signal calls" on realtime.messages';
        execute 'create policy "Anonymous users cannot signal calls" on realtime.messages as restrictive for all to anon
                 using ((select realtime.topic()) not like ''call-%'')
                 with check ((select realtime.topic()) not like ''call-%'')';
    end if;
end;
$migration$;
