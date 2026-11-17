#!/bin/sh
# Adds Utter's apt repository, so `sudo apt install utter` works and `apt upgrade` keeps it up to date.
#   curl -fsSL https://arunkukrety.github.io/utter/setup.sh | sudo sh
# To undo: sudo rm /etc/apt/sources.list.d/utter.list /etc/apt/keyrings/utter.gpg
set -e
REPO=https://arunkukrety.github.io/utter
[ "$(id -u)" = 0 ] || { echo "Run it with sudo: curl -fsSL $REPO/setup.sh | sudo sh"; exit 1; }
[ "$(dpkg --print-architecture)" = amd64 ] || { echo "Utter is built for 64-bit Intel/AMD (amd64) only."; exit 1; }
command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl; }

install -d -m 755 /etc/apt/keyrings
curl -fsSL "$REPO/utter.gpg" -o /etc/apt/keyrings/utter.gpg  # the key every release is signed with
chmod 644 /etc/apt/keyrings/utter.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/utter.gpg] $REPO stable main" > /etc/apt/sources.list.d/utter.list
apt-get update -qq
echo
echo "Utter's repository is added. Install it with:  sudo apt install utter"
