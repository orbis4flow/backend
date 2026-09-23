-- =============================================================================
-- Everyday lookups. Run one block at a time; replace the placeholder values.
-- =============================================================================

-- a user, their balances and who referred them
select p.id, p.email, p.full_name, p.phone, p.country_name, p.referral_code,
       ref.email as referred_by, p.created_at,
       max(a.balance) filter (where a.kind = 'real') as real_balance,
       max(a.balance) filter (where a.kind = 'demo') as demo_balance
from app.profiles p
left join app.profiles ref on ref.id = p.referred_by
left join app.accounts a on a.user_id = p.id
where lower(p.email) = lower('someone@example.com')
group by p.id, ref.email;


-- a user's money movements, newest first
select t.created_at, t.type, t.method, t.status, t.amount_usd, t.fee_usd, t.net_usd,
       t.local_amount, t.local_currency, t.reference, t.provider_ref, t.provider_receipt, t.failure_reason
from app.transactions t
join app.profiles p on p.id = t.user_id
where lower(p.email) = lower('someone@example.com')
order by t.created_at desc
limit 50;


-- deposits stuck at pending for more than 15 minutes (a lost callback, usually)
select t.reference, t.provider_ref, t.method, t.amount_usd, t.local_amount, t.created_at, p.email
from app.transactions t
join app.profiles p on p.id = t.user_id
where t.type = 'deposit' and t.status = 'pending' and t.created_at < now() - interval '15 minutes'
order by t.created_at;


-- who a referral code belongs to, and everyone who joined with it
select owner.email as owner, joined.email, joined.created_at
from app.profiles owner
left join app.profiles joined on joined.referred_by = owner.id
where owner.referral_code = upper('ORBIS-XXXX')
order by joined.created_at desc;


-- the latest provider callbacks, to see exactly what PayHero or Paystack sent
select id, provider, reference, received_at, processed, error, payload
from app.webhook_events
order by id desc
limit 20;


-- totals for today (Nairobi time)
select type, method, status, count(*) as n, sum(amount_usd) as usd
from app.transactions
where created_at >= date_trunc('day', now() at time zone 'Africa/Nairobi') at time zone 'Africa/Nairobi'
group by type, method, status
order by type, method, status;
