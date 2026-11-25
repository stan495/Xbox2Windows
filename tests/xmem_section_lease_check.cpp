#include <windows.h>
#include <d3d12.h>
#include <d3d12sdklayers.h>
#include <dxgi1_6.h>

#include "desktop_gpu.h"
#include "xmem_section_lease.h"

#include <array>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <stdexcept>

using Microsoft::WRL::ComPtr;

template<class T> T load(HMODULE module, const char* name)
{
    const auto function = reinterpret_cast<T>(GetProcAddress(module, name));
    if (!function) throw std::runtime_error(name);
    return function;
}

void require(bool ok, const char* what)
{
    if (!ok) throw std::runtime_error(what);
}

int wmain(int argc, wchar_t** argv) try {
    if (argc != 3 || (std::wcscmp(argv[1], L"warp") && std::wcscmp(argv[1], L"hardware"))) return 2;
    const bool warp = std::wcscmp(argv[1], L"warp") == 0;
    SetEnvironmentVariableA("X2W_XMEM_PERMISSIVE", "1");
    SetEnvironmentVariableA("X2W_XMEM_GPU_MIRROR", "1");
    SetEnvironmentVariableA("X2W_XMEM_LAZY_COMMIT", nullptr);
    const auto xmem = LoadLibraryW(argv[2]);
    require(xmem != nullptr, "load xmem");
    const auto reserve = load<void* (WINAPI*)(void*, SIZE_T, DWORD, ULONGLONG, DWORD)>(xmem, "XMemVirtualAlloc");
    const auto alloc = load<BOOL (WINAPI*)(ULONG_PTR, ULONG_PTR*, ULONG_PTR*)>(xmem, "XMemAllocatePhysicalPages");
    const auto free_pages = load<BOOL (WINAPI*)(ULONG_PTR, ULONG_PTR*)>(xmem, "XMemFreePhysicalPages");
    const auto map = load<BOOL (WINAPI*)(void* const*, ULONG_PTR, const ULONG_PTR*)>(xmem, "XMemMapPhysicalPagesScatter");
    const auto pin = load<BOOL (WINAPI*)(UINT, ULONG_PTR, SIZE_T, X2WXMemSectionLease*, SIZE_T)>(xmem, "x2w_xmem_lease_section");
    const auto unpin = load<BOOL (WINAPI*)(UINT, ULONG64)>(xmem, "x2w_xmem_release_section");
    const auto acquire = [pin](ULONG_PTR address, SIZE_T span, X2WXMemSectionLease* out, SIZE_T size) {
        return pin(x2w_xmem_section_lease_version, address, span, out, size);
    };
    const auto release = [unpin](ULONG64 id) { return unpin(x2w_xmem_section_lease_version, id); };
    constexpr UINT64 window = UINT64{16} << 30, page = 65536;
    constexpr UINT64 first_byte = 0xf800, bytes = 4096;
    auto* const cpu = static_cast<unsigned char*>(reserve(nullptr, window, 0x20402000, 0x21, 4));
    const auto mirror = reinterpret_cast<UINT64>(reserve(nullptr, window, 0x20402000, 3, 0x801));
    require(cpu && mirror, "reserve mirrored windows");
    void* const addresses[]{cpu, cpu + page};
    ULONG_PTR old[2]{}, fresh[2]{}, count = 2;
    require(alloc(0x280, &count, old) && count == 2 && map(addresses, 2, old), "map two adjacent source pages");
    X2WXMemSectionLease lease{}, gap{};
    require(acquire(mirror + first_byte, bytes, &lease, sizeof(lease)), "pin covered physical section");
    require(lease.section && lease.window_bytes == 2 * page && lease.first_byte == first_byte, "lease extent");
    require(!acquire(mirror + 2 * page - 16, 32, &gap, sizeof(gap)) && !gap.section, "reject a span into a missing page");
    require(!acquire(reinterpret_cast<UINT64>(cpu), 4, &gap, sizeof(gap)), "reject the wrong address space");
    require(!acquire(mirror + first_byte, bytes, &gap, sizeof(gap)-1), "reject a malformed output");
    gap.id = 123;
    gap.window_bytes = 456;
    require(!pin(x2w_xmem_section_lease_version + 1, mirror + first_byte, bytes, &gap, sizeof(gap)) &&
            !gap.section && !gap.id && !gap.section_offset && !gap.window_bytes && !gap.first_byte,
            "reject the wrong version and clear the valid output structure");
    require(!unpin(x2w_xmem_section_lease_version + 1, lease.id), "wrong-version release keeps the lease");

    ComPtr<ID3D12Debug> debug;
    if (SUCCEEDED(D3D12GetDebugInterface(IID_PPV_ARGS(&debug)))) debug->EnableDebugLayer();
    ComPtr<IDXGIFactory4> factory;
    check(CreateDXGIFactory2(0, IID_PPV_ARGS(&factory)), "DXGI factory");
    ComPtr<IDXGIAdapter> adapter;
    if (warp) check(factory->EnumWarpAdapter(IID_PPV_ARGS(&adapter)), "WARP");
    ComPtr<ID3D12Device3> device;
    check(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device)), "D3D12 device");
    D3D12_FEATURE_DATA_EXISTING_HEAPS existing{};
    if (FAILED(device->CheckFeatureSupport(D3D12_FEATURE_EXISTING_HEAPS, &existing, sizeof(existing))) ||
        !existing.Supported) return 77;
    ComPtr<ID3D12Heap> heap;
    void* small_view = MapViewOfFile(lease.section, FILE_MAP_WRITE, static_cast<DWORD>(lease.section_offset >> 32),
                                    static_cast<DWORD>(lease.section_offset), static_cast<SIZE_T>(lease.window_bytes));
    require(small_view != nullptr, "map a dense view of only the committed leased section span");
    CloseHandle(lease.section);
    check(device->OpenExistingHeapFromAddress(small_view, IID_PPV_ARGS(&heap)), "open dense mapped view of actual XMem section");
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = lease.window_bytes;
    desc.Height = desc.DepthOrArraySize = desc.MipLevels = desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_CROSS_ADAPTER;
    ComPtr<ID3D12Resource> alias;
    check(device->CreatePlacedResource(heap.Get(), 0, &desc, D3D12_RESOURCE_STATE_COMMON,
                                       nullptr, IID_PPV_ARGS(&alias)), "place shared buffer over the originally mapped pages");
    require(alias->GetGPUVirtualAddress() && alias->GetGPUVirtualAddress() != mirror, "independent desktop GPU VA");
    const auto upload = make_buffer(device.Get(), bytes, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    const auto readback = make_buffer(device.Get(), bytes, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST);
    void* data = nullptr;
    const D3D12_RANGE empty{};
    check(upload->Map(0, &empty, &data), "map upload");
    std::memset(data, 0x29, bytes);
    upload->Unmap(0, nullptr);
    {
        ComputeCommands copy(device.Get(), nullptr, D3D12_COMMAND_LIST_TYPE_DIRECT);
        transition(copy.list.Get(), alias.Get(), D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_DEST);
        copy.list->CopyBufferRegion(alias.Get(), first_byte, upload.Get(), 0, bytes);
        transition(copy.list.Get(), alias.Get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COMMON);
        copy.submit_and_wait(device.Get());
    }
    for (UINT64 i = 0; i < bytes; ++i)
        require(cpu[first_byte + i] == 0x29 && reinterpret_cast<unsigned char*>(mirror)[first_byte+i] == 0x29,
                "both title views see the GPU write");

    {
        ComputeCommands copy(device.Get(), nullptr, D3D12_COMMAND_LIST_TYPE_DIRECT);
        transition(copy.list.Get(), alias.Get(), D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_SOURCE);
        copy.list->CopyBufferRegion(readback.Get(), 0, alias.Get(), first_byte, bytes);
        transition(copy.list.Get(), alias.Get(), D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COMMON);
        for (UINT64 i = 0; i < bytes; ++i) cpu[first_byte + i] = static_cast<unsigned char>(i ^ 0xb7);
        const ULONG_PTR holes[]{0, 0};
        require(map(addresses, 2, holes) && free_pages(2, old), "free original title grant before queue submission");
        count = 2;
        require(alloc(0x280, &count, fresh) && count == 2 && map(addresses, 2, fresh), "reuse former virtual addresses");
        X2WXMemSectionLease replacement{};
        require(acquire(mirror + first_byte, bytes, &replacement, sizeof(replacement)) &&
                replacement.section_offset != lease.section_offset, "pinned slots were not reused");
        CloseHandle(replacement.section);
        require(release(replacement.id), "drop the unused replacement lease");
        std::memset(cpu + first_byte, 0xee, bytes);
        copy.submit_and_wait(device.Get());
    }
    const D3D12_RANGE range{0, bytes};
    check(readback->Map(0, &range, &data), "map readback");
    for (UINT64 i = 0; i < bytes; ++i) {
        require(static_cast<unsigned char*>(data)[i] == static_cast<unsigned char>(i ^ 0xb7), "GPU reads the old backing after title remaps");
        require(cpu[first_byte + i] == 0xee, "replacement backing remains unchanged");
    }
    readback->Unmap(0, &empty);
    alias.Reset();
    heap.Reset();
    if (small_view) require(UnmapViewOfFile(small_view), "drop dense heap view after completion");
    require(release(lease.id) && !release(lease.id), "retire the completed lease exactly once");
    ULONG_PTR extra{}, one = 1;
    void* const extra_address = cpu + 2 * page;
    require(alloc(0x280, &one, &extra) && map(&extra_address, 1, &extra), "acquire another title page");
    X2WXMemSectionLease reused{};
    require(acquire(mirror + 2 * page, 1, &reused, sizeof(reused)) && reused.section_offset == lease.section_offset,
            "retired slot is reusable after the queue fence");
    CloseHandle(reused.section);
    require(release(reused.id), "drop the unused reuse check lease");
    require(static_cast<unsigned char*>(extra_address)[first_byte] == 0, "reused slot was cleared");
    void* const reversed_addresses[]{cpu + 3 * page, cpu + 4 * page};
    const ULONG_PTR reversed_tokens[]{fresh[1], fresh[0]};
    require(map(reversed_addresses, 2, reversed_tokens), "map adjacent CPU addresses with reversed section slots");
    require(!acquire(mirror + 4 * page - 16, 32, &gap, sizeof(gap)), "reject a range whose section slots are reversed");
    std::printf("%s: bidirectional two-page alias, independent GPU address, remap while pinned, late CPU write and post-fence reuse passed (%llu bytes)\n",
                warp ? "WARP" : "physical", bytes);
    return 0;
} catch(const std::exception& error) {
    std::fprintf(stderr, "%s\n", error.what());
    return 1;
}
