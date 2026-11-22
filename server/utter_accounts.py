"""Utter accounts on Supabase: whose key this is, their plan's daily caps, and how much they have used today.

Nothing here touches the dictation's critical path. Keys, plans and today's totals sit in memory and are refreshed every
REFRESH_S; usage is counted in memory and written every FLUSH_S with one atomic add per user (two server instances can
write at once during a deploy). If Supabase goes down, the last copy keeps working; a NEW instance that never managed a
first load reports not-ready, so a deploy never swaps in a server that would turn every user away.

Set SUPABASE_URL and SUPABASE_SECRET_KEY to turn it on; without them every call here is a no-op.
"""
import datetime, hashlib, json, os, threading, time, urllib.request

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SECRET = os.environ.get("SUPABASE_SECRET_KEY", "")
ENABLED = bool(URL and SECRET)
REFRESH_S = 30
FLUSH_S = 10
PAGE = 1000  # PostgREST returns at most this many rows per request

_lock = threading.Lock()
_keys = {}     # sha256(key) -> user id
_caps = {}     # user id -> (daily dictations, daily audio seconds)
_used = {}     # user id -> [dictations, seconds] today, as Supabase last told us
_models = []   # [(model, provider)] for cleanup slots 1, 2, from public.cleanup_models; empty = the server's defaults
PARTS = ("stt", "cleanup_model1", "cleanup_model2", "other")  # cost parts, each its own usage_daily column: <part>_micro
_pending = {}  # (user id, day) -> [dictations, seconds, µ$ per part in PARTS order...] counted here and not yet written
LOADED = threading.Event()


def today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _rest(method: str, path: str, body=None):
    headers = {"apikey": SECRET, "Content-Type": "application/json"}
    if SECRET.startswith("eyJ"):  # a legacy service_role JWT also has to be the bearer; new sb_secret_ keys must not be
        headers["Authorization"] = "Bearer " + SECRET
    req = urllib.request.Request(f"{URL}/rest/v1/{path}", method=method, headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"null")


def _all(path: str) -> list:
    rows, offset = [], 0
    while True:
        page = _rest("GET", f"{path}&limit={PAGE}&offset={offset}")
        rows += page
        if len(page) < PAGE:
            return rows
        offset += PAGE


def refresh() -> None:
    day = today()
    keys = {r["key_hash"]: r["user_id"] for r in _all("api_keys?select=key_hash,user_id&revoked_at=is.null")}
    caps = {r["user_id"]: (r["plans"]["daily_dictations"], r["plans"]["daily_audio_seconds"])
            for r in _all("profiles?select=user_id,plans(daily_dictations,daily_audio_seconds)")}
    used = {r["user_id"]: [r["dictations"], float(r["audio_seconds"])]
            for r in _all(f"usage_daily?select=user_id,dictations,audio_seconds&day=eq.{day}")}
    models = [(r["model"], r["provider"]) for r in _all("cleanup_models?select=model,provider&order=slot")]
    if models != _models:  # at start, and whenever someone edits the table
        print(time.strftime("%H:%M:%S"), "cleanup models:", ", ".join(f"{i}={m} on {p}" for i, (m, p) in
                                                                      enumerate(models, 1)) or "server defaults", flush=True)
    with _lock:
        _keys.clear(); _keys.update(keys)
        _caps.clear(); _caps.update(caps)
        _used.clear(); _used.update(used)
        _models[:] = models
    LOADED.set()


def flush() -> None:
    with _lock:
        batch = {k: list(v) for k, v in _pending.items()}  # copies: more may be counted while this is in flight
    for (user, day), counts in batch.items():
        row = _rest("POST", "rpc/add_usage", {"p_user": user, "p_day": day, "p_dictations": counts[0],
                                              "p_seconds": counts[1],
                                              **{f"p_{part}_micro": c for part, c in zip(PARTS, counts[2:])}})[0]
        with _lock:
            left = _pending[(user, day)]
            for i, c in enumerate(counts):
                left[i] -= c
            if all(v <= 0 for v in left):
                del _pending[(user, day)]
            if day == today():
                _used[user] = [row["dictations"], float(row["audio_seconds"])]


def _loop() -> None:
    last_refresh = 0.0
    while True:
        # Separately, so a write that keeps failing can never stop keys, revocations and plans from refreshing.
        try:
            flush()
        except Exception as exc:  # Supabase down or slow: keep counting in memory, try again next round
            print(time.strftime("%H:%M:%S"), f"accounts: usage write failed ({str(exc)[:150]})", flush=True)
        if time.monotonic() - last_refresh >= REFRESH_S:
            try:
                refresh()
                last_refresh = time.monotonic()
            except Exception as exc:  # keep serving from the last copy
                print(time.strftime("%H:%M:%S"), f"accounts: refresh failed ({str(exc)[:150]})", flush=True)
        time.sleep(FLUSH_S)


def start() -> None:
    if ENABLED:
        threading.Thread(target=_loop, daemon=True).start()


def ready() -> bool:
    return not ENABLED or LOADED.is_set()


def cleanup_models() -> list:
    """[(model, provider)] for cleanup slots 1 and 2 from the last refresh, or [] to use the server's defaults."""
    with _lock:
        return list(_models)


def user_for(key: str):
    """The Supabase user id behind a key, or None."""
    with _lock:
        return _keys.get(hashlib.sha256(key.encode()).hexdigest())


def _today(user: str) -> tuple:
    n, secs = _used.get(user, [0, 0.0])
    p = _pending.get((user, today()), [0, 0.0])
    return n + p[0], secs + p[1]


def over_limit(user: str):
    """None while the user may dictate, else a sentence to show them. Users not in Supabase (the owner token, the
    legacy keys file) have no caps."""
    with _lock:
        caps = _caps.get(user)
        if not caps:
            return None
        n, secs = _today(user)
    if n >= caps[0] or secs >= caps[1]:
        return (f"You've reached today's limit ({caps[1] // 60} minutes or {caps[0]} dictations). "
                "It resets at midnight UTC, or upgrade your plan for more.")
    return None


def _count(user: str, n: int, seconds: float, part: str = "", micro: int = 0) -> None:
    if not ENABLED:
        return
    with _lock:
        if user not in _caps:  # the owner token and keys.json users are not Supabase accounts: nothing to write
            return
        p = _pending.setdefault((user, today()), [0, 0.0] + [0] * len(PARTS))
        p[0] += n; p[1] += round(seconds, 1)
        if part:
            p[2 + PARTS.index(part if part in PARTS else "other")] += micro


def record(user: str, seconds: float) -> None:
    """One finished dictation of this length."""
    _count(user, 1, seconds)


def add_cost(user: str, part: str, micro: int) -> None:
    """One part's provider cost in microdollars, whenever it is known (a race's loser reports after the dictation)."""
    _count(user, 0, 0.0, part, micro)


def audio_seconds(data: bytes, fmt: str) -> float:
    """How long a recording is. Ogg Opus: the last page's granule position (48 kHz samples). Anything else: the size
    at the app's 32 kbps MP3 rate, close enough for a daily cap."""
    if fmt == "ogg":
        i = data.rfind(b"OggS")
        if i >= 0 and len(data) >= i + 14:
            return int.from_bytes(data[i + 6:i + 14], "little") / 48000
    return len(data) * 8 / 32000


if __name__ == "__main__":  # self-check, no network
    page = b"OggS\x00\x04" + (48000 * 7).to_bytes(8, "little") + b"\x00" * 20
    assert abs(audio_seconds(b"junk" + page, "ogg") - 7) < 1e-9
    assert audio_seconds(b"\x00" * 4000, "mp3") == 1.0
    _caps["u"], _used["u"] = (10, 60), [9, 30.0]
    ENABLED = True
    assert over_limit("u") is None
    record("u", 5)
    assert over_limit("u") and over_limit("nobody") is None
    add_cost("u", "stt", 321); add_cost("u", "cleanup_model2", 39); add_cost("u", "cleanup_model1", 40)
    add_cost("u", "x", 2)
    record("owner", 5); add_cost("owner", "stt", 1)
    assert _pending == {("u", today()): [1, 5.0, 321, 40, 39, 2]}  # non-Supabase users are never queued for a write
    _keys[hashlib.sha256(b"utr_x").hexdigest()] = "u"
    assert user_for("utr_x") == "u" and user_for("utr_y") is None
    print("ok")
