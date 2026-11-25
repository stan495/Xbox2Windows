"""Synthetic AMD64 PE32+ unit and refusal suite for tools/ffx_working_set_delta.py."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "ffx_working_set_delta.py"
sys.path.insert(0, str(ROOT / "tools"))
import ffx_working_set_delta  # noqa: E402


IMAGE_BASE = 0x180000000
RAW_OFFSET = 0x200
SECTION_ALIGN = 0x1000
TEXT_RVA = 0x1000
RDATA_RVA = 0x2000
SECTION_SIZE = 0x1000


def build_image(
    *,
    machine: int = 0x8664,
    magic: int = 0x20B,
    include_create: bool = True,
    include_release: bool = True,
    include_pdata: bool = True,
    multi_range_create: bool = False,
) -> bytes:
    """Build a minimal synthetic AMD64 PE32+ DLL for ffx_working_set_delta."""
    text = bytearray(SECTION_SIZE)
    rdata = bytearray(SECTION_SIZE)
    text_cursor = 0
    rdata_cursor = 0

    def emit_text(blob: bytes) -> tuple[int, int]:
        nonlocal text_cursor
        start = TEXT_RVA + text_cursor
        text[text_cursor:text_cursor + len(blob)] = blob
        text_cursor += len(blob)
        return start, TEXT_RVA + text_cursor

    def alloc_rdata(size: int, align: int = 4) -> int:
        nonlocal rdata_cursor
        rdata_cursor = (rdata_cursor + align - 1) & ~(align - 1)
        rva = RDATA_RVA + rdata_cursor
        rdata_cursor += size
        return rva

    def put_rdata(rva: int, blob: bytes) -> None:
        offset = rva - RDATA_RVA
        rdata[offset:offset + len(blob)] = blob

    # create_delta: mov rax, rcx; add rax, rdx; ret
    create_rva, create_end = emit_text(b"\x48\x89\xC8\x48\x01\xD0\xC3")
    # release_delta: mov rax, rcx; sub rax, rdx; ret
    release_rva, release_end = emit_text(b"\x48\x89\xC8\x48\x29\xD0\xC3")
    # Optional chained cold fragment owned by create_delta.
    cold_rva, cold_end = emit_text(b"\x90\xC3")

    exports: list[tuple[str, int]] = []
    if include_create:
        exports.append(("create_delta", create_rva))
    if include_release:
        exports.append(("release_delta", release_rva))
    exports.sort(key=lambda item: item[0])

    export_rva = 0
    export_size = 0
    if exports:
        dll_name_rva = alloc_rdata(len(b"synthetic_delta.dll\0"), 2)
        put_rdata(dll_name_rva, b"synthetic_delta.dll\0")
        name_rvas = []
        for name, _ in exports:
            raw_name = name.encode("ascii") + b"\0"
            nrva = alloc_rdata(len(raw_name), 2)
            put_rdata(nrva, raw_name)
            name_rvas.append(nrva)

        n_exp = len(exports)
        eat_rva = alloc_rdata(4 * n_exp, 4)
        ent_rva = alloc_rdata(4 * n_exp, 4)
        eot_rva = alloc_rdata(2 * n_exp, 2)
        for idx, (_, fn_rva) in enumerate(exports):
            put_rdata(eat_rva + idx * 4, struct.pack("<I", fn_rva))
            put_rdata(ent_rva + idx * 4, struct.pack("<I", name_rvas[idx]))
            put_rdata(eot_rva + idx * 2, struct.pack("<H", idx))

        export_size = 40
        export_rva = alloc_rdata(export_size, 4)
        put_rdata(
            export_rva,
            struct.pack(
                "<IIHHIIIIIII",
                0, 0, 0, 0, dll_name_rva, 1, n_exp, n_exp, eat_rva, ent_rva, eot_rva,
            ),
        )

    pdata_rva = 0
    pdata_size = 0
    if include_pdata:
        unwind_rva = alloc_rdata(4, 4)
        put_rdata(unwind_rva, b"\x01\x00\x00\x00")
        entries = [
            (create_rva, create_end, unwind_rva),
            (release_rva, release_end, unwind_rva),
        ]
        if multi_range_create:
            # UNWIND_INFO version=1, flags=4 (UNW_FLAG_CHAININFO), count_of_codes=0,
            # followed by the primary RUNTIME_FUNCTION parent record.
            chained_rva = alloc_rdata(16, 4)
            put_rdata(
                chained_rva,
                b"\x21\x00\x00\x00" + struct.pack("<III", create_rva, create_end, unwind_rva),
            )
            entries.append((cold_rva, cold_end, chained_rva))
        entries.sort(key=lambda item: item[0])
        pdata_size = 12 * len(entries)
        pdata_rva = alloc_rdata(pdata_size, 4)
        for idx, (begin, end, info) in enumerate(entries):
            put_rdata(pdata_rva + idx * 12, struct.pack("<III", begin, end, info))

    buf = bytearray(RAW_OFFSET + SECTION_SIZE * 2)
    buf[0:2] = b"MZ"
    pe_off = 0x80
    struct.pack_into("<I", buf, 60, pe_off)
    buf[pe_off:pe_off + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", buf, pe_off + 4, machine, 2, 0, 0, 0, 0xF0, 0x2022)
    opt = pe_off + 24
    struct.pack_into(
        "<HBBIIIIIQIIHHHHHHIIIIHHQQQQII",
        buf,
        opt,
        magic, 14, 0, SECTION_SIZE, SECTION_SIZE, 0, create_rva, TEXT_RVA,
        IMAGE_BASE, SECTION_ALIGN, 0x200, 6, 0, 0, 0, 6, 0, 0,
        0x3000, RAW_OFFSET, 0, 3, 0x160,
        0x100000, 0x1000, 0x100000, 0x1000, 0, 16,
    )
    dd = opt + 112
    if export_size:
        struct.pack_into("<II", buf, dd + 0 * 8, export_rva, export_size)
    if pdata_size:
        struct.pack_into("<II", buf, dd + 3 * 8, pdata_rva, pdata_size)

    sections_off = opt + 0xF0
    sections = [
        (b".text\0\0\0", SECTION_SIZE, TEXT_RVA, SECTION_SIZE, RAW_OFFSET, 0x60000020),
        (b".rdata\0\0", SECTION_SIZE, RDATA_RVA, SECTION_SIZE, RAW_OFFSET + SECTION_SIZE, 0x40000040),
    ]
    for idx, (name, vsize, va, rsize, roff, chars) in enumerate(sections):
        struct.pack_into(
            "<8sIIIIIIHHI",
            buf,
            sections_off + idx * 40,
            name, vsize, va, rsize, roff, 0, 0, 0, 0, chars,
        )

    buf[RAW_OFFSET:RAW_OFFSET + SECTION_SIZE] = text
    buf[RAW_OFFSET + SECTION_SIZE:RAW_OFFSET + SECTION_SIZE * 2] = rdata
    return bytes(buf)


class FfxWorkingSetDeltaPositiveTests(unittest.TestCase):
    def test_generate_and_main_emit_delta_functions(self):
        raw = build_image()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "synthetic_delta.dll"
            out_cpp = Path(tmp) / "delta.cpp"
            cli_cpp = Path(tmp) / "delta_cli.cpp"
            image.write_bytes(raw)

            generated = ffx_working_set_delta.generate(image, False)
            self.assertEqual(ffx_working_set_delta.main([str(image), str(out_cpp)]), 0)
            written = out_cpp.read_text(encoding="ascii")
            self.assertEqual(generated, written)
            self.assertIn(
                'extern "C" __declspec(dllexport) word create_delta(word input, word counters)',
                written,
            )
            self.assertIn(
                'extern "C" __declspec(dllexport) word release_delta(word input, word counters)',
                written,
            )
            self.assertIn("rcx = input; rdx = counters;", written)
            self.assertIn("rax = static_cast<uint64_t>(static_cast<uint64_t>(rcx));", written)
            self.assertIn("a + b", written)
            self.assertIn("a - b", written)
            self.assertIn("return rax;", written)

            proc = subprocess.run(
                [sys.executable, "-B", str(TOOL), str(image), str(cli_cpp)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(cli_cpp.read_text(encoding="ascii"), generated)


class FfxWorkingSetDeltaRefusalTests(unittest.TestCase):
    def _run_cli_subprocess(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(TOOL), *args],
            capture_output=True, text=True, timeout=30,
        )

    def _run_main_in_process(self, argv: list[str]) -> tuple[int | str | None, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exc:
                ffx_working_set_delta.main(argv)
        return exc.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_package_sha256_mismatch_refused(self):
        raw = build_image()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "upscaler.dll"
            out_cpp = Path(tmp) / "out.cpp"
            image.write_bytes(raw)

            with self.assertRaises(ValueError) as ctx:
                ffx_working_set_delta.generate(image, True)
            self.assertIn("version-query profile", str(ctx.exception))

            code, _, err = self._run_main_in_process([str(image), str(out_cpp), "--package"])
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("version-query profile", err)
            self.assertFalse(out_cpp.exists())

    def test_missing_exports_rejected(self):
        for kwargs, missing_name in (
            ({"include_create": False}, "create_delta"),
            ({"include_release": False}, "release_delta"),
        ):
            with self.subTest(missing=missing_name):
                raw = build_image(**kwargs)
                with tempfile.TemporaryDirectory() as tmp:
                    image = Path(tmp) / "missing_export.dll"
                    out_cpp = Path(tmp) / "out.cpp"
                    image.write_bytes(raw)
                    with self.assertRaises(ValueError) as ctx:
                        ffx_working_set_delta.generate(image, False)
                    self.assertIn(missing_name, str(ctx.exception))

                    code, _, err = self._run_main_in_process([str(image), str(out_cpp)])
                    self.assertEqual(code, 1)
                    self.assertNotIn("Traceback", err)
                    self.assertIn(missing_name, err)
                    self.assertFalse(out_cpp.exists())

    def test_missing_and_multi_range_pdata_rejected(self):
        for kwargs in ({"include_pdata": False}, {"multi_range_create": True}):
            with self.subTest(kwargs=kwargs):
                raw = build_image(**kwargs)
                with tempfile.TemporaryDirectory() as tmp:
                    image = Path(tmp) / "bad_pdata.dll"
                    out_cpp = Path(tmp) / "out.cpp"
                    image.write_bytes(raw)
                    with self.assertRaises(ValueError):
                        ffx_working_set_delta.generate(image, False)

                    code, _, err = self._run_main_in_process([str(image), str(out_cpp)])
                    self.assertEqual(code, 1)
                    self.assertNotIn("Traceback", err)
                    self.assertTrue(err.strip())
                    self.assertFalse(out_cpp.exists())

    def test_non_amd64_pe_rejected(self):
        for kwargs in ({"machine": 0x014C}, {"magic": 0x10B}):
            with self.subTest(kwargs=kwargs):
                raw = build_image(**kwargs)
                with tempfile.TemporaryDirectory() as tmp:
                    image = Path(tmp) / "wrong_arch.dll"
                    image.write_bytes(raw)
                    with self.assertRaises(ValueError) as ctx:
                        ffx_working_set_delta.generate(image, False)
                    self.assertIn("AMD64 PE32+", str(ctx.exception))

    def test_non_cpp_suffix_and_matching_output_rejected(self):
        raw = build_image()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "input.dll"
            image.write_bytes(raw)
            bad_out = Path(tmp) / "out.txt"
            proc = self._run_cli_subprocess(str(image), str(bad_out))
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertFalse(bad_out.exists())

            same = Path(tmp) / "same.cpp"
            same.write_bytes(raw)
            proc = self._run_cli_subprocess(str(same), str(same))
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_missing_and_corrupt_inputs_exit_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.dll"
            out_cpp = Path(tmp) / "out.cpp"
            proc = self._run_cli_subprocess(str(missing), str(out_cpp))
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertTrue(proc.stderr.strip())
            self.assertFalse(out_cpp.exists())

            for label, payload in (
                ("empty.dll", b""),
                ("not_pe.dll", b"not a portable executable"),
                ("trunc_pe.dll", b"MZ" + b"\x00" * 58 + struct.pack("<I", 0x80)),
            ):
                with self.subTest(label=label):
                    corrupt = Path(tmp) / label
                    corrupt.write_bytes(payload)
                    code, _, err = self._run_main_in_process([str(corrupt), str(out_cpp)])
                    self.assertEqual(code, 1)
                    self.assertNotIn("Traceback", err)
                    self.assertTrue(err.strip())
                    self.assertFalse(out_cpp.exists())


if __name__ == "__main__":
    unittest.main()
