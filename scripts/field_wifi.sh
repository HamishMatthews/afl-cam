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
    # Prefer the netplan home connection; fall back to any non-afl-cam
    # wireless profile (the phone-hotspot one would otherwise win).
    local first=""
    local name
    while IFS= read -r name; do
        [ -n "$first" ] || first="$name"
        case "$name" in
            netplan-*) echo "$name"; return ;;
        esac
    done < <($NM -t -f NAME,TYPE con show 2>/dev/null \
        | awk -F: '$2 ~ /wireless/ && $1 !~ /afl-cam/ {print $1}')
    echo "$first"
}

start_hostapd() {
    # Lock NM out of wlan0 while hostapd owns it: NM/gnome-shell scan
    # requests flap the interface and wedge the bcmdhd firmware (channel
    # stuck at 5GHz/34, garbage SSID buffer — verified 2026-10-11).
    sudo_cmd $NM dev set wlan0 managed no
    if pgrep -x hostapd > /dev/null; then
        log "hostapd already running"
        return 0
    fi
    log "starting hostapd (SSID afl-cam / aflcam2026)..."
    # bcmdhd needs the interface explicitly switched to AP role; hostapd's
    # own set-type never sticks and then keys/beacons are refused.
    sudo_cmd ip link set wlan0 down
    sudo_cmd iw dev wlan0 set type __ap
    sudo_cmd ip link set wlan0 up
    sleep 1
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
    # -x exact-name only: pkill -f patterns match the caller's own command
    # line and kill it (exit 144 — bit us twice)
    sudo_cmd pkill -x hostapd 2>/dev/null || true
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
    # afl-cam-ap.service at boot. Internet comes from the WIRED LAN
    # (2026-10-11) — the WiFi radio's only job is the field AP, so it
    # comes up unconditionally. (The old home-WiFi gate made the AP stand
    # down at home and confused desk tests.)
    log "wired LAN is the internet path — bringing the AP up"
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
    sudo_cmd $NM dev set wlan0 managed yes
    log "scanning for hotspot SSID '$HOTSPOT_SSID' every 10 s (Ctrl-C to stop)..."
    trap 'log "stopped by user"; exit 0' INT
    while true; do
        # case- and apostrophe-insensitive match: iPhone names broadcast with
        # a typographic apostrophe (U+2019) and title case. Scan with iw
        # directly — nmcli scans need polkit, and this board's polkit agent
        # is wedged (stuck auth dialogs, 2026-10-11).
        actual_ssid=""
        while IFS= read -r line; do
            a=$(printf '%s' "$line" | tr '[:upper:]' '[:lower:]' | tr -d "'\342\200\231")
            b=$(printf '%s' "$HOTSPOT_SSID" | tr '[:upper:]' '[:lower:]' | tr -d "'\342\200\231")
            if [ "$a" = "$b" ]; then
                actual_ssid="$line"
                break
            fi
        done < <(sudo_cmd iw dev wlan0 scan 2>/dev/null \
            | awk '/SSID:/{sub(/^[ \t]*SSID: /, ""); print}')
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

cmd_recover() {
    # bcmdhd firmware-wedge recovery, verified 2026-10-11: unload the
    # driver, rebind the SDIO host (re-runs the pwrseq power-on sequence
    # for gpio85), reload. Clears the stuck-channel/garbage-SSID wedge
    # without a reboot.
    log "recovering the WiFi chip (driver reload + SDIO power cycle)..."
    stop_hostapd
    sudo_cmd $NM dev set wlan0 managed no
    sudo_cmd ip link set wlan0 down 2>/dev/null
    sudo_cmd rmmod bcmdhd_sdio 2>/dev/null
    sleep 1
    sudo_cmd bash -c "echo 3 > /proc/sys/vm/drop_caches"
    sudo_cmd bash -c "echo fe2d0000.mmc > /sys/bus/platform/drivers/dwmmc_rockchip/unbind"
    sleep 1
    sudo_cmd bash -c "echo fe2d0000.mmc > /sys/bus/platform/drivers/dwmmc_rockchip/bind"
    sleep 4
    sudo_cmd modprobe bcmdhd_sdio
    sleep 10
    sudo_cmd ip link set wlan0 up
    sleep 2
    log "recovery done — run 'field_wifi.sh ap' to start the AP"
}

cmd_stop() {
    stop_hostapd
    sudo_cmd $NM dev set wlan0 managed yes
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
    recover)     cmd_recover ;;
    stop)        cmd_stop ;;
    *)           echo "usage: $0 {ap|ap-boot|follow|recover|stop}" >&2; exit 2 ;;
esac
