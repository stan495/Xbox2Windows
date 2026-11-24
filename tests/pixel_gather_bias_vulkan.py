"""Generate Vulkan shaders and exact readback cases for ordinary gfx1030 biased gathers."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess

from pixel_gather_bias_probe import (
    CASES, FORMS, MIPS, PHASE, SIDE, first_difference, pixels, stem, texel,
)


FIXTURES = {stem(form, case): (form, case) for form in FORMS if not form.comparison for case in CASES}
OUT = Path(__file__).resolve().parents[1] / "out" / "gfx1030-gather-bias-vulkan"
VERTEX = """OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint Vertex %main "main" %index %position
OpDecorate %index BuiltIn VertexIndex
OpDecorate %position BuiltIn Position
%void = OpTypeVoid
%fn = OpTypeFunction %void
%int = OpTypeInt 32 1
%float = OpTypeFloat 32
%vec4 = OpTypeVector %float 4
%in_int = OpTypePointer Input %int
%out_vec4 = OpTypePointer Output %vec4
%index = OpVariable %in_int Input
%position = OpVariable %out_vec4 Output
%i1 = OpConstant %int 1
%i2 = OpConstant %int 2
%f0 = OpConstant %float 0
%f1 = OpConstant %float 1
%f2 = OpConstant %float 2
%main = OpFunction %void None %fn
%entry = OpLabel
%i = OpLoad %int %index
%ix2 = OpShiftLeftLogical %int %i %i1
%ix = OpBitwiseAnd %int %ix2 %i2
%iy = OpBitwiseAnd %int %i %i2
%x = OpConvertSToF %float %ix
%y = OpConvertSToF %float %iy
%x2 = OpFMul %float %x %f2
%y2 = OpFMul %float %y %f2
%sx = OpFSub %float %x2 %f1
%sy = OpFSub %float %y2 %f1
%pos = OpCompositeConstruct %vec4 %sx %sy %f0 %f1
OpStore %position %pos
OpReturn
OpFunctionEnd
"""


def fragment(form, case, mode):
    extension = ('OpCapability ImageGatherBiasLodAMD\n'
                 'OpExtension "SPV_AMD_texture_gather_bias_lod"\n') if mode != "single-mip" else ""
    operand = {"bias": ("Bias", "%bias"), "explicit": ("Lod", "%lod"), "single-mip": ("", "")}[mode]
    masks, values = ([operand[0]], [operand[1]]) if operand[0] else ([], [])
    if any(form.offset):
        masks.append("ConstOffset")
        values.append("%offset")
    image_operands = " " + " ".join(("|".join(masks), *values)) if masks else ""
    dx, dy = (scale / SIDE for scale in case.scale)
    ox, oy = form.offset
    return f"""OpCapability Shader
{extension}OpMemoryModel Logical GLSL450
OpEntryPoint Fragment %main "main" %position %target
OpExecutionMode %main OriginUpperLeft
OpDecorate %position BuiltIn FragCoord
OpDecorate %target Location 0
OpDecorate %texture DescriptorSet 0
OpDecorate %texture Binding 0
OpDecorate %scaled NoContraction
OpDecorate %uv NoContraction
%void = OpTypeVoid
%fn = OpTypeFunction %void
%int = OpTypeInt 32 1
%float = OpTypeFloat 32
%ivec2 = OpTypeVector %int 2
%vec2 = OpTypeVector %float 2
%vec4 = OpTypeVector %float 4
%image = OpTypeImage %float 2D 0 0 0 1 Unknown
%sampled = OpTypeSampledImage %image
%in_vec4 = OpTypePointer Input %vec4
%out_vec4 = OpTypePointer Output %vec4
%ptr_sampled = OpTypePointer UniformConstant %sampled
%position = OpVariable %in_vec4 Input
%target = OpVariable %out_vec4 Output
%texture = OpVariable %ptr_sampled UniformConstant
%c = OpConstant %int {form.component}
%ox = OpConstant %int {ox}
%oy = OpConstant %int {oy}
%offset = OpConstantComposite %ivec2 %ox %oy
%dx = OpConstant %float {dx}
%dy = OpConstant %float {dy}
%scale = OpConstantComposite %vec2 %dx %dy
%phase = OpConstant %float {float(PHASE)}
%shift = OpConstantComposite %vec2 %phase %phase
%bias = OpConstant %float {case.bias}
%lod = OpConstant %float {case.mip}
%main = OpFunction %void None %fn
%entry = OpLabel
%pos = OpLoad %vec4 %position
%xy = OpVectorShuffle %vec2 %pos %pos 0 1
%scaled = OpFMul %vec2 %xy %scale
%uv = OpFAdd %vec2 %scaled %shift
%resource = OpLoad %sampled %texture
%taps = OpImageGather %vec4 %resource %uv %c{image_operands}
OpStore %target %taps
OpReturn
OpFunctionEnd
"""


def write_shader(name, source, assembler, validator):
    assembly = OUT / f"{name}.spvasm"
    shader = OUT / f"{name}.spv"
    assembly.write_text(source, encoding="ascii")
    subprocess.run([assembler, "--target-env", "vulkan1.0", assembly, "-o", shader], check=True)
    subprocess.run([validator, "--target-env", "vulkan1.0", shader], check=True)


def write_bundle(assembler, validator):
    OUT.mkdir(parents=True, exist_ok=True)
    write_shader("fullscreen", VERTEX, assembler, validator)
    cases = []
    for name, (form, case) in FIXTURES.items():
        for mode in ("bias", "explicit", "single-mip"):
            write_shader(f"{name}-{mode}", fragment(form, case, mode), assembler, validator)
        for mip in range(MIPS):
            extent = SIDE >> mip
            data = b"".join(struct.pack("<4f", *(texel(form, mip, x, y, channel) for channel in range(4)))
                            for y in range(extent) for x in range(extent))
            (OUT / f"{name}.mip{mip}.rgba32f").write_bytes(data)
        expected = pixels(form, case, case.mip)
        (OUT / f"{name}.expected.rgba32f").write_bytes(struct.pack(f"<{len(expected)}f", *expected))
        cases.append({"stem": name, "expected_mip": case.mip, "scale": case.scale,
                      "bias": case.bias, "offset": form.offset})
    (OUT / "manifest.json").write_text(json.dumps(cases, indent=2) + "\n", encoding="ascii")
    print(f"Validated {len(cases)} Vulkan gather cases and their two controls")


def verify_draws(runner, names, *, native, device):
    draw_count = 0
    negative_count = 0
    for name in names:
        form, case = FIXTURES[name]
        references = [pixels(form, case, mip) for mip in range(MIPS)]
        assert len(set(references)) == MIPS, name
        draws = [("single-mip", mip, mip) for mip in range(MIPS)]
        if native:
            draws += [("explicit", 0, case.mip), ("bias", 0, case.mip)]
        for mode, base, expected_mip in draws:
            output = OUT / f"{name}-{mode}-base{base}.readback.rgba32f"
            output.unlink(missing_ok=True)
            cmd = [runner, OUT, name, mode, str(base), output]
            if device is not None:
                cmd.append(str(device))
            result = subprocess.run(cmd)
            if result.returncode == 77:
                raise SystemExit(77)
            result.check_returncode()
            raw = output.read_bytes()
            if len(raw) != SIDE * SIDE * 16:
                raise ValueError(f"{name} {mode}: expected {SIDE * SIDE * 16} readback bytes, got {len(raw)}")
            observed = struct.unpack(f"<{SIDE * SIDE * 4}f", raw)
            matching = [mip for mip in range(MIPS) if first_difference(references[mip], observed) is None]
            shader = OUT / f"{name}-{mode}.spv"
            print(f"{name} {mode} base={base} matching_mips={matching} "
                  f"shader_sha256={hashlib.sha256(shader.read_bytes()).hexdigest()} "
                  f"readback_sha256={hashlib.sha256(raw).hexdigest()}", flush=True)
            if matching != [expected_mip]:
                difference = first_difference(references[expected_mip], observed)
                raise ValueError(f"{name} {mode} expected mip {expected_mip}: {difference}")
            draw_count += 1
            if mode == "single-mip" and base != case.mip:
                difference = first_difference(references[case.mip], observed)
                assert difference is not None
                print(f"{name} wrong view mip {base}: {difference}", flush=True)
                negative_count += 1
    print(f"Verified {draw_count} Vulkan readbacks and {negative_count} wrong-view comparisons")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spirv_as")
    parser.add_argument("spirv_val")
    parser.add_argument("--runner", type=Path, help="compiled pixel_gather_bias_vulkan.cpp")
    parser.add_argument("--native", action="store_true", help="also execute AMD bias and explicit-LOD shaders")
    parser.add_argument("--device", type=int, help="zero-based Vulkan physical device index")
    parser.add_argument("--case", choices=tuple(FIXTURES), help="restrict draws to one CPU reference case")
    args = parser.parse_args()
    if (args.native or args.device is not None or args.case) and not args.runner:
        parser.error("--native, --device and --case require --runner")
    if args.device is not None and args.device < 0:
        parser.error("--device must be nonnegative")
    write_bundle(args.spirv_as, args.spirv_val)
    if args.runner:
        names = [args.case] if args.case else FIXTURES
        verify_draws(args.runner.resolve(), names, native=args.native, device=args.device)


if __name__ == "__main__":
    main()
