#pragma once

#include <windows.h>

// A recording stand-in for ID3D12Device, for era_gpu_alias_check (no GPU, no D3D12 runtime). It is
// defined in era_gpu_alias_fake_device.cpp, which is compiled with CINTERFACE so that each method
// it answers is assigned to its named slot of the C vtable. Every other method is counted as
// unexpected and returns E_FAIL.
struct FakeDevice {
    // Answer to CheckFeatureSupport(D3D12_FEATURE_EXISTING_HEAPS), set by the test.
    HRESULT feature_result = E_FAIL;
    BOOL existing_heaps = FALSE;
    // What the code under test called. QueryInterface always fails with E_NOINTERFACE.
    int feature_calls = 0;
    int feature = -1;
    UINT feature_bytes = 0;
    int queries = 0;
    GUID queried{};
    int unexpected_calls = 0;

    int calls() const { return feature_calls + queries + unexpected_calls; }
};

// Returns the ID3D12Device* (as void*) of the single fake, now recording into state.
void* fake_device(FakeDevice& state) noexcept;
