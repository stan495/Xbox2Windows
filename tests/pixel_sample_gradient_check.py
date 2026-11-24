"""Exercise the gfx1030 packed 2D SampleGrad forms, including G16."""
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

from compute_store_check import assemble
from pixel_aot_check import envelope, lower, pixel_aot, translate, write_record
from pixel_sample_lod_check import base_prefix, compile_ps, export_tail
from pixel_sample_offset_check import draw, ramp_case, reject


# Active address ranges printed by the LLVM 15 disassembler.
FORMS = {
    "image_sample_d": (4, 9),
    "image_sample_d_o": (3, 9),
    "image_sample_d_g16": (6, 9),
    "image_sample_d_o_g16": (5, 9),
}
COARSE_FORMS = {op.replace("sample_d", "sample_cd"): shape for op, shape in FORMS.items()}
ZERO = ("0", "0", "0", "0")
HORIZONTAL_V = ("0", "-0.25", "0", "0")
VERTICAL_U = ("0", "0", "0.25", "0")
MIP_TWO = ("0", "-0.5", "0", "0")
WAVE64 = "+wavefrontsize64,-wavefrontsize32"


def program(op, gradients, *, offset="0x3e01", entry_uv=False):
    first, last = (COARSE_FORMS if op in COARSE_FORMS else FORMS)[op]
    g16 = op.endswith("_g16")
    has_offset = "_o" in op
    body = [f"v_mov_b32_e32 v{first}, {offset}"] if has_offset else []
    words = gradients
    if g16:
        halves = struct.pack("<4e", *map(float, gradients))
        words = tuple(f"{word:#x}" if word else "0" for word in struct.unpack("<2I", halves))
    for register, word in enumerate(words, first + has_offset):
        body.append(f"v_mov_b32_e32 v{register}, {word}")
    body.append(f"{op} v[12:15], v[{first}:{last}], s[28:35], s[40:43] dmask:0xf dim:SQ_RSRC_IMG_2D")
    prefix = base_prefix()
    if entry_uv:
        prefix = [line for line in prefix if "exec" not in line]
    return prefix + body + export_tail()


def translate_coarse(lines):
    return lower(lines, 20, 24, [1, 2, 3, 4], constant_descriptor=64, cpu="gfx1030")


def check_coarse_lowering():
    distinct = ("0.125", "-0.25", "0.5", "1.0")
    for op, (first, _) in COARSE_FORMS.items():
        lines = program(op, distinct, entry_uv=True)
        plain = [line.replace("image_sample_cd", "image_sample_d") for line in lines]
        assert translate_coarse(lines) == translate(plain), op
        first_gradient = first + ("_o" in op)
        prefix = f"v_mov_b32_e32 v{first_gradient}, "
        bad_word = "0x7c003000" if op.endswith("_g16") else "0x7f800000"
        for replacement, reason in ((f"{prefix}s60", "statically known"), (prefix + bad_word, "finite slopes")):
            loads = ["s_load_dwordx4 s[44:47], s[20:21], 0x40", "s_waitcnt lgkmcnt(0)",
                     "s_buffer_load_dwordx4 s[60:63], s[44:47], null", "s_waitcnt lgkmcnt(0)"]
            edited = [item for line in lines for item in
                      (loads + [replacement] if line.startswith(prefix) else [line])]
            try:
                translate_coarse(edited)
            except ValueError as error:
                assert reason in str(error), (op, error)
            else:
                raise AssertionError((op, reason))


def check_lowering():
    distinct = ("0.125", "-0.25", "0.5", "1.0")
    for op, (first, _) in FORMS.items():
        lines = program(op, distinct, entry_uv=True)
        source = translate(lines)
        g16 = op.endswith("_g16")
        has_offset = "_o" in op
        # Decode the call's two gradient vectors back to the defining moves.
        first_gradient = next(i for i, line in enumerate(lines)
                              if line.startswith(f"v_mov_b32_e32 v{first + has_offset},"))
        values = [f"alu{i}" for i in range(first_gradient, first_gradient + (2 if g16 else 4))]
        if g16:
            expected = [(f"widen16(asuint({v}))", f"widen16(asuint({v}) >> 16)") for v in values]
            assert f"asfloat({0xb4003000}u)" in source
            assert f"asfloat({0x3c003800}u)" in source
        else:
            expected = [values[:2], values[2:]]
        ddx, ddy = [f"float2({u}, {v})" for u, v in expected]
        offset = ", int2(1, -2)" if has_offset else ""
        assert f"tex.SampleGrad(samp, float2(uv.x, uv.y), {ddx}, {ddy}{offset});" in source, op

    lines = program("image_sample_d", distinct)
    for old, new, diagnostic in (
        ("v[4:9]", "v[4:8]", "Descriptor or vector width mismatch"),
        ("v[4:9]", "[v4, v5, v6, v7, v8, v9]", "Unsupported instruction"),
        ("dmask:0xf", "dmask:0x7", "requires dmask:0xf"),
        ("SQ_RSRC_IMG_2D", "SQ_RSRC_IMG_3D", "Unsupported instruction"),
        ("SQ_RSRC_IMG_2D", "SQ_RSRC_IMG_2D a16", "Unsupported instruction"),
        ("SQ_RSRC_IMG_2D", "SQ_RSRC_IMG_2D d16", "Unsupported instruction"),
        ("image_sample_d ", "image_sample_cd ", "requires gfx1030"),
        ("image_sample_d ", "image_sample_d_cl ", "Unsupported instruction"),
        ("image_sample_d ", "image_sample_c_d_cl ", "Unsupported instruction"),
    ):
        reject([line.replace(old, new) for line in lines], diagnostic)
    undefined = [line for line in lines if not line.startswith("v_mov_b32_e32 v5,")]
    reject(undefined, "Unsupported texture coordinate")
    for op in ("image_sample_d", "image_sample_d_g16"):
        lines = program(op, distinct)
        i = next(i for i, line in enumerate(lines) if line.startswith(op))
        lines.insert(i, "image_sample v[4:7], v[8:9], s[28:35], s[40:43] dmask:0xf dim:SQ_RSRC_IMG_2D")
        reject(lines, "Sample used before vmcnt wait")
    bad_offset = program("image_sample_d_o_g16", distinct, offset="0x8")
    reject(bad_offset, "outside the DXIL immediate range")


def check_g16_codegen(directory, llc):
    # Independent check of the immediate gradient words produced by LLVM from
    # four distinct scalar derivatives. Keep full float UV to separate G16 from A16.
    ir = directory / "gradient-order.ll"
    ir.write_text('''target triple = "amdgcn-amd-amdpal"
declare <4 x float> @llvm.amdgcn.image.sample.d.2d.v4f32.f16.f32(
    i32 immarg, half, half, half, half, float, float, <8 x i32>, <4 x i32>, i1 immarg, i32 immarg, i32 immarg)
define amdgpu_ps <4 x float> @gradient_order(<8 x i32> inreg %r, <4 x i32> inreg %s, float %u, float %v) {
    %value = call <4 x float> @llvm.amdgcn.image.sample.d.2d.v4f32.f16.f32(
        i32 15, half 0xH3000, half 0xHB400, half 0xH3800, half 0xH3C00, float %u, float %v,
        <8 x i32> %r, <4 x i32> %s, i1 false, i32 0, i32 0)
    ret <4 x float> %value
}
''', encoding="ascii")
    asm = subprocess.run([llc, "-mcpu=gfx1030", f"-mattr={WAVE64},-nsa-encoding", "-o", "-", ir],
                         check=True, capture_output=True, text=True).stdout
    match = re.search(r"image_sample_d_g16 v\[\d+:\d+\], v\[(\d+):(\d+)\].*dim:SQ_RSRC_IMG_2D", asm)
    assert match, asm
    assert " a16" not in match[0], match[0]
    first, last = map(int, match.groups())
    assert last - first == 3, match[0]
    assert f"v_mov_b32_e32 v{first}, 0xb4003000" in asm, asm
    assert f"v_mov_b32_e32 v{first + 1}, 0x3c003800" in asm, asm


def check_binary(directory, assembler, objcopy, library, llc, *, coarse=False):
    check_g16_codegen(directory, llc)
    forms = COARSE_FORMS if coarse else FORMS
    translate_program = translate_coarse if coarse else translate
    for op in forms:
        lines = program(op, ("0", "-0.5", "0", "0"), entry_uv=True)
        assembly = lines
        if not op.endswith("_g16"):
            # The assembler takes the eight-VGPR tuple spelling. The decoded
            # instruction lists the six or seven active VGPRs from the same encoding.
            first, last = forms[op]
            assembly = [line.replace(f"v[{first}:{last}]", f"v[{first}:{first + 7}]")
                        if line.startswith(f"{op} ") else line for line in lines]
        code = assemble(directory, assembler, objcopy, assembly, cpu="gfx1030", features=WAVE64)
        decoded = pixel_aot.disassemble(code, library, cpu=b"gfx1030", features=WAVE64.encode(), offsets=True)
        assert [line for _, line in decoded if line.startswith("image_")] == [
            line for line in lines if line.startswith("image_")
        ]
        expected = translate_program(lines)
        assert translate_program(decoded) == expected
        archive = directory / "fixture.bin"
        archive.write_bytes(envelope(code))
        output = directory / "fixture.hlsl"
        subprocess.run([sys.executable, "-B", pixel_aot.__file__, archive, "fixture", "--llvm", library,
                        "--cpu", "gfx1030", "--texture-sgpr", "20", "--interp-sgpr", "24",
                        "--sampler-words", "1", "2", "3", "4", "--constant-descriptor", "0x40",
                        "--output", output], check=True, capture_output=True, text=True)
        assert output.read_text(encoding="utf-8") == expected


def check_draw(directory, compiler, renderer=None, adapter=None, *, coarse=False):
    forms = COARSE_FORMS if coarse else FORMS
    translate_program = translate_coarse if coarse else translate
    # On the 8x8 texture either nonzero slope gives a two-texel footprint
    # and selects the green mip. Implicit derivatives and zero slopes give mip zero.
    for op in forms:
        for label, gradients in (("horizontal_v", HORIZONTAL_V), ("vertical_u", VERTICAL_U), ("zero", ZERO)):
            shader = compile_ps(compiler, translate_program(program(op, gradients, entry_uv=True)), directory, f"{op}_{label}")
            if renderer is not None:
                draw(renderer, adapter, "--check-pixel-quad-lod", shader, mismatch=label == "zero")
        # A four-texel footprint selects blue mip 2 of a four-mip texture
        # across the full viewport, including pixels outside the partial quad above.
        shader = compile_ps(compiler, translate_program(program(op, MIP_TWO, entry_uv=True)), directory, f"{op}_mip_two")
        if renderer is not None:
            draw(renderer, adapter, "--check-pixel-mip-clamp", shader, "2")
    # Offsets must survive the SampleGrad lowering. Exercise both axes and clamp on a spatial ramp.
    case = directory / "gradient-offset.case"
    case.write_bytes(ramp_case(1, -2))
    for op in (op for op in forms if "_o" in op):
        for offset in ("0x3e01", "0"):
            shader = compile_ps(compiler, translate_program(program(op, ZERO, offset=offset, entry_uv=True)),
                                directory, f"{op}_ramp_{offset}")
            if renderer is not None:
                draw(renderer, adapter, "--check-pixel-case", shader, case, mismatch=offset == "0")
    if renderer is not None:
        write_record(os.environ.get("FORM_COVERAGE_RECORD"))


if __name__ == "__main__":
    check_lowering()
    if len(sys.argv) > 1:
        mode, *args = sys.argv[1:]
        coarse = mode.endswith("-coarse")
        mode = mode.removesuffix("-coarse")
        if coarse:
            check_coarse_lowering()
        out = Path(__file__).resolve().parents[1] / "out"
        out.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sample-grad-", dir=out) as tmp:
            {"binary": check_binary, "compile": check_draw, "draw": check_draw}[mode](Path(tmp), *args, coarse=coarse)
    print("Four 2D coarse-gradient sample forms checked" if len(sys.argv) > 1 and coarse
          else "Four 2D explicit-gradient sample forms checked")
