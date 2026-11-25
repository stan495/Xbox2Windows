// Loads the built d3d12_xs.dll by explicit path and calls its exported device creation entry.
// The fixture is source-built and does not load or inspect a title.
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <wrl/client.h>

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <string>
#include <string_view>

#include "d3d12x_struct_return.h"
#include "d3d12x_pso_cs.h"

using Microsoft::WRL::ComPtr;

namespace {

using XboxCreateDevice = HRESULT(WINAPI*)(IUnknown*, const void*, REFIID, void**);
using SerializeVersioned = HRESULT(WINAPI*)(const D3D12_VERSIONED_ROOT_SIGNATURE_DESC*,
                                            ID3DBlob**, ID3DBlob**);
using CreateDeserializer = HRESULT(WINAPI*)(LPCVOID, SIZE_T, REFIID, void**);

constexpr std::uint32_t observed_console_flags = 0x03000000u;

constexpr GUID unknown_iid = {0x7c0e1a10, 0x5f2d, 0x4b7a,
                              {0x9a, 0x51, 0x3d, 0x9c, 0x6b, 0x2e, 0x8f, 0x01}};

struct AdapterChoice {
    ComPtr<IDXGIFactory6> factory;
    ComPtr<IDXGIAdapter1> adapter;
    DXGI_ADAPTER_DESC1 desc{};
};

bool contains_ignoring_case(const wchar_t* text, const wchar_t* part)
{
    const std::size_t length = std::wcslen(part);
    for (; *text; ++text) {
        if (_wcsnicmp(text, part, length) == 0) return true;
    }
    return false;
}

// Same selection convention as the existing D3D12 fixtures: WARP on request, otherwise the first
// non-software adapter in high-performance order. XBOX2WINDOWS_ADAPTER filters by description.
int select_adapter(bool warp, AdapterChoice& choice)
{
    HRESULT hr = CreateDXGIFactory2(0, IID_PPV_ARGS(&choice.factory));
    if (FAILED(hr)) {
        std::fprintf(stderr, "CreateDXGIFactory2 failed: 0x%08lx\n", static_cast<unsigned long>(hr));
        return 1;
    }
    if (warp) {
        hr = choice.factory->EnumWarpAdapter(IID_PPV_ARGS(&choice.adapter));
        if (FAILED(hr) || !choice.adapter) {
            std::fprintf(stderr, "WARP adapter unavailable: 0x%08lx\n", static_cast<unsigned long>(hr));
            return 1;
        }
        hr = choice.adapter->GetDesc1(&choice.desc);
        if (FAILED(hr)) {
            std::fprintf(stderr, "WARP adapter description failed: 0x%08lx\n", static_cast<unsigned long>(hr));
            return 1;
        }
        return 0;
    }

    wchar_t wanted[128]{};
    const DWORD filter = GetEnvironmentVariableW(L"XBOX2WINDOWS_ADAPTER", wanted, 128);
    if (filter >= 128) {
        std::fputs("XBOX2WINDOWS_ADAPTER is longer than 127 characters\n", stderr);
        return 1;
    }
    for (UINT index = 0;; ++index) {
        hr = choice.factory->EnumAdapterByGpuPreference(
            index, DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE, IID_PPV_ARGS(choice.adapter.ReleaseAndGetAddressOf()));
        if (hr == DXGI_ERROR_NOT_FOUND) {
            if (filter) {
                std::fprintf(stderr, "No D3D12 hardware adapter matches XBOX2WINDOWS_ADAPTER=%ls\n", wanted);
                return 1;
            }
            std::fputs("No physical D3D12 adapter available\n", stderr);
            return 77;
        }
        if (FAILED(hr)) {
            std::fprintf(stderr, "EnumAdapterByGpuPreference failed: 0x%08lx\n", static_cast<unsigned long>(hr));
            return 1;
        }
        hr = choice.adapter->GetDesc1(&choice.desc);
        if (FAILED(hr)) {
            std::fprintf(stderr, "GetDesc1 failed: 0x%08lx\n", static_cast<unsigned long>(hr));
            return 1;
        }
        if (choice.desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) continue;
        if (filter && !contains_ignoring_case(choice.desc.Description, wanted)) {
            std::fprintf(stderr, "XBOX2WINDOWS_ADAPTER: skipping %ls\n", choice.desc.Description);
            continue;
        }
        return 0;
    }
}

bool enable_debug_layer()
{
    ComPtr<ID3D12Debug> debug;
    if (FAILED(D3D12GetDebugInterface(IID_PPV_ARGS(&debug))) || !debug) return false;
    debug->EnableDebugLayer();
    return true;
}

struct DesktopD3D12 {
    HMODULE module = nullptr;
    SerializeVersioned serialize = nullptr;

    bool load()
    {
        module = LoadLibraryExW(L"d3d12.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
        if (!module) return false;
        serialize = reinterpret_cast<SerializeVersioned>(
            GetProcAddress(module, "D3D12SerializeVersionedRootSignature"));
        return serialize != nullptr;
    }

    ~DesktopD3D12()
    {
        if (module) FreeLibrary(module);
    }
};

bool same_blob(ID3DBlob* a, ID3DBlob* b)
{
    return a && b && a->GetBufferSize() == b->GetBufferSize() &&
           std::memcmp(a->GetBufferPointer(), b->GetBufferPointer(), a->GetBufferSize()) == 0;
}

// The test root: descriptor tables for SRV(t0) and UAV(u0), plus one static sampler at s0 with
// distinct field values. A texture cannot use a root descriptor, so t0 and u0 each get a
// one-descriptor table.
struct TestRoot {
    D3D12_DESCRIPTOR_RANGE1 ranges[2]{};
    D3D12_ROOT_PARAMETER1 parameters[2]{};
    D3D12_STATIC_SAMPLER_DESC sampler{};
    D3D12_VERSIONED_ROOT_SIGNATURE_DESC desc{};

    explicit TestRoot(std::uint32_t flags)
    {
        ranges[0].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
        ranges[0].NumDescriptors = 1;
        ranges[0].BaseShaderRegister = 0;
        ranges[0].RegisterSpace = 0;
        ranges[0].Flags = D3D12_DESCRIPTOR_RANGE_FLAG_NONE;
        ranges[0].OffsetInDescriptorsFromTableStart = 0;
        ranges[1].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_UAV;
        ranges[1].NumDescriptors = 1;
        ranges[1].BaseShaderRegister = 0;
        ranges[1].RegisterSpace = 0;
        ranges[1].Flags = D3D12_DESCRIPTOR_RANGE_FLAG_NONE;
        ranges[1].OffsetInDescriptorsFromTableStart = 0;

        parameters[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        parameters[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
        parameters[0].DescriptorTable.NumDescriptorRanges = 1;
        parameters[0].DescriptorTable.pDescriptorRanges = &ranges[0];
        parameters[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        parameters[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
        parameters[1].DescriptorTable.NumDescriptorRanges = 1;
        parameters[1].DescriptorTable.pDescriptorRanges = &ranges[1];

        sampler.Filter = D3D12_FILTER_MIN_MAG_MIP_LINEAR;
        sampler.AddressU = D3D12_TEXTURE_ADDRESS_MODE_WRAP;
        sampler.AddressV = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
        sampler.AddressW = D3D12_TEXTURE_ADDRESS_MODE_BORDER;
        sampler.MipLODBias = 2.5f;
        sampler.MaxAnisotropy = 4;
        sampler.ComparisonFunc = D3D12_COMPARISON_FUNC_GREATER_EQUAL;
        sampler.BorderColor = D3D12_STATIC_BORDER_COLOR_OPAQUE_WHITE;
        sampler.MinLOD = 1.5f;
        sampler.MaxLOD = 9.0f;
        sampler.ShaderRegister = 0;
        sampler.RegisterSpace = 0;
        sampler.ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;

        desc.Version = D3D_ROOT_SIGNATURE_VERSION_1_1;
        desc.Desc_1_1.NumParameters = 2;
        desc.Desc_1_1.pParameters = parameters;
        desc.Desc_1_1.NumStaticSamplers = 1;
        desc.Desc_1_1.pStaticSamplers = &sampler;
        desc.Desc_1_1.Flags = static_cast<D3D12_ROOT_SIGNATURE_FLAGS>(flags);
    }
};

const char* sampler_mismatch(const D3D12_STATIC_SAMPLER_DESC& actual,
                             const D3D12_STATIC_SAMPLER_DESC& expected)
{
    if (actual.Filter != expected.Filter) return "Filter";
    if (actual.AddressU != expected.AddressU) return "AddressU";
    if (actual.AddressV != expected.AddressV) return "AddressV";
    if (actual.AddressW != expected.AddressW) return "AddressW";
    if (actual.MipLODBias != expected.MipLODBias) return "MipLODBias";
    if (actual.MaxAnisotropy != expected.MaxAnisotropy) return "MaxAnisotropy";
    if (actual.ComparisonFunc != expected.ComparisonFunc) return "ComparisonFunc";
    if (actual.BorderColor != expected.BorderColor) return "BorderColor";
    if (actual.MinLOD != expected.MinLOD) return "MinLOD";
    if (actual.MaxLOD != expected.MaxLOD) return "MaxLOD";
    if (actual.ShaderRegister != expected.ShaderRegister) return "ShaderRegister";
    if (actual.RegisterSpace != expected.RegisterSpace) return "RegisterSpace";
    if (actual.ShaderVisibility != expected.ShaderVisibility) return "ShaderVisibility";
    return nullptr;
}

void print_sampler(const D3D12_STATIC_SAMPLER_DESC& sampler)
{
    std::printf("pso_sampler filter=0x%x address=%u/%u/%u mip_bias=%g max_aniso=%u comparison=%u "
                "border=%u min_lod=%g max_lod=%g register=%u space=%u visibility=%u\n",
                static_cast<unsigned>(sampler.Filter), static_cast<unsigned>(sampler.AddressU),
                static_cast<unsigned>(sampler.AddressV), static_cast<unsigned>(sampler.AddressW),
                sampler.MipLODBias, sampler.MaxAnisotropy, static_cast<unsigned>(sampler.ComparisonFunc),
                static_cast<unsigned>(sampler.BorderColor), sampler.MinLOD, sampler.MaxLOD,
                sampler.ShaderRegister, sampler.RegisterSpace,
                static_cast<unsigned>(sampler.ShaderVisibility));
}

void print_environment(const AdapterChoice& choice, bool warp, bool debug)
{
    std::printf("backend=%s adapter=%ls vendor=%04x device=%04x\n", warp ? "warp" : "hardware",
                choice.desc.Description, choice.desc.VendorId, choice.desc.DeviceId);
    LARGE_INTEGER driver{};
    if (SUCCEEDED(choice.adapter->CheckInterfaceSupport(__uuidof(IDXGIDevice), &driver))) {
        std::printf("driver=%u.%u.%u.%u\n", HIWORD(driver.HighPart), LOWORD(driver.HighPart),
                    HIWORD(driver.LowPart), LOWORD(driver.LowPart));
    } else {
        std::puts("driver=unavailable via DXGI");
    }
    using RtlGetVersionFn = LONG(WINAPI*)(OSVERSIONINFOW*);
    const auto rtl_get_version = reinterpret_cast<RtlGetVersionFn>(
        GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "RtlGetVersion"));
    OSVERSIONINFOW os{};
    os.dwOSVersionInfoSize = sizeof(os);
    if (rtl_get_version && rtl_get_version(&os) == 0) {
        std::printf("windows=%lu.%lu.%lu\n", os.dwMajorVersion, os.dwMinorVersion, os.dwBuildNumber);
    } else {
        std::puts("windows=unavailable");
    }
    std::printf("debug_layer=%s\n", debug ? "enabled" : "unavailable");
}

HMODULE load_shim(const char* path, XboxCreateDevice& create, SerializeVersioned& serialize,
                  CreateDeserializer& deserialize)
{
    const int length = MultiByteToWideChar(CP_ACP, 0, path, -1, nullptr, 0);
    if (length <= 0) {
        std::fputs("Cannot convert the d3d12_xs.dll path\n", stderr);
        return nullptr;
    }
    std::wstring wide(static_cast<std::size_t>(length), L'\0');
    if (!MultiByteToWideChar(CP_ACP, 0, path, -1, wide.data(), length)) {
        std::fputs("Cannot convert the d3d12_xs.dll path\n", stderr);
        return nullptr;
    }
    HMODULE module = LoadLibraryExW(wide.c_str(), nullptr,
                                    LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!module) {
        std::fprintf(stderr, "LoadLibraryExW(%ls) failed: %lu\n", wide.c_str(),
                     static_cast<unsigned long>(GetLastError()));
        return nullptr;
    }
    create = reinterpret_cast<XboxCreateDevice>(GetProcAddress(module, "D3D12XboxCreateDevice"));
    serialize = reinterpret_cast<SerializeVersioned>(
        GetProcAddress(module, "D3D12SerializeVersionedRootSignature"));
    deserialize = reinterpret_cast<CreateDeserializer>(
        GetProcAddress(module, "D3D12CreateRootSignatureDeserializer"));
    if (!create || !serialize || !deserialize) {
        std::fputs("d3d12_xs.dll does not export D3D12XboxCreateDevice and the two "
                   "root-signature entries\n", stderr);
        FreeLibrary(module);
        return nullptr;
    }
    HMODULE owner = nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCWSTR>(create), &owner) ||
        owner != module) {
        std::fputs("D3D12XboxCreateDevice does not resolve inside d3d12_xs.dll\n", stderr);
        FreeLibrary(module);
        return nullptr;
    }
    return module;
}

int struct_return_checks(ID3D12Device* device)
{
    constexpr UINT node_mask = 0;
    constexpr auto heap_type = D3D12_HEAP_TYPE_UPLOAD;
    const auto expected = device->GetCustomHeapProperties(node_mask, heap_type);
    static_assert(sizeof(D3D12_HEAP_PROPERTIES) == 5 * sizeof(std::uint32_t));

    struct Case {
        const char* name;
        bool return_buffer_first;
        bool stale_vtable;
    };
    constexpr Case cases[] = {
        {"device_first", false, false},
        {"return_buffer_first", true, false},
        {"return_buffer_first_stale_vtable", true, true},
    };
    struct alignas(16) GuardedProperties {
        std::uint64_t before[2];
        D3D12_HEAP_PROPERTIES value;
        std::uint32_t after;
    };
    static_assert(offsetof(GuardedProperties, value) == 16);
    static_assert(offsetof(GuardedProperties, after) ==
                  offsetof(GuardedProperties, value) + sizeof(D3D12_HEAP_PROPERTIES));

    constexpr std::uint64_t prefix = 0xd942c503b845a70full;
    constexpr std::uint32_t suffix = 0xa658f9e2u;
    for (const auto& test : cases) {
        GuardedProperties output{{prefix, prefix}, {}, suffix};
        if (test.stale_vtable) {
            // Give the stack buffer the same first word as the real COM interface.
            // Both arguments now look like the device to a vtable-only classifier.
            std::memcpy(&output.value, device, sizeof(void*));
        }

        void* first = test.return_buffer_first ? static_cast<void*>(&output.value) : device;
        void* second = test.return_buffer_first ? device : static_cast<void*>(&output.value);
        auto* returned = d3d12x_struct_return::get_custom_heap_properties(
            first, second, node_mask, heap_type, device);
        if (returned != &output.value) {
            std::fprintf(stderr, "%s: did not return the caller's output pointer\n", test.name);
            return 1;
        }
        if (output.before[0] != prefix || output.before[1] != prefix || output.after != suffix) {
            std::fprintf(stderr, "%s: output guards changed\n", test.name);
            return 1;
        }
        if (std::memcmp(&output.value, &expected, sizeof(expected)) != 0) {
            std::fprintf(stderr, "%s: heap properties differ from the desktop call\n", test.name);
            return 1;
        }
        std::printf("struct_return %s: properties and guards match\n", test.name);
    }

    // The adapter must pass heap_type through. A DEFAULT heap has no CPU page property on any
    // adapter, so its properties differ from UPLOAD's even where READBACK's do not.
    const auto default_expected = device->GetCustomHeapProperties(node_mask, D3D12_HEAP_TYPE_DEFAULT);
    if (std::memcmp(&default_expected, &expected, sizeof(expected)) == 0) {
        std::fputs("FAIL: struct_return heap type: DEFAULT and UPLOAD properties are equal\n", stderr);
        return 1;
    }
    struct TypeCase {
        const char* name;
        D3D12_HEAP_TYPE type;
    };
    constexpr TypeCase type_cases[] = {
        {"default", D3D12_HEAP_TYPE_DEFAULT},
        {"readback", D3D12_HEAP_TYPE_READBACK},
    };
    for (const auto& type_case : type_cases) {
        const auto type_expected = device->GetCustomHeapProperties(node_mask, type_case.type);
        for (const bool return_buffer_first : {false, true}) {
            GuardedProperties output{{prefix, prefix}, {}, suffix};
            void* first = return_buffer_first ? static_cast<void*>(&output.value) : device;
            void* second = return_buffer_first ? device : static_cast<void*>(&output.value);
            auto* returned = d3d12x_struct_return::get_custom_heap_properties(
                first, second, node_mask, type_case.type, device);
            const char* order = return_buffer_first ? "return_buffer_first" : "device_first";
            if (returned != &output.value || output.before[0] != prefix ||
                output.before[1] != prefix || output.after != suffix ||
                std::memcmp(&output.value, &type_expected, sizeof(type_expected)) != 0) {
                std::fprintf(stderr, "FAIL: struct_return %s %s: pointer, guards or properties differ "
                             "from the desktop call\n", type_case.name, order);
                return 1;
            }
            std::printf("struct_return %s %s: properties and guards match\n", type_case.name, order);
        }
    }

    // Calls the adapter cannot resolve return nullptr and write nothing. Both buffers start
    // with the device's vtable pointer, so only the address comparison tells them apart.
    struct RejectCase {
        const char* name;
        int first;   // 0: nullptr, 1: device, 2: buffer a, 3: buffer b
        int second;
        bool pass_device;
    };
    constexpr RejectCase rejects[] = {
        {"null_first", 0, 1, true},
        {"null_second", 1, 0, true},
        {"device_twice", 1, 1, true},
        {"neither_is_device", 2, 3, true},
        {"null_device", 2, 1, false},
    };
    for (const auto& reject : rejects) {
        GuardedProperties a{{prefix, prefix}, {}, suffix};
        GuardedProperties b{{prefix, prefix}, {}, suffix};
        std::memcpy(&a.value, device, sizeof(void*));
        std::memcpy(&b.value, device, sizeof(void*));
        const GuardedProperties a_before = a;
        const GuardedProperties b_before = b;
        void* const candidates[] = {nullptr, device, &a.value, &b.value};
        auto* returned = d3d12x_struct_return::get_custom_heap_properties(
            candidates[reject.first], candidates[reject.second], node_mask, heap_type,
            reject.pass_device ? device : nullptr);
        if (returned) {
            std::fprintf(stderr, "FAIL: struct_return reject %s: returned %p instead of nullptr\n",
                         reject.name, static_cast<void*>(returned));
            return 1;
        }
        if (std::memcmp(&a, &a_before, sizeof(a)) != 0 || std::memcmp(&b, &b_before, sizeof(b)) != 0) {
            std::fprintf(stderr, "FAIL: struct_return reject %s: a candidate buffer changed\n",
                         reject.name);
            return 1;
        }
        std::printf("struct_return reject %s: nullptr, buffers unchanged\n", reject.name);
    }
    return 0;
}

int create_checks(XboxCreateDevice create, IUnknown* adapter, const LUID& expected_luid,
                  bool check_struct_return)
{
    void* raw = nullptr;
    const HRESULT hr = create(adapter, nullptr, __uuidof(ID3D12Device), &raw);
    if (hr != S_OK || !raw) {
        std::fprintf(stderr, "D3D12XboxCreateDevice create failed: 0x%08lx\n",
                     static_cast<unsigned long>(hr));
        return 1;
    }
    ComPtr<ID3D12Device> device;
    device.Attach(static_cast<ID3D12Device*>(raw));

    const LUID actual_luid = device->GetAdapterLuid();
    if (actual_luid.LowPart != expected_luid.LowPart || actual_luid.HighPart != expected_luid.HighPart) {
        std::fprintf(stderr, "device LUID {%08lx:%08lx} does not match adapter {%08lx:%08lx}\n",
                     static_cast<unsigned long>(actual_luid.HighPart),
                     static_cast<unsigned long>(actual_luid.LowPart),
                     static_cast<unsigned long>(expected_luid.HighPart),
                     static_cast<unsigned long>(expected_luid.LowPart));
        return 1;
    }
    if (device->GetNodeCount() != 1) {
        std::fprintf(stderr, "GetNodeCount returned %u\n", device->GetNodeCount());
        return 1;
    }
    ComPtr<ID3D12Fence> fence;
    const HRESULT fence_hr = device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence));
    if (fence_hr != S_OK || !fence || fence->GetCompletedValue() != 0) {
        std::fprintf(stderr, "CreateFence failed: 0x%08lx\n", static_cast<unsigned long>(fence_hr));
        return 1;
    }
    std::printf("device_create=S_OK node_count=1 fence=S_OK luid={%08lx:%08lx}\n",
                static_cast<unsigned long>(actual_luid.HighPart),
                static_cast<unsigned long>(actual_luid.LowPart));
    return check_struct_return ? struct_return_checks(device.Get()) : 0;
}

int negative_checks(XboxCreateDevice create, IUnknown* adapter)
{
    HRESULT hr = create(adapter, nullptr, __uuidof(ID3D12Device), nullptr);
    if (hr != E_POINTER) {
        std::fprintf(stderr, "null device output: hr=0x%08lx\n", static_cast<unsigned long>(hr));
        return 1;
    }

    const std::uint32_t parameters[16]{};
    void* device = reinterpret_cast<void*>(static_cast<std::uintptr_t>(0x1234));
    hr = create(adapter, parameters, __uuidof(ID3D12Device), &device);
    if (hr != E_INVALIDARG || device) {
        std::fprintf(stderr, "parameter block: hr=0x%08lx output=%p\n",
                     static_cast<unsigned long>(hr), device);
        return 1;
    }

    device = reinterpret_cast<void*>(static_cast<std::uintptr_t>(0x1234));
    hr = create(adapter, nullptr, unknown_iid, &device);
    if (hr != E_NOINTERFACE || device) {
        std::fprintf(stderr, "unknown IID: hr=0x%08lx output=%p\n",
                     static_cast<unsigned long>(hr), device);
        return 1;
    }
    std::puts("null_device=E_POINTER unknown_iid=0x80004002 output=null");
    return 0;
}

int pso_checks(XboxCreateDevice create, SerializeVersioned shim_serialize,
               CreateDeserializer shim_deserialize, IUnknown* adapter)
{
    DesktopD3D12 desktop;
    if (!desktop.load()) {
        std::fputs("Desktop d3d12.dll root-signature entries are unavailable\n", stderr);
        return 1;
    }

    TestRoot baseline(0);
    TestRoot tagged(observed_console_flags);

    // The desktop baseline clears both observed console bits; the tagged descriptor must be
    // rejected by desktop d3d12.dll and serialized byte-identically by d3d12_xs.dll.
    ComPtr<ID3DBlob> desktop_baseline;
    HRESULT hr = desktop.serialize(&baseline.desc, &desktop_baseline, nullptr);
    if (hr != S_OK || !desktop_baseline) {
        std::fprintf(stderr, "desktop baseline serialization failed: 0x%08lx\n",
                     static_cast<unsigned long>(hr));
        return 1;
    }
    ComPtr<ID3DBlob> desktop_tagged;
    const HRESULT desktop_tagged_hr = desktop.serialize(&tagged.desc, &desktop_tagged, nullptr);
    if (desktop_tagged_hr != E_INVALIDARG || desktop_tagged) {
        std::fprintf(stderr, "desktop tagged serialization: hr=0x%08lx blob=%s\n",
                     static_cast<unsigned long>(desktop_tagged_hr),
                     desktop_tagged ? "non-null" : "null");
        return 1;
    }
    ComPtr<ID3DBlob> shim_blob;
    hr = shim_serialize(&tagged.desc, &shim_blob, nullptr);
    if (hr != S_OK || !shim_blob || !same_blob(shim_blob.Get(), desktop_baseline.Get())) {
        std::fprintf(stderr, "shim tagged serialization: hr=0x%08lx blob=%s size=%zu\n",
                     static_cast<unsigned long>(hr), shim_blob ? "non-null" : "null",
                     shim_blob ? shim_blob->GetBufferSize() : 0);
        return 1;
    }

    ComPtr<ID3D12RootSignatureDeserializer> reader;
    hr = shim_deserialize(shim_blob->GetBufferPointer(), shim_blob->GetBufferSize(),
                           IID_PPV_ARGS(&reader));
    if (hr != S_OK || !reader) {
        std::fprintf(stderr, "root signature deserialize failed: 0x%08lx\n",
                     static_cast<unsigned long>(hr));
        return 1;
    }
    const D3D12_ROOT_SIGNATURE_DESC* roundtrip = reader->GetRootSignatureDesc();
    if (!roundtrip || roundtrip->NumParameters != 2 || !roundtrip->pParameters ||
        roundtrip->Flags != D3D12_ROOT_SIGNATURE_FLAG_NONE ||
        roundtrip->pParameters[0].ParameterType != D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE ||
        roundtrip->pParameters[1].ParameterType != D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE ||
        roundtrip->pParameters[0].DescriptorTable.NumDescriptorRanges != 1 ||
        roundtrip->pParameters[1].DescriptorTable.NumDescriptorRanges != 1 ||
        !roundtrip->pParameters[0].DescriptorTable.pDescriptorRanges ||
        !roundtrip->pParameters[1].DescriptorTable.pDescriptorRanges ||
        roundtrip->pParameters[0].DescriptorTable.pDescriptorRanges[0].RangeType !=
            D3D12_DESCRIPTOR_RANGE_TYPE_SRV ||
        roundtrip->pParameters[1].DescriptorTable.pDescriptorRanges[0].RangeType !=
            D3D12_DESCRIPTOR_RANGE_TYPE_UAV) {
        std::fputs("deserialized root signature parameters do not match SRV(t0), UAV(u0) tables\n",
                   stderr);
        return 1;
    }
    if (roundtrip->NumStaticSamplers != 1 || !roundtrip->pStaticSamplers) {
        std::fprintf(stderr, "deserialized static sampler count: %u\n", roundtrip->NumStaticSamplers);
        return 1;
    }
    print_sampler(roundtrip->pStaticSamplers[0]);
    if (const char* field = sampler_mismatch(roundtrip->pStaticSamplers[0], tagged.sampler)) {
        std::fprintf(stderr, "static sampler field %s differs after deserialization\n", field);
        return 1;
    }

    void* raw = nullptr;
    hr = create(adapter, nullptr, __uuidof(ID3D12Device), &raw);
    if (hr != S_OK || !raw) {
        std::fprintf(stderr, "D3D12XboxCreateDevice for PSO failed: 0x%08lx\n",
                     static_cast<unsigned long>(hr));
        return 1;
    }
    ComPtr<ID3D12Device> device;
    device.Attach(static_cast<ID3D12Device*>(raw));

    ComPtr<ID3D12RootSignature> root;
    const HRESULT root_hr = device->CreateRootSignature(
        0, shim_blob->GetBufferPointer(), shim_blob->GetBufferSize(), IID_PPV_ARGS(&root));
    if (root_hr != S_OK || !root) {
        std::fprintf(stderr, "CreateRootSignature from shim blob failed: 0x%08lx\n",
                     static_cast<unsigned long>(root_hr));
        return 1;
    }

    D3D12_COMPUTE_PIPELINE_STATE_DESC pso_desc{};
    pso_desc.pRootSignature = root.Get();
    pso_desc.CS = {d3d12x_pso_cs, sizeof(d3d12x_pso_cs)};
    ComPtr<ID3D12PipelineState> pipeline;
    const HRESULT pso_hr = device->CreateComputePipelineState(&pso_desc, IID_PPV_ARGS(&pipeline));
    if (pso_hr != S_OK || !pipeline) {
        std::fprintf(stderr, "CreateComputePipelineState failed: 0x%08lx\n",
                     static_cast<unsigned long>(pso_hr));
        return 1;
    }

    std::printf("pso_blob_bytes=%zu desktop_tagged_hr=0x%08lx root_hr=0x%08lx pso_hr=0x%08lx "
                "root=non-null pso=non-null\n",
                shim_blob->GetBufferSize(), static_cast<unsigned long>(desktop_tagged_hr),
                static_cast<unsigned long>(root_hr), static_cast<unsigned long>(pso_hr));
    return 0;
}

int reject_checks(XboxCreateDevice create, SerializeVersioned shim_serialize, IUnknown* adapter)
{
    DesktopD3D12 desktop;
    if (!desktop.load()) {
        std::fputs("Desktop d3d12.dll root-signature entries are unavailable\n", stderr);
        return 1;
    }

    const auto expect_serialize_reject = [&](const char* name,
                                             D3D12_VERSIONED_ROOT_SIGNATURE_DESC& desc) {
        ComPtr<ID3DBlob> desktop_blob, shim_blob;
        const HRESULT desktop_hr = desktop.serialize(&desc, &desktop_blob, nullptr);
        const HRESULT shim_hr = shim_serialize(&desc, &shim_blob, nullptr);
        if (desktop_hr != E_INVALIDARG || desktop_blob || shim_hr != E_INVALIDARG || shim_blob) {
            std::fprintf(stderr, "%s: desktop=0x%08lx/%s shim=0x%08lx/%s\n", name,
                         static_cast<unsigned long>(desktop_hr), desktop_blob ? "blob" : "null",
                         static_cast<unsigned long>(shim_hr), shim_blob ? "blob" : "null");
            return false;
        }
        std::printf("%s desktop=0x%08lx shim=0x%08lx blobs=null\n", name,
                    static_cast<unsigned long>(desktop_hr), static_cast<unsigned long>(shim_hr));
        return true;
    };

    TestRoot visibility(0);
    visibility.sampler.ShaderVisibility = static_cast<D3D12_SHADER_VISIBILITY>(99);
    if (!expect_serialize_reject("static_sampler_visibility_99", visibility.desc)) return 1;

    TestRoot filter(0);
    filter.sampler.Filter = static_cast<D3D12_FILTER>(0x7fffffff);
    if (!expect_serialize_reject("static_sampler_filter_0x7fffffff", filter.desc)) return 1;

    TestRoot flag(0x04000000u);
    if (!expect_serialize_reject("unknown_flag_0x04000000", flag.desc)) return 1;

    TestRoot parameter(0);
    parameter.parameters[0].ParameterType =
        static_cast<D3D12_ROOT_PARAMETER_TYPE>(0x7fffffff);
    if (!expect_serialize_reject("unknown_parameter_type_0x7fffffff", parameter.desc)) return 1;

    void* raw = nullptr;
    HRESULT hr = create(adapter, nullptr, __uuidof(ID3D12Device), &raw);
    if (hr != S_OK || !raw) {
        std::fprintf(stderr, "D3D12XboxCreateDevice for PSO reject failed: 0x%08lx\n",
                     static_cast<unsigned long>(hr));
        return 1;
    }
    ComPtr<ID3D12Device> device;
    device.Attach(static_cast<ID3D12Device*>(raw));

    D3D12_COMPUTE_PIPELINE_STATE_DESC pso_desc{};
    pso_desc.CS = {d3d12x_pso_cs, sizeof(d3d12x_pso_cs)};
    ComPtr<ID3D12PipelineState> pipeline;
    hr = device->CreateComputePipelineState(&pso_desc, IID_PPV_ARGS(&pipeline));
    if (hr != E_INVALIDARG || pipeline) {
        std::fprintf(stderr, "pso_without_root_signature: hr=0x%08lx pso=%s\n",
                     static_cast<unsigned long>(hr), pipeline ? "non-null" : "null");
        return 1;
    }
    std::printf("pso_without_root_signature=0x%08lx pso=null\n", static_cast<unsigned long>(hr));
    return 0;
}

}  // namespace

int main(int argc, char** argv)
{
    if (argc != 4) {
        std::fprintf(stderr,
                     "usage: %s <path-to-d3d12_xs.dll> <create|negative|pso|reject|struct-return> "
                     "<warp|hardware>\n",
                     argv[0]);
        return 2;
    }
    const std::string_view scenario = argv[2];
    const std::string_view backend = argv[3];
    if ((scenario != "create" && scenario != "negative" && scenario != "pso" &&
         scenario != "reject" && scenario != "struct-return") ||
        (backend != "warp" && backend != "hardware")) {
        std::fputs("unknown scenario or backend\n", stderr);
        return 2;
    }
    const bool warp = backend == "warp";

    XboxCreateDevice create = nullptr;
    SerializeVersioned shim_serialize = nullptr;
    CreateDeserializer shim_deserialize = nullptr;
    HMODULE shim = load_shim(argv[1], create, shim_serialize, shim_deserialize);
    if (!shim) return 1;

    AdapterChoice choice;
    const int selected = select_adapter(warp, choice);
    if (selected) {
        FreeLibrary(shim);
        return selected;
    }
    const bool debug = enable_debug_layer();
    print_environment(choice, warp, debug);

    int result = 0;
    if (scenario == "create" || scenario == "struct-return") {
        result = create_checks(create, choice.adapter.Get(), choice.desc.AdapterLuid,
                               scenario == "struct-return");
    } else if (scenario == "negative") {
        result = negative_checks(create, choice.adapter.Get());
    } else if (scenario == "pso") {
        result = pso_checks(create, shim_serialize, shim_deserialize, choice.adapter.Get());
    } else {
        result = reject_checks(create, shim_serialize, choice.adapter.Get());
    }
    FreeLibrary(shim);
    std::printf("d3d12x_device_check %s %s: %s\n", argv[2], argv[3], result == 0 ? "PASS" : "FAIL");
    return result;
}
