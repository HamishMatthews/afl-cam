#!/bin/bash
# field_wifi.sh — field network helper for the afl-cam rig (iPhone control).
#
# The board has onboard WiFi 6E. Two ways to connect the operator's iPhone
# at the ground (no router available), plus a way home:
#
#   field_wifi.sh ap      — board becomes its own access point.
#                           iPhone: join WiFi "afl-cam" (password aflcam2026),
#                           open http://aflcam.local/ (or http://10.42.0.1/).
#                           NOTE: at the ground the AP comes up BY ITSELF on
#                           boot (profile afl-cam-ap, autoconnect priority
#                           -999 — the home WiFi wins at home); this command
#                           is only for switching modes manually.
#
#   field_wifi.sh follow  — board hunts for the iPhone's Personal Hotspot and
#                           joins it (for when the phone is already sharing).
#                           Usage: HOTSPOT_SSID="John's iPhone" \
#                                  HOTSPOT_PASSWORD=secret field_wifi.sh follow
#                           The operator browses to the board's DHCP IP on
#                           port 80 (printed when connected).
#                           NOTE: iOS hotspot isolates clients, so ONLY the
#                           phone can reach the board in this mode.
#
#   field_wifi.sh stop    — leave field mode, restore the home WiFi profile.
#
# The web control is served by the afl-camd daemon on port 80
# (`uv run afl-cam daemon`, installed as a systemd service by
# scripts/install_field_services.sh). The GTK UI's remote page
# (afl_cam/remote.py, port 8080) is the alternative when the UI runs.

set -u
NM="nmcli"

AP_PROFILE="afl-cam-ap"
HOTSPOT_PROFILE="afl-cam-hotspot"
# NOTE: defaults set via [ -n ] tests below, not ${VAR:-word} — the
# apostrophe in the SSID breaks bash's ${} parsing (it opens a quote).
HOTSPOT_SSID="${HOTSPOT_SSID:-}"
HOTSPOT_PASSWORD="${HOTSPOT_PASSWORD:-}"
[ -n "$HOTSPOT_SSID" ] || HOTSPOT_SSID="hammer's telephone"
[ -n "$HOTSPOT_PASSWORD" ] || HOTSPOT_PASSWORD="despacito"

log() { echo "[field_wifi] $*"; }

board_ip() {
    ip -4 addr show dev wlan0 2>/dev/null | awk '/inet/ && !/127.0.0.1/ {print $2; exit}'
}

profile_active() {
    $NM -t -f NAME con show --active 2>/dev/null | grep -qx "$1"
}

home_profile() {
    # First non-afl-cam wifi profile (netplan-wlan0-* or similar)
    $NM -t -f NAME,TYPE con show 2>/dev/null \
        | awk -F: '$2 ~ /wireless/ && $1 !~ /afl-cam/ {print $1; exit}'
}

cmd_ap() {
    log "activating board AP '$AP_PROFILE' (SSID afl-cam / aflcam2026)..."
    $NM con up "$AP_PROFILE" || { log "failed to bring up $AP_PROFILE"; exit 1; }
    log "AP is up."
    log "On the iPhone: join WiFi 'afl-cam' (password aflcam2026),"
    log "then open http://aflcam.local/ (or http://10.42.0.1/)"
}

cmd_follow() {
    if [ -z "${HOTSPOT_PASSWORD:-}" ]; then
        log "HOTSPOT_PASSWORD is not set. Usage:"
        log "  HOTSPOT_SSID=\"<phone name>\" HOTSPOT_PASSWORD=<pw> field_wifi.sh follow"
        exit 1
    fi
    if profile_active "$AP_PROFILE"; then
        log "bringing the board AP down first"
        $NM con down "$AP_PROFILE"
    fi
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
                $NM con add type wifi ifname wlan0 con-name "$HOTSPOT_PROFILE" \
                    ssid "$actual_ssid" \
                    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$HOTSPOT_PASSWORD" \
                    || { log "profile creation failed"; exit 1; }
            fi
            if ! profile_active "$HOTSPOT_PROFILE"; then
                log "joining hotspot..."
                $NM con up "$HOTSPOT_PROFILE"
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
    for p in "$AP_PROFILE" "$HOTSPOT_PROFILE"; do
        if profile_active "$p"; then
            log "bringing down $p"
            $NM con down "$p"
        fi
    done
    local home
    home=$(home_profile)
    if [ -n "$home" ]; then
        log "reconnecting home WiFi profile '$home'"
        $NM con up "$home"
    else
        log "no home WiFi profile found — connect manually with: nmcli dev wifi connect <ssid>"
    fi
}

case "${1:-}" in
    ap)     cmd_ap ;;
    follow) cmd_follow ;;
    stop)   cmd_stop ;;
    *)      echo "usage: $0 {ap|follow|stop}" >&2; exit 2 ;;
esac
