"""Check the experimental gfx1030 comparison-gradient minimum-LOD profile."""
import argparse
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

from compute_store_check import assemble
from pixel_aot_check import envelope
from pixel_compare_check import pixel_aot, sampler_words
from pixel_compare_clamp_check import direct_hlsl, lower
from pixel_compare_grad_check import DISTINCT, FORMS as UNCLAMPED, has_offset, program as grad_program
from pixel_compare_lod_check import (
    OFFSET, first_mismatch, probe_shader_profile, reference_rows, run_draw, sampler, write_case,
)
from pixel_compare_sample_check import must_reject
from pixel_sample_gradient_check import WAVE64


OPCODES = {
    "image_sample_c_d_cl": 0x2b,
    "image_sample_c_d_cl_o": 0x3b,
    "image_sample_c_d_cl_g16": 0xab,
    "image_sample_c_d_cl_o_g16": 0xbb,
    "image_sample_c_cd_cl": 0x6b,
    "image_sample_c_cd_cl_o": 0x6f,
    "image_sample_c_cd_cl_g16": 0xeb,
    "image_sample_c_cd_cl_o_g16": 0xef,
}


def program(op, slopes=DISTINCT, *, floor="1.0", **kwargs):
    unclamped = op.replace("_cl", "")
    lines = grad_program(unclamped, slopes, **kwargs)
    image = next(i for i, line in enumerate(lines) if line.startswith(unclamped + " "))
    first = UNCLAMPED[unclamped]
    lines[image] = lines[image].replace(unclamped, op).replace(f"v[{first}:9]", f"v[{first}:10]")
    lines.insert(image, f"v_mov_b32_e32 v10, {floor}")
    return lines


def controls():
    for op in OPCODES:
        lines = program(op)
        hlsl = lower(lines)
        call = next(line for line in hlsl.splitlines() if "tex.SampleCmpGrad(" in line)
        offset = OFFSET if has_offset(op) else (0, 0)
        assert call.endswith(f", int2{offset}, 1.0f);"), call
        assert ("widen16(asuint(" in call) == op.endswith("_g16"), call
        for mips in (None, 1, 3):
            must_reject(lambda: lower(lines, sample_clamp_mips=mips), "--sample-clamp-mips 2")
        for floor in ("s61", "-1.0", "0.5", "2.0"):
            must_reject(lambda: lower(program(op, floor=floor)), "minimum LOD must be statically 0 or 1")
        for old, new, diagnostic in (
            (":10], s[28:35]", ":9], s[28:35]", "width mismatch"),
            (":10], s[28:35]", ":11], s[28:35]", "width mismatch"),
            ("dmask:0x1", "dmask:0x2", "Unsupported instruction"),
            ("SQ_RSRC_IMG_2D", "SQ_RSRC_IMG_2D a16", "Unsupported instruction"),
            ("SQ_RSRC_IMG_2D", "SQ_RSRC_IMG_2D d16", "Unsupported instruction"),
            ("SQ_RSRC_IMG_2D", "SQ_RSRC_IMG_3D", "Unsupported instruction"),
        ):
            must_reject(lambda: lower([line.replace(old, new) for line in lines]), diagnostic)
        for words, diagnostic in (
            (sampler_words("LESS", "point"), "MAX_LOD 1"),
            (sampler_words("LESS", "point", w1=0x100000, w2=2 << 26), "point mip"),
            (sampler_words("LESS", "bilinear", w1=0x100000, w2=1 << 26), "point XY"),
        ):
            must_reject(lambda: lower(program(op, words=words), words), diagnostic)
        reference = UNCLAMPED[op.replace("_cl", "")] + has_offset(op)
        grad = reference + 1
        for value, diagnostic in (
            ("s61", "gradient must be statically known"),
            ("0x7c007c00" if op.endswith("_g16") else "0x7f800000", "normal or zero"),
        ):
            invalid = [f"v_mov_b32_e32 v{grad}, {value}" if line.startswith(f"v_mov_b32_e32 v{grad},")
                       else line for line in lines]
            must_reject(lambda: lower(invalid), diagnostic)
        floor_move = lines.index("v_mov_b32_e32 v10, 1.0")
        pending = [*lines[:floor_move],
                   f"v_mov_b32_e32 v7, v{reference}",
                   "image_sample_c_lz v10, v[7:9], s[28:35], s[40:43] dmask:0x1 dim:SQ_RSRC_IMG_2D",
                   *lines[floor_move + 1:]]
        must_reject(lambda: lower(pending), "clamp used before vmcnt wait")
        if has_offset(op):
            must_reject(lambda: lower(program(op, packed="8")), "[-8, 7]")


def check_binary(directory, assembler, objcopy, library):
    words = sampler()
    archive, output = directory / "fixture.bin", directory / "fixture.hlsl"
    for op, opcode in OPCODES.items():
        # Both floor values exercise the full assemble/decode/container path.
        # LLVM prints the zero inline constant as 0, which preserves exact HLSL parity.
        for floor in ("0", "1.0"):
            lines = program(op, floor=floor)
            first = UNCLAMPED[op.replace("_cl", "")]
            active = 11 - first
            # MC register classes round the active span up to a power of two.
            padded_last = first + (1 << (active - 1).bit_length()) - 1
            image = next(line for line in lines if line.startswith(op + " "))
            assembly = [line.replace(f"v[{first}:10]", f"v[{first}:{padded_last}]")
                        if line == image else line for line in lines]
            code = assemble(directory, assembler, objcopy, assembly, cpu="gfx1030", features=WAVE64)
            decoded = pixel_aot.disassemble(code, library, cpu=b"gfx1030", features=WAVE64.encode(), offsets=True)
            images = [(pc, line) for pc, line in decoded if line.startswith("image_")]
            # LLVM 15 prints the full 16-register tuple for nine active words.
            printed = image.replace(f"v[{first}:10]", f"v[{first}:{padded_last}]") if active == 9 else image
            assert [line for _, line in images] == [printed], images
            pc = images[0][0]
            word0, word1 = struct.unpack_from("<2I", code, pc)
            encoded_opcode = ((word0 & 1) << 7) | ((word0 >> 18) & 0x7f)
            assert (encoded_opcode, word0 & 0x6, word1 & 0xff) == (opcode, 0, first), image
            print(f"{op} floor={floor} active={active}: {code[pc:pc + 8].hex(' ')}")
            expected = lower(lines, words)
            assert lower(decoded, words) == expected
            archive.write_bytes(envelope(code))
            subprocess.run([sys.executable, "-B", pixel_aot.__file__, archive, "fixture", "--llvm", library,
                            "--cpu", "gfx1030", "--texture-sgpr", "20", "--interp-sgpr", "24",
                            "--sampler-words", *(hex(word) for word in words), "--sample-clamp-mips", "2",
                            "--constant-descriptor", "0x40", "--output", output], check=True)
            assert output.read_text(encoding="utf-8") == expected


def check_dxil(assembly, slopes, floor, offset, reference=None):
    dxil = assembly.read_text(encoding="utf-8")
    calls = re.findall(r"call .* @dx\.op\.sampleCmpGrad\.f32\(([^)\n]+)\)", dxil)
    assert len(calls) == 1, dxil
    args = calls[0].split(", ")
    assert args[7:9] == [f"i32 {value}" for value in offset], calls[0]
    assert tuple(float(args[i].removeprefix("float ")) for i in (11, 12, 14, 15)) == slopes, calls[0]
    assert float(args[-1].removeprefix("float ")) == floor, calls[0]
    if reference is not None:
        assert float(args[10].removeprefix("float ")) == reference, calls[0]


def selected_mip(slopes, floor):
    # Each case has one nonzero component of one or two mip-zero texels.
    footprint = 8 * max(map(abs, slopes))
    return max(floor, {1: 0, 2: 1}[footprint])


def check_shader(directory, compiler, renderer=None, adapter=None):
    args = probe_shader_profile(directory, compiler, "ps_6_8")
    source, shader, assembly, case, wrong_case = (directory / f"gradient-clamp.{ext}"
                                                   for ext in ("hlsl", "cso", "ll", "case", "wrong.case"))

    def compile_source(hlsl):
        source.write_text(hlsl, encoding="ascii")
        subprocess.run([*args, "-Fo", shader, "-Fc", assembly, source], check=True)

    for op in OPCODES:
        offset = OFFSET if has_offset(op) else (0, 0)
        compile_source(lower(program(op, compare="0.375")))
        check_dxil(assembly, DISTINCT, 1, offset, reference=0.375)
        for size, above in ((8, (0.0, -0.25, 0.0, 0.0)), (16, (0.0, 0.0, 0.25, 0.0))):
            scale = size // 8
            below = tuple(value / 2 for value in above)
            cases = (
                ("below-floor", below, 1, "floor-zero", below, 0),
                ("above-floor", above, 0, "gradient-half", below, 0),
            )
            for name, slopes, floor, mutation, wrong_slopes, wrong_floor in cases:
                mip = selected_mip(slopes, floor)
                baseline = reference_rows(size, scale, mip, offset)
                lines = program(op, slopes, floor=f"{floor}.0", scale=scale)
                if renderer:
                    write_case(case, size, baseline)
                hlsl = lower(lines)
                compile_source(hlsl)
                check_dxil(assembly, slopes, floor, offset)
                if renderer:
                    run_draw(renderer, adapter, "grad", shader, case)
                    compile_source(direct_hlsl(scale, mip, offset))
                    run_draw(renderer, adapter, "grad", shader, case)

                variants = [(mutation, wrong_slopes, wrong_floor, offset)]
                if has_offset(op):
                    variants.append(("offset-zero", slopes, floor, (0, 0)))
                for variant, gradients, minimum, displacement in variants:
                    packed = "0x3e01" if displacement != (0, 0) else "0"
                    compile_source(lower(program(op, gradients, floor=f"{minimum}.0", scale=scale, packed=packed)))
                    check_dxil(assembly, gradients, minimum, displacement)
                    wrong = reference_rows(size, scale, selected_mip(gradients, minimum), displacement)
                    mismatch = first_mismatch(size, baseline, wrong)
                    if renderer:
                        write_case(wrong_case, size, wrong)
                        run_draw(renderer, adapter, "grad", shader, wrong_case)
                        run_draw(renderer, adapter, "grad", shader, case, mismatch)
                    print(f"{op} {size} {name} {variant} {'readback' if renderer else 'CPU prediction'} {mismatch}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    binary = commands.add_parser("binary")
    for tool in ("llvm_mc", "llvm_objcopy", "library"):
        binary.add_argument(tool)
    commands.add_parser("compile").add_argument("dxc")
    draw = commands.add_parser("draw")
    for arg in ("dxc", "renderer", "adapter"):
        draw.add_argument(arg)
    args = parser.parse_args()
    controls()
    out = Path(__file__).resolve().parents[1] / "out"
    out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="compare-gradient-clamp-", dir=out) as tmp:
        directory = Path(tmp)
        if args.command == "binary":
            check_binary(directory, args.llvm_mc, args.llvm_objcopy, args.library)
        else:
            check_shader(directory, args.dxc, getattr(args, "renderer", None), getattr(args, "adapter", None))
    print("gfx1030 experimental comparison gradient clamp checks passed")


if __name__ == "__main__":
    main()
