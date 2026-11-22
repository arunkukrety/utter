-- What each user's dictations cost us, in integer microdollars (1 USD = 1,000,000), summed per UTC day: AssemblyAI
-- through OpenRouter (BYOK, so computed from the audio seconds) plus every OpenRouter cleanup call, both sides of a race.
-- This is what the Pro plan's monthly provider-cost cap will read.

alter table public.usage_daily add column cost_micro bigint not null default 0 check (cost_micro >= 0);

-- add_usage with cost. The 4-argument version stays until no server calls it (a deploy runs old and new side by side).
create function public.add_usage(p_user uuid, p_day date, p_dictations integer, p_seconds numeric, p_cost_micro bigint)
returns table (dictations integer, audio_seconds numeric, cost_micro bigint)
language sql security definer set search_path = '' as $$
  insert into public.usage_daily as u (user_id, day, dictations, audio_seconds, cost_micro)
  values (p_user, p_day, p_dictations, p_seconds, p_cost_micro)
  on conflict (user_id, day) do update
    set dictations = u.dictations + excluded.dictations, audio_seconds = u.audio_seconds + excluded.audio_seconds,
        cost_micro = u.cost_micro + excluded.cost_micro
  returning u.dictations, u.audio_seconds, u.cost_micro;
$$;

revoke execute on function public.add_usage(uuid, date, integer, numeric, bigint) from public, anon, authenticated;
grant  execute on function public.add_usage(uuid, date, integer, numeric, bigint) to service_role;
