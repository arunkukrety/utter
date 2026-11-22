-- Which OpenRouter provider a plan's speech-to-text model goes to first. Unpinned, OpenRouter picked the cheapest
-- Whisper host, which took 1.1 s; Groq answered in ~314 ms p50 on 50 real dictations (2026-11-22). It is an order, not
-- a lock: if Groq is down OpenRouter falls back to another host rather than failing the dictation. Null = let
-- OpenRouter choose. Groq bills a 10 s minimum per request, so a typical dictation costs ~150 micro-dollars here.
alter table public.plans add column stt_provider text;
update public.plans set stt_provider = 'groq' where stt_model like 'openai/whisper%';
