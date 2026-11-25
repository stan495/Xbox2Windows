"""Compare native and translated INT3, UD2 and UD1 in the shared lowering.

Each call runs in a child process, behind the native SEH boundary in trap_harness.cpp: the child reports the
exception code, the export's result and the two state words. INT3 must raise EXCEPTION_BREAKPOINT, and UD2 and
UD1 STATUS_ILLEGAL_INSTRUCTION; the store before the trap must be visible and the guest work after it must not run.
UD1 with a memory operand on an inaccessible page must still raise STATUS_ILLEGAL_INSTRUCTION, not an access
violation. The untaken branch must return normally. Other interrupt, undefined-instruction and fast-fail encodings
stay refused; UD0 is refused because its decoded length depends on the target vendor.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pefile
from iced_x86 import Code, Decoder, FlowControl, Formatter, FormatterSyntax

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from cpu_leaf_aot import export_rva
from cpu_ranges import function_ranges
from ffx_version_query import INTEGER_HEADER, lower, region

EXPORTS = ("trap_int3", "trap_ud2", "trap_branch", "trap_ud1_reg", "trap_ud1_mem", "trap_ud1_disp")
UD1 = (Code.UD1_R16_RM16, Code.UD1_R32_RM32, Code.UD1_R64_RM64)
BREAKPOINT, ILLEGAL = 0x80000003, 0xC000001D
FILL = 0xA5A5A5A5A5A5A5A5
# (export, a) -> (exception code, result, state[0], state[1]); a "noaccess" is a PAGE_NOACCESS page of the child.
EXPECT = {
    ("trap_int3", 0): (BREAKPOINT, 0, 1, FILL),
    ("trap_ud2", 0): (ILLEGAL, 0, 1, FILL),
    ("trap_branch", 1): (BREAKPOINT, 0, 1, FILL),
    ("trap_branch", 0): (0, 7, 1, 2),
    ("trap_ud1_reg", 0): (ILLEGAL, 0, 1, FILL),
    ("trap_ud1_mem", "noaccess"): (ILLEGAL, 0, 1, FILL),
    ("trap_ud1_disp", "noaccess"): (ILLEGAL, 0, 1, FILL),
}
# The trap instructions each export must keep in an optimized translation.
TRAPS = {"trap_int3": [Code.INT3], "trap_ud2": [Code.UD2], "trap_branch": [Code.INT3],
         "trap_ud1_reg": [Code.UD1_R32_RM32], "trap_ud1_mem": [Code.UD1_R32_RM32],
         "trap_ud1_disp": [Code.UD1_R32_RM32]}
# The trap instruction each export has in the translation: UD1 is lowered to __ud2(), which compiles to UD2.
LIFTED_TRAPS = {**TRAPS, "trap_ud1_reg": [Code.UD2], "trap_ud1_mem": [Code.UD2], "trap_ud1_disp": [Code.UD2]}
# Encodings that stay refused, with the text the refusal must contain: INT 3 in its two-byte form, the
# fast-fail interrupt, INT1, INTO (invalid in 64-bit mode), and UD0, whose length differs between AMD (no
# ModRM) and iced's default decoding.
VENDOR = "length depends on the target vendor"
REFUSED = {"CD 03": ("int 3 (imm8)", None), "CD 29": ("int 29h (fast fail)", None), "F1": ("int1", None),
           "CE": ("into", None), "0F FF C0": ("ud0", VENDOR), "0F FF 01": ("ud0 with a memory ModRM", VENDOR),
           "0F FF 80 00 10 00 00": ("ud0 with a disp32", VENDOR)}
CONTROLS = {
    # The trap is dropped, so the guest work after it runs.
    "drop": [("__debugbreak();", ";")],
    # INT3 and UD2 trade places, so each reports the other's code (UD1's "__ud2();  // ud1" is left alone).
    "swap": [("__debugbreak();", "TRAP_SWAP_MARKER"), ("__ud2();\n", "__debugbreak();\n"),
             ("TRAP_SWAP_MARKER", "__ud2();")],
    # The store before each trap is dropped, so the caller does not see it.
    "nostore": [("store<uint64_t>(r9 + 0ull, 1ull);", ";")],
    # UD1 is omitted, so the guest work after it runs.
    "udonedrop": [("__ud2();  // ud1", ";  // ud1")],
    # UD1 evaluates its operand first, so the memory form raises an access violation.
    "udoneload": [("__ud2();  // ud1", "(void)load_always<uint32_t>(rcx); __ud2();  // ud1")],
}


def emit(image, output, variant="lifted"):
    parts = [INTEGER_HEADER]
    with pefile.PE(str(image), fast_load=True) as pe:
        for name in EXPORTS:
            (bounds,) = function_ranges(pe, export_rva(pe, name))
            body = region(pe, *bounds)
            found = [i.code for i in body if i.code in (Code.INT3, Code.UD2, *UD1)]
            if found != TRAPS[name]:
                raise AssertionError(f"{name} contains {found}, expected {TRAPS[name]}")
            code = lower(body, ["rcx = a; rdx = b; r8 = c; r9 = state;"], {}, {}, {})
            parts.append(f'extern "C" __declspec(dllexport) word {name}(word a, word b, word c, word state) {{\n{code}\n}}\n')
    text = "".join(parts)
    for literal, replacement in CONTROLS.get(variant, []):
        if literal not in text:
            raise AssertionError(f"variant {variant} changed nothing for {literal!r}")
        text = text.replace(literal, replacement)
    output.write_text(text, encoding="ascii")


def refusals():
    """Each refused encoding, decoded alone and followed by RET, must reach the lowering's refusal."""
    for hex_bytes, (label, text) in REFUSED.items():
        data = bytes.fromhex(hex_bytes) + b"\xc3"
        body = list(Decoder(64, data, ip=0x1000))
        try:
            lower(body, [], {}, {}, {})
        except ValueError as exc:
            if "unsupported query operation" not in str(exc) or (text is not None and text not in str(exc)):
                raise AssertionError(f"{label}: unexpected refusal {exc}")
        else:
            raise AssertionError(f"the lowering accepted {label} ({hex_bytes})")
    return [label for label, _ in REFUSED.values()]


CHILD = """
import ctypes, json, sys
ctypes.windll.kernel32.SetErrorMode(0x8003)
harness = ctypes.CDLL(sys.argv[1])
lib = ctypes.CDLL(sys.argv[2])
fn = getattr(lib, sys.argv[3])
harness.seh_call.argtypes = [ctypes.c_void_p] + [ctypes.c_uint64] * 3 + [ctypes.c_void_p, ctypes.c_void_p]
harness.seh_call.restype = ctypes.c_uint64
state = (ctypes.c_uint64 * 2)(0xA5A5A5A5A5A5A5A5, 0xA5A5A5A5A5A5A5A5)
if sys.argv[4] == "noaccess":
    ctypes.windll.kernel32.VirtualAlloc.restype = ctypes.c_void_p
    ctypes.windll.kernel32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]
    a = ctypes.windll.kernel32.VirtualAlloc(None, 4096, 0x3000, 0x01)
    if not a:
        raise OSError("VirtualAlloc failed")
else:
    a = int(sys.argv[4])
code = ctypes.c_uint32()
result = harness.seh_call(ctypes.cast(fn, ctypes.c_void_p), a, 0, 0, ctypes.addressof(state),
                          ctypes.addressof(code))
print(json.dumps([code.value, result, state[0], state[1]]), flush=True)
"""


def run(harness, path, name, a):
    child = subprocess.run([sys.executable, "-I", "-c", CHILD, str(Path(harness).resolve()), str(Path(path).resolve()),
                            name, str(a)], capture_output=True, text=True, timeout=60)
    if child.returncode or child.stderr:
        raise AssertionError(f"{Path(path).name} {name}({a}): child exit {child.returncode:#x} stderr {child.stderr!r}")
    return tuple(json.loads(child.stdout))


def violation(harness, native, path, label):
    for (name, a), expected in EXPECT.items():
        got = run(harness, path, name, a)
        reference = run(harness, native, name, a)
        if reference != expected:
            raise AssertionError(f"native {name}({a}) = {reference}, expected {expected}")
        if got != expected:
            return f"{label} {name}({a}): (code, result, before, after) {got}, expected {expected}"
    return None


def traps_in(path):
    """The INT3 and UD2 instructions reachable from each export of a DLL, following every branch."""
    with pefile.PE(str(path), fast_load=True) as pe:
        base, image = pe.OPTIONAL_HEADER.ImageBase, pe.get_memory_mapped_image()
        found = {}
        for name in EXPORTS:
            pending, seen, codes = [base + export_rva(pe, name)], set(), []
            while pending:
                ip = pending.pop()
                while ip not in seen:
                    seen.add(ip)
                    if not base <= ip < base + len(image):
                        raise AssertionError(f"{Path(path).name} {name}: branch leaves the image at {ip:#x}")
                    instruction = Decoder(64, image[ip - base:ip - base + 15], ip=ip).decode()
                    if instruction.code in (Code.INT3, Code.UD2, *UD1):
                        codes.append(instruction.code)
                    flow = instruction.flow_control
                    if flow in (FlowControl.RETURN, FlowControl.EXCEPTION, FlowControl.INTERRUPT):
                        break
                    if flow in (FlowControl.CONDITIONAL_BRANCH, FlowControl.UNCONDITIONAL_BRANCH):
                        pending.append(instruction.near_branch_target)
                        if flow == FlowControl.UNCONDITIONAL_BRANCH:
                            break
                    elif flow not in (FlowControl.NEXT, FlowControl.CALL):
                        raise AssertionError(f"{Path(path).name} {name}: cannot follow {instruction} at {ip:#x}")
                    ip = instruction.next_ip
            found[name] = sorted(set(codes))
    return found


def record_forms(native_path, record):
    """Write the forms of the translated exports for tools/form_coverage.py: decoded from the image, because ML64
    has no UD1 spelling and the fixture writes it as bytes, which a source scan would not see."""
    formatter = Formatter(FormatterSyntax.NASM)
    forms = set()
    with pefile.PE(str(native_path), fast_load=True) as pe:
        for name in EXPORTS:
            (bounds,) = function_ranges(pe, export_rva(pe, name))
            forms |= {formatter.format(i).split()[0] for i in region(pe, *bounds)}
    if not {"int3", "ud2", "ud1"} <= forms:
        raise AssertionError(f"recorded forms {sorted(forms)} miss a trap")
    Path(record).parent.mkdir(parents=True, exist_ok=True)
    Path(record).write_text("".join(form + "\n" for form in sorted(forms)), encoding="utf-8")


def check(harness, native_path, lifted_path, *extra_paths):
    record = os.environ.get("FORM_COVERAGE_RECORD")
    if record:
        Path(record).unlink(missing_ok=True)  # a failed run leaves no stale record
    print(f"refused: {', '.join(refusals())}")
    problem = violation(harness, native_path, lifted_path, "lifted")
    if problem:
        raise AssertionError(problem)
    print("native and translated: INT3 raises 0x80000003, and UD2 and UD1 (register form, and a memory operand on "
          "an inaccessible page) 0xc000001d, after the earlier store; the guest work after each trap does not run, "
          "and the untaken branch returns 7 with both stores")
    for path in extra_paths:
        stem = Path(path).stem
        if stem.endswith("_o2"):
            traps = traps_in(path)
            if any(traps[name] != sorted(set(codes)) for name, codes in LIFTED_TRAPS.items()):
                raise AssertionError(f"{stem}: traps {traps}, expected {LIFTED_TRAPS}")
            problem = violation(harness, native_path, path, stem)
            if problem:
                raise AssertionError(problem)
            print(f"{stem}: the /O2 build keeps every trap ({traps}) and every rule")
            continue
        variant = stem.rsplit("_", 1)[-1]
        problem = violation(harness, native_path, path, f"control {variant}")
        print(f"control {variant}: {problem}")
        if problem is None:
            raise AssertionError(f"control {variant} kept every rule")
    if record:
        record_forms(native_path, record)


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    if mode == "emit":
        image, output, *variant = args
        emit(Path(image), Path(output), *variant)
    elif mode == "check":
        check(*args)
    else:
        raise ValueError(mode)
