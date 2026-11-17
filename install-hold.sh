#!/usr/bin/env bash
# Hold-to-talk for Utter: hold Alt alone for 0.5 s to dictate, release to stop. Run once with sudo:
#   sudo /usr/lib/utter/install-hold.sh     (uninstall: add --remove; from a checkout: sudo ./install-hold.sh)
# Reading the keyboard needs root, so this installs a small root-owned service OUTSIDE your home folder (nothing your
# user can edit runs as root), instead of putting you in the "input" group, which would let any of your programs read
# every keystroke. The service only watches for the Alt key and signals Utter.
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "Run with sudo: sudo $0"; exit 1; }
USER_NAME="${SUDO_USER:?run this with sudo from your own account}"

if [ "${1:-}" = --remove ]; then
  systemctl disable --now utter-hold.service 2>/dev/null || true
  rm -f /etc/systemd/system/utter-hold.service /usr/local/lib/utter/utter-hold.py
  systemctl daemon-reload
  echo "Hold-to-talk removed. Alt+D still works."
  exit 0
fi

install -d -m 755 -o root -g root /usr/local/lib/utter
HELPER=src/utter-hold.py
[ -f "$HELPER" ] || HELPER=utter-hold.py  # installed by the .deb next to this script
install -m 755 -o root -g root "$HELPER" /usr/local/lib/utter/utter-hold.py
cat > /etc/systemd/system/utter-hold.service <<EOF
[Unit]
Description=Utter hold-to-talk: hold Alt alone to dictate (reads key events only, signals Utter)

[Service]
ExecStart=/usr/bin/python3 /usr/local/lib/utter/utter-hold.py --user $USER_NAME
Restart=always
RestartSec=2
NoNewPrivileges=yes
ProtectHome=read-only
PrivateTmp=yes
PrivateNetwork=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable utter-hold.service >/dev/null 2>&1
systemctl restart utter-hold.service  # restart, not just start: a reinstall must load the new code
sleep 1
systemctl is-active utter-hold.service >/dev/null && echo "Hold-to-talk is on: hold Alt alone for half a second to dictate, release to stop."
