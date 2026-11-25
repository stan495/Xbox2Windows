# Reverse encoder adapted from bcpack-rs by Ahmet Dedeler
# (https://github.com/Ahmet-Dedeler, https://ahmetdedeler.com).
# Source: https://github.com/Ahmet-Dedeler/bcpack-rs, src/encode.rs
# SPDX-License-Identifier: MIT OR Apache-2.0
# Preserve this credit when copying or porting the encoder; see tools/_bcpack_rans/.
"""Synthetic MDU entropy controls; no texture payload or endpoint codebook."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
from bcpack_symbols import BCPackError, MAX_INPUT, MAX_SYMBOLS, decode
from _bcpack_rans import LEGAL, model
from _bcpack_rans.bcpack_tables import (BASE_TABLE, FREQ_TABLE, RF_TABLE, RN_TABLE,
                                      RS_TABLE, TABLES_SHA256)
from bcpack_mode6_vector import PACKED

VECTORS = ROOT / "tools" / "_bcpack_rans" / "vectors"
BC1_WIDTHS = (8, 8, 5, 6, 5, 5, 6, 5, 8, 8, 8, 8)


def encode_fragment(bits, q, values, narrow=0, flush=True, trim=False):
    """Test-only reverse rANS equation; no decoder inverse or scheduling calls.

    Model intervals come directly from the published tables. Optionally omit up
    to two zeros emitted when encoding the last symbol, as the upstream encoder
    does. Interleave scheduling is not used by this single-lane encoder.
    """
    eab = bits - 1 - narrow
    state = 1 << 24
    emitted = bytearray()
    last_emitted = 0
    for index, value in enumerate(reversed(values)):
        segment = value.bit_length()
        first = 0 if not segment else 1 << (segment - 1)
        frequency = FREQ_TABLE[eab][q][segment]
        start = BASE_TABLE[eab][q][segment] + frequency * (value - first)
        if start == 0:
            frequency -= 1
            start = 1
        if not frequency:
            raise ValueError("Zero-frequency test symbol")
        while state >= frequency << 16:
            emitted.append(state & 255)
            state >>= 8
        if index == 0:
            last_emitted = len(emitted)
        state = (state // frequency << 16) + state % frequency + start
    omit = 0
    if trim:
        while omit < min(2, last_emitted) and emitted[omit] == 0:
            omit += 1
    payload = state.to_bytes(4, "big") + bytes(reversed(emitted[omit:]))
    header = (narrow << 6) | (0x10 if flush else 0) | q
    if len(payload) <= 256:
        return bytes((header, len(payload) - 1)) + payload
    return bytes((header | 0x20,)) + (len(payload) - 257).to_bytes(2, "little") + payload


class SymbolChecks(unittest.TestCase):
    def test_published_table_checksum_and_inverse(self):
        canonical = "".join(",".join(str(v) for alphabet in table for row in alphabet for v in row) + "\n"
                            for table in (BASE_TABLE, FREQ_TABLE, RF_TABLE, RS_TABLE, RN_TABLE))
        self.assertEqual(hashlib.sha256(canonical.encode()).hexdigest(), TABLES_SHA256)
        for bits in range(1, 10):
            for q in range(16):
                m = model(bits - 1, q)
                for symbol in range(1 << bits):
                    frequency, start = m.forward(symbol)
                    if frequency:
                        for slot in {start, start + frequency // 2, start + frequency - 1}:
                            self.assertEqual(m.inverse(slot), symbol, (bits, q, symbol, slot))

    def test_rust_generated_streams(self):
        # Exact upstream encoder inputs/outputs, checked by its Rust test suite.
        for seed in (11, 12):
            vector = json.loads((VECTORS / f"synthetic_{seed}.json").read_text())
            packet = bytes.fromhex(vector["chunk_hex"])
            result = decode(packet, BC1_WIDTHS, vector["blocks"])
            self.assertEqual(result["syms"], vector["symbols"])
            self.assertEqual(result["consumed"], len(packet))
            self.assertTrue(all(lane["final"] in LEGAL for lane in result["lanes"]))
            # The backend's callback-only order trace stays private.
            self.assertEqual(set(result), {"syms", "consumed", "cycles", "lanes"})
            padded = decode(packet + bytes(17), BC1_WIDTHS, vector["blocks"])
            self.assertEqual(padded, result)
        self.assertTrue(any(len(lane["frags"]) > 1 for lane in result["lanes"]))

    def test_all_models_and_alphabet_widths(self):
        for bits in range(1, 10):
            for q in range(16):
                m = model(bits - 1, q)
                choices = [s for s in range(1 << bits) if m.forward(s)[0]]
                values = (choices + choices[::-1]) * 2
                with self.subTest(bits=bits, q=q):
                    for trim in (False, True):
                        packet = encode_fragment(bits, q, values, trim=trim)
                        self.assertEqual(decode(packet, [bits], len(values))["syms"], [values])

    def test_sixteen_lane_mode6_entropy(self):
        # Existing public synthetic packet from issue #442. These symbols were
        # cross-checked with Leonida 6a6704d's decode_lanes, which also rebuilds
        # the issue's known mode-6 block exactly without provisional history.
        widths = [4, 4, 8, 8, 8, 8, 8, 7, 7, 5, 6, 8, 8, 8, 8, 5]
        expected = [[0, 0], [0, 0], [117, 223], [70, 95], [40, 75], [24, 124],
                    [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [8, 148],
                    [228, 254], [219, 107], [39, 2], [20, 0]]
        result = decode(PACKED, widths, 2)
        self.assertEqual(result["syms"], expected)
        self.assertEqual(result["consumed"], len(PACKED))
        self.assertTrue(all(lane["final"] in LEGAL for lane in result["lanes"]))
        for end in (48, 96, len(PACKED) - 1):
            with self.subTest(end=end), self.assertRaises(BCPackError):
                decode(PACKED[:end], widths, 2)

    def test_narrowing_long_headers_and_flushed_fragments(self):
        values = list(range(32)) * 30
        packet = encode_fragment(8, 0, values, narrow=3)
        self.assertTrue(packet[0] & 0x20)  # three-byte header, payload > 256
        result = decode(packet, [8], len(values))
        self.assertEqual(result["syms"], [values])
        packet = encode_fragment(5, 0, values[:90]) + encode_fragment(5, 7, values[90:])
        result = decode(packet, [5], len(values))
        self.assertEqual(result["syms"], [values])
        self.assertEqual(len(result["lanes"][0]["frags"]), 2)

    def test_reject_malformed_streams(self):
        vector = json.loads((VECTORS / "synthetic_11.json").read_text())
        packet = bytes.fromhex(vector["chunk_hex"])
        for end in (1, 2, 7, 8, 15, 47, 48, len(packet) // 2, len(packet) - 1):
            with self.subTest(truncated=end), self.assertRaises(BCPackError):
                decode(packet[:end], BC1_WIDTHS, vector["blocks"])
        with self.assertRaisesRegex(BCPackError, "trailing"):
            decode(packet + b"\x00\x01", BC1_WIDTHS, vector["blocks"])
        with self.assertRaises(BCPackError):
            decode(packet, BC1_WIDTHS, vector["blocks"] + 1)
        with self.assertRaisesRegex(BCPackError, "alphabet"):
            decode(b"\xc0\x03\x01\x00\x00\x00", [1], 1)
        with self.assertRaisesRegex(BCPackError, "illegal"):
            decode(bytes.fromhex("10 03 01 00 00 02"), [1], 1)

    def test_argument_bounds(self):
        for widths, count, data in (([], 1, b"x"), ([8] * 17, 1, b"x"),
                                    ([0], 1, b"x"), ([10], 1, b"x"), ([True], 1, b"x"),
                                    ([8], 0, b"x"), ([8], True, b"x"),
                                    ([8, 8], MAX_SYMBOLS, b"x"), ([8], 1, b""),
                                    ([8], 1, bytes(MAX_INPUT + 1))):
            with self.subTest(widths=widths, count=count, length=len(data)), self.assertRaises(ValueError):
                decode(data, widths, count)
        # A well-formed stream must still arrive as bytes, not as a list or str.
        values = [0, 1] * 7
        packet = encode_fragment(1, 0, values)
        self.assertEqual(decode(packet, [1], len(values))["syms"], [values])
        for data in (list(packet), packet.decode("latin-1")):
            with self.subTest(kind=type(data).__name__), self.assertRaisesRegex(ValueError, "bytes"):
                decode(data, [1], len(values))

    def test_flushed_fragment_must_reach_home_state(self):
        # A real encoder can omit the last zero byte here. But dropping
        # additional bytes, even while shortening the declared payload length,
        # must not silently advance to the next fragment's header.
        values = [0, 1, 2, 3] * 5
        first = encode_fragment(2, 0, values)
        second = encode_fragment(2, 7, values)
        self.assertFalse(first[0] & 0x20)
        self.assertEqual(first[-1], 0)
        expected = [values + values]
        self.assertEqual(decode(first + second, [2], len(values) * 2)["syms"], expected)
        valid_trim = bytes((first[0], first[1] - 1)) + first[2:-1] + second
        self.assertEqual(decode(valid_trim, [2], len(values) * 2)["syms"], expected)
        for missing in (2, 3, 4):
            with self.subTest(missing=missing):
                truncated = bytes((first[0], first[1] - missing)) + first[2:-missing] + second
                with self.assertRaisesRegex(BCPackError, "flushed fragment did not return"):
                    decode(truncated, [2], len(values) * 2)

    def test_exact_public_api_limits(self):
        # The backend would allocate millions of symbols. A sentinel proves
        # that the public validator accepts the exact bound, without decoding
        # or materializing a near-gigabyte result in a unit test.
        class EnteredDecoder(Exception):
            pass

        for lanes in (1, 2, 16):
            widths = [8] * lanes
            max_per_lane = MAX_SYMBOLS // lanes
            with self.subTest(lanes=lanes), patch(
                "bcpack_symbols.decode_lanes", side_effect=EnteredDecoder
            ):
                with self.assertRaises(EnteredDecoder):
                    decode(b"\x10", widths, max_per_lane)
                with self.assertRaisesRegex(ValueError, "at most 1048576"):
                    decode(b"\x10", widths, max_per_lane + 1)
        with patch("bcpack_symbols.decode_lanes", side_effect=EnteredDecoder):
            with self.assertRaises(EnteredDecoder):
                decode(bytes(MAX_INPUT), [8], 1)
            with self.assertRaisesRegex(ValueError, "2097152 bytes"):
                decode(bytes(MAX_INPUT + 1), [8], 1)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.bin"
            values = [0, 1] * 7
            path.write_bytes(encode_fragment(1, 0, values))
            command = [sys.executable, "-B", str(ROOT / "tools/bcpack_symbols.py"), str(path),
                       "--alphabet-bits", "1", "--symbols", str(len(values))]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)["syms"], [values])

            def assert_clean_failure(arguments, message):
                failed = subprocess.run(arguments, capture_output=True, text=True)
                self.assertEqual(failed.returncode, 1)
                self.assertEqual(failed.stdout, "")
                self.assertIn("BCPack symbols: " + message, failed.stderr)
                self.assertNotIn("Traceback", failed.stderr)

            path.write_bytes(b"\xc0")
            assert_clean_failure(command, "")
            # Zero padding is accepted, but the file read stops one byte past
            # the 2 MiB bound: the same valid stream padded to MAX_INPUT + 1 fails.
            path.write_bytes(encode_fragment(1, 0, values).ljust(MAX_INPUT + 1, b"\0"))
            assert_clean_failure(command, "Expected 1..2097152 bytes")
            # Argument errors and unreadable paths also exit 1 without a traceback.
            assert_clean_failure(command[:5] + ["10"] + command[6:], "Expected integer symbol widths")
            assert_clean_failure(command[:3] + [str(path) + ".missing"] + command[4:], "")


if __name__ == "__main__":
    unittest.main()
