# Live GPU Stitching During Recording (cam2stitch)

## Context

The rig records two 4K30 streams (MJPEG → jpegd → NV12 → rkvenc per camera, `cam2h264.c`) and stitches in post on CPU. The user wants the **stitched panorama produced during recording** — no post step. All building blocks are validated: analytic spherical warp+blend shader (9.3 ms/frame, SSIM 0.9997 on Mali G610), `params.bin` calibration layout, kernel-timestamp `.pts` sync (shared monotonic clock), frame pairing by nearest timestamp, and `mux_with_pts_rate` (CFR at measured rate).

User decisions: composite output is the guarantee; **per-camera backups only if measured headroom allows** (flag, measured in S3). **Live stitched preview is phase S5** (must not pressure the system). RGA is out (librga 1.10.6 has no perspective transform).

## Architecture

New binary `afl_cam/native/cam2stitch.c` (base = cam2h264.c, Vulkan init from gpu_stitch.c). One process, three threads:

```
cam0 ─ V4L2 MJPEG ─ jpegd ─ NV12 ─ ring0(4) ─┐
                                            ├─ main: pair by kernel pts (nearest + skip/dup,
cam1 ─ V4L2 MJPEG ─ jpegd ─ NV12 ─ ring1(4) ─┘   s0/e_cut trims) → Vulkan compute
                                                                  (NV12→NV12, SSBO)
                                                      → memcpy → rkvenc 3840×~1380 CBR 20M
                                                      → stitch.raw.h264 + stitch.pts (midpoint)
```

Target canvas 3840×~1380 (Level-5.2-safe; parity with post-stitch `output_width=3840`). Params regenerated via `scripts/dump_camera_params.py --canvas-width 3840`.

**Key design decisions:**

1. **Pre-recorded 3-slot command-buffer ring** — sidesteps the "one `vkBeginCommandBuffer` per recording" libmali quirk: nothing is ever re-recorded after init. Each slot k owns 4 input images (Y0/UV0/Y1/UV1), 4 staging buffers, 1 output SSBO, 1 cb, 1 fence, 1 descriptor set. Per frame: `vkWaitForFences(fence[k])` → memcpy 4 planes into staging (24.8 MB) → `vkQueueSubmit(cb[k], fence[k])` → memcpy SSBO→encoder frame (8 MB). Weights/params/pipeline bound once. Verify a per-frame re-record variant in S1 as documentation; primary design never depends on it.
2. **N-deep MPP frame rings** (4 slots/camera) replace the single-reused `out_frame`: slot states FREE→IN_DECODE→READY→HELD, producer blocks on FREE before DQBUF (backpressure), consumer deinits on release. Decoder contexts stay thread-confined; encoder only on main. Encoder input ring of 4 (async encode vs single-reuse today).
3. **NV12 in / NV12 out in-shader** (`stitch_nv12.comp`): Y-plane (R8_UNORM) + UV-plane (R8G8_UNORM) textures per camera, BT.601 convert→warp→blend→convert back. Chroma written only by (even,even) threads (block mean via 3 KB shared tile, no atomics). 8-bit SSBO storage feature-checked, uint32-packing fallback via `#if`. Warp/blend math copied verbatim from stitch.comp.
4. **Streaming pairing** — in-process port of `stitcher.py` pts logic (nearest + lo-compare, s0 start trim, e_cut end trim, skip/duplicate); composite pts = pair midpoint, strictly monotonic (keeps `mux_with_pts_rate` robust).
5. **v1 = staging memcpy** (proven path); **S2 spike = dma-buf zero-copy** (`mpp_buffer_get_fd` + `VK_EXT_external_memory_dma_buf`/`VK_KHR_external_memory_fd` — extensions present on this libmali; per-memory-type support unverified) — strictly additive, optional.
6. Encoder: cam2h264 config verbatim (CBR 20M, gop=fps, level 52, NV12), plus proper EOS flush at stop. `.pts` sidecar naming follows the existing `.raw.h264`→`.pts` convention; engine's `mux_with_pts_rate` (afl_cam/retime.py) works unchanged.

## Phases

**S1 — offline GPU loop validation (retires the core risks)** — `cam2stitch -src files|jpg` harness modes produce NV12 test frames through the production decode path; `scripts/verify_nv12.py` compares vs plain-math reference. Bars: SSIM ≥ 0.99, 1000-frame loop byte-stable (both cb-ring and re-record variants), <25 ms/frame, zero GPU stalls. Synthetic-pts pairing unit test vs `stitch_video_files` (identical midpoints/trims). Measure jpegd full-vs-limited range → set `RANGE_FULL`.

**S2 — dma-buf spike (optional/parallel)** — import jpegd NV12 fds as VkImages (kills the 4 upload copies); export composite SSBO fd → `mpp_buffer_import` as encoder input (kills the readback). Byte-compare vs S1; only adopt if clean.

**S3 — cam2stitch.c live soak** — 60 s + 5 min dual-camera recordings. Checks: median dt ≈ 33 ms (±10%), `gpu_stall_us == 0`, skip/dup counters logged, CMA stable, no leaks (VSZ flat). Quality cross-check: same scene live vs offline stitch, SSIM ≥ 0.97 on non-overlap region. **Headroom measurement for backups**: with the composite live, measure CPU/memory/encoder utilization; if comfortable, add optional per-camera backup encodes behind a config flag.

**S4 — engine/UI integration** — config: `LIVE_STITCH` flag, `STITCH_RECORDER_SRC/BIN`, `STITCH_PARAMS_DIR`, `STITCH_CANVAS_WIDTH=3840`, Vulkan env constants (engine must set `VK_DRIVER_FILES`/`LD_LIBRARY_PATH` explicitly — subprocesses don't inherit shell env; ICD verified at `/opt/libmali-g610/usr/share/vulkan/icd.d/mali.json`, `libMaliVulkan.so.1` in the `mali/` subdir). `ensure_cam2stitch()` + `can_live_stitch()` (`--probe` gate at startup and pre-record). `StitchRecorderMPP` class mirroring `CameraRecorderMPP`. UI: one spawn instead of two when live-stitch is available; single DB row `camera="stitch"` (free-form column, no db.py change); "Stitch Selected" shows "already stitched" for live sessions; **fallback = current per-camera path** if probe fails or a camera is missing.

**S5 — live stitched preview (later)** — optional `--preview-fifo`: tiny pre-recorded 2-cb downscale ring (GPU cost <0.5 ms), ~2 fps RGBA to a fifo the UI polls. Must not affect the recorded stream.

## Critical files

- `afl_cam/native/cam2stitch.c` (new) — V4L2/jpegd/encoder blocks + exposure machine lifted from `cam2h264.c` (129–266, 363–372, 384–579), Vulkan init from `gpu_stitch.c` (144–358), new ring/pairing/loop
- `afl_cam/native/stitch_nv12.comp` (new) + `.spv` — Y/UV sampling, BT.601, NV12 SSBO output
- `scripts/dump_camera_params.py` — add `--canvas-width`
- `scripts/verify_nv12.py` (new) — reference + SSIM bars
- `afl_cam/config.py`, `afl_cam/engine.py`, `afl_cam/ui.py` — integration + fallback
- Reference semantics to port: `afl_cam/stitcher.py` 866–896, 989–1022 (pairing/trims)

## Top risks

1. jpegd shared-unit throughput (historical ~46 4K-JPEG/s vs 60 demanded) — pairing design tolerates any lower rate; measure in S3; CPU-decode one stream as fallback. Only risk that could force a spec change.
2. libmali per-frame re-recording quirk — designed around (pre-recorded ring).
3. dma-buf import/export per-type unsupported — S2 is a spike; v1 staging is validated and fits budget.
4. MPP ring refcount subtleties — strict state machine; soak watches CMA/VSZ; `mpp_buffer_sync_ro_*` if stale reads appear.
5. 8-bit SSBO storage unavailable — feature check + uint32 packing fallback.
6. NV12 range mismatch — S1 measurement decides `RANGE_FULL`; round trip self-consistent either way.
