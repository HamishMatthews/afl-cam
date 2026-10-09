#!/usr/bin/env bash
# install_field_services.sh — deploy the afl-camd field stack
# (iphone-control-plan.md §2: cam2stitch + afl-camd + MediaMTX).
#
# Installs:
#   /usr/local/bin/mediamtx             (v1.21.2, arm64 — re-downloaded
#                                       and checksum-verified if missing)
#   /usr/local/etc/mediamtx.yml         (from deploy/mediamtx.yml)
#   /etc/systemd/system/mediamtx.service
#   /etc/systemd/system/afl-camd.service
# and enables both. The services run at every boot; the pipeline itself
# stays idle (cameras closed) until a phone connects or REC is pressed.
#
# The GTK UI (afl-cam ui) must NOT run while afl-camd is enabled — both
# want the cameras. Use one or the other.
set -eu

cd "$(dirname "$0")/.."
MTX_VERSION="v1.21.2"
MTX_SHA256="180a235374a548d9133cdd5e20f5dbd8b01317be1c86e0d7e66df585dc42ed6f"

needs_sudo() { [ "$(id -u)" -ne 0 ]; }
SUDO=""
if needs_sudo; then
    SUDO="sudo -S -p ''"
fi

echo "[install] checking MediaMTX $MTX_VERSION…"
if [ ! -x /usr/local/bin/mediamtx ] || ! /usr/local/bin/mediamtx --version 2>/dev/null | grep -q "$MTX_VERSION"; then
    TMP=$(mktemp -d)
    curl -sL -o "$TMP/mediamtx.tar.gz" \
        "https://github.com/bluenviron/mediamtx/releases/download/$MTX_VERSION/mediamtx_${MTX_VERSION#v}_linux_arm64.tar.gz"
    echo "$MTX_SHA256  $TMP/mediamtx.tar.gz" | sha256sum -c -
    tar xzf "$TMP/mediamtx.tar.gz" -C "$TMP"
    $SUDO bash -c "install -m 0755 '$TMP/mediamtx' /usr/local/bin/mediamtx"
    rm -rf "$TMP"
    echo "[install] MediaMTX installed"
else
    echo "[install] MediaMTX up to date"
fi

echo "[install] writing config + units…"
$SUDO bash -c "
    cp deploy/mediamtx.yml /usr/local/etc/mediamtx.yml
    cp deploy/systemd/mediamtx.service /etc/systemd/system/mediamtx.service
    cp deploy/systemd/afl-camd.service /etc/systemd/system/afl-camd.service
    systemctl daemon-reload
    systemctl enable mediamtx afl-camd
    systemctl restart mediamtx
    systemctl restart afl-camd || true
"
echo "[install] done. Check with:"
echo "    systemctl status mediamtx afl-camd"
echo "    curl -s http://localhost/api/status"
echo "On the field: scripts/field_wifi.sh ap, then the phone opens"
echo "http://10.42.0.1:80/ (or the QR code on the rig)."
