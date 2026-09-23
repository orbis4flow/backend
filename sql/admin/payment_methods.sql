-- =============================================================================
-- Payment methods
-- M-Pesa numbers verify themselves on the first successful deposit from them,
-- and cards on the first successful card payment. Bank accounts and USDT
-- wallets have no automatic check, so they are verified here after you have
-- confirmed the name matches the user's ID.
-- =============================================================================

-- methods waiting to be verified
select pm.id, p.email, p.full_name, pm.kind, pm.label, pm.masked, pm.details, pm.created_at
from app.payment_methods pm
join app.profiles p on p.id = pm.user_id
where pm.status = 'pending' and pm.removed_at is null
order by pm.created_at;


-- verify one
update app.payment_methods
set status = 'verified'
where id = '00000000-0000-0000-0000-000000000000';


-- block one (it stays on record, can no longer be paid to, and the user cannot add it back)
update app.payment_methods
set status = 'disabled', is_default = false
where id = '00000000-0000-0000-0000-000000000000';
