/*
 * cam2h264 - native RK3588 dual-purpose recorder prototype
 *
 * Pipeline: V4L2 (UVC MJPEG) -> MPP jpegd (NV12, hardware) -> MPP rkvenc (H.264, hardware)
 * No ffmpeg, no software format conversion, single tight loop.
 *
 * Build: gcc -O2 -I/usr/local/include/rockchip -o cam2h264 cam2h264.c \
 *            -L/usr/local/lib -lrockchip_mpp -lpthread
 *
 * Usage: cam2h264 /dev/video0 out.h264 [frames] [width] [height] [fps]
 */
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/time.h>
#include <unistd.h>
#include <linux/videodev2.h>

#include "rk_mpi.h"

#define MPP_ALIGN(x, a) (((x) + (a) - 1) & ~((a) - 1))

static volatile int g_running = 1;

static void sig_handler(int sig)
{
    (void)sig;
    g_running = 0;
}

typedef struct {
    void   *start;
    size_t  length;
} v4l2_buf_t;

static int64_t now_us(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000000 + tv.tv_usec;
}

static void set_exposure(int fd, int mode, RK_S32 value)
{
    struct v4l2_control ctrl;
    memset(&ctrl, 0, sizeof(ctrl));
    ctrl.id = V4L2_CID_EXPOSURE_AUTO;
    ctrl.value = mode;
    if (ioctl(fd, VIDIOC_S_CTRL, &ctrl) < 0)
        fprintf(stderr, "set exposure mode failed: %s\n", strerror(errno));
    if (mode == V4L2_EXPOSURE_MANUAL) {
        ctrl.id = V4L2_CID_EXPOSURE_ABSOLUTE;
        ctrl.value = value;
        if (ioctl(fd, VIDIOC_S_CTRL, &ctrl) < 0)
            fprintf(stderr, "set exposure time failed: %s\n", strerror(errno));
    }
}

/* Atomic JPEG snapshot write: the raw UVC MJPEG payload is a complete JPEG —
 * write it straight to *preview_path* via a temp file + rename so the UI
 * (which polls the file) never sees a half-written frame. */
static void write_preview(const char *preview_path, const void *jpeg, size_t size)
{
    char tmp[1024];
    snprintf(tmp, sizeof tmp, "%s.tmp", preview_path);
    FILE *f = fopen(tmp, "wb");
    if (!f)
        return;
    size_t n = fwrite(jpeg, 1, size, f);
    fclose(f);
    if (n != size)
        unlink(tmp);
    else
        rename(tmp, preview_path);
}

/* Mean luma of an NV12 frame over an 8x8 subsample — cheap brightness proxy
 * for the auto-settle-then-lock exposure loop. */
static double frame_luma(MppFrame frame)
{
    MppBuffer buf = mpp_frame_get_buffer(frame);
    if (!buf)
        return -1.0;
    RK_U8 *y = (RK_U8 *)mpp_buffer_get_ptr(buf);
    RK_U32 hstride = mpp_frame_get_hor_stride(frame);
    RK_U32 vstride = mpp_frame_get_ver_stride(frame);
    RK_U32 w = mpp_frame_get_width(frame);
    RK_U32 h = mpp_frame_get_height(frame);
    RK_U64 sum = 0;
    RK_U32 n = 0;
    for (RK_U32 yy = 0; yy < h; yy += 8)
        for (RK_U32 xx = 0; xx < w; xx += 8) {
            sum += y[yy * vstride + xx];
            n++;
        }
    return n ? (double)sum / n : -1.0;
}

int main(int argc, char **argv)
{
    const char *dev      = (argc > 1) ? argv[1] : "/dev/video0";
    const char *out_path = (argc > 2) ? argv[2] : "out.h264";
    RK_S32 frames         = (argc > 3) ? atoi(argv[3]) : 300;
    RK_U32 width          = (argc > 4) ? atoi(argv[4]) : 3840;
    RK_U32 height         = (argc > 5) ? atoi(argv[5]) : 2160;
    RK_U32 fps            = (argc > 6) ? atoi(argv[6]) : 30;
    RK_S32 exposure       = (argc > 7) ? atoi(argv[7]) : 0;     /* 0 = auto-lock, else 100us units */
    RK_S32 wall_sec       = (argc > 8) ? atoi(argv[8]) : 0;     /* 0 = unlimited; else stop after this many WALL seconds */

    /* Preview snapshot path: <out>.raw.h264 -> <out>_preview.jpg (raw UVC
     * MJPEG payloads, written every AFL_CAM_PREVIEW_SECS for the live UI). */
    char preview_path[1024];
    snprintf(preview_path, sizeof preview_path, "%s", out_path);
    {
        char *dot = strstr(preview_path, ".raw.h264");
        if (dot)
            strcpy(dot, "_preview.jpg");
        else
            strncat(preview_path, "_preview.jpg",
                    sizeof preview_path - strlen(preview_path) - 1);
    }
    /* Encode bitrate (kbps). The 4K30 UVC source MJPEG runs ~125 Mbps;
     * 20 Mbps starved grass/detail — 45 Mbps is the default sweet spot.
     * Tune with AFL_CAM_BITRATE_KBPS. */
    RK_S32 bps = 45000;
    if (getenv("AFL_CAM_BITRATE_KBPS"))
        bps = atoi(getenv("AFL_CAM_BITRATE_KBPS"));
    int64_t snap_interval_us = 2 * 1000000LL;
    if (getenv("AFL_CAM_PREVIEW_SECS"))
        snap_interval_us = (int64_t)(atof(getenv("AFL_CAM_PREVIEW_SECS")) * 1e6);
    int64_t last_snap_us = -snap_interval_us;

    int fd = -1, ret = 0;
    int stream_on = 0;
    FILE *fp_out = NULL;
    FILE *fp_pts = NULL;

    /* V4L2 */
    v4l2_buf_t *v4l2_bufs = NULL;
    RK_U32 n_v4l2 = 8;

    /* MPP decoder */
    MppCtx dec_ctx = NULL;
    MppApi *dec_mpi = NULL;
    MppBufferGroup frm_grp = NULL;
    MppBuffer frm_buf = NULL;
    MppBufferGroup pkt_grp = NULL;
    MppFrame out_frame = NULL;

    /* MPP encoder */
    MppCtx enc_ctx = NULL;
    MppApi *enc_mpi = NULL;
    MppEncCfg enc_cfg = NULL;
    MppBufferGroup enc_pkt_grp = NULL;

    RK_S32 dec_out_fmt = MPP_FMT_YUV420SP; /* NV12 - direct, no conversion */
    RK_U32 hor_stride = MPP_ALIGN(width, 16);
    RK_U32 ver_stride = MPP_ALIGN(height, 16);
    RK_S32 count = 0;
    int64_t t_start = 0;

    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);

    /* V4L2 open + config */
    fd = open(dev, O_RDWR | O_NONBLOCK, 0);
    if (fd < 0) {
        fprintf(stderr, "open %s failed: %s\n", dev, strerror(errno));
        return -1;
    }

    struct v4l2_capability cap;
    if (ioctl(fd, VIDIOC_QUERYCAP, &cap) < 0) {
        fprintf(stderr, "VIDIOC_QUERYCAP failed: %s\n", strerror(errno));
        goto OUT;
    }

    struct v4l2_format fmt;
    memset(&fmt, 0, sizeof(fmt));
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    fmt.fmt.pix.width = width;
    fmt.fmt.pix.height = height;
    fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG;
    fmt.fmt.pix.field = V4L2_FIELD_NONE;
    if (ioctl(fd, VIDIOC_S_FMT, &fmt) < 0) {
        fprintf(stderr, "VIDIOC_S_FMT failed: %s\n", strerror(errno));
        goto OUT;
    }
    fprintf(stderr, "v4l2: %ux%u MJPEG fmt\n", fmt.fmt.pix.width, fmt.fmt.pix.height);

    struct v4l2_streamparm parm;
    memset(&parm, 0, sizeof(parm));
    parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    parm.parm.capture.timeperframe.numerator = 1;
    parm.parm.capture.timeperframe.denominator = fps;
    ioctl(fd, VIDIOC_S_PARM, &parm); /* best effort */

    /* Exposure: 0 = auto-settle-then-lock (works day/night/indoor — the
     * camera doesn't report its auto-chosen value, so the lock matches
     * brightness via a short closed loop), or a fixed manual value in 100us
     * units (daytime: 100 = 10ms). Auto mode can settle at ~50ms and cap
     * the sensor at ~20 fps; locking keeps the rate consistent. Set AFTER
     * S_FMT (stream open can reset controls) and BEFORE STREAMON. */
    if (exposure > 0) {
        set_exposure(fd, V4L2_EXPOSURE_MANUAL, exposure);
        fprintf(stderr, "exposure: fixed manual, %d (%dms)\n", exposure, exposure / 10);
    } else {
        /* This camera's auto mode is menu value 3 ("Aperture Priority"),
         * NOT the generic V4L2_EXPOSURE_AUTO (0) — that value is rejected. */
        set_exposure(fd, 3, 0);
        fprintf(stderr, "exposure: auto settle, then lock\n");
    }

    struct v4l2_requestbuffers req;
    memset(&req, 0, sizeof(req));
    req.count = n_v4l2;
    req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;
    if (ioctl(fd, VIDIOC_REQBUFS, &req) < 0) {
        fprintf(stderr, "VIDIOC_REQBUFS failed: %s\n", strerror(errno));
        goto OUT;
    }
    n_v4l2 = req.count;

    v4l2_bufs = calloc(n_v4l2, sizeof(*v4l2_bufs));
    for (RK_U32 i = 0; i < n_v4l2; i++) {
        struct v4l2_buffer buf;
        memset(&buf, 0, sizeof(buf));
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i;
        if (ioctl(fd, VIDIOC_QUERYBUF, &buf) < 0) {
            fprintf(stderr, "VIDIOC_QUERYBUF failed: %s\n", strerror(errno));
            goto OUT;
        }
        v4l2_bufs[i].length = buf.length;
        v4l2_bufs[i].start = mmap(NULL, buf.length, PROT_READ | PROT_WRITE,
                                  MAP_SHARED, fd, buf.m.offset);
        if (v4l2_bufs[i].start == MAP_FAILED) {
            fprintf(stderr, "mmap failed: %s\n", strerror(errno));
            goto OUT;
        }
    }
    for (RK_U32 i = 0; i < n_v4l2; i++) {
        struct v4l2_buffer buf;
        memset(&buf, 0, sizeof(buf));
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i;
        if (ioctl(fd, VIDIOC_QBUF, &buf) < 0) {
            fprintf(stderr, "VIDIOC_QBUF failed: %s\n", strerror(errno));
            goto OUT;
        }
    }

    enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (ioctl(fd, VIDIOC_STREAMON, &type) < 0) {
        fprintf(stderr, "VIDIOC_STREAMON failed: %s\n", strerror(errno));
        goto OUT;
    }
    stream_on = 1;

    /* MPP decoder */
    ret = mpp_create(&dec_ctx, &dec_mpi);
    if (ret) { fprintf(stderr, "mpp_create dec failed\n"); goto OUT; }

    ret = mpp_init(dec_ctx, MPP_CTX_DEC, MPP_VIDEO_CodingMJPEG);
    if (ret) { fprintf(stderr, "mpp_init dec failed\n"); goto OUT; }

    MppDecCfg dec_cfg = NULL;
    mpp_dec_cfg_init(&dec_cfg);
    ret = dec_mpi->control(dec_ctx, MPP_DEC_GET_CFG, dec_cfg);
    if (!ret)
        mpp_dec_cfg_set_u32(dec_cfg, "base:split_parse", 1);
    ret = dec_mpi->control(dec_ctx, MPP_DEC_SET_CFG, dec_cfg);
    if (ret) { fprintf(stderr, "dec set cfg failed ret %d\n", ret); goto OUT; }

    /* jpegd outputs NV12 directly */
    ret = dec_mpi->control(dec_ctx, MPP_DEC_SET_OUTPUT_FORMAT, &dec_out_fmt);
    if (ret) { fprintf(stderr, "dec set output format failed ret %d\n", ret); goto OUT; }

    /* input packet group: MJPEG payloads (one buffer reused) */
    ret = mpp_buffer_group_get_internal(&pkt_grp, MPP_BUFFER_TYPE_DRM);
    if (ret) { fprintf(stderr, "dec pkt grp failed ret %d\n", ret); goto OUT; }
    ret = mpp_buffer_group_limit_config(pkt_grp, 1024 * 1024, 4);
    if (ret) { fprintf(stderr, "dec pkt grp limit failed ret %d\n", ret); goto OUT; }

    /* output frame group: JPEG 420/422 -> 2x w*h per buffer, 4 buffers */
    ret = mpp_buffer_group_get_internal(&frm_grp, MPP_BUFFER_TYPE_DRM);
    if (ret) { fprintf(stderr, "dec frm grp failed ret %d\n", ret); goto OUT; }
    ret = mpp_buffer_group_limit_config(frm_grp, hor_stride * ver_stride * 2, 4);
    if (ret) { fprintf(stderr, "dec frm grp limit failed ret %d\n", ret); goto OUT; }

    mpp_frame_init(&out_frame);
    mpp_frame_set_width(out_frame, width);
    mpp_frame_set_height(out_frame, height);
    mpp_frame_set_hor_stride(out_frame, hor_stride);
    mpp_frame_set_ver_stride(out_frame, ver_stride);
    mpp_frame_set_fmt(out_frame, MPP_FMT_YUV420SP);
    ret = mpp_buffer_get(frm_grp, &frm_buf, hor_stride * ver_stride * 2);
    if (ret) { fprintf(stderr, "frm buffer get failed ret %d\n", ret); goto OUT; }
    mpp_frame_set_buffer(out_frame, frm_buf);

    /* MPP encoder */
    ret = mpp_create(&enc_ctx, &enc_mpi);
    if (ret) { fprintf(stderr, "mpp_create enc failed\n"); goto OUT; }

    ret = mpp_init(enc_ctx, MPP_CTX_ENC, MPP_VIDEO_CodingAVC);
    if (ret) { fprintf(stderr, "mpp_init enc failed\n"); goto OUT; }

    mpp_enc_cfg_init(&enc_cfg);
    mpp_enc_cfg_set_s32(enc_cfg, "prep:width", width);
    mpp_enc_cfg_set_s32(enc_cfg, "prep:height", height);
    mpp_enc_cfg_set_s32(enc_cfg, "prep:hor_stride", hor_stride);
    mpp_enc_cfg_set_s32(enc_cfg, "prep:ver_stride", ver_stride);
    mpp_enc_cfg_set_s32(enc_cfg, "prep:format", MPP_FMT_YUV420SP);
    mpp_enc_cfg_set_s32(enc_cfg, "codec:type", MPP_VIDEO_CodingAVC);
    mpp_enc_cfg_set_s32(enc_cfg, "rc:mode", MPP_ENC_RC_MODE_CBR);
    mpp_enc_cfg_set_s32(enc_cfg, "rc:bps_target", bps * 1000);
    mpp_enc_cfg_set_s32(enc_cfg, "rc:bps_max", (RK_S32)(bps * 1050L));
    mpp_enc_cfg_set_s32(enc_cfg, "rc:bps_min", (RK_S32)(bps * 950L));
    mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_in_flex", 0);
    mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_in_num", fps);
    mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_in_denom", 1);
    mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_out_flex", 0);
    mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_out_num", fps);
    mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_out_denom", 1);
    mpp_enc_cfg_set_s32(enc_cfg, "rc:gop", fps);
    mpp_enc_cfg_set_s32(enc_cfg, "h264:profile", 100);
    mpp_enc_cfg_set_s32(enc_cfg, "h264:level", 52); /* 4K@30fps */
    mpp_enc_cfg_set_s32(enc_cfg, "h264:cabac_en", 1);
    mpp_enc_cfg_set_s32(enc_cfg, "h264:trans8x8", 1);
    ret = enc_mpi->control(enc_ctx, MPP_ENC_SET_CFG, enc_cfg);
    if (ret) { fprintf(stderr, "enc set cfg failed ret %d\n", ret); goto OUT; }

    /* encoder output packet group */
    ret = mpp_buffer_group_get_internal(&enc_pkt_grp, MPP_BUFFER_TYPE_DRM);
    if (ret) { fprintf(stderr, "enc pkt grp failed ret %d\n", ret); goto OUT; }
    ret = mpp_buffer_group_limit_config(enc_pkt_grp, 4 * 1024 * 1024, 4);
    if (ret) { fprintf(stderr, "enc pkt grp limit failed ret %d\n", ret); goto OUT; }

    fp_out = fopen(out_path, "wb");
    if (!fp_out) {
        fprintf(stderr, "open %s failed: %s\n", out_path, strerror(errno));
        goto OUT;
    }

    /* PTS sidecar: one line per encoded frame — the V4L2 buffer's kernel
     * timestamp in microseconds (monotonic clock). Both cameras share this
     * clock, so the stitcher can pair frames exactly (the USB-camera
     * equivalent of hardware sync). Path: <name>.raw.h264 -> <name>.pts */
    {
        char pts_path[1024];
        char *dot;
        snprintf(pts_path, sizeof pts_path, "%s", out_path);
        dot = strstr(pts_path, ".raw.h264");
        if (dot)
            strcpy(dot, ".pts");
        else
            strncat(pts_path, ".pts", sizeof pts_path - strlen(pts_path) - 1);
        fp_pts = fopen(pts_path, "w");
        if (!fp_pts)
            fprintf(stderr, "open %s failed (no frame sync sidecar): %s\n",
                    pts_path, strerror(errno));
    }

    /* SPS/PPS header */
    {
        MppBuffer hdr_buf = NULL;
        MppPacket hdr = NULL;
        ret = mpp_buffer_get(enc_pkt_grp, &hdr_buf, 1024 * 1024);
        if (!ret)
            mpp_packet_init_with_buffer(&hdr, hdr_buf);
        if (hdr) {
            mpp_packet_set_length(hdr, 0);
            ret = enc_mpi->control(enc_ctx, MPP_ENC_GET_HDR_SYNC, hdr);
            if (!ret) {
                void *ptr = mpp_packet_get_pos(hdr);
                size_t len = mpp_packet_get_length(hdr);
                if (ptr && len)
                    fwrite(ptr, 1, len, fp_out);
            } else {
                fprintf(stderr, "GET_HDR_SYNC failed ret %d\n", ret);
            }
            mpp_packet_deinit(&hdr);
        }
        if (hdr_buf)
            mpp_buffer_put(hdr_buf);
    }

    /* main loop */
    fprintf(stderr, "recording %u frames to %s\n", frames, out_path);
    t_start = now_us();
    /* Wall-clock deadline: auto-exposure can run the sensor below the
     * requested fps, and the user asked for duration_sec of REAL time —
     * the frame count is only a 30 fps backstop. */
    int64_t t_deadline = wall_sec > 0 ? t_start + (int64_t)wall_sec * 1000000 : 0;

    /* auto-settle-then-lock exposure state (exposure <= 0) */
    int lock_pending = (exposure <= 0);
    int lock_phase = 0;          /* 0 = auto settle, 1 = manual refine */
    double target_luma = 0.0, luma_acc = 0.0;
    int luma_n = 0;
    int64_t prev_pts = 0;
    int64_t dt_sum = 0;
    int dt_n = 0;
    RK_S32 lock_exp = 166;       /* camera default; replaced by dt estimate */
    int refine_left = 0, refine_rounds = 0;

    /* reusable encoder output packet buffer (as in mpi_enc_test) */
    MppBuffer enc_out_buf = NULL;
    ret = mpp_buffer_get(enc_pkt_grp, &enc_out_buf, 4 * 1024 * 1024);
    if (ret) {
        fprintf(stderr, "enc out buffer get failed ret %d\n", ret);
        goto OUT;
    }

    while (g_running && (frames == 0 || count < frames) &&
           !(t_deadline && now_us() > t_deadline)) {
        struct v4l2_buffer vbuf;
        memset(&vbuf, 0, sizeof(vbuf));
        vbuf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        vbuf.memory = V4L2_MEMORY_MMAP;

        if (ioctl(fd, VIDIOC_DQBUF, &vbuf) < 0) {
            if (errno == EAGAIN) {
                usleep(1000);
                continue;
            }
            fprintf(stderr, "DQBUF failed: %s\n", strerror(errno));
            break;
        }

        int64_t pts_us = (int64_t)vbuf.timestamp.tv_sec * 1000000 +
                         vbuf.timestamp.tv_usec;
        size_t jpeg_size = vbuf.bytesused;

        /* live-preview snapshot: raw JPEG payload straight to disk */
        if (now_us() - last_snap_us >= snap_interval_us) {
            write_preview(preview_path, v4l2_bufs[vbuf.index].start, jpeg_size);
            last_snap_us = now_us();
        }

        /* copy MJPEG payload into an MppBuffer for the decoder */
        MppBuffer dec_in_buf = NULL;
        MppPacket dec_pkt = NULL;
        ret = mpp_buffer_get(pkt_grp, &dec_in_buf, jpeg_size);
        if (ret) {
            fprintf(stderr, "pkt buffer get failed ret %d\n", ret);
            goto FRAME_DONE;
        }
        memcpy(mpp_buffer_get_ptr(dec_in_buf), v4l2_bufs[vbuf.index].start, jpeg_size);

        mpp_packet_init_with_buffer(&dec_pkt, dec_in_buf);
        mpp_packet_set_pos(dec_pkt, mpp_buffer_get_ptr(dec_in_buf));
        mpp_packet_set_length(dec_pkt, jpeg_size);

        /* tell the decoder to write into our preallocated NV12 frame */
        {
            MppMeta meta = mpp_packet_get_meta(dec_pkt);
            if (meta)
                mpp_meta_set_frame(meta, KEY_OUTPUT_FRAME, out_frame);
        }

        ret = dec_mpi->decode_put_packet(dec_ctx, dec_pkt);
        mpp_packet_deinit(&dec_pkt);
        /* release our ref; the decoder queue holds its own until decode completes */
        mpp_buffer_put(dec_in_buf);
        dec_in_buf = NULL;
        if (ret) {
            fprintf(stderr, "decode_put_packet failed ret %d\n", ret);
            goto FRAME_DONE;
        }

        MppFrame dec_frame = NULL;
        ret = dec_mpi->decode_get_frame(dec_ctx, &dec_frame);
        if (ret || !dec_frame) {
            if (ret)
                fprintf(stderr, "decode_get_frame failed ret %d\n", ret);
            goto FRAME_DONE;
        }

        if (mpp_frame_get_info_change(dec_frame)) {
            fprintf(stderr, "info change w %d h %d\n",
                    mpp_frame_get_width(dec_frame), mpp_frame_get_height(dec_frame));
            ret = dec_mpi->control(dec_ctx, MPP_DEC_SET_INFO_CHANGE_READY, NULL);
            if (ret)
                fprintf(stderr, "INFO_CHANGE_READY failed ret %d\n", ret);
            mpp_frame_deinit(&dec_frame);
            goto FRAME_DONE;
        }

        if (mpp_frame_get_errinfo(dec_frame)) {
            fprintf(stderr, "decoded frame %d has errinfo\n", count);
            mpp_frame_deinit(&dec_frame);
            goto FRAME_DONE;
        }

        /* auto-settle-then-lock: match the auto exposure's brightness with a
         * manual value so the recording stays consistent after the first
         * seconds. The initial manual guess comes from the auto phase's
         * mean frame interval (a 50ms exposure delivers exactly 20fps, so
         * dt IS the exposure), then luma refinement corrects it. */
        if (lock_pending) {
            if (prev_pts > 0 && pts_us > prev_pts) {
                dt_sum += pts_us - prev_pts;
                dt_n++;
            }
            prev_pts = pts_us;
            double l = frame_luma(dec_frame);
            if (l >= 0) {
                if (lock_phase == 0) {
                    if (count >= 60) { luma_acc += l; luma_n++; }
                    if (count >= 119) {
                        target_luma = luma_n ? luma_acc / luma_n : 64.0;
                        /* This camera's manual mode disables the internal
                         * analog gain that auto mode uses to reach brightness
                         * in dim light (no V4L2 gain control exists) — so a
                         * manual lock can NEVER match auto's brightness when
                         * the scene needs gain. Lock only when auto already
                         * runs at ~30fps (exposure <= ~33ms, bright scene,
                         * gain-free); otherwise stay in auto — the pts
                         * sidecar keeps sync perfect at any frame rate. */
                        if (dt_n > 0 && (dt_sum / dt_n) > 33333) {
                            lock_pending = 0;
                            fprintf(stderr, "exposure: auto needs %.1fms (>33ms) — "
                                    "staying in auto for brightness\n",
                                    (dt_sum / dt_n) / 1000.0);
                        } else {
                            if (dt_n > 0) {
                                lock_exp = (RK_S32)((dt_sum / dt_n) / 100);
                                if (lock_exp < 30) lock_exp = 30;
                                if (lock_exp > 330) lock_exp = 330;
                            }
                            set_exposure(fd, V4L2_EXPOSURE_MANUAL, lock_exp);
                            lock_phase = 1;
                            refine_left = 30;
                            luma_acc = 0.0; luma_n = 0;
                            fprintf(stderr, "exposure: auto settled at luma %.0f "
                                    "(dt %.1fms) -> manual %d (%dms), refining...\n",
                                    target_luma, dt_n ? (dt_sum / dt_n) / 1000.0 : 0.0,
                                    lock_exp, lock_exp / 10);
                        }
                    }
                } else {
                    luma_acc += l; luma_n++;
                    if (--refine_left == 0) {
                        double meas = luma_n ? luma_acc / luma_n : target_luma;
                        double ratio = meas > 1.0 ? target_luma / meas : 1.0;
                        if (ratio < 0.8) ratio = 0.8;
                        if (ratio > 1.25) ratio = 1.25;
                        lock_exp = (RK_S32)(lock_exp * ratio + 0.5);
                        if (lock_exp < 30) lock_exp = 30;    /* 3ms */
                        if (lock_exp > 330) lock_exp = 330;  /* 33ms = 30fps */
                        set_exposure(fd, V4L2_EXPOSURE_MANUAL, lock_exp);
                        luma_acc = 0.0; luma_n = 0;
                        int converged = (ratio > 0.9 && ratio < 1.11);
                        if (++refine_rounds >= 5 || converged) {
                            lock_pending = 0;
                            fprintf(stderr, "exposure: locked at %d (%dms)\n",
                                    lock_exp, lock_exp / 10);
                        } else {
                            refine_left = 30;
                        }
                    }
                }
            }
        }

        /* forward directly to the encoder: NV12 in, no conversion */
        mpp_frame_set_pts(dec_frame, pts_us);
        {
            /* attach output packet for the encoder to fill */
            MppPacket enc_pkt = NULL;
            mpp_packet_init_with_buffer(&enc_pkt, enc_out_buf);
            mpp_packet_set_length(enc_pkt, 0);
            MppMeta fmeta = mpp_frame_get_meta(dec_frame);
            if (fmeta)
                mpp_meta_set_packet(fmeta, KEY_OUTPUT_PACKET, enc_pkt);
            mpp_packet_deinit(&enc_pkt);
        }
        ret = enc_mpi->encode_put_frame(enc_ctx, dec_frame);
        mpp_frame_deinit(&dec_frame);
        if (ret) {
            fprintf(stderr, "encode_put_frame failed ret %d\n", ret);
            goto FRAME_DONE;
        }

        /* drain encoder packets */
        for (;;) {
            MppPacket enc_pkt = NULL;
            ret = enc_mpi->encode_get_packet(enc_ctx, &enc_pkt);
            if (ret == MPP_ERR_TIMEOUT || !enc_pkt)
                break;
            if (ret) {
                fprintf(stderr, "encode_get_packet failed ret %d\n", ret);
                break;
            }

            void *ptr = mpp_packet_get_pos(enc_pkt);
            size_t len = mpp_packet_get_length(enc_pkt);
            if (ptr && len) {
                static const RK_U8 sc[4] = { 0, 0, 0, 1 };
                if (len < 4 || memcmp(ptr, sc, 4) != 0)
                    fwrite(sc, 1, 4, fp_out);
                fwrite(ptr, 1, len, fp_out);
                if (fp_pts) {
                    RK_S64 pkt_pts = mpp_packet_get_pts(enc_pkt);
                    if (pkt_pts <= 0)
                        pkt_pts = pts_us;   /* encoder didn't propagate — use capture time */
                    fprintf(fp_pts, "%lld\n", (long long)pkt_pts);
                }
            }
            mpp_packet_deinit(&enc_pkt);
        }

        count++;

FRAME_DONE:
        ioctl(fd, VIDIOC_QBUF, &vbuf);
    }

    {
        int64_t elapsed = now_us() - t_start;
        fprintf(stderr, "captured %d frames in %.2fs = %.2f fps\n",
                count, elapsed / 1000000.0, count * 1000000.0 / elapsed);
    }

OUT:
    if (fd >= 0 && stream_on) {
        enum v4l2_buf_type t = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        ioctl(fd, VIDIOC_STREAMOFF, &t);
    }
    if (enc_ctx) {
        enc_mpi->reset(enc_ctx);
        mpp_destroy(enc_ctx);
    }
    if (dec_ctx) {
        dec_mpi->reset(dec_ctx);
        mpp_destroy(dec_ctx);
    }
    if (out_frame)
        mpp_frame_deinit(&out_frame);
    if (frm_buf) {
        mpp_buffer_put(frm_buf);
        frm_buf = NULL;
    }
    if (frm_grp)
        mpp_buffer_group_put(frm_grp);
    if (pkt_grp)
        mpp_buffer_group_put(pkt_grp);
    if (enc_out_buf) {
        mpp_buffer_put(enc_out_buf);
        enc_out_buf = NULL;
    }
    if (enc_pkt_grp)
        mpp_buffer_group_put(enc_pkt_grp);
    if (enc_cfg)
        mpp_enc_cfg_deinit(enc_cfg);
    if (v4l2_bufs) {
        for (RK_U32 i = 0; i < n_v4l2; i++)
            if (v4l2_bufs[i].start)
                munmap(v4l2_bufs[i].start, v4l2_bufs[i].length);
        free(v4l2_bufs);
    }
    if (fd >= 0)
        close(fd);
    if (fp_out)
        fclose(fp_out);
    if (fp_pts)
        fclose(fp_pts);

    return ret;
}
