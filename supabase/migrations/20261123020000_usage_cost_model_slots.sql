-- Name the two cleanup racers by slot, not by provider, so changing a model or provider never needs a new column:
-- cleanup_model1_micro is the race's first racer, cleanup_model2_micro its second (today Cerebras and Groq, both
-- gpt-oss-120b through OpenRouter).

alter table public.usage_daily rename column cerebras_micro to cleanup_model1_micro;
alter table public.usage_daily rename column groq_micro to cleanup_model2_micro;

-- Same types as before, so the old function has to go: a server still calling it with the old names fails its write
-- until the deploy, and keeps the counts in memory until then.
drop function public.add_usage(uuid, date, integer, numeric, bigint, bigint, bigint, bigint);

create function public.add_usage(p_user uuid, p_day date, p_dictations integer, p_seconds numeric, p_stt_micro bigint,
                                 p_cleanup_model1_micro bigint, p_cleanup_model2_micro bigint, p_other_micro bigint)
returns table (dictations integer, audio_seconds numeric, cost_micro bigint)
language sql security definer set search_path = '' as $$
  insert into public.usage_daily as u (user_id, day, dictations, audio_seconds, cost_micro,
                                       stt_micro, cleanup_model1_micro, cleanup_model2_micro, other_micro)
  values (p_user, p_day, p_dictations, p_seconds,
          p_stt_micro + p_cleanup_model1_micro + p_cleanup_model2_micro + p_other_micro,
          p_stt_micro, p_cleanup_model1_micro, p_cleanup_model2_micro, p_other_micro)
  on conflict (user_id, day) do update
    set dictations = u.dictations + excluded.dictations, audio_seconds = u.audio_seconds + excluded.audio_seconds,
        cost_micro = u.cost_micro + excluded.cost_micro, stt_micro = u.stt_micro + excluded.stt_micro,
        cleanup_model1_micro = u.cleanup_model1_micro + excluded.cleanup_model1_micro,
        cleanup_model2_micro = u.cleanup_model2_micro + excluded.cleanup_model2_micro,
        other_micro = u.other_micro + excluded.other_micro
  returning u.dictations, u.audio_seconds, u.cost_micro;
$$;

revoke execute on function public.add_usage(uuid, date, integer, numeric, bigint, bigint, bigint, bigint)
  from public, anon, authenticated;
grant  execute on function public.add_usage(uuid, date, integer, numeric, bigint, bigint, bigint, bigint) to service_role;
