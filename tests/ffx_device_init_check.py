"""Synthetic AMD64 PE32+ unit and refusal suite for tools/ffx_device_init.py."""

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
from uuid import UUID

import pefile

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "ffx_device_init.py"
sys.path.insert(0, str(ROOT / "tools"))
import ffx_device_init  # noqa: E402


IMAGE_BASE = 0x180000000
RAW_OFFSET = 0x200
SECTION_ALIGN = 0x1000
TEXT_RVA = 0x2000
TEXT_SIZE = 0xB000
RDATA_RVA = 0x48000
RDATA_SIZE = 0x1000
EXPECTED_HEAP_UUID = UUID("8efb471d-616c-4f49-90f7-127bb763fa51")


def build_synthetic_upscaler(
    *,
    slice_bytes: bytes | None = None,
    pdata_begin: int = 0x2720,
    pdata_end: int = 0x2B00,
    heap_guid: bytes = EXPECTED_HEAP_UUID.bytes_le,
) -> bytes:
    """Build a minimal synthetic AMD64 PE32+ image matching ffx_device_init slices."""
    text = bytearray(b"\x90" * TEXT_SIZE)
    rdata = bytearray(RDATA_SIZE)

    if slice_bytes is None:
        # [0x27f8, 0x2938): call [rax+8]; call [rax+0x70]; lea rdx, [rip+heap_iid]
        off = 0x27F8 - TEXT_RVA
        insns = (
            b"\xFF\x50\x08"
            b"\xFF\x50\x70"
            + b"\x48\x8D\x15"
            + struct.pack("<i", 0x482E0 - (0x27F8 + 6 + 7))
        )
        text[off:off + len(insns)] = insns
    else:
        off = 0x27F8 - TEXT_RVA
        if len(slice_bytes) > 0x2938 - 0x27F8:
            raise ValueError("slice_bytes exceeds [0x27f8, 0x2938)")
        text[off:off + len(slice_bytes)] = slice_bytes

    # .pdata RUNTIME_FUNCTION entry at RDATA_RVA and UNWIND_INFO at RDATA_RVA + 0x10
    unwind_rva = RDATA_RVA + 0x10
    rdata[0x10:0x14] = b"\x01\x00\x00\x00"
    pdata_rva = RDATA_RVA
    pdata_size = 12
    struct.pack_into("<III", rdata, 0, pdata_begin, pdata_end, unwind_rva)

    # Heap IID at 0x482e0
    guid_off = 0x482E0 - RDATA_RVA
    rdata[guid_off:guid_off + 16] = heap_guid

    buf = bytearray(RAW_OFFSET + TEXT_SIZE + RDATA_SIZE)
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
        0x20B, 14, 0, TEXT_SIZE, RDATA_SIZE, 0, 0x2720, TEXT_RVA,
        IMAGE_BASE, SECTION_ALIGN, 0x200, 6, 0, 0, 0, 6, 0, 0,
        RDATA_RVA + RDATA_SIZE, RAW_OFFSET, 0, 3, 0x160,
        0x100000, 0x1000, 0x100000, 0x1000, 0, 16,
    )
    dd = opt + 112
    struct.pack_into("<II", buf, dd + 3 * 8, pdata_rva, pdata_size)

    sections_off = opt + 0xF0
    sections = [
        (b".text\0\0\0", TEXT_SIZE, TEXT_RVA, TEXT_SIZE, RAW_OFFSET, 0x60000020),
        (b".rdata\0\0", RDATA_SIZE, RDATA_RVA, RDATA_SIZE, RAW_OFFSET + TEXT_SIZE, 0x40000040),
    ]
    for idx, (name, vsize, va, rsize, roff, chars) in enumerate(sections):
        struct.pack_into(
            "<8sIIIIIIHHI",
            buf,
            sections_off + idx * 40,
            name, vsize, va, rsize, roff, 0, 0, 0, 0, chars,
        )

    buf[RAW_OFFSET:RAW_OFFSET + TEXT_SIZE] = text
    buf[RAW_OFFSET + TEXT_SIZE:RAW_OFFSET + TEXT_SIZE + RDATA_SIZE] = rdata
    return bytes(buf)


class FfxDeviceInitPositiveTests(unittest.TestCase):
    def test_generate_and_main_with_synthetic_upscaler(self):
        raw = build_synthetic_upscaler()
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "synthetic_upscaler.dll"
            out_cpp = Path(tmp) / "device_init.cpp"
            image.write_bytes(raw)
            with (
                patch.object(ffx_device_init, "UPSCALER", digest),
                patch.object(ffx_device_init, "generate_recurrent", return_value="// recurrent\n"),
                patch.object(ffx_device_init, "generate_loop", return_value="// loop\n"),
                patch.object(ffx_device_init, "dispatch_slices", return_value="// dispatch\n"),
            ):
                generated = ffx_device_init.generate(image)
                rc = ffx_device_init.main([str(image), str(out_cpp)])
            self.assertEqual(rc, 0)
            written = out_cpp.read_text(encoding="ascii")
            self.assertEqual(generated, written)
            self.assertIn('#include "ffx_device_init.h"', written)
            expected_bytes = ", ".join(map(str, EXPECTED_HEAP_UUID.bytes_le))
            self.assertIn(f"static const unsigned char heap_iid[] = {{{expected_bytes}}};", written)
            self.assertIn("void run_device_slice(FfxDeviceProbe& probe) {", written)
            self.assertIn("rax = probe.retain(rcx);", written)
            self.assertIn("rax = probe.create(rcx, rdx, r8, r9);", written)
            self.assertIn("reinterpret_cast<word>(heap_iid)", written)
            self.assertIn(
                "uint32_t run_backend_slots(uint8_t* scratch, FfxSlotOutput& provider, const uint32_t* sizes) {",
                written,
            )
            self.assertIn("r14 = r9; goto block_27a7;", written)
            self.assertIn("goto block_2938;", written)
            self.assertIn("// recurrent\n// loop\n// dispatch\n", written)

    def test_generate_with_mocked_checked_image(self):
        raw = build_synthetic_upscaler()
        pe = pefile.PE(data=raw, fast_load=True)
        with (
            patch.object(ffx_device_init, "checked_image", return_value=pe),
            patch.object(ffx_device_init, "generate_recurrent", return_value=""),
            patch.object(ffx_device_init, "generate_loop", return_value=""),
            patch.object(ffx_device_init, "dispatch_slices", return_value=""),
        ):
            cpp = ffx_device_init.generate(Path("ignored.dll"))
        self.assertIn("rax = probe.retain(rcx);", cpp)
        self.assertIn("rax = probe.create(rcx, rdx, r8, r9);", cpp)


class FfxDeviceInitNegativeTests(unittest.TestCase):
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
                ffx_device_init.main(argv)
        return exc.exception.code, stdout.getvalue(), stderr.getvalue()

    def _generate_with_bytes(self, raw: bytes) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "upscaler.dll"
            image.write_bytes(raw)
            with (
                patch.object(ffx_device_init, "UPSCALER", digest),
                patch.object(ffx_device_init, "generate_recurrent", return_value=""),
                patch.object(ffx_device_init, "generate_loop", return_value=""),
                patch.object(ffx_device_init, "dispatch_slices", return_value=""),
            ):
                return ffx_device_init.generate(image)

    def test_missing_backend_callback_pdata_range_raises_value_error(self):
        raw = build_synthetic_upscaler(pdata_begin=0x2800, pdata_end=0x2B00)
        with self.assertRaises(ValueError) as exc:
            self._generate_with_bytes(raw)
        self.assertIn("function interval", str(exc.exception))

        raw_short = build_synthetic_upscaler(pdata_begin=0x2720, pdata_end=0x2900)
        with self.assertRaises(ValueError) as exc_short:
            self._generate_with_bytes(raw_short)
        self.assertIn("backend callback", str(exc_short.exception))

    def test_non_rax_call_base_raises_value_error(self):
        # call qword ptr [rcx+8]
        raw = build_synthetic_upscaler(slice_bytes=b"\xFF\x51\x08")
        with self.assertRaises(ValueError) as exc:
            self._generate_with_bytes(raw)
        self.assertIn("unexpected device call at 0x27f8", str(exc.exception))

    def test_unexpected_rax_displacement_raises_value_error_not_key_error(self):
        # call qword ptr [rax+0x10]
        raw = build_synthetic_upscaler(slice_bytes=b"\xFF\x50\x10")
        with self.assertRaises(ValueError) as exc:
            self._generate_with_bytes(raw)
        self.assertNotIsInstance(exc.exception, KeyError)
        self.assertIn("0x10", str(exc.exception))
        self.assertIn("0x27f8", str(exc.exception))

    def test_mismatched_heap_uuid_raises_value_error(self):
        wrong_guid = b"\x00" * 16
        raw = build_synthetic_upscaler(heap_guid=wrong_guid)
        with self.assertRaises(ValueError) as exc:
            self._generate_with_bytes(raw)
        self.assertIn("heap interface", str(exc.exception))

    def test_missing_and_non_pe_inputs_exit_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.dll"
            out_cpp = Path(tmp) / "out.cpp"
            proc = self._run_cli_subprocess(str(missing), str(out_cpp))
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertTrue(proc.stderr.strip())
            self.assertFalse(out_cpp.exists())

            bad = Path(tmp) / "not_pe.dll"
            payload = b"not a portable executable"
            bad.write_bytes(payload)

            proc_bad = self._run_cli_subprocess(str(bad), str(out_cpp))
            self.assertEqual(proc_bad.returncode, 1, proc_bad.stderr)
            self.assertNotIn("Traceback", proc_bad.stderr)
            self.assertFalse(out_cpp.exists())

            with patch.object(ffx_device_init, "UPSCALER", hashlib.sha256(payload).hexdigest()):
                code, _, err = self._run_main_in_process([str(bad), str(out_cpp)])
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertTrue(err.strip())
            self.assertFalse(out_cpp.exists())

    def test_struct_error_exits_cleanly(self):
        raw = build_synthetic_upscaler()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "upscaler.dll"
            out_cpp = Path(tmp) / "out.cpp"
            image.write_bytes(raw)
            with patch.object(ffx_device_init, "generate", side_effect=struct.error("bad unpack")):
                code, _, err = self._run_main_in_process([str(image), str(out_cpp)])
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("bad unpack", err)
            self.assertFalse(out_cpp.exists())

    def test_non_cpp_suffix_and_same_file_exit_1_cleanly(self):
        raw = build_synthetic_upscaler()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "upscaler.dll"
            image.write_bytes(raw)
            bad_out = Path(tmp) / "out.txt"
            proc_suffix = self._run_cli_subprocess(str(image), str(bad_out))
            self.assertEqual(proc_suffix.returncode, 1, proc_suffix.stderr)
            self.assertNotIn("Traceback", proc_suffix.stderr)
            self.assertTrue(proc_suffix.stderr.strip())
            self.assertFalse(bad_out.exists())

            same = Path(tmp) / "same.cpp"
            same.write_bytes(raw)
            proc_same = self._run_cli_subprocess(str(same), str(same))
            self.assertEqual(proc_same.returncode, 1, proc_same.stderr)
            self.assertNotIn("Traceback", proc_same.stderr)
            self.assertEqual(same.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
