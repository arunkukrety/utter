-- Which models clean up a dictation: the two racers of the OpenRouter cleanup race, by slot. Slot 1's cost lands in
-- usage_daily.cleanup_model1_micro, slot 2's in cleanup_model2_micro. Change a row to swap a model: the server re-reads
-- this table every 30 s in the background (never during a dictation) and uses it from memory, so no deploy is needed.
--   model    an OpenRouter model id, e.g. openai/gpt-oss-120b
--   provider the OpenRouter provider it is pinned to, e.g. cerebras (no fallback to another provider)
-- A bad model or provider fails that racer only; the other one answers, and if both fail the server falls back to
-- Cerebras Qwen on its own key.

create table public.cleanup_models (
  slot       smallint primary key check (slot in (1, 2)),
  model      text not null check (length(model) between 1 and 200),
  provider   text not null check (length(provider) between 1 and 100),
  updated_at timestamptz not null default now()
);

insert into public.cleanup_models (slot, model, provider) values
  (1, 'openai/gpt-oss-120b', 'cerebras'),
  (2, 'openai/gpt-oss-120b', 'groq');

-- Server only: RLS on and no policies, so only the secret key (which bypasses RLS) reads or changes it.
alter table public.cleanup_models enable row level security;
