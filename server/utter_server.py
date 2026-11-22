#!/usr/bin/env python3
"""Utter server: audio in, clean text out. One request does the whole after-you-stop pipeline next to the providers:
ElevenLabs Scribe v2 (batch) on the recording, then Groq cleanup, so the app makes one trip instead of two.

  POST /dictate?language=en|multi   body: the recording as MP3 (audio/mpeg)   header: Authorization: Bearer <token>
       -> {"text": cleaned, "raw": transcript, "stt_ms": .., "cleanup_ms": .., "total_ms": ..}
  GET  /health                      -> {"ok": true}
  WS   /stream?language=&script=[&stt=&cleanup=]  (port UTTER_WS_PORT; providers default to DEFAULT_*)  header: Authorization: Bearer <token>
       send Ogg/Opus bytes as binary frames WHILE the user speaks, then the text frame "end", then (optional)
       {"live": "<the app's live transcript>"}: cleanup starts on it at once, while Scribe works on the audio, and is
       used if Scribe's transcript has the same words (a head start of about one cleanup);
       -> one text frame with the same JSON as /dictate. The upload overlaps the speaking, so only the last moment
       is left to send when they stop.

Standard library only. Listens on 127.0.0.1; a Cloudflare tunnel puts HTTPS in front of it. Keys and the token come
from the environment (systemd EnvironmentFile), never from the request. Provider connections are kept open and reused,
so a request pays no handshake to ElevenLabs or Groq.
"""
import re, asyncio, contextvars, hashlib, http.client, json, os, secrets, subprocess, threading, time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import parse_qs, urlparse

import utter_accounts as accounts

ELEVENLABS_KEY = os.environ["ELEVENLABS_KEY"]
GROQ_KEY = os.environ["GROQ_KEY"]
# cleanup on Cerebras: 154 ms median vs Groq 258 ms (10 runs from this VPS, 2026-11-16); falls back to Groq
CEREBRAS_KEY = os.environ.get("CEREBRAS_KEY", "")
CEREBRAS_CLEANUP_MODEL = os.environ.get("CEREBRAS_CLEANUP_MODEL", "qwen-3.8-27b")
SURPLUS_KEY = os.environ.get("SURPLUS_KEY", "")
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
CARTESIA_KEY = os.environ.get("CARTESIA_KEY", "")
CARTESIA_STT_MODEL = os.environ.get("CARTESIA_STT_MODEL", "ink-whisper")  # Whisper-based; ~146 ms batch from here
# AssemblyAI through OpenRouter: ~100 ms from here, billed per audio second with no minimum (measured 2026-11-14)
OPENROUTER_STT_MODEL = os.environ.get("OPENROUTER_STT_MODEL", "assemblyai/universal-3-5-pro")
# Our AssemblyAI key is BYOK on OpenRouter, so its usage.cost reads 0 and AssemblyAI bills us directly: $0.21 an hour
# for universal-3-5-pro (2026-11-16). Used only when OpenRouter reports no cost.
OPENROUTER_STT_USD_PER_HOUR = float(os.environ.get("OPENROUTER_STT_USD_PER_HOUR", "0.21"))

# What the current dictation's provider calls cost: a callable taking (part, microdollars), set per dictation. Threads
# started for it (asyncio.to_thread, the race) carry it along, so a race's loser that finishes later is still charged.
# Parts (accounts.PARTS): "stt" (AssemblyAI), "cleanup_model1" and "cleanup_model2" (the first and second racer of a
# cleanup race, whatever model or provider each is), "other" (any other paid cleanup call).
_bill = contextvars.ContextVar("bill", default=None)


def charge(usd, part: str) -> None:
    f = _bill.get()
    if f and usd:
        f(part, round(float(usd) * 1_000_000))
# cleanup through OpenRouter, pinned to one provider (its capacity, not our Groq free-tier limit): 178 ms median
OPENROUTER_CLEANUP_MODEL = os.environ.get("OPENROUTER_CLEANUP_MODEL", "meta-llama/llama-3.1-8b-instruct")
OPENROUTER_CLEANUP_PROVIDER = os.environ.get("OPENROUTER_CLEANUP_PROVIDER", "groq")
SURPLUS_CLEANUP_MODEL = "llama-3.3-70b-instruct"
GROQ_STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
TOKEN = os.environ["UTTER_TOKEN"]
# The app sends only the user's language and script; which providers run is decided here. A request may still name
# them (older apps do), else these: AssemblyAI via OpenRouter, then Cerebras cleanup (each falls back to Groq).
DEFAULT_STT = os.environ.get("UTTER_DEFAULT_STT") or ("openrouter" if OPENROUTER_KEY else "direct")
DEFAULT_CLEANUP = os.environ.get("UTTER_DEFAULT_CLEANUP") or ("orrace" if OPENROUTER_KEY else "cerebras" if CEREBRAS_KEY else "groq")
# orrace: gpt-oss-120b through OpenRouter (paid), pinned once to Cerebras and once to Groq, sent at once; the first good
# answer wins. 100 paced rounds of real dictations from this VPS (2026-11-21): p50 271 / p99 647 ms, Cerebras alone
# p99 2,109 ms. Same model on both, so the text does not depend on which one answered. If both fail, Qwen on our own
# Cerebras key cleans it. ~$1/month at one person's use.
# race: the same race on our own FREE keys. Do not make it the default: Cerebras allows gpt-oss-120b only 5 requests a
# minute on the free tier (Qwen: 450), so ordinary bursts of dictation got both sides 429 (57 of 100 in a 2026-11-21 test).
RACE_CEREBRAS_MODEL = os.environ.get("RACE_CEREBRAS_MODEL", "gpt-oss-120b")
RACE_GROQ_MODEL = os.environ.get("RACE_GROQ_MODEL", "openai/gpt-oss-120b")


# When on, every dictation is kept: <day>/<HHMMSS>-<id>.<ogg|mp3> (the audio as received) and a .json beside it with the
# transcript before and after cleanup. Days and times are UTC. The folder is the service's private state directory
# (systemd StateDirectory, 0700). Off by default; UTTER_SAVE_DICTATIONS=1 turns it on (debugging).
SAVE_DICTATIONS = os.environ.get("UTTER_SAVE_DICTATIONS", "0") == "1"  # off unless asked for: dictations are private
DICTATIONS = os.path.join(os.environ.get("STATE_DIRECTORY") or "/var/lib/utter-server", "dictations")


def save_dictation(audio: bytes, ext: str, meta: dict) -> None:
    """Writes the recording and its record. Runs after the reply is sent, and never fails a dictation."""
    if not SAVE_DICTATIONS:
        return
    try:
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        name = time.strftime("%H%M%S", time.gmtime(now)) + "-" + secrets.token_hex(3)
        folder = os.path.join(DICTATIONS, day)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        with open(os.path.join(folder, f"{name}.{ext}"), "wb") as f:
            f.write(audio)
        record = {"id": f"{day}/{name}", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                  "audio": f"{name}.{ext}", "audio_bytes": len(audio), **meta}
        with open(os.path.join(folder, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(time.strftime("%H:%M:%S"), f"could not save the dictation: {exc}", flush=True)


# Utter keys, one per user: {sha256(key): {"name", "created", "revoked"}}, managed with utter_keys.py. Only hashes are
# stored, so the file itself unlocks nothing. Re-read whenever it changes, so adding or revoking needs no restart.
# UTTER_TOKEN stays valid as the owner's key.
KEYS_FILE = os.path.join(os.environ.get("STATE_DIRECTORY") or "/var/lib/utter-server", "keys.json")
_keys = {"mtime": None, "data": {}}


def key_owner(authorization: str):
    """The name behind an "Authorization: Bearer <key>" header, or None if the key is unknown or revoked."""
    key = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not key:
        return None
    if secrets.compare_digest(key, TOKEN):
        return "owner"
    try:
        mtime = os.stat(KEYS_FILE).st_mtime_ns
        if mtime != _keys["mtime"]:
            with open(KEYS_FILE) as f:
                _keys.update(mtime=mtime, data=json.load(f))
    except (OSError, ValueError):
        pass  # no keys file yet, or a half-written one: keep what was loaded last
    entry = _keys["data"].get(hashlib.sha256(key.encode()).hexdigest())
    if entry:
        return entry["name"] if not entry.get("revoked") else None
    return accounts.user_for(key)  # a Supabase account's key: its user id


def pick(q: dict, name: str, default: str, allowed: tuple) -> str:
    value = q.get(name, [default])[0]
    return value if value in allowed else allowed[0]
PORT = int(os.environ.get("UTTER_PORT", "8190"))
WS_PORT = int(os.environ.get("UTTER_WS_PORT", "8191"))
SCRIBE_MODEL = os.environ.get("SCRIBE_MODEL", "scribe_v2")
CLEANUP_MODEL = os.environ.get("CLEANUP_MODEL", "qwen/qwen3.8-27b")
CLEANUP_EXTRA = {"reasoning_effort": "none"} if "qwen" in CLEANUP_MODEL else {"reasoning_effort": "low"}
MAX_AUDIO = 25 * 1024 * 1024  # ~100 min of 32 kbps MP3; refuse anything bigger
TIMEOUT_S = 20
# cleanup normally takes 0.2-0.7 s; a stalled Groq request must retry (and then give up) inside the app's 8 s wait
CLEANUP_TIMEOUT_S = 3

# the cleanup prompt lives only here; deploy copies cleanup_prompt.txt in
CLEANUP_PROMPT = os.environ.get("CLEANUP_PROMPT") or open(os.path.join(os.path.dirname(__file__), "cleanup_prompt.txt")).read()
# Hinglish typed the way people chat (Roman letters, English words spelled in English), for apps whose Hindi setting is
# Roman: AssemblyAI writes Hinglish all in Devanagari, which letter-by-letter romanising turns into "miting", "minat"
_lines = CLEANUP_PROMPT.rstrip("\n").split("\n")
CLEANUP_PROMPT_ROMAN = "\n".join(_lines[:-1] + [open(os.path.join(os.path.dirname(__file__), "hinglish_rule.txt")).read().strip(),
                                                 _lines[-1]])  # before "Reply with only the cleaned text."

HOSTS = ("api.elevenlabs.io", "api.groq.com", "api.surplusintelligence.ai", "openrouter.ai", "api.cartesia.ai", "api.cerebras.ai")
POOL_SIZE = 2  # open, ready connections kept per provider
FRESH_S = 45   # replace an idle connection before the provider drops it
_pool = {h: [] for h in HOSTS}  # host -> [(connection, opened_at)]
_lock = threading.Lock()


def _open(host: str):
    c = http.client.HTTPSConnection(host, timeout=TIMEOUT_S)
    c.connect()  # TCP + TLS now, not on the request path
    return c, time.monotonic()


def take(host: str) -> http.client.HTTPSConnection:
    with _lock:
        while _pool[host]:
            c, opened = _pool[host].pop()
            if time.monotonic() - opened < FRESH_S:
                return c
            c.close()
    return _open(host)[0]  # pool empty: connect now (a cold request)


def give_back(host: str, c) -> None:
    with _lock:
        if len(_pool[host]) < POOL_SIZE:
            _pool[host].append((c, time.monotonic()))
            return
    c.close()


def call(host: str, method: str, path: str, body: bytes, headers: dict, timeout: float = TIMEOUT_S):
    """Send on a ready connection from the pool; if the provider closed it, open a fresh one and resend once."""
    for attempt in (1, 2):
        c = take(host) if attempt == 1 else _open(host)[0]  # the retry never trusts the pool again
        c.timeout = timeout
        if c.sock:
            c.sock.settimeout(timeout)
        try:
            c.request(method, path, body, headers)
            r = c.getresponse()
            data = r.read()
            give_back(host, c)
            return r.status, data
        except (http.client.HTTPException, ConnectionError, OSError):
            c.close()
            if attempt == 2:
                raise


def to_mp3(audio: bytes) -> bytes:
    """The Surplus key market prices WAV and MP3 only, so Opus is converted for that route (~50 ms here)."""
    return subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-c:a", "libmp3lame", "-b:a", "32k", "-f", "mp3",
                           "pipe:1"], input=audio, capture_output=True, timeout=20).stdout


def transcribe(mp3: bytes, language: str, route: str = "direct", fmt: str = "mp3") -> str:
    if route == "surplus" and fmt != "mp3":
        mp3, fmt = to_mp3(mp3), "mp3"
    filename, ctype = ("dictation.ogg", "audio/ogg") if fmt == "ogg" else ("dictation.mp3", "audio/mpeg")
    b = "utter" + secrets.token_hex(8)
    fields = {"model_id": SCRIBE_MODEL, "tag_audio_events": "false"}
    if language != "multi":
        fields["language_code"] = "en"
    body = b"".join(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode() for k, v in fields.items())
    body += (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
             f"Content-Type: {ctype}\r\n\r\n").encode() + mp3 + f"\r\n--{b}--\r\n".encode()
    if route == "groq":  # Groq Whisper: the same provider and warm connection as cleanup
        g = "utter" + secrets.token_hex(8)
        gfields = {"model": GROQ_STT_MODEL, "response_format": "json", "temperature": "0"}
        if language != "multi":
            gfields["language"] = "en"
        gbody = b"".join(f"--{g}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
                         for k, v in gfields.items())
        gbody += (f"--{g}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                  f"Content-Type: {ctype}\r\n\r\n").encode() + mp3 + f"\r\n--{g}--\r\n".encode()
        status, data = call("api.groq.com", "POST", "/openai/v1/audio/transcriptions", gbody,
                            {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": f"multipart/form-data; boundary={g}"})
    elif route == "cartesia":
        c = "utter" + secrets.token_hex(8)
        # its auto-detect turned Hindi into noise in testing, so the language is always named
        cfields = {"model": CARTESIA_STT_MODEL, "language": "hi" if language == "multi" else "en"}
        cbody = b"".join(f"--{c}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
                         for k, v in cfields.items())
        cbody += (f"--{c}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                  f"Content-Type: {ctype}\r\n\r\n").encode() + mp3 + f"\r\n--{c}--\r\n".encode()
        try:
            status, data = call("api.cartesia.ai", "POST", "/stt", cbody,
                                {"X-API-Key": CARTESIA_KEY, "Cartesia-Version": "2026-03-01",
                                 "Content-Type": f"multipart/form-data; boundary={c}"})
        except Exception as exc:
            status, data = 0, str(exc).encode()
        if status != 200:  # Cartesia is down or refused: the dictation still gets Groq Whisper
            print(time.strftime("%H:%M:%S"), f"cartesia stt failed ({status} {data[:120]!r}); using groq", flush=True)
            return transcribe(mp3, language, "groq", fmt)
    elif route == "openrouter":
        o = "utter" + secrets.token_hex(8)
        ofields = {"model": OPENROUTER_STT_MODEL, **({"language": "en"} if language != "multi" else {})}
        obody = b"".join(f"--{o}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
                         for k, v in ofields.items())
        obody += (f"--{o}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                  f"Content-Type: {ctype}\r\n\r\n").encode() + mp3 + f"\r\n--{o}--\r\n".encode()
        try:
            status, data = call("openrouter.ai", "POST", "/api/v1/audio/transcriptions", obody,
                                {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": f"multipart/form-data; boundary={o}"})
        except Exception as exc:
            status, data = 0, str(exc).encode()
        if status != 200:  # OpenRouter or AssemblyAI is down: the dictation still gets Groq Whisper
            print(time.strftime("%H:%M:%S"), f"openrouter stt failed ({status} {data[:120]!r}); using groq", flush=True)
            return transcribe(mp3, language, "groq", fmt)
    elif route == "surplus":  # the same Scribe v2, through the Surplus key market
        status, data = call("api.surplusintelligence.ai", "POST", "/v1/resources/elevenlabs/v1/speech-to-text", body,
                            {"xi-api-key": SURPLUS_KEY, "Content-Type": f"multipart/form-data; boundary={b}"})
    else:
        status, data = call("api.elevenlabs.io", "POST", "/v1/speech-to-text", body,
                            {"xi-api-key": ELEVENLABS_KEY, "Content-Type": f"multipart/form-data; boundary={b}"})
    if status != 200:
        raise RuntimeError(f"transcription ({route}) HTTP {status}: {data[:200]!r}")
    reply = json.loads(data)
    if route == "openrouter":
        usage = reply.get("usage") or {}
        charge(usage.get("cost") or (usage.get("seconds") or 0) * OPENROUTER_STT_USD_PER_HOUR / 3600, "stt")
    return reply.get("text", "").strip()


def clean(text: str, provider: str = "groq", roman: bool = False) -> str:
    """Cleaned text, or the raw transcript if cleanup stalls or fails: a dictation is never lost to cleanup."""
    try:
        return _clean(text, provider, roman)
    except Exception as exc:
        print(time.strftime("%H:%M:%S"), f"cleanup ({provider}) failed, sending the raw transcript: {str(exc)[:150]}", flush=True)
        return text


URDU_SCRIPT = re.compile(r"[\u0600-\u06ff]")  # AssemblyAI with no language hint sometimes writes spoken Hindi as Urdu


def _clean(text: str, provider: str = "groq", roman: bool = False) -> str:
    prompt = CLEANUP_PROMPT_ROMAN if roman else CLEANUP_PROMPT
    if URDU_SCRIPT.search(text):  # the general prompt rule alone was ignored on short sentences; say it for this one
        prompt += ("\nThis text is Hindi that the transcriber wrote in Urdu script. Write it in "
                   + ("Roman letters" if roman else "Devanagari") + ", same words.")
    msgs = [{"role": "system", "content": prompt}, {"role": "user", "content": text}]
    if provider == "race":
        return _race(msgs, len(text.split()), DIRECT_RACERS)
    if provider == "orrace":
        try:
            return _race(msgs, len(text.split()), openrouter_racers())
        except Exception as exc:
            print(time.strftime("%H:%M:%S"), f"orrace cleanup failed ({str(exc)[:160]}); using cerebras", flush=True)
            return _clean(text, "cerebras", roman)
    if provider == "surplus":
        body = json.dumps({"model": SURPLUS_CLEANUP_MODEL, "temperature": 0, "max_tokens": 2 * len(text.split()) + 512,
                           "messages": msgs}).encode()
        status, data = call("api.surplusintelligence.ai", "POST", "/v1/chat/completions", body,
                            {"Authorization": f"Bearer {SURPLUS_KEY}", "Content-Type": "application/json"})
    elif provider == "cerebras":
        body = json.dumps({"model": CEREBRAS_CLEANUP_MODEL, "temperature": 0, "max_tokens": 2 * len(text.split()) + 512,
                           "reasoning_effort": "none", "messages": msgs}).encode()
        try:
            status, data = call("api.cerebras.ai", "POST", "/v1/chat/completions", body,
                                {"Authorization": f"Bearer {CEREBRAS_KEY}", "Content-Type": "application/json"},
                                CLEANUP_TIMEOUT_S)
        except Exception as exc:
            status, data = 0, str(exc).encode()
        if status != 200:  # Cerebras down, throttling or stalled: clean on Groq instead
            print(time.strftime("%H:%M:%S"), f"cerebras cleanup failed ({status} {data[:120]!r}); using groq", flush=True)
            return _clean(text, "groq", roman)
    elif provider == "openrouter":
        body = json.dumps({"model": OPENROUTER_CLEANUP_MODEL, "temperature": 0, "max_tokens": 2 * len(text.split()) + 512,
                           "messages": msgs, "provider": {"order": [OPENROUTER_CLEANUP_PROVIDER], "allow_fallbacks": False}}).encode()
        try:
            status, data = call("openrouter.ai", "POST", "/api/v1/chat/completions", body,
                                {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"})
        except Exception as exc:
            status, data = 0, str(exc).encode()
        if status != 200:  # OpenRouter or its provider is down or throttling: clean on our own Groq instead
            print(time.strftime("%H:%M:%S"), f"openrouter cleanup failed ({status} {data[:120]!r}); using groq", flush=True)
            return _clean(text, "groq", roman)
    else:
        body = json.dumps({"model": CLEANUP_MODEL, "temperature": 0, "max_tokens": 2 * len(text.split()) + 512,
                           "messages": msgs, **CLEANUP_EXTRA}).encode()
        status, data = call("api.groq.com", "POST", "/openai/v1/chat/completions", body,
                            {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}, CLEANUP_TIMEOUT_S)
    if status != 200:
        raise RuntimeError(f"cleanup ({provider}) HTTP {status}: {data[:200]!r}")
    reply = json.loads(data)
    charge((reply.get("usage") or {}).get("cost"), "other")  # OpenRouter reports it; our own Groq/Cerebras keys do not
    return (reply["choices"][0]["message"]["content"] or "").strip()


_racers = ThreadPoolExecutor(max_workers=8)  # the loser keeps running to its timeout, then hands its socket back


# (name, host, path, key, extra body fields) per side of a race
DIRECT_RACERS = (("cerebras", "api.cerebras.ai", "/v1/chat/completions", CEREBRAS_KEY,
                  {"model": RACE_CEREBRAS_MODEL, "reasoning_effort": "low"}),
                 ("groq", "api.groq.com", "/openai/v1/chat/completions", GROQ_KEY,
                  {"model": RACE_GROQ_MODEL, "reasoning_effort": "low"}))
# The OpenRouter race's two racers come from Supabase's cleanup_models table (slot 1, slot 2), read in the background
# with the keys and kept in memory: choosing them costs a dictation nothing. These are used until it loads, or without it.
DEFAULT_CLEANUP_MODELS = [("openai/gpt-oss-120b", "cerebras"), ("openai/gpt-oss-120b", "groq")]


def openrouter_racers() -> tuple:
    return tuple((provider, "openrouter.ai", "/api/v1/chat/completions", OPENROUTER_KEY,
                  {"model": model, "reasoning": {"effort": "low"},
                   "provider": {"only": [provider], "allow_fallbacks": False}})
                 for model, provider in (accounts.cleanup_models() or DEFAULT_CLEANUP_MODELS))


def _ask(part: str, host: str, path: str, key: str, extra: dict, msgs: list, words: int) -> str:
    body = json.dumps({"temperature": 0, "max_tokens": 2 * words + 512, "messages": msgs, **extra}).encode()
    status, data = call(host, "POST", path, body, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        CLEANUP_TIMEOUT_S)
    if status != 200:
        raise RuntimeError(f"HTTP {status}: {data[:120]!r}")
    reply = json.loads(data)
    charge((reply.get("usage") or {}).get("cost"), part)  # both sides of a race are billed, the loser too
    out = (reply["choices"][0]["message"]["content"] or "").strip()
    if not out:  # reasoning used up max_tokens: let the other provider answer
        raise RuntimeError("empty answer")
    return out.replace("\u2011", "-")  # gpt-oss writes non-breaking hyphens ("pre\u2011Vercel"); type a plain one


def _race(msgs: list, words: int, racers: tuple) -> str:
    jobs = {_racers.submit(contextvars.copy_context().run, _ask, f"cleanup_model{i}", host, path, key, extra, msgs,
                           words): name
            for i, (name, host, path, key, extra) in enumerate(racers, 1)}  # billed by slot, so a model swap keeps columns
    pending, errors = set(jobs), []
    while pending:
        done, pending = wait(pending, return_when=FIRST_COMPLETED)
        for f in done:
            try:
                return f.result()
            except Exception as exc:
                errors.append(f"{jobs[f]} {str(exc)[:100]}")
    raise RuntimeError("both failed: " + "; ".join(errors))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep the app's connection open between dictations

    def reply(self, status: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        if status >= 400:
            self.close_connection = True  # an unread body must not be parsed as the next request
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            ok = READY.is_set() and accounts.ready() and not DRAINING.is_set()
            return self.reply(200 if ok else 503, {"ok": ok})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/dictate":
            return self.reply(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_AUDIO:
            return self.reply(413 if length else 400, {"error": "send the recording as an MP3 body"})
        # Read the body before any refusal: replying while the proxy is still uploading breaks its pipe, and Caddy then
        # hands the client a 502 instead of our 401/429.
        mp3 = self.rfile.read(length)
        user = key_owner(self.headers.get("Authorization", ""))
        if not user:
            return self.reply(401, {"error": "bad token"})
        notice = accounts.over_limit(user)
        if notice:
            return self.reply(429, {"error": "daily limit reached", "notice": notice})
        _bill.set(lambda part, micro: accounts.add_cost(user, part, micro))
        q = parse_qs(url.query)
        language = q.get("language", ["en"])[0]
        stt_route = pick(q, "stt", DEFAULT_STT, ("direct", "surplus", "groq", "openrouter", "cartesia"))
        cleaner = pick(q, "cleanup", DEFAULT_CLEANUP, ("groq", "surplus", "none", "openrouter", "cerebras", "race", "orrace"))       # ?cleanup=groq|surplus
        t0 = time.monotonic()
        try:
            raw = transcribe(mp3, language, stt_route)
            t1 = time.monotonic()
            text = clean(raw, cleaner) if raw else ""
            t2 = time.monotonic()
        except Exception as exc:
            self.log_message("dictate failed: %s", str(exc)[:200])
            self.reply(502, {"error": "transcription failed"})
            return save_dictation(mp3, "mp3", {"user": user, "language": language, "stt": stt_route, "cleanup": cleaner,
                                               "error": str(exc)[:500]})
        accounts.record(user, accounts.audio_seconds(mp3, "mp3"))
        stt_ms, clean_ms = (t1 - t0) * 1000, (t2 - t1) * 1000
        self.log_message("dictate: %d KB audio, stt %.0f ms, cleanup %.0f ms", length // 1024, stt_ms, clean_ms)
        self.reply(200, {"text": text or raw, "raw": raw, "stt_ms": round(stt_ms), "cleanup_ms": round(clean_ms),
                         "total_ms": round(stt_ms + clean_ms)})
        save_dictation(mp3, "mp3", {"user": user, "language": language, "stt": stt_route, "cleanup": cleaner, "raw": raw,
                                    "text": text or raw, "stt_ms": round(stt_ms), "cleanup_ms": round(clean_ms),
                                    "total_ms": round(stt_ms + clean_ms)})

    def log_message(self, fmt, *args):  # the log gets sizes and timings only; text and audio go to DICTATIONS
        print(time.strftime("%H:%M:%S"), fmt % args, flush=True)


READY = threading.Event()  # set once every provider has a warm connection; /health says 503 until then
DRAINING = threading.Event()  # set on SIGTERM: take no new dictations, finish the ones in flight, then exit
DRAIN_S = 30  # longest a dictation in flight may hold up a stop (a long one: upload, STT, cleanup)


def keep_warm() -> None:
    """Keep POOL_SIZE fresh, already-connected sockets per provider, so a request never waits for a handshake."""
    while True:
        for host in HOSTS:
            with _lock:
                now = time.monotonic()
                stale = [c for c, opened in _pool[host] if now - opened >= FRESH_S]
                _pool[host] = [(c, opened) for c, opened in _pool[host] if now - opened < FRESH_S]
                missing = POOL_SIZE - len(_pool[host])
            for c in stale:
                c.close()
            for _ in range(max(0, missing)):
                try:
                    pair = _open(host)
                    with _lock:
                        _pool[host].append(pair)
                except OSError:
                    pass
        READY.set()  # one full pass done (a provider that is down does not hold up readiness)
        time.sleep(5)


def pipeline(audio: bytes, fmt: str, language: str, stt_route: str, cleaner: str) -> dict:
    t0 = time.monotonic()
    raw = transcribe(audio, language, stt_route, fmt)
    t1 = time.monotonic()
    text = clean(raw, cleaner) if raw and cleaner != "none" else raw
    t2 = time.monotonic()
    return {"text": text or raw, "raw": raw, "stt_ms": round((t1 - t0) * 1000), "cleanup_ms": round((t2 - t1) * 1000),
            "total_ms": round((t2 - t0) * 1000)}


def same_words(a: str, b: str) -> bool:
    norm = lambda t: "".join(c.lower() if c.isalnum() else " " for c in t).split()
    return norm(a) == norm(b)


async def finish_stream(ws, audio: bytes, language: str, stt_route: str, cleaner: str, roman: bool = False) -> dict:
    """Scribe on the audio; meanwhile take the app's live transcript (if it sends one) and clean that early."""
    t0 = time.monotonic()
    stt = asyncio.ensure_future(asyncio.to_thread(transcribe, audio, language, stt_route, "ogg"))
    early, live_text = None, ""
    while not stt.done():
        recv = asyncio.ensure_future(ws.recv())
        done, _ = await asyncio.wait({stt, recv}, return_when=asyncio.FIRST_COMPLETED)
        if recv in done:
            try:
                msg = json.loads(recv.result())
                live_text = (msg.get("live") or "").strip()
                if live_text and early is None and cleaner != "none":
                    early = asyncio.ensure_future(asyncio.to_thread(clean, live_text, cleaner, roman))
            except (ValueError, AttributeError):
                pass
        else:
            recv.cancel()
    raw = stt.result()
    t1 = time.monotonic()
    head_start = bool(early and raw and same_words(raw, live_text))
    if cleaner == "none":  # transcription only: the transcript is the answer
        text, head_start = raw, False
    elif head_start:
        text = await early
    else:
        text = await asyncio.to_thread(clean, raw, cleaner, roman) if raw else ""
    t2 = time.monotonic()
    return {"text": text or raw, "raw": raw, "stt_ms": round((t1 - t0) * 1000), "cleanup_ms": round((t2 - t1) * 1000),
            "total_ms": round((t2 - t0) * 1000), "head_start": head_start}


async def stream_handler(ws, path=None):
    """One dictation per connection: Opus arrives while the user talks; "end" runs the pipeline and replies."""
    import urllib.parse
    # websockets 10 (Ubuntu 24.04) keeps these on the connection; 11+ on ws.request
    req_path = path or getattr(ws, "path", None) or ws.request.path
    headers = ws.request_headers if hasattr(ws, "request_headers") else ws.request.headers
    user = key_owner(headers.get("Authorization", ""))
    if not user:
        # who knocked with a bad or missing key; the key itself is never logged
        print(time.strftime("%H:%M:%S"), "rejected:", "no key" if not headers.get("Authorization") else "unknown or revoked key",
              "from", headers.get("Cf-Connecting-Ip") or headers.get("X-Forwarded-For") or "direct", flush=True)
        await ws.close(code=4401, reason="bad token")
        return
    q = urllib.parse.parse_qs(urllib.parse.urlparse(req_path).query)
    language = q.get("language", ["en"])[0]
    roman = q.get("script", [""])[0] == "roman"
    stt_route = pick(q, "stt", DEFAULT_STT, ("direct", "surplus", "groq", "openrouter", "cartesia"))
    cleaner = pick(q, "cleanup", DEFAULT_CLEANUP, ("groq", "surplus", "none", "openrouter", "cerebras", "race", "orrace"))
    audio = bytearray()
    try:
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                _busy.add(ws)  # a dictation has started on this connection: a drain waits for it
                audio.extend(msg)
                if len(audio) > MAX_AUDIO:
                    await ws.close(code=4413, reason="too much audio")
                    return
            elif msg == "end":
                if not audio:
                    await ws.send(json.dumps({"text": "", "raw": "", "error": "no audio"}))
                    return
                notice = accounts.over_limit(user)
                if notice:  # checked before any provider is paid; the app shows the notice
                    await ws.send(json.dumps({"text": "", "raw": "", "error": "daily limit reached", "notice": notice}))
                    return
                _bill.set(lambda part, micro: accounts.add_cost(user, part, micro))  # the threads below inherit it
                meta = {"user": user, "language": language, "script": "roman" if roman else "devanagari", "stt": stt_route,
                        "cleanup": cleaner}
                try:
                    result = await finish_stream(ws, bytes(audio), language, stt_route, cleaner, roman)
                except Exception as exc:
                    print(time.strftime("%H:%M:%S"), "stream failed:", str(exc)[:200], flush=True)
                    await ws.send(json.dumps({"error": "transcription failed"}))  # details stay in the log
                    await asyncio.to_thread(save_dictation, bytes(audio), "ogg", {**meta, "error": str(exc)[:500]})
                    return
                accounts.record(user, accounts.audio_seconds(bytes(audio), "ogg"))
                print(time.strftime("%H:%M:%S"), f"stream: {len(audio) // 1024} KB opus, stt {result['stt_ms']} ms, "
                      f"cleanup {result['cleanup_ms']} ms{' (head start used)' if result['head_start'] else ''}",
                      flush=True)
                await ws.send(json.dumps(result))
                await asyncio.to_thread(save_dictation, bytes(audio), "ogg", {
                    **meta, "raw": result["raw"], "text": result["text"], "stt_ms": result["stt_ms"],
                    "cleanup_ms": result["cleanup_ms"], "total_ms": result["total_ms"],
                    "head_start": result["head_start"]})
                return
    except Exception:
        return  # the client went away mid-dictation
    finally:
        _busy.discard(ws)


_busy = set()  # connections with a dictation in flight


async def ws_health(path, headers):
    """GET /health on the WebSocket port, for the proxy's health check: 200 only when warm and not draining."""
    if path.split("?")[0] == "/health":
        ok = READY.is_set() and accounts.ready() and not DRAINING.is_set()
        return HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE, [("Content-Type", "text/plain")], b"ok\n" if ok else b"not ready\n"
    return None


async def serve_ws(http_server):
    import signal, websockets
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    ws_server = await websockets.serve(stream_handler, "127.0.0.1", WS_PORT, max_size=MAX_AUDIO, ping_interval=20,
                                       process_request=ws_health)
    print(f"utter-server websocket on 127.0.0.1:{WS_PORT}", flush=True)
    await stop.wait()
    # Drain, the way a load balancer deregisters a target: the proxy already sends new connections to the new
    # instance once it is healthy. Stop listening, close idle connections (the app reopens them, through the proxy,
    # to the new instance), let dictations in flight finish, then exit.
    DRAINING.set()
    ws_server.server.close()
    threading.Thread(target=http_server.shutdown, daemon=True).start()
    idle = [c for c in ws_server.websockets if c not in _busy]
    await asyncio.gather(*(c.close(1012, "server restarting") for c in idle), return_exceptions=True)
    t = time.monotonic()
    while _busy and time.monotonic() - t < DRAIN_S:
        await asyncio.sleep(0.1)
    print(time.strftime("%H:%M:%S"), f"drained: closed {len(idle)} idle, waited {time.monotonic() - t:.1f} s for "
          f"in-flight dictations, {len(_busy)} still open", flush=True)


if __name__ == "__main__":
    threading.Thread(target=keep_warm, daemon=True).start()
    accounts.start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"utter-server on 127.0.0.1:{PORT} (stt {DEFAULT_STT}, cleanup {DEFAULT_CLEANUP})", flush=True)
    asyncio.run(serve_ws(server))
