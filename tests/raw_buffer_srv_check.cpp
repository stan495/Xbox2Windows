// make_raw_buffer_srv (src/raw_buffer_srv.h) without a device. vm_raw_srv_warp resolves two views
// inside one 128 KiB buffer and refuses a misaligned address, the address one past the buffer and a
// count one element too long. This check covers what that fixture leaves open: views that end exactly
// at the end of the buffer, an unaligned address made aligned by the first element, a buffer width
// that is not whole dwords, offsets, widths and first elements above 4 GiB, every field of the view,
// and each remaining refusal. Expected values are worked by hand.
// --mutate first-32: expects the 32-bit truncation of a first element above 4 GiB; it must fail.
#include "raw_buffer_srv.h"

#include <cstdio>
#include <cstring>

namespace {
int failures;

struct Accept {
    const char* name;
    D3D12_GPU_VIRTUAL_ADDRESS base;
    UINT64 width;
    UINT64 offset;  // address - base
    UINT64 first_element;
    UINT count;
    UINT64 view_first;
};

struct Refuse {
    const char* name;
    D3D12_GPU_VIRTUAL_ADDRESS base;
    UINT64 width;
    D3D12_GPU_VIRTUAL_ADDRESS address;
    UINT64 first_element;
    UINT count;
};

void accept(const Accept& c)
{
    // Start from a dirty description: the result must not keep any earlier field.
    D3D12_SHADER_RESOURCE_VIEW_DESC got;
    std::memset(&got, 0xcd, sizeof(got));
    const HRESULT hr = make_raw_buffer_srv(c.base, c.width, c.base + c.offset, c.first_element, c.count, got);
    if (hr != S_OK || got.Format != DXGI_FORMAT_R32_TYPELESS || got.ViewDimension != D3D12_SRV_DIMENSION_BUFFER ||
        got.Shader4ComponentMapping != D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING ||
        got.Buffer.FirstElement != c.view_first || got.Buffer.NumElements != c.count ||
        got.Buffer.StructureByteStride != 0 || got.Buffer.Flags != D3D12_BUFFER_SRV_FLAG_RAW) {
        std::printf("FAIL %s: hr=0x%08lx format=%d dimension=%d mapping=0x%x first=0x%llx elements=0x%x stride=%u "
                    "flags=%d, expected S_OK first=0x%llx elements=0x%x\n",
                    c.name, static_cast<unsigned long>(hr), got.Format, got.ViewDimension,
                    got.Shader4ComponentMapping, static_cast<unsigned long long>(got.Buffer.FirstElement),
                    got.Buffer.NumElements, got.Buffer.StructureByteStride, got.Buffer.Flags,
                    static_cast<unsigned long long>(c.view_first), c.count);
        ++failures;
    }
}

void refuse(const Refuse& c)
{
    D3D12_SHADER_RESOURCE_VIEW_DESC got{};
    const HRESULT hr = make_raw_buffer_srv(c.base, c.width, c.address, c.first_element, c.count, got);
    if (hr != E_INVALIDARG) {
        std::printf("FAIL %s: hr=0x%08lx first=0x%llx elements=0x%x, expected E_INVALIDARG\n", c.name,
                    static_cast<unsigned long>(hr), static_cast<unsigned long long>(got.Buffer.FirstElement),
                    got.Buffer.NumElements);
        ++failures;
    }
}
} // namespace

int main(int argc, char** argv)
{
    const bool first_32 = argc == 3 && !std::strcmp(argv[1], "--mutate") && !std::strcmp(argv[2], "first-32");
    if (argc != 1 && !first_32) {
        std::printf("usage: raw_buffer_srv_check [--mutate first-32]\n");
        return 64;
    }

    constexpr D3D12_GPU_VIRTUAL_ADDRESS low = 0x10000;
    constexpr D3D12_GPU_VIRTUAL_ADDRESS high = 0x200000000ull;
    const Accept accepted[]{
        // The whole 0x100-byte buffer: 0x40 elements from element 0.
        {"whole buffer", high, 0x100, 0, 0, 0x40, 0},
        // From byte 0x10, element 4 is byte 0x20: 0x38 elements reach byte 0x100, the end.
        {"view ending at the buffer end", low, 0x100, 0x10, 4, 0x38, 8},
        // The last 16 bytes: element 0x3c, 4 elements.
        {"last aligned vector", low, 0x100, 0xf0, 0, 4, 0x3c},
        // Byte 4 is not 16-aligned, but element 3 from it is byte 0x10: first element 4.
        {"unaligned address, aligned first element", low, 0x100, 4, 3, 1, 4},
        // A 0x102-byte buffer holds 0x40 whole elements; the trailing 2 bytes are not one.
        {"width not whole dwords", low, 0x102, 0, 0, 0x40, 0},
        // From byte 0x1'0000'0000 of a 0x5'0000'0010-byte buffer, 0x1'0000'0004 elements remain.
        // Element 0x1'0000'0000 is byte 0x5'0000'0000: first element 0x1'4000'0000, 4 elements to the end.
        {"offset and first element above 4 GiB", high, 0x500000010ull, 0x100000000ull, 0x100000000ull, 4,
         first_32 ? 0x40000000ull : 0x140000000ull},
        // From byte 0x10 of a 0x4'0000'0010-byte buffer, exactly 0x1'0000'0000 elements remain.
        // Element 0xffff'fffc is byte 0x4'0000'0000: first element 0x1'0000'0000, 4 elements to the end.
        {"0x1'0000'0000 elements remain", low, 0x400000010ull, 0x10, 0xfffffffcull, 4, 0x100000000ull},
    };
    for (const Accept& c : accepted) accept(c);

    const Refuse refused[]{
        // GetGPUVirtualAddress returns 0 for a resource that is not a buffer.
        {"null base", 0, 0x100, 0x10, 0, 1},
        {"address below the base", low, 0x100, low - 0x10, 0, 1},
        {"address past the buffer end", low, 0x100, low + 0x110, 0, 1},
        {"zero elements", low, 0x100, low, 0, 0},
        {"one element past the buffer end", low, 0x100, low + 0x10, 4, 0x39},
        {"width not whole dwords, one element too many", low, 0x102, low, 0, 0x41},
        // 0x40 elements are available; element 0x44 lies past them.
        {"first element past the buffer end", low, 0x100, low, 0x44, 1},
        // Byte 0 is aligned, but element 1 is byte 4.
        {"aligned address, unaligned first element", low, 0x100, low, 1, 1},
        // From byte 0x20 of a 0x1'0000'0010-byte buffer, 0x3fff'fffc elements remain.
        {"one element past a buffer above 4 GiB", low, 0x100000010ull, low + 0x20, 0, 0x3ffffffd},
    };
    for (const Refuse& c : refused) refuse(c);

    if (failures) {
        std::printf("raw_buffer_srv: %d FAILED\n", failures);
        return 1;
    }
    std::printf("raw_buffer_srv: PASS (7 resolved, 9 refused)\n");
    return 0;
}
