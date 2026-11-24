"""Compare gfx1030 depth gathers against a CPU oracle and D3D12 GatherCmp."""
import argparse
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

from compute_store_check import assemble
from pixel_aot_check import envelope
from pixel_compare_check import COMPARE, D3D12_FILTER, passes
import pixel_gather_check as gather


FEATURES = "+wavefrontsize64,-wavefrontsize32"
REFERENCE = 0.5


def opcode(form):
    return "image_gather4_c" + form.suffix


def sampler(form, function):
    word0, *rest = gather.sampler(form.mips)
    return (word0 | COMPARE[function] << 12, *rest)


def program(form, function, reference=REFERENCE):
    ordinary = gather.program(form, 0)
    first = 6 if form.suffix.endswith("_o") else 7
    last = 10 if form.suffix in ("_l", "_l_o") else 9
    # Keep the fixture's UV and LOD registers. Insert the reference ahead of UV
    # and move the optional packed offset to the first address register.
    instructions = [line.replace("v_mov_b32_e32 v7,", "v_mov_b32_e32 v6,") for line in ordinary]
    for index, (old, new) in enumerate(zip(gather.sampler(form.mips), sampler(form, function))):
        instructions = gather.replace(instructions, f"s_mov_b32 s{40+index}, {old:#x}",
                                      f"s_mov_b32 s{40+index}, {new:#x}")
    load = next(line for line in instructions if line.startswith("image_gather4"))
    return gather.replace(instructions, load, f"v_mov_b32_e32 v7, {reference}\n"
                          f"{opcode(form)} v[7:10], v[{first}:{last}], s[28:35], s[40:43] "
                          "dmask:0x1 dim:SQ_RSRC_IMG_2D")


def translate(lines, form, function="LESS", **profile):
    return gather.lower(lines, form, words=sampler(form, function), **profile)


def depth(mip, x, y):
    return ((x + 4 * y + 2 * mip) % 9) / 8


def expected_pixels(mip, offset, function, reference=REFERENCE):
    width = gather.SIDE >> mip

    def texel(x, y):
        return depth(mip, min(width - 1, max(0, x)), min(width - 1, max(0, y)))

    for y in range(gather.SIDE):
        for x in range(gather.SIDE):
            # Integer arithmetic gives the exact footprint at each pixel center.
            u = ((2 * x + 1) * width - gather.SIDE) // (2 * gather.SIDE) + offset[0]
            v = ((2 * y + 1) * width - gather.SIDE) // (2 * gather.SIDE) + offset[1]
            yield tuple(float(passes(function, reference, texel(u + dx, v + dy)))
                        for dx, dy in ((0, 1), (1, 1), (1, 0), (0, 0)))


def controls():
    for form in gather.FORMS:
        x, y = form.offset
        source = translate(program(form, "LESS"), form)
        assert f"gather_compare(float2(uv.x, uv.y), int2({x}, {y}), {form.lod}, alu" in source
        assert "float4(reference.xxxx < taps)" in source
        pixels = tuple(expected_pixels(form.lod, form.offset, "LESS"))
        assert len(set(zip(*pixels))) == 4, opcode(form)
    form = gather.FORMS[-1]
    instructions = program(form, "LESS")
    for old, new, diagnostic in (
        ("dmask:0x1", "dmask:0x2", "red DMASK 0x1"),
        ("v[6:10]", "v[7:10]", "width mismatch"),
        ("image_gather4_c_l_o", "image_gather4_c_b_o", "Unsupported instruction"),
        ("image_gather4_c_l_o", "image_gather4_c_cl_o", "Unsupported instruction"),
        ("v_mov_b32_e32 v7, 0.5", "", "v7"),
        ("v_mov_b32_e32 v10, 2.0", "v_mov_b32_e32 v10, 1.5", "known integer LOD"),
        ("s_waitcnt lgkmcnt(0)", "", "sampler binding"),
        ("s_waitcnt vmcnt(0)", "v_mov_b32_e32 v15, v7", "before vmcnt wait"),
    ):
        gather.rejects(diagnostic, gather.replace(instructions, old, new), form, words=sampler(form, "LESS"))
    gather.rejects("single visible mip", program(gather.FORMS[0], "LESS"), gather.FORMS[0],
                   gather_mips=3, words=sampler(form, "LESS"))


def binary(directory, assembler, objcopy, library):
    for form in gather.FORMS:
        words = sampler(form, "LESS")
        source = program(form, "LESS")
        code = assemble(directory, assembler, objcopy, source, cpu="gfx1030", features=FEATURES)
        decoded = gather.aot.disassemble(code, library, cpu=b"gfx1030", features=FEATURES.encode(), offsets=True)
        assert [line for _, line in decoded if line.startswith("image_")] == [
            line for line in source if line.startswith("image_")]
        expected = translate(source, form)
        assert translate(decoded, form) == expected
        archive, output = directory / "fixture.rpf", directory / "fixture.hlsl"
        archive.write_bytes(envelope(code))
        subprocess.run([sys.executable, "-B", gather.aot.__file__, archive, "fixture", "--llvm", library,
                        "--cpu", "gfx1030", "--gather-mips", str(form.mips), "--texture-sgpr", "20",
                        "--interp-sgpr", "24", "--sampler-words", *(hex(word) for word in words),
                        "--output", output], check=True)
        assert output.read_text(encoding="ascii") == expected
    print("Six gfx1030 comparison gather mnemonics round-trip through LLVM and the archive CLI")


def write_gather_case(path, form, pixels):
    data = bytearray(struct.pack("<II", 1, form.mips))
    for mip in range(form.mips):
        width = gather.SIDE >> mip
        for y in range(width):
            for x in range(width):
                # Other channels carry values different from the depth channel.
                red = depth(mip, x, y)
                data.extend(struct.pack("<4f", red, 1.0 - red, 0.0, 1.0))
    for pixel in pixels:
        data.extend(struct.pack("<4f", *pixel))
    path.write_bytes(data)


def write_desktop_case(path, form, function, pixels):
    side = gather.SIDE
    width = side >> form.lod
    data = bytearray(struct.pack("<6I", D3D12_FILTER["bilinear"], COMPARE[function] + 1, width, width, side, side))
    data.extend(bytes(64 * 4))  # unused b0
    for y in range(width):
        for x in range(width):
            data.extend(struct.pack("<f", depth(form.lod, x, y)))
    for pixel in pixels:
        data.extend(struct.pack("<4f", *pixel))
    path.write_bytes(data)


def draw(directory, dxc, renderer=None, adapter=None):
    hlsl, dxil, case = (directory / f"gather.{extension}" for extension in ("hlsl", "dxil", "case"))

    def run(label, source, form, function, *, desktop=False, wrong=None):
        hlsl.write_text(source, encoding="ascii")
        subprocess.run([dxc, "-T", "ps_6_0", "-E", "main", "-Gis", "-Fo", dxil, hlsl], check=True)
        wanted = tuple(expected_pixels(form.lod, form.offset, function))
        if wrong is not None:
            first = next(i for i, (a, b) in enumerate(zip(wanted, wrong)) if a != b)
        if renderer is None:
            return
        if desktop:
            write_desktop_case(case, form, function, wanted)
        else:
            write_gather_case(case, form, wanted)
        option = "--check-pixel-compare" if desktop else "--check-pixel-gather"
        result = subprocess.run([renderer, adapter, option, dxil, case], capture_output=True, text=True)
        output = result.stdout + result.stderr
        print(f"{label}\n{output}", end="", flush=True)
        if result.returncode == 77:
            raise SystemExit(77)
        if wrong is None:
            assert result.returncode == 0, label
            match = "pixels=64 mismatches=0" if desktop else "pixels=64 channels=256 max_absolute_error=0"
            assert match in output, label
        else:
            assert result.returncode == 1, label
            assert f"pixel_mismatch x={first % gather.SIDE} y={first // gather.SIDE}" in output, label

    for form in gather.FORMS:
        x, y = form.offset
        for function in (COMPARE if form == gather.FORMS[1] else ("LESS",)):
            run(f"{opcode(form)} {function}", translate(program(form, function), form, function), form, function)
            # Bind only the reference mip; GatherCmpRed need not compute the source LOD itself.
            run(f"D3D12 GatherCmpRed {opcode(form)} {function}", f"""Texture2D<float> tex : register(t0);
SamplerComparisonState samp : register(s0);
float4 main(float2 uv : TEXCOORD0) : SV_Target0 {{
    return tex.GatherCmpRed(samp, uv, {REFERENCE}, int2({x}, {y}));
}}
""", form, function, desktop=True)

    form = gather.FORMS[-1]
    source = translate(program(form, "LESS"), form)
    wanted = tuple(expected_pixels(form.lod, form.offset, "LESS"))
    for label, mutant, wrong in (
        ("reversed comparison", source.replace("reference.xxxx < taps", "reference.xxxx > taps"),
         expected_pixels(form.lod, form.offset, "GREATER")),
        ("wrong reference", translate(program(form, "LESS", reference=0.25), form),
         expected_pixels(form.lod, form.offset, "LESS", 0.25)),
        ("swapped taps", source.replace("float4(reference.xxxx < taps)", "float4(reference.xxxx < taps).wyzx"),
         ((d, b, c, a) for a, b, c, d in wanted)),
        ("wrong mip", source.replace("int2(1, -1), 2,", "int2(1, -1), 0,"),
         expected_pixels(0, form.offset, "LESS")),
        ("wrong offset", source.replace("int2(1, -1), 2,", "int2(1, 0), 2,"),
         expected_pixels(form.lod, (1, 0), "LESS")),
    ):
        assert mutant != source, label
        run(label, mutant, form, "LESS", wrong=wrong)
    if renderer is not None and (record := os.environ.get("FORM_COVERAGE_RECORD")):
        output = Path(record)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("".join(f"{opcode(form)}\n" for form in gather.FORMS), encoding="ascii")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--binary", nargs=3, metavar=("LLVM_MC", "LLVM_OBJCOPY", "LIBLLVM"))
    modes.add_argument("--compile", metavar="DXC")
    modes.add_argument("--draw", nargs=3, metavar=("DXC", "RENDERER", "ADAPTER"))
    args = parser.parse_args()
    controls()
    if args.binary or args.compile or args.draw:
        out = Path(__file__).resolve().parents[1] / "out"
        out.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="pixel_compare_gather_", dir=out) as tmp:
            if args.binary:
                binary(Path(tmp), *args.binary)
            else:
                draw(Path(tmp), *(args.draw or (args.compile,)))


if __name__ == "__main__":
    main()
