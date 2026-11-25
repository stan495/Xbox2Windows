"""Synthetic AMD64 PE32+ unit and refusal suite for tools/ffx_create_context.py."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from iced_x86 import Decoder

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "ffx_create_context.py"
sys.path.insert(0, str(ROOT / "tools"))
import ffx_create_context  # noqa: E402


IMAGE_BASE = 0x180000000
RAW_OFFSET = 0x200
SECTION_ALIGN = 0x1000
TEXT_RVA = 0x1000
RDATA_RVA = 0x2000
SECTION_SIZE = 0x1000


def _rel32(next_ip: int, target: int) -> bytes:
    return struct.pack("<i", target - next_ip)


def build_synthetic_loader() -> bytes:
    """Build a minimal synthetic AMD64 PE32+ image matching ffx_create_context slices."""
    text = bytearray(b"\x90" * SECTION_SIZE)
    rdata = bytearray(SECTION_SIZE)

    # Slice 1: [0x117f, 0x11a2) -> test rcx, rcx; jz 0x1273; NOPs
    off_117f = 0x117f - TEXT_RVA
    text[off_117f:off_117f + 3] = b"\x48\x85\xC9"
    text[off_117f + 3:off_117f + 9] = b"\x0F\x84" + _rel32(0x117f + 9, 0x1273)

    # Slice 2: [0x11a2, 0x125e) -> call sites at 0x11a7, 0x121f, 0x1232, 0x123c, xor eax, eax at 0x1241
    for call_rva in (0x11A7, 0x121F, 0x1232, 0x123C):
        off = call_rva - TEXT_RVA
        text[off:off + 5] = b"\xE8\x00\x00\x00\x00"
    off_1241 = 0x1241 - TEXT_RVA
    text[off_1241:off_1241 + 2] = b"\x33\xC0"

    # Slice 3: [0x1273, 0x127a) -> NOPs up to 0x127a

    rdata_cursor = 0

    def alloc_rdata(size: int, align: int = 4) -> int:
        nonlocal rdata_cursor
        rdata_cursor = (rdata_cursor + align - 1) & ~(align - 1)
        rva = RDATA_RVA + rdata_cursor
        rdata_cursor += size
        return rva

    def put_rdata(rva: int, blob: bytes) -> None:
        offset = rva - RDATA_RVA
        rdata[offset:offset + len(blob)] = blob

    # Export directory for ffxCreateContext at 0x117f
    dll_name_rva = alloc_rdata(len(b"synthetic_loader.dll\0"), 2)
    put_rdata(dll_name_rva, b"synthetic_loader.dll\0")
    fn_name_rva = alloc_rdata(len(b"ffxCreateContext\0"), 2)
    put_rdata(fn_name_rva, b"ffxCreateContext\0")
    eat_rva = alloc_rdata(4, 4)
    ent_rva = alloc_rdata(4, 4)
    eot_rva = alloc_rdata(2, 2)
    put_rdata(eat_rva, struct.pack("<I", 0x117F))
    put_rdata(ent_rva, struct.pack("<I", fn_name_rva))
    put_rdata(eot_rva, struct.pack("<H", 0))
    export_size = 40
    export_rva = alloc_rdata(export_size, 4)
    put_rdata(
        export_rva,
        struct.pack("<IIHHIIIIIII", 0, 0, 0, 0, dll_name_rva, 1, 1, 1, eat_rva, ent_rva, eot_rva),
    )

    # .pdata RUNTIME_FUNCTION covering [0x117f, 0x127a)
    unwind_rva = alloc_rdata(4, 4)
    put_rdata(unwind_rva, b"\x01\x00\x00\x00")
    pdata_size = 12
    pdata_rva = alloc_rdata(pdata_size, 4)
    put_rdata(pdata_rva, struct.pack("<III", 0x117F, 0x127A, unwind_rva))

    buf = bytearray(RAW_OFFSET + SECTION_SIZE * 2)
    buf[0:2] = b"MZ"
    pe_off = 0x80
    struct.pack_into("<I", buf, 60, pe_off)
    buf[pe_off:pe_off + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", buf, pe_off + 4, 0x8664, 2, 0, 0, 0, 0xF0, 0x2022)
    opt = pe_off + 24
    struct.pack_into("<HBBIIIIIQIIHHHHHHIIIIHHQQQQII", buf, opt,
                     0x20B, 14, 0, SECTION_SIZE, SECTION_SIZE, 0, 0x117F, TEXT_RVA,
                     IMAGE_BASE, SECTION_ALIGN, 0x200, 6, 0, 0, 0, 6, 0, 0,
                     0x3000, RAW_OFFSET, 0, 3, 0x160,
                     0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    dd = opt + 112
    struct.pack_into("<II", buf, dd + 0 * 8, export_rva, export_size)
    struct.pack_into("<II", buf, dd + 3 * 8, pdata_rva, pdata_size)

    sections_off = opt + 0xF0
    sections = [
        (b".text\0\0\0", SECTION_SIZE, TEXT_RVA, SECTION_SIZE, RAW_OFFSET, 0x60000020),
        (b".rdata\0\0", SECTION_SIZE, RDATA_RVA, SECTION_SIZE, RAW_OFFSET + SECTION_SIZE, 0x40000040),
    ]
    for idx, (name, vsize, va, rsize, roff, chars) in enumerate(sections):
        struct.pack_into("<8sIIIIIIHHI", buf, sections_off + idx * 40,
                         name, vsize, va, rsize, roff, 0, 0, 0, 0, chars)

    buf[RAW_OFFSET:RAW_OFFSET + SECTION_SIZE] = text
    buf[RAW_OFFSET + SECTION_SIZE:RAW_OFFSET + SECTION_SIZE * 2] = rdata
    return bytes(buf)


class FfxCreateContextPositiveTests(unittest.TestCase):
    def test_create_source_emits_guards_create_and_oom_rollback(self):
        # Minimal decoded body: call at 0x1000, call at 0x1005, call at 0x100a, xor eax, eax at 0x100f
        code = b"\xE8\x00\x00\x00\x00" * 3 + b"\x33\xC0"
        body = list(Decoder(64, code, ip=0x1000))
        calls = {0x1000: "resolve", 0x1005: "create", 0x100A: "allocate"}
        cpp = ffx_create_context.create_source(
            body,
            {},
            calls,
            {0x1011: "return static_cast<uint32_t>(rax);"},
            ["rcx = reinterpret_cast<word>(output); rdx = reinterpret_cast<word>(desc);",
             "r8 = reinterpret_cast<word>(allocator);"],
        )
        self.assertIn("if (!host || (allocator && (!allocator->allocate || !allocator->deallocate))", cpp)
        self.assertIn("|| (!allocator && !host->zero_alloc)) return 6;", cpp)
        self.assertIn("host->table->create(&inner, reinterpret_cast<const void*>(rdx),", cpp)
        self.assertIn("store<void*>(provider_slot, inner);", cpp)
        self.assertIn("host->table->destroy(&inner, allocator);", cpp)
        self.assertIn("return 5;", cpp)

    def test_generate_and_main_with_synthetic_loader(self):
        raw = build_synthetic_loader()
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            loader = Path(tmp) / "synthetic_loader.dll"
            out_cpp = Path(tmp) / "create_context.cpp"
            loader.write_bytes(raw)
            with patch.object(ffx_create_context, "LOADER", digest):
                generated = ffx_create_context.generate(loader)
                ffx_create_context.main([str(loader), str(out_cpp)])
            written = out_cpp.read_text(encoding="ascii")
            self.assertEqual(generated, written)
            self.assertIn('extern "C" __declspec(dllexport) uint32_t create_context', written)
            self.assertIn("host->table->create(&inner", written)
            self.assertIn("host->table->destroy(&inner, allocator);", written)
            self.assertIn("return 5;", written)
            self.assertIn("return 6;", written)


class FfxCreateContextRefusalTests(unittest.TestCase):
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
                ffx_create_context.main(argv)
        return exc.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_missing_loader_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing_loader.dll"
            out_cpp = Path(tmp) / "out.cpp"
            proc = self._run_cli_subprocess(str(missing), str(out_cpp))
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertTrue(proc.stderr.strip())
            self.assertFalse(out_cpp.exists())

    def test_non_pe_input_exits_cleanly(self):
        payload = b"not a portable executable"
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "not_pe.dll"
            out_cpp = Path(tmp) / "out.cpp"
            bad.write_bytes(payload)

            # Unpatched LOADER -> SHA-256 mismatch -> clean exit 1
            proc = self._run_cli_subprocess(str(bad), str(out_cpp))
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertFalse(out_cpp.exists())

            # Matching LOADER digest -> pefile.PEFormatError -> clean exit 1
            with patch.object(ffx_create_context, "LOADER", hashlib.sha256(payload).hexdigest()):
                code, _, err = self._run_main_in_process([str(bad), str(out_cpp)])
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertTrue(err.strip())
            self.assertFalse(out_cpp.exists())

    def test_sha256_mismatch_exits_cleanly(self):
        raw = build_synthetic_loader()
        with tempfile.TemporaryDirectory() as tmp:
            loader = Path(tmp) / "loader.dll"
            out_cpp = Path(tmp) / "out.cpp"
            loader.write_bytes(raw)
            proc = self._run_cli_subprocess(str(loader), str(out_cpp))
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertIn("version-query profile", proc.stderr)
            self.assertFalse(out_cpp.exists())

    def test_non_cpp_suffix_and_matching_output_rejected(self):
        raw = build_synthetic_loader()
        with tempfile.TemporaryDirectory() as tmp:
            loader = Path(tmp) / "loader.dll"
            loader.write_bytes(raw)
            bad_out = Path(tmp) / "out.txt"
            proc = self._run_cli_subprocess(str(loader), str(bad_out))
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertFalse(bad_out.exists())

            same = Path(tmp) / "same.cpp"
            same.write_bytes(raw)
            proc = self._run_cli_subprocess(str(same), str(same))
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
