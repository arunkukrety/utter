"""Linux (GNOME on Wayland): parecord for the mic, wl-copy + ydotool to paste and type, GNOME custom shortcuts for the
hotkey (it runs the `utter` command, which signals this process), a systemd user service for start at login."""
import ast, asyncio, json, os, re, shutil, subprocess, time

CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "utter")
DATA = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "utter")
SETTINGS = os.path.join(CONFIG, "settings.json")
RATE = 16000
PASTE_SETTLE_S = 0.01  # clipboard margin before Ctrl+V
PASTE_KEY_DELAY = "5"  # ms between the Ctrl+V key events (ydotool's default is ~12)
os.environ.setdefault("YDOTOOL_SOCKET", f"/run/user/{os.getuid()}/.ydotool_socket")
PASTE_KEYS = "Ctrl+V"
HOLD_KEY = "Alt"
HOLD_NOTE = ("Alt with any other key (Alt+Tab, Alt+D) never starts it. Needs the one-time helper: "
             "sudo /usr/lib/utter/install-hold.sh")
LOG = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)


def _setting(name: str):
    try:
        with open(SETTINGS) as f:
            return json.load(f).get(name)
    except (OSError, ValueError):
        return None


def _save_setting(key: str, value) -> None:
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


def copy(text: str) -> None:
    try:
        subprocess.run(["wl-copy", "--", text], stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


def paste(text: str) -> None:
    """Insert text at once: put it on the clipboard and press Ctrl+V. Instant at any length, where typing takes
    a few ms per character. Terminals paste with Ctrl+Shift+V, so there it lands on the clipboard only."""
    copy(text)
    # wl-copy returns once the clipboard is set; the short wait is a margin for apps slow to see a new clipboard.
    # Was 50 ms with default key delays (~120 ms in all); raise PASTE_SETTLE_S if an app pastes the old clipboard
    time.sleep(PASTE_SETTLE_S)
    subprocess.run(["ydotool", "key", "-d", PASTE_KEY_DELAY, "29:1", "47:1", "47:0", "29:0"],
                   stderr=subprocess.DEVNULL)  # Ctrl+V


def play(path: str) -> None:
    try:
        subprocess.Popen(["paplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def notify(msg: str) -> None:
    try:
        os.spawnlp(os.P_NOWAIT, "notify-send", "notify-send", "-t", "4000", "-i", "utter", "Utter", msg)
    except OSError:
        pass


def list_mics() -> list:
    """Input devices as (id, description), monitors of speakers left out."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True, timeout=3).stdout
        return [(m["name"], m.get("description") or m["name"]) for m in json.loads(out)
                if not m["name"].endswith(".monitor")]
    except (OSError, ValueError, subprocess.TimeoutExpired, KeyError):
        return []


def _mic_args(mic) -> list:
    """The mic chosen in Settings, if it is still plugged in; otherwise the system default (no --device)."""
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


class Mic:
    def __init__(self, proc) -> None:
        self.proc = proc

    async def read(self, n: int):
        """n bytes of 16 kHz mono s16le, or None once the mic has stopped."""
        try:
            return await self.proc.stdout.readexactly(n)
        except asyncio.IncompleteReadError:
            return None

    def stop(self) -> None:
        if self.proc.returncode is None:
            self.proc.terminate()


async def open_mic(mic) -> Mic:
    proc = await asyncio.create_subprocess_exec(
        "parecord", "--raw", f"--rate={RATE}", "--channels=1", "--format=s16le", "--latency-msec=20", *_mic_args(mic),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    return Mic(proc)


# ---- the keyboard shortcut lives in GNOME's custom shortcuts; it runs `utter`, which signals this process ----------
HOTKEY = "<Alt>d"
GNOME_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
UTTER_KEY_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/utter/"
GNOME_MODS = {"<Primary>": "ctrl", "<Control>": "ctrl", "<Ctrl>": "ctrl", "<Alt>": "alt", "<Shift>": "shift",
              "<Super>": "super"}


def _gset(*args) -> str:
    return subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=5).stdout.strip()


def _to_neutral(accel: str) -> str:
    """'<Alt>d' -> 'alt+d'"""
    key = re.sub(r"<\w+>", "", accel).lower()
    if not key:
        return ""
    mods = {GNOME_MODS.get(f"<{m.capitalize()}>", m.lower()) for m in re.findall(r"<(\w+)>", accel)}
    order = ["ctrl", "alt", "shift", "super"]
    return "+".join(sorted(mods, key=lambda m: order.index(m) if m in order else 9) + [key])


def _to_gnome(combo: str) -> str:
    """'alt+d' -> '<Alt>d', 'ctrl+f5' -> '<Primary>F5'"""
    *mods, key = combo.split("+")
    names = {"ctrl": "<Primary>", "alt": "<Alt>", "shift": "<Shift>", "super": "<Super>"}
    key = key.upper() if key[:1] == "f" and key[1:].isdigit() else key
    return "".join(names[m] for m in mods) + key


def _command() -> str:
    # the service's PATH often lacks ~/.local/bin, so look where the installs put the command
    return next((c for c in (shutil.which("utter"), os.path.expanduser("~/.local/bin/utter"), "/usr/bin/utter")
                 if c and os.access(c, os.X_OK)), "utter")


def _is_utter(command: str) -> bool:
    """The dictation shortcut's command: `utter` alone (`utter meeting` is the meeting one)."""
    return bool(command) and len(command.split()) == 1 and os.path.basename(command.split()[0]) == "utter"


MEETING_HOTKEY = "<Alt>m"
MEETING_KEY_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/utter-meeting/"


def _bind_meeting_key() -> None:
    """Once per user on GNOME: Alt+M runs `utter meeting` (start or stop meeting notes), unless Alt+M is taken."""
    if _setting("meeting_hotkey_checked"):
        return
    entries, command = _entries(), _command()
    if entries is None or command == "utter":
        return  # not GNOME, or no installed command to bind: try again next start
    taken = next((n or c for _p, b, c, n in entries if b.lower() == MEETING_HOTKEY.lower()
                  and c.split()[-1:] != ["meeting"]), None)
    if taken:
        notify(f"Alt+M is already used by {taken}. Bind any key to 'utter meeting' in Settings > Keyboard.")
    elif not any(p == MEETING_KEY_PATH for p, *_ in entries):
        node = f"{GNOME_KEYS}.custom-keybinding:{MEETING_KEY_PATH}"
        _gset("set", node, "name", "Utter: start or stop meeting notes")
        _gset("set", node, "command", f"{command} meeting")
        _gset("set", node, "binding", MEETING_HOTKEY)
        _gset("set", GNOME_KEYS, "custom-keybindings", str([e[0] for e in entries] + [MEETING_KEY_PATH]))
        notify("Press Alt+M to start meeting notes, and again to stop.")
        LOG("bound Alt+M")
    _save_setting("meeting_hotkey_checked", True)


def _entries():
    """GNOME's custom shortcuts as [(path, binding, command, name)], or None when they can't be read (not GNOME)."""
    try:
        raw = _gset("get", GNOME_KEYS, "custom-keybindings")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not raw.startswith(("[", "@as [")):
        return None
    out = []
    for path in ast.literal_eval(raw.removeprefix("@as ")):
        node = f"{GNOME_KEYS}.custom-keybinding:{path}"
        out.append((path, _gset("get", node, "binding").strip("'"), _gset("get", node, "command").strip("'"),
                    _gset("get", node, "name").strip("'")))
    return out


def current_shortcut():
    """Utter's shortcut ('alt+d'), '' if none is set, or None if this isn't GNOME (set it in the desktop's settings)."""
    entries = _entries()
    if entries is None:
        return None
    return _to_neutral(next((b for _p, b, c, _n in entries if _is_utter(c)), ""))


def set_shortcut(combo: str) -> str:
    """Bind combo to Utter. Returns an error message, or '' on success. Never rewrites a list it could not read."""
    accel = _to_gnome(combo)
    entries = _entries()
    if entries is None:
        return "Couldn't read your keyboard shortcuts."
    clash = next((n or c for _p, b, c, n in entries if b.lower() == accel.lower() and not _is_utter(c)), None)
    if clash:
        return f"That key is already used by another shortcut: {clash}."
    path = next((p for p, _b, c, _n in entries if _is_utter(c)), None)
    if path is None:  # no entry yet: add one, appended to the list as read
        path = UTTER_KEY_PATH
        _gset("set", GNOME_KEYS, "custom-keybindings", str([e[0] for e in entries] + [path]))
        _gset("set", f"{GNOME_KEYS}.custom-keybinding:{path}", "name", "Utter: start or stop dictation")
        _gset("set", f"{GNOME_KEYS}.custom-keybinding:{path}", "command", _command())
    _gset("set", f"{GNOME_KEYS}.custom-keybinding:{path}", "binding", accel)
    return ""


def start_keys(on_press, on_cancel) -> None:
    """Presses arrive as signals (wired in __main__). First run on GNOME: bind Alt+D to `utter`, unless Utter already
    has a shortcut or Alt+D is taken. Runs once per user; change it later in Settings. Alt+M (meeting notes) likewise."""
    try:
        _bind_meeting_key()
    except (OSError, ValueError, SyntaxError, subprocess.TimeoutExpired) as exc:
        LOG(f"could not set up the meeting shortcut: {exc}")
    if _setting("hotkey_checked"):
        return
    try:
        if not shutil.which("gsettings") or GNOME_KEYS not in _gset("list-schemas"):
            _save_setting("hotkey_checked", True)
            return
        raw = _gset("get", GNOME_KEYS, "custom-keybindings")
        if not raw.startswith(("[", "@as [")):
            return  # could not read the list: never overwrite the user's shortcuts with a guess
        paths = ast.literal_eval(raw.removeprefix("@as "))
        for path in paths:
            command = _gset("get", f"{GNOME_KEYS}.custom-keybinding:{path}", "command").strip("'")
            binding = _gset("get", f"{GNOME_KEYS}.custom-keybinding:{path}", "binding").strip("'")
            if _is_utter(command):
                _save_setting("hotkey_checked", True)
                return  # already bound
            if binding.lower() == HOTKEY.lower():
                notify("Alt+D is already used by another shortcut. Bind any key to the command 'utter' in "
                       "Settings > Keyboard > Custom Shortcuts.")
                _save_setting("hotkey_checked", True)
                return
        command = _command()
        if command == "utter":
            LOG("no 'utter' command found to bind")
            return
        node = f"{GNOME_KEYS}.custom-keybinding:{UTTER_KEY_PATH}"
        _gset("set", node, "name", "Utter: start or stop dictation")
        _gset("set", node, "command", command)
        _gset("set", node, "binding", HOTKEY)
        if UTTER_KEY_PATH not in paths:
            _gset("set", GNOME_KEYS, "custom-keybindings", str(paths + [UTTER_KEY_PATH]))
        notify("Press Alt+D to start dictating, and again to stop.")
        _save_setting("hotkey_checked", True)
        LOG("bound Alt+D")
    except (OSError, ValueError, SyntaxError, subprocess.TimeoutExpired) as exc:
        LOG(f"could not set up the shortcut: {exc}")


def should_run() -> bool:
    """With "Start at login" off, a start at login exits quietly; a start from Alt+D or the launcher runs."""
    marker = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "utter-started-by-hand")
    by_hand = os.path.exists(marker)
    if by_hand:
        os.remove(marker)
    return by_hand or _setting("autostart") is not False


def set_autostart(on: bool) -> None:
    pass  # the systemd user service always starts; should_run() reads the setting at each login


def quit() -> bool:
    # stopping the service ends this process too; Alt+D starts it again
    subprocess.Popen(["systemctl", "--user", "stop", "utter"])
    return True
