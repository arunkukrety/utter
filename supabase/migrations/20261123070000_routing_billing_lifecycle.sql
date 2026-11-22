-- 1. Routing by plan: which speech-to-text model and which cleanup each plan gets. The server reads these with the
--    plans every 30 s, so a change here needs no deploy. stt_model is an OpenRouter model id; cleanup is a server
--    cleanup route (orrace = the two-model race in cleanup_models). The owner token and keys.json users have no plan
--    and keep the server's defaults.
alter table public.plans
  add column stt_model text not null default 'openai/whisper-large-v3-turbo',
  add column cleanup   text not null default 'orrace'
    check (cleanup in ('orrace', 'race', 'cerebras', 'groq', 'openrouter', 'none'));
update public.plans set stt_model = 'assemblyai/universal-3-5-pro' where id = 'pro';
-- free and basic: Whisper Turbo (~$0.012/h); pro: AssemblyAI Universal 3.5 Pro (~$0.21/h, BYOK).


-- 3. Subscriptions, whatever the payment provider. Only the server's secret key writes here, through
--    apply_subscription(), which also sets the user's plan, so a plan is never edited by hand.
create table public.subscriptions (
  user_id            uuid primary key references auth.users (id) on delete cascade,
  provider           text not null,                -- e.g. paddle, lemonsqueezy, stripe
  customer_id        text,
  subscription_id    text not null,
  plan               text not null references public.plans (id),
  status             text not null,                -- the provider's word: active, trialing, past_due, canceled, ...
  current_period_end timestamptz,
  cancel_at          timestamptz,
  updated_at         timestamptz not null default now(),
  unique (provider, subscription_id)
);

-- Every webhook event, once: a provider retries deliveries, and the same event must never apply twice.
create table public.payment_events (
  provider    text not null,
  event_id    text not null,
  type        text not null,
  user_id     uuid references auth.users (id) on delete set null,
  received_at timestamptz not null default now(),
  primary key (provider, event_id)
);

alter table public.subscriptions  enable row level security;
alter table public.payment_events enable row level security;
create policy "own subscription" on public.subscriptions for select using (auth.uid() = user_id);
revoke all on public.payment_events from anon, authenticated;
revoke all on public.subscriptions from anon;

-- Record one webhook event and apply it. Returns false (and changes nothing) if the event was seen before. A
-- subscription that is active or trialing puts the user on its plan; anything else puts them back on free.
create function public.apply_subscription(
  p_provider text, p_event_id text, p_event_type text, p_user uuid, p_customer_id text, p_subscription_id text,
  p_plan text, p_status text, p_current_period_end timestamptz, p_cancel_at timestamptz)
returns boolean language plpgsql security definer set search_path = '' as $$
begin
  insert into public.payment_events (provider, event_id, type, user_id)
  values (p_provider, p_event_id, p_event_type, p_user)
  on conflict do nothing;
  if not found then
    return false;  -- a retried delivery
  end if;
  insert into public.subscriptions as s (user_id, provider, customer_id, subscription_id, plan, status,
                                         current_period_end, cancel_at, updated_at)
  values (p_user, p_provider, p_customer_id, p_subscription_id, p_plan, p_status, p_current_period_end, p_cancel_at, now())
  on conflict (user_id) do update set provider = excluded.provider, customer_id = excluded.customer_id,
    subscription_id = excluded.subscription_id, plan = excluded.plan, status = excluded.status,
    current_period_end = excluded.current_period_end, cancel_at = excluded.cancel_at, updated_at = now();
  update public.profiles
     set plan = case when p_status in ('active', 'trialing') then p_plan else 'free' end
   where user_id = p_user;
  return true;
end $$;
revoke execute on function public.apply_subscription(text, text, text, uuid, text, text, text, text, timestamptz, timestamptz)
  from public, anon, authenticated;
grant execute on function public.apply_subscription(text, text, text, uuid, text, text, text, text, timestamptz, timestamptz)
  to service_role;


-- 4. A user deletes their own account. Their profile, keys, usage and subscription row go with it (cascade); their
--    analysis log rows stay with the user cleared (set null). Refused while a paid subscription is live, so nobody is
--    billed for an account that no longer exists: they cancel first.
create function public.delete_my_account() returns void
language plpgsql security definer set search_path = '' as $$
begin
  if auth.uid() is null then raise exception 'sign in first'; end if;
  if exists (select 1 from public.subscriptions where user_id = auth.uid() and status in ('active', 'trialing', 'past_due')) then
    raise exception 'cancel your subscription first';
  end if;
  delete from auth.users where id = auth.uid();
end $$;
revoke execute on function public.delete_my_account() from public, anon;
grant execute on function public.delete_my_account() to authenticated;


-- 5. Block a user without deleting anything: the server refuses their dictations within 30 s.
--    update profiles set disabled = true, disabled_reason = '...' where user_id = '...';
alter table public.profiles
  add column disabled        boolean not null default false,
  add column disabled_reason text;


-- 6. Log retention: raw analysis rows are kept 90 days. Before they go, each finished day's model summary is copied to
--    model_performance_history, which is kept for good.
create table public.model_performance_history as select * from public.model_performance_daily with no data;
alter table public.model_performance_history add primary key (day, part, model, provider);
alter table public.model_performance_history enable row level security;
revoke all on public.model_performance_history from anon, authenticated;

create function public.roll_up_and_prune_logs() returns void
language sql security definer set search_path = '' as $$
  insert into public.model_performance_history
  select * from public.model_performance_daily where day < current_date
  on conflict (day, part, model, provider) do nothing;
  delete from public.provider_calls where at < now() - interval '90 days';
  delete from public.dictations     where at < now() - interval '90 days';
$$;
revoke execute on function public.roll_up_and_prune_logs() from public, anon, authenticated;

create extension if not exists pg_cron;
select cron.schedule('utter-roll-up-and-prune-logs', '15 3 * * *', 'select public.roll_up_and_prune_logs()');


-- 7. When each key was last used, written by the server in its 10 s batch (to the minute is plenty).
alter table public.api_keys add column last_used_at timestamptz;
