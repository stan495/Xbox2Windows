"""Draw source-built BC7 blocks and tiled mode-6 mip levels with the translated copy program."""
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

import bcpack_mode6_vector as mode6
import bcpack_mode6_multiblock_vector as multi_mode6
from pixel_aot_check import assembly, lod_zero_sample, translate
from bc7_tiles import linearize_4k_s


def make_block(x, y):
    # Mode 6, one subset. Even endpoint bytes give zero for both endpoint P bits.
    start = (x * 8, y * 6, 24, 60)
    end = (224, 196, 152, 254)
    bits = 1 << 6
    shift = 7
    for a, b in zip(start, end):
        bits |= ((a >> 1) | ((b >> 1) << 7)) << shift
        shift += 14
    shift += 2
    colors = bytearray()
    weights = (0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64)
    for index, weight in enumerate(weights):
        bits |= index << shift
        shift += 3 if index == 0 else 4
        colors.extend(((64 - weight) * a + weight * b + 32) >> 6 for a, b in zip(start, end))
    return bits.to_bytes(16, "little"), colors


def fixture(columns=20, rows=18):
    # Cross both 4 KiB tile boundaries and exercise padded D3D12 upload rows.
    width, height = columns * 4, rows * 4
    tile_pitch = (columns + 15) // 16
    tile_count = tile_pitch * ((rows + 15) // 16)
    tiled = bytearray(tile_count * 4096)
    linear = bytearray(columns * rows * 16)
    expected = bytearray(width * height * 4)
    for tile in range(tile_count):
        for index in range(256):
            x = (tile % tile_pitch) * 16 + ((index >> 2) & 3) + ((index >> 3) & 4) + ((index >> 4) & 8)
            y = (tile // tile_pitch) * 16 + (index & 3) + ((index >> 2) & 4) + ((index >> 3) & 8)
            if x >= columns or y >= rows:
                continue
            block, colors = make_block(x, y)
            src = tile * 4096 + index * 16
            tiled[src:src+16] = block
            dst = (y * columns + x) * 16
            linear[dst:dst+16] = block
            for row in range(4):
                pos = ((y * 4 + row) * width + x * 4) * 4
                expected[pos:pos+16] = colors[row*16:(row+1)*16]
    converted = linearize_4k_s(tiled, width, height)
    assert converted == linear
    return struct.pack("<4sII", b"BC7C", width, height), converted, expected


def check_all_modes(work: Path, renderer: str, backend: str, shader: Path) -> None:
    case = work / "all-modes.case"
    generator = Path(__file__).resolve().parents[1] / "tools" / "bc7_blocks.py"
    subprocess.run([sys.executable, "-B", generator, case], check=True)
    draw = [renderer, backend, "--check-bc7", shader, case]
    subprocess.run(draw, check=True)

    # The first eight blocks cycle through modes 0-7. Flip the low rotation bit
    # of the mode-4 block so its red and alpha channels swap at pixel (16, 0).
    changed = bytearray(case.read_bytes())
    changed[struct.calcsize("<4sII") + 4 * 16] ^= 1 << 5
    case.write_bytes(changed)
    wrong = subprocess.run(draw, capture_output=True, text=True)
    if wrong.returncode != 1 or "pixel_mismatch x=16 y=0" not in wrong.stderr.splitlines():
        raise AssertionError(f"Expected a mode-4 rotation mismatch at (16, 0): {wrong.stderr}")


def write_patterned_mip(work: Path, width: int, height: int) -> Path:
    # Keep the green endpoint (six times the second argument) within a byte.
    block, colors = make_block(width.bit_length(), min(height, 32))
    columns = (width + 3) // 4
    pattern = [bytes(colors[row * 16:row * 16 + 16]) for row in range(4)]
    visible = b"".join((pattern[y % 4] * columns)[:width * 4] for y in range(height))
    case = work / f"mip-{width}x{height}.case"
    case.write_bytes(struct.pack("<4sII", b"BC7C", width, height) +
                     block * (columns * ((height + 3) // 4)) + visible)
    return case


def check_band_limits(work: Path, compiler: str, renderer: str, backend: str, copy_source: str) -> None:
    # Replace chosen pixels of a magnified two-texel mip with a texel fetched
    # at its center, so every adapter returns the same readback. A center is
    # within 1/512 texel below the boundary from pixel 2044 of 4096 (2048x)
    # and exactly on that limit at pixel 255 of 512 (256x).
    first, second = "first", "second"
    mixed = "float4(first.r, second.g, first.b, second.a)"
    controls = (
        # name, vertical, replaced pixels, returned color, expected first mismatch
        ("inside", False, "(size.x == 4096 && pixel.x == 2044) || (size.x == 512 && pixel.x == 255)", second, None),
        ("outside", False, "size.x == 4096 && pixel.x == 2043", second, "pixel_mismatch x=2043 y=0"),
        ("outside-tie", False, "size.x == 512 && pixel.x == 254", second, "pixel_mismatch x=254 y=0"),
        ("mixed", False, "size.x == 4096 && pixel.x == 2044", mixed, "pixel_mismatch x=2044 y=0"),
        ("previous", False, "size.x == 4096 && pixel.x == 2048", first, "pixel_mismatch x=2048 y=0"),
        ("edge", False, "size.x == 4096 && pixel.x == 4095", first, "pixel_mismatch x=4095 y=0"),
        ("inside-y", True, "size.y == 4096 && pixel.y == 2044", second, None),
        ("outside-y", True, "size.y == 4096 && pixel.y == 2043", second, "pixel_mismatch x=0 y=2043"),
    )
    for name, vertical, condition, color, mismatch in controls:
        centers = ("0.5, 0.25", "0.5, 0.75") if vertical else ("0.25, 0.5", "0.75, 0.5")
        text, shader = work / f"band-{name}.hlsl", work / f"band-{name}.cso"
        text.write_text("#define main copy_pixel\n" + copy_source + f"""
#undef main
float4 main(float2 uv : TEXCOORD0, float4 position : SV_Position) : SV_Target0 {{
    float4 original = copy_pixel(uv);
    float4 first = copy_pixel(float2({centers[0]}));
    float4 second = copy_pixel(float2({centers[1]}));
    uint2 size = uint2(round(1.0 / float2(ddx(uv.x), ddy(uv.y))));
    uint2 pixel = uint2(position.xy);
    if ({condition}) return {color};
    return original;
}}
""", encoding="ascii")
        subprocess.run([compiler, "-T", "ps_6_0", "-E", "main", "-Fo", shader, text], check=True)
        width, height = (4, 4096) if vertical else (4096, 4)
        levels = [write_patterned_mip(work, max(1, width >> level), max(1, height >> level)) for level in (11, 12)]
        result = subprocess.run([renderer, backend, "--check-bc7-tail", shader, "11",
                                 "--base-size", str(width), str(height), *levels], capture_output=True, text=True)
        if mismatch is None:
            if result.returncode:
                raise AssertionError(f"Band control {name}: the next texel must be accepted:\n{result.stderr}")
        elif result.returncode != 1 or mismatch not in result.stderr.splitlines():
            raise AssertionError(f"Band control {name}: expected {mismatch}:\n{result.stderr}")
def solid_mip(size: int, stored: bytes, expected: bytes) -> bytes:
    # Mode 6, identical endpoints and zero p bits give the same color at all indices.
    assert size in (4, 8) and len(stored) == len(expected) == 4 and all(value % 2 == 0 for value in stored)
    bits = 1 << 6
    for channel, value in enumerate(stored):
        endpoint = value >> 1
        bits |= (endpoint | (endpoint << 7)) << (7 + 14 * channel)
    return (struct.pack("<4sII", b"BC7C", size, size) + bits.to_bytes(16, "little") * (size // 4) ** 2
            + expected * size ** 2)


def check_lod_zero(work: Path, compiler: str, renderer: str, backend: str, implicit: Path) -> None:
    source = work / "lod-zero.hlsl"
    explicit = work / "lod-zero.cso"
    source.write_text(translate(lod_zero_sample()), encoding="ascii")
    subprocess.run([compiler, "-T", "ps_6_0", "-E", "main", "-Fo", explicit, source], check=True)
    base = work / "lod-zero-base.case"
    lower = work / "lod-zero-lower.case"
    red, green = bytes((254, 0, 0, 254)), bytes((0, 254, 0, 254))
    base.write_bytes(solid_mip(8, red, red))
    # A 4x4 viewport selects mip 1 implicitly for this 8x8 texture.
    lower.write_bytes(solid_mip(4, green, green))
    draw = [renderer, backend, "--check-bc7-mip1"]
    subprocess.run([*draw, implicit, base, lower], check=True)
    lower.write_bytes(solid_mip(4, green, red))
    subprocess.run([*draw, explicit, base, lower], check=True)
    wrong = subprocess.run([*draw, implicit, base, lower], capture_output=True, text=True)
    if wrong.returncode != 1 or "pixel_mismatch x=0 y=0" not in wrong.stderr.splitlines():
        raise AssertionError("The implicit sample did not distinguish mip 1 from explicit mip 0")


def main():
    compiler, renderer, backend = sys.argv[1:]
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        shader, source = work / "copy.cso", work / "copy.hlsl"
        source.write_text(translate(assembly(False)), encoding="ascii")
        subprocess.run([compiler, "-T", "ps_6_0", "-E", "main", "-Fo", shader, source], check=True)
        header, rows, expected = fixture()
        case, output = work / "input.case", work / "actual.rgba"
        case.write_bytes(header + rows + expected)
        draw = [renderer, backend, "--check-bc7", shader, case]
        result = subprocess.run([*draw, output])
        if result.returncode:
            return result.returncode
        assert output.read_bytes() == expected
        check_lod_zero(work, compiler, renderer, backend, shader)
        check_all_modes(work, renderer, backend, shader)
        vector = work / "bcpack-mode6.case"
        vector.write_bytes(struct.pack("<4sII", b"BC7C", 4, 4) + mode6.BLOCK + mode6.RGBA)
        subprocess.run([renderer, backend, "--check-bc7", shader, vector], check=True)
        multi_vector = work / "bcpack-mode6-multiblock.case"
        multi_vector.write_bytes(struct.pack("<4sII", b"BC7C", 8, 4) + multi_mode6.BLOCKS + multi_mode6.RGBA)
        subprocess.run([renderer, backend, "--check-bc7", shader, multi_vector], check=True)
        multi_swapped = work / "bcpack-mode6-multiblock-swapped.case"
        multi_swapped.write_bytes(struct.pack("<4sII", b"BC7C", 8, 4) +
                                  multi_mode6.BLOCKS[16:] + multi_mode6.BLOCKS[:16] + multi_mode6.RGBA)
        wrong = subprocess.run([renderer, backend, "--check-bc7", shader, multi_swapped],
                               capture_output=True, text=True)
        if wrong.returncode != 1 or "pixel_mismatch" not in wrong.stderr:
            raise AssertionError("Swapping multi-block mode-6 blocks must fail readback")
        multi_mut = work / "bcpack-mode6-multiblock-mut.case"
        mut_b1 = multi_mode6.BLOCKS[16:-1] + bytes([multi_mode6.BLOCKS[-1] ^ 0xf0])
        multi_mut.write_bytes(struct.pack("<4sII", b"BC7C", 8, 4) +
                              multi_mode6.BLOCKS[:16] + mut_b1 + multi_mode6.RGBA)
        wrong = subprocess.run([renderer, backend, "--check-bc7", shader, multi_mut],
                               capture_output=True, text=True)
        if wrong.returncode != 1 or "pixel_mismatch x=7 y=3" not in wrong.stderr.splitlines():
            raise AssertionError(f"Mutating second block selector must fail at (7, 3): {wrong.stderr}")
        lower = work / "mip1.case"
        header, rows, expected = fixture(10, 9)
        lower.write_bytes(header + rows + expected)
        mip_draw = [renderer, backend, "--check-bc7-mip1", shader, case, lower]
        subprocess.run(mip_draw, check=True)
        lower.write_bytes(header + rows[16:] + rows[:16] + expected)
        wrong = subprocess.run(mip_draw, capture_output=True, text=True)
        if wrong.returncode != 1 or "pixel_mismatch" not in wrong.stderr:
            raise AssertionError("Moving BC7 blocks must change the readback")
        tail = []
        for level, blocks in enumerate((16, 8, 4, 2, 1), start=3):
            mip = work / f"resident{level}.case"
            mip.write_bytes(b"".join(fixture(blocks, blocks)))
            tail.append(mip)
        tail.extend([write_patterned_mip(work, 2, 2), write_patterned_mip(work, 1, 1)])
        chain = [renderer, backend, "--check-bc7-tail", shader, "3", *tail]
        subprocess.run(chain, check=True)
        header, rows, expected = fixture(4, 4)
        tail[2].write_bytes(header + rows[16:] + rows[:16] + expected)
        wrong = subprocess.run(chain, capture_output=True, text=True)
        if wrong.returncode != 1 or "pixel_mismatch" not in wrong.stderr:
            raise AssertionError("An incorrect middle mip must fail verification")
        # A 4096x4 strip uses all thirteen mip levels while staying small.
        narrow = [write_patterned_mip(work, 4096 >> level, max(1, 4 >> level)) for level in range(13)]
        subprocess.run([renderer, backend, "--check-bc7-mip1", shader, *narrow[:2]], check=True)
        narrow_chain = [renderer, backend, "--check-bc7-tail", shader, "0", *narrow]
        subprocess.run(narrow_chain, check=True)
        repeated = subprocess.run([*narrow_chain, narrow[-1]], capture_output=True, text=True)
        if repeated.returncode != 1 or "Expected consecutive BC7 mip sizes" not in repeated.stderr:
            raise AssertionError("A BC7 mip chain must end at 1x1")
        base = subprocess.run([renderer, backend, "--check-bc7", shader, narrow[-2]],
                              capture_output=True, text=True)
        if base.returncode != 1 or "Expected block-aligned BC7 base dimensions" not in base.stderr:
            raise AssertionError("A 2x1 BC7 level is valid only as a subresource")
        stale = bytearray(narrow[-1].read_bytes())
        stale[-4] ^= 0xff  # Change only the expected red channel of the 1x1 mip.
        narrow[-1].write_bytes(stale)
        wrong = subprocess.run(narrow_chain, capture_output=True, text=True)
        if wrong.returncode != 1 or "pixel_mismatch x=0 y=0" not in wrong.stderr.splitlines():
            raise AssertionError("The final 1x1 draw must check its visible texel")
        # At mip 4, the 28-texel axis has one visible texel but a 1.75-texel
        # unrounded span. Exercise both axes and a view starting at mip 2.
        for width, height, first in ((28, 128, 2), (128, 28, 0)):
            levels = [write_patterned_mip(work, max(1, width >> level), max(1, height >> level))
                      for level in range(first, 8)]
            subprocess.run([renderer, backend, "--check-bc7-tail", shader, str(first), *levels], check=True)
        # At mip 5 of 92x4 the resident width is 2; its successor must use
        # the 1x1 mip even though 92 / 64 is still 1.4375.
        for width, height, first in ((4096, 4, 11), (4, 4096, 11), (4096, 4, 12),
                                     (92, 4, 5), (4, 92, 5)):
            levels = [write_patterned_mip(work, max(1, width >> level), max(1, height >> level))
                      for level in range(first, max(width, height).bit_length())]
            draw = [renderer, backend, "--check-bc7-tail", shader, str(first),
                    "--base-size", str(width), str(height), *levels]
            result = subprocess.run(draw, capture_output=True, text=True)
            if result.returncode:
                raise AssertionError(f"Tail draw failed for {width}x{height} at mip {first}:\n"
                                     f"{result.stdout}\n{result.stderr}")
            for mip in range(first + len(levels)):
                pixels = max(1, width >> mip) * max(1, height >> mip)
                expected_draw = f"requested_mip={mip} sampled_mip={max(first, mip)} pixels={pixels} "
                if expected_draw not in result.stdout:
                    raise AssertionError(f"Missing verified draw for {width}x{height}: {expected_draw}")
        # Drawing the 2x1 mip of 4096x4 at 4096x4 puts pixels 2044-2047 within
        # 1/512 texel of the texel boundary, where 8-bit fixed-point addressing
        # may select the second texel. A 1/64-texel shift moves the first changed
        # pixel to 2016 (2012 with 8-bit snapping), which must still be rejected.
        strip = [write_patterned_mip(work, 4096 >> level, 1) for level in (11, 12)]
        strip_args = ["11", "--base-size", "4096", "4", *strip]
        shifted_source, shifted = work / "shifted.hlsl", work / "shifted.cso"
        shifted_source.write_text("#define main copy_pixel\n" + source.read_text(encoding="ascii") + """
#undef main
float4 main(float2 uv : TEXCOORD0) : SV_Target0 {
    return copy_pixel(uv + float2(1.0 / 128, 0));
}
""", encoding="ascii")
        subprocess.run([compiler, "-T", "ps_6_0", "-E", "main", "-Fo", shifted, shifted_source], check=True)
        wrong = subprocess.run([renderer, backend, "--check-bc7-tail", shifted, *strip_args],
                               capture_output=True, text=True)
        allowed = [f"pixel_mismatch x={x} y=0" for x in range(2012, 2017)]
        if wrong.returncode != 1 or not any(line in allowed for line in wrong.stderr.splitlines()):
            raise AssertionError(f"A texel shift beyond the snapping band must fail near x=2016:\n{wrong.stderr}")
        check_band_limits(work, compiler, renderer, backend, source.read_text(encoding="ascii"))
        # The 29x19 fallback has an exact point-sample tie at (14, 0).
        # Combine channels from the two texels on either side only at that pixel.
        boundary_levels = [write_patterned_mip(work, max(1, 116 >> level), max(1, 76 >> level))
                           for level in range(3, 7)]
        boundary_args = ["3", "--base-size", "116", "76", *boundary_levels]
        subprocess.run([renderer, backend, "--check-bc7-tail", shader, *boundary_args], check=True)
        source.write_text("#define main copy_pixel\n" + source.read_text(encoding="ascii") + """
#undef main
float4 main(float2 uv : TEXCOORD0) : SV_Target0 {
    float4 original = copy_pixel(uv);
    float4 left = copy_pixel(uv - float2(0.25 / 14, 0));
    float4 right = copy_pixel(uv + float2(0.25 / 14, 0));
    if (abs(uv.x - 0.5) < 0.00001 && abs(uv.y - 0.5 / 19) < 0.00001)
        return float4(left.r, right.g, left.b, right.a);
    return original;
}
""", encoding="ascii")
        mixed = work / "mixed.cso"
        subprocess.run([compiler, "-T", "ps_6_0", "-E", "main", "-Fo", mixed, source], check=True)
        wrong = subprocess.run([renderer, backend, "--check-bc7-tail", mixed, *boundary_args],
                               capture_output=True, text=True)
        if wrong.returncode != 1 or "pixel_mismatch x=14 y=0" not in wrong.stderr.splitlines():
            raise AssertionError("An exact boundary must still match one whole reference texel")
        # The same 1x1 data is also legal at mip 12 of a tall strip, but 4x4
        # has only three mip levels and cannot have mip 12 at all.
        tiny = write_patterned_mip(work, 1, 1)
        start = [renderer, backend, "--check-bc7-tail", shader, "12", "--base-size"]
        subprocess.run([*start, "4", "4096", tiny], check=True)
        invalid = subprocess.run([*start, "4", "4", tiny], capture_output=True, text=True)
        if invalid.returncode != 1 or "incompatible with the first resident mip" not in invalid.stderr:
            raise AssertionError("A declared texture must actually contain the supplied level")
        transposed = write_patterned_mip(work, 1, 2)
        wrong = subprocess.run([renderer, backend, "--check-bc7-tail", shader, "11",
                                "--base-size", "4096", "4", transposed], capture_output=True, text=True)
        if wrong.returncode != 1 or "incompatible with the first resident mip" not in wrong.stderr:
            raise AssertionError("Reject a resident mip with transposed dimensions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
