"""Synthetic AMD64 PE32+ unit and refusal suite for tools/bink_heap_aot.py."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from iced_x86 import Decoder
import pefile

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "bink_heap_aot.py"
sys.path.insert(0, str(ROOT / "tools"))
import bink_heap_aot  # noqa: E402


IMAGE_BASE = 0x180000000
RAW_OFFSET = 0x200
SECTION_ALIGN = 0x1000
TEXT_RVA = 0x1000
RDATA_RVA = 0x2000
DATA_RVA = 0x3000
SECTION_SIZE = 0x1000


def _rel32(next_ip: int, target: int) -> bytes:
    return struct.pack("<i", target - next_ip)


def _rel8(next_ip: int, target: int) -> bytes:
    return struct.pack("<b", target - next_ip)


def build_image(
    *,
    machine: int = 0x8664,
    magic: int = 0x20B,
    module_name: bytes = b"cpu_bink_heap_provider.dll",
    include_imports: bool = True,
    data_chars: int = 0xC0000040,
    data_vsize: int = SECTION_SIZE,
    data_rsize: int = SECTION_SIZE,
    alloc_slot_offset: int = 0x00,
    free_slot_offset: int = 0x08,
    alloc_slot_init: bytes = b"",
    text_vsize: int = SECTION_SIZE,
    alloc_import_name: bytes = b"XMemAlloc",
    free_import_name: bytes = b"XMemFree",
    alloc_call_count: int = 1,
    free_call_count: int = 1,
    thunk_override: dict[str, int] | None = None,
    extra_pdata: list[tuple[int, int]] | None = None,
    include_group: bool = True,
):
    """Build a minimal synthetic AMD64 PE32+ image exercising bink_heap_aot."""
    text = bytearray(SECTION_SIZE)
    rdata = bytearray(SECTION_SIZE)
    data = bytearray(SECTION_SIZE)

    text_cursor = 0
    rdata_cursor = 0

    def emit_text(blob: bytes, align: int = 1) -> int:
        nonlocal text_cursor
        text_cursor = (text_cursor + align - 1) & ~(align - 1)
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

    alloc_slot_rva = DATA_RVA + alloc_slot_offset
    free_slot_rva = DATA_RVA + free_slot_offset
    if alloc_slot_init:
        data[alloc_slot_offset:alloc_slot_offset + len(alloc_slot_init)] = alloc_slot_init

    # Build import structures in .rdata.
    import_rva = 0
    import_size = 0
    iat_alloc_rva = RDATA_RVA + 0x100
    iat_free_rva = RDATA_RVA + 0x108
    if include_imports:
        dll_rva = alloc_rdata(len(module_name) + 1, 2)
        put_rdata(dll_rva, module_name + b"\0")
        hint_alloc_rva = alloc_rdata(2 + len(alloc_import_name) + 1, 2)
        put_rdata(hint_alloc_rva, b"\0\0" + alloc_import_name + b"\0")
        hint_free_rva = alloc_rdata(2 + len(free_import_name) + 1, 2)
        put_rdata(hint_free_rva, b"\0\0" + free_import_name + b"\0")

        ilt_rva = alloc_rdata(24, 8)
        put_rdata(ilt_rva, struct.pack("<QQQ", hint_alloc_rva, hint_free_rva, 0))
        iat_rva = alloc_rdata(24, 8)
        put_rdata(iat_rva, struct.pack("<QQQ", hint_alloc_rva, hint_free_rva, 0))
        iat_alloc_rva = iat_rva
        iat_free_rva = iat_rva + 8

        import_size = 40
        import_rva = alloc_rdata(import_size, 4)
        put_rdata(import_rva, struct.pack("<5I", ilt_rva, 0, 0, dll_rva, iat_rva))

    # Build IAT thunks in .text (jmp qword ptr [rip + iat_slot]).
    alloc_thunk_rva = TEXT_RVA + text_cursor
    emit_text(b"\xFF\x25" + _rel32(alloc_thunk_rva + 6, iat_alloc_rva))
    free_thunk_rva = TEXT_RVA + text_cursor
    emit_text(b"\xFF\x25" + _rel32(free_thunk_rva + 6, iat_free_rva))

    overrides = thunk_override or {}
    alloc_target = overrides.get("alloc", alloc_thunk_rva)
    free_target = overrides.get("free", free_thunk_rva)

    # BinkSetMemory: store RCX and RDX into two callback slots, then ret.
    setter_rva = TEXT_RVA + text_cursor
    setter_code = bytearray()
    setter_code += b"\x48\x89\x0D" + _rel32(setter_rva + 7, alloc_slot_rva)
    setter_code += b"\x48\x89\x15" + _rel32(setter_rva + 14, free_slot_rva)
    setter_code += b"\xC3"
    emit_text(bytes(setter_code))

    # heap_request (non-leaf with .pdata): checks RCX, optional custom slot, or XMemAlloc.
    alloc_rva = TEXT_RVA + text_cursor
    # Layout offsets inside heap_request:
    # 0..3: test rcx, rcx
    # 3..5: jz invalid
    # 5..12: mov rax, [rip + alloc_slot_rva]
    # 12..15: test rax, rax
    # 15..17: jz fallback
    # 17..19: call rax
    # 19..20: ret
    # 20..25: fallback: mov edx, 0x05e50000
    # 25..30: call alloc_target (repeated alloc_call_count times)
    # ...: ret
    # invalid: xor eax, eax; ret
    fallback_off = 20
    calls_size = 5 * alloc_call_count
    invalid_off = fallback_off + 5 + calls_size + 1
    req = bytearray()
    req += b"\x48\x85\xC9"
    req += b"\x74" + _rel8(alloc_rva + 5, alloc_rva + invalid_off)
    req += b"\x48\x8B\x05" + _rel32(alloc_rva + 12, alloc_slot_rva)
    req += b"\x48\x85\xC0"
    req += b"\x74" + _rel8(alloc_rva + 17, alloc_rva + fallback_off)
    req += b"\xFF\xD0"
    req += b"\xC3"
    req += b"\xBA\x00\x00\xE5\x05"
    for _ in range(alloc_call_count):
        call_ip = alloc_rva + len(req)
        req += b"\xE8" + _rel32(call_ip + 5, alloc_target)
    req += b"\xC3"
    req += b"\x33\xC0\xC3"
    emit_text(bytes(req))
    alloc_end = alloc_rva + len(req)

    # heap_release (leaf): test rcx, rcx; jz released; mov edx, 0x05e50000; jmp/call free_target; ret
    free_rva = TEXT_RVA + text_cursor
    rel = bytearray()
    if free_call_count == 0:
        rel += b"\x48\x85\xC9\xC3"
    else:
        rel += b"\x48\x85\xC9"
        # released_off = 3 + 2 + 5 + 5 * free_call_count
        released_off = 10 + 5 * free_call_count
        rel += b"\x74" + _rel8(free_rva + 5, free_rva + released_off)
        rel += b"\xBA\x00\x00\xE5\x05"
        for i in range(free_call_count):
            op = b"\xE9" if i == free_call_count - 1 else b"\xE8"
            jmp_ip = free_rva + len(rel)
            rel += op + _rel32(jmp_ip + 5, free_target)
        rel += b"\xC3"
    emit_text(bytes(rel))

    # heap_group (non-leaf with .pdata): tail-jumps to heap_request when rcx==0, else calls heap_request.
    group_rva = TEXT_RVA + text_cursor
    grp = bytearray()
    # 0..3: test rcx, rcx
    # 3..5: jnz grouped (offset 13)
    # 5..8: mov rcx, rdx
    # 8..13: jmp heap_request
    # 13..14: push rdi
    # 14..17: mov rdi, rcx
    # 17..20: mov rcx, rdx
    # 20..25: call heap_request
    # 25..26: pop rdi
    # 26..27: ret
    grp += b"\x48\x85\xC9"
    grp += b"\x75" + _rel8(group_rva + 5, group_rva + 13)
    grp += b"\x48\x89\xD1"
    grp += b"\xE9" + _rel32(group_rva + 13, alloc_rva)
    grp += b"\x57\x48\x89\xCF\x48\x89\xD1"
    grp += b"\xE8" + _rel32(group_rva + 25, alloc_rva)
    grp += b"\x5F\xC3"
    emit_text(bytes(grp))
    group_end = group_rva + len(grp)

    # Exports in .rdata (sorted by ASCII name).
    exports_list = [
        ("BinkSetMemory", setter_rva),
        ("heap_release", free_rva),
        ("heap_request", alloc_rva),
    ]
    if include_group:
        exports_list.insert(1, ("heap_group", group_rva))
    exports_list.sort(key=lambda item: item[0])

    exp_dll_rva = alloc_rdata(len(b"synthetic_bink.dll\0"), 2)
    put_rdata(exp_dll_rva, b"synthetic_bink.dll\0")
    name_rvas = []
    for name, _ in exports_list:
        raw_name = name.encode("ascii") + b"\0"
        nrva = alloc_rdata(len(raw_name), 2)
        put_rdata(nrva, raw_name)
        name_rvas.append(nrva)

    n_exp = len(exports_list)
    eat_rva = alloc_rdata(4 * n_exp, 4)
    ent_rva = alloc_rdata(4 * n_exp, 4)
    eot_rva = alloc_rdata(2 * n_exp, 2)
    for idx, (_, fn_rva) in enumerate(exports_list):
        put_rdata(eat_rva + idx * 4, struct.pack("<I", fn_rva))
        put_rdata(ent_rva + idx * 4, struct.pack("<I", name_rvas[idx]))
        put_rdata(eot_rva + idx * 2, struct.pack("<H", idx))

    export_size = 40
    export_rva = alloc_rdata(export_size, 4)
    put_rdata(
        export_rva,
        struct.pack(
            "<IIHHIIIIIII",
            0, 0, 0, 0, exp_dll_rva, 1, n_exp, n_exp, eat_rva, ent_rva, eot_rva,
        ),
    )

    # .pdata and UNWIND_INFO in .rdata.
    unwind_rva = alloc_rdata(4, 4)
    put_rdata(unwind_rva, b"\x01\x00\x00\x00")
    pdata_entries = [(alloc_rva, alloc_end)]
    if include_group:
        pdata_entries.append((group_rva, group_end))
    if extra_pdata:
        pdata_entries.extend(extra_pdata)
    pdata_entries.sort()
    pdata_size = 12 * len(pdata_entries)
    pdata_rva = alloc_rdata(pdata_size, 4)
    for idx, (begin, end) in enumerate(pdata_entries):
        put_rdata(pdata_rva + idx * 12, struct.pack("<III", begin, end, unwind_rva))

    # Assemble PE32+ headers + 3 sections.
    buf = bytearray(RAW_OFFSET + SECTION_SIZE * 3)
    buf[0:2] = b"MZ"
    pe_off = 0x80
    struct.pack_into("<I", buf, 60, pe_off)
    buf[pe_off:pe_off + 4] = b"PE\0\0"
    # COFF header (20 bytes)
    struct.pack_into("<HHIIIHH", buf, pe_off + 4, machine, 3, 0, 0, 0, 0xF0, 0x2022)
    opt = pe_off + 24
    # Optional header PE32+ standard + Windows fields
    struct.pack_into("<HBBIIIIIQIIHHHHHHIIIIHHQQQQII", buf, opt,
                     magic, 14, 0, SECTION_SIZE, SECTION_SIZE * 2, 0, alloc_rva, TEXT_RVA,
                     IMAGE_BASE, SECTION_ALIGN, 0x200, 6, 0, 0, 0, 6, 0, 0,
                     0x4000, RAW_OFFSET, 0, 3, 0x160,
                     0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    dd = opt + 112
    struct.pack_into("<II", buf, dd + 0 * 8, export_rva, export_size)
    if include_imports:
        struct.pack_into("<II", buf, dd + 1 * 8, import_rva, import_size)
    struct.pack_into("<II", buf, dd + 3 * 8, pdata_rva, pdata_size)

    sections_off = opt + 0xF0
    sections = [
        (b".text\0\0\0", text_vsize, TEXT_RVA, SECTION_SIZE, RAW_OFFSET, 0x60000020),
        (b".rdata\0\0", SECTION_SIZE, RDATA_RVA, SECTION_SIZE, RAW_OFFSET + SECTION_SIZE, 0x40000040),
        (b".data\0\0\0", data_vsize, DATA_RVA, data_rsize, RAW_OFFSET + SECTION_SIZE * 2, data_chars),
    ]
    for idx, (name, vsize, va, rsize, roff, chars) in enumerate(sections):
        struct.pack_into("<8sIIIIIIHHI", buf, sections_off + idx * 40,
                         name, vsize, va, rsize, roff, 0, 0, 0, 0, chars)

    buf[RAW_OFFSET:RAW_OFFSET + SECTION_SIZE] = text
    buf[RAW_OFFSET + SECTION_SIZE:RAW_OFFSET + SECTION_SIZE * 2] = rdata
    buf[RAW_OFFSET + SECTION_SIZE * 2:RAW_OFFSET + SECTION_SIZE * 3] = data

    meta = {
        "setter_rva": setter_rva,
        "alloc_rva": alloc_rva,
        "free_rva": free_rva,
        "group_rva": group_rva,
        "alloc_slot_rva": alloc_slot_rva,
        "free_slot_rva": free_slot_rva,
        "alloc_thunk_rva": alloc_thunk_rva,
        "free_thunk_rva": free_thunk_rva,
        "rdata_rva": RDATA_RVA,
        "text_end_rva": TEXT_RVA + text_vsize,
    }
    return bytes(buf), meta


class BinkHeapAotPositiveTests(unittest.TestCase):
    def test_callback_setter_translate_group_and_generate(self):
        raw, meta = build_image()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "synthetic.dll"
            image.write_bytes(raw)
            with pefile.PE(str(image), fast_load=True) as pe:
                setter, slots = bink_heap_aot.callback_setter(pe)
                self.assertEqual(
                    slots,
                    {meta["alloc_slot_rva"]: "alloc_slot", meta["free_slot_rva"]: "free_slot"},
                )
                self.assertIn("alloc_slot.store(rcx);", setter)
                self.assertIn("free_slot.store(rdx);", setter)

                req = bink_heap_aot.translate(pe, meta["alloc_rva"], True, b"cpu_bink_heap_provider.dll", slots)
                self.assertIn("XMemAlloc(rcx, rdx)", req)
                self.assertIn("alloc_slot.load()", req)
                self.assertIn("reinterpret_cast<Allocate>(rax)(rcx)", req)

                rel = bink_heap_aot.translate(pe, meta["free_rva"], False, b"cpu_bink_heap_provider.dll", slots)
                self.assertIn("XMemFree(reinterpret_cast<void*>(rcx), rdx);", rel)

                grp = bink_heap_aot.group_caller(pe, meta["group_rva"], meta["alloc_rva"])
                self.assertIn("return request(rcx);", grp)
                self.assertIn("rax = request(rcx);", grp)

            cpp = bink_heap_aot.generate(image, None, "heap_group")
            self.assertIn('extern "C" __declspec(dllexport) void BinkSetMemory', cpp)
            self.assertIn('extern "C" __declspec(dllexport) void* heap_request', cpp)
            self.assertIn('extern "C" __declspec(dllexport) void heap_release', cpp)
            self.assertIn('extern "C" __declspec(dllexport) word heap_group', cpp)

    def test_entries_mode_with_xmem_module_and_cli_main(self):
        raw, meta = build_image(module_name=b"xmem.dll")
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "synthetic_xmem.dll"
            output = Path(tmp) / "out.cpp"
            image.write_bytes(raw)
            bink_heap_aot.main([
                str(image),
                str(output),
                "--entries",
                hex(meta["alloc_rva"]),
                hex(meta["free_rva"]),
                "--group-entry",
                hex(meta["group_rva"]),
            ])
            cpp = output.read_text(encoding="ascii")
            self.assertIn("XMemAlloc(rcx, rdx)", cpp)
            self.assertIn("XMemFree(reinterpret_cast<void*>(rcx), rdx);", cpp)
            self.assertIn('extern "C" __declspec(dllexport) word heap_group', cpp)


class BinkHeapAotRefusalTests(unittest.TestCase):
    def _assert_generate_fails(self, raw: bytes, entries=None, group_entry=None, expected_msg: str = ""):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "bad.dll"
            image.write_bytes(raw)
            with self.assertRaises(ValueError) as ctx:
                bink_heap_aot.generate(image, entries, group_entry)
            if expected_msg:
                self.assertIn(expected_msg, str(ctx.exception))

    def test_non_writable_callback_slot_rejected(self):
        raw, _ = build_image(data_chars=0x40000040)
        self._assert_generate_fails(raw, expected_msg="writable callback slot")

    def test_truncated_callback_slot_rejected(self):
        raw, _ = build_image(data_vsize=0x10, free_slot_offset=0x0C)
        self._assert_generate_fails(raw, expected_msg="truncated callback slot")

    def test_non_null_callback_slot_rejected(self):
        raw, _ = build_image(alloc_slot_init=b"\x01")
        self._assert_generate_fails(raw, expected_msg="null initial callback")

    def test_duplicate_callback_slot_writes_rejected(self):
        raw, _ = build_image(free_slot_offset=0x00)
        self._assert_generate_fails(raw, expected_msg="two distinct callback slots")

    def test_unexpected_heap_import_rejected(self):
        raw, _ = build_image(alloc_import_name=b"XMemFree")
        self._assert_generate_fails(raw, expected_msg="unexpected heap import")

    def test_missing_import_directory_raises_value_error(self):
        raw, _ = build_image(include_imports=False)
        self._assert_generate_fails(raw, expected_msg="import directory")

    def test_import_name_thunk_bounds_rejected(self):
        # Unmapped thunk target
        raw_unmapped, meta = build_image(thunk_override={"alloc": 0x9000})
        with pefile.PE(data=raw_unmapped, fast_load=True) as pe:
            ins = Decoder(64, b"\xE8" + _rel32(meta["alloc_rva"] + 30, 0x9000), ip=meta["alloc_rva"] + 25).decode()
            with self.assertRaises(ValueError):
                bink_heap_aot.import_name(pe, ins, {})

        # Non-executable thunk target (inside .rdata)
        raw_nonexec, meta = build_image(thunk_override={"alloc": meta["rdata_rva"]})
        with pefile.PE(data=raw_nonexec, fast_load=True) as pe:
            ins = Decoder(64, b"\xE8" + _rel32(meta["alloc_rva"] + 30, meta["rdata_rva"]),
                          ip=meta["alloc_rva"] + 25).decode()
            with self.assertRaises(ValueError):
                bink_heap_aot.import_name(pe, ins, {})

        # Truncated thunk target (< 6 bytes before end of .text virtual size)
        raw_trunc, meta = build_image(text_vsize=0x200)
        trunc_target = TEXT_RVA + 0x200 - 4
        with pefile.PE(data=raw_trunc, fast_load=True) as pe:
            ins = Decoder(64, b"\xE8" + _rel32(meta["alloc_rva"] + 30, trunc_target),
                          ip=meta["alloc_rva"] + 25).decode()
            with self.assertRaises(ValueError):
                bink_heap_aot.import_name(pe, ins, {})

    def test_missing_and_duplicate_heap_transfers_rejected(self):
        raw_missing_alloc, _ = build_image(alloc_call_count=0)
        self._assert_generate_fails(raw_missing_alloc, expected_msg="exactly one XMemAlloc transfer")

        raw_dup_alloc, _ = build_image(alloc_call_count=2)
        self._assert_generate_fails(raw_dup_alloc, expected_msg="exactly one XMemAlloc transfer")

        raw_missing_free, _ = build_image(free_call_count=0)
        self._assert_generate_fails(raw_missing_free, expected_msg="exactly one XMemFree transfer")

        raw_dup_free, _ = build_image(free_call_count=2)
        self._assert_generate_fails(raw_dup_free, expected_msg="exactly one XMemFree transfer")

    def test_non_amd64_machine_rejected(self):
        raw_i386, _ = build_image(machine=0x014C)
        self._assert_generate_fails(raw_i386, expected_msg="AMD64 PE32+")

        raw_pe32, _ = build_image(magic=0x10B)
        self._assert_generate_fails(raw_pe32, expected_msg="AMD64 PE32+")

    def test_cli_rejects_invalid_output_and_missing_input(self):
        raw, _ = build_image()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "input.dll"
            image.write_bytes(raw)

            # Non-.cpp output suffix -> parser.error (exit code 2)
            bad_ext = Path(tmp) / "output.txt"
            proc = subprocess.run(
                [sys.executable, "-B", str(TOOL), str(image), str(bad_ext)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertFalse(bad_ext.exists())

            # Output matches input path -> parser.error (exit code 2)
            same_cpp = Path(tmp) / "same.cpp"
            same_cpp.write_bytes(raw)
            proc = subprocess.run(
                [sys.executable, "-B", str(TOOL), str(same_cpp), str(same_cpp)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

            # Missing input file -> clean exit 1, zero traceback
            missing = Path(tmp) / "missing.dll"
            out_cpp = Path(tmp) / "out.cpp"
            proc = subprocess.run(
                [sys.executable, "-B", str(TOOL), str(missing), str(out_cpp)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertTrue(proc.stderr.strip())
            self.assertFalse(out_cpp.exists())

            # In-process check of main() with non-PE file -> SystemExit(1), zero traceback
            non_pe = Path(tmp) / "not_a_pe.dll"
            non_pe.write_bytes(b"not a PE image")
            stderr = io.StringIO()
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as exc:
                    bink_heap_aot.main([str(non_pe), str(out_cpp)])
            self.assertEqual(exc.exception.code, 1)
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertTrue(stderr.getvalue().strip())


if __name__ == "__main__":
    unittest.main()
