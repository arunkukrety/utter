"""Utter's dictation engine, the same on every platform: a client of the Utter server.

While you speak, the mic is encoded to Opus and streamed to the server; when you stop, the server transcribes and
cleans it (it alone decides which providers do that) and replies with the text, which is pasted where your cursor is,
copied to the clipboard and saved to History. A connection is kept open ahead of the next press, so a dictation never
waits for one. Platform specifics (mic, paste, hotkeys) live in utter.plat; the pill and windows in utter.ui, which
this talks to through Popup with one short text line per update. press(), cancel() and reload_key() may be called from
any thread.
"""
import array, asyncio, json, math, os, threading, time

import websockets

from . import plat
from .hinglish import romanize

RATE, CHUNK = 16000, 640  # 20 ms of 16-bit mono
FFMPEG = plat.ffmpeg()
CONFIG, DATA = plat.CONFIG, plat.DATA
TOKEN_FILE = os.path.join(CONFIG, "server.token")  # the user's Utter key
HISTORY = os.path.join(DATA, "history.jsonl")
SETTINGS = os.path.join(CONFIG, "settings.json")  # written by the window's Settings page
# The Utter server's public address (Tailscale Funnel in front of the VPS; only valid keys get past the handshake).
# "server_url" in settings.json or UTTER_SERVER overrides it, e.g. for an SSH forward.
SERVER_URL = os.environ.get("UTTER_SERVER") or "wss://vint-hill-maus.tail6d5405.ts.net/stream"
REPLY_TIMEOUT_S = 8  # from "end" to the server's text
DUCK_LEVEL = 30  # other audio while you dictate, % of its own volume (Settings: "duck_media", "duck_level")
# What went wrong, in words the user can act on (ServerStream.problem)
PROBLEMS = {
    "offline": "No internet connection. Check your Wi-Fi and try again.",
    "unreachable": "Couldn't reach the Utter server. Try again in a moment.",
    "bad_key": "Your Utter key wasn't accepted. Check it in Settings.",
    "timeout": "Utter took too long to answer. Try again.",
    "server_failed": "Transcription failed on the server. Try again.",
}


def _internet() -> bool:
    """Is there any internet at all? Two well-known public addresses, no DNS needed, 1.5 s at most."""
    import socket
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            socket.create_connection((host, port), timeout=1.5).close()
            return True
        except OSError:
            pass
    return False


async def _diagnose(exc: Exception, waiting_for_reply: bool) -> str:
    """A PROBLEMS key for a failed dictation: the key refused, the internet down, the server down, or too slow."""
    rcvd = getattr(exc, "rcvd", None)  # websockets: the close frame the server sent
    status = getattr(getattr(exc, "response", None), "status_code", None)  # websockets: a refused handshake
    if getattr(rcvd, "code", None) == 4401 or status in (401, 403):
        return "bad_key"
    if waiting_for_reply and isinstance(exc, asyncio.TimeoutError):
        return "timeout"  # the audio went up; the answer did not come back in time
    return "unreachable" if await asyncio.to_thread(_internet) else "offline"
LOG = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)


def setting(name: str):
    try:
        with open(SETTINGS) as f:
            return json.load(f).get(name)
    except (OSError, ValueError):
        return None


def read_token() -> str:
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def notify(msg: str) -> None:
    plat.notify(msg)


def play(name: str) -> None:
    plat.play(plat.resource("sounds", name))


MIN_FLOOR, DB_RANGE = 150, 21  # voice bars: full height at DB_RANGE dB above the room noise (raise for calmer bars)


class Popup:
    """The on-screen pill (utter.ui), driven by one short line per update (see ui.Pill.handle). A broken pill never
    stops dictation."""

    def __init__(self, send) -> None:
        self.send = send  # thread-safe: hands the line to the UI thread
        self.recent, self.heard = [], []

    def begin(self) -> None:
        self.recent = []  # mic levels for the noise floor, per dictation
        self.heard = []  # every level this dictation, for the summary in the log
        self._send("S")
        play("start.wav")

    def stopped(self) -> None:
        """The stop press: chime now, and the loader until hide() once the text is pasted."""
        play("stop.wav")
        self._send("P")

    def summary(self) -> str:
        """Mic levels for this dictation, to tune DB_RANGE against a real voice."""
        if not self.heard:
            return "no audio"
        h = sorted(self.heard)
        floor = max(MIN_FLOOR, h[len(h) // 5] * 1.1)
        loud = h[int(len(h) * 0.9)]
        return (f"room ~{floor:.0f} RMS, your speech ~{loud:.0f} RMS (90th pct) = "
                f"{20 * math.log10(max(loud, 1) / floor):.1f} dB above the room")

    def hide(self) -> None:
        self._send("H")

    def open(self, page: str) -> None:
        self._send(f"W {page}")

    def message(self, text: str) -> None:
        self._send("M " + text.replace("\n", " "))

    def heard_speech(self) -> bool:
        """Was there clearly voice in this dictation (well above the room), even though no words came back?"""
        h = sorted(self.heard or [0])
        floor = max(MIN_FLOOR, h[len(h) // 5] * 1.1)
        return 20 * math.log10(max(h[int(len(h) * 0.9)], 1) / floor) > 8

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
        try:
            self.send(line)
        except Exception:
            pass


# ---- the server connection ------------------------------------------------------------------------------------------
# One connection opened ahead of the next press (keep_warm keeps it fresh); it costs ~1 s through the forward.
WARM = {"key": None, "ws": None, "at": 0.0}
WARM_MAX_AGE_S = 240
WARM_NOW = None  # asyncio.Event: open the next one right away (after each dictation, or a new key or language)


def take_warm(key):
    ws, WARM["ws"] = WARM["ws"], None
    if ws and WARM["key"] == key and ws.close_code is None and time.monotonic() - WARM["at"] < WARM_MAX_AGE_S:
        return ws
    if ws:
        asyncio.ensure_future(ws.close())
    return None


async def keep_warm() -> None:
    """Hold one open, checked connection with the current key, language and script."""
    failed = False
    while True:
        try:
            await asyncio.wait_for(WARM_NOW.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        WARM_NOW.clear()
        if DICTATION["running"]:
            continue
        srv = ServerStream()
        ws = WARM["ws"]
        if ws and WARM["key"] == srv.key() and ws.close_code is None and time.monotonic() - WARM["at"] < WARM_MAX_AGE_S:
            try:  # still answering? (a dropped connection can leave a socket that looks open)
                await asyncio.wait_for(await ws.ping(), timeout=3)
                continue
            except Exception:
                pass
        if ws and WARM["ws"] is ws:
            WARM["ws"] = None
            asyncio.ensure_future(ws.close())
        if not srv.token:
            continue
        try:
            key = srv.key()
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


class ServerStream:
    """Streams Opus to the server WHILE you speak (so the upload is done when you stop), then "end"; the server
    replies with the finished text. Only the user's own preferences go with it: the language and how Hindi is
    written. Which providers transcribe and clean is the server's choice."""
    OPUS = [FFMPEG, "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0", "-c:a", "libopus",
            "-b:a", "16k", "-application", "voip", "-frame_duration", "20", "-page_duration", "20000",
            "-flush_packets", "1", "-f", "ogg", "pipe:1"]

    def __init__(self) -> None:
        self.url = setting("server_url") or SERVER_URL
        self.token = read_token()
        self.ws, self.encoder, self.pump, self.sent, self.problem, self.notice = None, None, None, 0, None, None

    def endpoint(self) -> str:
        language = "multi" if setting("language") == "multi" else "en"
        script = "devanagari" if setting("hindi") == "devanagari" else "roman"  # how Hindi/Hinglish should be written
        return f"{self.url}?language={language}&script={script}"

    def key(self):
        return self.endpoint(), self.token  # a warm connection is only reused with the same key and preferences

    def connect(self):
        return websockets.connect(self.endpoint(), additional_headers={"Authorization": f"Bearer {self.token}"},
                                  max_size=None, open_timeout=8)

    async def start(self) -> None:
        warm = take_warm(self.key())  # opened in advance, so a press never waits ~1 s for the connection
        LOG("server: using the pre-opened connection" if warm else "server: no warm connection, connecting now")
        connecting = asyncio.ensure_future(self.connect()) if not warm else None
        self.encoder = await asyncio.create_subprocess_exec(*self.OPUS, stdin=asyncio.subprocess.PIPE,
                                                            stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.DEVNULL, **plat.NO_WINDOW)

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
        """The server's reply ({"text", "raw", ...}), or None. self.problem (a PROBLEMS key) says why, when it is not
        simply that nothing was heard; self.notice is a sentence from the server itself (a limit, a disabled key)."""
        t = time.monotonic()
        waiting_for_reply = False
        try:
            self.encoder.stdin.close()
            await asyncio.wait_for(self.pump, timeout=5)  # the last Opus pages are out
            t_flushed = time.monotonic()
            await self.ws.send("end")
            waiting_for_reply = True
            reply = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=REPLY_TIMEOUT_S))
            t_reply = time.monotonic()
        except Exception as exc:
            LOG(f"server failed ({str(exc)[:150] or type(exc).__name__})")
            self.problem = await _diagnose(exc, waiting_for_reply)
            return None
        if reply.get("error") or not reply.get("text"):
            LOG(f"server returned no text ({reply.get('error', 'empty')})")
            self.notice = reply.get("notice")  # something the user must see, e.g. the daily limit
            if reply.get("error") and not self.notice:
                self.problem = "server_failed"
            return None
        LOG(f"server: {(time.monotonic() - t) * 1000:.0f} ms after the stop ({self.sent // 1024} KB Opus streamed;"
            f" server stt {reply.get('stt_ms')} ms + cleanup {reply.get('cleanup_ms')} ms) | encoder flush"
            f" {(t_flushed - t) * 1000:.0f} ms, end->reply {(t_reply - t_flushed) * 1000:.0f} ms of which server"
            f" {reply.get('total_ms')} ms")
        return reply

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


def keep(text: str, raw: str = "") -> None:
    """Copy the whole dictation to the clipboard (so it can be pasted again if no field had focus) and save it."""
    if not text:
        return
    plat.copy(text)
    os.makedirs(DATA, exist_ok=True)
    with open(os.open(HISTORY, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a", encoding="utf-8") as f:
        entry = {"ts": time.time(), "text": text}
        if raw and raw != text:
            entry["raw"] = raw  # what was actually said, before cleanup
        f.write(json.dumps(entry) + "\n")


# ---- one dictation --------------------------------------------------------------------------------------------------
POPUP = None
DICTATION = {"running": False, "cancelled": False}


async def dictate(stop: asyncio.Event, pressed_at: float) -> None:
    popup = POPUP
    server = ServerStream()
    if not server.token:
        popup.message("Add your Utter key in Settings to start dictating.")
        popup.open("settings")
        return
    DICTATION.update(running=True, cancelled=False)
    # other audio (music, a video, a call) is turned down while you talk, and back up the moment recording stops
    ducker = None  # glides other audio down now and back up on release(), on its own thread: nothing here waits for it
    if setting("duck_media") is not False and hasattr(plat, "Ducker"):
        level = (setting("duck_level") if isinstance(setting("duck_level"), (int, float)) else DUCK_LEVEL) / 100
        ducker = plat.Ducker(level)
    rec = await plat.open_mic(setting("mic"))
    popup.begin()
    roman = setting("hindi") != "devanagari"  # Hindi as Roman Hinglish unless Settings says Devanagari
    text = raw = None
    mic_off = asyncio.Event()
    audio_chunks = {"n": 0}

    def restore_audio() -> None:
        if ducker is not None:
            ducker.release()

    async def send_audio():
        first, n = True, 0
        while not mic_off.is_set():
            try:
                data = await asyncio.wait_for(rec.read(CHUNK), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if data is None:
                break
            server.feed(data)
            n += 1
            audio_chunks["n"] = n
            if n % 2 == 0:  # 25 level updates a second
                popup.level(data)
            if first:
                first = False
                LOG(f"press -> first audio: {(time.monotonic() - pressed_at) * 1000:.0f} ms")

    try:
        await server.start()  # connects in the background (or takes the warm one); audio is encoded meanwhile
        sender = asyncio.create_task(send_audio())
        await stop.wait()
        stopped_at = time.monotonic()
        LOG("mic levels: " + popup.summary())
        popup.stopped()
        mic_off.set()
        rec.stop()
        restore_audio()  # music starts gliding back now, not when the text arrives
        await sender
        # whatever the server heard is pasted, however short: a filter for Whisper's invented "Thank you." threw away
        # real one- and two-word dictations ("Yeah.", "Hey man.") on short clips in a noisy room
        served = None if DICTATION["cancelled"] else await server.finish()
        if served:
            raw = romanize(served["raw"]) if roman else served["raw"]
            text = romanize(served["text"]) if roman else served["text"]
            if not DICTATION["cancelled"]:
                plat.paste(text.strip() + " ")
            LOG(f"stop -> text: {(time.monotonic() - stopped_at) * 1000:.0f} ms")
    finally:
        rec.stop()
        restore_audio()  # also after a cancel or an error
        await server.close()
        WARM_NOW.set()  # open the next connection now, not at the next press
        cancelled = DICTATION["cancelled"]
        if cancelled:
            text = raw = None  # nothing pasted, nothing saved
        DICTATION["running"] = False
        if text:
            popup.hide()
        elif cancelled:
            popup.message("Cancelled")
        elif server.notice:
            popup.message(server.notice)
        elif server.problem:
            popup.message(PROBLEMS[server.problem])
        elif audio_chunks["n"] == 0:
            popup.message("No sound from the microphone. Check your mic in Settings.")
        elif setting("language") != "multi" and popup.heard_speech():
            popup.message("Didn't catch that. Speaking Hindi? Turn on English + Hindi in Settings.")
        else:
            popup.message("Didn't catch that. Try again.")
        keep(text, raw)


# ---- presses --------------------------------------------------------------------------------------------------------
# Presses come from other threads (hotkey listeners, signal handlers): they are handed to the loop, or queued until
# it is running so a press made while Utter is still starting is not lost.
LOOP = None
EARLY = []  # "press" / "cancel" before the loop is up
HANDLERS = {}
EARLY_LOCK = threading.Lock()


def _call(kind: str) -> None:
    with EARLY_LOCK:
        if LOOP is None:
            EARLY.append(kind)
            return
    LOOP.call_soon_threadsafe(HANDLERS[kind])


def press() -> None:
    _call("press")


def cancel() -> None:
    _call("cancel")


def reload_key() -> None:
    """A new key, language or script was saved: reconnect with it before the next press."""
    if LOOP is not None and WARM_NOW is not None:
        LOOP.call_soon_threadsafe(WARM_NOW.set)


def run(send_line) -> None:
    """The engine's thread: runs until the app exits. send_line(line) hands a pill update to the UI thread."""
    global POPUP
    POPUP = Popup(send_line)
    asyncio.run(main())


async def main() -> None:
    global LOOP, WARM_NOW
    if not read_token():
        POPUP.open("settings")  # first run: ask for the Utter key straight away
    toggle = asyncio.Event()
    presses = {"n": 0}  # presses not yet acted on: one Event cannot tell "start" from "start, then stop"

    def on_press() -> None:
        presses["n"] += 1
        toggle.set()

    def on_cancel() -> None:  # Alt+X (`utter cancel`), or on hold-to-talk another key while the hold key is down
        if DICTATION["running"] and not DICTATION["cancelled"]:
            DICTATION["cancelled"] = True
            LOG("dictation cancelled")
            on_press()  # stop it; nothing is pasted

    WARM_NOW = asyncio.Event()
    HANDLERS.update(press=on_press, cancel=on_cancel)
    with EARLY_LOCK:
        LOOP = asyncio.get_running_loop()
        for kind in EARLY:  # presses that arrived while it was still starting
            HANDLERS[kind]()
        EARLY.clear()
    await asyncio.to_thread(plat.start_keys, press, cancel)
    asyncio.ensure_future(keep_warm())
    while True:
        await toggle.wait()
        pressed_at = time.monotonic()
        waiting, presses["n"] = presses["n"], 0
        toggle.clear()  # consume the start press
        if waiting % 2 == 0:  # started and stopped before it could begin: nothing to record
            LOG(f"{waiting} presses arrived together; start and stop cancel out")
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
            await dictate(stop, pressed_at)
        except Exception as exc:
            LOG(f"session error: {exc}")
            POPUP.message("Something went wrong. Try again.")
        watcher.cancel()
