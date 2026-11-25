"""Synthetic AMD64 PE32+ unit and refusal suite for tools/bink_descriptors_aot.py."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

import pefile

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "bink_descriptors_aot.py"
sys.path.insert(0, str(ROOT / "tools"))
import bink_descriptors_aot  # noqa: E402


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
    int3_count: int = 1,
    call_r9_count: int = 5,
    include_frame_planes: bool = True,
    include_frame_pdata: bool = True,
) -> bytes:
    """Build a minimal synthetic AMD64 PE32+ DLL for bink_descriptors_aot."""
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

    # group_init(group, capacity): mov eax, edx; ret
    init_rva, _ = emit_text(b"\x89\xD0\xC3")

    # group_append(group, destination, size): test rcx, rcx; jnz ok; <int3>*int3_count; ok: mov rax, rdx; ret
    if int3_count == 0:
        append_code = b"\x48\x85\xC9\x48\x89\xD0\xC3"
    else:
        append_code = (
            b"\x48\x85\xC9"
            + b"\x75"
            + struct.pack("<b", int3_count)
            + (b"\xCC" * int3_count)
            + b"\x48\x89\xD0\xC3"
        )
    append_rva, _ = emit_text(append_code)

    # frame_planes(frames, index, pitches, observe): <call r9>*call_r9_count; ret
    frame_code = (b"\x41\xFF\xD1" * call_r9_count) + b"\xC3"
    frame_rva, frame_end = emit_text(frame_code)

    exports = [
        ("group_append", append_rva),
        ("group_init", init_rva),
    ]
    if include_frame_planes:
        exports.append(("frame_planes", frame_rva))
    exports.sort(key=lambda item: item[0])

    dll_name_rva = alloc_rdata(len(b"synthetic_bink_desc.dll\0"), 2)
    put_rdata(dll_name_rva, b"synthetic_bink_desc.dll\0")
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
    if include_frame_pdata:
        unwind_rva = alloc_rdata(4, 4)
        put_rdata(unwind_rva, b"\x01\x00\x00\x00")
        pdata_size = 12
        pdata_rva = alloc_rdata(pdata_size, 4)
        put_rdata(pdata_rva, struct.pack("<III", frame_rva, frame_end, unwind_rva))

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
        magic, 14, 0, SECTION_SIZE, SECTION_SIZE, 0, init_rva, TEXT_RVA,
        IMAGE_BASE, SECTION_ALIGN, 0x200, 6, 0, 0, 0, 6, 0, 0,
        0x3000, RAW_OFFSET, 0, 3, 0x160,
        0x100000, 0x1000, 0x100000, 0x1000, 0, 16,
    )
    dd = opt + 112
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


class BinkDescriptorsAotPositiveTests(unittest.TestCase):
    def test_generate_without_and_with_frame_planes(self):
        raw = build_image()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "synthetic_bink_desc.dll"
            image.write_bytes(raw)

            cpp_no_planes = bink_descriptors_aot.generate(image, package=False, frame_planes=False)
            self.assertIn(
                'extern "C" __declspec(dllexport) word group_init(word group, uint32_t capacity)',
                cpp_no_planes,
            )
            self.assertIn(
                'extern "C" __declspec(dllexport) word group_append(word group, word destination, word size)',
                cpp_no_planes,
            )
            self.assertIn("return 0;", cpp_no_planes)
            self.assertIn("return 1;", cpp_no_planes)
            self.assertNotIn("frame_planes", cpp_no_planes)
            self.assertNotIn("append_frame", cpp_no_planes)

            cpp_with_planes = bink_descriptors_aot.generate(image, package=False, frame_planes=True)
            self.assertIn(
                'extern "C" __declspec(dllexport) word frame_planes(word frames, uint32_t index, '
                'const uint32_t* pitches, Observer observe)',
                cpp_with_planes,
            )
            self.assertIn(
                'extern "C" word append_frame(word frames, uint32_t index, const uint32_t* pitches, word group)',
                cpp_with_planes,
            )
            self.assertEqual(cpp_with_planes.count("reinterpret_cast<Observer>(r9)(rcx, uint32_t(rdx));"), 5)
            self.assertEqual(cpp_with_planes.count("rax = group_append(group, rcx, uint32_t(rdx));"), 5)

    def test_frame_body_and_cli_main(self):
        raw = build_image()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "synthetic_bink_desc.dll"
            out_cpp = Path(tmp) / "descriptors.cpp"
            cli_cpp = Path(tmp) / "descriptors_cli.cpp"
            image.write_bytes(raw)

            with pefile.PE(str(image), fast_load=True) as pe:
                observer_body = bink_descriptors_aot.frame_body(pe, package=False, bind_group=False)
                bound_body = bink_descriptors_aot.frame_body(pe, package=False, bind_group=True)
            self.assertEqual(observer_body.count("reinterpret_cast<Observer>(r9)(rcx, uint32_t(rdx));"), 5)
            self.assertEqual(bound_body.count("rax = group_append(group, rcx, uint32_t(rdx));"), 5)

            self.assertEqual(
                bink_descriptors_aot.main([str(image), str(out_cpp), "--frame-planes"]),
                0,
            )
            written = out_cpp.read_text(encoding="ascii")
            self.assertEqual(written, bink_descriptors_aot.generate(image, False, True))

            proc = subprocess.run(
                [sys.executable, "-B", str(TOOL), str(image), str(cli_cpp)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(cli_cpp.read_text(encoding="ascii"), bink_descriptors_aot.generate(image, False, False))


class BinkDescriptorsAotRefusalTests(unittest.TestCase):
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
                bink_descriptors_aot.main(argv)
        return exc.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_missing_and_duplicate_int3_rejected(self):
        for count in (0, 2):
            with self.subTest(int3_count=count):
                raw = build_image(int3_count=count)
                with tempfile.TemporaryDirectory() as tmp:
                    image = Path(tmp) / "bad_append.dll"
                    out_cpp = Path(tmp) / "out.cpp"
                    image.write_bytes(raw)
                    with self.assertRaises(ValueError) as ctx:
                        bink_descriptors_aot.generate(image, False, False)
                    self.assertIn("capacity breakpoint", str(ctx.exception))

                    code, _, err = self._run_main_in_process([str(image), str(out_cpp)])
                    self.assertEqual(code, 1)
                    self.assertNotIn("Traceback", err)
                    self.assertIn("capacity breakpoint", err)
                    self.assertFalse(out_cpp.exists())

    def test_wrong_call_r9_count_in_frame_planes_rejected(self):
        for count in (0, 4, 6):
            with self.subTest(call_r9_count=count):
                raw = build_image(call_r9_count=count)
                with tempfile.TemporaryDirectory() as tmp:
                    image = Path(tmp) / "bad_frames.dll"
                    out_cpp = Path(tmp) / "out.cpp"
                    image.write_bytes(raw)
                    with self.assertRaises(ValueError) as ctx:
                        bink_descriptors_aot.generate(image, False, True)
                    self.assertIn("five frame plane request sites", str(ctx.exception))

                    code, _, err = self._run_main_in_process([str(image), str(out_cpp), "--frame-planes"])
                    self.assertEqual(code, 1)
                    self.assertNotIn("Traceback", err)
                    self.assertIn("five frame plane request sites", err)
                    self.assertFalse(out_cpp.exists())

    def test_package_sha256_mismatch_against_bink_sha256_rejected(self):
        raw = build_image()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "bink.dll"
            out_cpp = Path(tmp) / "out.cpp"
            image.write_bytes(raw)
            with self.assertRaises(ValueError) as ctx:
                bink_descriptors_aot.generate(image, package=True, frame_planes=False)
            self.assertIn("version-query profile", str(ctx.exception))

            code, _, err = self._run_main_in_process([str(image), str(out_cpp), "--package"])
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("version-query profile", err)
            self.assertFalse(out_cpp.exists())

    def test_missing_frame_planes_export_or_pdata_rejected(self):
        for kwargs in ({"include_frame_planes": False}, {"include_frame_pdata": False}):
            with self.subTest(kwargs=kwargs):
                raw = build_image(**kwargs)
                with tempfile.TemporaryDirectory() as tmp:
                    image = Path(tmp) / "bad_frame_meta.dll"
                    image.write_bytes(raw)
                    with self.assertRaises(ValueError):
                        bink_descriptors_aot.generate(image, package=False, frame_planes=True)

    def test_non_amd64_pe_rejected(self):
        for kwargs in ({"machine": 0x014C}, {"magic": 0x10B}):
            with self.subTest(kwargs=kwargs):
                raw = build_image(**kwargs)
                with tempfile.TemporaryDirectory() as tmp:
                    image = Path(tmp) / "wrong_arch.dll"
                    image.write_bytes(raw)
                    with self.assertRaises(ValueError) as ctx:
                        bink_descriptors_aot.generate(image, package=False, frame_planes=False)
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
