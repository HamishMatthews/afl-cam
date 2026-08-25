# Hardware Stitching at Record Time — Plan

> **Superseded 2026-08-25 by [live_stitch_plan.md](live_stitch_plan.md).** The RGA
> perspective-transform path below is dead — librga 1.10.6 on this board has no
> perspective API, and the Mali-G610 Vulkan compute stitch (analytic spherical warp,
> 9.3 ms/frame, SSIM 0.9997) is validated and is the chosen path. Kept for history.

Investigation plan for stitching the two camera feeds on RK3588 hardware at record time,
instead of the current offline OpenCV/CPU stitch. **Planning only — implementation is a
separate (much bigger) piece of work.**

## 1. Goal

Move the panorama step onto the RK3588's hardware blocks so it can run while recording:

```
cam0 ─ V4L2 ─→ jpegd ─ NV12 ─┐
                             ├─→ RGA warp + blend ─ NV12 ─→ rkvenc ─→ stitched .mp4
cam1 ─ V4L2 ─→ jpegd ─ NV12 ─┘
```

Two delivery shapes to decide between (see §6):
- **A. Real-time single file**: one stitched panorama .mp4 produced during recording.
- **B. Concurrent hardware stitch**: keep per-camera recordings (as today) AND run a
  hardware-accelerated stitch pass alongside, finished moments after recording ends.
  Lower risk, keeps full-quality sources, reuses the existing session/stitch flow.

## 2. Why the hardware blocks fit

| Step | Today (offline) | Hardware path |
|---|---|---|
| MJPEG decode | OpenCV/CPU (per stitch pass) | jpegd (already in cam2h264.c) |
| Warp (spherical remap) | `cv2.remap` per pixel, CPU | RGA3 perspective transform (3×3 matrix) |
| Overlap blend | OpenCV feather/blend, CPU | RGA composite/blend (two NV12 inputs) |
| Encode | ffmpeg h264_rkmpp (CPU pipe of BGR) | rkvenc direct (already in cam2h264.c) |

Key facts from the setup work (see orange_pi_5_max_setup.md):

- jpegd is **one shared unit, ~46 4K-JPEG decodes/sec total** — dual 4K30 hardware decode
  does not fit (~60 fps demanded). Mitigation: CPU-decode one stream (native ffmpeg mjpeg
  proved ~30 fps at ~1.5 cores) + jpegd the other. CPU headroom exists (whole recorder
  today uses ~0.2 cores/stream).
- RGA3 on RK3588 supports **perspective transform with a 3×3 coefficient matrix** and
  two-source blending; librga 1.10.6 is installed with its demo tools (verify the
  transform sample: `rgaImDemo` / `im2d` transform demos in the librga repo samples).
- The native pipeline (cam2h264.c) is ~380 lines and already does V4L2→jpegd→rkvenc with
  zero conversions — the natural base for the stitched variant.

## 3. The core problem: spherical maps → RGA coefficients

The current quality stitch warps each frame with OpenCV's exact spherical projector
(`afl_cam/stitcher.py` — `VideoStitcher` / spherical maps). RGA can only apply a global
**3×3 perspective transform** per image — a homography — not an arbitrary per-pixel remap.

Plan: fit a homography `H` per camera that best approximates the existing spherical maps
(e.g., RANSAC/LM fit of `H(x) ≈ map(x)` over the frame, using the same calibration
outputs). Then convert `H` to RGA's coefficient format (librga's transform samples show
the layout — 6 or 9 coefficient variants depending on the API call).

Expected tradeoff: slight edge distortion vs the spherical path. Validate visually
against the OpenCV output; if unacceptable, a split-frame approach (warp whole frames
with H, then crop/paste with a static alpha ramp in the overlap) is the fallback.

## 4. Implementation stages (each independently verifiable)

1. **RGA feasibility spike** — run librga's perspective-transform demo on 4K NV12 frames;
   confirm throughput ≫ 30 fps and coefficient layout. (Day 1 scope.)
2. **Homography fitter** — Python tool: read the existing calibration maps
   (`VideoStitcher.calibrate` output), fit H per camera, dump coefficients to JSON.
   Compare H-warped stills vs spherical stills side by side.
3. **Offline hardware stitcher** — extend cam2h264.c (or a sibling `cam2stitch.c`):
   two .mp4/.h264 inputs → jpegd… no — inputs are H.264 here, so rkvdec → RGA warp+blend
   → rkvenc → panorama.mp4. This validates the hardware chain without realtime pressure
   and gives a quality benchmark vs the current stitch.
4. **Record-time variant** — live V4L2 inputs, decode split (jpegd + CPU mjpeg),
   RGA warp+blend, rkvenc → single stitched stream (shape A) or run alongside the
   existing per-camera recorders (shape B).
5. **afl-cam integration** — new stitch mode (`--hw-stitch` / UI toggle); session catalog
   records the extra output; keep the OpenCV quality mode as fallback.

## 5. Known risks / open questions

- RGA output stride/alignment constraints on the panorama width (16-alignment).
- Homography approximation quality on wide-FOV shots; whether the overlap blending in
  RGA can match the current feather (probably a simple alpha ramp instead).
- jpegd unit contention in shape A (mitigated by CPU-decoding one stream — must be
  re-benchmarked with the warp stage added).
- librga 1.10.6 API specifics for transform + composite (the SDK-prebuilt lib; demos are
  in ~/builds/librga — confirm the exact im2d calls).
- Memory bandwidth with 3 hardware consumers (decode×2, warp, encode) at 4K30 — expected
  fine (RGA is designed for exactly this), but measure.

## 6. Decisions needed before implementation

1. Shape A (single stitched file, live) vs shape B (concurrent hardware stitch next to
   per-camera recordings)? B is lower risk and keeps sources; A is the end goal.
2. Accept homography-approximate warping for the hardware path (spherical stays
   available offline)?
3. Target output: 3840-wide panorama at 30 fps, H.264 ~30 Mbps?

## 7. Where the pieces live today

- `afl_cam/native/cam2h264.c` — native recorder (base to extend)
- `afl_cam/stitcher.py` — calibration, spherical maps, blend, encoders
- `~/builds/librga` — RGA demos/samples for the API spike
- `orange_pi_5_max_setup.md` — board setup and performance measurements
