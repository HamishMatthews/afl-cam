# CLAUDE.md — afl-cam handover

Dual-4K30 recording + live GPU panoramic stitching for AFL football on an
**Orange Pi 5 Max (RK3588), Armbian, kernel 6.1.115-vendor-rk35xx**. Two USB
UVC MJPEG cameras in a static rig. The deliverable C code lives in
`afl_cam/native/`; the GTK3 UI in `afl_cam/ui.py`.

## Running things

- UI: `uv run afl-cam ui` (project `.venv`, py3.14, has `gi`).
- Stitcher + alignment scripts: **`/home/hamish/venvs/cv2-new/bin/python3`**
  (OpenCV 5 + PyAV 13; the project venv has no wheels for the bundle API).
- sudo on this board: `echo 123 | sudo -S -p '' <cmd>`.
- Vulkan apps (cam2stitch): `LD_LIBRARY_PATH=/opt/libmali-g610/usr/lib/aarch64-linux-gnu:/opt/libmali-g610/usr/lib/aarch64-linux-gnu/mali VK_DRIVER_FILES=/opt/libmali-g610/usr/share/vulkan/icd.d/mali.json`.
- GPU governor (session-only): `sudo bash -c 'echo performance > /sys/class/devfreq/fb000000.gpu/governor'`.
- Cameras are only freed when the app is closed (one stream per camera at a time).
- Recordings: `outputs/<session>/` + `recordings.db` (WAL). Commit & push to
  GitHub (`origin`, branch `rk3588-native-recording`) when done — that's the
  standing instruction.
- Field remote (iPhone): the UI runs `afl_cam/remote.py` (stdlib HTTP server
  on 0.0.0.0:8080). Field networks: `scripts/field_wifi.sh ap` (board AP
  `afl-cam`/`aflcam2026`, iPhone opens http://10.42.0.1:8080/) or
  `field_wifi.sh follow` (join the iPhone's hotspot), `field_wifi.sh stop` to
  restore home WiFi. NM profile `afl-cam-ap` is pre-created.

## Key files

- `afl_cam/native/cam2stitch.c` — THE deliverable: both cameras, jpegd NV12,
  Vulkan map-texture warp (full canvas), two-thread pipeline (main = capture
  + GPU submit + encoder put/drain; worker = SSBO→encoder repack only).
- `afl_cam/native/stitch_nv12.comp` (+ `.spv`) — map-texture shader, BT.601
  limited (jpegd outputs Y 16–235 exactly, `RANGE_FULL=0`), 4-Y-bytes-per-uint
  SSBO packing.
- `afl_cam/native/cam2h264.c` — per-camera recorder (separate mode).
- `afl_cam/config.py` — camera discovery, `STITCH_PARAMS_DIR`
  (`calibration/stitch_live_full`), `STITCH_CANVAS_WIDTH=5304`, Vulkan env.
- `afl_cam/engine.py` — spawns recorders, `can_live_stitch()` gate.
- `afl_cam/stitcher.py` — post-stitch engine; **uses the frozen alignment
  when `calibration/stitch_live_full/map0.bin` is non-empty**
  (`AFL_CAM_REBUNDLE=1` restores per-session bundling).
- `afl_cam/ui.py` — GTK3 app; Re-align button streams output to the terminal.
- `afl_cam/retime.py` — VFR→CFR remux from `.pts` sidecars (cv2-new venv).
- `scripts/recapture_alignment.py` — re-align chain (UI button backend):
  stills → `dump_camera_params.py` (bundle) → `refine_rotation.py` →
  `dump_camera_params.py --r1` (final, atomic writes).
- `scripts/measure_alignment.py` — overlap misalignment ruler (see metric
  trap below).

## Hard-won invariants (violating these cost days last time)

- **MPP encoder put/drain MUST run on the main thread.** Encoder calls from a
  worker thread produce intermittent phantom/corrupt packets (allocator fill
  bytes as NALs). The worker only repacks memory; the main thread submits.
- **Encoder packet group limit must exceed the rotating buffers** (8 with a
  4-ring): exhaustion makes SPS/PPS header gets fail and packets alias stale
  memory.
- **Encoder input is per-row strided**: `ALIGN(w,16) ≠ w` for 5304 — a
  contiguous Y memcpy shears every row 8 px (3840 % 16 == 0 masked this).
- **Upload maps/weights once in a setup cb, never per frame.** libmali's
  "one vkBeginCommandBuffer" quirk is really "don't re-record the SAME cb
  handle"; separate cbs are fine. Re-uploading 148 MB/frame cost 35.6→16.4 ms.
- **SSBO sizes**: 4 Y bytes per uint, one `(u|v<<8)` uint per 2×2 chroma
  block → `2*cw*ch` bytes total; canvas width must be `% 4 == 0`. Past
  device-lost crashes were SSBO-size arithmetic (OOB writes), not LLVM.
- **Alignment math**: K is measured (checkerboard, distortion ZEROED — the
  paper board's waviness, not the lens); R is refined in **canvas space**
  with K fixed (`refine_rotation.py`). The 2-view bundle is degenerate on
  grass (focal 2125 vs measured 2237 → 13–70 px ghosting). Do NOT reintroduce
  per-session bundling as the primary path.
- **Metric traps**: ray-direction angular residuals measure the cameras'
  toe-in angle, NOT stitch alignment (per-camera `tl` absorbs toe-in) — only
  canvas-space residuals measure ghosting. Tile phase-correlation over-reads
  cross-camera shifts (differing exposure/gain decorrelate fine texture) —
  always Gaussian-blur σ=8 first; validated: same-image synthetic shifts read
  exactly, blurred cross-camera reads ~0.35 px when aligned.
- **Exposure**: the cameras' auto-exposure picks the sensor rate (dim scene =
  ~50 ms exposure = 20 fps); manual exposure without light records BLACK
  frames (no V4L2 gain control exists). The recorder's "auto-settle-then-lock"
  checks mean frame interval. Never assume fps limits from time-of-day — the
  pipeline capacity is ~30 fps at full canvas when light allows.
- **Camera discovery** (`config.py`): video nodes shuffle per reboot; a camera
  on a USB 2.0 port enumerates on the EHCI controller (`fc800000.usb`) — both
  cases are matched; use the Swap button if sides come out swapped.
- **NVMe**: root disk (Samsung 980) overheats → PCIe link drop → ext4 errors →
  black screen. `pcie_aspm=off nvme_core.default_ps_max_latency_us=0` are in
  `/boot/armbianEnv.txt`; the drive STILL needs a heatsink (hwmon7 crit
  84850 m°C). The journal IS persistent now (2026-09-07): Storage=persistent
  + `armbian-ramlog` patched to bind-mount `/var/log.hdd/journal` onto
  `/var/log/journal` (journald ignores symlinked journal dirs, and only opens
  the system journal after a flush request — `journalctl --flush`).
- **Crashes (2026-09-07)**: two hard lockups (black screen → 22 s hardware
  watchdog reboot, ~20:57 and ~21:42) BOTH while the rkvenc encoders were
  under load — bench runs and recorder smoke tests. Temps were fine both
  times; no panic in the (now persistent) journal. Prime suspect: power
  supply sag under VPU load. Watch for this pattern in the field.

## Current state (2026-09-06)

- Live stitch at 5304×1896 canvas, quality parity with `cv2.remap` math
  (Y corr 0.999965), encoder + GPU pipelined ≈ 30 fps capacity.
- Frozen alignment refined for the static rig: overlap misalignment ~0.5 px
  median (was 13–70 px before the canvas-space refinement). Near-field grass
  (~19 px bottom band) is genuine parallax — physics, not a bug.
- Re-align button works end-to-end and streams logs; dump writes are atomic.
- Live preview while recording (2026-09-07): the recorders box-downscale the
  decoded NV12 to 960×540 and MPP-jpegenc it (main thread only) to
  `<session>/camN_preview.jpg` every `AFL_CAM_PREVIEW_SECS` (default 0.2 s =
  5 fps, atomic tmp+rename); the UI polls at 200 ms and the field-remote
  page polls over HTTP. 10 fps is one env var away; true smooth preview
  would need a second low-res MPP encode + HLS (phase 3, not built).
- Encode defaults (2026-09-07): **HEVC** 60 Mbps per camera, 80 Mbps for the
  stitched canvas (`AFL_CAM_CODEC=h264` to revert; measured via `cam2h264
  --bench` — dual-concurrent 4K30 HEVC@60M sustains ~49 fps each on the
  single rkvenc core, HEVC is faster than H.264 at the canvas size). CBR
  scales with actual fps, so dim-light ~20 fps recordings undershoot the
  target proportionally.
- Field remote (2026-09-07): `afl_cam/remote.py` + `afl_cam/remote/index.html`
  — iPhone control (toggle/mode/res/duration/realign/swap) + 5 fps preview
  polling; actions cross to the GTK thread via a queue. Untested end-to-end
  with the real UI (standalone server tests pass).
- Pending: outdoor bright-daylight 30 fps verification; NVMe heatsink;
  gnome-shell burns ~385% CPU (software-rendered desktop — consider a lighter
  session); GPU governor persistence (user decision); the UI's
  `recordings not saving` report from 2026-09-06 may need reproducing;
  bcmdhd AP mode needs the 10-minute desk test (`field_wifi.sh ap` + iPhone).

## More context

- `orange_pi_5_max_setup.md` — board setup history (packages, MPP, libmali,
  IRQ pinning, verification cheat-sheet).
- `hardware_stitch_plan.md` / `live_stitch_plan.md` — original design plans.
- Session memory (auto-loaded): hardware topology, NVMe thermal crisis,
  alignment crisis + fixes, MPP quirks.
