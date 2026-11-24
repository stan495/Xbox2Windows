"""Compare gfx1030 explicit LOD and biased depth samples on two depth mips."""
from fractions import Fraction
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

from compute_store_check import assemble
from pixel_aot_check import envelope, write_record
from pixel_compare_check import (
    CONSTANT_REFERENCE, D3D12_FILTER, DEPTHS, pixel_aot, prefix, sampler_moves, sampler_words, tail, translate,
)
from pixel_compare_sample_check import must_reject

FORMS = {"level": ("image_sample_c_l", "image_sample_c_l_o"),
         "bias": ("image_sample_c_b", "image_sample_c_b_o")}
FEATURES = "+wavefrontsize64,-wavefrontsize32"
OFFSET = (1, -2)
MIPS = (DEPTHS, [((x * 5 + y * 9 + 8) % 16 * 4 + 1) / 64 for y in range(4) for x in range(4)])


def sampler():
    return sampler_words("LESS", "point", w1=0x100000, w2=1 << 26)


def program(op, *, operand="1.0", scale=1, packed="0x3e01", quad=True, words=None):
    words = sampler() if words is None else words
    lines = prefix()
    if quad:
        lines = ["s_mov_b64 s[50:51], exec", "s_wqm_b64 exec, exec", *lines]
        if scale == 2:
            lines += ["v_add_f32_e32 v8, v8, v8", "v_add_f32_e32 v9, v9, v9"]
        lines.append("s_and_b64 exec, exec, s[50:51]")
    bias = "_b" in op
    first, last = (6, 9) if bias else (7, 10)
    lines += [*sampler_moves(words), "v_mov_b32_e32 v7, s60",
              f"v_mov_b32_e32 v{6 if bias else 10}, {operand}"]
    if op.endswith("_o"):
        first -= 1
        lines.append(f"v_mov_b32_e32 v{first}, {packed}")
    return [*lines, f"{op} v12, v[{first}:{last}], s[28:35], s[40:43] dmask:0x1 dim:SQ_RSRC_IMG_2D",
            *tail(reference="v7")]


def controls():
    def lower(lines, words):
        return pixel_aot.lower(lines, 20, 24, words, constant_descriptor=64)

    words = sampler()
    for mode, forms in FORMS.items():
        for op in forms:
            hlsl = lower(program(op), words)
            method = "SampleCmpBias" if mode == "bias" else "SampleCmpLevel"
            assert f"tex.{method}(samp" in hlsl
            assert (", int2(1, -2))" in hlsl) == op.endswith("_o")
            for operand in ("s61", "0.5", "-1.0"):
                must_reject(lambda: lower(program(op, operand=operand), words), "statically 0 or 1")
            for state, diagnostic in (
                (sampler_words("LESS", "point"), "MAX_LOD 1"),
                (sampler_words("LESS", "point", w1=0x100000, w2=2 << 26), "point mip"),
                (sampler_words("LESS", "bilinear", w1=0x100000, w2=1 << 26), "point XY"),
            ):
                must_reject(lambda: lower(program(op, words=state), state), diagnostic)
            for old, new, diagnostic in (
                ("dmask:0x1", "dmask:0x2", "Unsupported instruction"),
                ("SQ_RSRC_IMG_2D", "SQ_RSRC_IMG_2D a16", "Unsupported instruction"),
                ("s_waitcnt vmcnt(0)", "v_add_f32_e32 v12, 0, v12", "outstanding sample"),
            ):
                lines = [line.replace(old, new) for line in program(op)]
                must_reject(lambda: lower(lines, words), diagnostic)
            if mode == "bias":
                must_reject(lambda: lower(program(op, quad=False), words), "quad provenance")
            else:
                lower(program(op, quad=False), words)
            if op.endswith("_o"):
                must_reject(lambda: lower(program(op, packed="8"), words), "[-8, 7]")


def binary(directory, assembler, objcopy, library):
    words = sampler()
    for forms in FORMS.values():
        for op in forms:
            lines = program(op, scale=2)
            code = assemble(directory, assembler, objcopy, lines, cpu="gfx1030", features=FEATURES)
            decoded = pixel_aot.disassemble(code, library, cpu=b"gfx1030", features=FEATURES.encode(), offsets=True)
            assert [line for _, line in decoded if line.startswith("image_")] == [
                line for line in lines if line.startswith("image_")
            ]
            expected = translate(lines, words)
            assert translate(decoded, words) == expected
            archive, output = directory / "fixture.bin", directory / "fixture.hlsl"
            archive.write_bytes(envelope(code))
            subprocess.run([sys.executable, "-B", pixel_aot.__file__, archive, "fixture", "--llvm", library,
                            "--cpu", "gfx1030", "--texture-sgpr", "20", "--interp-sgpr", "24",
                            "--sampler-words", *(hex(w) for w in words), "--constant-descriptor", "0x40",
                            "--output", output], check=True)
            assert output.read_text(encoding="utf-8") == expected


def reference_rows(size, scale, mip, displacement):
    width = 8 >> mip
    rows = []
    for y in range(size):
        for x in range(size):
            u = Fraction(width * scale * (2 * x + 1), 2 * size) + displacement[0]
            v = Fraction(width * scale * (2 * y + 1), 2 * size) + displacement[1]
            tx = min(width - 1, max(0, math.floor(u)))
            ty = min(width - 1, max(0, math.floor(v)))
            red = float(CONSTANT_REFERENCE < MIPS[mip][ty * width + tx])
            rows.append((red, CONSTANT_REFERENCE, 0.5, 1.0))
    return rows


def first_mismatch(size, expected, actual):
    for i, (wanted, observed) in enumerate(zip(expected, actual, strict=True)):
        if wanted != observed:
            want = ",".join(f"{channel:.9g}" for channel in wanted)
            got = ",".join(f"{channel:.9g}" for channel in observed)
            return f"pixel_mismatch x={i % size} y={i // size} expected={want} actual={got}"
    raise AssertionError("Negative control does not distinguish the CPU references")


def write_case(path, size, rows):
    data = bytearray(struct.pack("<6I", D3D12_FILTER["point"], 2, 8, 8, size, size))
    data.extend(struct.pack("<64f", CONSTANT_REFERENCE, *([0.0] * 63)))
    data.extend(struct.pack("<80f", *MIPS[0], *MIPS[1]))
    for row in rows:
        data.extend(struct.pack("<4f", *row))
    path.write_bytes(data)


def probe_shader_profile(directory, compiler, target):
    probe = directory / "probe.hlsl"
    probe.write_text("float4 main() : SV_Target { return 0; }\n", encoding="ascii")
    args = [compiler, "-T", target, "-E", "main"]
    result = subprocess.run([*args, "-Fo", directory / "probe.cso", probe], capture_output=True, text=True)
    if result.returncode:
        diagnostic = result.stdout + result.stderr
        if "invalid profile" in diagnostic.lower() or "unable to parse shader model" in diagnostic.lower():
            print(f"skip: DXC does not support {target}: {diagnostic.strip()}")
            raise SystemExit(77)
        raise AssertionError(diagnostic)

    return args


def run_draw(renderer, adapter, mode, shader, case, expected_mismatch=None):
    run = subprocess.run([renderer, adapter, f"--check-pixel-compare-{mode}", shader, case],
                         capture_output=True, text=True)
    print(run.stdout, end="")
    if run.returncode == 77:
        print(run.stderr, end="", file=sys.stderr)
        raise SystemExit(77)
    if expected_mismatch is None:
        assert run.returncode == 0, (run.stdout, run.stderr)
    else:
        observed = next((line for line in run.stderr.splitlines() if line.startswith("pixel_mismatch ")), None)
        assert run.returncode == 1 and observed == expected_mismatch, (
            expected_mismatch, run.stdout, run.stderr)


def check_shader(directory, mode, compiler, renderer=None, adapter=None):
    target = "ps_6_8" if mode == "bias" else "ps_6_7"
    args = probe_shader_profile(directory, compiler, target)

    source = directory / "sample.hlsl"
    shader, case = source.with_suffix(".cso"), source.with_suffix(".case")
    mutated_case = directory / "mutated.case"
    for op in FORMS[mode]:
        displacement = OFFSET if op.endswith("_o") else (0, 0)
        for size in (8, 16):
            # Mip-zero footprint is one texel per pixel, so implicit LOD is zero.
            scale = size // 8
            baseline = reference_rows(size, scale, 1, displacement)
            write_case(case, size, baseline)
            variants = [("original", 1, displacement), ("operand-zero", 0, displacement)]
            if op.endswith("_o"):
                variants.append(("offset-zero", 1, (0, 0)))
            for variant, mip, offset in variants:
                lines = program(op, scale=scale, operand=f"{mip}.0", packed="0x3e01" if offset != (0, 0) else "0")
                source.write_text(translate(lines, sampler()), encoding="ascii")
                subprocess.run([*args, "-Fo", shader, source], check=True)
                if variant == "original":
                    if renderer:
                        run_draw(renderer, adapter, mode, shader, case)
                    continue
                mutated = reference_rows(size, scale, mip, offset)
                mismatch = first_mismatch(size, baseline, mutated)
                if renderer:
                    write_case(mutated_case, size, mutated)
                    run_draw(renderer, adapter, mode, shader, mutated_case)
                    run_draw(renderer, adapter, mode, shader, case, mismatch)
                print(f"{op} {size} {variant} {'readback' if renderer else 'CPU prediction'} {mismatch}")
    if renderer:
        write_record(os.environ.get("FORM_COVERAGE_RECORD"))


if __name__ == "__main__":
    controls()
    if len(sys.argv) > 1:
        out = Path(__file__).resolve().parents[1] / "out"
        out.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="compare-lod-", dir=out) as directory:
            {"binary": binary, "draw": check_shader, "compile": check_shader}[sys.argv[1]](
                Path(directory), *sys.argv[2:])
    print("gfx1030 depth-comparison LOD/bias checks passed")
