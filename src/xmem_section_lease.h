#pragma once

#include <windows.h>
#include <cstdint>

inline constexpr std::uint32_t x2w_xmem_section_lease_version = 1;

// The caller closes section after importing it. It retains the lease through
// its last submitted GPU access and releases it after the completion fence.
struct X2WXMemSectionLease {
    HANDLE section;
    std::uint64_t id;
    std::uint64_t section_offset;
    std::uint64_t window_bytes;
    std::uint64_t first_byte;
};
static_assert(sizeof(X2WXMemSectionLease) == 40);
