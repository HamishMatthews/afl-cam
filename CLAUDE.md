# CLAUDE.md — afl-cam handover

Dual-4K30 recording + live GPU panoramic stitching for AFL football on an
**Orange Pi 5 Max (RK3588), Armbian, kernel 6.1.115-vendor-rk35xx**. Two USB
UVC MJPEG cameras in a static rig. The deliverable C code lives in
`afl_cam/native/`. The only interface is the phone web app served by
afl-camd (`afl_cam/web/index.html`); the GTK desk app was retired
2026-10-12 (issue #18) and its tools live in the page's Setup tab.

## Running things

- Field daemon: `uv run afl-cam daemon` — afl-camd (docs/archive/iphone-control-plan.md):
  supervises a long-running cam2stitch, serves the web UI + REST + WS API
  (port 80, falls back to 8080; `AFL_CAM_PORT` overrides), session
  finalize, clock sync, auto-stop, schedule, GPIO. Installed as a systemd
  service by `scripts/install_field_services.sh` (also installs MediaMTX).
  Open http://<board>/ in any browser for the UI.
- Stitcher + alignment scripts: **`/home/hamish/venvs/cv2-new/bin/python3`**
  (OpenCV 5 + PyAV 13; the project venv has no wheels for the bundle API).
- sudo on this board: `echo 123 | sudo -S -p '' <cmd>`.
- Vulkan apps (cam2stitch): `LD_LIBRARY_PATH=/opt/libmali-g610/usr/lib/aarch64-linux-gnu:/opt/libmali-g610/usr/lib/aarch64-linux-gnu/mali VK_DRIVER_FILES=/opt/libmali-g610/usr/share/vulkan/icd.d/mali.json`.
- GPU governor (session-only): `sudo bash -c 'echo performance > /sys/class/devfreq/fb000000.gpu/governor'`.
- One stream per camera at a time: the daemon closes cam2stitch before a
  re-align or a separate-mode recording takes the cameras.
- Recordings: `outputs/<session>/` + `recordings.db` (WAL).
- **Agent workflow (issues, branches, worktrees, rig tests): read
  `AGENTS.md` first.** Development happens off the board; the board only
  runs tests (`scripts/rig_test.sh`, queued on a lock). Work on a branch
  per issue and open a PR against `main`; never push to `main` directly.
  A merge to `main` goes live on its own: the `deploy` workflow
  fast-forwards the board's checkout and restarts afl-camd
  (`scripts/deploy_board.sh`; refuses while recording or with local edits,
  re-run it from Actions once idle).
- Field remote (iPhone): primary = the afl-camd daemon (port 80, web UI +
  WHEP/HLS preview via MediaMTX on :8889/:8888). **Productionised
  (2026-10-09): `scripts/install_field_services.sh` enables systemd units
  (mediamtx, afl-camd, gpu-governor) + the aflcam.local mDNS alias. At the
  ground the afl-cam-ap.service boots the AP via `field_wifi.sh ap-boot`
  (hostapd+dnsmasq, not the broken NM-AP path; waits 45 s for home WiFi
  first, so home wins at home). Field flow: power on → phone joins
  `afl-cam`/`aflcam2026` → http://aflcam.local/.**
  `scripts/field_wifi.sh ap|ap-boot|follow|recover|stop` switches modes
  manually (`recover` = bcmdhd firmware-wedge recovery, no reboot needed).
  iPhone desk test checklist:
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
  SSBO packing; per-camera gain at params [5..6]. Live, cam2stitch writes
  it (shader gain x seam trim) into the mapped params buffer; params.bin's
  stored value is only used by -src and the offline paths. After the 64
  params floats cam2stitch keeps the seam field: a per-camera RGB gain on a
  64 px node grid (bilinear, no blur, read once per thread), fitted live
  from the median brightness ratio (and each channel's ratio over it) of
  overlap points flat in both cameras (parallax-proof), applied only along
  the blend seam (full within 96 px, gone 320 px further). A global black
  offset was tried (2026-10-20) and dropped: estimated from the scene it
  wandered -10..+3 levels as the light changed (flare is additive too)
  (`AFL_CAM_SEAM_FIELD=0` = off; `AFL_CAM_SEAM_DUMP=1` dumps the
  readings for `tests/rig/test_seam_field.py`). The offline re-stitch has
  no seam field. Rebuild the .spv with `glslangValidator -V
  stitch_nv12.comp -o stitch_nv12.spv` (byte-reproducible).
- `afl_cam/native/cam2h264.c` — per-camera recorder (separate mode).
- `afl_cam/daemon.py` — afl-camd (stdlib only): supervises cam2stitch
  (spawn/restart/adopt a live socket from a previous run/idle-close on
  AFL_CAM_IDLE_SECS), REST + WebSocket API, web UI, finalize (master mux
  with hvc1 tag + proxy faststart + thumbnail + the camera audio, aligned
  to the video's first frame from the `.start` stamp beside each wav; the
  wavs go once muxed unless AFL_CAM_KEEP_WAV), phone clock sync, auto-stop
  (temp/storage), schedule, GPIO button/LED.
- `afl_cam/resume.py` — recording that survives a crash: the record intent
  (`outputs/_live/intent.json`, heartbeat 5 s) outlives a cam2stitch crash,
  a daemon restart and a reboot; the daemon seals the dead run's files as
  `stitch.NNN.*` and REC_STARTs the same folder again. Finalize muxes each
  segment and concatenates; gaps go in `segments.json`. Only an explicit
  stop, the schedule or an auto-stop clears it; a boot more than
  `AFL_CAM_RESUME_MAX_GAP_S` (900) after the last heartbeat finalizes
  instead of resuming. A power cut can leave the intent (and its .tmp)
  as NUL bytes even after fsync (2026-10-14, power bank): an unreadable
  intent falls back to the DB's newest live recording still marked
  `recording`, timed from the session folder's mtimes + `.pts`. No
  intent but such a row = a stop whose finalize was killed (daemon
  restart mid-mux): it is finalized again, never resumed. `rig_test.sh`
  and `deploy_board.sh` wait out / refuse a recording or finalizing
  daemon.
  Daemon intervals (watchdog, idle, heartbeat) use the monotonic clock:
  the phone/NTP clock sync jumps the wall clock by hours at the ground.
- `afl_cam/web/index.html` — the phone page: status/REC/elapsed, WHEP
  preview with HLS fallback, schedule, recordings browser (play/save/delete),
  and a Setup tab: pre-game self-test (`afl_cam/selftest.py`), re-align with
  streamed log + before/after stitch score (`RealignJob` in daemon.py runs
  `recapture_alignment.py`; the daemon closes the pipeline first, state
  `realigning`), framing guides (seam, overlap, horizon from the live maps,
  `afl_cam/framing.py`), PWA manifest + icons. The rig test re-aligns into
  `AFL_CAM_ALIGN_OUT` (scratch), never `calibration/`. The rig tests run the
  pipeline on a scratch copy of the live maps whose blend `rig_test.sh`
  re-cuts with this tree's `place_seam` (`scripts/rig_recut_blend.py`,
  `AFL_CAM_STITCH_DIR`): they judge the blend the next Re-align writes.
  Record tab has a Live stitch / Separate switch: separate mode closes
  cam2stitch and runs one cam2h264 per camera (`SeparateRecorder`),
  proxy.mp4 = hard link to cam0.mp4; "Stitch" on a separate session runs
  `python -m afl_cam.restitch <id>` (niced subprocess, stitch_video.mp4).
- `deploy/mediamtx.yml` + `deploy/systemd/*` + `scripts/install_field_services.sh`
  — MediaMTX (:8554 RTSP in, :8889 WHEP, :8888 HLS) and afl-camd units.
- `afl_cam/config.py` — camera discovery, `STITCH_PARAMS_DIR`
  (`calibration/stitch_live_full`), `STITCH_CANVAS_WIDTH=5304`, Vulkan env,
  `build_cam2stitch()`/`vk_env()` helpers (stdlib, shared with the daemon).
- `afl_cam/engine.py` — per-camera recorder for `afl-cam record` (CLI).
- `afl_cam/crop_maps.py` (+ stdlib `cropsig.py`) — the phone's Crop setting:
  derives `calibration/stitch_live_full_crop` from the live map set (largest
  fully covered rectangle, width % 4, even height; sliced maps/weights, `tl`
  shifted so the horizon guide still lands). cam2stitch is unchanged, it just
  gets a smaller canvas. Rebuilt when the live files change (signature in
  `source.sig`), at pipeline start only; the daemon (`set_crop`, POST
  `/api/crop`, `outputs/_live/settings.json`) restarts an idle pipeline on
  change and falls back to the full canvas if the crop can't be built. Tests
  point `AFL_CAM_CROP_DIR` at a scratch dir.
- `afl_cam/stitch_maps.py` — THE map-set format (params.bin, map0/1.bin,
  w0/1.bin): one writer (`dump_camera_params.py`), read by cam2stitch and
  the offline stitcher.
- `afl_cam/lens.py` + `afl_cam/intrinsics/cam{0,1}.json` — the lens: tracked
  K and (k1, k2, k3) per camera, fitted from checkerboard stills
  (`scripts/calibrate.py` captures, `python -m afl_cam.lens fit calibration`
  fits; reports board coverage). The map generator builds the warp
  undistorted and bakes the lens in last, so the maps hold RAW source coords
  and the shader never changes; `refine_rotation.py` and
  `measure_alignment.py` undistort their matches with it.
- `afl_cam/stitcher.py` — separate-mode re-stitch; **warps with the live
  map set itself** (same geometry + weights as cam2stitch), so a re-stitch
  of per-camera files reproduces the live stitch
  (`AFL_CAM_REBUNDLE=1` or a missing/torn set → per-session bundling).
- `afl_cam/retime.py` — timing from the `.pts` sidecars, two rules. The
  per-camera raws the stitcher PAIRS stay CFR at the median measured rate
  (`mux_with_pts_rate`) — VFR mp4s break frame pairing. The final
  deliverables a human watches (`retime_file`: stitch.mp4, proxy.mp4) carry
  the sidecar's real per-frame timing, so duration and playback speed are
  real time when auto-exposure changes the rate mid-game, and the camera
  audio muxed in alongside stays in sync. HEVC muxes are tagged `hvc1`
  (iPhone/QuickTime refuse `hev1`). Needs PyAV: the daemon shells out to
  the board venv at `AFL_CAM_RETIME_PY` (av 13; av 18 segfaults in
  `add_stream`).
- `scripts/recapture_alignment.py` — re-align chain (phone Setup → Re-align):
  stills → `dump_camera_params.py` (bundle) → `refine_rotation.py` →
  `dump_camera_params.py --r1` (final, atomic writes).
- `afl_cam/seam_refine.py` + `scripts/refine_seam.py` — fits the measured
  overlap misalignment (parallax) as a smooth field and warps both maps by
  half of it, fading out away from the overlap, matches exposure, and
  re-cuts the blend as a 64 px smoothstep ramp along the min-difference
  seam path, kept off the overlap's outer quarters (lens rims) and fully
  inside it (until 2026-10-20 a float32 tie in `seam_path` sent the path
  down the overlap's left edge: a hard cut at cam1's rim; a live set cut
  before that needs one Re-align to get the soft blend); last step of the re-align
  chain, writes only if the stitch score improves (`.prev` maps kept).
- `afl_cam/stitch_score.py` + `scripts/stitch_score.py` — THE stitch
  quality score (overlap misalignment median/p95 px, exposure step, seam
  crop) on a frame pair + live map set; the rig CI prints it and uploads
  the crop (`stitch-score` artifact). `scripts/measure_alignment.py`
  compares alignment variants with the same ruler.

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
- **Recording files are fdatasync'd every ~2 s by `sync_thread`** (opens the
  live session's files by name, touches no encoder or FILE*), so a power cut
  loses seconds, not the page cache; REC_STOP syncs once more after closing.
- **White balance (cam2stitch, 2026-10-19)**: both cameras start on their
  own auto white balance for ~3 s, then go manual at ONE temperature. A
  manual temperature written before the camera's auto has run on the stream
  is IGNORED (rig diag: 2800-6500 K all gave the same strongly blue U/V in
  daylight, until auto had been on once) — that was the "blue picture" and
  the earlier "one half blue" bugs. The canvas warmth (V - U over mid-tone,
  low-colour points of the whole canvas) the auto gave is the reference:
  with `AFL_CAM_WB_K` unset the common temperature follows the light to keep
  it (slowly, ~0.02 warmth per K); a kelvin value holds that temperature; 0
  leaves the cameras on their own auto. The units differ: cam1 renders ~6
  warmer in the overlap at any temperature (and under auto), so the
  temperature is split per camera, steered until the overlap's warmth
  matches (logged on the `seam trim:` line; `white balance:` lines log the
  lock and the follow). Each camera's value is re-checked every 300 frames.
  The dim-light cap (30 ms) is lifted with the camera's brightness then
  gamma.
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
- **bcmdhd AP mode (2026-10-11, root cause of the invisible AP)**: the chip
  can do AP, but only if nothing else touches wlan0. NM/gnome-shell scan
  requests while hostapd owns the interface flap it → the firmware wedges
  (stuck at 5 GHz channel 34, garbage SSID buffer, `iw` keeps reporting
  `type managed`, hostapd crash-loops "key not allowed / Failed to set
  beacon parameters") — and the wedge survives soft rfkill AND module
  reload; only the SDIO power-on sequence clears it. Rules: `field_wifi.sh`
  must (1) `nmcli dev set wlan0 managed no` and (2) `iw dev wlan0 set type
  __ap` BEFORE hostapd starts — hostapd's own set-type never sticks and
  without it keys/beacons are refused. `iw dev wlan0 info` is the only
  board-side proof that matters: `type AP` + `channel 6 (2437 MHz)`, plus a
  hostapd log that does NOT loop. Recovery without reboot
  (`field_wifi.sh recover`): rmmod bcmdhd_sdio → drop_caches (CMA) →
  unbind/rebind `fe2d0000.mmc` (dwmmc_rockchip; re-runs the sdio-pwrseq
  gpio85 power-on) → modprobe. Watch for a stuck polkit auth overlay too —
  a wedged gnome-shell agent respawns `polkit-agent-helper-1` at 100% CPU;
  restart polkitd + NetworkManager to clear the pending request.
- **Alignment math**: K and the lens (k1, k2, k3) are measured from the
  checkerboard stills (a full-screen board on a flat laptop display; NOT a
  wavy paper board) and tracked in `afl_cam/intrinsics/`; R is refined in
  **canvas space** with K and the lens fixed (`refine_rotation.py`). The
  lens is near-rectilinear (corner rows stray 0.3 px median, 1.3 px p95 from
  a straight line), so it is worth about 1 px in the overlap (far-field
  matches, 2026-10-12: 1.8 px median off after the rotation fit with the
  lens zeroed, 1.25 px with it); the large misalignment on indoor
  re-align stills is parallax, not the lens. Never keep a K fitted WITH
  distortion and drop the distortion (the old zeroing did). Close-range,
  low-tilt boards constrain the focal length weakly (a pinhole refit lands
  at fx 2150-2180 and fits 5x worse), so refit with boards tilted up to
  ~30 deg across the whole frame. The 2-view bundle is degenerate on
  grass (focal 2125 vs measured 2237 → 13–70 px ghosting). Do NOT reintroduce
  per-session bundling as the primary path.
- **Metric traps**: ray-direction angular residuals measure the cameras'
  toe-in angle, NOT stitch alignment (per-camera `tl` absorbs toe-in) — only
  canvas-space residuals measure ghosting. Unblurred tile matching
  over-reads cross-camera shifts (differing exposure/gain decorrelate fine
  texture) — always Gaussian-blur σ=8 first. The ruler is normalised
  cross-correlation with a sub-pixel peak (2026-10-12): the old phase
  correlation of gradient energy UNDER-read small shifts ~2× (a 2 px ghost
  read 1.0 px), so older "~0.5 px" figures are optimistic.
- **Exposure (cam2stitch, 2026-10-19)**: one virtual camera. Both cameras
  run the SAME manual exposure and gamma from the first frame (no camera
  auto phase), brightness 0, fixed WB. The meter is a canvas-space grid over
  the field (below `AFL_CAM_HORIZON`, which the daemon passes from the
  framing guides), read from whichever camera renders each point: the field
  median goes to `AFL_CAM_TARGET_LUMA` (120), held above 1% field clipping,
  pulled down above 2%; the sky may clip. Brighter = exposure up to the cap
  (`AFL_CAM_AE_CAP_MS`, default 30), then camera gamma to 300, then up to 2x
  shader gain; darker in reverse. Exposure snaps to 10 ms steps above 10 ms
  (`AFL_CAM_FLICKER_HZ`, 50; 0 = off). A slow overlap trim (+-8%) matches
  the two units. Measured: no analog gain exists in manual mode (no XU
  control changes the image), the brightness control is a pure offset
  (blacks 63 at 64), gamma 300 lifts a dark field 2 -> 23 with blacks 1 ->
  10, and the two units differ ~1% unlifted, ~5% at gamma 300. Never assume
  fps limits from time-of-day: exposure past 33 ms drops the sensor rate
  (40 ms ~26 fps, 66 ms ~17-20 fps). cam2h264 (separate mode) still runs its
  own per-camera auto-settle-then-lock.
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
  (docs/archive/iphone-control-plan.md build order steps 2–6): cam2stitch daemon mode
  (standby/record toggle + ctl socket + stitched H.264 preview + proxy),
  MediaMTX v1.21.2 (RTSP in :8554, WHEP :8889, HLS :8888), afl-camd
  (supervise/adopt/idle-close, REST + WebSocket, finalize with hvc1 master
  + faststart proxy + thumbnail, clock sync, auto-stop, schedule, GPIO
  stubs), phone web UI. Verified: record cycles (master 99% frame capture,
  proxy muxes, thumbs), Range/206 serving, WS pushes, RTSP decode +
  HLS playlist, idempotent start/stop, DELETE, crash-retry + socket
  adoption. **NOT verified: Safari on the board AP — see
  `field_desk_test.md` and run it before game day.**
- Master encode capacity note: at 80 Mbps HEVC the full canvas sustains
  roughly the sensor rate in dim scenes (~13–20 fps); at brighter light
  (~24–34 fps input) the encoder drops frames when input exceeds its
  capacity (pre-existing MPP real-time drop behaviour, same in legacy
  mode). Daylight 30 fps verification still pending.
- Live stitch at 5304×1876 canvas, quality parity with `cv2.remap` math
  (Y corr 0.999965), encoder + GPU pipelined ≈ 30 fps capacity.
- Frozen alignment refined for the static rig: overlap misalignment ~0.5 px
  median by the old (under-reading) ruler; the accurate ruler reads median
  18 px / p95 33 px and a 1.24× exposure ratio on the re-align stills
  (2026-10-12) — the rig CI's stitch-score line has the current number (was 13–70 px before the canvas-space refinement). Near-field grass
  (~19 px bottom band) is genuine parallax — physics, not a bug.
- Re-align from the phone works end-to-end on the rig (rig CI, 2026-10-12)
  and streams logs; dump writes are atomic.
- Live preview while recording: the recorders write the raw 4K UVC JPEG
  payload to `<session>/camN_preview.jpg` every `AFL_CAM_PREVIEW_SECS`
  (atomic tmp+rename; config default 1 s); the phone shows cam2h264's while
  recording in separate mode. Live mode uses the stitched H.264 preview
  (cam2stitch daemon mode → MediaMTX). (The
  old MPP-jpegenc downscale was never wired up and is removed.)
- Encode defaults (2026-09-07): **HEVC** 60 Mbps per camera, 80 Mbps for the
  stitched canvas (`AFL_CAM_CODEC=h264` to revert; measured via `cam2h264
  --bench` — dual-concurrent 4K30 HEVC@60M sustains ~49 fps each on the
  single rkvenc core, HEVC is faster than H.264 at the canvas size). CBR
  scales with actual fps, so dim-light ~20 fps recordings undershoot the
  target proportionally.
- Pending: the `field_desk_test.md` iPhone run; outdoor bright-daylight
  30 fps verification; NVMe heatsink; gnome-shell burns ~385% CPU
  (software-rendered desktop — consider a lighter session, or none: the
  board needs no desktop now).

## More context

- `orange_pi_5_max_setup.md` — board setup history (packages, MPP, libmali,
  IRQ pinning, verification cheat-sheet).
- `docs/archive/hardware_stitch_plan.md` / `docs/archive/live_stitch_plan.md` — original design plans.
- Session memory (auto-loaded): hardware topology, NVMe thermal crisis,
  alignment crisis + fixes, MPP quirks.
