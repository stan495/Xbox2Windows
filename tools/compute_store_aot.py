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
SALU_SOURCE = r"(s\d+|-?\d+|0x[0-9a-f]+)"
ALU_SOURCE = r"(s\d+|v\d+|-?\d+|0x[0-9a-f]+)"
# Results are inline HLSL expressions; a longer one is rejected instead of growing without bound.
ALU_EXPRESSION_LIMIT = 1024
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


def lower(instructions, group, *, table_reg, descriptor_offset, group_reg=None, stride=None,
          constant_descriptor=None, group_reg_y=None, group_reg_z=None, source_descriptor=None,
          source_stride=None, cpu="gfx1013", lds_bytes=0, lds_f32_mode=None, lds_f64_mode=None):
    """v0, v1 and v2 are local X, Y and Z; group_reg, group_reg_y and group_reg_z are the group IDs.

    gfx10 compute waves receive each local ID in its own VGPR. Y and Z are bound only when that
    group dimension is larger than one. Descriptors name u0 and optionally b0 and a read-only raw
    source t0; source_stride is the t0 byte stride that indexed reads use.
    """
    dims = checked_group(group)
    if cpu not in ("gfx1013", "gfx1030"):
        raise ValueError("Unsupported compute target")
    ds_group = cpu == "gfx1030" and dims == (32, 1, 1)
    if not 0 <= table_reg <= 105 or not 0 <= descriptor_offset < 1 << 20 or descriptor_offset % 4:
        raise ValueError("Invalid table or thread group binding")
    descriptor_bindings = {descriptor_offset: "uav"}
    if constant_descriptor is not None:
        if (not 0 <= constant_descriptor < 1 << 20 or constant_descriptor % 4
                or abs(constant_descriptor - descriptor_offset) < 16):
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
    group_regs = {axis: reg for axis, reg in enumerate((group_reg, group_reg_y, group_reg_z)) if reg is not None}
    if (any(not 0 <= reg <= 105 or reg == table_reg for reg in group_regs.values())
            or len(set(group_regs.values())) != len(group_regs)):
        raise ValueError("Invalid group register")
    if stride is not None and (not group_regs or not 4 <= stride <= 0x3fff or stride % 4):
        raise ValueError("Indexed stores require a group register and aligned stride")
    scalar = {table_reg: "table"}
    for axis, reg in group_regs.items():
        scalar[reg] = ("group", axis)
    vector = {axis: ("local", axis) for axis in range(3) if axis == 0 or dims[axis] > 1}
    pending = set()
    # VGPRs a buffer read still writes, with the word they receive (None when unknown);
    # they enter vector only at s_waitcnt vmcnt(0).
    loading = {}
    stores = []
    ds_steps = []
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
                    or len(ds_steps) >= 64):
                raise ValueError("Unverified DS lane routing")
            # The bijective address is (local X + bias) mod 32. Scatter reads from its inverse.
            bias = address[1] if op == "bpermute" else (-address[1]) % 32
            slot = f"ds_stage_{len(ds_steps)}"
            result = f"ds_result_{len(ds_steps)}"
            ds_steps.append((slot, f"    {slot}[local.x] = {hlsl_word(value)};\n"
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
                if len(ds_steps) >= 64:
                    raise ValueError("Too many private LDS floating-point steps")
                name = f"ds_f32_{len(ds_steps)}"
                ds_steps.append((None, f"    precise float {name} = asfloat({old}) + asfloat({src});"))
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
            if len(ds_steps) >= 64 or any(len(word) > ALU_EXPRESSION_LIMIT for word in inputs):
                raise ValueError("LDS expression exceeds the profile limit")
            prefix = f"ds_f64_{len(ds_steps)}"
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
            ds_steps.append((None, "\n".join(statements)))
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
                    or lds_address_shift != 2 or not is_word(lds_word) or len(ds_steps) >= 64):
                raise ValueError("Unverified LDS wave counter")
            old = hlsl_word(lds_word)
            slot = f"ds_stage_{len(ds_steps)}"
            previous = f"ds_result_{len(ds_steps)}"
            # LDS byte zero is lane zero's word. Snapshot it once per group,
            # then only change that lane's word in the private layout.
            operator = "+" if op == "append" else "-"
            ds_steps.append((slot, f"    if (local.x == 0u) {slot}[0] = {old};\n"
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
        elif m := re.fullmatch(r"(?:buffer_load_(?:dword(?:x(?P<count>[234]))?|(?P<subword>[us](?:byte|short)))"
                               r"|(?P<typed>tbuffer_load_format_x)) "
                               r"v(?:\[(?P<first>\d+):(?P<last>\d+)\]|(?P<dst>\d+)), "
                               r"(?P<addr>off|v\d+), s\[(?P<base>\d+):(?P<high>\d+)\], 0"
                               r"(?(typed) format:\[BUF_FMT_32_UINT\])(?P<idxen> idxen)?(?P<offen> offen)?"
                               r"(?: offset:(?P<offset>\d+))?", line):
            count = int(m["count"] or 1)
            first = int(m["dst"] or m["first"])
            if (count == 1 and m["dst"] is None or count > 1 and m["last"] is None
                    or m["last"] is not None and int(m["last"]) != first + count - 1
                    or first + count > 256):
                raise ValueError("Unverified buffer read destination")
            base, high = int(m["base"]), int(m["high"])
            offset = int(m["offset"] or 0)
            addr, idxen, offen = m["addr"], m["idxen"], m["offen"]
            subword = m["subword"]
            width = 4 if subword is None else 1 if subword.endswith("byte") else 2
            if m["typed"] and (cpu != "gfx1030" or not idxen or offen):
                raise ValueError("Unsupported typed buffer read target or address mode")
            if subword and (cpu != "gfx1030" or not idxen or offen):
                raise ValueError("Unsupported subword buffer read target or address mode")
            if count > 1 and (cpu != "gfx1030" or not idxen or offen):
                raise ValueError("Unsupported vector buffer read target or address mode")
            if (high != base + 3 or base % 4 or high > 105
                    or pending.intersection(range(base, high + 1))):
                raise ValueError("Unverified buffer read descriptor")
            kinds = tuple(scalar.get(base + i) for i in range(4))
            if kinds == tuple(("uav", i) for i in range(4)):
                # The store target would need ordering against other invocations' stores.
                raise ValueError("Reads from the stored UAV are not supported")
            if kinds != tuple(("srv", i) for i in range(4)):
                raise ValueError("Unverified buffer read descriptor")
            if offset % width or offset > 4095:
                raise ValueError("Unverified read offset")
            tag = vector.get(int(addr[1:])) if addr != "off" else None
            if addr == "off" and not idxen and not offen:
                address = f"{offset}u"
            elif (offen and not idxen and isinstance(tag, tuple) and tag[0] == "thread_bytes"
                  and 2 <= tag[2] < 32):
                # A shift below 2 gives unaligned byte offsets, which ByteAddressBuffer.Load cannot express.
                address = f"(tid.{AXES[tag[1]]} << {tag[2]}u) + {offset}u"
            elif (idxen and not offen and source_stride is not None and isinstance(tag, tuple)
                  and tag[0] == "thread"):
                # idxen: index * stride + instruction offset, inside one source element.
                if offset + width * count > source_stride:
                    raise ValueError("Read leaves its source element")
                address = f"tid.{AXES[tag[1]]} * {source_stride}u"
            else:
                raise ValueError("Unsupported read address")
            if count > 1:
                # The index was read above: it may alias any register in this range.
                for i in range(count):
                    vector.pop(first + i, None)
                    loading[first + i] = ("source", f"{address} + {offset + 4 * i}u")
                continue
            if idxen:
                address += f" + {offset & ~3}u"
            vector.pop(first, None)
            word = ("source", address)
            if subword:
                # The stride and SRV base are DWORD-aligned, so the enclosing word stays in the record.
                bits, shift = width * 8, (offset % 4) * 8
                field = f"(({hlsl_word(word)} >> {shift}u) & {((1 << bits) - 1):#x}u)"
                if subword.startswith("s"):
                    sign = f"{1 << (bits - 1):#x}u"
                    field = f"(({field} ^ {sign}) - {sign})"
                word = ("alu", field)
            loading[first] = word
        elif m := re.fullmatch(r"(?:buffer_store_dword(?:x([234]))?|tbuffer_store_format_(?P<channels>x|xy|xyz|xyzw)) "
                               r"(v\d+|v\[\d+:\d+\]), (off|v\d+|v\[\d+:\d+\]), s\[(\d+):(\d+)\], 0"
                               r"(?(channels) format:\[(?P<format>BUF_FMT_(?:32_){1,4}UINT)\])"
                               r"(?P<idxen> idxen)?(?P<offen> offen)?(?: offset:(?P<offset>\d+))?", line):
            channels = m["channels"]
            count = len(channels) if channels else int(m[1] or 1)
            idxen, offen = m["idxen"], m["offen"]
            if channels and (cpu != "gfx1030" or not idxen or offen or m["format"] != f"BUF_FMT_{'32_' * count}UINT"):
                raise ValueError("Unsupported typed buffer store format, target or address mode")
            first, *last = map(int, re.findall(r"\d+", m[3]))
            base, high = int(m[5]), int(m[6])
            offset = int(m["offset"] or 0)
            if (last != ([] if count == 1 else [first + count - 1])
                    or high != base + 3 or base % 4 or offset % 4 or offset > 4095
                    or pending.intersection(range(base, high + 1))
                    or any(scalar.get(base + i) != ("uav", i) for i in range(4))):
                raise ValueError("Unverified raw UAV store")
            address = f"{offset}u"
            addr = m[4]
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
            words = ", ".join(map(hlsl_word, values))
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
    local = ""
    if ds_steps:
        shared = [slot for slot, _ in ds_steps if slot is not None]
        cbuffer += "".join(f"groupshared uint {slot}[32];\n" for slot in shared)
        body = "\n".join([*(step for _, step in ds_steps), body])
        if shared:
            local = ", uint3 local : SV_GroupThreadID"
    return (f"{cbuffer}RWByteAddressBuffer outputData : register(u0);\n[numthreads({dims[0]},{dims[1]},{dims[2]})]\n"
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
    parser.add_argument("--descriptor-offset", type=lambda n: int(n, 0), required=True)
    parser.add_argument("--constant-descriptor", type=lambda n: int(n, 0), help="Table byte offset bound to b0")
    parser.add_argument("--group-reg", type=int, help="SGPR holding the group X ID")
    parser.add_argument("--group-reg-y", type=int, help="SGPR holding the group Y ID")
    parser.add_argument("--group-reg-z", type=int, help="SGPR holding the group Z ID")
    parser.add_argument("--stride", type=int, help="Indexed raw UAV byte stride")
    parser.add_argument("--source-descriptor", type=lambda n: int(n, 0),
                        help="Table byte offset bound to stable read-only t0, with no overlap with u0")
    parser.add_argument("--source-stride", type=int, help="Indexed t0 byte stride")
    args = parser.parse_args()
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
                   source_stride=args.source_stride, cpu=args.cpu, lds_bytes=args.lds_bytes,
                   lds_f32_mode=args.lds_f32_mode, lds_f64_mode=args.lds_f64_mode)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(source, encoding="ascii")
    print(json.dumps({"container_sha256": hashlib.sha256(blob).hexdigest(),
                      "native_sha256": hashlib.sha256(code).hexdigest(), "numthreads": list(group)}))


if __name__ == "__main__":
    main()
