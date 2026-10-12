#!/usr/bin/env bash
# install_field_services.sh — productionise the afl-cam rig.
#
# One idempotent command that turns the board into a GoPro-style recorder:
#   power on -> wait ~40s -> phone joins "afl-cam" -> opens http://aflcam.local/
#
# Installs and enables:
#   /usr/local/bin/mediamtx            MediaMTX v1.21.2 (checksum-verified)
#   /usr/local/etc/mediamtx.yml        preview server config (deploy/)
#   hostapd (apt) + /etc/hostapd/afl-cam.conf
#   systemd units:                     mediamtx, afl-camd, gpu-governor,
#                                      afl-cam-ap (boot-time field AP)
#   /etc/avahi/services/aflcam.service aflcam.local mDNS alias
# AP note (verified 2026-10-09): NM's AP mode hangs on this board's bcmdhd
# driver — the AP runs hostapd+dnsmasq (afl-cam-ap.service brings it up at
# boot only when no home WiFi is connected; NM keeps the home client job).
#
# The phone page is the only interface (desk tools live in its Setup tab).
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
    cp deploy/systemd/afl-cam-ap.service /etc/systemd/system/
    mkdir -p /etc/avahi/services /etc/hostapd
    cp deploy/avahi/aflcam.service /etc/avahi/services/
    cp deploy/avahi/hosts /etc/avahi/hosts
    cp deploy/hostapd-afl-cam.conf /etc/hostapd/afl-cam.conf
    systemctl daemon-reload
    systemctl enable mediamtx afl-camd gpu-governor afl-cam-ap
    systemctl restart avahi-daemon gpu-governor mediamtx afl-camd afl-cam-ap
"

echo "[install] hostapd (NM AP mode hangs on this bcmdhd board)…"
if ! which hostapd > /dev/null 2>&1; then
    $SUDO apt-get install -y hostapd
fi

echo "[install] neutering the legacy NM AP profile (its activations wedge the WiFi)…"
if nmcli -t -f NAME con show | grep -qx "afl-cam-ap"; then
    $SUDO nmcli con modify afl-cam-ap connection.autoconnect no
fi

echo
echo "[install] done. From now on: power on -> join 'afl-cam' (aflcam2026)"
echo "           -> open http://aflcam.local/  (or http://10.42.0.1/)."
echo "           At home the board joins the house WiFi instead:"
echo "           http://orangepi5-max.local/ works there too."
