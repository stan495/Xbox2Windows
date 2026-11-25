// Checks the slot-10 tile plan for the 64 KiB standard layout 0x10a (src/startup_stubs/tile64_plan.h)
// without a device: the tile shape of every listed format, the descriptions the plan takes, and
// hand-worked plans at the shared-tile bounds and for one-texel-wide and one-texel-high chains.
// --mutate bc4: expect BC4 formats to have no tile shape.
// --mutate width: expect a 16385-texel-wide description to be taken.
// --mutate clamp: expect a 1024x1 RGBA8 chain to stop counting tiles once the height reaches 0.
// Each mutated run is a negative control and must fail.
#include "startup_stubs/tile64_plan.h"

#include <cstdio>
#include <cstring>
#include <initializer_list>
#include <vector>

namespace {

using x2w_startup::tile64::Plan;

const char* mutation = "";

bool mutated(const char* name)
{
    return std::strcmp(mutation, name) == 0;
}

int failures;

void expect(const char* name, bool condition)
{
    if (!condition) {
        std::printf("FAIL %s\n", name);
        ++failures;
    }
}

struct Shape {
    UINT width, height;
    std::initializer_list<DXGI_FORMAT> formats;
};

// Tile shapes by texel or block size, from the DXGI format list.
const Shape shapes[] = {
    {512, 256, {DXGI_FORMAT_BC1_TYPELESS, DXGI_FORMAT_BC1_UNORM, DXGI_FORMAT_BC1_UNORM_SRGB,
                DXGI_FORMAT_BC4_TYPELESS, DXGI_FORMAT_BC4_UNORM, DXGI_FORMAT_BC4_SNORM}},
    {256, 256, {DXGI_FORMAT_BC2_TYPELESS, DXGI_FORMAT_BC2_UNORM, DXGI_FORMAT_BC2_UNORM_SRGB,
                DXGI_FORMAT_BC3_TYPELESS, DXGI_FORMAT_BC3_UNORM, DXGI_FORMAT_BC3_UNORM_SRGB,
                DXGI_FORMAT_BC5_TYPELESS, DXGI_FORMAT_BC5_UNORM, DXGI_FORMAT_BC5_SNORM,
                DXGI_FORMAT_BC6H_TYPELESS, DXGI_FORMAT_BC6H_UF16, DXGI_FORMAT_BC6H_SF16,
                DXGI_FORMAT_BC7_TYPELESS, DXGI_FORMAT_BC7_UNORM, DXGI_FORMAT_BC7_UNORM_SRGB,
                DXGI_FORMAT_R8_TYPELESS, DXGI_FORMAT_R8_UNORM, DXGI_FORMAT_R8_UINT, DXGI_FORMAT_R8_SNORM,
                DXGI_FORMAT_R8_SINT, DXGI_FORMAT_A8_UNORM}},
    {256, 128, {DXGI_FORMAT_R8G8_TYPELESS, DXGI_FORMAT_R8G8_UNORM, DXGI_FORMAT_R8G8_UINT, DXGI_FORMAT_R8G8_SNORM,
                DXGI_FORMAT_R8G8_SINT, DXGI_FORMAT_R16_TYPELESS, DXGI_FORMAT_R16_FLOAT, DXGI_FORMAT_D16_UNORM,
                DXGI_FORMAT_R16_UNORM, DXGI_FORMAT_R16_UINT, DXGI_FORMAT_R16_SNORM, DXGI_FORMAT_R16_SINT}},
    {128, 128, {DXGI_FORMAT_R8G8B8A8_TYPELESS, DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_FORMAT_R8G8B8A8_UNORM_SRGB,
                DXGI_FORMAT_R8G8B8A8_UINT, DXGI_FORMAT_R8G8B8A8_SNORM, DXGI_FORMAT_R8G8B8A8_SINT,
                DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_FORMAT_B8G8R8X8_UNORM, DXGI_FORMAT_B8G8R8A8_TYPELESS,
                DXGI_FORMAT_B8G8R8A8_UNORM_SRGB, DXGI_FORMAT_B8G8R8X8_TYPELESS, DXGI_FORMAT_B8G8R8X8_UNORM_SRGB}},
};

// Expects `format` to have the tile shape `width` x `height`, or no shape when width is 0.
void tile_shape(DXGI_FORMAT format, UINT width, UINT height)
{
    UINT w = 0, h = 0;
    const bool known = x2w_startup::tile64::tile_shape(format, w, h);
    if (known == (width != 0) && (!known || (w == width && h == height))) return;
    std::printf("FAIL format %d: ", static_cast<int>(format));
    if (known) std::printf("%ux%u", w, h);
    else std::printf("no shape");
    if (width) std::printf(", expected %ux%u\n", width, height);
    else std::printf(", expected no shape\n");
    ++failures;
}

void tile_shapes()
{
    for (const Shape& shape : shapes) {
        for (DXGI_FORMAT format : shape.formats) {
            const bool bc4 = format >= DXGI_FORMAT_BC4_TYPELESS && format <= DXGI_FORMAT_BC4_SNORM;
            if (mutated("bc4") && bc4) tile_shape(format, 0, 0);
            else tile_shape(format, shape.width, shape.height);
        }
    }
    // Texel sizes no listed shape fits: none, 8 bytes and 1 bit.
    for (DXGI_FORMAT format : {DXGI_FORMAT_UNKNOWN, DXGI_FORMAT_X32_TYPELESS_G8X24_UINT, DXGI_FORMAT_R1_UNORM})
        tile_shape(format, 0, 0);
}

D3D12_RESOURCE_DESC texture(DXGI_FORMAT format, UINT64 width, UINT height, UINT16 slices, UINT16 mips)
{
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = width;
    desc.Height = height;
    desc.DepthOrArraySize = slices;
    desc.MipLevels = mips;
    desc.Format = format;
    desc.SampleDesc.Count = 1;
    desc.Layout = static_cast<D3D12_TEXTURE_LAYOUT>(0x10a);
    return desc;
}

void descriptions()
{
    const auto base = texture(DXGI_FORMAT_R8_UNORM, 256, 256, 1, 1);
    expect("base taken", Plan::supports(base));
    const auto taken = [&](const char* name, D3D12_RESOURCE_DESC desc) { expect(name, Plan::supports(desc)); };
    const auto refused = [&](const char* name, D3D12_RESOURCE_DESC desc) { expect(name, !Plan::supports(desc)); };
    auto d = base;
    d.Width = 16384; taken("width 16384", d);
    d.Width = 16385;
    if (mutated("width")) taken("width 16385 taken", d);
    else refused("width 16385", d);
    d.Width = 0x100000100ull; refused("width 0x100000100", d);   // 256 when cut to 32 bits
    d.Width = 0; refused("width 0", d);
    d = base; d.Height = 16384; taken("height 16384", d);
    d.Height = 16385; refused("height 16385", d);
    d.Height = 0; refused("height 0", d);
    d = base; d.DepthOrArraySize = 2048; taken("2048 slices", d);
    d.DepthOrArraySize = 0; refused("no slices", d);
    d = base; d.Width = d.Height = 16384; d.MipLevels = 16; taken("16 mips", d);
    d.MipLevels = 17; refused("17 mips", d);
    d.MipLevels = 0; refused("no mips", d);
    d = base; d.SampleDesc.Count = 2; refused("two samples", d);
    d = base; d.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE3D; refused("three dimensions", d);
    d.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE1D; refused("one dimension", d);
    d.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER; refused("buffer", d);
    d = base; d.Layout = static_cast<D3D12_TEXTURE_LAYOUT>(0x109); refused("layout 0x109", d);
    d.Layout = D3D12_TEXTURE_LAYOUT_64KB_STANDARD_SWIZZLE; refused("desktop standard swizzle", d);
    d.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN; refused("unknown layout", d);
    d = base; d.Format = DXGI_FORMAT_UNKNOWN; refused("unknown format", d);
}

struct Mip {
    UINT wide, high, start;
};

void plan(const char* name, const D3D12_RESOURCE_DESC& desc, UINT tile_width, UINT tile_height, UINT slice_tiles,
          std::vector<Mip> standard)
{
    if (!Plan::supports(desc)) return expect(name, false);
    const Plan p(desc);
    bool ok = p.tile_width == tile_width && p.tile_height == tile_height && p.mips == desc.MipLevels &&
              p.slices == desc.DepthOrArraySize && p.standard == standard.size() &&
              p.shared_tiles == (standard.size() < desc.MipLevels ? 1u : 0u) && p.slice_tiles == slice_tiles;
    for (UINT mip = 0; ok && mip < standard.size(); ++mip)
        ok = p.wide[mip] == standard[mip].wide && p.high[mip] == standard[mip].high && p.start[mip] == standard[mip].start;
    if (!ok) {
        std::printf("FAIL %s: tile %ux%u, %u standard of %u mips, %u shared, %u tiles per slice; mips:", name, p.tile_width,
                    p.tile_height, p.standard, p.mips, p.shared_tiles, p.slice_tiles);
        for (UINT mip = 0; mip < p.standard; ++mip) std::printf(" %ux%u@%u", p.wide[mip], p.high[mip], p.start[mip]);
        std::printf("\n");
        ++failures;
    }
}

// Hand-worked plans. A mip shares the first tile once it is at most half a tile wide and at
// most one tile high; the standard mips follow it from the smallest to the largest.
void plans()
{
    // 1024x1 RGBA8, 128x128 tiles: 1024, 512, 256 and 128 texels wide are standard (8, 4, 2, 1
    // tiles, each one tile high); 64x1 is half a tile wide and shares tile 0.
    std::vector<Mip> row = {{8, 1, 8}, {4, 1, 4}, {2, 1, 2}, {1, 1, 1}};
    if (mutated("clamp")) row = {{8, 1, 1}, {4, 0, 1}, {2, 0, 1}, {1, 0, 1}};
    plan("1024x1 RGBA8", texture(DXGI_FORMAT_R8G8B8A8_UNORM, 1024, 1, 1, 11), 128, 128, mutated("clamp") ? 9 : 16, row);
    // 1x1024 RGBA8: 1024, 512 and 256 texels high are standard (8, 4, 2 tiles, one tile wide);
    // 1x128 is one tile high and shares tile 0.
    plan("1x1024 RGBA8", texture(DXGI_FORMAT_R8G8B8A8_UNORM, 1, 1024, 1, 11), 128, 128, 15, {{1, 8, 7}, {1, 4, 3}, {1, 2, 1}});
    // BC4, 512x256 tiles. 1024x512: mip 1 (512x256) is a full tile, mip 2 (256x128) is half a
    // tile wide and shares tile 0. 6 slices do not change the per-slice count.
    plan("1024x512 BC4", texture(DXGI_FORMAT_BC4_UNORM, 1024, 512, 6, 11), 512, 256, 6, {{2, 2, 2}, {1, 1, 1}});
    // R16 (256x128 tiles), 516x128: mip 0 is 3x1 tiles and mip 1 (258x64) 2x1; mip 2 (129x32) is
    // one texel more than half a tile wide and takes a whole tile; mip 3 (64x16) shares tile 0.
    plan("516x128 R16", texture(DXGI_FORMAT_R16_UNORM, 516, 128, 1, 10), 256, 128, 7, {{3, 1, 4}, {2, 1, 2}, {1, 1, 1}});
    // B8G8R8A8 (128x128 tiles), 128x256: mip 0 is 1x2 tiles; mip 1 (64x128) is half a tile wide
    // and exactly one tile high, so it shares tile 0.
    plan("128x256 BGRA8", texture(DXGI_FORMAT_B8G8R8A8_UNORM, 128, 256, 1, 9), 128, 128, 3, {{1, 2, 1}});
    // A single mip two tiles wide: no shared tile, so the mip starts at tile 0.
    // A single mip that is not a whole number of tiles: R8 (256x256 tiles) 300x600 covers 2x3.
    plan("300x600 R8, one mip", texture(DXGI_FORMAT_R8_UNORM, 300, 600, 1, 1), 256, 256, 6, {{2, 3, 0}});
    plan("512x256 BC6H, one mip", texture(DXGI_FORMAT_BC6H_UF16, 512, 256, 1, 1), 256, 256, 2, {{2, 1, 0}});
}

} // namespace

int main(int argc, char** argv)
{
    if (argc == 3 && std::strcmp(argv[1], "--mutate") == 0 &&
        (std::strcmp(argv[2], "bc4") == 0 || std::strcmp(argv[2], "width") == 0 || std::strcmp(argv[2], "clamp") == 0)) {
        mutation = argv[2];
    } else if (argc != 1) {
        std::printf("usage: startup_tile64_plan_check [--mutate bc4|width|clamp]\n");
        return 2;
    }
    tile_shapes();
    descriptions();
    plans();
    if (failures) return 1;
    std::printf("startup_tile64_plan: ok\n");
    return 0;
}
