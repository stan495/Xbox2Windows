// Checks src/startup_stubs/descriptor_heap_type.h without a device: which heap type values device slot 15
// (GetDescriptorHandleIncrementSize) forwards to desktop and which it answers 0 for itself. The mutated runs
// are the negative controls: --mutate off-by-one also forwards the first value past the last type, and
// --mutate signed reads the type as a signed 32-bit value.
#include "startup_stubs/descriptor_heap_type.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <initializer_list>

namespace {

enum class Mutation { none, off_by_one, signed_compare };
Mutation mutation = Mutation::none;
int failures = 0;

bool accepted(std::uint32_t type)
{
    switch (mutation) {
    case Mutation::off_by_one: return type <= x2w_startup::desktop_descriptor_heap_type_count;
    case Mutation::signed_compare: return static_cast<std::int32_t>(type) < static_cast<std::int32_t>(x2w_startup::desktop_descriptor_heap_type_count);
    case Mutation::none: break;
    }
    return x2w_startup::is_valid_desktop_descriptor_heap_type(type);
}

void expect(std::uint32_t type, bool valid)
{
    if (accepted(type) != valid) {
        std::printf("FAIL type 0x%x: %s, expected %s\n", type, valid ? "refused" : "accepted", valid ? "accepted" : "refused");
        ++failures;
    }
}

}  // namespace

int main(int argc, char** argv)
{
    if (argc == 3 && std::strcmp(argv[1], "--mutate") == 0) {
        if (std::strcmp(argv[2], "off-by-one") == 0) mutation = Mutation::off_by_one;
        else if (std::strcmp(argv[2], "signed") == 0) mutation = Mutation::signed_compare;
        else return 2;
    } else if (argc != 1) {
        return 2;
    }
    // The four desktop types: CBV/SRV/UAV, sampler, RTV, DSV.
    for (std::uint32_t type = 0; type < 4; ++type) expect(type, true);
    // The first value past them, and values a title could pass: a console-only type, the top bit, all bits.
    for (std::uint32_t type : {4u, 5u, 7u, 8u, 0x10u, 0xFFu, 0x100u, 0x7FFFFFFFu, 0x80000000u, 0x80000001u, 0xFFFFFFFEu, 0xFFFFFFFFu})
        expect(type, false);
    static_assert(x2w_startup::desktop_descriptor_heap_type_count == 4);
    std::printf("startup_descriptor_heap_type_check: %d failed\n", failures);
    return failures ? 1 : 0;
}
