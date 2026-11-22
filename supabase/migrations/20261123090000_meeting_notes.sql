-- Meeting notes (Linux app first): which plans include them. The server refuses mode=meeting and mode=notes for a
-- plan without it. Meeting audio counts toward the plan's daily audio cap (not its dictation count), and its costs
-- (transcription and notes) land in usage_daily.other_micro.
alter table public.plans add column meetings boolean not null default false;
update public.plans set meetings = true where id = 'pro';
