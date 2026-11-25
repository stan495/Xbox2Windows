// Startup blocks below 2^40 (#1758, under #874). Source-built check of the opt-in startup
// compatibility configuration X2W_XMEM_STARTUP_BELOW_40=1 in xmem.dll (src/xmem_mapped_pages.cpp).
//
// The configuration applies to one XMemVirtualAlloc shape: a null address, type 0x20003000
// (MEM_LARGE_PAGES | MEM_RESERVE | MEM_COMMIT), attribute 0x283 and protection 0x2004 or 0x2404.
// Such a block must lie entirely below 2^40 (exclusive), last byte included, with a 2 MiB-aligned
// base. A block that cannot be placed there fails with NULL and the host error, logs a distinct
// "below 2^40 refused" line and leaves no allocation behind.
//
// Usage: xmem_startup_below_40_check <path to xmem.dll>
// The DLL is loaded by path so the same checks run against the deployed shim and against the
// test-controls build, whose X2W_XMEM_TEST_BELOW_40 mutants each must make a named check fail
// (registered as xmem_startup_below_40_mutant_* in CMakeLists.txt).
//
// The low range is exhausted deterministically: every free region below 2^40 in this process is
// reserved (VirtualQuery walk + MEM_RESERVE), optionally leaving one 4 MiB window that ends exactly
// at 2^40. No placement by the OS is assumed for unconstrained allocations.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iterator>
#include <map>
#include <sstream>
#include <string>
#include <vector>

namespace {
using XMemVirtualAllocFn = void*(WINAPI*)(void*, SIZE_T, DWORD, ULONGLONG, DWORD);

constexpr ULONG_PTR limit = ULONG_PTR{1} << 40;  // exclusive
constexpr SIZE_T kib = SIZE_T{1} << 10;
constexpr SIZE_T mib = SIZE_T{1} << 20;
constexpr SIZE_T gib = SIZE_T{1} << 30;
constexpr SIZE_T large_page = 2 * mib;
constexpr DWORD pool_type = 0x20003000;
constexpr DWORD pages_64k_type = 0x20403000;
constexpr char config[] = "X2W_XMEM_STARTUP_BELOW_40";
constexpr char placed_marker[] = "x2w-xmem startup block below 2^40: ";
constexpr char refused_marker[] = "x2w-xmem startup block below 2^40 refused: ";

XMemVirtualAllocFn xmem_virtual_alloc = nullptr;
std::string log_path;
std::size_t log_seen = 0;
int failures = 0;

bool record(bool pass, const char* name)
{
    if (!pass) {
        std::printf("check_failed: %s\n", name);
        ++failures;
    }
    return pass;
}

void set_config(bool on) { SetEnvironmentVariableA(config, on ? "1" : nullptr); }

// Log lines written by xmem.dll (X2W_TRAP_LOG) since the previous call.
std::string new_log()
{
    std::ifstream in(log_path, std::ios::binary);
    std::string all((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    if (all.size() < log_seen) return {};
    std::string fresh = all.substr(log_seen);
    log_seen = all.size();
    return fresh;
}

std::size_t count(const std::string& text, const char* needle)
{
    std::size_t n = 0;
    for (auto at = text.find(needle); at != std::string::npos; at = text.find(needle, at + 1)) ++n;
    return n;
}

struct Call {
    void* result;
    DWORD error;
    std::string log;
};

Call call(SIZE_T bytes, DWORD type, ULONGLONG attributes, DWORD protection)
{
    new_log();
    SetLastError(0);
    void* result = xmem_virtual_alloc(nullptr, bytes, type, attributes, protection);
    const DWORD error = result ? 0 : GetLastError();
    return {result, error, new_log()};
}

bool below_limit(const void* base, SIZE_T bytes)
{
    const auto at = reinterpret_cast<ULONG_PTR>(base);
    return bytes && bytes <= limit && at <= limit - bytes;  // at + bytes - 1 < 2^40 without overflow
}

// Committed private read/write block of exactly bytes at base, first and last byte writable.
bool check_block(void* base, SIZE_T bytes)
{
    MEMORY_BASIC_INFORMATION info{};
    if (VirtualQuery(base, &info, sizeof(info)) != sizeof(info) || info.BaseAddress != base ||
        info.AllocationBase != base || info.State != MEM_COMMIT || info.Protect != PAGE_READWRITE ||
        info.Type != MEM_PRIVATE || info.RegionSize != bytes) {
        std::printf("  region at %p: base %p allocation %p state 0x%lx protect 0x%lx type 0x%lx size 0x%zx\n", base,
                    info.BaseAddress, info.AllocationBase, info.State, info.Protect, info.Type, info.RegionSize);
        return false;
    }
    auto* first = static_cast<volatile unsigned char*>(base);
    auto* last = first + (bytes - 1);
    if (*first != 0 || *last != 0) return false;
    *first = 0xA5;
    *last = 0x5A;
    return *first == 0xA5 && *last == 0x5A;
}

bool released(void* base)
{
    if (!VirtualFree(base, 0, MEM_RELEASE)) return false;
    MEMORY_BASIC_INFORMATION info{};
    return VirtualQuery(base, &info, sizeof(info)) == sizeof(info) && info.State == MEM_FREE;
}

// Non-free allocations in the whole user address space: allocation base -> bytes.
std::map<ULONG_PTR, SIZE_T> allocations()
{
    std::map<ULONG_PTR, SIZE_T> result;
    SYSTEM_INFO system{};
    GetSystemInfo(&system);
    auto at = reinterpret_cast<ULONG_PTR>(system.lpMinimumApplicationAddress);
    const auto end = reinterpret_cast<ULONG_PTR>(system.lpMaximumApplicationAddress);
    MEMORY_BASIC_INFORMATION info{};
    while (at < end && VirtualQuery(reinterpret_cast<void*>(at), &info, sizeof(info)) == sizeof(info)) {
        if (info.State != MEM_FREE) result[reinterpret_cast<ULONG_PTR>(info.AllocationBase)] += info.RegionSize;
        at = reinterpret_cast<ULONG_PTR>(info.BaseAddress) + info.RegionSize;
    }
    return result;
}

// An allocation of at least bytes that appeared or grew between the two snapshots.
bool nothing_new(const std::map<ULONG_PTR, SIZE_T>& before, const std::map<ULONG_PTR, SIZE_T>& after, SIZE_T bytes)
{
    bool clean = true;
    for (const auto& [base, size] : after) {
        const auto old = before.find(base);
        const SIZE_T grown = old == before.end() ? size : (size > old->second ? size - old->second : 0);
        if (grown >= bytes) {
            std::printf("  new allocation %p, 0x%zx bytes\n", reinterpret_cast<void*>(base), size);
            clean = false;
        }
    }
    return clean;
}

// Reserves every free region below 2^40 except [hole, hole + hole_bytes).
struct LowRangeBlockers {
    std::vector<void*> blocks;
    bool complete = true;

    LowRangeBlockers(ULONG_PTR hole, SIZE_T hole_bytes)
    {
        constexpr ULONG_PTR granularity = 64 * kib;
        ULONG_PTR at = 0;
        MEMORY_BASIC_INFORMATION info{};
        while (at < limit && VirtualQuery(reinterpret_cast<void*>(at), &info, sizeof(info)) == sizeof(info)) {
            const auto region = reinterpret_cast<ULONG_PTR>(info.BaseAddress);
            const ULONG_PTR region_end = region + info.RegionSize;
            at = region_end;
            if (info.State != MEM_FREE) continue;
            // Below the rounded start no allocation fits; below 64 KiB nothing can be reserved (and a
            // zero address would mean "anywhere").
            ULONG_PTR start = (region + granularity - 1) & ~(granularity - 1);
            if (start < granularity) start = granularity;
            const ULONG_PTR stop = region_end < limit ? region_end : limit;
            reserve(start, hole_bytes && hole < stop ? (hole > start ? hole : start) : stop);
            if (hole_bytes) reserve(hole + hole_bytes > start ? hole + hole_bytes : start, stop);
        }
    }
    void reserve(ULONG_PTR start, ULONG_PTR stop)
    {
        if (start >= stop) return;
        void* block = VirtualAlloc(reinterpret_cast<void*>(start), stop - start, MEM_RESERVE, PAGE_NOACCESS);
        if (block)
            blocks.push_back(block);
        else {
            std::printf("  could not reserve %p-%p (error %lu)\n", reinterpret_cast<void*>(start),
                        reinterpret_cast<void*>(stop), GetLastError());
            complete = false;
        }
    }
    ~LowRangeBlockers()
    {
        for (void* block : blocks) VirtualFree(block, 0, MEM_RELEASE);
    }
};

// True when no 64 KiB block can be placed below 2^40 outside the given hole.
bool low_range_exhausted()
{
    MEM_ADDRESS_REQUIREMENTS requirements{};
    requirements.HighestEndingAddress = reinterpret_cast<void*>(limit - 1);
    MEM_EXTENDED_PARAMETER parameter{};
    parameter.Type = MemExtendedParameterAddressRequirements;
    parameter.Pointer = &requirements;
    void* probe = VirtualAlloc2(GetCurrentProcess(), nullptr, 64 * kib, MEM_RESERVE, PAGE_NOACCESS, &parameter, 1);
    if (!probe) return true;
    std::printf("  low-range probe still placed a block at %p\n", probe);
    VirtualFree(probe, 0, MEM_RELEASE);
    return false;
}

// Configuration on: the traced shape lands 2 MiB aligned and below 2^40, first and last byte usable.
// Sizes: one large page; an odd number of large pages; a size that is not a 2 MiB multiple (the
// last byte is not at a 2 MiB boundary); 2 GiB, a multi-GiB-scale block larger than any slack a
// small allocation could hide in.
void check_placed()
{
    set_config(true);
    const struct {
        SIZE_T bytes;
        DWORD protection;
    } cases[] = {{2 * mib, 0x2004}, {2 * mib, 0x2404}, {6 * mib, 0x2004}, {2 * mib + 64 * kib, 0x2404}, {2 * gib, 0x2004}};
    for (const auto& c : cases) {
        const Call r = call(c.bytes, pool_type, 0x283, c.protection);
        std::printf("placed: size=0x%zx prot=0x%lx -> %p (error %lu)\n", c.bytes, c.protection, r.result, r.error);
        if (!record(r.result != nullptr, "placed_allocated")) continue;
        const auto base = reinterpret_cast<ULONG_PTR>(r.result);
        std::printf("  last byte %p\n", reinterpret_cast<void*>(base + (c.bytes - 1)));
        record(base % large_page == 0, "placed_aligned");
        record(below_limit(r.result, c.bytes), "placed_below_limit");
        record(check_block(r.result, c.bytes), "placed_block");
        char expected[160];
        std::snprintf(expected, sizeof(expected), "%s%p size=0x%llx ", placed_marker, r.result,
                      static_cast<unsigned long long>(c.bytes));
        record(count(r.log, expected) == 1 && count(r.log, refused_marker) == 0, "placed_logged");
        record(released(r.result), "placed_released");
    }
}

// Configuration off, and other shapes with it on: the call succeeds as before and does not go
// through the new branch (no below-2^40 log line). Placement is not asserted.
void check_unchanged()
{
    set_config(false);
    for (const DWORD protection : {DWORD{0x2004}, DWORD{0x2404}}) {
        const Call r = call(2 * mib, pool_type, 0x283, protection);
        std::printf("config off: size=0x200000 prot=0x%lx -> %p (error %lu)\n", protection, r.result, r.error);
        if (!record(r.result != nullptr, "off_allocated")) continue;
        record(reinterpret_cast<ULONG_PTR>(r.result) % large_page == 0, "off_aligned");
        record(check_block(r.result, 2 * mib), "off_block");
        record(count(r.log, "below 2^40") == 0, "off_not_constrained");
        record(released(r.result), "off_released");
    }
    set_config(true);
    const struct {
        DWORD type;
        ULONGLONG attributes;
        DWORD protection;
    } others[] = {{pool_type, 0x183, 0x22004}, {pages_64k_type, 0x181, PAGE_READWRITE}};
    for (const auto& o : others) {
        const Call r = call(2 * mib, o.type, o.attributes, o.protection);
        std::printf("other shape: type=0x%lx attr=0x%llx prot=0x%lx -> %p (error %lu)\n", o.type, o.attributes,
                    o.protection, r.result, r.error);
        if (!record(r.result != nullptr, "other_allocated")) continue;
        record(check_block(r.result, 2 * mib), "other_block");
        record(count(r.log, "below 2^40") == 0, "other_not_constrained");
        record(released(r.result), "other_released");
    }
    // An argument set the profile refuses keeps ERROR_INVALID_PARAMETER and never reaches the new branch.
    const Call refused = call(2 * mib + 4 * kib, pool_type, 0x283, 0x2004);
    record(!refused.result && refused.error == ERROR_INVALID_PARAMETER && count(refused.log, "below 2^40") == 0,
           "unaligned_size_refused");
    if (refused.result) VirtualFree(refused.result, 0, MEM_RELEASE);
}

// A refused traced call: NULL, a host error, the distinct diagnostic, nothing left allocated.
void expect_refused(SIZE_T bytes, const char* label, const char* null_check, const char* host_check,
                    const char* leak_check)
{
    const auto before = allocations();
    const Call r = call(bytes, pool_type, 0x283, 0x2004);
    const auto after = allocations();
    std::printf("%s: size=0x%zx -> %p (error %lu)\n", label, bytes, r.result, r.error);
    std::printf("  log: %s", r.log.empty() ? "(none)\n" : r.log.c_str());
    record(!r.result && r.error != 0, null_check);
    char size_text[64];
    std::snprintf(size_text, sizeof(size_text), "size=0x%llx attr=0x283 prot=0x2004", static_cast<unsigned long long>(bytes));
    char host_text[48];
    std::snprintf(host_text, sizeof(host_text), "(host error %lu)", r.error);
    record(count(r.log, refused_marker) == 1 && count(r.log, size_text) == 1 && count(r.log, host_text) == 1 &&
               count(r.log, placed_marker) == 0,
           host_check);
    record(nothing_new(before, after, bytes), leak_check);
    if (r.result) VirtualFree(r.result, 0, MEM_RELEASE);
}

// Every free region below 2^40 reserved by this process: the traced call is refused; with the
// blockers released, the same call is placed below 2^40.
void check_exhausted()
{
    set_config(true);
    constexpr SIZE_T bytes = 6 * mib;
    {
        LowRangeBlockers blockers(0, 0);
        std::printf("exhausted: %zu blocker reservation(s)\n", blockers.blocks.size());
        if (!record(blockers.complete && low_range_exhausted(), "exhausted_setup")) return;
        expect_refused(bytes, "exhausted", "exhausted_null", "exhausted_host_refusal", "exhausted_no_leak");
        // The same shape with the configuration off is not constrained: it is still placed (above 2^40).
        set_config(false);
        const Call off = call(bytes, pool_type, 0x283, 0x2004);
        std::printf("exhausted, config off: -> %p (error %lu)\n", off.result, off.error);
        if (record(off.result != nullptr, "exhausted_off_allocated")) {
            record(!below_limit(off.result, bytes) && count(off.log, "below 2^40") == 0, "exhausted_off_high");
            VirtualFree(off.result, 0, MEM_RELEASE);
        }
        set_config(true);
    }
    const Call again = call(bytes, pool_type, 0x283, 0x2004);
    std::printf("blockers released: -> %p (error %lu)\n", again.result, again.error);
    if (record(again.result != nullptr, "released_allocated")) {
        record(below_limit(again.result, bytes) && check_block(again.result, bytes), "released_below_limit");
        record(released(again.result), "released_released");
    }
}

// Only [2^40 - 4 MiB, 2^40) is free below 2^40: 4 MiB + 64 KiB does not fit and is refused; 4 MiB
// lands exactly there and its last byte is 2^40 - 1.
void check_boundary()
{
    set_config(true);
    constexpr SIZE_T window = 4 * mib;
    const ULONG_PTR hole = limit - window;
    MEMORY_BASIC_INFORMATION info{};
    if (!record(VirtualQuery(reinterpret_cast<void*>(hole), &info, sizeof(info)) == sizeof(info) &&
                    info.State == MEM_FREE &&
                    reinterpret_cast<ULONG_PTR>(info.BaseAddress) + info.RegionSize >= limit,
                "boundary_window_free"))
        return;
    LowRangeBlockers blockers(hole, window);
    std::printf("boundary: %zu blocker reservation(s), window %p-%p\n", blockers.blocks.size(),
                reinterpret_cast<void*>(hole), reinterpret_cast<void*>(limit - 1));
    if (!record(blockers.complete, "boundary_setup")) return;
    expect_refused(window + 64 * kib, "boundary overflow", "boundary_overflow_null", "boundary_overflow_host_refusal",
                   "boundary_overflow_no_leak");
    const Call r = call(window, pool_type, 0x283, 0x2404);
    std::printf("boundary: size=0x%zx -> %p (error %lu)\n", window, r.result, r.error);
    if (!record(r.result != nullptr, "boundary_allocated")) return;
    const auto base = reinterpret_cast<ULONG_PTR>(r.result);
    std::printf("  last byte %p\n", reinterpret_cast<void*>(base + (window - 1)));
    record(base == hole && base + (window - 1) == limit - 1, "boundary_last_byte");
    record(below_limit(r.result, window) && check_block(r.result, window), "boundary_block");
    record(released(r.result), "boundary_released");
}

// Sizes that cannot fit below 2^40 at all (no blockers): exactly 2^40 (would need base 0) and
// 2^40 + 2 MiB (larger than the range).
void check_impossible()
{
    set_config(true);
    expect_refused(limit, "size 2^40", "impossible_full_null", "impossible_full_host_refusal", "impossible_full_no_leak");
    expect_refused(limit + large_page, "size 2^40 + 2 MiB", "impossible_over_null", "impossible_over_host_refusal",
                   "impossible_over_no_leak");
}
}  // namespace

int main(int argc, char** argv)
{
    if (argc != 2) {
        std::printf("usage: xmem_startup_below_40_check <xmem.dll>\n");
        return 2;
    }
    HMODULE module = LoadLibraryA(argv[1]);
    if (module)
        xmem_virtual_alloc = reinterpret_cast<XMemVirtualAllocFn>(GetProcAddress(module, "XMemVirtualAlloc"));
    if (!xmem_virtual_alloc) {
        std::printf("could not load XMemVirtualAlloc from %s (error %lu)\n", argv[1], GetLastError());
        return 2;
    }
    char path[64];
    std::snprintf(path, sizeof(path), "xmem_startup_below_40.%lu.log", GetCurrentProcessId());
    log_path = path;
    DeleteFileA(path);
    SetEnvironmentVariableA("X2W_TRAP_LOG", path);

    check_unchanged();
    check_placed();
    check_exhausted();
    check_boundary();
    check_impossible();

    set_config(false);
    SetEnvironmentVariableA("X2W_TRAP_LOG", nullptr);
    DeleteFileA(path);
    std::printf("xmem_startup_below_40: %s (%d failed check(s))\n", failures ? "FAIL" : "PASS", failures);
    return failures ? 1 : 0;
}
