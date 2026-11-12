#!/usr/bin/env python3
"""Utter: push-to-talk dictation for Linux, on Deepgram's live speech-to-text.

Keeps a WebSocket to Deepgram open so a press starts streaming at once, types the words into the focused window
as they arrive (or all at once when you stop), and copies every dictation to the clipboard and History.

An idle socket gets a KeepAlive every KEEPALIVE_S (Deepgram closes one after ~10 s of silence) and is replaced
every REFRESH_S. Signals: SIGUSR1 starts/stops a dictation, SIGHUP re-reads the API key, SIGUSR2 opens the window.
Runs as the systemd user service `utter`; the `utter` command sends the signals.
"""
import array, asyncio, json, math, os, signal, subprocess, time

import websockets

RATE, CHUNK = 16000, 640  # 20 ms of 16-bit mono
FAST = "3"  # ms per key hold and between keys (ydotool defaults are 20+20); raise if an app drops letters
os.environ.setdefault("YDOTOOL_SOCKET", f"/run/user/{os.getuid()}/.ydotool_socket")


class Typer:
    """Keeps what has been typed for the current segment and edits it toward the newest text."""

    def __init__(self) -> None:
        self.shown = ""

    def update(self, text: str, final: bool = False) -> None:
        if not final:  # word by word: hold back the last, still-forming word
            cut = text.rfind(" ")
            text = text[:cut + 1] if cut >= 0 else ""
        common = os.path.commonprefix([self.shown, text])
        erase, add = len(self.shown) - len(common), text[len(common):]
        if erase:
            subprocess.run(["ydotool", "key", "-d", FAST, *(["14:1", "14:0"] * erase)], stderr=subprocess.DEVNULL)
        if add:
            subprocess.run(["ydotool", "type", "-d", FAST, "-H", FAST, "--", add], stderr=subprocess.DEVNULL)
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
KEY_CHANGED = False  # set by SIGHUP (the API keys page): reconnect with the new key


def reload_key() -> None:
    global KEY, KEY_CHANGED
    try:
        KEY = read_key()
        KEY_CHANGED = True
        LOG("key reloaded")
    except OSError as exc:
        LOG(f"key reload failed: {exc}")
URL = ("wss://api.deepgram.com/v1/listen?model=nova-3&language=en&encoding=linear16&sample_rate=16000&channels=1"
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
        if self.proc is None or self.proc.poll() is not None:
            self._spawn()
        self._send("S")
        play("start.wav")

    def stopped(self, wait_for_text: bool) -> None:
        """The stop press: chime now; hide now, or show the loader until hide() once the text is typed."""
        play("stop.wav")
        self._send("P" if wait_for_text else "H")

    def hide(self) -> None:
        self._send("H")

    def open(self, page: str) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self._spawn()
        self._send(f"W {page}")

    def reconnecting(self) -> None:
        self._send("R")

    def listening(self) -> None:
        self._send("A")

    def level(self, pcm: bytes) -> None:
        samples = array.array("h", pcm)
        rms = math.sqrt(sum(x * x for x in samples) / len(samples))
        self._send(f"L {min(1.0, rms / 3000) ** 0.6:.3f}")  # 3000 RMS ~ normal speech at a laptop mic

    def _send(self, line: str) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
            except OSError:
                pass



async def connect():
    t = time.monotonic()
    ws = await websockets.connect(URL, additional_headers={"Authorization": f"Token {KEY}"}, max_size=None)
    LOG(f"warm socket ready in {(time.monotonic() - t) * 1000:.0f} ms")
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
                return False  # reconnect now with the new key
            await ws.send(json.dumps({"type": "KeepAlive"}))
    return False


STALL_S = 6  # Deepgram answers about once a second, even in silence; this long without a word means a dead socket


async def dictate(ws, stop: asyncio.Event, pressed_at: float):
    """One dictation. Returns the socket to keep for the next press (a new one if it had to reconnect), or None."""
    rec = await asyncio.create_subprocess_exec(
        "parecord", "--raw", f"--rate={RATE}", "--channels=1", "--format=s16le", "--latency-msec=20", *mic_args(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    typer = Typer()
    popup = POPUP
    popup.begin()
    said = []  # final segments, for the clipboard and history
    live = mode() == "live"  # "once": type nothing until the dictation ends, then type it all
    conn = {"ws": ws, "heard": time.monotonic(), "receiver": None}

    async def receive(sock):
        try:
            async for raw in sock:
                conn["heard"] = time.monotonic()
                msg = json.loads(raw)
                if msg.get("type") != "Results":
                    if msg.get("type") not in ("Metadata", "SpeechStarted", "UtteranceEnd"):
                        LOG(f"{msg.get('type')}: {str(msg)[:150]}")
                    continue
                text = msg["channel"]["alternatives"][0]["transcript"]
                LOG(f"{'final' if msg.get('is_final') else 'interim'} {text!r}"[:160])
                if msg.get("is_final"):
                    if live:
                        typer.commit(text)
                    if text.strip():
                        said.append(text.strip())
                    # the answer to our Finalize ends the session; a late one from the previous session must not
                    if msg.get("from_finalize") and stop.is_set():
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

    async def send_audio():
        first, n = True, 0
        while not stop.is_set():
            try:
                data = await asyncio.wait_for(rec.stdout.readexactly(CHUNK), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except asyncio.IncompleteReadError:
                break
            try:
                if time.monotonic() - conn["heard"] > STALL_S:
                    raise ConnectionError(f"no reply for {STALL_S} s")
                await conn["ws"].send(data)
            except (websockets.ConnectionClosed, OSError) as exc:
                if not await reconnect(str(exc) or type(exc).__name__):
                    stop.set()
                    break
                await conn["ws"].send(data)
            n += 1
            if n % 3 == 0:  # ~16 level updates a second
                popup.level(data)
            if first:
                first = False
                LOG(f"press -> first audio on the wire: {(time.monotonic() - pressed_at) * 1000:.0f} ms")
        try:
            await conn["ws"].send(json.dumps({"type": "Finalize"}))  # flush the last words as a final result
        except (websockets.ConnectionClosed, OSError):
            pass

    conn["receiver"] = asyncio.create_task(receive(ws))
    sender = asyncio.create_task(send_audio())
    try:
        await stop.wait()
        popup.stopped(wait_for_text=not live)  # live: hide on the press; all at once: loader until it is typed
        rec.terminate()
        await sender
        try:
            await asyncio.wait_for(conn["receiver"], timeout=3)
        except asyncio.TimeoutError:
            conn["receiver"].cancel()
        if not live and said:
            typer.commit(" ".join(said))
    finally:
        if rec.returncode is None:
            rec.terminate()
        popup.hide()
        keep(" ".join(said))  # always: even a failed dictation keeps what was heard
    return conn["ws"] if conn["ws"].close_code is None else None


HISTORY = os.path.join(DATA, "history.jsonl")
SETTINGS = os.path.join(CONFIG, "settings.json")  # written by the window's Settings page


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


def mode() -> str:
    try:
        with open(SETTINGS) as f:
            return "once" if json.load(f).get("mode") == "once" else "live"
    except (OSError, ValueError):
        return "live"


def keep(text: str) -> None:
    """Copy the whole dictation to the clipboard (so Ctrl+V recovers it if no field had focus) and log it."""
    if not text:
        return
    try:
        subprocess.run(["wl-copy", "--", text], timeout=3, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    os.makedirs(DATA, exist_ok=True)
    with open(os.open(HISTORY, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as f:  # private: only you
        f.write(json.dumps({"ts": time.time(), "text": text}) + "\n")


POPUP = None


async def main() -> None:
    global POPUP
    POPUP = Popup()
    toggle = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, toggle.set)
    asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, reload_key)
    asyncio.get_running_loop().add_signal_handler(signal.SIGUSR2, lambda: POPUP.open("history"))
    ws = None
    while True:
        if ws is None:
            global KEY_CHANGED
            KEY_CHANGED = False
            try:
                if not KEY:
                    raise RuntimeError("no Deepgram API key saved")
                ws = await connect()
            except Exception as exc:
                LOG(f"connect failed: {exc}")
                try:  # a press while we cannot connect gets an answer instead of silence
                    await asyncio.wait_for(toggle.wait(), timeout=5)
                    toggle.clear()
                    if not KEY or "401" in str(exc):
                        notify("Add your Deepgram API key to start dictating.")
                        POPUP.open("keys")
                    else:
                        notify(f"Can't reach Deepgram yet ({exc}). Retrying…"[:200])
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
        stop = asyncio.Event()
        toggle.clear()  # consume the start press
        watcher = asyncio.create_task(toggle.wait())

        def on_stop(task):
            if not task.cancelled():
                toggle.clear()  # consume the stop press; a later press queues the next session
                stop.set()

        watcher.add_done_callback(on_stop)
        try:
            ws = await dictate(ws, stop, pressed_at)
        except Exception as exc:
            LOG(f"session error: {exc}")
            POPUP.hide()  # never leave the pill or its loader on screen after a failure
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
