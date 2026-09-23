-- =============================================================================
-- Withdrawals are paid by hand. Every request stops at status 'review'. The
-- balance (amount + fee) was taken when the user asked, so approving only
-- records the payout you made; rejecting refunds it.
-- For M-Pesa, local_amount is the KSh to send.
-- =============================================================================

-- 1 · what is waiting, oldest first
select t.reference, t.created_at, p.email, p.full_name, t.method,
       t.amount_usd, t.fee_usd, t.net_usd, t.local_amount, t.local_currency,
       t.destination, pm.details
from app.transactions t
join app.profiles p on p.id = t.user_id
left join app.payment_methods pm on pm.id = t.payment_method_id
where t.type = 'withdrawal' and t.status in ('review', 'processing')
order by t.created_at;


-- 2 · approve one you have paid by hand (set the reference, and the receipt
--     or transaction hash you paid with)
update app.transactions
set status = 'completed',
    completed_at = now(),
    provider_receipt = 'RECEIPT-OR-TX-HASH'
where reference = 'WD-XXXXXXXXXX'
  and type = 'withdrawal'
  and status in ('review', 'processing');


-- 3 · reject one and give the money back, in one go
with rejected as (
  update app.transactions
  set status = 'failed',
      failure_reason = 'Rejected on review',
      completed_at = now()
  where reference = 'WD-XXXXXXXXXX'
    and type = 'withdrawal'
    and status in ('review', 'processing')
  returning account_id, net_usd
)
update app.accounts a
set balance = a.balance + r.net_usd
from rejected r
where a.id = r.account_id;
