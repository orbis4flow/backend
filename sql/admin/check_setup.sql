-- =============================================================================
-- Run after 001 and 002 to confirm the setup. Every row should say ok.
-- =============================================================================

select 'schema app' as item,
       case when exists (select 1 from information_schema.schemata where schema_name = 'app')
            then 'ok' else 'MISSING' end as state
union all
select 'table ' || t,
       case when to_regclass('app.' || t) is not null then 'ok' else 'MISSING' end
from unnest(array['profiles', 'accounts', 'payment_methods', 'transactions', 'webhook_events']) as t
union all
select 'rls on ' || c.relname,
       case when c.relrowsecurity then 'ok' else 'OFF, run 002_security.sql' end
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'app' and c.relkind = 'r'
union all
select 'anon cannot use schema app',
       case when has_schema_privilege('anon', 'app', 'usage') then 'EXPOSED, run 002_security.sql' else 'ok' end;
