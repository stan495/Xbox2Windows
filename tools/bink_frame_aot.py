"""Translate the frame allocator, binding its direct calls to the existing Bink translations."""
import argparse
from pathlib import Path

from iced_x86 import Mnemonic as M, OpKind as K, Register as R
import pefile

from bink_descriptors_aot import BINK_SHA256
from bink_heap_aot import leaf_body
from cpu_leaf_aot import export_rva
from cpu_ranges import function_ranges
from ffx_version_query import INTEGER_HEADER, checked_image, lower, region


def generate(image):
    with checked_image(image, BINK_SHA256) as pe:
        entry = export_rva(pe, "BinkAllocateFrameBuffers")
        owned = [ins for lo, hi in function_ranges(pe, entry) for ins in region(pe, lo, hi)]
        # Use a desktop frame in place of the MSVC prologue, cookie and epilogue.
        first = next((n for n, i in enumerate(owned)
                      if i.mnemonic == M.MOV and (i.op0_register, i.op1_register) == (R.RDI, R.RDX)), None)
        if first is None:
            raise ValueError("missing mov rdi, rdx anchor in BinkAllocateFrameBuffers")
        failure = next((n for n, i in enumerate(owned) if i.mnemonic == M.XOR
                        and i.op0_register == i.op1_register == R.EAX), None)
        if failure is None:
            raise ValueError("missing xor eax, eax anchor in BinkAllocateFrameBuffers")
        if failure <= first:
            raise ValueError("invalid frame allocator anchor order: xor eax, eax precedes mov rdi, rdx")
        body = owned[first:failure+1]
        sites = [i for i in body if i.mnemonic == M.CALL]
        if len(sites) != 9 or any(i.op0_kind != K.NEAR_BRANCH64 for i in sites):
            raise ValueError("unexpected frame allocation call graph")
        init, version, *appends, total, group = sites
        if (init.near_branch_target != 0x25b80 or group.near_branch_target != 0x25a40
                or any(i.near_branch_target != 0x25b20 for i in appends)):
            raise ValueError("frame helper does not match the translated callee")
        helpers = []
        for callee, name in ((version, "aligned_pitch_format"), (total, "group_size")):
            code = lower(leaf_body(pe, callee.near_branch_target, {}), ["rcx = input;"], {}, {}, {})
            helpers.append(f"static word {name}(word input) {{\n{code}\n}}")
        calls = {init.ip: "rax = group_init(rcx, uint32_t(rdx));",
                 version.ip: "rax = aligned_pitch_format(rcx);",
                 total.ip: "rax = group_size(rcx);", group.ip: "rax = heap_group(rcx, rdx);"}
        calls.update({i.ip: "rax = group_append(rcx, rdx, r8);" for i in appends})
        # These five operations belong to the fingerprinted frame function only.
        # Each signed jump immediately follows its original test or compare.
        ops = {M.CMOVAE: "if (!carry) rcx = uint32_t(rbx);", M.NOT: "rax = uint32_t(~rax);"}
        conditions = {M.JLE: "int32_t(rax) <= 0", M.JL: "int32_t(rbp) < load<int32_t>(rdi)",
                      M.JGE: "int64_t(r8) >= 3"}
        bindings = {i.ip: ops[i.mnemonic] for i in body if i.mnemonic in ops}
        for i in body:
            if i.mnemonic in conditions:
                bindings[i.ip] = f"if ({conditions[i.mnemonic]}) goto block_{i.near_branch_target:x};"
        code = lower(body, ["alignas(16) uint8_t locals[0x2d0]{};",
                            "rsp = reinterpret_cast<word>(locals); rcx = context; rdx = frames; r8 = alignment;"],
                     {}, calls, {body[-1].next_ip: "return uint32_t(rax);"}, host_ops=bindings)
    return INTEGER_HEADER + """
extern "C" word group_init(word, uint32_t);
extern "C" word group_append(word, word, word);
extern "C" word heap_group(word, word);
""" + "\n".join(helpers) + """
extern "C" __declspec(dllexport) uint32_t frame_allocate(word context, word frames, uint32_t alignment) {
""" + code + "\n}\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    if args.output.suffix.lower() != ".cpp" or args.output.resolve() == args.image.resolve():
        parser.error("choose a distinct .cpp output")
    try:
        args.output.write_text(generate(args.image), encoding="ascii")
    except (OSError, ValueError, pefile.PEFormatError) as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
