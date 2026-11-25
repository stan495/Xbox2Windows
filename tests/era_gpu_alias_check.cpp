// era_gpu_alias::open without a GPU. open must refuse a span that is not the start of one committed,
// accessible allocation with E_INVALIDARG before it calls D3D12, and a span it accepts must reach
// the device's existing-heaps query. The device is the recording fake of
// era_gpu_alias_fake_device.h, so the check needs no D3D12 runtime and no debug layer.
// gpu_alias_warp makes the same refusals on WARP, where only the debug layer could show that D3D12
// was reached, since D3D12 refuses most of these spans with E_INVALIDARG as well.
#include "era_gpu_alias.h"
#include "era_gpu_alias_fake_device.h"

#include <cstdint>
#include <cstdio>
#include <initializer_list>
#include <limits>

namespace {
int failures = 0;
constexpr std::uint64_t page = 64 * 1024;
constexpr std::uint64_t stale_size = 7;  // left in the Alias by the caller; open must clear it

void fail(const char* what, const char* detail)
{
    std::printf("FAIL: %s: %s\n", what, detail);
    ++failures;
}

// Calls open on a fresh fake. Returns the result and leaves the fake's record in state.
HRESULT open_on_fake(FakeDevice& state, void* base, std::uint64_t size)
{
    era_gpu_alias::Alias alias;
    alias.size = stale_size;
    const HRESULT hr = era_gpu_alias::open(static_cast<ID3D12Device*>(fake_device(state)), base, size, alias);
    if (alias.heap || alias.buffer || alias.size != 0) fail("alias", "a failed open left the Alias filled");
    return hr;
}

// The span must be refused with E_INVALIDARG and no call to the device at all.
void refused(const char* what, void* base, std::uint64_t size)
{
    FakeDevice state;
    const HRESULT hr = open_on_fake(state, base, size);
    if (state.calls() != 0) {
        fail(what, "passed to D3D12");
    } else if (hr != E_INVALIDARG) {
        fail(what, "not refused with E_INVALIDARG");
    } else {
        std::printf("refused  %-36s E_INVALIDARG, 0 device calls\n", what);
    }
}

// The span must pass validation: open asks for D3D12_FEATURE_EXISTING_HEAPS once and, when the
// fake reports no support, returns E_NOTIMPL without asking for anything else.
void reaches_device(const char* what, void* base, std::uint64_t size)
{
    FakeDevice state;
    const HRESULT hr = open_on_fake(state, base, size);
    if (state.feature_calls != 1 || state.feature != D3D12_FEATURE_EXISTING_HEAPS ||
        state.feature_bytes != sizeof(D3D12_FEATURE_DATA_EXISTING_HEAPS)) {
        fail(what, "did not query D3D12_FEATURE_EXISTING_HEAPS once");
    } else if (hr != E_NOTIMPL || state.queries != 0 || state.unexpected_calls != 0) {
        fail(what, "a failed feature query did not end in E_NOTIMPL");
    } else {
        std::printf("accepted %-36s existing-heaps query, then E_NOTIMPL\n", what);
    }
}

void* reserve(std::uint64_t bytes)
{
    return VirtualAlloc(nullptr, static_cast<SIZE_T>(bytes), MEM_RESERVE, PAGE_READWRITE);
}

bool commit(void* at, std::uint64_t bytes, DWORD protect)
{
    return VirtualAlloc(at, static_cast<SIZE_T>(bytes), MEM_COMMIT, protect) != nullptr;
}

char* at(void* base, std::uint64_t offset)
{
    return static_cast<char*>(base) + offset;
}

// The feature answer decides between E_NOTIMPL and the ID3D12Device3 query.
void feature_answers(void* base)
{
    FakeDevice failed;
    failed.feature_result = E_FAIL;
    FakeDevice unsupported;
    unsupported.feature_result = S_OK;
    unsupported.existing_heaps = FALSE;
    FakeDevice supported;
    supported.feature_result = S_OK;
    supported.existing_heaps = TRUE;
    const HRESULT failed_hr = open_on_fake(failed, base, page);
    const HRESULT unsupported_hr = open_on_fake(unsupported, base, page);
    const HRESULT supported_hr = open_on_fake(supported, base, page);
    if (failed_hr != E_NOTIMPL || failed.queries != 0) fail("feature query fails", "not E_NOTIMPL before ID3D12Device3");
    if (unsupported_hr != E_NOTIMPL || unsupported.feature_calls != 1 || unsupported.queries != 0) {
        fail("existing heaps unsupported", "not E_NOTIMPL before ID3D12Device3");
    }
    if (supported_hr != E_NOTIMPL || supported.feature_calls != 1 || supported.queries != 1 ||
        supported.queried != __uuidof(ID3D12Device3) || supported.unexpected_calls != 0) {
        fail("existing heaps supported", "ID3D12Device3 not queried once, or its absence not E_NOTIMPL");
    }
    std::printf("feature: query failed -> E_NOTIMPL, unsupported -> E_NOTIMPL, supported -> ID3D12Device3 "
                "query -> E_NOTIMPL\n");
}
}

int main()
{
    // One committed read-write allocation of two pages, and a third page left reserved behind it.
    void* whole = reserve(3 * page);
    // Three committed regions of one allocation, with different protections.
    void* mixed = reserve(3 * page);
    // Committed head, with a tail that is reserved, PAGE_NOACCESS and then PAGE_GUARD.
    void* tail = reserve(2 * page);
    void* reserved = reserve(page);
    if (!whole || !commit(whole, 2 * page, PAGE_READWRITE) || !mixed || !commit(mixed, page, PAGE_READWRITE) ||
        !commit(at(mixed, page), page, PAGE_READONLY) || !commit(at(mixed, 2 * page), page, PAGE_EXECUTE_READ) ||
        !tail || !commit(tail, page, PAGE_READWRITE) || !reserved) {
        std::printf("FAIL: setup: VirtualAlloc\n");
        return 1;
    }

    era_gpu_alias::Alias alias;
    alias.size = stale_size;
    if (era_gpu_alias::open(nullptr, whole, page, alias) != E_INVALIDARG || alias.size != 0) {
        fail("null device", "not refused with E_INVALIDARG");
    }
    refused("null base", nullptr, page);
    refused("empty span", whole, 0);
    refused("interior pointer", at(whole, 0x1000), page);
    refused("second page of the allocation", at(whole, page), page);
    refused("span into the reserved page", whole, 2 * page + 1);
    refused("one byte past the allocation", mixed, 3 * page + 1);
    refused("span that wraps the address space", whole, (std::numeric_limits<std::uint64_t>::max)());
    refused("reserve-only allocation", reserved, page);
    refused("reserved tail", tail, 2 * page);

    reaches_device("one byte", whole, 1);
    reaches_device("first page of two", whole, page);
    reaches_device("whole committed range", whole, 2 * page);
    reaches_device("odd size inside the range", whole, page + 3);
    reaches_device("three regions, three protections", mixed, 3 * page);
    reaches_device("into the second region by one byte", mixed, page + 1);
    reaches_device("committed head of a reserved tail", tail, page);

    // The same tail committed PAGE_NOACCESS, then made PAGE_READWRITE | PAGE_GUARD.
    DWORD old_protect = 0;
    if (!commit(at(tail, page), page, PAGE_NOACCESS)) {
        fail("setup", "commit PAGE_NOACCESS");
    } else {
        refused("PAGE_NOACCESS tail", tail, 2 * page);
        if (!VirtualProtect(at(tail, page), page, PAGE_READWRITE | PAGE_GUARD, &old_protect)) {
            fail("setup", "VirtualProtect PAGE_GUARD");
        } else {
            refused("PAGE_GUARD tail", tail, 2 * page);
            refused("PAGE_GUARD tail, last byte", tail, page + 1);
        }
    }
    feature_answers(whole);

    for (void* allocation : {whole, mixed, tail, reserved}) VirtualFree(allocation, 0, MEM_RELEASE);
    if (failures) {
        std::printf("era_gpu_alias_check: %d FAILED\n", failures);
        return 1;
    }
    std::printf("era_gpu_alias_check: PASS\n");
    return 0;
}
