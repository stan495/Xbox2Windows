"""Test the experimental minimum-LOD interpretation of gfx1030 comparison clamps."""
import argparse
import re
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

from compute_store_check import assemble
from pixel_aot_check import envelope
from pixel_compare_check import pixel_aot, prefix, sampler_moves, sampler_words, tail
from pixel_compare_lod_check import FEATURES, OFFSET, first_mismatch, reference_rows, sampler, write_case
from pixel_compare_sample_check import must_reject


FORMS = {
    "plain": {"image_sample_c_cl": 0x29, "image_sample_c_cl_o": 0x39},
    "bias": {"image_sample_c_b_cl": 0x2e, "image_sample_c_b_cl_o": 0x3e},
}
# name, implicit LOD, bias, minimum LOD, expected mip, changed operand, changed value, wrong mip
CASES = {
    "plain": (
        ("raised", 0, 0, 1, 1, "clamp", 0, 0),
        ("pass-through", 1, 0, 0, 1, "force-mip", 0, 0),
    ),
    "bias": (
        ("bias-before-clamp", 1, -1, 1, 1, "clamp", 0, 0),
        ("negative-bias", 1, -1, 0, 0, "bias", 0, 1),
        ("positive-bias", 0, 1, 0, 1, "bias", 0, 0),
        ("zero-bias", 0, 0, 0, 0, "bias", 1, 1),
    ),
}


def program(op, *, clamp="1.0", bias="-1.0", scale=1, packed="0x3e01", quad=True, words=None):
    assert scale in (1, 2, 4)
    words = sampler() if words is None else words
    lines = prefix()
    if quad:
        lines = ["s_mov_b64 s[50:51], exec", "s_wqm_b64 exec, exec", *lines]
        for _ in range(scale.bit_length() - 1):
            lines += ["v_add_f32_e32 v8, v8, v8", "v_add_f32_e32 v9, v9, v9"]
        lines.append("s_and_b64 exec, exec, s[50:51]")
    lines += [*sampler_moves(words), "v_mov_b32_e32 v7, s60", f"v_mov_b32_e32 v10, {clamp}"]
    first = 7
    if op in FORMS["bias"]:
        first -= 1
        lines.append(f"v_mov_b32_e32 v{first}, {bias}")
    if op.endswith("_o"):
        first -= 1
        lines.append(f"v_mov_b32_e32 v{first}, {packed}")
    # Contiguous address is [offset, bias, reference, u, v, clamp].
    lines.append(f"{op} v12, v[{first}:10], s[28:35], s[40:43] dmask:0x1 dim:SQ_RSRC_IMG_2D")
    return [*lines, *tail(reference="v7")]


def lower(lines, words=None, **profile):
    options = {"cpu": "gfx1030", "sample_clamp_mips": 2, "constant_descriptor": 0x40} | profile
    return pixel_aot.lower(lines, 20, 24, sampler() if words is None else words, **options)


def controls(mode):
    for op in FORMS[mode]:
        lines = program(op)
        assert "tex." + ("SampleCmpBias" if mode == "bias" else "SampleCmp") + "(samp" in lower(lines)
        for count in (None, 1, 3):
            must_reject(lambda: lower(lines, sample_clamp_mips=count), "--sample-clamp-mips 2")
        for clamp in ("s61", "-1.0", "0.5", "2.0"):
            must_reject(lambda: lower(program(op, clamp=clamp)), "minimum LOD must be statically 0 or 1")
        if mode == "bias":
            for bias in ("s61", "0.5", "2.0", "-2.0"):
                must_reject(lambda: lower(program(op, bias=bias)), "bias must be statically -1, 0 or 1")
        must_reject(lambda: lower(program(op, quad=False)), "quad provenance")
        for words, diagnostic in (
            (sampler_words("LESS", "point"), "MAX_LOD 1"),
            (sampler_words("LESS", "point", w1=0x100000, w2=2 << 26), "point mip"),
            (sampler_words("LESS", "bilinear", w1=0x100000, w2=1 << 26), "point XY"),
        ):
            must_reject(lambda: lower(program(op, words=words), words), diagnostic)
        for old, new, diagnostic in (
            (":10], s[28:35]", ":9], s[28:35]", "width mismatch"),
            ("dmask:0x1", "dmask:0x2", "Unsupported instruction"),
            ("SQ_RSRC_IMG_2D", "SQ_RSRC_IMG_2D a16", "Unsupported instruction"),
            ("s_waitcnt vmcnt(0)", "v_add_f32_e32 v12, 0, v12", "outstanding sample"),
        ):
            changed = [line.replace(old, new) for line in lines]
            must_reject(lambda: lower(changed), diagnostic)
        if op.endswith("_o"):
            must_reject(lambda: lower(program(op, packed="8")), "[-8, 7]")


def binary(directory, assembler, objcopy, library):
    words = sampler()
    for forms in FORMS.values():
        for op, opcode in forms.items():
            lines = program(op, scale=2)
            code = assemble(directory, assembler, objcopy, lines, cpu="gfx1030", features=FEATURES)
            decoded = pixel_aot.disassemble(code, library, cpu=b"gfx1030", features=FEATURES.encode(), offsets=True)
            images = [(pc, line) for pc, line in decoded if line.startswith("image_")]
            expected_image = next(line for line in lines if line.startswith(op + " "))
            assert len(images) == 1 and images[0][1] == expected_image, images
            pc = images[0][0]
            word0, word1 = struct.unpack_from("<2I", code, pc)
            first = 7 - (op in FORMS["bias"]) - op.endswith("_o")
            assert ((word0 >> 18) & 0x7f, word0 & 0x6, word1 & 0xff) == (opcode, 0, first)
            print(f"{op}: {code[pc:pc + 8].hex(' ')}")
            expected = lower(lines, words)
            assert lower(decoded, words) == expected
            archive, output = directory / "fixture.bin", directory / "fixture.hlsl"
            archive.write_bytes(envelope(code))
            subprocess.run([sys.executable, "-B", pixel_aot.__file__, archive, "fixture", "--llvm", library,
                            "--cpu", "gfx1030", "--texture-sgpr", "20", "--interp-sgpr", "24",
                            "--sampler-words", *(hex(word) for word in words), "--sample-clamp-mips", "2",
                            "--constant-descriptor", "0x40", "--output", output], check=True)
            assert output.read_text(encoding="utf-8") == expected


def direct_hlsl(scale, mip, offset):
    # The fixture uses point filtering. Fetch the independently selected mip
    # and apply LESS explicitly so the control also compiles as ps_6_0.
    width = 8 >> mip
    return f"""Texture2D<float> tex : register(t0);
cbuffer Params : register(b0) {{ float4 constants; }};
float4 main(float2 uv : TEXCOORD0) : SV_Target0 {{
    int2 texel = int2(floor(uv * {scale * width}.0f)) + int2{offset};
    texel = clamp(texel, 0, {width - 1});
    return float4(float(constants.x < tex.Load(int3(texel, {mip}))), constants.x, 0.5f, 1.0f);
}}
"""


def check_dxil(compiler, shader, mode, clamp, bias, offset):
    dxop = "sampleCmpBias" if mode == "bias" else "sampleCmp"
    dxil = subprocess.run([compiler, "-dumpbin", shader], check=True, capture_output=True, text=True).stdout
    match = re.search(rf"call .* @dx\.op\.{dxop}\.f32\(([^)\n]+)\)", dxil)
    assert match, dxil
    args = match[1].split(", ")
    assert args[7:9] == [f"i32 {component}" for component in offset], match[0]
    assert float(args[-1].removeprefix("float ")) == clamp, match[0]
    if mode == "bias":
        assert float(args[-2].removeprefix("float ")) == bias, match[0]


def check_shader(directory, mode, compiler, renderer=None, adapter=None):
    target = "ps_6_8" if mode == "bias" else "ps_6_0"
    operation = "--check-pixel-compare-bias" if mode == "bias" else "--check-pixel-compare-level"
    args = [compiler, "-T", target, "-E", "main"]
    source, shader = directory / "fixture.hlsl", directory / "fixture.cso"
    case, wrong_case = directory / "fixture.case", directory / "wrong.case"
    source.write_text("float4 main() : SV_Target { return 0; }\n", encoding="ascii")
    probe = subprocess.run([*args, "-Fo", shader, source], capture_output=True, text=True)
    if probe.returncode:
        diagnostic = probe.stdout + probe.stderr
        if any(term in diagnostic.lower() for term in ("invalid profile", "unable to parse shader model")):
            print(f"skip: DXC does not support {target}: {diagnostic.strip()}")
            raise SystemExit(77)
        raise AssertionError(diagnostic)

    def compile_source(hlsl):
        source.write_text(hlsl, encoding="ascii")
        subprocess.run([*args, "-Fo", shader, source], check=True)

    def draw(case_path, expected_mismatch=None):
        result = subprocess.run([renderer, adapter, operation, shader, case_path], capture_output=True, text=True)
        print(result.stdout, end="")
        if result.returncode == 77:
            print(result.stderr, end="", file=sys.stderr)
            raise SystemExit(77)
        if expected_mismatch is None:
            assert result.returncode == 0, (result.stdout, result.stderr)
        else:
            observed = next((line for line in result.stderr.splitlines() if line.startswith("pixel_mismatch ")), None)
            assert result.returncode == 1 and observed == expected_mismatch, (
                expected_mismatch, result.stdout, result.stderr)

    for op in FORMS[mode]:
        offset = OFFSET if op.endswith("_o") else (0, 0)
        for size in (8, 16):
            for name, implicit_lod, bias, clamp, mip, mutant, changed_value, wrong_mip in CASES[mode]:
                scale = (size // 8) << implicit_lod
                assert mip == min(1, max(clamp, implicit_lod + bias))
                baseline = reference_rows(size, scale, mip, offset)
                wrong = reference_rows(size, scale, wrong_mip, offset)
                mismatch = first_mismatch(size, baseline, wrong)
                write_case(case, size, baseline)
                operands = {"clamp": f"{clamp}.0", "bias": f"{bias}.0", "scale": scale}
                compile_source(lower(program(op, **operands)))
                check_dxil(compiler, shader, mode, clamp, bias, offset)
                if renderer:
                    draw(case)
                compile_source(direct_hlsl(scale, mip, offset))
                if renderer:
                    draw(case)
                if mutant == "force-mip":
                    compile_source(direct_hlsl(scale, changed_value, offset))
                else:
                    changed_lod = implicit_lod + (changed_value if mutant == "bias" else bias)
                    changed_clamp = changed_value if mutant == "clamp" else clamp
                    assert wrong_mip == min(1, max(changed_clamp, changed_lod))
                    operands[mutant] = f"{changed_value}.0"
                    compile_source(lower(program(op, **operands)))
                if renderer:
                    write_case(wrong_case, size, wrong)
                    draw(wrong_case)
                    draw(case, mismatch)
                print(f"{op} {size} {name} wrong-{mutant} {'readback' if renderer else 'CPU prediction'} {mismatch}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    binary_parser = commands.add_parser("binary")
    for tool in ("llvm_mc", "llvm_objcopy", "library"):
        binary_parser.add_argument(tool)
    for command in ("compile", "draw"):
        shader_parser = commands.add_parser(command)
        shader_parser.add_argument("mode", choices=FORMS)
        shader_parser.add_argument("dxc")
        if command == "draw":
            shader_parser.add_argument("renderer")
            shader_parser.add_argument("adapter")
    args = parser.parse_args()
    out = Path(__file__).resolve().parents[1] / "out"
    out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="compare-clamp-", dir=out) as tmp:
        directory = Path(tmp)
        if args.command == "binary":
            for mode in FORMS:
                controls(mode)
            binary(directory, args.llvm_mc, args.llvm_objcopy, args.library)
        else:
            controls(args.mode)
            check_shader(directory, args.mode, args.dxc, getattr(args, "renderer", None), getattr(args, "adapter", None))
    print("gfx1030 experimental depth-comparison clamp checks passed")


if __name__ == "__main__":
    main()
