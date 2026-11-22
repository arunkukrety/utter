-- Analysis logs, written by the server in the background (bulk, every 10 s), never on a dictation's critical path.
-- No dictation text or audio is stored here: sizes, timings, routes, costs and outcomes only.
-- user_id is set for Supabase accounts; the owner token and legacy keys.json users have user_label instead.

-- One row per dictation.
create table public.dictations (
  id            text primary key,                 -- also provider_calls.dictation_id
  at            timestamptz not null,
  user_id       uuid references auth.users (id) on delete set null,
  user_label    text,
  language      text,                             -- en | multi
  script        text,                             -- roman | devanagari (streaming only)
  stt_route     text,
  cleanup_route text,
  audio_seconds numeric(8, 2),
  upload_kb     integer,
  stt_ms        integer,
  cleanup_ms    integer,                          -- after the transcript (0-ish when the head start was used)
  total_ms      integer,
  head_start    boolean,                          -- the cleanup of the live transcript was used
  raw_chars     integer,
  text_chars    integer,
  ok            boolean not null,
  error         text
);
create index dictations_at on public.dictations (at);
create index dictations_user on public.dictations (user_id, at);

-- One row per provider call: STT, each side of a cleanup race (the loser too), any other cleanup call, failures too.
create table public.provider_calls (
  id                bigint generated always as identity primary key,
  at                timestamptz not null,
  dictation_id      text,
  user_id           uuid references auth.users (id) on delete set null,
  user_label        text,
  part              text not null,                -- stt | cleanup_model1 | cleanup_model2 | other
  model             text,
  provider          text,                         -- the route or provider we asked
  upstream          text,                         -- the provider OpenRouter says served it
  ms                integer not null,             -- request sent to response read
  ok                boolean not null,
  http_status       integer,
  error             text,
  won               boolean,                      -- race sides only: this answer was used
  cost_micro        bigint not null default 0,    -- 1 USD = 1,000,000
  prompt_tokens     integer,
  completion_tokens integer,
  reasoning_tokens  integer,
  audio_seconds     numeric(8, 2)                 -- STT: what the provider billed
);
create index provider_calls_at on public.provider_calls (at);
create index provider_calls_dictation on public.provider_calls (dictation_id);
create index provider_calls_model on public.provider_calls (part, model, provider, at);

-- Server only: RLS on and no policies.
alter table public.dictations     enable row level security;
alter table public.provider_calls enable row level security;

-- Per day and model: volume, success, latency percentiles, race wins and cost. Run it from the SQL editor.
create view public.model_performance_daily as
select
  date_trunc('day', at)::date                                              as day,
  part, model, provider,
  count(*)                                                                 as calls,
  round(100.0 * avg(ok::int), 1)                                           as ok_pct,
  percentile_cont(0.5)  within group (order by ms) filter (where ok)      as p50_ms,
  percentile_cont(0.9)  within group (order by ms) filter (where ok)      as p90_ms,
  percentile_cont(0.99) within group (order by ms) filter (where ok)      as p99_ms,
  count(*) filter (where won)                                              as wins,
  sum(cost_micro)                                                          as cost_micro,
  round(avg(cost_micro) filter (where ok))                                 as avg_cost_micro
from public.provider_calls
group by 1, 2, 3, 4;

-- The view would otherwise be readable through the public API: server and SQL editor only.
revoke all on public.model_performance_daily from anon, authenticated;
