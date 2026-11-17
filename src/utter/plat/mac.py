"""macOS: one pynput keyboard listener does the shortcut (Option+D by default, swallowed so it never types "∂") and
hold-to-talk (Option alone). Pastes with Cmd+V. Start at login is a LaunchAgent. Needs Accessibility, Input
Monitoring and Microphone, which macOS asks for the first time."""
import os, subprocess, sys, time
from xml.sax.saxutils import escape

from .desktop import Hold, LOG, list_mics, notify, open_mic, press_paste, read_setting, save_setting  # noqa: F401

CONFIG = DATA = os.path.expanduser("~/Library/Application Support/Utter")
PASTE_KEYS = "Cmd+V"
HOLD_KEY = "Option"
HOLD_NOTE = "Option with any other key never starts it."
DEFAULT_SHORTCUT = "alt+d"
PLIST = os.path.expanduser("~/Library/LaunchAgents/ai.utter.app.plist")
# macOS virtual key codes (ANSI layout), so Option+D is matched by the key, not by the "∂" it would type
VK = {"a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
      "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26,
      "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46, "space": 49}
NAME_OF_VK = {v: k for k, v in VK.items()}


def copy(text: str) -> None:
    # launchd starts apps without LANG, and pbcopy then garbles anything that is not ASCII (Devanagari included)
    subprocess.run(["pbcopy"], input=text.encode(), env={**os.environ, "LANG": "en_US.UTF-8"}, check=False)


def paste(text: str) -> None:
    from pynput.keyboard import Key
    copy(text)
    time.sleep(0.01)
    press_paste(Key.cmd)


def play(path: str) -> None:
    try:
        subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def current_shortcut():
    return read_setting(CONFIG, "hotkey") or DEFAULT_SHORTCUT


def set_shortcut(combo: str) -> str:
    save_setting(CONFIG, "hotkey", combo)  # the listener reads it at each key press
    return ""


def _mod(key):
    from pynput.keyboard import Key
    for name, keys in (("alt", (Key.alt, Key.alt_l, Key.alt_r)), ("ctrl", (Key.ctrl, Key.ctrl_l, Key.ctrl_r)),
                       ("shift", (Key.shift, Key.shift_l, Key.shift_r)), ("super", (Key.cmd, Key.cmd_l, Key.cmd_r))):
        if key in keys:
            return name
    return None


def _name(key) -> str:
    vk = getattr(key, "vk", None)
    if vk in NAME_OF_VK:
        return NAME_OF_VK[vk]
    return getattr(key, "name", "") or ""  # Key.f5 -> "f5"


def _check_permission() -> None:
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}):
            notify("Allow Utter in System Settings > Privacy & Security > Accessibility and Input Monitoring, "
                   "then quit and reopen Utter.")
    except Exception as exc:
        LOG(f"could not check the Accessibility permission: {exc}")


def start_keys(on_press, on_cancel) -> None:
    from pynput import keyboard
    _check_permission()
    hold = Hold(CONFIG, on_press, on_cancel)
    held, swallow = set(), [False]

    def down(key, injected=False):
        if injected:
            return  # Utter's own Cmd+V
        mod = _mod(key)
        if mod:
            held.add(mod)
            hold.key_down() if mod == "alt" else hold.other_down()
            return
        hold.other_down()
        *mods, name = current_shortcut().split("+")
        if name == _name(key) and set(mods) == held:
            swallow[0] = True  # runs before intercept() for the same event
            on_press()

    def up(key, injected=False):
        if injected:
            return
        mod = _mod(key)
        if mod:
            held.discard(mod)
            if mod == "alt":
                hold.key_up()

    def intercept(_event_type, event):
        if swallow[0]:
            swallow[0] = False
            return None  # the shortcut never reaches the app (Option+D would type "∂")
        return event

    keyboard.Listener(on_press=down, on_release=up, darwin_intercept=intercept, daemon=True).start()


def should_run() -> bool:
    return True  # the LaunchAgent exists only while Start at login is on


def set_autostart(on: bool) -> None:
    if not getattr(sys, "frozen", False):
        return  # only the installed app starts at login, never a dev checkout
    if not on:
        try:
            os.remove(PLIST)
        except OSError:
            pass
        return
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "w") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.utter.app</string>
  <key>ProgramArguments</key><array><string>{escape(sys.executable)}</string><string>--background</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
""")


def quit() -> bool:
    return False  # the app quits itself
