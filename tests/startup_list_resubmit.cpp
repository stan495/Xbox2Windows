// Closed command lists submitted again, before and after their allocator's Reset (#1352).
//
//   startup_list_resubmit <warp|hardware>     (X2W_D3D12_PERMISSIVE=1 X2W_D3D12_RENDER=1)
//
// Through D3D12XboxCreateDevice the probe records list A on allocator a (clear to red) and list B on
// allocator b (clear to green), both on one 1x1 RGBA8 target, and reads the target back after each
// step:
//
//   1. submit A                          red
//   2. submit B, then A again            red    a closed list stays submittable
//   3. Reset allocator a, submit B, A    red    A's recording survives its allocator's Reset
//   4. Reset A on a, clear blue, submit  blue   the list records on the allocator now in use
//   5. queue Wait on H (slot 17), submit A behind it, Reset A on b (clear green), Reset allocator a,
//      then signal H on the CPU          blue   a submit the GPU has not passed survives the Reset
//      submit A again                    green
//   6. A on a, submit, Reset allocator a red    the earlier allocator, now idle, is reused: two
//      desktop allocators behind a; A on it, clear blue, submit                   blue
//   7. Fail Close on a compute list whose earlier submission is still queued. Reject a mismatched
//      allocator, then recover with the correct allocator and complete both valid submissions.
//
//   startup_list_resubmit <warp|hardware> untracked   (also X2W_TEST_SUBMIT_FENCE_FAIL=1)
//
// runs the same steps with the stand-in unable to track any submit. Steps 1-5 must read the same;
// nothing may be reused, so step 6 finds four desktop allocators behind a.
//
// The desktop device must not be removed and the debug layer must store no errors. On a stand-in
// that submits a closed list only once, step 2 reads green.
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
extern "C" __declspec(dllimport) unsigned x2w_d3d12_allocator_pool_size(void* allocator);

namespace {
using Word = std::uint64_t;
template <class T> Word arg(T* value) { return reinterpret_cast<Word>(value); }
Word invoke(void* object, unsigned index, Word a = 0, Word b = 0, Word c = 0, Word d = 0, Word e = 0)
{
    using Slot = Word (WINAPI*)(void*, Word, Word, Word, Word, Word);
    return reinterpret_cast<Slot>((*static_cast<void***>(object))[index])(object, a, b, c, d, e);
}
bool ok(Word hr) { return SUCCEEDED(static_cast<HRESULT>(hr)); }
void* make_fence(void* device)
{
    void* fence = nullptr;
    return ok(invoke(device, 36, 0, 0, arg(&__uuidof(ID3D12Fence)), arg(&fence))) ? fence : nullptr;
}

constexpr float red[] = {1, 0, 0, 1}, green[] = {0, 1, 0, 1}, blue[] = {0, 0, 1, 1};
constexpr std::uint32_t red_bits = 0xff0000ff, green_bits = 0xff00ff00, blue_bits = 0xffff0000;

struct Probe {
    ComPtr<ID3D12Device> desktop;
    ComPtr<ID3D12Resource> target, readback;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint{};
    void* queue = nullptr;
    void* fence = nullptr;
    Word fence_value = 0;
    D3D12_CPU_DESCRIPTOR_HANDLE rtv{};

    // Record a clear on list, after Reset on allocator, and close it.
    void record(void* list, void* allocator, const float* colour)
    {
        invoke(list, 139, arg(allocator));
        invoke(list, 52, rtv.ptr, arg(colour));
        invoke(list, 9);
    }
    // Submit a list through the console queue and, unless told not to, wait for the GPU.
    bool submit(void* list, bool wait = true)
    {
        void* lists[] = {list};
        invoke(queue, 10, 1, arg(lists));
        if (!ok(invoke(queue, 14, arg(fence), ++fence_value))) return false;
        return !wait || reached();
    }
    bool reached()
    {
        const auto deadline = GetTickCount64() + 10000;
        while (invoke(fence, 8) < fence_value) {
            if (GetTickCount64() >= deadline) return false;
            Sleep(1);
        }
        return true;
    }
    // Errors and corruption messages the D3D12 debug layer stored for the device.
    UINT64 debug_errors(UINT64 first = 0)
    {
        ComPtr<ID3D12InfoQueue> info;
        if (FAILED(desktop.As(&info))) return 0;
        UINT64 errors = 0;
        for (UINT64 i = first, n = info->GetNumStoredMessages(); i < n; ++i) {
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
    // The target's one texel, copied on a desktop queue.
    std::uint32_t texel()
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
        D3D12_TEXTURE_COPY_LOCATION from{target.Get(), D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX};
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

bool expect(Probe& probe, const char* step, std::uint32_t wanted)
{
    const std::uint32_t got = probe.texel();
    std::printf("%s: %08x (expected %08x)\n", step, got, wanted);
    return got == wanted;
}
}  // namespace

int main(int argc, char** argv)
{
    if (argc != 2 && !(argc == 3 && std::strcmp(argv[2], "untracked") == 0)) return 2;
    const bool untracked = argc == 3;
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

    // 1x1 RGBA8 target with a console RTV, and its readback buffer.
    D3D12_HEAP_PROPERTIES heap{D3D12_HEAP_TYPE_DEFAULT};
    const D3D12_RESOURCE_DESC target_desc{D3D12_RESOURCE_DIMENSION_TEXTURE2D, 0, 1, 1, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, {1, 0},
                                          D3D12_TEXTURE_LAYOUT_UNKNOWN, D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET};
    const D3D12_CLEAR_VALUE clear{DXGI_FORMAT_R8G8B8A8_UNORM, {0, 0, 0, 0}};
    if (FAILED(probe.desktop->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &target_desc, D3D12_RESOURCE_STATE_COMMON,
                                                      &clear, IID_PPV_ARGS(&probe.target)))) return 5;
    UINT64 bytes = 0;
    probe.desktop->GetCopyableFootprints(&target_desc, 0, 1, 0, &probe.footprint, nullptr, nullptr, &bytes);
    heap.Type = D3D12_HEAP_TYPE_READBACK;
    const D3D12_RESOURCE_DESC buffer{D3D12_RESOURCE_DIMENSION_BUFFER, 0, bytes, 1, 1, 1, DXGI_FORMAT_UNKNOWN, {1, 0},
                                     D3D12_TEXTURE_LAYOUT_ROW_MAJOR};
    if (FAILED(probe.desktop->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer, D3D12_RESOURCE_STATE_COPY_DEST,
                                                      nullptr, IID_PPV_ARGS(&probe.readback)))) return 5;
    const struct {
        D3D12_DESCRIPTOR_HEAP_DESC desc;
        Word backing, bytes;
    } rtv_desc{{D3D12_DESCRIPTOR_HEAP_TYPE_RTV, 1}, 0, 0};
    void* rtv_heap = nullptr;
    if (!ok(invoke(device, 14, arg(&rtv_desc), arg(&__uuidof(ID3D12DescriptorHeap)), arg(&rtv_heap)))) return 6;
    probe.rtv = {static_cast<SIZE_T>(invoke(rtv_heap, 9))};
    D3D12_RENDER_TARGET_VIEW_DESC view{DXGI_FORMAT_R8G8B8A8_UNORM, D3D12_RTV_DIMENSION_TEXTURE2D, {}};
    invoke(device, 20, arg(probe.target.Get()), arg(&view), probe.rtv.ptr);

    // One queue and fence; lists A and B on allocators a and b.
    D3D12_COMMAND_QUEUE_DESC queue_desc{};
    const unsigned list_desc[] = {D3D12_COMMAND_LIST_TYPE_DIRECT, 2, 1, 0};
    void *a = nullptr, *b = nullptr, *list_a = nullptr, *list_b = nullptr;
    if (!ok(invoke(device, 8, arg(&queue_desc), arg(&__uuidof(ID3D12CommandQueue)), arg(&probe.queue))) ||
        !ok(invoke(device, 36, 0, 0, arg(&__uuidof(ID3D12Fence)), arg(&probe.fence))) ||
        !ok(invoke(device, 9, D3D12_COMMAND_LIST_TYPE_DIRECT, arg(&__uuidof(ID3D12CommandAllocator)), arg(&a))) ||
        !ok(invoke(device, 9, D3D12_COMMAND_LIST_TYPE_DIRECT, arg(&__uuidof(ID3D12CommandAllocator)), arg(&b))) ||
        !ok(invoke(device, 73, arg(list_desc), arg(a), 0, arg(&__uuidof(ID3D12GraphicsCommandList)), arg(&list_a))) ||
        !ok(invoke(device, 73, arg(list_desc), arg(b), 0, arg(&__uuidof(ID3D12GraphicsCommandList)), arg(&list_b))))
        return 7;
    probe.record(list_a, a, red);
    probe.record(list_b, b, green);

    if (!probe.submit(list_a)) return 10;
    if (!expect(probe, "1 submit A", red_bits)) return 11;

    if (!probe.submit(list_b)) return 20;
    if (!expect(probe, "2 submit B", green_bits)) return 21;
    if (!probe.submit(list_a)) return 22;
    if (!expect(probe, "2 submit A again", red_bits)) return 23;

    if (!ok(invoke(a, 8))) return 30;  // allocator a Reset while A still holds its recording
    if (!probe.submit(list_b)) return 31;
    if (!expect(probe, "3 after allocator Reset, submit B", green_bits)) return 32;
    if (!probe.submit(list_a)) return 33;
    if (!expect(probe, "3 after allocator Reset, submit A", red_bits)) return 34;

    probe.record(list_a, a, blue);
    if (!probe.submit(list_a)) return 40;
    if (!expect(probe, "4 A reset on a, clear blue", blue_bits)) return 41;

    // 5. The GPU has not passed a's last submit at its Reset: the queue waits on H (slot 17), A (blue,
    // on a) is submitted behind the wait, then A is reset on b, so no list holds a recording on a.
    // Resetting a's allocator then would discard the pending submit; the stand-in must move to
    // another one. After the CPU signals H, the pending submit draws blue.
    void* h = make_fence(device);
    if (!h || !ok(invoke(probe.queue, 17, arg(h), 1))) return 50;
    if (!probe.submit(list_a, false)) return 51;
    probe.record(list_a, b, green);
    if (!ok(invoke(a, 8))) return 52;
    if (invoke(probe.fence, 8) >= probe.fence_value) return 53;  // the submit is still behind the wait
    if (!ok(invoke(h, 10, 1))) return 54;                         // fence H Signal(1) from the CPU
    if (!probe.reached()) return 55;
    if (!expect(probe, "5 Reset while the GPU waits, pending submit of A", blue_bits)) return 56;
    if (!probe.submit(list_a)) return 57;
    if (!expect(probe, "5 A re-recorded on b", green_bits)) return 58;

    // 6. Reuse: A records on a again and is submitted, so a's current allocator holds a recording at
    // the next Reset. The earlier allocator the GPU has now passed is reused, so the pool keeps two.
    // With X2W_TEST_SUBMIT_FENCE_FAIL=1 no submit can be tracked, nothing is reused and every Reset
    // that has to move adds an allocator: four by now.
    probe.record(list_a, a, red);
    if (!probe.submit(list_a)) return 60;
    if (!expect(probe, "6 A on a, clear red", red_bits)) return 61;
    if (!ok(invoke(a, 8))) return 62;
    const unsigned pool = x2w_d3d12_allocator_pool_size(a), wanted_pool = untracked ? 4 : 2;
    std::printf("6 desktop allocators behind a: %u (expected %u)\n", pool, wanted_pool);
    if (pool != wanted_pool) return 63;
    probe.record(list_a, a, blue);
    if (!probe.submit(list_a)) return 64;
    if (!expect(probe, "6 A after the Reset, clear blue", blue_bits)) return 65;

    const HRESULT removed = probe.desktop->GetDeviceRemovedReason();
    const UINT64 errors = probe.debug_errors();
    std::printf("device removed reason: 0x%08lx, debug layer errors: %llu\n", static_cast<unsigned long>(removed),
                static_cast<unsigned long long>(errors));
    if (removed != S_OK || errors != 0) return 70;

    // Use a real desktop recording error: a COMPUTE list cannot set a viewport.
    // Keep the earlier valid submit behind a queue wait during the replacement.
    const unsigned compute_desc[] = {D3D12_COMMAND_LIST_TYPE_COMPUTE, 2, 1, 0};
    queue_desc.Type = D3D12_COMMAND_LIST_TYPE_COMPUTE;
    void *compute = nullptr, *compute_a = nullptr, *compute_b = nullptr;
    if (!ok(invoke(device, 9, compute_desc[0], arg(&__uuidof(ID3D12CommandAllocator)), arg(&compute_a))) ||
        !ok(invoke(device, 9, compute_desc[0], arg(&__uuidof(ID3D12CommandAllocator)), arg(&compute_b))) ||
        !ok(invoke(device, 73, arg(compute_desc), arg(compute_a), 0, arg(&__uuidof(ID3D12GraphicsCommandList)), arg(&compute))) ||
        !ok(invoke(device, 8, arg(&queue_desc), arg(&__uuidof(ID3D12CommandQueue)), arg(&probe.queue)))) return 71;
    void* compute_gate = make_fence(device);
    if (!compute_gate || !ok(invoke(probe.queue, 17, arg(compute_gate), 1)) ||
        !ok(invoke(compute, 139, arg(compute_a))) || !ok(invoke(compute, 9)) || !probe.submit(compute, false)) return 72;

    if (!ok(invoke(compute, 139, arg(compute_b)))) return 73;
    const D3D12_VIEWPORT viewport{0, 0, 1, 1, 0, 1};
    invoke(compute, 21, 1, arg(&viewport));
    const auto bad_close = static_cast<HRESULT>(invoke(compute, 9));
    const auto bad_reset = static_cast<HRESULT>(invoke(compute, 139, arg(a)));
    std::printf("7 failed Close: 0x%08lx, wrong-type Reset: 0x%08lx\n",
                static_cast<unsigned long>(bad_close), static_cast<unsigned long>(bad_reset));
    if (SUCCEEDED(bad_close) || bad_reset != E_INVALIDARG) {
        invoke(compute_gate, 10, 1);
        return 74;
    }

    ComPtr<ID3D12InfoQueue> info;
    probe.desktop.As(&info);
    const UINT64 expected_fault_messages = info ? info->GetNumStoredMessages() : 0;
    if (!probe.submit(compute, false)) return 75; // The failed recording is dropped by the queue wrapper.
    const auto recovered_reset = static_cast<HRESULT>(invoke(compute, 139, arg(compute_b)));
    const auto recovered_close = static_cast<HRESULT>(invoke(compute, 9));
    const unsigned compute_b_pool = x2w_d3d12_allocator_pool_size(compute_b);
    std::printf("7 recovered Reset: 0x%08lx, Close: 0x%08lx, desktop allocators behind compute_b: %u (expected 2)\n",
                static_cast<unsigned long>(recovered_reset), static_cast<unsigned long>(recovered_close),
                compute_b_pool);
    if (FAILED(recovered_reset) || FAILED(recovered_close) || compute_b_pool != 2 || !probe.submit(compute, false))
        return 76;
    if (invoke(probe.fence, 8) >= probe.fence_value ||
        !ok(invoke(compute_gate, 10, 1)) || !probe.reached()) return 78;
    // Once the failed list is released, Reset on compute_b reuses the retired desktop allocator instead of growing.
    if (!ok(invoke(compute_b, 8))) return 80;
    const unsigned compute_b_reused = x2w_d3d12_allocator_pool_size(compute_b);
    std::printf("7 after compute_b Reset, desktop allocators behind compute_b: %u (expected 2)\n", compute_b_reused);
    if (compute_b_reused != 2 || !ok(invoke(compute, 139, arg(compute_b))) || !ok(invoke(compute, 9)) ||
        !probe.submit(compute))
        return 81;
    const auto remaining_errors = probe.debug_errors(expected_fault_messages);
    std::printf("7 device removed reason after recovery: 0x%08lx, unexpected debug layer errors: %llu\n",
                static_cast<unsigned long>(probe.desktop->GetDeviceRemovedReason()), remaining_errors);
    return probe.desktop->GetDeviceRemovedReason() == S_OK && remaining_errors == 0 ? 0 : 79;
}
