// Device slot 15, GetDescriptorHandleIncrementSize (#870), through D3D12XboxCreateDevice.
//
//   startup_descriptor_increment <warp|hardware>     (X2W_D3D12_PERMISSIVE=1 X2W_D3D12_DEBUG=1)
//
// The four types desktop has (0 to 3) must answer what a desktop device answers on the same adapter, and each answer
// must be above zero. A value past them (4, and 0xFFFFFFFF with the upper register bits set) must answer 0, as desktop
// does, and the desktop debug layer must store no error for any of the calls: desktop itself reports an error for such
// a value, so a stand-in that forwards it fails here.
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

namespace {
using Word = std::uint64_t;
// Slot 15 of the device with the type in the first argument word; the upper word bits are set on purpose, as a
// caller leaves the top half of a 32-bit argument undefined.
UINT increment_size(void* device, std::uint32_t type, Word upper)
{
    using Slot = UINT(WINAPI*)(void*, Word);
    return reinterpret_cast<Slot>((*static_cast<void***>(device))[15])(device, upper | type);
}
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

    std::array<unsigned char, 64> parameters{};
    void* device = nullptr;
    if (FAILED(D3D12XboxCreateDevice(adapter.Get(), parameters.data(), __uuidof(ID3D12Device), &device))) return 4;
    ComPtr<ID3D12Device> desktop;
    if (FAILED(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&desktop)))) return 4;
    ComPtr<ID3D12InfoQueue> info;
    desktop.As(&info);
    if (info) info->ClearStoredMessages();

    int failed = 0;
    for (std::uint32_t type = 0; type < D3D12_DESCRIPTOR_HEAP_TYPE_NUM_TYPES; ++type) {
        const UINT want = desktop->GetDescriptorHandleIncrementSize(static_cast<D3D12_DESCRIPTOR_HEAP_TYPE>(type));
        const UINT got = increment_size(device, type, 0xFFFFFFFF00000000ull);
        std::printf("type %u: %u (desktop %u)\n", type, got, want);
        if (got != want || got == 0) {
            std::printf("FAIL type %u\n", type);
            failed |= 1;
        }
    }
    for (std::uint32_t type : {4u, 0xFFFFFFFFu}) {
        const UINT got = increment_size(device, type, 0);
        std::printf("type 0x%x: %u (expected 0)\n", type, got);
        if (got != 0) {
            std::printf("FAIL type 0x%x\n", type);
            failed |= 2;
        }
    }
    if (info) {
        for (UINT64 i = 0, n = info->GetNumStoredMessages(); i < n; ++i) {
            SIZE_T bytes = 0;
            if (FAILED(info->GetMessage(i, nullptr, &bytes))) continue;
            std::vector<char> storage(bytes);
            auto* message = reinterpret_cast<D3D12_MESSAGE*>(storage.data());
            if (FAILED(info->GetMessage(i, message, &bytes))) continue;
            if (message->Severity <= D3D12_MESSAGE_SEVERITY_ERROR) {
                std::printf("debug layer id %d: %.*s\n", static_cast<int>(message->ID),
                            static_cast<int>(message->DescriptionByteLength), message->pDescription);
                failed |= 4;
            }
        }
    } else {
        std::printf("no debug layer: answers checked, messages not\n");
    }
    return failed ? 10 + failed : 0;
}
