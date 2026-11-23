-- The paid-plan cost cap, as a step-down ladder: as a user's provider cost in the current billing period passes a share
-- of the plan's budget, their dictations move to cheaper routes. Nothing is ever refused. All of it is data: change a
-- budget or a step here and the server follows within 30 s.

-- The budget per billing period, in microdollars. Null = no ladder (the plan's own routes always).
alter table public.plans add column monthly_budget_micro bigint check (monthly_budget_micro > 0);
update public.plans set monthly_budget_micro = 4000000 where id = 'pro';  -- $4 of the $7.99 Pro price

-- The steps: from from_pct of the budget upward, this STT and cleanup. The highest step reached applies, also past 100%.
-- cleanup: orrace = both cleanup_models racing; orslot2 = only slot 2 (the cheaper racer); orslot1 = only slot 1.
create table public.plan_tiers (
  plan         text not null references public.plans (id) on delete cascade,
  from_pct     smallint not null check (from_pct between 0 and 1000),
  stt_model    text not null,
  stt_provider text,
  cleanup      text not null check (cleanup in ('orrace', 'orslot1', 'orslot2', 'race', 'cerebras', 'groq', 'openrouter', 'none')),
  primary key (plan, from_pct)
);
insert into public.plan_tiers (plan, from_pct, stt_model, stt_provider, cleanup) values
  ('pro', 0,  'assemblyai/universal-3-5-pro',   null,   'orrace'),   -- fastest: AssemblyAI + the race
  ('pro', 50, 'assemblyai/universal-3-5-pro',   null,   'orslot2'),  -- the race is most of the cost: one racer
  ('pro', 75, 'openai/whisper-large-v3-turbo', 'groq',  'orslot2');  -- and the cheaper STT

alter table public.plan_tiers enable row level security;
revoke all on public.plan_tiers from anon, authenticated;

-- What each user has spent in their current billing period: from their subscription's renewal date a month back, or
-- the calendar month without one. Server-only (it bypasses nothing: RLS of the tables underneath applies).
create view public.period_spend with (security_invoker = true) as
select p.user_id, ps.period_start, coalesce(sum(u.cost_micro), 0)::bigint as spent_micro
from public.profiles p
left join public.subscriptions s on s.user_id = p.user_id and s.status in ('active', 'trialing', 'past_due')
cross join lateral (select coalesce((s.current_period_end - interval '1 month')::date,
                                    date_trunc('month', now() at time zone 'utc')::date) as period_start) ps
left join public.usage_daily u on u.user_id = p.user_id and u.day >= ps.period_start
group by p.user_id, ps.period_start;
revoke all on public.period_spend from anon, authenticated;
