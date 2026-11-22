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
_pending = {}  # (user id, day) -> [dictations, seconds] counted here and not yet written
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
    with _lock:
        _keys.clear(); _keys.update(keys)
        _caps.clear(); _caps.update(caps)
        _used.clear(); _used.update(used)
    LOADED.set()


def flush() -> None:
    with _lock:
        batch = dict(_pending)
    for (user, day), (n, secs) in batch.items():
        row = _rest("POST", "rpc/add_usage", {"p_user": user, "p_day": day, "p_dictations": n, "p_seconds": secs})[0]
        with _lock:
            left = _pending[(user, day)]
            left[0] -= n; left[1] -= secs  # more may have been counted while this was in flight
            if left[0] <= 0 and left[1] <= 0:
                del _pending[(user, day)]
            if day == today():
                _used[user] = [row["dictations"], float(row["audio_seconds"])]


def _loop() -> None:
    last_refresh = 0.0
    while True:
        try:
            flush()
            if time.monotonic() - last_refresh >= REFRESH_S:
                refresh()
                last_refresh = time.monotonic()
        except Exception as exc:  # Supabase down or slow: keep serving from memory, try again next round
            print(time.strftime("%H:%M:%S"), f"accounts: sync failed ({str(exc)[:150]})", flush=True)
        time.sleep(FLUSH_S)


def start() -> None:
    if ENABLED:
        threading.Thread(target=_loop, daemon=True).start()


def ready() -> bool:
    return not ENABLED or LOADED.is_set()


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


def record(user: str, seconds: float) -> None:
    if not ENABLED:
        return
    with _lock:
        p = _pending.setdefault((user, today()), [0, 0.0])
        p[0] += 1; p[1] += round(seconds, 1)


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
    _keys[hashlib.sha256(b"utr_x").hexdigest()] = "u"
    assert user_for("utr_x") == "u" and user_for("utr_y") is None
    print("ok")
