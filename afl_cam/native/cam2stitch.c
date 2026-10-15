/* cam2stitch — LIVE dual-camera GPU-stitched recorder (RK3588 / Mali G610 / MPP).
 *
 * Pipeline: V4L2 MJPEG (both UVC cameras) -> MPP jpegd (NV12, hardware)
 * -> Vulkan compute stitch (analytic spherical warp + blend, NV12 in/out)
 * -> MPP rkvenc H.264 -> .raw.h264 + .pts sidecar (composite pair midpoints
 * on the kernel monotonic clock).
 *
 * LIVE mode:
 *   cam2stitch <dev0> <dev1> <params_dir> <out.raw.h264>
 *              [frames] [width] [height] [fps] [exposure] [wall_sec] [shader.spv]
 *   params_dir: params.bin + w0.bin + w1.bin (dump_camera_params.py)
 *
 * HARNESS mode (S1 validation: NV12 frame pairs from files):
 *   cam2stitch -src <params_dir> <cam0.nv12> <cam1.nv12> <out.nv12>
 *               [n_iters] [shader.spv]
 *
 * GPU loop (see docs/archive/live_stitch_plan.md): a PRE-RECORDED 3-slot command-buffer
 * ring — nothing is re-recorded after init, sidestepping the libmali quirk
 * where later vkBeginCommandBuffer recordings silently discard earlier
 * ones. Each slot cb is SELF-CONTAINED (weight uploads + initial barriers +
 * frame upload + dispatch). Hard-won libmali g24p0 facts (see S1 commit):
 * 8-bit byte stores of texture-derived values crash/miscompile the LLVM
 * backend; high register pressure (4x2-pixel threads) silently miscompiles
 * — so: ONE THREAD PER CANVAS PIXEL, uint32 SSBO output (one byte per
 * uint, repacked on the CPU), no shared memory, no barriers, no
 * floatBitsToUint. Fences must be created pre-signaled.
 *
 * DAEMON mode (afl-camd field supervisor, docs/archive/iphone-control-plan.md §2.1):
 * when AFL_CAM_CTL_SOCK is set the pipeline runs CONTINUOUSLY in standby —
 * cameras open, GPU stitching, preview encoding — and recording is a
 * toggle. A unix-socket control interface accepts, one command per line:
 *   REC_START <dir>   open <dir>/stitch.raw.h264 + .pts + proxy files,
 *                     write the stored SPS/PPS header, force an IDR
 *   REC_STOP          flush + close the recording files
 *   STATUS            reply immediately with the current status line
 *   QUIT              exit cleanly
 * A JSON status line is pushed to every connected client ~1/s (fps per
 * camera, pairing dup/skip/skew, encoder stats, preview stats). The ctl
 * thread only PARSES and sets pending flags — every application action
 * (file open/close, encoder put/drain, IDR) runs on the MAIN thread.
 * While not recording the master encoder put/drain is skipped (idle VPU;
 * puts resume on REC_START with a forced IDR).
 *
 * PREVIEW encode (docs/archive/iphone-control-plan.md §4): with AFL_CAM_PREVIEW_UDP set,
 * a second rkvenc session (H.264 Constrained Baseline, ~1600x564 CBR) encodes
 * a box-downscale of the stitched canvas — done by the worker thread straight
 * from the packed SSBO. The main thread puts/drains the preview frames
 * (invariant) and fans the Annex-B packets out to (a) a UDP socket (default
 * 127.0.0.1:8890) for the ffmpeg->MediaMTX preview chain — best-effort,
 * dropped when nobody listens — and (b) <rec_dir>/preview.raw.h264 + .pts
 * while recording (the phone-playable proxy file).
 */

#include <arpa/inet.h>
#include <errno.h>
#include <execinfo.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <linux/videodev2.h>

#include "rk_mpi.h"
#include <vulkan/vulkan.h>

#define MPP_ALIGN(x, a) (((x) + (a) - 1) & ~((a) - 1))
#define RING_SLOTS 3        /* GPU command-buffer ring */
#define CAM_RING 2          /* decoded frame ring per camera (CMA budget) */
#define V4L2_BUFS 8

#define CHECK(call) do { VkResult r_ = (call); if (r_ != VK_SUCCESS) { \
    fprintf(stderr, "VK error %d at %s:%d\n", r_, __FILE__, __LINE__); exit(1); } } while (0)

static volatile int g_running = 1;

static void cma_print(const char *tag) {
    FILE *f = fopen("/proc/meminfo", "r");
    char line[256];
    int alloc = -1, free_kb = -1;
    while (f && fgets(line, sizeof line, f)) {
        if (sscanf(line, "CmaAllocated: %d", &alloc) == 1) continue;
        if (sscanf(line, "CmaFree: %d", &free_kb) == 1) continue;
    }
    if (f) fclose(f);
    fprintf(stderr, "CMA[%s] alloc=%dKB free=%dKB\n", tag, alloc, free_kb);
}

static void sig_handler(int sig)
{
    (void)sig;
    g_running = 0;
}

static void crash_handler(int sig)
{
    void *bt[32];
    int n = backtrace(bt, 32);
    backtrace_symbols_fd(bt, n, 2);
    _exit(128 + sig);
}

typedef struct { void *start; size_t length; } v4l2_buf_t;

static int64_t now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
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

static void set_ctrl(int fd, __u32 id, RK_S32 value)
{
    struct v4l2_control ctrl;
    memset(&ctrl, 0, sizeof(ctrl));
    ctrl.id = id;
    ctrl.value = value;
    if (ioctl(fd, VIDIOC_S_CTRL, &ctrl) < 0)
        fprintf(stderr, "set control 0x%x failed: %s\n", id, strerror(errno));
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
 * for the auto-settle-then-lock exposure loop (copied from cam2h264.c). */
/* mean U and V of a decoded NV12 frame (sparse sample) — for comparing the
 * cameras' colour balance */
static int frame_chroma(MppFrame frame, double *u, double *v)
{
    MppBuffer buf = mpp_frame_get_buffer(frame);
    if (!buf)
        return -1;
    RK_U8 *base = (RK_U8 *)mpp_buffer_get_ptr(buf);
    RK_U32 hs = mpp_frame_get_hor_stride(frame);
    RK_U32 vs = mpp_frame_get_ver_stride(frame);
    RK_U32 w = mpp_frame_get_width(frame);
    RK_U32 h = mpp_frame_get_height(frame);
    RK_U8 *uv = base + (size_t)hs * vs;
    RK_U64 su = 0, sv = 0;
    RK_U32 n = 0;
    for (RK_U32 yy = 0; yy < h / 2; yy += 8)
        for (RK_U32 xx = 0; xx + 1 < w; xx += 16) {
            su += uv[yy * hs + xx];
            sv += uv[yy * hs + xx + 1];
            n++;
        }
    if (!n)
        return -1;
    *u = (double)su / n;
    *v = (double)sv / n;
    return 0;
}

static double frame_luma(MppFrame frame)
{
    MppBuffer buf = mpp_frame_get_buffer(frame);
    if (!buf)
        return -1.0;
    RK_U8 *y = (RK_U8 *)mpp_buffer_get_ptr(buf);
    RK_U32 hstride = mpp_frame_get_hor_stride(frame);
    RK_U32 w = mpp_frame_get_width(frame);
    RK_U32 h = mpp_frame_get_height(frame);
    RK_U64 sum = 0;
    RK_U32 n = 0;
    for (RK_U32 yy = 0; yy < h; yy += 8)
        for (RK_U32 xx = 0; xx < w; xx += 8) {
            sum += y[yy * hstride + xx];
            n++;
        }
    return n ? (double)sum / n : -1.0;
}

static void *read_file(const char *path, size_t *out_len) {
    FILE *f = fopen(path, "rb");
    if (!f) { perror(path); exit(1); }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    void *buf = malloc(n);
    if (fread(buf, 1, n, f) != (size_t)n) { fprintf(stderr, "short read %s\n", path); exit(1); }
    fclose(f);
    *out_len = (size_t)n;
    return buf;
}

/* ============================ GPU context ============================ */

typedef struct {
    VkImage img;
    VkImageView iv;
    uint32_t w, h;
} plane_t;

typedef struct {
    plane_t planes[4];              /* 0=Y0 1=UV0 2=Y1 3=UV1 */
    VkBuffer staging;
    VkDeviceMemory staging_mem;
    void *staging_map;
    VkDeviceSize stage_off[4];
    VkBuffer ssbo;                  /* padded NV12 (one byte per uint) */
    VkDeviceMemory ssbo_mem;
    void *ssbo_map;                 /* persistent mapping (map/unmap per frame
                                     * is expensive on kbase) */
    VkCommandBuffer cb;
    VkFence fence;
    VkDescriptorSet dset;
} gpu_slot_t;

typedef struct {
    VkDevice dev;
    VkQueue queue;
    VkPhysicalDeviceMemoryProperties mprops;
    gpu_slot_t slots[RING_SLOTS];
    uint32_t canvas_w, canvas_h, src_w, src_h;
    VkDeviceSize ssbo_size;         /* padded */
    VkDeviceSize stage_size;        /* two tight NV12 frames */
    size_t tight_size;              /* cw*ch*3/2 */
    int k;                          /* next slot */
} gpu_t;

static VkDeviceMemory alloc_bind_buffer(VkDevice dev, VkPhysicalDeviceMemoryProperties *mprops,
                                        VkBuffer buf, VkMemoryPropertyFlags props) {
    VkMemoryRequirements req;
    vkGetBufferMemoryRequirements(dev, buf, &req);
    VkMemoryAllocateInfo ai = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
                                .allocationSize = req.size };
    for (uint32_t i = 0; i < mprops->memoryTypeCount; i++)
        if ((req.memoryTypeBits & (1u << i)) &&
            (mprops->memoryTypes[i].propertyFlags & props) == props) {
            ai.memoryTypeIndex = i;
            VkDeviceMemory mem;
            CHECK(vkAllocateMemory(dev, &ai, NULL, &mem));
            CHECK(vkBindBufferMemory(dev, buf, mem, 0));
            return mem;
        }
    fprintf(stderr, "no memory type for buffer (props %x)\n", props);
    exit(1);
}

static VkDeviceMemory alloc_bind_image(VkDevice dev, VkPhysicalDeviceMemoryProperties *mprops,
                                       VkImage img) {
    VkMemoryRequirements req;
    vkGetImageMemoryRequirements(dev, img, &req);
    VkMemoryAllocateInfo ai = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
                                .allocationSize = req.size };
    for (uint32_t i = 0; i < mprops->memoryTypeCount; i++)
        if ((req.memoryTypeBits & (1u << i)) &&
            (mprops->memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)) {
            ai.memoryTypeIndex = i;
            VkDeviceMemory mem;
            CHECK(vkAllocateMemory(dev, &ai, NULL, &mem));
            CHECK(vkBindImageMemory(dev, img, mem, 0));
            return mem;
        }
    fprintf(stderr, "no device-local memory type for image\n");
    exit(1);
}

static void create_plane(VkDevice dev, VkPhysicalDeviceMemoryProperties *mprops,
                         plane_t *pl, uint32_t w, uint32_t h, VkFormat fmt) {
    pl->w = w; pl->h = h;
    VkImageCreateInfo ci = { .sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
                             .imageType = VK_IMAGE_TYPE_2D,
                             .format = fmt,
                             .extent = { w, h, 1 },
                             .mipLevels = 1, .arrayLayers = 1,
                             .samples = VK_SAMPLE_COUNT_1_BIT,
                             .tiling = VK_IMAGE_TILING_OPTIMAL,
                             .usage = VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_SAMPLED_BIT,
                             .sharingMode = VK_SHARING_MODE_EXCLUSIVE,
                             .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED };
    CHECK(vkCreateImage(dev, &ci, NULL, &pl->img));
    alloc_bind_image(dev, mprops, pl->img);
    VkImageViewCreateInfo ivci = { .sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO,
                                   .viewType = VK_IMAGE_VIEW_TYPE_2D,
                                   .format = fmt,
                                   .image = pl->img,
                                   .subresourceRange = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 } };
    CHECK(vkCreateImageView(dev, &ivci, NULL, &pl->iv));
}

static void gpu_init(gpu_t *g, const char *spv_path,
                     void *params_data, void *w0d, void *w1d,
                     void *m0d, void *m1d) {
    size_t len;
    float *pf = params_data;
    g->canvas_w = (uint32_t)pf[0]; g->canvas_h = (uint32_t)pf[1];
    g->src_w = (uint32_t)pf[2]; g->src_h = (uint32_t)pf[3];
    if (!(g->canvas_w % 2 == 0 && g->canvas_h % 2 == 0) || g->canvas_w % 4 != 0) {
        fprintf(stderr, "canvas %ux%u must be even, width %% 4 == 0\n",
                g->canvas_w, g->canvas_h);
        exit(1);
    }
    g->ssbo_size = (VkDeviceSize)g->canvas_w * g->canvas_h * 2;  /* 4 Y bytes or 1 UV block per uint: cw*ch/2 uints */
    g->stage_size = (VkDeviceSize)g->src_w * g->src_h * 3;       /* two tight NV12 frames */
    g->tight_size = (size_t)g->canvas_w * g->canvas_h * 3 / 2;

    /* ---- instance + device ---- */
    VkApplicationInfo app = { .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
                              .pApplicationName = "cam2stitch", .apiVersion = VK_API_VERSION_1_3 };
    VkInstanceCreateInfo ici = { .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app };
    VkInstance inst;
    CHECK(vkCreateInstance(&ici, NULL, &inst));
    uint32_t ndev = 0;
    vkEnumeratePhysicalDevices(inst, &ndev, NULL);
    VkPhysicalDevice *devs = calloc(ndev, sizeof *devs);
    vkEnumeratePhysicalDevices(inst, &ndev, devs);
    VkPhysicalDevice phys = VK_NULL_HANDLE;
    for (uint32_t i = 0; i < ndev; i++) {
        VkPhysicalDeviceProperties props;
        vkGetPhysicalDeviceProperties(devs[i], &props);
        fprintf(stderr, "device[%u]: %s\n", i, props.deviceName);
        if (strstr(props.deviceName, "Mali")) phys = devs[i];
    }
    if (!phys) { fprintf(stderr, "no Mali GPU found\n"); exit(1); }
    uint32_t nq = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(phys, &nq, NULL);
    VkQueueFamilyProperties *qfps = calloc(nq, sizeof *qfps);
    vkGetPhysicalDeviceQueueFamilyProperties(phys, &nq, qfps);
    uint32_t qf = 0;
    for (uint32_t i = 0; i < nq; i++)
        if (qfps[i].queueFlags & VK_QUEUE_COMPUTE_BIT) { qf = i; break; }
    float prio = 1.0f;
    VkDeviceQueueCreateInfo qci = { .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
                                    .queueFamilyIndex = qf, .queueCount = 1, .pQueuePriorities = &prio };
    VkDeviceCreateInfo dci = { .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
                               .queueCreateInfoCount = 1, .pQueueCreateInfos = &qci };
    CHECK(vkCreateDevice(phys, &dci, NULL, &g->dev));
    vkGetDeviceQueue(g->dev, qf, 0, &g->queue);
    vkGetPhysicalDeviceMemoryProperties(phys, &g->mprops);

    /* ---- samplers: linear+black-border for frames, nearest for weights ---- */
    VkSamplerCreateInfo sci = { .sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO,
                                .magFilter = VK_FILTER_NEAREST, .minFilter = VK_FILTER_NEAREST,
                                .addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
                                .addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
                                .maxLod = 0.0f };
    VkSampler nearest;
    CHECK(vkCreateSampler(g->dev, &sci, NULL, &nearest));
    sci.magFilter = VK_FILTER_LINEAR; sci.minFilter = VK_FILTER_LINEAR;
    sci.addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER;
    sci.addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER;
    sci.borderColor = VK_BORDER_COLOR_FLOAT_TRANSPARENT_BLACK;
    VkSampler linear;
    CHECK(vkCreateSampler(g->dev, &sci, NULL, &linear));

    /* ---- descriptor layout: 4 frame planes + 2 weights + 2 warp maps
     * + ssbo + params ---- */
    VkDescriptorPoolSize dpss[2] = {
        { VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 8 * RING_SLOTS + 2 },
        { VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 2 * RING_SLOTS + 1 },
    };
    VkDescriptorPoolCreateInfo dpci = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,
                                        .maxSets = RING_SLOTS,
                                        .poolSizeCount = 2, .pPoolSizes = dpss };
    VkDescriptorPool dpool;
    CHECK(vkCreateDescriptorPool(g->dev, &dpci, NULL, &dpool));
    VkDescriptorSetLayoutBinding binds[10] = {0};
    for (int i = 0; i < 8; i++)
        binds[i] = (VkDescriptorSetLayoutBinding){
            i, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
    binds[8] = (VkDescriptorSetLayoutBinding){
        8, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
    binds[9] = (VkDescriptorSetLayoutBinding){
        9, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
    VkDescriptorSetLayoutCreateInfo dsci = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
                                             .bindingCount = 10, .pBindings = binds };
    VkDescriptorSetLayout dsl;
    CHECK(vkCreateDescriptorSetLayout(g->dev, &dsci, NULL, &dsl));

    void *spv = read_file(spv_path, &len);
    VkShaderModuleCreateInfo smci = { .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
                                      .codeSize = len, .pCode = spv };
    VkShaderModule sm;
    CHECK(vkCreateShaderModule(g->dev, &smci, NULL, &sm));
    VkPipelineLayoutCreateInfo plci = { .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
                                        .setLayoutCount = 1, .pSetLayouts = &dsl };
    VkPipelineLayout layout;
    CHECK(vkCreatePipelineLayout(g->dev, &plci, NULL, &layout));
    VkComputePipelineCreateInfo cpci = { .sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
                                         .stage = { .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
                                                    .stage = VK_SHADER_STAGE_COMPUTE_BIT,
                                                    .module = sm, .pName = "main" },
                                         .layout = layout };
    VkPipeline pipeline;
    CHECK(vkCreateComputePipelines(g->dev, VK_NULL_HANDLE, 1, &cpci, NULL, &pipeline));

    /* ---- static weights + warp maps (images + staging) + params SSBO ---- */
    plane_t wplanes[2];
    create_plane(g->dev, &g->mprops, &wplanes[0], g->canvas_w, g->canvas_h, VK_FORMAT_R8G8B8A8_UNORM);
    create_plane(g->dev, &g->mprops, &wplanes[1], g->canvas_w, g->canvas_h, VK_FORMAT_R8G8B8A8_UNORM);
    VkBufferCreateInfo wst_bci = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                                   .size = (VkDeviceSize)g->canvas_w * g->canvas_h * 4,
                                   .usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT };
    VkBuffer wst0, wst1;
    CHECK(vkCreateBuffer(g->dev, &wst_bci, NULL, &wst0));
    CHECK(vkCreateBuffer(g->dev, &wst_bci, NULL, &wst1));
    VkDeviceMemory wst0m = alloc_bind_buffer(g->dev, &g->mprops, wst0,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    VkDeviceMemory wst1m = alloc_bind_buffer(g->dev, &g->mprops, wst1,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    void *wmap;
    CHECK(vkMapMemory(g->dev, wst0m, 0, wst_bci.size, 0, &wmap));
    memcpy(wmap, w0d, wst_bci.size);
    vkUnmapMemory(g->dev, wst0m);
    CHECK(vkMapMemory(g->dev, wst1m, 0, wst_bci.size, 0, &wmap));
    memcpy(wmap, w1d, wst_bci.size);
    vkUnmapMemory(g->dev, wst1m);

    /* warp maps: ROI-sized R32G32F (s,t) textures, dims from params mapwh */
    plane_t mplanes[2];
    uint32_t map_w[2], map_h[2];
    for (int c = 0; c < 2; c++) {
        int base = 8 + c * 28;
        map_w[c] = (uint32_t)pf[base + 13];
        map_h[c] = (uint32_t)pf[base + 14];
        if (!map_w[c] || !map_h[c]) { fprintf(stderr, "bad map dims cam%d\n", c); exit(1); }
        create_plane(g->dev, &g->mprops, &mplanes[c], map_w[c], map_h[c],
                     VK_FORMAT_R32G32_SFLOAT);
    }
    VkBuffer mst[2];
    VkDeviceMemory mstm[2];
    const void *md[2] = { m0d, m1d };
    for (int c = 0; c < 2; c++) {
        VkBufferCreateInfo mbci = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                                    .size = (VkDeviceSize)map_w[c] * map_h[c] * 8,
                                    .usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT };
        CHECK(vkCreateBuffer(g->dev, &mbci, NULL, &mst[c]));
        mstm[c] = alloc_bind_buffer(g->dev, &g->mprops, mst[c],
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
        void *mmap;
        CHECK(vkMapMemory(g->dev, mstm[c], 0, mbci.size, 0, &mmap));
        memcpy(mmap, md[c], mbci.size);
        vkUnmapMemory(g->dev, mstm[c]);
    }

    VkBufferCreateInfo pbci = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                                .size = 64 * sizeof(float),
                                .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT };
    VkBuffer pbuf;
    CHECK(vkCreateBuffer(g->dev, &pbci, NULL, &pbuf));
    VkDeviceMemory pbmem = alloc_bind_buffer(g->dev, &g->mprops, pbuf,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    void *pmap;
    CHECK(vkMapMemory(g->dev, pbmem, 0, pbci.size, 0, &pmap));
    memcpy(pmap, params_data, pbci.size);
    vkUnmapMemory(g->dev, pbmem);

    /* ---- command pool + per-slot resources + pre-recorded cbs ---- */
    VkCommandPoolCreateInfo cpc = { .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
                                    .queueFamilyIndex = qf };
    VkCommandPool pool;
    CHECK(vkCreateCommandPool(g->dev, &cpc, NULL, &pool));
    VkCommandBufferBeginInfo bi = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
                                    .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT };
    VkBufferImageCopy wc = { .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 },
                             .imageExtent = { g->canvas_w, g->canvas_h, 1 } };

    /* one-time setup cb: weight + map uploads run ONCE, not per frame.
     * (Separate cb, recorded and submitted once — the libmali quirk was
     * re-recording a cb discarding its earlier content.) */
    {
        VkCommandBufferAllocateInfo cbi = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
                                            .commandPool = pool,
                                            .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
                                            .commandBufferCount = 1 };
        VkCommandBuffer setup_cb;
        CHECK(vkAllocateCommandBuffers(g->dev, &cbi, &setup_cb));
        CHECK(vkBeginCommandBuffer(setup_cb, &bi));
        VkImageMemoryBarrier wb4[4];
        VkImage images4[4] = { wplanes[0].img, wplanes[1].img,
                               mplanes[0].img, mplanes[1].img };
        for (int i = 0; i < 4; i++)
            wb4[i] = (VkImageMemoryBarrier){
                .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
                .srcAccessMask = 0, .dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT,
                .oldLayout = VK_IMAGE_LAYOUT_UNDEFINED, .newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                .image = images4[i], .subresourceRange = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 } };
        vkCmdPipelineBarrier(setup_cb, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                             0, 0, NULL, 0, NULL, 4, wb4);
        vkCmdCopyBufferToImage(setup_cb, wst0, wplanes[0].img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &wc);
        vkCmdCopyBufferToImage(setup_cb, wst1, wplanes[1].img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &wc);
        VkBufferImageCopy mc0 = { .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 },
                                  .imageExtent = { map_w[0], map_h[0], 1 } };
        VkBufferImageCopy mc1 = { .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 },
                                  .imageExtent = { map_w[1], map_h[1], 1 } };
        vkCmdCopyBufferToImage(setup_cb, mst[0], mplanes[0].img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &mc0);
        vkCmdCopyBufferToImage(setup_cb, mst[1], mplanes[1].img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &mc1);
        for (int i = 0; i < 4; i++) {
            wb4[i].srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
            wb4[i].dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
            wb4[i].oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
            wb4[i].newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        }
        vkCmdPipelineBarrier(setup_cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                             0, 0, NULL, 0, NULL, 4, wb4);
        CHECK(vkEndCommandBuffer(setup_cb));
        VkFenceCreateInfo fci = { .sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO,
                                  .flags = VK_FENCE_CREATE_SIGNALED_BIT };
        VkFence setup_fence;
        CHECK(vkCreateFence(g->dev, &fci, NULL, &setup_fence));
        CHECK(vkResetFences(g->dev, 1, &setup_fence));
        VkSubmitInfo si = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
                            .commandBufferCount = 1, .pCommandBuffers = &setup_cb };
        CHECK(vkQueueSubmit(g->queue, 1, &si, setup_fence));
        CHECK(vkWaitForFences(g->dev, 1, &setup_fence, VK_TRUE, UINT64_MAX));
    }

    for (int k = 0; k < RING_SLOTS; k++) {
        gpu_slot_t *s = &g->slots[k];
        create_plane(g->dev, &g->mprops, &s->planes[0], g->src_w, g->src_h, VK_FORMAT_R8_UNORM);
        create_plane(g->dev, &g->mprops, &s->planes[1], g->src_w / 2, g->src_h / 2, VK_FORMAT_R8G8_UNORM);
        create_plane(g->dev, &g->mprops, &s->planes[2], g->src_w, g->src_h, VK_FORMAT_R8_UNORM);
        create_plane(g->dev, &g->mprops, &s->planes[3], g->src_w / 2, g->src_h / 2, VK_FORMAT_R8G8_UNORM);

        VkBufferCreateInfo sbci = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                                    .size = g->stage_size, .usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT };
        CHECK(vkCreateBuffer(g->dev, &sbci, NULL, &s->staging));
        /* HOST_CACHED: uncached writes to the staging buffer run at ~1.1GB/s
         * (23 ms for the 24.8 MB frame pair) — cached writes + a per-frame
         * flush cut that to a few ms */
        s->staging_mem = alloc_bind_buffer(g->dev, &g->mprops, s->staging,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_CACHED_BIT);
        CHECK(vkMapMemory(g->dev, s->staging_mem, 0, g->stage_size, 0, &s->staging_map));
        s->stage_off[0] = 0;
        s->stage_off[1] = (VkDeviceSize)g->src_w * g->src_h;
        s->stage_off[2] = s->stage_off[1] + (VkDeviceSize)g->src_w * g->src_h / 2;
        s->stage_off[3] = s->stage_off[2] + (VkDeviceSize)g->src_w * g->src_h;

        VkBufferCreateInfo obci = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                                    .size = g->ssbo_size, .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT };
        CHECK(vkCreateBuffer(g->dev, &obci, NULL, &s->ssbo));
        /* HOST_CACHED memory type: the padded SSBO readback is ~34MB/frame
         * and kbase's non-cached host reads run at only ~270MB/s */
        s->ssbo_mem = alloc_bind_buffer(g->dev, &g->mprops, s->ssbo,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_CACHED_BIT);
        CHECK(vkMapMemory(g->dev, s->ssbo_mem, 0, g->ssbo_size, 0, &s->ssbo_map));

        VkCommandBufferAllocateInfo cbi = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
                                            .commandPool = pool,
                                            .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
                                            .commandBufferCount = 1 };
        CHECK(vkAllocateCommandBuffers(g->dev, &cbi, &s->cb));
        /* pre-signaled: the first WaitForFences per slot must pass */
        VkFenceCreateInfo fci = { .sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO,
                                  .flags = VK_FENCE_CREATE_SIGNALED_BIT };
        CHECK(vkCreateFence(g->dev, &fci, NULL, &s->fence));

        VkDescriptorSetAllocateInfo dsai = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
                                             .descriptorPool = dpool, .descriptorSetCount = 1,
                                             .pSetLayouts = &dsl };
        CHECK(vkAllocateDescriptorSets(g->dev, &dsai, &s->dset));

        VkSampler smps[8] = { linear, linear, linear, linear, nearest, nearest,
                              nearest, nearest };
        VkDescriptorImageInfo dii[8];
        for (int i = 0; i < 4; i++)
            dii[i] = (VkDescriptorImageInfo){ smps[i], s->planes[i].iv,
                                              VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
        dii[4] = (VkDescriptorImageInfo){ smps[4], wplanes[0].iv,
                                          VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
        dii[5] = (VkDescriptorImageInfo){ smps[5], wplanes[1].iv,
                                          VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
        dii[6] = (VkDescriptorImageInfo){ smps[6], mplanes[0].iv,
                                          VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
        dii[7] = (VkDescriptorImageInfo){ smps[7], mplanes[1].iv,
                                          VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
        VkDescriptorBufferInfo obi = { s->ssbo, 0, g->ssbo_size };
        VkDescriptorBufferInfo pbi = { pbuf, 0, pbci.size };
        VkWriteDescriptorSet wds[10] = {0};
        for (int i = 0; i < 8; i++)
            wds[i] = (VkWriteDescriptorSet){ .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
                                             .dstSet = s->dset, .dstBinding = i, .descriptorCount = 1,
                                             .descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,
                                             .pImageInfo = &dii[i] };
        wds[8] = (VkWriteDescriptorSet){ .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
                                         .dstSet = s->dset, .dstBinding = 8, .descriptorCount = 1,
                                         .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
                                         .pBufferInfo = &obi };
        wds[9] = (VkWriteDescriptorSet){ .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
                                         .dstSet = s->dset, .dstBinding = 9, .descriptorCount = 1,
                                         .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
                                         .pBufferInfo = &pbi };
        vkUpdateDescriptorSets(g->dev, 10, wds, 0, NULL);

        /* ---- pre-record the slot cb (never re-recorded): frame plane
         * transitions + frame upload + dispatch. Weights/maps are uploaded
         * once by the setup cb. ---- */
        CHECK(vkBeginCommandBuffer(s->cb, &bi));
        for (int i = 0; i < 4; i++) {
            VkImageMemoryBarrier fb = {
                .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
                .srcAccessMask = 0, .dstAccessMask = VK_ACCESS_SHADER_READ_BIT,
                .oldLayout = VK_IMAGE_LAYOUT_UNDEFINED, .newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                .image = s->planes[i].img,
                .subresourceRange = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 } };
            vkCmdPipelineBarrier(s->cb, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                                 0, 0, NULL, 0, NULL, 1, &fb);
        }
        VkImageMemoryBarrier bars[4];
        for (int i = 0; i < 4; i++)
            bars[i] = (VkImageMemoryBarrier){
                .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
                .srcAccessMask = VK_ACCESS_SHADER_READ_BIT,
                .dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT,
                .oldLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                .newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                .image = s->planes[i].img,
                .subresourceRange = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 } };
        vkCmdPipelineBarrier(s->cb, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                             0, 0, NULL, 0, NULL, 4, bars);
        VkBufferImageCopy bics[4];
        for (int i = 0; i < 4; i++)
            bics[i] = (VkBufferImageCopy){
                .bufferOffset = s->stage_off[i],
                .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 },
                .imageExtent = { s->planes[i].w, s->planes[i].h, 1 } };
        for (int i = 0; i < 4; i++)
            vkCmdCopyBufferToImage(s->cb, s->staging, s->planes[i].img,
                                   VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &bics[i]);
        for (int i = 0; i < 4; i++) {
            bars[i].srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
            bars[i].dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
            bars[i].oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
            bars[i].newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        }
        vkCmdPipelineBarrier(s->cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                             0, 0, NULL, 0, NULL, 4, bars);
        vkCmdBindPipeline(s->cb, VK_PIPELINE_BIND_POINT_COMPUTE, pipeline);
        vkCmdBindDescriptorSets(s->cb, VK_PIPELINE_BIND_POINT_COMPUTE, layout, 0, 1, &s->dset, 0, NULL);
        /* one thread per 4-pixel Y group (packed SSBO) */
        vkCmdDispatch(s->cb, (g->canvas_w / 4 + 15) / 16, (g->canvas_h + 15) / 16, 1);
        CHECK(vkEndCommandBuffer(s->cb));
    }
    g->k = 0;
    fprintf(stderr, "gpu: %u x %u canvas, %d pre-recorded cbs\n",
            g->canvas_w, g->canvas_h, RING_SLOTS);
}

/* Upload thread: copies a frame pair into the slot's staging buffer, flushes
 * it and submits the GPU job. Pairing and encoding stay on the main thread,
 * so the 25 MB copy overlaps them instead of adding to the frame period.
 * Only this thread touches the queue after setup. The main thread must call
 * upload_wait() before it releases or advances past the source buffers. */
typedef struct {
    gpu_t *g;
    pthread_mutex_t mu;
    pthread_cond_t cv;
    int has_job, done, stop;
    int k;
    void *src0, *src1;
    MppBuffer buf0, buf1;
    volatile int64_t last_us;     /* duration of the last job (stats) */
    /* helper thread: copies the cam1 frame while the upload thread copies
     * cam0 (one core moves ~1 GB/s here, so the pair takes half as long) */
    pthread_mutex_t mu2;
    pthread_cond_t cv2;
    int has_job2, done2, stop2;
} uploader_t;

static void *upload_helper(void *arg) {
    uploader_t *u = arg;
    gpu_t *g = u->g;
    pthread_mutex_lock(&u->mu2);
    for (;;) {
        while (!u->has_job2 && !u->stop2)
            pthread_cond_wait(&u->cv2, &u->mu2);
        if (!u->has_job2)
            break;
        void *s1 = u->src1;
        MppBuffer b1 = u->buf1;
        gpu_slot_t *s = &g->slots[u->k];
        pthread_mutex_unlock(&u->mu2);
        mpp_buffer_sync_ro_begin(b1);
        size_t nv12 = (size_t)g->src_w * g->src_h * 3 / 2;
        memcpy((char *)s->staging_map + s->stage_off[2], s1, nv12);
        pthread_mutex_lock(&u->mu2);
        u->has_job2 = 0;
        u->done2 = 1;
        pthread_cond_broadcast(&u->cv2);
    }
    pthread_mutex_unlock(&u->mu2);
    return NULL;
}

static void *upload_thread(void *arg) {
    uploader_t *u = arg;
    gpu_t *g = u->g;
    pthread_mutex_lock(&u->mu);
    for (;;) {
        while (!u->has_job && !u->stop)
            pthread_cond_wait(&u->cv, &u->mu);
        if (!u->has_job)
            break;
        int k = u->k;
        void *s0 = u->src0, *s1 = u->src1;
        MppBuffer b0 = u->buf0, b1 = u->buf1;
        pthread_mutex_unlock(&u->mu);

        int64_t t0 = now_us();
        gpu_slot_t *s = &g->slots[k];
        (void)b1; (void)s1;
        /* cam1 on the helper thread, cam0 here, then join */
        pthread_mutex_lock(&u->mu2);
        u->done2 = 0;
        u->has_job2 = 1;
        pthread_cond_signal(&u->cv2);
        pthread_mutex_unlock(&u->mu2);
        /* DMA-written memory via a cached alias: invalidate before reads */
        mpp_buffer_sync_ro_begin(b0);
        size_t nv12 = (size_t)g->src_w * g->src_h * 3 / 2;
        memcpy((char *)s->staging_map + s->stage_off[0], s0, nv12);
        pthread_mutex_lock(&u->mu2);
        while (!u->done2)
            pthread_cond_wait(&u->cv2, &u->mu2);
        pthread_mutex_unlock(&u->mu2);
        VkMappedMemoryRange mr = { .sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
                                   .memory = s->staging_mem, .offset = 0, .size = g->stage_size };
        vkFlushMappedMemoryRanges(g->dev, 1, &mr);
        VkSubmitInfo si = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
                            .commandBufferCount = 1, .pCommandBuffers = &s->cb };
        CHECK(vkQueueSubmit(g->queue, 1, &si, s->fence));
        u->last_us = now_us() - t0;

        pthread_mutex_lock(&u->mu);
        u->has_job = 0;
        u->done = 1;
        pthread_cond_broadcast(&u->cv);
    }
    pthread_mutex_unlock(&u->mu);
    return NULL;
}

/* Stitch one frame pair (tight NV12 in) → tight NV12 out (repacked).
 * Blocking: waits the slot's fence before reuse and after submission. */
static int64_t g_times[5];   /* wait-before, memcpy, submit, wait-after, repack */
static void gpu_stitch(gpu_t *g, const void *nv0, const void *nv1, void *tight_out) {
    int64_t t0 = now_us();
    gpu_slot_t *s = &g->slots[g->k];
    CHECK(vkWaitForFences(g->dev, 1, &s->fence, VK_TRUE, UINT64_MAX));
    int64_t t1 = now_us();
    CHECK(vkResetFences(g->dev, 1, &s->fence));
    size_t nv12 = (size_t)g->src_w * g->src_h * 3 / 2;
    memcpy((char *)s->staging_map + s->stage_off[0], nv0, nv12);
    memcpy((char *)s->staging_map + s->stage_off[2], nv1, nv12);
    /* cached staging: write back before the GPU transfer reads it */
    {
        VkMappedMemoryRange mr = { .sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
                                   .memory = s->staging_mem, .offset = 0, .size = g->stage_size };
        vkFlushMappedMemoryRanges(g->dev, 1, &mr);
    }
    int64_t t2 = now_us();
    VkSubmitInfo si = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
                        .commandBufferCount = 1, .pCommandBuffers = &s->cb };
    CHECK(vkQueueSubmit(g->queue, 1, &si, s->fence));
    int64_t t3 = now_us();
    g->k = (g->k + 1) % RING_SLOTS;
    CHECK(vkWaitForFences(g->dev, 1, &s->fence, VK_TRUE, UINT64_MAX));
    int64_t t4 = now_us();
    /* cached memory: invalidate before the CPU reads the GPU's writes */
    {
        VkMappedMemoryRange mr = { .sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
                                   .memory = s->ssbo_mem, .offset = 0, .size = g->ssbo_size };
        vkInvalidateMappedMemoryRanges(g->dev, 1, &mr);
    }
    g_times[0] = t1 - t0; g_times[1] = t2 - t1; g_times[2] = t3 - t2;
    g_times[3] = t4 - t3;

    /* repack: SSBO packs 4 Y bytes per uint32 (LSB..MSB = x..x+3) and one
     * (u | v<<8) uint per chroma block — unpack to tight NV12 */
    uint32_t *o32 = s->ssbo_map;
    uint8_t *out = tight_out;
    size_t cw4 = (size_t)g->canvas_w / 4;
    for (size_t row = 0; row < (size_t)g->canvas_h; row++) {
        uint8_t *orow = out + row * (size_t)g->canvas_w;
        const uint32_t *irow = o32 + row * cw4;
        for (size_t x = 0; x < cw4; x++) {
            uint32_t pk = irow[x];
            orow[4 * x + 0] = (uint8_t)(pk & 0xffu);
            orow[4 * x + 1] = (uint8_t)((pk >> 8) & 0xffu);
            orow[4 * x + 2] = (uint8_t)((pk >> 16) & 0xffu);
            orow[4 * x + 3] = (uint8_t)((pk >> 24) & 0xffu);
        }
    }
    size_t uv_uints = (size_t)g->canvas_w / 2 * ((size_t)g->canvas_h / 2);
    uint8_t *uv = out + (size_t)g->canvas_w * g->canvas_h;
    for (size_t i = 0; i < uv_uints; i++) {
        uint32_t pk = o32[(size_t)g->canvas_h * cw4 + i];
        uv[2 * i + 0] = (uint8_t)(pk & 0xffu);
        uv[2 * i + 1] = (uint8_t)((pk >> 8) & 0xffu);
    }
    g_times[4] = now_us() - t4;
}

/* ================== preview encode (daemon mode) ================== */

/* Second rkvenc session on a ~1600x564 box-downscale of the stitched
 * canvas (docs/archive/iphone-control-plan.md §4): H.264 Constrained Baseline, CBR
 * ~3.5 Mbps, GOP 1s — decodes everywhere (Safari WHEP/HLS, Android).
 * The worker downscales the packed SSBO into a tight staging buffer
 * (drop-on-busy); the MAIN thread copies it into the encoder input and
 * put/drains (encoder invariant), fanning packets to UDP + proxy files. */
typedef struct {
    MppCtx ctx;
    MppApi *mpi;
    MppBufferGroup pkt_grp;
    MppBuffer out_buf[4];
    MppFrame in_frame;
    MppBuffer in_buf;
    RK_U32 w, h, hs, vs;
    /* worker -> main handoff (tight staging NV12) */
    pthread_mutex_t mu;
    int fresh;
    uint8_t *stage;             /* tight pv->w * pv->h * 3/2 */
    /* outputs (main thread only) */
    FILE *fp_out, *fp_pts;      /* recording proxy files, NULL when idle */
    void *hdr_data;             /* SPS/PPS (Annex-B), re-sent before IDRs */
    size_t hdr_len;
    MppBufferGroup hdr_grp;     /* own group for header captures (avoids
                                   stealing packet-group buffers mid-stream) */
    int udp_fd;
    struct sockaddr_in udp_addr;
    int udp_ok;
    RK_U64 pv_packets, pv_bytes, pv_drops;
    RK_U64 pv_hdr_sent;         /* SPS/PPS datagrams injected before IDRs */
    int ok;
} pv_t;

/* Capture the preview encoder's SPS/PPS once. rkvenc only returns a
 * header via GET_HDR_SYNC AFTER a frame has been encoded, so an
 * init-time capture can come back empty — callers retry at the first
 * IDR. Main thread only (MPP control calls). */
static void pv_capture_hdr(pv_t *pv)
{
    if (pv->hdr_data || !pv->hdr_grp || getenv("AFL_CAM_PV_NOHDR"))
        return;
    MppBuffer hdr_buf = NULL;
    MppPacket hdr = NULL;
    if (!mpp_buffer_get(pv->hdr_grp, &hdr_buf, 1024 * 1024)
        && !mpp_packet_init_with_buffer(&hdr, hdr_buf)) {
        mpp_packet_set_length(hdr, 0);
        if (!pv->mpi->control(pv->ctx, MPP_ENC_GET_HDR_SYNC, hdr)) {
            void *ptr = mpp_packet_get_pos(hdr);
            size_t len = mpp_packet_get_length(hdr);
            if (ptr && len) {
                pv->hdr_data = malloc(len);
                if (pv->hdr_data) {
                    memcpy(pv->hdr_data, ptr, len);
                    pv->hdr_len = len;
                    fprintf(stderr, "preview: SPS/PPS captured (%zu bytes)%s\n",
                            len, pv->pv_packets ? " on retry" : "");
                }
            }
        }
        mpp_packet_deinit(&hdr);
    }
    if (hdr_buf)
        mpp_buffer_put(hdr_buf);
}

static int pv_init(pv_t *pv, int w, int h, int fps, int bitrate_kbps) {
    memset(pv, 0, sizeof *pv);
    pv->w = w; pv->h = h;
    pv->hs = MPP_ALIGN(w, 16);
    pv->vs = MPP_ALIGN(h, 16);
    pv->udp_fd = -1;
    pthread_mutex_init(&pv->mu, NULL);
    pv->stage = malloc((size_t)w * h * 3 / 2);
    if (!pv->stage)
        return -1;
    if (mpp_create(&pv->ctx, &pv->mpi))
        return -1;
    if (mpp_init(pv->ctx, MPP_CTX_ENC, MPP_VIDEO_CodingAVC))
        return -1;
    MppEncCfg cfg = NULL;
    mpp_enc_cfg_init(&cfg);
    pv->mpi->control(pv->ctx, MPP_ENC_GET_CFG, cfg);
    RK_S32 vals[8] = { (RK_S32)pv->w, (RK_S32)pv->h, (RK_S32)pv->hs,
                       (RK_S32)pv->vs, MPP_FMT_YUV420SP,
                       bitrate_kbps * 1000, (RK_S32)(bitrate_kbps * 950L),
                       (RK_S32)(bitrate_kbps * 1050L) };
    const char *keys[8] = { "prep:width", "prep:height", "prep:hor_stride",
                            "prep:ver_stride", "prep:format", "rc:bps_target",
                            "rc:bps_min", "rc:bps_max" };
    for (int i = 0; i < 8; i++)
        mpp_enc_cfg_set_s32(cfg, keys[i], vals[i]);
    mpp_enc_cfg_set_s32(cfg, "codec:type", MPP_VIDEO_CodingAVC);
    mpp_enc_cfg_set_s32(cfg, "rc:mode", MPP_ENC_RC_MODE_CBR);
    mpp_enc_cfg_set_s32(cfg, "rc:gop", fps);          /* 1 s — fast joins */
    mpp_enc_cfg_set_s32(cfg, "rc:fps_in_flex", 0);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_in_num", fps);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_in_denorm", 1);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_out_flex", 0);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_out_num", fps);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_out_denorm", 1);
    /* Constrained Baseline: no CABAC, no 8x8 — WebRTC-grade compatibility */
    mpp_enc_cfg_set_s32(cfg, "h264:profile", 66);
    mpp_enc_cfg_set_s32(cfg, "h264:level", 42);
    mpp_enc_cfg_set_s32(cfg, "h264:cabac_en", 0);
    mpp_enc_cfg_set_s32(cfg, "h264:trans8x8", 0);
    /* (prep:header_mode EACH_IDR is NOT set: the cfg write fails with
     * ret -1 on this encoder — rkvenc never emits parameter sets anyway,
     * the drain injects the captured SPS/PPS before IDRs instead) */
    if (pv->mpi->control(pv->ctx, MPP_ENC_SET_CFG, cfg))
        return -1;
    /* packet group EXACTLY as the proven cam2h264 recorder: 4MB buffers,
     * limit 4, ONE held output buffer (the 1MBx8 + 4-held arrangement
     * made rkvenc's H.264 path double-release packets every frame —
     * "non-positive ref_count" spam, 400MB log, wedged pipeline) */
    if (mpp_buffer_group_get_internal(&pv->pkt_grp, MPP_BUFFER_TYPE_DRM))
        return -1;
    if (mpp_buffer_group_limit_config(pv->pkt_grp, 4 * 1024 * 1024, 4))
        return -1;
    if (mpp_buffer_get(pv->pkt_grp, &pv->out_buf[0], 4 * 1024 * 1024))
        return -1;
    /* SPS/PPS header — rkvenc does NOT honour prep:header_mode EACH_IDR
     * (measured: IDRs carry SEI but no parameter sets), so capture the
     * header and re-send it before every IDR on the UDP path + write it
     * into the proxy file on REC_START. Init-time capture is a coin flip
     * (GET_HDR_SYNC is empty until the first frame is encoded — broken
     * previews where IDRs went out with no parameter sets, 2026-10-12),
     * so the send path retries at the first IDR. */
    if (mpp_buffer_group_get_internal(&pv->hdr_grp, MPP_BUFFER_TYPE_DRM))
        return -1;
    if (mpp_buffer_group_limit_config(pv->hdr_grp, 1024 * 1024, 1))
        return -1;
    pv_capture_hdr(pv);
    if (!pv->hdr_data)
        fprintf(stderr, "preview: no SPS/PPS at init — retrying at first IDR\n");
    else if (getenv("AFL_CAM_PV_DEBUG"))
        fprintf(stderr, "preview: SPS/PPS header captured: %zu bytes\n",
                pv->hdr_len);
    /* input: one strided NV12 buffer (the main thread copies into it).
     * Own group — 1600x576x1.5 exceeds the packet group's 1 MB limit. */
    mpp_frame_init(&pv->in_frame);
    mpp_frame_set_width(pv->in_frame, pv->w);
    mpp_frame_set_height(pv->in_frame, pv->h);
    mpp_frame_set_hor_stride(pv->in_frame, pv->hs);
    mpp_frame_set_ver_stride(pv->in_frame, pv->vs);
    mpp_frame_set_fmt(pv->in_frame, MPP_FMT_YUV420SP);
    MppBufferGroup in_grp = NULL;
    if (mpp_buffer_group_get_internal(&in_grp, MPP_BUFFER_TYPE_DRM))
        return -1;
    if (mpp_buffer_group_limit_config(in_grp, (size_t)pv->hs * pv->vs * 3 / 2, 2))
        return -1;
    if (mpp_buffer_get(in_grp, &pv->in_buf,
                       (size_t)pv->hs * pv->vs * 3 / 2))
        return -1;
    mpp_frame_set_buffer(pv->in_frame, pv->in_buf);

    /* UDP sink (ffmpeg -> MediaMTX chain) */
    const char *udp = getenv("AFL_CAM_PREVIEW_UDP");
    if (udp && *udp) {
        char tmp[128];
        snprintf(tmp, sizeof tmp, "%s", udp);
        char *colon = strrchr(tmp, ':');
        if (colon) {
            *colon++ = 0;
            pv->udp_fd = socket(AF_INET, SOCK_DGRAM, 0);
            if (pv->udp_fd >= 0) {
                memset(&pv->udp_addr, 0, sizeof pv->udp_addr);
                pv->udp_addr.sin_family = AF_INET;
                pv->udp_addr.sin_port = htons((uint16_t)atoi(colon));
                pv->udp_addr.sin_addr.s_addr = inet_addr(tmp);
                if (pv->udp_addr.sin_addr.s_addr != INADDR_NONE)
                    pv->udp_ok = 1;
                else {
                    close(pv->udp_fd);
                    pv->udp_fd = -1;
                }
            }
        }
    }
    pv->ok = 1;
    return 0;
}

/* box-downscale the packed SSBO (one Y byte per uint) to the tight
 * preview staging buffer. Runs on the WORKER thread — one extra SSBO
 * read pass after the repack; drops the frame if the main thread has
 * not consumed the previous one yet. */
/* col[x] += src[x] for n bytes, 16 at a time (-O2 leaves the plain loop
 * scalar) */
static void colsum_y(uint16_t *col, const uint8_t *src, RK_U32 n) {
    typedef uint8_t u8x16 __attribute__((vector_size(16)));
    typedef uint16_t u16x16 __attribute__((vector_size(32)));
    RK_U32 x = 0;
    for (; x + 16 <= n; x += 16) {
        u8x16 b;
        u16x16 c;
        memcpy(&b, src + x, sizeof b);
        memcpy(&c, col + x, sizeof c);
        c += __builtin_convertvector(b, u16x16);
        memcpy(col + x, &c, sizeof c);
    }
    for (; x < n; x++)
        col[x] += src[x];
}

/* cu[x] += u, cv[x] += v of n (u | v<<8) chroma uints, 8 at a time */
static void colsum_uv(uint16_t *cu, uint16_t *cv, const uint32_t *src, RK_U32 n) {
    typedef uint32_t u32x8 __attribute__((vector_size(32)));
    typedef uint16_t u16x8 __attribute__((vector_size(16)));
    RK_U32 x = 0;
    for (; x + 8 <= n; x += 8) {
        u32x8 p;
        u16x8 a, b;
        memcpy(&p, src + x, sizeof p);
        memcpy(&a, cu + x, sizeof a);
        memcpy(&b, cv + x, sizeof b);
        a += __builtin_convertvector(p & 0xffu, u16x8);
        b += __builtin_convertvector((p >> 8) & 0xffu, u16x8);
        memcpy(cu + x, &a, sizeof a);
        memcpy(cv + x, &b, sizeof b);
    }
    for (; x < n; x++) {
        cu[x] += (uint16_t)(src[x] & 0xffu);
        cv[x] += (uint16_t)((src[x] >> 8) & 0xffu);
    }
}

static void pv_downscale_ssbo(pv_t *pv, gpu_t *g, int slot) {
    if (!pv->ok)
        return;
    pthread_mutex_lock(&pv->mu);
    if (pv->fresh) {                 /* main thread behind — drop */
        pthread_mutex_unlock(&pv->mu);
        return;
    }
    /* little-endian: Y bytes sit in the SSBO in pixel order, and each
     * chroma uint is (u | v<<8). Each output row first sums its source
     * rows column-wise (vectorised), then box-sums that row along x. */
    const uint8_t *src = g->slots[slot].ssbo_map;
    RK_U32 cw = g->canvas_w, ch = g->canvas_h, cw2 = cw / 2, ch2 = ch / 2;
    RK_U32 pw = pv->w, ph = pv->h, pw2 = pw / 2, ph2 = ph / 2;
    /* canvas width <= 8192, pv->w <= 2048 (clamped at init). The box
     * edges along x are the same for every row: compute them once per
     * frame rather than two 64-bit divides per output pixel. */
    static uint16_t col[8192], colv[4096];
    static RK_U32 bx[2049], bx2[1025];
    for (RK_U32 ox = 0; ox <= pw; ox++)
        bx[ox] = (RK_U32)((uint64_t)ox * cw / pw);
    for (RK_U32 ox = 0; ox <= pw2; ox++)
        bx2[ox] = (RK_U32)((uint64_t)ox * cw2 / pw2);
    uint8_t *y = pv->stage;
    for (RK_U32 oy = 0; oy < ph; oy++) {
        RK_U32 y0 = (RK_U32)((uint64_t)oy * ch / ph);
        RK_U32 y1 = (RK_U32)((uint64_t)(oy + 1) * ch / ph);
        if (y1 <= y0) y1 = y0 + 1;
        memset(col, 0, cw * sizeof col[0]);
        for (RK_U32 yy = y0; yy < y1; yy++)
            colsum_y(col, src + (size_t)yy * cw, cw);
        uint8_t *orow = y + (size_t)oy * pw;
        for (RK_U32 ox = 0; ox < pw; ox++) {
            RK_U32 x0 = bx[ox], x1 = bx[ox + 1];
            if (x1 <= x0) x1 = x0 + 1;
            RK_U32 acc = 0;
            for (RK_U32 xx = x0; xx < x1; xx++)
                acc += col[xx];
            orow[ox] = (uint8_t)(acc / ((y1 - y0) * (x1 - x0)));
        }
    }
    /* chroma: SSBO UV section is one uint per 2x2 block, cw/2 x ch/2 */
    const uint32_t *uvsrc = (const uint32_t *)(src + (size_t)ch * cw);
    uint8_t *uv = pv->stage + (size_t)pw * ph;
    for (RK_U32 oy = 0; oy < ph2; oy++) {
        RK_U32 y0 = (RK_U32)((uint64_t)oy * ch2 / ph2);
        RK_U32 y1 = (RK_U32)((uint64_t)(oy + 1) * ch2 / ph2);
        if (y1 <= y0) y1 = y0 + 1;
        memset(col, 0, cw2 * sizeof col[0]);
        memset(colv, 0, cw2 * sizeof colv[0]);
        for (RK_U32 yy = y0; yy < y1; yy++)
            colsum_uv(col, colv, uvsrc + (size_t)yy * cw2, cw2);
        uint8_t *orow = uv + (size_t)oy * pw2 * 2;
        for (RK_U32 ox = 0; ox < pw2; ox++) {
            RK_U32 x0 = bx2[ox], x1 = bx2[ox + 1];
            if (x1 <= x0) x1 = x0 + 1;
            RK_U32 au = 0, av = 0;
            for (RK_U32 xx = x0; xx < x1; xx++) {
                au += col[xx];
                av += colv[xx];
            }
            RK_U32 n = (y1 - y0) * (x1 - x0);
            orow[2 * ox] = (uint8_t)(au / n);
            orow[2 * ox + 1] = (uint8_t)(av / n);
        }
    }
    pv->fresh = 1;
    pthread_mutex_unlock(&pv->mu);
}

/* does this packet contain an IDR NAL anywhere? rkvenc packs
 * [SEI][IDR slice] into ONE packet, so the packet's FIRST NAL type is 6 —
 * only a scan finds the IDR (the SPS/PPS injection hinges on it) */
static int pkt_has_idr(const RK_U8 *p, size_t n) {
    for (size_t i = 0; i + 3 < n; i++) {
        size_t sc = 0;
        if (p[i] == 0 && p[i + 1] == 0 && p[i + 2] == 1)
            sc = 3;
        else if (i + 4 < n && p[i] == 0 && p[i + 1] == 0 &&
                 p[i + 2] == 0 && p[i + 3] == 1)
            sc = 4;
        if (sc && i + sc < n && (p[i + sc] & 0x1f) == 5)
            return 1;
    }
    return 0;
}

/* copy the tight staging frame into the strided encoder input buffer and
 * put + drain. MAIN THREAD ONLY (encoder invariant). */
static void pv_put_drain(pv_t *pv, int64_t pts) {
    if (!pv->ok)
        return;
    static const RK_U8 sc[4] = { 0, 0, 0, 1 };
    pthread_mutex_lock(&pv->mu);
    if (!pv->fresh) {
        pthread_mutex_unlock(&pv->mu);
        return;
    }
    uint8_t *dst = (RK_U8 *)mpp_buffer_get_ptr(pv->in_buf);
    const uint8_t *y = pv->stage;
    for (RK_U32 row = 0; row < pv->h; row++)
        memcpy(dst + (size_t)row * pv->hs, y + (size_t)row * pv->w, pv->w);
    const uint8_t *uv = pv->stage + (size_t)pv->w * pv->h;
    uint8_t *duv = dst + (size_t)pv->hs * pv->vs;
    for (RK_U32 row = 0; row < pv->h / 2; row++)
        memcpy(duv + (size_t)row * pv->hs, uv + (size_t)row * pv->w, pv->w);
    pv->fresh = 0;
    pthread_mutex_unlock(&pv->mu);

    /* persistent input frame — the MASTER encoder reuses ONE frame per
     * put (set_pts + fresh output packet) and runs clean for hours. The
     * deinit/reinit-per-put variant corrupted MPP's buffer refcounts
     * under continuous preview encoding: after some hours the encoder
     * path spammed "non-positive ref_count / put_meta invalid negative
     * ref_count" (a 400MB log), stalled the main loop and wedged the
     * pipeline (2026-10-11). Do NOT reintroduce per-put frame teardown. */
    mpp_frame_set_pts(pv->in_frame, pts);
    {
        MppPacket enc_pkt = NULL;
        if (!mpp_packet_init_with_buffer(&enc_pkt, pv->out_buf[0])) {
            mpp_packet_set_length(enc_pkt, 0);
            MppMeta m = mpp_frame_get_meta(pv->in_frame);
            if (m)
                mpp_meta_set_packet(m, KEY_OUTPUT_PACKET, enc_pkt);
            mpp_packet_deinit(&enc_pkt);
        }
    }
    if (pv->mpi->encode_put_frame(pv->ctx, pv->in_frame))
        return;

    for (;;) {
        MppPacket pkt = NULL;
        MPP_RET ret = pv->mpi->encode_get_packet(pv->ctx, &pkt);
        if (ret == MPP_ERR_TIMEOUT || !pkt)
            break;
        if (ret)
            break;
        void *ptr = mpp_packet_get_pos(pkt);
        size_t plen = mpp_packet_get_length(pkt);
        if (ptr && plen) {
            /* same defensive NAL check as the master worker_drain:
             * reject the allocator-fill phantom (01 02 03 04 ... run,
             * no start code) */
            int has_sc = plen >= 4 && memcmp(ptr, sc, 4) == 0;
            RK_U8 nal = (has_sc ? ((RK_U8 *)ptr)[4] : ((RK_U8 *)ptr)[0]) & 0x1f;
            int phantom = !has_sc && plen >= 8 &&
                          ((RK_U8 *)ptr)[0] == 1 && ((RK_U8 *)ptr)[1] == 2 &&
                          ((RK_U8 *)ptr)[2] == 3 && ((RK_U8 *)ptr)[3] == 4;
            int ok_nal = (nal == 1 || nal == 5 || nal == 6 ||
                          nal == 7 || nal == 8 || nal == 9);
            if (getenv("AFL_CAM_PV_DEBUG") && nal == 5)
                fprintf(stderr, "pv: IDR packet — hdr=%p len=%zu udp_ok=%d\n",
                        (void *)pv->hdr_data, pv->hdr_len, pv->udp_ok);
            if (ok_nal && !phantom) {
                int has_header_sc = has_sc;
                if (pv->fp_out) {
                    if (!has_header_sc)
                        fwrite(sc, 1, 4, pv->fp_out);
                    fwrite(ptr, 1, plen, pv->fp_out);
                    if (pv->fp_pts) {
                        RK_S64 pkt_pts = mpp_packet_get_pts(pkt);
                        if (pkt_pts <= 0)
                            pkt_pts = pts;
                        fprintf(pv->fp_pts, "%lld\n", (long long)pkt_pts);
                    }
                    fflush(pv->fp_out);
                    if (pv->fp_pts)
                        fflush(pv->fp_pts);
                }
                if (pv->udp_ok) {
                    /* normalize to a start-code stream and send in 60 KB
                     * datagrams (well under the UDP payload limit);
                     * ffmpeg's h264 demuxer reassembles by start codes,
                     * so chunk boundaries may slice NALs freely.
                     * rkvenc emits IDRs WITHOUT parameter sets — prepend
                     * the captured SPS/PPS so late joiners (ffmpeg starts
                     * seconds after cam2stitch) self-sync within a GOP */
                    if (pkt_has_idr((const RK_U8 *)ptr, plen)
                        && !pv->hdr_data)
                        pv_capture_hdr(pv);  /* first IDR: header ready now */
                    if (pkt_has_idr((const RK_U8 *)ptr, plen)
                        && pv->hdr_data && pv->hdr_len) {
                        size_t off = 0;
                        while (off < pv->hdr_len) {
                            size_t n = pv->hdr_len - off;
                            if (n > 60000)
                                n = 60000;
                            sendto(pv->udp_fd, (char *)pv->hdr_data + off, n,
                                   0, (struct sockaddr *)&pv->udp_addr,
                                   sizeof pv->udp_addr);
                            off += n;
                        }
                        pv->pv_hdr_sent++;
                        if (getenv("AFL_CAM_PV_DEBUG") && pv->pv_hdr_sent < 4)
                            fprintf(stderr, "pv: sent %zu-byte SPS/PPS before "
                                    "IDR (udp_ok=%d)\n",
                                    pv->hdr_len, pv->udp_ok);
                    }
                    if (!has_header_sc)
                        sendto(pv->udp_fd, sc, 4, 0,
                               (struct sockaddr *)&pv->udp_addr,
                               sizeof pv->udp_addr);
                    size_t off = 0;
                    while (off < plen) {
                        size_t n = plen - off;
                        if (n > 60000)
                            n = 60000;
                        ssize_t sent = sendto(pv->udp_fd,
                                              (char *)ptr + off, n, 0,
                                              (struct sockaddr *)&pv->udp_addr,
                                              sizeof pv->udp_addr);
                        if (sent < 0 && (errno == EAGAIN || errno == ENOBUFS))
                            pv->pv_drops++;
                        off += n;
                    }
                    pv->pv_packets++;
                    pv->pv_bytes += plen;
                }
            }
        }
        mpp_packet_deinit(&pkt);
    }
}



/* main thread: pair → copy → submit GPU. Worker: repack SSBO → encoder.
 * The stages overlap: while the GPU runs frame i the worker encodes frame
 * i-3 and main copies frame i+1's pair. */

typedef struct {
    int slot;           /* GPU slot whose SSBO holds the packed frame */
    int64_t pts;        /* midpoint pts; < 0 = skip (startup padding) */
    int repack;         /* fill the master encoder input (recording only) */
} work_item;

typedef struct {
    pthread_mutex_t mu;
    pthread_cond_t cv;
    work_item items[16];
    int head, tail, count, stop;
} work_q_t;

typedef struct {
    gpu_t *g;
    work_q_t *q;
    MppCtx enc_ctx;
    MppApi *enc_mpi;
    MppFrame enc_in[4];
    MppBuffer enc_in_buf[4];
    MppBuffer enc_out_buf[4];
    RK_U32 ehs, evs;
    FILE *fp_out, *fp_pts;
    int recording;              /* daemon toggle — MAIN THREAD ONLY */
    int stopping;               /* REC_STOP: no new puts, drain the tail */
    int empty_drains;           /* consecutive empty drains while stopping */
    int64_t rec_bytes;          /* master bytes written (main thread) */
    pv_t *pv;                   /* preview encoder (daemon mode) or NULL */
    int use_hevc;               /* packet validator: H.264 whitelist vs HEVC */
    pthread_mutex_t ack_mu;
    pthread_cond_t ack_cv;
    int ack_done[3];
    int enc_count;
    int drained_total;
    int put_total;
    int64_t t_put_us;
} worker_ctx_t;

/* unpack one GPU slot's packed SSBO into strided NV12 planes (the encoder
 * buffer layout: row stride ehs, UV plane at ehs*evs, rows cw wide) */
static void repack_ssbo_to(gpu_t *g, int slot, void *enc_ptr,
                           RK_U32 ehs, RK_U32 evs) {
    /* little-endian: the SSBO's Y uints (x at the LSB) are already the
     * row's bytes in order, so a Y row is a memcpy; a chroma uint is
     * (u | v<<8), so its low 16 bits are the NV12 UV pair */
    const uint8_t *src = g->slots[slot].ssbo_map;
    uint8_t *dst = enc_ptr;
    size_t cw = g->canvas_w, ch = g->canvas_h;
    for (size_t row = 0; row < ch; row++)
        memcpy(dst + row * ehs, src + row * cw, cw);
    const uint32_t *uvsrc = (const uint32_t *)(src + ch * cw);
    uint8_t *uv = dst + (size_t)ehs * evs;
    for (size_t row = 0; row < ch / 2; row++) {
        uint16_t *orow = (uint16_t *)(uv + row * ehs);
        const uint32_t *irow = uvsrc + row * (cw / 2);
        for (size_t x = 0; x < cw / 2; x++)
            orow[x] = (uint16_t)irow[x];
    }
}

static void worker_put_frame(worker_ctx_t *w, MppFrame ef, MppBuffer out_buf) {
    MppPacket enc_pkt = NULL;
    MPP_RET pr = mpp_packet_init_with_buffer(&enc_pkt, out_buf);
    if (pr || !enc_pkt) {
        fprintf(stderr, "mpp_packet_init_with_buffer failed %d — dropping frame\n", pr);
        return;
    }
    mpp_packet_set_length(enc_pkt, 0);
    MppMeta fmeta = mpp_frame_get_meta(ef);
    if (fmeta)
        mpp_meta_set_packet(fmeta, KEY_OUTPUT_PACKET, enc_pkt);
    mpp_packet_deinit(&enc_pkt);
    MPP_RET ret = w->enc_mpi->encode_put_frame(w->enc_ctx, ef);
    if (ret) fprintf(stderr, "encode_put_frame failed %d\n", ret);
    else w->put_total++;
}

static int worker_drain(worker_ctx_t *w, int64_t fallback_pts) {
    static const RK_U8 sc[4] = { 0, 0, 0, 1 };
    int got = 0;
    for (;;) {
        MppPacket enc_pkt = NULL;
        MPP_RET ret = w->enc_mpi->encode_get_packet(w->enc_ctx, &enc_pkt);
        if (ret == MPP_ERR_TIMEOUT || !enc_pkt)
            break;
        if (ret)
            break;
        got++;
        void *ptr = mpp_packet_get_pos(enc_pkt);
        size_t plen = mpp_packet_get_length(enc_pkt);
        if (ptr && plen) {
            /* defensive: a phantom packet (never filled by rkvenc) holds
             * allocator fill bytes (01 02 03 04 ...) — its NAL type is 2/3/4
             * (data partitions, which rkvenc never emits: the clean
             * reference stream only has types 1/5/6/7/8/9). Skip it instead
             * of poisoning the stream. NOTE: rkvenc sometimes includes the
             * 4-byte start code in the packet and sometimes not — locate
             * the NAL header accordingly. */
            const RK_U8 *pb = ptr;
            int has_sc = plen >= 4 && memcmp(pb, sc, 4) == 0;
            RK_U8 nal = (has_sc ? pb[4] : pb[0]) & 0x1f;
            /* HEVC NAL types (& 0x1f) are legitimately 0/1/2/3/7... (VPS=32,
             * SPS=33, PPS=34, IDR=19-21), so the H.264 whitelist would drop
             * every keyframe. For HEVC only reject the allocator-fill
             * phantom (a strict 01 02 03 04 ... byte run with no start
             * code) — the one signature the H.264 whitelist was guarding
             * against anyway. */
            int ok_nal;
            if (w->use_hevc) {
                int phantom = !has_sc && plen >= 8 &&
                              pb[0] == 1 && pb[1] == 2 && pb[2] == 3 && pb[3] == 4 &&
                              pb[4] == 5 && pb[5] == 6 && pb[6] == 7 && pb[7] == 8;
                ok_nal = !phantom;
            } else {
                ok_nal = (nal == 1 || nal == 5 || nal == 6 ||
                          nal == 7 || nal == 8 || nal == 9);
            }
            if (ok_nal) {
                if (w->recording && w->fp_out) {
                    if (plen < 4 || memcmp(ptr, sc, 4) != 0)
                        fwrite(sc, 1, 4, w->fp_out);
                    fwrite(ptr, 1, plen, w->fp_out);
                    w->rec_bytes += plen;
                    if (w->fp_pts) {
                        RK_S64 pkt_pts = mpp_packet_get_pts(enc_pkt);
                        if (pkt_pts <= 0)
                            pkt_pts = fallback_pts;
                        fprintf(w->fp_pts, "%lld\n", (long long)pkt_pts);
                    }
                    /* flush per packet: a crash loses at most one frame,
                     * and the phone can watch bytes grow in near-realtime */
                    fflush(w->fp_out);
                    if (w->fp_pts)
                        fflush(w->fp_pts);
                }
            } else {
                fprintf(stderr, "worker: dropping bad packet (nal %u, %zu bytes)\n",
                        nal, plen);
            }
        }
        mpp_packet_deinit(&enc_pkt);
        w->enc_count++;
        w->drained_total++;
    }
    return got;
}

static void *encode_worker(void *arg) {
    worker_ctx_t *w = arg;
    for (;;) {
        work_item it;
        pthread_mutex_lock(&w->q->mu);
        while (w->q->count == 0 && !w->q->stop)
            pthread_cond_wait(&w->q->cv, &w->q->mu);
        if (w->q->count == 0) {
            pthread_mutex_unlock(&w->q->mu);
            break;
        }
        it = w->q->items[w->q->head % 16];
        w->q->head++;
        w->q->count--;
        pthread_mutex_unlock(&w->q->mu);

        if (it.slot == -1)
            break;      /* EOS handled on the main thread */

        if (it.pts < 0) {
            /* startup padding item — nothing to repack, release the ack */
            pthread_mutex_lock(&w->ack_mu);
            w->ack_done[it.slot] = 1;
            pthread_cond_broadcast(&w->ack_cv);
            pthread_mutex_unlock(&w->ack_mu);
            continue;
        }

        /* repack the finished slot's SSBO straight into the encoder input.
         * The ENCODER (put/drain) runs on the MAIN thread — the old sync
         * design was clean for hours, while encoder calls from this worker
         * thread produced intermittent phantom/corrupt packets. */
        /* standby: nothing encodes the master, so skip its repack (a
         * full-canvas pass that kept one core busy between recordings) */
        if (it.repack) {
            void *enc_ptr = mpp_buffer_get_ptr(w->enc_in_buf[0]);
            repack_ssbo_to(w->g, it.slot, enc_ptr, w->ehs, w->evs);
        }
        /* preview downscale (daemon mode): one extra SSBO read pass */
        if (w->pv)
            pv_downscale_ssbo(w->pv, w->g, it.slot);
        pthread_mutex_lock(&w->ack_mu);
        w->ack_done[it.slot] = 1;
        pthread_cond_broadcast(&w->ack_cv);
        pthread_mutex_unlock(&w->ack_mu);
    }
    return NULL;
}

/* ======================= camera capture ring ======================= */

typedef struct {
    MppFrame frame;             /* NULL after the consumer deinit'd it */
    MppBuffer buf;
    void *map_ptr;              /* our own cached mmap of the buffer's fd —
                                 * MPP's mapping is uncached (~120MB/s), this
                                 * one reads at >1GB/s */
    int64_t pts_us;
    int state;                  /* 0 FREE, 1 IN_DECODE/HELD, 2 READY */
} ring_slot_t;

typedef struct {
    ring_slot_t slots[CAM_RING];
    pthread_mutex_t mu;
    pthread_cond_t cv_ready, cv_free;
    int put_idx, get_idx, n_ready;
    int done;                   /* producer finished */
    int64_t first_pts;          /* pts of the first decoded frame */
    int total;                  /* frames decoded */
} ring_t;

/* wait for a READY slot (want_ready) or a FREE slot (!want_ready);
 * returns the slot index, -1 when stopped. Timed waits poll g_running. */
static int ring_wait_slot(ring_t *r, int want_ready) {
    pthread_mutex_lock(&r->mu);
    for (;;) {
        if (want_ready) {
            if (r->n_ready > 0) {
                int idx = r->get_idx;
                pthread_mutex_unlock(&r->mu);
                return idx;
            }
            if (r->done) {
                pthread_mutex_unlock(&r->mu);
                return -1;
            }
        } else {
            for (int i = 0; i < CAM_RING; i++) {
                int idx = (r->put_idx + i) % CAM_RING;
                if (r->slots[idx].state == 0) {
                    pthread_mutex_unlock(&r->mu);
                    return idx;
                }
            }
        }
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        ts.tv_nsec += 5 * 1000 * 1000;
        if (ts.tv_nsec >= 1000000000) { ts.tv_sec++; ts.tv_nsec -= 1000000000; }
        pthread_cond_timedwait(want_ready ? &r->cv_ready : &r->cv_free, &r->mu, &ts);
        if (!g_running) {
            pthread_mutex_unlock(&r->mu);
            return -1;
        }
    }
}

static void ring_push(ring_t *r, int idx, int64_t pts) {
    pthread_mutex_lock(&r->mu);
    r->slots[idx].pts_us = pts;
    r->slots[idx].state = 2;    /* READY */
    if (r->total == 0)
        r->first_pts = pts;
    r->total++;
    r->n_ready++;
    r->put_idx = (idx + 1) % CAM_RING;
    pthread_cond_signal(&r->cv_ready);
    pthread_mutex_unlock(&r->mu);
}

/* consumer: take ownership of the oldest READY frame (blocking).
 * Returns the slot index, -1 when the producer is done and empty. */
static int ring_take(ring_t *r, MppFrame *frame_out, int64_t *pts_out) {
    int idx = ring_wait_slot(r, 1);
    if (idx < 0)
        return -1;
    pthread_mutex_lock(&r->mu);
    r->slots[idx].state = 1;    /* held by consumer */
    r->n_ready--;
    r->get_idx = (idx + 1) % CAM_RING;
    pthread_cond_signal(&r->cv_free);
    pthread_mutex_unlock(&r->mu);
    *frame_out = r->slots[idx].frame;
    if (pts_out)
        *pts_out = r->slots[idx].pts_us;
    return idx;
}

/* consumer: release a taken frame back to FREE */
static void ring_release(ring_t *r, int idx) {
    pthread_mutex_lock(&r->mu);
    mpp_frame_deinit(&r->slots[idx].frame);
    r->slots[idx].frame = NULL;
    r->slots[idx].state = 0;    /* FREE */
    pthread_cond_signal(&r->cv_free);
    pthread_mutex_unlock(&r->mu);
}

/* ========================== camera thread ========================== */

typedef struct {
    const char *dev;
    int fd;
    v4l2_buf_t bufs[V4L2_BUFS];
    uint32_t n_v4l2;
    int stream_on;
    MppCtx dec_ctx;
    MppApi *dec_mpi;
    MppBufferGroup pkt_grp, frm_grp;
    ring_t ring;
    pthread_t thread;
    RK_U32 width, height, fps;
    RK_S32 exposure;
    int count;                  /* frames decoded */
    int errors;                 /* decode failures (status reporting) */
    /* capture-path timing (status "cam*.t"): where the frame rate goes */
    int64_t t_slot_us;          /* waiting for a free ring slot (pipeline behind) */
    int64_t t_dq_us;            /* waiting in DQBUF for the camera to deliver */
    int64_t t_dec_us;           /* jpegd put_packet..get_frame (shared VPU core) */
    int64_t t_n;                /* frames the three above were summed over */
    int64_t ts_prev;            /* previous frame's V4L2 timestamp */
    int64_t ts_dt_sum;          /* sum of intervals between frames */
    int ts_dt_n;
    int ts_gap;                 /* intervals above 1.5x the nominal 33.3 ms */
    uint32_t seq_prev;
    int seq_valid;
    int seq_lost;               /* V4L2 sequence numbers skipped by the driver */
    volatile double luma_last;  /* mean Y of a sampled frame (every 30th) */
    volatile double u_last, v_last;  /* mean U/V of the same frame */
    int cap_bright, cap_gamma;  /* dim-light lift at the exposure cap */
    /* exposure state machine (cam2h264.c) */
    int lock_pending, lock_phase, refine_left, refine_rounds;
    double target_luma, luma_acc;
    int luma_n;
    int64_t prev_pts;
    int64_t dt_sum;
    int dt_n;
    RK_S32 lock_exp;
    /* recording-time live preview: raw-JPEG snapshot path + pacing */
    char preview_path[512];
    int64_t snap_interval_us;
    int64_t last_snap;
} cam_t;

static int v4l2_open(cam_t *c) {
    int fd = open(c->dev, O_RDWR | O_NONBLOCK, 0);
    if (fd < 0) {
        fprintf(stderr, "open %s failed: %s\n", c->dev, strerror(errno));
        return -1;
    }
    c->fd = fd;
    struct v4l2_format fmt;
    memset(&fmt, 0, sizeof(fmt));
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    fmt.fmt.pix.width = c->width;
    fmt.fmt.pix.height = c->height;
    fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG;
    fmt.fmt.pix.field = V4L2_FIELD_NONE;
    if (ioctl(fd, VIDIOC_S_FMT, &fmt) < 0) {
        fprintf(stderr, "%s: S_FMT failed: %s\n", c->dev, strerror(errno));
        return -1;
    }
    struct v4l2_streamparm parm;
    memset(&parm, 0, sizeof(parm));
    parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    parm.parm.capture.timeperframe.numerator = 1;
    parm.parm.capture.timeperframe.denominator = c->fps;
    ioctl(fd, VIDIOC_S_PARM, &parm);

    if (c->exposure > 0) {
        set_exposure(fd, V4L2_EXPOSURE_MANUAL, c->exposure);
    } else {
        /* this camera's auto mode is menu value 3 (cam2h264.c); also drop
         * any brightness/gamma lift an earlier run left in the camera */
        set_ctrl(fd, V4L2_CID_BRIGHTNESS, 0);
        set_ctrl(fd, V4L2_CID_GAMMA, 100);
        set_exposure(fd, 3, 0);
        /* both cameras on the SAME fixed white balance: on the rig their own
         * auto white balance disagreed by ~15 in U and V (a visible colour
         * step at the seam), fixed at one temperature they are within ~3.
         * AFL_CAM_WB_K = kelvin (2800-6500), 0 = leave the cameras on auto */
        {
            const char *wbs = getenv("AFL_CAM_WB_K");
            int wbk = wbs ? atoi(wbs) : 5500;
            if (wbk >= 2800 && wbk <= 6500) {
                set_ctrl(fd, V4L2_CID_AUTO_WHITE_BALANCE, 0);
                set_ctrl(fd, V4L2_CID_WHITE_BALANCE_TEMPERATURE, wbk);
            } else {
                set_ctrl(fd, V4L2_CID_AUTO_WHITE_BALANCE, 1);
            }
        }
    }

    struct v4l2_requestbuffers req;
    memset(&req, 0, sizeof(req));
    req.count = V4L2_BUFS;
    req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;
    if (ioctl(fd, VIDIOC_REQBUFS, &req) < 0) {
        fprintf(stderr, "%s: REQBUFS failed: %s\n", c->dev, strerror(errno));
        return -1;
    }
    c->n_v4l2 = req.count;
    for (uint32_t i = 0; i < c->n_v4l2; i++) {
        struct v4l2_buffer buf;
        memset(&buf, 0, sizeof(buf));
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i;
        if (ioctl(fd, VIDIOC_QUERYBUF, &buf) < 0)
            return -1;
        c->bufs[i].length = buf.length;
        c->bufs[i].start = mmap(NULL, buf.length, PROT_READ | PROT_WRITE,
                                MAP_SHARED, fd, buf.m.offset);
        if (c->bufs[i].start == MAP_FAILED)
            return -1;
    }
    for (uint32_t i = 0; i < c->n_v4l2; i++) {
        struct v4l2_buffer buf;
        memset(&buf, 0, sizeof(buf));
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i;
        if (ioctl(fd, VIDIOC_QBUF, &buf) < 0)
            return -1;
    }
    enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (ioctl(fd, VIDIOC_STREAMON, &type) < 0) {
        fprintf(stderr, "%s: STREAMON failed: %s\n", c->dev, strerror(errno));
        return -1;
    }
    c->stream_on = 1;
    return 0;
}

static int dec_init(cam_t *c) {
    RK_S32 ret = mpp_create(&c->dec_ctx, &c->dec_mpi);
    if (ret) return ret;
    ret = mpp_init(c->dec_ctx, MPP_CTX_DEC, MPP_VIDEO_CodingMJPEG);
    if (ret) return ret;
    MppDecCfg dec_cfg = NULL;
    mpp_dec_cfg_init(&dec_cfg);
    ret = c->dec_mpi->control(c->dec_ctx, MPP_DEC_GET_CFG, dec_cfg);
    if (!ret)
        mpp_dec_cfg_set_u32(dec_cfg, "base:split_parse", 1);
    ret = c->dec_mpi->control(c->dec_ctx, MPP_DEC_SET_CFG, dec_cfg);
    if (ret) return ret;
    RK_S32 out_fmt = MPP_FMT_YUV420SP;
    ret = c->dec_mpi->control(c->dec_ctx, MPP_DEC_SET_OUTPUT_FORMAT, &out_fmt);
    if (ret) return ret;
    ret = mpp_buffer_group_get_internal(&c->pkt_grp, MPP_BUFFER_TYPE_DRM);
    if (ret) return ret;
    ret = mpp_buffer_group_limit_config(c->pkt_grp, 1024 * 1024, 4);
    if (ret) return ret;
    RK_U32 hs = MPP_ALIGN(c->width, 16), vs = MPP_ALIGN(c->height, 16);
    ret = mpp_buffer_group_get_internal(&c->frm_grp, MPP_BUFFER_TYPE_DRM);
    if (ret) return ret;
    ret = mpp_buffer_group_limit_config(c->frm_grp, hs * vs * 2, CAM_RING);
    return ret;
}

/* per-frame exposure state machine — ported verbatim from cam2h264.c */
static void exposure_step(cam_t *c, MppFrame dec_frame, int64_t pts_us) {
    if (!c->lock_pending)
        return;
    if (c->prev_pts > 0 && pts_us > c->prev_pts) {
        c->dt_sum += pts_us - c->prev_pts;
        c->dt_n++;
    }
    c->prev_pts = pts_us;
    double l = frame_luma(dec_frame);
    if (l < 0)
        return;
    if (c->lock_phase == 0) {
        if (c->count >= 60) { c->luma_acc += l; c->luma_n++; }
        if (c->count >= 119) {
            c->target_luma = c->luma_n ? c->luma_acc / c->luma_n : 64.0;
            if (c->dt_n > 0 && (c->dt_sum / c->dt_n) > 33333) {
                /* frame rate first: auto wants a longer exposure than one
                 * 30 fps frame, which drops the sensor to ~20 fps. Cap at
                 * 30ms (image is darker, rate holds near 28 fps). */
                c->lock_exp = 300;
                set_exposure(c->fd, V4L2_EXPOSURE_MANUAL, c->lock_exp);
                /* manual exposure drops the camera's own boost, so the
                 * picture goes much darker than auto: lift it with the
                 * camera's brightness (then gamma) until the mean luma
                 * is back near what auto gave (phase 2) */
                c->lock_phase = 2;
                c->refine_left = 40;
                c->refine_rounds = 0;
                c->cap_bright = 0;
                c->cap_gamma = 100;
                c->luma_acc = 0.0; c->luma_n = 0;
                fprintf(stderr, "%s: exposure: auto needs %.1fms (>33ms) — "
                        "capped at 30ms to hold the frame rate, lifting "
                        "brightness to luma %.0f\n",
                        c->dev, (c->dt_sum / c->dt_n) / 1000.0, c->target_luma);
            } else {
                if (c->dt_n > 0) {
                    c->lock_exp = (RK_S32)((c->dt_sum / c->dt_n) / 100);
                    if (c->lock_exp < 30) c->lock_exp = 30;
                    if (c->lock_exp > 330) c->lock_exp = 330;
                }
                set_exposure(c->fd, V4L2_EXPOSURE_MANUAL, c->lock_exp);
                c->lock_phase = 1;
                c->refine_left = 30;
                c->luma_acc = 0.0; c->luma_n = 0;
                fprintf(stderr, "%s: exposure: auto settled at luma %.0f -> manual %d (%dms), refining...\n",
                        c->dev, c->target_luma, c->lock_exp, c->lock_exp / 10);
            }
        }
    } else if (c->lock_phase == 2) {
        /* the control change takes a few frames: measure the last 15 of 40 */
        if (--c->refine_left <= 15) { c->luma_acc += l; c->luma_n++; }
        if (c->refine_left == 0) {
            double meas = c->luma_n ? c->luma_acc / c->luma_n : c->target_luma;
            double err = c->target_luma - meas;
            int done = (err < 0.12 * c->target_luma + 2.0 && err > -0.12 * c->target_luma - 2.0)
                       || ++c->refine_rounds >= 8;
            if (done) {
                c->lock_pending = 0;
                fprintf(stderr, "%s: exposure: capped at 30ms, brightness %d gamma %d, "
                        "luma %.0f (target %.0f)\n", c->dev, c->cap_bright,
                        c->cap_gamma, meas, c->target_luma);
            } else {
                if (err > 0 && c->cap_bright >= 64) {
                    c->cap_gamma += (int)(err / 0.3);
                    if (c->cap_gamma > 300) c->cap_gamma = 300;
                    set_ctrl(c->fd, V4L2_CID_GAMMA, c->cap_gamma);
                } else {
                    int step = (int)(err / 1.2);
                    if (step > 24) step = 24;
                    if (step < -24) step = -24;
                    c->cap_bright += step;
                    if (c->cap_bright > 64) c->cap_bright = 64;
                    if (c->cap_bright < 0) c->cap_bright = 0;
                    set_ctrl(c->fd, V4L2_CID_BRIGHTNESS, c->cap_bright);
                }
                c->refine_left = 40;
                c->luma_acc = 0.0; c->luma_n = 0;
            }
        }
    } else {
        c->luma_acc += l; c->luma_n++;
        if (--c->refine_left == 0) {
            double meas = c->luma_n ? c->luma_acc / c->luma_n : c->target_luma;
            double ratio = meas > 1.0 ? c->target_luma / meas : 1.0;
            if (ratio < 0.8) ratio = 0.8;
            if (ratio > 1.25) ratio = 1.25;
            c->lock_exp = (RK_S32)(c->lock_exp * ratio + 0.5);
            if (c->lock_exp < 30) c->lock_exp = 30;
            if (c->lock_exp > 330) c->lock_exp = 330;
            set_exposure(c->fd, V4L2_EXPOSURE_MANUAL, c->lock_exp);
            c->luma_acc = 0.0; c->luma_n = 0;
            int converged = (ratio > 0.9 && ratio < 1.11);
            if (++c->refine_rounds >= 5 || converged) {
                c->lock_pending = 0;
                fprintf(stderr, "%s: exposure: locked at %d (%dms)\n",
                        c->dev, c->lock_exp, c->lock_exp / 10);
            } else {
                c->refine_left = 30;
            }
        }
    }
}

static void *capture_thread(void *arg) {
    cam_t *c = arg;
    while (g_running) {
        /* backpressure: wait for a FREE ring slot BEFORE DQBUF */
        int64_t t_a = now_us();
        int idx = ring_wait_slot(&c->ring, 0);
        if (idx < 0)
            break;
        c->t_slot_us += now_us() - t_a;
        ring_slot_t *slot = &c->ring.slots[idx];

        if (slot->frame == NULL) {
            RK_U32 hs = MPP_ALIGN(c->width, 16), vs = MPP_ALIGN(c->height, 16);
            /* the slot's buffer is allocated ONCE and reused for the slot's
             * lifetime (cam2h264 pattern) — re-getting every cycle exhausts
             * the group pool, since the slot keeps its own get-ref */
            if (slot->buf == NULL) {
                RK_S32 r = mpp_buffer_get(c->frm_grp, &slot->buf, hs * vs * 2);
                if (r) { fprintf(stderr, "%s: frm buffer get failed %d\n", c->dev, r);
                         cma_print("frm get fail"); break; }
                int sfd = mpp_buffer_get_fd(slot->buf);
                if (sfd >= 0) {
                    slot->map_ptr = mmap(NULL, hs * vs * 2, PROT_READ | PROT_WRITE,
                                         MAP_SHARED, sfd, 0);
                    if (slot->map_ptr == MAP_FAILED)
                        slot->map_ptr = NULL;
                }
            }
            mpp_frame_init(&slot->frame);
            mpp_frame_set_width(slot->frame, c->width);
            mpp_frame_set_height(slot->frame, c->height);
            mpp_frame_set_hor_stride(slot->frame, hs);
            mpp_frame_set_ver_stride(slot->frame, vs);
            mpp_frame_set_fmt(slot->frame, MPP_FMT_YUV420SP);
            mpp_frame_set_buffer(slot->frame, slot->buf);
        }
        slot->state = 1;        /* IN_DECODE */

        struct v4l2_buffer vbuf;
        memset(&vbuf, 0, sizeof(vbuf));
        vbuf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        vbuf.memory = V4L2_MEMORY_MMAP;
        int q_ok = 0;
        int64_t t_b = now_us();
        while (g_running) {
            if (ioctl(c->fd, VIDIOC_DQBUF, &vbuf) < 0) {
                if (errno == EAGAIN) { usleep(1000); continue; }
                fprintf(stderr, "%s: DQBUF failed: %s\n", c->dev, strerror(errno));
                break;
            }
            q_ok = 1;
            break;
        }
        if (!q_ok) { slot->state = 0; break; }

        int64_t pts_us = (int64_t)vbuf.timestamp.tv_sec * 1000000 +
                         vbuf.timestamp.tv_usec;
        size_t jpeg_size = vbuf.bytesused;
        int64_t t_c = now_us();
        c->t_dq_us += t_c - t_b;
        if (c->ts_prev > 0 && pts_us > c->ts_prev) {
            c->ts_dt_sum += pts_us - c->ts_prev;
            c->ts_dt_n++;
            if (pts_us - c->ts_prev > 50000)
                c->ts_gap++;
        }
        c->ts_prev = pts_us;
        if (c->seq_valid && vbuf.sequence > c->seq_prev + 1)
            c->seq_lost += (int)(vbuf.sequence - c->seq_prev - 1);
        c->seq_prev = vbuf.sequence;
        c->seq_valid = 1;

        /* live-preview snapshot: raw UVC JPEG payload straight to disk
         * (full 4K, colors exact — the camera's own JPEG). The jpegenc
         * downscale path is parked: vepu's 420SP UV offset proved opaque
         * and RGB888 is rejected (2026-09-07). */
        if (now_us() - c->last_snap >= c->snap_interval_us) {
            write_preview(c->preview_path, c->bufs[vbuf.index].start, jpeg_size);
            c->last_snap = now_us();
        }

        MppBuffer dec_in_buf = NULL;
        MppPacket dec_pkt = NULL;
        RK_S32 r = mpp_buffer_get(c->pkt_grp, &dec_in_buf, jpeg_size);
        if (r) { ioctl(c->fd, VIDIOC_QBUF, &vbuf); slot->state = 0; continue; }
        memcpy(mpp_buffer_get_ptr(dec_in_buf), c->bufs[vbuf.index].start, jpeg_size);
        mpp_packet_init_with_buffer(&dec_pkt, dec_in_buf);
        mpp_packet_set_pos(dec_pkt, mpp_buffer_get_ptr(dec_in_buf));
        mpp_packet_set_length(dec_pkt, jpeg_size);
        {
            MppMeta meta = mpp_packet_get_meta(dec_pkt);
            if (meta)
                mpp_meta_set_frame(meta, KEY_OUTPUT_FRAME, slot->frame);
        }
        r = c->dec_mpi->decode_put_packet(c->dec_ctx, dec_pkt);
        mpp_packet_deinit(&dec_pkt);
        mpp_buffer_put(dec_in_buf);
        ioctl(c->fd, VIDIOC_QBUF, &vbuf);
        if (r) { c->errors++; slot->state = 0; continue; }

        MppFrame dec_frame = NULL;
        r = c->dec_mpi->decode_get_frame(c->dec_ctx, &dec_frame);
        c->t_dec_us += now_us() - t_c;
        c->t_n++;
        if (r || !dec_frame) { c->errors++; slot->state = 0; continue; }
        if (mpp_frame_get_info_change(dec_frame)) {
            c->dec_mpi->control(c->dec_ctx, MPP_DEC_SET_INFO_CHANGE_READY, NULL);
            mpp_frame_deinit(&dec_frame);
            slot->state = 0;
            continue;
        }
        if (mpp_frame_get_errinfo(dec_frame)) {
            c->errors++;
            mpp_frame_deinit(&dec_frame);
            slot->state = 0;
            continue;
        }
        c->count++;
        if (c->count % 30 == 0) {
            double ll = frame_luma(dec_frame);
            if (ll >= 0)
                c->luma_last = ll;
            double cu, cv;
            if (frame_chroma(dec_frame, &cu, &cv) == 0) {
                c->u_last = cu;
                c->v_last = cv;
            }
        }
        exposure_step(c, dec_frame, pts_us);
        ring_push(&c->ring, idx, pts_us);
    }
    pthread_mutex_lock(&c->ring.mu);
    c->ring.done = 1;
    pthread_cond_signal(&c->ring.cv_ready);
    pthread_mutex_unlock(&c->ring.mu);
    fprintf(stderr, "%s: captured %d frames\n", c->dev, c->count);
    return NULL;
}

/* ====================== harness mode (-src) ====================== */

static int run_src_mode(const char *dir, const char *in0, const char *in1,
                        const char *out_path, int n_iters, const char *spv_path) {
    char path[512];
    size_t len;
    void *params_data = read_file((snprintf(path, sizeof path, "%s/params.bin", dir), path), &len);
    if (len != 64 * sizeof(float)) { fprintf(stderr, "params.bin size mismatch\n"); return 1; }
    void *w0d = read_file((snprintf(path, sizeof path, "%s/w0.bin", dir), path), &len);
    void *w1d = read_file((snprintf(path, sizeof path, "%s/w1.bin", dir), path), &len);
    void *m0d = read_file((snprintf(path, sizeof path, "%s/map0.bin", dir), path), &len);
    void *m1d = read_file((snprintf(path, sizeof path, "%s/map1.bin", dir), path), &len);
    void *nv0 = read_file(in0, &len);
    void *nv1 = read_file(in1, &len);

    gpu_t g = {0};
    gpu_init(&g, spv_path, params_data, w0d, w1d, m0d, m1d);
    void *tight = malloc(g.tight_size);
    int64_t t0 = now_us();
    double acc[5] = {0};
    for (int i = 0; i < n_iters; i++) {
        gpu_stitch(&g, nv0, nv1, tight);
        for (int j = 0; j < 5; j++) acc[j] += g_times[j];
    }
    int64_t elapsed = now_us() - t0;
    printf("%d frames in %.1f ms (%.2f ms/frame)\n", n_iters, elapsed / 1000.0,
           n_iters ? elapsed / 1000.0 / n_iters : 0.0);
    printf("stage avg us: wait_before %.1f copy %.1f submit %.1f gpu_wait %.1f pack %.1f\n",
           acc[0] / n_iters / 1000.0, acc[1] / n_iters / 1000.0, acc[2] / n_iters / 1000.0,
           acc[3] / n_iters / 1000.0, acc[4] / n_iters / 1000.0);
    FILE *out = fopen(out_path, "wb");
    if (!out) { perror(out_path); return 1; }
    fwrite(tight, 1, g.tight_size, out);
    fclose(out);
    printf("wrote %s (%zu bytes, repacked)\n", out_path, g.tight_size);
    return 0;
}

/* ==================== daemon control socket ==================== */

/* Only when AFL_CAM_CTL_SOCK is set (afl-camd supervisor). Line protocol,
 * one command per line: REC_START <dir> / REC_STOP / STATUS / QUIT.
 * The ctl thread only parses and sets pending flags + replies; ALL
 * application actions run on the main thread (see ctl_apply_pending).
 * A JSON status line is pushed to every connected client ~1/s. */

typedef struct {
    int fd;
    char buf[1024];
    int len;
} ctl_client_t;

typedef struct {
    pthread_mutex_t mu;
    /* ctl thread -> main loop */
    int start_pending, stop_pending, quit_pending;
    char start_dir[512];
    /* main loop -> ctl thread (status) */
    int recording;
    char rec_dir[512];
    int64_t rec_start_us;
    int64_t rec_bytes;
    int status_gen;
    char status_buf[2048];
    int status_len;
    /* socket */
    int listen_fd;
    ctl_client_t clients[16];
    int n_clients;
} ctl_t;

static void ctl_reply(int fd, const char *msg) {
    char line[512];
    int n = snprintf(line, sizeof line, "%s\n", msg);
    if (n > 0)
        send(fd, line, (size_t)n, MSG_NOSIGNAL);
}

/* per-camera capture timing as JSON fields: mean ms per frame spent waiting
 * for a ring slot (pipeline behind), waiting for the camera, and in jpegd;
 * the mean interval between camera timestamps, intervals over 50 ms, and
 * driver-lost sequence numbers. Averages since the pipeline started. */
static const char *cam_timing(const cam_t *c, char *buf, size_t n) {
    double k = c->t_n > 0 ? 1.0 / (double)c->t_n / 1000.0 : 0.0;
    double dt = c->ts_dt_n > 0 ? (double)c->ts_dt_sum / c->ts_dt_n / 1000.0 : 0.0;
    snprintf(buf, n, "\"slot_ms\":%.2f,\"dq_ms\":%.2f,\"dec_ms\":%.2f,"
             "\"dt_ms\":%.2f,\"gap50\":%d,\"seq_lost\":%d,\"luma\":%.1f,\"u\":%.1f,\"v\":%.1f",
             c->t_slot_us * k, c->t_dq_us * k, c->t_dec_us * k, dt,
             c->ts_gap, c->seq_lost, c->luma_last, c->u_last, c->v_last);
    return buf;
}

/* called by the MAIN thread once per frame */
static void ctl_publish(ctl_t *c, cam_t *cams, worker_ctx_t *w, pv_t *pv,
                        int out_count, int dup_count, int skip_count,
                        int skipped_start, int64_t skew_sum, int64_t skew_n) {
    if (!c)
        return;
    char tbuf0[256], tbuf1[256];
    pthread_mutex_lock(&c->mu);
    c->rec_bytes = w->rec_bytes;
    int64_t elapsed = c->recording ? now_us() - c->rec_start_us : 0;
    int64_t skew = skew_n ? skew_sum / skew_n : -1;
    int n = snprintf(c->status_buf, sizeof c->status_buf,
        "{\"state\":\"%s\",\"t_us\":%lld,\"rec_dir\":\"%s\",\"rec_elapsed_s\":%lld.%03lld,"
        "\"rec_bytes\":%lld,"
        "\"cam0\":{\"frames\":%d,\"errors\":%d,%s},"
        "\"cam1\":{\"frames\":%d,\"errors\":%d,%s},"
        "\"paired\":%d,\"dup\":%d,\"skip\":%d,\"start_trim\":%d,"
        "\"skew_avg_us\":%lld,"
        "\"enc\":{\"packets\":%d,\"put\":%d,\"drained\":%d},"
        "\"preview\":{\"on\":%d,\"packets\":%llu,\"bytes\":%llu,"
        "\"drops\":%llu,\"hdr_sent\":%llu}}\n",
        c->recording ? "recording" : "standby",
        (long long)now_us(),
        c->recording ? c->rec_dir : "",
        (long long)(elapsed / 1000000), (long long)((elapsed / 1000) % 1000),
        (long long)c->rec_bytes,
        cams[0].count, cams[0].errors, cam_timing(&cams[0], tbuf0, sizeof tbuf0),
        cams[1].count, cams[1].errors, cam_timing(&cams[1], tbuf1, sizeof tbuf1),
        out_count, dup_count, skip_count, skipped_start,
        (long long)skew,
        w->enc_count, w->put_total, w->drained_total,
        pv && pv->ok ? 1 : 0,
        (unsigned long long)(pv ? pv->pv_packets : 0),
        (unsigned long long)(pv ? pv->pv_bytes : 0),
        (unsigned long long)(pv ? pv->pv_drops : 0),
        (unsigned long long)(pv ? pv->pv_hdr_sent : 0));
    if (n > 0 && n < (int)sizeof c->status_buf)
        c->status_len = n;
    c->status_gen++;
    pthread_mutex_unlock(&c->mu);
}

static void sync_rec_files(const char *dir) {
    static const char *names[] = { "stitch.raw.h264", "stitch.pts",
                                   "preview.raw.h264", "preview.pts" };
    char path[1100];
    for (size_t i = 0; i < sizeof names / sizeof names[0]; i++) {
        snprintf(path, sizeof path, "%s/%s", dir, names[i]);
        int fd = open(path, O_RDONLY);
        if (fd < 0)
            continue;
        fdatasync(fd);
        close(fd);
    }
}

/* called by the MAIN thread once the encoder drain has gone quiet while
 * stopping: flush + close the recording files (proxy included) */
static void ctl_finish_stop(ctl_t *c, worker_ctx_t *w, pv_t *pv) {
    if (w->fp_out)
        fclose(w->fp_out);
    if (w->fp_pts)
        fclose(w->fp_pts);
    w->fp_out = w->fp_pts = NULL;
    if (pv && pv->ok) {
        if (pv->fp_out)
            fclose(pv->fp_out);
        if (pv->fp_pts)
            fclose(pv->fp_pts);
        pv->fp_out = pv->fp_pts = NULL;
    }
    w->recording = 0;
    w->stopping = 0;
    w->empty_drains = 0;
    int64_t bytes = w->rec_bytes;
    char closed_dir[512];
    pthread_mutex_lock(&c->mu);
    c->recording = 0;
    strncpy(closed_dir, c->rec_dir, sizeof closed_dir - 1);
    closed_dir[sizeof closed_dir - 1] = 0;
    pthread_mutex_unlock(&c->mu);
    fprintf(stderr, "ctl: REC_STOP — closed %s (%lld bytes)\n",
            closed_dir, (long long)bytes);
    sync_rec_files(closed_dir);
}

/* called by the MAIN thread at the bottom of each loop iteration:
 * apply pending REC_START/REC_STOP/QUIT after the drain, so a stop
 * flushes the last packets and a start begins on a clean encoder. */
static void ctl_apply_pending(ctl_t *c, worker_ctx_t *w, pv_t *pv,
                              MppCtx enc_ctx, MppApi *enc_mpi,
                              const void *hdr_data, size_t hdr_len) {
    if (!c)
        return;
    pthread_mutex_lock(&c->mu);
    int stop = c->stop_pending;
    int start = c->start_pending;
    int quit = c->quit_pending;
    char dir[512];
    strncpy(dir, c->start_dir, sizeof dir - 1);
    dir[sizeof dir - 1] = 0;
    c->stop_pending = c->start_pending = 0;
    pthread_mutex_unlock(&c->mu);

    if (stop) {
        if (w->recording && !w->stopping) {
            /* no new puts; keep draining until the encoder goes quiet,
             * then close (the tail packets otherwise never reach disk) */
            w->stopping = 1;
            w->empty_drains = 0;
            fprintf(stderr, "ctl: REC_STOP requested — draining encoder tail\n");
        }
    }
    if (start) {
        if (!w->recording) {
            char rawp[1024], ptsp[1024], pvraw[1024], pvpts[1024];
            mkdir(dir, 0755);
            snprintf(rawp, sizeof rawp, "%s/stitch.raw.h264", dir);
            snprintf(ptsp, sizeof ptsp, "%s/stitch.pts", dir);
            FILE *f = fopen(rawp, "wb");
            if (!f) {
                fprintf(stderr, "ctl: REC_START open %s failed: %s\n",
                        rawp, strerror(errno));
            } else {
                if (hdr_data && hdr_len)
                    fwrite(hdr_data, 1, hdr_len, f);
                w->fp_out = f;
                w->fp_pts = fopen(ptsp, "w");
                w->recording = 1;
                w->rec_bytes = hdr_len;
                pthread_mutex_lock(&c->mu);
                c->recording = 1;
                c->rec_start_us = now_us();
                strncpy(c->rec_dir, dir, sizeof c->rec_dir - 1);
                c->rec_dir[sizeof c->rec_dir - 1] = 0;
                pthread_mutex_unlock(&c->mu);
                /* proxy preview files: start with the SPS/PPS header, then
                 * the next IDR (forced above) */
                if (pv && pv->ok) {
                    snprintf(pvraw, sizeof pvraw, "%s/preview.raw.h264", dir);
                    snprintf(pvpts, sizeof pvpts, "%s/preview.pts", dir);
                    pv->fp_out = fopen(pvraw, "wb");
                    pv->fp_pts = fopen(pvpts, "w");
                    if (pv->fp_out && pv->hdr_data && pv->hdr_len)
                        fwrite(pv->hdr_data, 1, pv->hdr_len, pv->fp_out);
                    pv->mpi->control(pv->ctx, MPP_ENC_SET_IDR_FRAME, NULL);
                }
                enc_mpi->control(enc_ctx, MPP_ENC_SET_IDR_FRAME, NULL);
                fprintf(stderr, "ctl: REC_START -> %s\n", dir);
            }
        }
    }
    if (quit)
        g_running = 0;
}

static int ctl_handle_line(ctl_t *c, int fd, char *line) {
    /* returns 1 to keep the client, 0 to close it.
     * CALLER HOLDS c->mu (the ctl thread's read loop) — no locking here. */
    while (*line == ' ' || *line == '\t')
        line++;
    char *eol = strpbrk(line, "\r\n");
    if (eol)
        *eol = 0;
    if (!*line)
        return 1;
    if (!strncmp(line, "REC_START ", 10) && strlen(line) > 10) {
        char *dir = line + 10;
        while (*dir == ' ')
            dir++;
        if (!*dir) {
            ctl_reply(fd, "ERR missing dir");
            return 1;
        }
        int already = c->recording || c->start_pending;
        if (!already) {
            strncpy(c->start_dir, dir, sizeof c->start_dir - 1);
            c->start_dir[sizeof c->start_dir - 1] = 0;
            c->start_pending = 1;
        }
        ctl_reply(fd, already ? "ERR already recording" : "OK");
        return 1;
    }
    if (!strcmp(line, "REC_STOP")) {
        c->stop_pending = 1;
        ctl_reply(fd, c->recording ? "OK" : "OK idle");
        return 1;
    }
    if (!strcmp(line, "STATUS")) {
        if (c->status_len > 0 && c->status_len < (int)sizeof c->status_buf)
            send(fd, c->status_buf, (size_t)c->status_len, MSG_NOSIGNAL);
        return 1;
    }
    if (!strcmp(line, "QUIT")) {
        c->quit_pending = 1;
        ctl_reply(fd, "OK");
        return 1;
    }
    ctl_reply(fd, "ERR unknown command");
    return 1;
}

/* Power-loss safety (#21): the recording files are fflush()ed per packet, so
 * a process crash loses nothing, but a power cut loses whatever sits in the
 * page cache. Every ~2 s this thread opens each file of the live recording by
 * name and fdatasync()s it. It only touches files (never the encoder or the
 * main thread's FILE*s), so the CLAUDE.md main-thread rule does not apply. */
static void *sync_thread(void *arg) {
    ctl_t *c = arg;
    for (;;) {
        struct timespec ts = { 2, 0 };
        nanosleep(&ts, NULL);
        char dir[512];
        pthread_mutex_lock(&c->mu);
        int rec = c->recording;
        strncpy(dir, c->rec_dir, sizeof dir - 1);
        dir[sizeof dir - 1] = 0;
        pthread_mutex_unlock(&c->mu);
        if (rec && dir[0])
            sync_rec_files(dir);
    }
    return NULL;
}

static void *ctl_thread(void *arg) {
    ctl_t *c = arg;
    int64_t last_push = 0;
    for (;;) {
        fd_set rfds;
        FD_ZERO(&rfds);
        int maxfd = c->listen_fd;
        FD_SET(c->listen_fd, &rfds);
        pthread_mutex_lock(&c->mu);
        for (int i = 0; i < c->n_clients; i++) {
            FD_SET(c->clients[i].fd, &rfds);
            if (c->clients[i].fd > maxfd)
                maxfd = c->clients[i].fd;
        }
        int status_gen = c->status_gen;
        pthread_mutex_unlock(&c->mu);
        struct timeval tv = { .tv_sec = 0, .tv_usec = 200000 };
        int n = select(maxfd + 1, &rfds, NULL, NULL, &tv);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            break;   /* listen fd gone — process exiting */
        }
        if (FD_ISSET(c->listen_fd, &rfds)) {
            int fd = accept(c->listen_fd, NULL, NULL);
            if (fd >= 0) {
                int fl = fcntl(fd, F_GETFL, 0);
                fcntl(fd, F_SETFL, fl | O_NONBLOCK);
                pthread_mutex_lock(&c->mu);
                if (c->n_clients < 16) {
                    c->clients[c->n_clients].fd = fd;
                    c->clients[c->n_clients].len = 0;
                    c->n_clients++;
                } else {
                    close(fd);
                }
                pthread_mutex_unlock(&c->mu);
            }
        }
        pthread_mutex_lock(&c->mu);
        for (int i = 0; i < c->n_clients; ) {
            ctl_client_t *cl = &c->clients[i];
            if (FD_ISSET(cl->fd, &rfds)) {
                char tmp[512];
                ssize_t r = recv(cl->fd, tmp, sizeof tmp, 0);
                if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                    i++;
                    continue;    /* spurious wakeup — nothing to read */
                }
                if (r <= 0) {
                    close(cl->fd);
                    memmove(&c->clients[i], &c->clients[i + 1],
                            (size_t)(c->n_clients - i - 1) * sizeof *cl);
                    c->n_clients--;
                    continue;
                }
                for (ssize_t k = 0; k < r && cl->len < (int)sizeof cl->buf - 1; k++) {
                    cl->buf[cl->len++] = tmp[k];
                    if (tmp[k] == '\n') {
                        cl->buf[cl->len] = 0;
                        char line[1024];
                        strncpy(line, cl->buf, sizeof line - 1);
                        line[sizeof line - 1] = 0;
                        cl->len = 0;
                        if (!ctl_handle_line(c, cl->fd, line)) {
                            close(cl->fd);
                            memmove(&c->clients[i], &c->clients[i + 1],
                                    (size_t)(c->n_clients - i - 1) * sizeof *cl);
                            c->n_clients--;
                            break;
                        }
                    }
                }
            }
            i++;
        }
        /* push status to everyone when it changed and ~1 s has passed */
        if (status_gen != c->status_gen && now_us() - last_push > 500000) {
            last_push = now_us();
            for (int i = 0; i < c->n_clients; i++)
                if (c->status_len > 0)
                    send(c->clients[i].fd, c->status_buf,
                         (size_t)c->status_len, MSG_NOSIGNAL);
        }
        pthread_mutex_unlock(&c->mu);
    }
    return NULL;
}

static ctl_t *ctl_start(const char *sock_path) {
    ctl_t *c = calloc(1, sizeof *c);
    if (!c)
        return NULL;
    pthread_mutex_init(&c->mu, NULL);
    c->listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (c->listen_fd < 0) {
        free(c);
        return NULL;
    }
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s", sock_path);
    unlink(sock_path);               /* stale socket from a dead instance */
    if (bind(c->listen_fd, (struct sockaddr *)&addr, sizeof addr) < 0 ||
        listen(c->listen_fd, 8) < 0) {
        fprintf(stderr, "ctl: bind %s failed: %s\n", sock_path,
                strerror(errno));
        close(c->listen_fd);
        free(c);
        return NULL;
    }
    chmod(sock_path, 0660);
    pthread_t th;
    pthread_create(&th, NULL, ctl_thread, c);
    pthread_t sth;
    pthread_create(&sth, NULL, sync_thread, c);
    fprintf(stderr, "ctl: listening on %s\n", sock_path);
    return c;
}

/* ========================= live mode ========================= */

static void usage(const char *argv0) {
    fprintf(stderr,
        "usage:\n"
        "  %s <dev0> <dev1> <params_dir> <out.raw.h264> [frames] [width] [height]\n"
        "     [fps] [exposure] [wall_sec] [shader.spv]\n"
        "  %s -src <params_dir> <cam0.nv12> <cam1.nv12> <out.nv12> [n] [shader.spv]\n",
        argv0, argv0);
}

int main(int argc, char **argv) {
    if (argc >= 3 && !strcmp(argv[1], "--probe")) {
        /* Vulkan+shader+params init only: "OK" + exit 0 when live stitch
         * is available (the engine's fallback gate). */
        const char *pdir = argv[2];
        const char *spv = argc > 3 ? argv[3] : "afl_cam/native/stitch_nv12.spv";
        char path[1024];
        size_t len;
        void *pd = read_file((snprintf(path, sizeof path, "%s/params.bin", pdir), path), &len);
        if (len != 64 * sizeof(float)) { fprintf(stderr, "params.bin size mismatch\n"); return 1; }
        void *w0d = read_file((snprintf(path, sizeof path, "%s/w0.bin", pdir), path), &len);
        void *w1d = read_file((snprintf(path, sizeof path, "%s/w1.bin", pdir), path), &len);
        void *m0d = read_file((snprintf(path, sizeof path, "%s/map0.bin", pdir), path), &len);
        void *m1d = read_file((snprintf(path, sizeof path, "%s/map1.bin", pdir), path), &len);
        gpu_t g = {0};
        gpu_init(&g, spv, pd, w0d, w1d, m0d, m1d);
        printf("OK\n");
        return 0;
    }
    if (argc >= 2 && !strcmp(argv[1], "-src")) {
        if (argc < 6) { usage(argv[0]); return 1; }
        return run_src_mode(argv[2], argv[3], argv[4], argv[5],
                            argc > 6 ? atoi(argv[6]) : 1,
                            argc > 7 ? argv[7] : "afl_cam/native/stitch_nv12.spv");
    }
    if (argc < 5) { usage(argv[0]); return 1; }
    const char *dev0 = argv[1], *dev1 = argv[2];
    const char *params_dir = argv[3];
    const char *out_path = argv[4];
    RK_S32 frames = argc > 5 ? atoi(argv[5]) : 0;
    RK_U32 width = argc > 6 ? atoi(argv[6]) : 3840;
    RK_U32 height = argc > 7 ? atoi(argv[7]) : 2160;
    RK_U32 fps = argc > 8 ? atoi(argv[8]) : 30;
    RK_S32 exposure = argc > 9 ? atoi(argv[9]) : 0;
    RK_S32 wall_sec = argc > 10 ? atoi(argv[10]) : 0;
    const char *spv_path = argc > 11 ? argv[11] : "afl_cam/native/stitch_nv12.spv";

    signal(SIGINT, sig_handler);
    signal(SIGSEGV, crash_handler);
    signal(SIGABRT, crash_handler);
    signal(SIGTERM, sig_handler);
    signal(SIGPIPE, SIG_IGN);   /* send() on a dead ctl client must not kill us */

    /* daemon mode: afl-camd supervises us — standby until REC_START */
    int daemon_mode = getenv("AFL_CAM_CTL_SOCK") != NULL;
    const char *snap_dir = getenv("AFL_CAM_PREVIEW_DIR");

    RK_S32 ret = 0;
    char path[1024];
    size_t len;
    void *params_data = read_file((snprintf(path, sizeof path, "%s/params.bin", params_dir), path), &len);
    if (len != 64 * sizeof(float)) { fprintf(stderr, "params.bin size mismatch\n"); return 1; }
    void *w0d = read_file((snprintf(path, sizeof path, "%s/w0.bin", params_dir), path), &len);
    void *w1d = read_file((snprintf(path, sizeof path, "%s/w1.bin", params_dir), path), &len);
    void *m0d = read_file((snprintf(path, sizeof path, "%s/map0.bin", params_dir), path), &len);
    void *m1d = read_file((snprintf(path, sizeof path, "%s/map1.bin", params_dir), path), &len);

    /* ---- GPU init BEFORE the cameras: fail fast if Vulkan is broken ---- */
    gpu_t g = {0};
    gpu_init(&g, spv_path, params_data, w0d, w1d, m0d, m1d);
    void *tight = malloc(g.tight_size);
    if (!tight) { fprintf(stderr, "tight alloc failed\n"); return 1; }

    /* ---- cameras ---- */
    cam_t cams[2] = {0};
    cams[0].dev = dev0; cams[1].dev = dev1;
    for (int i = 0; i < 2; i++) {
        cam_t *c = &cams[i];
        c->width = width; c->height = height; c->fps = fps; c->exposure = exposure;
        c->lock_pending = (exposure <= 0);
        c->lock_exp = 166;
        /* live preview: <outdir>/cam<i>_preview.jpg every AFL_CAM_PREVIEW_SECS
         * (or AFL_CAM_PREVIEW_DIR when set — daemon mode has no outdir yet) */
        if (snap_dir && *snap_dir)
            snprintf(c->preview_path, sizeof c->preview_path,
                     "%s/cam%d_preview.jpg", snap_dir, i);
        else {
            snprintf(c->preview_path, sizeof c->preview_path, "%s", out_path);
            char *slash = strrchr(c->preview_path, '/');
            if (slash)
                snprintf(slash + 1, sizeof c->preview_path - (slash + 1 - c->preview_path),
                         "cam%d_preview.jpg", i);
            else
                snprintf(c->preview_path, sizeof c->preview_path,
                         "cam%d_preview.jpg", i);
        }
        c->snap_interval_us = 200000LL;   /* 5 fps; tune with AFL_CAM_PREVIEW_SECS */
        if (getenv("AFL_CAM_PREVIEW_SECS"))
            c->snap_interval_us =
                (int64_t)(atof(getenv("AFL_CAM_PREVIEW_SECS")) * 1e6);
        c->last_snap = -c->snap_interval_us;
        pthread_mutex_init(&c->ring.mu, NULL);
        pthread_cond_init(&c->ring.cv_ready, NULL);
        pthread_cond_init(&c->ring.cv_free, NULL);
        if (v4l2_open(c) < 0) return 1;
        if (dec_init(c) < 0) { fprintf(stderr, "%s: dec init failed\n", c->dev); return 1; }
    }
    cma_print("after cameras");

    /* ---- encoder (composite canvas) ---- */
    MppCtx enc_ctx = NULL;
    MppApi *enc_mpi = NULL;
    ret = mpp_create(&enc_ctx, &enc_mpi);
    if (ret) { fprintf(stderr, "mpp_create enc failed\n"); return 1; }
    /* Codec: HEVC default (see cam2h264 --bench); H.264 via AFL_CAM_CODEC=h264 */
    int use_hevc = !(getenv("AFL_CAM_CODEC") && !strcmp(getenv("AFL_CAM_CODEC"), "h264"));
    ret = mpp_init(enc_ctx, MPP_CTX_ENC,
                   use_hevc ? MPP_VIDEO_CodingHEVC : MPP_VIDEO_CodingAVC);
    if (ret) { fprintf(stderr, "mpp_init enc failed\n"); return 1; }
    MppEncCfg enc_cfg = NULL;
    mpp_enc_cfg_init(&enc_cfg);
    enc_mpi->control(enc_ctx, MPP_ENC_GET_CFG, enc_cfg);
    RK_U32 ehs = MPP_ALIGN(g.canvas_w, 16);
    RK_U32 evs = MPP_ALIGN(g.canvas_h, 16);
    {
        /* Encode bitrate (kbps). The 5304-wide composite canvas holds 2.3x
         * a 4K frame's pixels; tune with AFL_CAM_STITCH_BITRATE_KBPS. */
        RK_S32 bps = 80000;
        if (getenv("AFL_CAM_STITCH_BITRATE_KBPS"))
            bps = atoi(getenv("AFL_CAM_STITCH_BITRATE_KBPS"));
        const char *keys[8] = { "prep:width", "prep:height", "prep:hor_stride",
                                "prep:ver_stride", "prep:format", "rc:bps_target",
                                "rc:bps_min", "rc:bps_max" };
        RK_S32 vals[8] = { (RK_S32)g.canvas_w, (RK_S32)g.canvas_h,
                           (RK_S32)ehs, (RK_S32)evs, MPP_FMT_YUV420SP,
                           bps * 1000, (RK_S32)(bps * 950L), (RK_S32)(bps * 1050L) };
        for (int i = 0; i < 8; i++)
            mpp_enc_cfg_set_s32(enc_cfg, keys[i], vals[i]);
        mpp_enc_cfg_set_s32(enc_cfg, "codec:type",
                            use_hevc ? MPP_VIDEO_CodingHEVC : MPP_VIDEO_CodingAVC);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:mode", MPP_ENC_RC_MODE_CBR);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:gop", fps);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_in_flex", 0);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_in_num", fps);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_in_denorm", 1);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_out_flex", 0);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_out_num", fps);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_out_denorm", 1);
        /* (prep:header_mode is NOT set — the cfg write fails ret -1 on
         * rkvenc and appears to leave the encoder emitting parameter-set
         * packets with a broken lifecycle: per-frame double-release spam.
         * The SPS/PPS header is captured once and written per file at
         * REC_START instead.) */
        if (!use_hevc) {
            mpp_enc_cfg_set_s32(enc_cfg, "h264:profile", 100);
            mpp_enc_cfg_set_s32(enc_cfg, "h264:level", 52);
            mpp_enc_cfg_set_s32(enc_cfg, "h264:cabac_en", 1);
            mpp_enc_cfg_set_s32(enc_cfg, "h264:trans8x8", 1);
        }
    }
    ret = enc_mpi->control(enc_ctx, MPP_ENC_SET_CFG, enc_cfg);
    if (ret) { fprintf(stderr, "enc set cfg failed %d\n", ret); return 1; }
    cma_print("after SET_CFG");

    const int ENC_RING = 1;
    MppBufferGroup enc_pkt_grp = NULL;
    ret = mpp_buffer_group_get_internal(&enc_pkt_grp, MPP_BUFFER_TYPE_DRM);
    /* 8 buffers: 4 for the rotating output packets + headroom so
     * mpp_packet_init_with_buffer never fails while packets are in flight
     * (a 4-buffer limit exhausted → the SPS/PPS header get fails and
     * encoder packets alias stale memory — corrupt CABAC streams) */
    ret |= mpp_buffer_group_limit_config(enc_pkt_grp, 4 * 1024 * 1024, 8);
    if (ret) { fprintf(stderr, "enc pkt grp failed %d\n", ret); return 1; }
    MppBuffer enc_out_buf[4] = {0};
    for (int i = 0; i < ENC_RING; i++) {
        ret = mpp_buffer_get(enc_pkt_grp, &enc_out_buf[i], 4 * 1024 * 1024);
        if (ret) { fprintf(stderr, "enc out buffer get failed %d\n", ret); return 1; }
    }

    /* encoder INPUT frame group (canvas-sized buffers exceed the packet
     * group's 4MB limit — own group, ENC_RING buffers for async pipeline) */
    MppBufferGroup enc_in_grp = NULL;
    ret = mpp_buffer_group_get_internal(&enc_in_grp, MPP_BUFFER_TYPE_DRM);
    ret |= mpp_buffer_group_limit_config(enc_in_grp, ehs * evs * 3 / 2, 4);
    if (ret) { fprintf(stderr, "enc in grp failed %d\n", ret); return 1; }


    /* master output: opened at startup in legacy mode; opened per
     * REC_START in daemon mode (the ctl section swaps the pointers) */
    FILE *fp_out = NULL;
    FILE *fp_pts = NULL;
    if (!daemon_mode) {
        fp_out = fopen(out_path, "wb");
        if (!fp_out) { perror(out_path); return 1; }
        char pts_path[1024];
        char *dot;
        snprintf(pts_path, sizeof pts_path, "%s", out_path);
        dot = strstr(pts_path, ".raw.h264");
        if (dot)
            strcpy(dot, ".pts");
        else
            strncat(pts_path, ".pts", sizeof pts_path - strlen(pts_path) - 1);
        fp_pts = fopen(pts_path, "w");
    }

    /* SPS/PPS header (cam2h264.c) — captured ONCE at init; legacy mode
     * writes it to the output file now, daemon mode replays it into every
     * recording's file on REC_START (the header get only works cleanly
     * before frames flow) */
    void *hdr_data = NULL;
    size_t hdr_len = 0;
    {
        MppBuffer hdr_buf = NULL;
        MppPacket hdr = NULL;
        ret = mpp_buffer_get(enc_pkt_grp, &hdr_buf, 1024 * 1024);
        if (!ret)
            mpp_packet_init_with_buffer(&hdr, hdr_buf);
        if (hdr) {
            mpp_packet_set_length(hdr, 0);
            ret = enc_mpi->control(enc_ctx, MPP_ENC_GET_HDR_SYNC, hdr);
            if (ret) fprintf(stderr, "GET_HDR_SYNC failed %d\n", ret);
            if (!ret) {
                void *ptr = mpp_packet_get_pos(hdr);
                hdr_len = mpp_packet_get_length(hdr);
                if (ptr && hdr_len) {
                    hdr_data = malloc(hdr_len);
                    if (hdr_data) {
                        memcpy(hdr_data, ptr, hdr_len);
                        if (fp_out)
                            fwrite(hdr_data, 1, hdr_len, fp_out);
                    } else {
                        hdr_len = 0;
                    }
                }
            }
            mpp_packet_deinit(&hdr);
        }
        if (hdr_buf)
            mpp_buffer_put(hdr_buf);
    }
    if (!hdr_data)
        fprintf(stderr, "WARNING: no SPS/PPS header captured\n");

    /* ---- encoder input frames (ENC_RING deep — the worker fills frame i
     * while the encoder still owns i-1..i-3) ---- */
    MppFrame enc_in[4] = {0};
    MppBuffer enc_in_buf[4] = {0};
    for (int i = 0; i < ENC_RING; i++) {
        mpp_frame_init(&enc_in[i]);
        mpp_frame_set_width(enc_in[i], g.canvas_w);
        mpp_frame_set_height(enc_in[i], g.canvas_h);
        mpp_frame_set_hor_stride(enc_in[i], ehs);
        mpp_frame_set_ver_stride(enc_in[i], evs);
        mpp_frame_set_fmt(enc_in[i], MPP_FMT_YUV420SP);
        cma_print("before enc_in get");
        ret = mpp_buffer_get(enc_in_grp, &enc_in_buf[i], ehs * evs * 3 / 2);
        cma_print("after enc_in get");
        if (ret) { fprintf(stderr, "enc in buffer get failed %d\n", ret); return 1; }
        mpp_frame_set_buffer(enc_in[i], enc_in_buf[i]);
    }

    /* ---- preview encoder (daemon mode, AFL_CAM_PREVIEW_UDP set) ----
     * H.264 ~1600x564 (canvas aspect) CBR — the phone preview + proxy.
     * Init BEFORE the worker starts: the worker downscales into its stage. */
    pv_t pv = {0};
    int pv_ok = 0;
    if (getenv("AFL_CAM_PREVIEW_UDP")) {
        int pv_w = 1600;
        if (getenv("AFL_CAM_PREVIEW_VIDEO_W"))
            pv_w = atoi(getenv("AFL_CAM_PREVIEW_VIDEO_W"));
        if (pv_w > 2048)
            pv_w = 2048;    /* well past any phone preview */
        int pv_h = (int)((int64_t)pv_w * g.canvas_h / g.canvas_w) & ~1;
        if (pv_h < 2)
            pv_h = 2;
        if (getenv("AFL_CAM_PREVIEW_VIDEO_H"))
            pv_h = atoi(getenv("AFL_CAM_PREVIEW_VIDEO_H"));
        int pv_bps = 3500;
        if (getenv("AFL_CAM_PREVIEW_BITRATE_KBPS"))
            pv_bps = atoi(getenv("AFL_CAM_PREVIEW_BITRATE_KBPS"));
        if (pv_init(&pv, pv_w, pv_h, (int)fps, pv_bps))
            fprintf(stderr, "preview: rkvenc init failed — no stitched preview\n");
        else {
            pv_ok = 1;
            fprintf(stderr, "preview: %dx%d H.264 CBR %d kbps%s\n",
                    pv.w, pv.h, pv_bps,
                    pv.udp_ok ? "" : " (UDP sink unavailable)");
        }
    }

    /* ---- start capture threads ---- */
    int64_t t_start = now_us();
    int64_t t_deadline = wall_sec > 0 ? t_start + (int64_t)wall_sec * 1000000 : 0;
    for (int i = 0; i < 2; i++)
        pthread_create(&cams[i].thread, NULL, capture_thread, &cams[i]);

    /* ---- pairing + GPU + encode loop (cam0 drives) ---- */
    int64_t prev1_pts = -1;
    int prev1_idx = -1;         /* retained cam1 frame (slot idx) */
    int cur1_idx = -1;
    int64_t cur1_pts = -1;
    MppFrame cur1 = NULL;
    int out_count = 0;
    int skipped_start = 0, dup_count = 0, skip_count = 0;
    int64_t skew_sum = 0, skew_n = 0;

    /* ---- pipelined worker (repack + encode) ---- */
    worker_ctx_t w = {0};
    w.g = &g;
    w.q = &(work_q_t){ .mu = PTHREAD_MUTEX_INITIALIZER };
    w.enc_ctx = enc_ctx;
    w.enc_mpi = enc_mpi;
    w.use_hevc = use_hevc;
    for (int i = 0; i < ENC_RING; i++) {
        w.enc_in[i] = enc_in[i];
        w.enc_in_buf[i] = enc_in_buf[i];
        w.enc_out_buf[i] = enc_out_buf[i];
    }
    w.ehs = ehs; w.evs = evs;
    w.fp_out = fp_out; w.fp_pts = fp_pts;
    w.recording = !daemon_mode;     /* legacy records from the first frame */
    w.rec_bytes = hdr_len;
    w.pv = pv_ok ? &pv : NULL;
    pthread_cond_init(&w.q->cv, NULL);
    pthread_cond_init(&w.ack_cv, NULL);
    pthread_mutex_init(&w.ack_mu, NULL);
    for (int k = 0; k < 3; k++)
        w.ack_done[k] = 1;   /* nothing pending before the first submits */
    pthread_t wthread;
    pthread_create(&wthread, NULL, encode_worker, &w);

    /* ---- daemon control socket (AFL_CAM_CTL_SOCK) ---- */
    ctl_t *ctl = NULL;
    const char *ctl_sock_path = NULL;
    if (daemon_mode) {
        ctl_sock_path = getenv("AFL_CAM_CTL_SOCK");
        ctl = ctl_start(ctl_sock_path);
        if (!ctl)
            fprintf(stderr, "ctl: control socket unavailable — recording "
                            "cannot be toggled remotely\n");
    }

    /* per-GPU-slot pending frame info (pts assigned at submit time, pushed
     * to the worker when the slot's result comes back 3 frames later) */
    int64_t pend_pts[3] = { -1, -1, -1 };

    /* wait for cam1's first frame to learn the start trim */
    cur1_idx = ring_take(&cams[1].ring, &cur1, &cur1_pts);

    /* main-loop stage timers (us, summed over 10 iterations, logged with
     * the "submitted" line): where the frame period goes */
    int64_t acc_take = 0, acc_fence = 0, acc_ack = 0, acc_copy = 0, acc_enc = 0, acc_iter = 0;
    int64_t acc_upw = 0;
    uploader_t up = { .g = &g };
    pthread_mutex_init(&up.mu, NULL);
    pthread_cond_init(&up.cv, NULL);
    pthread_t upthread;
    pthread_mutex_init(&up.mu2, NULL);
    pthread_cond_init(&up.cv2, NULL);
    pthread_t uphelper;
    pthread_create(&uphelper, NULL, upload_helper, &up);
    pthread_create(&upthread, NULL, upload_thread, &up);
    int up_pending = 0, up_f0 = -1;
    /* wait for the in-flight upload, then release its cam0 frame */
#define UPLOAD_WAIT() do { \
        if (up_pending) { \
            int64_t _w0 = now_us(); \
            pthread_mutex_lock(&up.mu); \
            while (!up.done) \
                pthread_cond_wait(&up.cv, &up.mu); \
            pthread_mutex_unlock(&up.mu); \
            ring_release(&cams[0].ring, up_f0); \
            up_pending = 0; \
            acc_upw += now_us() - _w0; \
            acc_copy += up.last_us; \
        } \
    } while (0)
    while (g_running && (frames == 0 || out_count < frames) &&
           !(t_deadline && now_us() > t_deadline)) {
        int64_t t_top = now_us();
        /* cam0 frame */
        int64_t p0_pts;
        MppFrame f0 = NULL;
        int f0_idx = ring_take(&cams[0].ring, &f0, &p0_pts);
        if (f0_idx < 0)
            break;
        /* the previous pair's copy must finish before the cam1 cursor
         * moves on (its source buffers may be released below) */
        UPLOAD_WAIT();

        if (cur1_idx < 0 || p0_pts < cams[1].ring.first_pts) {
            /* cam1 not started yet — start trim */
            ring_release(&cams[0].ring, f0_idx);
            skipped_start++;
            continue;
        }

        /* advance the cam1 cursor: pull frames until cur1.pts > p0 */
        while (cur1 && cur1_pts <= p0_pts) {
            if (prev1_idx >= 0)
                ring_release(&cams[1].ring, prev1_idx);
            prev1_pts = cur1_pts;
            prev1_idx = cur1_idx;
            cur1_idx = ring_take(&cams[1].ring, &cur1, &cur1_pts);
            if (cur1_idx < 0)
                break;      /* cam1 stream ended → e_cut */
            skip_count++;
        }
        if (cur1_idx < 0) {
            /* cam1 ended: e_cut trim */
            ring_release(&cams[0].ring, f0_idx);
            break;
        }

        /* pick the nearer of prev/cur — ties keep the upper (cur),
         * matching the offline |lo| < |pair| comparison */
        int pair_idx;
        int64_t pair_pts;
        if (prev1_idx < 0 || (p0_pts - prev1_pts) < (cur1_pts - p0_pts)) {
            if (prev1_idx < 0) { pair_idx = cur1_idx; pair_pts = cur1_pts; }
            else { pair_idx = prev1_idx; pair_pts = prev1_pts; dup_count++; }
        } else {
            pair_idx = cur1_idx;
            pair_pts = cur1_pts;
        }

        /* ---- pipelined GPU stage (main thread) ---- */
        int64_t mid = (p0_pts + pair_pts) / 2;
        skew_sum += p0_pts > pair_pts ? p0_pts - pair_pts : pair_pts - p0_pts;
        skew_n++;
        int64_t t_stage = now_us();
        acc_take += t_stage - t_top;
        /* our cached mmap of the decoder buffers (MPP's own mapping is
         * uncached — the 24.9MB staging copy would take ~200ms) */
        void *y0_ptr = cams[0].ring.slots[f0_idx].map_ptr ?
                       cams[0].ring.slots[f0_idx].map_ptr :
                       mpp_buffer_get_ptr(cams[0].ring.slots[f0_idx].buf);
        void *y1_ptr = cams[1].ring.slots[pair_idx].map_ptr ?
                       cams[1].ring.slots[pair_idx].map_ptr :
                       mpp_buffer_get_ptr(cams[1].ring.slots[pair_idx].buf);
        gpu_slot_t *s = &g.slots[g.k];
        int k = g.k;
        /* GPU job for frame i-3 is done → its packed SSBO is readable */
        int64_t t_f0 = now_us();
        CHECK(vkWaitForFences(g.dev, 1, &s->fence, VK_TRUE, UINT64_MAX));
        CHECK(vkResetFences(g.dev, 1, &s->fence));
        {
            VkMappedMemoryRange mr = { .sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
                                       .memory = s->ssbo_mem, .offset = 0, .size = g.ssbo_size };
            vkInvalidateMappedMemoryRanges(g.dev, 1, &mr);
        }
        acc_fence += now_us() - t_f0;
        /* the slot's previous result (frame i-3) — hand it to the
         * worker for repack, then wait before the GPU overwrites it */
        int64_t put_pts = pend_pts[k];
        pthread_mutex_lock(&w.q->mu);
        w.q->items[w.q->tail % 16] = (work_item){ k, put_pts, w.recording && !w.stopping };
        w.q->tail++; w.q->count++;
        pthread_cond_signal(&w.q->cv);
        pthread_mutex_unlock(&w.q->mu);
        /* the worker must finish repacking slot k's SSBO before the GPU
         * overwrites it with this frame's job (usually already done) */
        int64_t t_a0 = now_us();
        pthread_mutex_lock(&w.ack_mu);
        while (!w.ack_done[k])
            pthread_cond_wait(&w.ack_cv, &w.ack_mu);
        w.ack_done[k] = 0;
        pthread_mutex_unlock(&w.ack_mu);
        int64_t t_c0 = now_us();
        acc_ack += t_c0 - t_a0;
        /* hand this frame's pair to the upload thread (copy + flush +
         * submit); the encode below overlaps it */
        pthread_mutex_lock(&up.mu);
        up.k = k;
        up.src0 = y0_ptr;
        up.src1 = y1_ptr;
        up.buf0 = cams[0].ring.slots[f0_idx].buf;
        up.buf1 = cams[1].ring.slots[pair_idx].buf;
        up.done = 0;
        up.has_job = 1;
        pthread_cond_signal(&up.cv);
        pthread_mutex_unlock(&up.mu);
        up_pending = 1;
        up_f0 = f0_idx;
        pend_pts[k] = mid;
        g.k = (g.k + 1) % RING_SLOTS;
        int64_t t_e0 = now_us();
        /* encode frame i-3 (repacked by the worker into enc_in[0]) on
         * the MAIN thread — the proven-clean sync path. The drain
         * returns immediately: the encoder finished ~30ms ago. */
        if (put_pts >= 0) {
            if (w.recording && !w.stopping) {
                mpp_frame_set_pts(enc_in[0], put_pts);
                worker_put_frame(&w, enc_in[0], enc_out_buf[0]);
                worker_drain(&w, put_pts);
            }
            /* stitched preview (daemon): every canvas frame, on the
             * main thread — UDP best-effort + proxy while recording */
            if (pv_ok)
                pv_put_drain(&pv, put_pts);
        }
        acc_enc += now_us() - t_e0;
        if (w.recording && w.stopping) {
            /* draining the encoder tail: no new puts. Outside the
             * put_pts guard so a stop inside the first 3 frames also
             * drains and closes (put_pts < 0 there). */
            int got = worker_drain(&w, put_pts);
            if (got == 0) {
                if (++w.empty_drains >= 3)
                    ctl_finish_stop(ctl, &w, pv_ok ? &pv : NULL);
            } else {
                w.empty_drains = 0;
            }
        }
        out_count++;
        acc_iter += now_us() - t_top;
        if (out_count % 10 == 0) {
            fprintf(stderr, "submitted %d frames (main %.1fms, encoded %d)\n",
                    out_count, (now_us() - t_stage) / 1000.0, w.enc_count);
            /* per-iteration averages over the last 10: pair/wait for cameras,
             * GPU fence, worker ack, copy+submit, encode/preview puts, total */
            fprintf(stderr, "loop-ms take %.1f fence %.1f ack %.1f copy %.1f upwait %.1f enc %.1f iter %.1f\n",
                    acc_take / 10000.0, acc_fence / 10000.0, acc_ack / 10000.0,
                    acc_copy / 10000.0, acc_upw / 10000.0, acc_enc / 10000.0,
                    acc_iter / 10000.0);
            acc_take = acc_fence = acc_ack = acc_copy = acc_upw = acc_enc = acc_iter = 0;
        }

        /* daemon: publish status + apply pending REC_START/REC_STOP/QUIT
         * (bottom of the iteration — a stop flushes the last packets) */
        ctl_publish(ctl, cams, &w, pv_ok ? &pv : NULL, out_count, dup_count,
                    skip_count, skipped_start, skew_sum, skew_n);
        ctl_apply_pending(ctl, &w, pv_ok ? &pv : NULL, enc_ctx, enc_mpi,
                          hdr_data, hdr_len);
    }

    /* ---- stop + EOS (main thread — the old proven-clean path) ---- */
    UPLOAD_WAIT();
    pthread_mutex_lock(&up.mu);
    up.stop = 1;
    pthread_cond_signal(&up.cv);
    pthread_mutex_unlock(&up.mu);
    pthread_join(upthread, NULL);
    pthread_mutex_lock(&up.mu2);
    up.stop2 = 1;
    pthread_cond_signal(&up.cv2);
    pthread_mutex_unlock(&up.mu2);
    pthread_join(uphelper, NULL);
    g_running = 0;
    for (int i = 0; i < 2; i++) {
        pthread_cond_signal(&cams[i].ring.cv_free);
        pthread_cond_signal(&cams[i].ring.cv_ready);
    }
    pthread_mutex_lock(&w.q->mu);
    w.q->stop = 1;
    pthread_cond_signal(&w.q->cv);
    pthread_mutex_unlock(&w.q->mu);
    pthread_join(wthread, NULL);
    {
        MppFrame ef = enc_in[0];
        mpp_frame_set_eos(ef, 1);
        worker_put_frame(&w, ef, enc_out_buf[0]);
        worker_drain(&w, 0);
    }
    for (int i = 0; i < 2; i++)
        pthread_join(cams[i].thread, NULL);
    /* flush + close whatever recording files are still open (daemon mode
     * may quit mid-recording — afl-camd muxes what was written) */
    if (w.fp_out)
        fclose(w.fp_out);
    if (w.fp_pts)
        fclose(w.fp_pts);
    if (pv_ok) {
        if (pv.fp_out)
            fclose(pv.fp_out);
        if (pv.fp_pts)
            fclose(pv.fp_pts);
    }
    if (ctl && ctl_sock_path)
        unlink(ctl_sock_path);
    fprintf(stderr, "submitted %d frames, encoded %d (start-trimmed %d, dup %d, skip %d)\n",
            out_count, w.enc_count, skipped_start, dup_count, skip_count);
    return 0;
}
