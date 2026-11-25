// cpu_srv::refresh (src/cpu_srv_upload.h) without a device. cpu_srv_refresh_warp and
// cpu_srv_stale_warp refresh a real upload buffer: an exact-fit refresh at byte 16, an outstanding
// fence, a refresh one byte past the end, a SIZE_MAX offset and a source on an unreadable page.
// This check drives the header with a fake upload resource and fence and covers what those fixtures
// leave open: each argument refusal, a resource that is not a buffer, a heap that is not an upload
// heap, a failing GetHeapProperties or Map, a removed device, a fence that completed past the
// value, a source that straddles an unreadable page, and the Map and Unmap ranges. Every refusal
// must leave the upload unmapped and its bytes unchanged. Expected values are worked by hand.
// --mutate written-range: expects Unmap to report the range from byte 0; it must fail.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include "cpu_srv_upload.h"

#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>

namespace {
int failures;
bool written_from_zero;

constexpr std::byte sentinel{0xee};

// An upload buffer as refresh sees it: GetDesc, GetHeapProperties, Map and Unmap over 32 bytes of
// storage. Map hands out the storage and records the read range; Unmap records the written range.
class Upload final : public ID3D12Resource {
public:
    D3D12_RESOURCE_DIMENSION dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    UINT64 width = 24;
    D3D12_HEAP_TYPE heap = D3D12_HEAP_TYPE_UPLOAD;
    HRESULT heap_result = S_OK;
    HRESULT map_result = S_OK;
    std::array<std::byte, 32> storage;
    int maps = 0, unmaps = 0;
    UINT map_subresource = ~0u, unmap_subresource = ~0u;
    bool read_range_given = false, written_range_given = false;
    D3D12_RANGE read_range{~SIZE_T{0}, ~SIZE_T{0}}, written_range{~SIZE_T{0}, ~SIZE_T{0}};

    Upload() { storage.fill(sentinel); }

    D3D12_RESOURCE_DESC STDMETHODCALLTYPE GetDesc() override
    {
        D3D12_RESOURCE_DESC desc{};
        desc.Dimension = dimension;
        desc.Width = width;
        desc.Height = 1;
        desc.DepthOrArraySize = 1;
        desc.MipLevels = 1;
        desc.SampleDesc.Count = 1;
        desc.Layout = dimension == D3D12_RESOURCE_DIMENSION_BUFFER ? D3D12_TEXTURE_LAYOUT_ROW_MAJOR
                                                                   : D3D12_TEXTURE_LAYOUT_UNKNOWN;
        if (dimension != D3D12_RESOURCE_DIMENSION_BUFFER) desc.Format = DXGI_FORMAT_R8_UINT;
        return desc;
    }
    HRESULT STDMETHODCALLTYPE GetHeapProperties(D3D12_HEAP_PROPERTIES* properties, D3D12_HEAP_FLAGS* flags) override
    {
        if (FAILED(heap_result)) return heap_result;
        if (properties) {
            *properties = {};
            properties->Type = heap;
            properties->CreationNodeMask = properties->VisibleNodeMask = 1;
        }
        if (flags) *flags = D3D12_HEAP_FLAG_NONE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE Map(UINT subresource, const D3D12_RANGE* range, void** data) override
    {
        ++maps;
        map_subresource = subresource;
        read_range_given = range != nullptr;
        if (range) read_range = *range;
        if (FAILED(map_result)) {
            if (data) *data = nullptr;
            return map_result;
        }
        if (data) *data = storage.data();
        return S_OK;
    }
    void STDMETHODCALLTYPE Unmap(UINT subresource, const D3D12_RANGE* range) override
    {
        ++unmaps;
        unmap_subresource = subresource;
        written_range_given = range != nullptr;
        if (range) written_range = *range;
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, void** out) override
    {
        if (out) *out = nullptr;
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return 1; }
    ULONG STDMETHODCALLTYPE Release() override { return 1; }
    HRESULT STDMETHODCALLTYPE GetPrivateData(REFGUID, UINT*, void*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE SetPrivateData(REFGUID, UINT, const void*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE SetPrivateDataInterface(REFGUID, const IUnknown*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE SetName(LPCWSTR) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE GetDevice(REFIID, void** out) override
    {
        if (out) *out = nullptr;
        return E_NOTIMPL;
    }
    D3D12_GPU_VIRTUAL_ADDRESS STDMETHODCALLTYPE GetGPUVirtualAddress() override { return 0; }
    HRESULT STDMETHODCALLTYPE WriteToSubresource(UINT, const D3D12_BOX*, const void*, UINT, UINT) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE ReadFromSubresource(void*, UINT, UINT, UINT, const D3D12_BOX*) override { return E_NOTIMPL; }
};

// A fence that reports a fixed completed value.
class Fence final : public ID3D12Fence {
public:
    explicit Fence(UINT64 completed) : completed_(completed) {}
    UINT64 STDMETHODCALLTYPE GetCompletedValue() override { return completed_; }
    HRESULT STDMETHODCALLTYPE SetEventOnCompletion(UINT64, HANDLE) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE Signal(UINT64) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, void** out) override
    {
        if (out) *out = nullptr;
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return 1; }
    ULONG STDMETHODCALLTYPE Release() override { return 1; }
    HRESULT STDMETHODCALLTYPE GetPrivateData(REFGUID, UINT*, void*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE SetPrivateData(REFGUID, UINT, const void*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE SetPrivateDataInterface(REFGUID, const IUnknown*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE SetName(LPCWSTR) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE GetDevice(REFIID, void** out) override
    {
        if (out) *out = nullptr;
        return E_NOTIMPL;
    }

private:
    UINT64 completed_;
};

constexpr std::array<std::byte, 8> source{std::byte{0x11}, std::byte{0x22}, std::byte{0x33}, std::byte{0x44},
                                          std::byte{0x55}, std::byte{0x66}, std::byte{0x77}, std::byte{0x88}};

// A refresh of bytes [offset, offset + bytes) must map subresource 0 without a CPU read, copy the
// source there, leave every other byte alone and unmap with exactly that written range.
void accept(const char* name, Upload& upload, SIZE_T offset, SIZE_T bytes, ID3D12Fence* fence, UINT64 value)
{
    const HRESULT hr = cpu_srv::refresh(&upload, offset, source.data(), bytes, fence, value);
    bool contents = true;
    for (SIZE_T i = 0; i < upload.storage.size(); ++i) {
        const std::byte expected = i >= offset && i < offset + bytes ? source[i - offset] : sentinel;
        contents = contents && upload.storage[i] == expected;
    }
    const SIZE_T written_begin = written_from_zero ? 0 : offset;
    if (hr != S_OK || !contents || upload.maps != 1 || upload.unmaps != 1 || upload.map_subresource != 0 ||
        upload.unmap_subresource != 0 || !upload.read_range_given || upload.read_range.Begin != 0 ||
        upload.read_range.End != 0 || !upload.written_range_given || upload.written_range.Begin != written_begin ||
        upload.written_range.End != offset + bytes) {
        std::printf("FAIL %s: hr=0x%08lx contents=%d maps=%d unmaps=%d read=%d[%zu,%zu) written=%d[%zu,%zu), "
                    "expected S_OK, one map with read [0,0) and written [%zu,%zu)\n",
                    name, static_cast<unsigned long>(hr), contents, upload.maps, upload.unmaps,
                    upload.read_range_given, upload.read_range.Begin, upload.read_range.End,
                    upload.written_range_given, upload.written_range.Begin, upload.written_range.End,
                    written_begin, offset + bytes);
        ++failures;
    }
}

void refuse(const char* name, HRESULT expected, Upload* upload, SIZE_T offset, const void* data, SIZE_T bytes,
            ID3D12Fence* fence, UINT64 value)
{
    const HRESULT hr = cpu_srv::refresh(upload, offset, data, bytes, fence, value);
    bool unchanged = true;
    if (upload)
        for (const std::byte b : upload->storage) unchanged = unchanged && b == sentinel;
    const int unmaps = upload ? upload->unmaps : 0;
    const int maps = upload ? upload->maps : 0;
    // A failed Map is the only refusal that may call Map; nothing may be unmapped.
    const int allowed_maps = upload && FAILED(upload->map_result) ? 1 : 0;
    if (hr != expected || !unchanged || maps != allowed_maps || unmaps != 0) {
        std::printf("FAIL %s: hr=0x%08lx unchanged=%d maps=%d unmaps=%d, expected hr=0x%08lx, %d maps, no unmap\n",
                    name, static_cast<unsigned long>(hr), unchanged, maps, unmaps,
                    static_cast<unsigned long>(expected), allowed_maps);
        ++failures;
    }
}
} // namespace

int main(int argc, char** argv)
{
    written_from_zero =
        argc == 3 && !std::strcmp(argv[1], "--mutate") && !std::strcmp(argv[2], "written-range");
    if (argc != 1 && !written_from_zero) {
        std::printf("usage: cpu_srv_upload_check [--mutate written-range]\n");
        return 64;
    }

    // Accepted refreshes of a 24-byte upload buffer.
    {
        Upload upload;
        accept("first use, exact fit at byte 16", upload, 16, 8, nullptr, 0);
    }
    {
        Upload upload;
        accept("whole buffer prefix from byte 0", upload, 0, 8, nullptr, 0);
    }
    {
        Upload upload;
        Fence fence(5);
        accept("fence completed at the value", upload, 3, 5, &fence, 5);
    }
    {
        Upload upload;
        Fence fence(9);
        accept("fence completed past the value", upload, 1, 8, &fence, 5);
    }
    {
        Upload upload;
        Fence fence(0);
        accept("fence with value zero", upload, 8, 8, &fence, 0);
    }

    // Argument refusals.
    Fence retired(5);
    refuse("null upload", E_INVALIDARG, nullptr, 0, source.data(), 8, nullptr, 0);
    {
        Upload upload;
        refuse("null source", E_INVALIDARG, &upload, 0, nullptr, 8, nullptr, 0);
    }
    {
        Upload upload;
        refuse("zero bytes", E_INVALIDARG, &upload, 0, source.data(), 0, nullptr, 0);
    }
    {
        Upload upload;
        refuse("no fence with a nonzero value", E_INVALIDARG, &upload, 0, source.data(), 8, nullptr, 1);
    }
    {
        Upload upload;
        upload.dimension = D3D12_RESOURCE_DIMENSION_TEXTURE1D;
        refuse("1D texture", E_INVALIDARG, &upload, 0, source.data(), 8, nullptr, 0);
    }
    {
        Upload upload;
        refuse("offset at the end", E_INVALIDARG, &upload, 24, source.data(), 1, nullptr, 0);
    }
    {
        Upload upload;
        refuse("offset past the end", E_INVALIDARG, &upload, 25, source.data(), 1, nullptr, 0);
    }
    {
        Upload upload;
        refuse("one byte past the end", E_INVALIDARG, &upload, 17, source.data(), 8, nullptr, 0);
    }
    {
        Upload upload;
        upload.heap = D3D12_HEAP_TYPE_DEFAULT;
        refuse("default heap", E_INVALIDARG, &upload, 0, source.data(), 8, nullptr, 0);
    }
    {
        Upload upload;
        upload.heap = D3D12_HEAP_TYPE_READBACK;
        refuse("readback heap", E_INVALIDARG, &upload, 0, source.data(), 8, nullptr, 0);
    }

    // Results of the resource and fence are returned as they are.
    {
        Upload upload;
        upload.heap_result = E_FAIL;
        refuse("GetHeapProperties fails", E_FAIL, &upload, 0, source.data(), 8, nullptr, 0);
    }
    {
        Upload upload;
        Fence fence(4);
        refuse("fence one short of the value", DXGI_ERROR_WAS_STILL_DRAWING, &upload, 0, source.data(), 8, &fence, 5);
    }
    {
        Upload upload;
        Fence removed(UINT64_MAX);
        refuse("removed device", DXGI_ERROR_DEVICE_REMOVED, &upload, 0, source.data(), 8, &removed, 5);
    }
    {
        Upload upload;
        upload.map_result = E_OUTOFMEMORY;
        refuse("Map fails", E_OUTOFMEMORY, &upload, 0, source.data(), 8, &retired, 5);
    }

    // A source whose last 4 bytes lie on an inaccessible page: ReadProcessMemory reports a partial
    // copy, and the upload is neither mapped nor changed.
    SYSTEM_INFO system{};
    GetSystemInfo(&system);
    auto* pages = static_cast<std::byte*>(
        VirtualAlloc(nullptr, 2 * system.dwPageSize, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE));
    DWORD old_protect{};
    if (!pages || !VirtualProtect(pages + system.dwPageSize, system.dwPageSize, PAGE_NOACCESS, &old_protect)) {
        std::printf("FAIL cannot build the straddling source\n");
        return 1;
    }
    std::memcpy(pages + system.dwPageSize - 4, source.data(), 4);
    {
        Upload upload;
        refuse("source straddles an inaccessible page", HRESULT_FROM_WIN32(ERROR_PARTIAL_COPY), &upload, 0,
               pages + system.dwPageSize - 4, 8, &retired, 5);
    }
    VirtualFree(pages, 0, MEM_RELEASE);

    if (failures) {
        std::printf("cpu_srv_upload: %d FAILED\n", failures);
        return 1;
    }
    std::printf("cpu_srv_upload: PASS (5 refreshed, 15 refused)\n");
    return 0;
}
