// Calls the desktop d3d12_xs.dll through named imports, as the package DLLs do.
#include <d3d12.h>
#include <dxgi1_6.h>
#include <wrl/client.h>

#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string_view>

#include "pso_compute_embedded.h"
#include "pso_compute_stripped.h"

extern "C" __declspec(dllimport) HRESULT WINAPI D3D12ConfigureVirtualMemoryX(const void* configuration);

using Microsoft::WRL::ComPtr;
using namespace std::literals;

namespace {
constexpr std::uint32_t observed_console_flags = 0x03000000u;

struct DesktopD3D12 {
    HMODULE module = nullptr;
    PFN_D3D12_SERIALIZE_VERSIONED_ROOT_SIGNATURE serialize = nullptr;
    PFN_D3D12_CREATE_DEVICE create_device = nullptr;

    bool load()
    {
        module = LoadLibraryExW(L"d3d12.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
        if (!module) return false;
        serialize = reinterpret_cast<PFN_D3D12_SERIALIZE_VERSIONED_ROOT_SIGNATURE>(
            GetProcAddress(module, "D3D12SerializeVersionedRootSignature"));
        create_device = reinterpret_cast<PFN_D3D12_CREATE_DEVICE>(
            GetProcAddress(module, "D3D12CreateDevice"));
        return serialize && create_device;
    }

    ~DesktopD3D12()
    {
        if (module) FreeLibrary(module);
    }
};

bool same_blob(ID3DBlob* a, ID3DBlob* b)
{
    return a && b &&
           a->GetBufferSize() == b->GetBufferSize() &&
           std::memcmp(a->GetBufferPointer(), b->GetBufferPointer(), a->GetBufferSize()) == 0;
}

D3D12_ROOT_PARAMETER1 make_constants_param1()
{
    D3D12_ROOT_PARAMETER1 param{};
    param.ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    param.Constants.Num32BitValues = 4;
    return param;
}

bool export_checks()
{
    HMODULE shim = GetModuleHandleW(L"d3d12_xs.dll");
    if (!shim) return false;
    DesktopD3D12 desktop;
    if (!desktop.load()) return false;

    unsigned found = 0;
    for (const char* name : {"D3D12SerializeVersionedRootSignature", "D3D12CreateRootSignatureDeserializer",
                             "D3D12ConfigureVirtualMemoryX", "D3D12XboxCreateDevice"}) {
        FARPROC proc = GetProcAddress(shim, name);
        if (!proc) return false;
        HMODULE owner = nullptr;
        if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                                reinterpret_cast<LPCWSTR>(proc), &owner) || owner != shim) {
            return false;
        }
        ++found;
    }

    // Prove this executable's named import resolves to d3d12_xs.dll rather than desktop d3d12.dll:
    // desktop d3d12.dll rejects 0x03000000 while d3d12_xs.dll strips it and succeeds.
    D3D12_ROOT_PARAMETER1 param = make_constants_param1();
    D3D12_VERSIONED_ROOT_SIGNATURE_DESC desc{};
    desc.Version = D3D_ROOT_SIGNATURE_VERSION_1_1;
    desc.Desc_1_1.NumParameters = 1;
    desc.Desc_1_1.pParameters = &param;
    desc.Desc_1_1.Flags = static_cast<D3D12_ROOT_SIGNATURE_FLAGS>(observed_console_flags);

    ComPtr<ID3DBlob> desktop_blob, shim_blob;
    const HRESULT desktop_hr = desktop.serialize(&desc, &desktop_blob, nullptr);
    const HRESULT shim_hr = D3D12SerializeVersionedRootSignature(&desc, &shim_blob, nullptr);
    std::printf("exports=%u desktop_tagged_hr=0x%08lx shim_tagged_hr=0x%08lx\n",
                found, static_cast<unsigned long>(desktop_hr), static_cast<unsigned long>(shim_hr));
    return desktop_hr == E_INVALIDARG && !desktop_blob && SUCCEEDED(shim_hr) && shim_blob;
}

bool serialize_checks()
{
    DesktopD3D12 desktop;
    if (!desktop.load()) return false;

    constexpr auto desktop_flag = D3D12_ROOT_SIGNATURE_FLAG_DENY_VERTEX_SHADER_ROOT_ACCESS;
    D3D12_ROOT_PARAMETER1 param1 = make_constants_param1();
    D3D12_VERSIONED_ROOT_SIGNATURE_DESC base_v11{};
    base_v11.Version = D3D_ROOT_SIGNATURE_VERSION_1_1;
    base_v11.Desc_1_1.NumParameters = 1;
    base_v11.Desc_1_1.pParameters = &param1;
    base_v11.Desc_1_1.Flags = desktop_flag;

    ComPtr<ID3DBlob> desktop_v11, shim_v11;
    const HRESULT base_desktop_hr = desktop.serialize(&base_v11, &desktop_v11, nullptr);
    const HRESULT base_shim_hr = D3D12SerializeVersionedRootSignature(&base_v11, &shim_v11, nullptr);
    if (FAILED(base_desktop_hr) || FAILED(base_shim_hr) || !same_blob(desktop_v11.Get(), shim_v11.Get())) {
        std::fprintf(stderr, "v1.1 baseline: desktop=0x%08lx shim=0x%08lx\n",
                     static_cast<unsigned long>(base_desktop_hr), static_cast<unsigned long>(base_shim_hr));
        return false;
    }

    // Each observed console bit alone and as a pair must be rejected by desktop d3d12.dll and
    // serialized identically to the desktop baseline by d3d12_xs.dll without mutating caller input.
    for (const std::uint32_t bits : {0x01000000u, 0x02000000u, observed_console_flags}) {
        D3D12_VERSIONED_ROOT_SIGNATURE_DESC tagged = base_v11;
        const auto expected_flags = static_cast<D3D12_ROOT_SIGNATURE_FLAGS>(
            static_cast<std::uint32_t>(desktop_flag) | bits);
        tagged.Desc_1_1.Flags = expected_flags;

        ComPtr<ID3DBlob> rejected, sanitized;
        const HRESULT desktop_hr = desktop.serialize(&tagged, &rejected, nullptr);
        const HRESULT shim_hr = D3D12SerializeVersionedRootSignature(&tagged, &sanitized, nullptr);
        if (desktop_hr != E_INVALIDARG || rejected || FAILED(shim_hr) ||
            !same_blob(sanitized.Get(), desktop_v11.Get()) ||
            tagged.Desc_1_1.Flags != expected_flags) {
            std::fprintf(stderr, "v1.1 tagged 0x%08x: desktop=0x%08lx shim=0x%08lx\n",
                         bits, static_cast<unsigned long>(desktop_hr), static_cast<unsigned long>(shim_hr));
            return false;
        }
    }

    // v1.0 descriptor with the same console bits at +0x28.
    D3D12_ROOT_PARAMETER param0{};
    param0.ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    param0.Constants.Num32BitValues = 4;
    D3D12_VERSIONED_ROOT_SIGNATURE_DESC base_v10{};
    base_v10.Version = D3D_ROOT_SIGNATURE_VERSION_1_0;
    base_v10.Desc_1_0.NumParameters = 1;
    base_v10.Desc_1_0.pParameters = &param0;
    base_v10.Desc_1_0.Flags = desktop_flag;
    ComPtr<ID3DBlob> desktop_v10, rejected_v10, shim_v10;
    D3D12_VERSIONED_ROOT_SIGNATURE_DESC tagged_v10 = base_v10;
    tagged_v10.Desc_1_0.Flags = static_cast<D3D12_ROOT_SIGNATURE_FLAGS>(
        static_cast<std::uint32_t>(desktop_flag) | observed_console_flags);
    const HRESULT v10_base_hr = desktop.serialize(&base_v10, &desktop_v10, nullptr);
    const HRESULT v10_desktop_hr = desktop.serialize(&tagged_v10, &rejected_v10, nullptr);
    const HRESULT v10_shim_hr = D3D12SerializeVersionedRootSignature(&tagged_v10, &shim_v10, nullptr);
    if (FAILED(v10_base_hr) || v10_desktop_hr != E_INVALIDARG || rejected_v10 ||
        FAILED(v10_shim_hr) || !same_blob(shim_v10.Get(), desktop_v10.Get())) {
        std::fprintf(stderr, "v1.0 tagged: base=0x%08lx desktop=0x%08lx shim=0x%08lx\n",
                     static_cast<unsigned long>(v10_base_hr),
                     static_cast<unsigned long>(v10_desktop_hr),
                     static_cast<unsigned long>(v10_shim_hr));
        return false;
    }

    // Negative controls: unobserved invalid flag bits must still return E_INVALIDARG, and null
    // descriptor or unknown version must fail with the exact same HRESULT as desktop d3d12.dll.
    for (const std::uint32_t bad_bits : {0x04000000u, 0x07000000u, 0x08000000u}) {
        D3D12_VERSIONED_ROOT_SIGNATURE_DESC bad = base_v11;
        bad.Desc_1_1.Flags = static_cast<D3D12_ROOT_SIGNATURE_FLAGS>(bad_bits);
        ComPtr<ID3DBlob> desktop_bad, shim_bad;
        const HRESULT desktop_hr = desktop.serialize(&bad, &desktop_bad, nullptr);
        const HRESULT shim_hr = D3D12SerializeVersionedRootSignature(&bad, &shim_bad, nullptr);
        if (desktop_hr != E_INVALIDARG || desktop_bad || shim_hr != E_INVALIDARG || shim_bad) {
            std::fprintf(stderr, "bad_bits 0x%08x: desktop=0x%08lx shim=0x%08lx\n",
                         bad_bits, static_cast<unsigned long>(desktop_hr), static_cast<unsigned long>(shim_hr));
            return false;
        }
    }
    ComPtr<ID3DBlob> desktop_null, shim_null;
    const HRESULT desktop_null_hr = desktop.serialize(nullptr, &desktop_null, nullptr);
    const HRESULT shim_null_hr = D3D12SerializeVersionedRootSignature(nullptr, &shim_null, nullptr);
    if (!FAILED(desktop_null_hr) || desktop_null || shim_null_hr != desktop_null_hr || shim_null) {
        std::fprintf(stderr, "null desc: desktop=0x%08lx shim=0x%08lx\n",
                     static_cast<unsigned long>(desktop_null_hr), static_cast<unsigned long>(shim_null_hr));
        return false;
    }
    D3D12_VERSIONED_ROOT_SIGNATURE_DESC bad_ver{};
    bad_ver.Version = static_cast<D3D_ROOT_SIGNATURE_VERSION>(99);
    ComPtr<ID3DBlob> desktop_ver, shim_ver;
    const HRESULT desktop_ver_hr = desktop.serialize(&bad_ver, &desktop_ver, nullptr);
    const HRESULT shim_ver_hr = D3D12SerializeVersionedRootSignature(&bad_ver, &shim_ver, nullptr);
    if (!FAILED(desktop_ver_hr) || desktop_ver || shim_ver_hr != desktop_ver_hr || shim_ver) {
        std::fprintf(stderr, "bad version: desktop=0x%08lx shim=0x%08lx\n",
                     static_cast<unsigned long>(desktop_ver_hr), static_cast<unsigned long>(shim_ver_hr));
        return false;
    }
    std::printf("v11_cases=4 v10_cases=1 negative_cases=5 null_hr=0x%08lx bad_ver_hr=0x%08lx\n",
                static_cast<unsigned long>(shim_null_hr), static_cast<unsigned long>(shim_ver_hr));
    return true;
}

bool deserialize_checks()
{
    constexpr auto desktop_flag = D3D12_ROOT_SIGNATURE_FLAG_DENY_VERTEX_SHADER_ROOT_ACCESS;
    D3D12_ROOT_PARAMETER1 param = make_constants_param1();
    D3D12_VERSIONED_ROOT_SIGNATURE_DESC input{};
    input.Version = D3D_ROOT_SIGNATURE_VERSION_1_1;
    input.Desc_1_1.NumParameters = 1;
    input.Desc_1_1.pParameters = &param;
    input.Desc_1_1.Flags = static_cast<D3D12_ROOT_SIGNATURE_FLAGS>(
        static_cast<std::uint32_t>(desktop_flag) | observed_console_flags);

    ComPtr<ID3DBlob> serialized;
    if (FAILED(D3D12SerializeVersionedRootSignature(&input, &serialized, nullptr)) || !serialized) {
        return false;
    }

    ComPtr<ID3D12RootSignatureDeserializer> reader;
    if (FAILED(D3D12CreateRootSignatureDeserializer(
            serialized->GetBufferPointer(), serialized->GetBufferSize(), IID_PPV_ARGS(&reader))) || !reader) {
        return false;
    }
    const auto* roundtrip = reader->GetRootSignatureDesc();
    if (!roundtrip || roundtrip->Flags != desktop_flag || roundtrip->NumParameters != 1 ||
        roundtrip->pParameters[0].ParameterType != D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS ||
        roundtrip->pParameters[0].Constants.Num32BitValues != 4) {
        return false;
    }

    // Embedded DXBC container path (matching amd_fidelityfx_upscaler_xbox.dll at 0x4f88).
    ComPtr<ID3D12RootSignatureDeserializer> embedded_reader;
    if (FAILED(D3D12CreateRootSignatureDeserializer(
            pso_compute_embedded, sizeof(pso_compute_embedded), IID_PPV_ARGS(&embedded_reader))) || !embedded_reader) {
        return false;
    }
    const auto* embedded_desc = embedded_reader->GetRootSignatureDesc();
    if (!embedded_desc || embedded_desc->NumParameters != 3 || embedded_desc->NumStaticSamplers != 0) {
        return false;
    }

    // Negative controls: container with stripped RTS0 and empty/null input must fail.
    ComPtr<ID3D12RootSignatureDeserializer> stripped_reader, null_reader;
    const HRESULT stripped_hr = D3D12CreateRootSignatureDeserializer(
        pso_compute_stripped, sizeof(pso_compute_stripped), IID_PPV_ARGS(&stripped_reader));
    const HRESULT null_hr = D3D12CreateRootSignatureDeserializer(
        nullptr, 0, IID_PPV_ARGS(&null_reader));
    std::printf("embedded_params=%u stripped_hr=0x%08lx null_hr=0x%08lx\n",
                embedded_desc->NumParameters, static_cast<unsigned long>(stripped_hr),
                static_cast<unsigned long>(null_hr));
    return stripped_hr == E_INVALIDARG && !stripped_reader && null_hr == E_INVALIDARG && !null_reader;
}

bool warp_root_checks()
{
    DesktopD3D12 desktop;
    if (!desktop.load()) return false;

    // Layout matching tests/compute.hlsl (pso_compute_stripped): SRV(t0), UAV(u0), RootConstants(16, b0),
    // tagged with the observed Xbox root-signature flags 0x03000000.
    std::array<D3D12_ROOT_PARAMETER1, 3> params{};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_SRV;
    params[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    params[0].Descriptor = {0, 0, D3D12_ROOT_DESCRIPTOR_FLAG_NONE};
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
    params[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    params[1].Descriptor = {0, 0, D3D12_ROOT_DESCRIPTOR_FLAG_NONE};
    params[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[2].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    params[2].Constants = {0, 0, 16};

    D3D12_VERSIONED_ROOT_SIGNATURE_DESC desc{};
    desc.Version = D3D_ROOT_SIGNATURE_VERSION_1_1;
    desc.Desc_1_1.NumParameters = static_cast<UINT>(params.size());
    desc.Desc_1_1.pParameters = params.data();
    desc.Desc_1_1.Flags = static_cast<D3D12_ROOT_SIGNATURE_FLAGS>(observed_console_flags);

    ComPtr<ID3DBlob> rejected, serialized;
    if (desktop.serialize(&desc, &rejected, nullptr) != E_INVALIDARG || rejected) {
        return false;
    }
    if (FAILED(D3D12SerializeVersionedRootSignature(&desc, &serialized, nullptr)) || !serialized) {
        return false;
    }

    ComPtr<IDXGIFactory4> factory;
    ComPtr<IDXGIAdapter> warp;
    ComPtr<ID3D12Device> device;
    if (FAILED(CreateDXGIFactory2(0, IID_PPV_ARGS(&factory))) ||
        FAILED(factory->EnumWarpAdapter(IID_PPV_ARGS(&warp))) ||
        FAILED(desktop.create_device(warp.Get(), D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device)))) {
        return false;
    }

    ComPtr<ID3D12RootSignature> root;
    const HRESULT root_hr = device->CreateRootSignature(
        0, serialized->GetBufferPointer(), serialized->GetBufferSize(), IID_PPV_ARGS(&root));
    if (FAILED(root_hr) || !root) {
        std::fprintf(stderr, "CreateRootSignature failed: 0x%08lx\n", static_cast<unsigned long>(root_hr));
        return false;
    }

    D3D12_COMPUTE_PIPELINE_STATE_DESC pso_desc{};
    pso_desc.pRootSignature = root.Get();
    pso_desc.CS = {pso_compute_stripped, sizeof(pso_compute_stripped)};
    ComPtr<ID3D12PipelineState> pipeline;
    const HRESULT pso_hr = device->CreateComputePipelineState(&pso_desc, IID_PPV_ARGS(&pipeline));
    std::printf("warp_root_hr=0x%08lx warp_pso_hr=0x%08lx\n",
                static_cast<unsigned long>(root_hr), static_cast<unsigned long>(pso_hr));
    return SUCCEEDED(pso_hr) && pipeline;
}

bool virtual_memory_checks()
{
    if (D3D12ConfigureVirtualMemoryX(nullptr) != E_INVALIDARG) {
        return false;
    }

    struct Configuration {
        std::uint32_t flags = 0;
        std::uint32_t page_table_entries = 4;
        std::uint64_t reserved[3] = {};
    } config;
    static_assert(sizeof(config) == 32);

    const HRESULT hr = D3D12ConfigureVirtualMemoryX(&config);
    return hr == S_OK;
}
}

int main(int argc, char** argv)
{
    if (argc != 2) {
        return 2;
    }
    const std::string_view scenario = argv[1];
    bool passed = false;
    if (scenario == "exports"sv) {
        passed = export_checks();
    } else if (scenario == "serialize"sv) {
        passed = serialize_checks();
    } else if (scenario == "deserialize"sv) {
        passed = deserialize_checks();
    } else if (scenario == "warp_root"sv) {
        passed = warp_root_checks();
    } else if (scenario == "virtual_memory"sv) {
        passed = virtual_memory_checks();
    } else {
        return 2;
    }
    std::printf("Desktop d3d12_xs.dll %s: %s\n", argv[1], passed ? "PASS" : "FAIL");
    return passed ? 0 : 1;
}
