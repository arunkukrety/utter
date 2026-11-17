"""What macOS and Windows share: the mic through PortAudio (sounddevice), pasting through pynput, settings,
and hold-to-talk (hold the hold key alone, speak, release) with the same rules as the Linux helper utter-hold.py."""
import asyncio, json, os, threading, time

RATE, BLOCK = 16000, 320  # 20 ms of 16-bit mono per read, as the core expects
LOG = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)
NOTIFY = [lambda msg: LOG(f"notice: {msg}")]  # replaced by the tray icon's balloon once the UI is up


def notify(msg: str) -> None:
    NOTIFY[0](msg)


def settings_path(config: str) -> str:
    return os.path.join(config, "settings.json")


def read_setting(config: str, name: str):
    try:
        with open(settings_path(config)) as f:
            return json.load(f).get(name)
    except (OSError, ValueError):
        return None


def save_setting(config: str, key: str, value) -> None:
    path = settings_path(config)
    cfg = {}
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    cfg[key] = value
    os.makedirs(config, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(cfg))
    os.replace(tmp, path)


# ---- mic ---------------------------------------------------------------------------------------------------------
def _input_devices():
    """Input devices of the default host API only (Windows lists each mic once per API: MME, DirectSound, WASAPI)."""
    import sounddevice as sd
    try:
        default_api = sd.query_devices(kind="input")["hostapi"]
    except Exception:
        default_api = None
    return [(i, d["name"]) for i, d in enumerate(sd.query_devices())
            if d["max_input_channels"] > 0 and (default_api is None or d["hostapi"] == default_api)]


def list_mics() -> list:
    try:
        return [(name, name) for _i, name in _input_devices()]
    except Exception:
        return []


class Mic:
    def __init__(self, mic) -> None:
        import sounddevice as sd
        loop = asyncio.get_running_loop()
        self.q, self.stopped = asyncio.Queue(), False

        def callback(data, _frames, _t, _status):
            loop.call_soon_threadsafe(self.q.put_nowait, bytes(data))

        device = None
        if mic:
            device = next((i for i, name in _input_devices() if name == mic), None)
            if device is None:
                LOG(f"chosen mic {mic!r} not found; using the system default")
        self.stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK, device=device,
                                        callback=callback)
        self.stream.start()

    async def read(self, n: int):
        """The next 20 ms of audio, or None once the mic has stopped."""
        return await self.q.get()

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
        self.q.put_nowait(None)


async def open_mic(mic) -> Mic:
    return Mic(mic)


# ---- typing --------------------------------------------------------------------------------------------------------
_kb = []


def keyboard():
    if not _kb:
        from pynput.keyboard import Controller
        _kb.append(Controller())
    return _kb[0]


def press_paste(modifier) -> None:
    kb = keyboard()
    with kb.pressed(modifier):
        kb.press("v")
        kb.release("v")


# ---- hold-to-talk --------------------------------------------------------------------------------------------------
HOLD_S = 0.5


def hold_seconds(config: str) -> float:
    try:
        return min(3.0, max(0.2, float(read_setting(config, "hold_seconds") or HOLD_S)))
    except (TypeError, ValueError):
        return HOLD_S


class Hold:
    """Hold the hold key alone for the hold time (Settings, default 0.5 s) to start, release to stop.
      - held alone for the hold time    -> start
      - released after it started       -> stop and paste
      - any other key while it is held  -> before the start: nothing (Alt+Tab stays normal); after: cancel
    Events Utter injected itself (its own Ctrl+V or typing) are ignored, like the Linux helper skips ydotool's keyboard."""

    def __init__(self, config: str, on_press, on_cancel, on_start=None) -> None:
        self.config, self.on_press, self.on_cancel, self.on_start = config, on_press, on_cancel, on_start
        self.down_at, self.started, self.spoiled, self.timer = None, False, False, None
        self.lock = threading.Lock()

    def enabled(self) -> bool:
        return read_setting(self.config, "hold_to_talk") is not False

    def key_down(self) -> None:
        with self.lock:
            if self.down_at is not None:
                return  # auto-repeat
            self.down_at, self.started, self.spoiled = time.monotonic(), False, False
            self.timer = threading.Timer(hold_seconds(self.config), self._fire, args=(self.down_at,))
            self.timer.daemon = True
            self.timer.start()

    def _fire(self, down_at) -> None:
        with self.lock:
            if self.down_at != down_at or self.spoiled or self.started:
                return
            self.started = True
            if not self.enabled():
                self.spoiled = True  # switched off: behave as if nothing happened
                return
        if self.on_start:
            self.on_start()
        self.on_press()  # start

    def key_up(self) -> None:
        with self.lock:
            fire = self.started and not self.spoiled
            if self.timer:
                self.timer.cancel()
            self.down_at, self.started, self.spoiled, self.timer = None, False, False, None
        if fire:
            self.on_press()  # stop and paste

    def other_down(self) -> None:
        with self.lock:
            if self.down_at is None or self.spoiled:
                return
            self.spoiled = True  # the hold key + another key: a shortcut, not hold-to-talk
            cancel = self.started
        if cancel:
            self.on_cancel()  # cancel the dictation already running
