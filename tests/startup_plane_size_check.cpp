// Checks src/startup_stubs/console_plane_size.h without a device: the sizes it answers for
// depth-with-stencil and multisampled descriptions, and the descriptions it leaves alone.
#include "startup_stubs/console_plane_size.h"

#include <cstdio>
#include <cstring>

namespace {
int failures;

D3D12_RESOURCE_DESC texture(unsigned format, unsigned width, unsigned height, unsigned samples = 1, unsigned slices = 1, unsigned mips = 1)
{
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = width;
    desc.Height = height;
    desc.DepthOrArraySize = static_cast<UINT16>(slices);
    desc.MipLevels = static_cast<UINT16>(mips);
    desc.Format = static_cast<DXGI_FORMAT>(format);
    desc.SampleDesc.Count = samples;
    return desc;
}

// desktop is the answer handed in; expected is the size that must come back (alignment 64 KiB).
void answered(const char* name, const D3D12_RESOURCE_DESC& desc, D3D12_RESOURCE_ALLOCATION_INFO desktop, unsigned long long expected)
{
    D3D12_RESOURCE_ALLOCATION_INFO info = desktop;
    const bool changed = x2w_startup::console_plane_size(desc, info);
    if (!changed || info.SizeInBytes != expected || info.Alignment != 0x10000) {
        std::printf("FAIL %s: changed %d size 0x%llx alignment 0x%llx, expected 0x%llx\n", name, changed,
                    static_cast<unsigned long long>(info.SizeInBytes), static_cast<unsigned long long>(info.Alignment), expected);
        ++failures;
    }
}

void left_alone(const char* name, const D3D12_RESOURCE_DESC& desc, D3D12_RESOURCE_ALLOCATION_INFO desktop)
{
    D3D12_RESOURCE_ALLOCATION_INFO info = desktop;
    if (x2w_startup::console_plane_size(desc, info) || info.SizeInBytes != desktop.SizeInBytes || info.Alignment != desktop.Alignment) {
        std::printf("FAIL %s: changed to size 0x%llx alignment 0x%llx\n", name, static_cast<unsigned long long>(info.SizeInBytes),
                    static_cast<unsigned long long>(info.Alignment));
        ++failures;
    }
}
}  // namespace

int main(int argc, char** argv)
{
    // --mutate packed: the expectations of a rule without tile padding (a negative control: the
    // check must then fail on the descriptions whose sizes are not whole tiles).
    const bool packed = argc > 1 && std::strcmp(argv[1], "--mutate") == 0;
    const auto pad = [packed](unsigned long long padded, unsigned long long unpadded) { return packed ? unpadded : padded; };

    // D32_FLOAT_S8X24 family, desktop 8 bytes a texel. Depth 4 bytes in 128 x 128 tiles plus
    // stencil 1 byte in 256 x 256 tiles.
    answered("d32s8 2048x2048", texture(19, 2048, 2048), {0x2000000, 0x10000}, 0x1400000);
    answered("d32s8 1024x1024", texture(19, 1024, 1024), {0x800000, 0x10000}, 0x500000);
    answered("d32s8 3840x2160", texture(19, 3840, 2160), {0x3FC0000, 0x10000}, pad(0x2850000, 0x27A0000));
    answered("d32s8 2560x1440", texture(20, 2560, 1440), {0x1E00000, 0x10000}, pad(0x12C0000, 0x11A0000));
    answered("d32s8 1280x720", texture(19, 1280, 720), {0x780000, 0x10000}, pad(0x4B0000, 0x480000));
    answered("d32s8 994x624", texture(19, 994, 624), {0x4F0000, 0x10000}, pad(0x340000, 0x300000));
    // The console's 16-bit depth with stencil has no desktop format; a 4-byte desktop stand-in is assumed.
    answered("d16s8 2560x1440", texture(118, 2560, 1440), {0xF00000, 0x10000}, pad(0xB40000, 0xAA0000));
    // A console-only format has no desktop answer: the validated shape is answered over the failure.
    answered("d16s8 after a desktop failure", texture(118, 2560, 1440), {~0ull, 0}, pad(0xB40000, 0xAA0000));
    answered("d32s8 at the largest dimensions", texture(19, 16384, 16384, 8, 2048), {0x200000000000, 0x10000}, 0x140000000000);
    // Multisampling: a tile holds 64 KiB of samples, and the 4 MiB desktop alignment goes.
    answered("rgba8 1x1 4 samples", texture(28, 1, 1, 4), {0x400000, 0x400000}, 0x10000);
    answered("d32s8 1x1 4 samples", texture(19, 1, 1, 4), {0x400000, 0x400000}, 0x20000);
    answered("rgba8 1920x1080 4 samples", texture(28, 1920, 1080, 4), {0x2000000, 0x400000}, pad(0x1FE0000, 0x1FB0000));
    answered("rgba16f 64x64 2 samples, 3 slices", texture(10, 64, 64, 2, 3), {0x400000, 0x400000}, 0x30000);
    // Additional MSAA colour families (R32G32B32A32, R16G16, R32, R16, R8) and D24_UNORM_S8_UINT.
    answered("rgba32f 100x100 4 samples", texture(2, 100, 100, 4), {0x400000, 0x400000}, pad(0x100000, 0xA0000));
    answered("rgba32f at the largest dimensions", texture(2, 16384, 16384, 8, 2048), {0x500000000000, 0x400000}, 0x400000000000);
    answered("rg16f 1920x1080 4 samples", texture(34, 1920, 1080, 4), {0x2000000, 0x400000}, pad(0x1FE0000, 0x1FB0000));
    answered("rg16_typeless 64x64 4 samples", texture(33, 64, 64, 4), {0x400000, 0x400000}, 0x10000);
    answered("r32f 1280x720 2 samples", texture(41, 1280, 720, 2), {0x800000, 0x400000}, pad(0x780000, 0x710000));
    answered("r32_typeless 128x128 4 samples", texture(39, 128, 128, 4), {0x400000, 0x400000}, 0x40000);
    answered("r16f 300x200 4 samples", texture(54, 300, 200, 4), {0x400000, 0x400000}, pad(0xC0000, 0x80000));
    answered("r8 256x256 4 samples", texture(61, 256, 256, 4), {0x400000, 0x400000}, 0x40000);
    answered("r8 300x300 2 samples", texture(61, 300, 300, 2), {0x400000, 0x400000}, pad(0x60000, 0x30000));
    answered("d24s8 1x1 4 samples", texture(45, 1, 1, 4), {0x400000, 0x400000}, 0x20000);
    answered("d24s8 2560x1440 4 samples", texture(45, 2560, 1440, 4), {0x4C00000, 0x400000}, pad(0x4880000, 0x4650000));
    answered("r24g8_typeless 1024x1024", texture(44, 1024, 1024), {0x800000, 0x10000}, 0x500000);
    // B8G8R8A8 at 4 bytes, and R16G16B16A16 at 8 where 32 bytes of samples tile as 64 x 32.
    answered("bgra8 1920x1080 4 samples", texture(87, 1920, 1080, 4), {0x2000000, 0x400000}, pad(0x1FE0000, 0x1FB0000));
    answered("rgba16f 1920x1080 4 samples", texture(10, 1920, 1080, 4), {0x4000000, 0x400000}, pad(0x3FC0000, 0x3F50000));
    // The console 16-bit depth with stencil multisampled: 8 bytes of depth samples in 128 x 64
    // tiles plus 4 bytes of stencil samples in 128 x 128 tiles; and the last of its three formats.
    answered("d16s8 2560x1440 4 samples", texture(118, 2560, 1440, 4), {~0ull, 0}, pad(0x2BC0000, 0x2A30000));
    answered("d16s8 format 120 2560x1440", texture(120, 2560, 1440), {~0ull, 0}, pad(0xB40000, 0xAA0000));
    // A description that asks for 64 KiB alignment is in the rule.
    D3D12_RESOURCE_DESC aligned = texture(19, 2048, 2048);
    aligned.Alignment = 0x10000;
    answered("d32s8 2048x2048 at 64 KiB alignment", aligned, {0x2000000, 0x10000}, 0x1400000);
    // The same size as desktop with a smaller alignment is still answered.
    answered("rgba8 1x1 4 samples, desktop size 64 KiB", texture(28, 1, 1, 4), {0x10000, 0x400000}, 0x10000);
    // The first and last format of each multisampled colour family, 128 x 128 with four samples:
    // 16, 8, 4, 2 and 1 bytes a texel give 1 MiB, 512, 256, 128 and 64 KiB.
    const struct {
        const char* name;
        unsigned format;
        unsigned long long expected;
    } ends[] = {
        {"r32g32b32a32_typeless 128x128 4 samples", 1, 0x100000},
        {"r32g32b32a32_sint 128x128 4 samples", 4, 0x100000},
        {"r16g16b16a16_typeless 128x128 4 samples", 9, 0x80000},
        {"r16g16b16a16_sint 128x128 4 samples", 14, 0x80000},
        {"r10g10b10a2_typeless 128x128 4 samples", 23, 0x40000},
        {"r8g8b8a8_sint 128x128 4 samples", 32, 0x40000},
        {"r16g16_sint 128x128 4 samples", 38, 0x40000},
        {"r32_sint 128x128 4 samples", 43, 0x40000},
        {"r16_typeless 128x128 4 samples", 53, 0x20000},
        {"r16_sint 128x128 4 samples", 59, 0x20000},
        {"r8_typeless 128x128 4 samples", 60, 0x10000},
        {"a8_unorm 128x128 4 samples", 65, 0x10000},
        {"b8g8r8x8_unorm_srgb 128x128 4 samples", 93, 0x40000},
    };
    for (const auto& end : ends) answered(end.name, texture(end.format, 128, 128, 4), {0x400000, 0x400000}, end.expected);

    // Left alone: formats with the same size on both sides, mip chains, other dimensions, sample
    // counts and formats outside the rule, and a console size that is not smaller.
    left_alone("rgba8 single sample", texture(28, 2560, 1440), {0xF00000, 0x10000});
    left_alone("rg16f single sample", texture(34, 1024, 1024), {0x400000, 0x10000});
    left_alone("d32 without stencil", texture(40, 2560, 1440), {0xF00000, 0x10000});
    left_alone("d24s8 single sample already smaller", texture(45, 2560, 1440), {0xF00000, 0x10000});
    left_alone("d32s8 with mips", texture(19, 1024, 1024, 1, 1, 3), {0xA80000, 0x10000});
    left_alone("bc1 4 samples", texture(71, 256, 256, 4), {0x400000, 0x400000});
    left_alone("rgb32f 4 samples", texture(6, 256, 256, 4), {0x400000, 0x400000});
    left_alone("rgba8 3 samples", texture(28, 256, 256, 3), {0x400000, 0x400000});
    left_alone("rgba32f 3 samples", texture(2, 256, 256, 3), {0x400000, 0x400000});
    left_alone("d32s8 already smaller", texture(19, 2048, 2048), {0x1400000, 0x10000});
    // A desktop failure is kept for desktop formats, valid shape or not.
    left_alone("d32s8 after a desktop failure", texture(19, 2048, 2048), {~0ull, 0});
    left_alone("d24s8 after a desktop failure", texture(45, 2048, 2048), {~0ull, 0});
    left_alone("rgba8 4 samples after a desktop failure", texture(28, 256, 256, 4), {~0ull, 0});
    left_alone("rgba32f 4 samples after a desktop failure", texture(2, 256, 256, 4), {~0ull, 0});
    // Shapes outside the rule keep whatever desktop said, including for the console-only formats.
    D3D12_RESOURCE_DESC shape = texture(19, 2048, 2048);
    shape.Width = ~0ull;  // would wrap while padding to a tile
    left_alone("d32s8 width all ones", shape, {~0ull, 0});
    left_alone("d32s8 width all ones, desktop answer", shape, {0x2000000, 0x10000});
    shape = texture(118, 16385, 64);
    left_alone("d16s8 wider than a 2D texture can be", shape, {~0ull, 0});
    shape = texture(118, 64, 0);
    left_alone("d16s8 zero height", shape, {~0ull, 0});
    shape = texture(118, 0, 64);
    left_alone("d16s8 zero width", shape, {~0ull, 0});
    shape = texture(118, 64, 16385);
    left_alone("d16s8 taller than a 2D texture can be", shape, {~0ull, 0});
    shape = texture(118, 64, 64, 1, 2049);
    left_alone("d16s8 more slices than a 2D array can have", shape, {~0ull, 0});
    shape = texture(118, 64, 64, 1, 1, 0);
    left_alone("d16s8 full mip chain (MipLevels 0)", shape, {~0ull, 0});
    left_alone("d32s8 full mip chain (MipLevels 0)", texture(19, 1024, 1024, 1, 1, 0), {0xA80000, 0x10000});
    shape = texture(118, 64, 64);
    shape.DepthOrArraySize = 0;
    left_alone("d16s8 zero slices", shape, {~0ull, 0});
    shape = texture(118, 64, 64, 4);
    shape.SampleDesc.Quality = 1;
    left_alone("d16s8 sample quality 1", shape, {~0ull, 0});
    shape = texture(19, 64, 64, 4);
    shape.SampleDesc.Quality = 1;
    left_alone("d32s8 sample quality 1", shape, {0x400000, 0x400000});
    shape = texture(118, 64, 64);
    shape.SampleDesc.Count = 0;
    left_alone("d16s8 zero samples", shape, {~0ull, 0});
    shape = texture(118, 64, 64);
    shape.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    left_alone("d16s8 row-major layout", shape, {~0ull, 0});
    shape = texture(19, 1024, 1024);
    shape.Layout = D3D12_TEXTURE_LAYOUT_64KB_UNDEFINED_SWIZZLE;
    left_alone("d32s8 64 KiB swizzle layout", shape, {0x800000, 0x10000});
    shape = texture(118, 64, 64);
    shape.Alignment = 0x1000;
    left_alone("d16s8 4 KiB alignment", shape, {~0ull, 0});
    shape = texture(19, 1024, 1024);
    shape.Alignment = 0x400000;
    left_alone("d32s8 4 MiB alignment", shape, {0x800000, 0x400000});
    D3D12_RESOURCE_DESC volume = texture(19, 64, 64);
    volume.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE3D;
    left_alone("3D texture", volume, {0x80000, 0x10000});
    D3D12_RESOURCE_DESC buffer{};
    buffer.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    buffer.Width = 0x100000;
    buffer.Height = 1;
    left_alone("buffer", buffer, {0x100000, 0x10000});

    if (failures) {
        std::printf("startup_plane_size: %d FAILED\n", failures);
        return 1;
    }
    std::printf("startup_plane_size: PASS (43 answered, 32 left alone)\n");
    return 0;
}
