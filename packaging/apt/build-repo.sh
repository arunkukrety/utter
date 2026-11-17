#!/usr/bin/env bash
# Builds the signed apt repository that GitHub Pages serves at https://arunkukrety.github.io/utter/
#   packaging/apt/build-repo.sh <utter_x.y.z_amd64.deb> <out dir> <gpg key id>
# The signing key must already be in the gpg keyring (CI imports it from the APT_SIGNING_KEY secret).
set -euo pipefail
DEB="$1" OUT="$2" KEY="$3"
HERE="$(cd "$(dirname "$0")" && pwd)"
rm -rf "$OUT"
mkdir -p "$OUT/pool/main" "$OUT/dists/stable/main/binary-amd64"
cp "$DEB" "$OUT/pool/main/"
cd "$OUT"
apt-ftparchive packages pool > dists/stable/main/binary-amd64/Packages
gzip -9 -k dists/stable/main/binary-amd64/Packages
apt-ftparchive -o APT::FTPArchive::Release::Origin=Utter -o APT::FTPArchive::Release::Label=Utter \
  -o APT::FTPArchive::Release::Suite=stable -o APT::FTPArchive::Release::Codename=stable \
  -o APT::FTPArchive::Release::Architectures=amd64 -o APT::FTPArchive::Release::Components=main \
  release dists/stable > dists/stable/Release
gpg --batch --yes --local-user "$KEY" --clearsign -o dists/stable/InRelease dists/stable/Release
gpg --batch --yes --local-user "$KEY" --armor --detach-sign -o dists/stable/Release.gpg dists/stable/Release
gpg --export "$KEY" > utter.gpg  # binary keyring, as signed-by expects
install -m 644 "$HERE/setup.sh" setup.sh
touch .nojekyll  # serve every file as is
echo "apt repository in $OUT"
