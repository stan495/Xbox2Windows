// Checks the BMP writer of the D3D12 stand-in's draw dumps (src/startup_stubs/draw_bmp.h).
// --mutate tight: expect a copy footprint whose row pitch equals the row size to be refused.
// --mutate wrap: expect a width whose row size needs more than 32 bits to be accepted.
// --mutate size-image: expect biSizeImage to be 0.
// Each mutated run is a negative control and must fail.
#include "../src/startup_stubs/draw_bmp.h"

#include <array>
#include <cstdio>
#include <cstring>

namespace {

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

bool headers(FILE* file, UINT width, UINT height)
{
    BITMAPFILEHEADER header{};
    BITMAPINFOHEADER info{};
    const DWORD image = width * height * 4;
    return std::fread(&header, sizeof(header), 1, file) == 1 && std::fread(&info, sizeof(info), 1, file) == 1 &&
           header.bfType == 0x4d42 && header.bfOffBits == 54 && header.bfSize == 54 + image &&
           info.biSize == 40 && info.biWidth == static_cast<LONG>(width) && info.biHeight == static_cast<LONG>(height) &&
           info.biPlanes == 1 && info.biBitCount == 32 && info.biCompression == BI_RGB &&
           info.biSizeImage == (mutated("size-image") ? 0 : image);
}

// A single nonblack texel falls between the old 4x sample points. Row padding is
// nonzero so accidentally interpreting the aligned pitch as image width is visible.
bool check(DXGI_FORMAT format, bool bgra)
{
    constexpr UINT width = 7, height = 3, pitch = 256;
    std::array<unsigned char, pitch * height> rgba;
    rgba.fill(0xee);
    for (UINT y = 0; y < height; ++y) std::memset(rgba.data() + y * pitch, 0, width * 4);
    rgba[5 * 4] = bgra ? 19 : 231;
    rgba[5 * 4 + 1] = 73;
    rgba[5 * 4 + 2] = bgra ? 231 : 19;
    FILE* output = std::tmpfile();
    if (!output) return false;
    bool ok = x2w_draw_bmp::write(output, rgba.data(), width, height, pitch, format);
    std::rewind(output);
    ok = ok && headers(output, width, height);
    for (UINT y = 0; y < height && ok; ++y) {
        for (UINT x = 0; x < width && ok; ++x) {
            std::array<unsigned char, 4> pixel{};
            const std::array<unsigned char, 4> expected = y == height - 1 && x == 5
                ? std::array<unsigned char, 4>{19, 73, 231, 255}
                : std::array<unsigned char, 4>{0, 0, 0, 255};
            ok = std::fread(pixel.data(), 1, pixel.size(), output) == pixel.size() && pixel == expected;
        }
    }
    ok = ok && std::fgetc(output) == EOF &&
         !x2w_draw_bmp::write(output, rgba.data(), width, height, 16, format) &&
         !x2w_draw_bmp::write(output, rgba.data(), width, height, pitch, DXGI_FORMAT_R32_FLOAT);
    std::fclose(output);
    return ok;
}

// Rows without padding: a footprint whose pitch is exactly four bytes per texel. Two
// texels per row, bottom row first in the file, red and blue swapped from RGBA.
void tight_rows()
{
    const unsigned char rgba[16] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16};
    const unsigned char expected[16] = {11, 10, 9, 255, 15, 14, 13, 255, 3, 2, 1, 255, 7, 6, 5, 255};
    FILE* output = std::tmpfile();
    if (!output) return expect("tmpfile", false);
    const bool written = x2w_draw_bmp::write(output, rgba, 2, 2, 8, DXGI_FORMAT_R8G8B8A8_UNORM);
    expect("pitch equal to the row size is accepted", written == !mutated("tight"));
    if (written) {
        std::rewind(output);
        unsigned char pixels[16]{};
        expect("tight headers", headers(output, 2, 2));
        expect("tight texels", std::fread(pixels, 1, sizeof(pixels), output) == sizeof(pixels) &&
                                   std::memcmp(pixels, expected, sizeof(pixels)) == 0 && std::fgetc(output) == EOF);
    }
    std::fclose(output);
}

// Calls the writer must refuse before it writes a byte. Each call gets its own empty file.
// A writer that goes on reads past the four texel bytes, so some mutants fault here instead
// of printing a FAIL line.
bool refuses(bool with_file, const unsigned char* texels, UINT width, UINT height, UINT pitch)
{
    FILE* output = with_file ? std::tmpfile() : nullptr;
    if (with_file && !output) return false;
    const bool written = x2w_draw_bmp::write(output, texels, width, height, pitch, DXGI_FORMAT_R8G8B8A8_UNORM);
    const bool empty = !output || std::ftell(output) == 0;
    if (output) std::fclose(output);
    return !written && empty;
}

void refused()
{
    const unsigned char texel[4] = {1, 2, 3, 4};
    expect("no file", refuses(false, texel, 1, 1, 4));
    expect("no texels", refuses(true, nullptr, 1, 1, 4));
    expect("zero width", refuses(true, texel, 0, 1, 4));
    expect("zero height", refuses(true, texel, 1, 0, 4));
    expect("pitch one byte short", refuses(true, texel, 1, 1, 3));
    // 0x40000001 texels are 0x1'00000004 bytes; cut to 32 bits that is one texel and fits.
    if (mutated("wrap")) expect("row size above 4 GiB is accepted", !refuses(true, texel, 0x40000001, 1, 256));
    else expect("row size above 4 GiB", refuses(true, texel, 0x40000001, 1, 256));
    // The file size field is 32 bits: 0x3ffffff3 rows of one texel need 0xffffffcc image bytes,
    // and with the 54 header bytes the file is past 4 GiB. 0x10000 x 0x10000 needs 16 GiB.
    expect("image plus headers above 4 GiB", refuses(true, texel, 1, 0x3ffffff3, 4));
    expect("image above 4 GiB", refuses(true, texel, 0x10000, 0x10000, 0x40000));
}

} // namespace

int main(int argc, char** argv)
{
    if (argc == 3 && std::strcmp(argv[1], "--mutate") == 0 &&
        (std::strcmp(argv[2], "tight") == 0 || std::strcmp(argv[2], "wrap") == 0 || std::strcmp(argv[2], "size-image") == 0)) {
        mutation = argv[2];
    } else if (argc != 1) {
        std::printf("usage: startup_draw_bmp_check [--mutate tight|wrap|size-image]\n");
        return 2;
    }
    expect("R8G8B8A8_UNORM", check(DXGI_FORMAT_R8G8B8A8_UNORM, false));
    expect("B8G8R8A8_UNORM", check(DXGI_FORMAT_B8G8R8A8_UNORM, true));
    expect("R8G8B8A8_UNORM_SRGB", check(DXGI_FORMAT_R8G8B8A8_UNORM_SRGB, false));
    expect("B8G8R8A8_UNORM_SRGB", check(DXGI_FORMAT_B8G8R8A8_UNORM_SRGB, true));
    tight_rows();
    refused();
    if (failures) return 1;
    std::printf("startup_draw_bmp: ok\n");
    return 0;
}
