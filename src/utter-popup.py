#!/usr/bin/env python3
"""Small animated "listening" pill at the bottom of the screen, kept running hidden so it appears instantly.

stdin protocol, one message per line: "S" shows it (fade in), "P" swaps the bars for a loader (waiting for the
final text), "R" shows the loader with "Reconnecting…", "A" returns to the bars, "H" hides it (fade out), "L <0..1>" is the mic level (drives the bars). EOF exits. The animation timer only runs while it is visible.

Runs under XWayland (GDK_BACKEND=x11) on purpose: GNOME's Wayland ignores "don't take focus" and window positions,
and a popup that took focus would receive the typed transcript instead of the app you are dictating into.
"""
import json, math, os, signal, subprocess, sys, threading, time, urllib.error, urllib.request

os.environ["GDK_BACKEND"] = "x11"
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk, Gdk

BARS = 5
IN_MS, OUT_MS = 220, 180
SLIDE_PX = 18         # how far it rises while fading in
BOTTOM_GAP = 40
CSS = b"""
window { background: transparent; }
.pill { background: rgba(24,24,27,0.92); border-radius: 20px; padding: 8px 12px; }
.note { color: #f4f4f5; font-size: 13px; }
"""

win = Gtk.Window(type=Gtk.WindowType.POPUP)  # override-redirect: no decorations, no focus, no taskbar entry
win.set_accept_focus(False)
win.set_keep_above(True)
visual = win.get_screen().get_rgba_visual()
if visual:
    win.set_visual(visual)
win.set_app_paintable(True)
win.set_opacity(0)

style = Gtk.CssProvider()
style.load_from_data(CSS)
Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), style, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

meter = Gtk.DrawingArea()
meter.set_size_request(46, 22)
box = Gtk.Box(spacing=10)
box.get_style_context().add_class("pill")
box.pack_start(meter, False, False, 0)
note = Gtk.Label(label="Reconnecting…")  # shown only while the connection is being rebuilt
note.get_style_context().add_class("note")
note.set_no_show_all(True)
box.pack_start(note, False, False, 0)
win.add(box)

state = {"level": 0.0, "shown": 0.0, "bars": [0.0] * BARS, "start": 0.0, "closing": None, "timer": None,
         "processing": None}  # monotonic time the loader started, or None while listening


def ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


def draw_loader(cr, since: float) -> None:
    """Waiting for the final text: a calm grey dot and three dots rising in a wave."""
    cr.set_source_rgba(0.63, 0.63, 0.67, 0.9)
    cr.arc(7, 11, 4.5, 0, 2 * math.pi)
    cr.fill()
    for i in range(3):
        phase = since * 6.0 - i * 0.7
        lift = max(0.0, math.sin(phase))  # each dot rises then rests, one after another
        cr.set_source_rgba(0.96, 0.96, 0.96, 0.45 + 0.55 * lift)
        cr.arc(22 + i * 9, 13 - 4 * lift, 2.4, 0, 2 * math.pi)
        cr.fill()


def draw(_area, cr):
    now = time.monotonic() - state["start"]
    if state["processing"] is not None:
        draw_loader(cr, time.monotonic() - state["processing"])
        return
    # breathing red dot
    pulse = 0.65 + 0.35 * (0.5 + 0.5 * math.sin(now * 4.0))
    cr.set_source_rgba(0.94, 0.27, 0.27, pulse)
    cr.arc(7, 11, 4.5, 0, 2 * math.pi)
    cr.fill()
    # level bars: each bar follows the mic level with its own wobble, so speech looks alive and silence settles flat
    cr.set_source_rgba(0.96, 0.96, 0.96, 0.9)
    for i, h in enumerate(state["bars"]):
        height = 3 + 16 * h
        x = 18 + i * 6
        y = 11 - height / 2
        r = 1.25
        cr.new_sub_path()
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.arc(x + 2.5 - r, y + r, r, 1.5 * math.pi, 0)
        cr.arc(x + 2.5 - r, y + height - r, r, 0, 0.5 * math.pi)
        cr.arc(x + r, y + height - r, r, 0.5 * math.pi, math.pi)
        cr.close_path()
        cr.fill()


meter.connect("draw", draw)


def place(offset: float) -> None:
    display = Gdk.Display.get_default()
    area = (display.get_primary_monitor() or display.get_monitor(0)).get_workarea()
    w, h = win.get_size()
    win.move(area.x + (area.width - w) // 2, int(area.y + area.height - h - BOTTOM_GAP + offset))


def tick() -> bool:
    now = time.monotonic()
    # smooth the incoming level: fast attack, slow release
    target = state["level"]
    k = 0.5 if target > state["shown"] else 0.12
    state["shown"] += (target - state["shown"]) * k
    t = now - state["start"]
    for i in range(BARS):
        wobble = 0.55 + 0.45 * math.sin(t * (7 + i * 1.7) + i * 1.3)
        shape = (0.6, 0.85, 1.0, 0.85, 0.6)[i]
        goal = min(1.0, state["shown"] * shape * wobble * 1.6)
        state["bars"][i] += (goal - state["bars"][i]) * 0.35
    meter.queue_draw()

    if state["closing"] is None:
        p = min(1.0, (now - state["start"]) * 1000 / IN_MS)
        win.set_opacity(ease_out(p))
        place(SLIDE_PX * (1 - ease_out(p)))
    else:
        p = min(1.0, (now - state["closing"]) * 1000 / OUT_MS)
        win.set_opacity(1 - p)
        place(SLIDE_PX * 0.5 * p)
        if p >= 1.0:
            win.hide()
            state["timer"] = None
            return False
    return True


def show() -> None:
    state.update(level=0.0, shown=0.0, bars=[0.0] * BARS, start=time.monotonic(), closing=None, processing=None)
    note.hide()
    win.set_opacity(0)
    win.resize(1, 1)
    win.show_all()
    place(SLIDE_PX)
    if state["timer"] is None:
        state["timer"] = GLib.timeout_add(16, tick)  # ~60 fps, only while visible


def on_stdin(source, _cond):
    line = source.readline()
    if not line:
        Gtk.main_quit()
        return False
    line = line.rstrip("\n")
    if line == "S":
        show()
    elif line.startswith("W "):  # open the window on a page: history, keys or settings
        open_window(line[2:] or "history")
    elif line == "R":  # connection lost: loader plus "Reconnecting…" while it is rebuilt
        if state["timer"] is not None and state["closing"] is None:
            state["processing"] = time.monotonic()
            note.show()
            win.resize(1, 1)
    elif line == "A":  # reconnected: back to the listening bars
        state["processing"] = None
        note.hide()
        win.resize(1, 1)
    elif line == "P":  # stopped listening, waiting for the final text
        if state["timer"] is not None and state["closing"] is None:
            state["processing"] = time.monotonic()
    elif line == "H":
        if state["timer"] is not None and state["closing"] is None:
            state["closing"] = time.monotonic()
    elif line.startswith("L "):
        try:
            state["level"] = max(0.0, min(1.0, float(line[2:])))
        except ValueError:
            pass
    return True


# ---- top-bar icon: history and quit -------------------------------------------------------------------------
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "utter")
DATA = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "utter")
HISTORY = os.path.join(DATA, "history.jsonl")
HISTORY_SHOWN = 200
history_win = None


def copy(text: str) -> None:
    subprocess.Popen(["wl-copy", "--", text], stderr=subprocess.DEVNULL)


def read_history() -> list:
    entries = []
    try:
        with open(HISTORY) as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return entries[-HISTORY_SHOWN:][::-1]  # newest first


def fill_history(listbox: Gtk.ListBox) -> None:
    for child in listbox.get_children():
        listbox.remove(child)
    entries = read_history()
    if not entries:
        listbox.add(Gtk.Label(label="Nothing dictated yet.", margin=24))
    for e in entries:
        when = Gtk.Label(label=time.strftime("%d %b %H:%M", time.localtime(e.get("ts", 0))), xalign=0)
        when.get_style_context().add_class("dim-label")
        text = Gtk.Label(label=e.get("text", ""), xalign=0, wrap=True, selectable=True, max_width_chars=60)
        button = Gtk.Button(label="Copy", valign=Gtk.Align.CENTER)
        button.connect("clicked", lambda _b, t=e.get("text", ""): copy(t))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        col.pack_start(when, False, False, 0)
        col.pack_start(text, False, False, 0)
        row = Gtk.Box(spacing=12, margin=10)
        row.pack_start(col, True, True, 0)
        row.pack_end(button, False, False, 0)
        listbox.add(row)
    listbox.show_all()


KEY_FILE = os.path.join(CONFIG, "deepgram.key")
SETTINGS = os.path.join(CONFIG, "settings.json")


def masked_key() -> str:
    try:
        k = open(KEY_FILE).read().strip()
    except OSError:
        return "No key saved"
    return f"{k[:4]}…{k[-4:]}" if len(k) > 8 else "Saved"


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


def keys_page() -> Gtk.Widget:
    page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=20)
    title = Gtk.Label(xalign=0)
    title.set_markup("<b>Deepgram API key</b>")
    current = Gtk.Label(label="Current: " + masked_key(), xalign=0)
    current.get_style_context().add_class("dim-label")
    entry = Gtk.Entry(visibility=False, placeholder_text="Paste a new key")
    entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
    status = Gtk.Label(xalign=0, wrap=True)
    save = Gtk.Button(label="Check and save", halign=Gtk.Align.START)

    def done(ok: bool, key: str, why: str) -> None:
        save.set_sensitive(True)
        if not ok:
            status.set_text(why)
            return
        write_private(KEY_FILE, key)
        entry.set_text("")
        current.set_text("Current: " + masked_key())
        status.set_text("Saved. Dictation switches to the new key within a few seconds.")
        os.kill(os.getppid(), signal.SIGHUP)  # the daemon re-reads the key and reconnects

    def check(key: str) -> None:
        # GET /v1/projects needs only a valid key; the key goes in a header, never on the command line
        req = urllib.request.Request("https://api.deepgram.com/v1/projects", headers={"Authorization": f"Token {key}"})
        try:
            with urllib.request.urlopen(req, timeout=10):
                GLib.idle_add(done, True, key, "")
        except urllib.error.HTTPError as e:
            GLib.idle_add(done, False, key, f"Deepgram rejected the key (HTTP {e.code}). Nothing was saved.")
        except OSError as e:
            GLib.idle_add(done, False, key, f"Could not reach Deepgram ({e}). Nothing was saved.")

    def on_save(*_):
        key = entry.get_text().strip()
        if not key:
            status.set_text("Paste a key first.")
            return
        save.set_sensitive(False)
        status.set_text("Checking…")
        threading.Thread(target=check, args=(key,), daemon=True).start()

    save.connect("clicked", on_save)
    entry.connect("activate", on_save)
    for w in (title, current, entry, save, status):
        page.pack_start(w, False, False, 0)
    return page


def settings_page() -> Gtk.Widget:
    page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=20)
    title = Gtk.Label(xalign=0)
    title.set_markup("<b>Typing</b>")
    live = Gtk.RadioButton.new_with_label(None, "Live: type the words as you speak")
    once = Gtk.RadioButton.new_with_label_from_widget(live, "All at once: type everything after you press Alt+D to stop")
    (once if read_settings().get("mode") == "once" else live).set_active(True)
    note = Gtk.Label(label="Either way, the whole dictation is copied to the clipboard and saved to History.",
                     xalign=0, wrap=True)
    note.get_style_context().add_class("dim-label")

    def on_toggle(button):
        if button.get_active():
            cfg = read_settings()
            cfg["mode"] = "once" if button is once else "live"
            write_private(SETTINGS, json.dumps(cfg))  # read at the start of each dictation

    live.connect("toggled", on_toggle)
    once.connect("toggled", on_toggle)

    mic_title = Gtk.Label(xalign=0, margin_top=14)
    mic_title.set_markup("<b>Microphone</b>")
    mics = Gtk.ComboBoxText()
    fill_mics(mics)

    def on_mic(combo):
        cfg = read_settings()
        chosen = combo.get_active_id()
        if chosen:
            cfg["mic"] = chosen
        else:
            cfg.pop("mic", None)  # follow the system default
        write_private(SETTINGS, json.dumps(cfg))  # read at the start of each dictation

    mics._on_mic = on_mic
    mics.connect("changed", on_mic)
    mic_note = Gtk.Label(label="Takes effect on your next Alt+D. If the chosen mic is unplugged, the system default is used.",
                         xalign=0, wrap=True)
    mic_note.get_style_context().add_class("dim-label")
    for w in (title, live, once, note, mic_title, mics, mic_note):
        page.pack_start(w, False, False, 0)
    page.mics = mics
    return page


def list_mics() -> list:
    """Input devices as (name, description), monitors of speakers left out."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True, timeout=3).stdout
        return [(m["name"], m.get("description") or m["name"]) for m in json.loads(out)
                if not m["name"].endswith(".monitor")]
    except (OSError, ValueError, subprocess.TimeoutExpired, KeyError):
        return []


def fill_mics(combo: Gtk.ComboBoxText) -> None:
    """(Re)list the mics, keeping the saved choice selected. Blocks "changed" so filling never rewrites settings."""
    saved = read_settings().get("mic")
    handler = getattr(combo, "_on_mic", None)
    if handler:
        combo.handler_block_by_func(handler)
    combo.remove_all()
    combo.append("", "System default")
    names = []
    for name, desc in list_mics():
        combo.append(name, desc)
        names.append(name)
    if saved and saved not in names:
        combo.append(saved, "Unplugged: " + saved)
    combo.set_active_id(saved or "")
    if handler:
        combo.handler_unblock_by_func(handler)


app_win = None


def open_window(page: str) -> None:
    global app_win
    if app_win is None:
        app_win = Gtk.Window(title="Utter")
        app_win.set_icon_name("utter")
        app_win.set_default_size(720, 620)
        app_win.connect("delete-event", lambda w, _e: w.hide() or True)  # keep it for next time
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        scroll = Gtk.ScrolledWindow()
        scroll.add(listbox)
        stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        stack.add_titled(scroll, "history", "History")
        stack.add_titled(keys_page(), "keys", "API keys")
        settings = settings_page()
        stack.add_titled(settings, "settings", "Settings")
        sidebar = Gtk.StackSidebar(stack=stack)
        sidebar.set_size_request(150, -1)
        stack.connect("notify::visible-child-name",
                      lambda st, _p: st.get_visible_child_name() == "history" and fill_history(listbox))
        layout = Gtk.Box()
        layout.pack_start(sidebar, False, False, 0)
        layout.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        layout.pack_start(stack, True, True, 0)
        app_win.add(layout)
        app_win.listbox, app_win.stack, app_win.settings = listbox, stack, settings
    fill_history(app_win.listbox)
    fill_mics(app_win.settings.mics)
    app_win.show_all()
    app_win.stack.set_visible_child_name(page)
    app_win.present()


def show_history(*_):
    open_window("history")


def show_settings(*_):
    open_window("settings")


def quit_app(*_):
    # stopping the service ends this process too; Alt+D starts it again
    subprocess.Popen(["systemctl", "--user", "stop", "utter"])


def tray() -> None:
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AI
    except (ValueError, ImportError):
        return  # no top-bar support: dictation still works
    ind = AI.Indicator.new("utter", "audio-input-microphone-symbolic", AI.IndicatorCategory.APPLICATION_STATUS)
    ind.set_title("Utter")
    menu = Gtk.Menu()
    for label, handler in (("Dictation history", show_history), ("Copy last dictation", copy_last),
                           ("Settings and API keys", show_settings), (None, None), ("Quit dictation", quit_app)):
        item = Gtk.SeparatorMenuItem() if label is None else Gtk.MenuItem(label=label)
        if handler:
            item.connect("activate", handler)
        menu.append(item)
    menu.show_all()
    ind.set_menu(menu)
    ind.set_status(AI.IndicatorStatus.ACTIVE)
    globals()["_indicator"] = ind  # keep a reference or it disappears


def copy_last(*_):
    entries = read_history()
    if entries:
        copy(entries[0].get("text", ""))


GLib.io_add_watch(sys.stdin, GLib.IO_IN | GLib.IO_HUP, on_stdin)
box.show_all()
tray()
Gtk.main()
