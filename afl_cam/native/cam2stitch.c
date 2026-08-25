/* LIVE dual-camera GPU-stitched recorder (RK3588 / Mali G610 / MPP).
 *
 * S1 harness mode (this file so far): stitch NV12 frame pairs from files
 * through the Vulkan compute shader (stitch_nv12.spv) and write the NV12
 * composite — the validation path for the shader and the per-frame GPU loop.
 * Live capture (V4L2/jpegd/ring pairing/rkvenc) builds on this in S3.
 *
 * GPU loop design (see live_stitch_plan.md): a PRE-RECORDED 3-slot
 * command-buffer ring. Nothing is re-recorded after init — this sidesteps
 * the libmali quirk where a second vkBeginCommandBuffer silently discards
 * earlier commands. Each slot owns 4 input images (Y0/UV0/Y1/UV1), one
 * staging buffer, one output SSBO (NV12 layout), one command buffer, one
 * fence. Per frame: wait fence -> memcpy staging -> submit -> read back the
 * slot that finished.
 *
 * Usage: cam2stitch -src <params_dir> <cam0.nv12> <cam1.nv12> <out.nv12>
 *                   [n_iters] [shader.spv]
 *   params_dir: params.bin + w0.bin + w1.bin (dump_camera_params.py)
 *   nv12: Y plane (src_w*src_h) + interleaved UV (src_w*src_h/2), tight
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>

#include <vulkan/vulkan.h>

#define RING_SLOTS 3

#define CHECK(call) do { VkResult r_ = (call); if (r_ != VK_SUCCESS) { \
    fprintf(stderr, "VK error %d at %s:%d\n", r_, __FILE__, __LINE__); exit(1); } } while (0)

typedef struct {
    uint32_t canvas_w, canvas_h, src_w, src_h;
} Params;

typedef struct {
    VkImage img;
    VkImageView iv;
    uint32_t w, h;
    VkFormat fmt;
    uint32_t bpp;
} plane_t;

typedef struct {
    /* per-slot state */
    plane_t planes[4];              /* 0=Y0 1=UV0 2=Y1 3=UV1 */
    VkBuffer staging;               /* Y0+UV0+Y1+UV1, persistently mapped */
    VkDeviceMemory staging_mem;
    void *staging_map;
    VkDeviceSize stage_off[4];
    VkBuffer ssbo;                  /* NV12 output, host-visible */
    VkDeviceMemory ssbo_mem;
    VkCommandBuffer cb;
    VkFence fence;
    VkDescriptorSet dset;
} slot_t;

static VkDevice dev;
static VkPhysicalDeviceMemoryProperties mprops;
static VkQueue queue;
static VkDescriptorPool dpool;
static VkDescriptorSetLayout dsl;
static VkPipeline pipeline;
static VkPipelineLayout layout;

static int64_t now_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
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

static VkDeviceMemory alloc_bind_buffer(VkBuffer buf, VkMemoryPropertyFlags props) {
    VkMemoryRequirements req;
    vkGetBufferMemoryRequirements(dev, buf, &req);
    VkMemoryAllocateInfo ai = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
                                .allocationSize = req.size };
    int found = 0;
    for (uint32_t i = 0; i < mprops.memoryTypeCount; i++)
        if ((req.memoryTypeBits & (1u << i)) &&
            (mprops.memoryTypes[i].propertyFlags & props) == props) {
            ai.memoryTypeIndex = i; found = 1; break;
        }
    if (!found) { fprintf(stderr, "no memory type for buffer (props %x)\n", props); exit(1); }
    VkDeviceMemory mem;
    CHECK(vkAllocateMemory(dev, &ai, NULL, &mem));
    CHECK(vkBindBufferMemory(dev, buf, mem, 0));
    return mem;
}

static VkDeviceMemory alloc_bind_image(VkImage img) {
    VkMemoryRequirements req;
    vkGetImageMemoryRequirements(dev, img, &req);
    VkMemoryAllocateInfo ai = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
                                .allocationSize = req.size };
    int found = 0;
    for (uint32_t i = 0; i < mprops.memoryTypeCount; i++)
        if ((req.memoryTypeBits & (1u << i)) &&
            (mprops.memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)) {
            ai.memoryTypeIndex = i; found = 1; break;
        }
    if (!found) { fprintf(stderr, "no device-local memory type for image\n"); exit(1); }
    VkDeviceMemory mem;
    CHECK(vkAllocateMemory(dev, &ai, NULL, &mem));
    CHECK(vkBindImageMemory(dev, img, mem, 0));
    return mem;
}

static void create_plane(plane_t *pl, uint32_t w, uint32_t h, VkFormat fmt, uint32_t bpp) {
    pl->w = w; pl->h = h; pl->fmt = fmt; pl->bpp = bpp;
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
    alloc_bind_image(pl->img);
    VkImageViewCreateInfo ivci = { .sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO,
                                   .viewType = VK_IMAGE_VIEW_TYPE_2D,
                                   .format = fmt,
                                   .image = pl->img,
                                   .subresourceRange = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 } };
    CHECK(vkCreateImageView(dev, &ivci, NULL, &pl->iv));
}

static int has_8bit_storage(VkPhysicalDevice phys) {
    VkPhysicalDevice8BitStorageFeatures f8 = {
        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_8BIT_STORAGE_FEATURES };
    VkPhysicalDeviceFeatures2 f2 = { .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2,
                                     .pNext = &f8 };
    vkGetPhysicalDeviceFeatures2(phys, &f2);
    return f8.storageBuffer8BitAccess ? 1 : 0;
}

int main(int argc, char **argv) {
    if (argc < 6 || strcmp(argv[1], "-src")) {
        fprintf(stderr, "usage: %s -src <params_dir> <cam0.nv12> <cam1.nv12> "
                "<out.nv12> [n_iters] [shader.spv]\n", argv[0]);
        return 1;
    }
    const char *dir = argv[2];
    const char *in0 = argv[3], *in1 = argv[4], *out_path = argv[5];
    int n_iters = argc > 6 ? atoi(argv[6]) : 1;
    const char *spv_path = argc > 7 ? argv[7] : "afl_cam/native/stitch_nv12.spv";

    char path[512];
    size_t len;
    void *params_data = read_file((snprintf(path, sizeof path, "%s/params.bin", dir), path), &len);
    if (len != 64 * sizeof(float)) { fprintf(stderr, "params.bin size mismatch (%zu)\n", len); return 1; }
    float *pf = params_data;
    Params p = { (uint32_t)pf[0], (uint32_t)pf[1], (uint32_t)pf[2], (uint32_t)pf[3] };
    if (!(p.canvas_w % 2 == 0 && p.canvas_h % 2 == 0)) {
        fprintf(stderr, "canvas %ux%u must be even for NV12\n", p.canvas_w, p.canvas_h); return 1;
    }
    if (!(p.canvas_w % 4 == 0)) {
        fprintf(stderr, "canvas_w %u must be multiple of 4 (pack0 fallback)\n", p.canvas_w); return 1;
    }
    size_t nv12_size = (size_t)p.src_w * p.src_h * 3 / 2;
    size_t w_len;
    void *w0d = read_file((snprintf(path, sizeof path, "%s/w0.bin", dir), path), &w_len);
    void *w1d = read_file((snprintf(path, sizeof path, "%s/w1.bin", dir), path), &len);
    if (w_len != (size_t)p.canvas_w * p.canvas_h * 4 || len != w_len) {
        fprintf(stderr, "weight size mismatch\n"); return 1;
    }
    void *nv0 = read_file(in0, &len);
    void *nv1 = read_file(in1, &len);
    (void)nv1;

    printf("canvas %ux%u, src %ux%u, n_iters %d\n", p.canvas_w, p.canvas_h, p.src_w, p.src_h, n_iters);

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
        printf("device[%u]: %s\n", i, props.deviceName);
        if (strstr(props.deviceName, "Mali")) phys = devs[i];
    }
    if (!phys) { fprintf(stderr, "no Mali GPU found\n"); return 1; }
    int pack8 = has_8bit_storage(phys);
    printf("8-bit storage buffer access: %s\n", pack8 ? "yes (PACK8 shader)" : "no (pack0 fallback)");
    if (!pack8) {
        fprintf(stderr, "pack0 shader variant required — pass stitch_nv12_pack0.spv\n");
    }

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
    VkPhysicalDevice8BitStorageFeatures f8 = {
        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_8BIT_STORAGE_FEATURES,
        .storageBuffer8BitAccess = (VkBool32)pack8 };
    VkDeviceCreateInfo dci = { .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
                               .pNext = &f8,
                               .queueCreateInfoCount = 1, .pQueueCreateInfos = &qci };
    CHECK(vkCreateDevice(phys, &dci, NULL, &dev));
    vkGetDeviceQueue(dev, qf, 0, &queue);
    vkGetPhysicalDeviceMemoryProperties(phys, &mprops);

    /* ---- samplers: linear+black-border for frames, nearest for weights ---- */
    VkSamplerCreateInfo sci = { .sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO,
                                .magFilter = VK_FILTER_NEAREST, .minFilter = VK_FILTER_NEAREST,
                                .addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
                                .addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
                                .maxLod = 0.0f };
    VkSampler nearest;
    CHECK(vkCreateSampler(dev, &sci, NULL, &nearest));
    sci.magFilter = VK_FILTER_LINEAR; sci.minFilter = VK_FILTER_LINEAR;
    sci.addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER;
    sci.addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER;
    sci.borderColor = VK_BORDER_COLOR_FLOAT_TRANSPARENT_BLACK;
    VkSampler linear;
    CHECK(vkCreateSampler(dev, &sci, NULL, &linear));

    /* ---- descriptor layout: 4 frame planes + 2 weights + ssbo + params ---- */
    VkDescriptorPoolSize dpss[2] = {
        { VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 6 * RING_SLOTS + 2 },
        { VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 2 * RING_SLOTS + 1 },
    };
    VkDescriptorPoolCreateInfo dpci = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,
                                        .maxSets = RING_SLOTS + 1,
                                        .poolSizeCount = 2, .pPoolSizes = dpss };
    CHECK(vkCreateDescriptorPool(dev, &dpci, NULL, &dpool));

    VkDescriptorSetLayoutBinding binds[8] = {0};
    for (int i = 0; i < 6; i++)
        binds[i] = (VkDescriptorSetLayoutBinding){
            i, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
    binds[6] = (VkDescriptorSetLayoutBinding){
        6, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
    binds[7] = (VkDescriptorSetLayoutBinding){
        7, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
    VkDescriptorSetLayoutCreateInfo dsci = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
                                             .bindingCount = 8, .pBindings = binds };
    CHECK(vkCreateDescriptorSetLayout(dev, &dsci, NULL, &dsl));

    /* ---- shader + pipeline ---- */
    void *spv = read_file(spv_path, &len);
    VkShaderModuleCreateInfo smci = { .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
                                      .codeSize = len, .pCode = spv };
    VkShaderModule sm;
    CHECK(vkCreateShaderModule(dev, &smci, NULL, &sm));
    VkPipelineLayoutCreateInfo plci = { .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
                                        .setLayoutCount = 1, .pSetLayouts = &dsl };
    CHECK(vkCreatePipelineLayout(dev, &plci, NULL, &layout));
    VkComputePipelineCreateInfo cpci = { .sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
                                         .stage = { .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
                                                    .stage = VK_SHADER_STAGE_COMPUTE_BIT,
                                                    .module = sm, .pName = "main" },
                                         .layout = layout };
    CHECK(vkCreateComputePipelines(dev, VK_NULL_HANDLE, 1, &cpci, NULL, &pipeline));

    /* ---- static weights + params SSBO ---- */
    VkBufferCreateInfo bci_w = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                                 .size = (VkDeviceSize)p.canvas_w * p.canvas_h * 4,
                                 .usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT |
                                          VK_BUFFER_USAGE_STORAGE_BUFFER_BIT };
    (void)bci_w; /* weights stay plain sampled images (uploaded below) */

    plane_t wplanes[2];
    create_plane(&wplanes[0], p.canvas_w, p.canvas_h, VK_FORMAT_R8G8B8A8_UNORM, 4);
    create_plane(&wplanes[1], p.canvas_w, p.canvas_h, VK_FORMAT_R8G8B8A8_UNORM, 4);
    VkBufferCreateInfo wst_bci = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                                   .size = (VkDeviceSize)p.canvas_w * p.canvas_h * 4,
                                   .usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT };
    VkBuffer wst0, wst1;
    CHECK(vkCreateBuffer(dev, &wst_bci, NULL, &wst0));
    CHECK(vkCreateBuffer(dev, &wst_bci, NULL, &wst1));
    VkDeviceMemory wst0m = alloc_bind_buffer(wst0, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                                    VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    VkDeviceMemory wst1m = alloc_bind_buffer(wst1, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                                    VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    void *wmap;
    CHECK(vkMapMemory(dev, wst0m, 0, wst_bci.size, 0, &wmap));
    memcpy(wmap, w0d, wst_bci.size);
    vkUnmapMemory(dev, wst0m);
    CHECK(vkMapMemory(dev, wst1m, 0, wst_bci.size, 0, &wmap));
    memcpy(wmap, w1d, wst_bci.size);
    vkUnmapMemory(dev, wst1m);

    VkBufferCreateInfo pbci = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                                .size = 64 * sizeof(float),
                                .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT };
    VkBuffer pbuf;
    CHECK(vkCreateBuffer(dev, &pbci, NULL, &pbuf));
    VkDeviceMemory pbmem = alloc_bind_buffer(pbuf, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                                    VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    void *pmap;
    CHECK(vkMapMemory(dev, pbmem, 0, pbci.size, 0, &pmap));
    memcpy(pmap, params_data, pbci.size);
    fprintf(stderr, "params after upload: %.1f %.1f %.1f %.1f\n",
            ((float *)pmap)[0], ((float *)pmap)[1], ((float *)pmap)[2], ((float *)pmap)[3]);
    vkUnmapMemory(dev, pbmem);

    /* ---- command pool + setup recording (weights + initial layout) ---- */
    VkCommandPoolCreateInfo cpc = { .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
                                    .queueFamilyIndex = qf };
    VkCommandPool pool;
    CHECK(vkCreateCommandPool(dev, &cpc, NULL, &pool));
    VkCommandBufferAllocateInfo cbai = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
                                         .commandPool = pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
                                         .commandBufferCount = 1 };
    VkCommandBuffer setup_cb;
    CHECK(vkAllocateCommandBuffers(dev, &cbai, &setup_cb));

    /* ---- per-slot resources ---- */
    slot_t slots[RING_SLOTS];
    VkDeviceSize ssbo_size = (VkDeviceSize)p.canvas_w * p.canvas_h * 8; /* uint per byte, padded */
    for (int k = 0; k < RING_SLOTS; k++) {
        slot_t *s = &slots[k];
        s->planes[0] = (plane_t){0}; s->planes[1] = (plane_t){0};
        s->planes[2] = (plane_t){0}; s->planes[3] = (plane_t){0};
        create_plane(&s->planes[0], p.src_w, p.src_h, VK_FORMAT_R8_UNORM, 1);
        create_plane(&s->planes[1], p.src_w / 2, p.src_h / 2, VK_FORMAT_R8G8_UNORM, 2);
        create_plane(&s->planes[2], p.src_w, p.src_h, VK_FORMAT_R8_UNORM, 1);
        create_plane(&s->planes[3], p.src_w / 2, p.src_h / 2, VK_FORMAT_R8G8_UNORM, 2);

        VkDeviceSize stage_size = (VkDeviceSize)p.src_w * p.src_h * 3; /* two NV12 frames */
        VkBufferCreateInfo sbci = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                                    .size = stage_size, .usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT };
        CHECK(vkCreateBuffer(dev, &sbci, NULL, &s->staging));
        s->staging_mem = alloc_bind_buffer(s->staging, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                                       VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
        CHECK(vkMapMemory(dev, s->staging_mem, 0, stage_size, 0, &s->staging_map));
        s->stage_off[0] = 0;
        s->stage_off[1] = (VkDeviceSize)p.src_w * p.src_h;
        s->stage_off[2] = s->stage_off[1] + (VkDeviceSize)p.src_w * p.src_h / 2;
        s->stage_off[3] = s->stage_off[2] + (VkDeviceSize)p.src_w * p.src_h;

        VkBufferCreateInfo obci = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                                    .size = ssbo_size, .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT };
        CHECK(vkCreateBuffer(dev, &obci, NULL, &s->ssbo));
        s->ssbo_mem = alloc_bind_buffer(s->ssbo, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                                 VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);

        VkCommandBufferAllocateInfo cbi = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
                                            .commandPool = pool,
                                            .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
                                            .commandBufferCount = 1 };
        CHECK(vkAllocateCommandBuffers(dev, &cbi, &s->cb));
        /* pre-signaled: the first WaitForFences per slot must pass */
        VkFenceCreateInfo fci = { .sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO,
                                  .flags = VK_FENCE_CREATE_SIGNALED_BIT };
        CHECK(vkCreateFence(dev, &fci, NULL, &s->fence));

        VkDescriptorSetAllocateInfo dsai = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
                                             .descriptorPool = dpool, .descriptorSetCount = 1,
                                             .pSetLayouts = &dsl };
        CHECK(vkAllocateDescriptorSets(dev, &dsai, &s->dset));

        VkSampler smps[6] = { linear, linear, linear, linear, nearest, nearest };
        VkDescriptorImageInfo dii[6];
        for (int i = 0; i < 4; i++)
            dii[i] = (VkDescriptorImageInfo){ smps[i], s->planes[i].iv,
                                              VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
        dii[4] = (VkDescriptorImageInfo){ smps[4], wplanes[0].iv,
                                          VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
        dii[5] = (VkDescriptorImageInfo){ smps[5], wplanes[1].iv,
                                          VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
        VkDescriptorBufferInfo obi = { s->ssbo, 0, ssbo_size };
        VkDescriptorBufferInfo pbi = { pbuf, 0, pbci.size };
        VkWriteDescriptorSet wds[8] = {0};
        for (int i = 0; i < 6; i++)
            wds[i] = (VkWriteDescriptorSet){ .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
                                             .dstSet = s->dset, .dstBinding = i, .descriptorCount = 1,
                                             .descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,
                                             .pImageInfo = &dii[i] };
        wds[6] = (VkWriteDescriptorSet){ .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
                                         .dstSet = s->dset, .dstBinding = 6, .descriptorCount = 1,
                                         .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
                                         .pBufferInfo = &obi };
        wds[7] = (VkWriteDescriptorSet){ .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
                                         .dstSet = s->dset, .dstBinding = 7, .descriptorCount = 1,
                                         .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
                                         .pBufferInfo = &pbi };
        vkUpdateDescriptorSets(dev, 8, wds, 0, NULL);
    }

    /* ---- pre-record per-slot command buffers (never re-recorded) ---- */
    /* Each slot cb is SELF-CONTAINED: weight uploads + initial transitions +
     * per-frame upload + dispatch. This sidesteps the libmali quirk where
     * only ONE recording's commands survive (a separate setup recording's
     * commands were silently discarded). */
    VkCommandBufferBeginInfo bi = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
                                    .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT };
    VkBufferImageCopy wc = { .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 },
                             .imageExtent = { p.canvas_w, p.canvas_h, 1 } };
    /* CAM2STITCH_DBG truncates the frame cb for bisection:
       1 = barriers+copies, no dispatch; 2 = copies only; 3 = empty */
    int dbg = getenv("CAM2STITCH_DBG") ? atoi(getenv("CAM2STITCH_DBG")) : 0;
    for (int k = 0; k < RING_SLOTS; k++) {
        slot_t *s = &slots[k];
        CHECK(vkBeginCommandBuffer(s->cb, &bi));
        /* weights: UNDEFINED -> TRANSFER_DST -> copy -> SHADER_READ */
        VkImageMemoryBarrier wb[2];
        for (int i = 0; i < 2; i++)
            wb[i] = (VkImageMemoryBarrier){
                .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
                .srcAccessMask = 0, .dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT,
                .oldLayout = VK_IMAGE_LAYOUT_UNDEFINED, .newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                .image = wplanes[i].img, .subresourceRange = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 } };
        vkCmdPipelineBarrier(s->cb, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                             0, 0, NULL, 0, NULL, 2, wb);
        vkCmdCopyBufferToImage(s->cb, wst0, wplanes[0].img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &wc);
        vkCmdCopyBufferToImage(s->cb, wst1, wplanes[1].img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &wc);
        for (int i = 0; i < 2; i++) {
            wb[i].srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
            wb[i].dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
            wb[i].oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
            wb[i].newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        }
        vkCmdPipelineBarrier(s->cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                             0, 0, NULL, 0, NULL, 2, wb);
        /* frame images: first use UNDEFINED -> SHADER_READ */
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
        if (dbg != 3 && dbg != 2)
            vkCmdPipelineBarrier(s->cb, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                                 0, 0, NULL, 0, NULL, 4, bars);
        VkBufferImageCopy bics[4];
        for (int i = 0; i < 4; i++) {
            bics[i] = (VkBufferImageCopy){
                .bufferOffset = s->stage_off[i],
                .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 },
                .imageExtent = { s->planes[i].w, s->planes[i].h, 1 } };
        }
        if (dbg != 3)
            for (int i = 0; i < 4; i++)
                vkCmdCopyBufferToImage(s->cb, s->staging, s->planes[i].img,
                                       VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &bics[i]);
        for (int i = 0; i < 4; i++) {
            bars[i].srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
            bars[i].dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
            bars[i].oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
            bars[i].newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        }
        if (dbg != 3 && dbg != 2)
            vkCmdPipelineBarrier(s->cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                                 0, 0, NULL, 0, NULL, 4, bars);
        if (dbg == 0) {
            vkCmdBindPipeline(s->cb, VK_PIPELINE_BIND_POINT_COMPUTE, pipeline);
            vkCmdBindDescriptorSets(s->cb, VK_PIPELINE_BIND_POINT_COMPUTE, layout, 0, 1, &s->dset, 0, NULL);
            /* one thread per canvas pixel */
            vkCmdDispatch(s->cb, (p.canvas_w + 15) / 16, (p.canvas_h + 15) / 16, 1);
        }
        CHECK(vkEndCommandBuffer(s->cb));
    }
    printf("pre-recorded %d command buffers (dispatch %ux%u groups)\n",
           RING_SLOTS, (p.canvas_w + 15) / 16, (p.canvas_h + 15) / 16);

    /* ---- frame loop ---- */
    int64_t t0 = now_us();
    int k = 0;
    for (int i = 0; i < n_iters; i++) {
        slot_t *s = &slots[k];
        CHECK(vkWaitForFences(dev, 1, &s->fence, VK_TRUE, UINT64_MAX));
        CHECK(vkResetFences(dev, 1, &s->fence));
        memcpy((char *)s->staging_map + s->stage_off[0], nv0, nv12_size);
        memcpy((char *)s->staging_map + s->stage_off[2], nv1, nv12_size);
        VkSubmitInfo si = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
                            .commandBufferCount = 1, .pCommandBuffers = &s->cb };
        CHECK(vkQueueSubmit(queue, 1, &si, s->fence));
        k = (k + 1) % RING_SLOTS;
    }
    /* drain: wait for the last submitted frames */
    for (int j = 0; j < RING_SLOTS && j < n_iters; j++) {
        int idx = (n_iters - 1 - j) % RING_SLOTS;
        CHECK(vkWaitForFences(dev, 1, &slots[idx].fence, VK_TRUE, UINT64_MAX));
    }
    int64_t elapsed = now_us() - t0;
    printf("%d frames in %.1f ms (%.2f ms/frame)\n", n_iters, elapsed / 1000.0,
           n_iters ? elapsed / 1000.0 / n_iters : 0.0);

    /* ---- write the LAST slot's output (n_iters-1 % RING_SLOTS) ---- */
    int last = (n_iters - 1) % RING_SLOTS;
    void *omap;
    CHECK(vkMapMemory(dev, slots[last].ssbo_mem, 0, ssbo_size, 0, &omap));
    /* repack: the SSBO holds one byte per uint32 in NV12 order —
       Y plane first (cw*ch uints), then interleaved UV (cw*ch/2 uints) */
    uint32_t *o32 = omap;
    size_t tight = (size_t)p.canvas_w * p.canvas_h * 3 / 2;
    uint8_t *tightbuf = malloc(tight);
    for (size_t i = 0; i < (size_t)p.canvas_w * p.canvas_h; i++)
        tightbuf[i] = (uint8_t)o32[i];
    for (size_t i = 0; i < (size_t)p.canvas_w * p.canvas_h / 2; i++)
        tightbuf[(size_t)p.canvas_w * p.canvas_h + i] =
            (uint8_t)o32[(size_t)p.canvas_w * p.canvas_h + i];
    FILE *out = fopen(out_path, "wb");
    if (!out) { perror(out_path); return 1; }
    fwrite(tightbuf, 1, tight, out);
    fclose(out);
    free(tightbuf);
    vkUnmapMemory(dev, slots[last].ssbo_mem);
    printf("wrote %s (%zu bytes, repacked)\n", out_path, tight);

    return 0;
}
