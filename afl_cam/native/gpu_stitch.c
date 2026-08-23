/* GPU stitch validation: exact spherical remap + blend on the Mali G610 via a
 * Vulkan compute shader. Output goes through an SSBO (the libmali storage-image
 * path is broken on this driver). Times the dispatch with GPU timestamps.
 *
 * Usage: gpu_stitch <params_dir> [shader.spv]
 *   params_dir must contain: stitch.txt, map{0,1}.bin (canvas RG32F),
 *   cam{0,1}.rgb (RGB24), w{0,1}.bin (canvas RGBA8)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include <vulkan/vulkan.h>

#define WARMUP 10
#define ITERS 200
#define CHECK(call) do { VkResult r_ = (call); if (r_ != VK_SUCCESS) { \
    fprintf(stderr, "VK error %d at %s:%d\n", r_, __FILE__, __LINE__); exit(1); } } while (0)

typedef struct {
    uint32_t canvas_w, canvas_h, src_w, src_h;
} Params;

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

static Params parse_params(const char *dir) {
    char path[512];
    snprintf(path, sizeof path, "%s/cams.txt", dir);
    FILE *f = fopen(path, "r");
    if (!f) { perror(path); exit(1); }
    Params p = {0};
    char key[32];
    while (fscanf(f, "%31s", key) == 1) {
        if (!strcmp(key, "canvas_w")) fscanf(f, "%u", &p.canvas_w);
        else if (!strcmp(key, "canvas_h")) fscanf(f, "%u", &p.canvas_h);
        else if (!strcmp(key, "src_w")) fscanf(f, "%u", &p.src_w);
        else if (!strcmp(key, "src_h")) fscanf(f, "%u", &p.src_h);
    }
    fclose(f);
    if (!p.canvas_w || !p.canvas_h || !p.src_w || !p.src_h) {
        fprintf(stderr, "bad stitch.txt\n");
        exit(1);
    }
    printf("canvas %ux%u, inputs %ux%u\n", p.canvas_w, p.canvas_h, p.src_w, p.src_h);
    return p;
}

static VkDeviceMemory alloc_bind_buffer(VkDevice dev, VkPhysicalDeviceMemoryProperties *mprops,
                                        VkBuffer buf, VkMemoryPropertyFlags props) {
    VkMemoryRequirements req;
    vkGetBufferMemoryRequirements(dev, buf, &req);
    VkMemoryAllocateInfo ai = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
                                .allocationSize = req.size };
    for (uint32_t i = 0; i < mprops->memoryTypeCount; i++)
        if ((req.memoryTypeBits & (1u << i)) && (mprops->memoryTypes[i].propertyFlags & props)) {
            ai.memoryTypeIndex = i;
            break;
        }
    VkDeviceMemory mem;
    CHECK(vkAllocateMemory(dev, &ai, NULL, &mem));
    CHECK(vkBindBufferMemory(dev, buf, mem, 0));
    return mem;
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
            break;
        }
    VkDeviceMemory mem;
    CHECK(vkAllocateMemory(dev, &ai, NULL, &mem));
    CHECK(vkBindImageMemory(dev, img, mem, 0));
    return mem;
}

static VkImage create_img(VkDevice dev, uint32_t w, uint32_t h, VkFormat fmt) {
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
    VkImage img;
    CHECK(vkCreateImage(dev, &ci, NULL, &img));
    return img;
}

static VkBuffer create_staging(VkDevice dev, VkPhysicalDeviceMemoryProperties *mprops,
                               const void *data, VkDeviceSize size, VkDeviceMemory *mem_out) {
    VkBufferCreateInfo bci = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                               .size = size, .usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT };
    VkBuffer buf;
    CHECK(vkCreateBuffer(dev, &bci, NULL, &buf));
    VkDeviceMemory mem = alloc_bind_buffer(dev, mprops, buf,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    void *map;
    CHECK(vkMapMemory(dev, mem, 0, size, 0, &map));
    memcpy(map, data, size);
    vkUnmapMemory(dev, mem);
    *mem_out = mem;
    return buf;
}

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s <params_dir> [shader.spv]\n", argv[0]); return 1; }
    const char *dir = argv[1];
    Params p = parse_params(dir);

    char path[512];
    size_t len;
    void *rgb0 = read_file((snprintf(path, sizeof path, "%s/cam0.rgb", dir), path), &len);
    if (len != (size_t)p.src_w * p.src_h * 3) { fprintf(stderr, "cam0 size mismatch\n"); return 1; }
    void *rgb1 = read_file((snprintf(path, sizeof path, "%s/cam1.rgb", dir), path), &len);
    if (len != (size_t)p.src_w * p.src_h * 3) { fprintf(stderr, "cam1 size mismatch\n"); return 1; }
    void *w0d = read_file((snprintf(path, sizeof path, "%s/w0.bin", dir), path), &len);
    if (len != (size_t)p.canvas_w * p.canvas_h * 4) { fprintf(stderr, "w0 size mismatch\n"); return 1; }
    void *w1d = read_file((snprintf(path, sizeof path, "%s/w1.bin", dir), path), &len);
    if (len != (size_t)p.canvas_w * p.canvas_h * 4) { fprintf(stderr, "w1 size mismatch\n"); return 1; }

    /* ---- instance + device ---- */
    VkApplicationInfo app = { .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
                              .pApplicationName = "gpu_stitch", .apiVersion = VK_API_VERSION_1_3 };
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
    VkDevice dev;
    CHECK(vkCreateDevice(phys, &dci, NULL, &dev));
    VkQueue queue;
    vkGetDeviceQueue(dev, qf, 0, &queue);

    VkPhysicalDeviceMemoryProperties mprops;
    vkGetPhysicalDeviceMemoryProperties(phys, &mprops);

    /* ---- images: 2 maps (RG32F) + 2 cams (RGB8) + 2 weights (RGBA8) ---- */
    VkFormat fmts[4] = { VK_FORMAT_R8G8B8_UNORM, VK_FORMAT_R8G8B8_UNORM,
                         VK_FORMAT_R8G8B8A8_UNORM, VK_FORMAT_R8G8B8A8_UNORM };
    uint32_t iws[4] = { p.src_w, p.src_w, p.canvas_w, p.canvas_w };
    uint32_t ihs[4] = { p.src_h, p.src_h, p.canvas_h, p.canvas_h };
    VkDeviceSize bpp[4] = { 3, 3, 4, 4 };
    VkImage imgs[4];
    for (int i = 0; i < 4; i++) {
        imgs[i] = create_img(dev, iws[i], ihs[i], fmts[i]);
        alloc_bind_image(dev, &mprops, imgs[i]);
    }

    /* ---- staging buffers ---- */
    const void *srcs[4] = { rgb0, rgb1, w0d, w1d };
    VkBuffer bufs[4];
    VkDeviceMemory bufms[4];
    for (int i = 0; i < 4; i++)
        bufs[i] = create_staging(dev, &mprops, srcs[i], (VkDeviceSize)iws[i] * ihs[i] * bpp[i], &bufms[i]);

    /* ---- output SSBO + params SSBO ---- */
    VkDeviceSize out_size = (VkDeviceSize)p.canvas_w * p.canvas_h * 4;
    VkBufferCreateInfo obci = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                                .size = out_size, .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT };
    VkBuffer obuf;
    CHECK(vkCreateBuffer(dev, &obci, NULL, &obuf));
    VkDeviceMemory obmem = alloc_bind_buffer(dev, &mprops, obuf,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);

    void *params_data = read_file((snprintf(path, sizeof path, "%s/params.bin", dir), path), &len);
    if (len != 64 * sizeof(float)) { fprintf(stderr, "params.bin size mismatch (%zu)\n", len); return 1; }
    float *pf = params_data;
    p.canvas_w = (uint32_t)pf[0]; p.canvas_h = (uint32_t)pf[1]; p.src_w = (uint32_t)pf[2]; p.src_h = (uint32_t)pf[3];
    printf("params: canvas %ux%u, src %ux%u, warp_scale %.1f\n", p.canvas_w, p.canvas_h, p.src_w, p.src_h, pf[4]);
    float pdims[4] = { (float)p.canvas_w, (float)p.canvas_h, (float)p.src_w, (float)p.src_h };
    VkBufferCreateInfo pbci = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                                .size = len, .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT };
    VkBuffer pbuf;
    CHECK(vkCreateBuffer(dev, &pbci, NULL, &pbuf));
    VkDeviceMemory pbmem = alloc_bind_buffer(dev, &mprops, pbuf,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    void *pmap;
    CHECK(vkMapMemory(dev, pbmem, 0, len, 0, &pmap));
    memcpy(pmap, params_data, len);
    vkUnmapMemory(dev, pbmem);

    /* ---- command buffer: ONE recording (uploads + dispatch) ---- */
    VkCommandPoolCreateInfo cpci = { .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
                                     .queueFamilyIndex = qf };
    VkCommandPool pool;
    CHECK(vkCreateCommandPool(dev, &cpci, NULL, &pool));
    VkCommandBufferAllocateInfo cbai = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
                                         .commandPool = pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
                                         .commandBufferCount = 1 };
    VkCommandBuffer cb;
    CHECK(vkAllocateCommandBuffers(dev, &cbai, &cb));
    VkCommandBufferBeginInfo bi = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
                                    .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT };
    CHECK(vkBeginCommandBuffer(cb, &bi));

    VkImageMemoryBarrier barriers[4];
    for (int i = 0; i < 4; i++)
        barriers[i] = (VkImageMemoryBarrier){
            .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
            .srcAccessMask = 0, .dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT,
            .oldLayout = VK_IMAGE_LAYOUT_UNDEFINED, .newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
            .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
            .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
            .image = imgs[i], .subresourceRange = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 } };
    vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                         0, 0, NULL, 0, NULL, 4, barriers);

    for (int i = 0; i < 4; i++) {
        VkBufferImageCopy bic = { .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 },
                                  .imageExtent = { iws[i], ihs[i], 1 } };
        vkCmdCopyBufferToImage(cb, bufs[i], imgs[i], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &bic);
    }
    for (int i = 0; i < 4; i++)
        barriers[i].srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT,
        barriers[i].dstAccessMask = VK_ACCESS_SHADER_READ_BIT,
        barriers[i].oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
        barriers[i].newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                         0, 0, NULL, 0, NULL, 4, barriers);

    /* ---- samplers ---- */
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

    /* ---- descriptor set: 6 combined samplers + 2 storage buffers ---- */
    VkDescriptorPoolSize dpss[2] = {
        { VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 4 },
        { VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 2 },
    };
    VkDescriptorPoolCreateInfo dpci = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,
                                        .maxSets = 1, .poolSizeCount = 2, .pPoolSizes = dpss };
    VkDescriptorPool dpool;
    CHECK(vkCreateDescriptorPool(dev, &dpci, NULL, &dpool));

    VkDescriptorSetLayoutBinding binds[6] = {0};
    for (int i = 0; i < 4; i++)
        binds[i] = (VkDescriptorSetLayoutBinding){
            i, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
    binds[4] = (VkDescriptorSetLayoutBinding){
        4, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
    binds[5] = (VkDescriptorSetLayoutBinding){
        5, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
    VkDescriptorSetLayoutCreateInfo dsci = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
                                             .bindingCount = 6, .pBindings = binds };
    VkDescriptorSetLayout dsl;
    CHECK(vkCreateDescriptorSetLayout(dev, &dsci, NULL, &dsl));
    VkDescriptorSetAllocateInfo dsai = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
                                         .descriptorPool = dpool, .descriptorSetCount = 1, .pSetLayouts = &dsl };
    VkDescriptorSet dset;
    CHECK(vkAllocateDescriptorSets(dev, &dsai, &dset));

    VkImageViewCreateInfo ivci = { .sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO,
                                   .viewType = VK_IMAGE_VIEW_TYPE_2D,
                                   .subresourceRange = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 } };
    VkImageView ivs[4];
    for (int i = 0; i < 4; i++) {
        ivci.image = imgs[i];
        ivci.format = fmts[i];
        CHECK(vkCreateImageView(dev, &ivci, NULL, &ivs[i]));
    }

    VkSampler smp[4] = { linear, linear, nearest, nearest };
    VkDescriptorImageInfo dii[4];
    for (int i = 0; i < 4; i++)
        dii[i] = (VkDescriptorImageInfo){ smp[i], ivs[i], VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
    VkDescriptorBufferInfo obi = { obuf, 0, out_size };
    VkDescriptorBufferInfo pbi = { pbuf, 0, len };
    VkWriteDescriptorSet wds[6] = {0};
    for (int i = 0; i < 4; i++)
        wds[i] = (VkWriteDescriptorSet){ .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
                                         .dstSet = dset, .dstBinding = i, .descriptorCount = 1,
                                         .descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,
                                         .pImageInfo = &dii[i] };
    wds[4] = (VkWriteDescriptorSet){ .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
                                     .dstSet = dset, .dstBinding = 4, .descriptorCount = 1,
                                     .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .pBufferInfo = &obi };
    wds[5] = (VkWriteDescriptorSet){ .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
                                     .dstSet = dset, .dstBinding = 5, .descriptorCount = 1,
                                     .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .pBufferInfo = &pbi };
    vkUpdateDescriptorSets(dev, 6, wds, 0, NULL);

    /* ---- pipeline ---- */
    const char *spv_path = argc > 2 ? argv[2] : "afl_cam/native/stitch.spv";
    void *spv = read_file(spv_path, &len);
    VkShaderModuleCreateInfo smci = { .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
                                      .codeSize = len, .pCode = spv };
    VkShaderModule sm;
    CHECK(vkCreateShaderModule(dev, &smci, NULL, &sm));
    VkPipelineLayoutCreateInfo plci = { .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
                                        .setLayoutCount = 1, .pSetLayouts = &dsl };
    VkPipelineLayout layout;
    CHECK(vkCreatePipelineLayout(dev, &plci, NULL, &layout));
    VkComputePipelineCreateInfo cpci2 = { .sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
                                          .stage = { .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
                                                     .stage = VK_SHADER_STAGE_COMPUTE_BIT,
                                                     .module = sm, .pName = "main" },
                                          .layout = layout };
    VkPipeline pipeline;
    CHECK(vkCreateComputePipelines(dev, VK_NULL_HANDLE, 1, &cpci2, NULL, &pipeline));

    /* ---- timestamped dispatch loop (continues the SAME recording) ---- */
    VkQueryPoolCreateInfo qpci = { .sType = VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO,
                                   .queryType = VK_QUERY_TYPE_TIMESTAMP,
                                   .queryCount = (WARMUP + ITERS) * 2 };
    VkQueryPool qpool;
    CHECK(vkCreateQueryPool(dev, &qpci, NULL, &qpool));

    vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, pipeline);
    vkCmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_COMPUTE, layout, 0, 1, &dset, 0, NULL);
    vkCmdResetQueryPool(cb, qpool, 0, (WARMUP + ITERS) * 2);
    uint32_t gx = (p.canvas_w + 15) / 16, gy = (p.canvas_h + 15) / 16;
    for (int i = 0; i < WARMUP + ITERS; i++) {
        vkCmdWriteTimestamp(cb, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, qpool, i * 2);
        vkCmdDispatch(cb, gx, gy, 1);
        vkCmdWriteTimestamp(cb, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, qpool, i * 2 + 1);
    }
    CHECK(vkEndCommandBuffer(cb));

    VkSubmitInfo si = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
                        .commandBufferCount = 1, .pCommandBuffers = &cb };
    CHECK(vkQueueSubmit(queue, 1, &si, VK_NULL_HANDLE));
    CHECK(vkQueueWaitIdle(queue));

    uint64_t ts[(WARMUP + ITERS) * 2];
    CHECK(vkGetQueryPoolResults(dev, qpool, 0, (WARMUP + ITERS) * 2,
                                sizeof ts, ts, sizeof(uint64_t), VK_QUERY_RESULT_64_BIT));
    VkPhysicalDeviceProperties dprops;
    vkGetPhysicalDeviceProperties(phys, &dprops);
    double stamp = dprops.limits.timestampPeriod > 0 ? (double)dprops.limits.timestampPeriod * 1e-6 : 1.0;
    double total = 0, minv = 1e30, maxv = 0;
    for (int i = WARMUP; i < WARMUP + ITERS; i++) {
        double ms = (double)(ts[i * 2 + 1] - ts[i * 2]) * stamp;
        total += ms;
        if (ms < minv) minv = ms;
        if (ms > maxv) maxv = ms;
    }
    printf("dispatch over %dx%d canvas: avg %.3f ms, min %.3f ms, max %.3f ms (%d iters)\n",
           p.canvas_w, p.canvas_h, total / ITERS, minv, maxv, ITERS);

    /* ---- write SSBO result ---- */
    void *omap;
    CHECK(vkMapMemory(dev, obmem, 0, out_size, 0, &omap));
    snprintf(path, sizeof path, "%s/out_gpu.ssbo", dir);
    FILE *out = fopen(path, "wb");
    if (!out) { perror(path); return 1; }
    fwrite(omap, 1, (size_t)out_size, out);
    fclose(out);
    vkUnmapMemory(dev, obmem);
    printf("wrote %s\n", path);

    return 0;
}
