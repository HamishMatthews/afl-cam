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
 * GPU loop (see live_stitch_plan.md): a PRE-RECORDED 3-slot command-buffer
 * ring — nothing is re-recorded after init, sidestepping the libmali quirk
 * where later vkBeginCommandBuffer recordings silently discard earlier
 * ones. Each slot cb is SELF-CONTAINED (weight uploads + initial barriers +
 * frame upload + dispatch). Hard-won libmali g24p0 facts (see S1 commit):
 * 8-bit byte stores of texture-derived values crash/miscompile the LLVM
 * backend; high register pressure (4x2-pixel threads) silently miscompiles
 * — so: ONE THREAD PER CANVAS PIXEL, uint32 SSBO output (one byte per
 * uint, repacked on the CPU), no shared memory, no barriers, no
 * floatBitsToUint. Fences must be created pre-signaled.
 */

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
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

/* Mean luma of an NV12 frame over an 8x8 subsample — cheap brightness proxy
 * for the auto-settle-then-lock exposure loop (copied from cam2h264.c). */
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
    g->ssbo_size = (VkDeviceSize)g->canvas_w * g->canvas_h * 8;  /* byte per uint, padded */
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
        /* one thread per canvas pixel */
        vkCmdDispatch(s->cb, (g->canvas_w + 15) / 16, (g->canvas_h + 15) / 16, 1);
        CHECK(vkEndCommandBuffer(s->cb));
    }
    g->k = 0;
    fprintf(stderr, "gpu: %u x %u canvas, %d pre-recorded cbs\n",
            g->canvas_w, g->canvas_h, RING_SLOTS);
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

    /* repack: SSBO holds one byte per uint32 in NV12 order — Y plane first,
     * then interleaved UV (cw*ch/2 uints) */
    uint32_t *o32 = s->ssbo_map;
    uint8_t *out = tight_out;
    for (size_t i = 0; i < (size_t)g->canvas_w * g->canvas_h; i++)
        out[i] = (uint8_t)o32[i];
    for (size_t i = 0; i < (size_t)g->canvas_w * g->canvas_h / 2; i++)
        out[(size_t)g->canvas_w * g->canvas_h + i] =
            (uint8_t)o32[(size_t)g->canvas_w * g->canvas_h + i];
    g_times[4] = now_us() - t4;
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
    /* exposure state machine (cam2h264.c) */
    int lock_pending, lock_phase, refine_left, refine_rounds;
    double target_luma, luma_acc;
    int luma_n;
    int64_t prev_pts;
    int64_t dt_sum;
    int dt_n;
    RK_S32 lock_exp;
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
        /* this camera's auto mode is menu value 3 (cam2h264.c) */
        set_exposure(fd, 3, 0);
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
                c->lock_pending = 0;
                fprintf(stderr, "%s: exposure: auto needs %.1fms (>33ms) — "
                        "staying in auto for brightness\n",
                        c->dev, (c->dt_sum / c->dt_n) / 1000.0);
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
        int idx = ring_wait_slot(&c->ring, 0);
        if (idx < 0)
            break;
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
        if (r) { slot->state = 0; continue; }

        MppFrame dec_frame = NULL;
        r = c->dec_mpi->decode_get_frame(c->dec_ctx, &dec_frame);
        if (r || !dec_frame) { slot->state = 0; continue; }
        if (mpp_frame_get_info_change(dec_frame)) {
            c->dec_mpi->control(c->dec_ctx, MPP_DEC_SET_INFO_CHANGE_READY, NULL);
            mpp_frame_deinit(&dec_frame);
            slot->state = 0;
            continue;
        }
        if (mpp_frame_get_errinfo(dec_frame)) {
            mpp_frame_deinit(&dec_frame);
            slot->state = 0;
            continue;
        }
        c->count++;
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
    signal(SIGTERM, sig_handler);

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
    int nogpu = getenv("CAM2STITCH_NOGPU") != NULL;
    if (nogpu) {
        /* still parse the canvas dims so the encoder config is valid */
        float *pf0 = params_data;
        g.canvas_w = (uint32_t)pf0[0]; g.canvas_h = (uint32_t)pf0[1];
        g.src_w = (uint32_t)pf0[2]; g.src_h = (uint32_t)pf0[3];
        g.tight_size = (size_t)g.canvas_w * g.canvas_h * 3 / 2;
        fprintf(stderr, "CAM2STITCH_NOGPU: skipping GPU init (diagnostic)\n");
    } else {
        gpu_init(&g, spv_path, params_data, w0d, w1d, m0d, m1d);
    }
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
    ret = mpp_init(enc_ctx, MPP_CTX_ENC, MPP_VIDEO_CodingAVC);
    if (ret) { fprintf(stderr, "mpp_init enc failed\n"); return 1; }
    MppEncCfg enc_cfg = NULL;
    mpp_enc_cfg_init(&enc_cfg);
    enc_mpi->control(enc_ctx, MPP_ENC_GET_CFG, enc_cfg);
    RK_U32 ehs = MPP_ALIGN(g.canvas_w, 16);
    RK_U32 evs = MPP_ALIGN(g.canvas_h, 16);
    {
        const char *keys[8] = { "prep:width", "prep:height", "prep:hor_stride",
                                "prep:ver_stride", "prep:format", "rc:bps_target",
                                "rc:bps_min", "rc:bps_max" };
        RK_S32 vals[8] = { (RK_S32)g.canvas_w, (RK_S32)g.canvas_h,
                           (RK_S32)ehs, (RK_S32)evs, MPP_FMT_YUV420SP,
                           30000000, 28500000, 31500000 };
        for (int i = 0; i < 8; i++)
            mpp_enc_cfg_set_s32(enc_cfg, keys[i], vals[i]);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:mode", MPP_ENC_RC_MODE_CBR);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:gop", fps);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_in_flex", 0);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_in_num", fps);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_in_denorm", 1);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_out_flex", 0);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_out_num", fps);
        mpp_enc_cfg_set_s32(enc_cfg, "rc:fps_out_denorm", 1);
        mpp_enc_cfg_set_s32(enc_cfg, "h264:profile", 100);
        mpp_enc_cfg_set_s32(enc_cfg, "h264:level", 52);
        mpp_enc_cfg_set_s32(enc_cfg, "h264:cabac_en", 1);
        mpp_enc_cfg_set_s32(enc_cfg, "h264:trans8x8", 1);
    }
    ret = enc_mpi->control(enc_ctx, MPP_ENC_SET_CFG, enc_cfg);
    if (ret) { fprintf(stderr, "enc set cfg failed %d\n", ret); return 1; }
    cma_print("after SET_CFG");

    MppBufferGroup enc_pkt_grp = NULL;
    ret = mpp_buffer_group_get_internal(&enc_pkt_grp, MPP_BUFFER_TYPE_DRM);
    ret |= mpp_buffer_group_limit_config(enc_pkt_grp, 4 * 1024 * 1024, 4);
    if (ret) { fprintf(stderr, "enc pkt grp failed %d\n", ret); return 1; }
    MppBuffer enc_out_buf = NULL;
    ret = mpp_buffer_get(enc_pkt_grp, &enc_out_buf, 4 * 1024 * 1024);
    if (ret) { fprintf(stderr, "enc out buffer get failed %d\n", ret); return 1; }

    /* encoder INPUT frame group (canvas-sized buffers exceed the packet
     * group's 4MB limit — own group, single buffer, reused after each drain) */
    MppBufferGroup enc_in_grp = NULL;
    ret = mpp_buffer_group_get_internal(&enc_in_grp, MPP_BUFFER_TYPE_DRM);
    ret |= mpp_buffer_group_limit_config(enc_in_grp, ehs * evs * 3 / 2, 1);
    if (ret) { fprintf(stderr, "enc in grp failed %d\n", ret); return 1; }


    FILE *fp_out = fopen(out_path, "wb");
    if (!fp_out) { perror(out_path); return 1; }
    FILE *fp_pts = NULL;
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
    }

    /* SPS/PPS header (cam2h264.c) */
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
                size_t hlen = mpp_packet_get_length(hdr);
                if (ptr && hlen)
                    fwrite(ptr, 1, hlen, fp_out);
            }
            mpp_packet_deinit(&hdr);
        }
        if (hdr_buf)
            mpp_buffer_put(hdr_buf);
    }

    /* ---- encoder input frame (ONE, reused after each drain — CMA budget) ---- */
    MppFrame enc_in[1] = {0};
    MppBuffer enc_in_buf[1] = {0};
    for (int i = 0; i < 1; i++) {
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
    int enc_idx = 0;
    const int ENC_RING = 1;

    /* wait for cam1's first frame to learn the start trim */
    cur1_idx = ring_take(&cams[1].ring, &cur1, &cur1_pts);

    while (g_running && (frames == 0 || out_count < frames) &&
           !(t_deadline && now_us() > t_deadline)) {
        /* cam0 frame */
        int64_t p0_pts;
        MppFrame f0 = NULL;
        int f0_idx = ring_take(&cams[0].ring, &f0, &p0_pts);
        if (f0_idx < 0)
            break;

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

        /* stitch */
        int64_t t_stage = now_us();
        /* our cached mmap of the decoder buffers (MPP's own mapping is
         * uncached — the 24.9MB staging copy would take ~200ms) */
        void *y0_ptr = cams[0].ring.slots[f0_idx].map_ptr ?
                       cams[0].ring.slots[f0_idx].map_ptr :
                       mpp_buffer_get_ptr(cams[0].ring.slots[f0_idx].buf);
        void *y1_ptr = cams[1].ring.slots[pair_idx].map_ptr ?
                       cams[1].ring.slots[pair_idx].map_ptr :
                       mpp_buffer_get_ptr(cams[1].ring.slots[pair_idx].buf);
        /* DMA-written memory via a cached alias: invalidate before reads */
        mpp_buffer_sync_ro_begin(cams[0].ring.slots[f0_idx].buf);
        mpp_buffer_sync_ro_begin(cams[1].ring.slots[pair_idx].buf);
        if (!nogpu)
            gpu_stitch(&g, y0_ptr, y1_ptr, tight);
        else
            memset(tight, 128, g.tight_size ? g.tight_size : 8628480);
        int64_t t_gpu = now_us() - t_stage; t_stage = now_us();

        /* encode */
        MppFrame ef = enc_in[enc_idx];
        MppBuffer efb = enc_in_buf[enc_idx];
        enc_idx = (enc_idx + 1) % ENC_RING;
        int64_t mid = (p0_pts + pair_pts) / 2;
        mpp_frame_set_pts(ef, mid);
        void *enc_ptr = mpp_buffer_get_ptr(efb);
        memcpy(enc_ptr, tight, (size_t)g.canvas_w * g.canvas_h);
        memcpy((char *)enc_ptr + (size_t)ehs * evs,
               (char *)tight + (size_t)g.canvas_w * g.canvas_h,
               (size_t)g.canvas_w * g.canvas_h / 2);
        {
            MppPacket enc_pkt = NULL;
            mpp_packet_init_with_buffer(&enc_pkt, enc_out_buf);
            mpp_packet_set_length(enc_pkt, 0);
            MppMeta fmeta = mpp_frame_get_meta(ef);
            if (fmeta)
                mpp_meta_set_packet(fmeta, KEY_OUTPUT_PACKET, enc_pkt);
            mpp_packet_deinit(&enc_pkt);
        }
        ret = enc_mpi->encode_put_frame(enc_ctx, ef);
        if (ret) { fprintf(stderr, "encode_put_frame failed %d\n", ret); }
        int64_t t_put = now_us() - t_stage; t_stage = now_us();

        /* drain */
        for (;;) {
            MppPacket enc_pkt = NULL;
            ret = enc_mpi->encode_get_packet(enc_ctx, &enc_pkt);
            if (ret == MPP_ERR_TIMEOUT || !enc_pkt)
                break;
            if (ret)
                break;
            void *ptr = mpp_packet_get_pos(enc_pkt);
            size_t plen = mpp_packet_get_length(enc_pkt);
            if (ptr && plen) {
                static const RK_U8 sc[4] = { 0, 0, 0, 1 };
                if (plen < 4 || memcmp(ptr, sc, 4) != 0)
                    fwrite(sc, 1, 4, fp_out);
                fwrite(ptr, 1, plen, fp_out);
                if (fp_pts) {
                    RK_S64 pkt_pts = mpp_packet_get_pts(enc_pkt);
                    if (pkt_pts <= 0)
                        pkt_pts = mid;
                    fprintf(fp_pts, "%lld\n", (long long)pkt_pts);
                }
            }
            mpp_packet_deinit(&enc_pkt);
        }
        ring_release(&cams[0].ring, f0_idx);
        out_count++;
        if (out_count % 10 == 0)
            fprintf(stderr, "stitched %d frames (gpu %.1fms = wait0 %.1f + cpy %.1f + sub %.1f + wait1 %.1f + pack %.1f; put %.1fms)\n",
                    out_count, t_gpu / 1000.0,
                    g_times[0] / 1000.0, g_times[1] / 1000.0, g_times[2] / 1000.0,
                    g_times[3] / 1000.0, g_times[4] / 1000.0, t_put / 1000.0);
    }

    /* ---- stop + EOS drain ---- */
    g_running = 0;
    for (int i = 0; i < 2; i++) {
        pthread_cond_signal(&cams[i].ring.cv_free);
        pthread_cond_signal(&cams[i].ring.cv_ready);
    }
    {
        MppFrame ef = enc_in[enc_idx];
        enc_idx = (enc_idx + 1) % ENC_RING;
        mpp_frame_set_eos(ef, 1);
        {
            MppPacket enc_pkt = NULL;
            mpp_packet_init_with_buffer(&enc_pkt, enc_out_buf);
            mpp_packet_set_length(enc_pkt, 0);
            MppMeta fmeta = mpp_frame_get_meta(ef);
            if (fmeta)
                mpp_meta_set_packet(fmeta, KEY_OUTPUT_PACKET, enc_pkt);
            mpp_packet_deinit(&enc_pkt);
        }
        enc_mpi->encode_put_frame(enc_ctx, ef);
        for (;;) {
            MppPacket enc_pkt = NULL;
            ret = enc_mpi->encode_get_packet(enc_ctx, &enc_pkt);
            if (ret == MPP_ERR_TIMEOUT || !enc_pkt)
                break;
            if (ret)
                break;
            void *ptr = mpp_packet_get_pos(enc_pkt);
            size_t plen = mpp_packet_get_length(enc_pkt);
            if (ptr && plen) {
                static const RK_U8 sc[4] = { 0, 0, 0, 1 };
                if (plen < 4 || memcmp(ptr, sc, 4) != 0)
                    fwrite(sc, 1, 4, fp_out);
                fwrite(ptr, 1, plen, fp_out);
                if (fp_pts) {
                    RK_S64 pkt_pts = mpp_packet_get_pts(enc_pkt);
                    if (pkt_pts > 0)
                        fprintf(fp_pts, "%lld\n", (long long)pkt_pts);
                }
            }
            mpp_packet_deinit(&enc_pkt);
        }
    }
    for (int i = 0; i < 2; i++)
        pthread_join(cams[i].thread, NULL);
    if (fp_pts)
        fclose(fp_pts);
    fclose(fp_out);
    fprintf(stderr, "stitched %d frames (start-trimmed %d, dup %d, skip %d)\n",
            out_count, skipped_start, dup_count, skip_count);
    return 0;
}
