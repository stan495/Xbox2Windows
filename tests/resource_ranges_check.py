"""Byte-for-byte controls for logical asset reads and bounded section probes."""
import contextlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from paged_resource import PagedResource, Segment
from section_probe import inspect as inspect_sections
from section_probe import main as section_probe_main
from section_probe import scan_window, sections
from texture_descriptors import inventory
from texture_descriptors import main as texture_descriptors_main
from texture_descriptors import raw_page_map, zlib_segment


def run_cli(main_fn, *args: str | Path) -> subprocess.CompletedProcess[str]:
    """Invoke a tool CLI entrypoint in-process and capture stdout, stderr, and exit code."""
    argv = [str(arg) for arg in args]
    stdout = io.StringIO()
    stderr = io.StringIO()
    returncode = 0
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            returncode = main_fn(argv)
        except SystemExit as exc:
            if exc.code is None:
                returncode = 0
            elif isinstance(exc.code, int):
                returncode = exc.code
            else:
                stderr.write(f"{exc.code}\n")
                returncode = 1
    return subprocess.CompletedProcess(argv, returncode, stdout.getvalue(), stderr.getvalue())


class ShortReadStream(io.BytesIO):
    def read(self, size: int = -1, /) -> bytes:
        return super().read(min(size, 3) if size >= 0 else size)


def descriptor_archive() -> bytearray:
    archive = bytearray(9216)
    struct.pack_into("<4sIQ", archive, 0, b"RDS5", 3, 16384)
    # Boundaries give stored offsets 256, 8960, 9216 and virtual offsets
    # 0, 8192, 16384. The second chunk is outside the metadata inventory.
    struct.pack_into("<6I", archive, 16, 1, 0x70000000, (2 << 30) | (8 << 26) | 35, 0,
                     (5 << 26) | 36, 1)
    struct.pack_into("<4sIII", archive, 256, b"ETPF", 0, 258, 0)
    for index in range(257):
        # 44x12, DXGI 99, marker byte 2 and virtual page 3.
        struct.pack_into("<4I", archive, 272 + index * 16, 0x2C02B, 0x02000318, 3, 0)
    # Transposed dimensions and a different format after the first 4 KiB read.
    struct.pack_into("<4I", archive, 272 + 257 * 16, 0xAC00B, 0x238, 2, 0)
    return archive


def raw_page_archive() -> bytearray:
    archive = bytearray(16384)
    struct.pack_into("<4sIQ", archive, 0, b"RDS5", 5, 20480)
    # The raw interval at stored [4608, 12800) contains logical pages 2 and 3.
    # Its neighbors advertise zlib (8) and BCPack (7), respectively.
    struct.pack_into("<10I", archive, 16, 1, 0, (1 << 30) | (8 << 26) | 17, 0,
                     (2 << 30) | 18, 0, (7 << 26) | 50, 1, (1 << 30) | 64, 1)
    struct.pack_into("<4sIII", archive, 256, b"ETPF", 0, 5, 0)
    for index, page in enumerate((3, 2, 3, 1, 4)):
        struct.pack_into("<4I", archive, 272 + index * 16, 0x3c00f, 98 << 3, page, 0)
    archive[4608:8704] = bytes((i * 7 + 19) % 256 for i in range(4096))
    archive[8704:12800] = bytes((i * 13 + 17) % 256 for i in range(4096))
    return archive


def zlib_page_archive() -> tuple[bytearray, bytes, bytes]:
    archive = bytearray(8960)
    struct.pack_into("<4sIQ", archive, 0, b"RDS5", 4, 16384)
    # One zlib interval occupies 512 stored bytes from offset 4352, and
    # logical pages 1 and 2. The descriptor points to its second page.
    struct.pack_into("<8I", archive, 16, 1, 0, (1 << 30) | (8 << 26) | 17, 0,
                     (3 << 30) | 19, 0, 35, 1)
    struct.pack_into("<4sIII4I", archive, 256, b"ETPF", 0, 1, 0, 0x3c00f, 98 << 3, 2, 0)
    plain = b"A" * 4096 + b"BCDE" * 1024
    packed = zlib.compress(plain)
    archive[4352:4352 + len(packed)] = packed
    return archive, packed, plain


class ResourceChecks(unittest.TestCase):
    def test_texture_key_matches(self) -> None:
        key = 0xFEDCBA9876543210
        with TemporaryDirectory() as directory:
            first = Path(directory) / "assets.ttpf"
            second = Path(directory) / "pack" / "assets.ttpf"
            second.parent.mkdir()
            for path, keys in ((first, [key - (1 << 32), key, key + 1]),
                               (second, [key, key, (1 << 64) - 1])):
                archive = raw_page_archive()
                struct.pack_into("<4sIII", archive, 256, b"ETPF", 0x27a, len(keys), 0)
                struct.pack_into("<3Q", archive, 272 + len(keys) * 16, *keys)
                path.write_bytes(archive)

            def lookup(value: str, *paths: Path) -> subprocess.CompletedProcess[str]:
                return run_cli(texture_descriptors_main, "--lookup-key", value, *paths)

            for value, expected in (
                (hex(key), [(second, 0), (second, 1), (first, 1)]),
                (str(key - (1 << 32)), [(first, 0)]), (hex(key + 1), [(first, 2)]),
                (str((1 << 64) - 1), [(second, 2)]), ("0", []),
            ):
                with self.subTest(value=value):
                    result = lookup(value, second, first)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), {"matches": [
                        {"archive": str(path), "descriptor_index": index} for path, index in expected]})
            alias = Path(directory) / "alias.ttpf"
            alias.hardlink_to(first)
            for value, paths in ((hex(key), [first, first]), (hex(key), [first, alias]),
                                 ("-1", [first]), (str(1 << 64), [first])):
                with self.subTest(value=value, paths=len(paths)):
                    failed = lookup(value, *paths)
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertEqual(failed.stdout, "")

    def test_texture_key_table_bounds(self) -> None:
        from texture_descriptors import descriptor_indices

        # The largest supported descriptor array occupies exactly 1 MiB.
        count = 65536
        extent = ((16 + count * 24 + 4095) // 4096) * 4096
        archive = bytearray(256 + extent)
        struct.pack_into("<4sIQ4I", archive, 0, b"RDS5", 2, extent, 1, 0,
                         ((extent // 4096 % 4) << 30) | (len(archive) // 256), extent // 16384)
        struct.pack_into("<4sIII", archive, 256, b"ETPF", 0x27a, count, 0)
        keys_start = 272 + count * 16
        struct.pack_into("<Q", archive, keys_start + (count - 1) * 8, (1 << 64) - 1)

        class KeyTableOnly(io.BytesIO):
            def read(self, size: int = -1, /) -> bytes:
                pos = self.tell()
                if not 0 <= size <= 4096 or not any(
                    left <= pos <= pos + size <= right for left, right in
                    ((0, 32), (256, 272), (keys_start, keys_start + count * 8))
                ):
                    raise AssertionError("Lookup read outside bounded metadata or key array")
                return super().read(size)

        self.assertEqual(descriptor_indices(KeyTableOnly(archive), (1 << 64) - 1), [count - 1])
        for field, value, reason in ((260, 0, "version"), (264, count + 1, "budget")):
            changed = archive.copy()
            struct.pack_into("<I", changed, field, value)
            with self.assertRaisesRegex(ValueError, reason):
                descriptor_indices(KeyTableOnly(changed), 19)
        # Descriptor array fits in the first range but its key array ends beyond it.
        changed = raw_page_archive()
        struct.pack_into("<II", changed, 260, 0x27a, 171)
        with self.assertRaisesRegex(ValueError, "key table"):
            descriptor_indices(io.BytesIO(changed), 19)

    def test_verified_zlib_segment(self) -> None:
        archive, packed, plain = zlib_page_archive()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "zlib.ttpf"
            path.write_bytes(archive)
            result = run_cli(texture_descriptors_main, path, "--zlib-segment", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report, {
            "segment_logical_offset": 4096, "page_offset_in_segment": 4096,
            "segment": {"offset": 4352, "stored": len(packed), "size": 8192, "codec": "zlib"},
        })
        reader = PagedResource(io.BytesIO(archive), [Segment(**report["segment"])])
        self.assertEqual(reader.read(report["page_offset_in_segment"], 4096), plain[4096:])
        padded = PagedResource(io.BytesIO(archive), [Segment(4352, 512, 8192, "zlib")])
        with self.assertRaises(ValueError):
            padded.read(0, 1)

    def test_zlib_segment_rejections(self) -> None:
        archive, packed, plain = zlib_page_archive()
        bad_checksum = packed[:-1] + bytes([packed[-1] ^ 1])
        for label, data in (
            ("checksum", bad_checksum),
            ("truncated stored stream", zlib.compress(plain, level=0)[:512]),
            ("short output", zlib.compress(plain[:-1])),
            ("long output", zlib.compress(plain + b"x")),
            ("nonzero padding", packed + b"other bytes"),
            ("second member", packed + zlib.compress(b"another stream")),
        ):
            with self.subTest(label=label):
                changed = archive.copy()
                changed[4352:4864] = data.ljust(512, b"\0")
                with self.assertRaises(ValueError):
                    zlib_segment(io.BytesIO(changed), 0)
        struct.pack_into("<I", archive, 280, 3)
        with self.assertRaises(ValueError):
            zlib_segment(io.BytesIO(archive), 0)

    def test_zlib_segment_read_budget(self) -> None:
        archive, packed, _ = zlib_page_archive()

        class MetadataOnly(io.BytesIO):
            def read(self, size: int = -1, /) -> bytes:
                pos = self.tell()
                if not (0 <= size and (pos + size <= 48 or 256 <= pos <= pos + size <= 288)):
                    raise AssertionError("Attempt to read an oversized zlib interval")
                return super().read(size)

        oversized = (1 << 20) + 4096
        for label in ("stored", "decoded"):
            with self.subTest(label=label):
                changed = archive.copy()
                if label == "stored":
                    end = 4352 + oversized
                    changed.extend(bytes(end + 4096 - len(changed)))
                    struct.pack_into("<I", changed, 32, (3 << 30) | (end // 256))
                    struct.pack_into("<I", changed, 40, len(changed) // 256)
                else:
                    end = 4096 + oversized
                    struct.pack_into("<Q", changed, 8, end + 4096)
                    struct.pack_into("<II", changed, 32, (2 << 30) | 19, end // 16384)
                    struct.pack_into("<II", changed, 40, (3 << 30) | 35, (end + 4096) // 16384)
                with self.assertRaisesRegex(ValueError, "budget"):
                    zlib_segment(MetadataOnly(changed), 0)

        class OneInterval(MetadataOnly):
            def read(self, size: int = -1, /) -> bytes:
                if self.tell() == 4352 and size == 512:
                    return io.BytesIO.read(self, size)
                return super().read(size)

        self.assertEqual(zlib_segment(OneInterval(archive), 0).segment.stored, len(packed))

    def test_raw_page_map(self) -> None:
        archive = raw_page_archive()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "raw.ttpf"
            path.write_bytes(archive)
            result = run_cli(texture_descriptors_main, path, "--raw-page-map", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        mapping = json.loads(result.stdout)
        self.assertEqual(mapping, {"logical_offset": 12288, "file_offset": 8704})
        start = mapping["file_offset"]
        self.assertEqual(archive[start:start + 4096], bytes((i * 13 + 17) % 256 for i in range(4096)))

    def test_raw_page_map_boundaries(self) -> None:
        archive = raw_page_archive()
        self.assertEqual(raw_page_map(io.BytesIO(archive), 1).file_offset, 4608)
        self.assertEqual(raw_page_map(io.BytesIO(archive), 2).file_offset, 8704)
        for index in (-1, 3, 4, 5):
            with self.subTest(index=index), self.assertRaises(ValueError):
                raw_page_map(io.BytesIO(archive), index)

        for label, offset, change in (
            ("padded raw", 40, struct.pack("<I", (7 << 26) | 51)),
            ("undersized raw", 40, struct.pack("<I", (7 << 26) | 49)),
            ("duplicate logical boundary", 40, struct.pack("<2I", (2 << 30) | 50, 0)),
            ("descending stored boundary", 40, struct.pack("<I", (7 << 26) | 17)),
            ("page at logical end", 280, struct.pack("<I", 5)),
        ):
            with self.subTest(label=label):
                changed = archive.copy()
                changed[offset:offset + len(change)] = change
                with self.assertRaises(ValueError):
                    raw_page_map(io.BytesIO(changed), 0)

        changed = archive.copy()
        struct.pack_into("<I", changed, 4, 6)
        changed[48:64] = archive[40:56]  # Repeat the preceding boundary after the selected interval.
        with self.assertRaisesRegex(ValueError, "increase"):
            raw_page_map(io.BytesIO(changed), 0)

        count = (1 << 17) + 1  # One boundary above the 1 MiB mapping budget.
        start = (1 << 20) + 256
        changed = bytearray(start + len(archive) - 256)
        changed[:56] = archive[:56]
        struct.pack_into("<I", changed, 4, count)
        struct.pack_into("<I", changed, 16, start // 256)
        struct.pack_into("<I", changed, 24, (1 << 30) | (8 << 26) | (start // 256 + 16))
        struct.pack_into("<2I", changed, 16 + (count - 1) * 8, (1 << 30) | (len(changed) // 256), 1)
        changed[start:start + 96] = archive[256:352]
        with self.assertRaisesRegex(ValueError, "mapping budget"):
            raw_page_map(io.BytesIO(changed), 0)

    def test_raw_page_declared_padding(self) -> None:
        archive = raw_page_archive()
        padding = 1280
        archive[12800:12800] = bytes(padding)
        struct.pack_into("<I", archive, 36, padding << 20)
        # The following range's field is different. Keep its virtual position.
        struct.pack_into("<II", archive, 40, (7 << 26) | 55, (20 << 20) | 1)
        struct.pack_into("<I", archive, 48, (1 << 30) | 69)

        class PaddingOnly(io.BytesIO):
            readable_padding = padding

            def read(self, size: int = -1, /) -> bytes:
                pos = self.tell()
                if not (0 <= size and (pos + size <= 56 or 256 <= pos <= pos + size <= 288 or
                                       12800 <= pos <= pos + size <= 12800 + self.readable_padding)):
                    raise AssertionError("Raw page mapping read outside metadata and declared padding")
                return super().read(min(size, 7))

        self.assertEqual(raw_page_map(PaddingOnly(archive), 0).file_offset, 8704)

        class TruncatedPadding(PaddingOnly):
            def read(self, size: int = -1, /) -> bytes:
                if self.tell() >= 12800:
                    size = min(size, 12800 + padding - 1 - self.tell())
                return super().read(size)

        with self.assertRaisesRegex(ValueError, "Truncated segment"):
            raw_page_map(TruncatedPadding(archive), 0)
        for label, offset, value in (
            ("nonzero first padding byte", 12800, b"x"),
            ("nonzero last padding byte", 12800 + padding - 1, b"y"),
            ("wrong boundary's count", 36, struct.pack("<I", 20 << 20)),
            ("truncated count", 36, bytes(4)),
        ):
            with self.subTest(label=label):
                changed = archive.copy()
                changed[offset:offset + len(value)] = value
                with self.assertRaises(ValueError):
                    raw_page_map(PaddingOnly(changed), 0)

        # The preexisting equal-size mapping uses the range lengths alone.
        unchanged_size = raw_page_archive()
        struct.pack_into("<I", unchanged_size, 36, 0xe00 << 20)
        equal_range = PaddingOnly(unchanged_size)
        equal_range.readable_padding = 0
        self.assertEqual(raw_page_map(equal_range, 0).file_offset, 8704)

    def test_raw_page_map_across_boundary_batches(self) -> None:
        # Boundary 511 ends the first 4 KiB batch; boundary 512 ends its interval.
        count = 513
        start = 4352
        size = start + (count - 1) * 4096
        archive = bytearray(size)
        struct.pack_into("<4sIQ", archive, 0, b"RDS5", count, size - start)
        for index in range(count):
            stored_pages = (start + index * 4096) // 256
            struct.pack_into("<II", archive, 16 + 8 * index, stored_pages | ((index % 4) << 30), index // 4)
        struct.pack_into("<4sIII", archive, start, b"ETPF", 0, 1, 0)
        struct.pack_into("<4I", archive, start + 16, 0, 0, 511, 0)

        class MetadataOnly(io.BytesIO):
            def read(self, length: int = -1, /) -> bytes:
                pos = self.tell()
                if not (0 <= length <= 4096 and
                        (pos + length <= 16 + count * 8 or start <= pos <= pos + length <= start + 32)):
                    raise AssertionError(f"Unexpected map read of {length} bytes at {pos}")
                return super().read(length)

        self.assertEqual(raw_page_map(MetadataOnly(archive), 0).file_offset, size - 4096)

    def test_texture_descriptor_inventory(self) -> None:
        tool = Path(__file__).resolve().parents[1] / "tools" / "texture_descriptors.py"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.ttpf"
            path.write_bytes(descriptor_archive())
            result = subprocess.run([sys.executable, "-B", tool, path], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "descriptor_count": 258,
            "groups": [
                {"width": 12, "height": 44, "dxgi_format": 71, "mip_marker": 0, "count": 1},
                {"width": 44, "height": 12, "dxgi_format": 99, "mip_marker": 2, "count": 257},
            ],
        })

    def test_texture_descriptor_detail_command(self) -> None:
        tool = Path(__file__).resolve().parents[1] / "tools" / "texture_descriptors.py"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.ttpf"
            path.write_bytes(descriptor_archive())
            result = subprocess.run([sys.executable, "-B", str(tool), str(path), "--descriptor", "257"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {
                "descriptor_index": 257, "width": 12, "height": 44,
                "dxgi_format": 71, "mip_marker": 0, "logical_offset": 8192,
            })
            for index in ("-1", "258"):
                with self.subTest(index=index):
                    result = run_cli(texture_descriptors_main, path, "--descriptor", index)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("Descriptor index outside the ETPF table", result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_texture_descriptor_detail_read_scope(self) -> None:
        from texture_descriptors import describe_descriptor

        index = 12
        record = 272 + 16 * index

        class SelectedRecordOnly(ShortReadStream):
            def read(self, size: int = -1, /) -> bytes:
                start = self.tell()
                if not any(left <= start <= start + size <= right for left, right in
                           ((0, 40), (256, 272), (record, record + 16))):
                    raise AssertionError(f"Unexpected read of {size} bytes at {start}")
                return super().read(size)

        archive = descriptor_archive()
        self.assertEqual(describe_descriptor(SelectedRecordOnly(archive), index), {
            "descriptor_index": index, "width": 44, "height": 12,
            "dxgi_format": 99, "mip_marker": 2, "logical_offset": 12288,
        })
        struct.pack_into("<I", archive, record + 8, 4)
        with self.assertRaisesRegex(ValueError, "outside the declared logical archive"):
            describe_descriptor(SelectedRecordOnly(archive), index)

    def test_texture_descriptor_bounds(self) -> None:
        for label, offset, replacement in (
            ("RDS5 header", 0, b"TEST"),
            ("boundary table", 4, struct.pack("<I", 32)),
            ("raw metadata", 16, struct.pack("<I", 1 | (8 << 26))),
            ("stored range", 24, struct.pack("<I", (2 << 30) | 40)),
            ("stored table length", 24, struct.pack("<I", (2 << 30) | 2)),
            ("virtual range", 28, struct.pack("<I", 2)),
            ("ETPF header", 256, b"JUNK"),
            ("descriptor table", 264, struct.pack("<I", 512)),
            ("descriptor page", 272 + 257 * 16 + 8, struct.pack("<I", 4)),
        ):
            with self.subTest(label=label):
                archive = descriptor_archive()
                archive[offset:offset + len(replacement)] = replacement
                with self.assertRaises(ValueError):
                    inventory(io.BytesIO(archive))
        with self.assertRaises(ValueError):
            inventory(io.BytesIO(descriptor_archive()[:-1]))

        archive = descriptor_archive()
        archive.extend(bytes((1 << 20) + 16384 - len(archive)))
        struct.pack_into("<Q", archive, 8, len(archive))
        struct.pack_into("<4I", archive, 24, (2 << 30) | 4144, 64, 4160, 65)
        struct.pack_into("<I", archive, 264, 65537)
        with self.assertRaisesRegex(ValueError, "budget"):
            inventory(io.BytesIO(archive))

    def test_texture_descriptor_read_scope(self) -> None:
        class MetadataOnly(io.BytesIO):
            def read(self, size: int = -1, /) -> bytes:
                start = self.tell()
                if not (0 <= size <= 4096 and
                        (start + size <= 40 or 256 <= start <= start + size <= 272 + 258 * 16)):
                    raise AssertionError(f"Unexpected read of {size} bytes at {start}")
                return super().read(size)

        # Following bytes need not be part of the declared archive.
        stream = MetadataOnly(descriptor_archive() + bytes(256))
        self.assertEqual(sum(inventory(stream).values()), 258)

    def test_ranges(self):
        first, middle, last = b"header", b"m" * 93, b"end"
        compressed = zlib.compress(middle)
        stream = io.BytesIO(first + b"pad" + compressed + last)
        reader = PagedResource(stream, [Segment(0, 6, 6, "raw"), Segment(9, len(compressed), 93, "zlib"),
                                        Segment(9 + len(compressed), 3, 3, "raw")])
        reference = first + middle + last
        for pos in range(len(reference) + 1):
            for length in (0, min(5, len(reference)-pos), len(reference)-pos):
                self.assertEqual(reader.read(pos, length), reference[pos:pos+length])
        for pos, size in ((-1, 0), (1, -1), (reader.size, 1)):
            with self.assertRaises(ValueError):
                reader.read(pos, size)
        with self.assertRaises(ValueError):
            reader.read(7, 1, limit=8)
        # Budgets are inclusive: a 3-byte raw read fits a limit of 3, and the
        # 93-byte zlib segment decodes under a limit of 93.
        self.assertEqual(reader.read(99, 3, limit=3), last)
        self.assertEqual(reader.read(7, 1, limit=93), b"m")
        with self.assertRaises(ValueError):
            reader.read(99, 3, limit=2)
        # A stored (level 0) stream is 93 + 2 + 5 + 4 bytes, so its stored size alone exceeds 93.
        stored = zlib.compress(middle, 0)
        stored_reader = PagedResource(io.BytesIO(stored), [Segment(0, len(stored), 93, "zlib")])
        self.assertEqual(stored_reader.read(0, 1, limit=len(stored)), b"m")
        with self.assertRaisesRegex(ValueError, "exceeds read budget"):
            stored_reader.read(0, 1, limit=93)

    def test_short_reads_of_intact_segments(self) -> None:
        decoded = b"range data from a synthetic segment"
        for codec, stored, diagnostic in (
            ("raw", decoded, "Truncated segment"),
            ("zlib", zlib.compress(decoded), "Invalid compressed segment"),
        ):
            with self.subTest(codec=codec):
                stream = ShortReadStream(stored)
                reader = PagedResource(stream, [Segment(0, len(stored), len(decoded), codec)])
                self.assertEqual(reader.read(2, len(decoded) - 4), decoded[2:-2])
                stream.truncate(len(stored) - 1)
                with self.assertRaisesRegex(ValueError, diagnostic):
                    reader.read(0, len(decoded))

    def test_invalid_segments(self):
        packet = zlib.compress(b"page")
        intact_reader = PagedResource(io.BytesIO(packet), [Segment(0, len(packet), 4, "zlib")])
        self.assertEqual(intact_reader.read(0, 4), b"page")
        for payload, count in ((packet[:-1], 4), (packet + b"extra", 4), (packet, 2), (packet, 8), (b"not zlib", 4)):
            reader = PagedResource(io.BytesIO(payload), [Segment(0, len(payload), count, "zlib")])
            with self.assertRaisesRegex(ValueError, "Invalid compressed segment or decoded extent"):
                reader.read(0, 1)
        for seg in (Segment(0, 1, 2, "raw"), Segment(2, 1, 1, "raw"), Segment(-1, 1, 1, "raw"),
                    Segment(0, 0, 0, "raw"), Segment(0, 1, 0, "zlib"), Segment(0, 0, 1, "zlib")):
            with self.assertRaises(ValueError):
                PagedResource(io.BytesIO(b"ab"), [seg])
        reader = PagedResource(io.BytesIO(b"abcdef"), [Segment(0, 2, 2, "raw"),
                               Segment(2, 2, 2, "other"), Segment(4, 2, 2, "raw")])
        self.assertEqual(reader.read(4, 2), b"ef")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            reader.read(1, 2)

    def test_section_windows(self):
        doc = struct.pack(">4sI4s4sI", b"HEAD", 12, b"data", b"FOOT", 8)
        packet = zlib.compress(doc)
        self.assertEqual(scan_window(packet, 16, "HEAD", "FOOT"), (1, {"HEAD FOOT": 1}))
        self.assertEqual(scan_window(packet, 1, "HEAD", "FOOT"), (0, {}))
        self.assertEqual(scan_window(bytes(15) + packet, 1, "HEAD", "FOOT"), (1, {"HEAD FOOT": 1}))
        self.assertEqual(scan_window(packet, 0, "OTHER", "FOOT"), (1, {}))
        self.assertEqual(scan_window(packet[:-1], 0, "HEAD", "FOOT"), (0, {}))
        for data in (doc + b"x", doc[:-1], doc[:4] + bytes(4), doc[:12] + b"\0" * 8, b"head" + doc[4:]):
            self.assertFalse(sections(data))
        self.assertEqual(sections(struct.pack(">4sI4sI", b"A_1Z", 8, b"Z9_0", 8)), ("A_1Z", "Z9_0"))
        self.assertEqual(scan_window(packet, 0, "HEAD", "TAIL"), (1, {}))
        # An aligned method byte as the final byte cannot start a stream and is skipped.
        self.assertEqual(scan_window(packet + bytes(-len(packet) % 16) + b"\x78", 0, "HEAD", "FOOT"),
                         (1, {"HEAD FOOT": 1}))
        # CMF 0x08 declares a 256-byte window; 0x0899 is a multiple of 31.
        compressor = zlib.compressobj(wbits=9)
        small_window = b"\x08\x99" + (compressor.compress(doc) + compressor.flush())[2:]
        self.assertEqual(scan_window(small_window, 0, "HEAD", "FOOT"), (1, {"HEAD FOOT": 1}))

    def test_section_window_size_bounds(self) -> None:
        def table(size: int) -> bytes:
            return struct.pack(">4sI", b"HEAD", size - 8).ljust(size - 8, b"\0") + struct.pack(">4sI", b"FOOT", 8)

        # Decoded output is limited to 256 KiB inclusive, and the compressed input to
        # 256 KiB. A stored (level 0) stream adds 6 bytes of header and checksum and
        # 5 bytes per block of at most 65535 bytes: 26 bytes for 192 KiB, 31 for 256 KiB.
        for label, packet, expected in (
            ("decoded at limit", zlib.compress(table(256 << 10)), (1, {"HEAD FOOT": 1})),
            ("decoded above limit", zlib.compress(table((256 << 10) + 1)), (0, {})),
            ("stored below limit", zlib.compress(table(192 << 10), 0), (1, {"HEAD FOOT": 1})),
            ("stored above limit", zlib.compress(table(256 << 10), 0), (0, {})),
        ):
            with self.subTest(label=label):
                self.assertEqual(scan_window(packet, 0, "HEAD", "FOOT"), expected)

    def test_section_probe_counts_smaller_zlib_windows(self) -> None:
        doc = struct.pack(">4sI4s4sI", b"HEAD", 12, b"data", b"FOOT", 8)
        packets = []
        for window_bits in range(9, 16):
            compressor = zlib.compressobj(wbits=window_bits)
            packets.append(compressor.compress(doc) + compressor.flush())
        dictionary = zlib.compressobj(wbits=9, zdict=b"HEAD data FOOT")
        packets.append(dictionary.compress(doc) + dictionary.flush())
        archive = b"".join(packet + bytes(-len(packet) % 16) for packet in packets)
        archive += b"\0" + packets[0]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-sections.bin"
            path.write_bytes(archive)
            report = inspect_sections(path, "HEAD", "FOOT")
        self.assertEqual(report["valid_zlib"], 7)
        self.assertEqual(report["section_records"], 7)
        self.assertEqual(report["layouts"], {"HEAD FOOT": 7})

    def test_section_probe_cli_validates_tags(self) -> None:
        script = Path(__file__).resolve().parents[1] / "tools" / "section_probe.py"
        doc = struct.pack(">4sI4s4sI", b"HEAD", 12, b"data", b"FOOT", 8)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-sections.bin"
            path.write_bytes(zlib.compress(doc))

            def probe(first: str, last: str) -> subprocess.CompletedProcess[str]:
                return run_cli(section_probe_main, path, "--first", first, "--last", last)

            for first, last in (("head", "FOOT"), ("HEAD", "foot"), ("HEA", "FOOT"), ("HEADS", "FOOT"),
                                ("HÉAD", "FOOT"), ("HEAD", "FÖOT")):
                result = probe(first, last)
                self.assertEqual(result.returncode, 2, (first, last, result.stdout, result.stderr))
                self.assertIn("section tag", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
            result = subprocess.run([sys.executable, "-B", str(script), str(path), "--first", "HEAD",
                                     "--last", "FOOT"], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)[0]["section_records"], 1)


if __name__ == "__main__":
    unittest.main()
