-- How sure the transcriber was about each dictation (AssemblyAI's 0..1 score; 0 when Whisper shows its own signs of an
-- invented transcript; null when the route gives none). Dictations under UTTER_UNCLEAR_BELOW (0.70) with 3+ words are
-- not pasted (error 'unclear'): this column is what that threshold gets tuned from.
alter table public.dictations add column stt_confidence numeric(5, 4);
