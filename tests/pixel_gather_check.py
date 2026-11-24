"""Exercise the gfx1030 non-comparison 2D gather profile."""
import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

from pixel_aot_check import envelope
import pixel_aot as aot

HERE = Path(__file__).resolve().parent
SIDE = 8
MASKS = (1, 2, 4, 8)
OFFSET = (-1, 2)


@dataclass(frozen=True)
class Form:
    suffix: str
    address: str
    mips: int
    lod: int
    offset: tuple[int, int]

    @property
    def opcode(self):
        return "image_gather4" + self.suffix


FORMS = (
    Form("", "v[8:9]", 1, 0, (0, 0)),
    Form("_o", "v[7:9]", 1, 0, OFFSET),
    Form("_lz", "v[8:9]", 3, 0, (0, 0)),
    Form("_lz_o", "v[7:9]", 3, 0, OFFSET),
    Form("_l", "v[8:10]", 3, 1, (0, 0)),
    Form("_l_o", "v[7:10]", 3, 2, (1, -1)),
)


def sampler(mips):
    # ClampLastTexel in X/Y; max LOD = last visible mip; XY bilinear + mip point.
    return (2 + (2 << 3), (mips - 1) << 20, (1 << 20) + (1 << 22) + (1 << 26), 0)


def program(form, channel):
    # VDATA aliases VADDR. The offset goes before UV, and explicit LOD after UV.
    x, y = form.offset
    lines = [
        "s_mov_b32 m0, s24",
        "v_interp_p1_f32_e32 v8, v0, attr0.x",
        "v_interp_p2_f32_e32 v8, v1, attr0.x",
        "v_interp_p1_f32_e32 v9, v0, attr0.y",
        "v_interp_p2_f32_e32 v9, v1, attr0.y",
        "s_mov_b32 s21, 0",
        "s_load_dwordx8 s[28:35], s[20:21], null",
        "s_waitcnt lgkmcnt(0)",
        *(f"s_mov_b32 s{40+i}, {word:#x}" for i, word in enumerate(sampler(form.mips))),
    ]
    if form.suffix.endswith("_o"):
        lines.append(f"v_mov_b32_e32 v7, {(x % 64) + (y % 64) * 256:#x}")
    if form.suffix in ("_l", "_l_o"):
        lines.append(f"v_mov_b32_e32 v10, {form.lod}.0")
    return lines + [
        f"{form.opcode} v[7:10], {form.address}, s[28:35], s[40:43] dmask:0x{MASKS[channel]:x} dim:SQ_RSRC_IMG_2D",
        "s_waitcnt vmcnt(0)",
        "exp mrt0 v7, v8, v9, v10 done vm",
        "s_endpgm",
    ]


def lower(lines, form, *, words=None, **overrides):
    profile = {"cpu": "gfx1030", "gather_mips": form.mips} | overrides
    return aot.lower(lines, 20, 24, sampler(form.mips) if words is None else words, **profile)


def texel(mip, x, y, component):
    # All coordinates, channels and mip levels contribute different bits.
    return 1 + 4096 * mip + 256 * component + 16 * y + x


def pixels(mip, component, offset):
    extent = SIDE >> mip
    def clamp(position):
        return min(max(position, 0), extent - 1)

    for y in range(SIDE):
        for x in range(SIDE):
            # Exact floor at the pixel center, after subtracting half a texel in the sampled mip.
            left = ((2*x + 1)*extent - SIDE) // (2*SIDE) + offset[0]
            top = ((2*y + 1)*extent - SIDE) // (2*SIDE) + offset[1]
            # Assign the footprint to the guest's four destination registers.
            row0 = [texel(mip, clamp(i), clamp(top), component) for i in (left, left+1)]
            row1 = [texel(mip, clamp(i), clamp(top+1), component) for i in (left, left+1)]
            yield row1[0], row1[1], row0[1], row0[0]


def write_case(path, mips, first, expected):
    data = bytearray(struct.pack("<II", first, mips))
    for mip in range(mips):
        extent = SIDE >> mip
        for y in range(extent):
            for x in range(extent):
                data += struct.pack("<4f", *(texel(mip, x, y, channel) for channel in range(4)))
    for pixel in expected:
        data += struct.pack("<4f", *pixel)
    path.write_bytes(data)


def rejects(message, lines, form, **options):
    try:
        lower(lines, form, **options)
    except ValueError as exc:
        assert message in str(exc), exc
    else:
        raise AssertionError(f"Gather accepted an invalid program expected to report {message}")


def replace(lines, old, new):
    assert sum(line.count(old) for line in lines) == 1, old
    return [part for line in lines for part in line.replace(old, new).splitlines() if part]


def controls():
    for form in FORMS:
        for component in range(4):
            hlsl = lower(program(form, component), form)
            x, y = form.offset
            assert f"gather_2d(float2(uv.x, uv.y), int2({x}, {y}), {form.lod}, {component})" in hlsl
    base, lod_o = FORMS[0], FORMS[-1]
    lines = program(lod_o, 2)
    for old, new, diagnostic in (
        ("dmask:0x4", "dmask:0x3", "one-hot source DMASK"),
        ("dmask:0x4", "dmask:0x0", "one-hot source DMASK"),
        ("dim:SQ_RSRC_IMG_2D", "dim:SQ_RSRC_IMG_2D_ARRAY", "Unsupported instruction"),
        ("dim:SQ_RSRC_IMG_2D", "dim:SQ_RSRC_IMG_2D d16", "Unsupported instruction"),
        ("dim:SQ_RSRC_IMG_2D", "dim:SQ_RSRC_IMG_2D a16", "Unsupported instruction"),
        ("image_gather4_l_o", "image_gather4_c_b_o", "Unsupported instruction"),
        ("image_gather4_l_o", "image_gather4_c_l_o", "red DMASK"),
        ("v[7:10], v[7:10]", "v[7:10], [v7, v8, v9, v10]", "Unsupported instruction"),
        ("v[7:10], v[7:10]", "v[7:9], v[7:10]", "width mismatch"),
        ("v[7:10], v[7:10]", "v[253:256], v[7:10]", "register range"),
        ("v_mov_b32_e32 v7, 0x3f01", "v_mov_b32_e32 v7, 0x10000", "static packed XY offset"),
        ("v_mov_b32_e32 v7, 0x3f01", "v_mov_b32_e32 v7, 0x3f01\nv_add_f32_e32 v7, 1.0, v7", "static packed XY offset"),
        ("v_mov_b32_e32 v10, 2.0", "v_mov_b32_e32 v10, 0.5", "known integer LOD"),
        ("v_mov_b32_e32 v10, 2.0", "v_mov_b32_e32 v10, 4.0", "known integer LOD"),
        ("s[28:35], s[40:43]", "s[28:35], s[44:47]", "sampler binding"),
        ("s_waitcnt lgkmcnt(0)", "", "sampler binding"),
        ("s_waitcnt vmcnt(0)", "v_mov_b32_e32 v11, v7", "before vmcnt wait"),
    ):
        rejects(diagnostic, replace(lines, old, new), lod_o)
    rejects("gfx1030 float pixel profile", lines, lod_o, cpu="gfx1013")
    rejects("declared gather view", lines, lod_o, gather_mips=None)
    rejects("single visible mip", program(base, 0), base, gather_mips=2, words=sampler(2))
    base_lines = program(base, 0)
    gather = next(line for line in base_lines if line.startswith("image_gather4 "))
    sample = gather.replace("image_gather4 ", "image_sample_lz ").replace("dmask:0x1", "dmask:0xf")
    rejects("one unguarded instruction", replace(base_lines, gather, sample + "\ns_waitcnt vmcnt(0)\n" + gather), base)
    rejects("Expected one gather", replace(base_lines, gather, gather + "\ns_waitcnt vmcnt(0)\n" + sample), base)
    words = sampler(lod_o.mips)
    for index, bits in ((0, 1 << 15), (0, 1 << 27), (1, 1), (1, 1 << 21), (2, 1), (2, 3 << 26)):
        changed = tuple(word ^ bits if i == index else word for i, word in enumerate(words))
        rejects("Unsupported gather sampler", lines, lod_o, words=changed)


def binary(directory, assembler, objcopy, library):
    features = "+wavefrontsize64,-wavefrontsize32"
    for form in FORMS:
        for component in range(4):
            lines = program(form, component)
            asm, obj, raw, archive, hlsl = (directory / f"gather.{ext}" for ext in ("s", "o", "bin", "rpf", "hlsl"))
            asm.write_text("\n".join(lines) + "\n", encoding="ascii")
            subprocess.run([assembler, "-triple=amdgcn--amdpal", "-mcpu=gfx1030", f"-mattr={features}",
                            "-filetype=obj", asm, "-o", obj], check=True)
            subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, raw], check=True)
            code = raw.read_bytes()
            archive.write_bytes(envelope(code))
            decoded = aot.disassemble(code, library, cpu=b"gfx1030", features=features.encode(), offsets=True)
            assert [line for _, line in decoded if line.startswith("image_")] == [
                line for line in lines if line.startswith("image_")]
            expected = lower(lines, form)
            assert lower(decoded, form) == expected
            subprocess.run([sys.executable, "-B", aot.__file__, archive, "fixture", "--llvm", library, "--cpu", "gfx1030",
                            "--gather-mips", str(form.mips), "--texture-sgpr", "20", "--interp-sgpr", "24",
                            "--sampler-words", *(hex(word) for word in sampler(form.mips)), "--output", hlsl], check=True)
            assert hlsl.read_text(encoding="ascii") == expected
    print("gfx1030 gather also lowers identically through all 24 LLVM-assembled CLI cases")


def draw(directory, dxc, renderer=None, adapter=None):
    hlsl, dxil, case = (directory / f"gather.{ext}" for ext in ("hlsl", "dxil", "case"))

    def run(label, source, mips, expected, *, first=1, bad_xy=None):
        hlsl.write_text(source, encoding="ascii")
        subprocess.run([dxc, "-T", "ps_6_0", "-E", "main", "-Gis", "-Fo", dxil, hlsl], check=True)
        if renderer is None:
            return
        write_case(case, mips, first, expected)
        result = subprocess.run([renderer, adapter, "--check-pixel-gather", dxil, case], capture_output=True, text=True)
        output = result.stdout + result.stderr
        print(f"{label}\n{output}", end="", flush=True)
        if bad_xy is None:
            if result.returncode == 77:
                raise SystemExit(77)
            assert result.returncode == 0 and "pixels=64 channels=256 max_absolute_error=0" in output, label
        else:
            x, y = bad_xy
            assert result.returncode == 1 and f"pixel_mismatch x={x} y={y}" in output, label

    for form in FORMS:
        for component in range(4):
            expected = tuple(pixels(form.lod, component, form.offset))
            source = lower(program(form, component), form)
            run(f"{form.opcode} dmask:{MASKS[component]:#x}", source, form.mips, expected)

    # Use D3D12's own Gather to cross-check the CPU tap order and component selection at view LOD zero.
    for component, channel in enumerate(("Red", "Green", "Blue", "Alpha")):
        run(f"D3D12 Gather{channel} at the view's top mip", f"""Texture2D<float4> tex : register(t0);
SamplerState samp : register(s0);
float4 main(float2 uv : TEXCOORD0) : SV_Target0 {{ return tex.Gather{channel}(samp, uv, int2(-1, 2)); }}
""", 3, tuple(pixels(0, component, OFFSET)), first=1)

    form = FORMS[4]
    component = 2
    source = lower(program(form, component), form)
    expected = tuple(pixels(1, component, (0, 0)))
    call = "gather_2d(float2(uv.x, uv.y), int2(0, 0), 1, 2)"
    mutations = (
        ("different source channel", call, call.replace(", 1, 2)", ", 1, 1)"), tuple(pixels(1, 1, (0, 0)))),
        ("swap lower-left and upper-left taps", call, call + ".wyzx",
         tuple((d, b, c, a) for a, b, c, d in expected)),
        ("force view mip zero", call, call.replace(", 1, 2)", ", 0, 2)"), tuple(pixels(0, 2, (0, 0)))),
    )
    for label, old, new, wrong in mutations:
        first = next(i for i, (wanted, changed) in enumerate(zip(expected, wrong)) if wanted != changed)
        assert source.count(old) == 1 and old != new
        run(label, source.replace(old, new), form.mips, expected, bad_xy=(first % SIDE, first // SIDE))
    form = FORMS[1]
    source = lower(program(form, 3), form)
    expected = tuple(pixels(0, 3, OFFSET))
    assert expected[0] != next(pixels(0, 3, (63, 2)))
    run("zero-extend negative x offset", source.replace("int2(-1, 2)", "int2(63, 2)"), form.mips, expected,
        bad_xy=(0, 0))
    if renderer is not None and (record := os.environ.get("FORM_COVERAGE_RECORD")):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{form.opcode}\n" for form in FORMS), encoding="ascii")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--binary", nargs=3, metavar=("LLVM_MC", "LLVM_OBJCOPY", "LIBLLVM"))
    modes.add_argument("--compile", metavar="DXC")
    modes.add_argument("--draw", nargs=3, metavar=("DXC", "RENDERER", "ADAPTER"))
    args = parser.parse_args()
    controls()
    if args.binary or args.compile or args.draw:
        out = HERE.parent / "out"
        out.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="pixel_gather_", dir=out) as tmp:
            if args.binary:
                binary(Path(tmp), *args.binary)
            else:
                draw(Path(tmp), *(args.draw or (args.compile,)))


if __name__ == "__main__":
    main()
