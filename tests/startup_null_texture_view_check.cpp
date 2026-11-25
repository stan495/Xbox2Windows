// Checks src/startup_stubs/null_texture_view.h without a GPU. The WARP run of startup_null_views
// never takes the fallback, because WARP supports both packed formats (docs/startup-runtime.md), so
// this check answers CheckFeatureSupport from fake adapters instead: one that lacks both uses, one
// that has them, one that has each packed format's other use only, fallbacks that support one use,
// a fallback only B8G8R8A8 supports, and format queries that fail. For each it checks whether the
// slot 19 (UAV) and slot 20 (RTV) null descriptions are rewritten, that a rewrite keeps every other
// field, and that a refused one leaves the desktop description alone.
//   --mutate unchecked-fallback   rewrites without asking whether R8G8B8A8 supports the use; must fail
#include "startup_stubs/null_texture_view.h"

#include <array>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <utility>

namespace {
int failures, cases;
bool unchecked_fallback;

void expect(bool ok, const char* name)
{
    ++cases;
    if (!ok) {
        std::printf("FAIL %s\n", name);
        ++failures;
    }
}

// What a fake adapter answers for one format. Formats it does not list have no support.
struct FormatAnswer {
    DXGI_FORMAT format;
    D3D12_FORMAT_SUPPORT1 support;
    HRESULT result;  // a failed query still fills Support1, so only the result can refuse it
};

struct Adapter {
    const FormatAnswer* answers;
    std::size_t count;
};
const Adapter* current;
int queries, bad_queries;

// A COM object starts with a pointer to its method table; slot 13 is CheckFeatureSupport in the
// declaration order of ID3D12DeviceVtbl in d3d12.h. Every other slot ends the run.
constexpr std::size_t slots = 64, feature_support_slot = 13;

template <std::size_t N>
void unexpected_slot()
{
    std::printf("FAIL unexpected call to method slot %zu\n", N);
    std::fflush(stdout);
    std::_Exit(1);
}

template <std::size_t... N>
std::array<void*, slots> trap_table(std::index_sequence<N...>)
{
    return {reinterpret_cast<void*>(&unexpected_slot<N>)...};
}

HRESULT STDMETHODCALLTYPE fake_feature_support(ID3D12Device*, D3D12_FEATURE feature, void* data, UINT size)
{
    ++queries;
    if (feature != D3D12_FEATURE_FORMAT_SUPPORT || size != sizeof(D3D12_FEATURE_DATA_FORMAT_SUPPORT) || !data) {
        ++bad_queries;
        return E_INVALIDARG;
    }
    auto* support = static_cast<D3D12_FEATURE_DATA_FORMAT_SUPPORT*>(data);
    support->Support1 = D3D12_FORMAT_SUPPORT1_NONE;
    support->Support2 = D3D12_FORMAT_SUPPORT2_NONE;
    for (std::size_t i = 0; i < current->count; ++i)
        if (current->answers[i].format == support->Format) {
            support->Support1 = current->answers[i].support;
            return current->answers[i].result;
        }
    return S_OK;
}

struct FakeObject {
    const void* const* table;
};

constexpr auto uav_use = D3D12_FORMAT_SUPPORT1_TYPED_UNORDERED_ACCESS_VIEW;
constexpr auto rtv_use = D3D12_FORMAT_SUPPORT1_RENDER_TARGET;
constexpr auto both_uses = static_cast<D3D12_FORMAT_SUPPORT1>(uav_use | rtv_use);
constexpr auto sampled = static_cast<D3D12_FORMAT_SUPPORT1>(D3D12_FORMAT_SUPPORT1_TEXTURE2D | D3D12_FORMAT_SUPPORT1_SHADER_SAMPLE);

// The header with the fallback query left out: the rewrite then happens whenever the packed format
// lacks the use, also where R8G8B8A8 lacks it too or its query fails.
bool unchecked(ID3D12Device* device, DXGI_FORMAT original, D3D12_FORMAT_SUPPORT1 required)
{
    D3D12_FEATURE_DATA_FORMAT_SUPPORT support{original};
    return SUCCEEDED(device->CheckFeatureSupport(D3D12_FEATURE_FORMAT_SUPPORT, &support, sizeof(support))) &&
           !(support.Support1 & required);
}

bool adapt_uav(ID3D12Device* device, bool resource, bool counter, const D3D12_UNORDERED_ACCESS_VIEW_DESC& source,
               D3D12_UNORDERED_ACCESS_VIEW_DESC& desktop)
{
    if (!unchecked_fallback) return x2w_local::adapt_null_view(device, resource, counter, source, desktop);
    if (resource || counter || source.ViewDimension != D3D12_UAV_DIMENSION_TEXTURE2D ||
        source.Format != DXGI_FORMAT_B5G5R5A1_UNORM || !unchecked(device, source.Format, uav_use))
        return false;
    desktop = source;
    desktop.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    return true;
}

bool adapt_rtv(ID3D12Device* device, bool resource, const D3D12_RENDER_TARGET_VIEW_DESC& source,
               D3D12_RENDER_TARGET_VIEW_DESC& desktop)
{
    if (!unchecked_fallback) return x2w_local::adapt_null_view(device, resource, source, desktop);
    if (resource || source.ViewDimension != D3D12_RTV_DIMENSION_TEXTURE2D || source.Format != DXGI_FORMAT_B4G4R4A4_UNORM ||
        !unchecked(device, source.Format, rtv_use))
        return false;
    desktop = source;
    desktop.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    return true;
}

// Null descriptions with a distinct value in every Texture2D field, and a desktop description that
// differs from both in every field, so a rewrite that does not start from the source is seen.
D3D12_UNORDERED_ACCESS_VIEW_DESC uav_source(DXGI_FORMAT format = DXGI_FORMAT_B5G5R5A1_UNORM,
                                            D3D12_UAV_DIMENSION dimension = D3D12_UAV_DIMENSION_TEXTURE2D)
{
    D3D12_UNORDERED_ACCESS_VIEW_DESC desc{};
    desc.Format = format;
    desc.ViewDimension = dimension;
    if (dimension == D3D12_UAV_DIMENSION_TEXTURE2D) desc.Texture2D = {3, 1};
    else desc.Texture2DArray = {3, 4, 5, 1};
    return desc;
}

D3D12_RENDER_TARGET_VIEW_DESC rtv_source(DXGI_FORMAT format = DXGI_FORMAT_B4G4R4A4_UNORM,
                                         D3D12_RTV_DIMENSION dimension = D3D12_RTV_DIMENSION_TEXTURE2D)
{
    D3D12_RENDER_TARGET_VIEW_DESC desc{};
    desc.Format = format;
    desc.ViewDimension = dimension;
    if (dimension == D3D12_RTV_DIMENSION_TEXTURE2D) desc.Texture2D = {2, 1};
    else desc.Texture2DArray = {2, 4, 5, 1};
    return desc;
}

template <typename Desc>
Desc untouched()
{
    Desc desc;
    std::memset(&desc, 0x5a, sizeof(desc));
    return desc;
}

void uav(const char* name, ID3D12Device* device, bool rewritten, bool resource = false, bool counter = false,
         const D3D12_UNORDERED_ACCESS_VIEW_DESC& source = uav_source())
{
    D3D12_UNORDERED_ACCESS_VIEW_DESC desktop = untouched<D3D12_UNORDERED_ACCESS_VIEW_DESC>();
    const auto before = desktop;
    const bool changed = adapt_uav(device, resource, counter, source, desktop);
    bool ok = changed == rewritten;
    if (ok && rewritten) {
        auto wanted = source;
        wanted.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        ok = !std::memcmp(&desktop, &wanted, sizeof(desktop));
    } else if (ok) {
        ok = !std::memcmp(&desktop, &before, sizeof(desktop));
    }
    ++cases;
    if (!ok) {
        ++failures;
        std::printf("FAIL slot 19 %s: %s, format %u, dimension %u, mip %u, plane %u\n", name,
                    changed ? "rewritten" : "kept", desktop.Format, desktop.ViewDimension, desktop.Texture2D.MipSlice,
                    desktop.Texture2D.PlaneSlice);
    }
}

void rtv(const char* name, ID3D12Device* device, bool rewritten, bool resource = false,
         const D3D12_RENDER_TARGET_VIEW_DESC& source = rtv_source())
{
    D3D12_RENDER_TARGET_VIEW_DESC desktop = untouched<D3D12_RENDER_TARGET_VIEW_DESC>();
    const auto before = desktop;
    const bool changed = adapt_rtv(device, resource, source, desktop);
    bool ok = changed == rewritten;
    if (ok && rewritten) {
        auto wanted = source;
        wanted.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        ok = !std::memcmp(&desktop, &wanted, sizeof(desktop));
    } else if (ok) {
        ok = !std::memcmp(&desktop, &before, sizeof(desktop));
    }
    ++cases;
    if (!ok) {
        ++failures;
        std::printf("FAIL slot 20 %s: %s, format %u, dimension %u, mip %u, plane %u\n", name,
                    changed ? "rewritten" : "kept", desktop.Format, desktop.ViewDimension, desktop.Texture2D.MipSlice,
                    desktop.Texture2D.PlaneSlice);
    }
}

template <std::size_t N>
void use(const FormatAnswer (&answers)[N])
{
    static Adapter adapter;
    adapter = {answers, N};
    current = &adapter;
}
}  // namespace

int main(int argc, char** argv)
{
    unchecked_fallback = argc == 3 && !std::strcmp(argv[1], "--mutate") && !std::strcmp(argv[2], "unchecked-fallback");
    if (argc != 1 && !unchecked_fallback) return 2;
    const auto table = [] {
        auto t = trap_table(std::make_index_sequence<slots>{});
        t[feature_support_slot] = reinterpret_cast<void*>(&fake_feature_support);
        return t;
    }();
    FakeObject object{table.data()};
    auto* device = reinterpret_cast<ID3D12Device*>(&object);

    // The packed formats can be sampled but have neither use; R8G8B8A8 has both. Both slots are
    // rewritten, and each asks for the packed format first and R8G8B8A8 second.
    const FormatAnswer lacking[] = {{DXGI_FORMAT_B5G5R5A1_UNORM, sampled, S_OK},
                                    {DXGI_FORMAT_B4G4R4A4_UNORM, sampled, S_OK},
                                    {DXGI_FORMAT_R8G8B8A8_UNORM, both_uses, S_OK}};
    use(lacking);
    queries = 0;
    uav("packed format lacks typed UAV", device, true);
    expect(queries == 2, "slot 19 asks two formats");
    queries = 0;
    rtv("packed format lacks render target", device, true);
    expect(queries == 2, "slot 20 asks two formats");
    // Only null Texture2D views of that one format, with no counter, are rewritten.
    uav("with a resource", device, false, true);
    uav("with a counter", device, false, false, true);
    uav("Texture2DArray", device, false, false, false, uav_source(DXGI_FORMAT_B5G5R5A1_UNORM, D3D12_UAV_DIMENSION_TEXTURE2DARRAY));
    uav("B4G4R4A4 UAV", device, false, false, false, uav_source(DXGI_FORMAT_B4G4R4A4_UNORM));
    uav("R32_UINT UAV", device, false, false, false, uav_source(DXGI_FORMAT_R32_UINT));
    rtv("with a resource", device, false, true);
    rtv("Texture2DArray", device, false, false, rtv_source(DXGI_FORMAT_B4G4R4A4_UNORM, D3D12_RTV_DIMENSION_TEXTURE2DARRAY));
    rtv("B5G5R5A1 RTV", device, false, false, rtv_source(DXGI_FORMAT_B5G5R5A1_UNORM));
    rtv("R32_UINT RTV", device, false, false, rtv_source(DXGI_FORMAT_R32_UINT));

    // An adapter with both uses (WARP's answer) keeps both formats.
    const FormatAnswer supported[] = {{DXGI_FORMAT_B5G5R5A1_UNORM, both_uses, S_OK},
                                      {DXGI_FORMAT_B4G4R4A4_UNORM, both_uses, S_OK},
                                      {DXGI_FORMAT_R8G8B8A8_UNORM, both_uses, S_OK}};
    use(supported);
    uav("packed format has typed UAV", device, false);
    rtv("packed format has render target", device, false);

    // Each packed format has only the other slot's use: the slot asks for its own use.
    const FormatAnswer crossed[] = {{DXGI_FORMAT_B5G5R5A1_UNORM, rtv_use, S_OK},
                                    {DXGI_FORMAT_B4G4R4A4_UNORM, uav_use, S_OK},
                                    {DXGI_FORMAT_R8G8B8A8_UNORM, both_uses, S_OK}};
    use(crossed);
    uav("B5G5R5A1 with render target only", device, true);
    rtv("B4G4R4A4 with typed UAV only", device, true);

    // R8G8B8A8 has one use only: only that slot is rewritten.
    const FormatAnswer fallback_rtv[] = {{DXGI_FORMAT_B5G5R5A1_UNORM, sampled, S_OK},
                                         {DXGI_FORMAT_B4G4R4A4_UNORM, sampled, S_OK},
                                         {DXGI_FORMAT_R8G8B8A8_UNORM, rtv_use, S_OK}};
    use(fallback_rtv);
    uav("R8G8B8A8 without typed UAV", device, false);
    rtv("R8G8B8A8 with render target only", device, true);
    const FormatAnswer fallback_uav[] = {{DXGI_FORMAT_B5G5R5A1_UNORM, sampled, S_OK},
                                         {DXGI_FORMAT_B4G4R4A4_UNORM, sampled, S_OK},
                                         {DXGI_FORMAT_R8G8B8A8_UNORM, uav_use, S_OK}};
    use(fallback_uav);
    uav("R8G8B8A8 with typed UAV only", device, true);
    rtv("R8G8B8A8 without render target", device, false);

    // Only B8G8R8A8 has both uses: the fallback asked for is R8G8B8A8, so nothing is rewritten.
    const FormatAnswer bgra_only[] = {{DXGI_FORMAT_B5G5R5A1_UNORM, sampled, S_OK},
                                      {DXGI_FORMAT_B4G4R4A4_UNORM, sampled, S_OK},
                                      {DXGI_FORMAT_B8G8R8A8_UNORM, both_uses, S_OK}};
    use(bgra_only);
    uav("only B8G8R8A8 has typed UAV", device, false);
    rtv("only B8G8R8A8 has render target", device, false);

    // A failed query refuses the rewrite, whether it is the packed format's or the fallback's.
    const FormatAnswer packed_fails[] = {{DXGI_FORMAT_B5G5R5A1_UNORM, D3D12_FORMAT_SUPPORT1_NONE, E_FAIL},
                                         {DXGI_FORMAT_B4G4R4A4_UNORM, D3D12_FORMAT_SUPPORT1_NONE, E_FAIL},
                                         {DXGI_FORMAT_R8G8B8A8_UNORM, both_uses, S_OK}};
    use(packed_fails);
    uav("B5G5R5A1 query fails", device, false);
    rtv("B4G4R4A4 query fails", device, false);
    const FormatAnswer fallback_fails[] = {{DXGI_FORMAT_B5G5R5A1_UNORM, sampled, S_OK},
                                           {DXGI_FORMAT_B4G4R4A4_UNORM, sampled, S_OK},
                                           {DXGI_FORMAT_R8G8B8A8_UNORM, both_uses, E_FAIL}};
    use(fallback_fails);
    uav("R8G8B8A8 query fails", device, false);
    rtv("R8G8B8A8 query fails", device, false);

    expect(bad_queries == 0, "every query is a format-support query of the right size");
    if (failures) {
        std::printf("startup_null_texture_view: %d of %d FAILED\n", failures, cases);
        return 1;
    }
    std::printf("startup_null_texture_view: PASS (%d cases)\n", cases);
    return 0;
}
