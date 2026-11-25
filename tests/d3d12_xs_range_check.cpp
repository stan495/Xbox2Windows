// Checks src/d3d12_xs_range.h without a device: the window sizes create() asks for, the GPU address of
// spans at window edges and at the end of the range, and window references. A fake device reports a
// chosen per-resource GPU virtual address limit, and a fake CreateWindowFn hands out counted windows
// with distinct GPU addresses, so every expected value below is worked out by hand.
// --mutate full_last_window: expect the last window at the full window size (a negative control).
#include "d3d12_xs_range.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <vector>

namespace {
int failures;

void expect(bool ok, const char* what)
{
    if (!ok) {
        std::printf("FAIL %s\n", what);
        ++failures;
    }
}

constexpr UINT64 KiB = 1024, GiB = KiB * KiB * KiB;
constexpr UINT64 tile = 64 * KiB;
constexpr UINT64 window32 = 4 * GiB - tile;  // the largest whole number of tiles below 2^32 bytes

// ---- A device that answers only CheckFeatureSupport ----
// create() calls no other device method. The vtable follows the COM order of d3d12.h: IUnknown (0-2),
// ID3D12Object (3-6), ID3D12Device::GetNodeCount (7) to CreateCommandList (12), CheckFeatureSupport (13),
// and 47 slots in all. Every other slot counts an unexpected call.
struct FakeDevice {
    const void* const* vtable;
    UINT bits;
    HRESULT feature_hr;
};
unsigned unexpected_device_calls;

HRESULT STDMETHODCALLTYPE unexpected_device_call(FakeDevice*)
{
    ++unexpected_device_calls;
    return E_NOTIMPL;
}

HRESULT STDMETHODCALLTYPE check_feature_support(FakeDevice* device, D3D12_FEATURE feature, void* data, UINT size)
{
    if (feature != D3D12_FEATURE_GPU_VIRTUAL_ADDRESS_SUPPORT ||
        size != sizeof(D3D12_FEATURE_DATA_GPU_VIRTUAL_ADDRESS_SUPPORT) || !data) {
        ++unexpected_device_calls;
        return E_INVALIDARG;
    }
    if (FAILED(device->feature_hr)) return device->feature_hr;
    auto* va = static_cast<D3D12_FEATURE_DATA_GPU_VIRTUAL_ADDRESS_SUPPORT*>(data);
    va->MaxGPUVirtualAddressBitsPerResource = device->bits;
    va->MaxGPUVirtualAddressBitsPerProcess = 48;
    return S_OK;
}

struct DeviceVtable {
    const void* slots[47];
    DeviceVtable()
    {
        for (auto& slot : slots) slot = reinterpret_cast<const void*>(&unexpected_device_call);
        slots[13] = reinterpret_cast<const void*>(&check_feature_support);
    }
} const device_vtable;

// ---- Counted windows with distinct GPU addresses ----
// Window i starts at (i + 1) << 44, so an address taken from the wrong window or past a window's own
// size cannot equal the expected one.
constexpr D3D12_GPU_VIRTUAL_ADDRESS base(UINT64 i) { return (i + 1) << 44; }

unsigned live_windows, over_released;

class FakeWindow final : public ID3D12Resource {
public:
    FakeWindow(const D3D12_RESOURCE_DESC& desc, D3D12_GPU_VIRTUAL_ADDRESS va) : desc_(desc), va_(va) { ++live_windows; }
    ULONG refs() const { return refs_; }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, void** out) override
    {
        if (out) *out = nullptr;
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override
    {
        if (!refs_) {
            ++over_released;
            return 0;
        }
        if (!--refs_) --live_windows;
        return refs_;
    }
    HRESULT STDMETHODCALLTYPE GetPrivateData(REFGUID, UINT*, void*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE SetPrivateData(REFGUID, UINT, const void*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE SetPrivateDataInterface(REFGUID, const IUnknown*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE SetName(LPCWSTR) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE GetDevice(REFIID, void** out) override
    {
        if (out) *out = nullptr;
        return E_NOTIMPL;
    }
    HRESULT STDMETHODCALLTYPE Map(UINT, const D3D12_RANGE*, void**) override { return E_NOTIMPL; }
    void STDMETHODCALLTYPE Unmap(UINT, const D3D12_RANGE*) override {}
    D3D12_RESOURCE_DESC STDMETHODCALLTYPE GetDesc() override { return desc_; }
    D3D12_GPU_VIRTUAL_ADDRESS STDMETHODCALLTYPE GetGPUVirtualAddress() override { return va_; }
    HRESULT STDMETHODCALLTYPE WriteToSubresource(UINT, const D3D12_BOX*, const void*, UINT, UINT) override
    {
        return E_NOTIMPL;
    }
    HRESULT STDMETHODCALLTYPE ReadFromSubresource(void*, UINT, UINT, UINT, const D3D12_BOX*) override
    {
        return E_NOTIMPL;
    }
    HRESULT STDMETHODCALLTYPE GetHeapProperties(D3D12_HEAP_PROPERTIES*, D3D12_HEAP_FLAGS*) override
    {
        return E_NOTIMPL;
    }

private:
    D3D12_RESOURCE_DESC desc_;
    D3D12_GPU_VIRTUAL_ADDRESS va_;
    ULONG refs_ = 1;
};

// The windows outlive the range so their reference counts stay readable after it releases them.
std::vector<std::unique_ptr<FakeWindow>> windows;
std::vector<D3D12_RESOURCE_DESC> requests;
FakeDevice* expected_device;
unsigned fail_at;  // 1-based call that fails; 0 never

HRESULT create_window(ID3D12Device* device, const D3D12_RESOURCE_DESC& desc, ID3D12Resource** window)
{
    // No case needs more than 3 windows; a count that is not refused stops here instead of exhausting memory.
    if (requests.size() >= 16) return E_OUTOFMEMORY;
    requests.push_back(desc);
    if (reinterpret_cast<FakeDevice*>(device) != expected_device || !window) return E_INVALIDARG;
    if (requests.size() == fail_at) return E_OUTOFMEMORY;
    windows.push_back(std::make_unique<FakeWindow>(desc, base(windows.size())));
    *window = windows.back().get();
    return S_OK;
}

struct Fixture {
    FakeDevice device{device_vtable.slots, 32, S_OK};
    explicit Fixture(UINT bits, unsigned failing_call = 0, HRESULT feature_hr = S_OK)
    {
        device.bits = bits;
        device.feature_hr = feature_hr;
        expected_device = &device;
        fail_at = failing_call;
        windows.clear();
        requests.clear();
        live_windows = over_released = 0;
    }
    ID3D12Device* get() { return reinterpret_cast<ID3D12Device*>(&device); }
};

bool buffer_request(const D3D12_RESOURCE_DESC& desc, UINT64 width)
{
    return desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER && desc.Width == width && desc.Height == 1 &&
           desc.DepthOrArraySize == 1 && desc.MipLevels == 1 && desc.Format == DXGI_FORMAT_UNKNOWN &&
           desc.SampleDesc.Count == 1 && desc.Layout == D3D12_TEXTURE_LAYOUT_ROW_MAJOR &&
           (desc.Flags & D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
}

void va(IX2WGpuRange* range, UINT64 offset, UINT64 bytes, D3D12_GPU_VIRTUAL_ADDRESS expected)
{
    const D3D12_GPU_VIRTUAL_ADDRESS got = range->GetGPUVirtualAddress(offset, bytes);
    if (got != expected) {
        std::printf("FAIL GetGPUVirtualAddress(0x%llx, 0x%llx) = 0x%llx, expected 0x%llx\n",
                    static_cast<unsigned long long>(offset), static_cast<unsigned long long>(bytes),
                    static_cast<unsigned long long>(got), static_cast<unsigned long long>(expected));
        ++failures;
    }
}

// 32 address bits per resource and 2 windows + 3 tiles: windows of 4 GiB - 64 KiB, 4 GiB - 64 KiB and
// 192 KiB at 1 << 44, 2 << 44 and 3 << 44.
void three_windows(bool full_last_window)
{
    Fixture fixture(32);
    constexpr UINT64 size = 2 * window32 + 3 * tile;  // 0x2'0001'0000
    IX2WGpuRange* range = nullptr;
    const HRESULT hr = d3d12_xs_range::create(fixture.get(), size, &range, create_window);
    expect(hr == S_OK && range, "a range of two windows and three tiles is created");
    if (!range) return;
    const UINT64 last = full_last_window ? window32 : 3 * tile;
    expect(requests.size() == 3 && buffer_request(requests[0], window32) && buffer_request(requests[1], window32) &&
               buffer_request(requests[2], last),
           "three buffer windows of 4 GiB - 64 KiB, 4 GiB - 64 KiB and 192 KiB are requested");
    // QueryInterface answers for the two interfaces the range implements and refuses everything else,
    // clearing the out pointer first so a failed call cannot be mistaken for a result.
    {
        void* queried = reinterpret_cast<void*>(std::uintptr_t{1});
        expect(range->QueryInterface(__uuidof(IX2WGpuRange), &queried) == S_OK && queried == range,
               "QueryInterface(IX2WGpuRange) returns the range");
        queried = reinterpret_cast<void*>(std::uintptr_t{1});
        expect(range->QueryInterface(__uuidof(IUnknown), &queried) == S_OK && queried == range,
               "QueryInterface(IUnknown) returns the range");
        // Each successful query takes a reference the way COM does; give both back so the release
        // accounting below still sees the count the rest of the fixture expects.
        range->Release();
        range->Release();
        queried = reinterpret_cast<void*>(std::uintptr_t{1});
        expect(range->QueryInterface(__uuidof(ID3D12Device), &queried) == E_NOINTERFACE && !queried,
               "QueryInterface(other) is refused and clears the output");
        expect(range->QueryInterface(__uuidof(IX2WGpuRange), nullptr) == E_POINTER,
               "QueryInterface with a null output is refused");
    }    expect(range->GetSize() == size && range->GetWindowSize() == window32 && range->GetWindowCount() == 3,
           "the range reports 0x2'0001'0000 bytes in 3 windows of 0xffff'0000 bytes");

    va(range, 0, 1, base(0));
    va(range, 0, 0, base(0));                                  // 0 bytes counts as 1
    va(range, window32 - 4, 4, base(0) + window32 - 4);        // ends at the first window's edge
    va(range, window32 - 1, 0, base(0) + window32 - 1);
    va(range, window32 - 4, 5, 0);                             // crosses it by one byte
    va(range, window32 - 1, 2, 0);
    va(range, window32, 4, base(1));
    va(range, 2 * window32 - 1, 1, base(1) + window32 - 1);
    va(range, 2 * window32, 3 * tile, base(2));                // the whole last window
    va(range, 2 * window32, 3 * tile + 1, 0);                  // one byte past the range
    va(range, size - 1, 0, base(2) + 3 * tile - 1);
    va(range, size - 1, 2, 0);
    va(range, size, 0, 0);
    va(range, size, 1, 0);
    va(range, 0, size, 0);
    va(range, ~UINT64{0}, 1, 0);
    va(range, 1, ~UINT64{0}, 0);

    ID3D12Resource* window = reinterpret_cast<ID3D12Resource*>(std::uintptr_t{1});
    expect(range->GetWindow(3, &window) == E_INVALIDARG && !window, "window 3 of 3 is refused");
    expect(range->GetWindow(0, nullptr) == E_INVALIDARG, "a null output is refused");
    expect(range->GetWindow(2, &window) == S_OK && window == windows[2].get() && windows[2]->refs() == 2,
           "the last window is returned with a reference added");
    expect(windows[0]->refs() == 1 && windows[1]->refs() == 1 && live_windows == 3,
           "the other windows hold only the range's reference");

    range->Close();
    expect(live_windows == 1 && windows[2]->refs() == 1, "Close releases the range's references");
    expect(range->GetSize() == 0 && range->GetWindowCount() == 0 && range->GetGPUVirtualAddress(0, 1) == 0,
           "the closed range is empty");
    ID3D12Resource* none = nullptr;
    expect(range->GetWindow(0, &none) == E_INVALIDARG && !none, "a closed range has no window 0");
    range->Close();
    expect(live_windows == 1 && windows[2]->refs() == 1, "a second Close releases nothing");
    expect(window->GetGPUVirtualAddress() == base(2) && window->Release() == 0 && live_windows == 0,
           "the caller's window reference stays valid after Close");
    expect(range->Release() == 0 && over_released == 0, "no window is released twice");
}

// Window size from the adapter's per-resource limit: 4 GiB at 40 bits, one tile at 17 bits.
void adapter_limits()
{
    {
        Fixture fixture(40);
        IX2WGpuRange* range = nullptr;
        expect(d3d12_xs_range::create(fixture.get(), 8 * GiB + tile, &range, create_window) == S_OK && range,
               "40 bits: 8 GiB + 64 KiB is created");
        expect(requests.size() == 3 && buffer_request(requests[0], 4 * GiB) &&
                   buffer_request(requests[1], 4 * GiB) && buffer_request(requests[2], tile),
               "40 bits: windows of 4 GiB, 4 GiB and 64 KiB");
        if (range) {
            expect(range->GetWindowSize() == 4 * GiB, "40 bits: the window size is 4 GiB");
            va(range, 4 * GiB, 4, base(1));
            va(range, 8 * GiB, tile, base(2));
            range->Release();
        }
        expect(live_windows == 0 && over_released == 0, "40 bits: the last release frees every window");
    }
    {
        // One bit below the floor the range refuses instead of creating a window it cannot address.
        {
            Fixture below(16);
            IX2WGpuRange* refused_range = reinterpret_cast<IX2WGpuRange*>(std::uintptr_t{1});
            expect(d3d12_xs_range::create(below.get(), 3 * tile, &refused_range, create_window) == E_FAIL &&
                       !refused_range && requests.empty() && live_windows == 0,
                   "16 bits: the range is refused before a window is created");
        }
        Fixture fixture(17);
        IX2WGpuRange* range = nullptr;
        expect(d3d12_xs_range::create(fixture.get(), 3 * tile, &range, create_window) == S_OK && range,
               "17 bits: 192 KiB is created");
        expect(requests.size() == 3 && buffer_request(requests[0], tile) && buffer_request(requests[1], tile) &&
                   buffer_request(requests[2], tile),
               "17 bits: three windows of 64 KiB");
        if (range) {
            va(range, tile - 1, 2, 0);
            va(range, 2 * tile, tile, base(2));
            range->Release();
        }
        expect(live_windows == 0, "17 bits: the last release frees every window");
    }
}

void refused(const char* what, ID3D12Device* device, UINT64 size, HRESULT expected, bool null_out = false)
{
    IX2WGpuRange* range = reinterpret_cast<IX2WGpuRange*>(std::uintptr_t{1});
    const HRESULT hr = d3d12_xs_range::create(device, size, null_out ? nullptr : &range, create_window);
    if (hr != expected || (!null_out && range) || !requests.empty() || live_windows) {
        std::printf("FAIL %s: hr=0x%08lx, %zu window requests\n", what, static_cast<unsigned long>(hr),
                    requests.size());
        ++failures;
    }
}

// Refusals before any window is requested, and failures part way through.
void failures_and_refusals()
{
    {
        Fixture fixture(32);
        refused("a null output", fixture.get(), tile, E_POINTER, true);
        refused("a null device", nullptr, tile, E_INVALIDARG);
        refused("size 0", fixture.get(), 0, E_INVALIDARG);
        refused("size 64 KiB + 1", fixture.get(), tile + 1, E_INVALIDARG);
        refused("size 64 KiB - 4 KiB", fixture.get(), tile - 4 * KiB, E_INVALIDARG);
    }
    {
        // The largest multiple of 64 KiB, 2^64 - 64 KiB, takes 2^32 + 2^16 + 1 windows of 4 GiB - 64 KiB
        // (and 2^32 windows of 4 GiB), more than UINT holds.
        Fixture fixture(32);
        refused("2^32 + 2^16 + 1 windows", fixture.get(), ~UINT64{0} - (tile - 1), E_INVALIDARG);
    }
    {
        Fixture fixture(32, 0, DXGI_ERROR_UNSUPPORTED);
        refused("a failing feature query", fixture.get(), tile, DXGI_ERROR_UNSUPPORTED);
    }
    for (const unsigned call : {1u, 2u, 3u}) {
        Fixture fixture(32, call);
        IX2WGpuRange* range = reinterpret_cast<IX2WGpuRange*>(std::uintptr_t{1});
        const HRESULT hr = d3d12_xs_range::create(fixture.get(), 2 * window32 + 3 * tile, &range, create_window);
        if (hr != E_OUTOFMEMORY || range || requests.size() != call || windows.size() != call - 1 || live_windows ||
            over_released) {
            std::printf("FAIL window %u of 3 fails: hr=0x%08lx after %zu requests, %u windows left\n", call,
                        static_cast<unsigned long>(hr), requests.size(), live_windows);
            ++failures;
        }
    }
}
}  // namespace

int main(int argc, char** argv)
{
    const bool full_last_window = argc > 2 && std::strcmp(argv[1], "--mutate") == 0 &&
                                  std::strcmp(argv[2], "full_last_window") == 0;
    if (argc > 1 && !full_last_window) {
        std::fputs("usage: d3d12_xs_range_check [--mutate full_last_window]\n", stderr);
        return 2;
    }
    three_windows(full_last_window);
    adapter_limits();
    failures_and_refusals();
    expect(unexpected_device_calls == 0, "create calls no device method but CheckFeatureSupport");
    if (failures) return 1;
    std::printf("d3d12_xs_range: window sizes, edge spans and references exact\n");
    return 0;
}
