"""Exercise gfx1030 2D comparison sampling against an independent CPU image."""
from fractions import Fraction
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

from pixel_compare_check import (
    CONSTANT_REFERENCE, DEPTHS, D3D12_FILTER, TEXTURE, compile_shader, passes,
    pixel_aot, prefix, run_case, sampler_moves, sampler_words, tail, texel, translate,
)
from compute_store_check import assemble
from pixel_aot_check import envelope, write_record

FORMS = ("image_sample_c", "image_sample_c_o", "image_sample_c_lz_o")
OFFSET = (1, -2)


def program(op, words, *, packed="0x3e01", quad=True):
    lines = prefix()
    if quad:
        lines = ["s_mov_b64 s[50:51], exec", "s_wqm_b64 exec, exec", *lines,
                 "s_and_b64 exec, exec, s[50:51]"]
    first = 7
    if op.endswith("_o"):
        first = 6
        lines.append(f"v_mov_b32_e32 v6, {packed}")
    return lines + sampler_moves(words) + [
        "v_mov_b32_e32 v7, s60",
        f"{op} v12, v[{first}:9], s[28:35], s[40:43] dmask:0x1 dim:SQ_RSRC_IMG_2D",
    ] + tail(reference="v7")


def must_reject(function, diagnostic):
    try:
        function()
    except ValueError as error:
        assert diagnostic in str(error), str(error)
    else:
        raise AssertionError(f"Expected rejection containing {diagnostic!r}")


def controls():
    words = sampler_words("LESS", "point")
    for op in FORMS:
        hlsl = translate(program(op, words), words)
        method = "SampleCmpLevelZero" if "_lz" in op else "SampleCmp"
        assert f"tex.{method}(samp, float2(uv.x, uv.y)," in hlsl
        assert (", int2(1, -2))" in hlsl) == op.endswith("_o")
    for op in FORMS[:2]:
        must_reject(lambda: translate(program(op, words, quad=False), words), "quad provenance")
        clamped = sampler_words("LESS", "point", w1=0x100000)
        must_reject(lambda: translate(program(op, clamped), clamped), "MAX_LOD 0")
    for op in FORMS[1:]:
        for packed, reason in (("s61", "known packed integer"), ("0x40", "canonical 2D"), ("8", "[-8, 7]")):
            must_reject(lambda: translate(program(op, words, packed=packed), words), reason)
        for old, new, reason in (
            ("v[6:9]", "[v6, v8, v9]", "contiguous address"),
            ("dmask:0x1", "dmask:0x2", "Unsupported instruction"),
            ("SQ_RSRC_IMG_2D", "SQ_RSRC_IMG_2D a16", "Unsupported instruction"),
            ("s_waitcnt vmcnt(0)", "v_add_f32_e32 v12, 0, v12", "Write overlaps an outstanding sample"),
        ):
            lines = [line.replace(old, new) for line in program(op, words)]
            must_reject(lambda: translate(lines, words), reason)


def binary(directory, assembler, objcopy, library):
    words = sampler_words("LESS", "point")
    features = "+wavefrontsize64,-wavefrontsize32"
    for op in FORMS:
        lines = program(op, words)
        code = assemble(directory, assembler, objcopy, lines, cpu="gfx1030", features=features)
        decoded = pixel_aot.disassemble(code, library, cpu=b"gfx1030", features=features.encode(), offsets=True)
        assert [line for _, line in decoded if line.startswith("image_")] == [
            line for line in lines if line.startswith("image_")
        ]
        expected = translate(lines, words)
        assert translate(decoded, words) == expected
        archive = directory / "fixture.bin"
        archive.write_bytes(envelope(code))
        output = directory / "fixture.hlsl"
        subprocess.run([sys.executable, "-B", pixel_aot.__file__, archive, "fixture", "--llvm", library,
                        "--cpu", "gfx1030", "--texture-sgpr", "20", "--interp-sgpr", "24",
                        "--sampler-words", *(hex(word) for word in words), "--constant-descriptor", "0x40",
                        "--output", output], check=True)
        assert output.read_text(encoding="utf-8") == expected


def reference(px, py, size, mode, displacement):
    # Compare each tap before weighting. The fixture's positions are exact in the
    # six-bit fractional coordinate grid and include clamp-to-edge footprints.
    u = TEXTURE * Fraction(2 * px + 1, 2 * size) + displacement[0]
    v = TEXTURE * Fraction(2 * py + 1, 2 * size) + displacement[1]
    if mode == "point":
        return float(passes("LESS", CONSTANT_REFERENCE, texel(math.floor(u), math.floor(v))))
    u -= Fraction(1, 2)
    v -= Fraction(1, 2)
    x, y = math.floor(u), math.floor(v)
    fx, fy = u - x, v - y
    return float(sum(wx * wy * passes("LESS", CONSTANT_REFERENCE, texel(x + dx, y + dy))
                     for dx, wx in ((0, 1 - fx), (1, fx)) for dy, wy in ((0, 1 - fy), (1, fy))))


def write_case(path, size, mode, displacement):
    data = bytearray(struct.pack("<6I", D3D12_FILTER[mode], 2, TEXTURE, TEXTURE, size, size))
    data.extend(struct.pack("<64f", CONSTANT_REFERENCE, *([0.0] * 63)))
    data.extend(struct.pack(f"<{len(DEPTHS)}f", *DEPTHS))
    for y in range(size):
        for x in range(size):
            data.extend(struct.pack("<4f", reference(x, y, size, mode, displacement), CONSTANT_REFERENCE, 0.5, 1.0))
    path.write_bytes(data)


def draw(directory, compiler, renderer, adapter):
    for op in FORMS:
        displacement = OFFSET if op.endswith("_o") else (0, 0)
        for mode in ("point", "bilinear"):
            words = sampler_words("LESS", mode)
            for mutant in (False, True):
                if mutant and not op.endswith("_o"):
                    continue
                source = directory / f"{op}-{mode}-{mutant}.hlsl"
                shader = source.with_suffix(".cso")
                source.write_text(translate(program(op, words, packed="0" if mutant else "0x3e01"), words),
                                  encoding="ascii")
                compile_shader(compiler, source, shader)
                for size in (8, 16):
                    case = source.with_suffix(".case")
                    write_case(case, size, mode, displacement)
                    result = run_case(renderer, adapter, shader, case)
                    if result == 77:
                        raise SystemExit(77)
                    if mutant:
                        assert result.returncode == 1 and "pixel_mismatch" in result.stderr, (result.stdout, result.stderr)
                        print(f"{op} {mode} size={size} zero offset: {result.stderr.strip().splitlines()[0]}")
                    else:
                        assert result.returncode == 0, (result.stdout, result.stderr)
                    print(result.stdout, end="")
    write_record(os.environ.get("FORM_COVERAGE_RECORD"))


if __name__ == "__main__":
    controls()
    if len(sys.argv) > 1:
        out = Path(__file__).resolve().parents[1] / "out"
        out.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="compare-sample-", dir=out) as directory:
            {"binary": binary, "draw": draw}[sys.argv[1]](Path(directory), *sys.argv[2:])
    print("Three gfx1030 2D comparison sample forms checked")
