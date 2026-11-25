"""Separate census populations using owned, non-executable framing fixtures."""
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
from shader_census import cache_index, census
import shader_profile_audit


def container(digest_byte, word, stage=0):
    # This is a framing probe, not executable DXIL. Two different containers
    # deliberately share the same unverified 16-byte header digest.
    program = struct.pack("<II4sIII", (stage << 16) | 0x60, 7, b"DXIL", 0x100, 16, 4)
    program += struct.pack("<I", word)
    return (b"DXBC" + bytes([digest_byte]) * 16
            + struct.pack("<4I", 1, 44 + len(program), 1, 36)
            + b"DXIL" + struct.pack("<I", len(program)) + program)


def record(blob):
    encoder = zlib.compressobj(wbits=-15)
    payload = encoder.compress(blob) + encoder.flush()
    return b"owned_shader\0\0" + struct.pack("<4I", len(payload), 0, len(payload), len(blob)) + payload


def archive_fixture():
    a = container(1, 17)
    b = container(1, 18)
    c = container(2, 19, stage=5)
    return record(a) + record(a) + record(b) + record(c)


def cache_fixture(entries=None):
    entries = entries if entries is not None else [(16 * i, 16) for i in range(5)]
    index_start = 128 + 0x70 + 2 * 0x18 + 3 * 8
    code_base = index_start + len(entries) * 8
    data = bytearray(code_base + 96)
    data[:4] = b"PS\x00\x09"
    struct.pack_into("<4I", data, 80, 1, 2, 3, len(entries))
    for i, entry in enumerate(entries):
        struct.pack_into("<2I", data, index_start + i * 8, *entry)
    return data, code_base


class CensusChecks(unittest.TestCase):
    def test_distinct_populations_and_keys(self):
        data, base = cache_fixture()
        result = census(archive_fixture(), cache_blob=data, code_base=base)
        self.assertEqual(result["archive_records"]["count"], 4)
        self.assertEqual(result["archive_records"]["stages"], {"pixel": 3, "compute": 1})
        self.assertTrue(result["archive_records"]["coverage_denominator"])
        self.assertEqual(result["dxbc_header_digests"]["count"], 2)
        self.assertFalse(result["dxbc_header_digests"]["integrity_verified"])
        self.assertEqual(result["classification_cache"]["count"], 3)
        self.assertIn("SHA-256", result["classification_cache"]["key"])
        self.assertEqual(result["cache_index"]["count"], 5)
        self.assertEqual(result["cache_index"]["distinct_spans"], 5)
        self.assertEqual(result["cache_index"]["key"], "(code_offset, allocation_size)")
        self.assertEqual(result["cache_index"]["program_validation"], "not performed")
        # The index follows the header and three tables: 128 + 1 * 0x70 + 2 * 0x18 + 3 * 8.
        self.assertEqual(result["cache_index"]["index_offset"], 312)
        self.assertEqual(result["cache_index"]["code_base"], base)

    def test_header_digest_covers_bytes_4_through_19(self):
        # Changing only the first or the last digest byte gives a new header key.
        first, last = bytearray(container(1, 17)), bytearray(container(1, 17))
        first[4] = 9
        last[19] = 9
        result = census(record(container(1, 17)) + record(first) + record(last))
        self.assertEqual(result["archive_records"]["count"], 3)
        self.assertEqual(result["dxbc_header_digests"]["count"], 3)
        self.assertEqual(result["classification_cache"]["count"], 3)

    def test_profile_audit_keeps_occurrences_and_sha256_cache(self):
        # Isolate record counting from LLVM classification. Each distinct full
        # container is classified once even when header digests collide.
        with patch.object(shader_profile_audit, "classify", return_value=(["owned"], "")) as classify:
            result = shader_profile_audit.scan(archive_fixture(), None)
        self.assertEqual(classify.call_count, 3)
        self.assertEqual(result["records"], {"PS": 3, "CS": 1})
        self.assertEqual(result["hlsl_records"], {"PS": 3, "CS": 1})
        self.assertEqual(set(result), {"records", "hlsl_records", "profiles", "first_stops"})

    def test_absent_cache_is_unknown_and_invalid_archive_is_excluded(self):
        self.assertIsNone(census(archive_fixture())["cache_index"])
        bad = bytearray(container(3, 20))
        struct.pack_into("<I", bad, 24, len(bad) - 1)
        result = census(archive_fixture() + record(bad))
        self.assertEqual(result["archive_records"]["count"], 4)
        self.assertEqual(result["candidate_rejections"]["container_framing"], 1)
        self.assertEqual(census(b"")["archive_records"]["count"], 0)

    def test_index_aliases_and_directory_order(self):
        for entries, distinct in (([(0, 16), (16, 16), (0, 16)], 2),
                                  ([(32, 16), (0, 16), (16, 16)], 3)):
            data, base = cache_fixture(entries)
            result = cache_index(data, base)
            self.assertEqual(result["count"], 3)
            self.assertEqual(result["distinct_spans"], distinct)
            self.assertEqual(result["aliased_entries"], 3 - distinct)
        data = bytearray(128)
        data[:4] = b"PS\x00\x09"
        self.assertEqual(cache_index(data, 128)["count"], 0)

    def test_index_bounds_are_inclusive(self):
        # The fixture leaves a 96-byte code area; an allocation may end on its last byte.
        data, base = cache_fixture([(80, 16)])
        self.assertEqual(cache_index(data, base)["distinct_spans"], 1)
        # 1 << 20 aliased entries is the largest accepted index; one more is refused.
        # Zero table counts put the index at 128 and the code area right after it.
        for count, accepted in ((1 << 20, True), ((1 << 20) + 1, False)):
            data = bytearray(128 + count * 8 + 16)
            data[:4] = b"PS\x00\x09"
            struct.pack_into("<4I", data, 80, 0, 0, 0, count)
            data[128:128 + count * 8] = struct.pack("<2I", 0, 16) * count
            with self.subTest(count=count):
                if accepted:
                    result = cache_index(data, 128 + count * 8)
                    self.assertEqual((result["count"], result["distinct_spans"]), (count, 1))
                else:
                    with self.assertRaisesRegex(ValueError, "index entry limit"):
                        cache_index(data, 128 + count * 8)

    def test_bad_cache_headers_and_tables(self):
        data, base = cache_fixture()
        for length in (0, 4, 127):
            with self.subTest(length=length), self.assertRaises(ValueError):
                cache_index(data[:length], base)
        for value in (-1, 127, len(data) + 1, True):
            with self.subTest(base=value), self.assertRaises(ValueError):
                cache_index(data, value)
        for field in (0, 80, 84, 88, 92):
            bad = bytearray(data)
            struct.pack_into("<I", bad, field, 0xFFFFFFFF)
            with self.subTest(field=field), self.assertRaises(ValueError):
                cache_index(bad, base)
        with self.assertRaises(ValueError):
            cache_index(data, base - 1)

    def test_bad_allocations(self):
        for entries in ([(0, 0)], [(1, 16)], [(8, 16)], [(0, 97)], [(0xFFFFFFF0, 32)],
                        [(0, 32), (16, 16)], [(0, 16), (0, 32)]):
            data, base = cache_fixture(entries)
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                cache_index(data, base)
        data, base = cache_fixture()
        with self.assertRaises(ValueError):
            census(b"", cache_blob=data)
        with self.assertRaises(ValueError):
            census(b"", code_base=base)

    def test_cli_json_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "owned.archive"
            cache = Path(directory) / "decoded.cache"
            archive.write_bytes(archive_fixture())
            data, base = cache_fixture()
            cache.write_bytes(data)
            command = [sys.executable, "-B", str(ROOT / "tools/shader_census.py"), str(archive)]
            arguments = ["--cache-blob", str(cache), "--code-base", hex(base)]
            result = subprocess.run(command + arguments, check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)["cache_index"]["count"], 5)
            cache.write_bytes(b"invalid")
            failed = subprocess.run(command + arguments, capture_output=True, text=True)
            self.assertEqual(failed.returncode, 1)
            self.assertEqual(failed.stdout, "")
            self.assertIn("Shader census:", failed.stderr)
            failed = subprocess.run(command + ["--code-base", hex(base)], capture_output=True)
            self.assertEqual(failed.returncode, 2)
            archive.write_bytes(b"")
            result = subprocess.run(command, check=True, capture_output=True, text=True)
            self.assertIsNone(json.loads(result.stdout)["cache_index"])


if __name__ == "__main__":
    unittest.main()
