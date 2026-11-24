"""Generate native gfx1030 gather-clamp draws and classify their RGBA32F readbacks.

The inputs and pixel ABI are the same as the gather bias probe. See the
emitted manifest for the three explicit mip controls to draw per layout.
"""
import argparse
from dataclasses import dataclass, replace
import json
from pathlib import Path
import struct

import pixel_gather_bias_probe as bias_probe


OUT = Path(__file__).resolve().parents[1] / "out" / "gfx1030-gather-clamp-probe"
MODELS = ("min-after-bias", "max-after-bias", "min-before-bias", "max-before-bias", "ignore-clamp")


@dataclass(frozen=True)
class Trial:
    name: str
    clamp: int
    bias: int | None
    # View mip at implicit LOD 1, in MODELS order.
    mips: tuple[int, ...]


TRIALS = (
    Trial("clamp-low", 0, None, (0, 1, 0, 1, 1)),
    Trial("clamp-high", 2, None, (1, 2, 1, 2, 1)),
    Trial("bias-positive", 1, 1, (1, 2, 2, 2, 2)),
    Trial("bias-negative", 1, -1, (0, 1, 0, 0, 0)),
)
OPCODES = {
    "_cl": 0x41, "_cl_o": 0x51, "_b_cl": 0x46, "_b_cl_o": 0x56,
    "_c_cl": 0x49, "_c_cl_o": 0x59, "_c_b_cl": 0x4e, "_c_b_cl_o": 0x5e,
}
# Reuse the phase and (1, 2) texels per pixel from #1643, where base LOD is 1.
GEOMETRY = bias_probe.CASES[0]
LOD_STEM = "image_get_lod-base"
LOD_TOLERANCE = 1 / 16


def layout_name(form):
    return form.opcode.replace("_b", "")


def control_stem(form, mip):
    return f"{layout_name(form)}-explicit-mip{mip}"


def clamp_shader(form, trial):
    # Start from the explicit LOD control. Replace its trailing LOD with the
    # clamp and keep the reference and coordinates in exactly the same VGPRs.
    lines, control = bias_probe.native_program(form, replace(GEOMETRY, mip=trial.clamp), explicit=True)
    first = 5 if any(form.offset) else 6
    last = first + int(any(form.offset)) + int(form.comparison) + 2
    if trial.bias is not None:
        first -= 1
        bias_reg = first + int(any(form.offset))
        if any(form.offset):
            x, y = form.offset
            packed = (x & 63) | ((y & 63) << 8)
            index = lines.index(f"v_mov_b32_e32 v{bias_reg}, {packed:#x}")
            lines[index] = f"v_mov_b32_e32 v{first}, {packed:#x}"
        lines.insert(lines.index(control), f"v_mov_b32_e32 v{bias_reg}, {float(trial.bias)}")
    suffix = form.suffix.replace("_b", "_cl" if trial.bias is None else "_b_cl")
    instruction = (f"image_gather4{suffix} v[12:15], v[{first}:{last}], s[28:35], s[40:43] "
                   f"dmask:0x{1 << form.component:x} dim:SQ_RSRC_IMG_2D")
    lines[lines.index(control)] = instruction
    # MIMG encoding for this fixture's fixed VDATA, SRSRC, SSAMP and DIM.
    encoding = struct.pack("<II", 0xf0000008 | (1 << (8 + form.component)) | (OPCODES[suffix] << 18),
                           0x01470c00 | first)
    return lines, instruction, encoding.hex()


def lod_shader():
    form = bias_probe.FORMS[0]
    lines, control = bias_probe.native_program(form, GEOMETRY, explicit=True)
    instruction = "image_get_lod v[12:13], v[6:7], s[28:35], s[40:43] dmask:0x3 dim:SQ_RSRC_IMG_2D"
    index = lines.index(control)
    assert lines[index - 1].startswith("v_mov_b32_e32 v8,")
    lines[index - 1:index + 1] = ["v_mov_b32_e32 v14, 0", "v_mov_b32_e32 v15, 0", instruction]
    return lines, instruction


def pack(values):
    return struct.pack(f"<{len(values)}f", *values)


def reference(form, mip):
    return bias_probe.pixels(form, GEOMETRY, mip)


def validate_controls(form, controls):
    for mip, actual in enumerate(controls):
        differential = bias_probe.first_difference(reference(form, mip), actual)
        if differential:
            raise ValueError(f"{control_stem(form, mip)} {differential}")


def matching_mip(controls, samples):
    matches = [mip for mip, control in enumerate(controls) if bias_probe.first_mismatch(control, samples) is None]
    if len(matches) != 1:
        differences = [bias_probe.first_difference(control, samples) for control in controls]
        raise ValueError(f"matching mips={matches}, first differences by mip={differences}")
    return matches[0]


def cpu_check():
    assert GEOMETRY.scale == (1, 2)
    assert len(set(zip(*(trial.mips for trial in TRIALS), strict=True))) == len(MODELS)
    print("view mip predictions for implicit LOD 1")
    for trial in TRIALS:
        lod = 1
        bias = trial.bias or 0
        clamp = trial.clamp
        candidates = (min(lod + bias, clamp), max(lod + bias, clamp),
                      min(lod, clamp) + bias, max(lod, clamp) + bias, lod + bias)
        assert tuple(min(2, max(0, lod)) for lod in candidates) == trial.mips
        print(f"  {trial.name} {dict(zip(MODELS, trial.mips, strict=True))}")
    bias_probe.cpu_check(bias_probe.FORMS[0], GEOMETRY)
    for form in bias_probe.FORMS:
        controls = [reference(form, mip) for mip in range(bias_probe.MIPS)]
        assert len(set(controls)) == bias_probe.MIPS
        validate_controls(form, controls)
        for mip, control in enumerate(controls):
            assert matching_mip(controls, control) == mip
        if form.comparison:
            wrong = bias_probe.pixels(form, GEOMETRY, 1, reverse_compare=True)
            assert all(bias_probe.first_difference(control, wrong) for control in controls)
        if any(form.offset):
            wrong = bias_probe.pixels(form, GEOMETRY, 1, offset=(63, form.offset[1]))
            assert all(bias_probe.first_difference(control, wrong) for control in controls)


def write_shader(stem, lines, instruction, llvm, encoding=None):
    source = OUT / f"{stem}.s"
    source.write_text("\n".join(lines) + "\n", encoding="ascii")
    if llvm:
        bias_probe.assemble(source, instruction, *llvm, encoding=encoding)


def emit(llvm):
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {
        "cpu": "gfx1030", "wave_size": 64, "viewport": [bias_probe.SIDE] * 2,
        "pipeline": "Same native ABI as pixel_gather_bias_probe.py. Single-sample RGBA32F, one full-screen "
                    "triangle, perspective center weights v0:v1, m0 input s24, texture descriptor pointer s[20:21]. "
                    "Set attr0.xy to pixel_center.xy / 8 and use an identity-swizzled 2D SRV with mips 0..2. "
                    "The shader sets sampler words and computes UV in WQM. Read back tightly packed little-endian RGBA32F.",
        "scale": GEOMETRY.scale, "phase": float(bias_probe.PHASE), "implicit_lod": 1, "layouts": [],
    }
    write_shader(LOD_STEM, *lod_shader(), llvm)
    manifest["lod_control"] = {"stem": LOD_STEM, "texture_layout": "image_gather4",
                               "expected_rgba": [1, 1, 0, 0], "lod_tolerance": LOD_TOLERANCE}
    for form in bias_probe.FORMS:
        layout = layout_name(form)
        spec = {"layout": layout, "offset": form.offset, "dmask": 1 << form.component,
                "sampler": [f"0x{word:08x}" for word in form.sampler], "controls": [], "trials": []}
        for mip in range(bias_probe.MIPS):
            stem = control_stem(form, mip)
            lines, instruction = bias_probe.native_program(form, replace(GEOMETRY, mip=mip), explicit=True)
            write_shader(stem, lines, instruction, llvm)
            extent = bias_probe.SIDE >> mip
            texture = f"{layout}.mip{mip}.rgba32f"
            (OUT / texture).write_bytes(pack([bias_probe.texel(form, mip, x, y, c)
                                              for y in range(extent) for x in range(extent) for c in range(4)]))
            (OUT / f"{stem}.expected.rgba32f").write_bytes(pack(reference(form, mip)))
            spec["controls"].append({"stem": stem, "mip": mip, "texture": texture, "extent": extent})
        for trial in TRIALS:
            lines, instruction, encoding = clamp_shader(form, trial)
            stem = f"{layout}-{trial.name}"
            write_shader(stem, lines, instruction, llvm, encoding=encoding)
            spec["trials"].append({"stem": stem, "instruction": instruction, "bias": trial.bias,
                                  "clamp": trial.clamp, "predictions": dict(zip(MODELS, trial.mips, strict=True))})
        manifest["layouts"].append(spec)
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="ascii")
    print(f"Emitted 16 clamp shaders, 12 explicit-mip controls and one LOD control under {OUT}")
    if llvm:
        print("LLVM round-tripped all 29 shaders and checked the exact encodings of eight clamp mnemonics")


def read_rgba(directory, stem):
    path = directory / f"{stem}.rgba32f"
    raw = path.read_bytes()
    words = bias_probe.SIDE ** 2 * 4
    if len(raw) != words * 4:
        raise ValueError(f"{path.name} expected {words * 4} bytes of tightly packed RGBA32F, got {len(raw)}")
    return struct.unpack(f"<{words}f", raw)


def classify(directory):
    lod = read_rgba(directory, LOD_STEM)
    for index in range(0, len(lod), 4):
        r, g, b, a = lod[index:index + 4]
        if not (abs(r - 1) <= LOD_TOLERANCE and abs(g - 1) <= LOD_TOLERANCE and b == a == 0):
            y, x = divmod(index // 4, bias_probe.SIDE)
            raise ValueError(f"{LOD_STEM} pixel=({x},{y}) expected=(1,1,0,0) actual=({r:g},{g:g},{b:g},{a:g})")
    candidates = set(MODELS)
    for form in bias_probe.FORMS:
        layout = layout_name(form)
        controls = [read_rgba(directory, control_stem(form, mip)) for mip in range(bias_probe.MIPS)]
        validate_controls(form, controls)
        for trial in TRIALS:
            stem = f"{layout}-{trial.name}"
            try:
                mip = matching_mip(controls, read_rgba(directory, stem))
            except ValueError as error:
                raise ValueError(f"{stem} {error}") from error
            models = {model for model, expected in zip(MODELS, trial.mips, strict=True) if expected == mip}
            candidates.intersection_update(models)
            print(f"{stem} mip={mip} matches={','.join(sorted(models)) or 'none'}")
    if len(candidates) != 1:
        raise ValueError(f"no unique clamp interpretation across all draws, candidates={sorted(candidates)}")
    print(f"All 16 draws and 13 controls agree with {candidates.pop()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--emit", action="store_true", help="write source, synthetic textures and the manifest below out/")
    mode.add_argument("--binary", nargs=3, metavar=("LLVM_MC", "LLVM_OBJCOPY", "LIBLLVM"),
                      help="emit and round-trip all shaders with LLVM")
    mode.add_argument("--classify", type=Path, metavar="READBACK_DIR",
                      help="identify a clamp interpretation from <stem>.rgba32f for each manifest draw")
    args = parser.parse_args()
    if args.classify is not None:
        try:
            classify(args.classify)
        except (OSError, ValueError) as error:
            parser.exit(1, f"Readback classification failed: {error}\n")
        return
    cpu_check()
    if args.emit or args.binary:
        emit(args.binary)


if __name__ == "__main__":
    main()
