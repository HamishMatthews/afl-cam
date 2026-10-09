# CLAUDE.md — afl-cam handover

Dual-4K30 recording + live GPU panoramic stitching for AFL football on an
**Orange Pi 5 Max (RK3588), Armbian, kernel 6.1.115-vendor-rk35xx**. Two USB
UVC MJPEG cameras in a static rig. The deliverable C code lives in
`afl_cam/native/`; the GTK3 UI in `afl_cam/ui.py`.

## Running things

- UI: `uv run afl-cam ui` (project `.venv`, py3.14, has `gi`).
- Field daemon: `uv run afl-cam daemon` — afl-camd (iphone-control-plan.md):
  supervises a long-running cam2stitch, serves the web UI + REST + WS API
  (port 80, falls back to 8080; `AFL_CAM_PORT` overrides), session
  finalize, clock sync, auto-stop, schedule, GPIO. Installed as a systemd
  service by `scripts/install_field_services.sh` (also installs MediaMTX).
  The daemon and the GTK UI are mutually exclusive (both want the cameras).
- Stitcher + alignment scripts: **`/home/hamish/venvs/cv2-new/bin/python3`**
  (OpenCV 5 + PyAV 13; the project venv has no wheels for the bundle API).
- sudo on this board: `echo 123 | sudo -S -p '' <cmd>`.
- Vulkan apps (cam2stitch): `LD_LIBRARY_PATH=/opt/libmali-g610/usr/lib/aarch64-linux-gnu:/opt/libmali-g610/usr/lib/aarch64-linux-gnu/mali VK_DRIVER_FILES=/opt/libmali-g610/usr/share/vulkan/icd.d/mali.json`.
- GPU governor (session-only): `sudo bash -c 'echo performance > /sys/class/devfreq/fb000000.gpu/governor'`.
- Cameras are only freed when the app is closed (one stream per camera at a time).
- Recordings: `outputs/<session>/` + `recordings.db` (WAL). Commit & push to
  GitHub (`origin`, branch `rk3588-native-recording`) when done — that's the
  standing instruction.
- Field remote (iPhone): primary = the afl-camd daemon (port 80, web UI +
  WHEP/HLS preview via MediaMTX on :8889/:8888). **Productionised
  (2026-10-09): `scripts/install_field_services.sh` enables systemd units
  (mediamtx, afl-camd, gpu-governor) + the aflcam.local mDNS alias, and
  the afl-cam-ap NM profile autoconnects at priority -999 — at the ground
  the AP comes up by itself on boot (home WiFi wins at home). Field flow:
  power on → phone joins `afl-cam`/`aflcam2026` → http://aflcam.local/.**
  `scripts/field_wifi.sh ap|follow|stop` switches modes manually. The GTK
  UI's remote page (`afl_cam/remote.py`, port 8080) is the fallback when
  the UI runs — stop the daemon first (`systemctl stop afl-camd`; daemon
  and UI can't share the cameras). iPhone desk test checklist:
  `field_desk_test.md` (NOT yet run with a real iPhone).

## Key files

- `afl_cam/native/cam2stitch.c` — THE deliverable: both cameras, jpegd NV12,
  Vulkan map-texture warp (full canvas), two-thread pipeline (main = capture
  + GPU submit + encoder put/drain; worker = SSBO→encoder repack only).
  DAEMON mode (`AFL_CAM_CTL_SOCK` set): standby until `REC_START <dir>` /
  `REC_STOP` / `STATUS` / `QUIT` on a unix ctl socket (JSON status pushed
  ~1/s); master encoder idles in standby (skip put/drain), stop drains the
  encoder tail (3 quiet drains) before closing; a second rkvenc (H.264
  Constrained Baseline 1600×564 CBR 3.5M, GOP 1s) encodes a worker-side
  box-downscale of the canvas → UDP `AFL_CAM_PREVIEW_UDP` (SPS/PPS injected
  before every IDR packet — rkvenc never emits parameter sets) + proxy
  files while recording.
- `afl_cam/native/stitch_nv12.comp` (+ `.spv`) — map-texture shader, BT.601
  limited (jpegd outputs Y 16–235 exactly, `RANGE_FULL=0`), 4-Y-bytes-per-uint
  SSBO packing.
- `afl_cam/native/cam2h264.c` — per-camera recorder (separate mode).
- `afl_cam/daemon.py` — afl-camd (stdlib only): supervises cam2stitch
  (spawn/restart/adopt a live socket from a previous run/idle-close on
  AFL_CAM_IDLE_SECS), REST + WebSocket API, web UI, finalize (master mux
  with hvc1 tag + proxy faststart + thumbnail), phone clock sync, auto-stop
  (temp/storage), schedule, GPIO button/LED.
- `afl_cam/web/index.html` — the phone page: status/REC/elapsed, WHEP
  preview with HLS fallback, schedule, recordings browser (play/save/delete).
- `deploy/mediamtx.yml` + `deploy/systemd/*` + `scripts/install_field_services.sh`
  — MediaMTX (:8554 RTSP in, :8889 WHEP, :8888 HLS) and afl-camd units.
- `afl_cam/config.py` — camera discovery, `STITCH_PARAMS_DIR`
  (`calibration/stitch_live_full`), `STITCH_CANVAS_WIDTH=5304`, Vulkan env,
  `build_cam2stitch()`/`vk_env()` helpers (stdlib, shared with the daemon).
- `afl_cam/engine.py` — spawns recorders, `can_live_stitch()` gate.
- `afl_cam/stitcher.py` — post-stitch engine; **uses the frozen alignment
  when `calibration/stitch_live_full/map0.bin` is non-empty**
  (`AFL_CAM_REBUNDLE=1` restores per-session bundling).
- `afl_cam/ui.py` — GTK3 app; Re-align button streams output to the terminal.
- `afl_cam/retime.py` — VFR→CFR remux from `.pts` sidecars; HEVC muxes are
  tagged `hvc1` (iPhone/QuickTime refuse `hev1`).
- `scripts/recapture_alignment.py` — re-align chain (UI button backend):
  stills → `dump_camera_params.py` (bundle) → `refine_rotation.py` →
  `dump_camera_params.py --r1` (final, atomic writes).
- `scripts/measure_alignment.py` — overlap misalignment ruler (see metric
  trap below).

## Hard-won invariants (violating these cost days last time)

- **MPP encoder put/drain MUST run on the main thread.** Encoder calls from a
  worker thread produce intermittent phantom/corrupt packets (allocator fill
  bytes as NALs). The worker only repacks memory; the main thread submits.
- **Daemon-mode ctl thread only parses + sets flags.** Every application
  action in cam2stitch daemon mode (file open/close, IDR, encoder) runs on
  the main loop; the ctl thread must never touch the encoder or files.
  REC_STOP closes the files only after the encoder drain goes quiet (3
  consecutive empty drains), or the tail frames are lost.
- **rkvenc never emits SPS/PPS in-band** — `prep:header_mode` EACH_IDR is
  silently ignored (IDRs carry SEI only), and rkvenc packs `[SEI][IDR]`
  into ONE packet, so the packet's first NAL is type 6: any IDR test must
  SCAN the packet for a type-5 NAL, not read the first NAL. The preview
  encoder injects the GET_HDR_SYNC-captured SPS/PPS before every IDR on the
  UDP path (ffmpeg's h264 demuxer refuses PPS-less streams) and writes it
  into the proxy file on REC_START.
- **cam2stitch status/socket pitfalls (afl-camd)**: a supervisor must
  PROBE the ctl socket before unlinking it (unlinking a live socket orphans
  a still-running pipeline); killing the daemon without its SIGTERM handler
  orphans cam2stitch holding the cameras; `sqlite3` connections cross
  threads need `check_same_thread=False`; stdlib-WS servers must override
  `shutdown_request` (ThreadingMixIn's `shutdown(SHUT_WR)` FINs upgraded
  connections) and pass `socket.dup()` straight to the WS wrapper (a
  double-wrapped fd gets closed by the intermediate object's `__del__`).
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
- **Camera discovery** (`config.py`): the cameras carry IDENTICAL USB
  descriptors (serial `01.00.00`, same mfr/firmware — descriptor blobs
  verified byte-identical, 2026-10-09), so identity = the PHYSICALLY
  LABELED rig sockets. Hardcoded `_CAMERA_PORTS` map: cam0 (left) = xhci.11
  USB3 socket (USB2 aliases EHCI `fc800000.usb`/OHCI `fc840000.usb`),
  cam1 (right) = xhci.12 (aliases `fc880000.usb`/`fc8c0000.usb`). Node
  numbers shuffle per reboot; discovery keys by controller+port. NO swap
  feature (removed 2026-10-09 by design) — if the rig wiring ever changes,
  edit that one map. A camera whose high-speed chirp fails (USB firmware
  wedged after a crash mid-stream) re-enumerates at 12M on the OHCI side
  where uvcvideo's probe FAILS (no video node = frozen panel) —
  `scripts/usb_kick_cameras.sh` (authorized toggle → EHCI/OHCI rebind)
  recovers it without a replug, and the UI runs it at startup when a
  camera is missing (needs the `/etc/sudoers.d/afl-cam-usb-kick` NOPASSWD
  rule); physical replug = VBUS power cycle.
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

## Current state (2026-10-09, evening update)

- **The rig is productionised**: systemd units enabled for mediamtx +
  afl-camd + gpu-governor (performance at boot), aflcam.local mDNS alias
  published (verified answering A queries with 10.42.0.1), AP profile
  autoconnects at the ground. Power on → phone joins `afl-cam` → opens
  http://aflcam.local/. The daemon holds the cameras only when a phone is
  connected (idle closes them after AFL_CAM_IDLE_SECS).
- **afl-camd field daemon built and verified end-to-end from a laptop**
  (iphone-control-plan.md build order steps 2–6): cam2stitch daemon mode
  (standby/record toggle + ctl socket + stitched H.264 preview + proxy),
  MediaMTX v1.21.2 (RTSP in :8554, WHEP :8889, HLS :8888), afl-camd
  (supervise/adopt/idle-close, REST + WebSocket, finalize with hvc1 master
  + faststart proxy + thumbnail, clock sync, auto-stop, schedule, GPIO
  stubs), phone web UI. Verified: record cycles (master 99% frame capture,
  proxy muxes, thumbs), Range/206 serving, WS pushes, RTSP decode +
  HLS playlist, idempotent start/stop, DELETE, crash-retry + socket
  adoption. **NOT verified: Safari on the board AP — see
  `field_desk_test.md` and run it before game day.**
- The GTK UI path is unchanged and still works (legacy-mode regression
  run: 199/197 frames encoded, clean mux). UI + daemon are mutually
  exclusive — `systemctl stop afl-camd` to run the UI with cameras; a
  daemon wake attempt while the UI owns the cameras fails cleanly
  (error banner + retry), it does not steal them.
- Master encode capacity note: at 80 Mbps HEVC the full canvas sustains
  roughly the sensor rate in dim scenes (~13–20 fps); at brighter light
  (~24–34 fps input) the encoder drops frames when input exceeds its
  capacity (pre-existing MPP real-time drop behaviour, same in legacy
  mode). Daylight 30 fps verification still pending.
- Live stitch at 5304×1876 canvas, quality parity with `cv2.remap` math
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
- Pending: the `field_desk_test.md` iPhone run; outdoor bright-daylight
  30 fps verification; NVMe heatsink; gnome-shell burns ~385% CPU
  (software-rendered desktop — consider a lighter session); the UI's
  `recordings not saving` report from 2026-09-06 may need reproducing.

## More context

- `orange_pi_5_max_setup.md` — board setup history (packages, MPP, libmali,
  IRQ pinning, verification cheat-sheet).
- `hardware_stitch_plan.md` / `live_stitch_plan.md` — original design plans.
- Session memory (auto-loaded): hardware topology, NVMe thermal crisis,
  alignment crisis + fixes, MPP quirks.
