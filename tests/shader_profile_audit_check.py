"""Verify DXIL/PSV0 validation, profile classification, and CLI behavior for shader_profile_audit."""
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from resource_inventory import parts  # noqa: E402
import shader_profile_audit  # noqa: E402


def dxil_section(stage, code):
    """Build an owned DXIL section with one compressed FSSM native program entry."""
    base = 240
    start = base + 40
    padded_len = ((40 + len(code) + 4 + 15) // 16) * 16
    limit = base + padded_len
    image = bytearray(limit + 16)
    image[:4] = b"FSSM"
    struct.pack_into("<I", image, 4, 0x101)
    struct.pack_into("<QQ", image, 192, base, padded_len)
    struct.pack_into("<Q", image, 232, 0xDEAD_BEEF_DEAD_BEEF)
    image[start:start + len(code)] = code
    image[start + len(code):limit + 16] = b"\x00\x00\x9f\xbf" * ((limit + 16 - (start + len(code))) // 4)
    compressed = zlib.compress(bytes(image), 9)
    xbch = b"XBCH" + bytes(12) + struct.pack("<I", len(image)) + bytes(4) + compressed
    header = struct.pack("<II4sIII", (stage << 16) | 0x60, 7, b"DXIL", 0x100, 16, 4)
    table, native = 64, 128
    program = header + bytes(table - len(header)) + b"XDAT" + struct.pack("<I", 1) + bytes(4)
    program += struct.pack("<3I", 13, native, len(xbch))
    program += bytes(native - len(program))
    return program + xbch


def psv0_section(stage, *, group=(1, 1, 1), limits=(3, 1), topology=2):
    """Build a minimal 52-byte PSV0 runtime info section."""
    psv = bytearray(52)
    struct.pack_into("<I", psv, 0, 48)
    struct.pack_into("<HH", psv, 16, *limits)
    psv[28] = stage
    psv[31] = topology
    struct.pack_into("<3I", psv, 40, *group)
    return bytes(psv)


def dxbc_container(chunks, marker=1):
    """Build a DXBC container from (fourcc, payload) pairs."""
    header_size = 32 + 4 * len(chunks)
    offsets = []
    body = bytearray()
    cursor = header_size
    for tag, payload in chunks:
        offsets.append(cursor)
        body += tag + struct.pack("<I", len(payload)) + payload
        cursor += 8 + len(payload)
    header = b"DXBC" + bytes([marker]) * 16 + struct.pack("<III", 1, cursor, len(chunks))
    return header + struct.pack(f"<{len(chunks)}I", *offsets) + bytes(body)


def rpf_record(blob, name="owned_shader"):
    """Frame one raw-DEFLATE record compatible with resource_inventory.scene_records."""
    encoder = zlib.compressobj(wbits=-15)
    payload = encoder.compress(blob) + encoder.flush()
    return (name.encode("ascii") + b"\0\0"
            + struct.pack("<4I", len(payload), 0, len(payload), len(blob)) + payload)


def padded_code(tag, total_bytes):
    """Create a 4-byte-aligned code payload with a unique 4-byte tag and one s_endpgm marker."""
    if total_bytes < 8 or total_bytes % 4:
        raise ValueError("Code payload must be at least 8 bytes and 4-byte aligned")
    nops = (total_bytes - 8) // 4
    return tag + (b"\x00\x00\x80\xbf" * nops) + b"\x00\x00\x81\xbf"


def with_offsets(lines):
    sizes = [
        8 if line.startswith(("s_load_", "s_buffer_load_", "exp ")) or "_e64 " in line else 4
        for line in lines
    ]
    return [(sum(sizes[:i]), line) for i, line in enumerate(lines)]


PS_GENERAL_TAG = b"\x01\x00\x00\x00"
PS_ATTR_TAG = b"\x02\x00\x00\x00"
PS_DEPTH_TAG = b"\x03\x00\x00\x00"
PS_COV_TAG = b"\x04\x00\x00\x00"
PS_CB_TAG = b"\x05\x00\x00\x00"
PS_BAD_TAG = b"\x06\x00\x00\x00"
CS_STORE_TAG = b"\x07\x00\x00\x00"
MS_POINT_TAG = b"\x08\x00\x00\x00"
MS_INDEXED_TAG = b"\x09\x00\x00\x00"

PS_GENERAL_LINES = [
    "s_mov_b64 s[50:51], exec",
    "s_wqm_b64 exec, exec",
    "s_mov_b32 m0, s9",
    "v_interp_p1_f32_e32 v8, v0, attr0.x",
    "v_interp_p1_f32_e32 v9, v0, attr0.y",
    "v_interp_p2_f32_e32 v9, v1, attr0.y",
    "v_interp_p2_f32_e32 v8, v1, attr0.x",
    "s_and_b64 exec, exec, s[50:51]",
    "s_mov_b32 s6, s8",
    "s_mov_b32 s7, 0",
    "s_load_dwordx8 s[28:35], s[6:7], null",
    "s_mov_b32 s40, 0x92",
    "s_mov_b32 s41, 0xc00000",
    "s_mov_b32 s42, 0x14500000",
    "s_mov_b32 s43, 0",
    "s_waitcnt lgkmcnt(0)",
    "image_sample v[12:15], v[8:9], s[28:35], s[40:43] dmask:0xf dim:SQ_RSRC_IMG_2D",
    "s_waitcnt vmcnt(0)",
    "v_cvt_pkrtz_f16_f32_e32 v16, v12, v13",
    "v_cvt_pkrtz_f16_f32_e32 v17, v14, v15",
    "exp mrt0 v16, v16, v17, v17 done compr vm",
    "s_endpgm",
]

PS_ATTR_LINES = [
    "s_mov_b32 m0, s10",
    "v_interp_p1_f32_e32 v8, v0, attr0.x",
    "v_interp_p2_f32_e32 v8, v1, attr0.x",
    "v_interp_p1_f32_e32 v9, v0, attr0.y",
    "v_interp_p2_f32_e32 v9, v1, attr0.y",
    "v_cvt_pkrtz_f16_f32_e32 v16, v8, v9",
    "v_cvt_pkrtz_f16_f32_e32 v17, v8, v9",
    "exp mrt0 v16, v16, v17, v17 done compr vm",
    "s_endpgm",
]

PS_DEPTH_LINES = [
    "s_mov_b32 s6, s8",
    "s_mov_b32 s7, 0",
    "s_mov_b32 s10, s9",
    "s_mov_b32 s11, 0",
    "s_mov_b64 s[22:23], exec",
    "s_mov_b64 s[24:25], exec",
    "s_load_dwordx4 s[12:15], s[6:7], 0x480",
    "s_load_dwordx4 s[16:19], s[10:11], null",
    "s_waitcnt lgkmcnt(0)",
    "s_buffer_load_dword s30, s[12:15], 0x4",
    "s_buffer_load_dword s31, s[16:19], null",
    "s_waitcnt lgkmcnt(0)",
    "v_cvt_f32_ubyte0_e32 v5, s30",
    "v_mul_legacy_f32_e32 v7, 0x3e000000, v2",
    "v_fmac_f32_e32 v7, 0x3e000000, v5",
    "v_fract_f32_e32 v7, v7",
    "v_cmpx_gt_f32_e32 s31, v7",
    "s_andn2_b64 s[22:23], s[22:23], exec",
    "s_cbranch_scc0 4",
    "s_and_b64 exec, s[24:25], s[22:23]",
    "exp mrtz, v4, off, off, off done vm",
    "s_endpgm",
    "s_mov_b64 exec, s[22:23]",
    "exp null, off, off, off, off done vm",
    "s_endpgm",
]

PS_COV_LINES = [
    "s_mov_b32 s20, s8",
    "s_mov_b32 s21, 0",
    "s_mov_b32 s30, s9",
    "s_mov_b32 s31, 0",
    "s_mov_b32 s40, s14",
    "s_mov_b32 s41, 0",
    "s_mov_b32 s50, s15",
    "s_mov_b32 s51, 0",
    "s_load_dwordx4 s[60:63], s[50:51], 0x430",
    "s_load_dwordx4 s[68:71], s[30:31], null",
    "s_load_dwordx4 s[72:75], s[20:21], 0x740",
    "s_mov_b64 s[48:49], exec",
    "s_wqm_b64 exec, exec",
    "s_mov_b64 s[52:53], exec",
    "s_mov_b32 m0, s16",
    "s_waitcnt lgkmcnt(0)",
    "s_buffer_load_dword s6, s[72:75], 0xc",
    "s_buffer_load_dword s9, s[68:71], null",
    "v_interp_p1_f32_e32 v8, v0, attr0.x",
    "v_interp_p2_f32_e32 v8, v1, attr0.x",
    "v_interp_p1_f32_e32 v9, v0, attr0.y",
    "v_interp_p2_f32_e32 v9, v1, attr0.y",
    "s_waitcnt lgkmcnt(0)",
    "s_lshl_b32 s12, s6, 5",
    "s_load_dwordx8 s[64:71], s[40:41], s12",
    "s_waitcnt lgkmcnt(0)",
    "image_sample v10, v[8:9], s[64:71], s[60:63] dmask:0x8 dim:SQ_RSRC_IMG_2D",
    "s_waitcnt vmcnt(0)",
    "v_mul_legacy_f32_e32 v11, s9, v10",
    "v_cmpx_gt_f32_e32 s9, v11",
    "s_andn2_b64 s[48:49], s[48:49], exec",
    "s_cbranch_scc0 6",
    "s_wqm_b64 s[54:55], s[48:49]",
    "s_and_b64 exec, s[52:53], s[54:55]",
    "s_and_b64 exec, exec, s[48:49]",
    "exp null off, off, off, off done vm",
    "s_endpgm",
    "s_and_b64 exec, exec, s[48:49]",
    "s_mov_b64 exec, s[48:49]",
    "exp null off, off, off, off done vm",
    "s_endpgm",
]

PS_CB_LINES = [
    "s_mov_b32 s42, s9",
    "s_mov_b32 s43, 0",
    "s_load_dwordx4 s[36:39], s[42:43], null",
    "s_waitcnt lgkmcnt(0)",
    "s_buffer_load_dwordx4 s[12:15], s[36:39], 0x10",
    "s_waitcnt lgkmcnt(0)",
    "v_bad_cb_op v0, s12",
    "exp mrt0 v0, v0, v0, v0 done vm",
    "s_endpgm",
]

PS_BAD_LINES = ["v_bad_op v0, v1", "s_endpgm"]

CS_STORE_LINES = [
    "s_mov_b32 s4, s5",
    "s_mov_b32 s5, 0",
    "s_load_dwordx4 s[12:15], s[4:5], null",
    "v_lshl_add_u32 v3, s6, 2, v0",
    "v_lshl_add_u32 v4, s7, 3, v2",
    "v_lshlrev_b32_e32 v5, 2, v3",
    "s_waitcnt lgkmcnt(0)",
    "buffer_store_dword v4, v[4:5], s[12:15], 0 idxen offen",
    "s_endpgm",
]

MS_POINT_LINES = [
    "s_mov_b32 s10, s17",
    "s_mov_b32 s11, 0",
    "s_load_dwordx4 s[24:27], s[10:11], 0x20",
    "s_load_dwordx4 s[16:19], s[10:11], 0x60",
    "s_load_dwordx4 s[20:23], s[10:11], 0x80",
    "s_waitcnt lgkmcnt(0)",
    "s_buffer_load_dword s15, s[24:27], null",
    "s_buffer_load_dword s30, s[16:19], 0x4",
    "s_buffer_load_dword s31, s[20:23], 0xc",
    "s_waitcnt lgkmcnt(0)",
    "v_mbcnt_lo_u32_b32 v0, -1, 0",
    "v_mbcnt_hi_u32_b32 v0, -1, v0",
    "s_bfe_u32 s8, s3, 0x40018",
    "v_lshl_add_u32 v0, s8, 6, v0",
    "s_mov_b64 s[22:23], exec",
    "v_cmpx_eq_u32_e32 0, v0",
    "s_cbranch_execz 2",
    "s_mov_b32 s7, 0x1003",
    "s_mov_b32 m0, s7",
    "s_cbranch_execz 1",
    "s_sendmsg sendmsg(MSG_GS_ALLOC_REQ)",
    "v_mov_b32_e32 v9, 0x200400",
    "exp prim, v9, off, off, off done",
    "s_mov_b64 exec, s[22:23]",
    "v_cmpx_gt_u32_e32 3, v0",
    "v_cmp_eq_u32_e64 s[10:11], 1, v0",
    "v_cmp_eq_u32_e64 s[12:13], 2, v0",
    "v_mov_b32_e32 v8, 0x40400000",
    "v_mov_b32_e32 v9, 0xbf800000",
    "v_cndmask_b32_e64 v1, v9, v8, s[10:11]",
    "v_cndmask_b32_e64 v2, v9, v8, s[12:13]",
    "v_mov_b32_e32 v3, s30",
    "v_add_f32_e32 v3, s31, v3",
    "v_mov_b32_e32 v4, 0x3f800000",
    "exp pos0, v1, v2, v3, v4 done",
    "s_endpgm",
]

MS_INDEXED_LINES = [
    "s_mov_b32 s10, s17",
    "s_mov_b32 s11, 0",
    "s_load_dwordx4 s[24:27], s[10:11], null",
    "s_mov_b32 s32, s18",
    "s_or_b32 s33, s19, 0x10000",
    "s_mov_b32 s34, -1",
    "s_mov_b32 s35, 0x11014fac",
    "s_buffer_load_dword s6, s[32:35], null",
    "s_waitcnt lgkmcnt(0)",
    "s_buffer_load_dword s15, s[24:27], null",
    "s_lshr_b32 s4, s6, 3",
    "s_and_b32 s4, s4, 60",
    "s_lshr_b32 s5, s6, 7",
    "s_and_b32 s5, s5, 0x1fffffc",
    "s_waitcnt lgkmcnt(0)",
    "v_mbcnt_lo_u32_b32 v0, -1, 0",
    "v_mbcnt_hi_u32_b32 v0, -1, v0",
    "s_bfe_u32 s8, s3, 0x40018",
    "v_lshl_add_u32 v0, s8, 6, v0",
    "s_mov_b64 s[22:23], exec",
    "v_cmpx_eq_u32_e32 0, v0",
    "s_cbranch_execz 2",
    "s_mov_b32 s7, 0x1003",
    "s_mov_b32 m0, s7",
    "s_cbranch_execz 1",
    "s_sendmsg sendmsg(MSG_GS_ALLOC_REQ)",
    "v_mov_b32_e32 v9, 0x200400",
    "exp prim, v9, off, off, off done",
    "s_mov_b64 exec, s[22:23]",
    "v_cmpx_gt_u32_e32 3, v0",
    "v_lshrrev_b32_e32 v12, 1, v0",
    "v_lshl_add_u32 v12, v12, 2, 4",
    "buffer_load_dword v13, v12, s[24:27], 0 offen",
    "v_and_b32_e32 v10, 1, v0",
    "v_cmp_eq_u32_e32 vcc, 1, v10",
    "s_waitcnt vmcnt(0)",
    "v_lshrrev_b32_e32 v14, 16, v13",
    "v_and_b32_e32 v13, 0xffff, v13",
    "v_cndmask_b32_e32 v1, v13, v14, vcc",
    "v_mad_u32_u24 v1, s4, v1, s5",
    "buffer_load_dwordx3 v[1:3], v1, s[32:35], 0 offen",
    "s_waitcnt vmcnt(0)",
    "v_mov_b32_e32 v4, 0x3f800000",
    "exp pos0, v1, v2, v3, v4 done",
    "s_endpgm",
]

DISASM_BY_TAG = {
    PS_GENERAL_TAG: PS_GENERAL_LINES,
    PS_ATTR_TAG: PS_ATTR_LINES,
    PS_DEPTH_TAG: PS_DEPTH_LINES,
    PS_COV_TAG: PS_COV_LINES,
    PS_CB_TAG: PS_CB_LINES,
    PS_BAD_TAG: PS_BAD_LINES,
    CS_STORE_TAG: CS_STORE_LINES,
    MS_POINT_TAG: MS_POINT_LINES,
    MS_INDEXED_TAG: MS_INDEXED_LINES,
}


def stub_disassemble(code, library, *, cpu=b"gfx1013", offsets=False, features=b""):
    lines = DISASM_BY_TAG.get(bytes(code[:4]))
    if lines is None:
        raise ValueError(f"Unknown synthetic code tag: {bytes(code[:4]).hex()}")
    return with_offsets(lines) if offsets else list(lines)


def make_shader_blob(stage, tag, *, code_bytes=16, psv_group=(1, 1, 1), marker=1):
    chunks = [(b"DXIL", dxil_section(stage, padded_code(tag, code_bytes)))]
    if stage in (5, 13):
        chunks.append((b"PSV0", psv0_section(stage, group=psv_group)))
    return dxbc_container(chunks, marker=marker)


class ShaderProfileAuditChecks(unittest.TestCase):
    def test_classify_pixel_compute_and_mesh_profiles(self):
        with (patch.object(shader_profile_audit.pixel_aot, "disassemble", side_effect=stub_disassemble),
              patch.object(shader_profile_audit.mesh_entry_aot, "disassemble", side_effect=stub_disassemble)):
            for tag, expected_profile, expected_snippet in (
                (PS_GENERAL_TAG, "general", "SV_Target"),
                (PS_ATTR_TAG, "attribute", "float4 uv : TEXCOORD0"),
                (PS_DEPTH_TAG, "depth", "SV_Depth"),
                (PS_COV_TAG, "coverage", "discard;"),
            ):
                blob = make_shader_blob(0, tag)
                emitted = {}
                profiles, _ = shader_profile_audit.classify(
                    blob, parts(blob), 0, None, lambda p, s, out=emitted: out.setdefault(p, s)
                )
                self.assertIn(expected_profile, profiles)
                if expected_profile == "coverage":
                    self.assertIn("coverage-0", emitted)
                    self.assertIn("coverage-1", emitted)
                    self.assertIn(expected_snippet, emitted["coverage-0"])
                else:
                    self.assertIn(expected_snippet, emitted[expected_profile])

            cs_blob = make_shader_blob(5, CS_STORE_TAG, psv_group=(4, 1, 8))
            cs_emitted = {}
            profiles, reason = shader_profile_audit.classify(
                cs_blob, parts(cs_blob), 5, None, lambda p, s: cs_emitted.setdefault(p, s)
            )
            self.assertEqual(profiles, ["store"])
            self.assertEqual(reason, "store lowering")
            self.assertIn("outputData.Store(", cs_emitted["store"])

            ms_point_blob = make_shader_blob(13, MS_POINT_TAG, code_bytes=832, psv_group=(128, 1, 1))
            ms_point_emitted = {}
            profiles, _ = shader_profile_audit.classify(
                ms_point_blob, parts(ms_point_blob), 13, None, lambda p, s: ms_point_emitted.setdefault(p, s)
            )
            self.assertIn("point mesh", profiles)
            self.assertIn("pointData", ms_point_emitted["point-mesh"])

            ms_idx_blob = make_shader_blob(13, MS_INDEXED_TAG, code_bytes=512, psv_group=(128, 1, 1))
            ms_idx_emitted = {}
            profiles, _ = shader_profile_audit.classify(
                ms_idx_blob, parts(ms_idx_blob), 13, None, lambda p, s: ms_idx_emitted.setdefault(p, s)
            )
            self.assertEqual(profiles, ["indexed mesh"])
            self.assertIn("vertexData.Load(", ms_idx_emitted["indexed-mesh"])

            for non_profile_stage in (1, 3, 4, 99):
                stage_blob = make_shader_blob(non_profile_stage, PS_GENERAL_TAG)
                self.assertEqual(
                    shader_profile_audit.classify(stage_blob, parts(stage_blob), non_profile_stage, None, None),
                    ([], "no stage profile"),
                )

    def test_scan_source_dir_cb_diagnostic_and_cli(self):
        ps_ok = make_shader_blob(0, PS_GENERAL_TAG, marker=1)
        ps_cb = make_shader_blob(0, PS_CB_TAG, marker=2)
        ps_bad = make_shader_blob(0, PS_BAD_TAG, marker=3)
        cs_ok = make_shader_blob(5, CS_STORE_TAG, psv_group=(4, 1, 8), marker=4)
        ms_ok = make_shader_blob(13, MS_POINT_TAG, code_bytes=832, psv_group=(128, 1, 1), marker=5)
        vs_skip = make_shader_blob(1, PS_GENERAL_TAG, marker=6)
        hs_skip = make_shader_blob(3, PS_GENERAL_TAG, marker=7)
        archive = (
            rpf_record(ps_ok, "ps_ok")
            + rpf_record(ps_cb, "ps_cb")
            + rpf_record(ps_bad, "ps_bad")
            + rpf_record(cs_ok, "cs_ok")
            + rpf_record(ms_ok, "ms_ok")
            + rpf_record(vs_skip, "vs_skip")
            + rpf_record(hs_skip, "hs_skip")
        )

        with (patch.object(shader_profile_audit.pixel_aot, "disassemble", side_effect=stub_disassemble),
              patch.object(shader_profile_audit.mesh_entry_aot, "disassemble", side_effect=stub_disassemble)):
            group, read, method, stop, probe = shader_profile_audit.cb_diagnostic(parts(ps_cb)[b"DXIL"], None)
            self.assertEqual(group, "entry s9 + 0x0")
            self.assertEqual((read.width, read.offset), (4, 16))
            self.assertEqual(method, "4 DWORD(s) at 0x10")
            self.assertEqual(stop, "unsupported v_bad_cb_op")
            self.assertIsNotNone(probe)

            other_cb = make_shader_blob(0, PS_DEPTH_TAG, marker=7)
            other_group, _, other_method, other_stop, other_probe = shader_profile_audit.cb_diagnostic(
                parts(other_cb)[b"DXIL"], None
            )
            self.assertEqual(other_group, "entry s8 + 0x480")
            self.assertIsNone(other_method)
            self.assertIsNone(other_stop)
            self.assertIsNone(other_probe)

            untraced = make_shader_blob(0, PS_BAD_TAG, marker=8)
            self.assertEqual(
                shader_profile_audit.cb_diagnostic(parts(untraced)[b"DXIL"], None),
                ("untraced descriptor", None, None, None, None),
            )

            with tempfile.TemporaryDirectory() as directory:
                source_dir = Path(directory) / "hlsl"
                result = shader_profile_audit.scan(archive, None, source_dir, cb_reads=True)
                self.assertEqual(dict(result["records"]), {"PS": 3, "CS": 1, "MS": 1, "VS": 1, "3": 1})
                self.assertEqual(dict(result["hlsl_records"]), {"PS": 1, "CS": 1, "MS": 1})
                self.assertEqual(dict(result["profiles"]["PS"]), {"general": 1})
                self.assertEqual(dict(result["profiles"]["CS"]), {"store": 1})
                self.assertEqual(dict(result["profiles"]["MS"]), {"point mesh": 1})
                self.assertEqual(result["first_stops"]["3"], [("no stage profile", 1)])
                self.assertIn(("pixel lowering / first general profile: unsupported v_bad_op", 1),
                              result["first_stops"]["PS"])
                self.assertEqual(result["cb_reads"]["first_descriptor"], [("entry s9 + 0x0", 1)])
                self.assertEqual(result["cb_reads"]["read_modes"], [("4 DWORD(s), immediate", 1)])
                self.assertEqual(result["cb_reads"]["s9_zero_reads"], [("4 DWORD(s) at 0x10", 1)])
                self.assertEqual(result["cb_reads"]["s9_zero_full_shader_stops"], [("unsupported v_bad_cb_op", 1)])
                self.assertTrue((source_dir / "s9-slot0-read-probe.hlsl").is_file())
                self.assertTrue(list((source_dir / "general").glob("*.hlsl")))
                self.assertTrue(list((source_dir / "store").glob("*.hlsl")))
                self.assertTrue(list((source_dir / "point-mesh").glob("*.hlsl")))

                # CLI JSON execution with --source-dir and --cb-reads on an archive file.
                archive_path = Path(directory) / "synthetic.rpf"
                cli_source = Path(directory) / "cli_hlsl"
                archive_path.write_bytes(archive)
                driver = "\n".join([
                    "import sys",
                    "from unittest.mock import patch",
                    f"sys.path.insert(0, {str(ROOT / 'tests')!r})",
                    f"sys.path.insert(0, {str(ROOT / 'tools')!r})",
                    "import shader_profile_audit, shader_profile_audit_check as chk",
                    "sys.argv = ['shader_profile_audit.py', sys.argv[1], '--llvm', 'stub', '--source-dir', sys.argv[2], '--cb-reads']",
                    "with patch.object(shader_profile_audit.pixel_aot, 'disassemble', side_effect=chk.stub_disassemble), \\",
                    "     patch.object(shader_profile_audit.mesh_entry_aot, 'disassemble', side_effect=chk.stub_disassemble):",
                    "    shader_profile_audit.main()",
                ])
                proc = subprocess.run(
                    [sys.executable, "-B", "-c", driver, str(archive_path), str(cli_source)],
                    check=True, capture_output=True, text=True,
                )
                cli_json = json.loads(proc.stdout)
                self.assertEqual(cli_json["records"], {"PS": 3, "CS": 1, "MS": 1, "VS": 1, "3": 1})
                self.assertEqual(cli_json["hlsl_records"], {"PS": 1, "CS": 1, "MS": 1})
                self.assertTrue((cli_source / "s9-slot0-read-probe.hlsl").is_file())

    def test_negative_controls_and_missing_or_empty_archives(self):
        ps_blob = make_shader_blob(0, PS_GENERAL_TAG)
        cs_blob = make_shader_blob(5, CS_STORE_TAG, psv_group=(4, 1, 8))
        ms_blob = make_shader_blob(13, MS_POINT_TAG, code_bytes=832, psv_group=(128, 1, 1))

        # Missing DXIL section and malformed sections mapping.
        for bad_sections in (None, {}, {b"DXIL": b""}, {b"DXIL": b"short"}, {b"PSV0": psv0_section(5)}):
            with self.subTest(bad_sections=bad_sections), self.assertRaises(ValueError):
                shader_profile_audit.classify(ps_blob, bad_sections, 0, None, None)
        for bad_program in (None, b"", b"short", b"DXIL_WITHOUT_XDAT" + bytes(20)):
            with self.subTest(bad_program=bad_program), self.assertRaises(ValueError):
                shader_profile_audit.cb_diagnostic(bad_program, None)

        # Missing or invalid/truncated PSV0 for CS and MS.
        for stage, blob in ((5, cs_blob), (13, ms_blob)):
            base = {b"DXIL": parts(blob)[b"DXIL"]}
            oversized_psv = struct.pack("<I", 52) + psv0_section(stage)[4:]
            for bad_psv in (None, b"", b"\x01", b"\x30\x00\x00\x00" + bytes(16), oversized_psv, psv0_section(99)):
                sections = dict(base)
                if bad_psv is not None:
                    sections[b"PSV0"] = bad_psv
                with self.subTest(stage=stage, psv_len=None if bad_psv is None else len(bad_psv)):
                    with self.assertRaises(ValueError):
                        shader_profile_audit.classify(blob, sections, stage, None, None)

        # Truncated or non-bytes mesh container payload.
        ms_sections = {b"DXIL": parts(ms_blob)[b"DXIL"], b"PSV0": parts(ms_blob)[b"PSV0"]}
        for bad_blob in (None, b"short"):
            with self.subTest(bad_blob=bad_blob), self.assertRaises(ValueError):
                shader_profile_audit.classify(bad_blob, ms_sections, 13, None, None)

        # Malformed code / group_size in pixel_profiles and compute_profiles.
        for bad_code in (None, b"\x01\x02\x03"):
            with self.assertRaises(ValueError):
                shader_profile_audit.pixel_profiles(bad_code, None, None)
            with self.assertRaises(ValueError):
                shader_profile_audit.compute_profiles(bad_code, (1, 1, 1), None, None)
        for bad_group in ((1, 1), (0, 1, 1), (True, 1, 1), "1x1x1"):
            with self.assertRaises(ValueError):
                shader_profile_audit.compute_profiles(b"\x00\x00\x81\xbf", bad_group, None, None)

        # Unsupported instruction diagnostic.
        ps_bad = make_shader_blob(0, PS_BAD_TAG)
        with (patch.object(shader_profile_audit.pixel_aot, "disassemble", side_effect=stub_disassemble),
              patch.object(shader_profile_audit.mesh_entry_aot, "disassemble", side_effect=stub_disassemble)):
            profiles, reason = shader_profile_audit.classify(ps_bad, parts(ps_bad), 0, None, None)
            self.assertEqual(profiles, [])
            self.assertEqual(reason, "pixel lowering / first general profile: unsupported v_bad_op")

        # Unknown shader stage (negative or non-int).
        for unknown_stage in (-1, True, "PS"):
            with self.subTest(stage=unknown_stage), self.assertRaises(ValueError):
                shader_profile_audit.classify(ps_blob, parts(ps_blob), unknown_stage, None, None)

        # Empty and missing archive handling in scan() and CLI.
        empty = shader_profile_audit.scan(b"", None, cb_reads=True)
        self.assertEqual(dict(empty["records"]), {})
        self.assertEqual(dict(empty["hlsl_records"]), {})
        self.assertEqual(empty["cb_reads"]["first_descriptor"], [])

        with self.assertRaises(ValueError):
            shader_profile_audit.scan(None, None)

        with tempfile.TemporaryDirectory() as directory:
            empty_path = Path(directory) / "empty.rpf"
            empty_path.write_bytes(b"")
            missing_path = Path(directory) / "missing.rpf"
            with self.assertRaises(ValueError):
                shader_profile_audit.scan(missing_path, None)

            cli = [sys.executable, "-B", str(ROOT / "tools/shader_profile_audit.py")]
            ok = subprocess.run(cli + [str(empty_path), "--llvm", "stub", "--cb-reads"],
                                check=True, capture_output=True, text=True)
            parsed = json.loads(ok.stdout)
            self.assertEqual(parsed["records"], {})
            self.assertEqual(parsed["cb_reads"]["first_descriptor"], [])

            fail = subprocess.run(cli + [str(missing_path), "--llvm", "stub"],
                                  capture_output=True, text=True)
            self.assertEqual(fail.returncode, 1)
            self.assertEqual(fail.stdout, "")
            self.assertIn("Shader profile audit:", fail.stderr)


if __name__ == "__main__":
    unittest.main()
