#!/bin/bash
# field_wifi.sh — field network helper for the afl-cam rig (iPhone control).
#
# NM's AP mode is BROKEN on this board's bcmdhd driver (verified 2026-10-09:
# nmcli AP activation + even `nmcli con down` hang as an unprivileged user
# behind the wedged polkit/gnome-shell agent, and as root wpa_supplicant
# never completes AP bring-up — "802.1X supplicant took too long"). The
# working path is hostapd on wlan0 + dnsmasq, which this script manages.
# NM keeps its job: the home-WiFi CLIENT connection.
#
#   field_wifi.sh ap      — take down the home WiFi, run hostapd+dnsmasq.
#                           iPhone: join WiFi "afl-cam" (password aflcam2026),
#                           open http://aflcam.local/ (or http://10.42.0.1/).
#                           At the ground this runs AUTOMATICALLY at boot
#                           (afl-cam-ap.service: waits ~45s; if no home
#                           WiFi connected, brings the AP up).
#   field_wifi.sh follow  — board joins the iPhone's Personal Hotspot
#                           (for when the phone is already sharing).
#   field_wifi.sh stop    — leave field mode, restore the home WiFi.
set -u
NM="nmcli"
SUDO_PASS="${AFL_CAM_SUDO_PASS:-123}"

# passwordless first (root under systemd, or a NOPASSWD rule), else the
# board's documented sudo password (CLAUDE.md convention)
sudo_cmd() {
    sudo -n "$@" 2>/dev/null || echo "$SUDO_PASS" | sudo -S -p '' "$@"
}

AP_PROFILE="afl-cam-ap"          # legacy NM profile — no longer activated
HOTSPOT_PROFILE="afl-cam-hotspot"
HOSTAPD_CONF="/home/hamish/Documents/afl-cam/deploy/hostapd-afl-cam.conf"
HOTSPOT_SSID="${HOTSPOT_SSID:-}"
HOTSPOT_PASSWORD="${HOTSPOT_PASSWORD:-}"
[ -n "$HOTSPOT_SSID" ] || HOTSPOT_SSID="hammer's telephone"
[ -n "$HOTSPOT_PASSWORD" ] || HOTSPOT_PASSWORD="despacito"

log() { echo "[field_wifi] $*"; }

board_ip() {
    ip -4 addr show dev wlan0 2>/dev/null | awk '/inet/ && !/127.0.0.1/ {print $2; exit}'
}

home_active() {
    # a non-afl-cam wireless profile is activated
    $NM -t -f NAME,TYPE con show --active 2>/dev/null \
        | awk -F: '$2 ~ /wireless/ && $1 !~ /afl-cam/ {print $1; exit}'
}

profile_active() {
    $NM -t -f NAME con show --active 2>/dev/null | grep -qx "$1"
}

home_profile() {
    # First non-afl-cam wifi profile (netplan-wlan0-* or similar)
    $NM -t -f NAME,TYPE con show 2>/dev/null \
        | awk -F: '$2 ~ /wireless/ && $1 !~ /afl-cam/ {print $1; exit}'
}

start_hostapd() {
    if pgrep -f "hostapd .*afl-cam" > /dev/null; then
        log "hostapd already running"
        return 0
    fi
    log "starting hostapd (SSID afl-cam / aflcam2026)..."
    sudo_cmd nohup hostapd "$HOSTAPD_CONF" > /tmp/hostapd.log 2>&1 &
    sleep 3
    if pgrep -f "hostapd .*afl-cam" > /dev/null; then
        log "hostapd up"
    else
        log "ERROR: hostapd failed to start — see /tmp/hostapd.log"
        return 1
    fi
    sudo_cmd ip addr add 10.42.0.1/24 dev wlan0 2>/dev/null || true
    if ! pgrep -x dnsmasq > /dev/null; then
        sudo_cmd nohup dnsmasq --interface=wlan0 --bind-interfaces \
            --dhcp-range=10.42.0.50,10.42.0.150,12h \
            --dhcp-option=option:dns-server,10.42.0.1 \
            --no-resolv --address=/#/10.42.0.1 > /tmp/dnsmasq.log 2>&1 &
    fi
    return 0
}

stop_hostapd() {
    sudo_cmd pkill -f "hostapd .*afl-cam" 2>/dev/null || true
    sudo_cmd pkill -x dnsmasq 2>/dev/null || true
    sudo_cmd ip addr flush dev wlan0 2>/dev/null || true
    sleep 1
}

cmd_ap() {
    # Home WiFi must be released first (hostapd needs the interface).
    # NOTE: nmcli deactivation only works with sudo on this board (the
    # unprivileged D-Bus/polkit path hangs — gnome-shell's agent is wedged).
    local home
    home=$(home_active)
    if [ -n "$home" ]; then
        log "taking down '$home' (sudo needed on this board)..."
        sudo_cmd $NM con down "$home" || { log "failed to release $home"; exit 1; }
    fi
    stop_hostapd
    if start_hostapd; then
        log "AP is up. On the iPhone: join WiFi 'afl-cam' (password aflcam2026),"
        log "then open http://aflcam.local/ (or http://10.42.0.1/)"
    else
        exit 1
    fi
}

cmd_boot_ap() {
    # afl-cam-ap.service at boot: wait for NM to settle, then bring the AP
    # up ONLY if no home WiFi connected (at the ground). At home, do nothing.
    local waited=0
    while [ $waited -lt 45 ]; do
        if [ -n "$(home_active)" ]; then
            log "home WiFi connected — AP not needed"
            exit 0
        fi
        # a failed home connection attempt looks like "connecting"
        if $NM -t -f DEVICE,STATE dev status 2>/dev/null | grep -q "wlan0:connected"; then
            log "wlan0 connected — AP not needed"
            exit 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    log "no home WiFi after ${waited}s — bringing the AP up"
    stop_hostapd
    start_hostapd || exit 1
}

cmd_follow() {
    if [ -z "${HOTSPOT_PASSWORD:-}" ]; then
        log "HOTSPOT_PASSWORD is not set. Usage:"
        log "  HOTSPOT_SSID=\"<phone name>\" HOTSPOT_PASSWORD=<pw> field_wifi.sh follow"
        exit 1
    fi
    stop_hostapd
    log "scanning for hotspot SSID '$HOTSPOT_SSID' every 10 s (Ctrl-C to stop)..."
    trap 'log "stopped by user"; exit 0' INT
    while true; do
        # case- and apostrophe-insensitive match: iPhone names broadcast with
        # a typographic apostrophe (U+2019) and title case
        actual_ssid=""
        while IFS= read -r line; do
            a=$(printf '%s' "$line" | tr '[:upper:]' '[:lower:]' | tr -d "'\342\200\231")
            b=$(printf '%s' "$HOTSPOT_SSID" | tr '[:upper:]' '[:lower:]' | tr -d "'\342\200\231")
            if [ "$a" = "$b" ]; then
                actual_ssid="$line"
                break
            fi
        done < <($NM -t -f SSID dev wifi list 2>/dev/null)
        if [ -n "$actual_ssid" ]; then
            if ! $NM -t -f NAME con show | grep -qx "$HOTSPOT_PROFILE"; then
                log "creating profile $HOTSPOT_PROFILE for '$actual_ssid'"
                sudo_cmd $NM con add type wifi ifname wlan0 con-name "$HOTSPOT_PROFILE" \
                    ssid "$actual_ssid" \
                    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$HOTSPOT_PASSWORD" \
                    || { log "profile creation failed"; exit 1; }
            fi
            if ! profile_active "$HOTSPOT_PROFILE"; then
                log "joining hotspot..."
                sudo_cmd $NM con up "$HOTSPOT_PROFILE"
            fi
            if $NM -t -f GENERAL.STATE con show "$HOTSPOT_PROFILE" 2>/dev/null \
                    | grep -q activated; then
                local ip
                ip=$(board_ip)
                log "connected to the iPhone hotspot. Board IP: ${ip:-unknown}"
                log "On the iPhone, open http://${ip:-<board-ip>}/"
                trap - INT
                exit 0
            fi
        fi
        sleep 10
    done
}

cmd_stop() {
    stop_hostapd
    local home
    home=$(home_profile)
    if [ -n "$home" ]; then
        log "reconnecting home WiFi profile '$home'"
        sudo_cmd $NM con up "$home" || true
    else
        log "no home WiFi profile found — connect manually with: nmcli dev wifi connect <ssid>"
    fi
}

case "${1:-}" in
    ap)          cmd_ap ;;
    ap-boot)     cmd_boot_ap ;;
    follow)      cmd_follow ;;
    stop)        cmd_stop ;;
    *)           echo "usage: $0 {ap|ap-boot|follow|stop}" >&2; exit 2 ;;
esac
