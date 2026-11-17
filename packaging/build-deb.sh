#!/usr/bin/env bash
# Builds dist/utter_<version>_amd64.deb from the PyInstaller folder dist/Utter (run pyinstaller packaging/utter.spec
# first). Usage: packaging/build-deb.sh [version]
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${1:-$(sed -n 's/^VERSION = "\(.*\)".*/\1/p' src/utter/__init__.py)}"
[ -x dist/Utter/utter-app ] || { echo "dist/Utter/utter-app is missing: run pyinstaller packaging/utter.spec first"; exit 1; }
ROOT="build/utter_${VERSION}_amd64"
rm -rf "$ROOT"
mkdir -p "$ROOT/DEBIAN" "$ROOT/usr/lib" "$ROOT/usr/bin" "$ROOT/usr/lib/systemd/user" \
         "$ROOT/usr/share/applications" "$ROOT/usr/share/icons/hicolor/scalable/apps" "$ROOT/usr/share/doc/utter"

cp -a dist/Utter "$ROOT/usr/lib/utter"
install -m 755 bin/utter "$ROOT/usr/bin/utter"
# hold-to-talk's optional root helper uses the system Python and its standard library only
install -m 755 src/utter-hold.py install-hold.sh "$ROOT/usr/lib/utter/"
sed 's|^ExecStart=.*|ExecStart=/usr/lib/utter/utter-app|' data/utter.service > "$ROOT/usr/lib/systemd/user/utter.service"
chmod 644 "$ROOT/usr/lib/systemd/user/utter.service"
install -m 644 data/utter.desktop "$ROOT/usr/share/applications/utter.desktop"
install -m 644 data/utter.svg "$ROOT/usr/share/icons/hicolor/scalable/apps/utter.svg"
install -m 644 LICENSE "$ROOT/usr/share/doc/utter/copyright"

cat > "$ROOT/DEBIAN/control" <<EOF
Package: utter
Version: $VERSION
Section: utils
Priority: optional
Architecture: amd64
Depends: pulseaudio-utils, ydotool, wl-clipboard, libnotify-bin, ffmpeg, libglib2.0-0t64 | libglib2.0-0, libgtk-3-0t64 | libgtk-3-0, libdbus-1-3, libfontconfig1, libfreetype6, libharfbuzz0b, libx11-6, libx11-xcb1, libxkbcommon0, libxkbcommon-x11-0, libxcb1, libxcb-cursor0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-randr0, libxcb-render0, libxcb-render-util0, libxcb-shape0, libxcb-shm0, libxcb-sync1, libxcb-xfixes0, libxcb-xinerama0, libxcb-xkb1, libxcb-glx0, libegl1, libffi8, libexpat1
Recommends: gnome-shell-extension-appindicator, python3
Maintainer: ${DEB_MAINTAINER:-$(git config user.name) <$(git config user.email)>}
Homepage: https://github.com/arunkukrety/utter
Description: Push-to-talk dictation
 Press a key, speak, and your words are pasted into the focused window.
 Copies every dictation to the clipboard and a local History, and shows a
 small popup with live voice bars while you speak.
EOF

# start for every user at login; ydotool's own user service does the pasting
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
mkdir -p dist
dpkg-deb --build --root-owner-group -Zxz "$ROOT" "dist/utter_${VERSION}_amd64.deb" >/dev/null
echo "dist/utter_${VERSION}_amd64.deb"
