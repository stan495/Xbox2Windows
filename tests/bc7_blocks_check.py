"""Pin the BC7 block reference to hand-derived vectors, the mode-6 draw fixture and fixed digests."""
import hashlib
import os
import subprocess
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from bc7_blocks import (ANCHORS_2, ANCHORS_3, MODES, PARTITIONS_2, PARTITIONS_3, anchors, decode,
                        fixture, fixture_fields, layout, pack, subsets, unpack)
import bcpack_mode6_vector as mode6
import bcpack_mode6_multiblock_vector as multi_mode6
from bc7_draw_check import make_block

try:
    import texture2ddecoder
except ImportError:
    texture2ddecoder = None


# First bit of each field group, transcribed from the published per-mode bit layouts.
OFFSETS = (
    {"partition": 1, "r0": 5, "g0": 29, "b0": 53, "p0": 77, "index0": 83},
    {"partition": 2, "r0": 8, "g0": 32, "b0": 56, "p0": 80, "index0": 82},
    {"partition": 3, "r0": 9, "g0": 39, "b0": 69, "index0": 99},
    {"partition": 4, "r0": 10, "g0": 38, "b0": 66, "p0": 94, "index0": 98},
    {"rotation": 5, "index_selection": 7, "r0": 8, "g0": 18, "b0": 28, "a0": 38, "index0": 50, "index2_0": 81},
    {"rotation": 6, "r0": 8, "g0": 22, "b0": 36, "a0": 50, "index0": 66, "index2_0": 97},
    {"r0": 7, "g0": 21, "b0": 35, "a0": 49, "p0": 63, "index0": 65},
    {"partition": 8, "r0": 14, "g0": 34, "b0": 54, "a0": 74, "p0": 94, "index0": 98},
)


def block(*fields):
    return sum(value << offset for value, offset in fields).to_bytes(16, "little")


def texels(data):
    return [tuple(data[index:index + 4]) for index in range(0, 64, 4)]


def random_block(mode, case):
    bits = int.from_bytes(hashlib.sha256(f"random {mode} {case}".encode()).digest()[:16], "little")
    return (bits >> mode + 1 << mode + 1 | 1 << mode).to_bytes(16, "little")


class BlockChecks(unittest.TestCase):
    def test_bcpack_mode6_vector(self):
        self.assertEqual(decode(mode6.BLOCK), mode6.RGBA)
        # Change the last selector from 15 to 0; only the final pixel changes.
        changed = mode6.BLOCK[:-1] + bytes([mode6.BLOCK[-1] & 0x0f])
        self.assertEqual(decode(changed), mode6.RGBA[:-4] + bytes((197, 71, 41, 25)))

    @unittest.skipUnless(os.environ.get("BCPACK_REFERENCE_TOOLS"), "optional Leonida reference decoder")
    def test_bcpack_mode6_strict_reference(self):
        subprocess.run([sys.executable, "-B", mode6.__file__, os.environ["BCPACK_REFERENCE_TOOLS"]], check=True)

    def test_bcpack_mode6_multiblock_vector(self):
        b0_rgba = decode(multi_mode6.BLOCKS[:16])
        b1_rgba = decode(multi_mode6.BLOCKS[16:])
        self.assertEqual(multi_mode6.raster_8x4(b0_rgba, b1_rgba), multi_mode6.RGBA)
        # Swapping the two decoded blocks produces a surface mismatch.
        self.assertNotEqual(multi_mode6.raster_8x4(b1_rgba, b0_rgba), multi_mode6.RGBA)
        # Change the last selector of the second block from 15 to 0; only the final pixel (7, 3) changes.
        changed_b1 = multi_mode6.BLOCKS[16:-1] + bytes([multi_mode6.BLOCKS[-1] & 0x0f])
        expected_last_pixel = bytes((40, 120, 220, 200))
        self.assertEqual(multi_mode6.raster_8x4(b0_rgba, decode(changed_b1)),
                         multi_mode6.RGBA[:-4] + expected_last_pixel)

    @unittest.skipUnless(os.environ.get("BCPACK_REFERENCE_TOOLS"), "optional Leonida reference decoder")
    def test_bcpack_mode6_multiblock_strict_reference(self):
        subprocess.run([sys.executable, "-B", multi_mode6.__file__, os.environ["BCPACK_REFERENCE_TOOLS"]], check=True)

    def test_field_offsets(self):
        for mode, expected in enumerate(OFFSETS):
            for partition in range(1 << MODES[mode][1]):
                spans = layout(mode, partition)
                first = {name: offset for name, offset, _ in spans}
                self.assertEqual({name: first[name] for name in expected}, expected)
                self.assertEqual(set(first) & {"partition", "rotation", "index_selection", "a0", "p0", "index2_0"},
                                 set(expected) - {"r0", "g0", "b0", "index0"})
                self.assertEqual(spans[0], ("mode", 0, mode + 1))
                self.assertEqual(sum(width for _, _, width in spans), 128)
                self.assertEqual([offset for _, offset, _ in spans],
                                 [sum(width for _, _, width in spans[:index]) for index in range(len(spans))])
        with self.assertRaises(ValueError):
            layout(0, 16)
        for bad_mode in (-1, 8, True):
            with self.assertRaises(ValueError):
                layout(bad_mode)
        for bad_mode, bad_partition in ((1, -1), (6, 1)):
            with self.assertRaises(ValueError):
                layout(bad_mode, bad_partition)

    def test_partition_tables(self):
        for table, count in ((PARTITIONS_2, 2), (PARTITIONS_3, 3)):
            self.assertEqual((len(table), len(set(table))), (64, 64))
            for shape in table:
                self.assertEqual((len(shape), shape[0], len(set(shape))), (16, "0", count))
        for partition in range(64):
            self.assertEqual(PARTITIONS_2[partition][ANCHORS_2[partition]], "1")
            self.assertEqual(PARTITIONS_3[partition][ANCHORS_3[0][partition]], "1")
            self.assertEqual(PARTITIONS_3[partition][ANCHORS_3[1][partition]], "2")
            self.assertEqual(anchors(1, partition), (0, ANCHORS_2[partition]))
            self.assertEqual(len(set(anchors(2, partition))), 3)
        self.assertEqual(subsets(6, 0), (0,) * 16)
        self.assertEqual(subsets(1, 13), (0,) * 8 + (1,) * 8)
        for bad_mode, bad_partition in ((-1, 0), (8, 0), (1, -1), (1, 64), (6, 1)):
            with self.assertRaises(ValueError):
                subsets(bad_mode, bad_partition)
        for bad_mode, bad_partition in ((-1, 0), (8, 0), (2, -1), (2, 64), (6, 1)):
            with self.assertRaises(ValueError):
                anchors(bad_mode, bad_partition)

    def test_hand_derived_blocks(self):
        # Mode 1, partition 13: rows 0-1 use subset 0 and rows 2-3 use subset 1.
        # Subset 0 shares P = 1: red 63 becomes 127 then 255, and green and blue 0 become 1 then 2.
        # Subset 1 shares P = 0: its second endpoint has red 63, which becomes 126 then 253.
        # Texel 8 has index 7 at bit 82 + 2 + 7 * 3 and selects that second endpoint.
        data = block((1, 1), (13, 2), (63, 8), (63, 26), (1, 80), (7, 105))
        self.assertEqual(texels(decode(data)),
                         [(255, 2, 2, 255)] * 8 + [(253, 0, 0, 255)] + [(0, 0, 0, 255)] * 7)
        self.assertEqual(anchors(1, 13), (0, 15))

        # Mode 5: red runs from 127 (expanded to 255) to 0 and alpha from 64 to 192.
        # Texel 1 has colour index 3. Texel 2 has alpha index 1: (43 * 64 + 21 * 192 + 32) >> 6 = 106.
        fields = ((1, 5), (127, 8), (64, 50), (192, 58), (3, 67), (1, 100))
        self.assertEqual(texels(decode(block(*fields)))[:4],
                         [(255, 0, 0, 64), (0, 0, 0, 64), (255, 0, 0, 106), (255, 0, 0, 64)])
        # Rotation 1 exchanges red and alpha after interpolation.
        self.assertEqual(texels(decode(block(*fields, (1, 6))))[:4],
                         [(64, 0, 0, 255), (64, 0, 0, 0), (106, 0, 0, 255), (64, 0, 0, 255)])

        # Mode 4: red runs from 31 (255) to 0 and alpha from 0 to 63 (255).
        # Texel 1 has two-bit index 1 at bit 51 and three-bit index 7 at bit 83.
        fields = ((1, 4), (31, 8), (63, 44), (1, 51), (7, 83))
        # Colour takes the two-bit index: (43 * 255 + 32) >> 6 = 171. Alpha reaches its second endpoint.
        self.assertEqual(texels(decode(block(*fields)))[:2], [(255, 0, 0, 0), (171, 0, 0, 255)])
        # With index selection set, colour takes the three-bit index: alpha is (21 * 255 + 32) >> 6 = 84.
        self.assertEqual(texels(decode(block(*fields, (1, 7))))[:2], [(255, 0, 0, 0), (0, 0, 0, 84)])

        self.assertEqual(decode(bytes(16)), bytes(64))
        self.assertEqual(decode(bytes([0]) + b"\xff" * 15), bytes(64))

    def test_mode_6_draw_fixture(self):
        # The draw fixture builds its blocks and reference texels without this module.
        for x, y in ((0, 0), (1, 2), (19, 17), (31, 42)):
            data, colours = make_block(x, y)
            self.assertEqual(decode(data), bytes(colours))
            fields = unpack(data)
            self.assertEqual((fields["mode"], fields["r0"], fields["g0"], fields["a1"], fields["p0"], fields["p1"]),
                             (6, x * 4, y * 3, 127, 0, 0))
            self.assertEqual([fields[f"index{texel}"] for texel in range(16)], list(range(16)))

    def test_pack_inverts_unpack(self):
        for mode in range(8):
            seen = set()
            for case in range(512):
                data = random_block(mode, case)
                fields = unpack(data)
                seen.add(fields.get("partition", 0))
                self.assertEqual(fields["mode"], mode)
                self.assertEqual(pack(fields), data)
            self.assertEqual(len(seen), 1 << MODES[mode][1])
        fields = fixture_fields(1, 13)
        for change in ({"index0": 4}, {"index15": 4}, {"r0": 64}, {"mode": -1}, {"mode": 8}, {"mode": True},
                       {"partition": 64}, {"a0": 0}):
            with self.assertRaises(ValueError):
                pack({**fields, **change})
        for bad_mode in (-1, 8, True):
            with self.assertRaises(ValueError):
                fixture_fields(bad_mode, 0)
        with self.assertRaises(ValueError):
            pack({name: value for name, value in fields.items() if name != "p1"})
        for data in (bytes(16), bytes(15)):
            with self.assertRaises(ValueError):
                unpack(data)

    def test_fixture(self):
        fields, rows, pixels = fixture()
        self.assertEqual((len(fields), len(rows), len(pixels)), (512, 512 * 16, 512 * 64))
        for mode in range(8):
            entries = fields[mode::8]
            self.assertEqual({entry["mode"] for entry in entries}, {mode})
            self.assertEqual(len({entry.get("partition", 0) for entry in entries}), 1 << MODES[mode][1])
            if MODES[mode][2]:
                self.assertEqual({entry["rotation"] for entry in entries}, {0, 1, 2, 3})
        self.assertEqual({(entry["rotation"], entry["index_selection"]) for entry in fields[4::8]},
                         {(rotation, selection) for rotation in range(4) for selection in range(2)})
        for index, entry in enumerate(fields):
            data = rows[index * 16:index * 16 + 16]
            self.assertEqual(unpack(data), entry)
            texel = decode(data)
            start = (index // 16 * 4 * 16 + index % 16) * 16
            self.assertEqual(pixels[start:start + 16], texel[:16])
        self.assertEqual(hashlib.sha256(rows).hexdigest(), ROWS_SHA256)
        self.assertEqual(hashlib.sha256(pixels).hexdigest(), PIXELS_SHA256)
        self.assertEqual(fixture(2, 1)[1], rows[:32])
        with self.assertRaises(ValueError):
            fixture(0, 1)

    def test_rectangular_fixture_raster(self):
        # The reference decodes the 5-by-7 block grid as one 20-by-28 image
        # with texture2ddecoder 1.0.6 and converts its BGRA output to RGBA.
        _, _, pixels = fixture(columns=5, rows=7)
        self.assertEqual(hashlib.sha256(pixels).hexdigest(), RECTANGULAR_PIXELS_SHA256)

    @unittest.skipUnless(texture2ddecoder, "Optional comparison: pip install texture2ddecoder")
    def test_independent_decoder(self):
        rows = fixture()[1] + b"".join(random_block(mode, case) for mode in range(8) for case in range(2048))
        rows += bytes(16)
        count = len(rows) // 16
        # The other decoder returns BGRA. One block per row keeps the two rasters in the same order.
        other = texture2ddecoder.decode_bc7(rows, 4, count * 4)
        for index in range(count):
            ours = decode(rows[index * 16:index * 16 + 16])
            theirs = bytearray(other[index * 64:index * 64 + 64])
            theirs[0::4], theirs[2::4] = theirs[2::4], theirs[0::4]
            self.assertEqual(ours, bytes(theirs), index)


ROWS_SHA256 = "184fc0970dc8e1b91f79639d820bd0eb7df2659427bb8b17ce66f61fd7e969ba"
PIXELS_SHA256 = "79fe13df3cf1a31914c7a08d0e02d78840347d5e2dcbaa51b35c44afc7210383"
RECTANGULAR_PIXELS_SHA256 = "3aac1b30ad79ea78d6dc76b4ad137122441129ea98d797126c45c017e49b1e19"


if __name__ == "__main__":
    unittest.main()
