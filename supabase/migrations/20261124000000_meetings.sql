-- Meeting notes, kept: each meeting's transcript and notes, so a user can find them again (website, other devices).
-- Written only by the server (secret key) right after it writes the notes. A user reads and deletes only their own;
-- deleting the account deletes them. Kept until the user deletes them. The owner token and keys.json users have no
-- account: their meetings carry user_label instead of user_id.
-- Holding these means Utter stores what was said in people's meetings, including by people who did not record them:
-- the privacy policy must say so before this is offered to the public.

create table public.meetings (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid references auth.users (id) on delete cascade,
  user_label       text,
  started_at       timestamptz,
  duration_seconds numeric(10, 1),
  speakers         smallint,       -- distinct "Speaker N" labels among the others (not counting You)
  transcript       text not null,
  notes            text not null,
  notes_model      text,
  created_at       timestamptz not null default now()
);
create index meetings_user on public.meetings (user_id, created_at desc);

alter table public.meetings enable row level security;
create policy "own meetings: read"   on public.meetings for select using (auth.uid() = user_id);
create policy "own meetings: delete" on public.meetings for delete using (auth.uid() = user_id);
revoke all on public.meetings from anon;
revoke insert, update on public.meetings from authenticated;  -- only the server writes them
