#include <d3d12.h>
#include <dxgi1_6.h>
#include <wrl/client.h>

#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iterator>
#include <vector>

#include "startup_stream_setup.h"

using Microsoft::WRL::ComPtr;
extern "C" __declspec(dllimport) HRESULT WINAPI D3D12XboxCreateDevice(IUnknown*, const void*, REFIID, void**);

namespace {

struct alignas(8) StageSubobject {
    D3D12_PIPELINE_STATE_SUBOBJECT_TYPE tag;
    D3D12_SHADER_BYTECODE shader;
};
struct alignas(8) RootSubobject {
    D3D12_PIPELINE_STATE_SUBOBJECT_TYPE tag;
    ID3D12RootSignature* signature;
};
struct alignas(8) DepthStencilSubobject {
    D3D12_PIPELINE_STATE_SUBOBJECT_TYPE tag;
    D3D12_DEPTH_STENCIL_DESC desc;
};
static_assert(sizeof(DepthStencilSubobject) == 56);

struct alignas(8) DepthStencil1Subobject {
    D3D12_PIPELINE_STATE_SUBOBJECT_TYPE tag;
    D3D12_DEPTH_STENCIL_DESC desc;
    BOOL depth_bounds_test;
};
static_assert(sizeof(DepthStencil1Subobject) == 64);

struct alignas(8) DsvFormatSubobject {
    D3D12_PIPELINE_STATE_SUBOBJECT_TYPE tag;
    DXGI_FORMAT format;
};
static_assert(sizeof(DsvFormatSubobject) == 8);

struct DepthStream {
    StageSubobject ps;
    RootSubobject root;
    StageSubobject vs;
    DepthStencilSubobject ds;
    DsvFormatSubobject dsv_format;
};

struct DepthStream1 {
    StageSubobject ps;
    RootSubobject root;
    StageSubobject vs;
    DepthStencil1Subobject ds1;
    DsvFormatSubobject dsv_format;
};

using Word = std::uint64_t;
template<class T> Word ptr_arg(T* p) { return reinterpret_cast<Word>(p); }

bool read_bytes(const char* path, std::vector<char>& out)
{
    std::ifstream file(path, std::ios::binary);
    out.assign(std::istreambuf_iterator<char>(file), {});
    return !out.empty();
}

int pick_adapter(bool hardware, ComPtr<IDXGIAdapter>& adapter)
{
    ComPtr<IDXGIFactory4> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) return 4;
    if (!hardware) return SUCCEEDED(factory->EnumWarpAdapter(IID_PPV_ARGS(&adapter))) ? 0 : 4;
    ComPtr<IDXGIFactory6> factory6;
    if (FAILED(factory.As(&factory6))) return 77;
    for (UINT n = 0;; ++n) {
        ComPtr<IDXGIAdapter1> candidate;
        const HRESULT hr = factory6->EnumAdapterByGpuPreference(n, DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE, IID_PPV_ARGS(&candidate));
        if (hr == DXGI_ERROR_NOT_FOUND) return 77;
        if (FAILED(hr)) return 4;
        DXGI_ADAPTER_DESC1 desc{};
        if (FAILED(candidate->GetDesc1(&desc))) return 4;
        if (!(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) &&
            SUCCEEDED(D3D12CreateDevice(candidate.Get(), D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), nullptr))) {
            adapter = candidate;
            return 0;
        }
    }
}

void* create_stream_pso(void* device, ID3D12RootSignature* root, const std::vector<char>& vs,
                        const std::vector<char>& ps, const D3D12_DEPTH_STENCIL_DESC& ds, bool use_ds1,
                        BOOL depth_bounds = FALSE, DXGI_FORMAT dsv_format = DXGI_FORMAT_D32_FLOAT)
{
    using CreateStream = HRESULT (WINAPI*)(void*, const D3D12_PIPELINE_STATE_STREAM_DESC*, REFIID, void**);
    auto create = reinterpret_cast<CreateStream>((*static_cast<void***>(device))[86]);
    void* pso = nullptr;
    if (use_ds1) {
        DepthStream1 stream{
            {D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_PS, {ps.data(), ps.size()}},
            {D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_ROOT_SIGNATURE, root},
            {D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_VS, {vs.data(), vs.size()}},
            {static_cast<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE>(21), ds, depth_bounds},
            {D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_DEPTH_STENCIL_FORMAT, dsv_format},
        };
        const D3D12_PIPELINE_STATE_STREAM_DESC desc{sizeof(stream), &stream};
        if (FAILED(create(device, &desc, __uuidof(ID3D12PipelineState), &pso))) return nullptr;
    } else {
        DepthStream stream{
            {D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_PS, {ps.data(), ps.size()}},
            {D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_ROOT_SIGNATURE, root},
            {D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_VS, {vs.data(), vs.size()}},
            {D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_DEPTH_STENCIL, ds},
            {D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_DEPTH_STENCIL_FORMAT, dsv_format},
        };
        const D3D12_PIPELINE_STATE_STREAM_DESC desc{sizeof(stream), &stream};
        if (FAILED(create(device, &desc, __uuidof(ID3D12PipelineState), &pso))) return nullptr;
    }
    return pso;
}

int clear_depth_desktop(ID3D12Device* desktop, ID3D12Resource* depth_tex)
{
    D3D12_DESCRIPTOR_HEAP_DESC heap_desc{D3D12_DESCRIPTOR_HEAP_TYPE_DSV, 1};
    ComPtr<ID3D12DescriptorHeap> heap;
    if (FAILED(desktop->CreateDescriptorHeap(&heap_desc, IID_PPV_ARGS(&heap)))) return 40;
    const D3D12_DEPTH_STENCIL_VIEW_DESC dsv_desc{DXGI_FORMAT_D32_FLOAT, D3D12_DSV_DIMENSION_TEXTURE2D, D3D12_DSV_FLAG_NONE, {}};
    const D3D12_CPU_DESCRIPTOR_HANDLE handle = heap->GetCPUDescriptorHandleForHeapStart();
    desktop->CreateDepthStencilView(depth_tex, &dsv_desc, handle);

    D3D12_COMMAND_QUEUE_DESC qd{};
    ComPtr<ID3D12CommandQueue> queue;
    ComPtr<ID3D12CommandAllocator> alloc;
    ComPtr<ID3D12GraphicsCommandList> list;
    ComPtr<ID3D12Fence> fence;
    if (FAILED(desktop->CreateCommandQueue(&qd, IID_PPV_ARGS(&queue))) ||
        FAILED(desktop->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&alloc))) ||
        FAILED(desktop->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc.Get(), nullptr, IID_PPV_ARGS(&list))) ||
        FAILED(desktop->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)))) return 41;

    D3D12_RESOURCE_BARRIER b{};
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition = {depth_tex, D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_DEPTH_WRITE};
    list->ResourceBarrier(1, &b);
    list->ClearDepthStencilView(handle, D3D12_CLEAR_FLAG_DEPTH, 1.0f, 0, 0, nullptr);
    b.Transition.StateBefore = D3D12_RESOURCE_STATE_DEPTH_WRITE;
    b.Transition.StateAfter = D3D12_RESOURCE_STATE_COMMON;
    list->ResourceBarrier(1, &b);
    if (FAILED(list->Close())) return 42;

    ID3D12CommandList* lists[] = {list.Get()};
    queue->ExecuteCommandLists(1, lists);
    if (FAILED(queue->Signal(fence.Get(), 1))) return 43;
    HANDLE ev = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (!ev) return 44;
    const bool ok = SUCCEEDED(fence->SetEventOnCompletion(1, ev)) && WaitForSingleObject(ev, 10000) == WAIT_OBJECT_0;
    CloseHandle(ev);
    return ok ? 0 : 45;
}

// Clears the depth to 1 and the stencil to `value` on the desktop device, as clear_depth_desktop does for depth only.
int clear_stencil_desktop(ID3D12Device* desktop, ID3D12Resource* tex, UINT8 value)
{
    D3D12_DESCRIPTOR_HEAP_DESC heap_desc{D3D12_DESCRIPTOR_HEAP_TYPE_DSV, 1};
    ComPtr<ID3D12DescriptorHeap> heap;
    if (FAILED(desktop->CreateDescriptorHeap(&heap_desc, IID_PPV_ARGS(&heap)))) return 40;
    const D3D12_DEPTH_STENCIL_VIEW_DESC view{DXGI_FORMAT_D24_UNORM_S8_UINT, D3D12_DSV_DIMENSION_TEXTURE2D, D3D12_DSV_FLAG_NONE, {}};
    const D3D12_CPU_DESCRIPTOR_HANDLE handle = heap->GetCPUDescriptorHandleForHeapStart();
    desktop->CreateDepthStencilView(tex, &view, handle);

    D3D12_COMMAND_QUEUE_DESC qd{};
    ComPtr<ID3D12CommandQueue> queue;
    ComPtr<ID3D12CommandAllocator> alloc;
    ComPtr<ID3D12GraphicsCommandList> list;
    ComPtr<ID3D12Fence> fence;
    if (FAILED(desktop->CreateCommandQueue(&qd, IID_PPV_ARGS(&queue))) ||
        FAILED(desktop->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&alloc))) ||
        FAILED(desktop->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc.Get(), nullptr, IID_PPV_ARGS(&list))) ||
        FAILED(desktop->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)))) return 41;

    D3D12_RESOURCE_BARRIER b{};
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition = {tex, D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_DEPTH_WRITE};
    list->ResourceBarrier(1, &b);
    list->ClearDepthStencilView(handle, D3D12_CLEAR_FLAG_DEPTH | D3D12_CLEAR_FLAG_STENCIL, 1.0f, value, 0, nullptr);
    b.Transition.StateBefore = D3D12_RESOURCE_STATE_DEPTH_WRITE;
    b.Transition.StateAfter = D3D12_RESOURCE_STATE_COMMON;
    list->ResourceBarrier(1, &b);
    if (FAILED(list->Close())) return 42;

    ID3D12CommandList* lists[] = {list.Get()};
    queue->ExecuteCommandLists(1, lists);
    if (FAILED(queue->Signal(fence.Get(), 1))) return 43;
    HANDLE ev = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (!ev) return 44;
    const bool ok = SUCCEEDED(fence->SetEventOnCompletion(1, ev)) && WaitForSingleObject(ev, 10000) == WAIT_OBJECT_0;
    CloseHandle(ev);
    return ok ? 0 : 45;
}

int execute_guest_list(void* queue, void* list, void* fence, UINT64 fence_value)
{
    if (FAILED(static_cast<HRESULT>(invoke(list, 9)))) return 50;
    void* lists[] = {list};
    invoke(queue, 10, 1, ptr_arg(lists));
    if (FAILED(static_cast<HRESULT>(invoke(queue, 14, ptr_arg(fence), fence_value)))) return 51;
    const auto deadline = GetTickCount64() + 10000;
    while (invoke(fence, 8) < fence_value && GetTickCount64() < deadline) Sleep(1);
    return invoke(fence, 8) >= fence_value ? 0 : 53;
}

}  // namespace

int main(int argc, char** argv)
{
    if (argc != 8 && argc != 19) return 2;
    std::vector<char> vs_front, vs_back, ps_green, ps_red, ps_empty;
    if (!read_bytes(argv[1], vs_front) || !read_bytes(argv[2], vs_back) ||
        !read_bytes(argv[3], ps_green) || !read_bytes(argv[4], ps_red) || !read_bytes(argv[7], ps_empty)) return 3;
    const bool hardware = std::strcmp(argv[6], "hardware") == 0;

    ComPtr<IDXGIAdapter> adapter;
    if (const int rc = pick_adapter(hardware, adapter)) return rc;
    {
        DXGI_ADAPTER_DESC desc{};
        LARGE_INTEGER driver{};
        if (FAILED(adapter->GetDesc(&desc))) return 4;
        adapter->CheckInterfaceSupport(__uuidof(IDXGIDevice), &driver);
        std::printf("adapter=%ls vendor=%04x device=%04x driver=%u.%u.%u.%u\n", desc.Description, desc.VendorId, desc.DeviceId,
            HIWORD(driver.HighPart), LOWORD(driver.HighPart), HIWORD(driver.LowPart), LOWORD(driver.LowPart));
        std::fflush(stdout);
    }

    std::array<unsigned char, 64> parameters{};
    void* guest = nullptr;
    if (FAILED(D3D12XboxCreateDevice(adapter.Get(), parameters.data(), __uuidof(ID3D12Device), &guest))) return 5;

    D3D12_ROOT_SIGNATURE_DESC root_desc{};
    ComPtr<ID3DBlob> blob;
    if (FAILED(D3D12SerializeRootSignature(&root_desc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, nullptr))) return 6;
    std::ofstream(argv[5], std::ios::binary).write(static_cast<const char*>(blob->GetBufferPointer()), blob->GetBufferSize());
    ID3D12RootSignature* root = nullptr;
    if (FAILED(static_cast<HRESULT>(invoke(guest, 16, 0, ptr_arg(blob->GetBufferPointer()), blob->GetBufferSize(),
                                           ptr_arg(&__uuidof(ID3D12RootSignature)), ptr_arg(&root))))) return 7;

    D3D12_DEPTH_STENCIL_DESC ds_less{};
    ds_less.DepthEnable = TRUE;
    ds_less.DepthWriteMask = D3D12_DEPTH_WRITE_MASK_ALL;
    ds_less.DepthFunc = D3D12_COMPARISON_FUNC_LESS;

    D3D12_DEPTH_STENCIL_DESC ds_disabled{};
    ds_disabled.DepthEnable = FALSE;
    ds_disabled.DepthWriteMask = D3D12_DEPTH_WRITE_MASK_ZERO;
    ds_disabled.DepthFunc = D3D12_COMPARISON_FUNC_ALWAYS;

    void* pso_front_less = create_stream_pso(guest, root, vs_front, ps_green, ds_less, false);
    void* pso_back_less = create_stream_pso(guest, root, vs_back, ps_red, ds_less, false);
    void* pso_front_off = create_stream_pso(guest, root, vs_front, ps_green, ds_disabled, false);
    void* pso_back_off = create_stream_pso(guest, root, vs_back, ps_red, ds_disabled, false);
    // Depth-only pass: the pixel shader exports nothing, so nothing declares SV_Target0 for a zero-RTV bind.
    void* pso_front_depth_only = create_stream_pso(guest, root, vs_front, ps_empty, ds_less, true);
    // DepthBoundsTestEnable has no translated equivalent: the pipeline is refused for drawing, not translated without it.
    void* pso_front_bounds = create_stream_pso(guest, root, vs_front, ps_green, ds_less, true, TRUE);
    // Stencil test only: a pixel passes when the stored stencil equals the reference set with OMSetStencilRef (slot 24).
    D3D12_DEPTH_STENCIL_DESC ds_stencil{};
    ds_stencil.DepthEnable = FALSE;
    ds_stencil.DepthWriteMask = D3D12_DEPTH_WRITE_MASK_ZERO;
    ds_stencil.DepthFunc = D3D12_COMPARISON_FUNC_ALWAYS;
    ds_stencil.StencilEnable = TRUE;
    ds_stencil.StencilReadMask = 0xFF;
    ds_stencil.StencilWriteMask = 0;
    ds_stencil.FrontFace = ds_stencil.BackFace = {D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP,
                                                  D3D12_COMPARISON_FUNC_EQUAL};
    void* pso_front_stencil = create_stream_pso(guest, root, vs_front, ps_green, ds_stencil, false, FALSE,
                                                DXGI_FORMAT_D24_UNORM_S8_UINT);
    if (!pso_front_bounds || !pso_front_less || !pso_back_less || !pso_front_off || !pso_back_off || !pso_front_depth_only ||
        !pso_front_stencil) return 9;
    if (argc == 8) return 0;

    ComPtr<ID3D12Device> desktop;
    if (FAILED(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&desktop)))) return 20;

    constexpr UINT width = 8, height = 4;
    StreamTarget state{};
    state.count = 1;
    state.attachments[0].row_bytes = width * 4;

    D3D12_HEAP_PROPERTIES heap{D3D12_HEAP_TYPE_DEFAULT};
    const float clear_color[] = {0.0f, 0.0f, 1.0f, 1.0f};
    const D3D12_RESOURCE_DESC rtv_tex_desc{D3D12_RESOURCE_DIMENSION_TEXTURE2D, 0, width, height, 1, 1,
        DXGI_FORMAT_R8G8B8A8_UNORM, {1, 0}, D3D12_TEXTURE_LAYOUT_UNKNOWN, D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET};
    const D3D12_CLEAR_VALUE rtv_clear{DXGI_FORMAT_R8G8B8A8_UNORM, {0.0f, 0.0f, 1.0f, 1.0f}};
    if (FAILED(desktop->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &rtv_tex_desc,
            D3D12_RESOURCE_STATE_COMMON, &rtv_clear, IID_PPV_ARGS(&state.attachments[0].texture)))) return 21;

    ComPtr<ID3D12Resource> depth_tex;
    const D3D12_RESOURCE_DESC dsv_tex_desc{D3D12_RESOURCE_DIMENSION_TEXTURE2D, 0, width, height, 1, 1,
        DXGI_FORMAT_D32_FLOAT, {1, 0}, D3D12_TEXTURE_LAYOUT_UNKNOWN, D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL};
    D3D12_CLEAR_VALUE dsv_clear{};
    dsv_clear.Format = DXGI_FORMAT_D32_FLOAT;
    dsv_clear.DepthStencil = {1.0f, 0};
    if (FAILED(desktop->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &dsv_tex_desc,
            D3D12_RESOURCE_STATE_COMMON, &dsv_clear, IID_PPV_ARGS(&depth_tex)))) return 21;

    const D3D12_RESOURCE_DESC stencil_tex_desc{D3D12_RESOURCE_DIMENSION_TEXTURE2D, 0, width, height, 1, 1,
        DXGI_FORMAT_D24_UNORM_S8_UINT, {1, 0}, D3D12_TEXTURE_LAYOUT_UNKNOWN, D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL};

    const struct { D3D12_DESCRIPTOR_HEAP_DESC desc; Word backing, bytes; }
        rtv_heap_desc{{D3D12_DESCRIPTOR_HEAP_TYPE_RTV, 1}, 0, 0},
        dsv_heap_desc{{D3D12_DESCRIPTOR_HEAP_TYPE_DSV, 3}, 0, 0};
    void* dsv_heap = nullptr;
    if (FAILED(static_cast<HRESULT>(invoke(guest, 14, ptr_arg(&rtv_heap_desc), ptr_arg(&__uuidof(ID3D12DescriptorHeap)), ptr_arg(&state.rtv_heap)))) ||
        FAILED(static_cast<HRESULT>(invoke(guest, 14, ptr_arg(&dsv_heap_desc), ptr_arg(&__uuidof(ID3D12DescriptorHeap)), ptr_arg(&dsv_heap))))) return 22;

    const D3D12_CPU_DESCRIPTOR_HANDLE rtv{static_cast<SIZE_T>(invoke(state.rtv_heap, 9))};
    const D3D12_CPU_DESCRIPTOR_HANDLE dsv{static_cast<SIZE_T>(invoke(dsv_heap, 9))};
    const D3D12_RENDER_TARGET_VIEW_DESC rtv_view{DXGI_FORMAT_R8G8B8A8_UNORM, D3D12_RTV_DIMENSION_TEXTURE2D, {}};
    const D3D12_DEPTH_STENCIL_VIEW_DESC dsv_view{DXGI_FORMAT_D32_FLOAT, D3D12_DSV_DIMENSION_TEXTURE2D, D3D12_DSV_FLAG_NONE, {}};
    invoke(guest, 20, ptr_arg(state.attachments[0].texture.Get()), ptr_arg(&rtv_view), rtv.ptr);
    invoke(guest, 21, ptr_arg(depth_tex.Get()), ptr_arg(&dsv_view), dsv.ptr);
    // A second view of the same texture, read-only: depth tests but is never written.
    const D3D12_CPU_DESCRIPTOR_HANDLE dsv_read_only{dsv.ptr + desktop->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_DSV)};
    const D3D12_DEPTH_STENCIL_VIEW_DESC dsv_read_only_view{DXGI_FORMAT_D32_FLOAT, D3D12_DSV_DIMENSION_TEXTURE2D,
        D3D12_DSV_FLAG_READ_ONLY_DEPTH, {}};
    invoke(guest, 21, ptr_arg(depth_tex.Get()), ptr_arg(&dsv_read_only_view), dsv_read_only.ptr);
    const D3D12_CPU_DESCRIPTOR_HANDLE dsv_stencil{dsv_read_only.ptr + desktop->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_DSV)};
    const D3D12_DEPTH_STENCIL_VIEW_DESC dsv_stencil_view{DXGI_FORMAT_D24_UNORM_S8_UINT, D3D12_DSV_DIMENSION_TEXTURE2D,
        D3D12_DSV_FLAG_NONE, {}};

    D3D12_COMMAND_QUEUE_DESC qd{};
    const unsigned list_desc[] = {D3D12_COMMAND_LIST_TYPE_DIRECT, 2, 1, 0};
    if (FAILED(static_cast<HRESULT>(invoke(guest, 8, ptr_arg(&qd), ptr_arg(&__uuidof(ID3D12CommandQueue)), ptr_arg(&state.queue)))) ||
        FAILED(static_cast<HRESULT>(invoke(guest, 9, D3D12_COMMAND_LIST_TYPE_DIRECT, ptr_arg(&__uuidof(ID3D12CommandAllocator)), ptr_arg(&state.allocator)))) ||
        FAILED(static_cast<HRESULT>(invoke(guest, 73, ptr_arg(list_desc), ptr_arg(state.allocator), 0, ptr_arg(&__uuidof(ID3D12GraphicsCommandList)), ptr_arg(&state.list)))) ||
        FAILED(static_cast<HRESULT>(invoke(guest, 36, 0, 0, ptr_arg(&__uuidof(ID3D12Fence)), ptr_arg(&state.fence))))) return 23;

    const D3D12_VIEWPORT viewport{0, 0, static_cast<float>(width), static_cast<float>(height), 0, 1};
    const D3D12_RECT rect{0, 0, 4, static_cast<LONG>(height)};

    enum class Bind { normal, depth_only, read_only_depth, rtv_as_depth, dsv_as_target };
    auto run_case = [&](UINT64 fence_val, const char* out_path, Bind bind, void* first_pso, void* second_pso) -> int {
        const bool depth_only = bind == Bind::depth_only;
        if (const int rc = clear_depth_desktop(desktop.Get(), depth_tex.Get())) return rc;
        if (FAILED(static_cast<HRESULT>(invoke(state.allocator, 8)))) return 24;
        if (FAILED(static_cast<HRESULT>(invoke(state.list, 139, ptr_arg(state.allocator))))) return 25;
        invoke(state.list, 52, rtv.ptr, ptr_arg(clear_color));
        invoke(state.list, 21, 1, ptr_arg(&viewport));
        invoke(state.list, 22, 1, ptr_arg(&rect));
        invoke(state.list, 20, D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        if (depth_only) {
            invoke(state.list, 50, 0, 0, FALSE, ptr_arg(&dsv));
        } else if (bind == Bind::read_only_depth) {
            invoke(state.list, 50, 1, ptr_arg(&rtv), FALSE, ptr_arg(&dsv_read_only));
        } else if (bind == Bind::rtv_as_depth) {
            invoke(state.list, 50, 1, ptr_arg(&rtv), FALSE, ptr_arg(&rtv));  // an RTV where a DSV belongs
        } else if (bind == Bind::dsv_as_target) {
            invoke(state.list, 50, 1, ptr_arg(&dsv), FALSE, ptr_arg(&dsv));  // a DSV where an RTV belongs
        } else {
            invoke(state.list, 50, 1, ptr_arg(&rtv), FALSE, ptr_arg(&dsv));
        }
        invoke(state.list, 25, ptr_arg(first_pso));
        invoke(state.list, 12, 3, 1);
        if (depth_only) {
            invoke(state.list, 50, 1, ptr_arg(&rtv), FALSE, ptr_arg(&dsv));
        }
        invoke(state.list, 25, ptr_arg(second_pso));
        invoke(state.list, 12, 3, 1);
        if (const int rc = execute_guest_list(state.queue, state.list, state.fence, fence_val)) return rc;
        return read_stream_target(desktop.Get(), state, out_path);
    };

    if (const int rc = run_case(1, argv[8], Bind::normal, pso_front_less, pso_back_less)) return rc;
    if (const int rc = run_case(2, argv[9], Bind::normal, pso_front_off, pso_back_off)) return rc;
    if (const int rc = run_case(3, argv[10], Bind::depth_only, pso_front_depth_only, pso_back_less)) return rc;
    if (const int rc = run_case(4, argv[11], Bind::read_only_depth, pso_front_less, pso_back_less)) return rc;
    // Controls: nothing may be drawn for a wrong descriptor kind or an enabled depth bounds test.
    if (const int rc = run_case(5, argv[12], Bind::rtv_as_depth, pso_front_less, pso_back_less)) return rc;
    if (const int rc = run_case(6, argv[13], Bind::dsv_as_target, pso_front_less, pso_back_less)) return rc;
    if (const int rc = run_case(7, argv[14], Bind::normal, pso_front_bounds, pso_front_bounds)) return rc;

    // OMSetStencilRef (slot 24): the stencil is cleared to `stored`, the pipeline passes where it equals the
    // reference. A pixel is drawn only if the reference reached the desktop list. `closed_call` sets a reference
    // on the list after it was closed, which must be ignored.
    auto run_stencil_case = [&](UINT64 fence_val, const char* out_path, UINT8 stored, UINT reference, bool closed_call) -> int {
        // Each case clears to its own value without producing a diagnostic about a mismatched optimized clear.
        ComPtr<ID3D12Resource> stencil_tex;
        D3D12_CLEAR_VALUE stencil_clear{};
        stencil_clear.Format = DXGI_FORMAT_D24_UNORM_S8_UINT;
        stencil_clear.DepthStencil = {1.0f, stored};
        if (FAILED(desktop->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &stencil_tex_desc,
                D3D12_RESOURCE_STATE_COMMON, &stencil_clear, IID_PPV_ARGS(&stencil_tex)))) return 21;
        invoke(guest, 21, ptr_arg(stencil_tex.Get()), ptr_arg(&dsv_stencil_view), dsv_stencil.ptr);
        if (const int rc = clear_stencil_desktop(desktop.Get(), stencil_tex.Get(), stored)) return rc;
        if (FAILED(static_cast<HRESULT>(invoke(state.allocator, 8)))) return 24;
        if (FAILED(static_cast<HRESULT>(invoke(state.list, 139, ptr_arg(state.allocator))))) return 25;
        invoke(state.list, 52, rtv.ptr, ptr_arg(clear_color));
        invoke(state.list, 21, 1, ptr_arg(&viewport));
        invoke(state.list, 22, 1, ptr_arg(&rect));
        invoke(state.list, 20, D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        invoke(state.list, 50, 1, ptr_arg(&rtv), FALSE, ptr_arg(&dsv_stencil));
        invoke(state.list, 25, ptr_arg(pso_front_stencil));
        invoke(state.list, 24, reference);
        invoke(state.list, 12, 3, 1);
        if (const int rc = execute_guest_list(state.queue, state.list, state.fence, fence_val)) return rc;
        if (closed_call) invoke(state.list, 24, 0x33);
        return read_stream_target(desktop.Get(), state, out_path);
    };
    if (const int rc = run_stencil_case(8, argv[15], 0x5A, 0x5A, true)) return rc;   // equal: drawn
    if (const int rc = run_stencil_case(9, argv[16], 0x5A, 0x5B, false)) return rc;  // unequal: not drawn
    if (const int rc = run_stencil_case(10, argv[17], 0x00, 0x00, false)) return rc; // zero: drawn
    if (const int rc = run_stencil_case(11, argv[18], 0xFF, 0xFF, false)) return rc; // 0xFF: drawn
    return 0;
}
