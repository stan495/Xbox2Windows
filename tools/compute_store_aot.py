"""Translate bounded raw-UAV gfx1013 compute programs under explicit bindings."""
import argparse
import hashlib
import json
import re
import struct
from pathlib import Path

from pixel_aot import INTEGER, container_code, disassemble
from resource_inventory import parts


CONSTANT_WORDS = 16
AXES = "xyz"
# Unsigned VOPC compares a lane guard may use: v_cmp_<op>_u32 a, b is (a <op> b) per lane.
GUARD_COMPARES = {"gt": ">", "lt": "<", "ge": ">=", "le": "<="}
# The data ALU subset: the unsigned 32-bit operations the LLVM 15 code generator emits for the
# read-compute-write fixture, on known words only. v_and_b32, v_or_b32, v_xor_b32 and v_lshrrev_b32 (src1 >> src0[4:0])
# reuse the pixel profile's integer templates, as does v_mul_lo_u32. v_lshlrev_b32 and v_ashrrev_i32 shift src1
# by src0[4:0], the latter arithmetically; v_sub_nc_u32 is src0 - src1 and v_subrev_nc_u32 src1 - src0, both
# modulo 2^32. v_mad_u32_u24 is
# src0[23:0] * src1[23:0] + src2, modulo 2^32. None of them writes vcc, so the guard compare stays valid.
ALU = {
    "v_and_b32_e32": f"({INTEGER['and_b32'][1]})",
    "v_lshrrev_b32_e32": f"({INTEGER['lshrrev_b32'][1]})",
    "v_lshlrev_b32_e32": f"({INTEGER['lshlrev_b32'][1]})",
    "v_ashrrev_i32_e32": INTEGER["ashrrev_i32"][1],
    "v_sub_nc_u32_e32": "({0} - {1})",
    "v_subrev_nc_u32_e32": "({1} - {0})",
    "v_min_u32_e32": "min({0}, {1})",
    "v_max_u32_e32": "max({0}, {1})",
    "v_add_nc_u32_e32": "({0} + {1})",
    "v_or_b32_e32": f"({INTEGER['or_b32'][1]})",
    "v_xor_b32_e32": f"({INTEGER['xor_b32'][1]})",
    "v_mul_lo_u32": INTEGER["mul_lo_u32"][1],
    "v_mad_u32_u24": "((({0} & 0xffffffu) * ({1} & 0xffffffu)) + {2})",
}
# The scalar ALU subset, on known words only: s_<op> sD, a, b with two scalar sources (an SGPR holding a known
# word, an integer inline constant or a 32-bit literal). The carry out of s_add_u32 and s_sub_u32 and the
# nonzero flag of the logic forms go to SCC, which no accepted instruction reads. s_lshr_b32 is a logical
# shift of src0 by src1[4:0].
SALU = {
    "s_add_u32": "({0} + {1})",
    "s_sub_u32": "({0} - {1})",
    "s_and_b32": "({0} & {1})",
    "s_or_b32": "({0} | {1})",
    "s_xor_b32": "({0} ^ {1})",
    "s_lshr_b32": "({0} >> ({1} & 31u))",
}
# The binary32 vector ALU subset: VOP2 src0 is any source, src1 a VGPR. v_sub_f32 is src0 - src1 and v_subrev_f32
# is src1 - src0. Each operation rounds once to nearest even on the bit pattern of a uint word, so a result is again
# a word. The helpers keep the one rounding per operation: a multiply followed by an add must not be contracted.
# Fused multiply-add (v_fma_f32, v_fmac_f32) is not in the subset: DXIL has no binary32 fused form to lower it to.
# NaN payloads, infinities and denormals are not modelled. A literal that is one is refused; a word read from memory
# is not known statically, so the caller must keep its operands and results finite and normal.
FALU = {
    "v_add_f32_e32": "f32_add({0}, {1})",
    "v_sub_f32_e32": "f32_sub({0}, {1})",
    "v_subrev_f32_e32": "f32_sub({1}, {0})",
    "v_mul_f32_e32": "f32_mul({0}, {1})",
}
F32_HELPERS = {
    name: f"uint f32_{name}(uint a, uint b) {{ precise float r = asfloat(a) {op} asfloat(b); return asuint(r); }}\n"
    for name, op in (("add", "+"), ("sub", "-"), ("mul", "*"))
}
# The inline float constants of the encoding, as LLVM prints them, and the bits they hold.
FLOAT_INLINE = {
    "0.5": 0x3f000000, "-0.5": 0xbf000000, "1.0": 0x3f800000, "-1.0": 0xbf800000, "2.0": 0x40000000,
    "-2.0": 0xc0000000, "4.0": 0x40800000, "-4.0": 0xc0800000, "0.15915494": 0x3e22f983,
}
FLOAT_SOURCE = r"(s\d+|v\d+|-?\d+\.\d+|0x[0-9a-f]+|-?\d+)"
SALU_SOURCE = r"(s\d+|-?\d+|0x[0-9a-f]+)"
ALU_SOURCE = r"(s\d+|v\d+|-?\d+|0x[0-9a-f]+)"
# Results are inline HLSL expressions; a longer one is rejected instead of growing without bound.
ALU_EXPRESSION_LIMIT = 1024
# Caller guarantees for a MIMG image descriptor: an 8-dword resource descriptor that names a 2D,
# linear (non-swizzled) image of the listed format. The translator checks where the eight words were
# loaded from, not the 9-bit GFX10 format, type, swizzle, level or size fields inside them; the two
# 16-byte descriptor slots are bound to t0 for reads or u0 for writes.
# Value: (HLSL texel type, channel count).
IMAGE_FORMATS = {"R32G32B32A32_UINT": ("uint4", 4), "R8G8B8A8_UINT": ("uint4", 4)}
# Template operands are the old LDS word and the source VGPR, both HLSL uints.
LDS_RMW = {
    "ds_add_u32": "({0} + {1})",
    "ds_sub_u32": "({0} - {1})",
    "ds_and_b32": "({0} & {1})",
    "ds_or_b32": "({0} | {1})",
    "ds_xor_b32": "({0} ^ {1})",
    "ds_rsub_u32": "({1} - {0})",
    "ds_min_i32": "asuint(min(asint({0}), asint({1})))",
    "ds_max_i32": "asuint(max(asint({0}), asint({1})))",
    "ds_min_u32": "min({0}, {1})",
    "ds_max_u32": "max({0}, {1})",
}

MUBUF_ATOMIC_METHODS = {
    "add": "Add", "sub": "Add", "and": "And", "or": "Or", "xor": "Xor",
    "smin": "Min", "umin": "Min", "smax": "Max", "umax": "Max",
    "swap": "Exchange", "cmpswap": "CompareExchange",
}

IMAGE_ATOMIC_OPS = "add|sub|and|or|xor|smin|smax|umin|umax|swap|cmpswap"
IMAGE_ATOMIC_TYPES = {"R32_UINT": "uint", "R32_SINT": "int"}


def checked_group(group):
    """Return (x, y, z) within the D3D12 cs_6_0 numthreads limits."""
    x, y, z = (group, 1, 1) if isinstance(group, int) else tuple(group)
    if not (all(isinstance(n, int) for n in (x, y, z)) and 1 <= x <= 1024 and 1 <= y <= 1024
            and 1 <= z <= 64 and x * y * z <= 1024):
        raise ValueError("Unsupported thread group")
    return x, y, z


def group_size(psv):
    size = struct.unpack_from("<I", psv)[0] if len(psv) >= 4 else 0
    if not 48 <= size <= len(psv) - 4 or psv[28] != 5:
        raise ValueError("Expected compute PSV runtime info with thread dimensions")
    return checked_group(struct.unpack_from("<3I", psv, 40))


def is_word(value):
    return isinstance(value, int) or (isinstance(value, tuple)
                                      and value[0] in ("constant_word", "thread", "source", "alu"))


def alu_data_word(value):
    """A known word that is not a dispatch index: a shift of one is data, where a shifted index is a byte offset."""
    return is_word(value) and not (isinstance(value, tuple) and value[0] == "thread")


def hlsl_word(value):
    if isinstance(value, int):
        return f"0x{value:08x}u"
    if value[0] == "source":
        # The source binding is read-only, so the read may sit where the word is used.
        return f"inputData.Load({value[1]})"
    if value[0] == "alu":
        return value[1]
    return f"tid.{AXES[value[1]]}" if value[0] == "thread" else f"params[{value[1] // 4}].{'xyzw'[value[1] % 4]}"


def alu_word(token, scalar, vector, pending):
    """The known word an ALU source names, or None: a VGPR, a loaded SGPR, an inline constant or a literal."""
    if token[0] == "v":
        value = vector.get(int(token[1:]))
    elif token[0] == "s":
        n = int(token[1:])
        value = scalar.get(n) if n <= 105 and n not in pending else None
    else:
        value = int(token, 0) & 0xffffffff if -(1 << 31) <= int(token, 0) <= 0xffffffff else None
    return value if is_word(value) else None


def float_word(token, scalar, vector, pending):
    """The known word a binary32 source names, or None: as alu_word, but a constant must be a finite normal value or zero."""
    word = FLOAT_INLINE.get(token) if "." in token else alu_word(token, scalar, vector, pending)
    if isinstance(word, int) and ((word >> 23) & 0xff == 0xff or ((word >> 23) & 0xff == 0 and word & 0x7fffff)):
        return None
    return word


def vector_operands(line):
    """VGPR numbers named by an instruction, single or as a range."""
    regs = set()
    for single, first, last in re.findall(r"\bv(\d+)\b|\bv\[(\d+):(\d+)\]", line):
        regs.update([int(single)] if single else range(int(first), int(last) + 1))
    return regs


def labelled(decoded):
    """Name each s_cbranch_execz target as a label; decoded is [(byte offset, instruction)].

    Only forward targets at a decoded instruction boundary are named. lower() then checks
    where the label sits.
    """
    starts = {pc for pc, _ in decoded}
    targets, lines = set(), []
    for pc, line in decoded:
        if m := re.fullmatch(r"s_cbranch_execz (-?\d+)", line):
            # A backward target, printed signed or as its 16-bit pattern, is never accepted.
            target = pc + 4 + 4 * int(m[1])
            if target <= pc or target not in starts:
                raise ValueError("Branch outside decoded instruction boundaries")
            targets.add(target)
            line = f"s_cbranch_execz .L{target}"
        lines.append((pc, line))
    return [item for pc, line in lines for item in ([f".L{pc}:"] if pc in targets else []) + [line]]


MUBUF_UINT_FORMATS = tuple(f"BUF_FMT_{'32_' * count}UINT" for count in range(1, 5))
MUBUF_SOURCE_FORMAT = MUBUF_UINT_FORMATS[-1]
MUBUF_D16_OUTPUT_FORMATS = {1: "BUF_FMT_16_UINT", 2: "BUF_FMT_16_16_UINT", 4: "BUF_FMT_16_16_16_16_UINT"}
MUBUF_OUTPUT_FORMATS = (*MUBUF_UINT_FORMATS, *MUBUF_D16_OUTPUT_FORMATS.values())
MUBUF_TEXEL_BYTES = 16
MUBUF_D16_SOURCE_FORMAT = "BUF_FMT_16_16_16_16_UINT"
MUBUF_D16_TEXEL_BYTES = 8
MUBUF_SOURCE_TEXELS = {MUBUF_SOURCE_FORMAT: MUBUF_TEXEL_BYTES, MUBUF_D16_SOURCE_FORMAT: MUBUF_D16_TEXEL_BYTES}


def lower(instructions, group, *, table_reg, descriptor_offset=None, group_reg=None, stride=None,
          constant_descriptor=None, group_reg_y=None, group_reg_z=None, source_descriptor=None,
          source_stride=None, source_format=None, output_format=None, image_descriptor=None, image_format=None,
          image_uav_mip=None, cpu="gfx1013", lds_bytes=0, lds_f32_mode=None, lds_f64_mode=None):
    """v0, v1 and v2 are local X, Y and Z; group_reg, group_reg_y and group_reg_z are the group IDs.

    gfx10 compute waves receive each local ID in its own VGPR. Y and Z are bound only when that
    group dimension is larger than one. Descriptors name u0 and optionally b0 and a read-only raw
    source t0; source_stride is the t0 byte stride that indexed reads use. source_format declares
    the MUBUF source descriptor format, and output_format declares the u0 descriptor format
    for formatted MUBUF stores. Both declarations require identity channel selectors.
    image_descriptor is the table byte offset of the 8-dword image descriptor. image_format is
    the caller's statement of the image format inside it. The image binds to t0 for loads or u0
    for writes, where image_uav_mip selects the writable subresource and replaces descriptor_offset.
    R32_UINT selects the 4-sample Texture2DMS profile for an SRV or the
    non-returning 2D integer atomic profile for a UAV. R32_SINT is UAV-only
    and selects signed min/max.
    """
    dims = checked_group(group)
    if cpu not in ("gfx1013", "gfx1030"):
        raise ValueError("Unsupported compute target")
    ds_group = cpu == "gfx1030" and dims == (32, 1, 1)
    if not 0 <= table_reg <= 105:
        raise ValueError("Invalid table or thread group binding")
    image_output = image_uav_mip is not None
    image_msaa = not image_output and image_format == "R32_UINT"
    if image_msaa and cpu != "gfx1030":
        raise ValueError("The R32_UINT MSAA image binding requires gfx1030")
    image_atomic_type = IMAGE_ATOMIC_TYPES.get(image_format) if image_output else None
    if image_output:
        if (cpu != "gfx1030" or descriptor_offset is not None or stride is not None
                or image_descriptor is None or (image_format not in IMAGE_FORMATS and not image_atomic_type)
                or (image_atomic_type and image_uav_mip != 0)
                or not 0 <= image_uav_mip <= 15):
            raise ValueError("Invalid image UAV binding")
        descriptor_bindings = {}
    else:
        if descriptor_offset is None or not 0 <= descriptor_offset < 1 << 20 or descriptor_offset % 4:
            raise ValueError("Invalid table or thread group binding")
        descriptor_bindings = {descriptor_offset: "uav"}
    if constant_descriptor is not None:
        if (not 0 <= constant_descriptor < 1 << 20 or constant_descriptor % 4
                or any(abs(constant_descriptor - offset) < 16 for offset in descriptor_bindings)):
            raise ValueError("Invalid or overlapping constant descriptor binding")
        descriptor_bindings[constant_descriptor] = "cbv"
    if source_descriptor is not None:
        if (not 0 <= source_descriptor < 1 << 20 or source_descriptor % 4
                or any(abs(source_descriptor - offset) < 16 for offset in descriptor_bindings)):
            raise ValueError("Invalid or overlapping source descriptor binding")
        descriptor_bindings[source_descriptor] = "srv"
    if source_stride is not None and (source_descriptor is None or not 4 <= source_stride <= 0x3fff
                                      or source_stride % 4):
        raise ValueError("Indexed reads require a source binding and aligned stride")
    source_texel_bytes = MUBUF_SOURCE_TEXELS.get(source_format)
    if source_format is not None and (source_texel_bytes is None or cpu != "gfx1030"
                                      or source_stride is None or source_stride % source_texel_bytes):
        raise ValueError("Unsupported formatted source binding")
    if image_descriptor is not None or image_format is not None:
        # The 8-dword image descriptor occupies two 16-byte slots.
        if (not (image_format in IMAGE_FORMATS or image_msaa or image_atomic_type)
                or image_descriptor is None or source_descriptor is not None
                or not 0 <= image_descriptor < 1 << 20 or image_descriptor % 4
                or any(image_descriptor + half in descriptor_bindings for half in (0, 16))
                or any(abs(image_descriptor + half - offset) < 16
                       for half in (0, 16) for offset in descriptor_bindings)):
            raise ValueError("Invalid or overlapping image descriptor binding")
        descriptor_bindings[image_descriptor] = ("image", 0)
        descriptor_bindings[image_descriptor + 16] = ("image", 1)
    group_regs = {axis: reg for axis, reg in enumerate((group_reg, group_reg_y, group_reg_z)) if reg is not None}
    if (any(not 0 <= reg <= 105 or reg == table_reg for reg in group_regs.values())
            or len(set(group_regs.values())) != len(group_regs)):
        raise ValueError("Invalid group register")
    if stride is not None and (not group_regs or not 4 <= stride <= 0x3fff or stride % 4):
        raise ValueError("Indexed stores require a group register and aligned stride")
    if output_format is not None and (output_format not in MUBUF_OUTPUT_FORMATS or cpu != "gfx1030"
                                      or stride is None):
        raise ValueError("Unsupported formatted output binding")
    scalar = {table_reg: "table"}
    for axis, reg in group_regs.items():
        scalar[reg] = ("group", axis)
    vector = {axis: ("local", axis) for axis in range(3) if axis == 0 or dims[axis] > 1}
    pending = set()
    # VGPRs a buffer read still writes, with the word they receive (None when unknown);
    # they enter vector only at s_waitcnt vmcnt(0).
    loading = {}
    stores = []
    atomic_update = False
    # DS operations and image loads can consume each other's temporaries. Keep them in instruction order.
    staged_steps = []
    ds_step_count = 0
    image_step_count = 0
    lds_enabled = ds_group and 128 <= lds_bytes <= 65536
    lds_word = None
    lds_strided_words = {}
    lds_strided_width = None
    lds_wide_words = {}
    lds_address_shift = None
    lds_m0_zero = False
    ds_wait = False
    written_spans = []
    layout = None
    # One per-lane guard: v_cmp into vcc_lo, s_and_saveexec_b32, the stores under the mask, then
    # s_or_b32 restoring exec or the final exit. An optional s_cbranch_execz must land exactly
    # where the guarded stores end. The guarded stores become an HLSL if.
    vcc = guard = None
    guard_span = None
    if not instructions or instructions[-1] != "s_endpgm":
        raise ValueError("Missing final program exit")

    for line in instructions[:-1]:
        if ds_wait:
            if line != "s_waitcnt lgkmcnt(0)":
                raise ValueError("Expected immediate s_waitcnt lgkmcnt(0) after DS")
            ds_wait = False
        if m := re.fullmatch(r"(\.L\w+):", line):
            if guard is None or guard["label"] != m[1] or guard["at_label"]:
                raise ValueError("Unverified branch target")
            guard["at_label"] = True
            continue
        if (guard is not None and guard["at_label"]
                and not re.fullmatch(r"s_waitcnt_depctr 0x[0-9a-f]+|s_nop \d+|s_or_b32 .*", line)):
            raise ValueError("Lane guard branch does not reach its restore")
        if re.fullmatch(r"s_inst_prefetch (?:0x[0-9a-f]+|\d+)|s_nop \d+|s_waitcnt_depctr 0x[0-9a-f]+", line):
            continue
        if m := re.fullmatch(r"s_clause (0x[0-9a-f]+|\d+)", line):
            if int(m[1], 0) > 0x3f:
                raise ValueError("Invalid s_clause operand")
            continue
        if not line.startswith("s_waitcnt") and vector_operands(line) & loading.keys():
            raise ValueError("Vector load result used or overwritten before s_waitcnt vmcnt(0)")
        if line in ("s_waitcnt lgkmcnt(0)", "s_waitcnt lgkmcnt(0) vmcnt(0)", "s_waitcnt vmcnt(0) lgkmcnt(0)",
                    "s_waitcnt vmcnt(0)"):
            if "lgkmcnt" in line:
                pending.clear()
            if "vmcnt" in line:
                for reg, value in loading.items():
                    if value is None:
                        vector.pop(reg, None)
                    else:
                        vector[reg] = value
                loading.clear()
        elif image_output and stores and line == "s_waitcnt_vscnt null, 0x0":
            # The single image write becomes visible when the dispatch completes.
            continue
        elif m := re.fullmatch(r"v_cmp_(gt|lt|ge|le)_u32_e32 vcc_lo, (s\d+|v\d+|-?\d+|0x[0-9a-f]+), v(\d+)", line):
            op, src, right = m[1], m[2], int(m[3])
            if src[0] in "sv":
                n = int(src[1:])
                left = (None if src[0] == "s" and (n > 105 or n in pending) or src[0] == "v" and n > 255
                        else (scalar if src[0] == "s" else vector).get(n))
            else:
                value = int(src, 0)
                left = value & 0xffffffff if -(1 << 31) <= value <= 0xffffffff else None
            # A word read or computed in the program is not a verified guard operand.
            if (right > 255 or not all(is_word(word) and not (isinstance(word, tuple) and word[0] in ("source", "alu"))
                                       for word in (left, vector.get(right)))):
                raise ValueError("Unverified lane guard compare")
            vcc = (op, left, vector[right])
        elif m := re.fullmatch(r"s_and_saveexec_b32 s(\d+), vcc_lo", line):
            saved = int(m[1])
            if guard_span is not None or vcc is None or saved > 105 or saved in pending:
                raise ValueError("Unverified or repeated lane guard")
            guard = dict(label=None, at_label=False, holders=None,
                         scalar=dict(scalar), vector=dict(vector), pending=set(pending), loading=dict(loading))
            scalar[saved] = ("exec",)
            guard_span = [len(stores), None, vcc]
        elif m := re.fullmatch(r"s_cbranch_execz (\.L\w+)", line):
            # Taken only when no lane passed the guard; it must skip exactly the guarded stores.
            if guard is None:
                raise ValueError("Unverified lane guard branch")
            guard["label"] = m[1]
            # A taken branch skips the block: only an SGPR that holds the mask here holds it on both paths.
            holders = {reg for reg, value in scalar.items() if value == ("exec",)}
            guard["holders"] = holders if guard["holders"] is None else guard["holders"] & holders
        elif m := re.fullmatch(r"s_or_b32 exec_lo, (?:exec_lo, s(\d+)|s(\d+), exec_lo)", line):
            # Any SGPR that still holds the saved mask restores it, on the skipped path too.
            reg = int(m[1] or m[2])
            if (guard is None or scalar.get(reg) != ("exec",)
                    or guard["label"] is not None and (not guard["at_label"] or reg not in guard["holders"])):
                raise ValueError("Unverified lane guard restore")
            # Registers written under the guard keep their old values in the lanes that did not
            # pass it, or in the whole wave when the branch skips the block: unknown after the
            # restore. A wait under the guard may have been skipped, so earlier loads stay pending.
            # A load from before the guard keeps its word only if its register still holds that
            # word on the guarded path, waited for or not. Any other loaded register, including a
            # load issued under the guard (it writes only the lanes that passed), is unknown after
            # its wait.
            loading = {reg: guard["loading"].get(reg) if loading.get(reg, vector.get(reg)) == guard["loading"].get(reg)
                       else None for reg in guard["loading"].keys() | loading.keys()}
            for state, before in ((scalar, guard["scalar"]), (vector, guard["vector"])):
                for reg in [reg for reg, value in state.items() if reg not in before or before[reg] != value]:
                    del state[reg]
            pending |= guard["pending"]
            guard = None
            guard_span[1] = len(stores)
        elif line in ("s_mov_b32 m0, 0", "s_mov_b32 m0, 0x0"):
            if not lds_enabled or guard_span is not None:
                raise ValueError("Unverified private LDS thread-ID base")
            lds_m0_zero = True
        elif m := re.fullmatch(r"s_mov_b32 s(\d+), (s\d+|-?\d+|0x[0-9a-f]+)", line):
            dst, src = int(m[1]), m[2]
            if dst in pending or dst > 105:
                raise ValueError("Invalid scalar destination or pending load")
            if src.startswith("s"):
                n = int(src[1:])
                if n > 105 or n in pending or n not in scalar:
                    raise ValueError("Unknown or pending scalar source")
                scalar[dst] = scalar[n]
            else:
                value = int(src, 0)
                if not -(1 << 31) <= value <= 0xffffffff:
                    raise ValueError("Invalid scalar immediate")
                scalar[dst] = value & 0xffffffff
        elif m := re.fullmatch(rf"(s_(?:add_u32|sub_u32|and_b32|or_b32|xor_b32|lshr_b32)) s(\d+), {SALU_SOURCE}, {SALU_SOURCE}",
                               line):
            op, dst = m[1], int(m[2])
            words = [alu_word(token, scalar, vector, pending) for token in m.groups()[2:]]
            if dst > 105 or dst in pending or None in words:
                raise ValueError("Unverified scalar ALU operand")
            expression = SALU[op].format(*map(hlsl_word, words))
            if len(expression) > ALU_EXPRESSION_LIMIT:
                raise ValueError("ALU expression too large")
            scalar[dst] = ("alu", expression)
        elif m := re.fullmatch(r"s_movk_i32 s(\d+), (-?\d+|0x[0-9a-f]+)", line):
            dst = int(m[1])
            value = int(m[2], 0)
            if dst in pending or dst > 105 or not -0x8000 <= value <= 0xffff:
                raise ValueError("Invalid scalar destination or movk immediate")
            signed = value - 0x10000 if value >= 0x8000 else value
            scalar[dst] = signed & 0xffffffff
        elif m := re.fullmatch(r"s_lshl_b32 s(\d+), s(\d+), (\d+)", line):
            dst, group, shift = map(int, m.groups())
            tag = scalar.get(group)
            axis = tag[1] if isinstance(tag, tuple) and tag[0] == "group" else None
            # A group's first dispatch index, group * size, only for a power-of-two size.
            if (dst in pending or dst > 105 or axis is None or dims[axis] & (dims[axis] - 1)
                    or shift != dims[axis].bit_length() - 1):
                raise ValueError("Unverified group base")
            scalar[dst] = ("group_base", axis)
        elif m := re.fullmatch(r"s_load_dwordx([48]) s\[(\d+):(\d+)\], s\[(\d+):(\d+)\], (null|0x[0-9a-f]+)", line):
            # x8 loads two descriptors bound at consecutive 16-byte slots of the table.
            count = int(m[1])
            dest, last, base, high = map(int, m.groups()[1:5])
            offset = 0 if m[6] == "null" else int(m[6], 16)
            regs = range(dest, last + 1)
            slots = range(offset, offset + 4 * count, 16)
            if (dest % 4 or last != dest + count - 1 or last > 105 or base % 2 or high != base + 1
                    or high > 105 or pending.intersection((*regs, base, high))
                    or (scalar.get(base), scalar.get(high)) != ("table", 0)
                    or any(slot not in descriptor_bindings for slot in slots)):
                raise ValueError("Unresolved compute descriptor load")
            for word, reg in enumerate(regs):
                scalar[reg] = (descriptor_bindings[slots[word // 4]], word % 4)
            pending.update(regs)
        elif m := re.fullmatch(r"(?:s_buffer_load_dwordx([24]) s\[(\d+):(\d+)\]|s_buffer_load_dword s(\d+)), "
                               r"s\[(\d+):(\d+)\], (null|0x[0-9a-f]+)", line):
            count = int(m[1] or 1)
            dest, last = (int(m[4]), int(m[4])) if m[4] else (int(m[2]), int(m[3]))
            base, high = int(m[5]), int(m[6])
            offset = 0 if m[7] == "null" else int(m[7], 16)
            regs = range(dest, last + 1)
            if (dest % count or last != dest + count - 1 or last > 105
                    or base % 4 or high != base + 3 or high > 105
                    or offset % 4 or offset // 4 + count > CONSTANT_WORDS
                    or pending.intersection((*regs, *range(base, high + 1)))
                    or any(scalar.get(base + i) != ("cbv", i) for i in range(4))):
                raise ValueError("Unresolved or out-of-range compute constant read")
            for word, reg in enumerate(regs, offset // 4):
                scalar[reg] = ("constant_word", word)
            pending.update(regs)
        elif m := re.fullmatch(r"v_mov_b32(?:_e32|_e64)? v(\d+), (s\d+|v\d+|-?\d+|0x[0-9a-f]+)", line):
            dst, src = int(m[1]), m[2]
            if dst > 255:
                raise ValueError("Invalid vector destination")
            if src.startswith("s"):
                n = int(src[1:])
                val = scalar.get(n)
                if n > 105 or n in pending or not is_word(val):
                    raise ValueError("Unresolved scalar move source")
                vector[dst] = val
            elif src.startswith("v"):
                n = int(src[1:])
                val = vector.get(n)
                if n > 255 or not is_word(val):
                    raise ValueError("Unresolved vector move source")
                vector[dst] = val
            else:
                value = int(src, 0)
                if not -(1 << 31) <= value <= 0xffffffff:
                    raise ValueError("Invalid literal move")
                vector[dst] = value & 0xffffffff
        elif m := re.fullmatch(r"v_lshl_add_u32 v(\d+), s(\d+), (\d+), v(\d+)", line):
            dst, group, shift, local = map(int, m.groups())
            tag = scalar.get(group)
            axis = tag[1] if isinstance(tag, tuple) and tag[0] == "group" else None
            # The dispatch-thread ID is group * size + local only for a power-of-two size.
            if (dst > 255 or group in pending or axis is None or dims[axis] & (dims[axis] - 1)
                    or shift != dims[axis].bit_length() - 1 or vector.get(local) != ("local", axis)):
                raise ValueError("Unverified dispatch index")
            vector[dst] = ("thread", axis)
        elif m := re.fullmatch(r"v_add_lshl_u32 v(\d+), s(\d+), v(\d+), (\d+)", line):
            dst, base, local, shift = map(int, m.groups())
            tag = scalar.get(base)
            # (group base + local ID of the same axis) << shift: the byte offset of a dispatch
            # index, as from v_lshlrev_b32. The store checks bound the shift.
            if (dst > 255 or not (isinstance(tag, tuple) and tag[0] == "group_base")
                    or vector.get(local) != ("local", tag[1])):
                raise ValueError("Unverified dispatch byte offset")
            vector[dst] = ("thread_bytes", tag[1], shift)
        elif (ds_group and (m := re.fullmatch(r"v_add_nc_u32_e32 v(\d+), (\d+), v(\d+)", line))
              and vector.get(int(m[3])) == ("local", 0)):
            dst, rotation, _ = map(int, m.groups())
            if dst > 255 or rotation > 31:
                raise ValueError("Unverified DS lane bias")
            vector[dst] = ("ds_biased_lane", rotation)
        elif (ds_group and (m := re.fullmatch(r"v_and_b32_e32 v(\d+), 31, v(\d+)", line))
              and (tag := vector.get(int(m[2]))) and isinstance(tag, tuple) and tag[0] == "ds_biased_lane"):
            dst = int(m[1])
            if dst > 255:
                raise ValueError("Invalid vector destination")
            vector[dst] = ("ds_lane", tag[1])
        elif ((m := re.fullmatch(r"v_lshlrev_b32(?:_e32|_e64)? v(\d+), (\d+), v(\d+)", line))
              and not alu_data_word(vector.get(int(m[3])))):
            dst, shift, src = map(int, m.groups())
            tag = vector.get(src)
            if ds_group and dst <= 255 and shift == 2 and isinstance(tag, tuple) and tag[0] == "ds_lane":
                vector[dst] = ("ds_address", tag[1])
                continue
            if (lds_enabled and dst <= 255 and tag == ("local", 0)
                    and (shift == 2 or (shift == 4 and lds_bytes >= 512))):
                if lds_address_shift is not None and shift != lds_address_shift:
                    raise ValueError("Mixed private LDS address strides")
                lds_address_shift = shift
                vector[dst] = ("lds_address",) if shift == 2 else ("lds_wide_address",)
                continue
            if lds_enabled and dst <= 255 and shift == 3 and tag == ("local", 0):
                vector[dst] = ("lds_strided_qword_address",)
                continue
            # The element and row checks at the store bound the shift.
            if dst > 255 or not (isinstance(tag, tuple) and tag[0] == "thread"):
                raise ValueError("Unverified dispatch byte offset")
            vector[dst] = ("thread_bytes", tag[1], shift)
        elif m := re.fullmatch(r"ds_(b?permute)_b32 v(\d+), v(\d+), v(\d+)", line):
            op, dst, addr, src = m[1], *map(int, m.groups()[1:])
            address = vector.get(addr)
            value = vector.get(src)
            if (not ds_group or guard_span is not None or max(dst, addr, src) > 255
                    or not isinstance(address, tuple) or address[0] != "ds_address" or not is_word(value)
                    or ds_step_count >= 64):
                raise ValueError("Unverified DS lane routing")
            # The bijective address is (local X + bias) mod 32. Scatter reads from its inverse.
            bias = address[1] if op == "bpermute" else (-address[1]) % 32
            slot = f"ds_stage_{ds_step_count}"
            result = f"ds_result_{ds_step_count}"
            ds_step_count += 1
            staged_steps.append((slot, f"    {slot}[local.x] = {hlsl_word(value)};\n"
                                   f"    GroupMemoryBarrierWithGroupSync();\n"
                                   f"    uint {result} = {slot}[(local.x + {bias}u) & 31u];"))
            vector[dst] = ("alu", result)
            ds_wait = True
        elif m := re.fullmatch(r"ds_((?:cmpst|mskor)(?:_rtn)?|wrxchg_rtn)_b32 "
                               r"(v\d+(?:, v\d+){1,3})(?: offset:0)?", line):
            op = m[1]
            returns = op.endswith("_rtn")
            exchange = op == "wrxchg_rtn"
            regs = [int(token[1:]) for token in m[2].split(", ")]
            if len(regs) != (2 if exchange else 3) + returns:
                raise ValueError("Unverified private LDS replacement operands")
            addr, first, *second = regs[returns:]
            data0 = vector.get(first)
            data1 = vector.get(second[0]) if second else None
            if (not lds_enabled or guard_span is not None or max(regs) > 255
                    or vector.get(addr) != ("lds_address",) or lds_word is None or not is_word(data0)
                    or (not exchange and not is_word(data1))):
                raise ValueError("Unverified private LDS replacement")
            if exchange:
                new_word = data0
            else:
                old, a, b = hlsl_word(lds_word), hlsl_word(data0), hlsl_word(data1)
                if op.startswith("cmpst"):
                    expression = f"(({old} == {a}) ? {b} : {old})"
                else:
                    expression = f"(({old} & ~{a}) | {b})"
                if len(expression) > ALU_EXPRESSION_LIMIT:
                    raise ValueError("LDS expression exceeds the profile limit")
                new_word = ("alu", expression)
            if returns:
                vector[regs[0]] = lds_word
            lds_word = new_word
            ds_wait = True
        elif m := re.fullmatch(r"ds_(add|min|max|cmpst)(?P<ret>_rtn)?_f32 (v\d+(?:, v\d+){1,3})(?: offset:0)?", line):
            op = m[1]
            returns = bool(m["ret"])
            regs = [int(reg[1:]) for reg in m[3].split(", ")]
            if lds_f32_mode != "normal-rne":
                raise ValueError("Unverified private LDS floating-point mode")
            expected = 2 + int(returns) + int(op == "cmpst")
            if len(regs) != expected:
                raise ValueError("Unverified private LDS floating-point operands")
            start = int(returns)
            addr, data = regs[start:start + 2]
            value = vector.get(data)
            replacement = vector.get(regs[-1]) if op == "cmpst" else value
            if (guard_span is not None or max(regs) > 255 or vector.get(addr) != ("lds_address",)
                    or not all(map(is_word, (lds_word, value, replacement)))):
                raise ValueError("Unverified private LDS floating-point access")
            old, src = map(hlsl_word, (lds_word, value))
            if op.startswith("add"):
                if ds_step_count >= 64:
                    raise ValueError("Too many private LDS floating-point steps")
                name = f"ds_f32_{ds_step_count}"
                ds_step_count += 1
                staged_steps.append((None, f"    precise float {name} = asfloat({old}) + asfloat({src});"))
                expression = f"asuint({name})"
            else:
                condition = {"min": "<", "max": ">", "cmpst": "=="}[op]
                expression = f"((asfloat({src}) {condition} asfloat({old})) ? {hlsl_word(replacement)} : {old})"
                if len(expression) > ALU_EXPRESSION_LIMIT:
                    raise ValueError("LDS expression exceeds the profile limit")
            if returns:
                vector[regs[0]] = lds_word
            lds_word = ("alu", expression)
            ds_wait = True
        elif m := re.fullmatch(r"ds_(cmpst|min|max)(?P<ret>_rtn)?_f64 "
                               r"(?:(v\[\d+:\d+\]), )?v(\d+), (v\[\d+:\d+\])(?:, (v\[\d+:\d+\]))?"
                               r"(?: offset:(0|8))?", line):
            op, returns, dest, addr, compare, replacement, offset = m.groups()
            if lds_f64_mode != "normal":
                raise ValueError("Unverified private LDS binary64 mode")
            slot = int(offset or 0) // 4
            prev = tuple(lds_wide_words.get(i) for i in range(slot, slot + 2))
            if (guard_span is not None or vector.get(int(addr)) != ("lds_wide_address",)
                    or bool(returns) != (dest is not None) or (op == "cmpst") != (replacement is not None)
                    or not all(map(is_word, prev))):
                raise ValueError("Unverified private LDS binary64 access")
            ranges = []
            for operand in (operand for operand in (compare, replacement, dest) if operand is not None):
                first, last = map(int, operand[2:-1].split(":"))
                if last > 255 or last != first + 1:
                    raise ValueError("Unverified private LDS binary64 range")
                ranges.append((first, last))
            cmp_word = tuple(vector.get(i) for i in ranges[0])
            new_word = tuple(vector.get(i) for i in ranges[1]) if replacement else cmp_word
            if not all(map(is_word, cmp_word + new_word)):
                raise ValueError("Unverified private LDS binary64 source")
            inputs = tuple(map(hlsl_word, prev + cmp_word + new_word))
            if ds_step_count >= 64 or any(len(word) > ALU_EXPRESSION_LIMIT for word in inputs):
                raise ValueError("LDS expression exceeds the profile limit")
            prefix = f"ds_f64_{ds_step_count}"
            ds_step_count += 1
            old_lo, old_hi, cmp_lo, cmp_hi, new_lo, new_hi = (
                f"{prefix}_{suffix}" for suffix in ("old_lo", "old_hi", "cmp_lo", "cmp_hi", "new_lo", "new_hi")
            )
            if op == "cmpst":
                condition = f"({old_lo} == {cmp_lo} && {old_hi} == {cmp_hi})"
            else:
                # For equal signs, compare the full bit patterns in the
                # opposite direction for negative numbers. Handle signs first.
                below = f"({cmp_hi} < {old_hi} || ({cmp_hi} == {old_hi} && {cmp_lo} < {old_lo}))"
                above = f"({cmp_hi} > {old_hi} || ({cmp_hi} == {old_hi} && {cmp_lo} > {old_lo}))"
                opposite = f"(({cmp_hi} ^ {old_hi}) & 0x80000000u) != 0u"
                negative = f"({cmp_hi} & 0x80000000u) != 0u"
                if op == "min":
                    condition = f"({opposite} ? ({negative}) : (({negative}) ? {above} : {below}))"
                else:
                    condition = f"({opposite} ? !({negative}) : (({negative}) ? {below} : {above}))"
            names = (old_lo, old_hi, cmp_lo, cmp_hi, new_lo, new_hi)
            take = f"{prefix}_take"
            statements = [f"    uint {name} = {word};" for name, word in zip(names, inputs)]
            statements.append(f"    bool {take} = {condition};")
            staged_steps.append((None, "\n".join(statements)))
            update = (f"({take} ? {new_lo} : {old_lo})", f"({take} ? {new_hi} : {old_hi})")
            if dest:
                vector.update(zip(ranges[-1], prev))
            lds_wide_words.update((slot + i, ("alu", word)) for i, word in enumerate(update))
            ds_wait = True
        elif m := re.fullmatch(r"ds_wrxchg2_rtn_b64 v\[(\d+):(\d+)\], v(\d+), "
                               r"v\[(\d+):(\d+)\], v\[(\d+):(\d+)\]"
                               r"(?: offset0:([01]))?(?: offset1:([01]))?", line):
            dst, last, addr, first0, last0, first1, last1 = map(int, m.groups()[:7])
            offsets = tuple(int(offset or 0) for offset in m.groups()[7:])
            if (guard_span is not None or vector.get(addr) != ("lds_wide_address",) or offsets[0] == offsets[1]
                    or max(last, last0, last1) > 255 or last != dst + 3 or last0 != first0 + 1 or last1 != first1 + 1):
                raise ValueError("Unverified private paired qword exchange")
            slots = [2 * offset + i for offset in offsets for i in range(2)]
            previous = [lds_wide_words.get(slot) for slot in slots]
            data = [vector.get(reg) for reg in (first0, last0, first1, last1)]
            if not all(map(is_word, previous + data)):
                raise ValueError("Uninitialized private paired qword exchange")
            vector.update(zip(range(dst, last + 1), previous))
            lds_wide_words.update(zip(slots, data))
            ds_wait = True
        elif m := re.fullmatch(
                r"ds_((?:cmpst|mskor)(?:_rtn)?|wrxchg_rtn)_b64 "
                r"(?:(v\[\d+:\d+\]), )?v(\d+), (v\[\d+:\d+\])(?:, (v\[\d+:\d+\]))?"
                r"(?: offset:(0|8))?", line):
            op, dst, addr, data, data2, offset = m.groups()
            returns = op.endswith("_rtn")
            exchange = op.startswith("wrxchg")
            slot = int(offset or 0) // 4
            old_words = (lds_wide_words.get(slot), lds_wide_words.get(slot + 1))
            if (guard_span is not None or vector.get(int(addr)) != ("lds_wide_address",)
                    or returns != (dst is not None) or exchange == (data2 is not None)
                    or not all(map(is_word, old_words))):
                raise ValueError("Unverified private wide LDS replacement")
            ranges = []
            for operand in (item for item in (data, data2, dst) if item is not None):
                first, last = map(int, operand[2:-1].split(":"))
                if last > 255 or last != first + 1:
                    raise ValueError("Unverified private wide LDS replacement range")
                ranges.append((first, last))
            source = tuple(vector.get(reg) for reg in ranges[0])
            other = tuple(vector.get(reg) for reg in ranges[1]) if not exchange else ()
            if not all(map(is_word, source + other)):
                raise ValueError("Unverified private wide LDS replacement source")
            if not exchange:
                old_low, old_high = map(hlsl_word, old_words)
                data_low, data_high = map(hlsl_word, source)
                new_low, new_high = map(hlsl_word, other)
                if op.startswith("cmpst"):
                    equal = f"({old_low} == {data_low} && {old_high} == {data_high})"
                    low, high = f"({equal} ? {new_low} : {old_low})", f"({equal} ? {new_high} : {old_high})"
                else:
                    low = f"(({old_low} & ~{data_low}) | {new_low})"
                    high = f"(({old_high} & ~{data_high}) | {new_high})"
                if max(len(low), len(high)) > ALU_EXPRESSION_LIMIT:
                    raise ValueError("LDS expression exceeds the profile limit")
                source = (("alu", low), ("alu", high))
            if returns:
                vector.update(zip(ranges[-1], old_words))
            lds_wide_words.update(zip(range(slot, slot + 2), source))
            ds_wait = True
        elif m := re.fullmatch(
                r"ds_(read2st64_b(?:32|64)|write2st64_b(?:32|64)|wrxchg2st64_rtn_b(?:32|64)) "
                r"(v\[\d+:\d+\]|v\d+), (v\[\d+:\d+\]|v\d+)"
                r"(?:, (v\[\d+:\d+\]|v\d+))?(?:, (v\[\d+:\d+\]|v\d+))?"
                r" offset0:(\d+) offset1:(\d+)", line):
            op = m[1]
            width = int(op[-2:]) // 8
            reads = not op.startswith("write")
            writes = not op.startswith("read")
            exchange = reads and writes
            addr_text = m[3] if reads else m[2]
            addr = re.fullmatch(r"v(\d+)", addr_text)
            address_tag = ("lds_address",) if width == 4 else ("lds_strided_qword_address",)
            offsets = tuple(map(int, m.groups()[-2:]))
            # Keep strided planes above the contiguous private records, and
            # use a single element width so no two lane-owned planes alias.
            if (guard_span is not None or not addr or vector.get(int(addr[1])) != address_tag
                    or lds_strided_width not in (None, width) or max(offsets) > 255
                    or min(offsets) * 64 * width < 512 or (max(offsets) * 64 + 32) * width > lds_bytes
                    or writes != (m[4] is not None) or writes and offsets[0] == offsets[1]):
                raise ValueError("Unverified private strided LDS access")
            lds_strided_width = width
            count = width // 4
            if writes:
                sources = (m[4], m[5]) if exchange else (m[3], m[4])
                if not all(sources) or not exchange and m[5] is not None:
                    raise ValueError("Unverified private strided LDS operands")
                pairs = []
                for source in sources:
                    regs = re.fullmatch(r"v(\d+)" if count == 1 else r"v\[(\d+):(\d+)\]", source)
                    if not regs or count == 2 and int(regs[2]) != int(regs[1]) + 1:
                        raise ValueError("Unverified private strided LDS source range")
                    first = int(regs[1])
                    words = tuple(vector.get(reg) for reg in range(first, first + count))
                    if first + count > 256 or not all(map(is_word, words)):
                        raise ValueError("Unverified private strided LDS source")
                    pairs.append(words)
            if reads:
                regs = re.fullmatch(r"v\[(\d+):(\d+)\]", m[2])
                if not regs or int(regs[2]) > 255 or int(regs[2]) - int(regs[1]) + 1 != 2 * count:
                    raise ValueError("Unverified private strided LDS destination range")
                if not all(offset in lds_strided_words for offset in offsets):
                    raise ValueError("Uninitialized private strided LDS plane")
                first = int(regs[1])
                words = lds_strided_words[offsets[0]] + lds_strided_words[offsets[1]]
                vector.update(zip(range(first, first + 2 * count), words))
            if writes:
                lds_strided_words.update(zip(offsets, pairs))
            ds_wait = True
        elif m := re.fullmatch(r"ds_(write|read)_b(64|96|128) (v\[\d+:\d+\]|v\d+), "
                               r"(v\[\d+:\d+\]|v\d+)(?: offset:(0|8))?", line):
            write = m[1] == "write"
            count = int(m[2]) // 32
            addr_text, regs_text = (m[3], m[4]) if write else (m[4], m[3])
            addr = re.fullmatch(r"v(\d+)", addr_text)
            regs = re.fullmatch(r"v\[(\d+):(\d+)\]", regs_text)
            first_slot = int(m[5] or 0) // 4
            if (guard_span is not None or not addr or not regs or int(addr[1]) > 255
                    or vector.get(int(addr[1])) != ("lds_wide_address",) or first_slot + count > 4):
                raise ValueError("Unverified private wide LDS access")
            first, last = map(int, regs.groups())
            if last > 255 or last - first + 1 != count:
                raise ValueError("Unverified private wide LDS register range")
            slots = range(first_slot, first_slot + count)
            registers = range(first, last + 1)
            if write:
                words = [vector.get(reg) for reg in registers]
                if not all(map(is_word, words)):
                    raise ValueError("Unverified private wide LDS source")
                lds_wide_words.update(zip(slots, words))
            else:
                if not all(slot in lds_wide_words for slot in slots):
                    raise ValueError("Uninitialized private wide LDS word")
                vector.update(zip(registers, (lds_wide_words[slot] for slot in slots)))
            ds_wait = True
        elif m := re.fullmatch(r"ds_wrxchg2_rtn_b32 v\[(\d+):(\d+)\], v(\d+), v(\d+), v(\d+)"
                               r"(?: offset0:([0-3]))?(?: offset1:([0-3]))?", line):
            first, last, addr, src0, src1 = map(int, m.groups()[:5])
            slots = tuple(int(offset or 0) for offset in m.groups()[5:])
            if (guard_span is not None or max(last, addr, src0, src1) > 255 or last != first + 1
                    or vector.get(addr) != ("lds_wide_address",) or slots[0] == slots[1]):
                raise ValueError("Unverified private paired dword exchange")
            previous = [lds_wide_words.get(slot) for slot in slots]
            replacement = [vector.get(reg) for reg in (src0, src1)]
            if not all(map(is_word, previous + replacement)):
                raise ValueError("Uninitialized private paired dword exchange")
            vector.update(zip(range(first, last + 1), previous))
            lds_wide_words.update(zip(slots, replacement))
            ds_wait = True
        elif m := re.fullmatch(r"ds_(append|consume) v(\d+)(?: offset:0)?", line):
            op, dst = m[1], int(m[2])
            if (not lds_m0_zero or guard_span is not None or dst > 255
                    or lds_address_shift != 2 or not is_word(lds_word) or ds_step_count >= 64):
                raise ValueError("Unverified LDS wave counter")
            old = hlsl_word(lds_word)
            slot = f"ds_stage_{ds_step_count}"
            previous = f"ds_result_{ds_step_count}"
            ds_step_count += 1
            # LDS byte zero is lane zero's word. Snapshot it once per group,
            # then only change that lane's word in the private layout.
            operator = "+" if op == "append" else "-"
            staged_steps.append((slot, f"    if (local.x == 0u) {slot}[0] = {old};\n"
                             "    GroupMemoryBarrierWithGroupSync();\n"
                             f"    uint {previous} = {slot}[0];"))
            vector[dst] = ("alu", previous)
            lds_word = ("alu", f"(local.x == 0u ? ({previous} {operator} 32u) : {old})")
            if len(hlsl_word(lds_word)) > ALU_EXPRESSION_LIMIT:
                raise ValueError("LDS expression exceeds the profile limit")
            ds_wait = True
        elif m := re.fullmatch(
                r"ds_(read|write)2_b(32|64) (v\[\d+:\d+\]|v\d+), (v\[\d+:\d+\]|v\d+)"
                r"(?:, (v\[\d+:\d+\]|v\d+))?(?: offset0:([0-3]))?(?: offset1:([0-3]))?", line):
            write = m[1] == "write"
            width = int(m[2]) // 32
            addr_text = m[3] if write else m[4]
            addr = re.fullmatch(r"v(\d+)", addr_text)
            offsets = tuple(int(offset or 0) * width for offset in m.groups()[5:])
            slots = tuple(slot for offset in offsets for slot in range(offset, offset + width))
            if (guard_span is not None or not addr or vector.get(int(addr[1])) != ("lds_wide_address",)
                    or max(slots) >= 4 or write != (m[5] is not None) or write and offsets[0] == offsets[1]):
                raise ValueError("Unverified private paired LDS access")
            operands = (m[4], m[5]) if write else (m[3],)
            count = width if write else 2 * width
            registers = []
            for operand in operands:
                match = re.fullmatch(r"v(\d+)" if count == 1 else r"v\[(\d+):(\d+)\]", operand)
                if not match:
                    raise ValueError("Unverified private paired LDS register range")
                first = int(match[1])
                last = first if count == 1 else int(match[2])
                if last > 255 or last - first + 1 != count:
                    raise ValueError("Unverified private paired LDS register range")
                registers.extend(range(first, last + 1))
            if write:
                words = [vector.get(reg) for reg in registers]
                if not all(map(is_word, words)):
                    raise ValueError("Unverified private wide LDS source")
                lds_wide_words.update(zip(slots, words))
            else:
                if not all(slot in lds_wide_words for slot in slots):
                    raise ValueError("Uninitialized private wide LDS word")
                vector.update(zip(registers, (lds_wide_words[slot] for slot in slots)))
            ds_wait = True
        elif ((m := re.fullmatch(r"ds_(add|sub|rsub|and|or|xor|min|max|inc|dec|condxchg32)(?P<ret>_rtn)?(_[uib]64) "
                                 r"(?(ret)v\[(?P<dst_first>\d+):(?P<dst_last>\d+)\], )"
                                 r"v(?P<addr>\d+), v\[(?P<src_first>\d+):(?P<src_last>\d+)\]"
                                 r"(?: offset:(?P<offset>0|8))?", line))
              and ((op := m[1] + m[3]) in ("add_u64", "sub_u64", "rsub_u64", "and_b64", "or_b64",
                                        "xor_b64", "min_i64", "max_i64", "min_u64", "max_u64", "inc_u64", "dec_u64")
                   or op == "condxchg32_b64" and m["ret"])):
            addr, first, last = (int(m[key]) for key in ("addr", "src_first", "src_last"))
            destination = tuple(int(m[key]) for key in ("dst_first", "dst_last")) if m["ret"] else None
            slot = int(m["offset"] or 0) // 4
            source = (vector.get(first), vector.get(last))
            previous = (lds_wide_words.get(slot), lds_wide_words.get(slot + 1))
            if (guard_span is not None or vector.get(addr) != ("lds_wide_address",)
                    or last > 255 or last != first + 1 or not all(map(is_word, previous + source))
                    or destination and (destination[1] > 255 or destination[1] != destination[0] + 1)):
                raise ValueError("Unverified private wide LDS update")
            left, right = (source, previous) if op == "rsub_u64" else (previous, source)
            lo, hi = map(hlsl_word, left)
            rhs_lo, rhs_hi = map(hlsl_word, right)
            if op == "inc_u64":
                reset = f"({hi} > {rhs_hi} || ({hi} == {rhs_hi} && {lo} >= {rhs_lo}))"
                low = f"({reset} ? 0u : ({lo} + 1u))"
                high = f"({reset} ? 0u : ({hi} + uint({lo} == 0xffffffffu)))"
            elif op == "dec_u64":
                reset = f"(({hi} | {lo}) == 0u || {hi} > {rhs_hi} || ({hi} == {rhs_hi} && {lo} > {rhs_lo}))"
                low = f"({reset} ? {rhs_lo} : ({lo} - 1u))"
                high = f"({reset} ? {rhs_hi} : ({hi} - uint({lo} == 0u)))"
            elif op == "condxchg32_b64":
                low = f"(({rhs_lo} & 0x80000000u) != 0u ? {rhs_lo} : {lo})"
                high = f"(({rhs_hi} & 0x80000000u) != 0u ? {rhs_hi} : {hi})"
            elif op in ("and_b64", "or_b64", "xor_b64"):
                symbol = {"and_b64": "&", "or_b64": "|", "xor_b64": "^"}[op]
                low = f"({lo} {symbol} {rhs_lo})"
                high = f"({hi} {symbol} {rhs_hi})"
            elif op in ("min_i64", "max_i64", "min_u64", "max_u64"):
                relation = "<" if op.startswith("min") else ">"
                if op.endswith("_i64"):
                    first = f"asint({hi}) {relation} asint({rhs_hi})"
                else:
                    first = f"{hi} {relation} {rhs_hi}"
                condition = f"(({first}) || (({hi} == {rhs_hi}) && ({lo} {relation} {rhs_lo})))"
                low = f"({condition} ? {lo} : {rhs_lo})"
                high = f"({condition} ? {hi} : {rhs_hi})"
            elif op == "add_u64":
                low = f"({lo} + {rhs_lo})"
                high = f"({hi} + {rhs_hi} + uint({low} < {lo}))"
            else:
                low = f"({lo} - {rhs_lo})"
                high = f"({hi} - {rhs_hi} - uint({lo} < {rhs_lo}))"
            if max(len(low), len(high)) > ALU_EXPRESSION_LIMIT:
                raise ValueError("LDS expression exceeds the profile limit")
            lds_wide_words[slot] = ("alu", low)
            lds_wide_words[slot + 1] = ("alu", high)
            if destination is not None:
                vector.update(zip(destination, previous))
            ds_wait = True
        elif m := re.fullmatch(r"ds_(write|read)_addtid_b32 v(\d+)(?: offset:0)?", line):
            op, reg = m[1], int(m[2])
            # A single full wave32 makes TID equal to local.x. Zero M0 and
            # offset make this the same private word as an explicit byte address.
            if not lds_m0_zero or guard_span is not None or reg > 255:
                raise ValueError("Unverified private LDS thread-ID access")
            if lds_address_shift not in (None, 2):
                raise ValueError("Mixed private LDS address strides")
            lds_address_shift = 2
            if op == "write":
                value = vector.get(reg)
                if not is_word(value):
                    raise ValueError("Unverified private LDS thread-ID source")
                lds_word = value
            else:
                if lds_word is None:
                    raise ValueError("Uninitialized private LDS thread-ID word")
                vector[reg] = lds_word
            ds_wait = True
        elif m := re.fullmatch(r"ds_write_b(8|16|32)(?P<high>_d16_hi)? v(\d+), v(\d+)(?: offset:([0-3]))?", line):
            width, addr, src = map(int, (m[1], m[3], m[4]))
            offset = int(m[5] or 0)
            high = m["high"]
            if high and width == 32:
                raise ValueError("Unsupported compute instruction: " + line)
            value = vector.get(src)
            if (not lds_enabled or guard_span is not None or max(addr, src) > 255
                    or vector.get(addr) != ("lds_address",) or not is_word(value)
                    or offset % (width // 8) or (width < 32 and lds_word is None)):
                raise ValueError("Unverified private LDS write")
            # Every lane owns one disjoint word. Capture values even if their VGPRs are overwritten.
            if width == 32:
                lds_word = value
            else:
                shift = offset * 8
                mask = (1 << width) - 1
                keep = 0xffffffff ^ (mask << shift)
                data = hlsl_word(value)
                if high:
                    data = f"({data} >> 16u)"
                expression = (f"(({hlsl_word(lds_word)} & 0x{keep:08x}u) | "
                              f"(({data} & 0x{mask:x}u) << {shift}u))")
                if len(expression) > ALU_EXPRESSION_LIMIT:
                    raise ValueError("LDS expression exceeds the profile limit")
                lds_word = ("alu", expression)
            ds_wait = True
        elif ((m := re.fullmatch(r"(ds_[a-z]+)(?P<ret>_rtn)?(_[iub]32) "
                                 r"(?(ret)v(\d+), )v(\d+), v(\d+)(?: offset:0)?", line))
              and (template := LDS_RMW.get(m[1] + m[3]))):
            dst = int(m[4]) if m[4] is not None else None
            addr, src = int(m[5]), int(m[6])
            value = vector.get(src)
            if (not lds_enabled or guard_span is not None or max(addr, src) > 255
                    or (dst is not None and dst > 255) or vector.get(addr) != ("lds_address",)
                    or lds_word is None or not is_word(value)):
                raise ValueError("Unverified private LDS update")
            expression = template.format(hlsl_word(lds_word), hlsl_word(value))
            if len(expression) > ALU_EXPRESSION_LIMIT:
                raise ValueError("LDS expression exceeds the profile limit")
            # Capture the inputs before assigning the old word to a possibly overlapping VGPR.
            if dst is not None:
                vector[dst] = lds_word
            lds_word = ("alu", expression)
            ds_wait = True
        elif m := re.fullmatch(r"ds_read_(b32|[ui](?:8|16))(?P<d16>_d16(?P<hi>_hi)?)? "
                               r"v(?P<dst>\d+), v(?P<addr>\d+)(?: offset:(?P<offset>[0-3]))?", line):
            op, dst, addr, offset = m[1], int(m["dst"]), int(m["addr"]), int(m["offset"] or 0)
            d16, high = m["d16"], m["hi"]
            if d16 and op not in ("u8", "i8", "u16"):
                raise ValueError("Unsupported compute instruction: " + line)
            width = int(op[1:])
            if (not lds_enabled or guard_span is not None or max(dst, addr) > 255
                    or vector.get(addr) != ("lds_address",) or lds_word is None or offset % (width // 8)):
                raise ValueError("Unverified private LDS read")
            if width == 32:
                vector[dst] = lds_word
            else:
                shifted = f"({hlsl_word(lds_word)} >> {offset * 8}u)"
                if op.startswith("i"):
                    # Shifting to the sign bit discards any higher neighbors before the arithmetic shift.
                    extend = 32 - width
                    expression = f"asuint(asint({shifted} << {extend}u) >> {extend}u)"
                else:
                    expression = f"({shifted} & 0x{(1 << width) - 1:x}u)"
                if d16:
                    previous = vector.get(dst)
                    if not is_word(previous):
                        raise ValueError("Unverified D16 destination word")
                    keep, shift = ("0x0000ffffu", 16) if high else ("0xffff0000u", 0)
                    expression = f"(({hlsl_word(previous)} & {keep}) | (({expression} & 0xffffu) << {shift}u))"
                if len(expression) > ALU_EXPRESSION_LIMIT:
                    raise ValueError("LDS expression exceeds the profile limit")
                vector[dst] = ("alu", expression)
            ds_wait = True
        elif m := re.fullmatch(r"ds_((?:inc|dec)(?:_rtn)?_u32|wrap_rtn_b32) "
                               r"(v\d+(?:, v\d+){1,3})(?: offset:0)?", line):
            op = m[1]
            returns = "_rtn_" in op
            wraps = op == "wrap_rtn_b32"
            regs = [int(token[1:]) for token in m[2].split(", ")]
            if len(regs) != 2 + returns + wraps:
                raise ValueError("Unverified LDS counter operands")
            addr, src, *step = regs[returns:]
            value = vector.get(src)
            advance = vector.get(step[0]) if wraps else None
            if (not lds_enabled or guard_span is not None or max(regs) > 255
                    or vector.get(addr) != ("lds_address",) or lds_word is None or not is_word(value)
                    or (wraps and not is_word(advance))):
                raise ValueError("Unverified private LDS counter update")
            old, limit = hlsl_word(lds_word), hlsl_word(value)
            if wraps:
                expression = f"(({old} >= {limit}) ? ({old} - {limit}) : ({old} + {hlsl_word(advance)}))"
            elif op.startswith("inc_"):
                expression = f"(({old} >= {limit}) ? 0u : ({old} + 1u))"
            else:
                expression = f"((({old} == 0u) || ({old} > {limit})) ? {limit} : ({old} - 1u))"
            if len(expression) > ALU_EXPRESSION_LIMIT:
                raise ValueError("LDS expression exceeds the profile limit")
            if returns:
                vector[regs[0]] = lds_word
            lds_word = ("alu", expression)
            ds_wait = True
        elif m := re.fullmatch(r"v_lshl_add_u32 v(\d+), v(\d+), (\d+), v(\d+)", line):
            dst, row, shift, column = map(int, m.groups())
            row_tag, column_tag = vector.get(row), vector.get(column)
            # (row index << shift) + column byte offset, from two different dispatch axes.
            # The plane and element checks at the store bound both shifts.
            if (dst > 255 or not (isinstance(row_tag, tuple) and row_tag[0] == "thread")
                    or not (isinstance(column_tag, tuple) and column_tag[0] == "thread_bytes")
                    or row_tag[1] == column_tag[1]):
                raise ValueError("Unverified plane byte offset")
            vector[dst] = ("plane_bytes", row_tag[1], shift, *column_tag[1:])
        elif ((m := re.fullmatch(r"v_and_b32_e32 v(\d+), (?:0xffff|65535), v(\d+)", line))
              and isinstance(partial := vector.get(int(m[2])), tuple) and partial[0] == "source_low16"):
            dst = int(m[1])
            if dst > 255:
                raise ValueError("Invalid vector destination")
            vector[dst] = ("alu", f"(inputData.Load({partial[1]}) & 0xffffu)")
        elif m := re.fullmatch(rf"(v_(?:add|sub|subrev|mul)_f32_e32) v(\d+), {FLOAT_SOURCE}, (v\d+)", line):
            op, dst = m[1], int(m[2])
            words = [float_word(token, scalar, vector, pending) for token in m.groups()[2:]]
            if dst > 255 or None in words:
                raise ValueError("Unverified float operand")
            expression = FALU[op].format(*map(hlsl_word, words))
            if len(expression) > ALU_EXPRESSION_LIMIT:
                raise ValueError("ALU expression too large")
            vector[dst] = ("alu", expression)
        elif ((m := re.fullmatch(rf"(v_(?:and_b32|lshrrev_b32|min_u32|max_u32|add_nc_u32|or_b32|xor_b32|lshlrev_b32|ashrrev_i32|sub_nc_u32|subrev_nc_u32)_e32) v(\d+), {ALU_SOURCE}, (v\d+)", line))
              or (m := re.fullmatch(rf"(v_mul_lo_u32) v(\d+), {ALU_SOURCE}, {ALU_SOURCE}", line))
              or (m := re.fullmatch(rf"(v_mad_u32_u24) v(\d+), {ALU_SOURCE}, {ALU_SOURCE}, {ALU_SOURCE}", line))
              and None not in [alu_word(token, scalar, vector, pending) for token in m.groups()[2:]]):
            # VOP2 takes a VGPR as src1. A v_mad_u32_u24 with an address operand is a row pitch below.
            op, dst = m[1], int(m[2])
            words = [alu_word(token, scalar, vector, pending) for token in m.groups()[2:]]
            if dst > 255 or None in words:
                raise ValueError("Unverified ALU operand")
            expression = ALU[op].format(*map(hlsl_word, words))
            if len(expression) > ALU_EXPRESSION_LIMIT:
                raise ValueError("ALU expression too large")
            vector[dst] = ("alu", expression)
        elif m := re.fullmatch(r"v_mad_u32_u24 v(\d+), (v\d+|-?\d+|0x[0-9a-f]+), (v\d+|-?\d+|0x[0-9a-f]+), v(\d+)", line):
            dst, column = int(m[1]), int(m[4])
            # The literal pitch may be either multiplicand; LLVM 15 places it in src0.
            row, pitch = (m[2], m[3]) if m[2].startswith("v") else (m[3], m[2])
            row_tag = vector.get(int(row[1:])) if row.startswith("v") else None
            column_tag = vector.get(column)
            # (bits [23:0] of the row index) * pitch + column byte offset. The row check at the store
            # bounds the column shift against the pitch.
            if (dst > 255 or pitch.startswith("v") or not 4 <= int(pitch, 0) < 1 << 24 or int(pitch, 0) % 4
                    or not (isinstance(row_tag, tuple) and row_tag[0] == "thread")
                    or not (isinstance(column_tag, tuple) and column_tag[0] == "thread_bytes")
                    or row_tag[1] == column_tag[1]):
                raise ValueError("Unverified row pitch byte offset")
            vector[dst] = ("pitch_bytes", row_tag[1], int(pitch, 0), *column_tag[1:])
        elif cpu == "gfx1030" and line in ("buffer_gl0_inv", "buffer_gl1_inv"):
            # The desktop profile has no mapping for these cache levels or their acknowledgments.
            raise ValueError(f"Unsupported MUBUF cache invalidation: {line}")
        elif m := re.fullmatch(r"(?:buffer_load_(?:dword(?:x(?P<raw_count>[234]))?|(?P<subword>[us](?:byte|short))"
                               r"|(?P<d16_kind>[us]byte|short)_d16(?P<hi>_hi)?)"
                               r"|tbuffer_load_format_(?P<channels>(?:d16_)?(?:x|xy|xyz|xyzw))"
                               r"|buffer_load_format_(?P<resource_channels>(?:d16_)?(?:x|xy|xyz|xyzw))) "
                               r"(?P<dst>v\d+|v\[\d+:\d+\]), (?P<addr>off|v\d+), s\[(?P<base>\d+):(?P<high>\d+)\], 0"
                               r"(?(channels) format:\[(?P<format>BUF_FMT_(?:(?:32_){1,4}|(?:16_){1,4})UINT)\])"
                               r"(?P<idxen> idxen)?(?P<offen> offen)?"
                               r"(?: offset:(?P<offset>\d+))?", line):
            channels, resource_channels = m["channels"], m["resource_channels"]
            suffix = channels or resource_channels
            packed = suffix is not None and suffix.startswith("d16_")
            raw_count = m["raw_count"]
            components = len(suffix.removeprefix("d16_")) if suffix else int(raw_count or 1)
            count = (components + 1) // 2 if packed else components
            dst, *last = map(int, re.findall(r"\d+", m["dst"]))
            base, high = int(m["base"]), int(m["high"])
            offset = int(m["offset"] or 0)
            addr, idxen, offen = m["addr"], m["idxen"], m["offen"]
            d16_kind = m["d16_kind"]
            subword = m["subword"] or d16_kind
            width = 4 if subword is None else 1 if subword.endswith("byte") else 2
            if subword and (cpu != "gfx1030" or not idxen or offen):
                raise ValueError("Unsupported subword buffer read target or address mode")
            if raw_count and (cpu != "gfx1030" or not idxen or offen):
                raise ValueError("Unsupported vector buffer read target or address mode")
            if channels and (cpu != "gfx1030" or not idxen or offen):
                raise ValueError("Unsupported typed buffer read target or address mode")
            if resource_channels:
                if cpu != "gfx1030" or not idxen or offen:
                    raise ValueError("Unsupported formatted buffer read target or address mode")
                if source_format is None:
                    raise ValueError("Formatted buffer reads require a source format")
                expected = MUBUF_D16_SOURCE_FORMAT if packed else MUBUF_SOURCE_FORMAT
                if source_format != expected:
                    raise ValueError("Formatted buffer read does not match source format")
            # The three-component D16 form uses the four-component 16-bit format.
            format_components = 4 if packed and components == 3 else components
            if channels and m["format"] != f"BUF_FMT_{('16_' if packed else '32_') * format_components}UINT":
                raise ValueError("Unsupported typed buffer read format")
            if last != ([] if count == 1 else [dst + count - 1]) or dst + count > 256:
                raise ValueError("Unverified buffer read destination")
            old = vector.get(dst) if d16_kind else None
            if d16_kind and not is_word(old):
                raise ValueError("Unverified D16 buffer read destination input")
            if (high != base + 3 or base % 4 or high > 105
                    or pending.intersection(range(base, high + 1))):
                raise ValueError("Unverified buffer read descriptor")
            kinds = tuple(scalar.get(base + i) for i in range(4))
            if kinds == tuple(("uav", i) for i in range(4)):
                # The store target would need ordering against other invocations' stores.
                raise ValueError("Reads from the stored UAV are not supported")
            if kinds != tuple(("srv", i) for i in range(4)):
                raise ValueError("Unverified buffer read descriptor")
            alignment = source_texel_bytes if resource_channels else width
            if offset % alignment or offset > 4095:
                raise ValueError("Unverified read offset")
            tag = vector.get(int(addr[1:])) if addr != "off" else None
            if addr == "off" and not idxen and not offen:
                addresses = [f"{offset}u"]
            elif (offen and not idxen and isinstance(tag, tuple) and tag[0] == "thread_bytes"
                  and 2 <= tag[2] < 32):
                # A shift below 2 gives unaligned byte offsets, which ByteAddressBuffer.Load cannot express.
                addresses = [f"(tid.{AXES[tag[1]]} << {tag[2]}u) + {offset}u"]
            elif (idxen and not offen and source_stride is not None and isinstance(tag, tuple)
                  and tag[0] == "thread"):
                # idxen: index * stride + instruction offset, inside one source element.
                # A formatted load selects a prefix of a complete four-component texel.
                read_bytes = source_texel_bytes if resource_channels else width * count
                if offset + read_bytes > source_stride:
                    raise ValueError("Read leaves its source element")
                aligned = offset & ~3
                addresses = [f"tid.{AXES[tag[1]]} * {source_stride}u + {aligned + 4*i}u" for i in range(count)]
            else:
                raise ValueError("Unsupported read address")
            # The address has been captured, so the index can alias a destination.
            for reg, address in enumerate(addresses, dst):
                vector.pop(reg, None)
                # Whole-word consumers cannot read the undefined upper half of an odd D16 result.
                kind = "source_low16" if packed and components % 2 and reg == dst + count - 1 else "source"
                word = (kind, address)
                if subword:
                    # The stride and SRV base are DWORD-aligned, so the enclosing word stays in the record.
                    bits, shift = width * 8, (offset % 4) * 8
                    field = f"(({hlsl_word(word)} >> {shift}u) & {((1 << bits) - 1):#x}u)"
                    if subword in ("sbyte", "sshort"):
                        sign = f"{1 << (bits - 1):#x}u"
                        field = f"(({field} ^ {sign}) - {sign})"
                    if d16_kind:
                        hi = bool(m["hi"])
                        keep = "0xffffu" if hi else "0xffff0000u"
                        field = f"(({hlsl_word(old)} & {keep}) | (({field} & 0xffffu) << {16 if hi else 0}u))"
                        if len(field) > ALU_EXPRESSION_LIMIT:
                            raise ValueError("D16 buffer read expression is too large")
                    word = ("alu", field)
                loading[reg] = word
        elif m := re.fullmatch(r"image_msaa_load v\[(\d+):(\d+)\], v\[(\d+):(\d+)\], "
                               r"s\[(\d+):(\d+)\] dmask:0xf dim:SQ_RSRC_IMG_2D_MSAA unorm", line):
            first, last, addr, addr_last, base, high = map(int, m.groups())
            if not image_msaa or guard_span is not None:
                raise ValueError("image_msaa_load requires an unguarded R32_UINT MSAA binding")
            if last != first + 3 or addr_last != addr + 2 or max(last, addr_last) > 255:
                raise ValueError("Invalid image_msaa_load VGPR tuple")
            if high != base + 7 or base % 4 or pending.intersection(range(base, high + 1)):
                raise ValueError("Unresolved image descriptor")
            if tuple(scalar.get(base + index) for index in range(8)) != tuple(
                    (("image", index // 4), index % 4) for index in range(8)):
                raise ValueError("Unverified image descriptor")
            x, y, fragment = (vector.get(reg) for reg in range(addr, addr_last + 1))
            if not is_word(x) or not is_word(y):
                raise ValueError("Unverified image_msaa_load coordinates")
            if fragment != 0:
                raise ValueError("image_msaa_load requires a known zero fragment ID")
            # Snapshot both coordinates before publishing any result, including aliased VADDR/VDATA.
            address = f"int2(asint({hlsl_word(x)}), asint({hlsl_word(y)}))"
            for sample in range(4):
                name = f"image_msaa_{image_step_count}_{sample}"
                staged_steps.append((None, f"    uint {name} = imageData.Load({address}, {sample});"))
                reg = first + sample
                vector.pop(reg, None)
                loading[reg] = ("alu", name)
            image_step_count += 1
        elif m := re.fullmatch(rf"image_atomic_(?P<op>{IMAGE_ATOMIC_OPS}) "
                               r"v(?:(?P<single>\d+)|\[(?P<first>\d+):(?P<last>\d+)\]), "
                               r"v\[(?P<x>\d+):(?P<y>\d+)\], s\[(?P<base>\d+):(?P<high>\d+)\] "
                               r"dmask:0x(?P<mask>[0-9a-f]+) dim:SQ_RSRC_IMG_2D(?: unorm)?", line):
            op = m["op"]
            signed = op in ("smin", "smax")
            if image_atomic_type != ("int" if signed else "uint") or guard_span is not None or stores:
                raise ValueError("Unsupported image atomic target or binding")
            compare_swap = op == "cmpswap"
            data = int(m["single"] or m["first"])
            if (int(m["mask"], 16) != (3 if compare_swap else 1) or data + int(compare_swap) > 255
                    or (compare_swap and (m["last"] is None or int(m["last"]) != data + 1))
                    or (not compare_swap and m["single"] is None)):
                raise ValueError("Unverified image atomic data range or mask")
            base, high = int(m["base"]), int(m["high"])
            if (high != base + 7 or base % 4 or pending.intersection(range(base, high + 1))
                    or tuple(scalar.get(base + index) for index in range(8)) != tuple(
                        (("image", index // 4), index % 4) for index in range(8))):
                raise ValueError("Unverified image atomic descriptor")
            x, y = int(m["x"]), int(m["y"])
            coords = [vector.get(reg) for reg in (x, y)]
            if y != x + 1 or y > 255 or not all(map(is_word, coords)):
                raise ValueError("Unverified image atomic coordinates")
            sources = [vector.get(data + index) for index in range(1 + compare_swap)]
            if not all(map(is_word, sources)):
                raise ValueError("Unverified image atomic data")
            destination = f"outputData[uint2({', '.join(map(hlsl_word, coords))})]"
            value = hlsl_word(sources[0])
            if compare_swap:
                # RDNA stores VDATA on a match against VDATA+1; HLSL takes the comparison first.
                stores.append(f"    InterlockedCompareStore({destination}, {hlsl_word(sources[1])}, {value});")
            else:
                if op == "swap":
                    stores.append("    uint discardedOldValue;")
                    value += ", discardedOldValue"
                elif op == "sub":
                    value = f"(0u - {value})"
                elif signed:
                    value = f"asint({value})"
                stores.append(f"    Interlocked{MUBUF_ATOMIC_METHODS[op]}({destination}, {value});")
        elif m := re.fullmatch(r"image_(?P<kind>load|store|get_resinfo)(?P<mip>_mip)?(?P<packed>_pck(?:_sgn)?)? "
                               r"(?P<data>v\d+|v\[\d+:\d+\]), (?P<addr>v\d+|v\[\d+:\d+\]), "
                               r"s\[(?P<base>\d+):(?P<high>\d+)\] dmask:0x(?P<mask>[0-9a-f]+) "
                               r"dim:SQ_RSRC_IMG_2D(?: unorm)?(?P<a16> a16)?", line):
            # A16 packs x/y into one dword and takes the mip from the low half of the next dword.
            mip, a16 = bool(m["mip"]), bool(m["a16"])
            write = m["kind"] == "store"
            resinfo = m["kind"] == "get_resinfo"
            vaddr = [int(n) for n in re.findall(r"\d+", m["addr"])]
            address_words = 1 if resinfo else (1 if a16 else 2) + int(mip)
            base, high = int(m["base"]), int(m["high"])
            dmask = int(m["mask"], 16)
            # DMASK selects components in XYZW order and packs them into consecutive VGPRs.
            channels = [component for index, component in enumerate("xyzw") if dmask & (1 << index)]
            count = len(channels)
            data = [int(n) for n in re.findall(r"\d+", m["data"])]
            first, last = data[0], data[-1]
            if (cpu != "gfx1030" or image_format not in IMAGE_FORMATS or guard_span is not None or write != image_output
                    or len(vaddr) != min(address_words, 2) or vaddr[-1] != vaddr[0] + address_words - 1
                    or vaddr[-1] > 255 or last != first + count - 1
                    or not 0 < dmask < (1 << IMAGE_FORMATS[image_format][1])
                    or any(first + index > 255 or first + index in loading for index in range(count))
                    or (write and (m["packed"] == "_pck_sgn" or stores))
                    or (resinfo and (mip or m["packed"] or dmask & 0x4))):
                raise ValueError(f"Unsupported image {m['kind']} target")
            if resinfo and image_format != "R32G32B32A32_UINT":
                raise ValueError("Image get_resinfo requires an RGBA32 SRV")
            if bool(m["packed"]) != (image_format == "R8G8B8A8_UINT"):
                raise ValueError(f"Image {m['kind']} opcode does not match the image format")
            if high != base + 7 or base % 4 or pending.intersection(range(base, high + 1)):
                raise ValueError("Unresolved image descriptor")
            if tuple(scalar.get(base + index) for index in range(8)) != tuple(
                    (("image", index // 4), index % 4) for index in range(8)):
                raise ValueError("Unverified image descriptor")
            coordinates = [vector.get(reg) for reg in range(vaddr[0], vaddr[-1] + 1)]
            if not all(is_word(value) for value in coordinates):
                raise ValueError("Unverified image coordinates")
            if not resinfo:
                if a16:
                    packed = hlsl_word(coordinates[0])
                    x, y = f"{packed} & 0xffffu", f"{packed} >> 16u"
                else:
                    x, y = map(hlsl_word, coordinates[:2])
            if write:
                # A D3D12 Texture2D UAV names one mip. Verify native and desktop mip selection agree.
                level = coordinates[-1] if mip else 0
                if not isinstance(level, int) or (level & 0xffff if a16 else level) != image_uav_mip:
                    raise ValueError("Image store mip does not match the UAV view")
                values = [vector.get(first + index) for index in range(count)]
                if not all(is_word(value) for value in values):
                    raise ValueError("Unverified image store data")
                words = list(map(hlsl_word, values))
                if m["packed"]:
                    # Raw RGBA8 stores take the low byte of each enabled channel's VGPR.
                    words = [f"({word} & 0xffu)" for word in words]
                selected = dict(zip(channels, words))
                texel = ", ".join(selected.get(channel, "0u") for channel in "xyzw")
                stores.append(f"    outputData[uint2({x}, {y})] = uint4({texel});")
                continue
            if resinfo:
                name = f"image_resinfo_{image_step_count}"
                level = hlsl_word(coordinates[0])
                if a16:
                    level = f"({level} & 0xffffu)"
                staged_steps.append((None, f"    uint4 {name};\n"
                                     f"    imageData.GetDimensions({level}, {name}.x, {name}.y, {name}.w);"))
            else:
                lod = "0"
                if mip:
                    mip_word = hlsl_word(coordinates[-1])
                    lod = f"asint({mip_word}{' & 0xffffu' if a16 else ''})"
                address = f"int3(asint({x}), asint({y}), {lod})"
                name = f"image_load_{image_step_count}"
                staged_steps.append((None, f"    {IMAGE_FORMATS[image_format][0]} {name} = imageData.Load({address});"))
            image_step_count += 1
            if m["packed"] and m["packed"].endswith("_sgn"):
                # The UINT SRV exposes raw bytes. Extend each byte from bit 7 to a full VGPR.
                staged_steps.append((None, f"    {name} = asuint(asint({name} << 24u) >> 24u);"))
            for index, component in enumerate(channels):
                reg = first + index
                vector.pop(reg, None)
                loading[reg] = ("alu", f"{name}.{component}")
        elif m := re.fullmatch(rf"buffer_atomic_({'|'.join(MUBUF_ATOMIC_METHODS)})(_x2)? "
                               r"(v\d+|v\[\d+:\d+\]), off, s\[(\d+):(\d+)\], 0(?: offset:(\d+))?", line):
            if cpu != "gfx1030" or stride is not None or output_format is not None or image_output:
                raise ValueError("Unsupported atomic reduction target or binding")
            if stores:
                raise ValueError("An atomic reduction must be the only UAV operation")
            op, wide = m[1], bool(m[2])
            base, high = int(m[4]), int(m[5])
            offset = int(m[6] or 0)
            if (high != base + 3 or base % 4 or pending.intersection(range(base, high + 1))
                    or any(scalar.get(base + i) != ("uav", i) for i in range(4))):
                raise ValueError("Unverified atomic reduction descriptor")
            if offset > 4095 or offset % (8 if wide else 4):
                raise ValueError("Unverified atomic reduction address")
            src, *last = map(int, re.findall(r"\d+", m[3]))
            width = 2 if wide else 1
            count = 2 * width if op == "cmpswap" else width
            if last != ([src + count - 1] if count > 1 else []) or src + count > 256:
                raise ValueError("Unverified atomic reduction data range")
            values = [vector.get(reg) for reg in range(src, src + count)]
            if not all(is_word(value) for value in values):
                raise ValueError("Atomic reduction data is not a known word")
            words = list(map(hlsl_word, values))
            operands = ([f"(uint64_t({lo}) | (uint64_t({hi}) << 32u))" for lo, hi in zip(words[::2], words[1::2])]
                        if wide else words)
            word = operands[0]
            signed = op in ("smin", "smax")
            if signed:
                word = f"int64_t({word})" if wide else f"asint({word})"
            elif op == "sub":
                word = f"({'0ull' if wide else '0u'} - {word})"
            # Both overloads discard the old value. Raw UAV atomics on pairs require SM 6.6.
            kind = ("int64_t" if signed else "uint64_t") if wide else ("int" if signed else "uint")
            method = MUBUF_ATOMIC_METHODS[op] + ("64" if wide else "")
            if op == "cmpswap":
                # The ISA places replacement before compare, unlike the HLSL argument order.
                word = f"{operands[1]}, {word}"
            stores.extend((f"    {kind} ignoredAtomic;",
                           f"    outputData.Interlocked{method}({offset}u, {word}, ignoredAtomic);"))
            atomic_update = True
        elif m := re.fullmatch(r"(?:buffer_store_(?:dword(?:x(?P<raw_count>[234]))?|(?P<subword>byte|short)(?P<store_hi>_d16_hi)?)|"
                               r"(?P<typed>t)?buffer_store_format_(?P<channels>(?:d16_)?(?:x|xy|xyz|xyzw))) "
                               r"(?P<data>v\d+|v\[\d+:\d+\]), (?P<addr>off|v\d+|v\[\d+:\d+\]), "
                               r"s\[(?P<base>\d+):(?P<high>\d+)\], 0"
                               r"(?(typed) format:\[(?P<format>BUF_FMT_(?:(?:32_){1,4}|(?:16_){1,4})UINT)\])"
                               r"(?P<idxen> idxen)?(?P<offen> offen)?(?: offset:(?P<offset>\d+))?", line):
            if atomic_update:
                raise ValueError("An atomic reduction must be the only UAV operation")
            if image_output:
                raise ValueError("Buffer stores require a raw UAV binding")
            channels = m["channels"]
            packed = channels is not None and channels.startswith("d16_")
            components = len(channels.removeprefix("d16_")) if channels else int(m["raw_count"] or 1)
            partial = packed and components % 2 != 0
            count = (components + 1) // 2 if packed else components
            idxen, offen = m["idxen"], m["offen"]
            subword = m["subword"]
            if subword and (cpu != "gfx1030" or not idxen or offen or stride is None):
                raise ValueError("Unsupported subword buffer store target or address mode")
            width = {"byte": 1, "short": 2}.get(subword, 4)
            format_components = 4 if packed and components == 3 else components
            fmt = f"BUF_FMT_{('16_' if packed else '32_') * format_components}UINT"
            if m["typed"] and (cpu != "gfx1030" or not idxen or offen or m["format"] != fmt):
                raise ValueError("Unsupported typed buffer store format, target or address mode")
            formatted = channels is not None and not m["typed"]
            if formatted:
                if cpu != "gfx1030" or not idxen or offen:
                    raise ValueError("Unsupported formatted buffer store target or address mode")
                if output_format is None:
                    raise ValueError("Formatted buffer stores require an output format")
                # Only whole texels with identity selectors are proven here.
                if packed and components not in MUBUF_D16_OUTPUT_FORMATS:
                    raise ValueError("Formatted D16 buffer store requires a matching whole-texel format")
                expected = MUBUF_D16_OUTPUT_FORMATS[components] if packed else MUBUF_UINT_FORMATS[components - 1]
                if output_format != expected:
                    raise ValueError("Formatted buffer store width does not match output format")
            first, *last = map(int, re.findall(r"\d+", m["data"]))
            base, high = int(m["base"]), int(m["high"])
            offset = int(m["offset"] or 0)
            if (last != ([] if count == 1 else [first + count - 1])
                    or (formatted and first + count > 256)
                    or high != base + 3 or base % 4 or offset % width or offset > 4095
                    or pending.intersection(range(base, high + 1))
                    or any(scalar.get(base + i) != ("uav", i) for i in range(4))):
                raise ValueError("Unverified raw UAV store")
            byte_shift = (offset % 4) * 8
            # Reserve and address the containing DWORD for a subword store.
            offset &= ~3
            address = f"{offset}u"
            addr = m["addr"]
            addr_regs = list(map(int, re.findall(r"\d+", addr)))
            tags = [vector.get(reg) for reg in addr_regs]
            if addr == "off" and not idxen and not offen and dims == (1, 1, 1) and stride is None:
                store_layout = ("fixed",)
                covered = set()
            elif (stride is not None and idxen and not offen and len(addr_regs) == 1
                    and isinstance(tags[0], tuple) and tags[0][0] == "thread" and offset + 4 * count <= stride):
                # One index axis: every other group dimension must be one, so no two
                # invocations of a group share an address.
                index = tags[0][1]
                store_layout = ("index", index)
                covered = {index}
                address = f"tid.{AXES[index]} * {stride}u + {offset}u"
            elif (stride is not None and idxen and offen and len(addr_regs) == 2 and addr_regs[1] == addr_regs[0] + 1
                    and all(isinstance(tag, tuple) for tag in tags)
                    and tags[0][0] == "thread" and tags[1][0] == "thread_bytes" and tags[0][1] != tags[1][1]):
                # idxen offen: address = index * stride + byte offset + instruction offset.
                (_, index), (_, column, shift) = tags
                if (offset + 4 * count > 1 << shift or dims[column] << shift > stride):
                    raise ValueError("Store leaves its element or row")
                store_layout = ("index_offset", index, column, shift)
                covered = {index, column}
                address = f"tid.{AXES[index]} * {stride}u + (tid.{AXES[column]} << {shift}u) + {offset}u"
            elif (stride is not None and idxen and offen and len(addr_regs) == 2 and addr_regs[1] == addr_regs[0] + 1
                    and all(isinstance(tag, tuple) for tag in tags)
                    and tags[0][0] == "thread" and tags[1][0] == "plane_bytes"
                    and tags[0][1] not in (tags[1][1], tags[1][3])):
                # Three axes: index * stride + (row << row_shift) + (column << shift) + instruction offset.
                (_, index), (_, row, row_shift, column, shift) = tags
                if (offset + 4 * count > 1 << shift or dims[column] << shift > 1 << row_shift
                        or dims[row] << row_shift > stride):
                    raise ValueError("Store leaves its element, row or plane")
                store_layout = ("index_plane", index, row, row_shift, column, shift)
                covered = {index, row, column}
                address = (f"tid.{AXES[index]} * {stride}u + (tid.{AXES[row]} << {row_shift}u)"
                           f" + (tid.{AXES[column]} << {shift}u) + {offset}u")
            elif (offen and not idxen and len(addr_regs) == 1
                    and isinstance(tags[0], tuple) and tags[0][0] == "pitch_bytes"):
                # offen only: (row & 0xffffff) * pitch + (column << shift) + instruction offset.
                # Without idxen the descriptor stride is not used, so the stride binding is ignored.
                _, row, pitch, column, shift = tags[0]
                # A group's rows start on a 24-bit-aligned power-of-two boundary, so masking keeps them
                # consecutive; their pitches must not wrap the 32-bit address onto each other.
                if (offset + 4 * count > 1 << shift or dims[column] << shift > pitch
                        or dims[row] * pitch > 1 << 32):
                    raise ValueError("Store leaves its element or row")
                store_layout = ("pitch", row, pitch, column, shift)
                covered = {row, column}
                address = f"(tid.{AXES[row]} & 0xffffffu) * {pitch}u + (tid.{AXES[column]} << {shift}u) + {offset}u"
            else:
                raise ValueError("Unsupported store address")
            if any(dims[axis] > 1 for axis in range(3) if axis not in covered):
                raise ValueError("Store address does not separate every invocation of the group")
            if layout not in (None, store_layout):
                raise ValueError("Stores use different address layouts")
            layout = store_layout
            span_end = offset + 4 * count
            if any(lo < span_end and offset < hi for lo, hi in written_spans):
                raise ValueError("Overlapping raw UAV stores")
            written_spans.append((offset, span_end))
            values = [vector.get(reg) for reg in range(first, first + count)]
            if not all(is_word(value) for value in values):
                raise ValueError("Store data is not a known word")
            words = list(map(hlsl_word, values))
            if subword:
                # The indexed layout and whole-DWORD reservation make the read/modify/write single-writer.
                mask = (1 << (width * 8)) - 1
                keep = 0xffffffff ^ (mask << byte_shift)
                source_shift = 16 if m["store_hi"] else 0
                words[0] = (f"((outputData.Load({address}) & {keep:#x}u) | "
                            f"((({words[0]} >> {source_shift}u) & {mask:#x}u) << {byte_shift}u))")
            elif partial:
                # Reserve the entire final DWORD, including the untouched high half. The indexed
                # layout, aligned stride and overlap checks give that word a single writer.
                final_address = address if count == 1 else f"{address} + 4u"
                words[-1] = f"((outputData.Load({final_address}) & 0xffff0000u) | ({words[-1]} & 0xffffu))"
            words = ", ".join(words)
            arg = words if count == 1 else f"uint{count}({words})"
            stores.append(f"    outputData.Store{count if count > 1 else ''}({address}, {arg});")
        else:
            raise ValueError(f"Unsupported compute instruction: {line}")
    if ds_wait:
        raise ValueError("Expected immediate s_waitcnt lgkmcnt(0) after DS")
    if not stores:
        raise ValueError("Expected at least one observable store")
    if guard is not None and guard["label"] is not None and not guard["at_label"]:
        raise ValueError("Unverified branch target")
    if guard_span is not None:
        # Without a restore the mask holds until the exit.
        start, end = guard_span[0], len(stores) if guard_span[1] is None else guard_span[1]
        if start == end:
            raise ValueError("Expected a store under the lane guard")
        op, left, right = guard_span[2]
        stores[start:end] = [f"    if ({hlsl_word(left)} {GUARD_COMPARES[op]} {hlsl_word(right)}) {{",
                             *("    " + store for store in stores[start:end]), "    }"]
    body = "\n".join(stores)
    cbuffer = (f"cbuffer Constants : register(b0) {{ uint4 params[{CONSTANT_WORDS // 4}]; }};\n"
               if constant_descriptor is not None else "")
    if source_descriptor is not None:
        cbuffer += "ByteAddressBuffer inputData : register(t0);\n"
    if image_descriptor is not None and not image_output:
        texture_type = "Texture2DMS<uint, 4>" if image_msaa else f"Texture2D<{IMAGE_FORMATS[image_format][0]}>"
        cbuffer += f"{texture_type} imageData : register(t0);\n"
    local = ""
    if staged_steps:
        body = "\n".join([*(step for _, step in staged_steps), body])
        shared = [slot for slot, _ in staged_steps if slot is not None]
        cbuffer += "".join(f"groupshared uint {slot}[32];\n" for slot in shared)
        if shared:
            local = ", uint3 local : SV_GroupThreadID"
    # Helpers are selected from the final body, so a float ALU result used only by a DS step still gets its definition.
    cbuffer += "".join(text for name, text in F32_HELPERS.items() if f"f32_{name}(" in body)
    uav = f"RWTexture2D<{image_atomic_type or 'uint4'}>" if image_output else "RWByteAddressBuffer"
    return (f"{cbuffer}{uav} outputData : register(u0);\n[numthreads({dims[0]},{dims[1]},{dims[2]})]\n"
            f"void main(uint3 tid : SV_DispatchThreadID{local}) {{\n{body}\n}}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("container", type=Path, help="Local extracted DXBC container")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--llvm", required=True)
    parser.add_argument("--cpu", choices=("gfx1013", "gfx1030"), default="gfx1013")
    parser.add_argument("--lds-f32-mode", choices=("normal-rne",),
                        help="Private LDS f32 inputs and outputs are normal or +0, with round-to-nearest-even")
    parser.add_argument("--lds-f64-mode", choices=("normal",),
                        help="Private LDS f64 comparisons use normal values or positive zero")
    parser.add_argument("--lds-bytes", type=int, default=0, help="Known native group LDS allocation")
    parser.add_argument("--table-reg", type=int, required=True)
    parser.add_argument("--descriptor-offset", type=lambda n: int(n, 0), help="Table byte offset bound to raw u0")
    parser.add_argument("--constant-descriptor", type=lambda n: int(n, 0), help="Table byte offset bound to b0")
    parser.add_argument("--group-reg", type=int, help="SGPR holding the group X ID")
    parser.add_argument("--group-reg-y", type=int, help="SGPR holding the group Y ID")
    parser.add_argument("--group-reg-z", type=int, help="SGPR holding the group Z ID")
    parser.add_argument("--stride", type=int, help="Indexed raw UAV byte stride")
    parser.add_argument("--source-descriptor", type=lambda n: int(n, 0),
                        help="Table byte offset bound to stable read-only t0, with no overlap with u0")
    parser.add_argument("--source-stride", type=int, help="Indexed t0 byte stride")
    parser.add_argument("--source-format", choices=MUBUF_SOURCE_TEXELS,
                        help="Caller-declared MUBUF t0 descriptor format with identity channel selectors")
    parser.add_argument("--output-format", choices=MUBUF_OUTPUT_FORMATS,
                        help="Caller-declared MUBUF u0 descriptor format with identity channel selectors")
    parser.add_argument("--image-descriptor", type=lambda n: int(n, 0),
                        help="Table byte offset of the 8-dword image descriptor bound to t0 or u0")
    parser.add_argument("--image-uav-mip", type=int,
                        help="Bind the image to u0 at this mip instead of binding a raw output descriptor")
    parser.add_argument("--image-format", choices=sorted(IMAGE_FORMATS.keys() | IMAGE_ATOMIC_TYPES.keys()),
                        help="Caller-declared image format (R32_UINT for MSAA SRVs or atomic UAVs, "
                             "R32_SINT for signed min/max UAVs)")
    args = parser.parse_args()
    if (args.descriptor_offset is None) == (args.image_uav_mip is None):
        parser.error("Choose either --descriptor-offset or --image-uav-mip")
    if args.output.suffix.lower() != ".hlsl" or args.output.resolve() == args.container.resolve():
        parser.error("Choose an HLSL destination distinct from the input")
    blob = args.container.read_bytes()
    chunks = parts(blob)
    group = group_size(chunks.get(b"PSV0", b""))
    code = container_code(chunks.get(b"DXIL", b""), stage=5, llvm=args.llvm)
    features = b"+wavefrontsize32,-wavefrontsize64" if args.cpu == "gfx1030" else b""
    decoded = disassemble(code, args.llvm, offsets=True, cpu=args.cpu.encode(), features=features)
    source = lower(labelled(decoded), group, table_reg=args.table_reg,
                   descriptor_offset=args.descriptor_offset, group_reg=args.group_reg, stride=args.stride,
                   constant_descriptor=args.constant_descriptor, group_reg_y=args.group_reg_y,
                   group_reg_z=args.group_reg_z, source_descriptor=args.source_descriptor,
                   source_stride=args.source_stride, source_format=args.source_format,
                   output_format=args.output_format,
                   image_descriptor=args.image_descriptor, image_format=args.image_format,
                   image_uav_mip=args.image_uav_mip, cpu=args.cpu, lds_bytes=args.lds_bytes,
                   lds_f32_mode=args.lds_f32_mode, lds_f64_mode=args.lds_f64_mode)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(source, encoding="ascii")
    print(json.dumps({"container_sha256": hashlib.sha256(blob).hexdigest(),
                      "native_sha256": hashlib.sha256(code).hexdigest(), "numthreads": list(group)}))


if __name__ == "__main__":
    main()
