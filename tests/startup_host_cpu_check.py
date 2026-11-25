"""Checks tools/startup/host_cpu.py by running its replacements on the host processor.

Source-built x64 functions are assembled here byte by byte, patched the way the startup debugger
patches a faulting site, and called. Results are compared with a model of the instruction and, on a
processor that has the original instruction (CPUID), with the unpatched function as well.
Exits 77 on a host that is not x86-64.
"""
import ctypes
import os
import platform
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "startup"))
import host_cpu  # noqa: E402

M64 = (1 << 64) - 1
WINDOWS = os.name == "nt"
# The functions below take their arguments in rcx and rdx; System V passes them in rdi and rsi.
PROLOGUE = b"" if WINDOWS else b"\x48\x89\xf9\x48\x89\xf2"


class Arena:
    """One block of executable memory: functions and their stubs stay within rel32 reach."""

    def __init__(self, size=1 << 16):
        if WINDOWS:
            k32 = ctypes.WinDLL("kernel32")
            k32.VirtualAlloc.restype = ctypes.c_void_p
            k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]
            self.base = k32.VirtualAlloc(None, size, 0x3000, 0x40)
        else:
            import mmap
            self.map = mmap.mmap(-1, size, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
            self.base = ctypes.addressof(ctypes.c_char.from_buffer(self.map))
        if not self.base:
            raise OSError("no executable memory")
        self.next, self.end = self.base, self.base + size

    def put(self, data):
        at = self.next
        if at + len(data) > self.end:
            raise OSError("arena full")
        ctypes.memmove(at, bytes(data), len(data))
        self.next = at + ((len(data) + 15) & ~15)
        return at

    def write(self, address, data):
        ctypes.memmove(address, bytes(data), len(data))


ARENA = Arena()
CALL = ctypes.CFUNCTYPE(ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64)


def plan_ranges(failures):
    """plan_replacement refuses a site whose jmp rel32 fields would not fit.

    Both offsets are computed from the site, the stub address and the moved bytes; with a site
    more than 2 GiB away from the stub either field wraps, so the plan must be refused rather
    than encoded.
    """
    # clzero (3 bytes) followed by two nops, so the 5-byte jmp has instructions to move.
    code = bytes.fromhex("0f01fc9090")
    near = 0x1000
    plan = host_cpu.plan_replacement(code, near, 3, b"\x90" * 8, near + 0x100)
    if plan is None:
        failures.append("plan_replacement refused a near site")
    # A site more than 2 GiB from the stub cannot be encoded as a rel32 jump. The second range
    # check (the jump back from the moved instructions) needs a stub larger than 2 GiB to reach,
    # so it stays uncovered rather than being asserted with an input no caller produces.
    far = 0x7FFF0000_0000
    if host_cpu.plan_replacement(code, far, 3, b"\x90" * 8, 0x1000) is not None:
        failures.append("plan_replacement encoded an out-of-range forward offset")
def build(before, instruction, after, stub=None, patch=True):
    """A function `PROLOGUE before instruction after`; with patch, the instruction is replaced."""
    code = PROLOGUE + before + instruction + after
    start = ARENA.put(code)
    if patch:
        site = start + len(PROLOGUE) + len(before)
        tail = instruction + after
        if stub is None:
            length, stub = host_cpu.stub_for(tail)
        else:
            length = len(instruction)
        stub_address = ARENA.put(b"\xcc" * (len(stub) + 64))
        plan = host_cpu.plan_replacement(tail, site, length, stub, stub_address)
        assert plan, "no replacement plan"
        stub_bytes, site_bytes = plan
        assert len(stub_bytes) <= len(stub) + 64 and len(site_bytes) >= 5
        ARENA.write(stub_address, stub_bytes)
        ARENA.write(site, site_bytes)
    return CALL(start)


def cpuid(leaf):
    # push rbx; mov eax, ecx; xor ecx, ecx; cpuid; shl rcx, 32; mov eax, ebx; or rax, rcx; pop rbx; ret
    f = build(b"", b"", bytes.fromhex("5389c831c90fa248c1e12089d84809c85bc3"), patch=False)
    value = f(leaf, 0)
    return value & 0xFFFFFFFF, value >> 32  # ebx, ecx


def host_features():
    features = set()
    # Leaves 0x80000001 and 0x80000008 exist on every x86-64 processor.
    ebx7, ecx7 = cpuid(7)
    if ebx7 >> 29 & 1:
        features.add("sha")
    if ecx7 >> 22 & 1:
        features.add("rdpid")
    _, ecx = cpuid(0x80000001)
    if ecx >> 6 & 1:
        features.add("sse4a")
    ebx, _ = cpuid(0x80000008)
    if ebx & 1:
        features.add("clzero")
    return features


# ---- CLZERO -------------------------------------------------------------------------------------
def clzero_case(stub=None, patch=True):
    """True when a patched `clzero` zeroes exactly its line, keeps CF and runs the moved lea."""
    # mov rax, rcx; stc | clzero | lea rax, [rax + 0x40]; jnc bad; ret; bad: xor eax, eax; ret
    f = build(b"\x48\x89\xc8\xf9", host_cpu.CLZERO, bytes.fromhex("488d40407301c331c0c3"), stub, patch)
    raw = ctypes.create_string_buffer(64 * 5)
    base = (ctypes.addressof(raw) + 63) & ~63
    ctypes.memset(base, 0xAA, 64 * 3)
    pointer = base + 64 + 37  # inside the middle line
    result = f(pointer, 0)
    lines = [ctypes.string_at(base + 64 * i, 64) for i in range(3)]
    return result == pointer + 0x40 and lines == [b"\xaa" * 64, b"\0" * 64, b"\xaa" * 64]


def clzero_moves_rip_relative():
    # | clzero | mov rdx, [rip + 3]; mov rax, rdx; ret; <qword>   (the load moves behind the stub)
    value = 0x1122334455667788
    after = bytes.fromhex("488b1504000000") + bytes.fromhex("4889d0c3") + struct.pack("<Q", value)
    f = build(b"\x48\x89\xc8", host_cpu.CLZERO, after)
    raw = ctypes.create_string_buffer(256)
    return f((ctypes.addressof(raw) + 127) & ~63, 0) == value


# ---- INSERTQ / EXTRQ ----------------------------------------------------------------------------
def movq_to_xmm(xmm, gpr):   # movq xmm, r64
    rex = 0x48 | (4 if xmm >= 8 else 0) | (1 if gpr >= 8 else 0)
    return bytes([0x66, rex, 0x0F, 0x6E, 0xC0 | (xmm & 7) << 3 | (gpr & 7)])


def movq_from_xmm(xmm):      # movq rax, xmm
    rex = 0x48 | (4 if xmm >= 8 else 0)
    return bytes([0x66, rex, 0x0F, 0x7E, 0xC0 | (xmm & 7) << 3])


def movdqu_load(xmm, gpr):   # movdqu xmm, [r64]
    rex = ([0x44] if xmm >= 8 else [])
    return bytes([0xF3] + rex + [0x0F, 0x6F, (xmm & 7) << 3 | (gpr & 7)])


def movdqu_store(gpr, xmm):  # movdqu [r64], xmm
    rex = ([0x44] if xmm >= 8 else [])
    return bytes([0xF3] + rex + [0x0F, 0x7F, (xmm & 7) << 3 | (gpr & 7)])


def preserve_xmm(xmms):
    """Prologue/epilogue saving and restoring unique XMM registers across a Windows x64 call."""
    regs = sorted(set(xmms))
    n = len(regs) * 16
    save = bytes([0x48, 0x83, 0xEC, n])
    restore = b""
    for i, xmm in enumerate(regs):
        rex = [0x44] if xmm >= 8 else []
        modrm = 0x44 | (xmm & 7) << 3
        save += bytes([0xF3] + rex + [0x0F, 0x7F, modrm, 0x24, i * 16])
        restore += bytes([0xF3] + rex + [0x0F, 0x6F, modrm, 0x24, i * 16])
    return save, restore + bytes([0x48, 0x83, 0xC4, n])


def insertq_model(a, b, width, offset):
    mask = (1 << (width or 64)) - 1
    return (a & ~(mask << offset) | (b & mask) << offset) & M64


def extrq_model(a, width, offset):
    return a >> offset & ((1 << (width or 64)) - 1)


def _rex_prefix(dest, src=0):
    bits = (4 if dest >= 8 else 0) | (1 if src >= 8 else 0)
    return bytes([0x40 | bits]) if bits else b""


def insertq_function(dest, src, width, offset, patch=True, stub=None):
    instruction = b"\xf2" + _rex_prefix(dest, src) + bytes([0x0F, 0x78, 0xC0 | (dest & 7) << 3 | (src & 7), width, offset])
    save, restore = preserve_xmm((dest, src))
    return build(save + movq_to_xmm(dest, 1) + movq_to_xmm(src, 2), instruction,
                 movq_from_xmm(dest) + restore + b"\xc3", stub, patch)


def extrq_function(dest, width, offset, patch=True, stub=None):
    instruction = b"\x66" + _rex_prefix(0, dest) + bytes([0x0F, 0x78, 0xC0 | (dest & 7), width, offset])
    save, restore = preserve_xmm((dest,))
    return build(save + movq_to_xmm(dest, 1), instruction, movq_from_xmm(dest) + restore + b"\xc3", stub, patch)


def sse4a_128_function(instruction, dest, src, patch=True, stub=None):
    """Run an SSE4a instruction on [rcx] (xmm_dest) and [rdx] (xmm_src), writing xmm_dest back to [rcx]."""
    save, restore = preserve_xmm((dest, src))
    before = save + movdqu_load(dest, 1) + (movdqu_load(src, 2) if src != dest else b"")
    if src == dest and instruction[0] == 0xF2:
        before = save + movdqu_load(dest, 2)
    return build(before, instruction, movdqu_store(1, dest) + restore + b"\xc3", stub, patch)


VALUES = [(0x0123456789ABCDEF, 0xFEDCBA9876543210), (M64, 0), (0, M64), (0x8000000000000001, 0x5555AAAA5555AAAA)]
FIELDS = [(16, 16), (1, 0), (1, 63), (8, 56), (63, 1), (32, 0), (0, 0), (5, 11), (48, 16), (32, 32)]


def _ctrl_word(width, offset, noise=0xCAFE00000000C0C0):
    """Pack SSE4a register control word with non-zero ignored bits [7:6], [15:14], [63:16]."""
    return (width & 0x3F) | ((offset & 0x3F) << 8) | (noise & ~0x3F3F)


def sse4a_cases(native):
    checked = 0
    imm_pairs = ((0, 1), (1, 0), (3, 7), (7, 2), (5, 5), (0, 9), (8, 1), (9, 12), (15, 15))
    for dest, src in imm_pairs:
        for width, offset in FIELDS:
            patched = insertq_function(dest, src, width, offset)
            original = insertq_function(dest, src, width, offset, patch=False) if native else None
            for a, b in VALUES:
                if dest == src:
                    a = b  # the second load overwrites the first
                expected = insertq_model(a, b, width, offset)
                got = patched(a, b)
                assert got == expected, f"insertq xmm{dest}, xmm{src}, {width}, {offset}: {got:#x}, expected {expected:#x}"
                if original:
                    assert original(a, b) == expected, f"native insertq differs from the model ({width}, {offset})"
                checked += 1
    for dest in (0, 4, 7, 8, 11, 15):
        for width, offset in FIELDS:
            patched = extrq_function(dest, width, offset)
            original = extrq_function(dest, width, offset, patch=False) if native else None
            for a, _ in VALUES:
                expected = extrq_model(a, width, offset)
                got = patched(a, 0)
                assert got == expected, f"extrq xmm{dest}, {width}, {offset}: {got:#x}, expected {expected:#x}"
                if original:
                    assert original(a, 0) == expected, f"native extrq differs from the model ({width}, {offset})"
                checked += 1

    # Immediate upper-64 preservation for REX and non-REX forms.
    upper_sentinel = 0xCAFEBABE12345678
    for insn, dest, src, is_insertq in (
        (bytes.fromhex("f2450f78cc1010"), 9, 12, True),
        (bytes.fromhex("66410f78c31010"), 11, 11, False),
    ):
        f = sse4a_128_function(insn, dest, src)
        dst_buf = ctypes.create_string_buffer(struct.pack("<QQ", 0x0123456789ABCDEF, upper_sentinel), 16)
        src_buf = ctypes.create_string_buffer(struct.pack("<QQ", 0xFEDCBA9876543210, 0x1111222233334444), 16)
        f(ctypes.addressof(dst_buf), ctypes.addressof(src_buf))
        out_lo, out_hi = struct.unpack("<QQ", dst_buf.raw)
        expected_lo = (insertq_model(0x0123456789ABCDEF, 0xFEDCBA9876543210, 16, 16)
                       if is_insertq else extrq_model(0x0123456789ABCDEF, 16, 16))
        assert out_lo == expected_lo and out_hi == upper_sentinel, (hex(out_lo), hex(out_hi))
        checked += 1

    # Register-parameter forms (0F 79): INSERTQ xmm1, xmm2 and EXTRQ xmm1, xmm2.
    reg_pairs = ((0, 1), (1, 0), (5, 5), (0, 9), (8, 1), (9, 12), (15, 15))
    for dest, src in reg_pairs:
        ins_code = b"\xf2" + _rex_prefix(dest, src) + bytes([0x0F, 0x79, 0xC0 | (dest & 7) << 3 | (src & 7)])
        ext_code = b"\x66" + _rex_prefix(dest, src) + bytes([0x0F, 0x79, 0xC0 | (dest & 7) << 3 | (src & 7)])
        ins_patched = sse4a_128_function(ins_code, dest, src)
        ext_patched = sse4a_128_function(ext_code, dest, src)
        ins_native = sse4a_128_function(ins_code, dest, src, patch=False) if native else None
        ext_native = sse4a_128_function(ext_code, dest, src, patch=False) if native else None
        for width, offset in FIELDS:
            ctrl = _ctrl_word(width, offset)
            for a, b in VALUES:
                dst_in = b if dest == src else a
                expected_ins = insertq_model(dst_in, b, width, offset)
                dst_buf = ctypes.create_string_buffer(struct.pack("<QQ", a, upper_sentinel), 16)
                src_buf = ctypes.create_string_buffer(struct.pack("<QQ", b, ctrl), 16)
                ins_patched(ctypes.addressof(dst_buf), ctypes.addressof(src_buf))
                out_lo, out_hi = struct.unpack("<QQ", dst_buf.raw)
                exp_hi = ctrl if dest == src else upper_sentinel
                assert out_lo == expected_ins and out_hi == exp_hi, (
                    f"reg insertq xmm{dest}, xmm{src} ({width}, {offset}): {out_lo:#x}/{out_hi:#x}")
                if ins_native:
                    dst_buf_n = ctypes.create_string_buffer(struct.pack("<QQ", a, upper_sentinel), 16)
                    ins_native(ctypes.addressof(dst_buf_n), ctypes.addressof(src_buf))
                    nat_lo, _ = struct.unpack("<QQ", dst_buf_n.raw)
                    assert nat_lo == expected_ins, f"native reg insertq differs ({width}, {offset})"
                checked += 1

                if dest == src:
                    # EXTRQ xmm, xmm uses the same register for value and control (bits [13:8] and [5:0]).
                    val = (a & ~0xFFFF) | ctrl & 0xFFFF
                    expected_ext = extrq_model(val, width, offset)
                    dst_buf = ctypes.create_string_buffer(struct.pack("<QQ", val, upper_sentinel), 16)
                    src_buf = ctypes.create_string_buffer(struct.pack("<QQ", 0, 0), 16)
                else:
                    expected_ext = extrq_model(a, width, offset)
                    dst_buf = ctypes.create_string_buffer(struct.pack("<QQ", a, upper_sentinel), 16)
                    src_buf = ctypes.create_string_buffer(struct.pack("<QQ", ctrl, 0xBEEF000011112222), 16)
                ext_patched(ctypes.addressof(dst_buf), ctypes.addressof(src_buf))
                out_lo, out_hi = struct.unpack("<QQ", dst_buf.raw)
                assert out_lo == expected_ext and out_hi == upper_sentinel, (
                    f"reg extrq xmm{dest}, xmm{src} ({width}, {offset}): {out_lo:#x}/{out_hi:#x}")
                if ext_native:
                    dst_buf_n = ctypes.create_string_buffer(
                        struct.pack("<QQ", val if dest == src else a, upper_sentinel), 16)
                    ext_native(ctypes.addressof(dst_buf_n), ctypes.addressof(src_buf))
                    nat_lo, _ = struct.unpack("<QQ", dst_buf_n.raw)
                    assert nat_lo == expected_ext, f"native reg extrq differs ({width}, {offset})"
                checked += 1
    return checked


def sse4a_keeps_registers_and_flags():
    # Verify borrowed GPRs (rax, rcx, rdx, rsi, rdi) and CF stay intact across immediate/register stubs.
    # push rsi; push rdi; mov rsi, 0x1111; mov rdi, 0x2222; mov rax, 0x3333; stc
    # | <instruction> |
    # jnc bad; cmp rax, 0x3333; jne bad; cmp rsi, 0x1111; jne bad; cmp rdi, 0x2222; jne bad;
    # lea rax, [rcx + rdx]; jmp ok; bad: xor eax, eax; ok: pop rdi; pop rsi; ret
    setup = bytes.fromhex("565748be111100000000000048bf222200000000000048b83333000000000000f9")
    check = bytes.fromhex(
        "731d483d3333000075164881fe11110000750d4881ff222200007504488d0411eb0231c05f5ec3"
    )
    for insn in (
        bytes.fromhex("f20f78c11010"),     # insertq xmm0, xmm1, 16, 16
        bytes.fromhex("f2450f78cc1010"),   # insertq xmm9, xmm12, 16, 16
        bytes.fromhex("66410f78c31010"),   # extrq xmm11, 16, 16
        bytes.fromhex("f2450f79cc"),       # insertq xmm9, xmm12
        bytes.fromhex("66450f79cc"),       # extrq xmm9, xmm12
    ):
        f = build(setup, insn, check)
        if f(0x1000, 0x234) != 0x1234:
            return False
    return True


# ---- byte rewrites ------------------------------------------------------------------------------
def rewrites():
    import iced_x86

    def one(code):
        decoder = iced_x86.Decoder(64, bytes(code), ip=0x1000)
        return decoder.decode()

    for code in (bytes.fromhex("660fae30"), bytes.fromhex("66410fae31"), bytes.fromhex("660fae742408"),
                 bytes.fromhex("660fae3500100000"), bytes.fromhex("66420fae34c8")):
        before = one(code)
        assert before.mnemonic == iced_x86.Mnemonic.CLWB, code.hex()
        offset, byte = host_cpu.clwb_rewrite(code)
        after = one(code[:offset] + byte + code[offset + 1:])
        assert after.mnemonic == iced_x86.Mnemonic.CLFLUSHOPT and after.len == before.len == len(code), code.hex()
        for field in ("memory_base", "memory_index", "memory_index_scale", "memory_displacement", "memory_segment"):
            assert getattr(after, field) == getattr(before, field), (code.hex(), field)
        # A host without CLFLUSHOPT: one NOP of the same length, for the CLWB and for its rewrite.
        for flush in (code, code[:offset] + byte + code[offset + 1:]):
            nop = host_cpu.flush_nop(flush)
            assert host_cpu.flush_length(flush) == before.len and len(nop) == before.len, flush.hex()
            decoded = one(nop)
            assert decoded.mnemonic == iced_x86.Mnemonic.NOP and decoded.len == len(nop), (flush.hex(), nop.hex())
    # Every addressing form: the length must match the decoder's, or a NOP would cut an instruction.
    lengths = set()
    for rex in (b"", b"\x41", b"\x42", b"\x43"):
        for modrm in range(0xC0):
            if (modrm >> 3) & 7 not in (6, 7):
                continue
            for sib in ((0x24, 0x25, 0xC8, 0xCD) if modrm & 7 == 4 else (0,)):
                code = b"\x66" + rex + b"\x0f\xae" + bytes([modrm]) + (bytes([sib]) if modrm & 7 == 4 else b"") + bytes(8)
                decoded = one(code)
                assert decoded.mnemonic in (iced_x86.Mnemonic.CLWB, iced_x86.Mnemonic.CLFLUSHOPT), code.hex()
                assert host_cpu.flush_length(code) == decoded.len, (code.hex(), decoded.len)
                assert one(host_cpu.flush_nop(code)).len == decoded.len
                lengths.add(decoded.len)
    assert lengths == set(host_cpu.NOPS), sorted(lengths)
    for refused in (bytes.fromhex("0fae3000"), bytes.fromhex("660faef0"), bytes.fromhex("660fae28"),  # /5
                    bytes.fromhex("660fae"), bytes.fromhex("660fae742408")[:5]):                      # cut short
        assert host_cpu.flush_length(refused) is None and host_cpu.flush_nop(refused) is None, refused.hex()
    for refused in (bytes.fromhex("660fae38"),      # clflushopt already
                    bytes.fromhex("0fae3000"),      # xsaveopt: no 66 prefix
                    bytes.fromhex("660faef0"),      # register form
                    bytes.fromhex("660faf30")):     # another opcode
        assert host_cpu.clwb_rewrite(refused) is None, refused.hex()

    monitorx, mwaitx = host_cpu.wait_rewrite(b"\x0f\x01\xfa"), host_cpu.wait_rewrite(b"\x0f\x01\xfb")
    assert len(monitorx) == 3 and one(monitorx).mnemonic == iced_x86.Mnemonic.NOP and one(monitorx).len == 3
    first = one(mwaitx)
    assert len(mwaitx) == 3 and first.mnemonic == iced_x86.Mnemonic.PAUSE and first.len == 2 and mwaitx[2] == 0x90
    assert host_cpu.wait_rewrite(b"\x0f\x01\xf9") is None and host_cpu.wait_rewrite(host_cpu.CLZERO) is None

    # Encodings the stubs do not cover are refused, not approximated.
    for valid, wanted_len in (
        (bytes.fromhex("f20f78c12020"), 6),    # 32 bits at 32 is defined
        (bytes.fromhex("f2450f78cc2020"), 7),  # REX insertq xmm9, xmm12, 32, 32
        (bytes.fromhex("66410f78c12020"), 7),  # REX extrq xmm9, 32, 32
        (bytes.fromhex("f20f79c1"), 4),        # reg insertq xmm0, xmm1
        (bytes.fromhex("f2450f79cc"), 5),      # REX reg insertq xmm9, xmm12
        (bytes.fromhex("660f79c1"), 4),        # reg extrq xmm0, xmm1
        (bytes.fromhex("66450f79cc"), 5),      # REX reg extrq xmm9, xmm12
    ):
        found = host_cpu.stub_for(valid)
        assert found is not None and found[0] == wanted_len and host_cpu.sse4a_stub(valid) == found[1], valid.hex()
    for refused in (bytes.fromhex("f20f78c13030"),               # idx + len > 64
                    bytes.fromhex("f2450f78cc3030"),             # REX idx + len > 64
                    bytes.fromhex("66410f78c03030"),             # REX extrq idx + len > 64
                    bytes.fromhex("f20f78011010"),               # not a register operand
                    bytes.fromhex("f2410f78011010"),             # REX mod != 3
                    bytes.fromhex("660f7901"),                   # reg extrq mod != 3
                    bytes.fromhex("f2410f7901"),                 # REX reg insertq mod != 3
                    bytes.fromhex("660f78c81010"),               # extrq with /1
                    bytes.fromhex("66410f78c81010"),             # REX extrq with /1
                    bytes.fromhex("f30f78c11010"),               # wrong prefix f3
                    bytes.fromhex("f30f79c1"),                   # wrong prefix f3 on 0f 79
                    bytes.fromhex("0f78c11010"),                 # missing prefix
                    bytes.fromhex("0f79c1"),                     # missing prefix
                    bytes.fromhex("41660f78c11010"),             # REX before 66
                    bytes.fromhex("66f20f78c11010"),             # duplicate prefix
                    bytes.fromhex("6641420f79c1"),               # duplicate REX
                    bytes.fromhex("f2410f78c110"),               # truncated REX immediate
                    bytes.fromhex("660f79")):                    # truncated reg form
        assert host_cpu.sse4a_stub(refused) is None and host_cpu.stub_for(refused) is None, refused.hex()


# ---- MOVNTSS / MOVNTSD, RDPRU, RDPID ------------------------------------------------------------
def more_rewrites(features):
    import iced_x86

    def one(code):
        return iced_x86.Decoder(64, bytes(code), ip=0x1000).decode()

    stores = ((bytes.fromhex("f30f2b01"), iced_x86.Mnemonic.MOVNTSS, iced_x86.Mnemonic.MOVSS),
              (bytes.fromhex("f20f2b01"), iced_x86.Mnemonic.MOVNTSD, iced_x86.Mnemonic.MOVSD),
              (bytes.fromhex("f3450f2b4c2410"), iced_x86.Mnemonic.MOVNTSS, iced_x86.Mnemonic.MOVSS),
              (bytes.fromhex("f2440f2b3d00100000"), iced_x86.Mnemonic.MOVNTSD, iced_x86.Mnemonic.MOVSD))
    for code, original, wanted in stores:
        before = one(code)
        assert before.mnemonic == original, code.hex()
        offset, byte = host_cpu.nt_store_rewrite(code)
        after = one(code[:offset] + byte + code[offset + 1:])
        assert after.mnemonic == wanted and after.len == before.len == len(code), code.hex()
        assert after.op0_kind == iced_x86.OpKind.MEMORY and after.op1_register == before.op1_register, code.hex()
        for field in ("memory_base", "memory_index", "memory_index_scale", "memory_displacement"):
            assert getattr(after, field) == getattr(before, field), (code.hex(), field)
    for refused in (bytes.fromhex("0f2b0100"),      # movntps
                    bytes.fromhex("660f2b01"),      # movntpd
                    bytes.fromhex("f30f2bc1"),      # register form (not an instruction)
                    bytes.fromhex("f30f1101")):     # movss already
        assert host_cpu.nt_store_rewrite(refused) is None, refused.hex()
    # The rewritten stores run on any host: mov rax, 0x4142434445464748; movq xmm1, rax | store [rcx] | ret
    for prefix, stored in ((0xF3, 4), (0xF2, 8)):
        code = bytes([prefix, 0x0F, 0x2B, 0x09])  # movnts[sd] [rcx], xmm1
        offset, byte = host_cpu.nt_store_rewrite(code)
        f = build(bytes.fromhex("48b84847464544434241") + bytes.fromhex("66480f6ec8"), code[:offset] + byte + code[offset + 1:],
                  b"\xc3", patch=False)
        target = ctypes.create_string_buffer(b"\xee" * 16, 16)
        f(ctypes.addressof(target), 0)
        assert target.raw == b"HGFEDCBA"[:stored] + b"\xee" * (16 - stored), target.raw

    # RDPRU -> RDTSC; NOP. | rdtsc; nop | shl rdx, 32; or rax, rdx; ret: two calls give rising values.
    rdpru = host_cpu.rdpru_rewrite(b"\x0f\x01\xfd")
    assert len(rdpru) == 3 and one(rdpru).mnemonic == iced_x86.Mnemonic.RDTSC and rdpru[2] == 0x90
    assert host_cpu.rdpru_rewrite(b"\x0f\x01\xf9") is None and host_cpu.rdpru_rewrite(host_cpu.CLZERO) is None
    counter = build(b"", rdpru, bytes.fromhex("48c1e2204809d0c3"), patch=False)
    first, second = counter(1, 0), counter(1, 0)
    assert 0 < first < second, (first, second)

    # RDPID: the value RDTSCP leaves in ecx. A thread can move between two calls, so the reference
    # is sampled on both sides. rdtscp; mov eax, ecx; ret
    aux = build(b"", b"", bytes.fromhex("0f01f989c8c3"), patch=False)
    gprs = {"rax": 0, "rcx": 1, "rdx": 2, "r8": 8, "r9": 9, "r10": 10, "r11": 11}
    for name, number in gprs.items():
        rex = bytes([0x41]) if number >= 8 else b""
        instruction = b"\xf3" + rex + b"\x0f\xc7" + bytes([0xF8 | (number & 7)])
        assert one(instruction).mnemonic == iced_x86.Mnemonic.RDPID, instruction.hex()
        move = bytes([0x4C if number >= 8 else 0x48, 0x89, 0xC0 | (number & 7) << 3])  # mov rax, <register>
        f = build(b"", instruction, move + b"\xc3")
        for _ in range(8):
            before, got, after = aux(0, 0), f(0, 0), aux(0, 0)
            if got in (before, after):
                break
        assert got in (before, after), f"rdpid {name}: {got:#x}, rdtscp {before:#x}/{after:#x}"
        if "rdpid" in features:
            native = build(b"", instruction, move + b"\xc3", patch=False)
            assert native(0, 0) in (aux(0, 0), got, before, after), f"native rdpid {name} differs"
    # The stub keeps the registers it borrows and the flags:
    # mov rax, rcx; stc | rdpid r8 | jnc bad; lea rax, [rax + rdx]; ret; bad: xor eax, eax; ret
    kept = build(b"\x48\x89\xc8\xf9", bytes.fromhex("f3410fc7f8"), bytes.fromhex("7305488d0410c331c0c3"))
    assert kept(0x1000, 0x234) == 0x1234, "rdpid stub changed rax, rdx or CF"
    for refused in (bytes.fromhex("f30fc7fc"),      # rdpid rsp
                    bytes.fromhex("f30fc738"),      # memory operand: another instruction
                    bytes.fromhex("0fc7f8ff"),      # no f3: rdseed
                    bytes.fromhex("f30fc7f0")):     # /6
        assert host_cpu.rdpid_stub(refused) is None, refused.hex()
    return len(stores), len(gprs)


# ---- SHA extensions ------------------------------------------------------------------------------
K256 = [0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]


def padded(message):
    return message + b"\x80" + b"\0" * ((55 - len(message)) % 64) + struct.pack(">Q", len(message) * 8)


def sha256_by_instructions(message, ops=host_cpu):
    """SHA-256 the way code written for the extensions computes it: every schedule word past the
    first sixteen through SHA256MSG1 and SHA256MSG2, every round through SHA256RNDS2."""
    add = lambda a, b: host_cpu._pack([x + y for x, y in zip(host_cpu._dwords(a), host_cpu._dwords(b))])  # noqa: E731
    state = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]
    data = padded(message)
    for at in range(0, len(data), 64):
        w = [host_cpu._pack(struct.unpack(">4I", data[at + 16 * i:at + 16 * i + 16])) for i in range(4)]
        for t in range(4, 16):
            high, low = host_cpu._dwords(w[t - 1]), host_cpu._dwords(w[t - 2])
            shifted = host_cpu._pack([low[1], low[2], low[3], high[0]])  # palignr(w[t-1], w[t-2], 4)
            w.append(ops.sha256msg2(add(ops.sha256msg1(w[t - 4], w[t - 3]), shifted), w[t - 1]))
        a, b, c, d, e, f, g, h = state
        abef, cdgh = host_cpu._pack([f, e, b, a]), host_cpu._pack([h, g, d, c])
        for t in range(16):
            wk = add(w[t], host_cpu._pack(K256[4 * t:4 * t + 4]))
            cdgh = ops.sha256rnds2(cdgh, abef, wk)
            abef = ops.sha256rnds2(abef, cdgh, wk >> 64)
        f, e, b, a = host_cpu._dwords(abef)
        h, g, d, c = host_cpu._dwords(cdgh)
        state = [(x + y) & 0xFFFFFFFF for x, y in zip(state, (a, b, c, d, e, f, g, h))]
    return b"".join(struct.pack(">I", x) for x in state)


def sha1_by_instructions(message, ops=host_cpu):
    """SHA-1 through SHA1MSG1, SHA1MSG2, SHA1NEXTE and SHA1RNDS4 in the usual arrangement."""
    state = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    data = padded(message)
    for at in range(0, len(data), 64):
        w = [host_cpu._pack(struct.unpack(">4I", data[at + 16 * i:at + 16 * i + 16])[::-1]) for i in range(4)]
        for t in range(4, 20):
            w.append(ops.sha1msg2(ops.sha1msg1(w[t - 4], w[t - 3]) ^ w[t - 2], w[t - 1]))
        a, b, c, d, e = state
        abcd, previous = host_cpu._pack([d, c, b, a]), None
        for group in range(20):
            if group == 0:
                wk = host_cpu._pack([x + y for x, y in zip(host_cpu._dwords(w[0]), (0, 0, 0, e))])
            else:
                wk = ops.sha1nexte(previous, w[group])
            previous = abcd
            abcd = ops.sha1rnds4(abcd, wk, group // 5)
        d, c, b, a = host_cpu._dwords(abcd)
        e = host_cpu._rol(host_cpu._dwords(previous)[3], 30)
        state = [(x + y) & 0xFFFFFFFF for x, y in zip(state, (a, b, c, d, e))]
    return b"".join(struct.pack(">I", x) for x in state)


SHA_FORMS = {  # mnemonic: opcode bytes after 0F
    "sha1nexte": b"\x38\xc8", "sha1msg1": b"\x38\xc9", "sha1msg2": b"\x38\xca", "sha256rnds2": b"\x38\xcb",
    "sha256msg1": b"\x38\xcc", "sha256msg2": b"\x38\xcd", "sha1rnds4": b"\x3a\xcc"}


def sha_cases(native):
    import hashlib
    import random
    messages = (b"", b"abc", b"a" * 200, bytes(range(256)) * 3)
    for message in messages:
        assert sha256_by_instructions(message) == hashlib.sha256(message).digest(), f"sha-256 of {len(message)} bytes"
        assert sha1_by_instructions(message) == hashlib.sha1(message).digest(), f"sha-1 of {len(message)} bytes"

    # Negative controls: one changed detail in one operation must change the digest.
    class Wrong:
        sha256msg1, sha256msg2, sha1msg1, sha1msg2, sha1nexte = (host_cpu.sha256msg1, host_cpu.sha256msg2, host_cpu.sha1msg1,
                                                                 host_cpu.sha1msg2, host_cpu.sha1nexte)
        sha256rnds2 = staticmethod(lambda dest, source, xmm0: host_cpu.sha256rnds2(dest, source, xmm0 >> 32))
        sha1rnds4 = staticmethod(lambda dest, source, function: host_cpu.sha1rnds4(dest, source, function ^ 1))
    assert sha256_by_instructions(b"abc", Wrong) != hashlib.sha256(b"abc").digest(), "a wrong round constant order passed"
    assert sha1_by_instructions(b"abc", Wrong) != hashlib.sha1(b"abc").digest(), "a wrong round function passed"

    # sha_apply: operand forms, on a made-up register file and memory.
    rng = random.Random(0x5AA5)
    xmm = [rng.getrandbits(128) for _ in range(16)]
    gpr = [0x1000 * (i + 1) for i in range(16)]
    memory = {}

    def read_memory(address, size):
        return memory.get(address)

    def apply(code, rip=0x7000):
        return host_cpu.sha_apply(code, rip, lambda n: gpr[n], lambda n: xmm[n], read_memory)

    operand = rng.getrandbits(128)
    # sha256msg1 xmm3, xmm5
    assert apply(bytes.fromhex("0f38ccdd")) == (4, 3, host_cpu.sha256msg1(xmm[3], xmm[5]))
    # sha1rnds4 xmm9, xmm12, 2 (REX.RB)
    assert apply(bytes.fromhex("450f3acccc02")) == (6, 9, host_cpu.sha1rnds4(xmm[9], xmm[12], 2))
    # sha256rnds2 xmm2, [rbx + rsi*4 + 0x20]: xmm0 is the implicit third operand
    memory[gpr[3] + gpr[6] * 4 + 0x20] = operand.to_bytes(16, "little")
    assert apply(bytes.fromhex("0f38cb54b320")) == (6, 2, host_cpu.sha256rnds2(xmm[2], operand, xmm[0]))
    # sha1msg2 xmm1, [rip + 0x100]: relative to the end of the 8-byte instruction
    memory[0x7000 + 8 + 0x100] = operand.to_bytes(16, "little")
    assert apply(bytes.fromhex("0f38ca0d00010000")) == (8, 1, host_cpu.sha1msg2(xmm[1], operand))
    # Refused: an unreadable operand, a segment override, and other instructions.
    assert apply(bytes.fromhex("0f38cc5510")) is None                      # [rbp + 0x10] is not in the made-up memory
    assert apply(bytes.fromhex("640f38cc1b")) is None                      # fs:
    assert apply(bytes.fromhex("660f38dcc1")) is None                      # aesenc
    assert apply(bytes.fromhex("0f0b9090")) is None                        # ud2

    compared = 0
    if native:
        # movdqu xmm1, [rcx]; movdqu xmm2, [rdx]; movdqu xmm0, [rdx + 16] | op xmm1, xmm2 | movdqu [rcx], xmm1; ret
        before = bytes.fromhex("f30f6f09f30f6f12f30f6f4210")
        for name, opcode in SHA_FORMS.items():
            code = b"\x0f" + opcode + b"\xca" + (b"\x01" if name == "sha1rnds4" else b"")
            f = build(before, code, bytes.fromhex("f30f7f09c3"), patch=False)
            for _ in range(16):
                dest, source, implicit = (rng.getrandbits(128) for _ in range(3))
                first = ctypes.create_string_buffer(dest.to_bytes(16, "little"), 16)
                second = ctypes.create_string_buffer(source.to_bytes(16, "little") + implicit.to_bytes(16, "little"), 32)
                f(ctypes.addressof(first), ctypes.addressof(second))
                operation = getattr(host_cpu, name)
                expected = (operation(dest, source, 1) if name == "sha1rnds4" else
                            operation(dest, source, implicit) if name == "sha256rnds2" else operation(dest, source))
                assert int.from_bytes(first.raw, "little") == expected, f"native {name} differs from the model"
                compared += 1
    return len(messages), compared


def main():
    if platform.machine().lower() not in ("x86_64", "amd64"):
        print("startup_host_cpu: SKIP, host is not x86-64")
        return 77
    features = host_features()
    rewrites()

    planned = []
    plan_ranges(planned)
    assert not planned, "; ".join(planned)

    assert clzero_case(), "clzero stub"
    assert clzero_moves_rip_relative(), "an instruction moved behind the clzero stub lost its rip-relative operand"
    if "clzero" in features:
        assert clzero_case(patch=False), "native clzero differs from the check"
    # Negative controls: a stub that leaves the last quadword, and one that zeroes the next line too.
    short = host_cpu.CLZERO_STUB.replace(bytes.fromhex("48c7403800000000"), b"")
    long = host_cpu.CLZERO_STUB[:-2] + bytes.fromhex("48c7404000000000") + host_cpu.CLZERO_STUB[-2:]
    assert short != host_cpu.CLZERO_STUB and not clzero_case(short), "a 56-byte clzero stub passed"
    assert not clzero_case(long), "a 72-byte clzero stub passed"

    checked = sse4a_cases("sse4a" in features)
    assert sse4a_keeps_registers_and_flags(), "sse4a stub changed GPRs or CF"
    # Negative controls: wrong bit-offset or wrong-register stubs must not pass.
    wrong = host_cpu.sse4a_stub(bytes.fromhex("f20f78c11018"))
    mutant = insertq_function(0, 1, 16, 16, stub=wrong)
    a, b = VALUES[0]
    assert mutant(a, b) != insertq_model(a, b, 16, 16), "a stub for another bit offset passed"
    wrong_rex_reg = host_cpu.sse4a_stub(bytes.fromhex("f2450f78c41010"))  # xmm8, xmm12 instead of xmm9, xmm12
    mutant_rex = insertq_function(9, 12, 16, 16, stub=wrong_rex_reg)
    assert mutant_rex(a, b) != insertq_model(a, b, 16, 16), "a REX stub for another register passed"
    wrong_reg_form = host_cpu.sse4a_stub(bytes.fromhex("66450f79c9"))  # extrq xmm9, xmm9 instead of xmm9, xmm12
    mutant_reg = sse4a_128_function(bytes.fromhex("66450f79cc"), 9, 12, stub=wrong_reg_form)
    dst_m = ctypes.create_string_buffer(struct.pack("<QQ", a, 0), 16)
    src_m = ctypes.create_string_buffer(struct.pack("<QQ", _ctrl_word(16, 16), 0), 16)
    mutant_reg(ctypes.addressof(dst_m), ctypes.addressof(src_m))
    assert struct.unpack("<Q", dst_m.raw[:8])[0] != extrq_model(a, 16, 16), "a 0F 79 stub for wrong registers passed"

    stores, rdpid_registers = more_rewrites(features)
    digests, native_sha = sha_cases("sha" in features)

    native = ", ".join(sorted(features)) or "none"
    print(f"startup_host_cpu: PASS ({checked} insertq/extrq results, clzero with two moved-instruction forms, "
          f"5 clwb and 2 wait rewrites, clwb/clflushopt NOPs for every addressing form, "
          f"{stores} non-temporal stores, rdpru, rdpid into {rdpid_registers} registers, "
          f"sha-1 and sha-256 of {digests} messages through the seven sha operations with {native_sha} native results; "
          f"native comparison: {native})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
