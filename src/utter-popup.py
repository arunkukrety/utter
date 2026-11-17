#!/usr/bin/env python3
"""Small animated "listening" pill at the bottom of the screen, kept running hidden so it appears instantly.

stdin protocol, one message per line: "S" shows it (fade in), "P" swaps the bars for a loader (waiting for the
final text), "R" shows the loader with "Reconnecting…", "A" returns to the bars, "H" hides it (fade out), "L <0..1>" is the mic level (drives the bars). EOF exits. The animation timer only runs while it is visible.

Runs under XWayland (GDK_BACKEND=x11) on purpose: GNOME's Wayland ignores "don't take focus" and window positions,
and a popup that took focus would receive the typed transcript instead of the app you are dictating into.
"""
import ast, json, math, os, signal, subprocess, sys, threading, time, urllib.error, urllib.request

os.environ["GDK_BACKEND"] = "x11"
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk, Gdk

# one app identity, so the dock, window switcher and launcher all show Utter's own icon
GLib.set_prgname("utter")
GLib.set_application_name("Utter")
Gdk.set_program_class("utter")
HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.1.0"  # the one place the version lives; packaging/build-deb.sh reads it from here
REPO = "arunkukrety/utter"

BARS = 5
MESSAGE_S = 2.5  # how long a notice like "Didn't catch that" stays up
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

state = {"history": [], "bars": [0.0] * BARS, "start": 0.0, "closing": None, "timer": None,
         "processing": None, "message": False}  # monotonic time the loader started, or None while listening


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
    if state["message"]:  # a short notice ("Didn't catch that"): a calm grey dot beside the text
        cr.set_source_rgba(0.63, 0.63, 0.67, 0.9)
        cr.arc(7, 11, 4.5, 0, 2 * math.pi)
        cr.fill()
        return
    if state["processing"] is not None:
        draw_loader(cr, time.monotonic() - state["processing"])
        return
    # steady red recording dot
    cr.set_source_rgba(0.94, 0.27, 0.27, 1.0)
    cr.arc(7, 11, 4.5, 0, 2 * math.pi)
    cr.fill()
    # level bars: the middle bar is your voice right now, the outer ones a moment ago, so speech ripples outward
    # and silence lies flat. Nothing here moves unless the mic hears something.
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
    h = state["history"]  # newest level last
    for i in range(BARS):
        age = abs(i - BARS // 2)  # 0 for the middle bar, 1 and 2 for the outer pairs
        goal = h[-1 - age] if len(h) > age else 0.0
        state["bars"][i] += (goal - state["bars"][i]) * 0.65  # ease toward it so 25 updates/s look smooth
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
    state.update(history=[], bars=[0.0] * BARS, start=time.monotonic(), closing=None, processing=None, message=False)
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
    handle(line.rstrip("\n"))
    return True


def end_message() -> bool:
    if state["message"]:  # still showing the notice (no new dictation started meanwhile)
        handle("H")
    return False


def handle(line: str) -> None:
    if line == "S":
        show()
    elif line.startswith("W "):  # open the window on a page: history, keys or settings
        open_window(line[2:] or "history")
    elif line.startswith("M "):  # a notice: shown for MESSAGE_S seconds, then the pill fades out by itself
        if state["timer"] is None or state["closing"] is not None:
            show()  # the pill may already be gone (live mode hides it on the stop press)
        state["processing"], state["message"] = None, True
        note.set_text(line[2:])
        note.show()
        win.resize(1, 1)
        GLib.timeout_add(int(MESSAGE_S * 1000), end_message)
    elif line == "R":  # connection lost: loader plus "Reconnecting…" while it is rebuilt
        if state["timer"] is not None and state["closing"] is None:
            state["processing"] = time.monotonic()
            note.set_text("Reconnecting…")
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
            state["history"] = (state["history"] + [max(0.0, min(1.0, float(line[2:])))])[-BARS:]
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
    page.pack_start(cleanup_key_row("elevenlabs", "ElevenLabs", "for speech recognition", reconnect=True),
                    False, False, 0)
    page.pack_start(cleanup_key_row("assemblyai", "AssemblyAI", "for speech recognition", reconnect=True),
                    False, False, 0)
    for provider, label in CLEANUP_PROVIDERS:
        page.pack_start(cleanup_key_row(provider, label), False, False, 0)
    return page


CLEANUP_PROVIDERS = (("surplus", "Surplus Intelligence"), ("groq", "Groq"))


def cleanup_key_row(provider: str, label: str, purpose: str = "for AI cleanup", reconnect: bool = False) -> Gtk.Widget:
    """A provider key saved as CONFIG/<provider>.key. Used the next time you dictate (reconnect: right away)."""
    path = os.path.join(CONFIG, f"{provider}.key")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=18)
    title = Gtk.Label(xalign=0)
    title.set_markup(f"<b>{label} API key</b> <small>({purpose})</small>")

    def masked() -> str:
        try:
            k = open(path).read().strip()
        except OSError:
            return "No key saved"
        return f"Current: {k[:4]}…{k[-4:]}" if len(k) > 8 else "Current: saved"

    current = Gtk.Label(label=masked(), xalign=0)
    current.get_style_context().add_class("dim-label")
    entry = Gtk.Entry(visibility=False, placeholder_text="Paste a key")
    row = Gtk.Box(spacing=8)
    save = Gtk.Button(label="Save")
    remove = Gtk.Button(label="Remove")
    row.pack_start(entry, True, True, 0)
    row.pack_start(save, False, False, 0)
    row.pack_start(remove, False, False, 0)

    def on_save(*_):
        key = entry.get_text().strip()
        if key:
            write_private(path, key)
            entry.set_text("")
            current.set_text(masked())
            if reconnect:
                os.kill(os.getppid(), signal.SIGHUP)  # the daemon reconnects with it

    def on_remove(*_):
        try:
            os.remove(path)
        except OSError:
            pass
        current.set_text(masked())

    save.connect("clicked", on_save)
    entry.connect("activate", on_save)
    remove.connect("clicked", on_remove)
    for w in (title, current, row):
        box.pack_start(w, False, False, 0)
    return box


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

    key_title = Gtk.Label(xalign=0)
    key_title.set_markup("<b>Shortcut</b>")
    key_row = Gtk.Box(spacing=12)
    key_label = Gtk.Label(xalign=0)
    key_change = Gtk.Button(label="Change")
    key_row.pack_start(key_label, False, False, 0)
    key_row.pack_start(key_change, False, False, 0)
    key_status = Gtk.Label(xalign=0, wrap=True)
    key_status.get_style_context().add_class("dim-label")
    capture = {"handler": None}

    def show_key() -> None:
        accel = current_shortcut()
        if accel is None:  # not GNOME: point at the desktop's own settings
            key_label.set_text("Set in your desktop's keyboard settings")
            key_change.set_no_show_all(True)
            key_change.hide()
            key_status.set_text("Bind any key to the command: utter")
        else:
            key_label.set_text(pretty_accel(accel))

    def stop_capture() -> None:
        if capture["handler"] is not None:
            page.get_toplevel().disconnect(capture["handler"])
            capture["handler"] = None
        key_change.set_label("Change")

    def on_key(_win, event) -> bool:
        if event.is_modifier:
            return True  # wait for the real key
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        key = Gdk.keyval_to_lower(event.keyval)
        if key == Gdk.KEY_Escape and not mods:
            stop_capture()
            key_status.set_text("")
            return True
        name = Gdk.keyval_name(key) or ""
        if not mods and not (name.startswith("F") and name[1:].isdigit()):
            key_status.set_text("Use a combination with Ctrl, Alt or Super (or an F key), so normal typing isn't caught.")
            return True
        accel = Gtk.accelerator_name(key, mods)
        error = set_shortcut(accel)
        stop_capture()
        key_status.set_text(error or f"Saved. Press {pretty_accel(accel)} to dictate.")
        show_key()
        return True

    def on_change(*_):
        if capture["handler"] is not None:
            stop_capture()
            return
        key_change.set_label("Press the new shortcut… (Esc to cancel)")
        key_status.set_text("")
        capture["handler"] = page.get_toplevel().connect("key-press-event", on_key)

    key_change.connect("clicked", on_change)
    show_key()

    hold = Gtk.CheckButton(label="Hold Alt to talk: hold Alt on its own for half a second, speak, release to finish",
                           margin_top=6)
    hold.set_active(read_settings().get("hold_to_talk", True) is not False)

    def on_hold(button):
        cfg = read_settings()
        cfg["hold_to_talk"] = button.get_active()
        write_private(SETTINGS, json.dumps(cfg))  # the helper reads it at each press

    hold.connect("toggled", on_hold)
    hold_note = Gtk.Label(label="Alt with any other key (Alt+Tab, Alt+D) never starts it. Needs the one-time "
                                "helper: sudo ./install-hold.sh in the Utter folder.", xalign=0, wrap=True)
    hold_note.get_style_context().add_class("dim-label")

    autostart = Gtk.CheckButton(label="Start Utter when I log in", margin_top=14)
    autostart.set_active(read_settings().get("autostart") is not False)

    def on_autostart(button):
        cfg = read_settings()
        cfg["autostart"] = button.get_active()
        write_private(SETTINGS, json.dumps(cfg))  # read at the next login

    autostart.connect("toggled", on_autostart)
    autostart_note = Gtk.Label(label="When off, Utter starts the first time you press the shortcut instead.",
                               xalign=0, wrap=True)
    autostart_note.get_style_context().add_class("dim-label")

    insert_title = Gtk.Label(xalign=0, margin_top=14)
    insert_title.set_markup("<b>Insert text by</b>")
    by_paste = Gtk.RadioButton.new_with_label(None, "Pasting: the whole text appears at once (fastest)")
    by_type = Gtk.RadioButton.new_with_label_from_widget(by_paste, "Typing it out: letters appear one by one")
    (by_type if read_settings().get("insert") == "type" else by_paste).set_active(True)
    insert_note = Gtk.Label(label="Pasting uses Ctrl+V, which terminals don't take (they use Ctrl+Shift+V). In a "
                                  "terminal, choose typing, or paste the text yourself: it is on the clipboard.",
                            xalign=0, wrap=True)
    insert_note.get_style_context().add_class("dim-label")

    def on_insert(button):
        if button.get_active():
            cfg = read_settings()
            cfg["insert"] = "type" if button is by_type else "paste"
            write_private(SETTINGS, json.dumps(cfg))  # read at the start of each dictation

    by_paste.connect("toggled", on_insert)
    by_type.connect("toggled", on_insert)

    stt_title = Gtk.Label(xalign=0, margin_top=14)
    stt_title.set_markup("<b>Speech to text</b>")
    # each option sets: live engine (stt), the accurate pass after you stop (accurate), and where that pass runs
    STT_OPTIONS = [
        ("el-accurate", "ElevenLabs: Scribe v3 live, then Scribe v2 on the whole recording (most accurate)",
         {"stt": "elevenlabs", "accurate": True, "accurate_route": "elevenlabs"}),
        ("el-accurate-surplus", "ElevenLabs: Scribe v3 live, then Scribe v2 through Surplus (your Surplus key)",
         {"stt": "elevenlabs", "accurate": True, "accurate_route": "surplus"}),
        ("server-simple", "Utter server: Groq Whisper → cleanup, nothing else (no live transcript; one provider, cheapest)",
         {"stt": "none", "accurate": True, "accurate_route": "server", "server_stt": "groq"}),
        ("server-cartesia", "Utter server: Cartesia Ink-Whisper → cleanup (no live transcript; falls back to Groq Whisper)",
         {"stt": "none", "accurate": True, "accurate_route": "server", "server_stt": "cartesia"}),
        ("server-openrouter", "Utter server: AssemblyAI via OpenRouter → cleanup (no live transcript; falls back to Groq Whisper)",
         {"stt": "none", "accurate": True, "accurate_route": "server", "server_stt": "openrouter"}),
        ("server", "Utter server: stream to your server while you speak; Groq Whisper + Groq cleanup run there (fastest)",
         {"stt": "elevenlabs", "accurate": True, "accurate_route": "server", "server_stt": "groq"}),
        ("server-scribe", "Utter server: as above, with ElevenLabs Scribe v2 instead of Whisper",
         {"stt": "elevenlabs", "accurate": True, "accurate_route": "server", "server_stt": "direct"}),
        ("groq-local", "Groq Whisper from this computer: ElevenLabs live, then Whisper on the whole recording",
         {"stt": "elevenlabs", "accurate": True, "accurate_route": "groq"}),
        ("el-live", "ElevenLabs: Scribe v3 live only (faster)",
         {"stt": "elevenlabs", "accurate": False}),
        # AssemblyAI streaming bills every second a connection is open, idle included: reached via OpenRouter instead
        ("dg-accurate", "Deepgram: nova-3 live, then nova-3 on the whole recording",
         {"stt": "deepgram", "accurate": True, "accurate_route": "deepgram"}),
        ("dg-live", "Deepgram: nova-3 live only (fastest)",
         {"stt": "deepgram", "accurate": False}),
    ]

    def current_stt() -> str:
        cfg = read_settings()
        for oid, _label, values in STT_OPTIONS:
            if all(cfg.get(k, {"stt": "deepgram", "accurate": False, "server_stt": "groq"}.get(k)) == v
                   for k, v in values.items()):
                return oid
        return "dg-live"

    stt_buttons, group = [], None
    for oid, label, values in STT_OPTIONS:
        button = Gtk.RadioButton.new_with_label_from_widget(group, label)
        group = group or button
        button.set_active(oid == current_stt())

        def on_stt(btn, values=values):
            if btn.get_active():
                cfg = read_settings()
                live_changed = cfg.get("stt") != values["stt"]
                cfg.update(values)
                write_private(SETTINGS, json.dumps(cfg))
                if live_changed:
                    os.kill(os.getppid(), signal.SIGHUP)  # reconnect to the chosen live service

        button.connect("toggled", on_stt)
        stt_buttons.append(button)
    stt_note = Gtk.Label(label="The whole-recording pass runs after you stop, in All at once mode. Each option needs "
                               "its service's key on the API keys page.", xalign=0, wrap=True)
    stt_note.get_style_context().add_class("dim-label")

    lang_title = Gtk.Label(xalign=0, margin_top=14)
    lang_title.set_markup("<b>Language</b>")
    english = Gtk.RadioButton.new_with_label(None, "English only")
    mixed = Gtk.RadioButton.new_with_label_from_widget(english, "English + Hindi: follows you between the two")
    (mixed if read_settings().get("language") == "multi" else english).set_active(True)

    hindi_title = Gtk.Label(xalign=0, margin_top=14)
    hindi_title.set_markup("<b>Hindi</b>")
    roman = Gtk.RadioButton.new_with_label(None, "Roman Hinglish: kal meeting hai")
    deva = Gtk.RadioButton.new_with_label_from_widget(roman, "Devanagari: कल meeting है")
    (deva if read_settings().get("hindi") == "devanagari" else roman).set_active(True)
    hindi_note = Gtk.Label(label="How Hindi words are written. English is always written as is.",
                           xalign=0, wrap=True)
    hindi_note.get_style_context().add_class("dim-label")

    def on_hindi(button):
        if button.get_active():
            cfg = read_settings()
            cfg["hindi"] = "devanagari" if button is deva else "roman"
            write_private(SETTINGS, json.dumps(cfg))

    roman.connect("toggled", on_hindi)
    deva.connect("toggled", on_hindi)

    def show_hindi(is_mixed: bool) -> None:  # the Hindi options only matter when Hindi is on
        for w in (hindi_title, roman, deva, hindi_note):
            w.set_sensitive(is_mixed)

    def on_language(button):
        if button.get_active():
            cfg = read_settings()
            cfg["language"] = "multi" if button is mixed else "en"
            write_private(SETTINGS, json.dumps(cfg))
            show_hindi(button is mixed)
            os.kill(os.getppid(), signal.SIGHUP)  # the daemon reconnects in the new language within a few seconds

    english.connect("toggled", on_language)
    mixed.connect("toggled", on_language)
    show_hindi(mixed.get_active())

    clean_title = Gtk.Label(xalign=0, margin_top=14)
    clean_title.set_markup("<b>Cleanup</b>")
    CLEAN_OPTIONS = [
        ("off", "Off: paste what was transcribed", {"cleanup": False}),
        ("groq-qwen", "Groq · qwen3.8-27b, thinking off (fastest, best in tests)",
         {"cleanup": True, "cleanup_provider": "groq", "cleanup_model": "qwen/qwen3.8-27b"}),
        ("groq-gptoss", "Groq · gpt-oss-120b, thinking low",
         {"cleanup": True, "cleanup_provider": "groq", "cleanup_model": "openai/gpt-oss-120b"}),
        ("surplus-llama", "Surplus · llama-3.3-70b-instruct (your Surplus key)",
         {"cleanup": True, "cleanup_provider": "surplus", "cleanup_model": "llama-3.3-70b-instruct"}),
    ]

    def current_clean() -> str:
        cfg = read_settings()
        if not cfg.get("cleanup"):
            return "off"
        provider = cfg.get("cleanup_provider") or "surplus"
        model = cfg.get("cleanup_model") or {"groq": "qwen/qwen3.8-27b", "surplus": "llama-3.3-70b-instruct"}[provider]
        return next((oid for oid, _l, v in CLEAN_OPTIONS
                     if v.get("cleanup_provider") == provider and v.get("cleanup_model") == model), "groq-qwen")

    clean_buttons, cgroup = [], None
    for oid, label, values in CLEAN_OPTIONS:
        button = Gtk.RadioButton.new_with_label_from_widget(cgroup, label)
        cgroup = cgroup or button
        button.set_active(oid == current_clean())

        def on_clean(btn, values=values):
            if btn.get_active():
                cfg = read_settings()
                cfg.update(values)
                write_private(SETTINGS, json.dumps(cfg))  # read at the start of each dictation

        button.connect("toggled", on_clean)
        clean_buttons.append(button)
    clean_note = Gtk.Label(label="Drops filler words and applies your corrections (\"five, no, six\" → \"six\"), "
                                 "in All at once mode. Needs the provider's key on the API keys page.",
                           xalign=0, wrap=True)
    clean_note.get_style_context().add_class("dim-label")

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
    about_title = Gtk.Label(xalign=0, margin_top=14)
    about_title.set_markup(f"<b>Utter {VERSION}</b>")
    check = Gtk.Button(label="Check for updates", halign=Gtk.Align.START)
    update_status = Gtk.Label(xalign=0, wrap=True)
    download = Gtk.LinkButton(uri=f"https://github.com/{REPO}/releases/latest", label="Download the new version",
                              halign=Gtk.Align.START)
    download.set_no_show_all(True)

    def show_result(latest, url, error) -> bool:
        check.set_sensitive(True)
        if error:
            update_status.set_text(f"Couldn't check for updates ({error}).")
        elif version_tuple(latest) > version_tuple(VERSION):
            update_status.set_text(f"Utter {latest} is available. You have {VERSION}.")
            download.set_uri(url)
            download.show()
        else:
            update_status.set_text(f"You're up to date ({VERSION}).")
            download.hide()
        return False

    def fetch_latest() -> None:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": f"Utter/{VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                rel = json.load(r)
            GLib.idle_add(show_result, rel.get("tag_name", "").lstrip("v"), rel.get("html_url", ""), None)
        except (OSError, ValueError) as e:
            GLib.idle_add(show_result, "", "", getattr(e, "reason", None) or str(e))

    def on_check(*_):
        check.set_sensitive(False)
        update_status.set_text("Checking…")
        threading.Thread(target=fetch_latest, daemon=True).start()

    check.connect("clicked", on_check)

    for w in (key_title, key_row, key_status, hold, hold_note, autostart, autostart_note, title, live, once, note, insert_title, by_paste, by_type, insert_note, stt_title, *stt_buttons, stt_note, clean_title, *clean_buttons, clean_note, lang_title, english, mixed, hindi_title, roman, deva, hindi_note, mic_title,
              mics, mic_note, about_title, check, update_status, download):
        page.pack_start(w, False, False, 0)
    page.mics = mics
    return page


# ---- the keyboard shortcut lives in GNOME's custom shortcuts, so Settings edits that entry --------------------------
GNOME_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
UTTER_KEY_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/utter/"


def gset(*args) -> str:
    return subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=5).stdout.strip()


def shortcut_entries():
    """GNOME's custom shortcuts as [(path, binding, command)], or None when they can't be read (not GNOME)."""
    try:
        raw = gset("get", GNOME_KEYS, "custom-keybindings")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not raw.startswith(("[", "@as [")):
        return None
    out = []
    for path in ast.literal_eval(raw.removeprefix("@as ")):
        node = f"{GNOME_KEYS}.custom-keybinding:{path}"
        out.append((path, gset("get", node, "binding").strip("'"), gset("get", node, "command").strip("'"),
                    gset("get", node, "name").strip("'")))
    return out


def is_utter(command: str) -> bool:
    return bool(command) and os.path.basename(command.split()[0]) == "utter"


def current_shortcut():
    """Utter's shortcut as a GNOME accelerator ('<Alt>d'), '' if none is set, or None if this isn't GNOME."""
    entries = shortcut_entries()
    if entries is None:
        return None
    return next((b for _p, b, c, _n in entries if is_utter(c)), "")


def set_shortcut(accel: str) -> str:
    """Bind accel to Utter. Returns an error message, or '' on success. Never rewrites a list it could not read."""
    entries = shortcut_entries()
    if entries is None:
        return "Couldn't read your keyboard shortcuts."
    clash = next((n or c for _p, b, c, n in entries if b.lower() == accel.lower() and not is_utter(c)), None)
    if clash:
        return f"That key is already used by another shortcut: {clash}."
    path = next((p for p, _b, c, _n in entries if is_utter(c)), None)
    command = next((c for c in (os.path.expanduser("~/.local/bin/utter"), "/usr/bin/utter") if os.access(c, os.X_OK)),
                   "utter")
    if path is None:  # no entry yet: add one, appended to the list as read
        path = UTTER_KEY_PATH
        gset("set", GNOME_KEYS, "custom-keybindings", str([e[0] for e in entries] + [path]))
        gset("set", f"{GNOME_KEYS}.custom-keybinding:{path}", "name", "Utter: start or stop dictation")
        gset("set", f"{GNOME_KEYS}.custom-keybinding:{path}", "command", command)
    gset("set", f"{GNOME_KEYS}.custom-keybinding:{path}", "binding", accel)
    return ""


def pretty_accel(accel: str) -> str:
    if not accel:
        return "None"
    key, mods = Gtk.accelerator_parse(accel)
    return Gtk.accelerator_get_label(key, mods) or accel


def version_tuple(v: str) -> tuple:
    """'1.10.0' -> (1, 10, 0), so versions compare as numbers; anything unparsable counts as oldest."""
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


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


def scrollable(page: Gtk.Widget) -> Gtk.Widget:
    """Pages longer than the window scroll instead of being cut off."""
    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
    scroll.add(page)
    return scroll


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
        stack.add_titled(scrollable(keys_page()), "keys", "API keys")
        settings = settings_page()
        stack.add_titled(scrollable(settings), "settings", "Settings")
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
    tray_icon = os.path.join(HERE, "utter-tray.svg")
    ind = AI.Indicator.new("utter", "audio-input-microphone-symbolic", AI.IndicatorCategory.APPLICATION_STATUS)
    if os.path.exists(tray_icon):  # Utter's own mark in the top bar; the stock mic icon only if it is missing
        ind.set_icon_theme_path(HERE)
        ind.set_icon_full("utter-tray", "Utter")
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
