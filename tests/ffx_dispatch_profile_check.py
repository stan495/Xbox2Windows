"""Synthetic PE control suite for ffx_dispatch_profile.inspect."""

from contextlib import redirect_stdout
import hashlib
import io
from pathlib import Path
import struct
import sys
import unittest

import pefile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from ffx_dispatch_profile import inspect  # noqa: E402


IMAGE_BASE = 0x180000000
ENDPGM = b"\x00\x00\x81\xbf"


def pack_dxbc(chunks):
    """Build a minimal valid DXBC container from (tag, payload) pairs."""
    header_size = 32 + 4 * len(chunks)
    offsets = []
    body = bytearray()
    for tag, payload in chunks:
        offsets.append(header_size + len(body))
        body.extend(struct.pack("<4sI", tag, len(payload)))
        body.extend(payload)
    total = header_size + len(body)
    header = bytearray(32)
    header[0:4] = b"DXBC"
    struct.pack_into("<3I", header, 20, 1, total, len(chunks))
    return bytes(header) + struct.pack(f"<{len(chunks)}I", *offsets) + bytes(body)


def pack_psv0(threads_x, threads_y=1, threads_z=1):
    """Build a minimal PSV0 chunk with thread group dimensions at offset 40."""
    buf = bytearray(52)
    struct.pack_into("<3I", buf, 40, threads_x, threads_y, threads_z)
    return bytes(buf)


def pack_fspx(code_payload, *, trailing=b"\xaa\xbb\xcc\xdd"):
    """Build an FSPX ACCC chunk with a metadata section and a tag-6 shader section."""
    meta = b"\x01\x02\x03\x04"
    sec_hdr = bytes(0x24)
    sec_body = sec_hdr + code_payload + trailing
    count = 2
    table_end = 16 + count * 16
    meta_off = table_end
    code_off = meta_off + len(meta)
    header = struct.pack("<4s3I", b"FSPX", 0, count, 0)
    table = struct.pack("<4I4I", 1, meta_off, len(meta), 0, 6, code_off, len(sec_body), 0)
    return header + table + meta + sec_body


def build_synthetic_pe():
    """Construct a minimal PE64 image satisfying ffx_dispatch_profile.inspect()."""
    text_rva = 0x1000
    text_size = 0x26000
    rdata_rva = 0x27000
    rdata_size = 0xA9000
    total_size = rdata_rva + rdata_size
    buf = bytearray(total_size)

    # DOS header -> PE header at 0x80
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 0x80)
    buf[0x80:0x84] = b"PE\x00\x00"
    # COFF FileHeader (20 bytes at 0x84)
    struct.pack_into("<HHIIIHH", buf, 0x84, 0x8664, 2, 0, 0, 0, 0xF0, 0x2022)
    # OptionalHeader64 (240 bytes at 0x98)
    struct.pack_into("<H", buf, 0x98, 0x20B)
    struct.pack_into("<Q", buf, 0x98 + 24, IMAGE_BASE)
    struct.pack_into("<II", buf, 0x98 + 32, 0x1000, 0x200)
    struct.pack_into("<II", buf, 0x98 + 56, total_size, 0x200)
    struct.pack_into("<I", buf, 0x98 + 108, 16)

    # Section headers at 0x188
    struct.pack_into("<8sIIIIIIHHI", buf, 0x188,
                     b".text\x00\x00\x00", text_size, text_rva, text_size, text_rva,
                     0, 0, 0, 0, 0x60000020)
    struct.pack_into("<8sIIIIIIHHI", buf, 0x1B0,
                     b".rdata\x00\x00", rdata_size, rdata_rva, rdata_size, rdata_rva,
                     0, 0, 0, 0, 0x40000040)

    cursor = [0xC0000]

    def alloc(data):
        rva = cursor[0]
        buf[rva:rva + len(data)] = data
        cursor[0] = (rva + len(data) + 15) & ~15
        return rva

    def write_bindings(record, offset, items):
        table = record + offset
        struct.pack_into("<I", buf, table, len(items))
        if not items:
            return
        name_ptrs = bytearray()
        slots = bytearray()
        for name, slot in items.items():
            str_rva = alloc(name.encode("ascii") + b"\x00")
            name_ptrs.extend(struct.pack("<Q", IMAGE_BASE + str_rva))
            slots.extend(struct.pack("<I", slot))
        struct.pack_into("<QQ", buf, table + 8,
                         IMAGE_BASE + alloc(name_ptrs),
                         IMAGE_BASE + alloc(slots))

    def write_resource_table(start, entries):
        for i in range(12):
            row = start + i * 0x84
            rid, name = entries[i] if i < len(entries) else (100 + i, f"unused_{i}")
            struct.pack_into("<I", buf, row, rid)
            encoded = name.encode("utf-16le") + b"\x00\x00"
            buf[row + 4:row + 4 + len(encoded)] = encoded

    def write_shader(record, container):
        blob_rva = alloc(container)
        struct.pack_into("<IIQ", buf, record, len(container), 0, IMAGE_BASE + blob_rva)

    # SRV and UAV resource ID tables
    write_resource_table(0x6FB60, [(5, "r_other"), (16, "r_rcas_input")])
    write_resource_table(0x72E00, [(7, "rw_other"), (16, "rw_mlsr_output_color")])

    # RCAS permutation selector at 0x6FB20 (8 zeros)
    buf[0x6FB20:0x6FB40] = bytes(32)

    # 27-case dispatch table at 0x260C0
    pass_records = [0x80000 + i * 0x100 for i in range(26)] + [0xBD830]
    for index, record in enumerate(pass_records):
        stub_rva = 0x10000 + index * 16
        struct.pack_into("<I", buf, 0x260C0 + 4 * index, stub_rva)
        disp = (record + 8) - (stub_rva + 7)
        buf[stub_rva:stub_rva + 7] = b"\x48\x8d\x05" + struct.pack("<i", disp)
        write_bindings(record, 0x40, {"r_other": 0})
        write_bindings(record, 0x68, {"rw_mlsr_output_color": 2} if index == 26 else {"rw_other": 0})

    write_bindings(pass_records[24], 0xB8, {"ScratchBuffer": 11})
    write_bindings(pass_records[24], 0x90, {"InitializerBuffer": 18})
    write_bindings(pass_records[25], 0xB8, {"ScratchBuffer": 11})

    reader_record = 0xBC0D0
    write_bindings(reader_record, 0x40, {"r_rcas_input": 18})
    write_bindings(reader_record, 0x68, {"rw_rcas_output": 11})
    write_bindings(reader_record, 0xE0, {})

    payloads = {
        "pass24": b"\x01\x00\x80\xbe\x02\x00\x80\xbe" + ENDPGM,
        "pass25": b"\x11\x22\x33\x44" + ENDPGM,
        "producer": b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11" + ENDPGM,
        "rcas": b"\x55\x66\x77\x88" + ENDPGM,
    }
    shader_targets = [
        ("pass24", pass_records[24], 64),
        ("pass25", pass_records[25], 32),
        ("producer", pass_records[26], 256),
        ("rcas", reader_record, 64),
    ]
    for name, record, threads in shader_targets:
        container = pack_dxbc([(b"PSV0", pack_psv0(threads)), (b"ACCC", pack_fspx(payloads[name]))])
        write_shader(record, container)

    # Initializer selector at 0x78E5..0x78F1 and 128 KiB payload at 0x4EEE0
    init_disp = 0x4EEE0 - 0x78F1
    buf[0x78E5:0x78F1] = b"\xba" + struct.pack("<I", 0x20000) + b"\x48\x8d\x05" + struct.pack("<i", init_disp)
    init_bytes = bytes((i * 37 + 11) & 0xFF for i in range(0x20000))
    buf[0x4EEE0:0x4EEE0 + 0x20000] = init_bytes

    meta = {
        "pass_records": pass_records,
        "reader_record": reader_record,
        "payloads": {**payloads, "initializer": init_bytes},
        "write_bindings": write_bindings,
        "write_shader": write_shader,
    }
    return buf, meta


def run_inspect(buf):
    with pefile.PE(data=bytes(buf), fast_load=True) as pe:
        with redirect_stdout(io.StringIO()):
            return dict(inspect(pe))


class FfxDispatchProfileChecks(unittest.TestCase):
    def test_synthetic_pe_inspect_positive_controls(self):
        buf, meta = build_synthetic_pe()
        extracted = run_inspect(buf)
        self.assertEqual(list(extracted), ["pass24", "pass25", "producer", "rcas", "initializer"])
        for name, expected in meta["payloads"].items():
            with self.subTest(program=name):
                self.assertEqual(extracted[name], expected)
                self.assertEqual(
                    hashlib.sha256(extracted[name]).hexdigest(),
                    hashlib.sha256(expected).hexdigest(),
                )

    def test_unexpected_scratch_and_initializer_bindings_refused(self):
        for label, pass_idx, offset, bad_map, msg in (
            ("pass 24 scratch", 24, 0xB8, {"ScratchBuffer": 12}, "pass 24 scratch binding"),
            ("pass 25 scratch", 25, 0xB8, {"OtherBuffer": 11}, "pass 25 scratch binding"),
            ("pass 24 initializer", 24, 0x90, {"InitializerBuffer": 19}, "pass 24 model binding"),
        ):
            with self.subTest(case=label):
                buf, meta = build_synthetic_pe()
                meta["write_bindings"](meta["pass_records"][pass_idx], offset, bad_map)
                with self.assertRaisesRegex(ValueError, msg):
                    run_inspect(buf)

    def test_earlier_id16_reader_refused(self):
        buf, meta = build_synthetic_pe()
        meta["write_bindings"](meta["pass_records"][10], 0x40, {"r_rcas_input": 0})
        with self.assertRaisesRegex(ValueError, "earlier ID 16 reader in pass 10"):
            run_inspect(buf)

    def test_unexpected_psv0_thread_count_refused(self):
        buf, meta = build_synthetic_pe()
        bad = pack_dxbc([(b"PSV0", pack_psv0(128)), (b"ACCC", pack_fspx(b"\x01\x02\x03\x04" + ENDPGM))])
        meta["write_shader"](meta["pass_records"][24], bad)
        with self.assertRaisesRegex(ValueError, "Unexpected pass24 group size"):
            run_inspect(buf)

    def test_missing_and_truncated_psv0_refused(self):
        for label, chunks in (
            ("missing PSV0", [(b"ACCC", pack_fspx(b"\x01\x02\x03\x04" + ENDPGM))]),
            ("truncated PSV0", [(b"PSV0", b"\x00" * 44), (b"ACCC", pack_fspx(b"\x01\x02\x03\x04" + ENDPGM))]),
        ):
            with self.subTest(case=label):
                buf, meta = build_synthetic_pe()
                meta["write_shader"](meta["pass_records"][24], pack_dxbc(chunks))
                with self.assertRaisesRegex(ValueError, "PSV0"):
                    run_inspect(buf)

    def test_missing_or_truncated_fspx_magic_and_header_refused(self):
        for label, accc in (
            ("wrong magic", b"NOPE" + b"\x00" * 32),
            ("truncated header", b"FSPX\x00\x00\x00\x00"),
        ):
            with self.subTest(case=label):
                buf, meta = build_synthetic_pe()
                meta["write_shader"](meta["pass_records"][24], pack_dxbc([(b"PSV0", pack_psv0(64)), (b"ACCC", accc)]))
                with self.assertRaisesRegex(ValueError, "native compute shader"):
                    run_inspect(buf)

        buf, meta = build_synthetic_pe()
        meta["write_shader"](meta["pass_records"][24], pack_dxbc([(b"PSV0", pack_psv0(64))]))
        with self.assertRaisesRegex(ValueError, "native compute shader"):
            run_inspect(buf)

    def test_out_of_bounds_fspx_section_table_and_section_bounds_refused(self):
        # Truncated section table (claims 4 sections, only 16 bytes follow header)
        truncated_table = struct.pack("<4s3I", b"FSPX", 0, 4, 0) + b"\x00" * 16
        buf, meta = build_synthetic_pe()
        meta["write_shader"](meta["pass_records"][24], pack_dxbc([(b"PSV0", pack_psv0(64)), (b"ACCC", truncated_table)]))
        with self.assertRaisesRegex(ValueError, "FSPX section table"):
            run_inspect(buf)

        # Out-of-bounds tag-6 section span (offset + size exceeds ACCC payload)
        oob_section = struct.pack("<4s3I4I", b"FSPX", 0, 1, 0, 6, 32, 0x200, 0) + b"\x00" * 0x40
        buf, meta = build_synthetic_pe()
        meta["write_shader"](meta["pass_records"][24], pack_dxbc([(b"PSV0", pack_psv0(64)), (b"ACCC", oob_section)]))
        with self.assertRaisesRegex(ValueError, "Out-of-bounds pass24 FSPX code section"):
            run_inspect(buf)

        # Missing tag-6 section
        no_tag6 = struct.pack("<4s3I4I", b"FSPX", 0, 1, 0, 1, 32, 8, 0) + b"\x00" * 8
        buf, meta = build_synthetic_pe()
        meta["write_shader"](meta["pass_records"][24], pack_dxbc([(b"PSV0", pack_psv0(64)), (b"ACCC", no_tag6)]))
        with self.assertRaisesRegex(ValueError, "FSPX code section"):
            run_inspect(buf)

    def test_missing_and_duplicate_endpgm_terminator_refused(self):
        for label, code in (
            ("missing s_endpgm", b"\x01\x02\x03\x04\x05\x06\x07\x08"),
            ("duplicate s_endpgm", ENDPGM + b"\x01\x02\x03\x04" + ENDPGM),
        ):
            with self.subTest(case=label):
                buf, meta = build_synthetic_pe()
                bad = pack_dxbc([(b"PSV0", pack_psv0(64)), (b"ACCC", pack_fspx(code, trailing=b""))])
                meta["write_shader"](meta["pass_records"][24], bad)
                with self.assertRaisesRegex(ValueError, "s_endpgm"):
                    run_inspect(buf)

    def test_unexpected_rcas_permutation_selector_refused(self):
        buf, _ = build_synthetic_pe()
        struct.pack_into("<I", buf, 0x6FB20 + 12, 1)
        with self.assertRaisesRegex(ValueError, "RCAS permutation selector"):
            run_inspect(buf)

    def test_unexpected_writer_rcas_bindings_and_initializer_refused(self):
        buf, meta = build_synthetic_pe()
        meta["write_bindings"](meta["pass_records"][26], 0x68, {"rw_other": 2})
        with self.assertRaisesRegex(ValueError, "Unexpected ID 16 writers"):
            run_inspect(buf)

        buf, meta = build_synthetic_pe()
        meta["write_bindings"](meta["reader_record"], 0xE0, {"extra": 0})
        with self.assertRaisesRegex(ValueError, "Unexpected RCAS shader bindings"):
            run_inspect(buf)

        buf, _ = build_synthetic_pe()
        struct.pack_into("<I", buf, 0x78E6, 0x10000)
        with self.assertRaisesRegex(ValueError, "Unexpected i8 initializer"):
            run_inspect(buf)


if __name__ == "__main__":
    unittest.main()
