"""Controls for the relocated PRD0 v0x7a root directory."""
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
BASE = 0x50000000
ROOT_TYPE = 0x1234abcd


def resource() -> bytearray:
    data = bytearray(400)
    struct.pack_into("<Q4sH", data, 8, BASE + 112, b"0DRP", 122)
    struct.pack_into("<I", data, 32, 2)
    struct.pack_into("<Q", data, 40, BASE + 160)
    struct.pack_into("<Q", data, 56, BASE + 256)
    struct.pack_into("<3H", data, 72, 2, 0, 2)
    for address, kind, size in ((160, ROOT_TYPE, 32), (192, 0x8888, 16)):
        struct.pack_into("<I", data, address, kind)
        struct.pack_into("<I", data, address + 24, size)
    struct.pack_into("<IIQIIQ", data, 256, 0x8888, 16, BASE + 304, ROOT_TYPE, 32, BASE + 352)
    return data


class RootChecks(unittest.TestCase):
    def test_member_cli(self) -> None:
        data = resource()
        packed = zlib.compress(data)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "archive"
            path.write_bytes(b"abc" + packed + b"next")
            command = [sys.executable, "-B", str(ROOT / "tools/resource_root.py"), str(path),
                       "--offset", "3", "--decoded", str(len(data)), "--stored"]
            result = subprocess.run([*command, str(len(packed))], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"root_type_hash": "0x1234abcd", "root_bytes": 32})
            for extra in (-1, 1):
                failed = subprocess.run([*command, str(len(packed) + extra)], capture_output=True, text=True)
                self.assertNotEqual(failed.returncode, 0)
                self.assertEqual(failed.stdout, "")

    def test_nearby_root_and_invalid_directories(self) -> None:
        from resource_root import inspect_root
        data = resource()
        struct.pack_into("<I", data, 32, 1)
        self.assertEqual(inspect_root(data), (0x8888, 16))
        for label, offset, fmt, value in (
            ("nonzero leading qword", 0, "<Q", 1 << 56), ("version high byte", 20, "<H", 0x17a),
            ("version", 20, "<H", 121), ("zero index", 32, "<I", 0), ("past last index", 32, "<I", 3),
            ("schema outside virtual region", 40, "<Q", 0x60000000),
            ("directory past buffer", 56, "<Q", BASE + 384),
            ("overlapping directories", 56, "<Q", BASE + 160),
            ("schema mismatch", 184, "<I", 24), ("duplicate root schema", 192, "<I", ROOT_TYPE),
            ("unknown type", 272, "<I", 3), ("unbounded root", 280, "<Q", BASE + 390),
            ("root aliases directory", 280, "<Q", BASE + 256),
            ("block map inside header", 8, "<Q", BASE + 32),
            ("block map outside virtual region", 8, "<Q", 0x60000000),
            ("block map past buffer", 8, "<Q", BASE + 400),
            ("block map aliases schemas", 8, "<Q", BASE + 160),
            ("block map aliases directory", 8, "<Q", BASE + 256),
            ("block map aliases root", 8, "<Q", BASE + 352),
        ):
            with self.subTest(label=label):
                invalid = resource()
                struct.pack_into(fmt, invalid, offset, value)
                with self.assertRaises(ValueError):
                    inspect_root(invalid)

    def test_block_map_boundaries(self) -> None:
        """Check valid boundary placements of the block-map pointer adjacent to tables and the buffer end."""
        from resource_root import inspect_root
        for offset in (96, 159, 255, 351, 384, 399):
            valid = resource()
            struct.pack_into("<Q", valid, 8, BASE + offset)
            self.assertEqual(inspect_root(valid), (ROOT_TYPE, 32))

    def test_empty_root_and_schema_stride(self) -> None:
        """Reject a zero-length root even when its schema also declares zero bytes; read types at a 32-byte stride."""
        from resource_root import inspect_root
        empty = resource()
        struct.pack_into("<I", empty, 276, 0)  # root block length (entry 2 at 256 + 16, length at +4)
        struct.pack_into("<I", empty, 184, 0)  # root schema size (entry 1 at 160, size at +24)
        with self.assertRaises(ValueError):
            inspect_root(empty)
        # Byte 16 of the first 32-byte schema entry is not a type field, so a copy of the
        # root type there is not a second schema. A 16-byte stride would read it at 176.
        inner = resource()
        struct.pack_into("<I", inner, 176, ROOT_TYPE)
        self.assertEqual(inspect_root(inner), (ROOT_TYPE, 32))

    def test_read_budget(self) -> None:
        from resource_root import read_member

        class Unreadable(io.BytesIO):
            def read(self, size: int = -1, /) -> bytes:
                raise AssertionError("Oversized member entered the reader")

        for stored, decoded in ((1, (2 << 20) + 1), ((2 << 20) + 1, 1)):
            with self.assertRaisesRegex(ValueError, "budget"):
                read_member(Unreadable(b"x"), 0, stored, decoded)

        # A member that decodes to exactly 2 MiB is within the budget.
        data = resource() + bytes((2 << 20) - 400)
        packed = zlib.compress(data)
        self.assertEqual(read_member(io.BytesIO(packed), 0, len(packed), len(data)), (ROOT_TYPE, 32))


if __name__ == "__main__":
    unittest.main()
