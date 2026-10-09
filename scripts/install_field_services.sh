#!/usr/bin/env bash
# install_field_services.sh — productionise the afl-cam rig.
#
# One idempotent command that turns the board into a GoPro-style recorder:
#   power on -> wait ~40s -> phone joins "afl-cam" -> opens http://aflcam.local/
#
# Installs and enables:
#   /usr/local/bin/mediamtx            MediaMTX v1.21.2 (checksum-verified)
#   /usr/local/etc/mediamtx.yml        preview server config (deploy/)
#   systemd units:                     mediamtx, afl-camd, gpu-governor
#   /etc/avahi/services/aflcam.service aflcam.local mDNS alias
# and verifies the afl-cam-ap NetworkManager profile autoconnects
# (priority -999: the home WiFi wins at home; at the ground, where the
# home network is absent, the board's AP comes up by itself — no scripts).
#
# The GTK UI (afl-cam ui) is the desk tool: stop afl-camd first
# (systemctl stop afl-camd) — daemon and UI can't share the cameras.
set -eu

cd "$(dirname "$0")/.."
MTX_VERSION="v1.21.2"
MTX_SHA256="180a235374a548d9133cdd5e20f5dbd8b01317be1c86e0d7e66df585dc42ed6f"

SUDO="sudo -S -p ''"

echo "[install] MediaMTX $MTX_VERSION…"
if [ ! -x /usr/local/bin/mediamtx ] || ! /usr/local/bin/mediamtx --version 2>/dev/null | grep -q "$MTX_VERSION"; then
    TMP=$(mktemp -d)
    curl -sL -o "$TMP/mediamtx.tar.gz" \
        "https://github.com/bluenviron/mediamtx/releases/download/$MTX_VERSION/mediamtx_${MTX_VERSION#v}_linux_arm64.tar.gz"
    echo "$MTX_SHA256  $TMP/mediamtx.tar.gz" | sha256sum -c -
    tar xzf "$TMP/mediamtx.tar.gz" -C "$TMP"
    $SUDO bash -c "install -m 0755 '$TMP/mediamtx' /usr/local/bin/mediamtx"
    rm -rf "$TMP"
fi

echo "[install] config, units, mDNS alias…"
$SUDO bash -c "
    cp deploy/mediamtx.yml /usr/local/etc/mediamtx.yml
    cp deploy/systemd/mediamtx.service /etc/systemd/system/
    cp deploy/systemd/afl-camd.service /etc/systemd/system/
    cp deploy/systemd/gpu-governor.service /etc/systemd/system/
    mkdir -p /etc/avahi/services
    cp deploy/avahi/aflcam.service /etc/avahi/services/
    cp deploy/avahi/hosts /etc/avahi/hosts
    systemctl daemon-reload
    systemctl enable mediamtx afl-camd gpu-governor
    systemctl restart avahi-daemon gpu-governor mediamtx afl-camd
"

echo "[install] verifying the AP profile autoconnects (field boot = board AP)…"
if nmcli -t -f NAME con show | grep -qx "afl-cam-ap"; then
    $SUDO nmcli con modify afl-cam-ap \
        connection.autoconnect yes connection.autoconnect-priority -999
else
    echo "[install] WARNING: afl-cam-ap profile missing — create it with:"
    echo "  nmcli con add type wifi ifname wlan0 con-name afl-cam-ap mode ap \\"
    echo "    ssid afl-cam wifi-sec.key-mgmt wpa-psk wifi-sec.psk aflcam2026 \\"
    echo "    ipv4.method shared connection.autoconnect yes \\"
    echo "    connection.autoconnect-priority -999"
fi

echo
echo "[install] done. From now on: power on -> join 'afl-cam' (aflcam2026)"
echo "           -> open http://aflcam.local/  (or http://10.42.0.1/)."
echo "           At home the board joins the house WiFi instead:"
echo "           http://aflcam.local/ works there too."
