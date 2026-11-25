"""Synthetic AMD64 PE32+ unit and refusal suite for tools/bink_frame_aot.py."""

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

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "bink_frame_aot.py"
sys.path.insert(0, str(ROOT / "tools"))
import bink_frame_aot  # noqa: E402


IMAGE_BASE = 0x180000000
RAW_OFFSET = 0x200
SECTION_ALIGN = 0x1000
TEXT_RVA = 0x1000
RDATA_RVA = 0x2000
SECTION_SIZE = 0x1000


def _rel32(next_ip: int, target: int) -> bytes:
    return struct.pack("<i", target - next_ip)


def _rel8(next_ip: int, target: int) -> bytes:
    return struct.pack("<b", target - next_ip)


def build_image(
    *,
    include_export: bool = True,
    include_first_anchor: bool = True,
    include_failure_anchor: bool = True,
    failure_before_first: bool = False,
    init_target: int = 0x25B80,
    append_target: int = 0x25B20,
    group_target: int = 0x25A40,
    append_count: int = 5,
    indirect_last_call: bool = False,
) -> bytes:
    """Build a minimal synthetic AMD64 PE32+ image exercising bink_frame_aot."""
    text = bytearray(SECTION_SIZE)
    rdata = bytearray(SECTION_SIZE)
    text_cursor = 0
    rdata_cursor = 0

    def emit_text(blob: bytes) -> int:
        nonlocal text_cursor
        rva = TEXT_RVA + text_cursor
        text[text_cursor:text_cursor + len(blob)] = blob
        text_cursor += len(blob)
        return rva

    def alloc_rdata(size: int, align: int = 4) -> int:
        nonlocal rdata_cursor
        rdata_cursor = (rdata_cursor + align - 1) & ~(align - 1)
        rva = RDATA_RVA + rdata_cursor
        rdata_cursor += size
        return rva

    def put_rdata(rva: int, blob: bytes) -> None:
        offset = rva - RDATA_RVA
        rdata[offset:offset + len(blob)] = blob

    # Leaf helper 1 (aligned_pitch_format): mov eax, ecx; add eax, 1; ret
    version_rva = emit_text(b"\x8B\xC1\x83\xC0\x01\xC3")
    # Leaf helper 2 (group_size): mov eax, ecx; shl eax, 2; ret
    total_rva = emit_text(b"\x8B\xC1\xC1\xE0\x02\xC3")

    # Main function: BinkAllocateFrameBuffers
    entry_rva = TEXT_RVA + text_cursor
    fn = bytearray()
    # Prologue outside the lowered slice.
    fn += b"\x53"  # push rbx
    if failure_before_first and include_failure_anchor:
        fn += b"\x33\xC0"  # xor eax, eax
    if include_first_anchor:
        fn += b"\x48\x89\xD7"  # mov rdi, rdx

    # Reserve branch patches to the failure xor eax, eax offset.
    patch_sites = []

    def emit_jcc_to_failure(opcode: bytes) -> None:
        patch_sites.append(len(fn))
        fn.extend(opcode + b"\x00")

    fn += b"\x85\xC0"  # test eax, eax
    emit_jcc_to_failure(b"\x7E")  # jle failure
    fn += b"\x3B\x2F"  # cmp ebp, dword ptr [rdi]
    emit_jcc_to_failure(b"\x7C")  # jl failure
    fn += b"\x49\x83\xF8\x03"  # cmp r8, 3
    jge_ip = entry_rva + len(fn) + 2
    fn += b"\x7D" + _rel8(jge_ip, jge_ip + 1)  # jge after_nop
    fn += b"\x90"  # nop
    fn += b"\x0F\x43\xCB"  # cmovae ecx, ebx
    fn += b"\xF7\xD0"  # not eax

    targets = [init_target, version_rva] + [append_target] * append_count + [total_rva]
    for target in targets:
        call_ip = entry_rva + len(fn)
        fn += b"\xE8" + _rel32(call_ip + 5, target)

    if indirect_last_call:
        fn += b"\xFF\xD0"  # call rax
    else:
        call_ip = entry_rva + len(fn)
        fn += b"\xE8" + _rel32(call_ip + 5, group_target)

    failure_off = len(fn)
    if not failure_before_first and include_failure_anchor:
        fn += b"\x33\xC0"  # xor eax, eax

    for site in patch_sites:
        next_ip = entry_rva + site + 2
        fn[site + 1:site + 2] = _rel8(next_ip, entry_rva + failure_off)

    # Epilogue outside the lowered slice.
    fn += b"\x5B\xC3"  # pop rbx; ret
    emit_text(bytes(fn))
    entry_end = entry_rva + len(fn)

    # Export directory in .rdata.
    export_rva = 0
    export_size = 0
    if include_export:
        exp_dll_rva = alloc_rdata(len(b"synthetic_bink.dll\0"), 2)
        put_rdata(exp_dll_rva, b"synthetic_bink.dll\0")
        exp_name_rva = alloc_rdata(len(b"BinkAllocateFrameBuffers\0"), 2)
        put_rdata(exp_name_rva, b"BinkAllocateFrameBuffers\0")
        eat_rva = alloc_rdata(4, 4)
        put_rdata(eat_rva, struct.pack("<I", entry_rva))
        ent_rva = alloc_rdata(4, 4)
        put_rdata(ent_rva, struct.pack("<I", exp_name_rva))
        eot_rva = alloc_rdata(2, 2)
        put_rdata(eot_rva, struct.pack("<H", 0))
        export_size = 40
        export_rva = alloc_rdata(export_size, 4)
        put_rdata(
            export_rva,
            struct.pack("<IIHHIIIIIII", 0, 0, 0, 0, exp_dll_rva, 1, 1, 1, eat_rva, ent_rva, eot_rva),
        )

    # .pdata and UNWIND_INFO in .rdata.
    unwind_rva = alloc_rdata(4, 4)
    put_rdata(unwind_rva, b"\x01\x00\x00\x00")
    pdata_size = 12
    pdata_rva = alloc_rdata(pdata_size, 4)
    put_rdata(pdata_rva, struct.pack("<III", entry_rva, entry_end, unwind_rva))

    # Assemble PE32+ image.
    buf = bytearray(RAW_OFFSET + SECTION_SIZE * 2)
    buf[0:2] = b"MZ"
    pe_off = 0x80
    struct.pack_into("<I", buf, 60, pe_off)
    buf[pe_off:pe_off + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", buf, pe_off + 4, 0x8664, 2, 0, 0, 0, 0xF0, 0x2022)
    opt = pe_off + 24
    struct.pack_into(
        "<HBBIIIIIQIIHHHHHHIIIIHHQQQQII",
        buf,
        opt,
        0x20B, 14, 0, SECTION_SIZE, SECTION_SIZE, 0, entry_rva, TEXT_RVA,
        IMAGE_BASE, SECTION_ALIGN, 0x200, 6, 0, 0, 0, 6, 0, 0,
        0x3000, RAW_OFFSET, 0, 3, 0x160,
        0x100000, 0x1000, 0x100000, 0x1000, 0, 16,
    )
    dd = opt + 112
    if include_export:
        struct.pack_into("<II", buf, dd + 0 * 8, export_rva, export_size)
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


class BinkFrameAotPositiveTests(unittest.TestCase):
    def test_generate_and_cli_main_emit_expected_cpp(self):
        raw = build_image()
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "synthetic_bink.dll"
            out_cpp = Path(tmp) / "frame_allocator.cpp"
            image.write_bytes(raw)
            with patch.object(bink_frame_aot, "BINK_SHA256", digest):
                cpp = bink_frame_aot.generate(image)
                rc = bink_frame_aot.main([str(image), str(out_cpp)])
            self.assertEqual(rc, 0)
            written = out_cpp.read_text(encoding="ascii")
            self.assertEqual(cpp, written)
            self.assertIn("extern \"C\" word group_init(word, uint32_t);", written)
            self.assertIn("extern \"C\" word group_append(word, word, word);", written)
            self.assertIn("extern \"C\" word heap_group(word, word);", written)
            self.assertIn("static word aligned_pitch_format(word input) {", written)
            self.assertIn("static word group_size(word input) {", written)
            self.assertIn("extern \"C\" __declspec(dllexport) uint32_t frame_allocate(", written)
            self.assertIn("rax = group_init(rcx, uint32_t(rdx));", written)
            self.assertIn("rax = aligned_pitch_format(rcx);", written)
            self.assertEqual(written.count("rax = group_append(rcx, rdx, r8);"), 5)
            self.assertIn("rax = group_size(rcx);", written)
            self.assertIn("rax = heap_group(rcx, rdx);", written)
            self.assertIn("if (!carry) rcx = uint32_t(rbx);", written)
            self.assertIn("rax = uint32_t(~rax);", written)
            self.assertIn("if (int32_t(rax) <= 0) goto block_", written)
            self.assertIn("if (int32_t(rbp) < load<int32_t>(rdi)) goto block_", written)
            self.assertIn("if (int64_t(r8) >= 3) goto block_", written)
            self.assertIn("return uint32_t(rax);", written)


class BinkFrameAotNegativeTests(unittest.TestCase):
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
                bink_frame_aot.main(argv)
        return exc.exception.code, stdout.getvalue(), stderr.getvalue()

    def _generate_with_patched_sha(self, raw: bytes) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "synthetic.dll"
            image.write_bytes(raw)
            with patch.object(bink_frame_aot, "BINK_SHA256", digest):
                return bink_frame_aot.generate(image)

    def test_missing_file_and_sha256_mismatch_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.dll"
            out_cpp = Path(tmp) / "out.cpp"
            with self.assertRaises(OSError):
                bink_frame_aot.generate(missing)
            proc = self._run_cli_subprocess(str(missing), str(out_cpp))
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertTrue(proc.stderr.strip())
            self.assertFalse(out_cpp.exists())

            synthetic = Path(tmp) / "synthetic.dll"
            synthetic.write_bytes(build_image())
            with self.assertRaises(ValueError) as exc:
                bink_frame_aot.generate(synthetic)
            self.assertIn("version-query profile", str(exc.exception))
            proc_sha = self._run_cli_subprocess(str(synthetic), str(out_cpp))
            self.assertEqual(proc_sha.returncode, 1, proc_sha.stderr)
            self.assertNotIn("Traceback", proc_sha.stderr)
            self.assertIn("version-query profile", proc_sha.stderr)
            self.assertFalse(out_cpp.exists())

    def test_missing_export_raises_value_error(self):
        raw = build_image(include_export=False)
        with self.assertRaises(ValueError) as exc:
            self._generate_with_patched_sha(raw)
        self.assertIn("BinkAllocateFrameBuffers", str(exc.exception))

    def test_missing_anchors_and_invalid_bounds_raise_value_error(self):
        raw_no_first = build_image(include_first_anchor=False)
        with self.assertRaises(ValueError) as exc_first:
            self._generate_with_patched_sha(raw_no_first)
        self.assertNotIsInstance(exc_first.exception, StopIteration)
        self.assertIn("mov rdi, rdx", str(exc_first.exception))

        raw_no_fail = build_image(include_failure_anchor=False)
        with self.assertRaises(ValueError) as exc_fail:
            self._generate_with_patched_sha(raw_no_fail)
        self.assertNotIsInstance(exc_fail.exception, StopIteration)
        self.assertIn("xor eax, eax", str(exc_fail.exception))

        raw_inverted = build_image(failure_before_first=True)
        with self.assertRaises(ValueError) as exc_inv:
            self._generate_with_patched_sha(raw_inverted)
        self.assertIn("anchor order", str(exc_inv.exception))

    def test_wrong_call_count_and_indirect_call_rejected(self):
        raw_eight = build_image(append_count=4)
        with self.assertRaises(ValueError) as exc_cnt:
            self._generate_with_patched_sha(raw_eight)
        self.assertIn("call graph", str(exc_cnt.exception))

        raw_indirect = build_image(indirect_last_call=True)
        with self.assertRaises(ValueError) as exc_ind:
            self._generate_with_patched_sha(raw_indirect)
        self.assertIn("call graph", str(exc_ind.exception))

    def test_mismatched_helper_rvas_rejected(self):
        for kwargs in (
            {"init_target": 0x25B90},
            {"append_target": 0x25B30},
            {"group_target": 0x25A50},
        ):
            raw = build_image(**kwargs)
            with self.assertRaises(ValueError) as exc:
                self._generate_with_patched_sha(raw)
            self.assertIn("does not match the translated callee", str(exc.exception))

    def test_non_pe_and_invalid_output_cli_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "not_a_pe.dll"
            payload = b"not a valid portable executable"
            bad.write_bytes(payload)
            out_cpp = Path(tmp) / "out.cpp"

            # Unpatched SHA-256 -> ValueError -> exit 1
            proc_bad = self._run_cli_subprocess(str(bad), str(out_cpp))
            self.assertEqual(proc_bad.returncode, 1, proc_bad.stderr)
            self.assertNotIn("Traceback", proc_bad.stderr)
            self.assertFalse(out_cpp.exists())

            # Patched SHA-256 -> pefile.PEFormatError -> exit 1
            with patch.object(bink_frame_aot, "BINK_SHA256", hashlib.sha256(payload).hexdigest()):
                code, _, err = self._run_main_in_process([str(bad), str(out_cpp)])
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertTrue(err.strip())
            self.assertFalse(out_cpp.exists())

            # Non-.cpp output suffix -> parser.error (exit 2), no traceback
            image = Path(tmp) / "valid.dll"
            image.write_bytes(build_image())
            bad_out = Path(tmp) / "out.txt"
            proc_suffix = self._run_cli_subprocess(str(image), str(bad_out))
            self.assertEqual(proc_suffix.returncode, 2, proc_suffix.stderr)
            self.assertNotIn("Traceback", proc_suffix.stderr)
            self.assertFalse(bad_out.exists())

            # Output matches input path -> parser.error (exit 2), no traceback
            same = Path(tmp) / "same.cpp"
            same.write_bytes(b"keep-me")
            proc_same = self._run_cli_subprocess(str(same), str(same))
            self.assertEqual(proc_same.returncode, 2, proc_same.stderr)
            self.assertNotIn("Traceback", proc_same.stderr)
            self.assertEqual(same.read_bytes(), b"keep-me")


if __name__ == "__main__":
    unittest.main()
