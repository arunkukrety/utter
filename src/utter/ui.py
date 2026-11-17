"""Utter's windows, in Qt: the listening pill at the bottom of the screen, the tray icon, and the History / Settings window.

The pill takes one line per update from the engine (any thread; it is queued to the UI thread): "S" shows it (fade in),
"P" swaps the bars for a loader (waiting for the final text), "R" shows the loader with "Reconnecting…", "A" returns
to the bars, "H" hides it (fade out), "L <0..1>" is the mic level (drives the bars), "M <text>" shows a notice for a
moment, "W <page>" opens the window on history or settings. The animation timer only runs while it is visible.
It never takes focus, or the pasted text would land in the pill instead of the app you are dictating into.
"""
import json, math, os, sys, threading, time, urllib.error, urllib.request

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QIcon, QPainter
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QMenu, QPushButton, QRadioButton, QScrollArea, QStackedWidget,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

from . import VERSION, core, plat

REPO = "arunkukrety/utter"
MAC = sys.platform == "darwin"
# developer build: run from source, or UTTER_DEV=1 (Settings then shows the server address field)
DEV = not getattr(sys, "frozen", False) or os.environ.get("UTTER_DEV") == "1"
BARS = 5
MESSAGE_S = 2.5  # how long a notice like "Didn't catch that" stays up
IN_MS, OUT_MS = 220, 180
SLIDE_PX = 18  # how far it rises while fading in
BOTTOM_GAP = 40
PAD_X, PAD_Y, METER_W, METER_H, GAP = 12, 8, 46, 22, 10
CONFIG, DATA = plat.CONFIG, plat.DATA
HISTORY = os.path.join(DATA, "history.jsonl")
HISTORY_SHOWN = 200
KEY_FILE = os.path.join(CONFIG, "server.token")  # the Utter key
SETTINGS = os.path.join(CONFIG, "settings.json")


def ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


# ---- the pill -------------------------------------------------------------------------------------------------------
class Pill(QWidget):
    line = Signal(str)

    def __init__(self, open_window) -> None:
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
        if sys.platform.startswith("linux"):
            flags |= Qt.X11BypassWindowManagerHint  # override-redirect: GNOME places it where asked, never focuses it
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.open_window = open_window
        self.font_ = QFont()
        self.font_.setPixelSize(13)
        self.note = ""
        self.history, self.bars = [], [0.0] * BARS
        self.start, self.closing, self.processing, self.message = 0.0, None, None, False
        self.timer = QTimer(self, interval=16, timeout=self.tick)  # ~60 fps, only while visible
        self.message_timer = QTimer(self, singleShot=True, timeout=self.end_message)
        self.line.connect(self.handle)  # emitted from the engine's thread, run on this one

    def visible(self) -> bool:
        return self.timer.isActive()

    def fit(self) -> None:
        w = PAD_X * 2 + METER_W + (GAP + QFontMetrics(self.font_).horizontalAdvance(self.note) if self.note else 0)
        self.resize(w, METER_H + PAD_Y * 2)

    def place(self, offset: float) -> None:
        area = QGuiApplication.primaryScreen().availableGeometry()
        self.move(area.x() + (area.width() - self.width()) // 2,
                  int(area.y() + area.height() - self.height() - BOTTOM_GAP + offset))

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(24, 24, 27, 235))
        r = self.height() / 2
        p.drawRoundedRect(QRectF(self.rect()), r, r)
        p.translate(PAD_X, PAD_Y)
        if self.message:  # a short notice ("Didn't catch that"): a calm grey dot beside the text
            self.dot(p, QColor(161, 161, 171, 230))
        elif self.processing is not None:  # waiting for the final text: grey dot and three dots rising in a wave
            self.dot(p, QColor(161, 161, 171, 230))
            since = time.monotonic() - self.processing
            for i in range(3):
                lift = max(0.0, math.sin(since * 6.0 - i * 0.7))  # each dot rises then rests, one after another
                p.setBrush(QColor(245, 245, 245, int(255 * (0.45 + 0.55 * lift))))
                p.drawEllipse(QPointF(22 + i * 9, 13 - 4 * lift), 2.4, 2.4)
        else:
            self.dot(p, QColor(240, 69, 69))  # steady red recording dot
            # level bars: the middle bar is your voice right now, the outer ones a moment ago, so speech ripples
            # outward and silence lies flat. Nothing here moves unless the mic hears something.
            p.setBrush(QColor(245, 245, 245, 230))
            for i, h in enumerate(self.bars):
                height = 3 + 16 * h
                p.drawRoundedRect(QRectF(18 + i * 6, 11 - height / 2, 2.5, height), 1.25, 1.25)
        if self.note:
            p.setPen(QColor("#f4f4f5"))
            p.setFont(self.font_)
            p.drawText(QRectF(METER_W + GAP, 0, self.width(), METER_H), Qt.AlignVCenter | Qt.AlignLeft, self.note)

    @staticmethod
    def dot(p: QPainter, color: QColor) -> None:
        p.setBrush(color)
        p.drawEllipse(QPointF(7, 11), 4.5, 4.5)

    def tick(self) -> None:
        now = time.monotonic()
        for i in range(BARS):
            age = abs(i - BARS // 2)  # 0 for the middle bar, 1 and 2 for the outer pairs
            goal = self.history[-1 - age] if len(self.history) > age else 0.0
            self.bars[i] += (goal - self.bars[i]) * 0.65  # ease toward it so 25 updates/s look smooth
        if self.closing is None:
            t = min(1.0, (now - self.start) * 1000 / IN_MS)
            self.setWindowOpacity(ease_out(t))
            self.place(SLIDE_PX * (1 - ease_out(t)))
        else:
            t = min(1.0, (now - self.closing) * 1000 / OUT_MS)
            self.setWindowOpacity(1 - t)
            self.place(SLIDE_PX * 0.5 * t)
            if t >= 1.0:
                self.hide()
                self.timer.stop()
                return
        self.update()

    def show_pill(self) -> None:
        self.history, self.bars = [], [0.0] * BARS
        self.start, self.closing, self.processing, self.message = time.monotonic(), None, None, False
        self.note = ""
        self.fit()
        self.setWindowOpacity(0)
        self.place(SLIDE_PX)
        self.show()
        self.timer.start()

    def end_message(self) -> None:
        if self.message:  # still showing the notice (no new dictation started meanwhile)
            self.handle("H")

    def set_note(self, text: str) -> None:
        self.note = text
        self.fit()

    def handle(self, line: str) -> None:
        if line == "S":
            self.show_pill()
        elif line.startswith("W "):  # open the window on a page: history or settings
            self.open_window(line[2:] or "history")
        elif line.startswith("M "):  # a notice: shown for MESSAGE_S seconds, then the pill fades out by itself
            if not self.visible() or self.closing is not None:
                self.show_pill()  # the pill may already be gone (live mode hides it on the stop press)
            self.processing, self.message = None, True
            self.set_note(line[2:])
            self.message_timer.start(int(MESSAGE_S * 1000))
        elif line == "R":  # connection lost: loader plus "Reconnecting…" while it is rebuilt
            if self.visible() and self.closing is None:
                self.processing = time.monotonic()
                self.set_note("Reconnecting…")
        elif line == "A":  # reconnected: back to the listening bars
            self.processing = None
            self.set_note("")
        elif line == "P":  # stopped listening, waiting for the final text
            if self.visible() and self.closing is None:
                self.processing = time.monotonic()
        elif line == "H":
            if self.visible() and self.closing is None:
                self.closing = time.monotonic()
        elif line.startswith("L "):
            try:
                self.history = (self.history + [max(0.0, min(1.0, float(line[2:])))])[-BARS:]
            except ValueError:
                pass


# ---- settings and keys on disk --------------------------------------------------------------------------------------
def read_settings() -> dict:
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_private(path: str, text: str) -> None:
    """Atomic write, readable by you only."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(text)
    os.replace(tmp, path)


def update_settings(**values) -> None:
    cfg = read_settings()
    for k, v in values.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    write_private(SETTINGS, json.dumps(cfg))  # read at the start of each dictation


def read_history() -> list:
    entries = []
    try:
        with open(HISTORY, encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return entries[-HISTORY_SHOWN:][::-1]  # newest first


def masked(path: str) -> str:
    try:
        k = open(path).read().strip()
    except OSError:
        return "No key saved"
    return f"Current: {k[:4]}…{k[-4:]}" if len(k) > 8 else "Current: saved"


def version_tuple(v: str) -> tuple:
    """'1.10.0' -> (1, 10, 0), so versions compare as numbers; anything unparsable counts as oldest."""
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def dim(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: palette(placeholder-text);")
    return label


def title(text: str, top: int = 14) -> QLabel:
    label = QLabel(f"<b>{text}</b>")
    label.setContentsMargins(0, top, 0, 0)
    return label


def radios(options, current: str, on_pick) -> list:
    """options: [(id, label)]. on_pick(id) runs when one is chosen."""
    group, buttons = QButtonGroup(), []
    for oid, label in options:
        b = QRadioButton(label)
        b.setChecked(oid == current)
        b.toggled.connect(lambda on, oid=oid: on and on_pick(oid))
        group.addButton(b)
        buttons.append(b)
    buttons[0]._group = group  # keep the group alive
    return buttons


# ---- the window ----------------------------------------------------------------------------------------------------
QT_KEY_NAMES = {Qt.Key_Space: "space", **{getattr(Qt, f"Key_F{n}"): f"f{n}" for n in range(1, 25)}}
MODIFIER_KEYS = (Qt.Key_Control, Qt.Key_Alt, Qt.Key_Shift, Qt.Key_Meta, Qt.Key_AltGr, Qt.Key_Super_L, Qt.Key_Super_R)


def combo_of(event) -> str:
    """A key press as 'alt+d', '' for a lone modifier, or '?+…' for a key Utter can't bind. Qt calls Cmd "Control"
    and Ctrl "Meta" on macOS."""
    key = event.key()
    if key in MODIFIER_KEYS:
        return ""
    if Qt.Key_A <= key <= Qt.Key_Z or Qt.Key_0 <= key <= Qt.Key_9:
        name = chr(key).lower()
    else:
        name = QT_KEY_NAMES.get(key, "?")
    m = event.modifiers()
    ctrl, sup = (Qt.MetaModifier, Qt.ControlModifier) if MAC else (Qt.ControlModifier, Qt.MetaModifier)
    mods = [n for n, flag in (("ctrl", ctrl), ("alt", Qt.AltModifier), ("shift", Qt.ShiftModifier), ("super", sup))
            if m & flag]
    return "+".join(mods + [name])


class Window(QWidget):
    run_on_ui = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Utter")
        self.setWindowIcon(QIcon(plat.resource("data", "utter.svg")))
        self.resize(720, 620)
        self.run_on_ui.connect(lambda fn: fn())
        self.capturing = False  # set while the shortcut button waits for the new key
        self.sidebar = QListWidget()
        self.sidebar.addItems(["History", "Settings"])
        self.sidebar.setFixedWidth(150)
        self.stack = QStackedWidget()
        self.history_box = QVBoxLayout()
        self.stack.addWidget(self.scroll(self.history_box))
        settings = QVBoxLayout()
        self.settings_page(settings)
        self.stack.addWidget(self.scroll(settings))
        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.sidebar.currentRowChanged.connect(lambda row: row == 0 and self.fill_history())
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.stack, 1)

    @staticmethod
    def scroll(box: QVBoxLayout) -> QScrollArea:
        """Pages longer than the window scroll instead of being cut off."""
        page = QWidget()
        box.setContentsMargins(20, 20, 20, 20)
        box.setSpacing(8)
        page.setLayout(box)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setWidget(page)
        return area

    def later(self, fn) -> None:
        """From a worker thread: run fn on the UI thread."""
        self.run_on_ui.emit(fn)

    def open(self, page: str) -> None:
        self.fill_history()
        self.fill_mics()
        self.sidebar.setCurrentRow(0 if page == "history" else 1)
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()  # keep it for next time

    # -- History --
    def fill_history(self) -> None:
        box = self.history_box
        while box.count():
            item = box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        entries = read_history()
        if not entries:
            box.addWidget(QLabel("Nothing dictated yet."))
        for e in entries:
            col = QVBoxLayout()
            col.setSpacing(2)
            col.addWidget(dim(time.strftime("%d %b %H:%M", time.localtime(e.get("ts", 0)))))
            text = QLabel(e.get("text", ""))
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            col.addWidget(text)
            button = QPushButton("Copy")
            button.clicked.connect(lambda _c=False, t=e.get("text", ""): plat.copy(t))
            row = QHBoxLayout()
            row.addLayout(col, 1)
            row.addWidget(button, 0, Qt.AlignVCenter)
            holder = QWidget()
            holder.setLayout(row)
            box.addWidget(holder)
        box.addStretch(1)

    # -- Settings --
    def settings_page(self, page: QVBoxLayout) -> None:
        add = page.addWidget
        cfg = read_settings()

        add(title("Utter key", 0))
        current = dim(masked(KEY_FILE))
        entry = QLineEdit(placeholderText="Paste your Utter key", echoMode=QLineEdit.Password)
        save = QPushButton("Save")
        row = QHBoxLayout()
        row.addWidget(entry, 1)
        row.addWidget(save)

        def on_save() -> None:
            key = entry.text().strip()
            if key:
                write_private(KEY_FILE, key)
                entry.clear()
                current.setText(masked(KEY_FILE))
                core.reload_key()  # connects with it before your next dictation

        save.clicked.connect(on_save)
        entry.returnPressed.connect(on_save)
        add(current)
        page.addLayout(row)

        if DEV:
            self.server_field(page)

        add(title("Shortcut"))
        key_row = QHBoxLayout()
        self.key_label = QLabel()
        self.key_change = QPushButton("Change")
        self.key_change.clicked.connect(self.toggle_capture)
        key_row.addWidget(self.key_label)
        key_row.addWidget(self.key_change)
        key_row.addStretch(1)
        page.addLayout(key_row)
        self.key_status = dim("")
        add(self.key_status)
        self.show_key()

        hold = QCheckBox(f"Hold {plat.HOLD_KEY} to talk: hold {plat.HOLD_KEY} on its own for half a second, speak, "
                         "release to finish")
        hold.setChecked(cfg.get("hold_to_talk", True) is not False)
        hold.toggled.connect(lambda on: update_settings(hold_to_talk=on))  # read at each press
        add(hold)
        add(dim(plat.HOLD_NOTE))

        autostart = QCheckBox("Start Utter when I log in")
        autostart.setChecked(cfg.get("autostart") is not False)

        def on_autostart(on: bool) -> None:
            update_settings(autostart=on)
            plat.set_autostart(on)

        autostart.toggled.connect(on_autostart)
        add(autostart)
        if sys.platform.startswith("linux"):
            add(dim("When off, Utter starts the first time you press the shortcut instead."))

        add(title("Language"))
        hindi = []

        def on_language(oid: str) -> None:
            update_settings(language=oid)
            for w in hindi:
                w.setEnabled(oid == "multi")
            core.reload_key()  # reconnect in the new language before your next dictation

        for b in radios([("en", "English only"), ("multi", "English + Hindi: follows you between the two")],
                        "multi" if cfg.get("language") == "multi" else "en", on_language):
            add(b)
        hindi.append(title("Hindi"))
        hindi += radios([("roman", "Roman Hinglish: kal meeting hai"), ("devanagari", "Devanagari: कल meeting है")],
                        "devanagari" if cfg.get("hindi") == "devanagari" else "roman",
                        lambda oid: (update_settings(hindi=oid), core.reload_key()))
        hindi.append(dim("How Hindi words are written. English is always written as is."))
        for w in hindi:
            w.setEnabled(cfg.get("language") == "multi")
            add(w)

        add(title("Microphone"))
        self.mics = QComboBox()
        self.mics.currentIndexChanged.connect(self.on_mic)
        add(self.mics)
        add(dim("Takes effect on your next dictation. If the chosen mic is unplugged, the system default is used. "
                f"Every dictation is pasted with {plat.PASTE_KEYS} and also copied to the clipboard and History."))

        add(title(f"Utter {VERSION}"))
        check = QPushButton("Check for updates")
        update_status = QLabel(wordWrap=True)
        download = QPushButton("Download the new version")
        download.hide()
        url = {"v": f"https://github.com/{REPO}/releases/latest"}
        download.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url["v"])))

        def show_result(latest: str, link: str, error) -> None:
            check.setEnabled(True)
            if error:
                update_status.setText(f"Couldn't check for updates ({error}).")
            elif version_tuple(latest) > version_tuple(VERSION):
                update_status.setText(f"Utter {latest} is available. You have {VERSION}.")
                url["v"] = link or url["v"]
                download.show()
            else:
                update_status.setText(f"You're up to date ({VERSION}).")
                download.hide()

        def fetch_latest() -> None:
            req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                         headers={"Accept": "application/vnd.github+json", "User-Agent": f"Utter/{VERSION}"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    rel = json.load(r)
                self.later(lambda: show_result(rel.get("tag_name", "").lstrip("v"), rel.get("html_url", ""), None))
            except (OSError, ValueError) as e:
                why = getattr(e, "reason", None) or str(e)
                self.later(lambda: show_result("", "", why))

        def on_check() -> None:
            check.setEnabled(False)
            update_status.setText("Checking…")
            threading.Thread(target=fetch_latest, daemon=True).start()

        check.clicked.connect(on_check)
        page.addWidget(check, 0, Qt.AlignLeft)
        page.addWidget(update_status)
        page.addWidget(download, 0, Qt.AlignLeft)
        page.addStretch(1)

    def server_field(self, page: QVBoxLayout) -> None:
        """Developer builds only: point the app at another Utter server (a local one, an SSH forward, staging)."""
        page.addWidget(title("Server address (developer)"))
        current = dim("")
        entry = QLineEdit(placeholderText="wss://… or ws://127.0.0.1:18191/stream")
        save, reset = QPushButton("Save"), QPushButton("Use default")
        status = dim("")

        def show() -> None:
            custom = read_settings().get("server_url")
            current.setText(f"Using: {custom or core.SERVER_URL}" + ("" if custom else " (default)"))

        def on_save() -> None:
            url = entry.text().strip()
            if not url.startswith(("ws://", "wss://")):
                status.setText("The address starts with wss:// (or ws:// for a local server).")
                return
            update_settings(server_url=url)
            entry.clear()
            status.setText("Saved. Used from your next dictation.")
            show()
            core.reload_key()  # reconnect to it before the next press

        def on_reset() -> None:
            update_settings(server_url=None)
            status.setText("Back to the default server.")
            show()
            core.reload_key()

        save.clicked.connect(on_save)
        entry.returnPressed.connect(on_save)
        reset.clicked.connect(on_reset)
        row = QHBoxLayout()
        row.addWidget(entry, 1)
        row.addWidget(save)
        row.addWidget(reset)
        page.addWidget(current)
        page.addLayout(row)
        page.addWidget(status)
        show()

    def fill_mics(self) -> None:
        """(Re)list the mics, keeping the saved choice selected, without rewriting the setting."""
        saved = read_settings().get("mic")
        self.mics.blockSignals(True)
        self.mics.clear()
        self.mics.addItem("System default", "")
        names = []
        for name, desc in plat.list_mics():
            self.mics.addItem(desc, name)
            names.append(name)
        if saved and saved not in names:
            self.mics.addItem("Unplugged: " + saved, saved)
        self.mics.setCurrentIndex(max(0, self.mics.findData(saved or "")))
        self.mics.blockSignals(False)

    def on_mic(self, _index: int) -> None:
        update_settings(mic=self.mics.currentData() or None)  # None: follow the system default

    def show_key(self) -> None:
        combo = plat.current_shortcut()
        if combo is None:  # Linux without GNOME: point at the desktop's own settings
            self.key_label.setText("Set in your desktop's keyboard settings")
            self.key_change.hide()
            self.key_status.setText("Bind any key to the command: utter")
        else:
            self.key_label.setText(plat.shortcut_label(combo))

    def toggle_capture(self) -> None:
        self.capturing = not self.capturing
        self.key_change.setText("Press the new shortcut… (Esc to cancel)" if self.capturing else "Change")
        if self.capturing:
            self.key_status.setText("")
            self.setFocus()

    def keyPressEvent(self, event) -> None:
        if not self.capturing:
            return super().keyPressEvent(event)
        if event.key() == Qt.Key_Escape and not event.modifiers():
            self.toggle_capture()
            return
        combo = combo_of(event)
        if not combo:
            return  # wait for the real key
        *mods, name = combo.split("+")
        if name == "?":
            self.key_status.setText("Use a letter, a digit, Space or an F key.")
            return
        if not mods and not name.startswith("f"):
            self.key_status.setText("Use a combination with Ctrl, Alt or Super (or an F key), so normal typing isn't caught.")
            return
        error = plat.set_shortcut(combo)
        self.toggle_capture()
        self.key_status.setText(error or f"Saved. Press {plat.shortcut_label(combo)} to dictate.")
        self.show_key()


# ---- tray icon ------------------------------------------------------------------------------------------------------
def make_tray(window: Window) -> QSystemTrayIcon:
    icon = QIcon(plat.resource("data", "utter.svg" if sys.platform == "win32" else "utter-tray.svg"))
    if MAC:
        icon.setIsMask(True)  # a template image: macOS draws it black or white to match the menu bar
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("Utter")
    menu = QMenu()

    def copy_last() -> None:
        entries = read_history()
        if entries:
            plat.copy(entries[0].get("text", ""))

    def quit_app() -> None:
        if not plat.quit():
            QApplication.quit()

    for label, handler in (("Dictation history", lambda: window.open("history")), ("Copy last dictation", copy_last),
                           ("Settings", lambda: window.open("settings")), (None, None),
                           ("Quit Utter", quit_app)):
        if label is None:
            menu.addSeparator()
        else:
            action = QAction(label, menu)
            action.triggered.connect(handler)
            menu.addAction(action)
    tray.setContextMenu(menu)
    if sys.platform == "win32":  # a left click on Windows opens the window; the menu is on the right click
        tray.activated.connect(lambda reason: reason == QSystemTrayIcon.Trigger and window.open("history"))
    tray._menu = menu  # keep a reference or it disappears
    tray.show()
    return tray
