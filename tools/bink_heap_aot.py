"""Lower the Bink callback setter and heap callers to a Windows DLL."""
import argparse
from pathlib import Path

import pefile
from iced_x86 import Decoder, MemorySizeExt, Mnemonic as M, OpKind as K, Register as R

from cpu_leaf_aot import export_rva
from cpu_ranges import function_ranges, image_bytes
from ffx_version_query import INTEGER_HEADER, REGS, lower, region


def import_name(pe, ins, slots):
    if ins.mnemonic not in (M.CALL, M.JMP):
        return None
    if ins.op0_kind == K.NEAR_BRANCH64:
        target = ins.near_branch_target
        # A rel32 call or tail jump can target the linker-generated IAT thunk.
        code = image_bytes(pe, target, 6, executable=True)
        thunk = Decoder(64, code, ip=target).decode()
        if thunk.mnemonic != M.JMP or not thunk.is_ip_rel_memory_operand:
            return None
        ins = thunk
    if ins.is_ip_rel_memory_operand:
        return slots.get(ins.ip_rel_memory_address)
    return None


def leaf_body(pe, start, slots):
    # A tail-calling leaf may have no .pdata. Stop at its last reachable
    # return/tail, including forward skip and null-pointer branches.
    section = pe.get_section_by_rva(start)
    if not section:
        raise ValueError("leaf entry outside image")
    available = min(section.SizeOfRawData, section.Misc_VirtualSize) - (start - section.VirtualAddress)
    code = image_bytes(pe, start, min(512, available), executable=True)
    body = []
    furthest = start
    for ins in Decoder(64, code, ip=start):
        if ins.is_invalid:
            break
        body.append(ins)
        imported = import_name(pe, ins, slots)
        if ins.op0_kind == K.NEAR_BRANCH64 and ins.mnemonic != M.CALL and not imported:
            furthest = max(furthest, ins.near_branch_target)
        if ins.ip >= furthest and ins.mnemonic in (M.RET, M.JMP):
            return body
    raise ValueError("unbounded or invalid leaf")


def check_callback_slot(pe, rva):
    section = pe.get_section_by_rva(rva)
    if not section or not section.Characteristics & 0x80000000:
        raise ValueError("expected a writable callback slot")
    offset = rva - section.VirtualAddress
    if offset + 8 > section.Misc_VirtualSize:
        raise ValueError("truncated callback slot")
    backed = min(8, max(0, section.SizeOfRawData - offset))
    if backed and any(image_bytes(pe, rva, backed)):
        raise ValueError("expected a null initial callback")


def callback_setter(pe):
    entry = export_rva(pe, "BinkSetMemory")
    first = Decoder(64, image_bytes(pe, entry, 5, executable=True), ip=entry).decode()
    if first.mnemonic == M.JMP and first.op0_kind == K.NEAR_BRANCH64:
        entry = first.near_branch_target
    body = leaf_body(pe, entry, {})
    if len(body) != 3 or body[-1].mnemonic != M.RET:
        raise ValueError("expected a two-slot callback setter")
    sources = {R.RCX: "alloc_slot", R.RDX: "free_slot"}
    slots, stores = {}, {}
    for ins in body[:-1]:
        if (ins.mnemonic != M.MOV or ins.op0_kind != K.MEMORY or not ins.is_ip_rel_memory_operand
                or ins.op1_register not in sources or MemorySizeExt.size(ins.memory_size) != 8):
            raise ValueError("unexpected callback setter store")
        rva = ins.ip_rel_memory_address
        check_callback_slot(pe, rva)
        slot = sources[ins.op1_register]
        slots[rva] = slot
        stores[ins.ip] = f"{slot}.store({REGS[ins.op1_register]});"
    if set(slots.values()) != set(sources.values()):
        raise ValueError("setter must bind two distinct callback slots")
    setter = lower(body, ["rcx = allocate; rdx = release;"], {}, {}, {}, host_ops=stores)
    return setter, slots


def translate(pe, start, allocate, module, callback_slots):
    pe.parse_data_directories(directories=[1])
    imports = getattr(pe, "DIRECTORY_ENTRY_IMPORT", None)
    if not imports:
        raise ValueError("image has no import directory")
    slots = {imp.address - pe.OPTIONAL_HEADER.ImageBase: imp.name
             for desc in imports if desc.dll.lower() == module
             for imp in desc.imports}
    if allocate:
        (bounds,) = function_ranges(pe, start)
        body = region(pe, *bounds)
    else:
        body = leaf_body(pe, start, slots)
    expected = b"XMemAlloc" if allocate else b"XMemFree"
    host_ops = {}
    bound = 0
    for ins in body:
        op = ins.mnemonic
        name = import_name(pe, ins, slots)
        if name:
            if name != expected:
                raise ValueError("unexpected heap import")
            if allocate and op == M.CALL:
                statement = "rax = reinterpret_cast<word>(XMemAlloc(rcx, rdx));"
            elif not allocate and op in (M.CALL, M.JMP):
                statement = "XMemFree(reinterpret_cast<void*>(rcx), rdx);"
                if op == M.JMP:
                    statement += " return 0;"
            else:
                raise ValueError("unsupported heap transfer")
            host_ops[ins.ip] = statement
            bound += 1
        elif ins.is_ip_rel_memory_operand:
            if op != M.MOV or ins.op1_kind != K.MEMORY or ins.op0_register != R.RAX:
                raise ValueError("unsupported access to image data")
            rva = ins.ip_rel_memory_address
            if rva not in callback_slots or MemorySizeExt.size(ins.memory_size) != 8:
                raise ValueError("load does not match a registered callback slot")
            host_ops[ins.ip] = f"rax = {callback_slots[rva]}.load();"
        elif op in (M.PUSH, M.POP) and ins.op0_register == R.RBX:
            host_ops[ins.ip] = ("rsp -= 8; store<word>(rsp, rbx);" if op == M.PUSH
                                else "rbx = load<word>(rsp); rsp += 8;")
        elif allocate and op == M.CALL and ins.op0_register == R.RAX:
            host_ops[ins.ip] = "rax = reinterpret_cast<word>(reinterpret_cast<Allocate>(rax)(rcx));"
        elif (not allocate and op == M.JMP and ins.op0_kind == K.MEMORY
              and ins.memory_base in REGS and ins.memory_index == R.NONE
              and MemorySizeExt.size(ins.memory_size) == 8):
            address = f"{REGS[ins.memory_base]} + {ins.memory_displacement}ull"
            host_ops[ins.ip] = (f"load<Release>({address})(reinterpret_cast<void*>(rcx)); "
                                "return 0;")
    if bound != 1:
        raise ValueError(f"expected exactly one {expected.decode()} transfer")
    return lower(body, ["rsp = reinterpret_cast<word>(frame + 12);", "rcx = argument;"],
                 {}, {}, {}, host_ops=host_ops)


def group_caller(pe: pefile.PE, entry: int, allocator: int) -> str:
    (bounds,) = function_ranges(pe, entry)
    body = region(pe, *bounds)
    bindings = {}
    transfers = set()
    for ins in body:
        if (ins.mnemonic in (M.JMP, M.CALL) and ins.op0_kind == K.NEAR_BRANCH64
                and ins.near_branch_target == allocator):
            transfers.add(ins.mnemonic)
            bindings[ins.ip] = ("return request(rcx);" if ins.mnemonic == M.JMP
                                else "rax = request(rcx);")
        elif ins.mnemonic in (M.PUSH, M.POP) and ins.op0_register == R.RDI:
            bindings[ins.ip] = ("rsp -= 8; store<word>(rsp, rdi);" if ins.mnemonic == M.PUSH
                                else "rdi = load<word>(rsp); rsp += 8;")
    if transfers != {M.JMP, M.CALL}:
        raise ValueError("expected a tail jump and a call to the selected allocator")
    return lower(body, ["rsp = reinterpret_cast<word>(frame + 12);",
                        "rcx = group; rdx = bytes;"], {}, {}, {}, host_ops=bindings)


def generate(image, entries, group_entry=None):
    with pefile.PE(str(image), fast_load=True) as pe:
        if pe.FILE_HEADER.Machine != 0x8664 or pe.OPTIONAL_HEADER.Magic != 0x20b:
            raise ValueError("expected AMD64 PE32+")
        if entries:
            alloc, free = entries
            module = b"xmem.dll"
        else:
            alloc, free = (export_rva(pe, n) for n in ("heap_request", "heap_release"))
            module = b"cpu_bink_heap_provider.dll"
        setter, slots = callback_setter(pe)
        request = translate(pe, alloc, True, module, slots)
        release = translate(pe, free, False, module, slots)
        group = ""
        if group_entry is not None:
            entry = export_rva(pe, "heap_group") if group_entry == "heap_group" else int(group_entry, 0)
            body = group_caller(pe, entry, alloc)
            group = ('extern "C" __declspec(dllexport) word heap_group(word group, word bytes) {\n'
                     + body + "\n}\n")
    return INTEGER_HEADER + """
#include <atomic>
using Allocate = void* (*)(word);
using Release = void (*)(void*);
extern "C" __declspec(dllimport) void* XMemAlloc(word, word);
extern "C" __declspec(dllimport) void XMemFree(void*, word);
static std::atomic<word> alloc_slot{0}, free_slot{0};
static word set_memory(word allocate, word release) {
""" + setter + """
}
extern "C" __declspec(dllexport) void BinkSetMemory(Allocate allocate, Release release) {
    set_memory(reinterpret_cast<word>(allocate), reinterpret_cast<word>(release));
}
static word request(word argument) {
""" + request + """
}
static word release(word argument) {
""" + release + """
}
extern "C" __declspec(dllexport) void* heap_request(word bytes) {
    return reinterpret_cast<void*>(request(bytes));
}
extern "C" __declspec(dllexport) void heap_release(void* pointer) {
    release(reinterpret_cast<word>(pointer));
}
""" + group


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--entries", nargs=2, type=lambda value: int(value, 0),
                        metavar=("ALLOC_RVA", "FREE_RVA"))
    parser.add_argument("--group-entry", help="Grouped allocation caller RVA or the fixture export heap_group")
    args = parser.parse_args(argv)
    if args.output.suffix.lower() != ".cpp" or args.output.resolve() == args.image.resolve():
        parser.error("choose a distinct .cpp output")
    try:
        args.output.write_text(generate(args.image, args.entries, args.group_entry), encoding="ascii")
    except (ValueError, OSError, pefile.PEFormatError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
