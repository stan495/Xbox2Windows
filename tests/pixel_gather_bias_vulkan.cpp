// Standalone Linux Vulkan runner for the synthetic pixel_gather_bias_vulkan.py bundle.
#include <vulkan/vulkan.h>

#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {

constexpr uint32_t kSide = 8;
constexpr VkFormat kFormat = VK_FORMAT_R32G32B32A32_SFLOAT;
constexpr VkDeviceSize kPixelBytes = 4 * sizeof(float);
constexpr VkDeviceSize kOutputBytes = kSide * kSide * kPixelBytes;
constexpr std::array<VkDeviceSize, 3> kMipOffsets{0, 1024, 1280};
constexpr uint32_t kMips = static_cast<uint32_t>(kMipOffsets.size());
constexpr VkDeviceSize kUploadBytes = kMipOffsets.back() + 2 * 2 * kPixelBytes;
constexpr VkImageUsageFlags kTextureUsage = VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_SAMPLED_BIT;
constexpr VkImageUsageFlags kTargetUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT;

struct Skip : std::runtime_error {
    using std::runtime_error::runtime_error;
};

template <typename T>
T info(VkStructureType type)
{
    T value{};
    value.sType = type;
    return value;
}

void check(VkResult result, const char* operation)
{
    if (result != VK_SUCCESS)
        throw std::runtime_error(std::string(operation) + " failed with VkResult " + std::to_string(result));
}

uint32_t parse_index(std::string_view text, const char* name)
{
    uint32_t value = 0;
    const auto end = text.data() + text.size();
    const auto result = std::from_chars(text.data(), end, value);
    if (result.ec != std::errc{} || result.ptr != end)
        throw std::runtime_error(std::string("Invalid ") + name);
    return value;
}

std::ifstream open_input(const std::filesystem::path& path)
{
    std::ifstream file(path, std::ios::binary | std::ios::ate);
    if (!file)
        throw std::runtime_error("Cannot open " + path.string());
    return file;
}

void read_mip(const std::filesystem::path& path, void* dest, VkDeviceSize bytes)
{
    auto file = open_input(path);
    const auto size = static_cast<std::streamsize>(bytes);
    if (file.tellg() != size || !file.seekg(0) || !file.read(static_cast<char*>(dest), size))
        throw std::runtime_error("Invalid mip data size or read error: " + path.string());
}

bool has_extension(const std::vector<VkExtensionProperties>& extensions, std::string_view name)
{
    return std::any_of(extensions.begin(), extensions.end(), [name](const auto& extension) {
        return name == extension.extensionName;
    });
}

struct Buffer {
    VkBuffer handle{};
    VkDeviceMemory memory{};
    bool coherent{};
};

struct Image {
    VkImage handle{};
    VkDeviceMemory memory{};
    VkImageView view{};
};

// One draw owns the instance, graphics queue and all objects used by the command buffer.
struct Draw {
    VkInstance instance{};
    VkPhysicalDevice physical{};
    VkDevice device{};
    VkPhysicalDeviceMemoryProperties memory_properties{};
    VkQueue queue{};
    uint32_t graphics_family{};
    VkCommandPool command_pool{};
    VkCommandBuffer command{};
    Buffer upload, readback;
    Image texture, target;
    VkSampler sampler{};
    VkDescriptorSetLayout set_layout{};
    VkDescriptorPool descriptor_pool{};
    VkDescriptorSet descriptors{};
    VkPipelineLayout pipeline_layout{};
    VkRenderPass render_pass{};
    VkFramebuffer framebuffer{};
    VkShaderModule vertex{}, fragment{};
    VkPipeline pipeline{};

    ~Draw()
    {
        if (device) {
            vkDestroyCommandPool(device, command_pool, nullptr);
            vkDestroyPipeline(device, pipeline, nullptr);
            vkDestroyShaderModule(device, fragment, nullptr);
            vkDestroyShaderModule(device, vertex, nullptr);
            vkDestroyFramebuffer(device, framebuffer, nullptr);
            vkDestroyRenderPass(device, render_pass, nullptr);
            vkDestroyPipelineLayout(device, pipeline_layout, nullptr);
            vkDestroyDescriptorPool(device, descriptor_pool, nullptr);
            vkDestroyDescriptorSetLayout(device, set_layout, nullptr);
            vkDestroySampler(device, sampler, nullptr);
            for (auto* image : {&target, &texture}) {
                vkDestroyImageView(device, image->view, nullptr);
                vkDestroyImage(device, image->handle, nullptr);
                vkFreeMemory(device, image->memory, nullptr);
            }
            for (auto* buffer : {&readback, &upload}) {
                vkDestroyBuffer(device, buffer->handle, nullptr);
                vkFreeMemory(device, buffer->memory, nullptr);
            }
            vkDestroyDevice(device, nullptr);
        }
        if (instance)
            vkDestroyInstance(instance, nullptr);
    }

    void initialize(uint32_t device_index, bool single_mip, std::string_view mode,
                    uint32_t view_base, uint32_t view_count);
    uint32_t memory_type(uint32_t bits, VkMemoryPropertyFlags required, VkMemoryPropertyFlags preferred) const;
    void make_buffer(Buffer& buffer, VkDeviceSize size, VkBufferUsageFlags usage) const;
    void make_image(Image& image, VkImageUsageFlags usage, uint32_t mips,
                    uint32_t view_base, uint32_t view_count) const;
    VkShaderModule load_shader(const std::filesystem::path& path) const;
    void make_descriptors(bool single_mip);
    void make_pipeline(const std::filesystem::path& vertex_path, const std::filesystem::path& fragment_path);
    void copy_input(const std::filesystem::path& bundle, const std::string& stem) const;
    void record_and_submit();
    void save_output(const std::filesystem::path& path) const;
};

void Draw::initialize(uint32_t device_index, bool single_mip, std::string_view mode,
                      uint32_t view_base, uint32_t view_count)
{
    const char* properties_extension = VK_KHR_GET_PHYSICAL_DEVICE_PROPERTIES_2_EXTENSION_NAME;
    if (!single_mip) {
        uint32_t count = 0;
        check(vkEnumerateInstanceExtensionProperties(nullptr, &count, nullptr), "Enumerate instance extensions");
        std::vector<VkExtensionProperties> extensions(count);
        check(vkEnumerateInstanceExtensionProperties(nullptr, &count, extensions.data()),
              "Read instance extensions");
        if (!has_extension(extensions, properties_extension))
            throw Skip("VK_KHR_get_physical_device_properties2 unavailable");
    }

    auto app = info<VkApplicationInfo>(VK_STRUCTURE_TYPE_APPLICATION_INFO);
    app.pApplicationName = "gfx1030 gather bias probe";
    app.apiVersion = VK_API_VERSION_1_0;
    auto instance_info = info<VkInstanceCreateInfo>(VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO);
    instance_info.pApplicationInfo = &app;
    instance_info.enabledExtensionCount = single_mip ? 0 : 1;
    instance_info.ppEnabledExtensionNames = single_mip ? nullptr : &properties_extension;
    const VkResult instance_result = vkCreateInstance(&instance_info, nullptr, &instance);
    if (instance_result == VK_ERROR_INCOMPATIBLE_DRIVER)
        throw Skip("Vulkan 1.0 driver unavailable");
    check(instance_result, "Create Vulkan instance");

    uint32_t count = 0;
    check(vkEnumeratePhysicalDevices(instance, &count, nullptr), "Enumerate physical devices");
    if (!count)
        throw Skip("No Vulkan physical device");
    if (device_index >= count)
        throw std::runtime_error("Device index exceeds the number of Vulkan physical devices");
    std::vector<VkPhysicalDevice> physical_devices(count);
    check(vkEnumeratePhysicalDevices(instance, &count, physical_devices.data()), "Read physical devices");
    physical = physical_devices.at(device_index);

    VkPhysicalDeviceProperties props{};
    vkGetPhysicalDeviceProperties(physical, &props);
    std::cout << "device_index=" << device_index << " device=" << std::quoted(props.deviceName)
              << " vendor_id=0x" << std::hex << props.vendorID << " device_id=0x" << props.deviceID
              << " driver_version=0x" << props.driverVersion << std::dec
              << " device_api=" << VK_VERSION_MAJOR(props.apiVersion) << '.' << VK_VERSION_MINOR(props.apiVersion)
              << '.' << VK_VERSION_PATCH(props.apiVersion) << " mode=" << mode
              << " view_base_mip=" << view_base << " view_mip_count=" << view_count << '\n';

    const char* gather_extension = VK_AMD_TEXTURE_GATHER_BIAS_LOD_EXTENSION_NAME;
    if (!single_mip) {
        check(vkEnumerateDeviceExtensionProperties(physical, nullptr, &count, nullptr), "Enumerate device extensions");
        std::vector<VkExtensionProperties> extensions(count);
        check(vkEnumerateDeviceExtensionProperties(physical, nullptr, &count, extensions.data()), "Read device extensions");
        if (!has_extension(extensions, gather_extension))
            throw Skip("VK_AMD_texture_gather_bias_lod unavailable");
    }

    VkFormatProperties format{};
    vkGetPhysicalDeviceFormatProperties(physical, kFormat, &format);
    constexpr VkFormatFeatureFlags required_format = VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT |
        VK_FORMAT_FEATURE_SAMPLED_IMAGE_FILTER_LINEAR_BIT | VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BIT;
    if ((format.optimalTilingFeatures & required_format) != required_format)
        throw Skip("RGBA32F lacks sampled linear filter or color attachment support");

    if (!single_mip) {
        const auto query = reinterpret_cast<PFN_vkGetPhysicalDeviceImageFormatProperties2KHR>(
            vkGetInstanceProcAddr(instance, "vkGetPhysicalDeviceImageFormatProperties2KHR"));
        if (!query)
            throw std::runtime_error("Missing vkGetPhysicalDeviceImageFormatProperties2KHR entry point");
        auto format_info = info<VkPhysicalDeviceImageFormatInfo2KHR>(VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_FORMAT_INFO_2_KHR);
        format_info.format = kFormat;
        format_info.type = VK_IMAGE_TYPE_2D;
        format_info.tiling = VK_IMAGE_TILING_OPTIMAL;
        format_info.usage = kTextureUsage;
        auto gather_props = info<VkTextureLODGatherFormatPropertiesAMD>(VK_STRUCTURE_TYPE_TEXTURE_LOD_GATHER_FORMAT_PROPERTIES_AMD);
        auto image_props = info<VkImageFormatProperties2KHR>(VK_STRUCTURE_TYPE_IMAGE_FORMAT_PROPERTIES_2_KHR);
        image_props.pNext = &gather_props;
        const VkResult result = query(physical, &format_info, &image_props);
        if (result == VK_ERROR_FORMAT_NOT_SUPPORTED)
            throw Skip("RGBA32F sampled image format configuration unavailable");
        check(result, "Query AMD gather format support");
        if (!gather_props.supportsTextureGatherLODBiasAMD)
            throw Skip("RGBA32F supportsTextureGatherLODBiasAMD is false");
    }

    vkGetPhysicalDeviceQueueFamilyProperties(physical, &count, nullptr);
    std::vector<VkQueueFamilyProperties> families(count);
    vkGetPhysicalDeviceQueueFamilyProperties(physical, &count, families.data());
    for (graphics_family = 0; graphics_family < count; ++graphics_family) {
        if (families[graphics_family].queueCount && (families[graphics_family].queueFlags & VK_QUEUE_GRAPHICS_BIT))
            break;
    }
    if (graphics_family == count)
        throw Skip("No Vulkan graphics queue");

    const float priority = 1.0f;
    auto queue_info = info<VkDeviceQueueCreateInfo>(VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO);
    queue_info.queueFamilyIndex = graphics_family;
    queue_info.queueCount = 1;
    queue_info.pQueuePriorities = &priority;
    auto device_info = info<VkDeviceCreateInfo>(VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO);
    device_info.queueCreateInfoCount = 1;
    device_info.pQueueCreateInfos = &queue_info;
    device_info.enabledExtensionCount = single_mip ? 0 : 1;
    device_info.ppEnabledExtensionNames = single_mip ? nullptr : &gather_extension;
    check(vkCreateDevice(physical, &device_info, nullptr, &device), "Create Vulkan device");
    vkGetDeviceQueue(device, graphics_family, 0, &queue);
    vkGetPhysicalDeviceMemoryProperties(physical, &memory_properties);

    auto pool_info = info<VkCommandPoolCreateInfo>(VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO);
    pool_info.queueFamilyIndex = graphics_family;
    check(vkCreateCommandPool(device, &pool_info, nullptr, &command_pool), "Create command pool");
    auto command_info = info<VkCommandBufferAllocateInfo>(VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO);
    command_info.commandPool = command_pool;
    command_info.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    command_info.commandBufferCount = 1;
    check(vkAllocateCommandBuffers(device, &command_info, &command), "Allocate command buffer");
}

uint32_t Draw::memory_type(uint32_t bits, VkMemoryPropertyFlags required, VkMemoryPropertyFlags preferred) const
{
    uint32_t fallback = memory_properties.memoryTypeCount;
    for (uint32_t i = 0; i < memory_properties.memoryTypeCount; ++i) {
        const auto flags = memory_properties.memoryTypes[i].propertyFlags;
        if (!(bits & (1u << i)) || (flags & required) != required)
            continue;
        if ((flags & preferred) == preferred)
            return i;
        fallback = i;
    }
    if (fallback == memory_properties.memoryTypeCount)
        throw std::runtime_error("No compatible Vulkan memory type");
    return fallback;
}

void Draw::make_buffer(Buffer& buffer, VkDeviceSize size, VkBufferUsageFlags usage) const
{
    auto create = info<VkBufferCreateInfo>(VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO);
    create.size = size;
    create.usage = usage;
    create.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    check(vkCreateBuffer(device, &create, nullptr, &buffer.handle), "Create transfer buffer");
    VkMemoryRequirements requirements{};
    vkGetBufferMemoryRequirements(device, buffer.handle, &requirements);
    auto allocation = info<VkMemoryAllocateInfo>(VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO);
    allocation.allocationSize = requirements.size;
    allocation.memoryTypeIndex = memory_type(requirements.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT,
                                            VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    buffer.coherent = (memory_properties.memoryTypes[allocation.memoryTypeIndex].propertyFlags &
                       VK_MEMORY_PROPERTY_HOST_COHERENT_BIT) != 0;
    check(vkAllocateMemory(device, &allocation, nullptr, &buffer.memory), "Allocate transfer memory");
    check(vkBindBufferMemory(device, buffer.handle, buffer.memory, 0), "Bind transfer memory");
}

void Draw::make_image(Image& image, VkImageUsageFlags usage, uint32_t mips,
                      uint32_t view_base, uint32_t view_count) const
{
    auto create = info<VkImageCreateInfo>(VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO);
    create.imageType = VK_IMAGE_TYPE_2D;
    create.format = kFormat;
    create.extent = {kSide, kSide, 1};
    create.mipLevels = mips;
    create.arrayLayers = 1;
    create.samples = VK_SAMPLE_COUNT_1_BIT;
    create.tiling = VK_IMAGE_TILING_OPTIMAL;
    create.usage = usage;
    create.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    create.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    check(vkCreateImage(device, &create, nullptr, &image.handle), "Create RGBA32F image");
    VkMemoryRequirements requirements{};
    vkGetImageMemoryRequirements(device, image.handle, &requirements);
    auto allocation = info<VkMemoryAllocateInfo>(VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO);
    allocation.allocationSize = requirements.size;
    allocation.memoryTypeIndex = memory_type(requirements.memoryTypeBits, 0, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    check(vkAllocateMemory(device, &allocation, nullptr, &image.memory), "Allocate image memory");
    check(vkBindImageMemory(device, image.handle, image.memory, 0), "Bind image memory");

    auto view = info<VkImageViewCreateInfo>(VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO);
    view.image = image.handle;
    view.viewType = VK_IMAGE_VIEW_TYPE_2D;
    view.format = kFormat;
    view.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, view_base, view_count, 0, 1};
    check(vkCreateImageView(device, &view, nullptr, &image.view), "Create RGBA32F image view");
}

VkShaderModule Draw::load_shader(const std::filesystem::path& path) const
{
    auto file = open_input(path);
    const auto size = static_cast<std::streamoff>(file.tellg());
    if (size < 20 || size % sizeof(uint32_t) || size > std::numeric_limits<std::streamsize>::max())
        throw std::runtime_error("Invalid SPIR-V size: " + path.string());
    std::vector<uint32_t> words(static_cast<size_t>(size) / sizeof(uint32_t));
    if (!file.seekg(0) || !file.read(reinterpret_cast<char*>(words.data()), size) || words.front() != 0x07230203)
        throw std::runtime_error("Invalid SPIR-V data: " + path.string());
    auto module_info = info<VkShaderModuleCreateInfo>(VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO);
    module_info.codeSize = static_cast<size_t>(size);
    module_info.pCode = words.data();
    VkShaderModule module{};
    check(vkCreateShaderModule(device, &module_info, nullptr, &module), "Create shader module");
    return module;
}

void Draw::make_descriptors(bool single_mip)
{
    auto sampler_info = info<VkSamplerCreateInfo>(VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO);
    sampler_info.magFilter = VK_FILTER_LINEAR;
    sampler_info.minFilter = VK_FILTER_LINEAR;
    sampler_info.mipmapMode = VK_SAMPLER_MIPMAP_MODE_NEAREST;
    sampler_info.addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    sampler_info.addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    sampler_info.addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    sampler_info.maxLod = single_mip ? 0.0f : static_cast<float>(kMips - 1);
    check(vkCreateSampler(device, &sampler_info, nullptr, &sampler), "Create sampler");

    const VkDescriptorSetLayoutBinding binding{0, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1,
                                                VK_SHADER_STAGE_FRAGMENT_BIT, nullptr};
    auto layout = info<VkDescriptorSetLayoutCreateInfo>(VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO);
    layout.bindingCount = 1;
    layout.pBindings = &binding;
    check(vkCreateDescriptorSetLayout(device, &layout, nullptr, &set_layout), "Create descriptor layout");
    const VkDescriptorPoolSize size{VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1};
    auto pool = info<VkDescriptorPoolCreateInfo>(VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO);
    pool.maxSets = 1;
    pool.poolSizeCount = 1;
    pool.pPoolSizes = &size;
    check(vkCreateDescriptorPool(device, &pool, nullptr, &descriptor_pool), "Create descriptor pool");
    auto allocate = info<VkDescriptorSetAllocateInfo>(VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO);
    allocate.descriptorPool = descriptor_pool;
    allocate.descriptorSetCount = 1;
    allocate.pSetLayouts = &set_layout;
    check(vkAllocateDescriptorSets(device, &allocate, &descriptors), "Allocate descriptor set");
    const VkDescriptorImageInfo sampled{sampler, texture.view, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL};
    auto write = info<VkWriteDescriptorSet>(VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET);
    write.dstSet = descriptors;
    write.dstBinding = binding.binding;
    write.descriptorCount = 1;
    write.descriptorType = binding.descriptorType;
    write.pImageInfo = &sampled;
    vkUpdateDescriptorSets(device, 1, &write, 0, nullptr);

    auto pipeline_info = info<VkPipelineLayoutCreateInfo>(VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO);
    pipeline_info.setLayoutCount = 1;
    pipeline_info.pSetLayouts = &set_layout;
    check(vkCreatePipelineLayout(device, &pipeline_info, nullptr, &pipeline_layout), "Create pipeline layout");
}

void Draw::make_pipeline(const std::filesystem::path& vertex_path, const std::filesystem::path& fragment_path)
{
    VkAttachmentDescription attachment{};
    attachment.format = kFormat;
    attachment.samples = VK_SAMPLE_COUNT_1_BIT;
    attachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    attachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    attachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    attachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    attachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    attachment.finalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    const VkAttachmentReference color{0, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = 1;
    subpass.pColorAttachments = &color;
    auto pass = info<VkRenderPassCreateInfo>(VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO);
    pass.attachmentCount = 1;
    pass.pAttachments = &attachment;
    pass.subpassCount = 1;
    pass.pSubpasses = &subpass;
    check(vkCreateRenderPass(device, &pass, nullptr, &render_pass), "Create render pass");

    auto frame = info<VkFramebufferCreateInfo>(VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO);
    frame.renderPass = render_pass;
    frame.attachmentCount = 1;
    frame.pAttachments = &target.view;
    frame.width = kSide;
    frame.height = kSide;
    frame.layers = 1;
    check(vkCreateFramebuffer(device, &frame, nullptr, &framebuffer), "Create framebuffer");

    vertex = load_shader(vertex_path);
    fragment = load_shader(fragment_path);
    std::array stages{
        info<VkPipelineShaderStageCreateInfo>(VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO),
        info<VkPipelineShaderStageCreateInfo>(VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO),
    };
    stages[0].stage = VK_SHADER_STAGE_VERTEX_BIT;
    stages[0].module = vertex;
    stages[0].pName = "main";
    stages[1].stage = VK_SHADER_STAGE_FRAGMENT_BIT;
    stages[1].module = fragment;
    stages[1].pName = "main";
    const auto vertex_input = info<VkPipelineVertexInputStateCreateInfo>(VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO);
    auto assembly = info<VkPipelineInputAssemblyStateCreateInfo>(VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO);
    assembly.topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;
    const VkViewport viewport{0.0f, 0.0f, kSide, kSide, 0.0f, 1.0f};
    const VkRect2D scissor{{0, 0}, {kSide, kSide}};
    auto viewport_state = info<VkPipelineViewportStateCreateInfo>(VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO);
    viewport_state.viewportCount = 1;
    viewport_state.pViewports = &viewport;
    viewport_state.scissorCount = 1;
    viewport_state.pScissors = &scissor;
    auto raster = info<VkPipelineRasterizationStateCreateInfo>(VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO);
    raster.polygonMode = VK_POLYGON_MODE_FILL;
    raster.cullMode = VK_CULL_MODE_NONE;
    raster.frontFace = VK_FRONT_FACE_COUNTER_CLOCKWISE;
    raster.lineWidth = 1.0f;
    auto samples = info<VkPipelineMultisampleStateCreateInfo>(VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO);
    samples.rasterizationSamples = VK_SAMPLE_COUNT_1_BIT;
    VkPipelineColorBlendAttachmentState rgba{};
    rgba.colorWriteMask = VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT |
                          VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT;
    auto blend = info<VkPipelineColorBlendStateCreateInfo>(VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO);
    blend.attachmentCount = 1;
    blend.pAttachments = &rgba;
    auto graphics = info<VkGraphicsPipelineCreateInfo>(VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO);
    graphics.stageCount = static_cast<uint32_t>(stages.size());
    graphics.pStages = stages.data();
    graphics.pVertexInputState = &vertex_input;
    graphics.pInputAssemblyState = &assembly;
    graphics.pViewportState = &viewport_state;
    graphics.pRasterizationState = &raster;
    graphics.pMultisampleState = &samples;
    graphics.pColorBlendState = &blend;
    graphics.layout = pipeline_layout;
    graphics.renderPass = render_pass;
    check(vkCreateGraphicsPipelines(device, VK_NULL_HANDLE, 1, &graphics, nullptr, &pipeline),
          "Create gather graphics pipeline");
}

void Draw::copy_input(const std::filesystem::path& bundle, const std::string& stem) const
{
    std::array<char, kUploadBytes> bytes{};
    for (uint32_t mip = 0; mip < kMips; ++mip) {
        const auto side = kSide >> mip;
        const auto path = bundle / (stem + ".mip" + std::to_string(mip) + ".rgba32f");
        read_mip(path, bytes.data() + kMipOffsets[mip], side * side * kPixelBytes);
    }
    void* mapped = nullptr;
    check(vkMapMemory(device, upload.memory, 0, VK_WHOLE_SIZE, 0, &mapped), "Map upload buffer");
    std::memcpy(mapped, bytes.data(), bytes.size());
    VkResult result = VK_SUCCESS;
    if (!upload.coherent) {
        auto range = info<VkMappedMemoryRange>(VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE);
        range.memory = upload.memory;
        range.size = VK_WHOLE_SIZE;
        result = vkFlushMappedMemoryRanges(device, 1, &range);
    }
    vkUnmapMemory(device, upload.memory);
    check(result, "Flush upload buffer");
}

void image_barrier(VkCommandBuffer command, VkImage image, uint32_t mips,
                   VkPipelineStageFlags source_stage, VkPipelineStageFlags dest_stage,
                   VkAccessFlags source_access, VkAccessFlags dest_access,
                   VkImageLayout old_layout, VkImageLayout new_layout)
{
    auto barrier = info<VkImageMemoryBarrier>(VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER);
    barrier.srcAccessMask = source_access;
    barrier.dstAccessMask = dest_access;
    barrier.oldLayout = old_layout;
    barrier.newLayout = new_layout;
    barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.image = image;
    barrier.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, mips, 0, 1};
    vkCmdPipelineBarrier(command, source_stage, dest_stage, 0, 0, nullptr, 0, nullptr, 1, &barrier);
}

void Draw::record_and_submit()
{
    auto begin = info<VkCommandBufferBeginInfo>(VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO);
    begin.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    check(vkBeginCommandBuffer(command, &begin), "Begin recording");
    image_barrier(command, texture.handle, kMips, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                  VK_PIPELINE_STAGE_TRANSFER_BIT, 0, VK_ACCESS_TRANSFER_WRITE_BIT,
                  VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);
    std::array<VkBufferImageCopy, kMips> uploads{};
    for (uint32_t mip = 0; mip < kMips; ++mip) {
        auto& region = uploads[mip];
        region.bufferOffset = kMipOffsets[mip];
        region.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, mip, 0, 1};
        region.imageExtent = {kSide >> mip, kSide >> mip, 1};
    }
    vkCmdCopyBufferToImage(command, upload.handle, texture.handle, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                          kMips, uploads.data());
    image_barrier(command, texture.handle, kMips, VK_PIPELINE_STAGE_TRANSFER_BIT,
                  VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT,
                  VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL);

    const VkClearValue clear{{{-1.0f, -1.0f, -1.0f, -1.0f}}};
    auto pass = info<VkRenderPassBeginInfo>(VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO);
    pass.renderPass = render_pass;
    pass.framebuffer = framebuffer;
    pass.renderArea = {{0, 0}, {kSide, kSide}};
    pass.clearValueCount = 1;
    pass.pClearValues = &clear;
    vkCmdBeginRenderPass(command, &pass, VK_SUBPASS_CONTENTS_INLINE);
    vkCmdBindPipeline(command, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline);
    vkCmdBindDescriptorSets(command, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline_layout, 0, 1, &descriptors, 0, nullptr);
    vkCmdDraw(command, 3, 1, 0, 0);
    vkCmdEndRenderPass(command);

    image_barrier(command, target.handle, 1, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
                  VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT,
                  VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
    VkBufferImageCopy download{};
    download.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    download.imageExtent = {kSide, kSide, 1};
    vkCmdCopyImageToBuffer(command, target.handle, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, readback.handle, 1, &download);
    auto host = info<VkBufferMemoryBarrier>(VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER);
    host.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    host.dstAccessMask = VK_ACCESS_HOST_READ_BIT;
    host.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    host.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    host.buffer = readback.handle;
    host.size = VK_WHOLE_SIZE;
    vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT,
                         0, 0, nullptr, 1, &host, 0, nullptr);
    check(vkEndCommandBuffer(command), "Finish recording");
    auto submit = info<VkSubmitInfo>(VK_STRUCTURE_TYPE_SUBMIT_INFO);
    submit.commandBufferCount = 1;
    submit.pCommandBuffers = &command;
    check(vkQueueSubmit(queue, 1, &submit, VK_NULL_HANDLE), "Submit gather draw");
    check(vkQueueWaitIdle(queue), "Wait for gather readback");
}

void Draw::save_output(const std::filesystem::path& path) const
{
    std::array<char, kOutputBytes> bytes{};
    void* mapped = nullptr;
    check(vkMapMemory(device, readback.memory, 0, VK_WHOLE_SIZE, 0, &mapped), "Map readback buffer");
    VkResult result = VK_SUCCESS;
    if (!readback.coherent) {
        auto range = info<VkMappedMemoryRange>(VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE);
        range.memory = readback.memory;
        range.size = VK_WHOLE_SIZE;
        result = vkInvalidateMappedMemoryRanges(device, 1, &range);
    }
    if (result == VK_SUCCESS)
        std::memcpy(bytes.data(), mapped, bytes.size());
    vkUnmapMemory(device, readback.memory);
    check(result, "Invalidate readback buffer");

    std::ofstream file(path, std::ios::binary | std::ios::trunc);
    file.write(bytes.data(), bytes.size());
    file.close();
    if (!file)
        throw std::runtime_error("Cannot write " + path.string());
}

}  // namespace

int main(int argc, char** argv)
{
    if (argc != 6 && argc != 7) {
        std::cerr << "Usage: " << argv[0]
                  << " <bundle-dir> <case-stem> <bias|explicit|single-mip> <base-mip> <output.rgba32f> [device-index]\n";
        return 1;
    }
    try {
        const std::filesystem::path bundle = argv[1];
        const std::string stem = argv[2];
        const std::string mode = argv[3];
        const bool single_mip = mode == "single-mip";
        if (!single_mip && mode != "bias" && mode != "explicit")
            throw std::runtime_error("Invalid mode");
        const uint32_t base_mip = parse_index(argv[4], "base mip");
        if (base_mip >= kMips)
            throw std::runtime_error("Base mip must be in [0, 2]");
        if (!single_mip && base_mip != 0)
            throw std::runtime_error("Full-view modes require base mip 0");
        const uint32_t device_index = argc == 7 ? parse_index(argv[6], "device index") : 0;
        const uint32_t view_base = single_mip ? base_mip : 0;
        const uint32_t view_count = single_mip ? 1 : kMips;

        Draw draw;
        draw.initialize(device_index, single_mip, mode, view_base, view_count);
        draw.make_buffer(draw.upload, kUploadBytes, VK_BUFFER_USAGE_TRANSFER_SRC_BIT);
        draw.make_buffer(draw.readback, kOutputBytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
        draw.make_image(draw.texture, kTextureUsage, kMips, view_base, view_count);
        draw.make_image(draw.target, kTargetUsage, 1, 0, 1);
        draw.copy_input(bundle, stem);
        draw.make_descriptors(single_mip);
        draw.make_pipeline(bundle / "fullscreen.spv", bundle / (stem + "-" + mode + ".spv"));
        draw.record_and_submit();
        draw.save_output(argv[5]);
        std::cout << "case=" << stem << " wrote_bytes=" << kOutputBytes << '\n';
        return 0;
    } catch (const Skip& skip) {
        std::cout << "SKIP: " << skip.what() << '\n';
        return 77;
    } catch (const std::exception& error) {
        std::cerr << "Vulkan gather runner: " << error.what() << '\n';
        return 1;
    }
}
