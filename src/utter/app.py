"""Starts Utter: the Qt UI on the main thread, the dictation engine (utter.core) on its own thread.

Linux: run by the systemd user service `utter`; the `utter` command (bin/utter) and the hold-to-talk helper send it
signals: SIGUSR1 starts/stops a dictation, SIGRTMIN cancels one, SIGHUP re-reads the API key, SIGUSR2 opens the window,
SIGRTMIN+1 starts/stops meeting notes (Alt+M), SIGRTMIN+2 pauses/resumes them (Alt+P), SIGRTMIN+3 turns the
interpreter on or off (Alt+I). SIGRTMIN (Alt+X, `utter cancel`) also cancels meeting notes.
macOS / Windows: the hotkeys live in the app. One copy runs; opening the app again shows its window instead.
`--background` (used by start at login) starts it without opening the window.
"""
import os, sys

LINUX = sys.platform.startswith("linux")
EARLY = []  # Linux: presses that land while the slower imports below are still loading; replayed once it is up

if LINUX:
    import signal
    # an unhandled SIGUSR1 kills the process, and an early Alt+D can land before the real handlers are installed
    signal.signal(signal.SIGUSR1, lambda *_: EARLY.append("press"))
    signal.signal(signal.SIGRTMIN, lambda *_: EARLY.append("cancel"))
    signal.signal(signal.SIGRTMIN + 1, signal.SIG_IGN)  # Alt+M before the tray exists: ignored, never fatal
    signal.signal(signal.SIGRTMIN + 2, signal.SIG_IGN)
    signal.signal(signal.SIGRTMIN + 3, signal.SIG_IGN)
    signal.signal(signal.SIGUSR2, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


def _ready() -> None:
    """Tells the `utter` command and the hold helper it is safe to send presses (they wait for this PID)."""
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "utter.ready")
    with open(path, "w") as f:
        f.write(str(os.getpid()))


def _log_to_file(data_dir: str) -> None:
    """A windowed app has no console: keep the log in the data folder (a fresh one past 5 MB)."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "utter.log")
    mode = "w" if os.path.exists(path) and os.path.getsize(path) > 5_000_000 else "a"
    log = open(os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if mode == "w" else os.O_APPEND), 0o600),
               mode, buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr = log


def main() -> None:
    if LINUX:
        _ready()
        if getattr(sys, "frozen", False):
            # PyInstaller points LD_LIBRARY_PATH at its bundled libraries. This process has loaded them already; the
            # programs it runs (gsettings, notify-send, wl-copy, ydotool, ffmpeg) must get the system's own, or they
            # fail on the bundled, older glib (gsettings then silently saved nothing)
            orig = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
            if orig:
                os.environ["LD_LIBRARY_PATH"] = orig
            else:
                os.environ.pop("LD_LIBRARY_PATH", None)
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        import certifi  # the bundled Python has no CA list of its own on a user's Mac
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())

    from . import plat
    if not LINUX or sys.stdout is None:
        _log_to_file(plat.DATA)
    if not plat.should_run():
        print("Start at login is off; exiting until you press the shortcut", flush=True)
        return
    if LINUX:
        # XWayland on purpose: GNOME's Wayland ignores "don't take focus" and window positions for the pill
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    import getpass, threading
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    app.setApplicationName("Utter")
    app.setDesktopFileName("utter")
    app.setQuitOnLastWindowClosed(False)

    server = None
    if not LINUX:
        from PySide6.QtNetwork import QLocalServer, QLocalSocket
        name = "utter-" + getpass.getuser()
        probe = QLocalSocket()
        probe.connectToServer(name)
        if probe.waitForConnected(500):  # already running: show its window and leave
            probe.write(b"open")
            probe.waitForBytesWritten(500)
            return
        QLocalServer.removeServer(name)  # a stale one left by a crash
        server = QLocalServer()
        server.listen(name)

    from . import core, ui
    window = ui.Window()
    pill = ui.Pill(window.open)
    tray = ui.make_tray(window)
    if not LINUX:
        from .plat import desktop
        desktop.NOTIFY[0] = lambda msg: window.later(lambda: tray.showMessage("Utter", msg))
        server.newConnection.connect(lambda: (server.nextPendingConnection(), window.open("history")))
        plat.set_autostart(ui.read_settings().get("autostart") is not False)  # keeps the entry pointing at this copy

    threading.Thread(target=core.run, args=(pill.line.emit,), name="engine", daemon=True).start()

    if LINUX:
        _linux_signals(app, window, core, tray)
    elif "--background" not in sys.argv:
        window.open("history")
    code = app.exec()
    if LINUX:  # the interpreter's virtual microphone goes, and the user's own comes back
        from .interpret import INTERPRETER
        INTERPRETER.stop()
    # Leave at once: a normal interpreter shutdown tears Qt down while the engine thread is still running, which
    # segfaults. History and settings are written as they happen, so only the log needs flushing.
    sys.stdout.flush()
    os._exit(code)


def _linux_signals(app, window, core, tray) -> None:
    """Python runs signal handlers only when it gets control, and Qt's loop holds it in C: a wake-up socket makes Qt
    hand control back the moment a signal lands."""
    import signal, socket
    from PySide6.QtCore import QSocketNotifier
    rsock, wsock = socket.socketpair()
    rsock.setblocking(False)
    wsock.setblocking(False)
    signal.set_wakeup_fd(wsock.fileno())

    def drain() -> None:
        try:
            rsock.recv(512)
        except OSError:
            pass

    notifier = QSocketNotifier(rsock.fileno(), QSocketNotifier.Read, app)
    notifier.activated.connect(drain)
    app._signal_socks = (rsock, wsock, notifier)  # keep them alive
    signal.signal(signal.SIGUSR1, lambda *_: core.press())
    signal.signal(signal.SIGRTMIN, lambda *_: (core.cancel(), tray.cancel_meeting()))  # Alt+X: `utter cancel`
    signal.signal(signal.SIGHUP, lambda *_: core.reload_key())
    signal.signal(signal.SIGUSR2, lambda *_: window.open("history"))
    signal.signal(signal.SIGRTMIN + 1, lambda *_: tray.toggle_meeting())  # Alt+M: `utter meeting`
    signal.signal(signal.SIGRTMIN + 2, lambda *_: tray.pause_meeting())  # Alt+P: `utter pause`
    signal.signal(signal.SIGRTMIN + 3, lambda *_: tray.toggle_interpreter())  # Alt+I: `utter interpret`
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    for kind in EARLY:
        core.press() if kind == "press" else core.cancel()
    EARLY.clear()
