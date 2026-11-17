#!/usr/bin/env python3
"""Utter: push-to-talk dictation for Linux, on Deepgram's live speech-to-text.

Keeps a WebSocket to Deepgram open so a press starts streaming at once, types the words into the focused window
as they arrive (or all at once when you stop), and copies every dictation to the clipboard and History.

An idle socket gets a KeepAlive every KEEPALIVE_S (Deepgram closes one after ~10 s of silence) and is replaced
every REFRESH_S. Signals: SIGUSR1 starts/stops a dictation, SIGHUP re-reads the API key, SIGUSR2 opens the window.
Runs as the systemd user service `utter`; the `utter` command sends the signals.
"""
import signal

# Catch presses from the first instant: an unhandled SIGUSR1 kills the process, and the slower imports below take
# long enough for an early Alt+D to land before main() installs its real handlers. They are replayed in main().
EARLY_PRESSES = []
signal.signal(signal.SIGUSR1, lambda *_: EARLY_PRESSES.append(1))
signal.signal(signal.SIGUSR2, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
signal.signal(signal.SIGRTMIN, signal.SIG_IGN)  # hold-to-talk "cancel", handled once main() is running

import os  # noqa: E402
# Tells the `utter` command it is safe to send presses (it waits for this file to name the service's PID). Written
# only when this file runs as the service, never when other code imports it, or the command would wait for a PID
# that is not the service's and stall every press for its full 5-second limit.
READY_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "utter.ready")
if __name__ == "__main__":
    with open(READY_FILE, "w") as _f:
        _f.write(str(os.getpid()))

import array, ast, asyncio, base64, http.client, io, json, math, os, shutil, subprocess, time, wave  # noqa: E402

import websockets

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hinglish import romanize  # noqa: E402  (lives next to this file)

RATE, CHUNK = 16000, 640  # 20 ms of 16-bit mono
FAST = "3"  # ms per key hold and between keys (ydotool defaults are 20+20); raise if an app drops letters
PASTE_SETTLE_S = 0.01  # clipboard margin before Ctrl+V
PASTE_KEY_DELAY = "5"  # ms between the Ctrl+V key events (ydotool's default is ~12)
os.environ.setdefault("YDOTOOL_SOCKET", f"/run/user/{os.getuid()}/.ydotool_socket")


def paste(text: str) -> None:
    """Insert text at once: put it on the clipboard and press Ctrl+V. Instant at any length, where typing takes
    a few ms per character. Terminals paste with Ctrl+Shift+V, so there it lands on the clipboard only."""
    subprocess.run(["wl-copy", "--", text], stderr=subprocess.DEVNULL, timeout=3)
    # wl-copy returns once the clipboard is set; the short wait is a margin for apps slow to see a new clipboard.
    # Was 50 ms with default key delays (~120 ms in all); raise PASTE_SETTLE_S if an app pastes the old clipboard
    time.sleep(PASTE_SETTLE_S)
    subprocess.run(["ydotool", "key", "-d", PASTE_KEY_DELAY, "29:1", "47:1", "47:0", "29:0"],
                   stderr=subprocess.DEVNULL)  # Ctrl+V


class Typer:
    """Keeps what has been typed for the current segment and edits it toward the newest text.
    In paste mode nothing is typed while you speak: each finished sentence is pasted whole."""

    def __init__(self, pasting: bool = False) -> None:
        self.shown = ""
        self.pasting = pasting

    def update(self, text: str, final: bool = False) -> None:
        if self.pasting:
            if final and text.strip():
                paste(text)
            return
        if not final:  # word by word: hold back the last, still-forming word
            cut = text.rfind(" ")
            text = text[:cut + 1] if cut >= 0 else ""
        common = os.path.commonprefix([self.shown, text])
        erase, add = len(self.shown) - len(common), text[len(common):]
        if erase:
            subprocess.run(["ydotool", "key", "-d", FAST, *(["14:1", "14:0"] * erase)], stderr=subprocess.DEVNULL)
        if add:
            if add.isascii():
                subprocess.run(["ydotool", "type", "-d", FAST, "-H", FAST, "--", add], stderr=subprocess.DEVNULL)
            else:  # ydotool types US-keyboard characters only, so anything else is pasted
                subprocess.run(["wl-copy", "--", add], stderr=subprocess.DEVNULL, timeout=3)
                subprocess.run(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"], stderr=subprocess.DEVNULL)  # Ctrl+V
        self.shown = text

    def commit(self, text: str) -> None:
        self.update(text.strip() + " " if text.strip() else "", final=True)
        self.shown = ""

CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "utter")
DATA = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "utter")
HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(CONFIG, "deepgram.key")


def read_key() -> str:
    try:
        with open(KEY_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""  # no key yet: the first press opens the API keys page


KEY = read_key()
KEY_CHANGED = False  # set by SIGHUP (a new key or language in the window): reconnect with it


def reload_key() -> None:
    global KEY, KEY_CHANGED
    try:
        KEY = read_key()
        KEY_CHANGED = True
        LOG("key and settings reloaded; reconnecting")
    except OSError as exc:
        LOG(f"key reload failed: {exc}")
URL = ("wss://api.deepgram.com/v1/listen?model=nova-3&encoding=linear16&sample_rate=16000&channels=1"
       "&interim_results=true&punctuate=true&smart_format=true&endpointing=300")
KEEPALIVE_S, REFRESH_S = 5, 1800
LOG = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)


def notify(msg: str) -> None:
    os.spawnlp(os.P_NOWAIT, "notify-send", "notify-send", "-t", "4000", "-i", "utter", "Utter", msg)


SOUNDS = os.path.join(HERE, "sounds")


def play(name: str) -> None:
    try:
        subprocess.Popen(["paplay", os.path.join(SOUNDS, name)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


MIN_FLOOR, DB_RANGE = 150, 21  # voice bars: full height at DB_RANGE dB above the room noise (raise for calmer bars)


class Popup:
    """The on-screen pill (utter-popup.py), started once and kept hidden so it appears instantly.
    A broken popup never stops dictation; a dead one is restarted on the next press."""

    def __init__(self) -> None:
        self.proc = None
        self._spawn()

    def _spawn(self) -> None:
        try:
            self.proc = subprocess.Popen(["python3", os.path.join(HERE, "utter-popup.py")],
                                         stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError:
            self.proc = None

    def begin(self) -> None:
        self.recent = []  # mic levels for the noise floor, per dictation
        self.heard = []  # every level this dictation, for the summary in the log
        if self.proc is None or self.proc.poll() is not None:
            self._spawn()
        self._send("S")
        play("start.wav")

    def stopped(self, wait_for_text: bool) -> None:
        """The stop press: chime now; hide now, or show the loader until hide() once the text is typed."""
        play("stop.wav")
        self._send("P" if wait_for_text else "H")

    def summary(self) -> str:
        """Mic levels for this dictation, to tune DB_RANGE against a real voice."""
        if not getattr(self, "heard", None):
            return "no audio"
        h = sorted(self.heard)
        floor = max(MIN_FLOOR, h[len(h) // 5] * 1.1)
        loud = h[int(len(h) * 0.9)]
        return (f"room ~{floor:.0f} RMS, your speech ~{loud:.0f} RMS (90th pct) = "
                f"{20 * math.log10(max(loud, 1) / floor):.1f} dB above the room")

    def hide(self) -> None:
        self._send("H")

    def open(self, page: str) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self._spawn()
        self._send(f"W {page}")

    def message(self, text: str) -> None:
        self._send("M " + text.replace("\n", " "))

    def heard_speech(self) -> bool:
        """Was there clearly voice in this dictation (well above the room), even though no words came back?"""
        h = sorted(getattr(self, "heard", []) or [0])
        floor = max(MIN_FLOOR, h[len(h) // 5] * 1.1)
        return 20 * math.log10(max(h[int(len(h) * 0.9)], 1) / floor) > 8

    def reconnecting(self) -> None:
        self._send("R")

    def listening(self) -> None:
        self._send("A")

    def level(self, pcm: bytes) -> None:
        samples = array.array("h", pcm)
        rms = math.sqrt(sum(x * x for x in samples) / len(samples))
        # the room's own noise is the quiet end of the last ~2 s; only sound above it moves the bars
        self.recent = (self.recent + [rms])[-50:]
        self.heard.append(rms)
        floor = max(MIN_FLOOR, sorted(self.recent)[len(self.recent) // 5] * 1.1)
        # loudness is heard on a log scale, so the bars measure decibels above the room: whisper low, speech mid,
        # raised voice near the top, instead of all-or-nothing
        above_db = 20 * math.log10(max(rms, 1.0) / floor)
        self._send(f"L {min(1.0, max(0.0, above_db / DB_RANGE)):.3f}")

    def _send(self, line: str) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
            except OSError:
                pass



class Deepgram:
    """Deepgram nova-3: raw PCM in; Results messages out about once a second, even in silence."""
    name = "deepgram"
    replies_in_silence = True  # so a long silence from it means a dead socket
    reuse_socket = True  # Finalize answers are tagged, so one socket safely serves many dictations

    async def connect(self):
        # "multi" follows you between English and Hindi mid-sentence; "en" is English only (the default)
        language = "multi" if setting("language") == "multi" else "en"
        return await websockets.connect(f"{URL}&language={language}",
                                        additional_headers={"Authorization": f"Token {KEY}"}, max_size=None)

    async def keepalive(self, ws) -> None:
        await ws.send(json.dumps({"type": "KeepAlive"}))  # it closes a socket after ~10 s without audio

    def audio(self, pcm: bytes):
        return pcm

    def finish(self) -> str:
        return json.dumps({"type": "Finalize"})  # flush the last words as a final result

    def parse(self, msg: dict, finishing: bool):
        """-> (text or None, is_final, ends_the_dictation)"""
        if msg.get("type") != "Results":
            if msg.get("type") not in ("Metadata", "SpeechStarted", "UtteranceEnd"):
                LOG(f"{msg.get('type')}: {str(msg)[:150]}")
            return None, False, False
        final = bool(msg.get("is_final"))
        # the answer to our Finalize ends the dictation; a late one from the previous dictation must not
        return msg["channel"]["alternatives"][0]["transcript"], final, final and bool(msg.get("from_finalize")) and finishing


class ElevenLabs:
    """ElevenLabs Scribe realtime: base64 PCM in JSON; partial and committed transcripts out, only while you speak.
    It closes a socket after ~15 s without audio, so an idle one is simply reopened when that happens."""
    name = "elevenlabs"
    replies_in_silence = False
    reuse_socket = False  # a late commit could leak into the next dictation; it drops idle sockets in ~15 s anyway
    URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id=scribe_v3_live&audio_format=pcm_16000" \
          "&commit_strategy=vad"

    def __init__(self) -> None:
        try:
            with open(os.path.join(CONFIG, "elevenlabs.key")) as f:
                self.key = f.read().strip()
        except OSError:
            self.key = ""

    async def connect(self):
        if not self.key:
            raise RuntimeError("no ElevenLabs API key saved")
        # English only pins the language; English + Hindi lets it detect per utterance
        url = self.URL + ("" if setting("language") == "multi" else "&language_code=en")
        ws = await websockets.connect(url, additional_headers={"xi-api-key": self.key}, max_size=None)
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if first.get("message_type") != "session_started":
            raise RuntimeError(f"ElevenLabs: {str(first)[:150]}")
        return ws

    async def keepalive(self, ws) -> None:
        if ws.close_code is not None:  # it closed the idle socket: the main loop reconnects
            raise websockets.ConnectionClosed(None, None)

    def audio(self, pcm: bytes) -> str:
        return json.dumps({"message_type": "input_audio_chunk", "sample_rate": RATE,
                           "audio_base_64": base64.b64encode(pcm).decode(), "commit": False})

    def finish(self) -> str:  # a moment of silence marked commit=true finalises the last words
        return json.dumps({"message_type": "input_audio_chunk", "sample_rate": RATE,
                           "audio_base_64": base64.b64encode(b"\0" * CHUNK).decode(), "commit": True})

    def parse(self, msg: dict, finishing: bool):
        kind = msg.get("message_type", "")
        if kind == "partial_transcript":
            return msg.get("text", ""), False, False
        if kind == "committed_transcript":
            return msg.get("text", ""), True, finishing
        if kind in ("session_started", "warning") or "transcript" in kind:
            return None, False, False
        raise RuntimeError(f"ElevenLabs {kind}: {msg.get('error') or str(msg)[:150]}")  # quota, auth, rate limit


class AssemblyAI:
    """AssemblyAI Universal streaming: raw PCM in; growing partial turns out while you speak, a formatted final turn
    at each pause. ForceEndpoint finalises the last words; if the turn was already final it answers with an empty
    final turn, so a stop never waits on the timeout. Opening a socket takes ~1.5 s, so it is opened ahead."""
    name = "assemblyai"
    replies_in_silence = False
    reuse_socket = False  # a late final could leak into the next dictation; a fresh socket per dictation is opened ahead
    # English only: the multilingual model covers European languages, not Hindi
    URL = ("wss://streaming.assemblyai.com/v3/ws?sample_rate=16000&encoding=pcm_s16le&format_turns=true"
           "&speech_model=universal-streaming-english")

    def __init__(self) -> None:
        try:
            with open(os.path.join(CONFIG, "assemblyai.key")) as f:
                self.key = f.read().strip()
        except OSError:
            self.key = ""

    async def connect(self):
        if not self.key:
            raise RuntimeError("no AssemblyAI API key saved")
        ws = await websockets.connect(self.URL, additional_headers={"Authorization": self.key}, max_size=None)
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if first.get("type") != "Begin":
            raise RuntimeError(f"AssemblyAI: {str(first)[:150]}")
        return ws

    async def keepalive(self, ws) -> None:
        if ws.close_code is not None:  # it closed the socket: the main loop reconnects
            raise websockets.ConnectionClosed(None, None)

    MIN_BYTES = 3 * CHUNK  # it ignores messages under 50 ms of audio, and Utter reads 20 ms at a time

    def audio(self, pcm: bytes):
        self.pending = getattr(self, "pending", b"") + pcm
        if len(self.pending) < self.MIN_BYTES:
            return None  # held until there is enough to send
        out, self.pending = self.pending, b""
        return out

    TAIL_SILENCE = int(0.3 * RATE) * 2  # 300 ms: without it, it drops a last word said right before the stop

    def flush(self):
        """The last few ms plus 300 ms of silence, sent at once before ForceEndpoint (tested: "...the dentist at"
        became "...the dentist at four" for ~90 ms more wait)."""
        out, self.pending = getattr(self, "pending", b""), b""
        return out + b"\0" * self.TAIL_SILENCE

    def finish(self) -> str:
        return json.dumps({"type": "ForceEndpoint"})

    def parse(self, msg: dict, finishing: bool):
        kind = msg.get("type")
        if kind == "Turn":
            final = bool(msg.get("end_of_turn"))
            # ponytail: a pause-final arriving between the stop and the ForceEndpoint answer ends the dictation one
            # turn early; finals arrive in order, so that needs speech after a pause inside the last ~0.2 s
            return msg.get("transcript", ""), final, final and finishing
        if kind in ("Begin", "Termination"):
            return None, False, False
        raise RuntimeError(f"AssemblyAI {kind}: {str(msg)[:150]}")  # auth, quota, rate limit


class _Silent:
    """A stand-in socket for "no live transcript": takes audio and drops it, never answers, closes on request."""
    close_code = None

    def __init__(self) -> None:
        self._closed = asyncio.Event()

    async def send(self, _data) -> None:
        pass

    async def close(self) -> None:
        self.close_code = 1000
        self._closed.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._closed.wait()
        raise StopAsyncIteration


class NoLive:
    """No live transcript: the only transcript is the one from the server (or batch) after you stop. One provider,
    lowest cost; no head-start cleanup and no live backup if that request fails."""
    name = "none"
    replies_in_silence = False
    reuse_socket = True

    async def connect(self):
        return _Silent()

    async def keepalive(self, ws) -> None:
        pass

    def audio(self, pcm: bytes) -> bytes:
        return b""

    def finish(self) -> bytes:
        return b""

    def parse(self, msg: dict, finishing: bool):
        return None, False, False


ENGINE = Deepgram()  # replaced at each connect by the one chosen in Settings


async def connect():
    global ENGINE
    ENGINE = {"elevenlabs": ElevenLabs, "assemblyai": AssemblyAI, "none": NoLive}.get(setting("stt"), Deepgram)()
    t = time.monotonic()
    ws = await ENGINE.connect()
    language = "multi" if setting("language") == "multi" else "en"
    LOG(f"warm socket ready ({ENGINE.name}, {language}) in {(time.monotonic() - t) * 1000:.0f} ms")
    return ws


async def idle(ws, toggle: asyncio.Event) -> bool:
    """Keep the socket alive until a press (True) or the refresh deadline (False)."""
    deadline = time.monotonic() + REFRESH_S
    while time.monotonic() < deadline:
        try:
            await asyncio.wait_for(toggle.wait(), timeout=KEEPALIVE_S)
            return True
        except asyncio.TimeoutError:
            if KEY_CHANGED:
                return False  # reconnect now with the new key, language or speech service
            await ENGINE.keepalive(ws)
    return False


TAIL_S = 0  # no tail: the mic stops the moment you press stop (raise to 0.3 if last words get clipped)
STALL_S = 6  # Deepgram answers about once a second, even in silence; this long without a word means a dead socket


async def dictate(ws, stop: asyncio.Event, pressed_at: float):
    """One dictation. Returns the socket to keep for the next press (a new one if it had to reconnect), or None."""
    rec = await asyncio.create_subprocess_exec(
        "parecord", "--raw", f"--rate={RATE}", "--channels=1", "--format=s16le", "--latency-msec=20", *mic_args(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    DICTATION.update(running=True, cancelled=False)
    typer = Typer(pasting=setting("insert") != "type")  # paste unless Settings says type
    popup = POPUP
    popup.begin()
    said = []  # final segments, for the clipboard and history
    live = mode() == "live"  # "once": type nothing until the dictation ends, then type it all
    cleaner = None
    if not live and setting("cleanup"):
        cleaner = Cleaner(setting("cleanup_provider") or "surplus")
        if cleaner.key:
            cleaner.warm()  # open the HTTPS connection now, while you speak
        else:
            LOG("cleanup is on but no key is saved for its provider; typing raw text")
            cleaner = None
    final = {"text": None}
    spare = None  # second cleanup connection: cleans the live transcript while the accurate one is on its way
    batch, recorded = None, []
    server = None
    ab = take_ab_route() if not live else None
    ROUTE["override"] = {"server": "server", "local": "elevenlabs"}.get(ab) if ab else None
    if ab:
        LOG(f"A/B: this dictation goes {ab}")
    if not live and setting("accurate") and accurate_route() == "server":
        server = ServerStream()
        if server.token:
            await server.start()  # connects in the background; audio is encoded meanwhile
        else:
            LOG("Utter server is chosen but no server token is saved; using the live transcript")
            server = None
    elif not live and setting("accurate"):
        batch = BatchSTT()
        if batch.key:
            batch.warm()
            await batch.start_encoder()
            if cleaner:
                spare = Cleaner(setting("cleanup_provider") or "surplus")
                spare.warm()
        else:
            LOG(f"Most accurate is on but no {batch.route} key is saved; using the live transcript")
            batch = None
    roman = setting("hindi") != "devanagari"  # Hindi as Roman Hinglish unless Settings says Devanagari
    conn = {"ws": ws, "heard": time.monotonic(), "receiver": None, "finishing": False}
    engine = ENGINE

    async def receive(sock):
        try:
            async for raw in sock:
                conn["heard"] = time.monotonic()
                text, is_final, ends = engine.parse(json.loads(raw), conn["finishing"])
                if text is None:
                    continue
                if roman:
                    text = romanize(text)  # Hindi in Roman letters, as typed in chats
                LOG(f"{'final' if is_final else 'interim'} {text!r}"[:160])
                if is_final:
                    if live:
                        typer.commit(text)
                    if text.strip():
                        said.append(text.strip())
                    if ends:
                        return
                elif live:
                    typer.update(text)
        except websockets.ConnectionClosed:
            pass  # the sender notices and reconnects

    async def reconnect(why: str) -> bool:
        """Swap in a fresh socket mid-dictation. The mic keeps recording meanwhile (the pipe holds a few
        seconds), so the backlog is sent as soon as it is back and no words are lost."""
        LOG(f"connection lost ({why}); reconnecting")
        popup.reconnecting()
        conn["receiver"].cancel()
        try:
            await conn["ws"].close()
        except Exception:
            pass
        typer.shown = ""  # keep what is already typed; the new socket starts a fresh segment
        try:
            conn["ws"] = await asyncio.wait_for(connect(), timeout=10)
        except Exception as exc:
            LOG(f"reconnect failed: {exc}")
            notify("Lost the connection to Deepgram. What you said before it dropped is in History.")
            return False
        conn["heard"] = time.monotonic()
        conn["receiver"] = asyncio.create_task(receive(conn["ws"]))
        popup.listening()
        return True

    mic_off = asyncio.Event()  # set TAIL_S after the stop press: only then does the mic stop

    async def send_audio():
        first, n = True, 0
        while not mic_off.is_set():
            try:
                data = await asyncio.wait_for(rec.stdout.readexactly(CHUNK), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except asyncio.IncompleteReadError:
                break
            msg = None
            try:
                if engine.replies_in_silence and time.monotonic() - conn["heard"] > STALL_S:
                    raise ConnectionError(f"no reply for {STALL_S} s")
                msg = engine.audio(data)
                if msg:
                    await conn["ws"].send(msg)
            except (websockets.ConnectionClosed, OSError) as exc:
                if not await reconnect(str(exc) or type(exc).__name__):
                    stop.set()
                    break
                msg = msg or engine.audio(data)  # resend what failed, not a re-buffered copy
                if msg:
                    await conn["ws"].send(msg)
            if batch:
                recorded.append(data)
                batch.feed(data)
            if server:
                server.feed(data)
            n += 1
            if n % 2 == 0:  # 25 level updates a second
                popup.level(data)
            if first:
                first = False
                LOG(f"press -> first audio on the wire: {(time.monotonic() - pressed_at) * 1000:.0f} ms")
        try:
            conn["finishing"] = True
            tail = engine.flush() if hasattr(engine, "flush") else None
            if tail:
                await conn["ws"].send(tail)
            await conn["ws"].send(engine.finish())  # flush the last words as a final result
        except (websockets.ConnectionClosed, OSError):
            pass

    conn["receiver"] = asyncio.create_task(receive(ws))
    sender = asyncio.create_task(send_audio())
    try:
        await stop.wait()
        stopped_at = time.monotonic()
        LOG("mic levels: " + popup.summary())
        popup.stopped(wait_for_text=not live)  # live: hide on the press; all at once: loader until it is typed
        await asyncio.sleep(TAIL_S)  # the chime and popup react at once; the mic keeps catching your last words
        mic_off.set()
        rec.terminate()
        await sender
        # the audio is complete now: start the accurate transcript at once, while the live one finishes in parallel
        accurate_job = asyncio.ensure_future(batch.transcribe(b"".join(recorded))) if batch and recorded else None
        server_job = asyncio.ensure_future(server.finish()) if server else None
        if engine.name == "none":  # nothing live to wait for
            conn["receiver"].cancel()
        else:
            try:
                await asyncio.wait_for(conn["receiver"], timeout=3)
            except asyncio.TimeoutError:
                conn["receiver"].cancel()
        live_text = " ".join(said)
        if server_job and live_text:
            asyncio.ensure_future(server.send_live(live_text))
        # head start: clean the live transcript now, in parallel with the accurate one; used if the two agree
        early = asyncio.ensure_future(spare.clean(live_text)) if spare and accurate_job and live_text else None
        if accurate_job:
            accurate = await accurate_job
            if accurate:
                LOG(f"live: {live_text!r} | accurate: {accurate!r}"[:300])
                said[:] = [romanize(accurate) if roman else accurate]
        served = await server_job if server_job else None
        if served and engine.name == "none" and is_ghost(served.get("raw") or served.get("text", ""), popup):
            # nothing live to confirm speech, and Whisper invents phrases like "Thank you." in silence
            LOG(f"looks like Whisper's silence filler, not speech; ignoring {served.get('text', '')!r}")
            served = None
        if served:  # the server already transcribed and cleaned it
            said[:] = [romanize(served["raw"]) if roman else served["raw"]]
            text = romanize(served["text"]) if roman else served["text"]
            final["text"] = text
            if not DICTATION["cancelled"]:
                typer.commit(text)
            LOG(f"stop -> text: {(time.monotonic() - stopped_at) * 1000:.0f} ms (server)")
        elif not live and said:
            text = " ".join(said)
            if cleaner:
                if early and same_words(text, live_text) and (await early, spare.ok)[1]:
                    LOG("cleanup: the accurate transcript matched the live one, so the head-start cleanup was used")
                    text = early.result()
                else:
                    text = await cleaner.clean(text)
            final["text"] = text
            if not DICTATION["cancelled"]:
                typer.commit(text)
            LOG(f"stop -> text: {(time.monotonic() - stopped_at) * 1000:.0f} ms (local)")
    finally:
        if rec.returncode is None:
            rec.terminate()
        if cleaner:
            cleaner.close()
        if batch:
            batch.close()
        if spare:
            spare.close()
        if server:
            await server.close()
            WARM_NOW.set()  # open the next connection now, not at the next press
        cancelled_now = DICTATION["cancelled"]
        if cancelled_now:
            final["text"], said[:] = None, []  # nothing pasted, nothing saved
        DICTATION["running"] = False
        if ab and not final["text"]:
            give_back_ab_route(ab)
            LOG(f"A/B: no text, so the {ab} slot is kept for the next dictation")
        ROUTE["override"] = None
        if said:
            popup.hide()
        elif cancelled_now:
            popup.message("Cancelled")
        elif setting("language") != "multi" and popup.heard_speech():
            popup.message("Didn't catch that. Speaking Hindi? Turn on English + Hindi in Settings.")
        else:
            popup.message("Didn't catch that. Try again.")
        keep(final["text"] or " ".join(said), raw=" ".join(said))  # always: even a failed dictation is kept
    if conn["ws"].close_code is None and engine.reuse_socket:
        return conn["ws"]
    try:
        await conn["ws"].close()
    except Exception:
        pass
    return None


HISTORY = os.path.join(DATA, "history.jsonl")
SETTINGS = os.path.join(CONFIG, "settings.json")  # written by the window's Settings page

# ---- AI cleanup (All at once mode): self-corrections, filler words, punctuation -----------------------------------
# provider -> (host, chat path, default model, extra request fields). Both speak the OpenAI chat API; the key is
# <provider>.key in CONFIG. Measured 2026-11-12 from India on 4 dictations x 3: Surplus llama-3.3-70b-instruct median
# 1.2 s (0.8-3.1 s), Groq gpt-oss-120b with low reasoning 0.8 s (it cannot turn thinking off), Groq qwen3.8-27b with
# thinking off 0.4 s but it looped once (the loop guard types the raw text then). Groq no longer serves Llama.
CLEANUP_PROVIDERS = {
    "surplus": ("api.surplusintelligence.ai", "/v1/chat/completions", "llama-3.3-70b-instruct", {}),
    "groq": ("api.groq.com", "/openai/v1/chat/completions", "qwen/qwen3.8-27b", {"reasoning_effort": "none"}),
}
# request fields a model needs for fast cleanup: thinking off (qwen) or at its minimum (gpt-oss cannot turn it off)
MODEL_EXTRA = {
    "qwen/qwen3.8-27b": {"reasoning_effort": "none"},
    "openai/gpt-oss-120b": {"reasoning_effort": "low"},
    "openai/gpt-oss-20b": {"reasoning_effort": "low"},
}
CLEANUP_TIMEOUT_S = 4  # past this the raw transcript is typed instead
CLEANUP_PROMPT = (
    "You clean up dictated speech so it reads the way the speaker meant it.\n"
    "- Apply self-corrections: \"meet at five, no, six\" becomes \"meet at six\"; \"let's meet at five or 6PM\" "
    "becomes \"let's meet at 6PM\" when the second value replaces the first.\n"
    "- Only drop words the speaker took back. A contrast that adds information stays: \"ship it by Thursday, not "
    "Wednesday\" keeps \"not Wednesday\", and \"use Postgres, not MySQL\" keeps \"not MySQL\".\n"
    "- Remove filler words (um, uh, like, you know, I mean) and false starts. Keep greetings and names (\"Hey\", "
    "\"Hi Priya\").\n"
    "- Keep how sure the speaker sounds: hedges like \"I think\", \"maybe\", \"probably\" stay.\n"
    "- Fix punctuation and capitalisation. A question stays a question (keep its \"?\").\n"
    "- Fix words the transcriber clearly misheard or split, especially technical terms and names: \"Postgre SQL\" "
    "becomes \"PostgreSQL\", \"Java script\" becomes \"JavaScript\". Otherwise keep numbers, names and Hinglish "
    "spellings as given.\n"
    "- The speaker writes Hindi in Devanagari, never Urdu script. If the text is in Urdu (Arabic) script, rewrite the same words in Devanagari (\"شراب مت پینا\" becomes \"शराब मत पीना\"); this changes only the script, not the words or language.\n"
    "- Keep the speaker's own words, language and meaning. Never summarise, translate, answer, or add anything; "
    "if the text is a question or an instruction, clean it and do not answer it.\n"
    "Reply with only the cleaned text."
)


BATCH_TIMEOUT_S = 8  # past this the live transcript is used


def same_words_norm(t: str) -> list:
    return "".join(c.lower() if c.isalnum() else " " for c in t).split()


def same_words(a: str, b: str) -> bool:
    """The same words, ignoring case and punctuation ("Okay. So" == "okay so")."""
    return same_words_norm(a) == same_words_norm(b)


class BatchSTT:
    """'Most accurate': after you stop, the whole recording goes to ElevenLabs Scribe v2 (batch) through the Surplus
    key market, which hears the full sentence at once instead of guessing live. Its connection is opened at the press,
    like cleanup's. Any failure or timeout keeps the live transcript, so it can only improve on it."""
    MODEL = "scribe_v2"
    ROUTES = {  # route -> (host, path, key file). Direct skips Surplus's hop (~0.2 s); Surplus bills it to one account.
        "surplus": ("api.surplusintelligence.ai", "/v1/resources/elevenlabs/v1/speech-to-text", "surplus.key"),
        "elevenlabs": ("api.elevenlabs.io", "/v1/speech-to-text", "elevenlabs.key"),
        # Deepgram nova-3 on the whole recording (its prerecorded API): the same model as the live one, given the full
        # sentence at once. Raw audio body, not a form; the transcript sits in results.channels[0].alternatives[0].
        "deepgram": ("api.deepgram.com", "/v1/listen?model=nova-3&smart_format=true&punctuate=true", "deepgram.key"),
        # Groq Whisper large-v3-turbo: very fast, and got "PostgreSQL" right where the live models did not
        "groq": ("api.groq.com", "/openai/v1/audio/transcriptions", "groq.key"),
    }

    def __init__(self) -> None:
        self.route = accurate_route() if accurate_route() in self.ROUTES else "surplus"
        self.HOST, self.PATH, keyfile = self.ROUTES[self.route]
        try:
            with open(os.path.join(CONFIG, keyfile)) as f:
                self.key = f.read().strip()
        except OSError:
            self.key = ""
        self.conn, self.warming = None, None
        self.encoder, self.mp3 = None, None

    def warm(self) -> None:
        self.conn = http.client.HTTPSConnection(self.HOST, timeout=BATCH_TIMEOUT_S)
        self.warming = asyncio.ensure_future(asyncio.to_thread(self.conn.connect))

    async def start_encoder(self) -> None:
        """Encode while you speak, so the file is ready the moment you stop (encoding after cost ~150 ms).
        Opus at 16 kbps for the direct routes (half the MP3's size); MP3 for Surplus, which prices WAV and MP3 only."""
        self.fmt = "mp3" if self.route == "surplus" else "ogg"
        codec = ["-c:a", "libmp3lame", "-b:a", "32k", "-f", "mp3"] if self.fmt == "mp3" else \
            ["-c:a", "libopus", "-b:a", "16k", "-application", "voip", "-f", "ogg"]
        try:
            self.encoder = await asyncio.create_subprocess_exec(
                "ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0", *codec, "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            self.mp3 = asyncio.ensure_future(self.encoder.stdout.read())  # drained as it goes, so it never blocks
        except OSError:
            self.encoder = None  # no ffmpeg: encode() falls back to WAV at the end

    def feed(self, pcm: bytes) -> None:
        if self.encoder and self.encoder.returncode is None:
            self.encoder.stdin.write(pcm)

    async def finished_mp3(self):
        if not self.encoder:
            return None
        try:
            self.encoder.stdin.close()
            mp3 = await asyncio.wait_for(self.mp3, timeout=3)
            return mp3 or None
        except Exception:
            return None

    @staticmethod
    def encode(pcm: bytes):
        """MP3 at 32 kbps: an eighth of the WAV's size, and the upload is most of the wait (measured through Surplus
        on 10 s of speech: WAV 4.3 s median, MP3 1.6 s). WAV only if ffmpeg is missing. The key market prices both."""
        try:
            mp3 = subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0",
                                  "-c:a", "libmp3lame", "-b:a", "32k", "-f", "mp3", "pipe:1"],
                                 input=pcm, capture_output=True, timeout=5).stdout
            if mp3:
                return mp3, "dictation.mp3", "audio/mpeg"
        except (OSError, subprocess.TimeoutExpired):
            pass
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(pcm)
        return buf.getvalue(), "dictation.wav", "audio/wav"

    def _post(self, pcm: bytes, mp3=None) -> str:
        if mp3 and getattr(self, "fmt", "mp3") == "ogg":
            audio, filename, ctype = mp3, "dictation.ogg", "audio/ogg"
        elif mp3:
            audio, filename, ctype = mp3, "dictation.mp3", "audio/mpeg"
        else:
            audio, filename, ctype = self.encode(pcm)
        if self.route == "deepgram":
            path = self.PATH + "&language=" + ("multi" if setting("language") == "multi" else "en")
            return self._send(path, audio, {"Authorization": f"Token {self.key}", "Content-Type": ctype},
                              lambda d: d["results"]["channels"][0]["alternatives"][0]["transcript"])
        boundary = "utter" + os.urandom(8).hex()
        if self.route == "groq":
            fields = {"model": "whisper-large-v3-turbo", "response_format": "json", "temperature": "0"}
            if setting("language") != "multi":
                fields["language"] = "en"
        else:
            fields = {"model_id": self.MODEL, "tag_audio_events": "false"}
            if setting("language") != "multi":
                fields["language_code"] = "en"
        body = b"".join(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
                        for k, v in fields.items())
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                 f"Content-Type: {ctype}\r\n\r\n").encode() + audio + f"\r\n--{boundary}--\r\n".encode()
        auth = {"Authorization": f"Bearer {self.key}"} if self.route == "groq" else {"xi-api-key": self.key}
        headers = {**auth, "Content-Type": f"multipart/form-data; boundary={boundary}"}
        return self._send(self.PATH, body, headers, lambda d: (d.get("text") or "").strip())

    def _send(self, path: str, body: bytes, headers: dict, text_of) -> str:
        try:
            self.conn.request("POST", path, body, headers)
            resp = self.conn.getresponse()
        except (http.client.RemoteDisconnected, http.client.CannotSendRequest, ConnectionError):
            self.conn.close()  # the warm connection idled out during a long dictation: reopen once
            self.conn = http.client.HTTPSConnection(self.HOST, timeout=BATCH_TIMEOUT_S)
            self.conn.request("POST", path, body, headers)
            resp = self.conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {data[:160]!r}")
        return text_of(json.loads(data))

    async def transcribe(self, pcm: bytes) -> str:
        t = time.monotonic()
        try:
            if self.warming:
                await self.warming
        except Exception:
            pass
        mp3 = await self.finished_mp3()
        try:
            text = (await asyncio.wait_for(asyncio.to_thread(self._post, pcm, mp3), BATCH_TIMEOUT_S)).strip()
        except Exception as exc:
            LOG(f"accurate transcript failed ({str(exc)[:160] or type(exc).__name__}); using the live one")
            return ""
        LOG(f"accurate transcript: {(time.monotonic() - t) * 1000:.0f} ms for {len(pcm) / (2 * RATE):.1f} s of audio")
        return text

    def close(self) -> None:
        if self.conn:
            self.conn.close()
        if self.encoder and self.encoder.returncode is None:
            self.encoder.kill()


# One server connection opened ahead of the next press (keep_warm keeps it fresh); it costs ~1 s through the forward.
WARM = {"key": None, "ws": None, "at": 0.0}
WARM_MAX_AGE_S = 240
WARM_NOW = None  # asyncio.Event: open the next one right away (set after each dictation)


def take_warm(key: str):
    ws, WARM["ws"] = WARM["ws"], None
    if ws and WARM["key"] == key and ws.close_code is None and time.monotonic() - WARM["at"] < WARM_MAX_AGE_S:
        return ws
    if ws:
        asyncio.ensure_future(ws.close())
    return None


async def refresh_server_url() -> None:
    """A temporary address (Cloudflare quick tunnel) changes when the tunnel restarts. Settings "server_url_cmd" is a
    shell command printing the current one; run it after a failed connection, at most once a minute."""
    cmd = setting("server_url_cmd")
    if not cmd or time.monotonic() - WARM.get("url_checked", -1e9) < 60:
        return
    WARM["url_checked"] = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        url = out.decode().strip().splitlines()[-1].replace("https://", "wss://").rstrip("/") + "/stream"
    except Exception as exc:
        LOG(f"server: could not look up the current address ({str(exc)[:100] or type(exc).__name__})")
        return
    if url.startswith("wss://") and url != setting("server_url"):
        save_setting("server_url", url)
        LOG(f"server: address changed, now {url}")


async def keep_warm() -> None:
    """While the server route is chosen, hold one open, checked connection with the current settings."""
    failed = False
    while True:
        try:
            await asyncio.wait_for(WARM_NOW.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        WARM_NOW.clear()
        if DICTATION["running"]:
            continue
        want = setting("accurate") and setting("accurate_route") == "server"
        srv = ServerStream() if want else None
        ws = WARM["ws"]
        if ws and srv and WARM["key"] == srv.endpoint() and ws.close_code is None \
                and time.monotonic() - WARM["at"] < WARM_MAX_AGE_S:
            try:  # still answering? (a dropped forward can leave a socket that looks open)
                await asyncio.wait_for(await ws.ping(), timeout=3)
                continue
            except Exception:
                pass
        if ws and WARM["ws"] is ws:
            WARM["ws"] = None
            asyncio.ensure_future(ws.close())
        if not srv or not srv.token:
            continue
        try:
            key = srv.endpoint()
            new = await srv.connect()
            if DICTATION["running"] or WARM["ws"]:
                await new.close()  # a press started meanwhile; the next round opens a fresh one
                continue
            WARM.update(key=key, ws=new, at=time.monotonic())
            failed = False
        except Exception as exc:
            if not failed:
                LOG(f"server: could not pre-open a connection ({str(exc)[:100] or type(exc).__name__})")
            failed = True
            await refresh_server_url()


class ServerStream:
    """'Utter server' route: stream Opus to your server WHILE you speak (so the upload is done when you stop), then
    "end"; the server runs Scribe v2 + cleanup next to the providers and replies with the finished text. Any failure
    returns None and the dictation falls back to the live transcript and local cleanup."""
    OPUS = ["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0", "-c:a", "libopus",
            "-b:a", "16k", "-application", "voip", "-frame_duration", "20", "-page_duration", "20000",
            "-flush_packets", "1", "-f", "ogg", "pipe:1"]

    def __init__(self) -> None:
        self.url = setting("server_url") or "ws://127.0.0.1:18191/stream"
        try:
            with open(os.path.join(CONFIG, "server.token")) as f:
                self.token = f.read().strip()
        except OSError:
            self.token = ""
        self.ws, self.encoder, self.pump, self.sent = None, None, None, 0
        # where the server sends the work: transcription groq (Whisper) / direct (Scribe) / surplus; cleanup groq / surplus
        self.stt = setting("server_stt") or "groq"
        # off: transcript only. "server_cleanup" picks the server's provider apart from the app's own fallback cleaner
        self.cleanup = (setting("server_cleanup") or setting("cleanup_provider") or "groq") if setting("cleanup") else "none"
        self.ended = asyncio.Event()  # "end" is on the wire; the live transcript may follow it

    def endpoint(self) -> str:
        language = "multi" if setting("language") == "multi" else "en"
        script = "devanagari" if setting("hindi") == "devanagari" else "roman"  # how Hindi/Hinglish should be written
        return f"{self.url}?language={language}&stt={self.stt}&cleanup={self.cleanup}&script={script}"

    def connect(self):
        return websockets.connect(self.endpoint(), additional_headers={"Authorization": f"Bearer {self.token}"},
                                  max_size=None, open_timeout=8)

    async def start(self) -> None:
        warm = take_warm(self.endpoint())  # opened in advance, so a press never waits ~1 s for the connection
        LOG("server: using the pre-opened connection" if warm else "server: no warm connection, connecting now")
        connecting = asyncio.ensure_future(self.connect()) if not warm else None
        self.encoder = await asyncio.create_subprocess_exec(*self.OPUS, stdin=asyncio.subprocess.PIPE,
                                                            stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.DEVNULL)

        async def pump():  # encoder output -> server, as it is produced
            self.ws = warm or await connecting
            audio = bytearray()  # kept so a warm socket that turns out dead can be replaced without losing words
            while True:
                chunk = await self.encoder.stdout.read(4096)
                if not chunk:
                    return
                audio.extend(chunk)
                try:
                    await self.ws.send(chunk)
                except websockets.ConnectionClosed:
                    LOG("server: connection dropped, reconnecting and resending the audio so far")
                    self.ws = await self.connect()
                    await self.ws.send(bytes(audio))
                self.sent += len(chunk)

        self.pump = asyncio.ensure_future(pump())

    def feed(self, pcm: bytes) -> None:
        if self.encoder and self.encoder.returncode is None:
            self.encoder.stdin.write(pcm)

    async def finish(self):
        t = time.monotonic()
        try:
            self.encoder.stdin.close()
            await asyncio.wait_for(self.pump, timeout=5)  # the last Opus pages are out
            t_flushed = time.monotonic()
            await self.ws.send("end")
            self.ended.set()
            reply = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=BATCH_TIMEOUT_S))
            t_reply = time.monotonic()
        except Exception as exc:
            LOG(f"server route failed ({str(exc)[:150] or type(exc).__name__}); using the live transcript")
            return None
        if reply.get("error") or not reply.get("text"):
            LOG(f"server route returned no text ({reply.get('error', 'empty')}); using the live transcript")
            return None
        LOG(f"server route: {(time.monotonic() - t) * 1000:.0f} ms after the stop ({self.sent // 1024} KB Opus streamed;"
            f" server stt {reply.get('stt_ms')} ms + cleanup {reply.get('cleanup_ms')} ms"
            f"{', head start used' if reply.get('head_start') else ''}) | encoder flush {(t_flushed - t) * 1000:.0f} ms,"
            f" end->reply {(t_reply - t_flushed) * 1000:.0f} ms of which server {reply.get('total_ms')} ms")
        return reply

    async def send_live(self, text: str) -> None:
        """Give the server the live transcript for a head-start cleanup (used if Scribe hears the same words)."""
        try:
            await asyncio.wait_for(self.ended.wait(), timeout=5)
            await self.ws.send(json.dumps({"live": text}))
        except Exception:
            pass  # only a speed-up: the server cleans Scribe's text either way

    async def close(self) -> None:
        if self.encoder and self.encoder.returncode is None:
            self.encoder.kill()
        if self.pump and not self.pump.done():
            self.pump.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass


class Cleaner:
    """Sends a finished dictation to a fast language model to clean it. The HTTPS connection is opened the moment
    you press the shortcut, so the TLS handshake is done while you speak and the request leaves at once when you stop.
    Any failure, timeout or suspicious answer falls back to the raw transcript: cleanup can never lose your words."""

    def __init__(self, provider: str) -> None:
        self.host, self.path, default_model, extra = CLEANUP_PROVIDERS.get(provider, CLEANUP_PROVIDERS["surplus"])
        self.model = setting("cleanup_model") or default_model
        try:
            with open(os.path.join(CONFIG, f"{provider}.key")) as f:
                self.key = f.read().strip()
        except OSError:
            self.key = ""
        self.conn, self.warming = None, None
        self.extra = MODEL_EXTRA.get(self.model, {})  # e.g. turning a reasoning model's thinking off or down

    def warm(self) -> None:
        self.conn = http.client.HTTPSConnection(self.host, timeout=CLEANUP_TIMEOUT_S)
        self.warming = asyncio.ensure_future(asyncio.to_thread(self.conn.connect))

    def _ask(self, text: str) -> str:
        try:
            return self._request(text)
        except (http.client.RemoteDisconnected, http.client.CannotSendRequest, ConnectionError) as exc:
            # the provider closed the connection opened at the press (it idles out on long dictations): reopen once
            LOG(f"cleanup connection was closed ({type(exc).__name__}); retrying on a fresh one")
            self.conn.close()
            self.conn = http.client.HTTPSConnection(self.host, timeout=CLEANUP_TIMEOUT_S)
            return self._request(text)

    def _request(self, text: str) -> str:
        body = json.dumps({
            # generous: a reasoning model's hidden thinking counts against this limit too
            "model": self.model, "temperature": 0, "max_tokens": 2 * len(text.split()) + 512,
            "messages": [{"role": "system", "content": CLEANUP_PROMPT}, {"role": "user", "content": text}],
            # streamed: the reply flows as it is written, and a marketplace like Surplus answers before it settles the
            # charge instead of after (measured: 821 -> 676 ms median, worst of 8 runs 3.7 -> 1.2 s)
            "stream": True,
            **self.extra,
        })
        self.conn.request("POST", self.path, body, {"Authorization": f"Bearer {self.key}",
                                                    "Content-Type": "application/json"})
        resp = self.conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {resp.read()[:160]!r}")
        parts = []
        for raw in resp:  # server-sent events, one "data: {...}" line per chunk
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                parts.append(json.loads(payload)["choices"][0]["delta"].get("content") or "")
            except (ValueError, KeyError, IndexError):
                continue  # keep-alive comments, usage-only chunks, reasoning-only deltas
        resp.read()  # drain, so the connection is clean
        # the whole reply is checked before anything is inserted, so a bad answer can still fall back to your words
        return "".join(parts)

    async def clean(self, text: str) -> str:
        self.ok = False  # set only when a real cleanup came back (fallbacks return the input unchanged)
        t = time.monotonic()
        try:
            if self.warming:
                await self.warming  # usually long done; a failed warm-up just reconnects inside _ask
        except Exception:
            pass
        try:
            out = (await asyncio.wait_for(asyncio.to_thread(self._ask, text), CLEANUP_TIMEOUT_S)).strip().strip('"')
        except Exception as exc:  # timeout, network, auth, bad reply: type what was said
            LOG(f"cleanup failed ({str(exc) or type(exc).__name__}); typing the raw text")
            return text
        before, after = len(text.split()), len(out.split())
        if not out:  # an empty reply would paste nothing: keep what was said
            LOG("cleanup came back empty; typing the raw text")
            return text
        LOG(f"cleanup: {(time.monotonic() - t) * 1000:.0f} ms, {before} -> {after} words")
        self.ok = True
        return out

    def close(self) -> None:
        if self.conn:
            self.conn.close()


AB_QUEUE = os.path.join(CONFIG, "ab_queue.json")  # a blind A/B test: one route per dictation, popped in order
ROUTE = {"override": None}


def take_ab_route():
    """Pop the next route ("server" or "local") if an A/B test is queued, else None."""
    try:
        with open(AB_QUEUE) as f:
            queue = json.load(f)
    except (OSError, ValueError):
        return None
    if not queue:
        return None
    route, rest = queue[0], queue[1:]
    with open(AB_QUEUE, "w") as f:
        json.dump(rest, f)
    return route


def give_back_ab_route(route) -> None:
    """A dictation that produced no text does not use up its slot, so the pairs stay intact."""
    try:
        with open(AB_QUEUE) as f:
            queue = json.load(f)
    except (OSError, ValueError):
        queue = []
    with open(AB_QUEUE, "w") as f:
        json.dump([route] + queue, f)


def accurate_route():
    return ROUTE["override"] or setting("accurate_route")


# What Whisper says when it hears nothing. Matched on the whole transcript, never on part of a real sentence.
GHOSTS = {"thank you", "thank you for watching", "thanks for watching", "thank you very much", "thanks", "you", "bye",
          "okay", "ok", "so", "uh", "um", "hmm", "please subscribe", "subtitles by the amara org community"}


def is_ghost(text: str, popup) -> bool:
    """Discard only a known silence filler, or a very short result from near-silent audio. A real sentence from a quiet
    mic must never be thrown away (that lost a whole dictation once), so loudness alone decides nothing."""
    words = "".join(c.lower() if c.isalnum() or c == " " else " " for c in text).split()
    if not words or " ".join(words) in GHOSTS:
        return True
    return len(words) <= 2 and not popup.heard_speech()


def mic_args() -> list:
    """The mic chosen in Settings, if it is still plugged in; otherwise the system default (no --device)."""
    try:
        with open(SETTINGS) as f:
            mic = json.load(f).get("mic")
    except (OSError, ValueError):
        return []
    if not mic:
        return []
    try:
        present = subprocess.run(["pactl", "list", "sources", "short"], capture_output=True, text=True,
                                 timeout=2).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        present = []
    if mic not in present:
        LOG(f"chosen mic {mic!r} not found; using the system default")
        return []
    return [f"--device={mic}"]


def setting(name: str):
    try:
        with open(SETTINGS) as f:
            return json.load(f).get(name)
    except (OSError, ValueError):
        return None


def mode() -> str:
    try:
        with open(SETTINGS) as f:
            return "once" if json.load(f).get("mode") == "once" else "live"
    except (OSError, ValueError):
        return "live"


def keep(text: str, raw: str = "") -> None:
    """Copy the whole dictation to the clipboard (so Ctrl+V recovers it if no field had focus) and log it."""
    if not text:
        return
    try:
        subprocess.run(["wl-copy", "--", text], timeout=3, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    os.makedirs(DATA, exist_ok=True)
    with open(os.open(HISTORY, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as f:  # private: only you
        entry = {"ts": time.time(), "text": text}
        if raw and raw != text:
            entry["raw"] = raw  # what was actually said, before AI cleanup
        f.write(json.dumps(entry) + "\n")


POPUP = None
DICTATION = {"running": False, "cancelled": False}
HOTKEY = "<Alt>d"
GNOME_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
UTTER_KEY_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/utter/"


def save_setting(key: str, value) -> None:
    cfg = {}
    try:
        with open(SETTINGS) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    cfg[key] = value
    os.makedirs(CONFIG, mode=0o700, exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(json.dumps(cfg))
    os.replace(tmp, SETTINGS)


def gset(*args) -> str:
    return subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=5).stdout.strip()


def ensure_hotkey() -> None:
    """First run on GNOME: bind Alt+D to `utter`, unless Utter already has a shortcut or Alt+D is taken.
    Runs once per user; change it later in Settings > Keyboard > Custom Shortcuts."""
    try:
        with open(SETTINGS) as f:
            if json.load(f).get("hotkey_checked"):
                return
    except (OSError, ValueError):
        pass
    try:
        if not shutil.which("gsettings") or GNOME_KEYS not in gset("list-schemas"):
            save_setting("hotkey_checked", True)
            return
        raw = gset("get", GNOME_KEYS, "custom-keybindings")
        if not raw.startswith(("[", "@as [")):
            return  # could not read the list: never overwrite the user's shortcuts with a guess
        paths = ast.literal_eval(raw.removeprefix("@as "))
        for path in paths:
            command = gset("get", f"{GNOME_KEYS}.custom-keybinding:{path}", "command").strip("'")
            binding = gset("get", f"{GNOME_KEYS}.custom-keybinding:{path}", "binding").strip("'")
            if os.path.basename(command.split()[0] if command else "") == "utter":
                save_setting("hotkey_checked", True)
                return  # already bound
            if binding.lower() == HOTKEY.lower():
                notify("Alt+D is already used by another shortcut. Bind any key to the command 'utter' in "
                       "Settings > Keyboard > Custom Shortcuts.")
                save_setting("hotkey_checked", True)
                return
        # the service's PATH often lacks ~/.local/bin, so look where the two installs put the command
        command = next((c for c in (shutil.which("utter"), os.path.expanduser("~/.local/bin/utter"), "/usr/bin/utter")
                        if c and os.access(c, os.X_OK)), None)
        if command is None:
            LOG("no 'utter' command found to bind")
            return
        node = f"{GNOME_KEYS}.custom-keybinding:{UTTER_KEY_PATH}"
        gset("set", node, "name", "Utter: start or stop dictation")
        gset("set", node, "command", command)
        gset("set", node, "binding", HOTKEY)
        if UTTER_KEY_PATH not in paths:
            gset("set", GNOME_KEYS, "custom-keybindings", str(paths + [UTTER_KEY_PATH]))
        notify("Press Alt+D to start dictating, and again to stop.")
        save_setting("hotkey_checked", True)
        LOG("bound Alt+D")
    except (OSError, ValueError, SyntaxError, subprocess.TimeoutExpired) as exc:
        LOG(f"could not set up the shortcut: {exc}")


def should_run() -> bool:
    """With "Start at login" off, a start at login exits quietly; a start from Alt+D or the launcher runs."""
    marker = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "utter-started-by-hand")
    by_hand = os.path.exists(marker)
    if by_hand:
        os.remove(marker)
    return by_hand or setting("autostart") is not False


async def main() -> None:
    global POPUP
    if not should_run():
        LOG("Start at login is off; exiting until you press the shortcut")
        return
    POPUP = Popup()
    ensure_hotkey()
    if not KEY:
        POPUP.open("keys")  # first run: ask for the Deepgram key straight away
    toggle = asyncio.Event()
    presses = {"n": 0}  # presses not yet acted on: one Event cannot tell "start" from "start, then stop"

    def on_press() -> None:
        presses["n"] += 1
        toggle.set()

    asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, on_press)
    def on_cancel() -> None:  # hold-to-talk: Alt + another key after recording began
        if DICTATION["running"] and not DICTATION["cancelled"]:
            DICTATION["cancelled"] = True
            LOG("cancelled by another key while Alt was held")
            on_press()  # stop it; nothing is pasted

    asyncio.get_running_loop().add_signal_handler(signal.SIGRTMIN, on_cancel)
    for _ in EARLY_PRESSES:  # presses that arrived while it was still starting
        on_press()
    EARLY_PRESSES.clear()
    asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, reload_key)
    global WARM_NOW
    WARM_NOW = asyncio.Event()
    asyncio.ensure_future(keep_warm())
    asyncio.get_running_loop().add_signal_handler(signal.SIGUSR2, lambda: POPUP.open("history"))
    ws = None
    while True:
        if ws is None:
            global KEY_CHANGED
            KEY_CHANGED = False
            try:
                if not KEY and setting("stt") not in ("elevenlabs", "assemblyai"):
                    raise RuntimeError("no Deepgram API key saved")
                ws = await connect()
            except Exception as exc:
                LOG(f"connect failed: {exc}")
                try:  # a press while we cannot connect gets an answer instead of silence
                    await asyncio.wait_for(toggle.wait(), timeout=5)
                    toggle.clear()
                    presses["n"] = 0  # answered here; must not count toward the next start
                    service = {"elevenlabs": "ElevenLabs", "assemblyai": "AssemblyAI"}.get(setting("stt"), "Deepgram")
                    if "API key saved" in str(exc) or "401" in str(exc) or "403" in str(exc):
                        notify(f"Add your {service} API key to start dictating.")
                        POPUP.open("keys")
                    else:
                        notify(f"Can't reach {service} yet ({exc}). Retrying…"[:200])
                except asyncio.TimeoutError:
                    pass
                continue
        # never clear here: a press made while reconnecting or finishing the last session must still count
        try:
            pressed = await idle(ws, toggle)
        except websockets.ConnectionClosed:
            LOG("socket closed while idle; reconnecting")
            ws = None
            continue
        if not pressed:
            LOG("refreshing idle socket")
            await ws.close()
            ws = None
            continue
        pressed_at = time.monotonic()
        waiting, presses["n"] = presses["n"], 0
        toggle.clear()  # consume the start press
        if waiting % 2 == 0:  # started and stopped while it was still connecting: nothing to record
            LOG(f"{waiting} presses arrived before it was ready; start and stop cancel out")
            continue
        stop = asyncio.Event()
        watcher = asyncio.create_task(toggle.wait())

        def on_stop(task):
            if not task.cancelled():
                toggle.clear()  # consume the stop press; a later press queues the next session
                presses["n"] = max(0, presses["n"] - 1)
                stop.set()

        watcher.add_done_callback(on_stop)
        try:
            ws = await dictate(ws, stop, pressed_at)
        except Exception as exc:
            LOG(f"session error: {exc}")
            POPUP.message("Something went wrong. What was heard is in History.")
            notify(f"Failed: {exc}"[:200])
            ws = None  # unknown state: reconnect
        watcher.cancel()
        if ws is not None and (ws.close_code is not None or KEY_CHANGED):
            try:
                await ws.close()
            except Exception:
                pass
            ws = None
        # otherwise keep the socket: Finalize leaves it open, so the next press needs no reconnect


if __name__ == "__main__":
    asyncio.run(main())
