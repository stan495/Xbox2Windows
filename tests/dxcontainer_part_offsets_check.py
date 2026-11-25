"""Synthetic DXContainer directory-order and overlap regression fixtures."""

from collections import defaultdict
from pathlib import Path
import struct
import sys
import unittest
import zlib

import pefile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from call_evidence import shader_containers
from resource_inventory import dxil_program, parts, scene_records, shader_markers


def minimal_pe(section_payload, *, overlay=b"", image_base=0x180000000):
    """Build a minimal PE32+ image with one mapped .rdata section and an optional unmapped overlay."""
    pe_offset = 0x80
    section_rva = 0x1000
    raw_offset = 0x200
    raw_size = max(0x200, ((len(section_payload) + 0x1FF) // 0x200) * 0x200)
    virtual_size = max(1, len(section_payload))
    size_of_image = section_rva + ((virtual_size + 0xFFF) // 0x1000) * 0x1000
    buf = bytearray(raw_offset + raw_size)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 60, pe_offset)
    buf[pe_offset:pe_offset + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", buf, pe_offset + 4, 0x8664, 1, 0, 0, 0, 240, 0x2022)
    opt = pe_offset + 24
    struct.pack_into("<HBBIIIIIQIIHHHHHHIIIIHHQQQQII", buf, opt,
                     0x20B, 0, 0, 0, raw_size, 0, 0, section_rva, image_base,
                     0x1000, 0x200, 6, 0, 0, 0, 6, 0, 0, size_of_image, raw_offset,
                     0, 3, 0, 0, 0, 0, 0, 0, 16)
    sec = opt + 240
    struct.pack_into("<8sIIIIIIHHI", buf, sec,
                     b".rdata\0\0", virtual_size, section_rva, raw_size, raw_offset,
                     0, 0, 0, 0, 0x40000040)
    buf[raw_offset:raw_offset + len(section_payload)] = section_payload
    return bytes(buf) + overlay


def container(*, reversed_directory=False, overlapping=False, extra_fourcc=None, stage_kind=0):
    # This is a deliberately minimal framing probe, not executable DXIL.
    # No third-party shaders or game assets are required.
    program = struct.pack("<II4sIII", stage_kind << 16, 8, b"DXIL", 0, 16, 4) + b"BC\xc0\xde" + bytes(4)
    assert len(program) == 32
    dxil = b"DXIL" + struct.pack("<I", len(program)) + program
    note = b"NOTE" + struct.pack("<I", 4) + b"test"
    if extra_fourcc is not None:
        assert len(extra_fourcc) == 4 and not overlapping
    sections = [dxil, note]
    if extra_fourcc is not None:
        sections.append(extra_fourcc + struct.pack("<I", 5) + b"extra")
    table_end = 32 + 4 * len(sections)
    offsets = []
    cursor = table_end
    for section in sections:
        offsets.append(cursor)
        cursor += len(section)
    physical = b"".join(sections)
    if overlapping:
        # Make a second valid part header WITHIN the first one's payload.
        # In this case both intervals may be in-bounds, but overlap.
        physical = b"OUTR" + struct.pack("<I", 28) + b"INNR" + struct.pack("<I", 4) + b"test" + bytes(16)
        offsets = [table_end, table_end + 8]
    if reversed_directory:
        offsets.reverse()
    size = table_end + len(physical)
    return (b"DXBC" + bytes(16) + struct.pack("<III", 1, size, len(offsets))
            + struct.pack(f"<{len(offsets)}I", *offsets) + physical)


class DXContainerPartOffsets(unittest.TestCase):
    def test_directory_order_is_independent_of_physical_part_order(self):
        normal, reversed_offsets = container(), container(reversed_directory=True)
        self.assertNotEqual(normal, reversed_offsets)
        self.assertEqual(parts(normal), parts(reversed_offsets))
        self.assertTrue(dxil_program(normal, 0))
        self.assertTrue(dxil_program(reversed_offsets, 0))
        self.assertEqual(shader_markers(normal)["framed_dxil_programs"], 1)
        self.assertEqual(shader_markers(reversed_offsets)["framed_dxil_programs"], 1)

    def test_non_dxbc_magic_and_negative_offsets_rejected(self):
        valid = container()
        for magic in (b"XXXX", b"DXIL", b"\0\0\0\0"):
            corrupted = magic + valid[4:]
            with self.assertRaisesRegex(ValueError, "Expected a DXBC container"):
                parts(corrupted)
            self.assertFalse(dxil_program(corrupted, 0))
            self.assertEqual(shader_markers(corrupted)["framed_dxil_programs"], 0)
        padded = b"prefix" + valid + b"tail"
        self.assertTrue(dxil_program(padded, 6))
        for bad_offset in (-1, -len(padded), -len(padded) + 6, len(padded) - 31):
            self.assertFalse(dxil_program(padded, bad_offset))

    def test_overlap_rejected_regardless_of_directory_order(self):
        for reversed_directory in (False, True):
            with self.subTest(reversed_directory=reversed_directory):
                data = container(overlapping=True, reversed_directory=reversed_directory)
                with self.assertRaises(ValueError):
                    parts(data)
                self.assertFalse(dxil_program(data, 0))

    def test_duplicate_fourcc_rejected_by_both_readers(self):
        # Two separate NOTE sections overlap neither each other nor the DXIL
        # section. A unique FourCC control proves valid DXIL framing is present.
        for reverse in (False, True):
            with self.subTest(reversed_directory=reverse):
                control = container(reversed_directory=reverse, extra_fourcc=b"UNIQ")
                duplicate = container(reversed_directory=reverse, extra_fourcc=b"NOTE")
                self.assertEqual(len(control), len(duplicate))
                self.assertEqual(set(parts(control)), {b"DXIL", b"NOTE", b"UNIQ"})
                self.assertTrue(dxil_program(control, 0))
                self.assertEqual(shader_markers(control)["framed_dxil_programs"], 1)
                self.assertIn(b"BC\xc0\xde", duplicate)
                with self.assertRaisesRegex(ValueError, "Invalid container part"):
                    parts(duplicate)
                self.assertFalse(dxil_program(duplicate, 0))
                self.assertEqual(shader_markers(duplicate)["framed_dxil_programs"], 0)

    def test_part_outside_container_remains_invalid(self):
        data = bytearray(container())
        struct.pack_into("<I", data, 36, len(data) - 4)
        with self.assertRaises(ValueError):
            parts(data)
        self.assertFalse(dxil_program(data, 0))

    def test_call_evidence_matches_dxcontainer_rules_and_skips_unmapped_bytes(self):
        rts0 = struct.pack("<6I", 2, 3, 24, 1, 0, 0x10)
        ordered = container(extra_fourcc=b"RTS0")
        # Replace the 5-byte extra payload with a 24-byte RTS0 root header via reversed directory.
        program = struct.pack("<II4sIII", 0, 8, b"DXIL", 0, 16, 4) + b"BC\xc0\xde" + bytes(4)
        dxil_part = b"DXIL" + struct.pack("<I", len(program)) + program
        rts0_part = b"RTS0" + struct.pack("<I", len(rts0)) + rts0
        table_end = 40
        unordered = (b"DXBC" + bytes(16) + struct.pack("<III", 1, table_end + len(dxil_part) + len(rts0_part), 2)
                     + struct.pack("<2I", table_end + len(dxil_part), table_end) + dxil_part + rts0_part)
        duplicate = container(extra_fourcc=b"NOTE")
        overlapping = container(overlapping=True)
        image_base = 0x180000000
        # Place valid unordered container at section offset 0x20 (RVA 0x1020), plus mapped size+pointer pair at 0x100.
        # Pad the section past 0x1000 so the overlay sits at an offset where pefile.get_rva_from_offset returns None.
        for section_size in (0x200, 0x1200):
            with self.subTest(section_size=section_size):
                payload = bytearray(section_size)
                payload[0x20:0x20 + len(unordered)] = unordered
                payload[0x100:0x110] = struct.pack("<IIQ", len(unordered), 0, image_base + 0x1020)
                payload[0x120:0x120 + len(duplicate)] = duplicate
                payload[0x120 + len(duplicate):0x120 + len(duplicate) + len(overlapping)] = overlapping
                overlay = ordered + struct.pack("<IIQ", len(unordered), 0, image_base + 0x1020)
                raw = minimal_pe(bytes(payload), overlay=overlay, image_base=image_base)
                with pefile.PE(data=raw, fast_load=True) as pe:
                    rows = shader_containers(raw, pe, defaultdict(list))
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["rva"], "0x1020")
                self.assertEqual(row["bytes"], len(unordered))
                self.assertEqual(row["parts"], ["RTS0", "DXIL"])
                self.assertTrue(row["framed_dxil"])
                self.assertEqual(row["root_header"], dict(version=2, parameter_count=3, static_sampler_count=1, flags="0x10"))
                self.assertEqual(row["size_pointer_pairs"], [
                    dict(pointer_slot="0x1108", size_slot="0x1100", pointer_code_refs=[], size_code_refs=[]),
                ])

    def test_call_evidence_code_refs_size_match_and_virtual_extent(self):
        image_base = 0x180000000
        framed = container()
        no_program = container().replace(b"DXIL", b"STAT", 1)  # Renames the part tag only.
        empty = b"DXBC" + bytes(16) + struct.pack("<III", 1, 32, 0)
        # VirtualSize 0x180 < SizeOfRawData 0x200: bytes 0x180..0x1FF of the raw section are not mapped.
        payload = bytearray(0x180)
        payload[0x20:0x20 + len(no_program)] = no_program
        payload[0x80:0x90] = struct.pack("<IIQ", len(framed), 0, image_base + 0x1124)
        payload[0xA0:0xB0] = struct.pack("<IIQ", len(framed) + 1, 0, image_base + 0x1124)  # Size disagrees.
        payload[0xC0:0xE0] = empty
        payload[0x124:0x180] = framed  # 0x180 - 0x5C: ends exactly at VirtualSize.
        raw = bytearray(minimal_pe(bytes(payload), image_base=image_base))
        raw[0x200 + 0x190:0x200 + 0x190 + len(framed)] = framed  # Raw slack beyond VirtualSize.
        memory = defaultdict(list, {0x1020: [(0x2030, "lea")], 0x1124: [(0x2000, "lea")],
                                    0x1080: [(0x2010, "mov")], 0x1088: [(0x2020, "mov")]})
        with pefile.PE(data=bytes(raw), fast_load=True) as pe:
            rows = shader_containers(bytes(raw), pe, memory)
        self.assertEqual(len(framed), 0x5C)
        self.assertEqual(rows, [
            dict(rva="0x1020", bytes=len(no_program), parts=["STAT", "NOTE"], root_header=None,
                 framed_dxil=False, size_pointer_pairs=[], direct_code_refs=["0x2030"]),
            dict(rva="0x1124", bytes=len(framed), parts=["DXIL", "NOTE"], root_header=None,
                 framed_dxil=True, size_pointer_pairs=[
                     dict(pointer_slot="0x1088", size_slot="0x1080",
                          pointer_code_refs=["0x2020"], size_code_refs=["0x2010"]),
                 ], direct_code_refs=["0x2000"]),
        ])

    def test_scene_records_advances_cursor_by_consumed_deflate_bytes(self):
        def make_record(blob, *, name=b"owned", tail=b"", extra_stored=0):
            encoder = zlib.compressobj(wbits=-15)
            payload = encoder.compress(blob) + encoder.flush() + tail
            stored = len(payload) + extra_stored
            return name + b"\0\0" + struct.pack("<4I", stored, 0, stored, len(blob)) + payload

        pixel = container(stage_kind=0)
        compute = container(stage_kind=5)

        # Normal trailing padding bytes still preserve the following record and tail count.
        padded = make_record(pixel, tail=b"pad") + make_record(compute)
        padded_result = scene_records(padded)
        self.assertEqual(padded_result["records"], 2)
        self.assertEqual(padded_result["stages"], {"pixel": 1, "compute": 1})
        self.assertEqual(padded_result["framed_bitcode"], 2)
        self.assertEqual(padded_result["trailing_byte_counts"], {3: 1, 0: 1})
        self.assertEqual(padded_result["candidate_rejections"], {})

        # Oversized claimed span covering a following record inside unused_data must not skip it.
        second = make_record(compute, name=b"second")
        for tail in (b"", b"\x00\x01"):
            with self.subTest(tail=tail):
                data = make_record(pixel, name=b"first", tail=tail, extra_stored=len(second)) + second
                observed = []
                result = scene_records(data, observe=lambda kind, blob, _: observed.append((kind, blob)))
                self.assertEqual(observed, [(0, pixel), (5, compute)])
                self.assertEqual(result["records"], 2)
                self.assertEqual(result["stages"], {"pixel": 1, "compute": 1})
                self.assertEqual(result["framed_bitcode"], 2)
                self.assertEqual(result["section_counts"]["DXIL"], 2)
                self.assertEqual(result["trailing_byte_counts"], {len(tail) + len(second): 1, 0: 1})
                self.assertEqual(result["candidate_rejections"], {})


if __name__ == "__main__":
    unittest.main()
