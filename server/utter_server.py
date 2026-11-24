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
import re, asyncio, contextvars, hashlib, http.client, json, os, secrets, subprocess, threading, time, unicodedata
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
_dictation = contextvars.ContextVar("dictation", default=None)  # (user, dictation id) for the call log


def charge(usd, part: str) -> int:
    f = _bill.get()
    micro = round(float(usd or 0) * 1_000_000)
    if f and micro:
        f(part, micro)
    return micro


def start_dictation(user: str) -> str:
    """Bill this dictation's provider calls to the user and tag their call-log rows; the id ties them together."""
    did = secrets.token_hex(8)
    _bill.set(lambda part, micro: accounts.add_cost(user, part, micro))
    _dictation.set((user, did))
    return did


def log_call(part: str, model: str, provider: str, t0: float, status: int, reply=None, error: str = "",
             cost_micro: int = 0, won=None) -> None:
    """One provider call for later analysis (provider_calls): timing, outcome, cost, tokens. Never any text."""
    d = _dictation.get()
    if not d:
        return
    usage = (reply or {}).get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    accounts.log("provider_calls", d[0], {
        "at": accounts.now_iso(), "dictation_id": d[1], "part": part, "model": model, "provider": provider,
        "upstream": (reply or {}).get("provider"), "ms": round((time.monotonic() - t0) * 1000),
        "ok": status == 200 and not error,
        "http_status": status or None, "error": error[:300] or None, "won": won, "cost_micro": cost_micro,
        "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"), "audio_seconds": usage.get("seconds")})


def log_dictation(did: str, user: str, audio: bytes, fmt: str, language: str, script: str, stt_route: str,
                  cleaner: str, result=None, error: str = "") -> None:
    """One dictation's summary for later analysis (dictations): routes, sizes, timings, outcome. Never any text."""
    r = result or {}
    accounts.log("dictations", user, {
        "id": did, "at": accounts.now_iso(), "language": language, "script": script, "stt_route": stt_route,
        "cleanup_route": cleaner, "audio_seconds": round(accounts.audio_seconds(audio, fmt), 2),
        "upload_kb": len(audio) // 1024, "stt_ms": r.get("stt_ms"), "cleanup_ms": r.get("cleanup_ms"),
        "total_ms": r.get("total_ms"), "head_start": r.get("head_start"), "stt_confidence": r.get("confidence"),
        "raw_chars": len(r["raw"]) if "raw" in r else None, "text_chars": len(r["text"]) if "text" in r else None,
        "ok": not error, "error": error[:300] or None})
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


STT_MODELS = {"openrouter": OPENROUTER_STT_MODEL, "cartesia": CARTESIA_STT_MODEL}  # others are set further down


class Transcript(str):
    """A transcript that also says how sure the transcriber was: confidence 0..1, or None when the route gives none."""
    confidence = None


UNCLEAR_BELOW = float(os.environ.get("UTTER_UNCLEAR_BELOW", "0.70"))
UNCLEAR_NOTICE = "Didn't get that clearly. Please speak again."


def unclear(raw) -> bool:
    """Too unsure to paste: low confidence on 3 words or more. One- and two-word dictations always paste: the
    transcriber scores very short clips low even when they are right ("Hey man." 0.50, on 60 real dictations, while
    the one rubbish sentence scored 0.61 and every real sentence 0.77 or more, 2026-11-24)."""
    if foreign_script(str(raw)):
        return True
    conf = getattr(raw, "confidence", None)
    return conf is not None and conf < UNCLEAR_BELOW and len(str(raw).split()) >= 3


def foreign_script(text: str) -> bool:
    """Letters in a script Utter never asks for (only English and Hindi): Whisper guessing the language of a near-silent
    clip writes a made-up word in Chinese or Korean ("有。", "比如。" on 2026-11-24). Always wrong, however short."""
    return any(c.isalpha() and not unicodedata.name(c, "").startswith(("LATIN", "DEVANAGARI")) for c in text)


def transcribe(mp3: bytes, language: str, route: str = "direct", fmt: str = "mp3", stt_model: str = "",
               stt_provider: str = "") -> str:
    """stt_model picks the OpenRouter model (a plan's choice); empty means OPENROUTER_STT_MODEL. stt_provider is the
    OpenRouter host to try first (others remain as fallbacks); empty lets OpenRouter choose."""
    t0 = time.monotonic()
    or_model = stt_model or OPENROUTER_STT_MODEL
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
            log_call("stt", CARTESIA_STT_MODEL, "cartesia", t0, status, error=data[:300].decode(errors="replace"))
            return transcribe(mp3, language, "groq", fmt)
    elif route == "openrouter":
        o = "utter" + secrets.token_hex(8)
        ofields = {"model": or_model, "response_format": "verbose_json",  # verbose: it carries the confidence
                   **({"language": "en"} if language != "multi" else {}),
                   **({"provider": json.dumps({"order": [stt_provider]})} if stt_provider else {})}
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
            log_call("stt", or_model, "openrouter", t0, status, error=data[:300].decode(errors="replace"))
            return transcribe(mp3, language, "groq", fmt)
    elif route == "surplus":  # the same Scribe v2, through the Surplus key market
        status, data = call("api.surplusintelligence.ai", "POST", "/v1/resources/elevenlabs/v1/speech-to-text", body,
                            {"xi-api-key": SURPLUS_KEY, "Content-Type": f"multipart/form-data; boundary={b}"})
    else:
        status, data = call("api.elevenlabs.io", "POST", "/v1/speech-to-text", body,
                            {"xi-api-key": ELEVENLABS_KEY, "Content-Type": f"multipart/form-data; boundary={b}"})
    model = or_model if route == "openrouter" else STT_MODELS.get(route) or (GROQ_STT_MODEL if route == "groq" else SCRIBE_MODEL)
    if status != 200:
        log_call("stt", model, route, t0, status, error=data[:300].decode(errors="replace"))
        raise RuntimeError(f"transcription ({route}) HTTP {status}: {data[:200]!r}")
    reply = json.loads(data)
    cost = 0
    if route == "openrouter":
        usage = reply.get("usage") or {}
        usd = usage.get("cost")
        if not usd and or_model.startswith("assemblyai/"):  # our BYOK key: AssemblyAI bills us, OpenRouter says 0
            usd = (usage.get("seconds") or 0) * OPENROUTER_STT_USD_PER_HOUR / 3600
        cost = charge(usd, "stt")
    log_call("stt", model, route, t0, status, reply, cost_micro=cost)
    text = Transcript(reply.get("text", "").strip())
    if route == "openrouter":
        if reply.get("confidence") is not None:  # AssemblyAI: one score for the whole clip
            text.confidence = float(reply["confidence"])
        elif reply.get("segments"):  # Whisper: its own signs of an invented transcript count as no confidence
            segs = reply["segments"]
            invented = any((s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -1.0)
                           or s.get("compression_ratio", 0) > 2.4 for s in segs)
            text.confidence = 0.0 if invented else None
    return text


def routes_for(user: str, q: dict) -> tuple:
    """(stt route, stt model, cleanup route, stt provider). A user on a plan gets the plan's routes and nothing the
    request asks for, so a free key can never pick a pricier route; the owner token and keys.json users may still choose
    with ?stt= and ?cleanup=."""
    plan = accounts.route_for(user)
    if plan:
        return "openrouter", plan[0], plan[1], plan[2]
    return (pick(q, "stt", DEFAULT_STT, ("direct", "surplus", "groq", "openrouter", "cartesia")), "",
            pick(q, "cleanup", DEFAULT_CLEANUP, ("groq", "surplus", "none", "openrouter", "cerebras", "race", "orrace", "orslot1", "orslot2")), "")


def clean(text: str, provider: str = "groq", roman: bool = False) -> str:
    """Cleaned text, or the raw transcript if cleanup stalls or fails: a dictation is never lost to cleanup."""
    if len(text.split()) < 3:  # "Yes.", "Thank you." — nothing to clean, so no cleanup call (its cost and ~0.25 s)
        return text
    try:
        return _clean(text, provider, roman)
    except Exception as exc:
        print(time.strftime("%H:%M:%S"), f"cleanup ({provider}) failed, sending the raw transcript: {str(exc)[:150]}", flush=True)
        return text


URDU_SCRIPT = re.compile(r"[\u0600-\u06ff]")  # AssemblyAI with no language hint sometimes writes spoken Hindi as Urdu


def _clean(text: str, provider: str = "groq", roman: bool = False) -> str:
    t0 = time.monotonic()
    prompt = CLEANUP_PROMPT_ROMAN if roman else CLEANUP_PROMPT
    if URDU_SCRIPT.search(text):  # the general prompt rule alone was ignored on short sentences; say it for this one
        prompt += ("\nThis text is Hindi that the transcriber wrote in Urdu script. Write it in "
                   + ("Roman letters" if roman else "Devanagari") + ", same words.")
    msgs = [{"role": "system", "content": prompt}, {"role": "user", "content": text}]
    if provider == "race":
        return _race(msgs, len(text.split()), DIRECT_RACERS)
    if provider in ("orrace", "orslot1", "orslot2"):  # both cleanup_models racing, or one of them alone (the cost cap)
        racers, first = openrouter_racers(), 1
        if provider != "orrace":
            first = int(provider[-1])
            racers = racers[first - 1:first]
        try:
            return _race(msgs, len(text.split()), racers, first)
        except Exception as exc:
            print(time.strftime("%H:%M:%S"), f"{provider} cleanup failed ({str(exc)[:160]}); using cerebras", flush=True)
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
            log_call("other", CEREBRAS_CLEANUP_MODEL, "cerebras", t0, status, error=data[:300].decode(errors="replace"))
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
            log_call("other", OPENROUTER_CLEANUP_MODEL, "openrouter", t0, status,
                     error=data[:300].decode(errors="replace"))
            return _clean(text, "groq", roman)
    else:
        body = json.dumps({"model": CLEANUP_MODEL, "temperature": 0, "max_tokens": 2 * len(text.split()) + 512,
                           "messages": msgs, **CLEANUP_EXTRA}).encode()
        status, data = call("api.groq.com", "POST", "/openai/v1/chat/completions", body,
                            {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}, CLEANUP_TIMEOUT_S)
    model = {"surplus": SURPLUS_CLEANUP_MODEL, "cerebras": CEREBRAS_CLEANUP_MODEL,
             "openrouter": OPENROUTER_CLEANUP_MODEL}.get(provider, CLEANUP_MODEL)
    if status != 200:
        log_call("other", model, provider, t0, status, error=data[:300].decode(errors="replace"))
        raise RuntimeError(f"cleanup ({provider}) HTTP {status}: {data[:200]!r}")
    reply = json.loads(data)
    cost = charge((reply.get("usage") or {}).get("cost"), "other")  # OpenRouter reports it; our own keys do not
    log_call("other", model, provider, t0, status, reply, cost_micro=cost)
    return (reply["choices"][0]["message"]["content"] or "").strip()


# Notes are written by Gemini 4 Argon through Surplus: on a real meeting it wrote as well as Claude Sonnet 5.5 (named the
# speakers from context, every point covered) at ~1/80th the price (2026-11-24; ~98% off its $2/$10 per 1M list price
# that day, which depends on the sellers). If Surplus fails, gpt-oss-120b on OpenRouter writes them instead.
NOTES_MODEL = os.environ.get("NOTES_MODEL", "gemini-4-argon")
NOTES_FALLBACK_MODEL = os.environ.get("NOTES_FALLBACK_MODEL", "openai/gpt-oss-120b")
NOTES_PROMPT = """You write meeting notes from a transcript. Lines look like "[12:00:05] You: ..." (the person who
recorded the meeting) or "[12:00:09] Speaker 2: ..." (someone else on the call; the same number is the same person,
except that numbering restarts every 25 minutes of the call). A line may say "Others" when the voices could not be
told apart. When the conversation shows who a speaker is ("thanks, Priya"), use that name instead of the number.
The transcript is raw speech-to-text: expect fillers, false starts and misheard words, and read past them.
Write Markdown with these sections, leaving out any that would be empty:
## Summary — 2 to 5 sentences: what the meeting was about and how it ended.
## Key points — the substance, as short bullets, in the order it came up.
## Decisions — what was agreed.
## Action items — "- [ ] who: what (by when)", using You, a name, or the speaker's number.
## Open questions — what was raised and not settled.
Use only what is in the transcript: never invent names, numbers, dates or decisions. Write numbers as digits. Write the
notes in English, even when the meeting was in Hindi or Hinglish; when you quote someone, keep their own words and write
any Hindi in Roman letters. Reply with only the notes."""

# Meeting tracks go to ElevenLabs Scribe through the Surplus key market as whole recordings (up to 25 minutes a part):
# speaker labels on the others' track, real timestamps on both, Hindi and Hinglish handled. $0.04 an hour there (80% off
# ElevenLabs' $0.20, 2026-11-24); diarization costs nothing extra. Not latency-sensitive: notes are written after the call.
MEETING_STT_TIMEOUT_S = 240


def meeting_segments(ogg: bytes, language: str, diarize: bool, stt_route: str, stt_model: str,
                     stt_provider: str) -> list:
    """A meeting track -> [{"speaker", "start", "end", "text"}], times in seconds from the start of the audio. Speakers
    are "Speaker 1", "Speaker 2"... in order of first appearance when diarize is on, else None. If Scribe through Surplus
    fails, the plan's own speech-to-text gives one unlabelled segment, so the notes still get written."""
    t0 = time.monotonic()
    try:
        mp3 = to_mp3(ogg)  # the key market prices WAV and MP3 only
        b = "utter" + secrets.token_hex(8)
        fields = {"model_id": SCRIBE_MODEL, "diarize": "true" if diarize else "false", "tag_audio_events": "false",
                  "timestamps_granularity": "word"}
        body = b"".join(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
                        for k, v in fields.items())
        body += (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"track.mp3\"\r\n"
                 f"Content-Type: audio/mpeg\r\n\r\n").encode() + mp3 + f"\r\n--{b}--\r\n".encode()
        c = http.client.HTTPSConnection("api.surplusintelligence.ai", timeout=MEETING_STT_TIMEOUT_S)  # its own: we need the headers
        c.request("POST", "/v1/resources/elevenlabs/v1/speech-to-text", body,
                  {"xi-api-key": SURPLUS_KEY, "Content-Type": f"multipart/form-data; boundary={b}"})
        r = c.getresponse()
        data, cost_micro = r.read(), int(r.getheader("x-si-buyer-cost-micro") or 0)
        c.close()
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}: {data[:150]!r}")
        reply = json.loads(data)
        charge(cost_micro / 1_000_000, "other")  # exactly what Surplus billed
        log_call("stt", SCRIBE_MODEL + ("+diarize" if diarize else ""), "surplus", t0, 200, cost_micro=cost_micro)
        names, segs = {}, []
        for w in reply.get("words") or []:
            if w.get("type") != "word" or not (w.get("text") or "").strip():
                continue
            who = names.setdefault(w.get("speaker_id"), f"Speaker {len(names) + 1}") if diarize else None
            last = segs[-1] if segs else None
            if last and last["speaker"] == who and w["start"] - last["end"] < 1.5 and last["end"] - last["start"] < 30:
                last["text"] += " " + w["text"].strip()
                last["end"] = w["end"]
            else:
                segs.append({"speaker": who, "start": w["start"], "end": w["end"], "text": w["text"].strip()})
        return segs
    except Exception as exc:
        print(time.strftime("%H:%M:%S"), f"meeting stt via surplus failed ({str(exc)[:150]}); using {stt_route}", flush=True)
        log_call("stt", SCRIBE_MODEL, "surplus", t0, 0, error=str(exc))
        text = transcribe(ogg, language, stt_route, "ogg", stt_model, stt_provider)
        return [{"speaker": None, "start": 0.0, "end": accounts.audio_seconds(ogg, "ogg"), "text": text}] if text else []


def meeting_notes(transcript: str, language: str) -> tuple:
    """A meeting's transcript -> (Markdown notes, the model that wrote them): NOTES_MODEL through Surplus, else
    NOTES_FALLBACK_MODEL on OpenRouter."""
    if not transcript.strip():
        return "_Nothing was said that could be transcribed._", None
    msgs = [{"role": "system", "content": NOTES_PROMPT}, {"role": "user", "content": transcript}]
    t0 = time.monotonic()
    try:
        body = json.dumps({"model": NOTES_MODEL, "temperature": 0.2, "max_tokens": 6000, "messages": msgs}).encode()
        status, data = call("api.surplusintelligence.ai", "POST", "/v1/chat/completions", body,
                            {"Authorization": f"Bearer {SURPLUS_KEY}", "Content-Type": "application/json"}, 180)
        if status != 200:
            raise RuntimeError(f"HTTP {status}: {data[:150]!r}")
        reply = json.loads(data)
        notes = (reply["choices"][0]["message"]["content"] or "").strip()
        if not notes:
            raise RuntimeError("empty notes")
        cost = (reply.get("usage") or {}).get("buyer_cost_micro") or 0  # exactly what Surplus billed
        charge(cost / 1_000_000, "other")
        log_call("meeting_notes", NOTES_MODEL, "surplus", t0, status, reply, cost_micro=cost)
        return notes, NOTES_MODEL
    except Exception as exc:
        print(time.strftime("%H:%M:%S"), f"notes via surplus failed ({str(exc)[:150]}); using {NOTES_FALLBACK_MODEL}",
              flush=True)
        log_call("meeting_notes", NOTES_MODEL, "surplus", t0, 0, error=str(exc))
    t0 = time.monotonic()
    body = json.dumps({"model": NOTES_FALLBACK_MODEL, "temperature": 0.2, "max_tokens": 6000,
                       "reasoning": {"effort": "medium"}, "provider": {"order": ["cerebras"]}, "messages": msgs}).encode()
    status, data = call("openrouter.ai", "POST", "/api/v1/chat/completions", body,
                        {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"}, 180)
    if status != 200:
        log_call("meeting_notes", NOTES_FALLBACK_MODEL, "openrouter", t0, status, error=data[:300].decode(errors="replace"))
        raise RuntimeError(f"notes HTTP {status}: {data[:200]!r}")
    reply = json.loads(data)
    cost = charge((reply.get("usage") or {}).get("cost"), "other")
    log_call("meeting_notes", NOTES_FALLBACK_MODEL, "openrouter", t0, status, reply, cost_micro=cost)
    return (reply["choices"][0]["message"]["content"] or "").strip(), NOTES_FALLBACK_MODEL


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


def _ask(part: str, name: str, race: dict, host: str, path: str, key: str, extra: dict, msgs: list, words: int) -> str:
    model, provider = extra.get("model", ""), (extra.get("provider") or {}).get("only", [name])[0]
    body = json.dumps({"temperature": 0, "max_tokens": 2 * words + 512, "messages": msgs, **extra}).encode()
    t0 = time.monotonic()
    try:
        status, data = call(host, "POST", path, body,
                            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, CLEANUP_TIMEOUT_S)
    except Exception as exc:  # timed out or the connection failed
        log_call(part, model, provider, t0, 0, error=str(exc), won=False)
        raise
    if status != 200:
        log_call(part, model, provider, t0, status, error=data[:300].decode(errors="replace"), won=False)
        raise RuntimeError(f"HTTP {status}: {data[:120]!r}")
    reply = json.loads(data)
    cost = charge((reply.get("usage") or {}).get("cost"), part)  # both sides of a race are billed, the loser too
    out = (reply["choices"][0]["message"]["content"] or "").strip()
    if not out:  # reasoning used up max_tokens: let the other provider answer
        log_call(part, model, provider, t0, status, reply, error="empty answer", cost_micro=cost, won=False)
        raise RuntimeError("empty answer")
    won = race.setdefault("winner", part) == part  # the first good answer is the one the race returns
    log_call(part, model, provider, t0, status, reply, cost_micro=cost, won=won)
    return out.replace("\u2011", "-")  # gpt-oss writes non-breaking hyphens ("pre\u2011Vercel"); type a plain one


def _race(msgs: list, words: int, racers: tuple, first_slot: int = 1) -> str:
    """The first good answer of the racers; first_slot numbers them for billing (slot 2 alone stays cleanup_model2)."""
    race = {}  # the first racer to answer well claims "winner" here, for the call log
    jobs = {_racers.submit(contextvars.copy_context().run, _ask, f"cleanup_model{i}", name, race, host, path, key, extra,
                           msgs, words): name
            for i, (name, host, path, key, extra) in enumerate(racers, first_slot)}  # billed by slot: a model swap keeps columns
    pending, errors = set(jobs), []
    while pending:
        done, pending = wait(pending, return_when=FIRST_COMPLETED)
        for f in done:
            try:
                return f.result()
            except Exception as exc:
                errors.append(f"{jobs[f]} {str(exc)[:100]}")
    raise RuntimeError("both failed: " + "; ".join(errors))


# ---- the interpreter: one spoken sentence -> what was said, in which language, translated, and spoken ----------------
INTERPRET_TTS_MODEL = os.environ.get("INTERPRET_TTS_MODEL", "eleven_flash_v2_5")  # ~75 ms, 32 languages
INTERPRET_VOICES = {"them": os.environ.get("INTERPRET_VOICE_THEM", "EXAVITQu4vr4xnSDxMaL"),  # what they say, to you
                    "me": os.environ.get("INTERPRET_VOICE_ME", "JBFqnCBsd6RMkjVDRZzb")}  # what you say, to them
INTERPRET_RATE = 24000  # the voice comes back as raw 16-bit mono PCM at this rate


def hear(ogg: bytes) -> tuple:
    """A sentence -> (its text, its language as Whisper names it: "chinese", "english", ...). Whisper's own signs of
    nothing said (silence, a cough) give ("", "")."""
    t0 = time.monotonic()
    g = "utter" + secrets.token_hex(8)
    fields = {"model": GROQ_STT_MODEL, "response_format": "verbose_json", "temperature": "0"}
    body = b"".join(f"--{g}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
                    for k, v in fields.items())
    body += (f"--{g}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"s.ogg\"\r\n"
             f"Content-Type: audio/ogg\r\n\r\n").encode() + ogg + f"\r\n--{g}--\r\n".encode()
    status, data = call("api.groq.com", "POST", "/openai/v1/audio/transcriptions", body,
                        {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": f"multipart/form-data; boundary={g}"})
    if status != 200:
        log_call("interpret_stt", GROQ_STT_MODEL, "groq", t0, status, error=data[:300].decode(errors="replace"))
        raise RuntimeError(f"stt HTTP {status}")
    reply = json.loads(data)
    log_call("interpret_stt", GROQ_STT_MODEL, "groq", t0, status, reply)
    segs = reply.get("segments") or []
    if not segs or all(x.get("no_speech_prob", 0) > 0.6 or x.get("compression_ratio", 0) > 2.4 for x in segs):
        return "", ""
    return reply.get("text", "").strip(), (reply.get("language") or "").lower()


def translate(text: str, to: str) -> str:
    msgs = [{"role": "system", "content": f"You are a live interpreter. Translate the user's message into {to}. Reply "
                                          f"with the translation only: no notes, no quotes. Keep names as they are. If "
                                          f"it is already in {to}, reply with it unchanged."},
            {"role": "user", "content": text}]
    return _race(msgs, len(text.split()) * 3, DIRECT_RACERS)


def speak(text: str, lane: str) -> bytes:
    """ElevenLabs Flash through the Surplus key market -> raw PCM at INTERPRET_RATE."""
    t0 = time.monotonic()
    c = http.client.HTTPSConnection("api.surplusintelligence.ai", timeout=TIMEOUT_S)  # its own: we need the headers
    try:
        # the /stream route: its first audio comes back in ~0.55 s every time, the plain one swung 0.45-1.7 s (2026-11-24)
        c.request("POST", f"/v1/resources/elevenlabs/v1/text-to-speech/{INTERPRET_VOICES[lane]}/stream"
                          f"?output_format=pcm_{INTERPRET_RATE}",
                  json.dumps({"text": text, "model_id": INTERPRET_TTS_MODEL}).encode(),
                  {"xi-api-key": SURPLUS_KEY, "Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        cost = int(r.getheader("x-si-buyer-cost-micro") or 0)
    finally:
        c.close()
    if r.status != 200:
        log_call("interpret_tts", INTERPRET_TTS_MODEL, "surplus", t0, r.status, error=data[:300].decode(errors="replace"))
        raise RuntimeError(f"tts HTTP {r.status}")
    charge(cost / 1_000_000, "other")
    log_call("interpret_tts", INTERPRET_TTS_MODEL, "surplus", t0, r.status, cost_micro=cost)
    return data


# what Whisper writes for a breath, a cough or background music: never worth saying out loud (2026-11-24, Discord test)
FILLERS = {"you", "uh", "um", "uh-huh", "mm", "mhm", "hmm", "ah", "oh", "thank you", "thanks for watching", "bye"}


def peek(ogg: bytes, to: str) -> dict:
    """The sentence so far, while it is still being said: its words and their translation, nothing spoken."""
    heard, language = hear(ogg)
    if not heard or re.sub(r"[^\w\s'-]", "", heard).strip().lower() in FILLERS:
        return {"heard": "", "text": ""}
    if language == to.lower():
        return {"heard": heard, "text": heard, "same": True}
    return {"heard": heard, "text": translate(heard, to)}


def interpret(ogg: bytes, to: str, lane: str) -> tuple:
    """-> (reply for the app, the translated voice as PCM or b""). Already in the language wanted ("same"): nothing is
    translated or spoken; the app shows it and, for your own voice, sends your words as you said them."""
    t = time.monotonic()
    heard, language = hear(ogg)
    if not heard or re.sub(r"[^\w\s'-]", "", heard).strip().lower() in FILLERS:
        return {"heard": "", "language": "", "text": ""}, b""
    if language == to.lower():
        return {"heard": heard, "language": language, "text": heard, "same": True}, b""
    t_stt = time.monotonic()
    text = translate(heard, to)
    t_tr = time.monotonic()
    pcm = speak(text, lane) if text else b""
    print(time.strftime("%H:%M:%S"), f"interpret {lane}: {language} -> {to}: stt {(t_stt - t) * 1000:.0f} ms, "
          f"translate {(t_tr - t_stt) * 1000:.0f} ms, voice {(time.monotonic() - t_tr) * 1000:.0f} ms", flush=True)
    return {"heard": heard, "language": language, "text": text, "rate": INTERPRET_RATE}, pcm


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
        notice = accounts.blocked(user)
        if notice:
            return self.reply(403, {"error": "account disabled", "notice": notice})
        notice = accounts.over_limit(user)
        if notice:
            return self.reply(429, {"error": "daily limit reached", "notice": notice})
        did = start_dictation(user)
        q = parse_qs(url.query)
        language = q.get("language", ["en"])[0]
        stt_route, stt_model, cleaner, stt_provider = routes_for(user, q)
        t0 = time.monotonic()
        try:
            raw = transcribe(mp3, language, stt_route, "mp3", stt_model, stt_provider)
            t1 = time.monotonic()
            text = "" if unclear(raw) else clean(raw, cleaner) if raw else ""
            t2 = time.monotonic()
        except Exception as exc:
            self.log_message("dictate failed: %s", str(exc)[:200])
            self.reply(502, {"error": "transcription failed"})
            log_dictation(did, user, mp3, "mp3", language, "", stt_route, cleaner, error=str(exc))
            return save_dictation(mp3, "mp3", {"user": user, "language": language, "stt": stt_route, "cleanup": cleaner,
                                               "error": str(exc)[:500]})
        accounts.record(user, accounts.audio_seconds(mp3, "mp3"))
        stt_ms, clean_ms = (t1 - t0) * 1000, (t2 - t1) * 1000
        self.log_message("dictate: %d KB audio, stt %.0f ms, cleanup %.0f ms", length // 1024, stt_ms, clean_ms)
        result = {"text": text or raw, "raw": raw, "stt_ms": round(stt_ms), "cleanup_ms": round(clean_ms),
                  "total_ms": round(stt_ms + clean_ms), "confidence": getattr(raw, "confidence", None)}
        if unclear(raw):  # too unsure to paste: ask again
            result.update(text="", error="unclear", notice=UNCLEAR_NOTICE)
        self.reply(200, result)
        log_dictation(did, user, mp3, "mp3", language, "", stt_route, cleaner, result, error=result.get("error", ""))
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


async def finish_stream(ws, audio: bytes, language: str, stt_route: str, cleaner: str, roman: bool = False,
                        stt_model: str = "", stt_provider: str = "") -> dict:
    """Scribe on the audio; meanwhile take the app's live transcript (if it sends one) and clean that early."""
    t0 = time.monotonic()
    stt = asyncio.ensure_future(asyncio.to_thread(transcribe, audio, language, stt_route, "ogg", stt_model, stt_provider))
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
    conf = getattr(raw, "confidence", None)
    if unclear(raw):  # nothing pasted, no cleanup paid for: the user is asked to say it again
        return {"text": "", "raw": raw, "error": "unclear", "notice": UNCLEAR_NOTICE, "confidence": conf,
                "stt_ms": round((t1 - t0) * 1000), "cleanup_ms": 0, "total_ms": round((t1 - t0) * 1000),
                "head_start": False}
    head_start = bool(early and raw and same_words(raw, live_text))
    if cleaner == "none":  # transcription only: the transcript is the answer
        text, head_start = raw, False
    elif head_start:
        text = await early
    else:
        text = await asyncio.to_thread(clean, raw, cleaner, roman) if raw else ""
    t2 = time.monotonic()
    return {"text": text or raw, "raw": raw, "stt_ms": round((t1 - t0) * 1000), "cleanup_ms": round((t2 - t1) * 1000),
            "total_ms": round((t2 - t0) * 1000), "head_start": head_start, "confidence": conf}


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
    stt_route, stt_model, cleaner, stt_provider = routes_for(user, q)
    # mode=meeting: one minute of a meeting track -> its plain transcript (no cleanup). mode=notes: a meeting's
    # transcript -> notes. Both only on plans with meeting notes (Pro), and for the owner token.
    mode = q.get("mode", ["dictate"])[0]
    if mode in ("meeting", "notes", "interpret"):
        notice = accounts.blocked(user) or (None if accounts.meetings_allowed(user) else
                                            "Meeting notes are part of the Pro plan.")
        if notice:
            await ws.send(json.dumps({"text": "", "error": "not allowed", "notice": notice}))
            return
    if mode == "interpret":  # one connection per lane for the whole session: {"to", "lane"}, the audio, "end"; repeat
        _bill.set(lambda part, micro: accounts.add_cost(user, "other", micro))
        to, lane, audio = "English", "them", bytearray()
        try:
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    audio.extend(msg)
                    if len(audio) > MAX_AUDIO:
                        await ws.close(code=4413, reason="too much audio")
                        return
                elif msg == "peek":  # a live preview of the sentence so far: text only, not counted as audio used
                    try:
                        if accounts.blocked(user) or accounts.over_limit(user):
                            raise RuntimeError("not allowed")  # the next "end" tells them why
                        reply = await asyncio.to_thread(peek, bytes(audio), to)
                    except Exception as exc:
                        print(time.strftime("%H:%M:%S"), "peek failed:", str(exc)[:200], flush=True)
                        reply = {"error": "peek failed"}
                    audio = bytearray()
                    await ws.send(json.dumps(reply))
                elif msg == "end":
                    notice = accounts.blocked(user) or accounts.over_limit(user)
                    if notice:
                        await ws.send(json.dumps({"error": "not allowed", "notice": notice}))
                        return
                    start_dictation(user)
                    try:
                        reply, pcm = await asyncio.to_thread(interpret, bytes(audio), to, lane)
                    except Exception as exc:
                        print(time.strftime("%H:%M:%S"), "interpret failed:", str(exc)[:200], flush=True)
                        reply, pcm = {"error": "interpret failed"}, b""
                    accounts.record_audio(user, accounts.audio_seconds(bytes(audio), "ogg"))
                    audio = bytearray()
                    await ws.send(json.dumps({**reply, "audio": len(pcm)}))
                    if pcm:
                        await ws.send(pcm)
                else:
                    req = json.loads(msg)
                    to = str(req.get("to") or to)[:40]
                    lane = req.get("lane") if req.get("lane") in INTERPRET_VOICES else lane
        except Exception:
            return  # the app closed the session
        return
    if mode == "notes":
        try:
            req = json.loads(await ws.recv())
            transcript = req.get("transcript", "")
            start_dictation(user)
            _bill.set(lambda part, micro: accounts.add_cost(user, "other", micro))  # meeting costs: usage other_micro
            t = time.monotonic()
            notes, model = await asyncio.to_thread(meeting_notes, transcript, language)
            meeting_id = None
            if transcript.strip():  # kept for the user (public.meetings); a failed save never costs them their notes
                try:
                    started = req.get("started")
                    meeting_id = await asyncio.to_thread(accounts.save_meeting, user, {
                        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)) if started else None,
                        "duration_seconds": req.get("duration_s"),
                        "speakers": len(set(re.findall(r"\] (Speaker \d+):", transcript))),
                        "transcript": transcript, "notes": notes, "notes_model": model})
                except Exception as exc:
                    print(time.strftime("%H:%M:%S"), "meeting not saved:", str(exc)[:200], flush=True)
            await ws.send(json.dumps({"notes": notes, "meeting_id": meeting_id}))
            print(time.strftime("%H:%M:%S"), f"meeting notes: {len(transcript)} chars of transcript -> {len(notes)} "
                  f"chars of notes in {(time.monotonic() - t) * 1000:.0f} ms ({model}; saved as {meeting_id})",
                  flush=True)
        except Exception as exc:
            print(time.strftime("%H:%M:%S"), "meeting notes failed:", str(exc)[:200], flush=True)
            await ws.send(json.dumps({"error": "notes failed"}))
        return
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
                notice = accounts.blocked(user)
                if notice:  # checked before any provider is paid; the app shows the notice
                    await ws.send(json.dumps({"text": "", "raw": "", "error": "account disabled", "notice": notice}))
                    return
                notice = accounts.over_limit(user)
                if notice:
                    await ws.send(json.dumps({"text": "", "raw": "", "error": "daily limit reached", "notice": notice}))
                    return
                did = start_dictation(user)  # the threads below inherit its bill and call-log tag
                script = "roman" if roman else "devanagari"
                if mode == "meeting":  # a meeting track -> timed segments; meeting audio is never saved to disk
                    _bill.set(lambda part, micro: accounts.add_cost(user, "other", micro))
                    t = time.monotonic()
                    diarize = q.get("diarize", ["0"])[0] == "1"
                    try:
                        segs = await asyncio.to_thread(meeting_segments, bytes(audio), language, diarize, stt_route,
                                                       stt_model, stt_provider)
                    except Exception as exc:
                        log_dictation(did, user, bytes(audio), "ogg", language, script, stt_route, "meeting", error=str(exc))
                        await ws.send(json.dumps({"error": "transcription failed"}))
                        return
                    accounts.record_audio(user, accounts.audio_seconds(bytes(audio), "ogg"))
                    raw = " ".join(s["text"] for s in segs)
                    result = {"segments": segs, "text": raw, "raw": raw, "stt_ms": round((time.monotonic() - t) * 1000),
                              "cleanup_ms": 0, "total_ms": round((time.monotonic() - t) * 1000)}
                    log_dictation(did, user, bytes(audio), "ogg", language, script, stt_route, "meeting", result)
                    await ws.send(json.dumps(result))
                    return
                meta = {"user": user, "language": language, "script": script, "stt": stt_route, "cleanup": cleaner}
                try:
                    result = await finish_stream(ws, bytes(audio), language, stt_route, cleaner, roman, stt_model, stt_provider)
                except Exception as exc:
                    print(time.strftime("%H:%M:%S"), "stream failed:", str(exc)[:200], flush=True)
                    await ws.send(json.dumps({"error": "transcription failed"}))  # details stay in the log
                    log_dictation(did, user, bytes(audio), "ogg", language, script, stt_route, cleaner, error=str(exc))
                    await asyncio.to_thread(save_dictation, bytes(audio), "ogg", {**meta, "error": str(exc)[:500]})
                    return
                accounts.record(user, accounts.audio_seconds(bytes(audio), "ogg"))
                log_dictation(did, user, bytes(audio), "ogg", language, script, stt_route, cleaner, result,
                              error=result.get("error", ""))
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
