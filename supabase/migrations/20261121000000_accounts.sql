-- Utter accounts: a user (Supabase Auth) has a plan, any number of API keys (stored as SHA-256 hashes only) and one
-- usage row per UTC day. Limits are per USER, not per key, so making more keys does not buy more usage.
-- The Utter server reads and writes with the secret key (bypasses RLS); a signed-in user sees only their own rows.

create extension if not exists pgcrypto with schema extensions;

create table public.plans (
  id                  text primary key,
  daily_dictations    integer not null check (daily_dictations >= 0),
  daily_audio_seconds integer not null check (daily_audio_seconds >= 0),
  max_keys            integer not null default 5
);
-- Starting caps; change them with an UPDATE, no deploy (the server re-reads them every 30 s).
insert into public.plans values
  ('free',   100,   900, 3),   -- 15 min of speech a day
  ('basic', 1500,  7200, 5),   -- 2 h
  ('pro',   4000, 21600, 10);  -- 6 h

create table public.profiles (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  plan       text not null default 'free' references public.plans (id),
  created_at timestamptz not null default now()
);

create table public.api_keys (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users (id) on delete cascade,
  name       text not null check (length(name) between 1 and 64),
  key_hash   text not null unique,          -- sha256 hex of the key; the key itself is never stored
  prefix     text not null,                 -- first characters, so a user can tell their keys apart
  created_at timestamptz not null default now(),
  revoked_at timestamptz
);
create index api_keys_user on public.api_keys (user_id);

create table public.usage_daily (
  user_id       uuid not null references auth.users (id) on delete cascade,
  day           date not null,
  dictations    integer not null default 0,
  audio_seconds numeric(12, 1) not null default 0,
  primary key (user_id, day)
);

-- Every new sign-up gets a free profile.
create function public.handle_new_user() returns trigger language plpgsql security definer set search_path = '' as $$
begin
  insert into public.profiles (user_id) values (new.id);
  return new;
end $$;
create trigger on_auth_user_created after insert on auth.users for each row execute function public.handle_new_user();

alter table public.plans       enable row level security;
alter table public.profiles    enable row level security;
alter table public.api_keys    enable row level security;
alter table public.usage_daily enable row level security;
create policy "plans are public"       on public.plans       for select using (true);
create policy "own profile"            on public.profiles    for select using (auth.uid() = user_id);
create policy "own keys"               on public.api_keys    for select using (auth.uid() = user_id);
create policy "own usage"              on public.usage_daily for select using (auth.uid() = user_id);
-- No insert/update/delete policies: users change keys only through the two functions below, and nobody but the
-- server writes usage or plans.

-- Make a key for the signed-in user and return it ONCE.
create function public.create_key(p_name text) returns text
language plpgsql security definer set search_path = '' as $$
declare
  k text;
  cap integer;
begin
  if auth.uid() is null then raise exception 'sign in first'; end if;
  select pl.max_keys into cap from public.profiles p join public.plans pl on pl.id = p.plan where p.user_id = auth.uid();
  if (select count(*) from public.api_keys where user_id = auth.uid() and revoked_at is null) >= coalesce(cap, 0) then
    raise exception 'key limit reached: revoke a key first';
  end if;
  k := 'utr_' || translate(encode(extensions.gen_random_bytes(32), 'base64'), '+/=', '-_');
  insert into public.api_keys (user_id, name, key_hash, prefix)
  values (auth.uid(), p_name, encode(extensions.digest(k, 'sha256'), 'hex'), left(k, 10));
  return k;
end $$;

create function public.revoke_key(p_id uuid) returns void
language sql security definer set search_path = '' as $$
  update public.api_keys set revoked_at = now() where id = p_id and user_id = auth.uid() and revoked_at is null;
$$;

-- The server adds usage atomically (two server instances can write at once during a deploy) and gets the new totals.
create function public.add_usage(p_user uuid, p_day date, p_dictations integer, p_seconds numeric)
returns table (dictations integer, audio_seconds numeric)
language sql security definer set search_path = '' as $$
  insert into public.usage_daily as u (user_id, day, dictations, audio_seconds)
  values (p_user, p_day, p_dictations, p_seconds)
  on conflict (user_id, day) do update
    set dictations = u.dictations + excluded.dictations, audio_seconds = u.audio_seconds + excluded.audio_seconds
  returning u.dictations, u.audio_seconds;
$$;

revoke execute on function public.create_key(text), public.revoke_key(uuid) from public, anon;
grant  execute on function public.create_key(text), public.revoke_key(uuid) to authenticated;
revoke execute on function public.add_usage(uuid, date, integer, numeric) from public, anon, authenticated;
grant  execute on function public.add_usage(uuid, date, integer, numeric) to service_role;
revoke execute on function public.handle_new_user() from public, anon, authenticated;
