"""Check gfx1030 2D depth comparisons with explicit FP32 or G16 gradients."""
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

from compute_store_check import assemble
from pixel_aot_check import envelope, write_record
from pixel_compare_check import pixel_aot, prefix, sampler_moves, sampler_words, tail, translate
from pixel_compare_lod_check import (
    OFFSET, first_mismatch, probe_shader_profile, reference_rows, run_draw, sampler, write_case,
)
from pixel_compare_sample_check import must_reject
from pixel_sample_gradient_check import WAVE64


FORMS = {
    "image_sample_c_d": 3,
    "image_sample_c_d_o": 2,
    "image_sample_c_d_g16": 5,
    "image_sample_c_d_o_g16": 4,
}
DISTINCT = (0.125, -0.25, 0.5, 1.0)
ZERO = (0.0, 0.0, 0.0, 0.0)
DRAWS = ((8, (0.0, -0.25, 0.0, 0.0)), (16, (0.0, 0.0, 0.25, 0.0)))


def has_offset(op):
    return op.removesuffix("_g16").endswith("_o")


def program(op, slopes, *, scale=1, packed="0x3e01", compare="s60", words=None):
    words = sampler() if words is None else words
    first = FORMS[op]
    g16 = op.endswith("_g16")
    if g16:
        slopes = struct.unpack("<2I", struct.pack("<4e", *slopes))
    else:
        slopes = struct.unpack("<4I", struct.pack("<4f", *slopes))
    reference = first + has_offset(op)
    lines = prefix()
    if scale == 2:
        lines = ["s_mov_b64 s[50:51], exec", "s_wqm_b64 exec, exec", *lines,
                 "v_add_f32_e32 v8, v8, v8", "v_add_f32_e32 v9, v9, v9",
                 "s_and_b64 exec, exec, s[50:51]"]
    lines += sampler_moves(words)
    if has_offset(op):
        lines.append(f"v_mov_b32_e32 v{first}, {packed}")
    lines.append(f"v_mov_b32_e32 v{reference}, {compare}")
    inline = {0: "0", 0x3f000000: "0.5", 0x3f800000: "1.0"}
    lines.extend(f"v_mov_b32_e32 v{reg}, {inline.get(value, hex(value))}"
                 for reg, value in enumerate(slopes, reference + 1))
    return [*lines, f"{op} v12, v[{first}:9], s[28:35], s[40:43] dmask:0x1 dim:SQ_RSRC_IMG_2D",
            *tail(reference=f"v{reference}")]


def controls():
    def lower(lines, words):
        return pixel_aot.lower(lines, 20, 24, words, constant_descriptor=64)

    words = sampler()
    for op, first in FORMS.items():
        lines = program(op, DISTINCT)
        hlsl = lower(lines, words)
        assert "tex.SampleCmpGrad(samp, float2(uv.x, uv.y)," in hlsl, op
        assert (", int2(1, -2))" in hlsl) == has_offset(op), op
        assert ("widen16(asuint(" in next(line for line in hlsl.splitlines()
                                         if "tex.SampleCmpGrad(" in line)) == op.endswith("_g16"), op
        nsa = "[" + ", ".join(f"v{reg}" for reg in range(first, 10)) + "]"
        for old, new, diagnostic in (
            (f"v[{first}:9]", f"v[{first}:8]", "Descriptor or vector width mismatch"),
            (f"v[{first}:9]", nsa, "Unsupported instruction"),
            ("dmask:0x1", "dmask:0x2", "Unsupported instruction"),
            ("SQ_RSRC_IMG_2D", "SQ_RSRC_IMG_2D a16", "Unsupported instruction"),
            ("SQ_RSRC_IMG_2D", "SQ_RSRC_IMG_2D d16", "Unsupported instruction"),
            ("SQ_RSRC_IMG_2D", "SQ_RSRC_IMG_3D", "Unsupported instruction"),
        ):
            must_reject(lambda: lower([line.replace(old, new) for line in lines], words), diagnostic)
        for state, diagnostic in (
            (sampler_words("LESS", "point"), "MAX_LOD 1"),
            (sampler_words("LESS", "point", w1=0x100000, w2=2 << 26), "point mip"),
            (sampler_words("LESS", "bilinear", w1=0x100000, w2=1 << 26), "point XY"),
        ):
            must_reject(lambda: lower(program(op, DISTINCT, words=state), state), diagnostic)
        grad = first + has_offset(op) + 1
        for value, diagnostic in (
            ("s61", "must be statically known"),
            ("0x7c007c00" if op.endswith("_g16") else "0x7f800000", "must be normal or zero"),
            ("0x00010000" if op.endswith("_g16") else "0x00000001", "must be normal or zero"),
        ):
            bad = [f"v_mov_b32_e32 v{grad}, {value}" if line.startswith(f"v_mov_b32_e32 v{grad},") else line
                   for line in lines]
            must_reject(lambda: lower(bad, words), diagnostic)
        image = next(i for i, line in enumerate(lines) if line.startswith(f"{op} "))
        lines.insert(image, f"image_sample_c_lz v{grad}, v[7:9], s[28:35], s[40:43] dmask:0x1 dim:SQ_RSRC_IMG_2D")
        # Copy the ready comparison reference to the address of the first sample.
        lines.insert(image, f"v_mov_b32_e32 v7, v{first + has_offset(op)}")
        must_reject(lambda: lower(lines, words), "Comparison gradient used before vmcnt wait")
        if has_offset(op):
            must_reject(lambda: lower(program(op, DISTINCT, packed="8"), words), "[-8, 7]")


def check_llvm_codegen(directory, llc, op):
    g16 = op.endswith("_g16")
    suffix = ".o" if has_offset(op) else ""
    precision = "f16" if g16 else "f32"
    intrinsic = f"llvm.amdgcn.image.sample.c.d{suffix}.2d.f32.{precision}.f32"
    slope_type = "half" if g16 else "float"
    offset_decl = "i32, " if has_offset(op) else ""
    offset_arg = "i32 15873, " if has_offset(op) else ""
    slopes = ("half 0xH3000, half 0xHB400, half 0xH3800, half 0xH3C00" if g16 else
              "float 0.125, float -0.25, float 0.5, float 1.0")
    ir = directory / "gradient-order.ll"
    ir.write_text(f'''target triple = "amdgcn-amd-amdpal"
    declare float @{intrinsic}(i32 immarg, {offset_decl}float, {', '.join([slope_type] * 4)},
        float, float, <8 x i32>, <4 x i32>, i1 immarg, i32 immarg, i32 immarg)
    define amdgpu_ps float @gradient_order(<8 x i32> inreg %r, <4 x i32> inreg %s, float %u, float %v) {{
        %value = call float @{intrinsic}(i32 1, {offset_arg}float 0.375, {slopes}, float %u, float %v,
            <8 x i32> %r, <4 x i32> %s, i1 false, i32 0, i32 0)
        ret float %value
    }}
    ''', encoding="ascii")
    asm = subprocess.run([llc, "-mcpu=gfx1030", f"-mattr={WAVE64},-nsa-encoding", "-o", "-", ir],
                         check=True, capture_output=True, text=True).stdout
    match = re.search(rf"\b{op} v\d+, v\[(\d+):(\d+)\].*dim:SQ_RSRC_IMG_2D[^\r\n]*", asm)
    assert match and " a16" not in match[0], asm
    first, last = map(int, match.groups())
    expected = [0x3e01] if has_offset(op) else []
    expected += [0x3ec00000]  # Compare reference precedes both gradient vectors.
    expected += ([0xb4003000, 0x3c003800] if g16 else list(struct.unpack("<4I", struct.pack("<4f", *DISTINCT))))
    assert last - first + 1 == len(expected) + 2, match[0]
    for reg, bits in enumerate(expected, first):
        literal = re.search(rf"v_mov_b32_e32 v{reg}, (\S+)", asm)
        assert literal, asm
        value = literal[1]
        actual = int(value, 0) if value.startswith("0x") else struct.unpack("<I", struct.pack("<f", float(value)))[0]
        assert actual == bits, (reg, bits, actual, asm)


def check_binary(directory, assembler, objcopy, library, llc):
    words = sampler()
    for op, first in FORMS.items():
        check_llvm_codegen(directory, llc, op)
        lines = program(op, DISTINCT)
        # LLVM MC uses an eight-VGPR tuple for the six/seven-word encoding.
        assembly = [line.replace(f"v[{first}:9]", f"v[{first}:{first + 7}]")
                    if line.startswith(f"{op} ") and first in (3, 4) else line for line in lines]
        code = assemble(directory, assembler, objcopy, assembly, cpu="gfx1030", features=WAVE64)
        decoded = pixel_aot.disassemble(code, library, cpu=b"gfx1030", features=WAVE64.encode(), offsets=True)
        assert [line for _, line in decoded if line.startswith("image_")] == [
            line for line in lines if line.startswith("image_")]
        expected = translate(lines, words)
        assert translate(decoded, words) == expected
        archive, output = directory / "fixture.bin", directory / "fixture.hlsl"
        archive.write_bytes(envelope(code))
        subprocess.run([sys.executable, "-B", pixel_aot.__file__, archive, "fixture", "--llvm", library,
                        "--cpu", "gfx1030", "--texture-sgpr", "20", "--interp-sgpr", "24", "--constant-descriptor", "0x40",
                        "--sampler-words", *(hex(word) for word in words), "--output", output], check=True)
        assert output.read_text(encoding="utf-8") == expected


def check_glsl(directory, glslang, validator, disassembler):
    """Cross-check the source-level compare/Grad/ConstOffset contract in SPIR-V."""
    source, binary = directory / "gradient.frag", directory / "gradient.spv"
    source.write_text('''#version 450
layout(set=0, binding=0) uniform sampler2DShadow depth_tex;
layout(location=0) in vec2 uv;
layout(location=0) out vec2 depth;
void main() {
    vec3 coord = vec3(uv, 0.375);
    vec2 dx = vec2(0.125, -0.25), dy = vec2(0.5, 1.0);
    depth = vec2(textureGrad(depth_tex, coord, dx, dy),
                 textureGradOffset(depth_tex, coord, dx, dy, ivec2(1, -2)));
}
''', encoding="ascii")
    subprocess.run([glslang, "-V", "-Os", "-o", binary, source], check=True)
    subprocess.run([validator, binary], check=True)
    asm = subprocess.run([disassembler, binary], check=True, capture_output=True, text=True).stdout
    samples = re.findall(r"OpImageSampleDrefExplicitLod \S+ \S+ \S+ (.+)", asm)
    assert len(samples) == 2, asm
    plain, offset = [sample.split() for sample in samples]
    assert plain[1] == "Grad" and offset[1] == "Grad|ConstOffset", asm
    assert plain[0] == offset[0] and plain[2:] == offset[2:4] and len(offset) == 5, asm
    constants = dict(re.findall(r"(%\S+) = OpConstant \S+ (\S+)", asm))
    vectors = {name: (lo, hi) for name, lo, hi in
               re.findall(r"(%\S+) = OpConstantComposite \S+ (%\S+) (%\S+)\s*\n", asm)}
    assert float(constants[plain[0]]) == 0.375, asm
    assert tuple(float(constants[component]) for vector in plain[2:] for component in vectors[vector]) == DISTINCT, asm
    assert tuple(int(constants[component]) for component in vectors[offset[4]]) == OFFSET, asm


def check_dxil_order(directory, args, op):
    source, shader, assembly = (directory / name for name in ("order.hlsl", "order.cso", "order.ll"))
    source.write_text(translate(program(op, DISTINCT, compare="0.375"), sampler()), encoding="ascii")
    subprocess.run([*args, "-Fo", shader, "-Fc", assembly, source], check=True)
    text = assembly.read_text(encoding="utf-8")
    call = re.search(r"call .* @dx\.op\.sampleCmpGrad\.f32\(([^)]+)\)", text)
    assert call, text
    operands = call[1].split(", ")
    if has_offset(op):
        assert operands[7:9] == ["i32 1", "i32 -2"], call[0]
    assert float(operands[10].split()[1]) == 0.375, call[0]
    assert tuple(float(operands[i].split()[1]) for i in (11, 12, 14, 15)) == DISTINCT, call[0]


def whole_mip(slopes):
    # These cases use one nonzero component. Its footprint is exactly two mip-zero texels.
    squared = [sum((8 * component)**2 for component in axis) for axis in (slopes[:2], slopes[2:])]
    return {0: 0, 4: 1}[max(squared)]


def check_shader(directory, compiler, renderer=None, adapter=None):
    args = probe_shader_profile(directory, compiler, "ps_6_8")
    source, shader, case, mutation = (directory / name for name in ("grad.hlsl", "grad.cso", "grad.case", "mutation.case"))
    for op in FORMS:
        check_dxil_order(directory, args, op)
        displacement = OFFSET if has_offset(op) else (0, 0)
        for size, slopes in DRAWS:
            scale = size // 8
            baseline = reference_rows(size, scale, whole_mip(slopes), displacement)
            write_case(case, size, baseline)
            variants = [("original", slopes, displacement), ("gradient-zero", ZERO, displacement)]
            if has_offset(op):
                variants.append(("offset-zero", slopes, (0, 0)))
            for variant, gradients, offset in variants:
                packed = "0x3e01" if offset != (0, 0) else "0"
                source.write_text(translate(program(op, gradients, scale=scale, packed=packed), sampler()),
                                  encoding="ascii")
                subprocess.run([*args, "-Fo", shader, source], check=True)
                if variant == "original":
                    if renderer:
                        run_draw(renderer, adapter, "grad", shader, case)
                    continue
                mutated = reference_rows(size, scale, whole_mip(gradients), offset)
                mismatch = first_mismatch(size, baseline, mutated)
                if renderer:
                    write_case(mutation, size, mutated)
                    run_draw(renderer, adapter, "grad", shader, mutation)
                    run_draw(renderer, adapter, "grad", shader, case, mismatch)
                print(f"{op} {size} {variant} {'readback' if renderer else 'CPU prediction'} {mismatch}")
    if renderer:
        write_record(os.environ.get("FORM_COVERAGE_RECORD"))


if __name__ == "__main__":
    controls()
    if len(sys.argv) > 1:
        out = Path(__file__).resolve().parents[1] / "out"
        out.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="compare-grad-", dir=out) as directory:
            {"binary": check_binary, "compile": check_shader, "draw": check_shader, "glsl": check_glsl}[sys.argv[1]](
                Path(directory), *sys.argv[2:])
    print("gfx1030 depth-comparison gradient checks passed")
