#!/usr/bin/env bash
# Removes Utter. Keeps your API key, settings and History unless you pass --purge.
set -euo pipefail
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"

systemctl --user disable --now utter.service 2>/dev/null || true
rm -f "$CONFIG/systemd/user/utter.service" "$HOME/.local/bin/utter" \
      "$DATA/applications/utter.desktop" "$DATA/icons/hicolor/scalable/apps/utter.svg"
rm -rf "$DATA/utter/app"
systemctl --user daemon-reload

SCHEMA=org.gnome.settings-daemon.plugins.media-keys
KPATH=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/utter/
if gsettings list-keys "$SCHEMA" >/dev/null 2>&1; then
  list=$(gsettings get "$SCHEMA" custom-keybindings)
  list=$(python3 -c "import ast,sys; l=[p for p in ast.literal_eval(sys.argv[1].replace('@as ','')) if p!=sys.argv[2]]; print(str(l))" "$list" "$KPATH")
  gsettings set "$SCHEMA" custom-keybindings "$list"
  gsettings reset-recursively "$SCHEMA.custom-keybinding:$KPATH" 2>/dev/null || true
fi

if [ "${1:-}" = --purge ]; then
  rm -rf "$CONFIG/utter" "$DATA/utter"
  echo "Utter removed, with your key, settings and History."
else
  echo "Utter removed. Your key, settings and History are still in $CONFIG/utter and $DATA/utter (use --purge to delete them)."
fi
