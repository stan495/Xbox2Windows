#include "startup_stubs/list_upload_pages.h"

#include <d3d12sdklayers.h>
#include <dxgi1_6.h>

#include <cstdio>
#include <cstring>

using Microsoft::WRL::ComPtr;

namespace {
int select_adapter(bool hardware, IDXGIFactory6* factory, ComPtr<IDXGIAdapter>& adapter)
{
    if (!hardware) return SUCCEEDED(factory->EnumWarpAdapter(IID_PPV_ARGS(&adapter))) ? 0 : 3;
    for (UINT index = 0;; ++index) {
        ComPtr<IDXGIAdapter1> candidate;
        const HRESULT hr = factory->EnumAdapterByGpuPreference(index, DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE,
                                                                IID_PPV_ARGS(&candidate));
        if (hr == DXGI_ERROR_NOT_FOUND) return 77;
        if (FAILED(hr)) return 3;
        DXGI_ADAPTER_DESC1 desc{};
        if (FAILED(candidate->GetDesc1(&desc))) return 3;
        if (!(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) &&
            SUCCEEDED(D3D12CreateDevice(candidate.Get(), D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), nullptr))) {
            adapter = candidate;
            return 0;
        }
    }
}

const char* check_arena(ID3D12Device* device)
{
    x2w_upload::Arena arena;
    const auto first = arena.allocate(device, 640, 16);
    if (!first.cpu || !first.gpu || !first.resource || first.cpu % 16 || first.gpu % 16 || first.bytes != 640 ||
        first.gpu != first.resource->GetGPUVirtualAddress() + first.offset) return "640/16 allocation";
    const auto second = arena.allocate(device, 16, 16);
    if (!second.cpu || second.resource != first.resource || second.cpu < first.cpu + first.bytes) return "same-page allocation";
    const auto large = arena.allocate(device, 65536, 16);
    if (!large.cpu || large.resource == first.resource || large.bytes != 65536) return "page rollover";

    auto* payload = reinterpret_cast<unsigned char*>(first.cpu);
    std::memset(payload, 0x17, 640);
    std::memset(reinterpret_cast<void*>(second.cpu), 0x34, 16);
    std::memset(reinterpret_cast<void*>(large.cpu), 0x56, 65536);
    if (!std::all_of(payload, payload + 640, [](unsigned char value) { return value == 0x17; })) return "retained CPU payload";
    if (arena.translate(first.cpu, 640) != first.gpu || arena.translate(first.cpu + 16, 624) != first.gpu + 16 ||
        arena.translate(first.cpu + 639, 1) != first.gpu + 639 || arena.translate(second.cpu, 16) != second.gpu ||
        arena.translate(large.cpu, 65536) != large.gpu) return "live-range translation";
    if (arena.translate(first.cpu, 641) || arena.translate(first.cpu + 639, 2) || arena.translate(first.cpu, 0) ||
        arena.allocate(nullptr, 640, 16).cpu || arena.allocate(device, 0, 16).cpu ||
        arena.allocate(device, 640, 0).cpu || arena.allocate(device, 640, 3).cpu ||
        arena.allocate(device, UINT64_MAX, 16).cpu) return "invalid allocation or range accepted";

    // The fixture made no GPU submissions. Both pages can be retired now.
    arena.reset();
    if (arena.translate(first.cpu, 1) || arena.translate(second.cpu, 1) || arena.translate(large.cpu, 1)) return "retired range translated";
    const auto next = arena.allocate(device, 640, 16);
    if (!next.cpu || !next.gpu || arena.translate(next.cpu, 640) != next.gpu) return "allocation after idle reset";
    return nullptr;
}
}

int main(int argc, char** argv)
{
    if (argc != 2 || (std::strcmp(argv[1], "warp") && std::strcmp(argv[1], "hardware"))) return 2;
    ComPtr<ID3D12Debug> debug;
    const bool debugging = SUCCEEDED(D3D12GetDebugInterface(IID_PPV_ARGS(&debug)));
    if (debugging) debug->EnableDebugLayer();
    ComPtr<IDXGIFactory6> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) return 3;
    ComPtr<IDXGIAdapter> adapter;
    if (const int result = select_adapter(!std::strcmp(argv[1], "hardware"), factory.Get(), adapter)) return result;
    DXGI_ADAPTER_DESC adapter_desc{};
    LARGE_INTEGER driver{};
    if (FAILED(adapter->GetDesc(&adapter_desc))) return 3;
    adapter->CheckInterfaceSupport(__uuidof(IDXGIDevice), &driver);
    std::printf("adapter=%ls driver=%u.%u.%u.%u debug_layer=%s\n", adapter_desc.Description,
                HIWORD(driver.HighPart), LOWORD(driver.HighPart), HIWORD(driver.LowPart), LOWORD(driver.LowPart),
                debugging ? "enabled" : "unavailable");
    ComPtr<ID3D12Device> device;
    if (FAILED(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&device)))) return 4;
    if (const char* mismatch = check_arena(device.Get())) {
        std::fprintf(stderr, "Upload arena contract failed: %s\n", mismatch);
        return 5;
    }
    if (const HRESULT hr = device->GetDeviceRemovedReason(); FAILED(hr)) {
        std::fprintf(stderr, "Device removed: 0x%08lx\n", static_cast<unsigned long>(hr));
        return 6;
    }
    std::puts("640/16 allocation, page rollover, bounded translation and idle reset passed");
    return 0;
}
