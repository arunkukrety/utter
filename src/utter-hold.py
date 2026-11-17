#!/usr/bin/env python3
"""Hold-to-talk for Utter: hold Alt on its own for the hold time (Settings, default 0.5 s) to start, release to stop.

  - Alt held alone for the hold time  -> start (signal the Utter service: SIGUSR1)
  - Alt released after it started     -> stop and paste (SIGUSR1 again)
  - any other key while Alt is held   -> before the start: nothing happens (Alt+Tab, Alt+D stay normal);
                                         after the start: cancel, nothing is pasted (SIGRTMIN)

Reading key events needs access to /dev/input, which can see every keystroke, so this runs as a small root-owned
system service (installed by install-hold.sh outside your home folder) instead of giving your user that access. It reads
only key codes, keeps no record of them, and talks to Utter by signal alone.
Usage: utter-hold.py --user <name>
"""
import os, pwd, re, select, signal, struct, sys, time

HOLD_S = 0.5  # default; Settings can change it ("hold_seconds"), read at each press so no reinstall is needed
ALT = {56, 100}  # KEY_LEFTALT, KEY_RIGHTALT
EVENT = struct.Struct("llHHi")  # struct input_event on 64-bit Linux: time (2 longs), type, code, value
EV_KEY = 1


def keyboards():
    """Real keyboards from /proc/bus/input/devices, skipping ydotool's virtual one (Utter's own typing)."""
    found = []
    for block in open("/proc/bus/input/devices").read().split("\n\n"):
        name = re.search(r'N: Name="([^"]*)"', block)
        handlers = re.search(r"H: Handlers=(.*)", block)
        ev = re.search(r"B: EV=(\w+)", block)
        if not (name and handlers and ev) or "kbd" not in handlers.group(1):
            continue
        if "ydotool" in name.group(1).lower() or not int(ev.group(1), 16) & (1 << EV_KEY):
            continue
        event = re.search(r"event\d+", handlers.group(1))
        if event:
            found.append(f"/dev/input/{event.group(0)}")
    return found


def utter_pid(uid: int):
    try:
        with open(f"/run/user/{uid}/utter.ready") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def settings(home: str) -> dict:
    try:
        import json
        with open(os.path.join(home, ".config", "utter", "settings.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def enabled(home: str) -> bool:
    """Settings > Hold Alt to talk (on unless switched off). Read at each press, so the switch works at once."""
    return settings(home).get("hold_to_talk", True) is not False


def hold_seconds(home: str) -> float:
    try:
        return min(3.0, max(0.2, float(settings(home).get("hold_seconds", HOLD_S))))
    except (TypeError, ValueError):
        return HOLD_S


def send(uid: int, sig) -> None:
    pid = utter_pid(uid)
    if pid:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def main() -> None:
    user = sys.argv[sys.argv.index("--user") + 1] if "--user" in sys.argv else os.environ.get("SUDO_USER")
    entry = pwd.getpwnam(user)
    uid, home = entry.pw_uid, entry.pw_dir
    while True:  # keyboards come and go (USB, Bluetooth): reopen on any error
        fds = {}
        for path in keyboards():
            try:
                fds[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = path
            except OSError:
                pass
        alt_down_at, started, spoiled, hold = None, False, False, HOLD_S
        rescan_at = time.monotonic() + 30
        try:
            while True:
                timeout = None
                if alt_down_at is not None and not started and not spoiled:
                    timeout = max(0.0, alt_down_at + hold - time.monotonic())
                ready, _, _ = select.select(list(fds), [], [], min(timeout, 1.0) if timeout is not None else 1.0)
                now = time.monotonic()
                if alt_down_at is not None and not started and not spoiled and now - alt_down_at >= hold:
                    started = True
                    if enabled(home):
                        send(uid, signal.SIGUSR1)  # start
                    else:
                        spoiled = True  # switched off: behave as if nothing happened
                for fd in ready:
                    data = os.read(fd, EVENT.size * 64)
                    for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                        _s, _us, etype, code, value = EVENT.unpack_from(data, off)
                        if etype != EV_KEY or value == 2:  # ignore auto-repeat
                            continue
                        if code in ALT:
                            if value == 1 and alt_down_at is None:
                                alt_down_at, started, spoiled, hold = now, False, False, hold_seconds(home)
                            elif value == 0 and alt_down_at is not None:
                                if started and not spoiled:
                                    send(uid, signal.SIGUSR1)  # stop and paste
                                alt_down_at, started, spoiled = None, False, False
                        elif value == 1 and alt_down_at is not None and not spoiled:
                            spoiled = True  # Alt + another key: a shortcut, not hold-to-talk
                            if started:
                                send(uid, signal.SIGRTMIN)  # cancel the dictation already running
                if now > rescan_at and alt_down_at is None:
                    break  # pick up keyboards plugged in since
        except OSError:
            time.sleep(1)
        finally:
            for fd in fds:
                os.close(fd)
        if not fds:
            time.sleep(5)


if __name__ == "__main__":
    main()
