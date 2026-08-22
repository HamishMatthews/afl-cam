# Orange Pi 5 Max — Hardware Recording Setup

Setup, findings, and verification for running afl-cam's hardware recording pipeline on the
Orange Pi 5 Max (RK3588). Written 2026-08-23; all commands were run and verified on this board.

## 1. Hardware & OS

| Component | Detail |
|---|---|
| Board | Orange Pi 5 Max — RK3588 (4× Cortex-A76 @ 2.3 GHz, 4× Cortex-A55 @ 1.8 GHz), 8 GB RAM, 918 GB NVMe |
| OS | Armbian (Ubuntu 26.04 "resolute"), kernel `6.1.115-vendor-rk35xx` (Rockchip vendor kernel — required for the NPU/VPU/RGA drivers) |
| Cameras | 2× Sony IMX415 on Sunplus USB UVC bridges (`1bcf:28c4` "Lightburn Cam"), **USB 2.0**, MJPEG up to 3840×2160@30 |
| Camera nodes | cam0 = `/dev/video0` (Bus 1, xhci.12), cam1 = `/dev/video2` (Bus 3, xhci.11). `/dev/video1` and `/dev/video3` are per-camera metadata nodes |
| Camera mics | ALSA `hw:3,0` (cam0, card "Camera"), `hw:4,0` (cam1, card "Camera_1") |

The cameras are **USB UVC, not native MIPI CSI** — frames arrive MJPEG-compressed, and the
camera bridge does its own ISP/debayer/JPEG encode. Nothing raw reaches the RK3588.

## 2. Base packages (apt)

```bash
sudo apt-get update
sudo apt-get install -y cmake meson ninja-build python3-pip python3-venv \
    ffmpeg gstreamer1.0-tools libdrm-dev v4l-utils
```

(v4l-utils was preinstalled; ffmpeg 8.0.1 from apt has **no** Rockchip MPP support — see §5.)

### afl-cam python environment

```bash
sudo apt-get install -y python3-opencv python3-numpy   # cv2/numpy for the system python 3.14
python3 -m venv --system-site-packages .venv           # created from the SYSTEM python
```

The venv must be created **from the system python3** (not a uv-managed python):
`gi`/`cv2` come from the system dist-packages (compiled for 3.14), and a uv-managed
python 3.12 venv can't import them.

## 3. Rockchip MPP (VPU codecs) — source build

```bash
git clone https://github.com/rockchip-linux/mpp ~/builds/mpp
cd ~/builds/mpp
cmake -DRKPLATFORM=ON -DHAVE_DRM=ON -DCMAKE_INSTALL_PREFIX=/usr/local ..
make -j4 && sudo make install && sudo ldconfig
```

- Version built: **1.1.0** (2026-08-11). Installs `librockchip_mpp` + test tools
  (`mpi_enc_test`, `mpi_dec_test`, …) into `/usr/local`.
- Verified with a real hardware encode: `mpi_enc_test` produced H.264 via `/dev/mpp_service`.

## 4. librga (2D accelerator) — prebuilt from official repo

The airockchip/librga repo no longer ships the library source (it lives in the Rockchip SDK),
so install the official prebuilt for RK3588 Linux:

```bash
git clone https://github.com/airockchip/librga ~/builds/librga
sudo cp ~/builds/librga/libs/Linux/gcc-aarch64/librga* /usr/local/lib/
sudo cp -r ~/builds/librga/include/rga /usr/local/include/
sudo ldconfig
```

- Version **1.10.6**. Verified: `rgaImDemo` reports `RGA_2_Enhance RGA_3`, `rga_copy_demo`
  executes a real hardware copy.
- Note: this Armbian image has no `/dev/dma_heap` — the RGA dma_heap allocator is unavailable
  (use other allocators).

## 5. ffmpeg-rockchip (hardware encode/decode in ffmpeg)

The stock apt ffmpeg has no MPP support and there is no jellyfin-ffmpeg package in the
Armbian repos, so build the official Rockchip fork:

```bash
# librga ships without a pkg-config file — create one first:
cat <<'EOF' | sudo tee /usr/local/lib/pkgconfig/librga.pc
prefix=/usr/local
exec_prefix=${prefix}
libdir=${exec_prefix}/lib
includedir=${prefix}/include/rga

Name: librga
Description: Rockchip RGA 2D graphics library
Version: 1.10.6
Libs: -L${libdir} -lrga
Cflags: -I${includedir}
EOF

git clone https://github.com/nyanmisaka/ffmpeg-rockchip ~/builds/ffmpeg-rockchip
cd ~/builds/ffmpeg-rockchip
export PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:$PKG_CONFIG_PATH
./configure --prefix=/usr/local --enable-gpl --enable-version3 --enable-libdrm \
    --enable-rkmpp --enable-rkrga
make -j8 && sudo make install && sudo ldconfig
```

- Installs to `/usr/local/bin/ffmpeg` (shadows stock `/usr/bin/ffmpeg`; the stock one is untouched).
- Provides `h264_rkmpp`/`hevc_rkmpp`/`mjpeg_rkmpp` encoders+decoders and `scale_rkrga`/`vpp_rkrga` filters.
- **Critical flag learned the hard way:** with `h264_rkmpp` you MUST pass `-pix_fmt nv12`.
  Default format negotiation picks `nv16` and inserts a 4K software 422→420 conversion that
  costs a full CPU core and caps throughput at ~23 fps.
- The rkrga filters are built but **unusable from the CLI**: they require `drm_prime` frames
  and the rkmpp decoders deliver software frames, so auto-negotiation fails. They exist for
  custom code that feeds native drm_prime frames.

## 6. RKNN runtime (NPU)

```bash
# Runtime: librknnrt.so from the repo tag (GitHub releases carry no binary assets)
# https://github.com/airockchip/rknn-toolkit2  (tag v2.3.2, rknpu2/runtime/Linux/librknn_api/aarch64/)
sudo cp librknnrt.so /usr/local/lib/ && sudo cp librknnrt.so /usr/lib/ && sudo ldconfig
#   /usr/lib copy is REQUIRED — the lite2 python wrapper hardcodes that path

# Python lite API (system python is 3.14; wheels are cp312 — use uv)
curl -LsSf https://astral.sh/uv/install.sh | sh
~/.local/bin/uv python install 3.12
~/.local/bin/uv venv --python 3.12 ~/venvs/rknn
~/venvs/rknn/bin/pip install rknn_toolkit_lite2-2.3.2-cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
```

- Verified end-to-end: real `mobilenet_v1.rknn` inference on the NPU
  (`RKNPU v2`, driver 0.9.8, output `(1, 1001)`).
- The NPU is exposed as a DRM render node: **`/dev/dri/renderD129`** (`DRIVER=RKNPU`) — not `/dev/rknpu`.

## 7. Camera configuration findings

- **Manual exposure is required for stable 30 fps.** cam1's auto-exposure settled at ~50 ms
  exposure, capping the sensor at ~20 fps (the sensor is exposure-rate-limited). Fix:
  `v4l2-ctl -d /dev/video2 --set-ctrl=auto_exposure=1 --set-ctrl=exposure_time_absolute=100`
  (10 ms → ~28.5-30 fps at all resolutions). Exposure controls do NOT reset on stream open.
- ffmpeg/v4l2 must request the frame interval explicitly (`S_PARM` 1/30) for the bridge to
  honor 30 fps — v4l2-ctl never does, which is why its numbers look lower.
- No quality/bitrate controls are exposed by the bridge — only standard UVC controls.

## 8. Native recorder — `cam2h264` (the production path)

`afl_cam/native/cam2h264.c` — a ~380-line C recorder with no ffmpeg, no GStreamer, no format
conversions:

```
V4L2 (UVC MJPEG) → jpegd (MPP_DEC_SET_OUTPUT_FORMAT = NV12) → rkvenc (H.264 CBR) → .h264 file
```

The decoded NV12 frame goes straight into the encoder (zero copies, zero conversions).

Build: `gcc -O2 -I/usr/local/include/rockchip -o cam2h264 cam2h264.c -L/usr/local/lib -lrockchip_mpp -lpthread`

**Measured performance (dual 4K30, 300 frames each):**

| Pipeline | Dual fps | Total CPU |
|---|---|---|
| ffmpeg-rockchip wrapper | ~20-27 + duplicate-padded frames | 90%+ |
| **native cam2h264** | **29.5 + 29.5, 0 dropped frames** | **~30% (0.1-0.2 cores per stream)** |

The ffmpeg wrapper's per-frame copies and conversions were the root of all dual-stream
contention; with them gone, ~5.5 cores + the NPU stay free during recording (headroom for
live inference). Per-frame pts is propagated from the V4L2 capture timestamp through the
pipeline (ready for clap-sync sidecars).

### MPP 1.1.0 API notes (for future code)

- Uses the new string-key config API: `mpp_enc_cfg_set_s32(cfg, "prep:width", …)`,
  applied with `MPP_ENC_SET_CFG`. The old struct API (`mpp_enc_prep_cfg_init` etc.) is gone.
- `MPP_FMT_YUV420SP == 0` (NV12).
- `MPP_ENC_GET_HDR_SYNC` and encoder output packets need real `MppBuffer`s (the kmpp ioctl
  path crashes/misbehaves with bufferless packets).
- After `mpp_packet_deinit`, release your own `mpp_buffer_get` refs — otherwise the buffer
  group is exhausted after N frames and `mpp_buffer_get` blocks forever.
- MJPEG decode uses the "advanced" path: preallocated output frame + `KEY_OUTPUT_FRAME`
  packet meta + external buffer group sized `hor_stride × ver_stride × 2` (420/422 safe).

## 9. USB IRQ affinity fix (not persistent — redo after reboot)

Both cameras' xhci IRQs default to CPU0 (an A55) — under load, URB servicing falls behind and
the higher-traffic camera drops frames. Move each camera's controller to its own A76 core:

```bash
# IRQ numbers from: grep -E "xhci" /proc/interrupts  (95 = usb1/cam0, 106 = usb3/cam1 on this board)
echo 10 | sudo tee /proc/irq/95/smp_affinity   # CPU4
echo 20 | sudo tee /proc/irq/106/smp_affinity  # CPU5
```

To persist: a small systemd service or rc.local entry. (irqbalance is not installed.)

## 10. Other environment notes

- The GNOME desktop on this image is **software-rendered** — gnome-shell alone eats ~180% CPU
  (ghostty ~100%). For performance work, close Firefox and/or pin the desktop to the A55s:
  `taskset -a -p 0-3 $(pgrep -x gnome-shell)` (and ghostty). Fixing GPU-accelerated desktop
  rendering is a separate work item.
- Keep an eye on swap (3.9 GiB configured) — Firefox pushed memory to ~54% of 8 GB.

## 11. Verification cheat-sheet

```bash
# Cameras present + formats
v4l2-ctl --list-devices && v4l2-ctl -d /dev/video0 --list-formats-ext

# Dual 4K30 capture check (should be ~29.4 fps each)
/usr/local/bin/ffmpeg -f v4l2 -input_format mjpeg -video_size 3840x2160 -framerate 30 \
    -i /dev/video0 -t 5 -c copy -f null - &
/usr/local/bin/ffmpeg -f v4l2 -input_format mjpeg -video_size 3840x2160 -framerate 30 \
    -i /dev/video2 -t 5 -c copy -f null - &

# Native dual recording (10 s = 300 frames each)
cam2h264 /dev/video0 cam0.h264 300 & cam2h264 /dev/video2 cam1.h264 300 &

# Verify the files
ffprobe -count_frames -select_streams v:0 -show_entries stream=nb_read_frames,width,height,codec_name cam0.h264

# Confirm the VPU actually did the work (IRQ counters increase during recording)
grep -E "rkvenc|jpegd" /proc/interrupts

# NPU smoke test
~/venvs/rknn/bin/python -c "from rknnlite.api import RKNNLite; print(RKNNLite.list_devices())"
```

## 12. Install locations (summary)

| Component | Location |
|---|---|
| librockchip_mpp + tools | `/usr/local/lib`, `/usr/local/bin` (source: `~/builds/mpp`) |
| librga + demos | `/usr/local/lib`, `/usr/local/include/rga`, `/usr/local/bin` |
| librga.pc | `/usr/local/lib/pkgconfig/librga.pc` (hand-written) |
| ffmpeg-rockchip | `/usr/local/bin/ffmpeg` (stock at `/usr/bin/ffmpeg` untouched; source: `~/builds/ffmpeg-rockchip`) |
| librknnrt.so | `/usr/local/lib` **and** `/usr/lib` (wrapper hardcodes the latter) |
| RKNN python venv | `~/venvs/rknn` (python 3.12 via uv) |
| cam2h264 recorder | `afl_cam/native/cam2h264.c` (built binary lives alongside) |
| Test recordings | `afl-cam/outputs/cam-hardware-tests/`, `afl-cam/outputs/raw-cam-tests/` (gitignored) |
