# afl-cam

Dual-4K30 recording + hardware-accelerated panoramic stitching for amateur AFL
football, built for the **Orange Pi 5 Max (RK3588, Armbian)**.

Two Sony IMX415 4K USB cameras sit side by side in a rigid frame with a
~15–25% overlap band in the middle. The app records both feeds plus their USB
microphones and produces one wide stitched video — **live on the GPU during
recording** (no post step), or per-camera files stitched afterwards.

```
            ┌─────────┐      ┌─────────┐
            │  cam0   │  ~~  │  cam1   │        (static rig, frozen alignment)
            │ (left)  │  ~~  │ (right) │
            └────┬────┘  ~~  └────┬────┘
                 ▼    overlap     ▼
        outputs/20260901_190732/
          cam0.mp4  cam1.mp4          ← per-camera recordings (separate mode)
          cam0.pts cam1.pts           ← per-frame kernel timestamps (the sync)
          cam0.wav  cam1.wav          ← USB mics
          stitch.raw.h264 + .pts      ← live-stitch mode output
          stitch_video.mp4            ← stitched result
          panorama.jpg                ← debug still
```

## How it works (current architecture)

- **Capture**: native C recorders (`afl_cam/native/`) — V4L2 UVC → hardware
  MJPEG decode (jpegd) → NV12, no GStreamer in the hot path.
- **Recording, separate mode**: one `cam2h264` per camera → rkvenc H.264 CBR
  20M → `.raw.h264` + a `.pts` sidecar, muxed to `.mp4` on completion.
- **Recording, live mode** (default): `cam2stitch` records BOTH cameras in one
  process and writes the stitched panorama directly — Vulkan compute on the
  Mali G610 (map-texture spherical warp + feather blend, NV12 in/out) →
  rkvenc H.264 CBR 30M at the full **5304×1896** canvas. GPU stages are
  pipelined with the encoder (~30 fps capacity when light allows).
- **Sync**: per-frame kernel-monotonic `.pts` timestamps (the xhci completion
  clock — no clap sync, no genlock). The stitcher pairs frames by nearest
  timestamp and retimes the mux (`afl_cam/retime.py`).
- **Alignment**: frozen in `calibration/stitch_live_full/` (measured
  checkerboard K + a canvas-space-refined inter-camera R). The rig is static;
  both the live stitch and the post-stitcher use these frozen params. See
  *Alignment* below.

## Install / run

**Requirements:** Python ≥ 3.12, [uv](https://docs.astral.sh/uv/), ffmpeg,
GTK3 + PyGObject, `v4l2-utils`. Board-level setup (MPP, librga, libmali,
RKNN, IRQ pinning, NVMe fixes) is documented step-by-step in
[`orange_pi_5_max_setup.md`](orange_pi_5_max_setup.md).

```bash
uv sync                       # project venv (py3.14, GTK app + engine)
uv run afl-cam ui             # the GTK app
```

Two Python environments exist on the board:

| venv | uses |
|---|---|
| project `.venv` (py3.14, has `gi`) | the UI, engine, config, retime caller |
| `/home/hamish/venvs/cv2-new` (OpenCV 5 + PyAV 13) | stitcher, alignment scripts, retime worker |

The GPU recorder needs the libmali Vulkan ICD (spawned processes don't
inherit shell env — `engine.py` sets it from config):
`VK_DRIVER_FILES=/opt/libmali-g610/usr/share/vulkan/icd.d/mali.json`,
`LD_LIBRARY_PATH=/opt/libmali-g610/usr/lib/aarch64-linux-gnu{:/mali}`.

## Usage

The UI has everything: previews (tap to refresh), **Mode** dropdown
(Live / Separate), resolution, duration, record, stitch, play, delete,
**Re-align Cameras**, and a collapsible recordings list. On the 5" screen it
shrinks to 420×240.

```bash
uv run afl-cam ui               # the app
afl-cam list                    # sessions with stitch status
afl-cam stitch --session 12     # stitch a separate-mode session
afl-cam stitch --all            # stitch every unstitched session
```

Recordings land in `outputs/<YYYYMMDD_HHMMSS>/` with rows in `recordings.db`
(SQLite, WAL).

## Alignment (re-aligning the static rig)

The bundle adjustment that used to run per session is **degenerate on a grass
field** (focal/R entangled — it once estimated focal 2125 vs the measured
2237 and left 13–70 px of ghosting in the overlap). Since the rig is static:

1. **K (intrinsics)** is measured once from a checkerboard
   (`calibration/cam{0,1}_calib.npz` — distortion deliberately zeroed).
2. **R (rotation)** is refined in **canvas space** from SIFT matches with the
   measured K fixed — `scripts/refine_rotation.py` (RANSAC over 2-point
   rotation hypotheses + LM on inliers). This is what kills the ghosting.

The **Re-align Cameras** button runs the whole chain (output streams to the
terminal you launched the UI from):

```
scripts/recapture_alignment.py          capture one settled still per camera
  └─ scripts/dump_camera_params.py      bundle → maps/weights/params (+atomic writes)
  └─ scripts/refine_rotation.py         canvas-space R refinement
  └─ scripts/dump_camera_params.py --r1 refined_cams.txt   final freeze
```

Tip: stand in the overlap at playing distance during the capture — features
on you bias the fit plane to where the players run. Verify alignment any
time with `scripts/measure_alignment.py <cam0.jpg> <cam1.jpg>` (tile
phase-correlation with a blur pre-filter; ~0.5 px median mid-band = good).

## Scripts

| script | purpose |
|---|---|
| `scripts/recapture_alignment.py` | one-command re-alignment (UI button backend) |
| `scripts/dump_camera_params.py` | stills → `params.bin`, `map0/1.bin`, `w0/1.bin`, `cams.txt` (+ `--r1`, `--canvas-width`) |
| `scripts/refine_rotation.py` | canvas-space rotation refinement (`--band` selects a depth band) |
| `scripts/measure_alignment.py` | overlap misalignment in px (the ghosting ruler) |
| `scripts/verify_nv12.py` | GPU stitch vs exact `cv2.remap` reference (quality parity) |
| `afl_cam/retime.py` | VFR `.h264` + `.pts` → CFR `.mp4` (run via cv2-new venv) |

## Known issues / gotchas

- **Root NVMe (Samsung 980) overheats and drops the PCIe link → ext4 errors
  → black screen.** `pcie_aspm=off nvme_core.default_ps_max_latency_us=0` are
  set in `/boot/armbianEnv.txt`; the drive still needs a heatsink
  (M.2 slot gets no airflow; check `sensors`-style
  `cat /sys/class/hwmon/hwmon7/temp1_input` — crit is 84850 m°C).
- **Camera discovery**: video nodes shuffle across reboots and a camera on a
  USB 2.0 port enumerates on the EHCI controller — `config.py` resolves both
  cases at runtime. If the sides come out swapped, use the **Swap** button.
- **Frame rate** = min(sensor exposure, pipeline). Auto-exposure picks the
  rate; on a bright field it locks 30 fps. Manual exposure without light
  records black frames — trust the auto-exposure.
- **Only one stream per camera at a time** — close the app before running
  scripts that open the cameras.

## Handover to a fresh Claude session

Read [`CLAUDE.md`](CLAUDE.md) (loaded automatically) — it condenses the
hard-won invariants. The hardware history lives in
[`orange_pi_5_max_setup.md`](orange_pi_5_max_setup.md);
`hardware_stitch_plan.md` / `live_stitch_plan.md` are the original design
plans (mostly realised; kept for archaeology).
