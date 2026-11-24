"""Check the single-store compute profile with original synthetic kernels."""
from pathlib import Path
import operator
import os
import operator
import struct
import subprocess
import sys
import tempfile
from unittest.mock import patch

from pixel_aot_check import envelope, must_reject
import compute_store_aot as aot
import shader_profile_audit
from pixel_aot import _read_named_container, disassemble
from resource_inventory import parts

SENTINEL = 0xc5c5c5c5
INDEXED = [
    "s_mov_b32 s12, s7", "s_mov_b32 s13, 0",
    "s_load_dwordx4 s[20:23], s[12:13], 0x30",
    "v_lshl_add_u32 v8, s11, 5, v0",
    "v_mov_b32_e32 v15, 19", "v_mov_b32_e32 v16, -5",
    "s_waitcnt lgkmcnt(0)", "buffer_store_dwordx2 v[15:16], v8, s[20:23], 0 idxen offset:4",
    "s_endpgm",
]
FIXED = [
    "s_mov_b32 s9, 0", "s_load_dwordx4 s[12:15], s[8:9], null",
    "v_mov_b32_e32 v20, 11", "v_mov_b32_e32 v21, 23", "v_mov_b32_e32 v22, -2",
    "s_waitcnt lgkmcnt(0)", "buffer_store_dwordx3 v[20:22], off, s[12:15], 0 offset:20",
    "s_endpgm",
]
MULTI_FIXED = [
    "s_mov_b32 s9, 0",
    "s_load_dwordx4 s[12:15], s[8:9], null",
    "s_mov_b32 s10, 0x12345678",
    "s_movk_i32 s11, 0xfffc",
    "v_mov_b32_e32 v4, s10",
    "v_mov_b32_e32 v5, s11",
    "v_mov_b32_e32 v6, v4",
    "v_mov_b32_e32 v7, 7",
    "s_waitcnt lgkmcnt(0)",
    "s_clause 0x1",
    "buffer_store_dwordx2 v[4:5], off, s[12:15], 0 offset:8",
    "buffer_store_dwordx2 v[6:7], off, s[12:15], 0 offset:32",
    "s_endpgm",
]
MULTI_INDEXED = [
    "s_mov_b32 s12, s7",
    "s_mov_b32 s13, 0",
    "s_load_dwordx4 s[20:23], s[12:13], 0x30",
    "s_mov_b32 s14, 0x00abcdef",
    "s_movk_i32 s15, 0xfff0",
    "v_lshl_add_u32 v8, s11, 5, v0",
    "v_mov_b32_e32 v10, s14",
    "v_mov_b32_e32 v11, s15",
    "s_waitcnt lgkmcnt(0) vmcnt(0)",
    "buffer_store_dword v10, v8, s[20:23], 0 idxen",
    "buffer_store_dword v11, v8, s[20:23], 0 idxen offset:8",
    "s_endpgm",
]
RUNTIME = [
    "s_mov_b32 s9, 0",
    "s_load_dwordx4 s[12:15], s[8:9], null",
    "s_load_dwordx4 s[16:19], s[8:9], 0x20",
    "s_waitcnt lgkmcnt(0)",
    "s_buffer_load_dword s20, s[16:19], null",
    "s_buffer_load_dwordx2 s[22:23], s[16:19], 0xc",
    "s_buffer_load_dwordx4 s[24:27], s[16:19], 0x30",
    "s_waitcnt lgkmcnt(0)",
    "s_mov_b32 s28, s20",
    "v_mov_b32_e32 v10, s28", "v_mov_b32_e32 v11, s22", "v_mov_b32_e32 v12, s23",
    "v_mov_b32_e32 v13, s27", "v_mov_b32_e32 v14, s26", "v_mov_b32_e32 v15, s25",
    "v_mov_b32_e32 v16, s24", "v_mov_b32_e32 v17, v16",
    "buffer_store_dwordx3 v[10:12], off, s[12:15], 0 offset:16",
    "buffer_store_dwordx4 v[13:16], off, s[12:15], 0 offset:32",
    "buffer_store_dword v17, off, s[12:15], 0 offset:64",
    "s_endpgm",
]
# 8x8 tile: row = dispatch Y through idxen, column = dispatch X through offen (8-byte
# elements); each invocation stores (tid.x, tid.y).
TILE = [
    "s_mov_b32 s9, 0",
    "s_load_dwordx4 s[12:15], s[8:9], null",
    "v_lshl_add_u32 v3, s10, 3, v0",
    "v_lshl_add_u32 v4, s11, 3, v1",
    "v_lshlrev_b32_e32 v5, 3, v3",
    "v_mov_b32_e32 v6, v3",
    "v_mov_b32_e32 v7, v4",
    "s_waitcnt lgkmcnt(0)",
    "buffer_store_dwordx2 v[6:7], v[4:5], s[12:15], 0 idxen offen",
    "s_endpgm",
]
# 4x1x8 slab: row = dispatch Z, column = dispatch X (16-byte elements); stores (tid.z, tid.x)
# after a gap word.
SLAB = [
    "s_mov_b32 s9, 0",
    "s_load_dwordx4 s[12:15], s[8:9], null",
    "v_lshl_add_u32 v10, s16, 3, v2",
    "v_lshl_add_u32 v3, s10, 2, v0",
    "v_lshlrev_b32_e32 v11, 4, v3",
    "v_mov_b32_e32 v20, v10",
    "v_mov_b32_e32 v21, v3",
    "s_waitcnt lgkmcnt(0)",
    "buffer_store_dwordx2 v[20:21], v[10:11], s[12:15], 0 idxen offen offset:4",
    "s_endpgm",
]
# 4x4 tile with several groups in X and Y: one word per invocation, the dispatch X index.
QUAD = [
    "s_mov_b32 s9, 0",
    "s_load_dwordx4 s[12:15], s[8:9], null",
    "v_lshl_add_u32 v4, s11, 2, v1",
    "v_lshl_add_u32 v3, s10, 2, v0",
    "v_lshlrev_b32_e32 v5, 2, v3",
    "s_waitcnt lgkmcnt(0)",
    "buffer_store_dword v3, v[4:5], s[12:15], 0 idxen offen",
    "s_endpgm",
]
# One 256-thread group: one word per invocation, the dispatch X index.
WIDE = [
    "s_mov_b32 s9, 0",
    "s_load_dwordx4 s[12:15], s[8:9], null",
    "v_lshl_add_u32 v3, s10, 8, v0",
    "s_waitcnt lgkmcnt(0)",
    "buffer_store_dword v3, v3, s[12:15], 0 idxen",
    "s_endpgm",
]
# 4x4x4 cube: plane = dispatch Z through idxen; row = dispatch Y and column = dispatch X
# folded into one offen byte offset. One word per invocation, the dispatch Z index.
CUBE = [
    "s_mov_b32 s9, 0",
    "s_load_dwordx4 s[12:15], s[8:9], null",
    "v_lshl_add_u32 v3, s10, 2, v0",
    "v_lshl_add_u32 v4, s11, 2, v1",
    "v_lshl_add_u32 v6, s16, 2, v2",
    "v_lshlrev_b32_e32 v5, 2, v3",
    "v_lshl_add_u32 v7, v4, 4, v5",
    "s_waitcnt lgkmcnt(0)",
    "buffer_store_dword v6, v[6:7], s[12:15], 0 idxen offen",
    "s_endpgm",
]
# The same three-axis address as LLVM 15 emits it (llc-15 -mtriple=amdgcn--amdpal -mcpu=gfx1013
# -O2, amdgpu_kernel): the X byte offset is s_lshl_b32 of the group ID, then v_add_lshl_u32 with
# the local ID. Only the kernel-argument load is replaced, by the descriptor-table load above.
LLVM_CUBE = [
    "s_mov_b32 s1, 0",
    "v_lshl_add_u32 v1, s3, 2, v1",
    "s_waitcnt lgkmcnt(0)",
    "s_load_dwordx4 s[8:11], s[0:1], null",
    "s_lshl_b32 s0, s2, 2",
    "v_add_lshl_u32 v3, s0, v0, 2",
    "v_lshl_add_u32 v0, s4, 2, v2",
    "v_lshl_add_u32 v1, v1, 4, v3",
    "s_waitcnt lgkmcnt(0)",
    "buffer_store_dword v0, v[0:1], s[8:11], 0 idxen offen",
    "s_endpgm",
]
# 4x4 tile with a 72-byte row pitch folded by v_mad_u32_u24 (pitch in src0, as LLVM 15 emits
# it) and a pure offen store; each invocation stores (tid.x, tid.y) in an 8-byte column.
PITCH = [
    "s_mov_b32 s9, 0",
    "s_load_dwordx4 s[12:15], s[8:9], null",
    "v_lshl_add_u32 v3, s10, 2, v0",
    "v_lshl_add_u32 v4, s11, 2, v1",
    "v_lshlrev_b32_e32 v5, 3, v3",
    "v_mad_u32_u24 v6, 0x48, v4, v5",
    "v_mov_b32_e32 v8, v3",
    "v_mov_b32_e32 v9, v4",
    "s_waitcnt lgkmcnt(0)",
    "buffer_store_dwordx2 v[8:9], v6, s[12:15], 0 offen",
    "s_endpgm",
]
INDEX_BINDING = dict(table_reg=7, group_reg=11, descriptor_offset=48, stride=12)
FIXED_BINDING = dict(table_reg=8, descriptor_offset=0)
RUNTIME_BINDING = dict(FIXED_BINDING, constant_descriptor=32)
TILE_BINDING = dict(FIXED_BINDING, group_reg=10, group_reg_y=11, stride=72)
SLAB_BINDING = dict(FIXED_BINDING, group_reg=10, group_reg_z=16, stride=68)
QUAD_BINDING = dict(FIXED_BINDING, group_reg=10, group_reg_y=11, stride=52)
WIDE_BINDING = dict(FIXED_BINDING, group_reg=10, stride=4)
CUBE_BINDING = dict(FIXED_BINDING, group_reg=10, group_reg_y=11, group_reg_z=16, stride=132)
LLVM_CUBE_BINDING = dict(table_reg=0, descriptor_offset=0, group_reg=2, group_reg_y=3, group_reg_z=4, stride=136)
PITCH_BINDING = dict(FIXED_BINDING, group_reg=10, group_reg_y=11)
CASES = [
    ("indexed", INDEXED, 32, INDEX_BINDING),
    ("fixed", FIXED, 1, FIXED_BINDING),
    ("multi_fixed", MULTI_FIXED, 1, FIXED_BINDING),
    ("multi_indexed", MULTI_INDEXED, 32, INDEX_BINDING),
    ("runtime", RUNTIME, 1, RUNTIME_BINDING),
    ("tile", TILE, (8, 8, 1), TILE_BINDING),
    ("slab", SLAB, (4, 1, 8), SLAB_BINDING),
    ("quad", QUAD, (4, 4, 1), QUAD_BINDING),
    ("wide", WIDE, (256, 1, 1), WIDE_BINDING),
    ("cube", CUBE, (4, 4, 4), CUBE_BINDING),
    ("llvm_cube", LLVM_CUBE, (4, 4, 4), LLVM_CUBE_BINDING),
    ("pitch", PITCH, (4, 4, 1), PITCH_BINDING),
]


def grid_words(name, groups):
    """Independent expected readback for the multi-dimensional cases."""
    dims, words = dict((case[0], case[2]) for case in CASES)[name], [SENTINEL] * 320
    gx, gy, gz = groups
    for x in range(gx * dims[0]):
        for y in range(gy * dims[1]):
            for z in range(gz * dims[2]):
                if name == "tile":
                    words[(72 * y + 8 * x) // 4:(72 * y + 8 * x) // 4 + 2] = [x, y]
                elif name == "slab":
                    words[(68 * z + 16 * x + 4) // 4:(68 * z + 16 * x + 4) // 4 + 2] = [z, x]
                elif name == "quad":
                    words[(52 * y + 4 * x) // 4] = x
                elif name == "cube":
                    words[(132 * z + 16 * y + 4 * x) // 4] = z
                elif name == "llvm_cube":
                    words[(136 * z + 16 * y + 4 * x) // 4] = z
                elif name == "pitch":
                    words[(72 * y + 8 * x) // 4:(72 * y + 8 * x) // 4 + 2] = [x, y]
                elif name == "wide":
                    words[x] = x
                else:
                    raise ValueError(f"No expected layout for {name}")
    return words


GRID_DISPATCHES = {
    "tile": ((1, 1, 1), (1, 2, 1)),
    "slab": ((1, 1, 1), (1, 1, 2)),
    "quad": ((1, 1, 1), (3, 2, 1)),
    "wide": ((1, 1, 1),),
    "cube": ((1, 1, 1), (1, 2, 2)),
    "llvm_cube": ((1, 1, 1), (1, 1, 2), (1, 2, 2)),
    "pitch": ((1, 1, 1), (2, 3, 1)),
}
# Recompiled mutations run on the largest dispatch. Swapping two axes that span the same range
# leaves the buffer unchanged: Y and Z both span 8 in the 1x2x2 cube, so that swap runs on 1x1x2,
# where Y spans 4 and Z spans 8.
MUTATION_DISPATCH = {("llvm_cube", "swapped Y/Z"): (1, 1, 2)}


def controls():
    for change in (dict(table_reg=6), dict(group_reg=10), dict(descriptor_offset=32), dict(stride=8)):
        must_reject(lambda: aot.lower(INDEXED, 32, **(INDEX_BINDING | change)))
    for old, new in (("lgkmcnt(0)", "lgkmcnt(1)"), ("s11, 5", "s11, 4"),
                     ("s[20:23], 0 idxen", "s[24:27], 0 idxen"), ("s13, 0", "s13, 1")):
        must_reject(lambda: aot.lower([s.replace(old, new) for s in INDEXED], 32, **INDEX_BINDING))
    must_reject(lambda: aot.lower(INDEXED[:6] + INDEXED[7:], 32, **INDEX_BINDING))
    must_reject(lambda: aot.lower(INDEXED[:7] + ["s_mov_b32 s21, 0"] + INDEXED[7:], 32, **INDEX_BINDING))
    must_reject(lambda: aot.lower(FIXED, 32, **FIXED_BINDING))
    must_reject(lambda: aot.lower(["s_endpgm"], 1, **FIXED_BINDING))

    # Read a constant word directly into v10 before the constant-load wait.
    must_reject(lambda: aot.lower(
        RUNTIME[:7] + ["v_mov_b32_e32 v10, s20", RUNTIME[7]] + RUNTIME[10:],
        1, **RUNTIME_BINDING))
    for old, new in (("s11, 5, v0", "s11, 5, v1"),
                     ("v[15:16], v8,", "v[15:16], v0,"),
                     ("0 idxen offset:4", "0 offset:4")):
        must_reject(lambda: aot.lower([s.replace(old, new) for s in INDEXED], 32, **INDEX_BINDING))
    must_reject(lambda: aot.lower(
        RUNTIME[:4] + ["s_mov_b32 s17, 0"] + RUNTIME[4:], 1, **RUNTIME_BINDING))
    must_reject(lambda: aot.lower(
        INDEXED[:7] + ["s_mov_b32 s23, 0"] + INDEXED[7:], 32, **INDEX_BINDING))
    movk_boundary = FIXED[:2] + ["s_movk_i32 s10, 0x8000", "v_mov_b32_e32 v20, s10"] + FIXED[3:]
    assert "    outputData.Store3(20u, uint3(0xffff8000u, 0x00000017u, 0xfffffffeu));" in (
        aot.lower(movk_boundary, 1, **FIXED_BINDING).splitlines())
    must_reject(lambda: aot.lower(FIXED, 1, **dict(FIXED_BINDING, constant_descriptor=8)))

    # Overlapping store spans in one invocation are rejected.
    must_reject(lambda: aot.lower(
        MULTI_FIXED[:-1] + ["buffer_store_dword v7, off, s[12:15], 0 offset:12", "s_endpgm"],
        1, **FIXED_BINDING))
    must_reject(lambda: aot.lower(
        MULTI_INDEXED[:-1] + ["buffer_store_dwordx2 v[10:11], v8, s[20:23], 0 idxen offset:4", "s_endpgm"],
        32, **INDEX_BINDING))
    # Non-constant or pending scalar/vector sources and out-of-range hints/immediates are rejected.
    must_reject(lambda: aot.lower(
        [s.replace("v_mov_b32_e32 v4, s10", "v_mov_b32_e32 v4, s8") for s in MULTI_FIXED],
        1, **FIXED_BINDING))
    must_reject(lambda: aot.lower(
        [s.replace("v_mov_b32_e32 v4, s10", "v_mov_b32_e32 v4, s12") for s in MULTI_FIXED],
        1, **FIXED_BINDING))
    must_reject(lambda: aot.lower(
        [s.replace("v_mov_b32_e32 v6, v4", "v_mov_b32_e32 v6, v0") for s in MULTI_FIXED],
        1, **FIXED_BINDING))
    must_reject(lambda: aot.lower(
        [s.replace("s_clause 0x1", "s_clause 0x40") for s in MULTI_FIXED],
        1, **FIXED_BINDING))
    must_reject(lambda: aot.lower(
        [s.replace("s_movk_i32 s11, 0xfffc", "s_movk_i32 s11, 0x10000") for s in MULTI_FIXED],
        1, **FIXED_BINDING))
    must_reject(aot.group_size, bytes(52))
    # Runtime info shorter than 48 bytes, or a non-compute stage, has no usable thread dimensions.
    for size, stage in ((47, 5), (48, 4)):
        psv = bytearray(52)
        struct.pack_into("<I", psv, 0, size)
        psv[28] = stage
        struct.pack_into("<3I", psv, 40, 1, 1, 1)
        rejects("Expected compute PSV runtime info", aot.group_size, bytes(psv))

    # The 6-bit s_clause field, 32-bit literals and the 12-bit MUBUF offset.
    max_clause = [s.replace("s_clause 0x1", "s_clause 0x3f") for s in MULTI_FIXED]
    assert aot.lower(max_clause, 1, **FIXED_BINDING) == aot.lower(MULTI_FIXED, 1, **FIXED_BINDING)
    rejects("Invalid scalar immediate", lambda: aot.lower(
        [s.replace("s10, 0x12345678", "s10, -2147483649") for s in MULTI_FIXED],
        1, **FIXED_BINDING))
    rejects("Invalid literal move", lambda: aot.lower(
        [s.replace("v20, 11", "v20, 0x100000000") for s in FIXED],
        1, **FIXED_BINDING))
    rejects("Unverified raw UAV store", lambda: aot.lower(
        [s.replace("offset:20", "offset:4096") for s in FIXED],
        1, **FIXED_BINDING))
    for descriptor in (None, 8, 16, 33):
        must_reject(lambda: aot.lower(RUNTIME, 1, **dict(FIXED_BINDING, constant_descriptor=descriptor)))
    for old, new in (
        ("s[16:19], 0x30", "s[16:19], 0x34"),
        ("s[16:19], 0xc", "s[16:19], 0xa"),
        ("s[16:19], 0xc", "s[12:15], 0xc"),
        ("s[22:23]", "s[21:22]"),
        ("s[24:27]", "s[20:23]"),
        ("s[12:15], 0 offset:64", "s[16:19], 0 offset:64"),
    ):
        must_reject(lambda: aot.lower([line.replace(old, new) for line in RUNTIME], 1, **RUNTIME_BINDING))
    for slot in (3, 7):
        early = RUNTIME.copy()
        early[slot] = "s_waitcnt vmcnt(0)"
        must_reject(lambda: aot.lower(early, 1, **RUNTIME_BINDING))
    grid_controls()
    guard_controls()
    read_controls()
    alu_controls()
    f32_controls()
    scalar_alu_controls()


def grid_controls():
    """Multi-dimensional groups: exact output, then one rejection per broken assumption."""
    tile = aot.lower(TILE, (8, 8, 1), **TILE_BINDING).splitlines()
    assert "[numthreads(8,8,1)]" in tile
    assert "    outputData.Store2(tid.y * 72u + (tid.x << 3u) + 0u, uint2(tid.x, tid.y));" in tile
    assert "    outputData.Store2(tid.z * 68u + (tid.x << 4u) + 4u, uint2(tid.z, tid.x));" in (
        aot.lower(SLAB, (4, 1, 8), **SLAB_BINDING).splitlines())
    assert "    outputData.Store(tid.x * 4u + 0u, tid.x);" in aot.lower(WIDE, 256, **WIDE_BINDING).splitlines()
    # Group sizes: per-axis and total D3D12 limits; the former 1D widths still pass.
    for group in ((1024, 1, 1), (1, 1024, 1), (1, 1, 64), (16, 16, 4), 128, 32, 64, 1):
        aot.checked_group(group)
    for group in ((1025, 1, 1), (1, 1, 0), (1, 1, 65), (64, 4, 8), (0, 1, 1), (8, 0, 1), 0, (8.0, 8, 1)):
        must_reject(aot.checked_group, group)
    # Wrong group size: the shifts no longer match, or the size is not a power of two.
    for group in ((8, 4, 1), (4, 8, 1), (8, 6, 1), (8, 12, 1), (12, 8, 1), (8, 8, 2), (8, 1, 1)):
        must_reject(lambda: aot.lower(TILE, group, **TILE_BINDING))
    # A group whose Y is one launches without a Y local ID; v1 is then undefined.
    must_reject(lambda: aot.lower([s.replace("s11, 2, v1", "s11, 0, v1") for s in QUAD], (4, 1, 1),
                                  **QUAD_BINDING))
    # Axes swapped or ignored: local X with group Y, local Z read in a group whose Z is one.
    for old, new in (("s11, 3, v1", "s11, 3, v0"), ("s10, 3, v0", "s10, 3, v1"),
                     ("s10, 3, v0", "s11, 3, v0")):
        must_reject(lambda: aot.lower([s.replace(old, new) for s in TILE], (8, 8, 1), **TILE_BINDING))
    must_reject(lambda: aot.lower(SLAB, (4, 8, 1), **SLAB_BINDING))
    must_reject(lambda: aot.lower([s.replace("3, v2", "3, v1") for s in SLAB], (4, 1, 8), **SLAB_BINDING))
    # Wrong or missing group-register bindings.
    for change in (dict(group_reg_y=12), dict(group_reg_y=None), dict(group_reg=11), dict(group_reg_y=8),
                   dict(group_reg_z=11)):
        must_reject(lambda: aot.lower(TILE, (8, 8, 1), **(TILE_BINDING | change)))
    must_reject(lambda: aot.lower(SLAB, (4, 1, 8), **(SLAB_BINDING | dict(group_reg_z=None, group_reg_y=16))))
    must_reject(lambda: aot.lower(SLAB, (4, 1, 8), **(SLAB_BINDING | dict(group_reg_y=16))))
    # Index and byte offset from the same axis would alias rows.
    same_axis = WIDE[:3] + ["v_lshlrev_b32_e32 v4, 2, v3", WIDE[3],
                            "buffer_store_dword v3, v[3:4], s[12:15], 0 idxen offen", "s_endpgm"]
    must_reject(lambda: aot.lower(same_axis, 256, **(WIDE_BINDING | dict(stride=1024))))
    # Addresses that leave an element or a row, or that two invocations of one group share.
    for binding in (dict(stride=60), dict(stride=None)):
        must_reject(lambda: aot.lower(TILE, (8, 8, 1), **(TILE_BINDING | binding)))
    for old, new in (("v_lshlrev_b32_e32 v5, 3, v3", "v_lshlrev_b32_e32 v5, 2, v3"),
                     ("v_lshlrev_b32_e32 v5, 3, v3", "v_lshlrev_b32_e32 v5, 1, v3"),
                     ("v_lshlrev_b32_e32 v5, 3, v3", "v_lshlrev_b32_e32 v5, 3, v4"),
                     ("v_lshlrev_b32_e32 v5, 3, v3", "v_lshlrev_b32_e32 v5, 3, v0"),
                     ("v[4:5], s[12:15], 0 idxen offen", "v4, s[12:15], 0 idxen"),
                     ("v[4:5], s[12:15], 0 idxen offen", "v[4:5], s[12:15], 0 offen"),
                     ("v[4:5], s[12:15], 0 idxen offen", "v[3:4], s[12:15], 0 idxen offen"),
                     ("0 idxen offen", "0 idxen offen offset:4")):
        must_reject(lambda: aot.lower([s.replace(old, new) for s in TILE], (8, 8, 1), **TILE_BINDING))
    must_reject(lambda: aot.lower(WIDE, (256, 2, 1), **WIDE_BINDING))
    must_reject(lambda: aot.lower(QUAD[:-1] + ["buffer_store_dword v3, v4, s[12:15], 0 idxen offset:4", "s_endpgm"],
                                  (4, 4, 1), **QUAD_BINDING))
    # Two stores that are valid alone but transpose the layout race across invocations.
    transposed = ["v_mov_b32_e32 v6, v3", "v_lshlrev_b32_e32 v7, 3, v4",
                  "buffer_store_dword v3, v[6:7], s[12:15], 0 idxen offen offset:4"]
    aot.lower(QUAD[:5] + transposed[:2] + QUAD[5:6] + transposed[2:] + ["s_endpgm"], (4, 4, 1), **QUAD_BINDING)
    must_reject(lambda: aot.lower(QUAD[:5] + transposed[:2] + QUAD[5:-1] + transposed[2:] + ["s_endpgm"],
                                  (4, 4, 1), **QUAD_BINDING))
    # A store with an unresolved local ID as data is still rejected.
    must_reject(lambda: aot.lower([s.replace("v_mov_b32_e32 v6, v3", "v_mov_b32_e32 v6, v1") for s in TILE],
                                  (8, 8, 1), **TILE_BINDING))
    cube_controls()
    llvm_cube_controls()
    pitch_controls()
    audit_candidates()


def cube_controls():
    """Three-axis stores: exact output, then one rejection per broken assumption."""
    cube = aot.lower(CUBE, (4, 4, 4), **CUBE_BINDING).splitlines()
    assert "[numthreads(4,4,4)]" in cube
    assert "    outputData.Store(tid.z * 132u + (tid.y << 4u) + (tid.x << 2u) + 0u, tid.z);" in cube
    # Z of one leaves v2 undefined; a wrong group size breaks a shift; a group register is missing.
    for group in ((4, 4, 1), (4, 4, 2), (4, 2, 4), (2, 4, 4), (4, 4, 8)):
        must_reject(lambda: aot.lower(CUBE, group, **CUBE_BINDING))
    for change in (dict(group_reg_z=None), dict(group_reg_z=11), dict(group_reg_z=12)):
        must_reject(lambda: aot.lower(CUBE, (4, 4, 4), **(CUBE_BINDING | change)))
    for old, new in (
        # Row and column from the same axis, or index and row from the same axis.
        ("v_lshl_add_u32 v7, v4, 4, v5", "v_lshl_add_u32 v7, v3, 4, v5"),
        ("v_lshl_add_u32 v6, s16, 2, v2", "v_lshl_add_u32 v6, s11, 2, v1"),
        # Row operand is a raw local ID; column operand is an index, not a byte offset.
        ("v_lshl_add_u32 v7, v4, 4, v5", "v_lshl_add_u32 v7, v1, 4, v5"),
        ("v_lshl_add_u32 v7, v4, 4, v5", "v_lshl_add_u32 v7, v4, 4, v3"),
        # The group's columns overflow a row, or the element overflows a column.
        ("v_lshl_add_u32 v7, v4, 4, v5", "v_lshl_add_u32 v7, v4, 3, v5"),
        ("v_lshlrev_b32_e32 v5, 2, v3", "v_lshlrev_b32_e32 v5, 1, v3"),
        ("0 idxen offen", "0 idxen offen offset:4"),
        # A plane offset without its index, or with the operands in the wrong order.
        ("v[6:7], s[12:15], 0 idxen offen", "v7, s[12:15], 0 offen"),
        ("v[6:7], s[12:15], 0 idxen offen", "v[7:8], s[12:15], 0 idxen offen"),
    ):
        must_reject(lambda: aot.lower([s.replace(old, new) for s in CUBE], (4, 4, 4), **CUBE_BINDING))
    # The group's rows overflow the plane stride.
    for stride in (60, None):
        must_reject(lambda: aot.lower(CUBE, (4, 4, 4), **(CUBE_BINDING | dict(stride=stride))))
    # The plane offset is not a storable word.
    must_reject(lambda: aot.lower([s.replace("buffer_store_dword v6,", "buffer_store_dword v7,") for s in CUBE],
                                  (4, 4, 4), **CUBE_BINDING))


def llvm_cube_controls():
    """The LLVM 15 cube: exact output, then one rejection per group-base assumption."""
    def cube(*changes, group=(4, 4, 4), **binding):
        lines = LLVM_CUBE
        for old, new in changes:
            assert sum(old in line for line in lines) == 1, old
            lines = [part for line in lines for part in line.replace(old, new).split("\n")]
        return aot.lower(lines, group, **(LLVM_CUBE_BINDING | binding))

    source = cube().splitlines()
    assert "[numthreads(4,4,4)]" in source
    assert "    outputData.Store(tid.z * 136u + (tid.y << 4u) + (tid.x << 2u) + 0u, tid.z);" in source
    # Other group sizes: a shift no longer matches, local Y or Z is not launched, or Z is not a
    # power of two.
    for group in ((4, 8, 4), (4, 4, 8), (4, 4, 2), (4, 4, 1), (4, 1, 4), (4, 4, 3)):
        must_reject(lambda: cube(group=group))
    # Group X of 2, 8 or 6: with Y moved to bit 5 the spans still fit, so only the group base
    # rejects them, for its shift (2 and 8) or because 6 is not a power of two.
    must_reject(lambda: cube(group=(2, 4, 4)))
    for group in ((8, 4, 4), (6, 4, 4)):
        must_reject(lambda: cube(("v1, v1, 4, v3", "v1, v1, 5, v3"), group=group))
    # Group base: wrong shift, a source that is not a group ID, a destination past s105 or still
    # pending from a descriptor load, a group base scaled again.
    for changes in ((("s0, s2, 2", "s0, s2, 3"),), (("s0, s2, 2", "s0, s5, 2"),),
                    (("s_lshl_b32 s0,", "s_lshl_b32 s106,"), ("v3, s0, v0", "v3, s106, v0")),
                    (("s_load_dwordx4 s[8:11], s[0:1], null",
                      "s_load_dwordx4 s[8:11], s[0:1], null\ns_load_dwordx4 s[12:15], s[0:1], null"),
                     ("s_lshl_b32 s0,", "s_lshl_b32 s12,"), ("v3, s0, v0", "v3, s12, v0")),
                    (("s0, s2, 2", "s0, s2, 2\ns_lshl_b32 s0, s0, 2"),)):
        must_reject(lambda: cube(*changes))
    # The same form on Y, whose group size differs from X: the base's shift and power of two
    # are checked against its own axis.
    y_form = (("v1, s3, 2, v1", "v4, s2, 2, v0"), ("s0, v0, 2", "s0, v1, 2"), ("v1, v1, 4, v3", "v1, v4, 4, v3"))
    source = cube(("s0, s2, 2", "s0, s3, 1"), *y_form, group=(4, 2, 4)).splitlines()
    assert "    outputData.Store(tid.z * 136u + (tid.x << 4u) + (tid.y << 2u) + 0u, tid.z);" in source
    must_reject(lambda: cube(("s0, s2, 2", "s0, s3, 2"), *y_form, group=(4, 2, 4)))
    must_reject(lambda: cube(("s0, s2, 2", "s0, s3, 1"), *y_form, group=(4, 3, 4)))
    # Byte offset: an unscaled group ID, the group base of Y with local X, local Z added to the X
    # base, a destination past v255.
    for changes in ((("v3, s0, v0", "v3, s2, v0"),), (("s0, s2, 2", "s0, s3, 2"),),
                    (("s0, v0, 2", "s0, v2, 2"),),
                    (("v_add_lshl_u32 v3,", "v_add_lshl_u32 v256,"), ("v1, v1, 4, v3", "v1, v1, 4, v256"))):
        must_reject(lambda: cube(*changes))
    # The new byte offset still meets the element and row checks from #724.
    for change in (("s0, v0, 2", "s0, v0, 1"), ("s0, v0, 2", "s0, v0, 3")):
        must_reject(lambda: cube(change))
    # A group base is not a storable word.
    must_reject(lambda: cube(("v_lshl_add_u32 v1, v1, 4, v3", "v_lshl_add_u32 v1, v1, 4, v3\nv_mov_b32_e32 v5, s0"),
                             ("buffer_store_dword v0,", "buffer_store_dword v5,")))


def pitch_controls():
    """Row-pitch stores: exact output, then one rejection per broken assumption."""
    expected = "    outputData.Store2((tid.y & 0xffffffu) * 72u + (tid.x << 3u) + 0u, uint2(tid.x, tid.y));"
    pitch = aot.lower(PITCH, (4, 4, 1), **PITCH_BINDING)
    assert "[numthreads(4,4,1)]" in pitch.splitlines() and expected in pitch.splitlines()
    # Either multiplicand may carry the pitch; without idxen the stride binding does not change the output.
    assert aot.lower([s.replace("0x48, v4,", "v4, 0x48,") for s in PITCH], (4, 4, 1), **PITCH_BINDING) == pitch
    assert aot.lower(PITCH, (4, 4, 1), **(PITCH_BINDING | dict(stride=16))) == pitch
    # The column byte offset may also come in the LLVM 15 group-base form.
    group_base = [part for line in PITCH for part in line.replace(
        "v_lshlrev_b32_e32 v5, 3, v3", "s_lshl_b32 s20, s10, 2\nv_add_lshl_u32 v5, s20, v0, 3").split("\n")]
    assert len(group_base) == len(PITCH) + 1 and aot.lower(group_base, (4, 4, 1), **PITCH_BINDING) == pitch
    # An inline-constant pitch is printed in decimal.
    assert expected.replace("72u", "64u") in aot.lower(
        [s.replace("0x48", "64") for s in PITCH], (4, 4, 1), **PITCH_BINDING).splitlines()
    for old, new in (
        # Pitch unaligned, zero, negative, narrower than one group's columns, or wider than 24 bits.
        ("0x48, v4", "0x46, v4"), ("0x48, v4", "0, v4"), ("0x48, v4", "-4, v4"),
        ("0x48, v4", "0x1c, v4"), ("0x48, v4", "0x1000000, v4"),
        # Pitch from a register, or no literal at all.
        ("0x48, v4", "s10, v4"), ("0x48, v4", "v3, v4"),
        # Row is a raw local ID, shares the column's axis, or the addend is an index, not bytes.
        ("0x48, v4, v5", "0x48, v1, v5"), ("0x48, v4, v5", "0x48, v3, v5"), ("0x48, v4, v5", "0x48, v4, v3"),
        # Output modifier.
        ("0x48, v4, v5", "0x48, v4, v5 clamp"),
        # The element overflows its column.
        ("v_lshlrev_b32_e32 v5, 3, v3", "v_lshlrev_b32_e32 v5, 2, v3"), ("0 offen", "0 offen offset:4"),
        # idxen on a pitch offset, or no offen.
        ("0 offen", "0 idxen offen"), ("v6, s[12:15], 0 offen", "v6, s[12:15], 0 idxen"),
        # The pitch offset is not a storable word.
        ("buffer_store_dwordx2 v[8:9]", "buffer_store_dwordx2 v[6:7]"),
    ):
        must_reject(lambda: aot.lower([s.replace(old, new) for s in PITCH], (4, 4, 1), **PITCH_BINDING))
    # A wrong group size breaks a shift or leaves an axis unseparated; a group register is missing.
    for group in ((8, 4, 1), (4, 8, 1), (4, 4, 2), (4, 1, 1)):
        must_reject(lambda: aot.lower(PITCH, group, **PITCH_BINDING))
    for change in (dict(group_reg_y=None), dict(group_reg_y=12)):
        must_reject(lambda: aot.lower(PITCH, (4, 4, 1), **(PITCH_BINDING | change)))
    # One group's rows may span the whole 32-bit address space but not wrap onto each other.
    tall = [s.replace("s10, 2, v0", "s10, 0, v0").replace("s11, 2, v1", "s11, 10, v1") for s in PITCH]
    aot.lower([s.replace("0x48", "0x400000") for s in tall], (1, 1024, 1), **PITCH_BINDING)
    must_reject(lambda: aot.lower([s.replace("0x48", "0x400004") for s in tall], (1, 1024, 1), **PITCH_BINDING))
    # A second store with another pitch would use a different layout.
    second = ["v_mad_u32_u24 v7, 0x50, v4, v5", "buffer_store_dword v8, v7, s[12:15], 0 offen offset:4"]
    try:
        aot.lower(PITCH[:6] + second[:1] + PITCH[6:-1] + second[1:] + ["s_endpgm"], (4, 4, 1), **PITCH_BINDING)
    except ValueError as error:
        assert str(error) == "Stores use different address layouts", error
    else:
        raise AssertionError("Two row pitches were accepted")


def audit_candidates():
    """The audit tries group Z in the SGPR after group X when the group's Y is one."""
    slab = ["s_mov_b32 s4, s5", "s_mov_b32 s5, 0", "s_load_dwordx4 s[12:15], s[4:5], null",
            "v_lshl_add_u32 v3, s6, 2, v0", "v_lshl_add_u32 v4, s7, 3, v2", "v_lshlrev_b32_e32 v5, 2, v3",
            "s_waitcnt lgkmcnt(0)", "buffer_store_dword v4, v[4:5], s[12:15], 0 idxen offen", "s_endpgm"]
    # The audit always offers a stride for multi-group shaders; a pure offen pitch store ignores it.
    pitch = [s.replace("s[8:9]", "s[4:5]").replace("s9, 0", "s5, 0").replace("s10, 2", "s6, 2")
             .replace("s11, 2", "s7, 2") for s in PITCH]
    pitch.insert(0, "s_mov_b32 s4, s5")
    pitch_line = "    outputData.Store2((tid.y & 0xffffffu) * 72u + (tid.x << 3u) + 0u, uint2(tid.x, tid.y));"
    for lines, group, expected in ((slab, (4, 1, 8), "    outputData.Store(tid.z * 16u + (tid.x << 2u) + 0u, tid.z);"),
                                   ([s.replace("s7, 3", "s8, 3") for s in slab], (4, 1, 8), None),
                                   (pitch, (4, 4, 1), pitch_line)):
        emitted = []
        with patch.object(shader_profile_audit.pixel_aot, "disassemble", return_value=lines):
            profiles = shader_profile_audit.compute_profiles(b"", group, None,
                                                             lambda kind, source: emitted.append(source))
        if expected is None:
            assert profiles == [] and not emitted
        else:
            assert profiles == ["store"] and expected in emitted[0].splitlines()


# Two-exit compute programs (#721), reusing the pixel verifier from #477. Both kernels are the
# llc-15 output (-mtriple=amdgcn--amdpal -mcpu=gfx1013 -O2) of an original source:
#   if (gid >= n) return;   // wave-uniform, with branch weights that mark the return likely
#   store gid at a byte offset;
# The conditional branch jumps over the early s_endpgm to the store block.
EXIT_KERNEL = [  # amdgpu_kernel: group X in s2, bound n and descriptor from the kernel arguments
    "s_load_dword s3, s[0:1], 0x8",
    "s_waitcnt lgkmcnt(0)",
    "s_cmp_ge_u32 s2, s3",
    "s_cbranch_scc0 .Lbody",
    "s_endpgm",
    ".Lbody:",
    "s_load_dwordx2 s[0:1], s[0:1], null",
    "v_lshlrev_b32_e32 v0, 2, v0",
    "v_mov_b32_e32 v1, s2",
    "s_waitcnt lgkmcnt(0)",
    "s_load_dwordx4 s[4:7], s[0:1], null",
    "s_waitcnt lgkmcnt(0)",
    "buffer_store_dword v1, v0, s[4:7], 0 offen",
    "s_endpgm",
]
EXIT_CS = [  # amdgpu_cs: descriptor, n and the group ID as SGPR arguments
    "s_cmp_ge_u32 s5, s4",
    "s_cbranch_scc0 .Lbody",
    "s_endpgm",
    ".Lbody:",
    "s_lshl_b32 s4, s5, 2",
    "v_mov_b32_e32 v0, s5",
    "v_mov_b32_e32 v1, s4",
    "buffer_store_dword v0, v1, s[0:3], 0 offen",
    "s_endpgm",
]
# The same source with a per-lane bound (local X >= n). LLVM 15 merges the per-lane return into
# one exit: s_cbranch_execz skips the store and reaches the only s_endpgm.
LANE_EXIT = [
    "s_load_dword s3, s[0:1], 0x8",
    "s_waitcnt lgkmcnt(0)",
    "v_cmp_gt_u32_e32 vcc_lo, s3, v0",
    "s_and_saveexec_b32 s3, vcc_lo",
    "s_cbranch_execz .Lend",
    "s_load_dwordx2 s[0:1], s[0:1], null",
    "v_lshlrev_b32_e32 v0, 2, v0",
    "v_mov_b32_e32 v1, s2",
    "s_waitcnt lgkmcnt(0)",
    "s_load_dwordx4 s[4:7], s[0:1], null",
    "s_waitcnt lgkmcnt(0)",
    "buffer_store_dword v1, v0, s[4:7], 0 offen",
    ".Lend:",
    "s_endpgm",
]
# Per-lane guards (#721), unchanged LLVM 15 output (-mtriple=amdgcn--amdpal -mcpu=gfx1013 -O2) of
# original amdgpu_cs sources with a 32-bit table pointer and the group X ID as SGPR arguments, a
# group of 32, u0 at table byte 0 and b0 at byte 32:
#   gid = group.x * 32 + local.x;
#   if (gid < n) store gid at element gid;        // n = params[0].x
# The guard stays open to the exit; the u0 descriptor load is sunk under it.
GUARD_END = [
    "s_mov_b32 s2, s1",
    "s_mov_b32 s1, 0",
    "v_lshl_add_u32 v0, s2, 5, v0",
    "s_load_dwordx4 s[4:7], s[0:1], 0x20",
    "s_waitcnt lgkmcnt(0)",
    "s_buffer_load_dword s3, s[4:7], 0x0",
    "s_waitcnt lgkmcnt(0)",
    "v_cmp_gt_u32_e32 vcc_lo, s3, v0",
    "s_and_saveexec_b32 s2, vcc_lo",
    "s_cbranch_execz .LBB0_2",
    "s_load_dwordx4 s[0:3], s[0:1], 0x0",
    "s_waitcnt lgkmcnt(0)",
    "buffer_store_dword v0, v0, s[0:3], 0 idxen",
    ".LBB0_2:",
    "s_endpgm",
]
# The same guard, then a store outside it, so exec is restored before the second store:
#   if (gid < n) store gid at element gid, word 0;
#   store 0x600d at element gid, word 1;
GUARD_JOIN = [
    "s_mov_b32 s2, s1",
    "s_mov_b32 s1, 0",
    "v_lshl_add_u32 v0, s2, 5, v0",
    "s_load_dwordx4 s[8:11], s[0:1], 0x20",
    "s_waitcnt lgkmcnt(0)",
    "s_buffer_load_dword s3, s[8:11], 0x0",
    "s_load_dwordx4 s[4:7], s[0:1], 0x0",
    "s_waitcnt lgkmcnt(0)",
    "v_cmp_gt_u32_e32 vcc_lo, s3, v0",
    "s_and_saveexec_b32 s0, vcc_lo",
    "s_cbranch_execz .LBB1_2",
    "buffer_store_dword v0, v0, s[4:7], 0 idxen",
    ".LBB1_2:",
    "s_waitcnt_depctr 0xffe3",
    "s_or_b32 exec_lo, exec_lo, s0",
    "v_mov_b32_e32 v1, 0x600d",
    "buffer_store_dword v1, v0, s[4:7], 0 idxen offset:4",
    "s_endpgm",
]
GUARD_BINDING = dict(table_reg=0, descriptor_offset=0, constant_descriptor=32, group_reg=1, stride=4)
GUARD_CASES = [
    ("guard_end", GUARD_END, 32, GUARD_BINDING),
    ("guard_join", GUARD_JOIN, 32, GUARD_BINDING | dict(stride=8)),
]
GUARD_IF = "    if (params[0].x > tid.x) {"
# Vector reads (#721), unchanged LLVM 15 code generator output (-mtriple=amdgcn--amdpal
# -mcpu=gfx1013 -O2, no IR passes) of original amdgpu_cs sources as for the guards above, with a
# second, read-only raw buffer at table byte 16 (t0). Both descriptors come from one x8 load.
#   gid = group.x * 32 + local.x;
#   if (gid < n) store source word gid (byte offset gid * 4) at element gid;
READ_COPY = [
    "s_mov_b32 s2, s1",
    "s_mov_b32 s1, 0",
    "v_lshl_add_u32 v0, s2, 5, v0",
    "s_load_dwordx4 s[4:7], s[0:1], 0x20",
    "s_waitcnt lgkmcnt(0)",
    "s_buffer_load_dword s3, s[4:7], 0x0",
    "s_waitcnt lgkmcnt(0)",
    "v_cmp_gt_u32_e32 vcc_lo, s3, v0",
    "s_and_saveexec_b32 s2, vcc_lo",
    "s_cbranch_execz .LBB0_2",
    "s_load_dwordx8 s[0:7], s[0:1], 0x0",
    "v_lshlrev_b32_e32 v1, 2, v0",
    "s_waitcnt lgkmcnt(0)",
    "buffer_load_dword v1, v1, s[4:7], 0 offen",
    "s_waitcnt vmcnt(0)",
    "buffer_store_dword v1, v0, s[0:3], 0 idxen",
    ".LBB0_2:",
    "s_endpgm",
]
#   if (gid < n) store word 1 of indexed source element gid at element gid;
# The source stride is a descriptor field, bound here as 8 bytes.
READ_INDEX = [
    "s_mov_b32 s2, s1",
    "s_mov_b32 s1, 0",
    "v_lshl_add_u32 v0, s2, 5, v0",
    "s_load_dwordx4 s[4:7], s[0:1], 0x20",
    "s_waitcnt lgkmcnt(0)",
    "s_buffer_load_dword s3, s[4:7], 0x0",
    "s_waitcnt lgkmcnt(0)",
    "v_cmp_gt_u32_e32 vcc_lo, s3, v0",
    "s_and_saveexec_b32 s2, vcc_lo",
    "s_cbranch_execz .LBB1_2",
    "s_load_dwordx8 s[0:7], s[0:1], 0x0",
    "s_waitcnt lgkmcnt(0)",
    "buffer_load_dword v1, v0, s[4:7], 0 idxen offset:4",
    "s_waitcnt vmcnt(0)",
    "buffer_store_dword v1, v0, s[0:3], 0 idxen",
    ".LBB1_2:",
    "s_endpgm",
]
READ_BINDING = GUARD_BINDING | dict(source_descriptor=16)
READ_CASES = [
    ("read_copy", READ_COPY, 32, READ_BINDING),
    ("read_index", READ_INDEX, 32, READ_BINDING | dict(source_stride=8)),
]
READ_LOADS = {"read_copy": "inputData.Load((tid.x << 2u) + 0u)", "read_index": "inputData.Load(tid.x * 8u + 4u)"}
# Read-compute-write (#721), unchanged LLVM 15 code generator output of an original amdgpu_cs
# source as for the reads, with the same bindings:
#   if (gid < n) {
#       v = source word gid (byte offset gid * 4);
#       store umin((v & 0xffff) * (v >> 16) + gid, cap) at element gid;    // cap = params[0].y
#   }
READ_COMPUTE = [
    "s_mov_b32 s2, s1",
    "s_mov_b32 s1, 0",
    "v_lshl_add_u32 v0, s2, 5, v0",
    "s_load_dwordx4 s[4:7], s[0:1], 0x20",
    "s_waitcnt lgkmcnt(0)",
    "s_buffer_load_dword s3, s[4:7], 0x0",
    "s_waitcnt lgkmcnt(0)",
    "v_cmp_gt_u32_e32 vcc_lo, s3, v0",
    "s_and_saveexec_b32 s2, vcc_lo",
    "s_cbranch_execz .LBB0_2",
    "s_load_dwordx8 s[8:15], s[0:1], 0x0",
    "v_lshlrev_b32_e32 v1, 2, v0",
    "s_buffer_load_dword s0, s[4:7], 0x4",
    "s_waitcnt lgkmcnt(0)",
    "buffer_load_dword v1, v1, s[12:15], 0 offen",
    "s_waitcnt vmcnt(0)",
    "v_and_b32_e32 v2, 0xffff, v1",
    "v_lshrrev_b32_e32 v1, 16, v1",
    "v_mad_u32_u24 v1, v2, v1, v0",
    "v_min_u32_e32 v1, s0, v1",
    "buffer_store_dword v1, v0, s[8:11], 0 idxen",
    ".LBB0_2:",
    "s_endpgm",
]
ALU_WORD = ("min(params[0].y, ((((0x0000ffffu & {0}) & 0xffffffu) * (({0} >> (0x00000010u & 31u)) & 0xffffffu))"
            " + tid.x))").format(READ_LOADS["read_copy"])


def variant(lines, *changes):
    """Apply (old, new) to the one line containing old; a newline in new inserts lines, "" deletes it."""
    for old, new in changes:
        assert sum(old in line for line in lines) == 1, old
        lines = [part for line in lines for part in ((line.replace(old, new).split("\n") if new else [])
                                                     if old in line else [line])]
    return lines


# Leave a different bit above bit 23 set in each multiplicand. The ordinary
# codegen fixture above limits both inputs to 16 bits.
READ_MAD24 = variant(
    READ_COMPUTE,
    ("v_and_b32_e32 v2, 0xffff, v1", "v_and_b32_e32 v2, 0x1ffffff, v1"),
    ("v_lshrrev_b32_e32 v1, 16, v1", "v_and_b32_e32 v1, 0x2ffffff, v1"),
)
# Keep the same guarded read and write. Use the entire source and constant words.
READ_MUL_LO = variant(
    READ_COMPUTE,
    ("v_and_b32_e32 v2, 0xffff, v1", ""),
    ("v_lshrrev_b32_e32 v1, 16, v1", ""),
    ("v_mad_u32_u24 v1, v2, v1, v0", "v_mul_lo_u32 v1, s0, v1"),
    ("v_min_u32_e32 v1, s0, v1", ""),
)
# The remaining unsigned VOP2 operations of the subset, chained so each result feeds the next: the cap constant added
# to the product (it wraps for most lanes), a high-bit OR and a high-bit XOR, then the unsigned maximum of the product
# and the chain (the product wins for some lanes and the chain for others).
READ_VOP2 = variant(
    READ_COMPUTE,
    ("v_min_u32_e32 v1, s0, v1", "v_add_nc_u32_e32 v2, s0, v1\nv_or_b32_e32 v2, 0x80000010, v2\n"
                                 "v_xor_b32_e32 v2, 0x80000001, v2\nv_max_u32_e32 v1, v1, v2"),
)
# Binary32 add, sub, subrev and mul, chained so each result feeds the next. The source word w gives two operands that
# are always finite and normal, x = 1.5 + (w & 0x3fffff) / 2^23 in [1.5, 2) and y = 3.0 + (w >> 10) / 2^22 in [3, 4);
# the cap word is the float 20.0. Every result stays in [1.5, 30], so no operation overflows or goes denormal. The
# exponent constants 0x3fc00000 and 0x40400000 are not inline constants, which LLVM would print as floats:
#   a = x + y; b = cap - a; c = b - pi; d = x * c; e = 1.0 + d; out = e - (-4.0)
# The sources cover a VGPR, an SGPR, a literal and the inline constants 1.0 and -4.0.
READ_F32 = variant(
    READ_COMPUTE,
    ("v_and_b32_e32 v2, 0xffff, v1", "v_and_b32_e32 v2, 0x3fffff, v1\nv_or_b32_e32 v2, 0x3fc00000, v2\n"
                                     "v_lshrrev_b32_e32 v3, 10, v1\nv_or_b32_e32 v3, 0x40400000, v3"),
    ("v_lshrrev_b32_e32 v1, 16, v1", "v_add_f32_e32 v4, v2, v3\nv_sub_f32_e32 v5, s0, v4\n"
                                     "v_subrev_f32_e32 v6, 0x40490fdb, v5\nv_mul_f32_e32 v6, v2, v6\n"
                                     "v_add_f32_e32 v1, 1.0, v6"),
    ("v_mad_u32_u24 v1, v2, v1, v0", "v_subrev_f32_e32 v1, -4.0, v1"),
    ("v_min_u32_e32 v1, s0, v1", ""),
)
# The shift and subtract operations of the subset on a computed word: the product is shifted left until its
# bit 14 is the sign bit, shifted right arithmetically, subtracted from the cap (src0 - src1), and the literal
# 0x1234 is reverse-subtracted (src1 - src0).
READ_SHIFT_SUB = variant(
    READ_COMPUTE,
    ("v_min_u32_e32 v1, s0, v1", "v_lshlrev_b32_e32 v2, 17, v1\nv_ashrrev_i32_e32 v2, 20, v2\n"
                                 "v_sub_nc_u32_e32 v3, s0, v2\nv_subrev_nc_u32_e32 v1, 0x1234, v3"),
)
ALU_CASES = [
    ("read_compute", READ_COMPUTE, 32, READ_BINDING),
    ("read_mad24", READ_MAD24, 32, READ_BINDING),
    ("read_mul_lo", READ_MUL_LO, 32, READ_BINDING),
    ("read_vop2", READ_VOP2, 32, READ_BINDING),
    ("read_shift_sub", READ_SHIFT_SUB, 32, READ_BINDING),
]
F32_CASES = [("read_f32", READ_F32, 32, READ_BINDING)]


def guard_controls():
    """Per-lane guards: exact output, then one rejection per broken control-flow assumption."""
    def guard(name, *changes):
        _, lines, width, binding = dict((case[0], case) for case in GUARD_CASES)[name]
        return aot.lower(variant(lines, *changes), width, **binding).splitlines()

    end, join = guard("guard_end"), guard("guard_join")
    assert end[-4:] == [GUARD_IF, "        outputData.Store(tid.x * 4u + 0u, tid.x);", "    }", "}"], end
    assert join[-5:] == [GUARD_IF, "        outputData.Store(tid.x * 8u + 0u, tid.x);", "    }",
                         "    outputData.Store(tid.x * 8u + 4u, 0x0000600du);", "}"], join
    # The compare picks the operator and operand order; an immediate bound is a literal.
    for op, symbol in (("gt", ">"), ("lt", "<"), ("ge", ">="), ("le", "<=")):
        assert f"    if (params[0].x {symbol} tid.x) {{" in guard("guard_join", ("v_cmp_gt_u32", f"v_cmp_{op}_u32"))
    assert "    if (0x00000028u > tid.x) {" in guard("guard_join", ("vcc_lo, s3, v0", "vcc_lo, 40, v0"))
    # Without the restore and the branch, exec stays masked to the exit: both stores are guarded.
    unrestored = guard("guard_join", ("s_cbranch_execz", ""), (".LBB1_2:", ""), ("s_or_b32", ""))
    assert unrestored[-5:] == [GUARD_IF, "        outputData.Store(tid.x * 8u + 0u, tid.x);",
                               "        outputData.Store(tid.x * 8u + 4u, 0x0000600du);", "    }", "}"], unrestored
    # A load waited for only under the guard is pending again after the restore, unless waited for.
    late_load = (("s_load_dwordx4 s[4:7], s[0:1], 0x0", ""),
                 ("vcc_lo, s3, v0", "vcc_lo, s3, v0\ns_load_dwordx4 s[4:7], s[0:1], 0x0"),
                 ("s_cbranch_execz .LBB1_2", "s_cbranch_execz .LBB1_2\ns_waitcnt lgkmcnt(0)"))
    assert guard("guard_join", *late_load, ("v1, 0x600d", "v1, 0x600d\ns_waitcnt lgkmcnt(0)")) == join
    saved_s12 = (("saveexec_b32 s0", "saveexec_b32 s12"), ("exec_lo, exec_lo, s0", "exec_lo, exec_lo, s12"))
    assert guard("guard_join", *saved_s12) == join
    # A later compare does not change the guard.
    assert guard("guard_join", ("v1, 0x600d", "v1, 0x600d\nv_cmp_lt_u32_e32 vcc_lo, s3, v0")) == join
    # A copy of the saved mask restores it too.
    assert guard("guard_join", ("s_cbranch_execz .LBB1_2", "s_mov_b32 s12, s0\ns_cbranch_execz .LBB1_2"),
                 ("exec_lo, exec_lo, s0", "exec_lo, exec_lo, s12")) == join
    restore, reach, target = ("Unverified lane guard restore", "Lane guard branch does not reach its restore",
                              "Unverified branch target")
    compare, repeated = "Unverified lane guard compare", "Unverified or repeated lane guard"
    # Each control names its diagnostic, so it cannot pass for an unrelated reason.
    for message, changes in (
        # The exec mask is not restored before the store outside the guard, a register that
        # does not hold the saved mask restores it, or a register overwrites it under the guard.
        (reach, (("s_or_b32", ""),)),
        (restore, (("exec_lo, exec_lo, s0", "exec_lo, exec_lo, s3"),)),
        (restore, (("buffer_store_dword v0, v0", "s_mov_b32 s0, 0\nbuffer_store_dword v0, v0"),)),
        # A copy made only inside the block that the branch skips holds the mask on one path only.
        (restore, (("buffer_store_dword v0, v0", "s_mov_b32 s12, s0\nbuffer_store_dword v0, v0"),
                   ("exec_lo, exec_lo, s0", "exec_lo, exec_lo, s12"))),
        # The branch lands past the restore, inside the guarded block, or nowhere; a label
        # that no branch names; a second branch to another label.
        (restore, ((".LBB1_2:", ""), ("s_or_b32 exec_lo, exec_lo, s0", "s_or_b32 exec_lo, exec_lo, s0\n.LBB1_2:"))),
        (reach, ((".LBB1_2:", ""), ("buffer_store_dword v0, v0", ".LBB1_2:\nbuffer_store_dword v0, v0"))),
        (restore, ((".LBB1_2:", ""),)),
        (target, (("s_cbranch_execz .LBB1_2", ""),)),
        (target, (("s_cbranch_execz .LBB1_2", "s_cbranch_execz .LBB1_2\ns_cbranch_execz .LBB1_3"),)),
        ("Unsupported compute instruction: s_cbranch_execnz", (("s_cbranch_execz", "s_cbranch_execnz"),)),
        # No compare, a compare of a pending, unknown or non-word operand, or a mask from another source.
        (repeated, (("v_cmp_gt_u32_e32 vcc_lo, s3, v0", ""),)),
        (compare, (("vcc_lo, s3, v0", "vcc_lo, s12, v0"),)),
        (compare, (("v_cmp_gt_u32_e32 vcc_lo, s3, v0", "s_buffer_load_dword s12, s[8:11], 0x4\n"
                                                       "v_cmp_gt_u32_e32 vcc_lo, s12, v0"),)),
        (compare, (("vcc_lo, s3, v0", "vcc_lo, s3, v1"),)),
        (compare, (("vcc_lo, s3, v0", "vcc_lo, s2, v0"),)),
        (compare, (("vcc_lo, s3, v0", "vcc_lo, s0, v0"),)),
        ("Unsupported compute instruction: s_and_saveexec_b32 s0, s3",
         (("s_and_saveexec_b32 s0, vcc_lo", "s_and_saveexec_b32 s0, s3"),)),
        ("Unsupported compute instruction: s_and_saveexec_b64",
         (("s_and_saveexec_b32 s0, vcc_lo", "s_and_saveexec_b64 s[0:1], vcc"),)),
        # A second or nested guard.
        (repeated, (("buffer_store_dword v0, v0", "v_cmp_gt_u32_e32 vcc_lo, s3, v0\n"
                                                  "s_and_saveexec_b32 s12, vcc_lo\nbuffer_store_dword v0, v0"),)),
        (repeated, (("v_mov_b32_e32 v1, 0x600d", "v_cmp_gt_u32_e32 vcc_lo, s3, v0\n"
                                                 "s_and_saveexec_b32 s12, vcc_lo\nv_mov_b32_e32 v1, 0x600d"),)),
        # A guard around no store.
        ("Expected a store under the lane guard", (("buffer_store_dword v0, v0, s[4:7], 0 idxen", ""),)),
        # A load waited for only under the guard, then used after the restore.
        ("Unverified raw UAV store", late_load),
        # A vector or scalar register written under the guard, read after the restore.
        ("Store data is not a known word",
         (("v_mov_b32_e32 v1, 0x600d", ""), ("buffer_store_dword v0, v0", "v_mov_b32_e32 v1, 0x600d\n"
                                                                          "buffer_store_dword v0, v0"))),
        ("Unverified raw UAV store",
         (("s_load_dwordx4 s[4:7], s[0:1], 0x0", ""), *saved_s12,
          ("buffer_store_dword v0, v0", "s_load_dwordx4 s[4:7], s[0:1], 0x0\ns_waitcnt lgkmcnt(0)\n"
                                        "buffer_store_dword v0, v0"))),
        # The saved mask stored as data.
        ("Unresolved scalar move source", (("v_mov_b32_e32 v1, 0x600d", "v_mov_b32_e32 v1, s0"),)),
    ):
        rejects(message, guard, "guard_join", *changes)
    # A restore with no guard.
    rejects(restore, aot.lower, WIDE[:-1] + ["s_or_b32 exec_lo, exec_lo, s4", "s_endpgm"], 256, **WIDE_BINDING)
    # The open guard of guard_end: its branch must reach a label, and only no-ops may follow the
    # label before the exit.
    rejects(target, guard, "guard_end", (".LBB0_2:", ""))
    rejects(reach, guard, "guard_end", (".LBB0_2:", ""),
            ("s[0:3], 0 idxen", "s[0:3], 0 idxen\n.LBB0_2:\ns_nop 0\nv_mov_b32_e32 v1, 0"))
    # Branch labels come from decoded offsets: forward, at an instruction start, inside the span.
    decoded = [(0, "v_cmp_gt_u32_e32 vcc_lo, s3, v0"), (4, "s_and_saveexec_b32 s0, vcc_lo"),
               (8, "s_cbranch_execz 2"), (12, "buffer_store_dword v0, v0, s[4:7], 0 idxen"),
               (20, "s_or_b32 exec_lo, exec_lo, s0"), (24, "s_endpgm")]
    assert aot.labelled(decoded) == [line for _, line in decoded[:2]] + [
        "s_cbranch_execz .L20", decoded[3][1], ".L20:", *(line for _, line in decoded[4:])]
    for distance in ("1", "5", "65535", "-1"):
        rejects("Branch outside decoded instruction boundaries",
                aot.labelled, decoded[:2] + [(8, f"s_cbranch_execz {distance}")] + decoded[3:])


def read_controls():
    """Vector reads: exact output, then one rejection per broken read assumption."""
    def read(name, *changes, **binding):
        _, lines, width, bound = dict((case[0], case) for case in READ_CASES)[name]
        return aot.lower(variant(lines, *changes), width, **(bound | binding)).splitlines()

    for name in ("read_copy", "read_index"):
        lines = read(name)
        assert lines[:3] == ["cbuffer Constants : register(b0) { uint4 params[4]; };",
                             "ByteAddressBuffer inputData : register(t0);",
                             "RWByteAddressBuffer outputData : register(u0);"], lines
        assert lines[-4:] == [GUARD_IF, f"        outputData.Store(tid.x * 4u + 0u, {READ_LOADS[name]});",
                              "    }", "}"], lines
    copy = read("read_copy")
    # The same program with both descriptors loaded apart, as the full -O2 pipeline emits it.
    assert read("read_copy", ("s_load_dwordx8 s[0:7], s[0:1], 0x0", "s_load_dwordx4 s[4:7], s[0:1], 0x10"),
                ("v_lshlrev_b32_e32 v1, 2, v0", "v_lshlrev_b32_e32 v1, 2, v0\ns_load_dwordx4 s[0:3], s[0:1], 0x0")) == copy
    # A combined wait, and a copy of the word read, are the same program.
    assert read("read_copy", ("s_waitcnt vmcnt(0)", "s_waitcnt vmcnt(0) lgkmcnt(0)")) == copy
    assert read("read_copy", ("s_waitcnt vmcnt(0)", "s_waitcnt vmcnt(0)\nv_mov_b32_e32 v2, v1"),
                ("buffer_store_dword v1, v0", "buffer_store_dword v2, v0")) == copy
    # A fixed read address.
    assert "        outputData.Store(tid.x * 4u + 0u, inputData.Load(8u));" in read(
        "read_copy", ("v1, v1, s[4:7], 0 offen", "v1, off, s[4:7], 0 offset:8"))
    wait = "Vector load result used or overwritten before s_waitcnt vmcnt(0)"
    descriptor, address = "Unverified buffer read descriptor", "Unsupported read address"
    for name, message, changes, binding in (
        # The word read is stored, copied or overwritten before its wait, or only a scalar wait follows.
        ("read_copy", wait, (("s_waitcnt vmcnt(0)", ""),), {}),
        ("read_copy", wait, (("s_waitcnt vmcnt(0)", "s_waitcnt lgkmcnt(0)"),), {}),
        ("read_copy", wait, (("s_waitcnt vmcnt(0)", "v_mov_b32_e32 v2, v1\ns_waitcnt vmcnt(0)"),), {}),
        ("read_copy", wait, (("s_waitcnt vmcnt(0)", "v_mov_b32_e32 v1, 7\ns_waitcnt vmcnt(0)"),), {}),
        ("read_index", wait, (("s_waitcnt vmcnt(0)", ""),), {}),
        # A partial count does not say which load completed.
        ("read_copy", "Unsupported compute instruction: s_waitcnt vmcnt(1)",
         (("s_waitcnt vmcnt(0)", "s_waitcnt vmcnt(1)"),), {}),
        ("read_copy", "Unsupported compute instruction: s_waitcnt lgkmcnt(0) vmcnt(1)",
         (("s_waitcnt vmcnt(0)", "s_waitcnt lgkmcnt(0) vmcnt(1)"),), {}),
        # The read uses the store target, the constant buffer, an unbound slot, or a pending descriptor.
        ("read_copy", "Reads from the stored UAV are not supported", (("v1, v1, s[4:7]", "v1, v1, s[0:3]"),), {}),
        ("read_copy", "Unresolved compute descriptor load", (), dict(source_descriptor=48)),
        ("read_copy", "Unresolved compute descriptor load", (), dict(source_descriptor=None)),
        ("read_copy", descriptor, (("s_load_dwordx8 s[0:7], s[0:1], 0x0",
                                    "s_load_dwordx4 s[8:11], s[0:1], 0x20\ns_load_dwordx4 s[0:3], s[0:1], 0x0"),
                                   ("v1, v1, s[4:7]", "v1, v1, s[8:11]")), {}),
        ("read_copy", descriptor, (("buffer_load_dword v1, v1, s[4:7], 0 offen", ""),
                                   ("v_lshlrev_b32_e32 v1, 2, v0",
                                    "v_lshlrev_b32_e32 v1, 2, v0\nbuffer_load_dword v1, v1, s[4:7], 0 offen")), {}),
        # The index or byte offset is not the dispatch index, or the read uses the other addressing.
        ("read_copy", address, (("v_lshlrev_b32_e32 v1, 2, v0", ""),), {}),
        ("read_copy", address, (("v_lshlrev_b32_e32 v1, 2, v0", "v_mov_b32_e32 v1, 0"),), {}),
        ("read_copy", address, (("v_lshlrev_b32_e32 v1, 2, v0", "v_lshlrev_b32_e32 v1, 1, v0"),), {}),
        ("read_copy", address, (("v1, v1, s[4:7], 0 offen", "v1, v1, s[4:7], 0 idxen"),), dict(source_stride=4)),
        ("read_index", address, (("v1, v0, s[4:7], 0 idxen", "v1, v0, s[4:7], 0 offen"),), {}),
        ("read_index", address, (), dict(source_stride=None)),
        ("read_index", address, (("v1, v0, s[4:7]", "v1, v2, s[4:7]"),), {}),
        # The instruction offset is unaligned, leaves the source element, or leaves the 12-bit field.
        ("read_copy", "Unverified read offset", (("s[4:7], 0 offen", "s[4:7], 0 offen offset:2"),), {}),
        ("read_index", "Read leaves its source element", (("offset:4", "offset:8"),), {}),
        ("read_copy", "Unverified read offset", (("s[4:7], 0 offen", "s[4:7], 0 offen offset:4096"),), {}),
        # A load under the guard is unknown after the restore, even once waited for.
        ("read_copy", "Store data is not a known word",
         (("s_load_dwordx8 s[0:7], s[0:1], 0x0", ""),
          ("s_and_saveexec_b32 s2, vcc_lo", "s_load_dwordx8 s[0:7], s[0:1], 0x0\ns_waitcnt lgkmcnt(0)\n"
                                            "s_and_saveexec_b32 s12, vcc_lo"),
          ("s_waitcnt vmcnt(0)", "s_or_b32 exec_lo, exec_lo, s12\ns_waitcnt vmcnt(0)"),
          ("s_cbranch_execz .LBB0_2", ""), (".LBB0_2:", "")), {}),
        # Binding errors: overlapping or unaligned slots, a stride without a source.
        ("read_copy", "Invalid or overlapping source descriptor binding", (), dict(source_descriptor=24)),
        ("read_copy", "Invalid or overlapping source descriptor binding", (), dict(source_descriptor=18)),
        ("read_index", "Indexed reads require a source binding and aligned stride", (), dict(source_stride=10)),
        ("read_index", "Indexed reads require a source binding and aligned stride", (),
         dict(source_descriptor=None)),
    ):
        rejects(message, read, name, *changes, **binding)
    # A read issued before the guard and waited for only under it is pending again after the
    # restore; waited for again, it keeps its word.
    early = (("s_load_dwordx8 s[0:7], s[0:1], 0x0", ""), ("v_lshlrev_b32_e32 v1, 2, v0", ""),
             ("buffer_load_dword v1, v1, s[4:7], 0 offen", ""),
             ("s_and_saveexec_b32 s2, vcc_lo", "s_load_dwordx8 s[8:15], s[0:1], 0x0\nv_lshlrev_b32_e32 v1, 2, v0\n"
              "s_waitcnt lgkmcnt(0)\nbuffer_load_dword v1, v1, s[12:15], 0 offen\ns_and_saveexec_b32 s2, vcc_lo"),
             ("s_cbranch_execz .LBB0_2", ""), (".LBB0_2:", ""),
             ("buffer_store_dword v1, v0, s[0:3], 0 idxen",
              "buffer_store_dword v0, v0, s[8:11], 0 idxen\ns_or_b32 exec_lo, exec_lo, s2"))
    late = "buffer_store_dword v1, v0, s[8:11], 0 idxen offset:4\ns_endpgm"
    rejects(wait, read, "read_copy", *early, ("s_endpgm", late), stride=8)
    assert read("read_copy", *early, ("s_endpgm", "s_waitcnt vmcnt(0)\n" + late), stride=8)[-5:] == [
        GUARD_IF, "        outputData.Store(tid.x * 8u + 0u, tid.x);", "    }",
        "    outputData.Store(tid.x * 8u + 4u, inputData.Load((tid.x << 2u) + 0u));", "}"]
    # Overwritten under the guard after its wait, the word read is unknown after the restore.
    rejects("Store data is not a known word", read, "read_copy", *early,
            ("s_or_b32 exec_lo", "v_mov_b32_e32 v1, 5\ns_or_b32 exec_lo"),
            ("s_endpgm", "s_waitcnt vmcnt(0)\n" + late), stride=8)


def alu_controls():
    """Read-compute-write: exact output, then one rejection per instruction or operand outside the subset."""
    def alu(*changes, **binding):
        _, lines, width, bound = ALU_CASES[0]
        return aot.lower(variant(lines, *changes), width, **(bound | binding)).splitlines()

    lines = alu()
    assert lines[:3] == ["cbuffer Constants : register(b0) { uint4 params[4]; };",
                         "ByteAddressBuffer inputData : register(t0);",
                         "RWByteAddressBuffer outputData : register(u0);"], lines
    assert lines[-4:] == [GUARD_IF, f"        outputData.Store(tid.x * 4u + 0u, {ALU_WORD});", "    }", "}"], lines
    # A copy of a result is the same program. Literal, negative inline and SGPR sources keep their order.
    mad = "v_mad_u32_u24 v1, v2, v1, v0"
    assert alu(("v_min_u32_e32 v1, s0, v1", "v_min_u32_e32 v3, s0, v1\nv_mov_b32_e32 v1, v3")) == lines
    load = READ_LOADS["read_copy"]
    assert (f"min(params[0].y, (((0x0000012cu & 0xffffffu) * ((0xfffffff0u & {load}) & 0xffffffu)) + params[0].y)"
            in alu(("0xffff, v1", "-16, v1"), (mad, "v_mad_u32_u24 v1, 0x12c, v2, s0"))[-3])
    # Add, or, xor and max apply in order: max(cap, 0x80000001 ^ (cap | (7 + mad))); src0 prints first.
    mad_word = ALU_WORD.split(", ", 1)[1][:-1]
    vop2 = aot.lower(READ_VOP2, 32, **READ_BINDING).splitlines()
    assert vop2[-3] == ("        outputData.Store(tid.x * 4u + 0u, max(" + mad_word + ", (0x80000001u ^ (0x80000010u | "
                        f"(params[0].y + {mad_word})))));"), vop2
    # Shift and subtract in order; src0 prints first for the reversed-operand forms, so each operand order is pinned.
    shift_sub = aot.lower(READ_SHIFT_SUB, 32, **READ_BINDING).splitlines()
    product = f"((((0x0000ffffu & {load}) & 0xffffffu) * (({load} >> (0x00000010u & 31u)) & 0xffffffu)) + tid.x)"
    assert shift_sub[-3] == ("        outputData.Store(tid.x * 4u + 0u, ((params[0].y - asuint(asint((" + product
                             + " << (0x00000011u & 31u))) >> (0x00000014u & 31u))) - 0x00001234u));"), shift_sub
    mul = "v_mul_lo_u32 v1, s0, v1"
    lines = aot.lower(READ_MUL_LO, 32, **READ_BINDING).splitlines()
    assert lines[-4:] == [GUARD_IF, f"        outputData.Store(tid.x * 4u + 0u, (params[0].y * {load}));",
                         "    }", "}"], lines
    rejects("Unverified ALU operand", aot.lower, variant(READ_MUL_LO, (mul, mul.replace("s0", "v255"))),
            32, **READ_BINDING)
    rejects("Unsupported compute instruction: v_mul_lo_u32", aot.lower, variant(READ_MUL_LO, (mul, mul + " clamp")),
            32, **READ_BINDING)
    unsupported, operand = "Unsupported compute instruction: ", "Unverified ALU operand"
    compare = "Unverified lane guard compare"
    # The x8 descriptor load moved before the guard, so only the result is written under it.
    early = (("s_load_dwordx8 s[8:15], s[0:1], 0x0", ""),
             ("s_and_saveexec_b32 s2, vcc_lo", "s_load_dwordx8 s[8:15], s[0:1], 0x0\ns_waitcnt lgkmcnt(0)\n"
                                               "s_and_saveexec_b32 s2, vcc_lo"))
    for message, changes in (
        # Wrong opcodes: signed or other operations, and a carry form that would write vcc.
        (unsupported + "v_min_i32_e32", (("v_min_u32_e32", "v_min_i32_e32"),)),
        (unsupported + "v_max_i32_e32", (("v_min_u32_e32", "v_max_i32_e32"),)),
        (unsupported + "v_mul_hi_u32_e32", (("v_lshrrev_b32_e32", "v_mul_hi_u32_e32"),)),
        (unsupported + "v_xnor_b32_e32", (("v_and_b32_e32", "v_xnor_b32_e32"),)),
        (unsupported + "v_mad_i32_i24", ((mad, "v_mad_i32_i24 v1, v2, v1, v0"),)),
        (unsupported + "v_add_co_u32", ((mad, "v_add_co_u32 v1, vcc_lo, v2, v0"),)),
        # Wrong operand widths: 16-bit, 64-bit and sub-dword (SDWA) forms, and a register range.
        (unsupported + "v_min_u16_e32", (("v_min_u32_e32", "v_min_u16_e32"),)),
        (unsupported + "v_mad_u32_u16", ((mad, "v_mad_u32_u16 v1, v2, v1, v0"),)),
        (unsupported + "v_lshrrev_b64", (("v_lshrrev_b32_e32 v1, 16, v1", "v_lshrrev_b64 v[1:2], 16, v[1:2]"),)),
        (unsupported + "v_mad_u64_u32", ((mad, "v_mad_u64_u32 v[1:2], s4, v2, v1, v[0:1]"),)),
        (unsupported + "v_and_b32_sdwa", (("v_and_b32_e32 v2, 0xffff, v1", "v_and_b32_sdwa v2, v1, v1 dst_sel:DWORD "
                                           "dst_unused:UNUSED_PAD src0_sel:WORD_0 src1_sel:DWORD"),)),
        (unsupported + "v_and_b32_e32", (("0xffff, v1", "0xffff, v[1:2]"),)),
        # An SGPR as the VOP2 src1, and an output modifier.
        (unsupported + "v_min_u32_e32", (("s0, v1", "v1, s0"),)),
        (unsupported + "v_mad_u32_u24", ((mad, mad + " clamp"),)),
        # Sources that are not known words: an unknown, pending, descriptor or exec-mask SGPR, an
        # unknown VGPR, a literal wider than 32 bits, a byte offset, and a word read before its wait.
        # Also a destination past v255.
        (operand, (("s0, v1", "s20, v1"),)),
        (operand, (("s_buffer_load_dword s0, s[4:7], 0x4", ""),
                   ("v_and_b32_e32", "s_buffer_load_dword s0, s[4:7], 0x4\nv_and_b32_e32"))),
        (operand, (("s0, v1", "s8, v1"),)),
        (operand, (("s0, v1", "s2, v1"),)),
        (operand, (("16, v1", "16, v5"),)),
        (operand, (("0xffff, v1", "0x1ffffffff, v1"),)),
        (operand, (("v_min_u32_e32 v1, s0, v1", "v_min_u32_e32 v256, s0, v1"),)),
        (operand, (("v_and_b32_e32 v2, 0xffff, v1", "v_lshlrev_b32_e32 v3, 2, v0\nv_and_b32_e32 v2, 0xffff, v3"),)),
        ("Unverified row pitch byte offset", ((mad, "v_lshlrev_b32_e32 v3, 2, v0\nv_mad_u32_u24 v1, v2, v1, v3"),)),
        ("Vector load result used or overwritten before s_waitcnt vmcnt(0)", (("s_waitcnt vmcnt(0)", ""),)),
        # A result or a word read used as an address or as a guard operand.
        ("Unsupported store address", (("v1, v0, s[8:11]", "v0, v1, s[8:11]"),)),
        ("Unsupported read address", (("v_min_u32_e32 v1, s0, v1", "v_min_u32_e32 v1, s0, v1\n"
                                       "buffer_load_dword v3, v1, s[12:15], 0 offen"),)),
        (compare, (("v_min_u32_e32 v1, s0, v1", "v_min_u32_e32 v1, s0, v1\nv_cmp_gt_u32_e32 vcc_lo, s3, v1"),)),
        (compare, (("v_and_b32_e32", "v_cmp_gt_u32_e32 vcc_lo, s3, v1\nv_and_b32_e32"),)),
        # Expressions are inline, so a result that keeps doubling is rejected.
        ("ALU expression too large", (("v_min_u32_e32 v1, s0, v1",
                                       "v_min_u32_e32 v1, s0, v1" + "\nv_and_b32_e32 v1, v1, v1" * 4),)),
    ):
        rejects(message, alu, *changes)
    # A result computed under the guard is unknown after the restore; a word set after it is known.
    restored = (*early, ("s_cbranch_execz .LBB0_2", ""), (".LBB0_2:", ""),
                ("buffer_store_dword v1, v0, s[8:11], 0 idxen", "buffer_store_dword v0, v0, s[8:11], 0 idxen\n"
                 "s_or_b32 exec_lo, exec_lo, s2\nbuffer_store_dword v1, v0, s[8:11], 0 idxen offset:4"))
    rejects("Store data is not a known word", alu, *restored, stride=8)
    assert alu(*restored, ("s_or_b32 exec_lo, exec_lo, s2", "s_or_b32 exec_lo, exec_lo, s2\nv_mov_b32_e32 v1, 9"),
               stride=8)[-5:] == [GUARD_IF, "        outputData.Store(tid.x * 8u + 0u, tid.x);", "    }",
                                  "    outputData.Store(tid.x * 8u + 4u, 0x00000009u);", "}"]


F32_CAP = 0x41a00000  # the float 20.0
F32_STORE = ("        outputData.Store(tid.x * 4u + 0u, f32_sub(f32_add(0x3f800000u, f32_mul((0x3fc00000u | (0x003fffffu & {0})), "
             "f32_sub(f32_sub(params[0].y, f32_add((0x3fc00000u | (0x003fffffu & {0})), (0x40400000u | ({0} >> "
             "(0x0000000au & 31u))))), 0x40490fdbu))), 0xc0800000u));")


def f32_controls():
    """Binary32 ALU: exact output, then one rejection per form, operand or value outside the subset."""
    def f32(*changes):
        return aot.lower(variant(READ_F32, *changes), 32, **READ_BINDING).splitlines()

    lines = f32()
    helpers = [line.split("(")[0] for line in lines if line.startswith("uint f32_")]
    assert helpers == ["uint f32_add", "uint f32_sub", "uint f32_mul"], helpers
    assert all("precise float r = asfloat(a)" in line for line in lines if line.startswith("uint f32_")), lines
    assert lines[-4:] == [GUARD_IF, F32_STORE.format(READ_LOADS["read_copy"]), "    }", "}"], lines
    # A program that uses one operation carries only that helper.
    only_mul = f32(("v_add_f32_e32 v4, v2, v3", "v_mul_f32_e32 v4, v2, v3"), ("v_sub_f32_e32 v5, s0, v4", "v_mul_f32_e32 v5, s0, v4"),
                   ("v_subrev_f32_e32 v6, 0x40490fdb, v5", "v_mul_f32_e32 v6, 0x40490fdb, v5"),
                   ("v_add_f32_e32 v1, 1.0, v6", "v_mul_f32_e32 v1, 1.0, v6"), ("v_subrev_f32_e32 v1, -4.0, v1", "v_mul_f32_e32 v1, -4.0, v1"))
    assert [line.split("(")[0] for line in only_mul if line.startswith("uint f32_")] == ["uint f32_mul"]
    # A helper used only inside an LDS statement is still defined.
    in_lds = aot.lower(variant(lds_f32_program("ds_add_f32"), ("ds_add_f32 v5, v7", "v_mul_f32_e32 v7, 2.0, v7\nds_add_f32 v5, v7")),
                       32, **LDS_F32_BINDING).splitlines()
    assert any(line.startswith("uint f32_mul(") for line in in_lds), in_lds
    # v_sub_f32 is src0 - src1 and v_subrev_f32 is src1 - src0. A bare sign flip or an exchange is another program.
    assert f32(("v_sub_f32_e32 v5, s0, v4", "v_subrev_f32_e32 v5, s0, v4")) != lines
    assert f32(("v_subrev_f32_e32 v6, 0x40490fdb, v5", "v_sub_f32_e32 v6, 0x40490fdb, v5")) != lines
    # A copy of a result is the same program, and so is the float zero spelled as the inline integer 0.
    assert f32(("v_add_f32_e32 v1, 1.0, v6", "v_add_f32_e32 v7, 1.0, v6\nv_mov_b32_e32 v1, v7")) == lines
    assert f32(("v_add_f32_e32 v1, 1.0, v6", "v_add_f32_e32 v1, 0, v6"))[-3].count("0x00000000u") == 1
    unsupported, operand = "Unsupported compute instruction: ", "Unverified float operand"
    for message, changes in (
        # Fused multiply-add has no binary32 form in DXIL, so both stay refused, as do the other binary32 forms.
        (unsupported + "v_fmac_f32_e32", (("v_mul_f32_e32 v6, v2, v6", "v_fmac_f32_e32 v6, v2, v5"),)),
        (unsupported + "v_fma_f32", (("v_mul_f32_e32 v6, v2, v6", "v_fma_f32 v6, v2, v6, v5"),)),
        (unsupported + "v_mac_f32_e32", (("v_mul_f32_e32 v6, v2, v6", "v_mac_f32_e32 v6, v2, v5"),)),
        (unsupported + "v_max_f32_e32", (("v_mul_f32_e32 v6, v2, v6", "v_max_f32_e32 v6, v2, v6"),)),
        (unsupported + "v_mul_legacy_f32_e32", (("v_mul_f32_e32 v6, v2, v6", "v_mul_legacy_f32_e32 v6, v2, v6"),)),
        # The VOP3 spelling, output and input modifiers, an SGPR as the second source and a register range.
        (unsupported + "v_add_f32_e64", (("v_add_f32_e32 v4, v2, v3", "v_add_f32_e64 v4, v2, v3"),)),
        (unsupported + "v_add_f32_e32", (("v_add_f32_e32 v4, v2, v3", "v_add_f32_e32 v4, v2, v3 clamp"),)),
        (unsupported + "v_add_f32_e32", (("v_add_f32_e32 v4, v2, v3", "v_add_f32_e32 v4, -v2, v3"),)),
        (unsupported + "v_add_f32_e32", (("v_add_f32_e32 v4, v2, v3", "v_add_f32_e32 v4, |v2|, v3"),)),
        (unsupported + "v_mul_f32_e32", (("v_mul_f32_e32 v6, v2, v6", "v_mul_f32_e32 v6, v6, s0"),)),
        (unsupported + "v_sub_f32_e32", (("v_sub_f32_e32 v5, s0, v4", "v_sub_f32_e32 v5, s0, v[4:5]"),)),
        # A constant that is NaN, infinite or denormal, as a literal, a negative or positive inline integer and a scalar move.
        (operand, (("0x40490fdb", "0x7fc00000"),)),
        (operand, (("0x40490fdb", "0xff800000"),)),
        (operand, (("0x40490fdb", "0x00000001"),)),
        (operand, (("0x40490fdb", "0x807fffff"),)),
        (operand, (("0x40490fdb", "-1"),)),
        (operand, (("0x40490fdb", "5"),)),
        (operand, (("0x40490fdb", "1.5"),)),
        (operand, (("0x40490fdb", "0x1ffffffff"),)),
        (operand, (("v_subrev_f32_e32 v6, 0x40490fdb, v5", "s_mov_b32 s20, 0x7fc00000\nv_subrev_f32_e32 v6, s20, v5"),)),
        (operand, (("v_subrev_f32_e32 v6, 0x40490fdb, v5", "s_mov_b32 s20, 0x00400000\nv_subrev_f32_e32 v6, s20, v5"),)),
        # Sources that are not known words, and a destination past v255.
        (operand, (("s0, v4", "s20, v4"),)),
        (operand, (("s0, v4", "s8, v4"),)),
        (operand, (("v2, v3", "v2, v7"),)),
        (operand, (("v_add_f32_e32 v4, v2, v3", "v_add_f32_e32 v256, v2, v3"),)),
        (operand, (("v_subrev_f32_e32 v1, -4.0, v1", "v_subrev_f32_e32 v1, -4.0, v8"),)),
        ("Vector load result used or overwritten before s_waitcnt vmcnt(0)", (("s_waitcnt vmcnt(0)", ""),)),
        # A result is data only: not an address, not a lane-guard operand, and a long chain is refused.
        ("Unsupported store address", (("v1, v0, s[8:11]", "v0, v1, s[8:11]"),)),
        ("Unverified lane guard compare", (("v_add_f32_e32 v1, 1.0, v6",
                                            "v_add_f32_e32 v1, 1.0, v6\nv_cmp_gt_u32_e32 vcc_lo, s3, v1"),)),
        ("ALU expression too large", (("v_add_f32_e32 v1, 1.0, v6", "v_add_f32_e32 v1, 1.0, v6" + "\nv_add_f32_e32 v1, v1, v1" * 4),)),
    ):
        rejects(message, f32, *changes)


# The read-compute-write fixture with a scalar chain over the cap word s0 and the bound s3 feeding the min:
# add and sub that both wrap, and with a literal, or, xor with the bound and a logical shift right, each reading the
# previous result.
SCALAR_CHAIN = ("s_add_u32 s16, s0, -16\ns_sub_u32 s17, s16, 0x10000\ns_and_b32 s18, s17, 0xffffffe0\n"
                "s_or_b32 s19, s18, 10\ns_xor_b32 s20, s19, s3\ns_lshr_b32 s21, s20, 3")
READ_SCALAR = variant(READ_COMPUTE, ("v_and_b32_e32 v2, 0xffff, v1", SCALAR_CHAIN + "\nv_and_b32_e32 v2, 0xffff, v1"),
                      ("v_min_u32_e32 v1, s0, v1", "v_min_u32_e32 v1, s21, v1"))
SCALAR_WORD = ("min(((((((params[0].y + 0xfffffff0u) - 0x00010000u) & 0xffffffe0u) | 0x0000000au) ^ params[0].x)"
               " >> (0x00000003u & 31u)), ")

SCALAR_CASES = [("read_scalar", READ_SCALAR, 32, READ_BINDING)]


def scalar_alu_controls():
    """Scalar ALU on known words: exact output, then one rejection per operand or opcode outside the subset."""
    def scalar(*changes):
        return aot.lower(variant(READ_SCALAR, *changes), 32, **READ_BINDING).splitlines()

    lines = scalar()
    assert SCALAR_WORD in lines[-3] and lines[-3].startswith("        outputData.Store(tid.x * 4u + 0u, "), lines
    # The earliest scalar results stay inline in the chain: the first operand prints before the second.
    assert scalar(("s_lshr_b32 s21, s20, 3", "s_sub_u32 s21, 100, s20"))[-3].count("(0x00000064u - ") == 1
    # A scalar result can feed a vector instruction as a source, and a copy of it is the same program.
    assert scalar(("v_min_u32_e32 v1, s21, v1", "s_mov_b32 s22, s21\nv_min_u32_e32 v1, s22, v1")) == lines
    unsupported, operand = "Unsupported compute instruction: ", "Unverified scalar ALU operand"
    for message, changes in (
        # Opcodes outside the subset: signed, carry-in and 64-bit forms.
        (unsupported + "s_ashr_i32", (("s_lshr_b32 s21", "s_ashr_i32 s21"),)),
        (unsupported + "s_addc_u32", (("s_add_u32 s16", "s_addc_u32 s16"),)),
        (unsupported + "s_and_b64", (("s_and_b32 s18, s17, 0xffffffe0", "s_and_b64 s[18:19], s[16:17], 0xffffffe0"),)),
        # Sources that are not known words: an unwritten SGPR, a descriptor SGPR, the saved exec mask, and a
        # constant word still waiting for its s_waitcnt.
        (operand, (("s_add_u32 s16, s0, -16", "s_add_u32 s16, s30, 7"),)),
        (operand, (("s_add_u32 s16, s0, -16", "s_add_u32 s16, s8, 7"),)),
        (operand, (("s_add_u32 s16, s0, -16", "s_add_u32 s16, s2, 7"),)),
        (operand, (("s_add_u32 s16, s0, -16", "s_buffer_load_dword s23, s[4:7], 0x8\ns_add_u32 s16, s23, 7"),)),
        # A destination past s105, and a pending destination.
        (operand, (("s_add_u32 s16, s0, -16", "s_add_u32 s106, s0, 7"),)),
        (operand, (("s_add_u32 s16, s0, -16", "s_buffer_load_dword s16, s[4:7], 0x8\ns_add_u32 s16, s0, 7"),)),
        # A source wider than 32 bits.
        (operand, (("s_add_u32 s16, s0, -16", "s_add_u32 s16, s0, 0x1ffffffff"),)),
        # A vector register is not a scalar source.
        (unsupported + "s_add_u32", (("s_add_u32 s16, s0, -16", "s_add_u32 s16, s0, v1"),)),
        # Overwriting the saved exec mask breaks the guard restore.
        ("Unverified lane guard restore", (("s_cbranch_execz .LBB0_2", ""), (".LBB0_2:", "s_add_u32 s2, s0, 7"),
                                           ("s_endpgm", "s_or_b32 exec_lo, exec_lo, s2\ns_endpgm"))),
        # A scalar result is data only: not an address and not a lane-guard compare operand.
        ("Unsupported store address", (("v1, v0, s[8:11]", "v1, v21, s[8:11]"),
                                       ("v_min_u32_e32 v1, s21, v1", "v_min_u32_e32 v1, s21, v1\nv_mov_b32_e32 v21, s21"))),
        ("Unverified lane guard compare", (("v_min_u32_e32 v1, s21, v1",
                                            "v_min_u32_e32 v1, s21, v1\nv_cmp_gt_u32_e32 vcc_lo, s21, v1"),)),
        # Expressions are inline, so a scalar result that keeps doubling is rejected.
        ("ALU expression too large", (("s_lshr_b32 s21, s20, 3", "s_lshr_b32 s21, s20, 3" + "\ns_add_u32 s21, s21, s21" * 8),)),
    ):
        rejects(message, scalar, *changes)


def assemble(directory, assembler, objcopy, lines, *, cpu="gfx1013", features=None):
    src, obj, raw = [directory / x for x in ("exit.s", "exit.o", "exit.bin")]
    src.write_text("\n".join(lines) + "\n", encoding="ascii")
    attributes = [f"-mattr={features}"] if features is not None else []
    subprocess.run([assembler, "-triple=amdgcn--amdpal", f"-mcpu={cpu}", *attributes,
                    "-filetype=obj", src, "-o", obj], check=True)
    subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, raw], check=True)
    return raw.read_bytes()


def bounded_container(code, *, mesh=False):
    """A DXBC container whose FSSM v0x101 section ends in s_code_end padding."""
    padded = code + bytes.fromhex("00009fbf") * (4 - (len(code) // 4 + 10) % 4)
    payload = envelope(padded, directory_end=(40 + len(padded), 288 + len(padded)), compute_width=64,
                       mesh_limits=(64, 32) if mesh else None)
    return _read_named_container(payload, 1 + len(b"fixture") + 1)


def rejects(message, function, *args, **kwargs):
    """Require the named diagnostic, so a rejection cannot pass for an unrelated reason."""
    try:
        function(*args, **kwargs)
    except ValueError as error:
        assert message in str(error), error
    except Exception as error:
        raise AssertionError(f"Expected rejection: {message}, got {type(error).__name__}: {error}") from error
    else:
        raise AssertionError(f"Expected rejection: {message}")


def two_exits(directory, assembler, objcopy, library):
    """Extraction only: the code span is verified, the compute lowering still stops at the branch."""
    def extract(code, *, mesh=False, llvm=library):
        return aot.container_code(parts(bounded_container(code, mesh=mesh))[b"DXIL"],
                                  stage=13 if mesh else 5, llvm=llvm)

    def retarget(code, target):
        # Point the conditional branch at a byte offset that labels cannot express.
        pc = next(pc for pc, line in disassemble(code, library, offsets=True) if line.startswith("s_cbranch"))
        return code[:pc] + struct.pack("<h", (target - pc - 4) // 4) + code[pc + 2:]

    accepted = []
    for lines, first_stop in ((EXIT_KERNEL, "s_load_dword s3, s[0:1], 0x8"), (EXIT_CS, "s_cmp_ge_u32 s5, s4")):
        code = assemble(directory, assembler, objcopy, lines)
        decoded = disassemble(code, library)
        assert decoded.count("s_endpgm") == 2 and "s_cbranch_scc0 1" in decoded, decoded
        assert extract(code) == code
        accepted.append(code)
        # The stage and the exact count stay explicit: LLVM is required, mesh stays rejected.
        rejects("Unverified multi-exit program", extract, code, llvm=None)
        rejects("Unverified multi-exit program", extract, code, mesh=True)
        # The CLI and the audit pass LLVM through, so the first stop moves to the lowering.
        blob = bounded_container(code)
        container, output = directory / "exit.dxbc", directory / "exit.hlsl"
        container.write_bytes(blob)
        cli = subprocess.run([sys.executable, "-B", str(Path(aot.__file__)), container, "--output", output,
                              "--llvm", library, "--table-reg", "0", "--descriptor-offset", "0"],
                             capture_output=True, text=True)
        assert cli.returncode == 1 and f"Unsupported compute instruction: {first_stop}" in cli.stderr, cli.stderr
        assert shader_profile_audit.classify(blob, parts(blob), 5, library, None) == ([], "store lowering")

    kernel = accepted[0]
    # A per-lane return is one exit and passes without the two-exit check.
    lane = assemble(directory, assembler, objcopy, LANE_EXIT)
    assert extract(lane, llvm=None) == lane
    # A direct branch may skip alignment NOPs, which stay in the span; any other skipped word fails.
    nop = assemble(directory, assembler, objcopy, ["s_branch .Lstart", "s_nop 0", ".Lstart:"] + EXIT_KERNEL)
    assert extract(nop) == nop and nop.endswith(kernel)
    exit_at = EXIT_KERNEL.index(".Lbody:")
    skipped = EXIT_KERNEL[:exit_at] + ["s_mov_b32 s9, 0"] + EXIT_KERNEL[exit_at:]
    skipped_start = ["s_branch .Lstart", "s_mov_b32 s9, 0", ".Lstart:"] + EXIT_KERNEL
    # The target must directly follow the early exit, even with a NOP between them.
    after_exit = EXIT_KERNEL[:exit_at] + ["s_nop 0"] + EXIT_KERNEL[exit_at:]
    # Wrong targets: one instruction late, or the final exit with the store block unreachable.
    late = EXIT_KERNEL[:exit_at] + EXIT_KERNEL[exit_at + 1:exit_at + 2] + [".Lbody:"] + EXIT_KERNEL[exit_at + 2:]
    final = EXIT_KERNEL[:exit_at] + EXIT_KERNEL[exit_at + 1:-1] + [".Lbody:", "s_endpgm"]
    # Disconnected exits: an unconditional branch strands the early exit; no branch strands the store.
    jump = [line.replace("s_cbranch_scc0", "s_branch") for line in EXIT_KERNEL]
    straight = [line.replace("s_cbranch_scc0 .Lbody", "s_nop 0") for line in EXIT_KERNEL]
    for lines in (skipped, skipped_start, after_exit, late, final, jump, straight):
        rejects("Unreachable code or unverified alternate exit",
                extract, assemble(directory, assembler, objcopy, lines))
    # A third exit, and a literal that spells s_endpgm, exceed the exact two-marker limit.
    third = EXIT_KERNEL[:exit_at] + ["s_cmp_eq_u32 s3, 0", "s_cbranch_scc0 .Lmid", "s_endpgm", ".Lmid:"] + \
        EXIT_KERNEL[exit_at:]
    spelled = [line.replace("v_mov_b32_e32 v1, s2", "v_mov_b32_e32 v1, 0xbf810000") for line in EXIT_KERNEL]
    for lines in (third, spelled):
        rejects("Unverified multi-exit program", extract, assemble(directory, assembler, objcopy, lines))
    # In a one-exit program the same literal makes two raw markers but only one decoded exit.
    rejects("Expected two decoded exits", extract, assemble(directory, assembler, objcopy, [
        line.replace("v_mov_b32_e32 v1, s2", "v_mov_b32_e32 v1, 0xbf810000") for line in LANE_EXIT]))
    # Branches into a literal operand or into the trailing s_code_end padding.
    literal = assemble(directory, assembler, objcopy,
                       [line.replace("v_mov_b32_e32 v1, s2", "v_mov_b32_e32 v1, 0x12345678") for line in EXIT_KERNEL])
    assert extract(literal) == literal
    operand = next(pc for pc, line in disassemble(literal, library, offsets=True) if "0x12345678" in line) + 4
    for target in (operand, len(literal)):
        rejects("Branch outside decoded instruction boundaries", extract, retarget(literal, target))


def store_container(code, dims):
    padded = code + bytes.fromhex("00009fbf") * (4 - (len(code) // 4 + 10) % 4)
    payload = envelope(padded, directory_end=(40 + len(padded), 288 + len(padded)), compute_width=dims[0])
    blob = bytearray(_read_named_container(payload, 1 + len(b"fixture") + 1))
    # The shared envelope writes X only; set Y and Z in the PSV0 runtime info.
    struct.pack_into("<2I", blob, blob.index(b"PSV0") + 8 + 44, *dims[1:])
    return bytes(blob)



DS_BINDING = dict(table_reg=7, descriptor_offset=0x30, group_reg=11, stride=4, cpu="gfx1030")
DS_FEATURES = "+wavefrontsize32,-wavefrontsize64"
DS_PREFIX = INDEXED[:4] + ["s_waitcnt lgkmcnt(0)"]
DS_OPS = ("ds_bpermute_b32", "ds_permute_b32")


def ds_program(op, bias):
    return DS_PREFIX + [
        f"v_add_nc_u32_e32 v5, {bias}, v0",
        "v_and_b32_e32 v5, 31, v5",
        "v_lshlrev_b32_e32 v5, 2, v5",
        # Alias the byte address to prove that the result can replace its input.
        f"{op} v5, v5, v8",
        "s_waitcnt lgkmcnt(0)",
        "buffer_store_dword v5, v8, s[20:23], 0 idxen",
        "s_endpgm",
    ]


DS_CASES = [(op, ds_program(op, 5), DS_BINDING) for op in DS_OPS]


def ds_reference(op, groups):
    bias = 5 if op == "ds_bpermute_b32" else -5
    return [lane // 32 * 32 + ((lane % 32 + bias) % 32) for lane in range(groups * 32)]


def ds_controls():
    for op, lines, binding in DS_CASES:
        expected_bias = 5 if op == "ds_bpermute_b32" else 27
        assert f"[(local.x + {expected_bias}u) & 31u]" in aot.lower(lines, 32, **binding)
        valid = f"{op} v5, v5, v8"
        for operands in ("v5, v8, v8", "v5, v5, v255"):
            rejects("Unverified DS lane routing", aot.lower, variant(lines, (valid, f"{op} {operands}")),
                    32, **binding)
        rejects("Unsupported compute instruction", aot.lower,
                variant(lines, (valid, valid + " offset:4")), 32, **binding)
        i = lines.index(valid)
        masked = lines[:i] + ["v_cmp_gt_u32_e32 vcc_lo, 16, v8", "s_and_saveexec_b32 s5, vcc_lo"] + lines[i:]
        rejects("Unverified DS lane routing", aot.lower, masked, 32, **binding)
        rejects("Expected immediate s_waitcnt lgkmcnt(0) after DS", aot.lower,
                lines[:i + 1] + lines[i + 2:], 32, **binding)


LDS_BINDING = DS_BINDING | {"stride": 12, "lds_bytes": 128}


def signed_order(word):
    # Flipping the sign bit places negatives before nonnegatives in an unsigned comparison.
    return word ^ 0x80000000


LDS_UPDATES = {
    "ds_add_u32": operator.add,
    "ds_sub_u32": operator.sub,
    "ds_and_b32": operator.and_,
    "ds_or_b32": operator.or_,
    "ds_xor_b32": operator.xor,
    "ds_rsub_u32": lambda old, value: value - old,
    "ds_min_i32": lambda old, value: min(old, value, key=signed_order),
    "ds_max_i32": lambda old, value: max(old, value, key=signed_order),
    "ds_min_u32": min,
    "ds_max_u32": max,
}
LDS_ORDER_MUTANTS = {
    "ds_rsub_u32": "ds_sub_u32",
    "ds_min_i32": "ds_min_u32",
    "ds_max_i32": "ds_max_u32",
    "ds_min_u32": "ds_min_i32",
    "ds_max_u32": "ds_max_i32",
}


def lds_update_inputs(op):
    # Each pair is (scale, base) for a lane-dependent 32-bit word.
    if op in LDS_ORDER_MUTANTS:
        return (1, 0x7ffffff0), (1, 0xfffffff8), (4, 0xfffffff0)
    return (1, 0xfffffff0), (1, 0x40000021), (1, 0x80000003)


def lds_update_program(op):
    (init_scale, init_base), (first_scale, first_base), (second_scale, second_base) = lds_update_inputs(op)
    return DS_PREFIX + [
        "v_lshlrev_b32_e32 v5, 2, v0",
        f"v_mad_u32_u24 v6, {init_scale}, v8, 0x{init_base:08x}",
        "ds_write_b32 v5, v6",
        "s_waitcnt lgkmcnt(0)",
        "ds_read_b32 v10, v5",
        "s_waitcnt lgkmcnt(0)",
        f"v_mad_u32_u24 v6, {first_scale}, v8, 0x{first_base:08x}",
        f"{op} v5, v6",
        "s_waitcnt lgkmcnt(0)",
        "v_mov_b32_e32 v6, 0",
        "ds_read_b32 v11, v5",
        "s_waitcnt lgkmcnt(0)",
        f"v_mad_u32_u24 v9, {second_scale}, v8, 0x{second_base:08x}",
        f"{op} v5, v9",
        "s_waitcnt lgkmcnt(0)",
        "v_mov_b32_e32 v9, 0",
        "ds_read_b32 v12, v5",
        "s_waitcnt lgkmcnt(0)",
        "buffer_store_dwordx3 v[10:12], v8, s[20:23], 0 idxen",
        "s_endpgm",
    ]


def lds_update_reference(op, groups):
    update = LDS_UPDATES[op]
    inputs = lds_update_inputs(op)
    words = []
    for lane in range(groups * 32):
        initial, src1, src2 = ((scale * lane + base) & 0xffffffff for scale, base in inputs)
        first = update(initial, src1) & 0xffffffff
        second = update(first, src2) & 0xffffffff
        words.extend((initial, first, second))
    return words


LDS_RETURN_UPDATES = {
    "ds_add_rtn_u32": "ds_add_u32",
    "ds_sub_rtn_u32": "ds_sub_u32",
    "ds_and_rtn_b32": "ds_and_b32",
    "ds_or_rtn_b32": "ds_or_b32",
    "ds_xor_rtn_b32": "ds_xor_b32",
    "ds_rsub_rtn_u32": "ds_rsub_u32",
    "ds_min_rtn_i32": "ds_min_i32",
    "ds_max_rtn_i32": "ds_max_i32",
    "ds_min_rtn_u32": "ds_min_u32",
    "ds_max_rtn_u32": "ds_max_u32",
}


def lds_return_update_program(op):
    (init_scale, init_base), (first_scale, first_base), (second_scale, second_base) = lds_update_inputs(LDS_RETURN_UPDATES[op])
    return DS_PREFIX + [
        "v_lshlrev_b32_e32 v5, 2, v0",
        "v_lshlrev_b32_e32 v11, 2, v0",
        f"v_mad_u32_u24 v6, {init_scale}, v8, 0x{init_base:08x}",
        "ds_write_b32 v5, v6",
        "s_waitcnt lgkmcnt(0)",
        # First alias the data VGPR, then alias a copy of the address VGPR.
        f"v_mad_u32_u24 v10, {first_scale}, v8, 0x{first_base:08x}",
        f"{op} v10, v5, v10",
        "s_waitcnt lgkmcnt(0)",
        f"v_mad_u32_u24 v9, {second_scale}, v8, 0x{second_base:08x}",
        f"{op} v11, v11, v9",
        "s_waitcnt lgkmcnt(0)",
        "ds_read_b32 v12, v5",
        "s_waitcnt lgkmcnt(0)",
        "buffer_store_dwordx3 v[10:12], v8, s[20:23], 0 idxen",
        "s_endpgm",
    ]


LDS_PROGRAM = DS_PREFIX + [
    "v_lshlrev_b32_e32 v5, 2, v0",
    "v_mad_u32_u24 v6, 1, v8, 0x89abfff0",
    "ds_write_b32 v5, v6",
    "s_waitcnt lgkmcnt(0)",
    "v_mov_b32_e32 v6, 0",
    "ds_read_b32 v10, v5",
    "s_waitcnt lgkmcnt(0)",
    "ds_read_u16 v11, v5",
    "s_waitcnt lgkmcnt(0)",
    "ds_read_u16 v12, v5 offset:2",
    "s_waitcnt lgkmcnt(0)",
    "buffer_store_dwordx3 v[10:12], v8, s[20:23], 0 idxen",
    "s_endpgm",
]
LDS_SUBWORDS = {f"subword_{width}_{offset}": (width, offset)
                for width, offsets in ((8, range(4)), (16, (0, 2))) for offset in offsets}


def lds_subword_program(width, offset):
    suffix = f" offset:{offset}" if offset else ""
    base = 0x12340070 if width == 8 else 0x12347ff0
    neighbor = "" if offset else " offset:3"
    return DS_PREFIX + [
        "v_lshlrev_b32_e32 v5, 2, v0",
        "v_mov_b32_e32 v6, 0x89abcdef",
        "ds_write_b32 v5, v6",
        "s_waitcnt lgkmcnt(0)",
        # A second partial write must preserve this change as well as untouched bytes.
        "v_mov_b32_e32 v9, 0x42",
        f"ds_write_b8 v5, v9{neighbor}",
        "s_waitcnt lgkmcnt(0)",
        f"v_mad_u32_u24 v6, 1, v8, 0x{base:08x}",
        f"ds_write_b{width} v5, v6{suffix}",
        "s_waitcnt lgkmcnt(0)",
        "v_mov_b32_e32 v6, 0",
        "ds_read_b32 v10, v5",
        "s_waitcnt lgkmcnt(0)",
        f"ds_read_u{width} v11, v5{suffix}",
        "s_waitcnt lgkmcnt(0)",
        f"ds_read_i{width} v12, v5{suffix}",
        "s_waitcnt lgkmcnt(0)",
        # Reading captures the value before this overwrite, even though export comes later.
        "ds_write_b32 v5, v6",
        "s_waitcnt lgkmcnt(0)",
        "buffer_store_dwordx3 v[10:12], v8, s[20:23], 0 idxen",
        "s_endpgm",
    ]


# Capture the same byte before and after a halfword write, then overwrite
# that byte alone and read the complete word.
LDS_OVERLAP_PROGRAM = DS_PREFIX + [
    "v_lshlrev_b32_e32 v5, 2, v0",
    "v_mad_u32_u24 v6, 1, v8, 0x89abfff0",
    "ds_write_b32 v5, v6",
    "s_waitcnt lgkmcnt(0)",
    "ds_read_u8 v10, v5 offset:1",
    "s_waitcnt lgkmcnt(0)",
    "v_mov_b32_e32 v7, 0x1234",
    "ds_write_b16 v5, v7",
    "s_waitcnt lgkmcnt(0)",
    "ds_read_u8 v11, v5 offset:1",
    "s_waitcnt lgkmcnt(0)",
    "v_mov_b32_e32 v7, 0x5a",
    "ds_write_b8 v5, v7 offset:1",
    "s_waitcnt lgkmcnt(0)",
    "ds_read_b32 v12, v5",
    "s_waitcnt lgkmcnt(0)",
    "buffer_store_dwordx3 v[10:12], v8, s[20:23], 0 idxen",
    "s_endpgm",
]


LDS_CASES = [("private", LDS_PROGRAM, LDS_BINDING),
             ("lds_overlap", LDS_OVERLAP_PROGRAM, LDS_BINDING)] + [
    (name, lds_subword_program(*layout), LDS_BINDING) for name, layout in LDS_SUBWORDS.items()
]
LDS_ARITHMETIC_CASES = [(op, lds_update_program(op), LDS_BINDING) for op in LDS_UPDATES] + [
    (op, lds_return_update_program(op), LDS_BINDING) for op in LDS_RETURN_UPDATES
]


LDS_D16_READS = {
    "ds_read_u8_d16": (1, False, False),
    "ds_read_u8_d16_hi": (1, False, True),
    "ds_read_i8_d16": (1, True, False),
    "ds_read_i8_d16_hi": (1, True, True),
    "ds_read_u16_d16": (2, False, False),
    "ds_read_u16_d16_hi": (2, False, True),
}
LDS_D16_DESTINATIONS = (0xa53172be, 0xe8ba23f1, 0x64c139a7)


def lds_d16_offsets(size):
    return (0, 1, 3) if size == 1 else (0, 2, 0)


def lds_d16_program(op):
    size, _, _ = LDS_D16_READS[op]
    lines = DS_PREFIX + [
        "v_lshlrev_b32_e32 v5, 2, v0",
        "v_mad_u32_u24 v6, 1, v8, 0x887f80f0",
        "ds_write_b32 v5, v6",
        "s_waitcnt lgkmcnt(0)",
    ]
    for dst, value, offset in zip(range(10, 13), LDS_D16_DESTINATIONS, lds_d16_offsets(size)):
        suffix = f" offset:{offset}" if offset else ""
        lines += [f"v_mov_b32_e32 v{dst}, 0x{value:08x}", f"{op} v{dst}, v5{suffix}", "s_waitcnt lgkmcnt(0)"]
    return lines + ["buffer_store_dwordx3 v[10:12], v8, s[20:23], 0 idxen", "s_endpgm"]


def lds_d16_reference(op, groups):
    size, signed, high = LDS_D16_READS[op]
    words = []
    start = 2 if high else 0
    for lane in range(groups * 32):
        data = (0x887f80f0 + lane).to_bytes(4, "little")
        for initial, offset in zip(LDS_D16_DESTINATIONS, lds_d16_offsets(size)):
            field = int.from_bytes(data[offset:offset + size], "little", signed=signed)
            result = bytearray(initial.to_bytes(4, "little"))
            result[start:start + 2] = (field & 0xffff).to_bytes(2, "little")
            words.append(int.from_bytes(result, "little"))
    return words


LDS_CASES += [(op, lds_d16_program(op), LDS_BINDING) for op in LDS_D16_READS]


def lds_subword_reference(width, offset, groups):
    expected = []
    size = width // 8
    for lane in range(groups * 32):
        value = (0x70 if width == 8 else 0x7ff0) + lane
        data = value.to_bytes(size, "little")
        word = bytearray.fromhex("ef cd ab 89")
        word[0 if offset else 3] = 0x42
        word[offset:offset + size] = data
        expected.extend((int.from_bytes(word, "little"), value,
                         int.from_bytes(data, "little", signed=True) & 0xffffffff))
    return expected


LDS_REPLACEMENTS = ("ds_cmpst_b32", "ds_cmpst_rtn_b32", "ds_mskor_b32", "ds_mskor_rtn_b32", "ds_wrxchg_rtn_b32")


def lds_replacement_instruction(op):
    dst = "v10, " if "_rtn_" in op else ""
    replacement = ", v7" if op != "ds_wrxchg_rtn_b32" else ""
    return f"{op} {dst}v5, v6{replacement}"


def lds_replacement_program(op):
    lines = DS_PREFIX + [
        "v_lshlrev_b32_e32 v5, 2, v0",
        "v_mad_u32_u24 v9, 1, v8, 0x81a23450",
        "ds_write_b32 v5, v9",
        "s_waitcnt lgkmcnt(0)",
        "v_mov_b32_e32 v6, 0x81a23450" if "cmpst" in op else "v_mov_b32_e32 v6, 0x00ff0ff0",
        "v_mov_b32_e32 v7, 0x400580b1",
    ]
    if "_rtn_" not in op:
        lines += ["ds_read_b32 v10, v5", "s_waitcnt lgkmcnt(0)"]
    return lines + [
        lds_replacement_instruction(op),
        "s_waitcnt lgkmcnt(0)",
        "ds_read_b32 v11, v5",
        "s_waitcnt lgkmcnt(0)",
        "buffer_store_dwordx2 v[10:11], v8, s[20:23], 0 idxen",
        "s_endpgm",
    ]


def lds_replacement_reference(op, groups):
    words = []
    for lane in range(groups * 32):
        old = 0x81a23450 + lane
        if "cmpst" in op:
            updated = 0x400580b1 if lane == 0 else old
        elif "mskor" in op:
            updated = (old & 0xff00f00f) | 0x400580b1
        else:
            updated = 0x00ff0ff0
        words.extend((old, updated, SENTINEL))
    return words


LDS_REPLACEMENT_CASES = [(op, lds_replacement_program(op), LDS_BINDING) for op in LDS_REPLACEMENTS]


def lds_overlap_reference(groups):
    words = []
    for lane in range(groups * 32):
        data = bytearray((0x89abfff0 + lane).to_bytes(4, "little"))
        pre = data[1]
        data[0:2] = b"\x34\x12"
        middle = data[1]
        data[1] = 0x5a
        words.extend((pre, middle, int.from_bytes(data, "little")))
    return words


LDS_COUNTERS = {
    "ds_inc_u32": lambda old: old + 1 if old < 7 else 0,
    "ds_dec_u32": lambda old: old - 1 if 1 <= old <= 7 else 7,
    "ds_inc_rtn_u32": lambda old: old + 1 if old < 7 else 0,
    "ds_dec_rtn_u32": lambda old: old - 1 if 1 <= old <= 7 else 7,
    "ds_wrap_rtn_b32": lambda old: old - 7 if old >= 7 else old + 3,
}


def lds_counter_instruction(op):
    if op == "ds_wrap_rtn_b32":
        return f"{op} v10, v5, v10, v7"
    if "_rtn_" in op:
        return f"{op} v10, v5, v10"
    return f"{op} v5, v6"


def lds_counter_program(op):
    snapshot = ["v_mov_b32_e32 v10, 7"] if "_rtn_" in op else ["ds_read_b32 v10, v5", "s_waitcnt lgkmcnt(0)"]
    return DS_PREFIX + [
        "v_lshlrev_b32_e32 v5, 2, v0",
        "v_mad_u32_u24 v6, 1, v8, 0xfffffff0",
        "ds_write_b32 v5, v6",
        "s_waitcnt lgkmcnt(0)",
        "v_mov_b32_e32 v6, 7",
        "v_mov_b32_e32 v7, 3",
    ] + snapshot + [
        lds_counter_instruction(op),
        "s_waitcnt lgkmcnt(0)",
        "ds_read_b32 v11, v5",
        "s_waitcnt lgkmcnt(0)",
        "buffer_store_dwordx2 v[10:11], v8, s[20:23], 0 idxen",
        "s_endpgm",
    ]


def lds_counter_reference(op, groups):
    words = []
    for lane in range(groups * 32):
        old = (lane - 16) & 0xffffffff
        words.extend((old, LDS_COUNTERS[op](old), SENTINEL))
    return words


LDS_REPLACEMENT_CASES += [(op, lds_counter_program(op), LDS_BINDING) for op in LDS_COUNTERS]


def lds_reference(groups):
    words = []
    for lane in range(groups * 32):
        word = 0x89abfff0 + lane
        words.extend((word, word & 0xffff, word >> 16))
    return words


LDS_STRIDED_PROGRAMS = {
    "lds_st64_b32": DS_PREFIX + [
        "v_lshlrev_b32_e32 v5, 2, v0", "v_mad_u32_u24 v6, 1, v8, 0x76543210", "v_mov_b32_e32 v7, 0x89abcdef",
        "ds_write2st64_b32 v5, v6, v7 offset0:2 offset1:3", "s_waitcnt lgkmcnt(0)",
        "ds_read2st64_b32 v[10:11], v5 offset0:3 offset1:2", "s_waitcnt lgkmcnt(0)",
        "buffer_store_dwordx2 v[10:11], v8, s[20:23], 0 idxen", "s_endpgm",
    ],
    "lds_st64_b64": DS_PREFIX + [
        "v_lshlrev_b32_e32 v5, 3, v0", "v_mad_u32_u24 v6, 1, v8, 0x76543210", "v_mov_b32_e32 v7, 0x89abcdef",
        "v_mov_b32_e32 v10, 0x12345678", "v_mov_b32_e32 v11, 0x9abcdef0",
        "ds_write2st64_b64 v5, v[6:7], v[10:11] offset0:1 offset1:2", "s_waitcnt lgkmcnt(0)",
        "ds_read2st64_b64 v[10:13], v5 offset0:2 offset1:1", "s_waitcnt lgkmcnt(0)",
        "buffer_store_dwordx4 v[10:13], v8, s[20:23], 0 idxen", "s_endpgm",
    ],
    "lds_st64_xchg_b64": DS_PREFIX + [
        "v_lshlrev_b32_e32 v5, 3, v0", "v_mad_u32_u24 v6, 1, v8, 0x76543210", "v_mov_b32_e32 v7, 0x89abcdef",
        "v_mov_b32_e32 v10, 0x12345678", "v_mov_b32_e32 v11, 0x9abcdef0",
        "ds_write2st64_b64 v5, v[6:7], v[10:11] offset0:1 offset1:2", "s_waitcnt lgkmcnt(0)",
        "v_mov_b32_e32 v12, 0x11111111", "v_mov_b32_e32 v13, 0x22222222",
        "v_mov_b32_e32 v14, 0x33333333", "v_mov_b32_e32 v15, 0x44444444",
        "ds_wrxchg2st64_rtn_b64 v[12:15], v5, v[12:13], v[14:15] offset0:2 offset1:1", "s_waitcnt lgkmcnt(0)",
        "ds_read2st64_b64 v[16:19], v5 offset0:1 offset1:2", "s_waitcnt lgkmcnt(0)",
        "buffer_store_dwordx4 v[12:15], v8, s[20:23], 0 idxen",
        "buffer_store_dwordx4 v[16:19], v8, s[20:23], 0 idxen offset:16", "s_endpgm",
    ],
    "lds_st64_xchg": DS_PREFIX + [
        "v_lshlrev_b32_e32 v5, 2, v0", "v_mad_u32_u24 v6, 1, v8, 0x76543210", "v_mov_b32_e32 v7, 0x89abcdef",
        "ds_write2st64_b32 v5, v6, v7 offset0:2 offset1:3", "s_waitcnt lgkmcnt(0)",
        "v_mov_b32_e32 v10, 0x12345678", "v_mov_b32_e32 v11, 0x9abcdef0",
        "ds_wrxchg2st64_rtn_b32 v[10:11], v5, v10, v11 offset0:3 offset1:2", "s_waitcnt lgkmcnt(0)",
        "ds_read2st64_b32 v[12:13], v5 offset0:2 offset1:3", "s_waitcnt lgkmcnt(0)",
        "buffer_store_dwordx4 v[10:13], v8, s[20:23], 0 idxen", "s_endpgm",
    ],
}
LDS_STRIDED_CASES = [(name, lines, LDS_BINDING | {"lds_bytes": 1280, "stride": {"lds_st64_b32": 8, "lds_st64_xchg_b64": 32}.get(name, 16)})
                     for name, lines in LDS_STRIDED_PROGRAMS.items()]


def lds_strided_reference(name, groups):
    words = []
    for lane in range(groups * 32):
        first = 0x76543210 + lane
        if name == "lds_st64_b32":
            words.extend((0x89abcdef, first))
        elif name == "lds_st64_xchg_b64":
            words.extend((0x12345678, 0x9abcdef0, first, 0x89abcdef,
                          0x33333333, 0x44444444, 0x11111111, 0x22222222))
        elif name == "lds_st64_b64":
            words.extend((0x12345678, 0x9abcdef0, first, 0x89abcdef))
        else:
            words.extend((0x89abcdef, first, 0x9abcdef0, 0x12345678))
    return words



LDS_F32_BINDING = LDS_BINDING | {"stride": 8, "lds_f32_mode": "normal-rne"}
LDS_F32_OPS = {
    "ds_add_f32": "v5, v7", "ds_add_rtn_f32": "v11, v5, v11",
    "ds_min_f32": "v5, v7", "ds_max_f32": "v5, v7", "ds_cmpst_f32": "v5, v6, v7",
    "ds_min_rtn_f32": "v11, v5, v11", "ds_max_rtn_f32": "v11, v5, v11",
    "ds_cmpst_rtn_f32": "v11, v5, v6, v11",
}


def lds_f32_program(op):
    add = op.startswith("ds_add")
    return DS_PREFIX + [
        "v_lshlrev_b32_e32 v5, 2, v0", "v_and_b32_e32 v6, 1, v8",
        "v_mov_b32_e32 v7, 0x40200000", "v_mad_u32_u24 v6, 0x800000, v6, v7",
        "ds_write_b32 v5, v6", "s_waitcnt lgkmcnt(0)",
        "ds_read_b32 v11, v5", "s_waitcnt lgkmcnt(0)",
        f"v_mov_b32_e32 v7, {'0xc0400000' if add else '0x40400000'}",
    ] + (["v_mov_b32_e32 v11, v7"] if "_rtn" in op else []) + (
        ["v_mov_b32_e32 v6, 0x40200000"] if op.startswith("ds_cmpst") else []) + [
        f"{op} {LDS_F32_OPS[op]}", "s_waitcnt lgkmcnt(0)",
        "ds_read_b32 v10, v5", "s_waitcnt lgkmcnt(0)",
        "buffer_store_dwordx2 v[10:11], v8, s[20:23], 0 idxen", "s_endpgm",
    ]


def lds_f32_reference(op, groups):
    words = []
    for lane in range(groups * 32):
        old = 2.5 if lane % 2 == 0 else 5.0
        if op.startswith("ds_add"):
            new = old - 3.0
        elif op.startswith("ds_cmpst"):
            new = 3.0 if old == 2.5 else old
        else:
            new = (min if op.startswith("ds_min") else max)(old, 3.0)
        words.extend(struct.unpack("<II", struct.pack("<ff", new, old)))
    return words


LDS_F32_CASES = [(op, lds_f32_program(op), LDS_F32_BINDING) for op in LDS_F32_OPS]


LDS_WIDE_BINDING = LDS_BINDING | {"lds_bytes": 512}
LDS_WIDE_SETUP = DS_PREFIX + [
    "v_lshlrev_b32_e32 v5, 4, v0",
    "v_mad_u32_u24 v6, 1, v8, 0x12345670",
    "v_mov_b32_e32 v7, 0x89abcdef",
    "v_mov_b32_e32 v9, 0x76543210",
]


def _signed64(value):
    return value - (1 << 64) if value >> 63 else value


def _min_i64(a, b):
    return a if _signed64(a) < _signed64(b) else b


def _max_i64(a, b):
    return a if _signed64(a) > _signed64(b) else b


LDS_QWORD_UPDATES = {
    "ds_add_u64": operator.add, "ds_sub_u64": operator.sub, "ds_rsub_u64": lambda a, b: b - a,
    "ds_and_b64": operator.and_, "ds_or_b64": operator.or_, "ds_xor_b64": operator.xor,
    "ds_min_u64": min, "ds_max_u64": max, "ds_min_i64": _min_i64, "ds_max_i64": _max_i64,
}


LDS_QWORD_RETURNS = {f"{stem}_rtn_{width}": op for op in LDS_QWORD_UPDATES
                     for stem, width in [op.rsplit("_", 1)]}


LDS_QWORD_COUNTERS = ("ds_inc_u64", "ds_inc_rtn_u64", "ds_dec_u64", "ds_dec_rtn_u64", "ds_condxchg32_rtn_b64")


def lds_qword_counter_program(op):
    returns = "_rtn" in op
    dest = "v[18:19], " if returns else ""
    inputs = (["v_mov_b32_e32 v18, 0x92345678", "v_mov_b32_e32 v19, 0x12345678"]
              if "condxchg" in op else ["v_mov_b32_e32 v18, 8", "v_mov_b32_e32 v19, 1"])
    return LDS_WIDE_SETUP + [
        "v_and_b32_e32 v16, 31, v8", "v_mad_u32_u24 v16, 1, v16, 0xfffffff0", "v_lshrrev_b32_e32 v17, 5, v8",
        "ds_write_b64 v5, v[16:17] offset:8", "s_waitcnt lgkmcnt(0)",
    ] + inputs + [
        f"{op} {dest}v5, v[18:19] offset:8", "s_waitcnt lgkmcnt(0)",
        "ds_read_b64 v[10:11], v5 offset:8", "s_waitcnt lgkmcnt(0)",
    ] + (["v_mov_b32_e32 v12, v18", "v_mov_b32_e32 v13, v19",
          "buffer_store_dwordx4 v[10:13], v8, s[20:23], 0 idxen"] if returns else
         ["buffer_store_dwordx2 v[10:11], v8, s[20:23], 0 idxen"]) + ["s_endpgm"]


def lds_qword_counter_reference(op, groups):
    words = []
    for lane in range(groups * 32):
        old = (lane // 32 << 32) | ((0xfffffff0 + lane % 32) & 0xffffffff)
        limit = 0x100000008
        if "inc" in op:
            new = 0 if old >= limit else old + 1
        elif "dec" in op:
            new = limit if old == 0 or old > limit else old - 1
        else:
            new = old & 0xffffffff00000000 | 0x92345678
        words.extend((new & 0xffffffff, new >> 32))
        if "_rtn" in op:
            words.extend((old & 0xffffffff, old >> 32))
    return words


def lds_qword_program(op):
    returns = op in LDS_QWORD_RETURNS
    ret_operand = "v[18:19], " if returns else ""
    if LDS_QWORD_RETURNS.get(op, op) in ("ds_min_i64", "ds_max_i64", "ds_min_u64", "ds_max_u64"):
        # Even lanes cross the signed high-word boundary; odd lanes repeat the
        # high word and exercise the unsigned low-word tie-break.
        prior = [
            "v_mad_u32_u24 v16, 1, v8, 0xfffffff0",
            "v_and_b32_e32 v17, 1, v8",
            "v_mad_u32_u24 v17, 1, v17, 0x7fffffff",
            "v_mov_b32_e32 v18, 0x00000018", "v_mov_b32_e32 v19, 0x80000000",
        ]
    else:
        prior = [
            "v_mad_u32_u24 v16, 1, v8, 0xfffffff0",
            "v_mov_b32_e32 v17, 0xfffffff1",
            "v_mov_b32_e32 v18, 0x00000018", "v_mov_b32_e32 v19, 0x0000000e",
        ]
    return LDS_WIDE_SETUP + [
        *prior,
        "ds_write_b64 v5, v[16:17] offset:8", "s_waitcnt lgkmcnt(0)",
        f"{op} {ret_operand}v5, v[18:19] offset:8", "s_waitcnt lgkmcnt(0)",
        "ds_read_b64 v[10:11], v5 offset:8", "s_waitcnt lgkmcnt(0)",
    ] + (["v_mov_b32_e32 v12, v18", "v_mov_b32_e32 v13, v19",
          "buffer_store_dwordx4 v[10:13], v8, s[20:23], 0 idxen"] if returns else
         ["buffer_store_dwordx2 v[10:11], v8, s[20:23], 0 idxen"]) + ["s_endpgm"]


def lds_qword_reference(op, groups):
    words = []
    for lane in range(groups * 32):
        if LDS_QWORD_RETURNS.get(op, op) in ("ds_min_i64", "ds_max_i64", "ds_min_u64", "ds_max_u64"):
            previous = ((0x7fffffff + (lane & 1)) << 32) | ((0xfffffff0 + lane) & 0xffffffff)
            source = 0x8000000000000018
        else:
            previous = (0xfffffff1 << 32) | ((0xfffffff0 + lane) & 0xffffffff)
            source = 0x0000000e00000018
        current = LDS_QWORD_UPDATES[LDS_QWORD_RETURNS.get(op, op)](previous, source)
        words.extend((current & 0xffffffff, (current >> 32) & 0xffffffff))
        if op in LDS_QWORD_RETURNS:
            words.extend((previous & 0xffffffff, previous >> 32))
    return words


def without_low_tiebreak(source):
    """Remove the low-word clause from a qword min/max condition."""
    start = source.index(" || ((")
    depth = 0
    for end in range(start + len(" || "), len(source)):
        depth += {"(": 1, ")": -1}.get(source[end], 0)
        if depth == 0:
            return source[:start] + source[end + 1:]
    raise AssertionError("Missing qword comparison tie-break")


LDS_WIDE_QUAD_SETUP = [
    "v_mov_b32_e32 v16, v6", "v_mov_b32_e32 v17, v7",
    "v_mov_b32_e32 v18, v9", "v_mov_b32_e32 v19, 0xf0e1d2c3",
]
LDS_WIDE_QUADS = {
    "lds_b128": ["ds_write_b128 v5, v[16:19]", "ds_read_b128 v[10:13], v5"],
    "lds_write2_b32": ["ds_write_b128 v5, v[16:19]", "ds_write2_b32 v5, v6, v9 offset0:3 offset1:1",
                       "ds_read_b128 v[10:13], v5"],
    "lds_pair_b64": ["ds_write2_b64 v5, v[16:17], v[18:19] offset0:1",
                     "ds_read2_b64 v[10:13], v5 offset1:1"],
}
LDS_WIDE_PROGRAMS = {
    "lds_b64": [
        "v_mov_b32_e32 v16, 5", "v_mov_b32_e32 v17, 9",
        "ds_write_b64 v5, v[16:17]", "s_waitcnt lgkmcnt(0)",
        "ds_write_b64 v5, v[6:7] offset:8", "s_waitcnt lgkmcnt(0)",
        "ds_read_b64 v[10:11], v5 offset:8", "s_waitcnt lgkmcnt(0)",
        "v_mov_b32_e32 v12, 0x76543210",
    ],
    "lds_b96": [
        "v_mov_b32_e32 v16, v6", "v_mov_b32_e32 v17, v7", "v_mov_b32_e32 v18, v9",
        "ds_write_b96 v5, v[16:18]", "s_waitcnt lgkmcnt(0)",
        "ds_read_b96 v[10:12], v5", "s_waitcnt lgkmcnt(0)",
    ],
    "lds_xchg2_b32": [
        "v_mov_b32_e32 v16, v6", "v_mov_b32_e32 v17, v7", "v_mov_b32_e32 v18, v9",
        "ds_write_b96 v5, v[16:18]", "s_waitcnt lgkmcnt(0)",
        "v_mov_b32_e32 v10, 0x13572468", "v_mov_b32_e32 v11, 0x24681357",
        "ds_wrxchg2_rtn_b32 v[10:11], v5, v10, v11 offset0:2", "s_waitcnt lgkmcnt(0)",
        "ds_read2_b32 v[12:13], v5 offset0:2", "s_waitcnt lgkmcnt(0)",
    ],
    "lds_read2_b32": [
        "v_mov_b32_e32 v16, v6", "v_mov_b32_e32 v17, v7", "v_mov_b32_e32 v18, v9",
        "ds_write_b96 v5, v[16:18]", "s_waitcnt lgkmcnt(0)",
        "ds_read2_b32 v[10:11], v5 offset0:2 offset1:1", "s_waitcnt lgkmcnt(0)",
        "v_mov_b32_e32 v12, v6",
    ],
}


def lds_wide_program(name):
    if name in LDS_WIDE_QUADS:
        ds = [line for op in LDS_WIDE_QUADS[name] for line in (op, "s_waitcnt lgkmcnt(0)")]
        return LDS_WIDE_SETUP + LDS_WIDE_QUAD_SETUP + ds + [
            "buffer_store_dwordx4 v[10:13], v8, s[20:23], 0 idxen", "s_endpgm",
        ]
    store = ("buffer_store_dwordx4 v[10:13]" if name == "lds_xchg2_b32"
             else "buffer_store_dwordx3 v[10:12]")
    return LDS_WIDE_SETUP + LDS_WIDE_PROGRAMS[name] + [
        store + ", v8, s[20:23], 0 idxen", "s_endpgm",
    ]


def lds_wide_reference(name, groups):
    words = []
    for lane in range(groups * 32):
        data = 0x12345670 + lane
        if name == "lds_xchg2_b32":
            words.extend((0x76543210, data, 0x13572468, 0x24681357))
            continue
        if name in LDS_WIDE_QUADS:
            words.extend({
                "lds_b128": (data, 0x89abcdef, 0x76543210, 0xf0e1d2c3),
                "lds_write2_b32": (data, 0x76543210, 0x76543210, data),
                "lds_pair_b64": (0x76543210, 0xf0e1d2c3, data, 0x89abcdef),
            }[name])
            continue
        words.extend((0x76543210, 0x89abcdef, data) if name == "lds_read2_b32"
                     else (data, 0x89abcdef, 0x76543210))
    return words


LDS_PACKED_CASES = [(name, lds_wide_program(name), LDS_WIDE_BINDING | {"stride": 16} if name == "lds_xchg2_b32"
                     else LDS_WIDE_BINDING) for name in LDS_WIDE_PROGRAMS]
LDS_PACKED_CASES += [(name, lds_wide_program(name), LDS_WIDE_BINDING | {"stride": 16}) for name in LDS_WIDE_QUADS]


LDS_HIGH_WRITES = {"ds_write_b8_d16_hi": (1, 1), "ds_write_b16_d16_hi": (2, 2)}


def lds_high_write_program(name):
    _, offset = LDS_HIGH_WRITES[name]
    return DS_PREFIX + [
        "v_lshlrev_b32_e32 v5, 2, v0",
        "v_mov_b32_e32 v6, 0x89abcdef",
        "ds_write_b32 v5, v6", "s_waitcnt lgkmcnt(0)",
        "v_mov_b32_e32 v7, 0x9180abcd", "v_mad_u32_u24 v7, 0x10000, v8, v7",
        f"{name} v5, v7 offset:{offset}", "s_waitcnt lgkmcnt(0)",
        "v_mov_b32_e32 v7, 0",
        "ds_read_b32 v10, v5", "s_waitcnt lgkmcnt(0)",
        "buffer_store_dword v10, v8, s[20:23], 0 idxen", "s_endpgm",
    ]


def lds_high_write_reference(name, groups):
    size, offset = LDS_HIGH_WRITES[name]
    words = []
    for lane in range(groups * 32):
        word = bytearray.fromhex("ef cd ab 89")
        source = (0x9180abcd + lane * 0x10000).to_bytes(4, "little")
        word[offset:offset + size] = source[2:2 + size]
        words.append(int.from_bytes(word, "little"))
    return words


LDS_ADDTID_PROGRAM = DS_PREFIX + [
    "s_mov_b32 m0, 0",
    "v_lshlrev_b32_e32 v5, 2, v0",
    "v_mad_u32_u24 v6, 1, v8, 0x12345670",
    "ds_write_addtid_b32 v6", "s_waitcnt lgkmcnt(0)",
    "ds_read_b32 v10, v5", "s_waitcnt lgkmcnt(0)",
    "v_mad_u32_u24 v6, 1, v8, 0x89abcdef",
    "ds_write_b32 v5, v6", "s_waitcnt lgkmcnt(0)",
    "ds_read_addtid_b32 v11", "s_waitcnt lgkmcnt(0)",
    "buffer_store_dwordx2 v[10:11], v8, s[20:23], 0 idxen", "s_endpgm",
]
LDS_PACKED_CASES += [(op, lds_high_write_program(op), LDS_BINDING | {"stride": 4}) for op in LDS_HIGH_WRITES]
LDS_PACKED_CASES += [("lds_addtid", LDS_ADDTID_PROGRAM, LDS_BINDING | {"stride": 8})]


LDS_WIDE_REPLACEMENTS = ("ds_cmpst_b64", "ds_cmpst_rtn_b64", "ds_mskor_b64", "ds_mskor_rtn_b64", "ds_wrxchg_rtn_b64")


def lds_wide_replacement_instruction(op):
    returns = "_rtn" in op
    dest = "v[20:21], " if returns else ""
    data2 = "" if "wrxchg" in op else ", v[20:21]"
    return f"{op} {dest}v5, v[18:19]{data2} offset:8"


def lds_wide_replacement_program(op):
    returns = "_rtn" in op
    return LDS_WIDE_SETUP + [
        "v_and_b32_e32 v16, 1, v8", "v_lshrrev_b32_e32 v17, 1, v8", "v_and_b32_e32 v17, 1, v17",
        "v_mad_u32_u24 v16, 1, v16, 0x12345670",
        "v_mad_u32_u24 v17, 1, v17, 0x9abcdef0",
        "v_mov_b32_e32 v18, 0x12345670", "v_mov_b32_e32 v19, 0x9abcdef0",
        "v_mov_b32_e32 v20, 0xf0e1d2c3", "v_mov_b32_e32 v21, 0x87654321",
        "ds_write_b64 v5, v[16:17] offset:8", "s_waitcnt lgkmcnt(0)",
        lds_wide_replacement_instruction(op), "s_waitcnt lgkmcnt(0)",
        "ds_read_b64 v[10:11], v5 offset:8", "s_waitcnt lgkmcnt(0)",
    ] + (["v_mov_b32_e32 v12, v20", "v_mov_b32_e32 v13, v21",
          "buffer_store_dwordx4 v[10:13], v8, s[20:23], 0 idxen"] if returns else
         ["buffer_store_dwordx2 v[10:11], v8, s[20:23], 0 idxen"]) + ["s_endpgm"]


def lds_wide_replacement_reference(op, groups):
    words = []
    for lane in range(groups * 32):
        old = (0x9abcdef0 + (lane >> 1 & 1)) << 32 | (0x12345670 + (lane & 1))
        data = 0x9abcdef012345670
        other = 0x87654321f0e1d2c3
        if "cmpst" in op:
            new = other if old == data else old
        elif "mskor" in op:
            new = (old & ~data) | other
        else:
            new = data
        words.extend((new & 0xffffffff, new >> 32))
        if "_rtn" in op:
            words.extend((old & 0xffffffff, old >> 32))
    return words


LDS_WIDE_REPLACEMENT_CASES = [
    (op, lds_wide_replacement_program(op), LDS_WIDE_BINDING | {"stride": 16 if "_rtn" in op else 8})
    for op in LDS_WIDE_REPLACEMENTS
]

LDS_QWORD_BINDING = LDS_WIDE_BINDING | {"stride": 8}
LDS_QWORD_CASES = [(op, lds_qword_program(op), LDS_QWORD_BINDING) for op in LDS_QWORD_UPDATES]
LDS_QWORD_RETURN_BINDING = LDS_WIDE_BINDING | {"stride": 16}
LDS_QWORD_CASES += [(op, lds_qword_program(op), LDS_QWORD_RETURN_BINDING) for op in LDS_QWORD_RETURNS]
LDS_QWORD_CASES += [(op, lds_qword_counter_program(op),
                     LDS_QWORD_RETURN_BINDING if "_rtn" in op else LDS_QWORD_BINDING) for op in LDS_QWORD_COUNTERS]

LDS_QWORD_BORROW_PROGRAM = LDS_WIDE_SETUP + [
    "v_mov_b32_e32 v16, 0", "v_mov_b32_e32 v17, 0x40000001",
    "ds_write_b64 v5, v[16:17] offset:8", "s_waitcnt lgkmcnt(0)",
    "v_mov_b32_e32 v18, 0", "v_mov_b32_e32 v19, 0x80000000",
    "ds_dec_u64 v5, v[18:19] offset:8", "s_waitcnt lgkmcnt(0)",
    "ds_read_b64 v[10:11], v5 offset:8", "s_waitcnt lgkmcnt(0)",
    "buffer_store_dwordx2 v[10:11], v8, s[20:23], 0 idxen", "s_endpgm",
]
LDS_QWORD_CASES += [("lds_dec_borrow", LDS_QWORD_BORROW_PROGRAM, LDS_QWORD_BINDING)]


def lds_qword_borrow_reference(groups):
    # 0x40000001_00000000 - 1 under a larger limit: the zero low word borrows.
    return [word for _ in range(groups * 32) for word in (0xffffffff, 0x40000000)]


LDS_F64_CMPST = ("ds_cmpst_f64", "ds_cmpst_rtn_f64")


def lds_f64_cmpst(op):
    returns = "_rtn" in op
    dest = "v[18:19], " if returns else ""
    return LDS_WIDE_SETUP + [
        "v_and_b32_e32 v16, 1, v8", "v_lshrrev_b32_e32 v17, 1, v8", "v_and_b32_e32 v17, 1, v17",
        "v_mad_u32_u24 v17, 1, v17, 0x40040000",
        "v_mov_b32_e32 v18, 0x12345678", "v_mov_b32_e32 v19, 0xbff80000",
        "v_mov_b32_e32 v20, 0", "v_mov_b32_e32 v21, 0x40040000",
        "ds_write_b64 v5, v[16:17] offset:8", "s_waitcnt lgkmcnt(0)",
        f"{op} {dest}v5, v[20:21], v[18:19] offset:8", "s_waitcnt lgkmcnt(0)",
        "ds_read_b64 v[10:11], v5 offset:8", "s_waitcnt lgkmcnt(0)",
    ] + (["v_mov_b32_e32 v12, v18", "v_mov_b32_e32 v13, v19",
          "buffer_store_dwordx4 v[10:13], v8, s[20:23], 0 idxen"] if returns else
         ["buffer_store_dwordx2 v[10:11], v8, s[20:23], 0 idxen"]) + ["s_endpgm"]


def lds_f64_reference(op, groups):
    words = []
    for lane in range(groups * 32):
        previous = (lane & 1, 0x40040000 + (lane >> 1 & 1))
        value = struct.unpack("<d", struct.pack("<II", *previous))[0]
        words.extend((0x12345678, 0xbff80000) if value == 2.5 else previous)
        if "_rtn" in op:
            words.extend(previous)
    return words


LDS_F64_CASES = [(op, lds_f64_cmpst(op), LDS_WIDE_BINDING | {"stride": 16 if "_rtn" in op else 8,
                                                          "lds_f64_mode": "normal"}) for op in LDS_F64_CMPST]
LDS_F64_ORDER = ("ds_min_f64", "ds_max_f64", "ds_min_rtn_f64", "ds_max_rtn_f64")
LDS_QWORD_PAIR_CASES = ("lds_qword_exchange_old", "lds_qword_exchange_new")


def lds_f64_order(op):
    returns = "_rtn" in op
    dest = "v[18:19], " if returns else ""
    return LDS_WIDE_SETUP + [
        "v_and_b32_e32 v16, 1, v8", "v_lshrrev_b32_e32 v17, 1, v8", "v_and_b32_e32 v17, 1, v17",
        "v_mov_b32_e32 v18, 0x40040000", "v_mad_u32_u24 v17, 0x800000, v17, 0",
        "v_mad_u32_u24 v17, 256, v17, v18",
        "v_mov_b32_e32 v18, 0", "v_mov_b32_e32 v19, 0xc0040000",
        "ds_write_b64 v5, v[16:17] offset:8", "s_waitcnt lgkmcnt(0)",
        f"{op} {dest}v5, v[18:19] offset:8", "s_waitcnt lgkmcnt(0)",
        "ds_read_b64 v[10:11], v5 offset:8", "s_waitcnt lgkmcnt(0)",
    ] + (["v_mov_b32_e32 v12, v18", "v_mov_b32_e32 v13, v19",
          "buffer_store_dwordx4 v[10:13], v8, s[20:23], 0 idxen"] if returns else
         ["buffer_store_dwordx2 v[10:11], v8, s[20:23], 0 idxen"]) + ["s_endpgm"]


def lds_qword_exchange(name):
    return LDS_WIDE_SETUP + [
        "v_mov_b32_e32 v16, 0x11111111", "v_mov_b32_e32 v17, 0x22222222",
        "v_mov_b32_e32 v18, 0x33333333", "v_mov_b32_e32 v19, 0x44444444",
        "ds_write_b64 v5, v[16:17]", "s_waitcnt lgkmcnt(0)",
        "ds_write_b64 v5, v[18:19] offset:8", "s_waitcnt lgkmcnt(0)",
        "v_mov_b32_e32 v10, v6", "v_mov_b32_e32 v11, 0xabcdefed",
        "v_mov_b32_e32 v12, 0x76543210", "v_mov_b32_e32 v13, 0x87654321",
        "ds_wrxchg2_rtn_b64 v[10:13], v5, v[10:11], v[12:13] offset0:1", "s_waitcnt lgkmcnt(0)",
    ] + (["ds_read_b64 v[10:11], v5", "s_waitcnt lgkmcnt(0)",
          "ds_read_b64 v[12:13], v5 offset:8", "s_waitcnt lgkmcnt(0)"] if name.endswith("new") else []) + [
        "buffer_store_dwordx4 v[10:13], v8, s[20:23], 0 idxen", "s_endpgm",
    ]


def lds_f64_order_reference(name, groups):
    words = []
    for lane in range(groups * 32):
        prior = (lane & 1, 0x40040000 | ((lane >> 1 & 1) << 31))
        other = (0, 0xc0040000)
        def as_double(pair):
            return struct.unpack("<d", struct.pack("<II", *pair))[0]
        words.extend((min if name.startswith("ds_min") else max)((prior, other), key=as_double))
        if "_rtn" in name:
            words.extend(prior)
    return words


LDS_FLOAT_CASES = LDS_F32_CASES + LDS_F64_CASES + [
    (op, lds_f64_order(op), LDS_WIDE_BINDING | {"stride": 16 if "_rtn" in op else 8, "lds_f64_mode": "normal"})
    for op in LDS_F64_ORDER
] + [(name, lds_qword_exchange(name), LDS_WIDE_BINDING | {"stride": 16}) for name in LDS_QWORD_PAIR_CASES]


LDS_WAVE_COUNTER = DS_PREFIX + [
    "s_mov_b32 m0, 0", "v_lshlrev_b32_e32 v5, 2, v0",
    "v_mad_u32_u24 v6, 1, v8, 0xfffffff0", "ds_write_b32 v5, v6", "s_waitcnt lgkmcnt(0)",
    "ds_append v10", "s_waitcnt lgkmcnt(0)",
    "ds_read_b32 v11, v5", "s_waitcnt lgkmcnt(0)",
    "ds_consume v12", "s_waitcnt lgkmcnt(0)",
    "ds_read_b32 v13, v5", "s_waitcnt lgkmcnt(0)",
    "buffer_store_dwordx4 v[10:13], v8, s[20:23], 0 idxen", "s_endpgm",
]
LDS_CASES += [("lds_wave_counter", LDS_WAVE_COUNTER, LDS_BINDING | {"stride": 16})]


def lds_wave_counter_reference(groups):
    words = []
    for lane in range(groups * 32):
        first = lane // 32 * 32
        seed = (0xfffffff0 + first) & 0xffffffff
        incremented = (seed + 32) & 0xffffffff
        private = (0xfffffff0 + lane) & 0xffffffff
        words.extend((seed, incremented if lane == first else private, incremented, private))
    return words


def lds_controls():
    op, lines, binding = LDS_WIDE_REPLACEMENT_CASES[1]
    target = lds_wide_replacement_instruction(op)
    for replacement, message in (
        (target.replace("offset:8", "offset:0"), "Unverified private wide LDS replacement"),
        (target.replace("v[18:19]", "v[18:20]"), "Unverified private wide LDS replacement range"),
        (target.replace("v[18:19]", "v[254:255]"), "Unverified private wide LDS replacement source"),
        (target + " gds", "Unsupported compute instruction"),
    ):
        rejects(message, aot.lower, variant(lines, (target, replacement)), 32, **binding)


    pair = lds_qword_exchange("lds_qword_exchange_old")
    rejects("Unverified private paired qword exchange", aot.lower,
            variant(pair, ("offset0:1", "offset0:0")), 32, **(LDS_WIDE_BINDING | {"stride": 16}))
    lines = lds_f64_cmpst("ds_cmpst_f64")
    rejects("Unverified private LDS binary64 mode", aot.lower, lines, 32, **(LDS_WIDE_BINDING | {"stride": 8}))
    lines = lds_f32_program("ds_add_f32")
    rejects("Unverified private LDS floating-point mode", aot.lower, lines, 32, **LDS_BINDING)
    for bad, message in (
        ("ds_add_f32 v5, v7 gds", "Unsupported compute instruction"),
        ("ds_add_f32 v5, v7 offset:4", "Unsupported compute instruction"),
        ("ds_add_f32 v5, v255", "Unverified private LDS floating-point access"),
        ("ds_add_f32 v5, v7, v8", "Unverified private LDS floating-point operands"),
    ):
        rejects(message, aot.lower, variant(lines, ("ds_add_f32 v5, v7", bad)), 32, **LDS_F32_BINDING)

    cmpst = lds_replacement_instruction("ds_cmpst_rtn_b32")
    lines = lds_replacement_program("ds_cmpst_rtn_b32")
    for bad, message in (
        (cmpst + " offset:4", "Unsupported compute instruction"),
        (cmpst + " gds", "Unsupported compute instruction"),
        (cmpst.replace(", v7", ""), "Unverified private LDS replacement operands"),
        (cmpst.replace("v7", "v254"), "Unverified private LDS replacement"),
    ):
        rejects(message, aot.lower, variant(lines, (cmpst, bad)), 32, **LDS_BINDING)
    for old, new, message in (
        ("s_mov_b32 m0, 0", "s_nop 0", "Unverified private LDS thread-ID access"),
        ("s_mov_b32 m0, 0", "s_mov_b32 m0, 4", "Unsupported compute instruction"),
        ("ds_write_addtid_b32 v6", "ds_write_addtid_b32 v6 offset:4", "Unsupported compute instruction"),
        ("ds_write_addtid_b32 v6", "ds_write_addtid_b32 v255", "Unverified private LDS thread-ID source"),
        ("ds_write_addtid_b32 v6", "ds_read_addtid_b32 v11", "Uninitialized private LDS thread-ID word"),
        ("ds_read_addtid_b32 v11", "ds_read_addtid_b32 v11\ns_nop 0",
         "Expected immediate s_waitcnt lgkmcnt(0) after DS"),
    ):
        rejects(message, aot.lower, variant(LDS_ADDTID_PROGRAM, (old, new)), 32, **LDS_BINDING)
    high = lds_high_write_program("ds_write_b16_d16_hi")
    rejects("Unverified private LDS write", aot.lower,
            variant(high, ("offset:2", "offset:1")), 32, **LDS_BINDING)
    rejects("Unverified private LDS write", aot.lower,
            variant(high, ("ds_write_b32 v5, v6", "s_nop 0")), 32, **LDS_BINDING)

    assert lds_qword_borrow_reference(1)[:2] == [0xffffffff, 0x40000000]
    aot.lower(LDS_QWORD_BORROW_PROGRAM, 32, **LDS_QWORD_BINDING)
    ret = lds_qword_program("ds_add_rtn_u64")
    instruction = "ds_add_rtn_u64 v[18:19], v5, v[18:19] offset:8"
    for bad in (instruction.replace("v[18:19], v5", "v[18:20], v5"),
                instruction.replace("v[18:19], v5", "v[255:256], v5")):
        rejects("Unverified private wide LDS update", aot.lower, variant(ret, (instruction, bad)), 32,
                **LDS_QWORD_RETURN_BINDING)

    quad = lds_qword_program("ds_add_u64")
    op = "ds_add_u64 v5, v[18:19] offset:8"
    for bad, message in (
        ("ds_add_u64 v5, v[18:19]", "Unverified private wide LDS update"),
        ("ds_add_u64 v5, v[18:20] offset:8", "Unverified private wide LDS update"),
        ("ds_add_u64 v5, v[254:255] offset:8", "Unverified private wide LDS update"),
        (op + " gds", "Unsupported compute instruction"),
    ):
        rejects(message, aot.lower, variant(quad, (op, bad)), 32, **LDS_QWORD_BINDING)

    for name in ("ds_xor_b64", "ds_min_u64", "ds_max_u64", "ds_min_i64", "ds_max_i64"):
        lines = lds_qword_program(name)
        update = f"{name} v5, v[18:19] offset:8"
        for bad, message in (
            (f"{name} v5, v[18:19]", "Unverified private wide LDS update"),
            (update + " gds", "Unsupported compute instruction"),
        ):
            rejects(message, aot.lower, variant(lines, (update, bad)), 32, **LDS_QWORD_BINDING)

    pair = lds_wide_program("lds_pair_b64")
    for before, after, message in (
        ("offset0:1", "offset0:0", "Unverified private paired LDS access"),
        ("offset1:1", "offset1:2", "Unverified private paired LDS access"),
        ("v[18:19] offset0:1", "v[18:20] offset0:1", "Unverified private paired LDS register range"),
        ("ds_write2_b64 v5, v[16:17], v[18:19] offset0:1", "ds_write_b64 v5, v[16:17]",
         "Uninitialized private wide LDS word"),
        ("offset1:1", "offset1:1 gds", "Unsupported compute instruction"),
    ):
        rejects(message, aot.lower, variant(pair, (before, after)), 32, **(LDS_WIDE_BINDING | {"stride": 16}))

    name, lines, binding = LDS_STRIDED_CASES[1]
    assert name == "lds_st64_b64"
    for before, after, message in (
        ("offset0:1 offset1:2", "offset0:1 offset1:1", "Unverified private strided LDS access"),
        ("offset0:1 offset1:2", "offset0:0 offset1:2", "Unverified private strided LDS access"),
        ("offset0:1 offset1:2", "offset0:1 offset1:3", "Unverified private strided LDS access"),
        ("v[10:11] offset0:1", "v[10:12] offset0:1", "Unverified private strided LDS source range"),
        ("v[10:13], v5", "v[10:12], v5", "Unverified private strided LDS destination range"),
        ("ds_write2st64_b64 v5, v[6:7], v[10:11] offset0:1 offset1:2", "s_nop 0",
         "Uninitialized private strided LDS plane"),
        ("offset0:2 offset1:1", "offset0:2 offset1:1 gds", "Unsupported compute instruction"),
    ):
        rejects(message, aot.lower, variant(lines, (before, after)), 32, **binding)
    rejects("Unverified private strided LDS access", aot.lower, lines, 32, **(binding | {"lds_bytes": 1279}))
    for old, bad, message in (
        ("s_mov_b32 m0, 0", "s_nop 0", "Unverified LDS wave counter"),
        ("s_mov_b32 m0, 0", "s_mov_b32 m0, 4", "Unsupported compute instruction"),
        ("ds_write_b32 v5, v6", "s_nop 0", "Unverified LDS wave counter"),
        ("ds_append v10", "ds_append v10 gds", "Unsupported compute instruction"),
        ("ds_consume v12", "ds_consume v12 offset:4", "Unsupported compute instruction"),
        ("ds_append v10", "ds_append v10\ns_nop 0", "Expected immediate s_waitcnt lgkmcnt(0) after DS"),
    ):
        rejects(message, aot.lower, variant(LDS_WAVE_COUNTER, (old, bad)), 32, **LDS_BINDING)
    lines = lds_wide_program("lds_xchg2_b32")
    rejects("Unverified private paired dword exchange", aot.lower,
            variant(lines, ("v10, v11 offset0:2", "v10, v11")), 32, **LDS_WIDE_BINDING)
    rejects("Uninitialized private paired dword exchange", aot.lower,
            variant(lines, ("ds_wrxchg2_rtn_b32 v[10:11], v5, v10, v11 offset0:2",
                            "ds_wrxchg2_rtn_b32 v[10:11], v5, v10, v11 offset0:3")), 32, **LDS_WIDE_BINDING)
    wide = lds_wide_program("lds_b96")
    rejects("Unverified dispatch byte offset", aot.lower, wide, 32, **LDS_BINDING)
    for old, bad, message in (
        ("ds_write_b96 v5, v[16:18]", "s_nop 0", "Uninitialized private wide LDS word"),
        ("ds_write_b96 v5, v[16:18]", "ds_write_b96 v5, v[16:17]", "Unverified private wide LDS register range"),
        ("ds_write_b96 v5, v[16:18]", "ds_write_b96 v5, v[16:18] offset:8", "Unverified private wide LDS access"),
        ("ds_read_b96 v[10:12], v5", "ds_read_b96 v[10:12], v5 gds", "Unsupported compute instruction"),
        ("ds_read_b96 v[10:12], v5", "ds_read_b96 v[10:12], v5\nv_mov_b32_e32 v6, 0",
         "Expected immediate s_waitcnt lgkmcnt(0) after DS"),
    ):
        rejects(message, aot.lower, variant(wide, (old, bad)), 32, **LDS_WIDE_BINDING)
    rejects("Mixed private LDS address strides", aot.lower,
            variant(wide, ("v_lshlrev_b32_e32 v5, 4, v0", "v_lshlrev_b32_e32 v3, 2, v0\n"
                            "v_lshlrev_b32_e32 v5, 4, v0")), 32, **LDS_WIDE_BINDING)

    wide_address = "v_lshlrev_b32_e32 v5, 4, v0"
    implicit_write = "\n".join(("s_mov_b32 m0, 0", "v_mov_b32_e32 v15, 0x12345678",
                                "ds_write_addtid_b32 v15", "s_waitcnt lgkmcnt(0)"))
    for mixed in (implicit_write + "\n" + wide_address, wide_address + "\n" + implicit_write):
        rejects("Mixed private LDS address strides", aot.lower,
                variant(wide, (wide_address, mixed)), 32, **LDS_WIDE_BINDING)

    lines = LDS_PROGRAM
    assert " >> 16u) & 0xffffu)" in aot.lower(lines, 32, **LDS_BINDING)
    for old, new, message in (
        ("ds_write_b32 v5, v6", "s_nop 0", "Unverified private LDS read"),
        ("ds_write_b32 v5, v6", "ds_write_b32 v8, v6", "Unverified private LDS write"),
        ("ds_read_b32 v10, v5", "ds_read_b32 v10, v5 offset:2", "Unverified private LDS read"),
        ("ds_read_u16 v12, v5 offset:2", "ds_read_u16 v12, v5 offset:1", "Unverified private LDS read"),
    ):
        rejects(message, aot.lower, variant(lines, (old, new)), 32, **LDS_BINDING)
    i = lines.index("ds_write_b32 v5, v6")
    masked = lines[:i] + ["v_cmp_gt_u32_e32 vcc_lo, 16, v8", "s_and_saveexec_b32 s5, vcc_lo"] + lines[i:]
    rejects("Unverified private LDS write", aot.lower, masked, 32, **LDS_BINDING)
    rejects("Expected immediate s_waitcnt lgkmcnt(0) after DS", aot.lower,
            lines[:i + 1] + lines[i + 2:], 32, **LDS_BINDING)
    rejects("Unverified dispatch byte offset", aot.lower, lines, 32, **(LDS_BINDING | {"lds_bytes": 127}))


    lines = lds_subword_program(16, 2)
    write = "ds_write_b16 v5, v6 offset:2"
    read = "ds_read_i16 v12, v5 offset:2"
    for old, new, message in (
        ("ds_write_b32 v5, v6", "s_nop 0", "Unverified private LDS write"),
        (write, "ds_write_b16 v5, v6 offset:1", "Unverified private LDS write"),
        (write, "ds_write_b16 v5, v6 offset:3", "Unverified private LDS write"),
        (write, "ds_write_b16 v5, v6 offset:2 gds", "Unsupported compute instruction"),
        (write, "ds_write_b8 v5, v6 offset:4", "Unsupported compute instruction"),
        (read, "ds_read_i16 v12, v5 offset:1", "Unverified private LDS read"),
        (read, "ds_read_i16 v12, v8 offset:2", "Unverified private LDS read"),
        (read, "ds_read_i8 v12, v5 offset:4", "Unsupported compute instruction"),
        (read, "ds_read_u8 v12, v5 offset:2 gds", "Unsupported compute instruction"),
    ):
        # Only omit the first full-word write in the initialization control.
        mutated = lines.copy()
        mutated[mutated.index(old)] = new
        rejects(message, aot.lower, mutated, 32, **LDS_BINDING)
    i = lines.index(write)
    rejects("Expected immediate s_waitcnt lgkmcnt(0) after DS", aot.lower,
            lines[:i + 1] + lines[i + 2:], 32, **LDS_BINDING)
    pair = ["ds_write_b8 v5, v6", "s_waitcnt lgkmcnt(0)"]
    rejects("LDS expression exceeds the profile limit", aot.lower,
            lines[:i] + pair * 32 + lines[i:], 32, **LDS_BINDING)


    update = "ds_add_u32 v5, v6"
    lines = lds_update_program("ds_add_u32")
    no_init = variant(lines, ("ds_write_b32 v5, v6", "s_nop 0"), ("ds_read_b32 v10, v5", "s_nop 0"))
    rejects("Unverified private LDS update", aot.lower, no_init, 32, **LDS_BINDING)
    for bad, message in (
        ("ds_add_u32 v8, v6", "Unverified private LDS update"),
        ("ds_add_u32 v5, v254", "Unverified private LDS update"),
        ("ds_add_u32 v5, v6 offset:4", "Unsupported compute instruction"),
        ("ds_add_u32 v5, v6 gds", "Unsupported compute instruction"),
        ("ds_inc_rtn_u64 v[10:11], v5, v[6:7] offset:4", "Unsupported compute instruction"),
    ):
        rejects(message, aot.lower, variant(lines, (update, bad)), 32, **LDS_BINDING)
    i = lines.index(update)
    masked = lines[:i] + ["v_cmp_gt_u32_e32 vcc_lo, 16, v8", "s_and_saveexec_b32 s5, vcc_lo"] + lines[i:]
    rejects("Unverified private LDS update", aot.lower, masked, 32, **LDS_BINDING)
    rejects("Expected immediate s_waitcnt lgkmcnt(0) after DS", aot.lower,
            lines[:i + 1] + lines[i + 2:], 32, **LDS_BINDING)
    pair = [update, "s_waitcnt lgkmcnt(0)"]
    rejects("LDS expression exceeds the profile limit", aot.lower,
            lines[:i] + pair * 32 + lines[i:], 32, **LDS_BINDING)


    wrap = lds_counter_instruction("ds_wrap_rtn_b32")
    lines = lds_counter_program("ds_wrap_rtn_b32")
    for bad, message in (
        (wrap + " offset:4", "Unsupported compute instruction"),
        (wrap + " gds", "Unsupported compute instruction"),
        (wrap.replace(", v7", ""), "Unverified LDS counter operands"),
        (wrap.replace("v7", "v254"), "Unverified private LDS counter update"),
        (wrap.replace("v5", "v8"), "Unverified private LDS counter update"),
    ):
        rejects(message, aot.lower, variant(lines, (wrap, bad)), 32, **LDS_BINDING)
    i = lines.index(wrap)
    rejects("Expected immediate s_waitcnt lgkmcnt(0) after DS", aot.lower,
            lines[:i + 1] + lines[i + 2:], 32, **LDS_BINDING)


    update = "ds_add_rtn_u32 v10, v5, v10"
    lines = lds_return_update_program("ds_add_rtn_u32")
    expected = aot.lower(lines, 32, **LDS_BINDING)
    assert aot.lower(variant(lines, (update, update + " offset:0")), 32, **LDS_BINDING) == expected
    no_init = variant(lines, ("ds_write_b32 v5, v6", "s_nop 0"))
    rejects("Unverified private LDS update", aot.lower, no_init, 32, **LDS_BINDING)
    for bad, message in (
        ("ds_add_rtn_u32 v256, v5, v10", "Unverified private LDS update"),
        ("ds_add_rtn_u32 v10, v8, v10", "Unverified private LDS update"),
        ("ds_add_rtn_u32 v10, v5, v254", "Unverified private LDS update"),
        ("ds_add_rtn_u32 v10, v5, v10 offset:4", "Unsupported compute instruction"),
        ("ds_add_rtn_u32 v10, v5, v10 gds", "Unsupported compute instruction"),
        ("ds_add_rtn_u32 v5, v10", "Unsupported compute instruction"),
        ("ds_add_u32 v10, v5, v10", "Unsupported compute instruction"),
    ):
        rejects(message, aot.lower, variant(lines, (update, bad)), 32, **LDS_BINDING)
    # The result in v11 replaces its address tag.
    rejects("Unverified private LDS read", aot.lower,
            variant(lines, ("ds_read_b32 v12, v5", "ds_read_b32 v12, v11")), 32, **LDS_BINDING)
    i = lines.index(update)
    masked = lines[:i] + ["v_cmp_gt_u32_e32 vcc_lo, 16, v8", "s_and_saveexec_b32 s5, vcc_lo"] + lines[i:]
    rejects("Unverified private LDS update", aot.lower, masked, 32, **LDS_BINDING)
    rejects("Expected immediate s_waitcnt lgkmcnt(0) after DS", aot.lower,
            lines[:i + 1] + lines[i + 2:], 32, **LDS_BINDING)


    for ret, op in LDS_RETURN_UPDATES.items():
        if op not in LDS_ORDER_MUTANTS:
            continue
        for lines, update in ((lds_update_program(op), f"{op} v5, v6"),
                              (lds_return_update_program(ret), f"{ret} v10, v5, v10")):
            for modifier in ("offset:4", "gds"):
                rejects("Unsupported compute instruction", aot.lower,
                        variant(lines, (update, f"{update} {modifier}")), 32, **LDS_BINDING)


    lines = lds_d16_program("ds_read_u16_d16_hi")
    read = "ds_read_u16_d16_hi v10, v5"
    for bad, message in (
        (read.replace("v10", "v254"), "Unverified D16 destination word"),
        (read.replace("v10", "v5"), "Unverified D16 destination word"),
        (read + " offset:1", "Unverified private LDS read"),
        (read + " offset:4", "Unsupported compute instruction"),
        (read + " gds", "Unsupported compute instruction"),
        (read.replace("u16", "b32"), "Unsupported compute instruction"),
        (read.replace("u16", "i16"), "Unsupported compute instruction"),
    ):
        rejects(message, aot.lower, variant(lines, (read, bad)), 32, **LDS_BINDING)
    i = lines.index(read)
    rejects("Expected immediate s_waitcnt lgkmcnt(0) after DS", aot.lower,
            lines[:i + 1] + lines[i + 2:], 32, **LDS_BINDING)


def gfx1030_binary(directory, assembler, objcopy, library, cases, prefix, group=32):
    dims = aot.checked_group(group)
    for name, lines, binding in cases:
        src, obj, raw = (directory / f"{name}.{ext}" for ext in ("s", "o", "bin"))
        src.write_text("\n".join(lines) + "\n", encoding="ascii")
        subprocess.run([assembler, "-triple=amdgcn--amdpal", "-mcpu=gfx1030", f"-mattr={DS_FEATURES}",
                        "-filetype=obj", src, "-o", obj], check=True)
        subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, raw], check=True)
        code = raw.read_bytes()
        decoded = aot.labelled(disassemble(code, library, offsets=True, cpu=b"gfx1030",
                                          features=DS_FEATURES.encode()))
        assert [line for line in decoded if line.startswith(prefix)] == [
            line for line in lines if line.startswith(prefix)]
        expected = aot.lower(lines, dims, **binding)
        assert aot.lower(decoded, dims, **binding) == expected
        container, output = directory / f"{name}.dxbc", directory / f"{name}-cli.hlsl"
        container.write_bytes(store_container(code, dims))
        options = [item for key, value in binding.items() for item in ("--" + key.replace("_", "-"), str(value))]
        subprocess.run([sys.executable, "-B", aot.__file__, container, "--output", output,
                        "--llvm", library, *options], check=True)
        assert output.read_text() == expected


def ds_binary(directory, assembler, objcopy, library):
    cases = DS_CASES + LDS_CASES + LDS_ARITHMETIC_CASES + LDS_REPLACEMENT_CASES + LDS_PACKED_CASES + LDS_STRIDED_CASES
    cases += LDS_WIDE_REPLACEMENT_CASES
    cases += LDS_QWORD_CASES
    cases += LDS_FLOAT_CASES
    gfx1030_binary(directory, assembler, objcopy, library, cases, "ds_")


def ds_mutants(name, lines, binding, source):
    if name in LDS_ORDER_MUTANTS:
        changed = variant(lines, (f"{name} v5, v6", f"{LDS_ORDER_MUTANTS[name]} v5, v6"))
        yield aot.lower(changed, 32, **binding), 1
    elif name in LDS_UPDATES:
        changed = variant(lines, (f"{name} v5, v6", "ds_write_b32 v5, v6"))
        yield aot.lower(changed, 32, **binding), 1
    elif name in LDS_RETURN_UPDATES:
        op = LDS_RETURN_UPDATES[name]
        return_new = f"{op} v5, v10\ns_waitcnt lgkmcnt(0)\nds_read_b32 v10, v5"
        changed = variant(lines, (f"{name} v10, v5, v10", return_new))
        words = lds_update_reference(op, 3)
        # At some lanes a min/max update keeps the old value. Find the first changed lane.
        first = next(i for i in range(0, len(words), 3) if words[i] != words[i + 1])
        yield aot.lower(changed, 32, **binding), first
        changed = variant(lines, (f"{name} v11, v11, v9", "ds_read_b32 v11, v11"))
        second = next(i for i in range(2, len(words), 3) if words[i - 1] != words[i])
        yield aot.lower(changed, 32, **binding), second
        if op in LDS_ORDER_MUTANTS:
            wrong_op = next(ret for ret, base in LDS_RETURN_UPDATES.items() if base == LDS_ORDER_MUTANTS[op])
            changed = variant(lines, (f"{name} v10, v5, v10", f"{wrong_op} v10, v5, v10"))
            yield aot.lower(changed, 32, **binding), 1
    elif name in LDS_REPLACEMENTS:
        instruction = lds_replacement_instruction(name)
        if "cmpst" in name:
            wrong = instruction.replace("v6, v7", "v7, v6")
        elif "mskor" in name:
            # Masking DATA1 would incorrectly discard bits outside the mask.
            masked = 0x400580b1 & 0x00ff0ff0
            changed = variant(lines, ("v_mov_b32_e32 v7, 0x400580b1", f"v_mov_b32_e32 v7, 0x{masked:08x}"))
            yield aot.lower(changed, 32, **binding), 1
            return
        else:
            wrong = "ds_read_b32 v10, v5"
        yield aot.lower(variant(lines, (instruction, wrong)), 32, **binding), 1
    elif name in LDS_SUBWORDS:
        width, offset = LDS_SUBWORDS[name]
        suffix = f" offset:{offset}" if offset else ""
        changed = variant(lines, (f"ds_write_b{width} v5, v6{suffix}", "ds_write_b32 v5, v6"))
        yield aot.lower(changed, 32, **binding), 0
        changed = variant(lines, (f"ds_read_i{width} v12, v5{suffix}", f"ds_read_u{width} v12, v5{suffix}"))
        # Lane 16 is the first to set the sign bit. The signed read is its third word.
        yield aot.lower(changed, 32, **binding), 16 * 3 + 2
    elif name in LDS_D16_READS:
        changed = variant(lines, (f"v_mov_b32_e32 v10, 0x{LDS_D16_DESTINATIONS[0]:08x}", "v_mov_b32_e32 v10, 0"))
        yield aot.lower(changed, 32, **binding), 0
        if name in ("ds_read_i8_d16", "ds_read_i8_d16_hi"):
            changed = variant(lines, (f"{name} v10, v5", f"{name.replace('i8', 'u8')} v10, v5"))
            yield aot.lower(changed, 32, **binding), 0
    elif name == "lds_wave_counter":
        yield aot.lower(variant(lines, ("ds_append v10", "ds_consume v10")), 32, **binding), 1
        yield aot.lower(variant(lines, ("ds_consume v12", "ds_append v12")), 32, **binding), 3
    elif name in LDS_QWORD_COUNTERS:
        if "condxchg" in name:
            # Incorrectly perform the high-half store when its sign bit is clear.
            wrong = source.replace("(0x12345678u & 0x80000000u) != 0u", "true")
            yield wrong, 1
        else:
            # Returning forms use the read to keep the old pair observable.
            no_update = ("ds_read_b64 v[18:19], v5 offset:8" if "_rtn" in name else "s_nop 0")
            instruction = next(line for line in lines if line.startswith(name + " "))
            yield aot.lower(variant(lines, (instruction, no_update)), 32, **binding), 0
    elif name in LDS_QWORD_RETURNS:
        # Substitute the post-update word pair for the returned pre-update pair.
        changed = variant(lines, ("v_mov_b32_e32 v12, v18", "v_mov_b32_e32 v12, v10"),
                          ("v_mov_b32_e32 v13, v19", "v_mov_b32_e32 v13, v11"))
        ref = lds_qword_reference(name, 2)
        word = next(i for i in range(len(ref)) if i % 4 >= 2 and ref[i] != ref[i - 2])
        yield aot.lower(changed, 32, **binding), word
    elif name in LDS_QWORD_UPDATES:
        if name == "ds_add_u64":
            wrong = source.replace(" + uint(", " + 0u * uint(")
            word = 1
            yield wrong, word
        elif name in ("ds_sub_u64", "ds_rsub_u64"):
            wrong = source.replace(" - uint(", " - 0u * uint(")
            word = 33 if name == "ds_sub_u64" else 1
            yield wrong, word
        elif name == "ds_xor_b64":
            wrong = aot.lower(variant(lines, (name, "ds_and_b64")), 32, **binding)
            yield wrong, 0
        elif name in ("ds_min_u64", "ds_max_u64", "ds_min_i64", "ds_max_i64"):
            alternate = {"ds_min_u64": "ds_max_u64", "ds_max_u64": "ds_min_u64",
                         "ds_min_i64": "ds_max_i64", "ds_max_i64": "ds_min_i64"}[name]
            wrong = aot.lower(variant(lines, (name, alternate)), 32, **binding)
            yield wrong, 0
            # With only the high-word test left, min keeps the source on a tie
            # and first differs at lane 17. Max instead keeps the source at
            # lane 1, whose low word should have won.
            wrong = without_low_tiebreak(source)
            assert wrong != source
            yield wrong, 34 if name.startswith("ds_min") else 2
        else:
            alternate = "ds_or_b64" if name == "ds_and_b64" else "ds_and_b64"
            wrong = aot.lower(variant(lines, (name, alternate)), 32, **binding)
            yield wrong, 0
    elif name == "lds_dec_borrow":
        # Drop the high-word borrow; the low word still wraps to 0xffffffff.
        wrong = source.replace(" - uint(", " - 0u * uint(")
        assert wrong != source
        yield wrong, 1
    elif name in LDS_WIDE_QUADS:
        if name == "lds_b128":
            change = ("v_mov_b32_e32 v19, 0xf0e1d2c3", "v_mov_b32_e32 v19, 0")
            word = 3
        elif name == "lds_write2_b32":
            change = ("offset0:3 offset1:1", "offset0:1 offset1:3")
            word = 1
        else:
            change = ("ds_read2_b64 v[10:13], v5 offset1:1", "ds_read2_b64 v[10:13], v5 offset0:1")
            word = 0
        yield aot.lower(variant(lines, change), 32, **binding), word
    elif name in LDS_WIDE_PROGRAMS:
        if name == "lds_xchg2_b32":
            change = ("ds_wrxchg2_rtn_b32 v[10:11], v5, v10, v11 offset0:2",
                      "ds_wrxchg2_rtn_b32 v[10:11], v5, v10, v11 offset1:2")
        elif name == "lds_read2_b32":
            change = ("offset0:2 offset1:1", "offset0:1 offset1:2")
        elif name == "lds_b64":
            change = ("ds_read_b64 v[10:11], v5 offset:8", "ds_read_b64 v[10:11], v5")
        else:
            change = ("v_mad_u32_u24 v6, 1, v8, 0x12345670", "v_mov_b32_e32 v6, 0")
        yield aot.lower(variant(lines, change), 32, **binding), 0
    elif name in LDS_HIGH_WRITES:
        yield aot.lower(variant(lines, (name, name.removesuffix("_d16_hi"))), 32, **binding), 0
    elif name == "lds_addtid":
        changed = variant(lines, ("ds_read_addtid_b32 v11", "v_mov_b32_e32 v11, v10"))
        yield aot.lower(changed, 32, **binding), 1
    elif name == "lds_overlap":
        no_halfword = variant(lines, ("ds_write_b16 v5, v7", "s_nop 0"))
        yield aot.lower(no_halfword, 32, **binding), 1
        no_byte = variant(lines, ("ds_write_b8 v5, v7 offset:1", "s_nop 0"))
        yield aot.lower(no_byte, 32, **binding), 2
    elif name in LDS_COUNTERS:
        no_update = "ds_read_b32 v10, v5" if "_rtn_" in name else "s_nop 0"
        changed = variant(lines, (lds_counter_instruction(name), no_update))
        yield aot.lower(changed, 32, **binding), 1
    elif name in LDS_F64_ORDER:
        other = name.replace("min", "max") if name.startswith("ds_min") else name.replace("max", "min")
        yield aot.lower(variant(lines, (name, other)), 32, **binding), 1
    elif name in LDS_QWORD_PAIR_CASES:
        yield aot.lower(variant(lines, ("offset0:1", "offset1:1")), 32, **binding), 0
    elif name in LDS_F64_CMPST:
        changed = variant(lines, ("v[20:21], v[18:19] offset:8", "v[18:19], v[20:21] offset:8"))
        yield aot.lower(changed, 32, **binding), 0
    elif name in LDS_STRIDED_PROGRAMS:
        change = {
            "lds_st64_b32": ("offset0:3 offset1:2", "offset0:2 offset1:3"),
            "lds_st64_b64": ("offset0:2 offset1:1", "offset0:1 offset1:2"),
            "lds_st64_xchg": ("offset0:3 offset1:2", "offset0:2 offset1:3"),
            "lds_st64_xchg_b64": ("offset0:2 offset1:1", "offset0:1 offset1:2"),
        }[name]
        yield aot.lower(variant(lines, change), 32, **binding), 0
    elif name in LDS_WIDE_REPLACEMENTS:
        op = lds_wide_replacement_instruction(name)
        if "cmpst" in name:
            changed = variant(lines, ("v[18:19], v[20:21] offset:8", "v[20:21], v[18:19] offset:8"))
            word = 0
        elif "mskor" in name:
            changed = variant(lines, ("v_mov_b32_e32 v20, 0xf0e1d2c3", "v_mov_b32_e32 v20, 0x10205240"))
            word = 0
        else:
            changed = variant(lines, (op, "ds_read_b64 v[20:21], v5 offset:8"))
            word = 4
        yield aot.lower(changed, 32, **binding), word
    elif name in LDS_F32_OPS:
        if name in ("ds_min_rtn_f32", "ds_max_rtn_f32", "ds_cmpst_rtn_f32"):
            changed = variant(lines, ("buffer_store_dwordx2 v[10:11]", "v_mov_b32_e32 v11, v10\n"
                                      "buffer_store_dwordx2 v[10:11]"))
            ref = lds_f32_reference(name, 3)
            word = next(i for i in range(1, len(ref), 2) if ref[i - 1] != ref[i])
            yield aot.lower(changed, 32, **binding), word
        elif name.startswith("ds_add"):
            yield source.replace(" + asfloat(", " - asfloat("), 0
        elif name == "ds_cmpst_f32":
            yield aot.lower(variant(lines, ("ds_cmpst_f32 v5, v6, v7", "ds_cmpst_f32 v5, v7, v6")),
                            32, **binding), 0
        else:
            other = "ds_max_f32" if name == "ds_min_f32" else "ds_min_f32"
            yield aot.lower(variant(lines, (name, other)), 32, **binding), 0
    elif name == "private":
        yield source.replace(" >> 16u) & 0xffffu)", " >> 0u) & 0xffffu)"), 2
    else:
        bias = 5 if name == "ds_bpermute_b32" else 27
        yield source.replace(f"local.x + {bias}u", f"local.x + {32 - bias}u"), 0


def ds_draw(directory, compiler, renderer, backend, *, private=False, cases=None):
    if private:
        lds_controls()
    else:
        ds_controls()
    accepted = set()
    if cases is None:
        cases = LDS_CASES if private else DS_CASES
    for name, lines, binding in cases:
        source = aot.lower(lines, 32, **binding)
        src, cso, case = (directory / f"{name}.{ext}" for ext in ("hlsl", "cso", "case"))
        src.write_text(source, encoding="ascii")
        compile = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
        subprocess.run(compile, check=True)
        run = [renderer, backend, "--compute-store-case", cso, case]
        # Four words per lane fill the fixed 320-word readback before three groups.
        four_words = (name in ("lds_wave_counter", "lds_xchg2_b32") or name in LDS_QWORD_RETURNS
                      or name in LDS_WIDE_QUADS or name in LDS_STRIDED_PROGRAMS
                      or name in LDS_QWORD_PAIR_CASES
                      or name in LDS_F64_CMPST + LDS_F64_ORDER and "_rtn" in name
                      or name in LDS_QWORD_COUNTERS and "_rtn" in name
                      or name in LDS_WIDE_REPLACEMENTS and "_rtn" in name)
        group_counts = (1, 2) if four_words else (1, 3)
        if name == "lds_st64_xchg_b64":
            group_counts = (1,)
        for groups in group_counts:
            if name in LDS_UPDATES or name in LDS_RETURN_UPDATES:
                words = lds_update_reference(LDS_RETURN_UPDATES.get(name, name), groups)
            elif name in LDS_SUBWORDS:
                words = lds_subword_reference(*LDS_SUBWORDS[name], groups)
            elif name in LDS_D16_READS:
                words = lds_d16_reference(name, groups)
            elif name in LDS_REPLACEMENTS:
                words = lds_replacement_reference(name, groups)
            elif name in LDS_HIGH_WRITES:
                words = lds_high_write_reference(name, groups)
            elif name == "lds_addtid":
                words = [base + lane for lane in range(groups * 32) for base in (0x12345670, 0x89abcdef)]
            elif name == "lds_overlap":
                words = lds_overlap_reference(groups)
            elif name == "lds_wave_counter":
                words = lds_wave_counter_reference(groups)
            elif name in LDS_QWORD_COUNTERS:
                words = lds_qword_counter_reference(name, groups)
            elif name == "lds_dec_borrow":
                words = lds_qword_borrow_reference(groups)
            elif name in LDS_QWORD_UPDATES or name in LDS_QWORD_RETURNS:
                words = lds_qword_reference(name, groups)
            elif name in LDS_WIDE_PROGRAMS or name in LDS_WIDE_QUADS:
                words = lds_wide_reference(name, groups)
            elif name in LDS_COUNTERS:
                words = lds_counter_reference(name, groups)
            elif name in LDS_STRIDED_PROGRAMS:
                words = lds_strided_reference(name, groups)
            elif name in LDS_WIDE_REPLACEMENTS:
                words = lds_wide_replacement_reference(name, groups)
            elif name in LDS_F32_OPS:
                words = lds_f32_reference(name, groups)
            elif name in LDS_F64_CMPST:
                words = lds_f64_reference(name, groups)
            elif name in LDS_F64_ORDER:
                words = lds_f64_order_reference(name, groups)
            elif name == "lds_qword_exchange_old":
                words = [0x33333333, 0x44444444, 0x11111111, 0x22222222] * (groups * 32)
            elif name == "lds_qword_exchange_new":
                words = [word for lane in range(groups * 32)
                         for word in (0x76543210, 0x87654321, 0x12345670 + lane, 0xabcdefed)]
            elif private:
                words = lds_reference(groups)
            else:
                words = ds_reference(name, groups)
            words += [SENTINEL] * (320 - len(words))
            case.write_bytes(struct.pack("<321I", groups, *words))
            execution = subprocess.run(run)
            if execution.returncode:
                return execution.returncode
        for mutated, mismatched_word in ds_mutants(name, lines, binding, source):
            assert mutated != source
            src.write_text(mutated, encoding="ascii")
            subprocess.run(compile, check=True)
            bad = subprocess.run(run, capture_output=True, text=True)
            if bad.returncode != 1 or f"word={mismatched_word} expected=" not in bad.stderr:
                raise AssertionError(f"{name}: DS mutation escaped detection\n{bad.stderr}")
            print(bad.stderr, end="")
        accepted.update(line.split()[0] for line in lines)
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{op}\n" for op in sorted(accepted)), encoding="utf-8")
    return 0


MUBUF_STORE_VALUE = 0x12345678
MUBUF_SUBWORD_STORES = {
    "buffer_store_byte": (1, 0, 5),
    "buffer_store_byte_d16_hi": (1, 2, 7),
    "buffer_store_short": (2, 0, 4),
    "buffer_store_short_d16_hi": (2, 2, 6),
}


def mubuf_subword_store_cases():
    for op, (_, _, offset) in MUBUF_SUBWORD_STORES.items():
        yield op, [
            "s_mov_b32 s12, s7", "s_mov_b32 s13, 0",
            "s_load_dwordx4 s[20:23], s[12:13], 0x30", "v_lshl_add_u32 v8, s11, 5, v0",
            "s_waitcnt lgkmcnt(0)", f"v_xor_b32_e32 v10, {MUBUF_STORE_VALUE:#x}, v8",
            "v_mul_lo_u32 v12, 0x10000, v8", "v_xor_b32_e32 v10, v12, v10",
            f"{op} v10, v8, s[20:23], 0 idxen offset:{offset}", "s_endpgm",
        ], DS_BINDING | {"stride": 12}


def mubuf_subword_store_controls():
    _, lines, binding = next(mubuf_subword_store_cases())
    store = lines[-2]
    for replacement, diagnostic in (
        (store.replace("idxen", "offen"), "Unsupported subword buffer store target or address mode"),
        (store.replace("idxen", "idxen offen"), "Unsupported subword buffer store target or address mode"),
        (store.replace("offset:5", "offset:12"), "Unsupported store address"),
        (store.replace("buffer_store_byte", "buffer_store_short"), "Unverified raw UAV store"),
        (store.replace("v10,", "v[10:11],"), "Unverified raw UAV store"),
        (store + " glc", "Unsupported compute instruction"),
        (store + "\n" + store.replace("offset:5", "offset:7"), "Overlapping raw UAV stores"),
        (store + "\nbuffer_store_dword v10, v8, s[20:23], 0 idxen offset:4", "Overlapping raw UAV stores"),
    ):
        rejects(diagnostic, aot.lower, variant(lines, (store, replacement)), 32, **binding)
    rejects("Unsupported subword buffer store target or address mode", aot.lower, lines, 32,
            **(binding | {"cpu": "gfx1013"}))
    rejects("Unverified raw UAV store", aot.lower, variant(lines, ("s_waitcnt lgkmcnt(0)", "")), 32, **binding)
    rejects("Store address does not separate every invocation", aot.lower, lines, (32, 2, 1),
            **(binding | {"group_reg_y": 9}))


def mubuf_subword_store_expected(op, groups, *, wrong_half=False):
    width, source_byte, offset = MUBUF_SUBWORD_STORES[op]
    if wrong_half:
        source_byte = 2 - source_byte
    data = bytearray(SENTINEL.to_bytes(4, "little") * 320)
    for gid in range(groups * 32):
        base = gid * 12 + offset
        source = (MUBUF_STORE_VALUE ^ gid ^ (gid << 16)).to_bytes(4, "little")
        data[base:base + width] = source[source_byte:source_byte + width]
    return struct.unpack("<320I", data)


def mubuf_subword_store_draw(directory, compiler, renderer, backend):
    src, cso, case = (directory / f"mubuf-subword-store.{ext}" for ext in ("hlsl", "cso", "case"))
    compile_shader = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
    run = [renderer, backend, "--compute-store-case", cso, case]
    for op, lines, binding in mubuf_subword_store_cases():
        print(f"{op}: CPU reference", flush=True)
        src.write_text(aot.lower(lines, 32, **binding), encoding="ascii")
        subprocess.run(compile_shader, check=True)
        for groups in (1, 3):
            words = mubuf_subword_store_expected(op, groups)
            case.write_bytes(struct.pack("<321I", groups, *words))
            result = subprocess.run(run)
            if result.returncode:
                return result.returncode
        other = op.removesuffix("_d16_hi") if op.endswith("_hi") else op + "_d16_hi"
        full_word = "buffer_store_dword v10, v8, s[20:23], 0 idxen offset:4"
        controls = [("select the wrong VGPR half", lines[-2].replace(op, other, 1),
                     mubuf_subword_store_expected(op, groups, wrong_half=True)[1]),
                    ("overwrite the containing DWORD", full_word, MUBUF_STORE_VALUE)]
        for label, store, actual in controls:
            print(f"{op}: {label}", flush=True)
            src.write_text(aot.lower(variant(lines, (lines[-2], store)), 32, **binding), encoding="ascii")
            subprocess.run(compile_shader, check=True)
            result = subprocess.run(run, capture_output=True, text=True)
            if result.returncode != 1 or f"word=1 expected={words[1]:08x} actual={actual:08x}" not in result.stderr:
                raise AssertionError(f"{op}: {label} escaped detection\n{result.stderr}")
            print(result.stderr, end="")
    mubuf_subword_store_controls()
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{op}\n" for op in MUBUF_SUBWORD_STORES), encoding="utf-8")
    return 0


MTBUF_STORE_WORDS = (0x01234567, 0x89abcdef, 0x76543210, 0xfedcba98)


def mtbuf_store_cases():
    shapes = [(components, packed) for packed in (False, True) for components in range(1, 5)]
    for components, packed in shapes:
        count = (components + 1) // 2 if packed else components
        op = "tbuffer_store_format_" + ("d16_" if packed else "") + "xyzw"[:components]
        data = "v10" if count == 1 else f"v[10:{9 + count}]"
        format_components = 4 if packed and components == 3 else components
        fmt = f"BUF_FMT_{('16_' if packed else '32_') * format_components}UINT"
        store = f"{op} {data}, v8, s[20:23], 0 format:[{fmt}] idxen offset:4"
        lines = ["s_mov_b32 s12, s7", "s_mov_b32 s13, 0", "s_load_dwordx4 s[20:23], s[12:13], 0x30",
                 "v_lshl_add_u32 v8, s11, 5, v0", "s_waitcnt lgkmcnt(0)"]
        lines += [f"v_xor_b32_e32 v{10 + i}, {word:#x}, v8" for i, word in enumerate(MTBUF_STORE_WORDS[:count])]
        yield count, [*lines, store, "s_endpgm"], DS_BINDING | {"stride": 4 * (count + 1)}


def mtbuf_store_binary(directory, assembler, objcopy, library):
    src, obj, raw = (directory / f"mtbuf-store.{ext}" for ext in ("s", "o", "bin"))
    for _, lines, binding in mtbuf_store_cases():
        src.write_text("\n".join(lines) + "\n", encoding="ascii")
        subprocess.run([assembler, "-triple=amdgcn--amdpal", "-mcpu=gfx1030", f"-mattr={DS_FEATURES}",
                        "-filetype=obj", src, "-o", obj], check=True)
        subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, raw], check=True)
        decoded = aot.labelled(disassemble(raw.read_bytes(), library, offsets=True, cpu=b"gfx1030",
                                          features=DS_FEATURES.encode()))
        assert decoded[-2] == lines[-2]
        assert aot.lower(decoded, 32, **binding) == aot.lower(lines, 32, **binding)


def mtbuf_store_draw(directory, compiler, renderer, backend):
    accepted = []
    src, cso, case = (directory / f"mtbuf-store.{ext}" for ext in ("hlsl", "cso", "case"))
    compile = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
    run = [renderer, backend, "--compute-store-case", cso, case]
    for count, lines, binding in mtbuf_store_cases():
        store = lines[-2]
        op = store.split()[0]
        packed = "_d16_" in op
        partial = packed and op.endswith(("_x", "_xyz"))
        rejects("Unsupported typed buffer store", aot.lower, lines, 32, **(binding | {"cpu": "gfx1013"}))
        rejects("Unsupported typed buffer store", aot.lower, variant(lines, ("idxen", "offen")), 32, **binding)
        fmt = store.split("format:[")[1].split("]")[0]
        wrong_format = "BUF_FMT_32_32_UINT" if fmt == "BUF_FMT_32_UINT" else "BUF_FMT_32_UINT"
        rejects("Unsupported typed buffer store", aot.lower,
                variant(lines, (fmt, wrong_format)), 32, **binding)
        rejects("Unverified raw UAV store", aot.lower,
                variant(lines, ("s_waitcnt lgkmcnt(0)", "")), 32, **binding)
        rejects("Unsupported store address", aot.lower, lines, 32, **(binding | {"stride": 4 * count}))
        rejects("Overlapping raw UAV stores", aot.lower,
                variant(lines, (store, store + f"\nbuffer_store_dword v10, v8, s[20:23], 0 idxen offset:{4 * count}")),
                32, **binding)
        source = aot.lower(lines, 32, **binding)
        src.write_text(source, encoding="ascii")
        subprocess.run(compile, check=True)
        for groups in (1, 2):
            words = [SENTINEL] * 320
            for gid in range(groups * 32):
                start = gid * (count + 1) + 1
                words[start:start + count] = [word ^ gid for word in MTBUF_STORE_WORDS[:count]]
                if partial:
                    last = start + count - 1
                    words[last] = (SENTINEL & 0xffff0000) | (words[last] & 0xffff)
            case.write_bytes(struct.pack("<321I", groups, *words))
            result = subprocess.run(run)
            if result.returncode:
                return result.returncode
        if packed:
            # Swap the low and high channel in the first VGPR only.
            swapped = "v_lshlrev_b32_e32 v22, 16, v10\nv_lshrrev_b32_e32 v23, 16, v10\nv_or_b32_e32 v10, v22, v23"
            mutant, mismatch = variant(lines, (store, swapped + "\n" + store)), 1
        else:
            # A legal translation shifted one DWORD early must clobber the record padding.
            mutant, mismatch = variant(lines, (store, store.removesuffix(" offset:4"))), 0
        mutants = [(aot.lower(mutant, 32, **binding), mismatch)]
        if partial:
            mutants.append((source.replace("& 0xffff0000u", "& 0u"), count))
        for changed, mismatch in mutants:
            assert changed != source
            src.write_text(changed, encoding="ascii")
            subprocess.run(compile, check=True)
            bad = subprocess.run(run, capture_output=True, text=True)
            if bad.returncode != 1 or f"word={mismatch} expected=" not in bad.stderr:
                raise AssertionError(f"{op}: incorrect store escaped detection\n{bad.stderr}")
            print(bad.stderr, end="")
        accepted.append(op)
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(accepted) + "\n", encoding="utf-8")
    return 0


MUBUF_REDUCTION_INPUT = 0x2434c810


def mubuf_reduction_cases(wide=False):
    for op in ("add", "sub", "and", "or", "xor", "smin", "umin", "smax", "umax"):
        name = f"buffer_atomic_{op}{'_x2' if wide else ''}"
        data, offset = ("v[10:11]", 8) if wide else ("v10", 4)
        lines = ["s_mov_b32 s12, s7", "s_mov_b32 s13, 0",
                 "s_load_dwordx4 s[20:23], s[12:13], 0x30",
                 "v_lshl_add_u32 v8, s11, 5, v0", "s_waitcnt lgkmcnt(0)",
                 f"v_xor_b32_e32 v10, {MUBUF_REDUCTION_INPUT:#x}, v8",
                 "v_and_b32_e32 v12, 3, v8", "v_lshlrev_b32_e32 v12, 30, v12",
                 "v_xor_b32_e32 v10, v12, v10"]
        if wide:
            lines += ["v_xor_b32_e32 v11, 0x100000, v10"]
        lines += ["v_cmp_gt_u32_e32 vcc_lo, 63, v8", "s_and_saveexec_b32 s14, vcc_lo",
                  f"{name} {data}, off, s[20:23], 0 offset:{offset}", "s_endpgm"]
        yield name, lines, {k: v for k, v in DS_BINDING.items() if k != "stride"}


def mubuf_reduction_reference(op, groups, wide=False):
    inputs = [MUBUF_REDUCTION_INPUT ^ gid ^ ((gid % 4) * 0x40000000) for gid in range(min(32 * groups, 63))]
    initial, bits = SENTINEL, 32
    if wide:
        initial |= SENTINEL << 32
        bits = 64
        inputs = [word + (word ^ 0x100000) * (1 << 32) for word in inputs]
    if op in ("add", "sub"):
        return (initial + (sum(inputs) if op == "add" else -sum(inputs))) % (1 << bits)
    values = [initial, *inputs]
    if op in ("smin", "smax", "umin", "umax"):
        compare = min if op.endswith("min") else max
        signed = op.startswith("s")
        return compare(values, key=lambda word: word - (1 << bits) if signed and word >= (1 << (bits - 1)) else word)
    result = initial
    for value in inputs:
        if op == "and":
            result &= value
        elif op == "or":
            result |= value
        elif op == "xor":
            result ^= value
        else:
            raise AssertionError(op)
    return result


def mubuf_reduction_controls():
    _, lines, binding = next(mubuf_reduction_cases())
    atomic = lines[-2]
    for replacement, diagnostic in (
        (atomic + " glc", "Unsupported compute instruction"),
        (atomic.replace("off", "v8", 1) + " idxen", "Unsupported compute instruction"),
        (atomic.replace("], 0", "], s12"), "Unsupported compute instruction"),
        (atomic.replace("offset:4", "offset:2"), "Unverified atomic reduction address"),
        (atomic.replace("offset:4", "offset:4096"), "Unverified atomic reduction address"),
        (atomic.replace("s[20:23]", "s[21:24]"), "Unverified atomic reduction descriptor"),
        (atomic.replace("v10", "v255"), "Atomic reduction data is not a known word"),
        (atomic + "\n" + atomic, "An atomic reduction must be the only UAV operation"),
        (atomic + "\nbuffer_store_dword v10, off, s[20:23], 0", "An atomic reduction must be the only UAV operation"),
    ):
        rejects(diagnostic, aot.lower, variant(lines, (atomic, replacement)), 32, **binding)
    rejects("Unverified atomic reduction descriptor", aot.lower,
            variant(lines, ("s_waitcnt lgkmcnt(0)", "")), 32, **binding)
    rejects("Unsupported atomic reduction target or binding", aot.lower, lines, 32, **(binding | {"cpu": "gfx1013"}))
    rejects("Unsupported atomic reduction target or binding", aot.lower, lines, 32, **(binding | {"stride": 4}))


def mubuf_reduction_x2_controls():
    _, lines, binding = next(mubuf_reduction_cases(wide=True))
    atomic = lines[-2]
    for replacement, diagnostic in (
        (atomic.replace("v[10:11]", "v10"), "Unverified atomic reduction data range"),
        (atomic.replace("v[10:11]", "v[10:12]"), "Unverified atomic reduction data range"),
        (atomic.replace("v[10:11]", "v[255:256]"), "Unverified atomic reduction data range"),
        (atomic.replace("v[10:11]", "v[12:13]"), "Atomic reduction data is not a known word"),
        (atomic.replace("offset:8", "offset:4"), "Unverified atomic reduction address"),
        (atomic + " glc", "Unsupported compute instruction"),
    ):
        rejects(diagnostic, aot.lower, variant(lines, (atomic, replacement)), 32, **binding)


def mubuf_reduction_draw(directory, compiler, renderer, backend, wide=False):
    (mubuf_reduction_x2_controls if wide else mubuf_reduction_controls)()
    src, cso, case = (directory / f"mubuf-reduction.{ext}" for ext in ("hlsl", "cso", "case"))
    compile_shader = [compiler, "-T", "cs_6_6" if wide else "cs_6_0", "-E", "main", "-Fo", cso, src]
    runner = "--compute-store-case-atomic64" if wide else "--compute-store-case"
    run = [renderer, backend, runner, cso, case]
    wrong_ops = {"add": "sub", "sub": "add", "and": "or", "or": "and", "xor": "or",
                 "smin": "umin", "umin": "smin", "smax": "umax", "umax": "smax"}
    accepted = []
    for name, lines, binding in mubuf_reduction_cases(wide):
        op = name.removeprefix("buffer_atomic_").removesuffix("_x2")
        print(name, flush=True)
        src.write_text(aot.lower(lines, 32, **binding), encoding="ascii")
        subprocess.run(compile_shader, check=True)
        for groups in (1, 2):
            words = [SENTINEL] * 320
            reference = mubuf_reduction_reference(op, groups, wide)
            if wide:
                words[2:4] = struct.unpack("<2I", struct.pack("<Q", reference))
            else:
                words[1] = reference
            case.write_bytes(struct.pack("<321I", groups, *words))
            result = subprocess.run(run)
            if result.returncode:
                return result.returncode
        wrong = wrong_ops[op]
        changed = variant(lines, (name, f"buffer_atomic_{wrong}{'_x2' if wide else ''}"))
        src.write_text(aot.lower(changed, 32, **binding), encoding="ascii")
        subprocess.run(compile_shader, check=True)
        result = subprocess.run(run, capture_output=True, text=True)
        actual = mubuf_reduction_reference(wrong, groups, wide)
        if wide:
            actual_low, actual_high = struct.unpack("<2I", struct.pack("<Q", actual))
            first = 2 if words[2] != actual_low else 3
            actual = actual_low if first == 2 else actual_high
        else:
            first = 1
        if result.returncode != 1 or f"word={first} expected={words[first]:08x} actual={actual:08x}" not in result.stderr:
            raise AssertionError(f"{name}: incorrect reduction escaped detection\n{result.stderr}")
        print(result.stderr, end="")
        accepted.append(name)
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(accepted) + "\n", encoding="utf-8")
    return 0


MUBUF_EXCHANGE_DATA = (0x89abcdef, 0x12345678)


def mubuf_exchange_cases(wide=False):
    for op in ("swap", "cmpswap"):
        name = f"buffer_atomic_{op}{'_x2' if wide else ''}"
        width = 2 if wide else 1
        count = 2 * width if op == "cmpswap" else width
        data = f"v[10:{9 + count}]" if count > 1 else "v10"
        lines = ["s_mov_b32 s12, s7", "s_mov_b32 s13, 0",
                 "s_load_dwordx4 s[20:23], s[12:13], 0x30", "s_waitcnt lgkmcnt(0)"]
        words = MUBUF_EXCHANGE_DATA[:width] + (SENTINEL,) * (count - width)
        lines += [f"v_mov_b32_e32 v{10 + i}, {word:#x}" for i, word in enumerate(words)]
        lines += [f"{name} {data}, off, s[20:23], 0 offset:8", "s_endpgm"]
        yield name, lines, {k: v for k, v in DS_BINDING.items() if k != "stride"}


def mubuf_exchange_controls(wide):
    _, lines, binding = list(mubuf_exchange_cases(wide))[-1]
    atomic = lines[-2]
    for replacement, diagnostic in (
        (atomic.replace(atomic.split()[1], "v10,"), "Unverified atomic reduction data range"),
        (atomic + " glc", "Unsupported compute instruction"),
    ):
        rejects(diagnostic, aot.lower, variant(lines, (atomic, replacement)), 32, **binding)
    rejects("Atomic reduction data is not a known word", aot.lower,
            variant(lines, (lines[-3], "")), 32, **binding)


def mubuf_exchange_draw(directory, compiler, renderer, backend, wide=False):
    mubuf_exchange_controls(wide)
    src, cso, case = (directory / f"mubuf-exchange.{ext}" for ext in ("hlsl", "cso", "case"))
    compile_shader = [compiler, "-T", "cs_6_6" if wide else "cs_6_0", "-E", "main", "-Fo", cso, src]
    runner = "--compute-store-case-atomic64" if wide else "--compute-store-case"
    run = [renderer, backend, runner, cso, case]
    width = 2 if wide else 1
    accepted = []
    for name, lines, binding in mubuf_exchange_cases(wide):
        print(name, flush=True)
        compare = "cmpswap" in name
        for match in ((True, False) if compare else (True,)):
            program = lines if match else variant(lines, (lines[-3], lines[-3].replace(f"{SENTINEL:#x}", "0")))
            src.write_text(aot.lower(program, 32, **binding), encoding="ascii")
            subprocess.run(compile_shader, check=True)
            words = [SENTINEL] * 320
            if match:
                words[2:2 + width] = MUBUF_EXCHANGE_DATA[:width]
            for groups in (1, 2):
                case.write_bytes(struct.pack("<321I", groups, *words))
                result = subprocess.run(run)
                if result.returncode:
                    return result.returncode
        # This still makes a valid exchange, but writes the target or its neighbor incorrectly.
        if compare:
            pair = [line for line in lines if line.startswith("v_mov_b32_e32")]
            changed = list(lines)
            for i in range(width * 2):
                j = lines.index(pair[i])
                repl = pair[(i + width) % (width * 2)].split(", ")[-1]
                changed[j] = pair[i].split(", ")[0] + ", " + repl
            mismatch, expected, actual = 2, MUBUF_EXCHANGE_DATA[0], SENTINEL
        else:
            changed = variant(lines, ("offset:8", "offset:0"))
            mismatch, expected, actual = 0, SENTINEL, MUBUF_EXCHANGE_DATA[0]
        words[2:2 + width] = MUBUF_EXCHANGE_DATA[:width]
        case.write_bytes(struct.pack("<321I", 2, *words))
        src.write_text(aot.lower(changed, 32, **binding), encoding="ascii")
        subprocess.run(compile_shader, check=True)
        result = subprocess.run(run, capture_output=True, text=True)
        if result.returncode != 1 or f"word={mismatch} expected={expected:08x} actual={actual:08x}" not in result.stderr:
            raise AssertionError(f"{name}: incorrect exchange escaped detection\n{result.stderr}")
        print(result.stderr, end="")
        accepted.append(name)
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(accepted) + "\n", encoding="utf-8")
    return 0


def scalar_alu_binary(directory, assembler, objcopy, library):
    """The assembled scalar chain decodes to the same program as its text."""
    src, obj, raw = [directory / x for x in ("scalar.s", "scalar.o", "scalar.bin")]
    src.write_text("\n".join(READ_SCALAR) + "\n", encoding="ascii")
    subprocess.run([assembler, "-triple=amdgcn--amdpal", "-mcpu=gfx1013", "-filetype=obj", src, "-o", obj], check=True)
    subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, raw], check=True)
    decoded = aot.labelled(disassemble(raw.read_bytes(), library, offsets=True))
    assert aot.lower(decoded, 32, **READ_BINDING) == aot.lower(READ_SCALAR, 32, **READ_BINDING)


def mimg_load_binary(directory, assembler, objcopy, library):
    """Assemble and decode each dmask form, then lower the decoded program through the CLI."""
    mimg_load_controls()
    mimg_a16_controls()
    mimg_mip_controls()
    cases = [(f"{op}{'_a16' if a16 else ''}_{mask:x}", lines, MIMG_BINDING)
             for op in ("image_load", "image_load_mip") for a16 in (False, True)
             for mask, _, _, _, lines in mimg_load_cases(a16, mip=op == "image_load_mip")]
    gfx1030_binary(directory, assembler, objcopy, library, cases, "image_", group=(8, 8, 1))


def mubuf_cache_invalidate_binary(directory, assembler, objcopy, library):
    """Check that recognized cache operations stop before the CLI emits HLSL."""
    binding = INDEX_BINDING | {"cpu": "gfx1030"}
    dims = aot.checked_group(32)
    original = aot.lower(INDEXED, dims, **binding)
    options = [item for key, value in binding.items() for item in ("--" + key.replace("_", "-"), str(value))]
    for op in ("s_nop 0", "buffer_gl0_inv", "buffer_gl1_inv"):
        lines = INDEXED[:-1] + [op, "s_waitcnt vmcnt(0)", INDEXED[-1]]
        code = assemble(directory, assembler, objcopy, lines, cpu="gfx1030", features=DS_FEATURES)
        decoded = aot.labelled(disassemble(code, library, offsets=True, cpu=b"gfx1030",
                                          features=DS_FEATURES.encode()))
        assert decoded[-3:] == lines[-3:], decoded
        name = op.split()[0]
        container, output = directory / f"{name}.dxbc", directory / f"{name}.hlsl"
        container.write_bytes(store_container(code, dims))
        cli = subprocess.run([sys.executable, "-B", aot.__file__, container, "--output", output,
                              "--llvm", library, *options], capture_output=True, text=True)
        if op == "s_nop 0":
            assert cli.returncode == 0, cli.stderr
            assert output.read_text() == original
            continue
        diagnostic = f"Unsupported MUBUF cache invalidation: {op}"
        assert cli.returncode == 1 and cli.stderr.splitlines()[-1:] == [f"ValueError: {diagnostic}"], cli.stderr
        assert not output.exists()
        rejects(diagnostic, aot.lower, lines, dims, **binding)
        print(diagnostic)


def binary(directory, assembler, objcopy, library):
    mubuf_cache_invalidate_binary(directory, assembler, objcopy, library)
    mubuf_reduction_controls()
    mubuf_reduction_x2_controls()
    for wide in (False, True):
        gfx1030_binary(directory, assembler, objcopy, library, mubuf_reduction_cases(wide), "buffer_atomic_")
        mubuf_exchange_controls(wide)
        gfx1030_binary(directory, assembler, objcopy, library, mubuf_exchange_cases(wide), "buffer_atomic_")
    mubuf_subword_controls()
    gfx1030_binary(directory, assembler, objcopy, library, mubuf_subword_cases(), "buffer_load_")
    mubuf_vector_controls()
    gfx1030_binary(directory, assembler, objcopy, library, mubuf_vector_cases(), "buffer_load_")
    mubuf_format_d16_controls()
    gfx1030_binary(directory, assembler, objcopy, library, mubuf_format_d16_cases(), "buffer_load_format_d16_")
    mubuf_d16_controls()
    gfx1030_binary(directory, assembler, objcopy, library, mubuf_d16_cases(), "buffer_load_")
    mtbuf_store_binary(directory, assembler, objcopy, library)
    scalar_alu_binary(directory, assembler, objcopy, library)
    mubuf_subword_store_controls()
    gfx1030_binary(directory, assembler, objcopy, library, mubuf_subword_store_cases(), "buffer_store_")
    ds_controls()
    lds_controls()
    ds_binary(directory, assembler, objcopy, library)
    mtbuf_load_controls()
    mimg_load_binary(directory, assembler, objcopy, library)
    mimg_resinfo_controls()
    gfx1030_binary(directory, assembler, objcopy, library,
                   [(name, lines, MIMG_BINDING) for name, _, _, _, _, lines in mimg_resinfo_cases()],
                   "image_get_resinfo", group=(8, 8, 1))
    for minmax in (False, True):
        mimg_atomic_controls(minmax=minmax)
        gfx1030_binary(directory, assembler, objcopy, library,
                       [(f"image_atomic_{name}", lines, binding)
                        for name, _, lines, binding in mimg_atomic_cases(minmax=minmax)],
                       "image_atomic_", group=MIMG_STORE_GROUP)
    mimg_store_controls()
    gfx1030_binary(directory, assembler, objcopy, library,
                   [(name, lines, binding) for name, _, _, lines, binding in mimg_store_cases()],
                   "image_", group=(4, 2, 1))
    mimg_store_controls(packed=True)
    gfx1030_binary(directory, assembler, objcopy, library,
                   [(name, lines, binding) for name, _, _, lines, binding in mimg_store_cases(packed=True)],
                   "image_", group=MIMG_STORE_GROUP)
    mimg_packed_controls()
    gfx1030_binary(directory, assembler, objcopy, library,
                   [(name, lines, MIMG_PACKED_BINDING) for name, _, _, _, _, _, _, lines in mimg_packed_cases()],
                   "image_", group=(8, 8, 1))
    gfx1030_binary(directory, assembler, objcopy, library, MTBUF_LOAD_CASES, "tbuffer_")
    two_exits(directory, assembler, objcopy, library)
    mubuf_format_store_controls()
    mubuf_format_d16_store_controls()
    for packed in (False, True):
        gfx1030_binary(directory, assembler, objcopy, library, mubuf_format_store_cases(packed),
                       "buffer_store_format_", group=16)
    mubuf_format_controls()
    gfx1030_binary(directory, assembler, objcopy, library, mubuf_format_cases(), "buffer_load_format_")
    for name, lines, width, binding in CASES + GUARD_CASES + READ_CASES + ALU_CASES + F32_CASES:
        src, obj, raw = [directory / x for x in ("test.s", "test.o", "test.bin")]
        src.write_text("\n".join(lines) + "\n", encoding="ascii")
        subprocess.run([assembler, "-triple=amdgcn--amdpal", "-mcpu=gfx1013", "-filetype=obj", src, "-o", obj], check=True)
        subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, raw], check=True)
        code = raw.read_bytes()
        dims = aot.checked_group(width)
        blob = store_container(code, dims)
        sections = parts(blob)
        assert aot.group_size(sections[b"PSV0"]) == dims
        assert aot.container_code(sections[b"DXIL"], stage=5) == code
        must_reject(lambda: aot.container_code(sections[b"DXIL"], stage=0))
        decoded = aot.labelled(disassemble(code, library, offsets=True))
        assert aot.lower(decoded, width, **binding) == aot.lower(lines, width, **binding)
        container, output = directory / "input.dxbc", directory / "cli.hlsl"
        container.write_bytes(blob)
        options = [item for key, value in binding.items() for item in ("--" + key.replace("_", "-"), str(value))]
        cli = [sys.executable, "-B", str(Path(aot.__file__)), container, "--output", output,
               "--llvm", library, *options]
        subprocess.run(cli, check=True)
        assert output.read_text() == aot.lower(lines, width, **binding)
        if name in {case[0] for case in GUARD_CASES}:
            # The decoded branch lands where the guarded store ends. Retarget it to the store (inside
            # the guarded block), into the store's second word, and past the restore or the exit.
            offsets = dict((line, pc) for pc, line in disassemble(code, library, offsets=True))
            pc, store = offsets[next(line for line in offsets if line.startswith("s_cbranch"))], max(
                pc for line, pc in offsets.items() if line.startswith("buffer_store") and "v0, v0" in line)
            after = offsets.get("v_mov_b32_e32 v1, 0x600d", len(code))
            if name == "guard_join":
                # Landing on the restore itself, past the hazard wait, is the same program.
                accepted = code[:pc] + struct.pack("<h", (offsets["s_or_b32 exec_lo, exec_lo, s0"] - pc - 4) // 4)
                container.write_bytes(store_container(accepted + code[pc + 2:], dims))
                subprocess.run(cli, check=True)
                assert output.read_text() == aot.lower(lines, width, **binding)
            for target, message in ((store, "Lane guard branch does not reach its restore"),
                                    (store + 4, "Branch outside decoded instruction boundaries"),
                                    (after, "Unverified lane guard restore" if name == "guard_join"
                                     else "Branch outside decoded instruction boundaries")):
                wrong = code[:pc] + struct.pack("<h", (target - pc - 4) // 4) + code[pc + 2:]
                container.write_bytes(store_container(wrong, dims))
                bad = subprocess.run(cli, capture_output=True, text=True)
                assert bad.returncode == 1 and message in bad.stderr, (name, target, bad.stderr)


def draw_runtime(cmd, case):
    first = [0x80000000 + i * 0x101 for i in range(16)]
    second = [0x7fc00000 + i * 0x181 for i in range(16)]
    refs = []
    for values in (first, second):
        words = [SENTINEL] * 320
        words[4:7] = [values[0], values[3], values[4]]
        words[8:12] = values[12:16][::-1]
        words[16] = values[12]
        refs.append(words)
        case.write_bytes(struct.pack("<337I", 1, *words, *values))
        result = subprocess.run(cmd)
        if result.returncode:
            return result.returncode
    case.write_bytes(struct.pack("<337I", 1, *refs[0], *second))
    stale = subprocess.run(cmd, capture_output=True, text=True)
    if (stale.returncode != 1 or "word=4 expected=80000000 actual=7fc00000" not in stale.stderr
            or "groups=1 compared_words=320 mismatches=8" not in stale.stdout.splitlines()):
        raise AssertionError(f"Expected a stale runtime constant mismatch\n{stale.stdout}\n{stale.stderr}")
    case.write_bytes(struct.pack("<337I", 1, *refs[1], *second)[:-4])
    truncated = subprocess.run(cmd, capture_output=True, text=True)
    if truncated.returncode != 1 or "Invalid compute store constants" not in truncated.stderr:
        raise AssertionError(f"Expected a truncated constants error\n{truncated.stdout}\n{truncated.stderr}")
    return 0


def grid_mutations(source, name):
    """Translations that a dispatch-dimension mistake would produce; each must fail on the GPU."""
    a, b = {"slab": ("tid.z", "tid.x")}.get(name, ("tid.x", "tid.y"))
    if name != "wide":
        yield "swapped axes", source.replace(a, "tid.T").replace(b, a).replace("tid.T", b)
    if name in ("slab", "cube", "llvm_cube"):
        yield "Z ignored", source.replace("tid.z", "tid.y")
    if name == "llvm_cube":
        for a, b in (("tid.x", "tid.z"), ("tid.y", "tid.z")):
            yield f"swapped {a[-1].upper()}/{b[-1].upper()}", (
                source.replace(a, "tid.T").replace(b, a).replace("tid.T", b))
    if name == "pitch":
        yield "power-of-two pitch", source.replace(" * 72u", " * 64u")
    dims = dict((case[0], case[2]) for case in CASES)[name]
    shrunk = (dims[0] // 2, *dims[1:])
    yield "halved group X", source.replace("[numthreads({},{},{})]".format(*dims),
                                           "[numthreads({},{},{})]".format(*shrunk))


def draw_grid(directory, compiler, cmd, case, name):
    dispatches = GRID_DISPATCHES[name]
    for groups in dispatches:
        words = grid_words(name, groups)
        packed = groups[0] | groups[1] << 8 | groups[2] << 16
        case.write_bytes(struct.pack("<321I", packed, *words))
        result = subprocess.run(cmd)
        if result.returncode:
            return result.returncode
    guard = words.index(SENTINEL)
    words[guard] ^= 1
    case.write_bytes(struct.pack("<321I", packed, *words))
    bad = subprocess.run(cmd, capture_output=True, text=True)
    if bad.returncode != 1 or f"word={guard} expected=" not in bad.stderr:
        raise AssertionError(f"{name}: a wrong untouched word escaped detection")
    words[guard] ^= 1
    if len(dispatches) > 1:
        first = dispatches[0]
        case.write_bytes(struct.pack("<321I", first[0] | first[1] << 8 | first[2] << 16, *words))
        bad = subprocess.run(cmd, capture_output=True, text=True)
        if bad.returncode != 1 or "Compute readback differs" not in bad.stderr:
            raise AssertionError(f"{name}: a wrong group count escaped detection")
    source = (directory / "store.hlsl").read_text(encoding="ascii")
    for label, mutated in grid_mutations(source, name):
        if mutated == source:
            raise AssertionError(f"{name}: mutation '{label}' did not change the source")
        src, cso = directory / "mutated.hlsl", directory / "mutated.cso"
        src.write_text(mutated, encoding="ascii")
        subprocess.run([compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src], check=True)
        groups = MUTATION_DISPATCH.get((name, label), dispatches[-1])
        assert groups in dispatches, (name, label)
        case.write_bytes(struct.pack("<321I", groups[0] | groups[1] << 8 | groups[2] << 16,
                                     *grid_words(name, groups)))
        bad = subprocess.run([*cmd[:3], cso, case], capture_output=True, text=True)
        if bad.returncode != 1 or "Compute readback differs" not in bad.stderr:
            raise AssertionError(f"{name}: mutation '{label}' escaped detection with groups={groups}")
    return 0


def guard_words(name, groups, bound):
    """Independent expected readback: lanes at or past the bound leave guarded words untouched."""
    words = [SENTINEL] * 320
    for gid in range(32 * groups):
        if name == "guard_end" and gid < bound:
            words[gid] = gid
        elif name == "guard_join":
            if gid < bound:
                words[2 * gid] = gid
            words[2 * gid + 1] = 0x600d
    return words


# (groups, bound): no lane, one group split, exactly one group; one lane, the third group split,
# exactly the dispatch, past the dispatch. Mutations run on the largest dispatch with a split group.
GUARD_RUNS = ((1, 0), (1, 17), (1, 32), (3, 1), (3, 70), (3, 96), (3, 200))
GUARD_MUTATION_RUN = (3, 70)


def guard_mutations(source, name):
    """Translations that a guard mistake would produce; each must fail on the GPU."""
    lines = source.splitlines()
    at = lines.index(GUARD_IF)
    close = lines.index("    }", at)
    yield "guard inverted", source.replace(GUARD_IF, "    if (!(params[0].x > tid.x)) {")
    yield "bound off by one", source.replace(GUARD_IF, "    if (params[0].x >= tid.x) {")
    yield "guard ignored", "\n".join(lines[:at] + [line[4:] for line in lines[at + 1:close]] + lines[close + 1:]) + "\n"
    if name == "guard_join":
        # The exec restore ignored: the store after it stays under the guard.
        moved = lines[:close] + ["    " + lines[close + 1], "    }"] + lines[close + 2:]
        yield "restore ignored", "\n".join(moved) + "\n"


def draw_guard(directory, compiler, cmd, case, name):
    # params[0].x is the bound; the other words differ so that reading another word fails.
    constants = [0x5eed0000 + i for i in range(16)]

    def write(groups, bound, words=None):
        case.write_bytes(struct.pack("<337I", groups, *(words or guard_words(name, groups, bound)), bound,
                                     *constants[1:]))

    for groups, bound in GUARD_RUNS:
        write(groups, bound)
        result = subprocess.run(cmd)
        if result.returncode:
            return result.returncode
    groups, bound = GUARD_MUTATION_RUN
    # Reference controls: the words of the first lane past the bound, and of the last lane under it.
    for gid, wrong in ((bound, bound + 1), (bound - 1, bound - 1)):
        reference = guard_words(name, groups, wrong)
        write(groups, bound, reference)
        bad = subprocess.run(cmd, capture_output=True, text=True)
        word = gid * (1 if name == "guard_end" else 2)
        if bad.returncode != 1 or f"word={word} expected=" not in bad.stderr:
            raise AssertionError(f"{name}: a reference with bound {wrong} escaped detection\n{bad.stderr}")
    write(groups, bound)
    source = (directory / "store.hlsl").read_text(encoding="ascii")
    for label, mutated in guard_mutations(source, name):
        if mutated == source:
            raise AssertionError(f"{name}: mutation '{label}' did not change the source")
        src, cso = directory / "mutated.hlsl", directory / "mutated.cso"
        src.write_text(mutated, encoding="ascii")
        subprocess.run([compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src], check=True)
        bad = subprocess.run([*cmd[:3], cso, case], capture_output=True, text=True)
        if bad.returncode != 1 or "Compute readback differs" not in bad.stderr:
            raise AssertionError(f"{name}: mutation '{label}' escaped detection with groups={groups} bound={bound}")
    return 0


def source_word(i):
    """t0 as the harness fills it: 320 known words, then 320 sentinel words."""
    return (0x8b02a7f1 + i * 0x413b1) & 0xffffffff if i < 320 else SENTINEL


MUBUF_SUBWORD = {"ubyte": (1, False, 1), "sbyte": (1, True, 5), "ushort": (2, False, 6), "sshort": (2, True, 0)}


def mubuf_subword_cases():
    for name, (_, _, offset) in MUBUF_SUBWORD.items():
        suffix = f" offset:{offset}" if offset else ""
        yield name, [
            "s_mov_b32 s12, s7", "s_mov_b32 s13, 0",
            "s_load_dwordx4 s[20:23], s[12:13], 0x30",
            "s_load_dwordx4 s[16:19], s[12:13], 0x40",
            "v_lshl_add_u32 v8, s11, 5, v0", "s_waitcnt lgkmcnt(0)",
            f"buffer_load_{name} v10, v8, s[16:19], 0 idxen{suffix}", "s_waitcnt vmcnt(0)",
            "buffer_store_dword v10, v8, s[20:23], 0 idxen offset:4", "s_endpgm",
        ], DS_BINDING | {"stride": 8, "source_descriptor": 0x40, "source_stride": 8}


def mubuf_subword_controls():
    _, lines, binding = next(mubuf_subword_cases())
    load = lines[6]
    for new, diagnostic in (
        (load + " glc", "Unsupported compute instruction"),
        (load + " tfe", "Unsupported compute instruction"),
        (load.replace("load_ubyte", "load_format_d16_x"), "Formatted buffer reads require a source format"),
        (load.replace(", 0 idxen", ", s13 idxen"), "Unsupported compute instruction"),
        (load.replace("idxen", "offen"), "Unsupported subword buffer read target or address mode"),
        (load.replace("idxen", "idxen offen"), "Unsupported subword buffer read target or address mode"),
        (load.replace("load_ubyte", "load_ushort"), "Unverified read offset"),
        (load.replace("s[16:19]", "s[20:23]"), "Reads from the stored UAV"),
        (load.replace("offset:1", "offset:8"), "Read leaves its source element"),
        (load.replace("offset:1", "offset:4096"), "Unverified read offset"),
    ):
        rejects(diagnostic, aot.lower, variant(lines, (load, new)), 32, **binding)
    for wait, diagnostic in (("s_waitcnt lgkmcnt(0)", "Unverified buffer read descriptor"),
                             ("s_waitcnt vmcnt(0)", "before s_waitcnt vmcnt(0)")):
        rejects(diagnostic, aot.lower, variant(lines, (wait, "")), 32, **binding)
    rejects("Unsupported read address", aot.lower, lines, 32, **(binding | {"source_stride": None}))
    rejects("Unsupported subword buffer read target", aot.lower, lines, 32, **(binding | {"cpu": "gfx1013"}))


def mubuf_subword_draw(directory, compiler, renderer, backend):
    mubuf_subword_controls()
    src, cso, case = (directory / f"mubuf-subword.{ext}" for ext in ("hlsl", "cso", "case"))
    compile = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
    run = [renderer, backend, "--compute-store-case", cso, case]
    data = struct.pack("<320I", *(source_word(i) for i in range(320)))
    for name, lines, binding in mubuf_subword_cases():
        width, signed, offset = MUBUF_SUBWORD[name]
        print(f"{name}: CPU reference", flush=True)
        src.write_text(aot.lower(lines, 32, **binding), encoding="ascii")
        subprocess.run(compile, check=True)
        for groups in (1, 3):
            words = [SENTINEL] * 320
            for gid in range(32 * groups):
                start = gid * 8 + offset
                words[2 * gid + 1] = int.from_bytes(data[start:start + width], "little", signed=signed) & 0xffffffff
            case.write_bytes(struct.pack("<321I", groups, *words))
            result = subprocess.run(run)
            if result.returncode:
                return result.returncode
        load = lines[6]
        # Flip the subword lane, or drop sign extension for a signed load.
        stem = load.split(" offset:")[0]
        other = offset ^ width
        mutants = [stem + (f" offset:{other}" if other else "")]
        if signed:
            mutants.append(load.replace(f"load_{name}", f"load_u{name[1:]}"))
        for wrong in mutants:
            print(f"{name}: negative control {wrong}", flush=True)
            src.write_text(aot.lower(variant(lines, (load, wrong)), 32, **binding), encoding="ascii")
            subprocess.run(compile, check=True)
            bad = subprocess.run(run, capture_output=True, text=True)
            if bad.returncode != 1 or "word=1 expected=" not in bad.stderr:
                raise AssertionError(f"{name}: incorrect subword load escaped detection\n{bad.stderr}")
            print(bad.stderr, end="")
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"buffer_load_{name}\n" for name in MUBUF_SUBWORD), encoding="utf-8")
    return 0


MUBUF_RGBA16_UINT = "BUF_FMT_16_16_16_16_UINT"


def mubuf_format_d16_cases():
    for components in range(1, 5):
        count = (components + 1) // 2
        name = f"buffer_load_format_d16_{'xyzw'[:components]}"
        data = "v8" if count == 1 else "v[8:9]"
        store = "buffer_store_dword" if count == 1 else "buffer_store_dwordx2"
        last = 7 + count
        mask = [f"v_and_b32_e32 v{last}, 0xffff, v{last}"] if components % 2 else []
        # The index aliases the first load destination. The store has its own index.
        yield name, [
            "s_mov_b32 s12, s7", "s_mov_b32 s13, 0",
            "s_load_dwordx4 s[20:23], s[12:13], 0x30",
            "s_load_dwordx4 s[16:19], s[12:13], 0x40",
            "v_lshl_add_u32 v8, s11, 5, v0", "v_lshl_add_u32 v15, s11, 5, v0",
            "s_waitcnt lgkmcnt(0)", f"{name} {data}, v8, s[16:19], 0 idxen offset:8",
            "s_waitcnt vmcnt(0)", *mask, f"{store} {data}, v15, s[20:23], 0 idxen offset:4", "s_endpgm",
        ], DS_BINDING | {"stride": 4 * (count + 1), "source_descriptor": 0x40,
                          "source_stride": 16, "source_format": MUBUF_RGBA16_UINT}


def mubuf_format_d16_controls():
    cases = list(mubuf_format_d16_cases())
    _, lines, binding = cases[0]
    load = lines[7]
    for replacement, diagnostic in (
        (load.replace("idxen", "offen"), "Unsupported formatted buffer read target or address mode"),
        (load.replace(", 0 idxen", ", s0 idxen"), "Unsupported compute instruction"),
        (load + " tfe", "Unsupported compute instruction"),
        (load.replace("offset:8", "offset:4"), "Unverified read offset"),
        (load.replace("offset:8", "offset:16"), "Read leaves its source element"),
        (load.replace("s[16:19]", "s[20:23]"), "Reads from the stored UAV"),
        (load.replace("v8, v8", "v[8:9], v8"), "Unverified buffer read destination"),
    ):
        rejects(diagnostic, aot.lower, variant(lines, (load, replacement)), 32, **binding)
    for options, diagnostic in (
        ({"source_format": None}, "Formatted buffer reads require a source format"),
        ({"source_format": MUBUF_RGBA_UINT}, "Formatted buffer read does not match source format"),
        ({"source_format": "BUF_FMT_16_UINT"}, "Unsupported formatted source binding"),
        ({"source_stride": 12}, "Unsupported formatted source binding"),
    ):
        rejects(diagnostic, aot.lower, lines, 32, **(binding | options))
    rejects("Unverified buffer read descriptor", aot.lower,
            variant(lines, ("s_waitcnt lgkmcnt(0)", "")), 32, **binding)
    for _, lines, binding in cases:
        rejects("Vector load result used or overwritten before s_waitcnt vmcnt(0)", aot.lower,
                variant(lines, ("s_waitcnt vmcnt(0)", "")), 32, **binding)
    for _, lines, binding in (cases[0], cases[2]):
        mask = lines[9]
        rejects("Store data is not a known word", aot.lower, variant(lines, (mask, "")), 32, **binding)
        rejects("Unverified ALU operand", aot.lower, variant(lines, ("0xffff", "0x1ffff")), 32, **binding)
    _, lines, binding = cases[-1]
    rejects("Unverified buffer read destination", aot.lower,
            variant(lines, (lines[7], lines[7].replace("v[8:9]", "v[255:256]"))), 32, **binding)
    rejects("Vector load result used or overwritten before s_waitcnt vmcnt(0)", aot.lower,
            variant(lines, ("s_waitcnt vmcnt(0)", "v_mov_b32_e32 v9, 0\ns_waitcnt vmcnt(0)")), 32, **binding)


def mubuf_format_d16_draw(directory, compiler, renderer, backend):
    mubuf_format_d16_controls()
    src, cso, case = (directory / f"mubuf-format-d16.{ext}" for ext in ("hlsl", "cso", "case"))
    compile_shader = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
    run = [renderer, backend, "--compute-store-case", cso, case]
    data = struct.pack("<320I", *(source_word(i) for i in range(320)))
    accepted = []
    for name, lines, binding in mubuf_format_d16_cases():
        print(name, flush=True)
        components = len(name.rsplit("_", 1)[-1])
        count = (components + 1) // 2
        src.write_text(aot.lower(lines, 32, **binding), encoding="ascii")
        subprocess.run(compile_shader, check=True)
        for groups in (1, 2):
            words = [SENTINEL] * 320
            for gid in range(32 * groups):
                element = 16 * gid + 8
                texel = data[element:element + 2 * components]
                output = gid * (count + 1) + 1
                words[output:output + count] = [int.from_bytes(texel[i:i + 4], "little")
                                                for i in range(0, len(texel), 4)]
            case.write_bytes(struct.pack("<321I", groups, *words))
            result = subprocess.run(run)
            if result.returncode:
                return result.returncode
        # Selecting the preceding texel is a valid translation with a wrong immediate.
        changed = variant(lines, (" idxen offset:8", " idxen"))
        src.write_text(aot.lower(changed, 32, **binding), encoding="ascii")
        subprocess.run(compile_shader, check=True)
        result = subprocess.run(run, capture_output=True, text=True)
        actual = int.from_bytes(data[:min(components * 2, 4)], "little")
        if result.returncode != 1 or f"word=1 expected={words[1]:08x} actual={actual:08x}" not in result.stderr:
            raise AssertionError(f"{name}: wrong texel escaped detection\n{result.stderr}")
        print(result.stderr, end="")
        accepted.append(name)
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(accepted) + "\n", encoding="utf-8")
    return 0


def mubuf_vector_cases():
    for count in (2, 3, 4):
        name = f"buffer_load_dwordx{count}"
        data = f"v[7:{6 + count}]"
        # v8 is both the input index and a destination; v15 holds the separate output index.
        yield name, [
            "s_mov_b32 s12, s7", "s_mov_b32 s13, 0",
            "s_load_dwordx4 s[20:23], s[12:13], 0x30",
            "s_load_dwordx4 s[16:19], s[12:13], 0x40",
            "v_lshl_add_u32 v8, s11, 5, v0", "v_lshl_add_u32 v15, s11, 5, v0",
            "s_waitcnt lgkmcnt(0)", f"{name} {data}, v8, s[16:19], 0 idxen offset:4",
            "s_waitcnt vmcnt(0)", f"buffer_store_dwordx{count} {data}, v15, s[20:23], 0 idxen", "s_endpgm",
        ], DS_BINDING | {"stride": 4 * (count + 1), "source_descriptor": 0x40, "source_stride": 4 * (count + 1)}


def mubuf_vector_controls():
    for name, lines, binding in mubuf_vector_cases():
        count = int(name[-1])
        load = lines[7]
        data = f"v[7:{6 + count}]"
        for old, new, diagnostic in (
            (data, "v7", "Unverified buffer read destination"),
            (data, f"v[255:{254 + count}]", "Unverified buffer read destination"),
            (data, f"v[7:{7 + count}]", "Unverified buffer read destination"),
            ("idxen", "offen", "Unsupported vector buffer read target or address mode"),
            ("offset:4", "offset:2", "Unverified read offset"),
            ("offset:4", "offset:8", "Read leaves its source element"),
            ("offset:4", "offset:4 tfe", "Unsupported compute instruction"),
            ("s[16:19]", "s[20:23]", "Reads from the stored UAV"),
        ):
            rejects(diagnostic, aot.lower, variant(lines, (load, load.replace(old, new))), 32, **binding)
        for use in ("", f"v_mov_b32_e32 v16, v{6 + count}\ns_waitcnt vmcnt(0)",
                    f"v_mov_b32_e32 v{6 + count}, 0\ns_waitcnt vmcnt(0)"):
            rejects("before s_waitcnt vmcnt(0)", aot.lower, variant(lines, ("s_waitcnt vmcnt(0)", use)), 32, **binding)
        rejects("Unverified buffer read descriptor", aot.lower,
                variant(lines, ("s_waitcnt lgkmcnt(0)", "")), 32, **binding)
        rejects("Unsupported vector buffer read target", aot.lower, lines, 32, **(binding | {"cpu": "gfx1013"}))


def mubuf_vector_draw(directory, compiler, renderer, backend):
    mubuf_vector_controls()
    src, cso, case = (directory / f"mubuf-vector.{ext}" for ext in ("hlsl", "cso", "case"))
    compile = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
    run = [renderer, backend, "--compute-store-case", cso, case]
    for name, lines, binding in mubuf_vector_cases():
        count = int(name[-1])
        print(f"{name}: CPU reference", flush=True)
        src.write_text(aot.lower(lines, 32, **binding), encoding="ascii")
        subprocess.run(compile, check=True)
        for groups in (1, 2):
            words = [SENTINEL] * 320
            for gid in range(32 * groups):
                start = gid * (count + 1)
                words[start:start + count] = [source_word(start + 1 + i) for i in range(count)]
            case.write_bytes(struct.pack("<321I", groups, *words))
            result = subprocess.run(run)
            if result.returncode:
                return result.returncode
        print(f"{name}: swap first and last channels", flush=True)
        last = 6 + count
        swap = f"s_waitcnt vmcnt(0)\nv_mov_b32_e32 v16, v7\nv_mov_b32_e32 v7, v{last}\nv_mov_b32_e32 v{last}, v16"
        src.write_text(aot.lower(variant(lines, ("s_waitcnt vmcnt(0)", swap)), 32, **binding), encoding="ascii")
        subprocess.run(compile, check=True)
        bad = subprocess.run(run, capture_output=True, text=True)
        if bad.returncode != 1 or f"word=0 expected={source_word(1):08x} actual={source_word(count):08x}" not in bad.stderr:
            raise AssertionError(f"{name}: swapped channels escaped detection\n{bad.stderr}")
        print(bad.stderr, end="")
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{name}\n" for name, _, _ in mubuf_vector_cases()), encoding="utf-8")
    return 0


MUBUF_D16 = {
    "buffer_load_ubyte_d16": (1, False, 5, False),
    "buffer_load_ubyte_d16_hi": (1, False, 5, True),
    "buffer_load_sbyte_d16": (1, True, 4, False),
    "buffer_load_sbyte_d16_hi": (1, True, 4, True),
    "buffer_load_short_d16": (2, False, 6, False),
    "buffer_load_short_d16_hi": (2, False, 6, True),
}


def mubuf_d16_cases():
    for name, (_, _, offset, _) in MUBUF_D16.items():
        yield name, [
            "s_mov_b32 s12, s7", "s_mov_b32 s13, 0",
            "s_load_dwordx4 s[20:23], s[12:13], 0x30",
            "s_load_dwordx4 s[16:19], s[12:13], 0x40",
            "v_lshl_add_u32 v8, s11, 5, v0", "s_waitcnt lgkmcnt(0)",
            "buffer_load_dword v10, v8, s[16:19], 0 idxen", "s_waitcnt vmcnt(0)",
            f"{name} v10, v8, s[16:19], 0 idxen offset:{offset}", "s_waitcnt vmcnt(0)",
            "buffer_store_dword v10, v8, s[20:23], 0 idxen offset:4", "s_endpgm",
        ], DS_BINDING | {"stride": 8, "source_descriptor": 0x40, "source_stride": 8}


def mubuf_d16_controls():
    _, lines, binding = next(mubuf_d16_cases())
    load = lines[8]
    rejects("Unverified D16 buffer read destination input", aot.lower, lines[:6] + lines[8:], 32, **binding)
    rejects("Unverified D16 buffer read destination input", aot.lower, variant(lines, (lines[6],
            "tbuffer_load_format_d16_x v10, v8, s[16:19], 0 format:[BUF_FMT_16_UINT] idxen")), 32, **binding)
    for missing in (7, 9):
        rejects("before s_waitcnt vmcnt(0)", aot.lower, lines[:missing] + lines[missing + 1:], 32, **binding)
    rejects("before s_waitcnt vmcnt(0)", aot.lower,
            lines[:9] + ["v_mov_b32_e32 v10, 0"] + lines[9:], 32, **binding)
    rejects("Unverified buffer read descriptor", aot.lower, lines[:5] + lines[6:], 32, **binding)
    for new, diagnostic in (
        (load + " tfe", "Unsupported compute instruction"),
        (load.replace("idxen", "offen"), "Unsupported subword buffer read target or address mode"),
        (load.replace("offset:5", "offset:8"), "Read leaves its source element"),
        (load.replace("ubyte_d16", "short_d16"), "Unverified read offset"),
        (load.replace("s[16:19]", "s[20:23]"), "Reads from the stored UAV"),
        (load.replace("v10,", "v[10:11],"), "Unverified buffer read destination"),
    ):
        rejects(diagnostic, aot.lower, variant(lines, (load, new)), 32, **binding)
    rejects("Unsupported subword buffer read target", aot.lower, lines, 32, **(binding | {"cpu": "gfx1013"}))
    rejects("D16 buffer read expression is too large", aot.lower, lines[:8] + lines[8:10] * 32 + lines[10:], 32, **binding)


def mubuf_d16_draw(directory, compiler, renderer, backend):
    mubuf_d16_controls()
    src, cso, case = (directory / f"mubuf-d16.{ext}" for ext in ("hlsl", "cso", "case"))
    compile = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
    run = [renderer, backend, "--compute-store-case", cso, case]
    data = struct.pack("<320I", *(source_word(i) for i in range(320)))
    for name, lines, binding in mubuf_d16_cases():
        width, signed, offset, high = MUBUF_D16[name]
        print(f"{name}: CPU reference", flush=True)
        src.write_text(aot.lower(lines, 32, **binding), encoding="ascii")
        subprocess.run(compile, check=True)
        for groups in (1, 3):
            words = [SENTINEL] * 320
            for gid in range(32 * groups):
                start = gid * 8
                old = data[start:start + 4]
                field = int.from_bytes(data[start + offset:start + offset + width], "little", signed=signed)
                field = field.to_bytes(2, "little", signed=signed)
                words[2 * gid + 1] = int.from_bytes(old[:2] + field if high else field + old[2:], "little")
            case.write_bytes(struct.pack("<321I", groups, *words))
            result = subprocess.run(run)
            if result.returncode:
                return result.returncode
        load = lines[8]
        written = 0xffff0000 if high else 0x0000ffff
        controls = [("clear preserved half", f"v_and_b32_e32 v10, {written:#x}, v10\n{load}", words[1] & written)]
        if signed:
            controls.append(("zero-extend signed byte", load.replace("_sbyte_", "_ubyte_"),
                             words[1] & ~(0xff000000 if high else 0x0000ff00)))
        for label, wrong, actual in controls:
            print(f"{name}: {label}", flush=True)
            src.write_text(aot.lower(variant(lines, (load, wrong)), 32, **binding), encoding="ascii")
            subprocess.run(compile, check=True)
            bad = subprocess.run(run, capture_output=True, text=True)
            if bad.returncode != 1 or f"word=1 expected={words[1]:08x} actual={actual:08x}" not in bad.stderr:
                raise AssertionError(f"{name}: {label} escaped detection\n{bad.stderr}")
            print(bad.stderr, end="")
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        # The preserved half comes from an executed DWORD load in every D16 case.
        path.write_text("".join(f"{name}\n" for name in ("buffer_load_dword", *MUBUF_D16)), encoding="utf-8")
    return 0


MUBUF_RGBA_UINT = "BUF_FMT_32_32_32_32_UINT"


def mubuf_format_cases():
    for count in range(1, 5):
        channels = "xyzw"[:count]
        name = f"buffer_load_format_{channels}"
        last = 7 + count
        data = "v8" if count == 1 else f"v[8:{last}]"
        store = "buffer_store_dword" if count == 1 else f"buffer_store_dwordx{count}"
        # v8 is the source index and first result. v15 keeps the output index.
        yield name, [
            "s_mov_b32 s12, s7", "s_mov_b32 s13, 0",
            "s_load_dwordx4 s[20:23], s[12:13], 0x30",
            "s_load_dwordx4 s[16:19], s[12:13], 0x40",
            "v_lshl_add_u32 v8, s11, 5, v0", "v_lshl_add_u32 v15, s11, 5, v0",
            "s_waitcnt lgkmcnt(0)", f"{name} {data}, v8, s[16:19], 0 idxen",
            "s_waitcnt vmcnt(0)", f"{store} {data}, v15, s[20:23], 0 idxen", "s_endpgm",
        ], DS_BINDING | {"stride": 4 * (count + 1), "source_descriptor": 0x40,
                          "source_stride": 16, "source_format": MUBUF_RGBA_UINT}


def mubuf_format_controls():
    cases = list(mubuf_format_cases())
    _, lines, binding = cases[0]
    load = lines[7]
    for replacement, diagnostic in (
        (load.replace("idxen", "offen"), "Unsupported formatted buffer read target or address mode"),
        (load + " tfe", "Unsupported compute instruction"),
        (load.replace(", 0 idxen", ", s0 idxen"), "Unsupported compute instruction"),
        (load + " offset:4", "Unverified read offset"),
        (load + " offset:16", "Read leaves its source element"),
        (load.replace("v8, v8", "v[8:9], v8"), "Unverified buffer read destination"),
        (load.replace("s[16:19]", "s[20:23]"), "Reads from the stored UAV"),
        (load.replace("buffer_load_format_x", "buffer_load_format_d16_x"), "Formatted buffer read does not match source format"),
    ):
        rejects(diagnostic, aot.lower, variant(lines, (load, replacement)), 32, **binding)
    for options, diagnostic in (
        ({"source_format": None}, "Formatted buffer reads require a source format"),
        ({"source_format": "BUF_FMT_32_FLOAT"}, "Unsupported formatted source binding"),
        ({"source_stride": 20}, "Unsupported formatted source binding"),
        ({"source_descriptor": None, "source_stride": None}, "Unsupported formatted source binding"),
        ({"cpu": "gfx1013"}, "Unsupported formatted source binding"),
    ):
        rejects(diagnostic, aot.lower, lines, 32, **(binding | options))
    rejects("Unverified buffer read descriptor", aot.lower,
            variant(lines, ("s_waitcnt lgkmcnt(0)", "")), 32, **binding)
    rejects("Vector load result used or overwritten before s_waitcnt vmcnt(0)", aot.lower,
            variant(lines, ("s_waitcnt vmcnt(0)", "")), 32, **binding)

    _, lines, binding = cases[-1]
    for use in ("v_mov_b32_e32 v16, v11", "v_mov_b32_e32 v11, 0"):
        rejects("Vector load result used or overwritten before s_waitcnt vmcnt(0)", aot.lower,
                variant(lines, ("s_waitcnt vmcnt(0)", use + "\ns_waitcnt vmcnt(0)")), 32, **binding)


def mubuf_format_draw(directory, compiler, renderer, backend):
    src, cso, case = (directory / f"mubuf-format.{ext}" for ext in ("hlsl", "cso", "case"))
    compile_shader = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
    run = [renderer, backend, "--compute-store-case", cso, case]
    for name, lines, binding in mubuf_format_cases():
        count = len(name.rsplit("_", 1)[1])
        print(f"{name}: CPU reference", flush=True)
        src.write_text(aot.lower(lines, 32, **binding), encoding="ascii")
        subprocess.run(compile_shader, check=True)
        for groups in (1, 2):
            words = [SENTINEL] * 320
            for gid in range(32 * groups):
                start = gid * (count + 1)
                words[start:start + count] = [source_word(gid * 4 + i) for i in range(count)]
            case.write_bytes(struct.pack("<321I", groups, *words))
            result = subprocess.run(run)
            if result.returncode:
                return result.returncode
        if count == 1:
            changed = variant(lines, (lines[7], "buffer_load_dword v8, v8, s[16:19], 0 idxen offset:4"))
            actual = source_word(1)
        else:
            last = 7 + count
            swap = ["s_waitcnt vmcnt(0)", "v_mov_b32_e32 v16, v8",
                    f"v_mov_b32_e32 v8, v{last}", f"v_mov_b32_e32 v{last}, v16"]
            changed = variant(lines, ("s_waitcnt vmcnt(0)", "\n".join(swap)))
            actual = source_word(count - 1)
        print(f"{name}: select the wrong first channel", flush=True)
        src.write_text(aot.lower(changed, 32, **binding), encoding="ascii")
        subprocess.run(compile_shader, check=True)
        result = subprocess.run(run, capture_output=True, text=True)
        if result.returncode != 1 or f"word=0 expected={words[0]:08x} actual={actual:08x}" not in result.stderr:
            raise AssertionError(f"{name}: wrong channel escaped detection\n{result.stderr}")
        print(result.stderr, end="")
    mubuf_format_controls()
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{name}\n" for name, _, _ in mubuf_format_cases()), encoding="utf-8")
    return 0


MUBUF_STORE_WORDS = (0x89abcdef, 0x01234567, 0xdcbafed0, 0x76543210)


def mubuf_format_store_cases(packed=False):
    for components in ((1, 2, 4) if packed else range(1, 5)):
        name = f"buffer_store_format_{'d16_' if packed else ''}{'xyzw'[:components]}"
        count = (components + 1) // 2 if packed else components
        component_format = "16_" if packed else "32_"
        data = "v10" if count == 1 else f"v[10:{9 + count}]"
        lines = ["s_mov_b32 s12, s7", "s_mov_b32 s13, 0",
                 "s_load_dwordx4 s[20:23], s[12:13], 0x30",
                 "v_lshl_add_u32 v8, s11, 4, v0", "s_waitcnt lgkmcnt(0)"]
        lines += [f"v_xor_b32_e32 v{10 + i}, {word:#x}, v8" for i, word in enumerate(MUBUF_STORE_WORDS[:count])]
        yield name, [*lines, f"{name} {data}, v8, s[20:23], 0 idxen offset:4", "s_endpgm"], DS_BINDING | {
            "stride": 4 * (count + 1), "output_format": f"BUF_FMT_{component_format * components}UINT"}


def mubuf_format_store_controls():
    cases = list(mubuf_format_store_cases())
    _, lines, binding = cases[0]
    store = lines[-2]
    for replacement, diagnostic in (
        (store.replace("idxen", "offen"), "Unsupported formatted buffer store target or address mode"),
        (store.replace("idxen", "idxen offen"), "Unsupported formatted buffer store target or address mode"),
        (store.replace("format_x", "format_d16_x"), "Formatted buffer store width does not match output format"),
        (store.replace(", 0 idxen", ", s0 idxen"), "Unsupported compute instruction"),
        (store + " glc", "Unsupported compute instruction"),
        (store.replace("offset:4", "offset:2"), "Unverified raw UAV store"),
        (store.replace("offset:4", "offset:8"), "Unsupported store address"),
        (store.replace(", v8,", ", v10,"), "Unsupported store address"),
        (store.replace("s[20:23]", "s[16:19]"), "Unverified raw UAV store"),
        (store + "\nbuffer_store_dword v10, v8, s[20:23], 0 idxen offset:4", "Overlapping raw UAV stores"),
    ):
        rejects(diagnostic, aot.lower, variant(lines, (store, replacement)), 16, **binding)
    for options, diagnostic in (
        ({"output_format": None}, "Formatted buffer stores require an output format"),
        ({"output_format": "BUF_FMT_32_32_UINT"}, "Formatted buffer store width does not match output format"),
        ({"output_format": "BUF_FMT_32_FLOAT"}, "Unsupported formatted output binding"),
        ({"stride": None}, "Unsupported formatted output binding"),
        ({"cpu": "gfx1013"}, "Unsupported formatted output binding"),
    ):
        rejects(diagnostic, aot.lower, lines, 16, **(binding | options))
    rejects("Unverified raw UAV store", aot.lower, variant(lines, ("s_waitcnt lgkmcnt(0)", "")), 16, **binding)
    rejects("Store address does not separate every invocation", aot.lower, lines, (16, 2, 1), **binding)

    _, lines, binding = cases[-1]
    store = lines[-2]
    for data in ("v[10:12]", "v[253:256]"):
        rejects("Unverified raw UAV store", aot.lower,
                variant(lines, ("v[10:13]", data)), 16, **binding)
    # The last data VGPR is still pending even if the other three are ready.
    lines = variant(lines, ("s_waitcnt lgkmcnt(0)",
                            "s_load_dwordx4 s[16:19], s[12:13], 0x40\ns_waitcnt lgkmcnt(0)"),
                    (store, "buffer_load_dword v13, v8, s[16:19], 0 idxen\n" + store))
    binding |= {"source_descriptor": 0x40, "source_stride": 4}
    rejects("Vector load result used or overwritten before s_waitcnt vmcnt(0)", aot.lower, lines, 16, **binding)
    aot.lower(variant(lines, (store, "s_waitcnt vmcnt(0)\n" + store)), 16, **binding)


def mubuf_format_d16_store_controls():
    cases = list(mubuf_format_store_cases(packed=True))
    _, lines, binding = cases[0]
    store = lines[-2]
    for replacement, diagnostic in (
        (store.replace("idxen", "offen"), "Unsupported formatted buffer store target or address mode"),
        (store.replace("offset:4", "offset:6"), "Unverified raw UAV store"),
        (store.replace("offset:4", "offset:8"), "Unsupported store address"),
        (store + "\nbuffer_store_short v10, v8, s[20:23], 0 idxen offset:6", "Overlapping raw UAV stores"),
    ):
        rejects(diagnostic, aot.lower, variant(lines, (store, replacement)), 16, **binding)
    rejects("Formatted buffer store width does not match output format", aot.lower, lines, 16,
            **(binding | {"output_format": "BUF_FMT_16_16_UINT"}))
    rejects("Unverified raw UAV store", aot.lower, variant(lines, ("s_waitcnt lgkmcnt(0)", "")), 16, **binding)

    _, lines, binding = cases[-1]
    rejects("Formatted D16 buffer store requires a matching whole-texel format", aot.lower,
            variant(lines, ("buffer_store_format_d16_xyzw", "buffer_store_format_d16_xyz")), 16, **binding)
    rejects("Unverified raw UAV store", aot.lower, variant(lines, ("v[10:11]", "v10")), 16, **binding)


def mubuf_format_store_draw(directory, compiler, renderer, backend):
    mubuf_format_d16_store_controls()
    mubuf_format_store_controls()
    src, cso, case = (directory / f"mubuf-format-store.{ext}" for ext in ("hlsl", "cso", "case"))
    compile_shader = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
    run = [renderer, backend, "--compute-store-case", cso, case]
    accepted = []
    for name, lines, binding in (*mubuf_format_store_cases(), *mubuf_format_store_cases(packed=True)):
        print(f"{name} ({binding['output_format']})", flush=True)
        components = len(name.rsplit("_", 1)[1])
        packed = "_d16_" in name
        count = (components + 1) // 2 if packed else components
        store = lines[-2]
        source = aot.lower(lines, 16, **binding)
        src.write_text(source, encoding="ascii")
        subprocess.run(compile_shader, check=True)
        for groups in (1, 2):
            words = [SENTINEL] * 320
            for gid in range(16 * groups):
                start = gid * (count + 1) + 1
                if packed:
                    payload = struct.pack(f"<{count}I", *(word ^ gid for word in MUBUF_STORE_WORDS[:count]))
                    reference = bytearray(SENTINEL.to_bytes(4, "little") * count)
                    reference[:2 * components] = payload[:2 * components]
                    words[start:start + count] = struct.unpack(f"<{count}I", reference)
                else:
                    words[start:start + count] = [word ^ gid for word in MUBUF_STORE_WORDS[:count]]
            case.write_bytes(struct.pack("<321I", groups, *words))
            result = subprocess.run(run)
            if result.returncode:
                return result.returncode
        if packed and components == 1:
            changed = source.replace("& 0xffff0000u", "& 0u")
            mismatch, actual = 1, MUBUF_STORE_WORDS[0] & 0xffff
        elif packed:
            swapped = "v_lshlrev_b32_e32 v22, 16, v10\nv_lshrrev_b32_e32 v23, 16, v10\nv_or_b32_e32 v10, v22, v23"
            changed = aot.lower(variant(lines, (store, swapped + "\n" + store)), 16, **binding)
            mismatch, actual = 1, 0xcdef89ab
        elif count == 1:
            changed = aot.lower(variant(lines, (store, store.removesuffix(" offset:4"))), 16, **binding)
            mismatch, actual = 0, MUBUF_STORE_WORDS[0]
        else:
            first, last = lines[5], lines[4 + count]
            mutant = variant(lines, (first, first.replace("v10,", f"v{9 + count},")),
                             (last, last.replace(f"v{9 + count},", "v10,")))
            changed = aot.lower(mutant, 16, **binding)
            mismatch, actual = 1, MUBUF_STORE_WORDS[count - 1]
        assert changed != source
        src.write_text(changed, encoding="ascii")
        subprocess.run(compile_shader, check=True)
        bad = subprocess.run(run, capture_output=True, text=True)
        if bad.returncode != 1 or f"word={mismatch} expected={words[mismatch]:08x} actual={actual:08x}" not in bad.stderr:
            raise AssertionError(f"{name}: incorrect store escaped detection\n{bad.stderr}")
        print(bad.stderr, end="")
        accepted.append(name)
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(accepted) + "\n", encoding="utf-8")
    return 0


MTBUF_LOAD = "tbuffer_load_format_x v10, v8, s[16:19], 0 format:[BUF_FMT_32_UINT] idxen offset:4"
MTBUF_LOAD_CASES = [("mtbuf_load_x", [
    "s_mov_b32 s12, s7", "s_mov_b32 s13, 0",
    "s_load_dwordx4 s[20:23], s[12:13], 0x30",
    "s_load_dwordx4 s[16:19], s[12:13], 0x40",
    "v_lshl_add_u32 v8, s11, 5, v0", "s_waitcnt lgkmcnt(0)",
    MTBUF_LOAD, "s_waitcnt vmcnt(0)",
    "buffer_store_dword v10, v8, s[20:23], 0 idxen offset:4", "s_endpgm",
], DS_BINDING | {"stride": 8, "source_descriptor": 0x40, "source_stride": 12})]

for count in range(2, 5):
    channels = "xyzw"[:count]
    data = f"v[10:{9 + count}]"
    load = f"tbuffer_load_format_{channels} {data}, v8, s[16:19], 0 format:[BUF_FMT_{'32_' * count}UINT] idxen offset:4"
    store = f"buffer_store_dwordx{count} {data}, v8, s[20:23], 0 idxen offset:4"
    _, single, binding = MTBUF_LOAD_CASES[0]
    stride = 4 * (count + 1)
    MTBUF_LOAD_CASES.append((f"mtbuf_load_{channels}", [*single[:6], load, single[7], store, single[-1]],
                             binding | {"stride": stride, "source_stride": stride}))
for components in range(1, 5):
    channels = "xyzw"[:components]
    count = (components + 1) // 2
    data = "v10" if count == 1 else "v[10:11]"
    fmt = f"BUF_FMT_{'16_' * (4 if components == 3 else components)}UINT"
    load = (f"tbuffer_load_format_d16_{channels} {data}, v8, s[16:19], 0 "
            f"format:[{fmt}] idxen offset:4")
    _, single, binding = MTBUF_LOAD_CASES[0]
    store = single[8] if count == 1 else "buffer_store_dwordx2 v[10:11], v8, s[20:23], 0 idxen offset:4"
    last = 9 + count
    mask = [f"v_and_b32_e32 v{last}, 0xffff, v{last}"] if components % 2 else []
    MTBUF_LOAD_CASES.append((f"mtbuf_load_d16_{channels}", [*single[:6], load, single[7], *mask, store, single[-1]],
                             binding | {"stride": 4 * (count + 1), "source_stride": 12}))


def mtbuf_load_controls():
    _, lines, binding = MTBUF_LOAD_CASES[0]
    for old, new, diagnostic in (
        ("BUF_FMT_32_UINT", "BUF_FMT_32_FLOAT", "Unsupported compute instruction"),
        (" format:[BUF_FMT_32_UINT]", "", "Unsupported compute instruction"),
        (" offset:4", " offset:4 glc", "Unsupported compute instruction"),
        ("], 0 format:", "], s13 format:", "Unsupported compute instruction"),
        ("idxen offset:4", "offen offset:4", "Unsupported typed buffer read target or address mode"),
        ("v10, v8", "v10, v0", "Unsupported read address"),
        ("s[16:19]", "s[20:23]", "Reads from the stored UAV"),
        ("offset:4", "offset:2", "Unverified read offset"),
        ("offset:4", "offset:12", "Read leaves its source element"),
    ):
        rejects(diagnostic, aot.lower, [line.replace(old, new) if line == MTBUF_LOAD else line for line in lines],
                32, **binding)
    rejects("before s_waitcnt vmcnt(0)", aot.lower, variant(lines, ("s_waitcnt vmcnt(0)", "")), 32, **binding)
    rejects("Unverified buffer read descriptor", aot.lower,
            variant(lines, ("s_waitcnt lgkmcnt(0)", "")), 32, **binding)
    rejects("Unsupported read address", aot.lower, lines, 32, **(binding | {"source_stride": None}))
    rejects("Unsupported typed buffer read target", aot.lower, lines, 32, **(binding | {"cpu": "gfx1013"}))
    for _, lines, binding in MTBUF_LOAD_CASES[1:4]:
        load = lines[6]
        rejects("Unverified buffer read destination", aot.lower,
                variant(lines, (load, load.replace(load.split()[1], "v10,"))), 32, **binding)
        rejects("Unsupported typed buffer read format", aot.lower,
                variant(lines, (load, load.replace(load.split("format:[")[1].split("]")[0], "BUF_FMT_32_UINT"))),
                32, **binding)
        rejects("before s_waitcnt vmcnt(0)", aot.lower,
                variant(lines, (load, load + "\nv_mov_b32_e32 v12, v11")), 32, **binding)
        rejects("Read leaves its source element", aot.lower, lines, 32,
                **(binding | {"source_stride": binding["source_stride"] - 4}))
    for _, lines, binding in MTBUF_LOAD_CASES[4:]:
        load = lines[6]
        fmt = load.split("format:[")[1].split("]")[0]
        rejects("Unsupported typed buffer read format", aot.lower,
                variant(lines, (fmt, "BUF_FMT_32_UINT")), 32, **binding)
        rejects("before s_waitcnt vmcnt(0)", aot.lower, variant(lines, ("s_waitcnt vmcnt(0)", "")), 32, **binding)
        rejects("Unverified read offset", aot.lower,
                variant(lines, (load, load.replace("offset:4", "offset:2"))), 32, **binding)
        if load.split()[0].endswith(("_x", "_xyz")):
            mask = lines[8]
            rejects("Store data is not a known word", aot.lower, variant(lines, (mask, "")), 32, **binding)
            rejects("Unverified ALU operand", aot.lower,
                    variant(lines, (mask, mask.replace("0xffff", "0x1ffff"))), 32, **binding)


def mtbuf_load_draw(directory, compiler, renderer, backend):
    mtbuf_load_controls()
    accepted = []
    for name, lines, binding in MTBUF_LOAD_CASES:
        load = lines[6]
        op = load.split()[0]
        packed = "_d16_" in op
        components = len(op.rsplit("_", 1)[-1])
        count = (components + 1) // 2 if packed else components
        src, cso, case = (directory / f"{name}.{ext}" for ext in ("hlsl", "cso", "case"))
        src.write_text(aot.lower(lines, 32, **binding), encoding="ascii")
        compile = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
        subprocess.run(compile, check=True)
        run = [renderer, backend, "--compute-store-case", cso, case]
        for groups in ((1, 3) if count == 1 else (1, 2)):
            words = [SENTINEL] * 320
            for gid in range(32 * groups):
                output = gid * (count + 1) + 1
                first = gid * (3 if count == 1 or packed else count + 1) + 1
                words[output:output + count] = [source_word(first + i) for i in range(count)]
                if packed and components % 2:
                    words[output + count - 1] &= 0xffff
            case.write_bytes(struct.pack("<321I", groups, *words))
            result = subprocess.run(run)
            if result.returncode:
                return result.returncode
        if packed:
            swapped = "v_lshlrev_b32_e32 v22, 16, v10\nv_lshrrev_b32_e32 v23, 16, v10\nv_or_b32_e32 v10, v22, v23"
            store = lines[-2]
            mutant = aot.lower(variant(lines, (store, swapped + "\n" + store)), 32, **binding)
            mismatch = 1
        elif count == 1:
            # Stride 8 still admits the aligned read. Only lanes after lane 0 expose the error.
            mutant = aot.lower(lines, 32, **(binding | {"source_stride": 8}))
            mismatch = 3
        else:
            mutant = aot.lower(variant(lines, (load, load.removesuffix(" offset:4"))), 32, **binding)
            mismatch = 1
        src.write_text(mutant, encoding="ascii")
        subprocess.run(compile, check=True)
        bad = subprocess.run(run, capture_output=True, text=True)
        if bad.returncode != 1 or f"word={mismatch} expected=" not in bad.stderr:
            raise AssertionError(f"{name}: incorrect load escaped detection\n{bad.stderr}")
        print(bad.stderr, end="")
        accepted.append(op)
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(accepted) + "\n", encoding="utf-8")
    return 0


# The translated image load reads a caller-supplied R32G32B32A32_UINT texture and writes the
# selected texel components into the output UAV. The case file appends the 32x8x4 texture words
# after the 320 expected readback words; the runner uploads them verbatim.
TEXTURE_WIDTH, TEXTURE_HEIGHT, TEXTURE_CHANNELS = 32, 8, 4
TEXTURE_WORDS = [[(((channel + 1) << 28) | (y << 12) | (x << 4) | channel)
                  for channel in range(TEXTURE_CHANNELS)]
                 for y in range(TEXTURE_HEIGHT) for x in range(TEXTURE_WIDTH)]
MIMG_BINDING = DS_BINDING | {"group_reg": 10, "group_reg_y": 11, "stride": 128,
                             "image_descriptor": 0x40, "image_format": "R32G32B32A32_UINT"}
MIMG_IMAGE = "image_load v[14:17], v[8:9], s[24:31] dmask:0xf dim:SQ_RSRC_IMG_2D"


def texture_word(x, y, channel):
    return TEXTURE_WORDS[y * TEXTURE_WIDTH + x][channel]


# Literal channel orders keep the CPU expectation independent of the lowerer's bit walk.
MIMG_CHANNELS = {
    0x1: (0,), 0x2: (1,), 0x3: (0, 1), 0x4: (2,),
    0x5: (0, 2), 0x6: (1, 2), 0x7: (0, 1, 2), 0x8: (3,),
    0x9: (0, 3), 0xa: (1, 3), 0xb: (0, 1, 3), 0xc: (2, 3),
    0xd: (0, 2, 3), 0xe: (1, 2, 3), 0xf: (0, 1, 2, 3),
}
MIMG_PREFIX_MASKS = (0x1, 0x3, 0x7, 0xf)


def mimg_load_cases(a16=False, *, mip=False):
    """All nonzero masks on one descriptor, with a dispatch size per result width."""
    for mask, channels in MIMG_CHANNELS.items():
        count = len(channels)
        groups, shift = ((4, 2), (2, 3), (1, 4), (1, 4))[count - 1]
        data = "v14" if count == 1 else f"v[14:{13 + count}]"
        store = "buffer_store_dword" if count == 1 else f"buffer_store_dwordx{count}"
        lines = [
            "s_mov_b32 s12, s7", "s_mov_b32 s13, 0",
            "s_load_dwordx4 s[20:23], s[12:13], 0x30",
            "s_load_dwordx8 s[24:31], s[12:13], 0x40",
            "v_lshl_add_u32 v8, s10, 3, v0",
            "v_lshl_add_u32 v9, s11, 3, v1",
        ]
        if mip:
            # Use all three levels within a dispatch. X/4 and Y/4 fit the smallest mip.
            # Preserve the original X/Y for UAV addressing. The a16 LOD high half is padding.
            lines += ["v_lshrrev_b32_e32 v16, 2, v8", "v_lshrrev_b32_e32 v17, 2, v9",
                      "v_min_u32_e32 v18, 2, v9"]
            if a16:
                lines += ["v_mad_u32_u24 v17, v17, 0x10000, v16",
                          "v_or_b32_e32 v18, 0xabcd0000, v18"]
            addr = "v[17:18]" if a16 else "v[16:18]"
        elif a16:
            lines.append("v_mad_u32_u24 v10, v9, 0x10000, v8")
            addr = "v10"
        else:
            addr = "v[8:9]"
        lines.append("s_waitcnt lgkmcnt(0)")
        op = "image_load_mip" if mip else "image_load"
        flags = " unorm a16" if a16 else ""
        lines.append(f"{op} {data}, {addr}, s[24:31] dmask:0x{mask:x} dim:SQ_RSRC_IMG_2D{flags}")
        lines += [
            "v_mov_b32_e32 v12, v9",
            f"v_lshlrev_b32_e32 v13, {shift}, v8",
            "s_waitcnt vmcnt(0)",
            f"{store} {data}, v[12:13], s[20:23], 0 idxen offen",
            "s_endpgm",
        ]
        yield mask, channels, groups, shift, lines


def mimg_mip_texels():
    """Additional subresources in mip order. Distinct level, channel, row and column bits."""
    return [((channel + 1) << 28) | (level << 24) | (y << 12) | (x << 4) | channel
            for level in (1, 2) for y in range(TEXTURE_HEIGHT >> level)
            for x in range(TEXTURE_WIDTH >> level) for channel in range(TEXTURE_CHANNELS)]


def mimg_load_words(channels, shift, groups, *, mip=False):
    """Independent readback: lane (tid.x, tid.y) stores its texel at tid.y*32 + tid.x*step."""
    step = 1 << (shift - 2)
    words = [SENTINEL] * 320
    for y in range(TEXTURE_HEIGHT):
        for x in range(8 * groups):
            base = y * 32 + x * step
            for index, channel in enumerate(channels):
                if mip:
                    level = min(y, 2)
                    words[base + index] = texture_word(x // 4, y // 4, channel) | (level << 24)
                else:
                    words[base + index] = texture_word(x, y, channel)
    return words


def mimg_ds_program():
    lines = ds_program("ds_bpermute_b32", 5)
    lines.insert(lines.index("s_waitcnt lgkmcnt(0)"), "s_load_dwordx8 s[24:31], s[12:13], 0x40")
    return variant(lines, ("ds_bpermute_b32 v5, v5, v8", "v_mov_b32_e32 v9, 0\n"
                           "image_load v14, v[8:9], s[24:31] dmask:0x1 dim:SQ_RSRC_IMG_2D\n"
                           "s_waitcnt vmcnt(0)\nds_bpermute_b32 v5, v5, v14"))


def mimg_load_controls():
    _, _, _, _, lines = next(mimg_load_cases())
    binding = MIMG_BINDING
    image = next(line for line in lines if line.startswith("image_load"))

    def changed(old, new):
        return [line.replace(old, new) if line == image else line for line in lines]

    for old, new, diagnostic in (
        ("dmask:0x1", "dmask:0x3", "Unsupported image load target"),
        ("dmask:0x1", "dmask:0x0", "Unsupported image load target"),
        ("dmask:0x1", "dmask:0x10", "Unsupported image load target"),
        ("dmask:0x1", "dmask:0xa", "Unsupported image load target"),
        ("dim:SQ_RSRC_IMG_2D", "dim:SQ_RSRC_IMG_1D", "Unsupported compute instruction"),
        ("dim:SQ_RSRC_IMG_2D", "dim:SQ_RSRC_IMG_2D a16", "Unsupported image load target"),
        ("dim:SQ_RSRC_IMG_2D", "dim:SQ_RSRC_IMG_2D d16", "Unsupported compute instruction"),
        ("dim:SQ_RSRC_IMG_2D", "dim:SQ_RSRC_IMG_2D glc", "Unsupported compute instruction"),
        ("s[24:31]", "s[20:27]", "Unverified image descriptor"),
        ("v[8:9]", "v[6:7]", "Unverified image coordinates"),
    ):
        rejects(diagnostic, aot.lower, changed(old, new), (8, 8, 1), **binding)
    # LLVM 15 encodes non-sampler image loads with the unorm flag; it selects integer,
    # un-normalized addresses, which is what this lowering already assumes.
    assert aot.lower(changed(" dim:SQ_RSRC_IMG_2D", " dim:SQ_RSRC_IMG_2D unorm"), (8, 8, 1),
                     **binding) == aot.lower(lines, (8, 8, 1), **binding)
    rejects("Unresolved image descriptor", aot.lower,
            [line for line in lines if not line.startswith("s_waitcnt lgkmcnt")], (8, 8, 1), **binding)
    rejects("before s_waitcnt vmcnt(0)", aot.lower,
            variant(lines, ("s_waitcnt vmcnt(0)", "")), (8, 8, 1), **binding)
    rejects("Unverified image descriptor", aot.lower,
            [line for line in lines if not line.startswith("s_load_dwordx8")], (8, 8, 1), **binding)
    rejects("Invalid or overlapping image descriptor binding", aot.lower, lines, (8, 8, 1),
            **(binding | {"image_descriptor": 0x30}))
    rejects("Invalid or overlapping image descriptor binding", aot.lower, lines, (8, 8, 1),
            **(binding | {"image_descriptor": 0x28}))
    rejects("Invalid or overlapping image descriptor binding", aot.lower, lines, (8, 8, 1),
            **(binding | {"image_format": None}))
    rejects("Unsupported image load target", aot.lower, lines, (8, 8, 1),
            **(binding | {"cpu": "gfx1013"}))
    rejects("Unsupported image load target", aot.lower,
            [line for line in lines if not line.startswith("s_load_dwordx8")], (8, 8, 1),
            **{key: value for key, value in binding.items() if key not in ("image_descriptor", "image_format")})
    rejects("Unresolved compute descriptor load", aot.lower, lines, (8, 8, 1),
            **{key: value for key, value in binding.items() if key not in ("image_descriptor", "image_format")})
    # A lane route can consume a texel after vmcnt; the image temporary must be declared first.
    mixed = aot.lower(mimg_ds_program(), 32, **(DS_BINDING | {"image_descriptor": 0x40,
                                                            "image_format": "R32G32B32A32_UINT"}))
    assert mixed.index("uint4 image_load_0 =") < mixed.index("= image_load_0.x;")


def mimg_a16_controls():
    _, _, _, _, lines = next(mimg_load_cases(a16=True))
    image = next(line for line in lines if line.startswith("image_load"))

    def changed(old, new):
        return [line.replace(old, new) if line == image else line for line in lines]

    # The packed address is required; a two-dword address with the a16 flag, the NSA register list
    # and an unverified packed register are each refused.
    rejects("Unsupported image load target", aot.lower, changed(" unorm a16", ""),
            (8, 8, 1), **MIMG_BINDING)
    rejects("Unsupported compute instruction", aot.lower, changed(" unorm a16", " d16"),
            (8, 8, 1), **MIMG_BINDING)
    rejects("Unsupported image load target", aot.lower, changed("v10,", "v[8:9],"),
            (8, 8, 1), **MIMG_BINDING)
    rejects("Unsupported compute instruction", aot.lower, changed("v10,", "[v9, v8],"),
            (8, 8, 1), **MIMG_BINDING)
    rejects("Unverified image coordinates", aot.lower, changed("v10,", "v30,"),
            (8, 8, 1), **MIMG_BINDING)


def mimg_mip_controls():
    for a16 in (False, True):
        _, _, _, _, lines = next(mimg_load_cases(a16, mip=True))
        image = next(line for line in lines if line.startswith("image_load_mip"))
        addr = "v[17:18]" if a16 else "v[16:18]"
        for original, replacement, diagnostic in (
            (addr, "v[16:18]" if a16 else "v[17:18]", "Unsupported image load target"),
            (addr, "v[17:19]" if not a16 else "v[18:19]", "Unverified image coordinates"),
            (addr, "[v16,v17,v18]", "Unsupported compute instruction"),
            (image, image + " tfe", "Unsupported compute instruction"),
            ("s_waitcnt vmcnt(0)", "", "before s_waitcnt vmcnt(0)"),
        ):
            rejects(diagnostic, aot.lower, variant(lines, (original, replacement)), (8, 8, 1), **MIMG_BINDING)


MIMG_PACKED_BINDING = MIMG_BINDING | {"image_format": "R8G8B8A8_UINT"}
MIMG_PACKED_OPS = ("image_load_pck", "image_load_pck_sgn", "image_load_mip_pck", "image_load_mip_pck_sgn")
MIMG_PACKED_CASE_FLAG = 1 << 24


def mimg_packed_cases():
    for op in MIMG_PACKED_OPS:
        mip, signed = "_mip" in op, op.endswith("_sgn")
        for a16 in (False, True):
            for mask, channels, groups, shift, lines in mimg_load_cases(a16, mip=mip):
                if mask not in (0x1, 0xa, 0xf):
                    continue
                original = "image_load_mip" if mip else "image_load"
                lines = variant(lines, (original + " ", op + " "))
                name = f"{op}{'_a16' if a16 else ''}_{mask:x}"
                yield name, mip, signed, mask, channels, groups, shift, lines


def mimg_packed_channel(level, x, y, channel):
    return (0x80 + 29 * level + 13 * x + 19 * y + 63 * channel) % 256


def mimg_packed_words(channels, shift, groups, mip, signed):
    words = [SENTINEL] * 320
    for y in range(TEXTURE_HEIGHT):
        for x in range(8 * groups):
            level, tx, ty = (min(y, 2), x // 4, y // 4) if mip else (0, x, y)
            base = y * 32 + x * (1 << (shift - 2))
            for offset, channel in enumerate(channels):
                raw = mimg_packed_channel(level, tx, ty, channel)
                value = int.from_bytes(bytes([raw]), "little", signed=signed)
                words[base + offset] = value % (1 << 32)
    return words


def mimg_packed_controls():
    *_, lines = next(mimg_packed_cases())
    rejects("Image load opcode does not match the image format", aot.lower, lines, (8, 8, 1), **MIMG_BINDING)
    rejects("Image load opcode does not match the image format", aot.lower,
            variant(lines, ("image_load_pck ", "image_load ")), (8, 8, 1), **MIMG_PACKED_BINDING)
    rejects("Unsupported compute instruction", aot.lower,
            variant(lines, ("dim:SQ_RSRC_IMG_2D", "dim:SQ_RSRC_IMG_2D d16")), (8, 8, 1), **MIMG_PACKED_BINDING)
    rejects("Unresolved image descriptor", aot.lower,
            variant(lines, ("s_waitcnt lgkmcnt(0)", "")), (8, 8, 1), **MIMG_PACKED_BINDING)
    rejects("before s_waitcnt vmcnt(0)", aot.lower,
            variant(lines, ("s_waitcnt vmcnt(0)", "")), (8, 8, 1), **MIMG_PACKED_BINDING)


def mimg_packed_draw(directory, compiler, renderer, backend):
    mimg_packed_controls()
    texture = bytes(mimg_packed_channel(level, x, y, channel)
                    for level in range(3) for y in range(TEXTURE_HEIGHT >> level)
                    for x in range(TEXTURE_WIDTH >> level) for channel in range(TEXTURE_CHANNELS))
    for name, mip, signed, mask, channels, groups, shift, lines in mimg_packed_cases():
        src, cso, case = (directory / f"{name}.{ext}" for ext in ("hlsl", "cso", "case"))
        source = aot.lower(lines, (8, 8, 1), **MIMG_PACKED_BINDING)
        expected = mimg_packed_words(channels, shift, groups, mip, signed)
        # Bit 24 marks the RGBA8 case payload. XYZ dispatch still occupies the low 24 bits.
        case.write_bytes(struct.pack("<321I", groups | MIMG_PACKED_CASE_FLAG, *expected) + texture)
        compile_shader = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
        run = [renderer, backend, "--compute-texture-case", cso, case]
        src.write_text(source, encoding="ascii")
        subprocess.run(compile_shader, check=True)
        result = subprocess.run(run)
        if result.returncode:
            return result.returncode
        if mask != 0xf:
            continue
        load = next(line for line in source.splitlines() if "imageData.Load" in line)
        extension = "    image_load_0 = asuint(asint(image_load_0 << 24u) >> 24u);\n"
        mutation = source.replace(extension, "") if signed else source.replace(load, load + "\n" + extension.rstrip())
        mismatch = "word=0 expected=ffffff80 actual=00000080" if signed else "word=0 expected=00000080 actual=ffffff80"
        mutants = [("wrong extension", mutation, mismatch)]
        if mip:
            base_lod = load[:load.rindex(", asint(")] + ", 0));"
            mismatch = "word=32 expected=ffffff9d actual=ffffff80" if signed else "word=32 expected=0000009d actual=00000080"
            mutants.append(("fixed LOD", source.replace(load, base_lod), mismatch))
        for label, mutation, mismatch in mutants:
            assert mutation != source, label
            src.write_text(mutation, encoding="ascii")
            subprocess.run(compile_shader, check=True)
            bad = subprocess.run(run, capture_output=True, text=True)
            if bad.returncode != 1 or mismatch not in bad.stderr:
                raise AssertionError(f"{name}: {label} escaped detection\n{bad.stderr}")
            print(f"{name} {label}: {bad.stderr}", end="")
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(MIMG_PACKED_OPS) + "\n", encoding="utf-8")
    return 0


MIMG_STORE_GROUP = (4, 2, 1)
MIMG_STORE_BINDING = {key: value for key, value in MIMG_BINDING.items()
                      if key not in ("descriptor_offset", "stride")}


def mimg_store_cases(*, packed=False):
    for mip in (0, 1, 2):
        for a16 in (False, True):
            for mask, groups in ((0x1, 1), (0xa, 2), (0xf, 1)):
                lines = ["s_mov_b32 s12, s7", "s_mov_b32 s13, 0",
                         "s_load_dwordx8 s[24:31], s[12:13], 0x40",
                         "v_lshl_add_u32 v8, s10, 2, v0", "v_lshl_add_u32 v9, s11, 1, v1",
                         "v_mad_u32_u24 v11, v9, 32, v8"]
                addr = "v[8:9]"
                if a16:
                    lines.append("v_mad_u32_u24 v8, v9, 0x10000, v8")
                    addr = "v8"
                if mip:
                    if a16:
                        lines.append(f"v_mov_b32_e32 v9, {0xabcd0000 | mip:#x}")
                    else:
                        lines += [f"v_mov_b32_e32 v10, {mip}"]
                    addr = "v[8:9]" if a16 else "v[8:10]"
                channels = MIMG_CHANNELS[mask]
                for index, channel in enumerate(channels):
                    tag = (channel + 1) * 0x11000000
                    if packed:
                        # High bytes distinguish raw low-byte writes from other conversions.
                        tag |= 0x005a2400 | ((channel + 1) * 0x39)
                        lines.append(f"v_add_nc_u32_e32 v{14 + index}, {tag:#x}, v11")
                    else:
                        lines.append(f"v_or_b32_e32 v{14 + index}, {tag:#x}, v11")
                data = "v14" if len(channels) == 1 else f"v[14:{13 + len(channels)}]"
                op = "image_store_mip" if mip else "image_store"
                if packed:
                    op += "_pck"
                flags = " unorm a16" if a16 else ""
                lines += ["s_waitcnt lgkmcnt(0)",
                          f"{op} {data}, {addr}, s[24:31] dmask:0x{mask:x} dim:SQ_RSRC_IMG_2D{flags}",
                          "s_waitcnt_vscnt null, 0x0", "s_endpgm"]
                name = f"{op}_{mip}{'_a16' if a16 else ''}_{mask:x}"
                binding = MIMG_STORE_BINDING | {"image_uav_mip": mip}
                if packed:
                    binding["image_format"] = "R8G8B8A8_UINT"
                yield name, mask, groups, lines, binding


def mimg_store_controls(*, packed=False):
    _, _, _, lines, binding = next(mimg_store_cases(packed=packed))
    op = "image_store_pck" if packed else "image_store"
    for diagnostic, change in (
        ("Unresolved image descriptor", ("s_waitcnt lgkmcnt(0)", "")),
        ("Unresolved compute descriptor load", ("s_load_dwordx8 s[24:31], s[12:13], 0x40",
                                         "s_load_dwordx8 s[24:31], s[12:13], 0x50")),
        ("Unverified image store data", (f"{op} v14", f"{op} v15")),
        ("Unsupported image store target", ("v14, v[8:9]", "v14, v8")),
        ("Unsupported image store target", ("dmask:0x1", "dmask:0x0")),
        ("Unsupported compute instruction", ("dim:SQ_RSRC_IMG_2D", "dim:SQ_RSRC_IMG_2D d16")),
        ("Unsupported compute instruction", ("s_waitcnt_vscnt null, 0x0", "s_waitcnt_vscnt null, 0x1")),
    ):
        rejects(diagnostic, aot.lower, variant(lines, change), MIMG_STORE_GROUP, **binding)
    image = next(line for line in lines if line.startswith(f"{op} "))
    for _, atomic_lines, _ in (next(mubuf_reduction_cases()), next(mubuf_reduction_cases(wide=True))):
        rejects("Unsupported atomic reduction target or binding", aot.lower,
                variant(lines, (image, atomic_lines[-2])), MIMG_STORE_GROUP, **binding)
    rejects("Unsupported image store target", aot.lower,
            variant(lines, (image, f"{image}\n{image}")), MIMG_STORE_GROUP, **binding)
    assert aot.lower(variant(lines, ("s_waitcnt_vscnt null, 0x0", "")), MIMG_STORE_GROUP, **binding) == aot.lower(
        lines, MIMG_STORE_GROUP, **binding)
    other_format = "R32G32B32A32_UINT" if packed else "R8G8B8A8_UINT"
    rejects("Image store opcode does not match the image format", aot.lower, lines, MIMG_STORE_GROUP,
            **(binding | {"image_format": other_format}))
    rejects("Invalid image UAV binding", aot.lower, lines, MIMG_STORE_GROUP,
            **(binding | {"image_format": "R8G8B8A8_UNORM"}))
    if packed:
        rejects("Unsupported image store target", aot.lower,
                variant(lines, (f"{op} v14", f"{op}_sgn v14")), MIMG_STORE_GROUP, **binding)
    rejects("Invalid image UAV binding", aot.lower, lines, MIMG_STORE_GROUP,
            **(binding | {"descriptor_offset": 0x30}))
    rejects("Image store mip does not match the UAV view", aot.lower, lines, MIMG_STORE_GROUP,
            **(binding | {"image_uav_mip": 1}))
    _, _, _, lines, binding = next(case for case in mimg_store_cases(packed=packed)
                                   if case[4]["image_uav_mip"] == 1)
    rejects("Image store mip does not match the UAV view", aot.lower,
            variant(lines, ("v_mov_b32_e32 v10, 1", "v_mov_b32_e32 v10, v8")), MIMG_STORE_GROUP, **binding)


def mimg_store_reference(mask, groups, mip):
    initial, expected = [], []
    for level in range(3):
        for y in range(TEXTURE_HEIGHT >> level):
            for x in range(TEXTURE_WIDTH >> level):
                for channel in range(TEXTURE_CHANNELS):
                    before = 0xc0000000 | (level << 20) | (y << 12) | (x << 4) | channel
                    initial.append(before)
                    if level != mip or x >= 4 * groups or y >= 2:
                        expected.append(before)
                    else:
                        expected.append((channel + 1) * 0x11000000 + 32 * y + x if mask & (1 << channel) else 0)
    return initial, expected


def mimg_packed_store_payload(mask, groups, mip):
    initial, expected = bytearray(), bytearray()
    for level in range(3):
        for y in range(TEXTURE_HEIGHT >> level):
            for x in range(TEXTURE_WIDTH >> level):
                for channel in range(TEXTURE_CHANNELS):
                    before = 1 + (0x80 + 17 * level + 31 * y + 7 * x + 13 * channel) % 255
                    initial.append(before)
                    after = before
                    if level == mip and x < 4 * groups and y < 2:
                        data = (channel + 1) * 57 + 32 * y + x
                        after = data % 256 if mask & (1 << channel) else 0
                    expected.append(after)
    return initial + expected


def mimg_store_draw(directory, compiler, renderer, backend, *, packed=False):
    mimg_store_controls(packed=packed)
    for name, mask, groups, lines, binding in mimg_store_cases(packed=packed):
        mip = binding["image_uav_mip"]
        src, cso, case = (directory / f"{name}.{ext}" for ext in ("hlsl", "cso", "case"))
        code = aot.lower(lines, MIMG_STORE_GROUP, **binding)
        src.write_text(code, encoding="ascii")
        compile_shader = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
        subprocess.run(compile_shader, check=True)
        dispatch = groups
        if packed:
            dispatch |= MIMG_PACKED_CASE_FLAG
            payload = mimg_packed_store_payload(mask, groups, mip)
        else:
            initial, expected = mimg_store_reference(mask, groups, mip)
            payload = struct.pack(f"<{len(initial) + len(expected)}I", *initial, *expected)
        case.write_bytes(struct.pack("<2I", dispatch, mip) + payload)
        run = [renderer, backend, "--compute-texture-store-case", cso, case]
        result = subprocess.run(run)
        if result.returncode:
            return result.returncode
        mutants = []
        if name in ("image_store_0_a", "image_store_pck_0_a"):
            # Check both byte selection and zeroing of omitted channels.
            if packed:
                mutants.append(("wrong byte", code.replace(" & 0xffu)", " >> 8u & 0xffu)"),
                                "mip=0 word=0 expected=e4007200 actual=24002400"))
            value = "0xefu" if packed else "0xdeadbeefu"
            mismatch = ("mip=0 word=0 expected=e4007200 actual=e40072ef" if packed
                        else "mip=0 word=0 expected=00000000 actual=deadbeef")
            mutants.append(("unmasked channel", code.replace("= uint4(0u,", f"= uint4({value},", 1), mismatch))
        elif name in ("image_store_mip_1_f", "image_store_mip_pck_1_f"):
            case.write_bytes(struct.pack("<2I", dispatch, 0) + payload)
            mismatch = ("mip=0 word=0 expected=a89b8e81 actual=e4ab7239" if packed
                        else "mip=0 word=0 expected=c0000000 actual=11000000")
            mutants.append(("wrong UAV mip", None, mismatch))
        for label, mutation, mismatch in mutants:
            if mutation is not None:
                assert mutation != code, label
                src.write_text(mutation, encoding="ascii")
                subprocess.run(compile_shader, check=True)
            bad = subprocess.run(run, capture_output=True, text=True)
            if bad.returncode != 1 or mismatch not in bad.stderr:
                raise AssertionError(f"{name}: {label} escaped detection\n{bad.stderr}")
            print(f"{name} {label}: {bad.stderr}", end="")
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        suffix = "_pck" if packed else ""
        path.write_text(f"image_store{suffix}\nimage_store_mip{suffix}\n", encoding="utf-8")
    return 0


def mimg_resinfo_cases():
    for a16 in (False, True):
        for mask, channels, groups, shift, lines in mimg_load_cases():
            if mask & 0x4:
                continue
            # Use one mip operand, with nonzero upper padding for a16. v14 is also the first result.
            levels = 1 if mask == 0x8 else 3
            query = f"v_min_u32_e32 v14, {levels - 1}, v9"
            if a16:
                query += "\nv_or_b32_e32 v14, 0xabcd0000, v14"
            image = next(line for line in lines if line.startswith("image_load "))
            replacement = image.replace("image_load", "image_get_resinfo").replace("v[8:9]", "v14")
            if a16:
                replacement += " unorm a16"
            name = f"mimg_resinfo{int(a16)}_{mask:x}"
            yield name, channels, groups, shift, levels, variant(lines, (image, f"{query}\n{replacement}"))


def mimg_resinfo_controls():
    _, _, _, _, _, lines = next(mimg_resinfo_cases())
    for diagnostic, change in (
        ("Unresolved image descriptor", ("s_waitcnt lgkmcnt(0)", "")),
        ("before s_waitcnt vmcnt(0)", ("s_waitcnt vmcnt(0)", "")),
        ("Unverified image coordinates", ("v14, v14, s[24:31]", "v14, v15, s[24:31]")),
        ("Unsupported image get_resinfo target", ("v14, v14, s[24:31]", "v14, v[14:15], s[24:31]")),
        ("Unsupported image get_resinfo target", ("dmask:0x1", "dmask:0x4")),
        ("Unsupported compute instruction", ("dim:SQ_RSRC_IMG_2D", "dim:SQ_RSRC_IMG_3D")),
    ):
        rejects(diagnostic, aot.lower, variant(lines, change), (8, 8, 1), **MIMG_BINDING)
    rejects("Image get_resinfo requires an RGBA32 SRV", aot.lower, lines, (8, 8, 1), **MIMG_PACKED_BINDING)


def mimg_resinfo_draw(directory, compiler, renderer, backend):
    mimg_resinfo_controls()
    base_texture = [word for texel in TEXTURE_WORDS for word in texel]
    for name, channels, groups, shift, levels, lines in mimg_resinfo_cases():
        texture = base_texture if levels == 1 else base_texture + mimg_mip_texels()
        payload = struct.pack(f"<{len(texture)}I", *texture)
        src, cso, case = (directory / f"{name}.{ext}" for ext in ("hlsl", "cso", "case"))
        code = aot.lower(lines, (8, 8, 1), **MIMG_BINDING)
        expected = [SENTINEL] * 320
        for y in range(TEXTURE_HEIGHT):
            width, height = ((32, 8), (16, 4), (8, 2))[min(y, levels - 1)]
            components = {0: width, 1: height, 3: levels}
            for x in range(groups * 8):
                base = y * 32 + x * (1 << (shift - 2))
                expected[base:base + len(channels)] = [components[channel] for channel in channels]
        case.write_bytes(struct.pack("<321I", groups, *expected) + payload)
        compile_shader = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
        run = [renderer, backend, "--compute-texture-case", cso, case]
        src.write_text(code, encoding="ascii")
        subprocess.run(compile_shader, check=True)
        result = subprocess.run(run)
        if result.returncode:
            return result.returncode
        if name == "mimg_resinfo1_b":
            mutant = code.replace("imageData.GetDimensions((", "imageData.GetDimensions(0u * (")
            assert mutant != code
            src.write_text(mutant, encoding="ascii")
            subprocess.run(compile_shader, check=True)
            bad = subprocess.run(run, capture_output=True, text=True)
            mismatch = "word=32 expected=00000010 actual=00000020"
            if bad.returncode != 1 or mismatch not in bad.stderr:
                raise AssertionError(f"{name}: fixed mip escaped detection\n{bad.stderr}")
            print(f"{name} fixed mip: {bad.stderr}", end="")
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("image_get_resinfo\n", encoding="utf-8")
    return 0


MIMG_ATOMIC_UINT_FLAG = 1 << 25
MIMG_ATOMIC_MINMAX_FLAGS = {"s": 1 << 26, "u": 1 << 27}
MIMG_ATOMIC_MINMAX_OPS = ("smin", "smax", "umin", "umax")
MIMG_ATOMIC_BEFORE = (0xfffffff0, 0x7ffffff0, 0x20, 0x80000000)
MIMG_ATOMIC_VALUE = 0x80000011


def mimg_atomic_cases(*, minmax=False):
    for op in MIMG_ATOMIC_MINMAX_OPS if minmax else ("add", "sub", "and", "or", "xor"):
        image_format = "R32_SINT" if op in ("smin", "smax") else "R32_UINT"
        binding = MIMG_STORE_BINDING | {"image_uav_mip": 0, "image_format": image_format}
        for contended in ((False, True) if op == "add" else (False,)):
            lines = ["s_mov_b32 s12, s7", "s_mov_b32 s13, 0",
                     "s_load_dwordx8 s[24:31], s[12:13], 0x40",
                     "v_lshl_add_u32 v8, s10, 2, v0",
                     "v_mov_b32_e32 v9, 1" if contended else "v_lshl_add_u32 v9, s11, 1, v1"]
            if not contended:
                lines.append("v_add_nc_u32_e32 v9, 1, v9")
            lines.append(f"v_mov_b32_e32 v14, {MIMG_ATOMIC_VALUE:#x}")
            flags = " unorm" if contended else ""
            lines += ["s_waitcnt lgkmcnt(0)",
                      f"image_atomic_{op} v14, v[8:9], s[24:31] dmask:0x1 dim:SQ_RSRC_IMG_2D{flags}",
                      "s_waitcnt_vscnt null, 0x0", "s_endpgm"]
            yield op + ("_contended" if contended else ""), contended, lines, binding


def mimg_atomic_controls(*, minmax=False):
    cases = {op: (lines, binding) for op, _, lines, binding in mimg_atomic_cases(minmax=minmax)}
    base_op = "smin" if minmax else "add"
    lines, binding = cases[base_op]
    instruction = lines[-3]
    for changed, diagnostic in (
        (instruction.replace("dmask:0x1", "dmask:0xf"), "Unverified image atomic data range or mask"),
        (instruction.replace("dmask:0x1", "dmask:0x3"), "Unverified image atomic data range or mask"),
        (instruction.replace("v14", "v256"), "Unverified image atomic data range or mask"),
        (instruction.replace("v14", "v15"), "Unverified image atomic data"),
        (instruction.replace("v14", "v[14:15]"), "Unsupported compute instruction"),
        (instruction.replace("v[8:9]", "v[8:10]"), "Unverified image atomic coordinates"),
        (instruction.replace("v[8:9]", "v[9:10]"), "Unverified image atomic coordinates"),
        (instruction.replace("s[24:31]", "s[28:35]"), "Unverified image atomic descriptor"),
        (instruction + "\n" + instruction, "Unsupported image atomic target or binding"),
        (instruction.replace("IMG_2D", "IMG_2D_ARRAY"), "Unsupported compute instruction"),
        (instruction.replace("v[8:9]", "[v8, v9]"), "Unsupported compute instruction"),
        *((instruction + flag, "Unsupported compute instruction")
          for flag in (" glc", " dlc", " slc", " a16", " d16", " tfe", " lwe", " r128")),
    ):
        rejects(diagnostic, aot.lower, variant(lines, (instruction, changed)), MIMG_STORE_GROUP, **binding)
    rejects("Unverified image atomic descriptor", aot.lower,
            lines[:-4] + lines[-3:], MIMG_STORE_GROUP, **binding)
    rejects("Unsupported image atomic target or binding", aot.lower, lines, MIMG_STORE_GROUP,
            **(binding | {"image_format": "R32G32B32A32_UINT"}))
    rejects("Invalid image UAV binding", aot.lower, lines, MIMG_STORE_GROUP,
            **(binding | {"image_uav_mip": 1}))
    srv_binding = {key: value for key, value in binding.items() if key != "image_uav_mip"}
    rejects("Invalid or overlapping image descriptor binding" if minmax else "Unsupported image atomic target or binding",
            aot.lower, lines, MIMG_STORE_GROUP,
            **(srv_binding | {"descriptor_offset": MIMG_BINDING["descriptor_offset"]}))
    msaa = "image_msaa_load v[14:17], v[8:10], s[24:31] dmask:0xf dim:SQ_RSRC_IMG_2D_MSAA unorm"
    rejects("requires an unguarded R32_UINT MSAA binding", aot.lower,
            variant(lines, (instruction, msaa)), MIMG_STORE_GROUP, **binding)
    rejects("Invalid image UAV binding", aot.lower, lines, MIMG_STORE_GROUP, **(binding | {"cpu": "gfx1013"}))
    for case, case_binding in cases.values():
        wrong_format = "R32_UINT" if case_binding["image_format"] == "R32_SINT" else "R32_SINT"
        rejects("Unsupported image atomic target or binding", aot.lower, case, MIMG_STORE_GROUP,
                **(case_binding | {"image_format": wrong_format}))
    for op in ("swap", "cmpswap", "inc", "dec", "fcmpswap", "fmin", "fmax"):
        rejects("Unsupported compute instruction", aot.lower,
                variant(lines, ("image_atomic_" + base_op, "image_atomic_" + op)), MIMG_STORE_GROUP, **binding)
    rejects("Unsupported image store target", aot.lower,
            variant(lines, ("image_atomic_" + base_op, "image_store")), MIMG_STORE_GROUP, **binding)


def mimg_atomic_reference(op, contended, groups):
    value = MIMG_ATOMIC_VALUE
    if op in MIMG_ATOMIC_MINMAX_OPS:
        # Flipping the sign bit maps two's-complement order to unsigned order.
        key = (lambda word: word ^ 0x80000000) if op[0] == "s" else None
        choose = min if op[1:] == "min" else max
        update = lambda old: choose(old, value, key=key)
    else:
        update = {"add": lambda old: old + value, "sub": lambda old: old - value,
                  "and": lambda old: old & value, "or": lambda old: old | value, "xor": lambda old: old ^ value}[op]
    initial, expected = [], []
    for level in range(3):
        for y in range(TEXTURE_HEIGHT >> level):
            for x in range(TEXTURE_WIDTH >> level):
                before = 0xc0000000 | (level << 20) | (y << 12) | x
                after = before
                if level == 0 and x < groups * MIMG_STORE_GROUP[0] and y in ((1,) if contended else (1, 2)):
                    before = MIMG_ATOMIC_BEFORE[(x + y - 1) % len(MIMG_ATOMIC_BEFORE)]
                    after = update(before)
                    if contended:
                        after = update(after)
                initial.append(before)
                expected.append(after & 0xffffffff)
    return initial, expected


def mimg_atomic_integer(directory, compiler, renderer=None, backend=None, *, minmax=False):
    mimg_atomic_controls(minmax=minmax)
    record_path = os.environ.get("FORM_COVERAGE_RECORD") if renderer else None
    record = Path(record_path) if record_path else None
    if record:
        record.unlink(missing_ok=True)
    completed = set()
    for name, contended, lines, binding in mimg_atomic_cases(minmax=minmax):
        src, cso, case = (directory / f"image_atomic_{name}.{ext}" for ext in ("hlsl", "cso", "case"))
        code = aot.lower(lines, MIMG_STORE_GROUP, **binding)
        src.write_text(code, encoding="ascii")
        compile_shader = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
        subprocess.run(compile_shader, check=True)
        op = name.removesuffix("_contended")
        flag = MIMG_ATOMIC_MINMAX_FLAGS[op[0]] if minmax else MIMG_ATOMIC_UINT_FLAG
        for groups in (1, 2):
            initial, expected = mimg_atomic_reference(op, contended, groups)
            assert len(initial) == len(expected) == 336 and initial != expected
            if renderer:
                dispatch = groups | flag
                case.write_bytes(struct.pack("<674I", dispatch, 0, *initial, *expected))
                run = [renderer, backend, "--compute-texture-store-case", cso, case]
                print(f"image_atomic_{name} groups={groups}", flush=True)
                result = subprocess.run(run)
                if result.returncode:
                    return result.returncode
        mutant = None
        if op == "sub":
            correct, wrong = f"(0u - {MIMG_ATOMIC_VALUE:#010x}u)", f"{MIMG_ATOMIC_VALUE:#010x}u"
            mismatch = "mip=0 word=32 expected=7fffffdf actual=80000001"
            control = "wrong atomic operand"
            assert code.count(correct) == 1
            mutant = code.replace(correct, wrong)
        elif minmax:
            signed = op[0] == "s"
            other = "u" if signed else "s"
            decl, wrong_decl = ("int", "uint") if signed else ("uint", "int")
            value = f"{MIMG_ATOMIC_VALUE:#010x}u"
            correct, wrong = (f"asint({value})", value) if signed else (value, f"asint({value})")
            assert code.count(f"RWTexture2D<{decl}>") == code.count(f", {correct});") == 1
            mutant = code.replace(f"RWTexture2D<{decl}>", f"RWTexture2D<{wrong_decl}>").replace(
                f", {correct});", f", {wrong});")
            _, wrong_expected = mimg_atomic_reference(other + op[1:], contended, groups)
            first = next(i for i, (a, b) in enumerate(zip(expected, wrong_expected, strict=True)) if a != b)
            assert first == TEXTURE_WIDTH + 1
            mismatch = f"mip=0 word={first} expected={expected[first]:08x} actual={wrong_expected[first]:08x}"
            control = "wrong atomic signedness"
            if renderer:
                dispatch = groups | MIMG_ATOMIC_MINMAX_FLAGS[other]
                case.write_bytes(struct.pack("<674I", dispatch, 0, *initial, *expected))
        if mutant is not None:
            src.write_text(mutant, encoding="ascii")
            subprocess.run(compile_shader, check=True)
            if renderer:
                bad = subprocess.run(run, capture_output=True, text=True)
                if minmax and bad.returncode == 77:
                    print(bad.stdout, end="")
                    print(bad.stderr, end="", file=sys.stderr)
                    return 77
                if bad.returncode != 1 or mismatch not in bad.stderr:
                    raise AssertionError(f"{name}: {control} escaped detection\n{bad.stderr}")
                print(f"{name} {control}: {bad.stderr}", end="")
        completed.add(f"image_atomic_{op}")
    if record:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("\n".join(sorted(completed)) + "\n", encoding="utf-8")
    return 0


def mimg_mip_draw(directory, compiler, renderer, backend):
    return mimg_load_draw(directory, compiler, renderer, backend, mip=True)


def mimg_load_draw(directory, compiler, renderer, backend, *, mip=False):
    mimg_load_controls()
    mimg_a16_controls()
    # Each mutation is a translation mistake that must change the readback at a named word: the
    # dmask selects ascending texel components, and the first address dword is the x coordinate.
    families = (
        (False, {
            0x1: [("component order", "image_load_0.x", "image_load_0.y", 0)],
            0xf: [("coordinate order", "int3(asint(tid.x), asint(tid.y), 0)",
                 "int3(asint(tid.y), asint(tid.x), 0)", 4),
                ("channel order", "uint4(image_load_0.x, image_load_0.y, image_load_0.z, image_load_0.w)",
                 "uint4(image_load_0.y, image_load_0.x, image_load_0.z, image_load_0.w)", 0)],
        }),
        (True, {0xf: [("packed high half", ">> 16u", ">> 0u", 4)]}),
    )
    if mip:
        mimg_mip_controls()
        families = ((False, {}), (True, {}))
    texture = [word for texel in TEXTURE_WORDS for word in texel]
    if mip:
        texture += mimg_mip_texels()
    packed_texture = struct.pack(f"<{len(texture)}I", *texture)
    for a16, mutations in families:
        for mask, channels, groups, shift, lines in mimg_load_cases(a16, mip=mip):
            stem = f"mimg_load{'_mip' if mip else ''}{'_a16' if a16 else ''}_{mask:x}"
            src, cso, case = (directory / f"{stem}.{ext}" for ext in ("hlsl", "cso", "case"))
            source = aot.lower(lines, (8, 8, 1), **MIMG_BINDING)
            src.write_text(source, encoding="ascii")
            compile_shader = [compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src]
            subprocess.run(compile_shader, check=True)
            run = [renderer, backend, "--compute-texture-case", cso, case]
            expected = mimg_load_words(channels, shift, groups, mip=mip)
            reference = struct.pack("<321I", groups, *expected) + packed_texture
            case.write_bytes(reference)
            result = subprocess.run(run)
            if result.returncode:
                return result.returncode
            # The comparison itself must fail on one wrong written word and one wrong untouched word.
            reference_mutations = (0, 300) if mask in MIMG_PREFIX_MASKS else ()
            for word in reference_mutations:
                wrong = list(expected)
                wrong[word] ^= 1
                case.write_bytes(struct.pack("<321I", groups, *wrong) + packed_texture)
                bad = subprocess.run(run, capture_output=True, text=True)
                if bad.returncode != 1 or f"word={word} expected=" not in bad.stderr:
                    raise AssertionError(f"{stem}: wrong word {word} escaped detection\n{bad.stderr}")
                print(bad.stderr, end="")
            case.write_bytes(reference)
            controls = mutations.get(mask, ())
            if mask == 0xa:
                controls = [("treat sparse mask as xy", "uint2(image_load_0.y, image_load_0.w)",
                             "uint2(image_load_0.x, image_load_0.y)", 0)]
            if mip and mask == 0xf:
                load = next(line for line in source.splitlines() if "imageData.Load" in line)
                zero_lod = load[:load.rindex(", asint(")] + ", 0));"
                controls = [("fixed LOD", load, zero_lod, 32)]
                if a16:
                    high_lod = load[:load.rindex(" & 0xffffu")] + " >> 16u)));"
                    controls.append(("packed LOD high half", load, high_lod, 0))
            for label, old, new, word in controls:
                mutated = source.replace(old, new)
                assert mutated != source, label
                src.write_text(mutated, encoding="ascii")
                subprocess.run(compile_shader, check=True)
                bad = subprocess.run(run, capture_output=True, text=True)
                if bad.returncode != 1 or f"word={word} expected=" not in bad.stderr:
                    raise AssertionError(f"{stem}: {label} mutation escaped detection\n{bad.stderr}")
                print(bad.stderr, end="")
    if record := os.environ.get("FORM_COVERAGE_RECORD"):
        path = Path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("image_load_mip\n" if mip else "image_load\n", encoding="utf-8")
    return 0


def read_words(name, groups, bound):
    """Independent expected readback: each lane under the bound copies its source word."""
    words = [SENTINEL] * 320
    for gid in range(min(32 * groups, bound)):
        words[gid] = source_word(gid if name == "read_copy" else 2 * gid + 1)
    return words


# (groups, bound): no lane passes, a split wave, and the largest dispatch the harness allows with its
# fifth group split. Every lane reads one of the 320 known source words. Mutations run on the last.
READ_RUNS = ((1, 0), (1, 17), (5, 150))


def read_mutations(source, name):
    """Translations that a read mistake would produce; each must fail on the GPU."""
    load = READ_LOADS[name]
    yield "index ignored", source.replace(load, "inputData.Load(4u)")
    yield "next source word", source.replace(load, load.replace(" + ", " + 4u + "))
    yield "read from the store target", source.replace(load, load.replace("inputData", "outputData"))


def draw_read(directory, compiler, cmd, case, name):
    constants = [0x5eed0000 + i for i in range(16)]

    def write(groups, bound, words=None):
        case.write_bytes(struct.pack("<337I", groups, *(words or read_words(name, groups, bound)), bound,
                                     *constants[1:]))

    for groups, bound in READ_RUNS:
        write(groups, bound)
        result = subprocess.run(cmd)
        if result.returncode:
            return result.returncode
    groups, bound = READ_RUNS[-1]
    # Reference controls: one source word off, and the first lane past the bound copying too.
    off = read_words(name, groups, bound)
    off[bound - 1] ^= 1
    for word, reference in ((bound - 1, off), (bound, read_words(name, groups, bound + 1))):
        write(groups, bound, reference)
        bad = subprocess.run(cmd, capture_output=True, text=True)
        if bad.returncode != 1 or f"word={word} expected=" not in bad.stderr:
            raise AssertionError(f"{name}: a wrong reference word {word} escaped detection\n{bad.stderr}")
    write(groups, bound)
    source = (directory / "store.hlsl").read_text(encoding="ascii")
    for label, mutated in read_mutations(source, name):
        if mutated == source:
            raise AssertionError(f"{name}: mutation '{label}' did not change the source")
        src, cso = directory / "mutated.hlsl", directory / "mutated.cso"
        src.write_text(mutated, encoding="ascii")
        subprocess.run([compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src], check=True)
        bad = subprocess.run([*cmd[:3], cso, case], capture_output=True, text=True)
        if bad.returncode != 1 or "Compute readback differs" not in bad.stderr:
            raise AssertionError(f"{name}: mutation '{label}' escaped detection with groups={groups} bound={bound}")
    return 0


# cap = params[0].y clamps about half of the lanes of the largest dispatch; the other constant words
# differ from it, so a translation that reads another word fails.
ALU_CAP = 0x45000000


def alu_word(gid, *, low=0xffff, shift=16, pick=min):
    """Independent CPU reference of the read_compute result for one lane."""
    word = source_word(gid)
    return pick(((word & low) * (word >> shift) + gid) & 0xffffffff, ALU_CAP)


def alu_words(groups, bound, **reference):
    words = [SENTINEL] * 320
    for gid in range(min(32 * groups, bound)):
        words[gid] = alu_word(gid, **reference)
    return words


def alu_mutations(source):
    """Translations that a wrong opcode or operand would produce; each must fail on the GPU."""
    yield "max for min", source.replace("min(params", "max(params")
    yield "shift by 15", source.replace(">> (0x00000010u", ">> (0x0000000fu")
    yield "24-bit mask for 16-bit", source.replace("(0x0000ffffu &", "(0x00ffffffu &")
    yield "addend dropped", source.replace(" + tid.x))", "))")
    yield "cap from another word", source.replace("min(params[0].y", "min(params[0].z")


def draw_alu(directory, compiler, cmd, case, name):
    constants = [0x5eed0000 + i for i in range(16)]
    constants[1] = ALU_CAP

    def write(groups, bound, words=None):
        case.write_bytes(struct.pack("<337I", groups, *(words or alu_words(groups, bound)), bound,
                                     *constants[1:]))

    for groups, bound in READ_RUNS:
        write(groups, bound)
        result = subprocess.run(cmd)
        if result.returncode:
            return result.returncode
    groups, bound = READ_RUNS[-1]
    expected = alu_words(groups, bound)
    assert 0 < sum(word == ALU_CAP for word in expected) < bound, "the cap must clamp some lanes, not all"
    # Reference controls: a CPU reference that disagrees with the program in one word, in the
    # operation, the shift or the mask, or in the bound.
    off = list(expected)
    off[bound - 1] ^= 1
    for label, reference in (("one word off", off), ("max for min", alu_words(groups, bound, pick=max)),
                             ("shift by 15", alu_words(groups, bound, shift=15)),
                             ("24-bit mask", alu_words(groups, bound, low=0xffffff)),
                             ("lane past the bound", alu_words(groups, bound + 1))):
        word = next(i for i, (a, b) in enumerate(zip(expected, reference)) if a != b)
        write(groups, bound, reference)
        bad = subprocess.run(cmd, capture_output=True, text=True)
        if bad.returncode != 1 or f"word={word} expected=" not in bad.stderr:
            raise AssertionError(f"{name}: reference '{label}' escaped detection\n{bad.stderr}")
    write(groups, bound)
    source = (directory / "store.hlsl").read_text(encoding="ascii")
    for label, mutated in alu_mutations(source):
        if mutated == source:
            raise AssertionError(f"{name}: mutation '{label}' did not change the source")
        src, cso = directory / "mutated.hlsl", directory / "mutated.cso"
        src.write_text(mutated, encoding="ascii")
        subprocess.run([compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src], check=True)
        bad = subprocess.run([*cmd[:3], cso, case], capture_output=True, text=True)
        if bad.returncode != 1 or "Compute readback differs" not in bad.stderr:
            raise AssertionError(f"{name}: mutation '{label}' escaped detection with groups={groups} bound={bound}")
    return 0


SCALAR_CAP = 0x8068  # cap + 0xfffffff0 and then - 0x10000 both wrap; bits 5 and 6 survive the and mask


def scalar_chain(cap, bound, *, skip=(), shift=3, bound_word=None):
    """Independent CPU reference of the scalar chain: add -16, sub 0x10000, and, or 10, xor the bound, shift."""
    word = (cap + 0xfffffff0) & 0xffffffff if "add" not in skip else cap
    word = (word - 0x10000) & 0xffffffff if "sub" not in skip else word
    word = word & 0xffffffe0 if "and" not in skip else word
    word = word | 10 if "or" not in skip else word
    word = word ^ (bound if bound_word is None else bound_word) if "xor" not in skip else word
    return word >> shift


def scalar_word(gid, bound, *, pick=min, **chain):
    """Reference of read_scalar for one lane: the unsigned minimum of the chain and the product plus the lane index."""
    word = source_word(gid)
    product = ((word & 0xffff) * (word >> 16) + gid) & 0xffffffff
    return pick(product, scalar_chain(SCALAR_CAP, bound, **chain))


def scalar_words(groups, bound, **reference):
    words = [SENTINEL] * 320
    for gid in range(min(32 * groups, bound)):
        words[gid] = scalar_word(gid, bound, **reference)
    return words


def draw_scalar(directory, compiler, cmd, case, name):
    """Scalar chain against scalar_word, from the 320-word case format, with wrong references and mutated sources."""
    constants = [0x5eed0000 + i for i in range(16)]
    constants[1] = SCALAR_CAP

    def write(groups, bound, words):
        case.write_bytes(struct.pack("<337I", groups, *words, bound, *constants[1:]))

    for groups, bound in READ_RUNS:
        write(groups, bound, scalar_words(groups, bound))
        result = subprocess.run(cmd)
        if result.returncode:
            return result.returncode
    groups, bound = READ_RUNS[-1]
    expected = scalar_words(groups, bound)
    products = [scalar_word(g, bound, pick=lambda p, c: p) for g in range(bound)]
    chain = scalar_chain(SCALAR_CAP, bound)
    # Inputs must show both add and sub wrapping, and the minimum taking each operand.
    assert SCALAR_CAP + 0xfffffff0 >= 1 << 32 and (SCALAR_CAP + 0xfffffff0) & 0xffffffff < 0x10000, "no wrap"
    assert any(p < chain for p in products) and any(p >= chain for p in products), "min takes one operand only"
    controls = (("max for min", dict(pick=max)), ("add dropped", dict(skip=("add",))),
                ("sub dropped", dict(skip=("sub",))), ("and dropped", dict(skip=("and",))),
                ("or dropped", dict(skip=("or",))), ("xor dropped", dict(skip=("xor",))),
                ("shift by 2", dict(shift=2)), ("cap for bound", dict(bound_word=SCALAR_CAP)))
    for label, reference in controls:
        wrong = scalar_words(groups, bound, **reference)
        word = next(i for i, (a, b) in enumerate(zip(expected, wrong)) if a != b)
        write(groups, bound, wrong)
        bad = subprocess.run(cmd, capture_output=True, text=True)
        if bad.returncode != 1 or f"word={word} expected=" not in bad.stderr:
            raise AssertionError(f"{name}: reference '{label}' escaped detection\n{bad.stderr}")
    write(groups, bound, expected)
    source = (directory / "store.hlsl").read_text(encoding="ascii")
    for label, old, new in (("max for min", "min(((((((params", "max(((((((params"),
                            ("add dropped", "(params[0].y + 0xfffffff0u)", "params[0].y"),
                            ("sub dropped", "- 0x00010000u)", "- 0x00000000u)"),
                            ("and dropped", "& 0xffffffe0u", "& 0xffffffffu"),
                            ("or dropped", "| 0x0000000au", "| 0x00000000u"),
                            ("xor dropped", "^ params[0].x", "^ 0x00000000u"),
                            ("shift by 2", ">> (0x00000003u", ">> (0x00000002u")):
        mutated = source.replace(old, new)
        if mutated == source:
            raise AssertionError(f"{name}: mutation '{label}' did not change the source")
        src, cso = directory / "mutated.hlsl", directory / "mutated.cso"
        src.write_text(mutated, encoding="ascii")
        subprocess.run([compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src], check=True)
        bad = subprocess.run([*cmd[:3], cso, case], capture_output=True, text=True)
        if bad.returncode != 1 or "Compute readback differs" not in bad.stderr:
            raise AssertionError(f"{name}: mutation '{label}' escaped detection")
    return 0


VOP2_CAP = 0xe0000000  # large enough that the product plus the cap wraps for many lanes


def vop2_word(gid, *, pick="max", skip=()):
    """Independent CPU reference of the read_vop2 result for one lane: the product p is added to the cap (wrapping),
    OR-ed and XOR-ed with high-bit constants, and the unsigned maximum of p and that chain is stored."""
    word = source_word(gid)
    product = ((word & 0xffff) * (word >> 16) + gid) & 0xffffffff
    chain = product if "add" in skip else (product + VOP2_CAP) & 0xffffffff
    chain = chain if "or" in skip else chain | 0x80000010
    chain = chain if "xor" in skip else chain ^ 0x80000001
    if pick == "signed":
        return max(product, chain, key=lambda v: v - (1 << 32) if v >> 31 else v)
    return min(product, chain) if pick == "min" else max(product, chain)


def vop2_words(groups, bound, **reference):
    words = [SENTINEL] * 320
    for gid in range(min(32 * groups, bound)):
        words[gid] = vop2_word(gid, **reference)
    return words


def draw_vop2(directory, compiler, cmd, case, name):
    """Chained add, or, xor and unsigned max against vop2_word, from the 320-word case format."""
    constants = [0x5eed0000 + i for i in range(16)]
    constants[1] = VOP2_CAP
    groups, bound = READ_RUNS[-1]

    def write(words):
        case.write_bytes(struct.pack("<337I", groups, *words, bound, *constants[1:]))

    expected = vop2_words(groups, bound)
    products = [((source_word(g) & 0xffff) * (source_word(g) >> 16) + g) & 0xffffffff for g in range(bound)]
    # Inputs must show an add that wraps, the max choosing each operand, and a signed max that would differ.
    assert any(p + VOP2_CAP >= 1 << 32 for p in products), "no lane wraps the add"
    assert any(e == p for e, p in zip(expected, products)), "no lane takes the product"
    assert any(e != p for e, p in zip(expected, products)), "no lane takes the chain"
    assert expected != vop2_words(groups, bound, pick="signed"), "a signed max would go unseen"
    write(expected)
    result = subprocess.run(cmd)
    if result.returncode:
        return result.returncode
    # Each wrong reference must fail the run at the first word where it differs from the right one.
    for label, reference in (("signed max", vop2_words(groups, bound, pick="signed")),
                             ("min for max", vop2_words(groups, bound, pick="min")),
                             ("add dropped", vop2_words(groups, bound, skip=("add",))),
                             ("or dropped", vop2_words(groups, bound, skip=("or",))),
                             ("xor dropped", vop2_words(groups, bound, skip=("xor",)))):
        word = next(i for i, (a, b) in enumerate(zip(expected, reference)) if a != b)
        write(reference)
        bad = subprocess.run(cmd, capture_output=True, text=True)
        if bad.returncode != 1 or f"word={word} expected=" not in bad.stderr:
            raise AssertionError(f"{name}: reference '{label}' escaped detection\n{bad.stderr}")
    write(expected)
    source = (directory / "store.hlsl").read_text(encoding="ascii")
    for label, old, new in (("min for max", "max(((((", "min((((("), ("xor dropped", "0x80000001u ^", "0x00000000u ^"),
                            ("or dropped", "0x80000010u |", "0x00000000u |"),
                            ("add dropped", "(params[0].y + (", "(0u + (")):
        mutated = source.replace(old, new)
        if mutated == source:
            raise AssertionError(f"{name}: mutation '{label}' did not change the source")
        src, cso = directory / "mutated.hlsl", directory / "mutated.cso"
        src.write_text(mutated, encoding="ascii")
        subprocess.run([compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src], check=True)
        bad = subprocess.run([*cmd[:3], cso, case], capture_output=True, text=True)
        if bad.returncode != 1 or "Compute readback differs" not in bad.stderr:
            raise AssertionError(f"{name}: mutation '{label}' escaped detection")
    return 0


def f32_value(bits):
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def f32_round(value):
    """Round a Python double to binary32, to nearest even. For add, subtract and multiply of two binary32 values
    the double result is rounded once more, which is exact: a double carries more than 2 * 24 + 2 bits."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


def f32_bits(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


F32_PI = 0x40490fdb


def f32_steps(gid, wrong=()):
    """Independent CPU model of read_f32 for one lane, every intermediate value. wrong names one operation to swap."""
    word = source_word(gid)
    x = f32_value(0x3fc00000 | word & 0x3fffff)
    y = f32_value(0x40400000 | word >> 10)
    cap, pi = f32_value(F32_CAP), f32_value(F32_PI)
    a = f32_round(x + y)
    b = f32_round(a - cap if "sub" in wrong else cap - a)
    c = f32_round(pi - b if "subrev" in wrong else b - pi)
    d = f32_round(x + c if "mul" in wrong else x * c)
    e = f32_round(1.0 - d if "add" in wrong else 1.0 + d)
    return x, y, a, b, c, d, e, f32_round(e - 4.0 if "inline" in wrong else e - (-4.0))


def f32_words(groups, bound, **reference):
    words = [SENTINEL] * 320
    for gid in range(min(32 * groups, bound)):
        words[gid] = f32_bits(f32_steps(gid, **reference)[-1])
    return words


def draw_f32(directory, compiler, cmd, case, name):
    """Binary32 add, sub, subrev and mul against f32_steps, from the 320-word case format."""
    constants = [0x5eed0000 + i for i in range(16)]
    constants[1] = F32_CAP
    groups, bound = READ_RUNS[-1]

    def write(words):
        case.write_bytes(struct.pack("<337I", groups, *words, bound, *constants[1:]))

    # Every operand and result of an active lane is finite and normal, which the translation does not model otherwise,
    # and at least one multiply is inexact, so the single rounding is observed.
    for gid in range(bound):
        for value in f32_steps(gid):
            assert 0x00800000 <= f32_bits(value) & 0x7fffffff < 0x7f800000, (gid, value)
    assert any(f32_round(x * c) != x * c for x, _, _, _, c, _, _, _ in map(f32_steps, range(bound))), "every multiply exact"
    expected = f32_words(groups, bound)
    for run_groups, run_bound in READ_RUNS:
        case.write_bytes(struct.pack("<337I", run_groups, *f32_words(run_groups, run_bound), run_bound, *constants[1:]))
        result = subprocess.run(cmd)
        if result.returncode:
            return result.returncode
    # Each reference with one operation swapped must fail the run at the first word where it differs.
    for label, reference in (("sub and subrev exchanged", f32_words(groups, bound, wrong=("sub",))),
                             ("subrev exchanged", f32_words(groups, bound, wrong=("subrev",))),
                             ("add for mul", f32_words(groups, bound, wrong=("mul",))),
                             ("sub for add", f32_words(groups, bound, wrong=("add",))),
                             ("inline sign dropped", f32_words(groups, bound, wrong=("inline",)))):
        word = next(i for i, (a, b) in enumerate(zip(expected, reference)) if a != b)
        write(reference)
        bad = subprocess.run(cmd, capture_output=True, text=True)
        if bad.returncode != 1 or f"word={word} expected={reference[word]:08x}" not in bad.stderr:
            raise AssertionError(f"{name}: reference '{label}' escaped detection\n{bad.stderr}")
    # A lane past the bound that writes, and a word past every lane that changes, both fail at their own word.
    for word in (bound, 319):
        stale = expected.copy()
        stale[word] ^= 1
        write(stale)
        bad = subprocess.run(cmd, capture_output=True, text=True)
        if bad.returncode != 1 or f"word={word} expected={stale[word]:08x} actual={SENTINEL:08x}" not in bad.stderr:
            raise AssertionError(f"{name}: sentinel word {word} escaped detection\n{bad.stderr}")
    write(expected)
    source = (directory / "store.hlsl").read_text(encoding="ascii")
    for label, old, new in (("add as sub", "asfloat(a) + asfloat(b)", "asfloat(a) - asfloat(b)"),
                            ("sub operands exchanged", "asfloat(a) - asfloat(b)", "asfloat(b) - asfloat(a)"),
                            ("mul as add", "asfloat(a) * asfloat(b)", "asfloat(a) + asfloat(b)"),
                            ("cap from the next constant", "f32_sub(params[0].y", "f32_sub(params[0].z"),
                            ("literal off by one bit", "0x40490fdbu", "0x40490fdau"),
                            ("inline constant sign", "0xc0800000u", "0x40800000u"),
                            ("inline one for two", "f32_add(0x3f800000u,", "f32_add(0x40000000u,"),
                            ("lane at the bound writes", "if (params[0].x > tid.x)", "if (params[0].x >= tid.x)")):
        mutated = source.replace(old, new)
        if mutated == source:
            raise AssertionError(f"{name}: mutation '{label}' did not change the source")
        src, cso = directory / "mutated.hlsl", directory / "mutated.cso"
        src.write_text(mutated, encoding="ascii")
        subprocess.run([compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src], check=True)
        bad = subprocess.run([*cmd[:3], cso, case], capture_output=True, text=True)
        if bad.returncode != 1 or "Compute readback differs" not in bad.stderr:
            raise AssertionError(f"{name}: mutation '{label}' escaped detection")
    return 0


SHIFT_SUB_CAP = 0x0badf00d


def shift_sub_word(gid, *, logical=False, shift=17, reverse_sub=False, reverse_subrev=False, skip=()):
    """Independent CPU reference of the read_shift_sub result for one lane: the product is shifted left, shifted
    right (arithmetically unless logical), subtracted from the cap, and 0x1234 is reverse-subtracted."""
    word = source_word(gid)
    value = (((word & 0xffff) * (word >> 16) + gid) & 0xffffffff)
    value = value if "shl" in skip else (value << shift) & 0xffffffff
    signed = value - (1 << 32) if value >> 31 else value
    value = value >> 20 if logical else (signed >> 20) & 0xffffffff
    value = (value - SHIFT_SUB_CAP if reverse_sub else SHIFT_SUB_CAP - value) & 0xffffffff
    return (0x1234 - value if reverse_subrev else value - 0x1234) & 0xffffffff


def shift_sub_words(groups, bound, **reference):
    words = [SENTINEL] * 320
    for gid in range(min(32 * groups, bound)):
        words[gid] = shift_sub_word(gid, **reference)
    return words


def draw_shift_sub(directory, compiler, cmd, case, name):
    """Shift and subtract chain against shift_sub_word, from the 320-word case format."""
    constants = [0x5eed0000 + i for i in range(16)]
    constants[1] = SHIFT_SUB_CAP
    groups, bound = READ_RUNS[-1]

    def write(words):
        case.write_bytes(struct.pack("<337I", groups, *words, bound, *constants[1:]))

    expected = shift_sub_words(groups, bound)
    wrong = (("logical right shift", dict(logical=True)), ("shift by 16", dict(shift=16)),
             ("shift dropped", dict(skip=("shl",))), ("sub reversed", dict(reverse_sub=True)),
             ("subrev not reversed", dict(reverse_subrev=True)))
    for label, reference in wrong:
        assert expected != shift_sub_words(groups, bound, **reference), f"{label} would go unseen"
    write(expected)
    result = subprocess.run(cmd)
    if result.returncode:
        return result.returncode
    # Each wrong reference must fail the run at the first word where it differs from the right one.
    for label, reference in wrong:
        wrong_words = shift_sub_words(groups, bound, **reference)
        word = next(i for i, (a, b) in enumerate(zip(expected, wrong_words)) if a != b)
        write(wrong_words)
        bad = subprocess.run(cmd, capture_output=True, text=True)
        if bad.returncode != 1 or f"word={word} expected=" not in bad.stderr:
            raise AssertionError(f"{name}: reference '{label}' escaped detection\n{bad.stderr}")
    write(expected)
    source = (directory / "store.hlsl").read_text(encoding="ascii")
    for label, old, new in (("logical right shift", "asuint(asint(", "(("),
                            ("shift by 16", "(0x00000011u & 31u)", "(0x00000010u & 31u)"),
                            ("sub dropped", "(params[0].y - ", "(0u - "),
                            ("subrev dropped", " - 0x00001234u)", " - 0x00000000u)")):
        mutated = source.replace(old, new)
        if mutated == source:
            raise AssertionError(f"{name}: mutation '{label}' did not change the source")
        src, cso = directory / "mutated.hlsl", directory / "mutated.cso"
        src.write_text(mutated, encoding="ascii")
        subprocess.run([compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src], check=True)
        bad = subprocess.run([*cmd[:3], cso, case], capture_output=True, text=True)
        if bad.returncode != 1 or "Compute readback differs" not in bad.stderr:
            raise AssertionError(f"{name}: mutation '{label}' escaped detection")
    return 0


def draw_mad24(directory, compiler, cmd, case):
    # Dispatch one group, enable only lane zero and leave all 319 other words
    # at their sentinel. With cap = UINT_MAX the entire multiply is observable.
    word = source_word(0)
    low = word % (1 << 24)
    assert word & (3 << 24) == 3 << 24 and low & 1
    expected = (low * low) % (1 << 32)
    values = [expected] + [SENTINEL] * 319
    case.write_bytes(struct.pack("<337I", 1, *values, 1, 0xffffffff, *([0] * 14)))
    result = subprocess.run(cmd)
    if result.returncode:
        return result.returncode

    source = (directory / "store.hlsl").read_text(encoding="ascii")
    src, cso = directory / "mad24-mutant.hlsl", directory / "mad24-mutant.cso"
    for removed_bit in (24, 25):
        # Remove the lowerer's u24 truncation from exactly one source.
        prepared = f"(0x{((1 << removed_bit) | 0xffffff):08x}u & {READ_LOADS['read_copy']})"
        truncated = f"({prepared} & 0xffffffu)"
        assert source.count(truncated) == 1
        src.write_text(source.replace(truncated, prepared), encoding="ascii")
        subprocess.run([compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src], check=True)
        bad = subprocess.run([*cmd[:3], cso, case], capture_output=True, text=True)
        actual = (low * (low + (1 << removed_bit))) % (1 << 32)
        mismatch = f"word=0 expected={expected:08x} actual={actual:08x}"
        if bad.returncode != 1 or mismatch not in bad.stderr:
            raise AssertionError(f"MAD24 bit {removed_bit} mutation escaped detection\n{bad.stderr}")
        print(mismatch)
    return 0


def draw_mul_lo(cmd, case):
    factor = 0x01000003
    modulus = 1 << 32

    def write(groups, bound, words):
        case.write_bytes(struct.pack("<337I", groups, *words, bound, factor, *([0] * 14)))

    for groups, bound in READ_RUNS:
        words = [SENTINEL] * 320
        for gid in range(bound):
            words[gid] = (source_word(gid) * factor) % modulus
        write(groups, bound, words)
        result = subprocess.run(cmd)
        if result.returncode:
            return result.returncode

    assert source_word(0) * factor >= modulus
    controls = ((0, (source_word(0) * (factor & 0xffffff)) % modulus),
                (0, ((source_word(0) & 0xffffff) * factor) % modulus),
                (bound, (source_word(bound) * factor) % modulus))
    for gid, wrong in controls:
        assert wrong != words[gid]
        stale = words.copy()
        stale[gid] = wrong
        write(groups, bound, stale)
        bad = subprocess.run(cmd, capture_output=True, text=True)
        mismatch = f"word={gid} expected={wrong:08x} actual={words[gid]:08x}"
        if bad.returncode != 1 or mismatch not in bad.stderr:
            raise AssertionError(f"read_mul_lo: expected {mismatch}\n{bad.stderr}")
        print(mismatch)
    return 0


def draw(directory, compiler, renderer, backend, cases=None):
    for name, lines, width, binding in (CASES if cases is None else cases):
        src, cso, case = [directory / x for x in ("store.hlsl", "store.cso", "store.case")]
        src.write_text(aot.lower(lines, width, **binding), encoding="ascii")
        subprocess.run([compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, src], check=True)
        cmd = [renderer, backend, "--compute-store-case", cso, case]
        if name == "read_mul_lo":
            result = draw_mul_lo(cmd, case)
            if result:
                return result
            continue
        if name == "read_scalar":
            result = draw_scalar(directory, compiler, cmd, case, name)
            if result:
                return result
            continue
        if name == "read_mad24":
            result = draw_mad24(directory, compiler, cmd, case)
            if result:
                return result
            continue
        if name.startswith(("guard_", "read_")):
            result = (draw_guard if name.startswith("guard_") else draw_alu if name == "read_compute"
                      else draw_vop2 if name == "read_vop2" else draw_f32 if name == "read_f32"
                      else draw_shift_sub if name == "read_shift_sub" else draw_read)(directory, compiler, cmd, case, name)
            if result:
                return result
            continue
        if name == "runtime" or name in GRID_DISPATCHES:
            result = draw_runtime(cmd, case) if name == "runtime" else draw_grid(directory, compiler, cmd, case, name)
            if result:
                return result
            continue
        for groups in ((1, 3) if width == 32 else (1,)):
            words = [SENTINEL] * 320
            if name == "fixed":
                words[5:8] = [11, 23, 0xfffffffe]
                guard_word, stored_word = 0, 5
            elif name == "indexed":
                for i in range(groups * 32):
                    words[3*i+1:3*i+3] = [19, 0xfffffffb]
                guard_word, stored_word = 0, 1
            elif name == "multi_fixed":
                words[2:4] = [0x12345678, 0xfffffffc]
                words[8:10] = [0x12345678, 7]
                guard_word, stored_word = 4, 9
            else:
                for i in range(groups * 32):
                    words[3*i] = 0x00abcdef
                    words[3*i+2] = 0xfffffff0
                guard_word, stored_word = 1, 2
            case.write_bytes(struct.pack("<321I", groups, *words))
            result = subprocess.run(cmd)
            if result.returncode:
                return result.returncode
        words[guard_word] ^= 1
        case.write_bytes(struct.pack("<321I", groups, *words))
        bad = subprocess.run(cmd, capture_output=True, text=True)
        if bad.returncode != 1 or f"word={guard_word} expected=" not in bad.stderr:
            raise AssertionError("A wrong untouched word escaped detection")
        words[guard_word] ^= 1
        words[stored_word] ^= 1
        case.write_bytes(struct.pack("<321I", groups, *words))
        bad = subprocess.run(cmd, capture_output=True, text=True)
        if bad.returncode != 1 or "Compute readback differs" not in bad.stderr:
            raise AssertionError("A wrong stored word escaped detection")
    return 0


if __name__ == "__main__":
    controls()
    mode, *args = sys.argv[1:]
    with tempfile.TemporaryDirectory() as tmp:
        # The store cases and the guard/read cases run as separate CTest entries so each stays within its timeout.
        modes = {"binary": binary, "draw": draw,
                 "draw-mubuf-subword": mubuf_subword_draw,
                 "draw-mubuf-reduction": mubuf_reduction_draw,
                 "draw-mubuf-exchange": mubuf_exchange_draw,
                 "draw-mubuf-exchange-x2": lambda d, *a: mubuf_exchange_draw(d, *a, wide=True),
                 "draw-mubuf-reduction-x2": lambda d, *a: mubuf_reduction_draw(d, *a, wide=True),
                 "draw-mubuf-vector": mubuf_vector_draw,
                 "draw-mubuf-d16": mubuf_d16_draw,
                 "draw-mubuf-load-format-d16": mubuf_format_d16_draw,
                 "draw-mubuf-format": mubuf_format_draw,
                 "draw-mubuf-format-store": mubuf_format_store_draw,
                 "draw-mtbuf-load": mtbuf_load_draw,
                 "draw-mimg-load": mimg_load_draw,
                 "draw-mimg-mip": mimg_mip_draw,
                 "draw-mimg-resinfo": mimg_resinfo_draw,
                 "draw-mimg-packed": mimg_packed_draw,
                 "draw-mimg-store": mimg_store_draw,
                 "compile-mimg-atomic-integer": lambda d, compiler: mimg_atomic_integer(d, compiler),
                 "draw-mimg-atomic-integer": mimg_atomic_integer,
                 "compile-mimg-atomic-minmax": lambda d, compiler: mimg_atomic_integer(d, compiler, minmax=True),
                 "draw-mimg-atomic-minmax": lambda d, *a: mimg_atomic_integer(d, *a, minmax=True),
                 "draw-mimg-packed-store": lambda d, *a: mimg_store_draw(d, *a, packed=True),
                 "draw-ds-route": ds_draw,
                 "draw-lds-qword": lambda d, *a: ds_draw(d, *a, private=True, cases=LDS_QWORD_CASES),
                 "draw-lds-strided": lambda d, *a: ds_draw(d, *a, private=True, cases=LDS_STRIDED_CASES),
                 "draw-lds-wide-replacement": lambda d, *a: ds_draw(d, *a, private=True, cases=LDS_WIDE_REPLACEMENT_CASES),
                 "draw-lds-f32": lambda d, *a: ds_draw(d, *a, private=True, cases=LDS_FLOAT_CASES),
                 "draw-lds-private": lambda d, *a: ds_draw(d, *a, private=True),
                 "draw-lds-packed": lambda d, *a: ds_draw(d, *a, private=True, cases=LDS_PACKED_CASES),
                 "draw-lds-arithmetic": lambda d, *a: ds_draw(d, *a, private=True, cases=LDS_ARITHMETIC_CASES),
                 "draw-lds-replacement": lambda d, *a: ds_draw(d, *a, private=True, cases=LDS_REPLACEMENT_CASES),
                 "draw-f32": lambda d, *a: draw(d, *a, cases=F32_CASES),
                 "draw-mtbuf-store": mtbuf_store_draw,
                 "draw-mubuf-subword-store": mubuf_subword_store_draw,
                 "draw-guard-read": lambda d, *a: draw(d, *a, cases=GUARD_CASES + READ_CASES + ALU_CASES + SCALAR_CASES)}
        sys.exit(modes[mode](Path(tmp), *args))
