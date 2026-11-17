"""Everything that differs between Linux, macOS and Windows, behind one set of names.

Each module provides: CONFIG, DATA, copy, paste, play, notify, list_mics, open_mic, start_keys,
current_shortcut, set_shortcut, should_run, set_autostart, quit, PASTE_KEYS, HOLD_KEY, HOLD_NOTE.
Shortcuts are kept in one neutral form everywhere: "alt+d", "ctrl+shift+f5".
"""
import os, sys

if sys.platform == "darwin":
    from .mac import *  # noqa: F401,F403
elif sys.platform == "win32":
    from .windows import *  # noqa: F401,F403
else:
    from .linux import *  # noqa: F401,F403

# Bundled files (sounds, icons): inside the PyInstaller bundle when frozen, else the repo checkout.
RES = getattr(sys, "_MEIPASS", None) or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def resource(*parts: str) -> str:
    return os.path.join(RES, *parts)


MOD_NAMES = {"darwin": {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "super": "⌘"}}.get(
    sys.platform, {"ctrl": "Ctrl+", "alt": "Alt+", "shift": "Shift+", "super": "Super+" if sys.platform != "win32" else "Win+"})


def shortcut_label(combo: str) -> str:
    """"alt+d" -> "Alt+D" (macOS: "⌥D")."""
    if not combo:
        return "None"
    *mods, key = combo.split("+")
    return "".join(MOD_NAMES.get(m, m) for m in mods) + (key.upper() if len(key) == 1 else key.capitalize())


def ffmpeg() -> str:
    """The ffmpeg bundled with the app (imageio-ffmpeg), else the one on PATH."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


# Windows opens a console window for every child process of a windowed app unless told not to
NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
