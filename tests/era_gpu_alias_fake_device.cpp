// The recording ID3D12Device of era_gpu_alias_fake_device.h. CINTERFACE selects the C declarations
// of d3d12.h, so the vtable is a struct of named, typed function pointers.
#define CINTERFACE
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "era_gpu_alias_fake_device.h"

#include <d3d12.h>

#include <cstddef>

namespace {
struct Fake {
    ID3D12Device device;  // first member: its lpVtbl is what callers see
    FakeDevice* state;
};

FakeDevice& state_of(ID3D12Device* self) noexcept
{
    return *reinterpret_cast<Fake*>(self)->state;
}

// Stands in for every slot that era_gpu_alias::open has no reason to call before it has an
// ID3D12Device3. Under the x64 calling convention the extra arguments of those methods are ignored.
HRESULT STDMETHODCALLTYPE unexpected(ID3D12Device* self) noexcept
{
    ++state_of(self).unexpected_calls;
    return E_FAIL;
}

HRESULT STDMETHODCALLTYPE query_interface(ID3D12Device* self, REFIID riid, void** object) noexcept
{
    auto& state = state_of(self);
    ++state.queries;
    state.queried = riid;
    if (object) *object = nullptr;
    return E_NOINTERFACE;
}

HRESULT STDMETHODCALLTYPE check_feature_support(ID3D12Device* self, D3D12_FEATURE feature, void* data,
                                                UINT bytes) noexcept
{
    auto& state = state_of(self);
    ++state.feature_calls;
    state.feature = static_cast<int>(feature);
    state.feature_bytes = bytes;
    if (SUCCEEDED(state.feature_result) && feature == D3D12_FEATURE_EXISTING_HEAPS && data &&
        bytes == sizeof(D3D12_FEATURE_DATA_EXISTING_HEAPS)) {
        static_cast<D3D12_FEATURE_DATA_EXISTING_HEAPS*>(data)->Supported = state.existing_heaps;
    }
    return state.feature_result;
}

ID3D12DeviceVtbl make_vtable() noexcept
{
    ID3D12DeviceVtbl table;
    static_assert(sizeof(table) % sizeof(void*) == 0, "the vtable is a list of function pointers");
    auto* slots = reinterpret_cast<HRESULT(STDMETHODCALLTYPE**)(ID3D12Device*)>(&table);
    for (std::size_t i = 0; i < sizeof(table) / sizeof(void*); ++i) slots[i] = unexpected;
    table.QueryInterface = query_interface;
    table.CheckFeatureSupport = check_feature_support;
    return table;
}

ID3D12DeviceVtbl vtable = make_vtable();  // lpVtbl is not a const pointer without CONST_VTABLE
Fake fake{{&vtable}, nullptr};
}

void* fake_device(FakeDevice& state) noexcept
{
    fake.state = &state;
    return &fake.device;
}
