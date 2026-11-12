#!/usr/bin/env python3
"""Utter: push-to-talk dictation for Linux, on Deepgram's live speech-to-text.

Keeps a WebSocket to Deepgram open so a press starts streaming at once, types the words into the focused window
as they arrive (or all at once when you stop), and copies every dictation to the clipboard and History.

An idle socket gets a KeepAlive every KEEPALIVE_S (Deepgram closes one after ~10 s of silence) and is replaced
every REFRESH_S. Signals: SIGUSR1 starts/stops a dictation, SIGHUP re-reads the API key, SIGUSR2 opens the window.
Runs as the systemd user service `utter`; the `utter` command sends the signals.
"""
import array, ast, asyncio, json, math, os, shutil, signal, subprocess, time

import websockets

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hinglish import romanize  # noqa: E402  (lives next to this file)

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



async def connect():
    t = time.monotonic()
    # "multi" follows you between English and Hindi mid-sentence; "en" is English only (the default)
    language = "multi" if setting("language") == "multi" else "en"
    ws = await websockets.connect(f"{URL}&language={language}", additional_headers={"Authorization": f"Token {KEY}"},
                                  max_size=None)
    LOG(f"warm socket ready ({language}) in {(time.monotonic() - t) * 1000:.0f} ms")
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
    roman = setting("hindi") != "devanagari"  # Hindi as Roman Hinglish unless Settings says Devanagari
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
                if roman:
                    text = romanize(text)  # Hindi in Roman letters, as typed in chats
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
            if n % 2 == 0:  # 25 level updates a second
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
        LOG("mic levels: " + popup.summary())
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
