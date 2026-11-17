#!/usr/bin/env python3
"""Utter for macOS (trial): hold Right Option, speak, let go, and the cleaned text is pasted where your cursor is.

Same server as the Linux app: Opus is streamed to the Utter server WHILE you speak, "end" is sent when you let go, and
the server replies with the transcribed and cleaned text. The text also stays on the clipboard, so nothing is lost if
no text box had focus (press Cmd+V).

Setup (once):
    brew install ffmpeg
    pip3 install sounddevice pynput websockets rumps
    echo '<server token>' > ~/.utter-token && chmod 600 ~/.utter-token
    # reach the server like the Linux laptop does (keep this running in another terminal):
    ssh -N -i ~/.ssh/vps_tierhive -p 7169 -L 18191:127.0.0.1:8191 root@147.135.16.160
Run:
    python3 utter_mac.py
macOS asks for Microphone, Accessibility and Input Monitoring for your terminal app the first time; allow all three,
then quit and rerun.

Settings come from the environment: UTTER_SERVER (ws://127.0.0.1:18191/stream), UTTER_LANGUAGE (multi | en),
UTTER_STT (openrouter), UTTER_CLEANUP (cerebras | groq | none), UTTER_SCRIPT (roman | devanagari), UTTER_KEY (alt_r).
Test without a mic or hotkey:  python3 utter_mac.py --file some.wav
"""
from __future__ import annotations  # macOS ships Python 3.9

import json, os, queue, subprocess, sys, threading, time

RATE = 16000
SERVER = os.environ.get("UTTER_SERVER", "ws://127.0.0.1:18191/stream")
PARAMS = {"language": os.environ.get("UTTER_LANGUAGE", "multi"), "stt": os.environ.get("UTTER_STT", "openrouter"),
          "cleanup": os.environ.get("UTTER_CLEANUP", "cerebras"), "script": os.environ.get("UTTER_SCRIPT", "roman")}
KEY_NAME = os.environ.get("UTTER_KEY", "alt_r")  # Right Option
MIN_HOLD_S = 0.3   # shorter presses are accidental taps, not dictations
REPLY_TIMEOUT_S = 10
# the same encoder settings as the Linux app: small Ogg pages so audio leaves while you are still speaking
OPUS = ["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0", "-c:a", "libopus",
        "-b:a", "16k", "-application", "voip", "-frame_duration", "20", "-page_duration", "20000",
        "-flush_packets", "1", "-f", "ogg", "pipe:1"]


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def token() -> str:
    for path in ("~/.utter-token", "~/.config/utter/server.token"):  # the second is where the Linux app keeps it
        try:
            with open(os.path.expanduser(path)) as f:
                return f.read().strip()
        except OSError:
            pass
    sys.exit("No server token: put it in ~/.utter-token (it is ~/.config/utter/server.token on the Linux laptop)")


def endpoint() -> str:
    return SERVER + "?" + "&".join(f"{k}={v}" for k, v in PARAMS.items())


def connect():
    from websockets.sync.client import connect as ws_connect
    return ws_connect(endpoint(), additional_headers={"Authorization": f"Bearer {TOKEN}"}, max_size=None,
                      open_timeout=8)


class Warm:
    """One connection opened ahead of time, so a press never waits a round trip to the server. The server takes one
    dictation per connection, so a new one is opened after every dictation."""

    def __init__(self) -> None:
        self.ws, self.lock = None, threading.Lock()

    def refill(self) -> None:
        def run():
            try:
                ws = connect()
            except Exception as exc:
                log(f"could not pre-open a server connection: {str(exc)[:120]}")
                return
            with self.lock:
                old, self.ws = self.ws, ws
            if old:
                old.close()
        threading.Thread(target=run, daemon=True).start()

    def take(self):
        with self.lock:
            ws, self.ws = self.ws, None
        return ws


class Dictation:
    """PCM in (from the mic callback) -> ffmpeg Opus -> server, all while speaking; finish() returns the reply."""

    def __init__(self, warm: Warm) -> None:
        self.ws = warm.take()
        self.warm_used = self.ws is not None
        self.enc = subprocess.Popen(OPUS, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.pcm = queue.Queue()
        self.sent, self.error = 0, None
        self.writer = threading.Thread(target=self._write, daemon=True)
        self.pump = threading.Thread(target=self._pump, daemon=True)
        self.writer.start(); self.pump.start()

    def feed(self, pcm: bytes) -> None:  # called from the audio thread: never block it
        self.pcm.put(pcm)

    def _write(self) -> None:
        while (chunk := self.pcm.get()) is not None:
            try:
                self.enc.stdin.write(chunk)
            except (BrokenPipeError, ValueError):
                return
        self.enc.stdin.close()

    def _pump(self) -> None:
        try:
            if self.ws is None:
                self.ws = connect()  # no warm connection: connect now, while the user is still speaking
            audio = bytearray()
            while chunk := self.enc.stdout.read1(4096):
                audio.extend(chunk)
                try:
                    self.ws.send(chunk)
                except Exception:  # a warm socket that died: reconnect and resend what we have, no words lost
                    log("server connection dropped; reconnecting and resending")
                    self.ws = connect()
                    self.ws.send(bytes(audio))
                self.sent += len(chunk)
        except Exception as exc:
            self.error = exc

    def finish(self) -> dict | None:
        t0 = time.monotonic()
        self.pcm.put(None)
        self.pump.join(timeout=5)
        if self.error or self.ws is None:
            log(f"server route failed: {str(self.error)[:150]}")
            return None
        try:
            self.ws.send("end")
            reply = json.loads(self.ws.recv(timeout=REPLY_TIMEOUT_S))
        except Exception as exc:
            log(f"server route failed: {str(exc)[:150] or type(exc).__name__}")
            return None
        finally:
            # the closing handshake is another round trip to the server: never make the paste wait for it
            threading.Thread(target=self._close, daemon=True).start()
        if reply.get("error"):
            log(f"server error: {reply['error'][:150]}")
            return None
        log(f"stop -> reply {(time.monotonic() - t0) * 1000:.0f} ms ({self.sent // 1024} KB Opus, "
            f"{'warm' if self.warm_used else 'cold'} connection; server stt {reply.get('stt_ms')} ms + "
            f"cleanup {reply.get('cleanup_ms')} ms)")
        return reply

    def _close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def cancel(self) -> None:
        self.pcm.put(None)
        self.enc.kill()
        if self.ws:
            threading.Thread(target=self._close, daemon=True).start()


def paste(text: str) -> None:
    """Clipboard + Cmd+V. The text stays on the clipboard on purpose: if nothing had focus, Cmd+V still has it."""
    subprocess.run(["pbcopy"], input=text.encode(), check=False)
    from pynput.keyboard import Controller, Key
    kb = Controller()
    with kb.pressed(Key.cmd):
        kb.press("v"); kb.release("v")


def sound(name: str) -> None:
    subprocess.Popen(["afplay", "-v", "0.4", f"/System/Library/Sounds/{name}.aiff"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def notify(text: str) -> None:
    subprocess.Popen(["osascript", "-e", f"display notification {json.dumps(text)} with title \"Utter\""])


class App:
    """Hotkey + mic + menu bar. State is read by a main-thread timer: AppKit must only be touched from the main thread."""

    def __init__(self) -> None:
        self.warm = Warm()
        self.warm.refill()
        self.state, self.last = "idle", ""
        self.dictation, self.stream, self.pressed_at = None, None, 0.0
        self.lock = threading.Lock()

    def on_press(self, key) -> None:
        from pynput.keyboard import Key
        if key != getattr(Key, KEY_NAME) or self.state != "idle":
            return
        import sounddevice as sd
        with self.lock:
            self.pressed_at = time.monotonic()
            self.dictation = Dictation(self.warm)
            d = self.dictation
            self.stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=1600,
                                            callback=lambda data, frames, t, status: d.feed(bytes(data)))
            self.stream.start()
            self.state = "recording"
        sound("Tink")

    def on_release(self, key) -> None:
        from pynput.keyboard import Key
        if key != getattr(Key, KEY_NAME) or self.state != "recording":
            return
        with self.lock:
            self.stream.stop(); self.stream.close()
            d, held = self.dictation, time.monotonic() - self.pressed_at
            self.state = "working"
        if held < MIN_HOLD_S:  # a tap, not a dictation
            d.cancel()
            self.state = "idle"
            self.warm.refill()
            return
        sound("Pop")
        threading.Thread(target=self._finish, args=(d,), daemon=True).start()

    def _finish(self, d: Dictation) -> None:
        reply = d.finish()
        self.warm.refill()  # the next press gets a ready connection
        text = (reply or {}).get("text", "").strip()
        if text:
            self.last = text
            paste(text)
        elif reply is None:
            notify("Couldn't reach the Utter server. Is the SSH forward running?")
        self.state = "idle"

    def run(self) -> None:
        import rumps
        from pynput import keyboard
        keyboard.Listener(on_press=self.on_press, on_release=self.on_release, daemon=True).start()
        icons = {"idle": "🎙", "recording": "🔴", "working": "⏳"}
        app = rumps.App("Utter", title=icons["idle"], quit_button="Quit Utter")

        @rumps.clicked("Copy last dictation")
        def copy_last(_):
            if self.last:
                subprocess.run(["pbcopy"], input=self.last.encode(), check=False)

        @rumps.timer(0.1)
        def tick(_):
            if app.title != icons[self.state]:
                app.title = icons[self.state]

        app.menu = ["Copy last dictation"]
        log(f"ready: hold {KEY_NAME} to dictate ({endpoint().split('?')[1]})")
        app.run()


def file_test(path: str) -> None:
    """Stream a recording at speaking pace, exactly as the mic would, and print the reply. No mic, hotkey or GUI."""
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-"],
                         capture_output=True, check=True).stdout
    warm = Warm(); warm.refill(); time.sleep(1.5)
    d = Dictation(warm)
    for k in range(0, len(pcm), 3200):  # 100 ms blocks, in real time
        d.feed(pcm[k:k + 3200]); time.sleep(0.1)
    reply = d.finish()
    print(json.dumps(reply, ensure_ascii=False, indent=1) if reply else "no reply")


TOKEN = token()
if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--file":
        file_test(sys.argv[2])
    else:
        if sys.platform != "darwin":
            sys.exit("This is the macOS trial; on Linux use the regular Utter app (or --file to test the server path).")
        App().run()
