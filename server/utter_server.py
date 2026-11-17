#!/usr/bin/env python3
"""Utter server: audio in, clean text out. One request does the whole after-you-stop pipeline next to the providers:
ElevenLabs Scribe v2 (batch) on the recording, then Groq cleanup, so the app makes one trip instead of two.

  POST /dictate?language=en|multi   body: the recording as MP3 (audio/mpeg)   header: Authorization: Bearer <token>
       -> {"text": cleaned, "raw": transcript, "stt_ms": .., "cleanup_ms": .., "total_ms": ..}
  GET  /health                      -> {"ok": true}
  WS   /stream?language=&stt=&cleanup=  (port UTTER_WS_PORT)  header: Authorization: Bearer <token>
       send Ogg/Opus bytes as binary frames WHILE the user speaks, then the text frame "end", then (optional)
       {"live": "<the app's live transcript>"}: cleanup starts on it at once, while Scribe works on the audio, and is
       used if Scribe's transcript has the same words (a head start of about one cleanup);
       -> one text frame with the same JSON as /dictate. The upload overlaps the speaking, so only the last moment
       is left to send when they stop.

Standard library only. Listens on 127.0.0.1; a Cloudflare tunnel puts HTTPS in front of it. Keys and the token come
from the environment (systemd EnvironmentFile), never from the request. Provider connections are kept open and reused,
so a request pays no handshake to ElevenLabs or Groq.
"""
import re, asyncio, http.client, json, os, secrets, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

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
# cleanup through OpenRouter, pinned to one provider (its capacity, not our Groq free-tier limit): 178 ms median
OPENROUTER_CLEANUP_MODEL = os.environ.get("OPENROUTER_CLEANUP_MODEL", "meta-llama/llama-3.1-8b-instruct")
OPENROUTER_CLEANUP_PROVIDER = os.environ.get("OPENROUTER_CLEANUP_PROVIDER", "groq")
SURPLUS_CLEANUP_MODEL = "llama-3.3-70b-instruct"
GROQ_STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
TOKEN = os.environ["UTTER_TOKEN"]
PORT = int(os.environ.get("UTTER_PORT", "8190"))
WS_PORT = int(os.environ.get("UTTER_WS_PORT", "8191"))
SCRIBE_MODEL = os.environ.get("SCRIBE_MODEL", "scribe_v2")
CLEANUP_MODEL = os.environ.get("CLEANUP_MODEL", "qwen/qwen3.8-27b")
CLEANUP_EXTRA = {"reasoning_effort": "none"} if "qwen" in CLEANUP_MODEL else {"reasoning_effort": "low"}
MAX_AUDIO = 25 * 1024 * 1024  # ~100 min of 32 kbps MP3; refuse anything bigger
TIMEOUT_S = 20
# cleanup normally takes 0.2-0.7 s; a stalled Groq request must retry (and then give up) inside the app's 8 s wait
CLEANUP_TIMEOUT_S = 3

# kept identical to the app's prompt (src/utter-daemon.py CLEANUP_PROMPT); deploy copies it in
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
    return json.loads(data).get("text", "").strip()


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
    return (json.loads(data)["choices"][0]["message"]["content"] or "").strip()


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
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/dictate":
            return self.reply(404, {"error": "not found"})
        if not secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {TOKEN}"):
            return self.reply(401, {"error": "bad token"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_AUDIO:
            return self.reply(413 if length else 400, {"error": "send the recording as an MP3 body"})
        mp3 = self.rfile.read(length)
        q = parse_qs(url.query)
        language = q.get("language", ["en"])[0]
        stt_route = q.get("stt", ["direct"])[0] if q.get("stt", ["direct"])[0] in ("surplus", "groq", "openrouter", "cartesia") else "direct"
        cleaner = q.get("cleanup", ["groq"])[0] if q.get("cleanup", ["groq"])[0] in ("surplus", "none", "openrouter", "cerebras") else "groq"       # ?cleanup=groq|surplus
        t0 = time.monotonic()
        try:
            raw = transcribe(mp3, language, stt_route)
            t1 = time.monotonic()
            text = clean(raw, cleaner) if raw else ""
            t2 = time.monotonic()
        except Exception as exc:
            self.log_message("dictate failed: %s", str(exc)[:200])
            return self.reply(502, {"error": str(exc)[:300]})
        stt_ms, clean_ms = (t1 - t0) * 1000, (t2 - t1) * 1000
        self.log_message("dictate: %d KB audio, stt %.0f ms, cleanup %.0f ms", length // 1024, stt_ms, clean_ms)
        self.reply(200, {"text": text or raw, "raw": raw, "stt_ms": round(stt_ms), "cleanup_ms": round(clean_ms),
                         "total_ms": round(stt_ms + clean_ms)})

    def log_message(self, fmt, *args):  # never log audio or text, only sizes and timings
        print(time.strftime("%H:%M:%S"), fmt % args, flush=True)


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
    if not secrets.compare_digest(headers.get("Authorization", ""), f"Bearer {TOKEN}"):
        await ws.close(code=4401, reason="bad token")
        return
    q = urllib.parse.parse_qs(urllib.parse.urlparse(req_path).query)
    language = q.get("language", ["en"])[0]
    roman = q.get("script", [""])[0] == "roman"
    stt_route = q.get("stt", ["direct"])[0] if q.get("stt", ["direct"])[0] in ("surplus", "groq", "openrouter", "cartesia") else "direct"
    cleaner = q.get("cleanup", ["groq"])[0] if q.get("cleanup", ["groq"])[0] in ("surplus", "none", "openrouter", "cerebras") else "groq"
    audio = bytearray()
    try:
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                audio.extend(msg)
                if len(audio) > MAX_AUDIO:
                    await ws.close(code=4413, reason="too much audio")
                    return
            elif msg == "end":
                if not audio:
                    await ws.send(json.dumps({"text": "", "raw": "", "error": "no audio"}))
                    return
                try:
                    result = await finish_stream(ws, bytes(audio), language, stt_route, cleaner, roman)
                except Exception as exc:
                    print(time.strftime("%H:%M:%S"), "stream failed:", str(exc)[:200], flush=True)
                    await ws.send(json.dumps({"error": str(exc)[:300]}))
                    return
                print(time.strftime("%H:%M:%S"), f"stream: {len(audio) // 1024} KB opus, stt {result['stt_ms']} ms, "
                      f"cleanup {result['cleanup_ms']} ms{' (head start used)' if result['head_start'] else ''}",
                      flush=True)
                await ws.send(json.dumps(result))
                return
    except Exception:
        return  # the client went away mid-dictation


async def serve_ws():
    import websockets
    async with websockets.serve(stream_handler, "127.0.0.1", WS_PORT, max_size=MAX_AUDIO, ping_interval=20):
        print(f"utter-server websocket on 127.0.0.1:{WS_PORT}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    threading.Thread(target=keep_warm, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"utter-server on 127.0.0.1:{PORT} ({SCRIBE_MODEL} + {CLEANUP_MODEL})", flush=True)
    asyncio.run(serve_ws())
