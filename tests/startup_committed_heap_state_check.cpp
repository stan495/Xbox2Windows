// Device check for committed resources made through the startup device wrapper (slot 127, the
// CreateCommittedResource3 argument order). D3D12 requires an upload-heap resource to start in
// GENERIC_READ and a readback-heap resource in COPY_DEST; the Windows 10 runtime refuses COMMON there
// with E_INVALIDARG. Buffers on the default, upload and readback heaps, passed as a title passes them
// (initial layout 0xffffffff, no clear value), must all be created.
// usage: startup_committed_heap_state_check warp|hardware [--mutate upload-texture]
// The mutated run asks for a 2D texture on an upload heap, which no runtime creates: the negative control.
#include "xmem_test_device.h"

#include <cstring>

extern "C" __declspec(dllimport) HRESULT WINAPI D3D12XboxCreateDevice(IUnknown*, const void*, REFIID, void**);

namespace {
HRESULT create_wrapper(IUnknown* adapter, REFIID iid, void** out)
{
    unsigned char parameters[64]{};
    return D3D12XboxCreateDevice(adapter, parameters, iid, out);
}

// Console device slot 127: heap properties, heap flags, desc, initial state or layout, clear value,
// protected session, castable format count and formats, riid, ppv.
using Committed127 = HRESULT (*)(void*, const D3D12_HEAP_PROPERTIES*, UINT, const D3D12_RESOURCE_DESC*, UINT,
                                 const D3D12_CLEAR_VALUE*, void*, UINT, const void*, const GUID*, void**);
constexpr UINT layout_undefined = 0xffffffff;

Committed127 slot127(ID3D12Device* device)
{
    return reinterpret_cast<Committed127>((*reinterpret_cast<void***>(device))[127]);
}

D3D12_RESOURCE_DESC buffer(UINT64 bytes)
{
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = bytes;
    desc.Height = 1;
    desc.DepthOrArraySize = 1;
    desc.MipLevels = 1;
    desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    return desc;
}

D3D12_RESOURCE_DESC texture(UINT width, UINT height)
{
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = width;
    desc.Height = height;
    desc.DepthOrArraySize = 1;
    desc.MipLevels = 1;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.SampleDesc.Count = 1;
    return desc;
}

const char* heap_name(D3D12_HEAP_TYPE type)
{
    return type == D3D12_HEAP_TYPE_DEFAULT ? "default" : type == D3D12_HEAP_TYPE_UPLOAD ? "upload" : "readback";
}

// Returns 0 when slot 127 creates the resource and the device stays alive.
int check(ID3D12Device* wrapper, D3D12_HEAP_TYPE type, const D3D12_RESOURCE_DESC& desc)
{
    const D3D12_HEAP_PROPERTIES props{type};
    const GUID iid = __uuidof(ID3D12Resource);
    void* created = nullptr;
    const HRESULT hr = slot127(wrapper)(wrapper, &props, 0, &desc, layout_undefined, nullptr, nullptr, 0, nullptr, &iid, &created);
    const HRESULT removed = wrapper->GetDeviceRemovedReason();
    std::printf("%s heap, %s of 0x%llx: slot 127 -> 0x%08lx\n", heap_name(type),
                desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER ? "buffer" : "2D texture",
                static_cast<unsigned long long>(desc.Width), static_cast<unsigned long>(hr));
    const int result = FAILED(hr) || !created || FAILED(removed) ? 1 : 0;
    if (result)
        std::printf("FAIL %s heap: hr 0x%08lx object %p device 0x%08lx\n", heap_name(type), static_cast<unsigned long>(hr), created,
                    static_cast<unsigned long>(removed));
    if (created) static_cast<IUnknown*>(created)->Release();
    return result;
}
}  // namespace

int main(int argc, char** argv)
{
    if (argc != 2 && !(argc == 4 && std::strcmp(argv[2], "--mutate") == 0 && std::strcmp(argv[3], "upload-texture") == 0)) return 1;
    const bool warp = std::strcmp(argv[1], "warp") == 0;
    const bool mutated = argc == 4;
    bool unmatched = false;
    auto desktop = create_device(warp, unmatched);
    if (!desktop) return warp || unmatched ? 1 : 77;

    // What this runtime does with COMMON on an upload heap: recorded, not checked (Windows 10 refuses it).
    const D3D12_HEAP_PROPERTIES upload{D3D12_HEAP_TYPE_UPLOAD};
    const D3D12_RESOURCE_DESC probe = buffer(0x10000);
    Microsoft::WRL::ComPtr<ID3D12Resource> common;
    const HRESULT common_hr = desktop->CreateCommittedResource(&upload, D3D12_HEAP_FLAG_NONE, &probe, D3D12_RESOURCE_STATE_COMMON,
                                                               nullptr, IID_PPV_ARGS(&common));
    std::printf("this runtime, upload buffer in COMMON: 0x%08lx (%s)\n", static_cast<unsigned long>(common_hr),
                SUCCEEDED(common_hr) ? "accepted" : "refused");

    SetEnvironmentVariableW(L"X2W_D3D12_PERMISSIVE", L"1");
    auto wrapper = create_device(warp, unmatched, create_wrapper);
    if (!wrapper) return 1;
    int failures = 0;
    // Sizes from a startup run: 150 and 320 entries of 768 bytes on an upload heap, one entry on a readback heap.
    failures += check(wrapper.Get(), D3D12_HEAP_TYPE_DEFAULT, buffer(0x10000));
    failures += check(wrapper.Get(), D3D12_HEAP_TYPE_UPLOAD, mutated ? texture(64, 64) : buffer(150 * 768));
    failures += check(wrapper.Get(), D3D12_HEAP_TYPE_UPLOAD, buffer(320 * 768));
    failures += check(wrapper.Get(), D3D12_HEAP_TYPE_READBACK, buffer(768));
    if (failures) return 1;
    std::puts("startup_committed_heap_state: PASS (default, upload and readback buffers created through slot 127)");
    return 0;
}
