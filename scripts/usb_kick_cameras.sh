#!/usr/bin/env bash
# Soft-reset a wedged afl-cam camera WITHOUT replugging cables.
#
# Failure modes (both seen):
#   A) after a hard crash mid-stream the camera's USB firmware wedges, its
#      high-speed connect chirp fails, and it re-enumerates at FULL speed
#      (12M) on its socket's OHCI companion — where uvcvideo's probe fails
#      ("Failed to initialize the device (-5)"), so no video node appears
#      and the panel sits frozen. dmesg shows:
#        "usb N-1: not running at top speed; connect to a high speed hub"
#   B) the camera vanishes from the bus entirely (no device at all).
#
# Recovery ladder (each step re-scans; stops at first success):
#   1. authorized 0/1 — re-runs the USB connect sequence (no power cut)
#   2. unbind + rebind the socket's EHCI/OHCI companion controllers —
#      forces a clean high-speed chirp AND cuts port power briefly. This
#      is the step that recovered cam0 (2026-09-08) and revives absent
#      cameras whose firmware is fully wedged.
#   3. give up — the camera's VBUS needs a physical replug.
#
# The controller pairs are the rig's two USB2 sockets (see config.py):
#   cam0 (left)  = EHCI fc880000.usb / OHCI fc8c0000.usb
#   cam1 (right) = EHCI fc800000.usb / OHCI fc840000.usb
# Rebinding a controller drops anything streaming on that socket — close
# the UI (or at least stop previews/recording) first.
set -u

# (EHCI, OHCI) companion pairs for the two USB2 sockets (the full-speed
# wedge ladder) — and the dwc3 xhci controllers behind the USB3 sockets
# (the absent-camera power cycle; verified 2026-10-11: cameras sit at
# 480M under xhci-hcd.11/12.auto = fc000000.usb/fc400000.usb)
PAIRS="fc880000.usb:fc8c0000.usb fc800000.usb:fc840000.usb"
DWC3S="fc000000.usb fc400000.usb"

controller_of() {  # $1 = device name (e.g. 6-1); normalize to the fcXXXX.usb token
    readlink "/sys/bus/usb/devices/$1" | sed -n 's#.*/\(fc[0-9a-f]*\.usb\)/.*#\1#p'
}

# names of controllers that currently have a camera (present at any speed)
present_controllers() {
    for dev in /sys/bus/usb/devices/*; do
        [ -f "$dev/idProduct" ] || continue
        [ "$(cat "$dev/idProduct")" = "28c4" ] || continue
        controller_of "$(basename "$dev")"
    done
}

# camera devices present but NOT at 480M (the full-speed wedge)
wedged_devs() {
    for dev in /sys/bus/usb/devices/*; do
        [ -f "$dev/idProduct" ] || continue
        [ "$(cat "$dev/idProduct")" = "28c4" ] || continue
        [ "$(cat "$dev/speed")" = "480" ] || basename "$dev"
    done
}

kick_controllers() {  # $1 = controller address of the wedged/missing socket
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

kick_dwc3() {  # $1 = fcXXXX.usb controller — power-cycles its socket
    echo "Rebinding $1 (dwc3) — cuts socket power ..."
    echo "$1" > /sys/bus/platform/drivers/dwc3/unbind 2>/dev/null
    sleep 2
    echo "$1" > /sys/bus/platform/drivers/dwc3/bind
    sleep 5
}

sudo true || { echo "Needs sudo."; exit 1; }

# absent cameras: a dwc3 controller with no camera under it gets a socket
# power cycle (this is the soft replug; verified 2026-10-11)
for ctl in $DWC3S; do
    if ! echo "$(present_controllers)" | grep -qx "$ctl"; then
        echo "Controller $ctl has NO camera — power-cycling its socket ..."
        kick_dwc3 "$ctl"
    fi
done

# full-speed wedge ladder (present but slow)
for dev in $(wedged_devs); do
    echo "Camera $dev is wedged (full-speed). Step 1: authorized toggle ..."
    sudo sh -c "echo 0 > /sys/bus/usb/devices/$dev/authorized; sleep 2; \
                echo 1 > /sys/bus/usb/devices/$dev/authorized"
    sleep 4
done

[ "$(wedged_devs | wc -l)" = 0 ] || {
    for dev in $(wedged_devs); do
        echo "Still wedged: $dev. Step 2: controller rebind ..."
        ctl=$(controller_of "$dev")
        sudo sh -c "$(declare -f kick_controllers); kick_controllers '$ctl'" || exit 1
    done
}

total=0
for dev in /sys/bus/usb/devices/*; do
    [ -f "$dev/idProduct" ] || continue
    [ "$(cat "$dev/idProduct")" = "28c4" ] || continue
    total=$((total + 1))
done

if [ "$total" = 2 ] && [ "$(wedged_devs | wc -l)" = 0 ]; then
    echo "Recovered — both cameras present at 480M."
else
    echo "Still $total/2 cameras after soft reset — physically replug the missing camera (USB power cycle)."
    [ "$total" = 2 ] || exit 1
fi
