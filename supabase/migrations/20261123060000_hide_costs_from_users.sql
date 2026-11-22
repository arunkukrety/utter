-- A signed-in user could read their own usage_daily row in full, cost columns included: what we pay providers per
-- dictation. Column privileges fix it. RLS still limits them to their own rows; now they may read only these columns.
-- The server's secret key (service_role) is unaffected and keeps reading and writing everything.
--
-- A client must name the columns it wants (select=day,dictations,audio_seconds): select=* now fails for a user,
-- because * includes columns they may not read.

revoke select on public.usage_daily from anon, authenticated;
grant select (user_id, day, dictations, audio_seconds) on public.usage_daily to authenticated;
