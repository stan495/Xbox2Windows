// Included after the resource creation and descriptor handlers.
struct MemoryShaderResourceView {
    D3D12_SHADER_RESOURCE_VIEW_DESC view;
    u64 address;
};
static_assert(offsetof(MemoryShaderResourceView, address) == 0x28);

bool same_texture_description(const D3D12_RESOURCE_DESC& a, const D3D12_RESOURCE_DESC& b)
{
    return a.Dimension == b.Dimension && a.Alignment == b.Alignment && a.Width == b.Width &&
           a.Height == b.Height && a.DepthOrArraySize == b.DepthOrArraySize && a.MipLevels == b.MipLevels &&
           a.Format == b.Format && a.SampleDesc.Count == b.SampleDesc.Count &&
           a.SampleDesc.Quality == b.SampleDesc.Quality && a.Layout == b.Layout && a.Flags == b.Flags;
}

bool tiled_bc_srv(const D3D12_RESOURCE_DESC& r)
{
    return r.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D && r.DepthOrArraySize == 1 &&
           r.MipLevels == 1 && r.SampleDesc.Count == 1 && r.SampleDesc.Quality == 0 &&
           (r.Format == DXGI_FORMAT_BC3_UNORM || r.Format == DXGI_FORMAT_BC7_UNORM) &&
           static_cast<UINT>(r.Layout) == 0x105 && r.Alignment == 0 &&
           (r.Flags == D3D12_RESOURCE_FLAG_NONE || static_cast<UINT>(r.Flags) == 0x300000) &&
           r.Width <= D3D12_REQ_TEXTURE2D_U_OR_V_DIMENSION && r.Height <= D3D12_REQ_TEXTURE2D_U_OR_V_DIMENSION &&
           // A single mip pads its pitch and height to whole 4 KiB tiles (Bc16Surface); the desktop
           // texture needs whole 4x4 blocks.
           r.Width && r.Height && r.Width % 4 == 0 && r.Height % 4 == 0;
}

// One line for the first publication of a view whose last tile row or column is partial. A published
// view gets a content summary, so a title run can tell console-tiled data from zeros; a held one names
// why it stays refused.
void log_partial_tile_view(const D3D12_RESOURCE_DESC& r, u64 address, const char* held, bool published)
{
    static LONG lines, unpublished;
    const auto bytes = x2w_startup::Bc16Surface(r).size();
    if (!published) {
        if (first_calls(unpublished, 8))
            log("x2w-d3d12 Device::slot70 partial-tile BC view %llux%u format %d layout 0x%x: %s, %llu tiled bytes",
                r.Width, r.Height, r.Format, static_cast<unsigned>(r.Layout), held ? held : "texture_unavailable", bytes);
        return;
    }
    if (!first_calls(lines, 64)) return;
    const auto* data = reinterpret_cast<const unsigned char*>(address);
    const auto nonzero = [&](u64 from) {
        unsigned count = 0;
        for (u64 i = from; i < from + 4096; ++i) count += data[i] != 0;
        return count;
    };
    // publish_tiled_texture already found the whole range readable.
    log("x2w-d3d12 Device::slot70 partial-tile BC view %llux%u format %d layout 0x%x: published, %llu tiled bytes, "
        "nonzero first/last 4 KiB %u/%u hash %016llx", r.Width, r.Height, r.Format, static_cast<unsigned>(r.Layout),
        bytes, nonzero(0), nonzero(bytes - 4096), hash_memory(data, static_cast<std::size_t>(bytes)));
}

// The size the XG stand-in reports for a description: desktop copyable footprints rounded to 64 KiB.
u64 xg_reported_bytes(Wrapper* device, const D3D12_RESOURCE_DESC& desc)
{
    const x2w_startup::SliceFootprints plan(device_of(device), sanitized(desc), 1);
    return plan.size == UINT64_MAX ? 0 : (plan.size + 0xFFFF) & ~u64{0xFFFF};
}

bool supports_placed_memory_srv(const D3D12_RESOURCE_DESC& r, const D3D12_SHADER_RESOURCE_VIEW_DESC& v);
// The shapes slot 70 took before address_texture.h, or a texture in a console layout gfx10_layout.h
// addresses with a view that stays inside it.
bool supports_memory_srv(const D3D12_RESOURCE_DESC& r, const D3D12_SHADER_RESOURCE_VIEW_DESC& v)
{
    if (supports_placed_memory_srv(r, v)) return true;
    return console_layout_srv(r) && x2w_startup::address_view_fits(r, v);
}

bool supports_placed_memory_srv(const D3D12_RESOURCE_DESC& r, const D3D12_SHADER_RESOURCE_VIEW_DESC& v)
{
    if (r.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER)
        return x2w_startup::supports_buffer_memory_srv(r, v);
    if (x2w_startup::bc64::supports(r)) {
        if (v.Format != r.Format || v.Shader4ComponentMapping != D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING)
            return false;
        if (v.ViewDimension == D3D12_SRV_DIMENSION_TEXTURE2DARRAY) {
            const auto& a = v.Texture2DArray;
            return r.DepthOrArraySize == 2 && a.FirstArraySlice < r.DepthOrArraySize && a.ArraySize > 0 &&
                   a.ArraySize <= r.DepthOrArraySize - a.FirstArraySlice && a.MostDetailedMip < r.MipLevels &&
                   a.MipLevels > 0 && a.MipLevels <= r.MipLevels - a.MostDetailedMip &&
                   a.PlaneSlice == 0 && a.ResourceMinLODClamp == 0;
        }
        return r.DepthOrArraySize == 1 && v.ViewDimension == D3D12_SRV_DIMENSION_TEXTURE2D &&
               v.Texture2D.MostDetailedMip < r.MipLevels && v.Texture2D.MipLevels > 0 &&
               v.Texture2D.MipLevels <= r.MipLevels - v.Texture2D.MostDetailedMip &&
               !v.Texture2D.PlaneSlice && v.Texture2D.ResourceMinLODClamp == 0;
    }
    if (x2w_startup::supports_single_element_srv(r, v)) return true;
    return r.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D && r.Width && r.Height &&
           r.DepthOrArraySize == 1 && r.MipLevels == 1 && r.SampleDesc.Count == 1 && r.SampleDesc.Quality == 0 &&
           (r.Format == DXGI_FORMAT_R8_UNORM || tiled_bc_srv(r)) && v.Format == r.Format && !(r.Flags & D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE) &&
           v.ViewDimension == D3D12_SRV_DIMENSION_TEXTURE2D &&
           v.Shader4ComponentMapping == D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING &&
           v.Texture2D.MostDetailedMip == 0 && v.Texture2D.MipLevels == 1 && v.Texture2D.PlaneSlice == 0 &&
           v.Texture2D.ResourceMinLODClamp == 0.0f;
}

// The layout tables describe the bytes independently of the desktop adapter's format support.
bool desktop_address_view_supported(Wrapper* device, const D3D12_RESOURCE_DESC& r,
                                    const D3D12_SHADER_RESOURCE_VIEW_DESC& v)
{
    auto* const desktop = device_of(device);
    D3D12_FEATURE_DATA_FORMAT_INFO planes{r.Format};
    if (FAILED(desktop->CheckFeatureSupport(D3D12_FEATURE_FORMAT_INFO, &planes, sizeof(planes))) || planes.PlaneCount != 1)
        return false;

    D3D12_FEATURE_DATA_FORMAT_SUPPORT view{v.Format};
    if (FAILED(desktop->CheckFeatureSupport(D3D12_FEATURE_FORMAT_SUPPORT, &view, sizeof(view))) ||
        !(view.Support1 & (D3D12_FORMAT_SUPPORT1_SHADER_LOAD | D3D12_FORMAT_SUPPORT1_SHADER_SAMPLE)))
        return false;

    D3D12_FEATURE_DATA_FORMAT_SUPPORT storage{r.Format};
    if (r.Format == v.Format) storage = view;
    else if (FAILED(desktop->CheckFeatureSupport(D3D12_FEATURE_FORMAT_SUPPORT, &storage, sizeof(storage)))) return false;
    return (storage.Support1 & D3D12_FORMAT_SUPPORT1_TEXTURE2D) &&
           (r.MipLevels == 1 || (storage.Support1 & D3D12_FORMAT_SUPPORT1_MIP));
}

bool memory_srv_destination(u64 handle)
{
    bool valid = false;
    AcquireSRWLockShared(&note_lock);
    for (const auto& heap : heap_infos()) {
        if (heap.type != D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV || !heap.increment) continue;
        const u64 base = heap.title_cpu ? heap.title_cpu : heap.desktop_cpu;
        if (handle >= base && (handle - base) % heap.increment == 0 &&
            (handle - base) / heap.increment < heap.count) {
            valid = true;
            break;
        }
    }
    ReleaseSRWLockShared(&note_lock);
    return valid;
}

Wrapper* existing_memory_texture(u64 address, const D3D12_RESOURCE_DESC& desc)
{
    Wrapper* match = nullptr;
    AcquireSRWLockShared(&note_lock);
    for (const auto& [wrapper, info] : resource_infos()) {
        if (reinterpret_cast<u64>(wrapper->own().title_memory) != address ||
            wrapper->own().tiled_address_srv ||
            InterlockedCompareExchange(&wrapper->own().refs, 0, 0) <= 0 ||
            !same_texture_description(desc, info.console)) continue;
        if (match) {
            match = nullptr;
            break;
        }
        match = const_cast<Wrapper*>(wrapper);
    }
    ReleaseSRWLockShared(&note_lock);
    return match;
}

// An unmatched or interior view must not reinterpret bytes owned by a live resource.
bool address_resource_overlap(u64 address, u64 span)
{
    bool found = false;
    AcquireSRWLockShared(&note_lock);
    for (const auto& [wrapper, info] : resource_infos()) {
        if (wrapper->own().tiled_address_srv || InterlockedCompareExchange(&wrapper->own().refs, 0, 0) <= 0)
            continue;
        const u64 base = reinterpret_cast<u64>(wrapper->own().title_memory);
        const u64 bytes = info.bytes ? info.bytes
            : info.console.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER ? info.console.Width : 0;
        if (base && (address <= base ? base - address < span : address - base < bytes)) {
            found = true;
            break;
        }
    }
    ReleaseSRWLockShared(&note_lock);
    return found;
}

// A live render-target, depth or UAV resource overlapping the texel is written on the desktop side, so its
// title memory does not hold the texel. For a resource of unknown size, its base is the only known byte.
bool gpu_written_overlap(u64 address, UINT texel_bytes)
{
    constexpr UINT gpu_written = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET | D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL |
                                 D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    bool found = false;
    AcquireSRWLockShared(&note_lock);
    for (const auto& [wrapper, info] : resource_infos()) {
        const u64 base = reinterpret_cast<u64>(wrapper->own().title_memory);
        const u64 bytes = info.bytes ? info.bytes : info.console.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER ? info.console.Width : 0;
        const bool overlap = address <= base ? base - address < texel_bytes : address - base < bytes;
        if (base && overlap &&
            (static_cast<UINT>(info.console.Flags) & gpu_written) && InterlockedCompareExchange(&wrapper->own().refs, 0, 0) > 0) {
            found = true;
            break;
        }
    }
    ReleaseSRWLockShared(&note_lock);
    return found;
}

Wrapper* existing_memory_buffer(Wrapper* device, u64 address, u64 bytes)
{
    if (!address || !bytes) return nullptr;
    const auto placed = placed_buffer_range(device, address, bytes);
    if (placed.reject) return nullptr;
    if (placed.owner) {
        if (InterlockedCompareExchange(&placed.owner->own().refs, 0, 0) <= 0) return nullptr;
        const auto* info = resource_info(placed.owner);
        if (!info || info->console.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER ||
            (info->console.Flags & D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE))
            return nullptr;
        return const_cast<Wrapper*>(placed.owner);
    }
    Wrapper* match = nullptr;
    AcquireSRWLockShared(&note_lock);
    for (const auto& [wrapper, info] : resource_infos()) {
        if (info.placement != Placement::none ||
            info.console.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER ||
            (info.console.Flags & D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE) ||
            InterlockedCompareExchange(&wrapper->own().refs, 0, 0) <= 0)
            continue;
        const u64 base = reinterpret_cast<u64>(wrapper->own().title_memory);
        const u64 width = info.console.Width;
        if (!base || address < base || address - base >= width || bytes > width - (address - base))
            continue;
        if (match) {
            match = nullptr;
            break;
        }
        match = const_cast<Wrapper*>(wrapper);
    }
    ReleaseSRWLockShared(&note_lock);
    return match;
}

// Keep metadata for published tiled SRVs; allocate GPU memory only when a
// translated draw actually samples one. A later write to the same address is
// picked up by the uploader even when the description and view token are reused.
std::mutex tiled_srv_mutex;
struct TiledAddressTexture {
    Wrapper* device;
    Wrapper* resource;
    D3D12_RESOURCE_DESC desc;
    x2w_startup::ComPtr<ID3D12Resource> desktop;
};
std::unordered_map<Wrapper*, TiledAddressTexture> tiled_srv_textures;
std::unordered_multimap<u64, Wrapper*> tiled_srv_addresses;

bool partial_tiled_bc(const D3D12_RESOURCE_DESC& desc)
{
    return tiled_bc_srv(desc) && (desc.Width % 64 || desc.Height % 64);
}

bool latest_texture_write_is_rows(u64 address)
{
    AcquireSRWLockShared(&written_lock);
    const auto found = written_textures().find(address);
    const bool rows = found != written_textures().end() && found->second.desktop_rows;
    ReleaseSRWLockShared(&written_lock);
    return rows;
}

// A texture slot 70 reads in a console layout of gfx10_layout.h: not one of the shapes it read before
// (the single texel, the 0x105 BC3/BC7 surface and the 0x109 BC chains keep their own paths).
bool console_layout_srv(const D3D12_RESOURCE_DESC& desc)
{
    return x2w_startup::console_address_texture(desc) && !x2w_startup::single_element_bytes(desc) &&
           !tiled_bc_srv(desc) && !x2w_startup::bc64::supports(desc);
}

// One line for each new kind of slot-70 texture call (format, layout, shape class, view and outcome), so
// a run shows every shape a title asks for without a line per texture.
void log_address_kind(const char* outcome, const D3D12_RESOURCE_DESC& r, const D3D12_SHADER_RESOURCE_VIEW_DESC& v, u64 address)
{
    static std::mutex mutex;
    static std::unordered_map<u64, LONG> kinds;
    const u64 words[] = {static_cast<u64>(r.Format), static_cast<u64>(r.Layout), r.MipLevels > 1 ? 2u : 1u,
                         r.DepthOrArraySize > 1 ? 2u : 1u, static_cast<u64>(r.Flags), static_cast<u64>(r.Dimension),
                         static_cast<u64>(v.Format), static_cast<u64>(v.ViewDimension), reinterpret_cast<u64>(outcome)};
    const u64 key = fnv_bytes(words, sizeof(words));
    std::lock_guard lock(mutex);
    if (kinds.size() >= 200 || kinds[key]++) return;
    log("x2w-d3d12 slot70 texture kind %zu: %s; first seen res{dim=%d fmt=%d size=%llux%ux%u mips=%u layout=0x%x flags=0x%x "
        "align=%llu} view{dim=%d fmt=%d map=0x%x} address=0x%llx", kinds.size(), outcome, r.Dimension, r.Format, r.Width,
        r.Height, r.DepthOrArraySize, r.MipLevels, static_cast<UINT>(r.Layout), static_cast<UINT>(r.Flags), r.Alignment,
        v.ViewDimension, v.Format, v.Shader4ComponentMapping, address);
}

// Bytes the texels of a by-address texture span at their address.
u64 address_texture_bytes(const D3D12_RESOURCE_DESC& desc)
{
    if (const UINT element = x2w_startup::single_element_bytes(desc)) return element;
    if (x2w_startup::bc64::supports(desc)) return x2w_startup::bc64::Chain(desc).bytes();
    if (console_layout_srv(desc)) return x2w_startup::gfx10::Layout(desc).bytes();
    return x2w_startup::Bc16Surface(desc).size();
}

// Why a console-layout description is not read from an address, or null. The two cases a partial-tile
// BC view is held for:
// - the latest XG stand-in copy to the address wrote desktop-footprint rows (only the opt-in BC7 4 KiB
//   copy records a tiled write; the 0x109 BC chains, which XG writes in their layout, keep their own
//   path and never come through here);
// - the layout runs past the size the XG stand-in reports for the description (the desktop copyable
//   footprints of a slice rounded up to 64 KiB, times the slices), which the title may not own.
const char* console_address_hold(Wrapper* device, u64 address, const D3D12_RESOURCE_DESC& desc)
{
    if (latest_texture_write_is_rows(address)) return "console_layout_xg_rows";
    const x2w_startup::SliceFootprints plan(device_of(device), sanitized(desc), desc.MipLevels);
    if (plan.size == UINT64_MAX || !plan.size) return "console_layout_without_footprints";
    const u64 reported = ((plan.size + 0xFFFF) & ~u64{0xFFFF}) * desc.DepthOrArraySize;
    return x2w_startup::gfx10::Layout(desc).bytes() > reported ? "console_layout_beyond_xg_size" : nullptr;
}

Wrapper* publish_tiled_texture(Wrapper* device, u64 address, const D3D12_RESOURCE_DESC& desc)
{
    const auto bytes = address_texture_bytes(desc);
    // The transient upload ring accepts individual copies up to 64 MiB.
    if (bytes > (64ull << 20) || !x2w_local::readable(reinterpret_cast<const void*>(address), bytes)) return nullptr;
    std::lock_guard lock(tiled_srv_mutex);
    const auto [first, last] = tiled_srv_addresses.equal_range(address);
    for (auto it = first; it != last; ++it) {
        const auto& known = tiled_srv_textures.at(it->second);
        if (known.device == device && same_texture_description(known.desc, desc)) {
            AcquireSRWLockExclusive(&written_lock);
            ++written_textures()[address].generation;
            ReleaseSRWLockExclusive(&written_lock);
            return known.resource;
        }
    }
    Wrapper* resource = wrap(nullptr, resource_kind);
    resource->own().title_memory = reinterpret_cast<void*>(address);
    resource->own().tiled_address_srv = true;
    note_resource(resource, &desc, bytes);
    tiled_srv_textures.emplace(resource, TiledAddressTexture{device, resource, desc, {}});
    tiled_srv_addresses.emplace(address, resource);
    return resource;
}

ID3D12Resource* sampled_address_texture(Wrapper* resource)
{
    if (!resource || resource->own().kind != resource_kind) return nullptr;
    if (!resource->own().tiled_address_srv) return static_cast<ID3D12Resource*>(resource->own().real);
    std::lock_guard lock(tiled_srv_mutex);
    auto& sampled = tiled_srv_textures.at(resource);
    // An already-published token can outlive a write in a different layout.
    if ((partial_tiled_bc(sampled.desc) || console_layout_srv(sampled.desc)) &&
        latest_texture_write_is_rows(reinterpret_cast<u64>(resource->own().title_memory)))
        return nullptr;
    if (!sampled.desktop) {
        const auto desc = sanitized(sampled.desc);
        const D3D12_HEAP_PROPERTIES heap{D3D12_HEAP_TYPE_DEFAULT};
        const HRESULT hr = device_of(sampled.device)->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc,
            D3D12_RESOURCE_STATE_COMMON, nullptr, IID_PPV_ARGS(&sampled.desktop));
        if (FAILED(hr)) {
            static LONG failures;
            if (first_calls(failures, 8)) log("x2w-d3d12 failed to materialize tiled memory SRV: 0x%08lx", hr);
            return nullptr;
        }
        x2w_budget::committed(device_of(sampled.device), sampled.desktop.Get(), D3D12_HEAP_TYPE_DEFAULT);
        x2w_startup::mark_packed_stand_in(sampled.desktop.Get(), desktop_format(sampled.desc.Format));
    }
    return sampled.desktop.Get();
}

// The first check that refuses a call, in the order the checks run; null when every check passes.
const char* memory_srv_refusal(const D3D12_RESOURCE_DESC* resource, const MemoryShaderResourceView* input, u64 destination)
{
    if (!destination) return "null_destination";
    if (!x2w_local::readable(resource, sizeof(*resource))) return "unreadable_resource";
    if (!x2w_local::readable(input, sizeof(*input))) return "unreadable_view";
    if (!input->address) return "null_address";
    if (!supports_memory_srv(*resource, input->view)) return "unsupported_description";
    if (!memory_srv_destination(destination)) return "destination_outside_view_heaps";
    return nullptr;
}

// One line with every field the checks read; the caller rate-limits it.
void log_refused_memory_srv(const char* what, const char* reason, const D3D12_RESOURCE_DESC* resource,
                            const MemoryShaderResourceView* input, u64 destination)
{
    const bool resource_ok = x2w_local::readable(resource, sizeof(*resource));
    const bool input_ok = x2w_local::readable(input, sizeof(*input));
    const u64 address = input_ok ? input->address : 0;
    char line[640];
    x2w_startup::report_refused_memory_srv(line, sizeof(line), what, reason, resource_ok ? resource : nullptr,
        input_ok ? &input->view : nullptr, address, address && x2w_local::readable(reinterpret_cast<const void*>(address), 1),
        destination, destination && memory_srv_destination(destination));
    log("%s", line);
}

// Counts a refusal (memory_srv_census) and logs its line while the first 8 refusals last, and also for
// the first refusal of each of the first few distinct shapes (MemorySrvCensus::detail_shapes).
void refuse_memory_srv(LONG& refused, const char* what, const char* reason, const D3D12_RESOURCE_DESC* resource,
                       const MemoryShaderResourceView* input, u64 destination)
{
    const bool within_limit = first_calls(refused, 8);
    const bool new_shape = memory_srv_census.note(reason, x2w_local::readable(resource, sizeof(*resource)) ? resource : nullptr);
    if (within_limit || new_shape) log_refused_memory_srv(what, reason, resource, input, destination);
}

// Counts a successful write (memory_srv_writes) and logs the first write to each of the first few
// distinct destination descriptors.
void note_memory_srv_write(const D3D12_RESOURCE_DESC* resource, const MemoryShaderResourceView* input, u64 destination)
{
    const u64 ordinal = memory_srv_writes.note(destination);
    if (!ordinal) return;
    char resource_text[224], view_text[256];
    x2w_startup::report_resource(resource_text, sizeof(resource_text), resource);
    x2w_startup::report_view(view_text, sizeof(view_text), &input->view);
    log("x2w-d3d12 memory SRV written %llu: dest=0x%llx address=0x%llx res{%s} view{%s}", ordinal, destination, input->address,
        resource_text, view_text);
}

void __stdcall device_memory_srv(Wrapper* self, const D3D12_RESOURCE_DESC* resource,
                                 const MemoryShaderResourceView* input, u64 destination)
{
    static LONG refused;
    if (const char* reason = memory_srv_refusal(resource, input, destination)) {
        refuse_memory_srv(refused, "unsupported memory SRV arguments", reason, resource, input, destination);
        return;
    }
    // A single-element texture is published by address like a tiled view: its texel is read at offset 0.
    if (x2w_startup::supports_single_element_srv(*resource, input->view)) {
        if (gpu_written_overlap(input->address, x2w_startup::single_element_bytes(*resource))) {
            refuse_memory_srv(refused, "GPU-written resource at the supplied address", "gpu_written_resource", resource, input,
                              destination);
        } else if (Wrapper* texture = publish_tiled_texture(self, input->address, *resource)) {
            view_slot<18, 2>(self, reinterpret_cast<u64>(texture), reinterpret_cast<u64>(&input->view), destination, 0);
            note_memory_srv_write(resource, input, destination);
        } else {
            refuse_memory_srv(refused, "unavailable texture at the supplied base and layout", "texture_unavailable", resource, input,
                              destination);
        }
        return;
    }
    if (resource->Dimension == D3D12_RESOURCE_DIMENSION_BUFFER) {
        const u64 elem_bytes = x2w_startup::buffer_srv_element_bytes(input->view);
        const u64 span_bytes = (input->view.Buffer.FirstElement + input->view.Buffer.NumElements) * elem_bytes;
        Wrapper* buffer = existing_memory_buffer(self, input->address, span_bytes);
        const ResourceInfo* info = buffer ? resource_info(buffer) : nullptr;
        D3D12_SHADER_RESOURCE_VIEW_DESC adjusted{};
        if (!buffer || !info ||
            !x2w_startup::resolve_buffer_srv_desc(reinterpret_cast<u64>(buffer->own().title_memory),
                                                  info->console.Width, input->address, input->view, adjusted)) {
            refuse_memory_srv(refused, "unavailable buffer at the supplied address", "buffer_unavailable", resource, input,
                              destination);
            return;
        }
        view_slot<18, 2>(self, reinterpret_cast<u64>(buffer), reinterpret_cast<u64>(&adjusted), destination, 0);
        note_memory_srv_write(resource, input, destination);
        return;
    }
    // A partial-tile view stays refused when the last XG stand-in copy to the address wrote desktop-footprint
    // rows, or when its padded tiles run past the size the stand-in reported, which the title may not own.
    bool first = false;
    const char* held = nullptr;
    if (partial_tiled_bc(*resource)) {
        {
            std::lock_guard lock(tiled_srv_mutex);
            first = !tiled_srv_addresses.count(input->address);
        }
        if (latest_texture_write_is_rows(input->address)) held = "partial_tile_xg_rows";
        else if (x2w_startup::Bc16Surface(*resource).size() > xg_reported_bytes(self, *resource))
            held = "partial_tile_beyond_xg_size";
    }
    Wrapper* texture = held ? nullptr
        : (tiled_bc_srv(*resource) || x2w_startup::bc64::supports(*resource)) ? publish_tiled_texture(self, input->address, *resource)
                                              : existing_memory_texture(input->address, *resource);
    if (first) log_partial_tile_view(*resource, input->address, held, texture != nullptr);
    // A new console-layout view reads the address only when there is no matching live resource.
    const char* outcome = tiled_bc_srv(*resource) || x2w_startup::bc64::supports(*resource) ? "published BC shape" : "existing resource";
    if (console_layout_srv(*resource)) {
        if ((!texture || !supports_placed_memory_srv(*resource, input->view)) &&
            !desktop_address_view_supported(self, *resource, input->view)) {
            held = "unsupported_desktop_format";
            texture = nullptr;
        } else if (!texture) {
            held = console_address_hold(self, input->address, *resource);
            if (!held && address_resource_overlap(input->address, address_texture_bytes(*resource)))
                held = "overlapping_resource";
            texture = held ? nullptr : publish_tiled_texture(self, input->address, *resource);
            outcome = "published console layout";
        }
    }
    if (!texture) outcome = held ? held : "texture_unavailable (no resource there, or texels not readable or over 64 MiB)";
    log_address_kind(outcome, *resource, input->view, input->address);
    if (!texture) {
        refuse_memory_srv(refused, "unavailable texture at the supplied base and layout", held ? held : "texture_unavailable",
                          resource, input, destination);
        return;
    }
    view_slot<18, 2>(self, reinterpret_cast<u64>(texture), reinterpret_cast<u64>(&input->view), destination, 0);
    note_memory_srv_write(resource, input, destination);
}
