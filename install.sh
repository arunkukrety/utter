#!/usr/bin/env bash
# Installs Utter for the current user. No sudo needed unless packages are missing (it prints the apt line).
#   ./install.sh    install, ask for your Deepgram key, start Utter (it binds Alt+D on GNOME if that key is free)
set -euo pipefail
cd "$(dirname "$0")"

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
APP="$DATA/utter/app"
BIN="$HOME/.local/bin"
UNIT_DIR="$CONFIG/systemd/user"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

# ---- 1. dependencies ------------------------------------------------------------------------------------------------
missing=()
need_cmd() { command -v "$1" >/dev/null 2>&1 || missing+=("$2"); }
need_cmd python3 python3
need_cmd parecord pulseaudio-utils
need_cmd pactl pulseaudio-utils
need_cmd paplay pulseaudio-utils
need_cmd ydotool ydotool
need_cmd wl-copy wl-clipboard
need_cmd notify-send libnotify-bin
python3 -c 'import websockets' 2>/dev/null || missing+=(python3-websockets)
python3 -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' 2>/dev/null \
  || missing+=(python3-gi gir1.2-gtk-3.0)
python3 -c 'import gi; gi.require_version("AyatanaAppIndicator3", "0.1")' 2>/dev/null \
  || missing+=(gir1.2-ayatanaappindicator3-0.1)
if [ ${#missing[@]} -gt 0 ]; then
  pkgs=$(printf '%s\n' "${missing[@]}" | sort -u | tr '\n' ' ')
  say "Missing packages. Install them, then run ./install.sh again:"
  echo "  sudo apt install $pkgs"
  exit 1
fi

# ---- 2. ydotool needs its daemon running to type for you --------------------------------------------------------------
if systemctl --user cat ydotool.service >/dev/null 2>&1; then
  systemctl --user enable --now ydotool.service >/dev/null 2>&1 || true
fi
if ! pgrep -x ydotoold >/dev/null; then
  say "Warning: ydotoold is not running, so Utter cannot type for you."
  echo "  On Ubuntu: systemctl --user enable --now ydotool"
  echo "  Elsewhere, start ydotoold and make sure you can write /dev/uinput (see README, Troubleshooting)."
fi

# ---- 3. files -----------------------------------------------------------------------------------------------------
say "Installing Utter…"
mkdir -p "$APP/sounds" "$BIN" "$UNIT_DIR" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps" "$CONFIG/utter"
chmod 700 "$CONFIG/utter"
install -m 644 src/utter-daemon.py src/utter-popup.py "$APP/"
install -m 644 sounds/start.wav sounds/stop.wav "$APP/sounds/"
install -m 755 bin/utter "$BIN/utter"
install -m 644 data/utter.service "$UNIT_DIR/utter.service"
install -m 644 data/utter.desktop "$DATA/applications/utter.desktop"
install -m 644 data/utter.svg "$DATA/icons/hicolor/scalable/apps/utter.svg"
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" 2>/dev/null || true

# ---- 4. Deepgram key --------------------------------------------------------------------------------------------------
KEY_FILE="$CONFIG/utter/deepgram.key"
if [ ! -s "$KEY_FILE" ] && [ -t 0 ]; then
  echo
  echo "Utter uses Deepgram for speech-to-text. Get a key at https://console.deepgram.com (new accounts get free credit)."
  read -rsp "Paste your Deepgram API key (Enter to add it later in Settings): " key; echo
  if [ -n "$key" ]; then
    (umask 077; printf '%s\n' "$key" > "$KEY_FILE")
    echo "Saved to $KEY_FILE (readable by you only)."
  fi
fi

# ---- 5. service ---------------------------------------------------------------------------------------------------
systemctl --user daemon-reload
systemctl --user enable utter.service >/dev/null 2>&1
systemctl --user restart utter.service

echo
say "Utter is installed and running."
echo "  Press Alt+D to start dictating, and again to stop. (If Alt+D was taken, a notice says so: bind any key to"
echo "  $BIN/utter in Settings > Keyboard > Custom Shortcuts.)"
echo "  Open the window (history, API key, settings) from the microphone icon in the top bar, or run: utter settings"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "  Note: $BIN is not on your PATH, so call it as $BIN/utter" ;; esac
