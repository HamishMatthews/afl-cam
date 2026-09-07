#!/usr/bin/env bash
# Soft-reset a wedged afl-cam camera WITHOUT replugging cables.
#
# Failure mode (seen 2026-09-08): after a hard crash mid-stream the
# camera's USB firmware wedges, its high-speed connect chirp fails, and it
# re-enumerates at FULL speed (12M) on its socket's OHCI companion — where
# uvcvideo's probe fails ("Failed to initialize the device (-5)"), so no
# video node appears and the panel sits frozen. dmesg shows:
#   "usb N-1: not running at top speed; connect to a high speed hub"
#
# Recovery ladder (each step re-scans; stops at first success):
#   1. authorized 0/1 — re-runs the USB connect sequence (no power cut)
#   2. unbind + rebind the socket's EHCI/OHCI companion controllers —
#      forces a clean high-speed chirp. THIS is the step that recovered
#      cam0 (came back at 480M on EHCI).
#   3. give up — the camera's VBUS needs a physical replug.
#
# The controller pairs are the rig's two USB2 sockets (see config.py):
#   cam0 (left)  = EHCI fc880000.usb / OHCI fc8c0000.usb
#   cam1 (right) = EHCI fc800000.usb / OHCI fc840000.usb
# Rebinding a controller drops anything streaming on that socket — close
# the UI (or at least stop previews/recording) first.
set -u

# (EHCI, OHCI) companion pairs for the two USB2 sockets
PAIRS="fc880000.usb:fc8c0000.usb fc800000.usb:fc840000.usb"

wedged_devs() {
    for dev in /sys/bus/usb/devices/*; do
        [ -f "$dev/idProduct" ] || continue
        [ "$(cat "$dev/idProduct")" = "28c4" ] || continue
        [ "$(cat "$dev/speed")" = "480" ] || basename "$dev"
    done
}

controller_of() {  # $1 = device name (e.g. 6-1)
    readlink "/sys/bus/usb/devices/$1" | sed 's#.*platform/##; s#/usb.*##'
}

kick_controllers() {  # $1 = controller address of the wedged device
    for pair in $PAIRS; do
        ehci="${pair%%:*}"; ohci="${pair##*:}"
        case ":$ehci:$ohci:" in
            *":$1:"*) break ;;
            *) continue ;;
        esac
    done
    if [ -z "${ehci:-}" ]; then
        echo "No known controller pair for $1 — physical replug needed"
        return 1
    fi
    echo "Rebinding $ehci (EHCI) + $ohci (OHCI) ..."
    echo "$ohci" > /sys/bus/platform/drivers/ohci-platform/unbind 2>/dev/null
    echo "$ehci" > /sys/bus/platform/drivers/ehci-platform/unbind 2>/dev/null
    sleep 2
    echo "$ohci" > /sys/bus/platform/drivers/ohci-platform/bind
    echo "$ehci" > /sys/bus/platform/drivers/ehci-platform/bind
    sleep 4
}

if [ "$(wedged_devs | wc -l)" = 0 ]; then
    echo "Both cameras are at 480M — nothing to kick."
    exit 0
fi

sudo true || { echo "Needs sudo."; exit 1; }

for dev in $(wedged_devs); do
    echo "Camera $dev is wedged (full-speed). Step 1: authorized toggle ..."
    sudo sh -c "echo 0 > /sys/bus/usb/devices/$dev/authorized; sleep 2; \
                echo 1 > /sys/bus/usb/devices/$dev/authorized"
    sleep 4
done

[ "$(wedged_devs | wc -l)" = 0 ] && { echo "Recovered — both cameras at 480M."; exit 0; }

for dev in $(wedged_devs); do
    echo "Still wedged: $dev. Step 2: controller rebind ..."
    ctl=$(controller_of "$dev")
    sudo sh -c "$(declare -f kick_controllers); kick_controllers '$ctl'" || exit 1
done

if [ "$(wedged_devs | wc -l)" = 0 ]; then
    echo "Recovered — both cameras at 480M."
else
    echo "Still wedged after soft reset — physically replug the camera (USB power cycle)."
    exit 1
fi
