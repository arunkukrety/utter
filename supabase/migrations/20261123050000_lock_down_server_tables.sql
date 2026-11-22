-- The view ran with its owner's rights, so it would have skipped provider_calls' RLS (Supabase flags it "Unrestricted").
-- security_invoker makes it run as whoever queries it, so RLS applies: only the server's secret key and the SQL editor
-- see rows.
alter view public.model_performance_daily set (security_invoker = true);

-- Server-only tables: RLS with no policies already returns nothing to the public API; drop the default grants too, so
-- a policy added by mistake later still exposes nothing.
revoke all on public.cleanup_models, public.dictations, public.provider_calls from anon, authenticated;
