#!/bin/bash
# afl-cam capture script — record MJPEG from UVC cameras straight to disk
# Usage: ./capture.sh [-r 1080p|4K] [-d duration_sec] [-c camera1,camera2] [-o output_dir]

set -euo pipefail

RESOLUTION="1080p"
DURATION=0  # 0 = run until interrupted
CAMERAS=()   # auto-detect if empty
OUTPUT_DIR="./captures"

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  -r RES        Resolution: 1080p (default) or 4K
  -d SEC        Duration in seconds (0 = run until Ctrl+C)
  -c DEV1,DEV2  Camera devices (e.g. /dev/video0,/dev/video2)
  -o DIR        Output directory (default: ./captures)
  -l            List available cameras and exit
  -h            Show this help

Examples:
  $(basename "$0") -l                      # list cameras
  $(basename "$0") -r 1080p -d 7200        # 2hr game at 1080p30
  $(basename "$0") -r 4K -o /mnt/game_12   # 4K, custom output dir
EOF
    exit 0
}

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# --- parse args ---
while getopts "r:d:c:o:lh" opt; do
    case $opt in
        r) RESOLUTION="$OPTARG" ;;
        d) DURATION="$OPTARG" ;;
        c) IFS=',' read -ra CAMERAS <<< "$OPTARG" ;;
        o) OUTPUT_DIR="$OPTARG" ;;
        l) gst-device-monitor-1.0 Video/Source 2>/dev/null | grep -E "name|object.path" | grep -v "GstDeviceMonitor" ; exit 0 ;;
        h) usage ;;
        *) usage ;;
    esac
done

# --- resolution config ---
case "$RESOLUTION" in
    1080p) WIDTH=1920; HEIGHT=1080; FRAMERATE="30/1" ;;
    4K)    WIDTH=3840; HEIGHT=2160; FRAMERATE="30/1" ;;
    *)     echo "Unknown resolution: $RESOLUTION (use 1080p or 4K)"; exit 1 ;;
esac

# --- auto-detect cameras (MJPEG-capable only) ---
if [ ${#CAMERAS[@]} -eq 0 ]; then
    ALL_VIDEO_DEVS=(/dev/video*)
    for DEV in "${ALL_VIDEO_DEVS[@]}"; do
        [ -e "$DEV" ] || continue
        # Check if device supports MJPEG via v4l2-ctl if available, otherwise include it
        if command -v v4l2-ctl &>/dev/null; then
            if v4l2-ctl -d "$DEV" --list-formats 2>/dev/null | grep -q "MJPG\|JPEG\|Motion-JPEG"; then
                CAMERAS+=("$DEV")
            fi
        else
            # Without v4l2-ctl, just use /dev/video0 (the primary camera)
            # Higher IDs are typically metadata/sensor subdevices — use -c to override
            if [ "$DEV" = "/dev/video0" ]; then
                CAMERAS+=("$DEV")
            fi
        fi
    done
    if [ ${#CAMERAS[@]} -eq 0 ]; then
        echo "No MJPEG cameras found. Install v4l-utils or use -c /dev/videoN"
        exit 1
    fi
fi

log "============================================"
log "afl-cam capture starting"
log "  Resolution: ${WIDTH}x${HEIGHT} @ ${FRAMERATE}"
log "  Duration:   ${DURATION}s (0 = until Ctrl+C)"
log "  Cameras:    ${CAMERAS[*]}"
log "  Output:     ${OUTPUT_DIR}"
log "============================================"

mkdir -p "$OUTPUT_DIR"

# --- build pipeline for each camera ---
PIPELINE_PARTS=()
CAM_IDX=0
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')

for DEV in "${CAMERAS[@]}"; do
    if [ ! -e "$DEV" ]; then
        log "WARNING: $DEV not found, skipping"
        continue
    fi

    OUTFILE="${OUTPUT_DIR}/cam${CAM_IDX}_${TIMESTAMP}_${RESOLUTION}.mkv"

    # Pipeline per camera:
    # v4l2src → queue (buffer 5s) → jpegparse → queue → matroskamux → filesink
    # queue elements decouple the source from the muxer to absorb USB jitter
    SRC="v4l2src device=${DEV} do-timestamp=true"
    CAPS="image/jpeg,width=${WIDTH},height=${HEIGHT},framerate=${FRAMERATE}"
    QUEUE_SRC="queue max-size-time=5000000000 max-size-buffers=300 leaky=no"
    QUEUE_MUX="queue max-size-time=5000000000 max-size-buffers=300 leaky=no"

    PIPELINE="${SRC} ! ${CAPS} ! ${QUEUE_SRC} ! jpegparse ! ${QUEUE_MUX} ! matroskamux ! filesink location=${OUTFILE}"

    PIPELINE_PARTS+=("${PIPELINE}")
    log "Cam ${CAM_IDX}: ${DEV} → ${OUTFILE}"
    ((CAM_IDX++)) || true
done

if [ ${#PIPELINE_PARTS[@]} -eq 0 ]; then
    log "ERROR: no valid cameras to capture from"
    exit 1
fi

# --- build num-buffers arg ---
NUM_BUFFERS=""
if [ "$DURATION" -gt 0 ]; then
    FPS=$(echo "$FRAMERATE" | cut -d/ -f1)
    BUFFERS=$((DURATION * FPS))
    NUM_BUFFERS="num-buffers=${BUFFERS}"
    log "Total frames per camera: ${BUFFERS} (${DURATION}s × ${FPS}fps)"
fi

# --- run ---
# Insert num-buffers into each v4l2src if duration is set
FINAL_PARTS=()
for PART in "${PIPELINE_PARTS[@]}"; do
    if [ -n "$NUM_BUFFERS" ]; then
        PART="${PART/v4l2src/v4l2src ${NUM_BUFFERS}}"
    fi
    FINAL_PARTS+=("${PART}")
done

# Compose: all cameras run in a single pipeline (linked by matroskamux per-stream,
# but since each has its own mux+filesink they're independent parallel branches)
FULL_PIPELINE=$(IFS=' '; echo "${FINAL_PARTS[*]}")

log "GStreamer pipeline: ${FULL_PIPELINE}"
log "Press Ctrl+C to stop recording..."

# Trap SIGINT for clean shutdown
trap 'log "Caught SIGINT, stopping..."; kill 0' INT TERM

gst-launch-1.0 -e ${FULL_PIPELINE} 2>&1 | while IFS= read -r line; do
    # Filter out the known MJPEG null-ref warnings (harmless, fixed in GStreamer 1.22+)
    if [[ ! "$line" =~ "gst_mini_object_unref" ]]; then
        echo "$line"
    fi
done

RET=${PIPESTATUS[0]}
log "Capture finished (exit code: ${RET})"
exit $RET
