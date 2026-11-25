"""Fixed-count lane controls and a physical block-address oracle for packed tails."""
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from bc7_tiles import linearize_4k_s_tail
from bcpack_probe import probe, scan_lanes


class TextureChecks(unittest.TestCase):
    def test_forward_decode_supplied_refills(self) -> None:
        prefix = bytearray(48)
        # Mode DD has eight escape slots. Encoding 100,000 fixed symbols
        # backwards from 2**24 emits E3 then 93 and leaves state 030B87C1.
        prefix[3::8] = bytes.fromhex("dd 05 03 0b 87 c1")
        for second, status in ((b"\xe3", "verified_constant_lane_with_supplied_refills"),
                               (b"\xe2", "header_match")):
            result, = probe(prefix, 100000, prefetch_mask=0x09, prefetch=b"\xaa\x93",
                            next_mask=0x18, next_bytes=second + b"\xbb")
            self.assertEqual(result["status"], status)
            if second == b"\xe3":
                self.assertEqual(result["refill_symbol_indices"], [9129, 54564])
            else:
                self.assertNotIn("refill_symbol_indices", result)

        partial, = probe(prefix, 100000, prefetch_mask=0x08, prefetch=b"\x93")
        self.assertEqual(partial["status"], "header_match")

        # At 50,000 symbols the same model needs just E3, supplied in round two.
        prefix[3::8] = bytes.fromhex("dd 04 01 be a9 cd")
        delayed, = probe(prefix, 50000, prefetch_mask=0x01, prefetch=b"\xaa",
                         next_mask=0x08, next_bytes=b"\xe3")
        self.assertEqual(delayed["refill_symbol_indices"], [4564])
        # E4 adds one to the refilled state. Every later slot stays in range, so the
        # offset survives the decode and the lane ends at 01000001, not 01000000.
        off_by_one, = probe(prefix, 50000, prefetch_mask=0x08, prefetch=b"\xe4")
        self.assertEqual(off_by_one["status"], "header_match")
        surplus, = probe(prefix, 50000, prefetch_mask=0x08, prefetch=b"\xe3",
                         next_mask=0x08, next_bytes=b"\x00")
        self.assertEqual(surplus["status"], "header_match")
        self.assertEqual(surplus["unbudgeted_read_offsets"], [49])

    def test_constant_lane_with_two_refills(self):
        prefix = bytearray(48)
        prefix[4::8] = bytes.fromhex("de 05 03 0c 86 4e")

        result, = probe(prefix, 25000, prefetch_mask=0x10, prefetch=b"\xac")

        self.assertEqual(result["lane"], 4)
        self.assertEqual(result["refill_bytes"], 2)
        self.assertEqual(result["first_refill_offset"], 48)
        self.assertTrue(result["first_refill_matches"])

        # Lane 4 consumes AC then 7D. Other lanes shift its packet offsets.
        for byte, expected in ((0x7D, True), (0x7C, False)):
            result, = probe(prefix, 25000, prefetch_mask=0x11, prefetch=bytes([0, 0xAC]),
                            next_mask=0x14, next_bytes=bytes([0, byte]))
            self.assertEqual(result["second_refill_offset"], 51)
            self.assertTrue(result["first_refill_matches"])
            self.assertEqual(result["second_refill_matches"], expected)
        with self.assertRaises(ValueError):
            probe(prefix, 25000, next_mask=0x10)

    def test_refill_follows_lane_read_count(self):
        prefix = bytearray(48)
        prefix[4::8] = bytes.fromhex("de 05 03 0c 86 4e")
        # Lane 4 skips the first round, so its first read in the second round takes AC.
        for byte, expected in ((0xAC, True), (0x7D, False)):
            result, = probe(prefix, 25000, prefetch_mask=0x01, prefetch=b"\x00",
                            next_mask=0x11, next_bytes=bytes([0, byte]))
            self.assertEqual(result["first_refill_offset"], 50)
            self.assertEqual(result["first_refill_matches"], expected)
            self.assertNotIn("second_refill_offset", result)

        # The one-refill lane has no byte left for a second read.
        prefix[4::8] = bytes.fromhex("de 04 01 00 11 30")
        result, = probe(prefix, 11358, prefetch_mask=0x10, prefetch=b"\x7d",
                        next_mask=0x11, next_bytes=b"\x00\x00")
        self.assertTrue(result["first_refill_matches"])
        self.assertEqual(result["unbudgeted_read_offsets"], [50])
        self.assertNotIn("second_refill_offset", result)
        # A verified constant lane reads nothing after its header.
        prefix[4::8] = bytes.fromhex("de 03 01 00 40 02")
        result, = probe(prefix, 2, prefetch_mask=0x10, prefetch=b"\x00")
        self.assertEqual((result["status"], result["unbudgeted_read_offsets"]), ("verified_constant_lane", [48]))
        self.assertNotIn("first_refill_matches", result)

    def test_constant_lane(self):
        prefix = bytearray(48)
        prefix[4::8] = bytes.fromhex("de 03 01 00 40 02")
        result, = probe(prefix, 2)
        self.assertEqual((result["lane"], result["status"]), (4, "verified_constant_lane"))
        self.assertFalse(probe(prefix, 3))
        # Mode DC has four escape slots, so each early symbol adds 256 * 4 + 1 = 1025
        # to the state: two symbols give 2**24 + 2050 = 01000802 with no refills.
        prefix[4::8] = bytes.fromhex("dc 03 01 00 08 02")
        result, = probe(prefix, 2)
        self.assertEqual((result["mode"], result["status"]), (0xDC, "verified_constant_lane"))
        prefix[4::8] = bytes.fromhex("de 04 01 00 11 30")
        result, = probe(prefix, 11358)
        self.assertEqual((result["refill_bytes"], result["status"]), (1, "header_match"))
        # Lanes 0, 2 and 4 read in this round. Lane 4 takes the third byte.
        result, = probe(prefix, 11358, prefetch_mask=0x15, prefetch=bytes([2, 17, 125]))
        self.assertEqual(result["first_refill_offset"], 50)
        self.assertTrue(result["first_refill_matches"])
        result, = probe(prefix, 11358, prefetch_mask=0x15, prefetch=bytes([2, 125, 17]))
        self.assertFalse(result["first_refill_matches"])
        with self.assertRaises(ValueError):
            probe(prefix, 11358, prefetch_mask=0x15, prefetch=bytes([125]))
        prefix[12] -= 1
        self.assertFalse(probe(prefix, 11358))
        for short_or_long in (prefix[:-1], prefix + b"\x00"):
            with self.assertRaises(ValueError):
                probe(short_or_long, 2)

    def test_input_bounds(self):
        # No lane carries a known mode, so the largest block count returns at once.
        self.assertEqual(probe(bytes(48), 1 << 20), [])
        for options in ({"blocks": (1 << 20) + 1},
                        {"blocks": 2, "prefetch_mask": 0x100, "prefetch": b"\x00"},
                        {"blocks": 2, "next_mask": 0x100, "next_bytes": b"\x00"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                probe(bytes(48), **options)

    def test_contiguous_six_byte_lanes(self):
        prefix = bytearray(48)
        prefix[24:30] = bytes.fromhex("de 03 01 00 40 02")
        result, = probe(prefix, 2, lane_stride=6)
        self.assertEqual((result["lane"], result["status"]), (4, "verified_constant_lane"))
        # The same bytes are not a valid lane in the default interleaved layout.
        self.assertFalse(probe(prefix, 2))
        with self.assertRaises(ValueError):
            probe(prefix, 2, lane_stride=7)

    def test_scan_lanes(self):
        records = [bytes(6) for _ in range(8)]
        records[1] = bytes([0x7b, 0x3b, 0x03, 0x03, 0x00, 0x00])
        prefix = b"".join(records)
        scan = scan_lanes(prefix, lane_stride=6)
        self.assertEqual(len(scan), 8)
        self.assertEqual((scan[1]["mode"], scan[1]["budget"]), (0x7b, 0x3b))
        self.assertEqual(scan[1]["state"], "03030000")
        self.assertFalse(scan[1]["known_constant_mode"])
        known = scan_lanes(bytes.fromhex("de 03 01 00 40 02") + bytes(42), lane_stride=6)
        self.assertTrue(known[0]["known_constant_mode"])
        for short_or_long in (prefix[:-1], prefix + b"\x00"):
            with self.assertRaises(ValueError):
                scan_lanes(short_or_long, lane_stride=6)
        with self.assertRaises(ValueError):
            scan_lanes(prefix, lane_stride=7)

    def test_packed_tail_slots(self):
        tile = b"".join(struct.pack("<4I", *([index] * 4)) for index in range(256))
        for level, first in enumerate((128, 96, 80, 64, 48, 32, 16, 0)):
            side = max(1, 32 >> level)
            data = linearize_4k_s_tail(tile, side, side, level)
            count = (side + 3) // 4
            self.assertEqual(len(data), count * count * 16)
            self.assertEqual(struct.unpack_from("<I", data)[0], first)
            if count >= 2:
                self.assertEqual(struct.unpack_from("<I", data, 16)[0], first + 4)
                self.assertEqual(struct.unpack_from("<I", data, count * 16)[0], first + 1)
        for size, level in ((64, 0), (8, 7), (1, 8)):
            with self.assertRaises(ValueError):
                linearize_4k_s_tail(tile, size, size, level)

    def test_partial_tail_reads(self) -> None:
        class CountedTile(bytes):
            bytes_read: int = 0

            def __getitem__(self, key: int | slice) -> int | bytes:
                value = super().__getitem__(key)
                self.bytes_read += 1 if isinstance(value, int) else len(value)
                return value

        # Each physical block carries its own address in all sixteen bytes.
        tile = CountedTile(b"".join(bytes([index]) * 16 for index in range(256)))
        # At level zero the fifth block in a row crosses a swizzle boundary.
        for width, height, level, addresses in (
            (17, 5, 0, (128, 132, 136, 140, 160, 129, 133, 137, 141, 161)),
            (1, 1, 7, (0,)),
        ):
            with self.subTest(level=level):
                tile.bytes_read = 0
                expected = b"".join(bytes([index]) * 16 for index in addresses)
                self.assertEqual(linearize_4k_s_tail(tile, width, height, level), expected)
                self.assertLessEqual(tile.bytes_read, len(expected))

        for size in (4095, 4097):
            with self.assertRaises(ValueError):
                linearize_4k_s_tail(bytes(size), 1, 1, 7)

    def test_cli_reports_bad_packets_without_a_traceback(self) -> None:
        script = Path(__file__).resolve().parents[1] / "tools" / "bcpack_probe.py"
        with TemporaryDirectory() as directory:
            short, full = Path(directory) / "short.bin", Path(directory) / "full.bin"
            short.write_bytes(bytes(10))
            full.write_bytes(bytes(48))
            for packet, options in ((short, ["--blocks", "4"]), (full, ["--blocks", "0"]),
                                    (Path(directory) / "missing.bin", ["--blocks", "4"]),
                                    (short, ["--scan-lanes"])):
                result = subprocess.run([sys.executable, "-B", str(script), str(packet), *options],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 1, (packet.name, options, result.stderr))
                self.assertIn(packet.name, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
            result = subprocess.run([sys.executable, "-B", str(script), str(full), "--scan-lanes"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
