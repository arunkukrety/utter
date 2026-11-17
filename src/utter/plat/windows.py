"""Windows: the shortcut is a system hotkey (RegisterHotKey, so the app in front never sees Alt+D), hold-to-talk is a
pynput listener watching Alt alone, pasting is Ctrl+V, start at login is the Run registry key.

Windows opens an app's menu bar when Alt is pressed and released with nothing in between that it saw, which would
swallow the paste. So when Utter takes an Alt press for itself it sends an unassigned key (0xE8, as AutoHotkey does)
while Alt is still down, and the release no longer counts as an Alt tap."""
import ctypes, os, sys, threading, time

from .desktop import Hold, LOG, keyboard, list_mics, notify, open_mic, press_paste, read_setting, save_setting  # noqa: F401

CONFIG = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Utter")
DATA = os.path.join(os.environ.get("LOCALAPPDATA") or CONFIG, "Utter")
PASTE_KEYS = "Ctrl+V"
HOLD_KEY = "Alt"
HOLD_NOTE = "Alt with any other key (Alt+Tab, Alt+D) never starts it."
DEFAULT_SHORTCUT = "alt+d"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "super": 0x8}
MOD_NOREPEAT, WM_HOTKEY, WM_QUIT = 0x4000, 0x0312, 0x0012


def copy(text: str) -> None:
    import pyperclip
    try:
        pyperclip.copy(text)
    except Exception as exc:
        LOG(f"clipboard: {exc}")


def paste(text: str) -> None:
    from pynput.keyboard import Key
    copy(text)
    time.sleep(0.01)
    press_paste(Key.ctrl)


def play(path: str) -> None:
    import winsound
    try:
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except RuntimeError:
        pass


def mask_alt() -> None:
    from pynput.keyboard import KeyCode
    kb = keyboard()
    kb.press(KeyCode.from_vk(0xE8))
    kb.release(KeyCode.from_vk(0xE8))


def _vk(name: str):
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    if name[:1] == "f" and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        return 0x70 + int(name[1:]) - 1
    return {"space": 0x20}.get(name)


class Hotkey(threading.Thread):
    """One system hotkey, registered and served on its own thread (RegisterHotKey belongs to the registering thread)."""

    def __init__(self, combo: str, on_fire) -> None:
        super().__init__(daemon=True)
        self.combo, self.on_fire, self.ok, self.ready, self.tid = combo, on_fire, False, threading.Event(), None

    def run(self) -> None:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        self.tid = ctypes.windll.kernel32.GetCurrentThreadId()
        *mods, name = self.combo.split("+")
        vk = _vk(name)
        flags = MOD_NOREPEAT
        for m in mods:
            flags |= MODS.get(m, 0)
        self.ok = bool(vk) and bool(user32.RegisterHotKey(None, 1, flags, vk))
        self.ready.set()
        if not self.ok:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                if "alt" in mods:
                    mask_alt()
                self.on_fire()
        user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        if self.tid:
            ctypes.windll.user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)


HOTKEY = {"thread": None, "on_press": None}


def current_shortcut():
    return read_setting(CONFIG, "hotkey") or DEFAULT_SHORTCUT


def _register(combo: str) -> bool:
    old = HOTKEY["thread"]
    if old:
        old.stop()
        old.join(timeout=1)
    t = Hotkey(combo, HOTKEY["on_press"])
    t.start()
    t.ready.wait(2)
    HOTKEY["thread"] = t if t.ok else None
    return t.ok


def set_shortcut(combo: str) -> str:
    if not _register(combo):
        _register(current_shortcut())  # put the old one back
        return "That key is already used by Windows or another app. Pick another."
    save_setting(CONFIG, "hotkey", combo)
    return ""


def start_keys(on_press, on_cancel) -> None:
    from pynput import keyboard as kb
    from pynput.keyboard import Key
    HOTKEY["on_press"] = on_press
    if not _register(current_shortcut()) and not read_setting(CONFIG, "hotkey_checked"):
        notify("Alt+D is already used by another app. Pick a shortcut in Utter's Settings.")
    save_setting(CONFIG, "hotkey_checked", True)
    alts = (Key.alt, Key.alt_l, Key.alt_r, Key.alt_gr)
    hold = Hold(CONFIG, on_press, on_cancel, on_start=mask_alt)

    def down(key, injected=False):
        if injected:
            return  # Utter's own Ctrl+V and the 0xE8 mask
        hold.key_down() if key in alts else hold.other_down()

    def up(key, injected=False):
        if not injected and key in alts:
            hold.key_up()

    kb.Listener(on_press=down, on_release=up, daemon=True).start()


def should_run() -> bool:
    return True  # the Run entry exists only while Start at login is on


def set_autostart(on: bool) -> None:
    if not getattr(sys, "frozen", False):
        return  # only the installed app starts at login, never a dev checkout
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, "Utter", 0, winreg.REG_SZ, f'"{sys.executable}" --background')
        else:
            try:
                winreg.DeleteValue(key, "Utter")
            except FileNotFoundError:
                pass


def quit() -> bool:
    return False  # the app quits itself
