-- Native Supabase scheduling, alongside the existing daily Vercel integrity audit.
-- Vault values are provisioned separately; no credential is stored in git or in
-- the cron command. Empty queues cause no HTTP request.
create extension if not exists pg_cron;
create extension if not exists pg_net;

select cron.schedule('joschat-push-retry', '* * * * *', $job$
    select net.http_get(
        url := origin.decrypted_secret || '/api/system/notifications',
        headers := jsonb_build_object('Authorization', 'Bearer ' || token.decrypted_secret),
        timeout_milliseconds := 55000
    )
    from vault.decrypted_secrets origin
    join vault.decrypted_secrets token on token.name = 'joschat_cron_secret'
    where origin.name = 'joschat_app_origin'
      and exists(select 1 from private.push_deliveries
                 where expires_at > now() and attempts < 5);
$job$);
