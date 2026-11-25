"""Translate sampled pixel programs that also read attr0.z and attr0.w as plain values.

Each gfx1013 program samples t0 at the interpolated attr0.xy and scales the sample by the
interpolated attr0.z and attr0.w: red by z, green by w, blue by z * w, alpha unscaled.
Two forms interpolate the coordinates in whole-quad mode for an implicit-LOD sample, without
and with a zero bias ahead of them; the third samples with image_sample_lz in the entry mask.
All three interpolate z and w in the entry mask.

The test vertex shader gives each vertex its own z and w, so both vary across the 8x8 target.
Texels, pixel-center attributes and products are short binary fractions, so the CPU reference
below is exact and the readback must match bit for bit.
"""

from fractions import Fraction
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
import pixel_aot as aot  # noqa: E402

SIDE = 8
SAMPLER = (1, 2, 3, 4)
COORDINATES = [
    "s_mov_b32 m0, s24",
    "v_interp_p1_f32_e32 v8, v0, attr0.x",
    "v_interp_p1_f32_e32 v9, v0, attr0.y",
    "v_interp_p2_f32_e32 v9, v1, attr0.y",
    "v_interp_p2_f32_e32 v8, v1, attr0.x",
]
SCALES = [
    "v_interp_p1_f32_e32 v2, v0, attr0.z",
    "v_interp_p1_f32_e32 v3, v0, attr0.w",
    "v_interp_p2_f32_e32 v2, v1, attr0.z",
    "v_interp_p2_f32_e32 v3, v1, attr0.w",
]
DESCRIPTORS = [
    "s_mov_b32 s21, 0",
    "s_load_dwordx8 s[28:35], s[20:21], null",
    *(f"s_mov_b32 s{40 + i}, {word}" for i, word in enumerate(SAMPLER)),
    "s_waitcnt lgkmcnt(0)",
]
TAIL = [
    "s_waitcnt vmcnt(0)",
    "v_mul_f32_e32 v12, v12, v2",
    "v_mul_f32_e32 v13, v13, v3",
    "v_mul_f32_e32 v4, v2, v3",
    "v_mul_f32_e32 v14, v14, v4",
    "exp mrt0 v12, v13, v14, v15 done vm",
    "s_endpgm",
]
SAMPLE = "image_sample v[12:15], v[8:9], s[28:35], s[40:43] dmask:0xf dim:SQ_RSRC_IMG_2D"
SAMPLE_LZ = "image_sample_lz v[12:15], v[8:9], s[28:35], s[40:43] dmask:0xf dim:SQ_RSRC_IMG_2D"
FORMS = {
    "whole_quad": [
        "s_mov_b64 s[50:51], exec",
        "s_wqm_b64 exec, exec",
        *COORDINATES,
        "s_and_b64 exec, exec, s[50:51]",
        *SCALES,
        *DESCRIPTORS,
        SAMPLE,
        *TAIL,
    ],
    "entry_lz": [*COORDINATES, *SCALES, *DESCRIPTORS, SAMPLE_LZ, *TAIL],
}
# The bias precedes the coordinates in the address; a zero bias leaves the single-mip sample unchanged.
FORMS["whole_quad_bias"] = [
    line for line in FORMS["whole_quad"] for line in (
        ["v_mov_b32_e32 v7, 0", SAMPLE.replace("image_sample ", "image_sample_b ").replace("v[8:9]", "v[7:9]")]
        if line == SAMPLE else [line])
]
ADDRESS = "A sampled program that reads attr0.z/w must take sample coordinates from attr0.x/y"
COMPARISON_SAMPLER = (0x08003012, 0, 0, 0)


def comparison(coordinates):
    """A depth comparison at the given coordinate registers, translated only (not drawn)."""
    return [
        *COORDINATES, *SCALES, "s_mov_b32 s21, 0", "s_load_dwordx8 s[28:35], s[20:21], null",
        *(f"s_mov_b32 s{40 + i}, {word:#x}" for i, word in enumerate(COMPARISON_SAMPLER)),
        "s_waitcnt lgkmcnt(0)", "v_mov_b32_e32 v5, 0.5",
        f"image_sample_c_lz v12, [v5, {coordinates}], s[28:35], s[40:43] dmask:0x1 dim:SQ_RSRC_IMG_2D",
        "s_waitcnt vmcnt(0)", "v_mul_f32_e32 v13, v12, v2", "exp mrt0 v12, v13, v2, v3 done vm", "s_endpgm",
    ]


# gfx1030 gather profile of tests/pixel_gather_check.py for one visible mip: clamp, bilinear XY, mip point.
GATHER_SAMPLER = (2 + (2 << 3), 0, (1 << 20) + (1 << 22) + (1 << 26), 0)
GATHER = "image_gather4 v[12:15], v[8:9], s[28:35], s[40:43] dmask:0x1 dim:SQ_RSRC_IMG_2D"


def gather(before_gather, after_gather=()):
    """A gfx1030 red gather at v[8:9], translated only (not drawn)."""
    return [
        *COORDINATES, *before_gather, "s_mov_b32 s21, 0", "s_load_dwordx8 s[28:35], s[20:21], null",
        *(f"s_mov_b32 s{40 + i}, {word:#x}" for i, word in enumerate(GATHER_SAMPLER)),
        "s_waitcnt lgkmcnt(0)", GATHER, *after_gather, "s_waitcnt vmcnt(0)", "v_mul_f32_e32 v12, v12, v2",
        "exp mrt0 v12, v13, v14, v3 done vm", "s_endpgm",
    ]


def lower_gather(lines):
    return aot.lower(lines, 20, 24, GATHER_SAMPLER, cpu="gfx1030", gather_mips=1)


def lower(lines):
    return aot.lower(lines, 20, 24, SAMPLER)


def replace(lines, old, new):
    if sum(line == old for line in lines) != 1:
        raise AssertionError(f"Expected one line {old!r}")
    return [part for line in lines for part in (new.split("\n") if line == old else [line])]


def swapped(lines):
    """The same program with the z and w interpolations exchanged."""
    table = {"attr0.z": "attr0.w", "attr0.w": "attr0.z"}
    return [line.replace(line[-7:], table[line[-7:]]) if line[-7:] in table else line for line in lines]


def refuse(lines, message):
    try:
        lower(lines)
    except ValueError as error:
        if message not in str(error):
            raise AssertionError(f"Expected {message!r}, got {error}") from None
        return
    raise AssertionError(f"Accepted a program expected to report {message!r}")


def exact(value):
    if Fraction(struct.unpack("<f", struct.pack("<f", float(value)))[0]) != value:
        raise AssertionError(f"{value} is not an exact binary32 value")
    return value


def texel(x, y):
    # Red and green are zero in the first column and row, so the swapped-z/w control
    # cannot differ at the first pixel.
    return tuple(exact(Fraction(v)) for v in (Fraction(x, 8), Fraction(y, 8), Fraction(1 + x + 8 * y, 64),
                                                 Fraction(128 - x - 8 * y, 128)))


def attributes(x, y):
    """attr0.z/w of tests/pixel_aot_vs.hlsl at the pixel center: z = 0.25 + 0.5u, w = 1 - v."""
    u, v = Fraction(2 * x + 1, 2 * SIDE), Fraction(2 * y + 1, 2 * SIDE)
    return exact(Fraction(1, 4) + u / 2), exact(1 - v)


def reference(swap=False):
    """Expected RGBA for each pixel, row by row. A point sample at the center returns texel (x, y)."""
    out = []
    for y in range(SIDE):
        for x in range(SIDE):
            r, g, b, a = texel(x, y)
            z, w = attributes(x, y)
            if swap:
                z, w = w, z
            out.append(tuple(exact(c) for c in (r * z, g * w, b * exact(z * w), a)))
    return out


def predicted_mismatch():
    good, bad = reference(), reference(swap=True)
    first = next(i for i, (want, got) in enumerate(zip(good, bad)) if want != got)
    return first % SIDE, first // SIDE


def source():
    """Translator acceptances and refusals, without a GPU."""
    texts = {}
    for name, lines in FORMS.items():
        text = lower(lines)
        for needle in ("float4 main(float4 uv : TEXCOORD0)", " = uv.z;", " = uv.w;", "float2(uv.x, uv.y)"):
            if needle not in text:
                raise AssertionError(f"{name}: {needle!r} missing from the translation")
        if lower(swapped(lines)) == text:
            raise AssertionError(f"{name}: swapping z and w changes nothing")
        texts[name] = text
    quad, lz = FORMS["whole_quad"], FORMS["entry_lz"]
    unknown = "Unknown interpolation context"
    refusals = [
        # z and w as the sample coordinates.
        (replace(lz, SAMPLE_LZ, SAMPLE_LZ.replace("v[8:9]", "v[2:3]")), ADDRESS),
        (replace(quad, SAMPLE, SAMPLE.replace("v[8:9]", "v[2:3]")), ADDRESS),
        # A copy of z, or a value computed from it, as the v coordinate.
        (replace(lz, SAMPLE_LZ, "v_mov_b32_e32 v9, v2\n" + SAMPLE_LZ), ADDRESS),
        (replace(lz, SAMPLE_LZ, "v_add_f32_e32 v9, v2, v3\n" + SAMPLE_LZ), ADDRESS),
        # A coordinate that is not attr0.x/y, sampled before z and w are interpolated.
        ([*COORDINATES, *DESCRIPTORS, "v_mov_b32_e32 v9, 0.5", SAMPLE_LZ, *SCALES, *TAIL], ADDRESS),
        # attr1 is not bound.
        ([line.replace("attr0.z", "attr1.z") for line in lz], "Unsupported instruction"),
        # z and w interpolated in whole-quad mode.
        ([*quad[:2], *COORDINATES, *SCALES, quad[7], *DESCRIPTORS, SAMPLE, *TAIL], unknown),
        # Whole-quad arithmetic on z interpolated in the entry mask.
        ([COORDINATES[0], *SCALES, *quad[:2], *COORDINATES[1:], "v_add_f32_e32 v8, v8, v2", quad[7],
          *DESCRIPTORS, SAMPLE, *TAIL], "helper-lane definition"),
    ]
    for lines, message in refusals:
        refuse(lines, message)
    # The comparison sample follows the same coordinate rule.
    if "SampleCmpLevelZero(samp, float2(uv.x, uv.y), " not in aot.lower(comparison("v8, v9"), 20, 24,
                                                                        COMPARISON_SAMPLER):
        raise AssertionError("comparison: expected a sample at attr0.xy")
    # z as the comparison reference, e.g. an interpolated light-space depth, is not a coordinate.
    reference = [line.replace("[v5, v8, v9]", "[v2, v8, v9]") for line in comparison("v8, v9")]
    if not re.search(r"SampleCmpLevelZero\(samp, float2\(uv\.x, uv\.y\), alu\d+\)",
                     aot.lower(reference, 20, 24, COMPARISON_SAMPLER)):
        raise AssertionError("comparison: expected z as the reference")
    # So is the gfx1030 gather.
    if "gather_2d(float2(uv.x, uv.y)" not in lower_gather(gather(SCALES)):
        raise AssertionError("gather: expected a gather at attr0.xy")
    rejected = [
        lambda: aot.lower(comparison("v8, v2"), 20, 24, COMPARISON_SAMPLER),
        lambda: aot.lower(comparison("v3, v9"), 20, 24, COMPARISON_SAMPLER),
        # z copied into the gather's v coordinate.
        lambda: lower_gather(gather([*SCALES, "v_mov_b32_e32 v9, v2"])),
        # A constant gather coordinate, then z and w.
        lambda: lower_gather(gather(["v_mov_b32_e32 v9, 0.5"], SCALES)),
    ]
    for index, translate in enumerate(rejected):
        try:
            translate()
        except ValueError as error:
            if ADDRESS not in str(error):
                raise
        else:
            raise AssertionError(f"comparison or gather refusal {index}: accepted")
    if predicted_mismatch() == (0, 0):
        raise AssertionError("The swapped-z/w control must not differ at the first pixel")
    return texts


def case(expected):
    data = bytearray(struct.pack("<II", 0, 1))
    for y in range(SIDE):
        for x in range(SIDE):
            data += struct.pack("<4f", *map(float, texel(x, y)))
    for pixel in expected:
        data += struct.pack("<4f", *map(float, pixel))
    return bytes(data)


def debug_status(output):
    """The debug-layer result of one renderer run, or None if it is not clean.

    With the layer enabled the renderer prints its error and warning counts, and both must be
    zero. Without it (no Graphics Tools on the host) it prints debug_layer=unavailable and no
    counts, so there is nothing to check beyond the draw itself.
    """
    if "debug_layer=enabled" in output:
        return "debug_errors=0 debug_warnings=0" if "debug_errors=0 debug_warnings=0\n" in output else None
    if "debug_layer=unavailable" in output and "debug_errors=" not in output:
        return "debug_layer=unavailable"
    return None


def draw(dxc, renderer, adapter, tmp):
    # Counts from an enabled layer must be zero; a missing layer is accepted only without counts.
    for output, expected in (
        ("backend=warp debug_layer=enabled\ndebug_errors=0 debug_warnings=0\n", "debug_errors=0 debug_warnings=0"),
        ("backend=warp debug_layer=unavailable\n", "debug_layer=unavailable"),
        ("backend=warp debug_layer=enabled\ndebug_errors=1 debug_warnings=0\n", None),
        ("backend=warp debug_layer=enabled\ndebug_errors=0 debug_warnings=2\n", None),
        ("backend=warp debug_layer=enabled\n", None),
        ("backend=warp\n", None),
    ):
        if debug_status(output) != expected:
            raise AssertionError(f"debug_status({output!r}) is not {expected!r}")
    texts = source()
    # The --check-pixel-gather case format binds one RGBA32F level through the point-sampling root
    # signature and compares one reference color per pixel of the 8x8 target.
    path = tmp / "attribute.case"
    path.write_bytes(case(reference()))
    hlsl, dxil = tmp / "attribute.hlsl", tmp / "attribute.dxil"

    def run(label, text, code, needle):
        hlsl.write_text(text, encoding="ascii")
        subprocess.run([dxc, "-T", "ps_6_0", "-E", "main", "-Gis", "-Fo", dxil, hlsl], check=True)
        result = subprocess.run([renderer, adapter, "--check-pixel-gather", dxil, path], capture_output=True, text=True)
        output = result.stdout + result.stderr
        if result.returncode == 77:
            print(output, end="")
            raise SystemExit(77)
        status = debug_status(output)
        if result.returncode != code or needle not in output or status is None:
            sys.stderr.write(output)
            raise AssertionError(f"{label}: unexpected result (exit {result.returncode})")
        print(f"{label}: exit={code} {needle.strip()} {status}")

    x, y = predicted_mismatch()
    for name, text in texts.items():
        run(name, text, 0, "pixels=64 channels=256 max_absolute_error=0")
        # Negative control: z and w exchanged in the program, drawn against the correct reference.
        run(f"{name} swapped z/w", lower(swapped(FORMS[name])), 1, f"pixel_mismatch x={x} y={y}\n")
    return 0


def binary(tmp, assembler, objcopy, library):
    """Assemble the three forms with LLVM and translate the machine code through the CLI."""
    from pixel_aot_check import envelope

    texts = source()
    for name, lines in FORMS.items():
        asm, obj, raw, archive, out = (tmp / f"attribute.{ext}" for ext in ("s", "o", "bin", "rpf", "hlsl"))
        asm.write_text("\n".join(lines) + "\n", encoding="ascii")
        subprocess.run([assembler, "-triple=amdgcn--amdpal", "-mcpu=gfx1013", "-filetype=obj", asm, "-o", obj],
                       check=True)
        subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, raw], check=True)
        code = raw.read_bytes()
        archive.write_bytes(envelope(code))
        if lower(aot.disassemble(code, library)) != texts[name]:
            raise AssertionError(f"{name}: the disassembly translates differently from the source text")
        subprocess.run([sys.executable, "-B", aot.__file__, archive, "fixture", "--llvm", library,
                        "--texture-sgpr", "20", "--interp-sgpr", "24", "--sampler-words", *map(str, SAMPLER),
                        "--output", out], check=True)
        if out.read_text(encoding="ascii") != texts[name]:
            raise AssertionError(f"{name}: the CLI translation differs from the source text")
        print(f"{name}: {len(code)} bytes, binary translation matches the source text")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        source()
        print(f"{len(FORMS)} sampled attr0.z/w translations and their refusals checked")
    else:
        with tempfile.TemporaryDirectory() as directory:
            if sys.argv[1] == "binary":
                binary(Path(directory), *sys.argv[2:])
            else:
                sys.exit(draw(*sys.argv[1:], Path(directory)))
