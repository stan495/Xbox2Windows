// OMSetRenderTargets with a single descriptor range, handles that hold no view, and ClearRenderTargetView on a
// copied descriptor (slots 50 and 52, device slot 24).
//
//   startup_rtv_bind <warp|hardware>     (X2W_D3D12_PERMISSIVE=1 X2W_D3D12_LIST_NOOP=1 X2W_D3D12_RENDER=1 X2W_D3D12_DEBUG=1)
//
// Through D3D12XboxCreateDevice the probe makes an RTV heap of three descriptors over two 1x1 RGBA8 targets,
// views 0 and 1 created, view 2 left empty, and records OMSetRenderTargets calls on one list. After each call
// x2w_d3d12_render_target_stats reports how many binds were accepted and how many handles held no view:
//
//   1. one handle, array form                      accepted
//   2. one handle, single range, count 1           accepted   (the title's first handle only)
//   3. one handle, single range, count 2           accepted   (view 1 follows view 0 at the descriptor stride)
//   4. two handles, array form                     accepted
//   5. one handle, single range, count 3           refused    (view 2 holds nothing: counted)
//   6. view 2 as the only target                   refused    (counted)
//
// Then view 0 is copied into the empty slot 2 with CopyDescriptorsSimple, ClearRenderTargetView(slot 2) clears
// the copy red, the copy is bound, and a valid view for target 1 is copied into slot 4 with CopyDescriptors and
// cleared green. A packed-format RTV that view_slot<20> skips is placed in slot 3 and copied into slot 5 with
// CopyDescriptors: clearing or binding either skipped view (directly or in a single range) must be refused and
// leave target 2 untouched.
//
// The desktop device must not be removed and the debug layer must store no errors.
//
// Exit codes: 0 pass, 77 no physical adapter, other values name the failing step.
#include <d3d12.h>
#include <d3d12sdklayers.h>
#include <dxgi1_6.h>
#include <wrl/client.h>

#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

using Microsoft::WRL::ComPtr;
extern "C" __declspec(dllimport) HRESULT WINAPI D3D12XboxCreateDevice(IUnknown*, const void*, REFIID, void**);
extern "C" __declspec(dllimport) void x2w_d3d12_render_target_stats(unsigned* binds, unsigned* unresolved);

namespace {
using Word = std::uint64_t;
template <class T> Word arg(T* value) { return reinterpret_cast<Word>(value); }
Word invoke(void* object, unsigned index, Word a = 0, Word b = 0, Word c = 0, Word d = 0, Word e = 0, Word f = 0, Word g = 0)
{
    using Slot = Word (WINAPI*)(void*, Word, Word, Word, Word, Word, Word, Word);
    return reinterpret_cast<Slot>((*static_cast<void***>(object))[index])(object, a, b, c, d, e, f, g);
}
bool ok(Word hr) { return SUCCEEDED(static_cast<HRESULT>(hr)); }

constexpr float red[] = {1, 0, 0, 1};
constexpr std::uint32_t red_bits = 0xff0000ff;
constexpr float green[] = {0, 1, 0, 1};
constexpr std::uint32_t green_bits = 0xff00ff00;

struct Stats {
    unsigned binds = 0, unresolved = 0;
    static Stats read()
    {
        Stats stats;
        x2w_d3d12_render_target_stats(&stats.binds, &stats.unresolved);
        return stats;
    }
};

// A bind call and what it did to the counters: accepted binds and handles that held no view.
bool step(const char* name, unsigned added_binds, unsigned added_unresolved, Stats& before)
{
    const Stats now = Stats::read();
    const bool good = now.binds - before.binds == added_binds && now.unresolved - before.unresolved == added_unresolved;
    std::printf("%s: binds +%u (expected +%u), unresolved +%u (expected +%u)\n", name, now.binds - before.binds, added_binds,
                now.unresolved - before.unresolved, added_unresolved);
    before = now;
    return good;
}

struct Probe {
    ComPtr<ID3D12Device> desktop;
    ComPtr<ID3D12Resource> readback;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint{};
    void* queue = nullptr;
    void* fence = nullptr;
    Word fence_value = 0;

    bool submit(void* list)
    {
        void* lists[] = {list};
        invoke(queue, 10, 1, arg(lists));
        if (!ok(invoke(queue, 14, arg(fence), ++fence_value))) return false;
        const auto deadline = GetTickCount64() + 10000;
        while (invoke(fence, 8) < fence_value) {
            if (GetTickCount64() >= deadline) return false;
            Sleep(1);
        }
        return true;
    }
    UINT64 debug_errors()
    {
        ComPtr<ID3D12InfoQueue> info;
        if (FAILED(desktop.As(&info))) return 0;
        UINT64 errors = 0;
        for (UINT64 i = 0, n = info->GetNumStoredMessages(); i < n; ++i) {
            SIZE_T bytes = 0;
            if (FAILED(info->GetMessage(i, nullptr, &bytes))) continue;
            std::vector<char> storage(bytes);
            auto* message = reinterpret_cast<D3D12_MESSAGE*>(storage.data());
            if (FAILED(info->GetMessage(i, message, &bytes))) continue;
            if (message->Severity <= D3D12_MESSAGE_SEVERITY_ERROR) {
                ++errors;
                std::printf("debug layer: %.*s\n", static_cast<int>(message->DescriptionByteLength), message->pDescription);
            }
        }
        return errors;
    }
    // The one texel of a target, copied on a desktop queue.
    std::uint32_t texel(ID3D12Resource* target)
    {
        D3D12_COMMAND_QUEUE_DESC desc{};
        ComPtr<ID3D12CommandQueue> observer;
        ComPtr<ID3D12CommandAllocator> allocator;
        ComPtr<ID3D12GraphicsCommandList> copy;
        ComPtr<ID3D12Fence> done;
        if (FAILED(desktop->CreateCommandQueue(&desc, IID_PPV_ARGS(&observer))) ||
            FAILED(desktop->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&allocator))) ||
            FAILED(desktop->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator.Get(), nullptr, IID_PPV_ARGS(&copy))) ||
            FAILED(desktop->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&done))))
            return 0;
        D3D12_TEXTURE_COPY_LOCATION to{readback.Get(), D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT};
        to.PlacedFootprint = footprint;
        D3D12_TEXTURE_COPY_LOCATION from{target, D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX};
        copy->CopyTextureRegion(&to, 0, 0, 0, &from, nullptr);
        if (FAILED(copy->Close())) return 0;
        ID3D12CommandList* lists[] = {copy.Get()};
        observer->ExecuteCommandLists(1, lists);
        HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        const bool finished = event && SUCCEEDED(observer->Signal(done.Get(), 1)) &&
                              SUCCEEDED(done->SetEventOnCompletion(1, event)) && WaitForSingleObject(event, 10000) == WAIT_OBJECT_0;
        if (event) CloseHandle(event);
        void* memory = nullptr;
        if (!finished || FAILED(readback->Map(0, nullptr, &memory))) return 0;
        std::uint32_t bits = 0;
        std::memcpy(&bits, memory, sizeof(bits));
        readback->Unmap(0, nullptr);
        return bits;
    }
};
}  // namespace

int main(int argc, char** argv)
{
    if (argc != 2) return 2;
    ComPtr<IDXGIFactory6> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) return 3;
    ComPtr<IDXGIAdapter> adapter;
    if (std::strcmp(argv[1], "hardware") == 0) {
        for (UINT n = 0;; ++n) {
            ComPtr<IDXGIAdapter1> candidate;
            const HRESULT hr = factory->EnumAdapterByGpuPreference(n, DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE, IID_PPV_ARGS(&candidate));
            if (hr == DXGI_ERROR_NOT_FOUND) return 77;
            if (FAILED(hr)) return 3;
            DXGI_ADAPTER_DESC1 desc{};
            if (FAILED(candidate->GetDesc1(&desc))) return 3;
            if (!(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) &&
                SUCCEEDED(D3D12CreateDevice(candidate.Get(), D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), nullptr))) {
                adapter = candidate;
                break;
            }
        }
    } else if (std::strcmp(argv[1], "warp") != 0 || FAILED(factory->EnumWarpAdapter(IID_PPV_ARGS(&adapter)))) {
        return 3;
    }
    DXGI_ADAPTER_DESC adapter_desc{};
    if (FAILED(adapter->GetDesc(&adapter_desc))) return 3;
    std::printf("adapter=%ls\n", adapter_desc.Description);

    Probe probe;
    std::array<unsigned char, 64> parameters{};
    void* device = nullptr;
    if (FAILED(D3D12XboxCreateDevice(adapter.Get(), parameters.data(), __uuidof(ID3D12Device), &device))) return 4;
    if (FAILED(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&probe.desktop)))) return 4;

    // Three 1x1 RGBA8 targets and the readback buffer for one texel.
    D3D12_HEAP_PROPERTIES heap{D3D12_HEAP_TYPE_DEFAULT};
    const D3D12_RESOURCE_DESC target_desc{D3D12_RESOURCE_DIMENSION_TEXTURE2D, 0, 1, 1, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, {1, 0},
                                          D3D12_TEXTURE_LAYOUT_UNKNOWN, D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET};
    const D3D12_CLEAR_VALUE clear{DXGI_FORMAT_R8G8B8A8_UNORM, {0, 0, 0, 0}};
    ComPtr<ID3D12Resource> targets[3];
    for (auto& target : targets)
        if (FAILED(probe.desktop->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &target_desc, D3D12_RESOURCE_STATE_COMMON,
                                                          &clear, IID_PPV_ARGS(&target)))) return 5;
    UINT64 bytes = 0;
    probe.desktop->GetCopyableFootprints(&target_desc, 0, 1, 0, &probe.footprint, nullptr, nullptr, &bytes);
    heap.Type = D3D12_HEAP_TYPE_READBACK;
    const D3D12_RESOURCE_DESC buffer{D3D12_RESOURCE_DIMENSION_BUFFER, 0, bytes, 1, 1, 1, DXGI_FORMAT_UNKNOWN, {1, 0},
                                     D3D12_TEXTURE_LAYOUT_ROW_MAJOR};
    if (FAILED(probe.desktop->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer, D3D12_RESOURCE_STATE_COPY_DEST,
                                                      nullptr, IID_PPV_ARGS(&probe.readback)))) return 5;

    // The RTV heap: views 0 and 1 over targets 0 and 1, slot 2 empty, slot 3 a refused packed-format view of
    // target 2, slots 4 and 5 empty for CopyDescriptors; plus a two-descriptor staging heap.
    const auto make_heap = [&](UINT count, void** out) {
        const struct {
            D3D12_DESCRIPTOR_HEAP_DESC desc;
            Word backing, bytes;
        } desc{{D3D12_DESCRIPTOR_HEAP_TYPE_RTV, count}, 0, 0};
        return ok(invoke(device, 14, arg(&desc), arg(&__uuidof(ID3D12DescriptorHeap)), arg(out)));
    };
    void *rtv_heap = nullptr, *staging_heap = nullptr;
    if (!make_heap(6, &rtv_heap) || !make_heap(2, &staging_heap)) return 6;
    const UINT stride = probe.desktop->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    const D3D12_CPU_DESCRIPTOR_HANDLE first{static_cast<SIZE_T>(invoke(rtv_heap, 9))};
    const std::array<D3D12_CPU_DESCRIPTOR_HANDLE, 6> rtv{{
        first, {first.ptr + stride}, {first.ptr + 2 * stride}, {first.ptr + 3 * stride}, {first.ptr + 4 * stride},
        {first.ptr + 5 * stride},
    }};
    const D3D12_CPU_DESCRIPTOR_HANDLE staging_first{static_cast<SIZE_T>(invoke(staging_heap, 9))};
    const std::array<D3D12_CPU_DESCRIPTOR_HANDLE, 2> staging{{staging_first, {staging_first.ptr + stride}}};
    D3D12_RENDER_TARGET_VIEW_DESC view{DXGI_FORMAT_R8G8B8A8_UNORM, D3D12_RTV_DIMENSION_TEXTURE2D, {}};
    D3D12_RENDER_TARGET_VIEW_DESC refused_view{DXGI_FORMAT_B5G5R5A1_UNORM, D3D12_RTV_DIMENSION_TEXTURE2D, {}};
    invoke(device, 20, arg(targets[0].Get()), arg(&view), rtv[0].ptr);
    invoke(device, 20, arg(targets[1].Get()), arg(&view), rtv[1].ptr);
    invoke(device, 20, arg(targets[2].Get()), arg(&refused_view), rtv[3].ptr);
    invoke(device, 20, arg(targets[0].Get()), arg(&view), staging[0].ptr);
    invoke(device, 20, arg(targets[1].Get()), arg(&view), staging[1].ptr);

    D3D12_COMMAND_QUEUE_DESC queue_desc{};
    const unsigned list_desc[] = {D3D12_COMMAND_LIST_TYPE_DIRECT, 2, 1, 0};
    void *allocator = nullptr, *list = nullptr;
    if (!ok(invoke(device, 8, arg(&queue_desc), arg(&__uuidof(ID3D12CommandQueue)), arg(&probe.queue))) ||
        !ok(invoke(device, 36, 0, 0, arg(&__uuidof(ID3D12Fence)), arg(&probe.fence))) ||
        !ok(invoke(device, 9, D3D12_COMMAND_LIST_TYPE_DIRECT, arg(&__uuidof(ID3D12CommandAllocator)), arg(&allocator))) ||
        !ok(invoke(device, 73, arg(list_desc), arg(allocator), 0, arg(&__uuidof(ID3D12GraphicsCommandList)), arg(&list))))
        return 7;
    if (!ok(invoke(list, 139, arg(allocator), 0))) return 8;

    const D3D12_CPU_DESCRIPTOR_HANDLE pair[] = {rtv[0], rtv[1]};
    Stats seen = Stats::read();
    invoke(list, 50, 1, arg(&rtv[0]), FALSE, 0);
    if (!step("1 one handle", 1, 0, seen)) return 10;
    invoke(list, 50, 1, arg(&rtv[0]), TRUE, 0);
    if (!step("2 single range, count 1", 1, 0, seen)) return 11;
    invoke(list, 50, 2, arg(&rtv[0]), TRUE, 0);
    if (!step("3 single range, count 2", 1, 0, seen)) return 12;
    invoke(list, 50, 2, arg(pair), FALSE, 0);
    if (!step("4 two handles", 1, 0, seen)) return 13;
    invoke(list, 50, 3, arg(&rtv[0]), TRUE, 0);
    if (!step("5 single range reaching the empty view", 0, 1, seen)) return 14;
    invoke(list, 50, 1, arg(&rtv[2]), FALSE, 0);
    if (!step("6 the empty view alone", 0, 1, seen)) return 15;
    invoke(list, 50, 0, 0, TRUE, 0);
    if (!step("6b single range, count 0, null handles", 0, 0, seen)) return 16;
    invoke(list, 50, 9, arg(&rtv[0]), TRUE, 0);
    if (!step("6c single range, count above 8", 0, 1, seen)) return 17;
    invoke(list, 52, rtv[2].ptr, arg(red));
    if (!step("6d clear on the empty view", 0, 1, seen)) return 18;

    // Refused shadow RTV (desktop creation skipped): clear and bind must both be skipped.
    invoke(list, 52, rtv[3].ptr, arg(red));
    if (!step("6e clear on a refused view", 0, 1, seen)) return 19;
    invoke(list, 50, 1, arg(&rtv[3]), FALSE, 0);
    if (!step("6f bind a refused view", 0, 1, seen)) return 20;

    // A copied descriptor: the empty slot 2 now holds view 0, so it binds and clears target 0 red.
    invoke(device, 24, 1, rtv[2].ptr, staging[0].ptr, D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    invoke(list, 52, rtv[2].ptr, arg(red));
    invoke(list, 50, 1, arg(&rtv[2]), FALSE, 0);
    if (!step("7 copied view (CopyDescriptorsSimple), bound after the clear", 1, 0, seen)) return 21;

    // Single range reaching the refused view in slot 3 (now that slot 2 is populated): must be refused.
    invoke(list, 50, 4, arg(&rtv[0]), TRUE, 0);
    if (!step("7b single range reaching a refused view", 0, 1, seen)) return 24;

    // CopyDescriptors (slot 23): copy a valid view into slot 4 and the refused view into slot 5.
    const UINT one = 1;
    invoke(device, 23, 1, arg(&rtv[4]), arg(&one), 1, arg(&staging[1]), arg(&one), D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    invoke(list, 52, rtv[4].ptr, arg(green));
    invoke(list, 50, 1, arg(&rtv[4]), FALSE, 0);
    if (!step("7c copied valid view (CopyDescriptors), bound after the clear", 1, 0, seen)) return 25;

    invoke(device, 23, 1, arg(&rtv[5]), arg(&one), 1, arg(&rtv[3]), arg(&one), D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    invoke(list, 52, rtv[5].ptr, arg(red));
    if (!step("7d clear on a copied refused view", 0, 1, seen)) return 26;
    invoke(list, 50, 1, arg(&rtv[5]), FALSE, 0);
    if (!step("7e bind a copied refused view", 0, 1, seen)) return 27;

    invoke(list, 9);
    if (!probe.submit(list)) return 22;
    const std::uint32_t bits0 = probe.texel(targets[0].Get());
    const std::uint32_t bits1 = probe.texel(targets[1].Get());
    const std::uint32_t bits2 = probe.texel(targets[2].Get());
    std::printf("8 target 0 after clearing the CopyDescriptorsSimple view: %08x (expected %08x)\n", bits0, red_bits);
    std::printf("9 target 1 after clearing the CopyDescriptors view: %08x (expected %08x)\n", bits1, green_bits);
    std::printf("10 target 2 after skipped refused clears: %08x (expected 00000000)\n", bits2);
    if (bits0 != red_bits) return 23;
    if (bits1 != green_bits) return 28;
    if (bits2 != 0) return 29;

    const HRESULT removed = probe.desktop->GetDeviceRemovedReason();
    const UINT64 errors = probe.debug_errors();
    std::printf("device removed reason: 0x%08lx, debug layer errors: %llu\n", static_cast<unsigned long>(removed),
                static_cast<unsigned long long>(errors));
    return removed == S_OK && errors == 0 ? 0 : 70;
}
