#!/usr/bin/env bash
# Builds dist/utter_<version>_all.deb.   Usage: packaging/build-deb.sh [version]
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${1:-1.0.0}"
ROOT="build/utter_${VERSION}_all"
rm -rf "$ROOT"
mkdir -p "$ROOT/DEBIAN" "$ROOT/usr/lib/utter/sounds" "$ROOT/usr/bin" "$ROOT/usr/lib/systemd/user" \
         "$ROOT/usr/share/applications" "$ROOT/usr/share/icons/hicolor/scalable/apps" "$ROOT/usr/share/doc/utter" dist

install -m 644 src/utter-daemon.py src/utter-popup.py "$ROOT/usr/lib/utter/"
install -m 644 sounds/start.wav sounds/stop.wav "$ROOT/usr/lib/utter/sounds/"
install -m 755 bin/utter "$ROOT/usr/bin/utter"
sed 's|%h/.local/share/utter/app/|/usr/lib/utter/|' data/utter.service > "$ROOT/usr/lib/systemd/user/utter.service"
chmod 644 "$ROOT/usr/lib/systemd/user/utter.service"
install -m 644 data/utter.desktop "$ROOT/usr/share/applications/utter.desktop"
install -m 644 data/utter.svg "$ROOT/usr/share/icons/hicolor/scalable/apps/utter.svg"
install -m 644 LICENSE "$ROOT/usr/share/doc/utter/copyright"

cat > "$ROOT/DEBIAN/control" <<EOF
Package: utter
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3, python3-websockets, python3-gi, gir1.2-gtk-3.0, pulseaudio-utils, ydotool, wl-clipboard, libnotify-bin
Recommends: gir1.2-ayatanaappindicator3-0.1, gnome-shell-extension-appindicator
Maintainer: $(git config user.name) <$(git config user.email)>
Homepage: https://github.com/arunkukrety/utter
Description: Push-to-talk dictation for Linux
 Press a key, speak, and your words are typed into the focused window.
 Uses Deepgram's live speech-to-text (bring your own API key), copies every
 dictation to the clipboard and a local History, and shows a small popup
 with live voice bars while you speak.
EOF

# start for every user at login; ydotool's own user service does the typing
cat > "$ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
  systemctl --global enable utter.service >/dev/null 2>&1 || true
  if [ -f /usr/lib/systemd/user/ydotool.service ]; then
    systemctl --global enable ydotool.service >/dev/null 2>&1 || true
  fi
  command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
  echo "Utter installed. Open it from your apps, or log out and back in; then press Alt+D to dictate."
fi
EOF
cat > "$ROOT/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = purge ]; then
  systemctl --global disable utter.service >/dev/null 2>&1 || true
fi
EOF
chmod 755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/prerm"

find "$ROOT" -type d -exec chmod 755 {} +
dpkg-deb --build --root-owner-group "$ROOT" "dist/utter_${VERSION}_all.deb" >/dev/null
echo "dist/utter_${VERSION}_all.deb"
