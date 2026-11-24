"""Verify the bounded gfx1030 2D per-instruction minimum LOD profile."""
import math
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

from compute_store_check import assemble
from pixel_aot_check import envelope, pixel_aot
from pixel_sample_lod_check import base_prefix, compile_ps, export_tail
from pixel_sample_offset_check import draw


# Clamp addressing, zero sampler LOD floor and bias, max LOD 2, point XY and mip.
SAMPLER = (0x12, 0x200000, 0x04000000, 0)
FEATURES = "+wavefrontsize64,-wavefrontsize32"
FORMS = {
    "image_sample_cl": (0x21, 8),
    "image_sample_cl_o": (0x31, 7),
    "image_sample_b_cl": (0x26, 7),
    "image_sample_b_cl_o": (0x36, 6),
}
SIDE, MIPS = 8, 3


def program(op, *, floor="1.0", bias="0.0", offset="0x3f01", scale=1, runtime=False, sampler=SAMPLER):
    _, first = FORMS[op]
    prefix = base_prefix(quad_scale=runtime)
    if scale != 1:
        prefix.insert(prefix.index("s_and_b64 exec, exec, s[50:51]"), f"v_fma_f32 v8, {scale}.0, v8, 0")
    body = ["v_mov_b32_e32 v10, " + floor]
    if "_b_" in op:
        body.append(f"v_mov_b32_e32 v7, {bias}")
    if op.endswith("_o"):
        body.append(f"v_mov_b32_e32 v{first}, {offset}")
    body.append(f"{op} v[12:15], v[{first}:10], s[28:35], s[40:43] dmask:0xf dim:SQ_RSRC_IMG_2D")
    for index, value in enumerate(sampler, 40):
        prefix[prefix.index(f"s_mov_b32 s{index}, {index - 39}")] = f"s_mov_b32 s{index}, 0x{value:x}"
    return prefix + body + export_tail()


def translate(lines, *, sampler=SAMPLER, **kwargs):
    profile = dict(cpu="gfx1030", sample_clamp_mips=MIPS, constant_descriptor=0x40) | kwargs
    return pixel_aot.lower(lines, 20, 24, sampler, **profile)


def rejects(lines, message, **kwargs):
    try:
        translate(lines, **kwargs)
    except ValueError as exc:
        assert message in str(exc), (message, str(exc))
    else:
        raise AssertionError(f"Expected rejection with {message!r}")


def check_lowering():
    for op in FORMS:
        lines = program(op)
        offset = "int2(1, -1)" if op.endswith("_o") else "int2(0, 0)"
        method = "SampleBias(samp, float2(uv.x, uv.y), 0.0f" if "_b_" in op else "Sample(samp, float2(uv.x, uv.y)"
        assert f"tex.{method}, {offset}, 1.0f);" in translate(lines), op
        rejects(lines, "requires --sample-clamp-mips", sample_clamp_mips=None)
        rejects(lines, "gfx1030", cpu="gfx1013")
        for floor in ("-1.0", "3.0", "0x7f800000", "0x7fc00000"):
            rejects(program(op, floor=floor), "known minimum LOD inside the view")
        rejects(program(op, floor="s60", runtime=True), "known minimum LOD inside the view")
        first = FORMS[op][1]
        nsa = "[" + ", ".join(f"v{i}" for i in range(first, 11)) + "]"
        rejects([line.replace(f"v[{first}:10]", nsa) for line in lines], "Unsupported instruction")
        for old, new, diagnostic in (
            ("dmask:0xf", "dmask:0x7", "requires dmask:0xf"),
            ("IMG_2D", "IMG_2D a16", "Unsupported instruction"),
            ("IMG_2D", "IMG_2D d16", "Unsupported instruction"),
            ("IMG_2D", "IMG_3D", "Unsupported instruction"),
            (":10]", ":9]", "width mismatch"),
            ("s[40:43]", "s[44:47]", "Unsupported image or sampler binding"),
        ):
            rejects([line.replace(old, new) for line in lines], diagnostic)
        rejects([line for line in lines if line != "s_waitcnt vmcnt(0)"], "Unsupported pack operand")
    op = "image_sample_b_cl_o"
    for bias in ("-17.0", "16.0", "0x7fc00000"):
        rejects(program(op, bias=bias), "known LOD bias")
    rejects(program(op, bias="s60", runtime=True), "known LOD bias")
    rejects(program(op, offset="0x08"), "outside the DXIL immediate range")
    rejects(program(op, offset="0x10000"), "canonical 2D offset word")
    assert "int2(-8, 7), 0.75f" in translate(program(op, floor="0.75", offset="0x738"))
    for sampler in ((0x13, *SAMPLER[1:]), (0x12, 0x200001, *SAMPLER[2:]),
                    (0x12, 0x100000, *SAMPLER[2:]), (*SAMPLER[:2], 0x04500000, 0),
                    (*SAMPLER[:2], 0x04000001, 0)):
        rejects(program(op, sampler=sampler), "requires a normalized clamp sampler", sampler=sampler)


def texel(mip, x, y):
    # Exactly representable as binary16, with independent mip, X and Y channels.
    return ((mip + 1) / 4, (x + 1) / 16, (y + 1) / 16, (mip + x + 2 * y + 1) / 32)


def mip_for(scale, bias, floor):
    lod = max(math.log2(scale) + bias, floor)
    return min(MIPS - 1, math.floor(lod + 0.5))


def reference(mip, scale, offset):
    size = SIDE >> mip
    def point(coord, slope, shift):
        return max(0, min(size - 1, math.floor((coord + 0.5) * slope * size / SIDE) + shift))
    return [texel(mip, point(x, scale, offset[0]), point(y, 1, offset[1]))
            for y in range(SIDE) for x in range(SIDE)]


def case_data(expected):
    colors = [texel(m, x, y) for m in range(MIPS) for y in range(SIDE >> m) for x in range(SIDE >> m)]
    return struct.pack("<2I", 0, MIPS) + b"".join(struct.pack("<4f", *color) for color in colors + expected)


def cases(op):
    # Bias-before-floor needs a negative bias and a nonzero floor. A separate
    # positive-bias case ensures that bias can also select a mip above the floor.
    if "_b_" in op:
        yield "raised", 2, -1.0, 2.0, 2
        yield "higher", 1, 2.0, 1.0, 2
    else:
        yield "raised", 1, 0.0, 1.0, 1
        yield "higher", 4, 0.0, 1.0, 2


def wrong_cases(op, name, scale, bias, floor):
    offset = (1, -1) if op.endswith("_o") else (0, 0)
    if name == "raised":
        yield "drop_floor", mip_for(scale, bias, 0.0), offset
        if "_b_" in op:
            yield "floor_before_bias", math.floor(max(math.log2(scale), floor) + bias + 0.5), offset
        if offset != (0, 0):
            yield "drop_offset", mip_for(scale, bias, floor), (0, 0)
    elif "_b_" in op:
        yield "drop_bias", mip_for(scale, 0, floor), offset
    else:
        yield "force_floor", int(floor), offset


def check_reference():
    for op in FORMS:
        offset = (1, -1) if op.endswith("_o") else (0, 0)
        for name, scale, bias, floor, mip in cases(op):
            assert mip_for(scale, bias, floor) == mip
            correct = reference(mip, scale, offset)
            assert len(correct) == 64 and correct[0] == texel(mip, offset[0], 0)
            for kind, wrong_mip, wrong_offset in wrong_cases(op, name, scale, bias, floor):
                wrong = reference(wrong_mip, scale, wrong_offset)
                assert correct[0] != wrong[0], (op, name, kind)
                print(f"CPU {op}/{kind} (0,0) expected={correct[0]} actual={wrong[0]}")


def check_intrinsic_order(directory, llc):
    # Use distinct non-inline literals to observe their assigned VGPRs. Mesa's
    # AMD LLVM backend maps NIR min_lod to the final float on these intrinsics.
    for op in FORMS:
        has_offset, has_bias = op.endswith("_o"), "_b_" in op
        mods = op.removeprefix("image_sample").replace("_", ".")
        intrinsic = f"llvm.amdgcn.image.sample{mods}.2d.v4f32.f32" + (".f32" if has_bias else "")
        operands = [("float", "0.125", 0x3E000000), ("float", "0.375", 0x3EC00000), ("float", "1.25", 0x3FA00000)]
        if has_bias:
            operands.insert(0, ("float", "-0.75", 0xBF400000))
        if has_offset:
            operands.insert(0, ("i32", "16129", 0x3F01))
        types = ", ".join(kind for kind, _, _ in operands)
        args = ", ".join(f"{kind} {value}" for kind, value, _ in operands)
        ir = directory / f"{op}.ll"
        ir.write_text(f'''target triple = "amdgcn-amd-amdpal"
declare <4 x float> @{intrinsic}(i32 immarg, {types}, <8 x i32>, <4 x i32>, i1 immarg, i32 immarg, i32 immarg)
define amdgpu_ps <4 x float> @order(<8 x i32> inreg %rsrc, <4 x i32> inreg %sampler) {{
    %value = call <4 x float> @{intrinsic}(i32 15, {args}, <8 x i32> %rsrc, <4 x i32> %sampler, i1 false, i32 0, i32 0)
    ret <4 x float> %value
}}
''', encoding="ascii")
        asm = subprocess.run([llc, "-mcpu=gfx1030", f"-mattr={FEATURES},-nsa-encoding", "-o", "-", ir],
                             check=True, capture_output=True, text=True).stdout
        match = re.search(rf"{op} v\[\d+:\d+\], v\[(\d+):(\d+)\].*dim:SQ_RSRC_IMG_2D", asm)
        assert match and " a16" not in match[0], asm
        first, last = map(int, match.groups())
        assert last - first + 1 == len(operands), match[0]
        for reg, (_, _, bits) in enumerate(operands, first):
            assert re.search(rf"v_mov_b32_e32 v{reg}, 0x{bits:x}\b", asm), asm
        print(f"{op} LLVM intrinsic address order: " + ", ".join(f"0x{bits:08x}" for _, _, bits in operands))


def check_binary(directory, assembler, objcopy, library, llc):
    check_intrinsic_order(directory, llc)
    for op, (opcode, first) in FORMS.items():
        lines = program(op, bias="-1.0")
        # LLVM MC requires the padded sequential tuple for a five-word address.
        assembly = [line.replace(f"v[{first}:10]", f"v[{first}:{first + 7}]")
                    if line.startswith(op + " ") and first == 6 else line for line in lines]
        data = assemble(directory, assembler, objcopy, assembly, cpu="gfx1030", features=FEATURES)
        decoded = pixel_aot.disassemble(data, library, cpu=b"gfx1030", features=FEATURES.encode(), offsets=True)
        image = [(pc, text) for pc, text in decoded if text.startswith("image_")]
        assert len(image) == 1 and image[0][1] == next(line for line in lines if line.startswith(op + " ")), image
        pc, _ = image[0]
        w0, w1 = struct.unpack_from("<2I", data, pc)
        assert ((w0 >> 18) & 0x7f, w1 & 0xff, w0 & 0x6) == (opcode, first, 0), (op, hex(w0), hex(w1))
        print(f"{op}: {data[pc:pc + 8].hex(' ')}")
        expected = translate(lines)
        assert translate(decoded) == expected
        archive, output = directory / "fixture.bin", directory / "fixture.hlsl"
        archive.write_bytes(envelope(data))
        subprocess.run([sys.executable, "-B", pixel_aot.__file__, archive, "fixture", "--llvm", library,
                        "--cpu", "gfx1030", "--texture-sgpr", "20", "--interp-sgpr", "24",
                        "--sampler-words", *(hex(word) for word in SAMPLER), "--sample-clamp-mips", str(MIPS),
                        "--constant-descriptor", "0x40",
                        "--output", output], check=True)
        assert output.read_text() == expected


def direct_hlsl(scale, mip, offset):
    return f"""Texture2D<float4> tex : register(t0);
SamplerState samp : register(s0);
float4 main(float4 uv : TEXCOORD0) : SV_Target0 {{
    return tex.SampleLevel(samp, float2(uv.x * {scale}.0f, uv.y), {mip}.0f, int2{offset});
}}
"""


def check_draw(directory, compiler, renderer=None, adapter=None):
    case = directory / "sample-clamp.case"
    for op in FORMS:
        offset = (1, -1) if op.endswith("_o") else (0, 0)
        for name, scale, bias, floor, mip in cases(op):
            stem = f"{op}_{name}"
            source = translate(program(op, scale=scale, bias=str(bias), floor=str(floor)))
            shader = compile_ps(compiler, source, directory, stem)
            dxil = subprocess.run([compiler, "-dumpbin", shader], check=True, capture_output=True, text=True).stdout
            dxop = "sampleBias" if "_b_" in op else "sample"
            call = re.search(rf"call .* @dx\.op\.{dxop}\.f32\(([^)\n]+)\)", dxil)
            assert call, dxil
            args = call[1].split(", ")
            assert float(args[-1].removeprefix("float ")) == floor, call[0]
            if "_b_" in op:
                assert float(args[-2].removeprefix("float ")) == bias, call[0]
            if offset != (0, 0):
                assert args[7:9] == [f"i32 {component}" for component in offset], call[0]
            direct = compile_ps(compiler, direct_hlsl(scale, mip, offset), directory, stem + "_direct")
            case.write_bytes(case_data(reference(mip, scale, offset)))
            if renderer is not None:
                if op == "image_sample_cl" and name == "raised":
                    draw(renderer, adapter, "--check-pixel-mip-exact", shader, "1")
                draw(renderer, adapter, "--check-pixel-gather", shader, case)
                draw(renderer, adapter, "--check-pixel-gather", direct, case)
            for kind, wrong_mip, wrong_offset in wrong_cases(op, name, scale, bias, floor):
                if kind == "drop_floor":
                    marker = f", {floor}f);"
                    assert source.count(marker) == 1
                    wrong = source.replace(marker, ", 0.0f);")
                elif kind == "drop_bias":
                    wrong = translate(program(op, scale=scale, bias="0.0", floor=str(floor)))
                elif kind == "drop_offset":
                    wrong = translate(program(op, scale=scale, bias=str(bias), floor=str(floor), offset="0"))
                else:
                    wrong = direct_hlsl(scale, wrong_mip, wrong_offset)
                mutated = compile_ps(compiler, wrong, directory, f"{stem}_{kind}")
                if renderer is not None:
                    draw(renderer, adapter, "--check-pixel-gather", mutated, case, mismatch=True)


if __name__ == "__main__":
    check_reference()
    check_lowering()
    if len(sys.argv) > 1:
        mode, *args = sys.argv[1:]
        out = Path(__file__).resolve().parents[1] / "out"
        out.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sample-clamp-", dir=out) as tmp:
            {"binary": check_binary, "compile": check_draw, "draw": check_draw}[mode](Path(tmp), *args)
    print("Four 2D minimum-LOD sample forms checked")
