"""Source-built SGD5 program groups and independently framed effect records."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import zlib


def word(n: int) -> bytes:
    return struct.pack("<I", n)


def name(s: str) -> bytes:
    encoded = s.encode("ascii") + b"\0"
    return bytes([len(encoded)]) + encoded


def deflate(blob: bytes, mode: int = zlib.Z_FINISH) -> bytes:
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(blob) + compressor.flush(mode)


def program(label: str, kind: int, identity: int, *, words: int = 8, trailer: bytes = b"",
            payload_magic: bytes = b"BC\xc0\xde", dxil: bytes | None = None, pack=deflate,
            declared: int = 0, reflection: tuple[int, int, int, int] = (0, 0, 9, 9)) -> tuple[bytes, str]:
    if dxil is None:
        dxil = struct.pack("<HHI4sIII4sI", 0x60, kind, words, b"DXIL", 0x100, 16, 8, payload_magic, identity) + trailer
    blob = b"DXBC" + bytes(16) + struct.pack("<4I", 1, 44 + len(dxil), 1, 36) + b"DXIL" + word(len(dxil)) + dxil
    packed = pack(blob)
    size = 12 + len(packed)
    wrapped = struct.pack("<3I", 0xddce7, size, len(blob) + declared) + packed
    # Reflection word, then register count, buffer count, an opaque half and the repeated length.
    registers, buffers, length, repeated = reflection
    block = word(length) + struct.pack("<4H", registers, buffers, 0, repeated) + bytes(length - 8)
    record = name(label) + b"\x20" + word(size) + wrapped + bytes(152) + block
    return record, hashlib.sha256(blob).hexdigest()


def effect(label: str, vs: list[int], ps: list[int]) -> bytes:
    lists = b"".join(word(len(group)) + b"".join(map(word, group)) for group in (vs, ps, [], [], [], []))
    strings = b"\0sample\0"
    return name(label) + word(0) + lists + b"\xff\xff" + word(len(strings)) + strings


class EffectChecks(unittest.TestCase):
    def test_padded_dxil_part(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from sgd5_effects import candidates

        # The part occupies nine words. Its bitcode ends at the eighth word.
        for words in (8, 7, 10):
            with self.subTest(words=words):
                entry, digest = program("padded", 0, 7, words=words, trailer=bytes(4))
                table = b"SGD5" + bytes(28) + word(0) + word(1) + entry + word(0) * 4 + word(1)
                data = table + effect("probe", [], [0])
                if words == 8:
                    result = candidates(io.BytesIO(data), "probe", 0, len(data), 32, len(table), len(data))
                    self.assertEqual([(item.dxil_kind, item.container_sha256) for item in result], [(0, digest)])
                else:
                    with self.assertRaises(ValueError):
                        candidates(io.BytesIO(data), "probe", 0, len(data), 32, len(table), len(data))

    def test_container_fields(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from sgd5_effects import candidates

        def inventory(entry: bytes) -> list:
            table = b"SGD5" + bytes(28) + word(0) + word(1) + entry + word(0) * 4 + word(1)
            data = table + effect("probe", [], [0])
            return candidates(io.BytesIO(data), "probe", 0, len(data), 32, len(table), len(data))

        # One register and one buffer add 24 bytes each to the 9-byte reflection block: 9 + 48 = 57.
        entry, digest = program("fields", 0, 5, reflection=(1, 1, 57, 57))
        self.assertEqual([item.container_sha256 for item in inventory(entry)], [digest])

        def header(tag: bytes = b"DXIL", start: int = 16, size: int = 8) -> bytes:
            # Eight declared words hold 32 bytes; 8 + 15 + 8 and 8 + 16 + 3 both still fit.
            return struct.pack("<HHI4sIII4sI", 0x60, 0, 8, tag, 0x100, start, size, b"BC\xc0\xde", 5)

        for case, message, options in (
            ("repeated reflection length", "reflection block", {"reflection": (0, 0, 9, 10)}),
            ("empty compressed stream", "bounded", {"pack": lambda blob: b""}),
            ("bytes after the final block", "container disagrees", {"pack": lambda blob: deflate(blob) + b"\0"}),
            ("no final block", "container disagrees", {"pack": lambda blob: deflate(blob, zlib.Z_SYNC_FLUSH)}),
            ("decoded size one byte long", "container disagrees", {"declared": 1}),
            ("part shorter than its header", "Truncated", {"dxil": header()[:20]}),
            ("program tag", "invalid header", {"dxil": header(tag=b"DXBC")}),
            ("payload offset 15", "invalid header", {"dxil": header(start=15)}),
            ("payload size 3", "invalid header", {"dxil": header(size=3)}),
        ):
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, message):
                inventory(program("fields", 0, 5, **options)[0])

    def test_candidates_and_boundaries(self) -> None:
        tools_dir = str(Path(__file__).resolve().parents[1] / "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        from sgd5_effects import main as sgd5_main

        tool = Path(tools_dir) / "sgd5_effects.py"
        mesh, mesh_digest = program("shared", 13, 1, payload_magic=b"TEST")
        ps0, first_digest = program("shared", 0, 2)
        ps1, second_digest = program("shared", 0, 3)
        arrays = word(1) + mesh + word(2) + ps0 + ps1 + word(0) * 4
        first = effect("empty", [], [])
        second = effect("choice", [0], [1, 0])
        header = bytes(19) + b"SGD5" + bytes(28)
        begin = len(header) + len(arrays) + 4
        selected = begin + len(first)
        archive = header + arrays + word(2) + first + second
        end = len(archive)
        group_start = 19
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.bin"

            def argv(label: str, start: int, stop: int, group_end: int) -> list[str]:
                return [str(path), label, "--group", str(group_start), str(group_end),
                        "--arrays", str(len(header)), "--record", str(start), str(stop)]

            def run(data: bytes, label: str = "choice", start: int = selected, stop: int = end,
                    group_end: int = end) -> subprocess.CompletedProcess[str]:
                path.write_bytes(data)
                args = argv(label, start, stop, group_end)
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    try:
                        rc = sgd5_main(args)
                    except SystemExit as exc:
                        if exc.code is None:
                            rc = 0
                        elif isinstance(exc.code, int):
                            rc = int(exc.code)
                        else:
                            stderr.write(f"{exc.code}\n")
                            rc = 1
                return subprocess.CompletedProcess(args, rc, stdout.getvalue(), stderr.getvalue())

            def run_subprocess(data: bytes, label: str = "choice", start: int = selected, stop: int = end,
                               group_end: int = end) -> subprocess.CompletedProcess[str]:
                path.write_bytes(data)
                return subprocess.run([sys.executable, "-B", tool, *argv(label, start, stop, group_end)],
                                      capture_output=True, text=True)

            expected_choice = {"effect": "choice", "candidates": [
                {"group": "VS", "index": 0, "name": "shared", "dxil_kind": 13, "container_sha256": mesh_digest},
                {"group": "PS", "index": 1, "name": "shared", "dxil_kind": 0, "container_sha256": second_digest},
                {"group": "PS", "index": 0, "name": "shared", "dxil_kind": 0, "container_sha256": first_digest},
            ]}
            result = run_subprocess(archive)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), expected_choice)
            in_proc = run(archive)
            self.assertEqual(in_proc.returncode, 0, in_proc.stderr)
            self.assertEqual(json.loads(in_proc.stdout), expected_choice)
            empty = run(archive, "empty", begin, selected)
            self.assertEqual(empty.returncode, 0, empty.stderr)
            self.assertEqual(json.loads(empty.stdout), {"effect": "empty", "candidates": []})
            # Each mutation keeps the requested name and plausible neighboring bytes.
            prefix = selected + len(name("choice")) + 4
            for offset, value in ((prefix + 4, 1), (prefix + 12, 2), (prefix, 65537),
                                  (len(header), 65537), (begin - 4, 0), (begin - 4, 0xffffffff), (end - 12, 7)):
                with self.subTest(offset=offset, value=value):
                    changed = bytearray(archive)
                    struct.pack_into("<I", changed, offset, value)
                    rejected = run(changed)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertEqual(rejected.stdout, "")

            for label, start, stop, limit in (("missing", selected, end, end), ("choice", selected + 1, end, end),
                                              ("choice", begin, selected, end), ("choice", selected, end - 1, end),
                                              ("choice", selected, end, end - 1)):
                with self.subTest(label=label, start=start, stop=stop, limit=limit):
                    rejected = run(archive, label, start, stop, limit)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertEqual(rejected.stdout, "")

    def test_selected_container_reads(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from sgd5_effects import candidates

        unused, _ = program("unused", 0, 117)
        selected, expected = program("selected", 0, 221)
        prefix = b"SGD5" + bytes(28) + word(0) + word(2)
        table = prefix + unused + selected + word(0) * 4 + word(1)
        packed = len(prefix) + len(name("unused")) + 5
        unused_end = packed + struct.unpack_from("<I", table, packed - 4)[0]
        selected_wrapper = len(prefix) + len(unused) + len(name("selected")) + 5
        data = table + effect("probe", [], [1])

        class Guard(io.BytesIO):
            block_selected = False

            def read(self, size: int = -1, /) -> bytes:
                start = self.tell()
                if size < 0 or start < unused_end and start + size > packed + 12:
                    raise AssertionError("Read unreferenced compressed payload")
                if self.block_selected and start == selected_wrapper + 12:
                    raise AssertionError("Read candidate payload before applying budget")
                return super().read(min(size, 7))

        got = candidates(Guard(data), "probe", 0, len(data), 32, len(table), len(data))
        self.assertEqual([item.container_sha256 for item in got], [expected])
        # The selected record itself cannot provide space for a second effect.
        phantom = table[:-4] + word(2) + data[len(table):-12] + bytes(48) + data[-12:]
        with self.assertRaisesRegex(ValueError, "effect count"):
            candidates(Guard(phantom), "probe", 0, len(phantom), 32, len(table), len(phantom))
        changed = bytearray(table + effect("probe", [], [1] * 4))
        struct.pack_into("<I", changed, selected_wrapper + 8, 16 << 20)
        guarded = Guard(changed)
        guarded.block_selected = True
        with self.assertRaisesRegex(ValueError, "combined"):
            candidates(guarded, "probe", 0, len(changed), 32, len(table), len(changed))
        for offset, replacement in ((selected_wrapper, word(7)), (selected_wrapper + 4, word(13)),
                                    (selected_wrapper + 8, word(16 << 20 | 1)),
                                    (selected_wrapper + 12, b"\x07")):
            changed = bytearray(data)
            changed[offset:offset + len(replacement)] = replacement
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                candidates(Guard(changed), "probe", 0, len(data), 32, len(table), len(data))


if __name__ == "__main__":
    unittest.main()
