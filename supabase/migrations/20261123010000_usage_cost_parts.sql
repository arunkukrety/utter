-- Cost per part, in integer microdollars, beside the total cost_micro (which stays the sum of the four):
--   stt_micro       AssemblyAI through OpenRouter (BYOK: audio seconds x $0.21/h)
--   cerebras_micro  the Cerebras side of the OpenRouter cleanup race
--   groq_micro      the Groq side of the race (both sides are billed, the loser too)
--   other_micro     any other paid cleanup call (the single-provider OpenRouter route)
-- Rows written before this migration have only the total.

alter table public.usage_daily
  add column stt_micro      bigint not null default 0 check (stt_micro >= 0),
  add column cerebras_micro bigint not null default 0 check (cerebras_micro >= 0),
  add column groq_micro     bigint not null default 0 check (groq_micro >= 0),
  add column other_micro    bigint not null default 0 check (other_micro >= 0);

-- The 5-argument add_usage stays until no server calls it (a deploy runs old and new side by side).
create function public.add_usage(p_user uuid, p_day date, p_dictations integer, p_seconds numeric,
                                 p_stt_micro bigint, p_cerebras_micro bigint, p_groq_micro bigint, p_other_micro bigint)
returns table (dictations integer, audio_seconds numeric, cost_micro bigint)
language sql security definer set search_path = '' as $$
  insert into public.usage_daily as u (user_id, day, dictations, audio_seconds, cost_micro,
                                       stt_micro, cerebras_micro, groq_micro, other_micro)
  values (p_user, p_day, p_dictations, p_seconds, p_stt_micro + p_cerebras_micro + p_groq_micro + p_other_micro,
          p_stt_micro, p_cerebras_micro, p_groq_micro, p_other_micro)
  on conflict (user_id, day) do update
    set dictations = u.dictations + excluded.dictations, audio_seconds = u.audio_seconds + excluded.audio_seconds,
        cost_micro = u.cost_micro + excluded.cost_micro, stt_micro = u.stt_micro + excluded.stt_micro,
        cerebras_micro = u.cerebras_micro + excluded.cerebras_micro, groq_micro = u.groq_micro + excluded.groq_micro,
        other_micro = u.other_micro + excluded.other_micro
  returning u.dictations, u.audio_seconds, u.cost_micro;
$$;

revoke execute on function public.add_usage(uuid, date, integer, numeric, bigint, bigint, bigint, bigint)
  from public, anon, authenticated;
grant  execute on function public.add_usage(uuid, date, integer, numeric, bigint, bigint, bigint, bigint) to service_role;
