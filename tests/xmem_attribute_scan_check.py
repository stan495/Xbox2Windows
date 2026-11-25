"""Checks for tools/xmem_attribute_scan.py.

Without arguments: word decoding and desktop classification (no Windows needed).
With the source-built fixtures (tests/cpu/xmem_attributes.asm, then tests/cpu/xmem_delay.asm):
every call-site route and attribute-word source, the summary, and the exit codes.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import pefile

TOOL = Path(__file__).resolve().parents[1] / "tools" / "xmem_attribute_scan.py"
sys.path.insert(0, str(TOOL.parent))
import xmem_attribute_scan as scan  # noqa: E402

BINK_WORD = 0x05E50000  # era_heap_check.cpp bink_word
AMD_WORD = 0xA8004000   # era_heap_check.cpp amd_word
GRAPHICS = "graphics: VirtualAlloc on an allocation-granularity boundary"
HEAP32 = "heap: aligned allocation, 32-byte alignment"
HEAP16 = "heap: aligned allocation, 16-byte alignment"
PHYSICAL = "null: physical memory type not modeled"
RESERVED = "null: reserved bits or undefined page size"


def check(condition, what):
    if not condition:
        raise AssertionError(what)


def check_unreadable_image_is_not_a_gate_result():
    # Exit status 1 is the --fail-on-* gate result, so an image that cannot be read must use another status.
    with tempfile.TemporaryDirectory() as directory:
        not_pe = Path(directory) / "not-pe.bin"
        empty = Path(directory) / "empty.bin"
        not_pe.write_bytes(b"this is not a PE image" * 8)
        empty.write_bytes(b"")
        for image in (not_pe, empty, Path(directory) / "missing.bin"):
            for flag in ("--fail-on-null", "--fail-on-unresolved"):
                result = subprocess.run([sys.executable, "-B", str(TOOL), str(image), flag],
                                        capture_output=True, text=True)
                check(result.returncode == 2, f"{image.name} {flag}: exit {result.returncode}, {result.stderr}")
                check(image.name in result.stderr and "Traceback" not in result.stderr,
                      f"{image.name} {flag}: stderr {result.stderr}")


def check_decoding():
    bink = scan.decode(BINK_WORD)
    check(bink == {"object_type": 0, "page_size": 0, "allocator_id": 0xE5, "alignment": 5, "memory_type": 0,
                   "reserved": 0}, "Bink word fields")
    amd = scan.decode(AMD_WORD)
    check(amd == {"object_type": 0, "page_size": 1, "allocator_id": 0, "alignment": 8, "memory_type": 5,
                  "reserved": 0}, "AMD word fields")
    # Bits 0-32 all set plus reserved bit 33: every field at its widest value, so each mask is checked
    # at full width (neither word above sets an object type bit).
    widest = scan.decode(0x3FFFFFFFF)
    check(widest == {"object_type": 0x3FFF, "page_size": 3, "allocator_id": 0xFF, "alignment": 0x1F,
                     "memory_type": 0xF, "reserved": 1}, f"widest word fields {widest}")
    cases = {
        BINK_WORD: HEAP32,
        0: HEAP16,
        # 1 << 12 is exactly one 4 KiB page: era_heap pages an alignment only above the page size.
        12 << 24: "heap: aligned allocation, 4096-byte alignment",
        13 << 24: "heap: page-backed, 8192-byte alignment",
        # Memory type 6 is the last graphics type; 4 KiB pages and 16-byte alignment fit in 64 KiB.
        6 << 29: GRAPHICS,
        AMD_WORD: GRAPHICS,
        (1 << 29) | (2 << 14): "null: graphics page size or alignment above the allocation granularity",
        (1 << 29) | (17 << 24): "null: graphics page size or alignment above the allocation granularity",
        7 << 29: PHYSICAL,
        9 << 29: PHYSICAL,
        10 << 29: "null: undefined memory type",
        3 << 14: RESERVED,
        1 << 33: RESERVED,
    }
    for word, expected in cases.items():
        check(scan.desktop(word) == expected, f"desktop({word:#x}) = {scan.desktop(word)!r}")
    print(f"decoding: {len(cases) + 3} cases passed")


def sites_by_export(report, path):
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]])
    exports = sorted((sym.address, sym.name.decode()) for sym in pe.DIRECTORY_ENTRY_EXPORT.symbols)
    by_export = {}
    for site in report["sites"]:
        ip = int(site["site"], 16)
        owner = max((a, n) for a, n in exports if a <= ip)[1]
        by_export.setdefault(owner, []).append(site)
    return by_export


def check_fixture(path):
    report = scan.scan(path)
    by_export = sites_by_export(report, path)
    check([(i["import"], i["kind"]) for i in report["imports"]] ==
          [("XMemAlloc", "import"), ("XMemAllocDefault", "import"),
           ("XMemFree", "import"), ("XMemFreeDefault", "import")],
          f"imports {report['imports']}")
    check(report["unmatched_xmem_ordinals"] == [], "no ordinal imports")
    check(len(report["thunks"]) >= 1 and all(not t["exported"] for t in report["thunks"]), f"thunks {report['thunks']}")

    def site(name):
        found = by_export.get(name, [])
        check(len(found) == 1, f"{name}: expected one site, found {len(found)}")
        return found[0]

    def attributes(name, source, **expected):
        a = site(name)["attributes"]
        check(a["source"] == source, f"{name}: source {a}")
        for key, value in expected.items():
            check(a.get(key) == value, f"{name}: {key} = {a.get(key)!r}, expected {value!r}")
        return a

    def constant(name, word, desktop_rule, **expected):
        return attributes(name, "constant", word=word, desktop=desktop_rule, **expected)

    first = site("immediate_iat")
    check(first["route"] == "slot" and first["transfer"] == "call" and first["region"].startswith("function"),
          f"immediate_iat: {first}")
    constant("immediate_iat", BINK_WORD, HEAP32)
    check(site("thunk_call")["route"] == "thunk", "thunk_call goes through the import thunk")
    constant("thunk_call", AMD_WORD, GRAPHICS)
    constant("read_only_constant", 0x20004000, GRAPHICS, certainty="read-only section")
    # Writable data is never a constant. The initial value and the RIP-relative writers found
    # are reported as evidence, with no desktop classification.
    unwritten = attributes("data_unwritten", "data", word=AMD_WORD, written_at=[])
    check("desktop" not in unwritten and "pointers" in unwritten["detail"], f"data_unwritten: {unwritten}")
    pointer = attributes("pointer_written", "data", word=AMD_WORD, written_at=[])
    check("desktop" not in pointer, f"pointer_written must not classify the initial value: {pointer}")
    written = attributes("data_written", "data", word=BINK_WORD)
    check(written.get("written_at") and "desktop" not in written, f"data_written: {written}")
    check("via" in constant("register_copy", 0xE0000000, PHYSICAL), "register_copy follows r8")
    check(site("register_call")["route"] == "register", "register_call goes through rax")
    constant("register_call", 0x20004000, GRAPHICS)
    tail = site("tail_argument")
    check(tail["transfer"] == "jmp" and tail["import"] == "XMemFree", f"tail_argument: {tail}")
    # Exported, so callers outside the image are possible: resolved but not complete.
    argument = attributes("tail_argument", "argument", exported=True)
    check(len(argument["callers"]) == 1 and argument["callers"][0]["source"] == "constant"
          and argument["callers"][0]["word"] == 0x20004000, f"tail_argument callers: {argument}")
    # Not exported: the image's only caller decides the word, so the site is fully constant.
    internal = attributes("forward_alloc", "argument")
    check("exported" not in internal and len(internal["callers"]) == 1
          and internal["callers"][0]["word"] == BINK_WORD, f"internal_passthrough callers: {internal}")
    constant("reserved_bit", 0x2A8004000, RESERVED)
    constant("zero_word", 0, HEAP16)
    attributes("computed_word", "computed")
    constant("jump_reached", 1, HEAP16)
    reached = attributes("indirect_reached", "unknown")
    check("indirect" in reached["detail"], f"indirect_reached must not report the write before jmp rax: {reached}")
    same = constant("join_same", BINK_WORD, HEAP32)
    check(len(same.get("also_at", [])) == 1, f"join_same: both branches counted: {same}")
    differ = attributes("join_differ", "multiple")
    check(sorted(p["word"] for p in differ["paths"]) == [BINK_WORD, AMD_WORD]
          and all(p["source"] == "constant" for p in differ["paths"]), f"join_differ: {differ}")
    constant("loop_constant", 0x20004000, GRAPHICS)
    clobber = attributes("call_clobber", "unknown")
    check("call" in clobber["detail"], f"call_clobber: {clobber}")
    check("via" in constant("saved_across_call", AMD_WORD, GRAPHICS), "saved_across_call follows rbx")
    constant("low_half", AMD_WORD, GRAPHICS)
    attributes("partial_write", "computed")
    # An 8- or 16-bit xor of a register with itself clears only the low bits, so unlike
    # xor edx, edx (zero_word) it is a partial write and never the constant 0.
    for name in ("partial_xor_byte", "partial_xor_word"):
        partial = attributes(name, "computed")
        check("xor" in partial["instruction"].lower(), f"{name}: {partial}")
    passthrough = attributes("exported_passthrough", "argument", exported=True)
    check(passthrough["callers"] == [], f"exported_passthrough: {passthrough}")
    # Entry-register propagation: the callee copies R8 into RDX, while the caller also sets RDX to
    # a different word. The site must be resolved from the caller's R8, not from the default RDX.
    copied = attributes("caller_copy", "argument")
    check("exported" not in copied and copied["register"] == "R8" and len(copied["callers"]) == 1,
          f"caller_copy must resolve from R8: {copied}")
    check(copied["callers"][0]["word"] == AMD_WORD, f"caller_copy caller word: {copied['callers']}")
    check("truncated" not in copied, f"caller_copy is a 64-bit copy: {copied}")
    # Negative control: this copy is 32-bit, so the caller's R8=0x1a8004000 must be truncated to
    # 0xa8004000. The untruncated word sets bit 32, which makes memory type 13 and classifies as
    # null.
    low = attributes("caller_copy_low", "argument")
    check(low["register"] == "R8" and low.get("truncated") is True and len(low["callers"]) == 1,
          f"caller_copy_low must be truncated from R8: {low}")
    check(low["callers"][0]["word"] == AMD_WORD, f"caller_copy_low caller word: {low['callers']}")
    # The Default aliases have their own IAT slots and import names. A site through one must be
    # recorded under the alias it imported, not under the name the alias forwards to.
    alloc_alias = site("default_alloc")
    check(alloc_alias["import"] == "XMemAllocDefault" and alloc_alias["route"] == "slot"
          and alloc_alias["transfer"] == "call", f"default_alloc: {alloc_alias}")
    constant("default_alloc", BINK_WORD, HEAP32)
    free_alias = site("default_free_tail")
    check(free_alias["import"] == "XMemFreeDefault" and free_alias["route"] == "slot"
          and free_alias["transfer"] == "jmp", f"default_free_tail: {free_alias}")
    constant("default_free_tail", AMD_WORD, GRAPHICS)
    # A conditional move chooses the word at run time. The scanner has no constant for it, so it
    # must report the site as computed and never report the immediate written before the compare.
    guarded = attributes("cmove_guarded", "computed")
    check("cmove" in guarded["instruction"].lower(), f"cmove_guarded: {guarded}")
    for name in ("forward_free", "set_word"):
        check(name not in by_export, f"{name} has no xmem call site")
    check(len(report["sites"]) == 30, f"expected 30 sites, found {len(report['sites'])}")

    summary = report["summary"]
    check(summary["sites"] == 30, "summary site count")
    check(sorted(summary["null_words"]) == ["0x2a8004000", "0xe0000000"], f"null words {summary['null_words']}")
    # --fail-on-unresolved exits with 1 exactly when this list is not empty.
    unresolved = {int(s, 16) for s in summary["sites_not_fully_constant"]}
    expected_unresolved = {int(site(n)["site"], 16) for n in
                           ("data_unwritten", "pointer_written", "data_written", "tail_argument", "computed_word",
                            "indirect_reached", "call_clobber", "partial_write", "partial_xor_byte",
                            "partial_xor_word", "exported_passthrough", "cmove_guarded")}
    check(unresolved == expected_unresolved, f"not fully constant: {summary['sites_not_fully_constant']}")
    words = {w["word_hex"]: w for w in summary["words"]}
    check(set(words) == {"0x0", "0x1", "0x5e50000", "0xa8004000", "0x20004000", "0xe0000000", "0x2a8004000"},
          f"distinct words {sorted(words)}")
    # Not counted: data_unwritten and pointer_written, whose 0xa8004000 is only an initial value.
    check(len(words["0xa8004000"]["sites"]) == 7, f"0xa8004000 sites {words['0xa8004000']['sites']}")

    for flag, code in ((None, 0), ("--fail-on-null", 1), ("--fail-on-unresolved", 1)):
        command = [sys.executable, "-B", str(TOOL), str(path), "--output", str(Path(path).with_suffix(".scan.json"))]
        result = subprocess.run(command + ([flag] if flag else []), capture_output=True, text=True)
        check(result.returncode == code, f"exit code with {flag}: {result.returncode}, stderr {result.stderr}")
    print(f"fixture: {len(report['sites'])} sites, summary and exit codes passed")


def check_delay(path):
    report = scan.scan(path)
    check([(i["import"], i["kind"]) for i in report["imports"]] == [("XMemAlloc", "delay")],
          f"delay imports {report['imports']}")
    check(len(report["sites"]) == 1, f"delay sites {report['sites']}")
    a = report["sites"][0]["attributes"]
    check(a["source"] == "constant" and a["word"] == BINK_WORD, f"delay site: {a}")
    check(report["summary"]["sites_not_fully_constant"] == [], f"delay summary {report['summary']}")
    # Control for the fixture's exit codes: with every site constant and valid, both flags pass.
    for flag in ("--fail-on-null", "--fail-on-unresolved"):
        command = [sys.executable, "-B", str(TOOL), str(path), "--output", str(Path(path).with_suffix(".scan.json")), flag]
        result = subprocess.run(command, capture_output=True, text=True)
        check(result.returncode == 0, f"delay exit code with {flag}: {result.returncode}, stderr {result.stderr}")
    print("delay-load fixture: 1 site and exit codes passed")


def main():
    check_decoding()
    check_unreadable_image_is_not_a_gate_result()
    if len(sys.argv) >= 2:
        check_fixture(Path(sys.argv[1]))
    if len(sys.argv) >= 3:
        check_delay(Path(sys.argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
