"""Rewrites for x64 instructions a console executable uses and the host CPU may lack.

The startup debugger (debug_launch.py, X2W_HOST_CPU=1) calls these when the main image raises an
illegal-instruction exception. Each site is rewritten once, in the running process only:

- CLWB m8 (66 [REX] 0F AE /6) becomes CLFLUSHOPT m8 (/7): same bytes apart from one ModRM bit.
  Both write the line back; CLFLUSHOPT also evicts it, which no program can observe. A host
  older than CLFLUSHOPT faults on that as well; the site then becomes one NOP of the same length
  (flush_nop), and so does every later CLWB. A write-back hint has no effect a process can observe.
- MONITORX (0F 01 FA) becomes a 3-byte NOP and MWAITX (0F 01 FB) becomes PAUSE; NOP. MWAITX may
  return at any time, so a caller already has to re-check its condition in a loop.
- CLZERO (0F 01 FC) and the SSE4a forms INSERTQ (F2 [REX] 0F 78 /r ib ib, F2 [REX] 0F 79 /r)
  and EXTRQ (66 [REX] 0F 78 /0 ib ib, 66 [REX] 0F 79 /r) become a jump to a stub with the same
  effect. The stubs keep every other register and the flags. Instructions the 5-byte jump
  overwrites are moved behind the stub (plan_replacement).

- MOVNTSS and MOVNTSD (F3/F2 [REX] 0F 2B /r) become MOVSS and MOVSD stores (opcode 11): the same
  bytes reach memory without the non-temporal hint.
- RDPRU (0F 01 FD) becomes RDTSC; NOP: the time-stamp counter stands in for MPERF and APERF.
- RDPID r64 becomes a jump to a stub that runs RDTSCP and moves its ecx (the same IA32_TSC_AUX
  value) to the target register.
- The SHA extensions (SHA1RNDS4, SHA1NEXTE, SHA1MSG1, SHA1MSG2, SHA256RNDS2, SHA256MSG1,
  SHA256MSG2) are not rewritten: sha_apply performs one on register values the debugger reads from
  the stopped thread, at every hit. That is a debugger round trip per instruction.

Limits: bits 127:64 of an SSE4a destination, undefined after the original instruction, keep their
value. A thread that is inside the overwritten bytes of a jump site while another thread faults on
it is not accounted for.
"""
import struct

CLZERO = b"\x0f\x01\xfc"

# push rax; push rcx; mov ecx, 6; shrx rax, rax, rcx; shlx rax, rax, rcx (rax &= ~63, flags kept);
# mov qword [rax + 0..56], 0; pop rcx; pop rax
CLZERO_STUB = (bytes.fromhex("5051b906000000c4e2f3f7c0c4e2f1f7c048c70000000000") +
               b"".join(bytes.fromhex("48c740%02x00000000" % d) for d in range(8, 64, 8)) + bytes.fromhex("5958"))


def clwb_rewrite(code):
    """(offset, byte) that turns CLWB m8 at the start of code into CLFLUSHOPT m8, or None."""
    if len(code) < 4 or code[0] != 0x66:
        return None
    at = 2 if 0x40 <= code[1] <= 0x4F else 1
    if code[at:at + 2] != b"\x0f\xae" or len(code) <= at + 2:
        return None
    modrm = code[at + 2]
    if (modrm >> 3) & 7 != 6 or modrm >> 6 == 3:
        return None
    return at + 2, bytes([modrm | 0x08])


def flush_length(code):
    """Length of the CLWB m8 or CLFLUSHOPT m8 at the start of code (66 [REX] 0F AE /6 or /7), or None."""
    if len(code) < 4 or code[0] != 0x66:
        return None
    at = 2 if 0x40 <= code[1] <= 0x4F else 1
    if code[at:at + 2] != b"\x0f\xae" or len(code) <= at + 2:
        return None
    modrm = code[at + 2]
    mod, rm = modrm >> 6, modrm & 7
    if (modrm >> 3) & 7 not in (6, 7) or mod == 3:
        return None
    length = at + 3
    if rm == 4:
        if len(code) <= length:
            return None
        if mod == 0 and code[length] & 7 == 5:
            length += 4  # SIB without a base: disp32
        length += 1
    elif mod == 0 and rm == 5:
        length += 4      # rip-relative
    length += (0, 1, 4)[mod]
    return length if len(code) >= length else None


# One NOP instruction for each length a CLWB m8 can have.
NOPS = {4: "0f1f4000", 5: "0f1f440000", 6: "660f1f440000", 7: "0f1f8000000000", 8: "0f1f840000000000",
        9: "660f1f840000000000", 10: "662e0f1f840000000000"}


def flush_nop(code):
    """A single NOP as long as the CLWB m8 or CLFLUSHOPT m8 at the start of code, or None."""
    length = flush_length(code)
    return bytes.fromhex(NOPS[length]) if length in NOPS else None


def wait_rewrite(code):
    """Three replacement bytes for MONITORX or MWAITX at the start of code, or None."""
    if code[:3] == b"\x0f\x01\xfa":
        return b"\x0f\x1f\x00"  # nop dword ptr [rax]
    if code[:3] == b"\x0f\x01\xfb":
        return b"\xf3\x90\x90"  # pause; nop
    return None


def nt_store_rewrite(code):
    """(offset, byte) that turns MOVNTSS/MOVNTSD m, xmm (F3/F2 [REX] 0F 2B /r) into MOVSS/MOVSD m, xmm
    (opcode 11), or None. The non-temporal hint is dropped; the bytes stored are the same."""
    if len(code) < 4 or code[0] not in (0xF2, 0xF3):
        return None
    at = 2 if 0x40 <= code[1] <= 0x4F else 1
    if code[at:at + 2] != b"\x0f\x2b" or len(code) <= at + 2 or code[at + 2] >> 6 == 3:
        return None
    return at + 1, b"\x11"


def rdpru_rewrite(code):
    """Three bytes for RDPRU (0F 01 FD), or None: RDTSC and a NOP. RDPRU returns MPERF (ecx 0) or
    APERF (ecx 1) in edx:eax; the time-stamp counter stands in for both, so their ratio reads 1."""
    return b"\x0f\x31\x90" if code[:3] == b"\x0f\x01\xfd" else None


def rdpid_stub(code):
    """(length, stub) for RDPID r64 (F3 [REX] 0F C7 /7, register form), or None. The stub runs RDTSCP,
    whose ecx is the same IA32_TSC_AUX value, and keeps rax, rdx and rcx unless one is the target."""
    if len(code) < 4 or code[0] != 0xF3:
        return None
    rex = code[1] if 0x40 <= code[1] <= 0x4F else 0
    at = 2 if rex else 1
    if code[at:at + 2] != b"\x0f\xc7" or len(code) <= at + 2:
        return None
    modrm = code[at + 2]
    if modrm >> 6 != 3 or (modrm >> 3) & 7 != 7:
        return None
    dest = (modrm & 7) | (8 if rex & 1 else 0)
    if dest == 4:
        return None  # rsp: the stub's own stack
    # push rax; push rcx; push rdx; rdtscp
    stub = b"\x50\x51\x52\x0f\x01\xf9"
    saved = {2: 0, 1: 8, 0: 16}  # where rdx, rcx and rax sit on the stack
    if dest in saved:
        stub += bytes([0x48, 0x89, 0x4C, 0x24, saved[dest]])              # mov [rsp + slot], rcx
    else:
        stub += bytes([0x48 | (1 if dest >= 8 else 0), 0x89, 0xC8 | (dest & 7)])  # mov dest, rcx
    return at + 3, stub + b"\x5a\x59\x58"                                 # pop rdx; pop rcx; pop rax


def _xmm_modrm(prefix, opcode, xmm, gpr=0, imm=()):
    rex = 0x48 | (4 if xmm >= 8 else 0) | (1 if gpr >= 8 else 0)
    return bytes([prefix, rex, *opcode, 0xC0 | (xmm & 7) << 3 | (gpr & 7), *imm])


def _sse4a_decode(code):
    """(length, insertq, reg_form, dest, src, width, offset) for SSE4a INSERTQ/EXTRQ, or None."""
    if len(code) < 4 or code[0] not in (0xF2, 0x66):
        return None
    rex = code[1] if 0x40 <= code[1] <= 0x4F else 0
    at = 2 if rex else 1
    if len(code) <= at + 2 or code[at] != 0x0F or code[at + 1] not in (0x78, 0x79):
        return None
    insertq = code[0] == 0xF2
    reg_form = code[at + 1] == 0x79
    length = at + (3 if reg_form else 5)
    if len(code) < length:
        return None
    modrm = code[at + 2]
    if modrm >> 6 != 3:
        return None
    reg, rm = (modrm >> 3) & 7, modrm & 7
    if not insertq and not reg_form:
        if reg:
            return None
        dest = src = rm | (8 if rex & 1 else 0)
    else:
        dest = reg | (8 if rex & 4 else 0)
        src = rm | (8 if rex & 1 else 0)
    if reg_form:
        return length, insertq, True, dest, src, None, None
    width, offset = (code[at + 3] & 63) or 64, code[at + 4] & 63
    if offset + width > 64:
        return None  # undefined result on the processor
    return length, insertq, False, dest, src, width, offset


def _sse4a_stub_with_length(code):
    """(length, stub) for immediate or register INSERTQ/EXTRQ at the start of code, or None."""
    decoded = _sse4a_decode(code)
    if not decoded:
        return None
    length, insertq, reg_form, dest, src, width, offset = decoded
    movq_src = _xmm_modrm(0x66, (0x0F, 0x7E), src, 0)                  # movq rax, xmm_src
    movq_dest = _xmm_modrm(0x66, (0x0F, 0x7E), dest, 2)                # movq rdx, xmm_dest
    pinsrq_dest = _xmm_modrm(0x66, (0x0F, 0x3A, 0x22), dest, 2, (0,))  # pinsrq xmm_dest, rdx, 0
    if not reg_form:
        mask = (1 << width) - 1
        if insertq:
            body = (movq_src +
                    b"\x48\xb9" + struct.pack("<Q", mask) + b"\x48\x21\xc8" +
                    (bytes([0x48, 0xC1, 0xE0, offset]) if offset else b"") +
                    movq_dest +
                    b"\x48\xb9" + struct.pack("<Q", ~(mask << offset) & (1 << 64) - 1) + b"\x48\x21\xca" +
                    b"\x48\x09\xc2")
        else:
            body = (movq_dest +
                    (bytes([0x48, 0xC1, 0xEA, offset]) if offset else b"") +
                    b"\x48\xb9" + struct.pack("<Q", mask) + b"\x48\x21\xca")
        return length, b"\x9c\x50\x51\x52" + body + pinsrq_dest + b"\x5a\x59\x58\x9d"
    if insertq:
        # pushfq; push rax; push rcx; push rdx; push rsi; push rdi
        # pextrq rdi, xmm_src, 1; movq rax, xmm_src; movq rdx, xmm_dest
        # mov ecx, edi; neg ecx; mov rsi, -1; shr rsi, cl; and rax, rsi
        # mov rcx, rdi; shr rcx, 8; shl rax, cl; shl rsi, cl; not rsi; and rdx, rsi; or rdx, rax
        body = (_xmm_modrm(0x66, (0x0F, 0x3A, 0x16), src, 7, (1,)) +
                movq_src + movq_dest +
                bytes.fromhex("89f9f7d948c7c6ffffffff48d3ee4821f04889f948c1e90848d3e048d3e648f7d64821f24809c2"))
        return length, b"\x9c\x50\x51\x52\x56\x57" + body + pinsrq_dest + b"\x5f\x5e\x5a\x59\x58\x9d"
    # EXTRQ xmm_dest, xmm_src: ctrl in xmm_src[63:0], value in xmm_dest[63:0].
    # movq rax, xmm_src; movq rdx, xmm_dest; mov rcx, rax; shr rcx, 8; shr rdx, cl
    # mov ecx, eax; neg ecx; mov rax, -1; shr rax, cl; and rdx, rax
    body = (movq_src + movq_dest +
            bytes.fromhex("4889c148c1e90848d3ea89c1f7d948c7c0ffffffff48d3e84821c2"))
    return length, b"\x9c\x50\x51\x52" + body + pinsrq_dest + b"\x5a\x59\x58\x9d"


def sse4a_stub(code):
    """Stub for immediate or register INSERTQ/EXTRQ at the start of code, or None."""
    result = _sse4a_stub_with_length(code)
    return result[1] if result else None


def stub_for(code):
    """(instruction length, stub bytes) for an instruction that needs a stub, or None."""
    if code[:3] == CLZERO:
        return 3, CLZERO_STUB
    rdpid = rdpid_stub(code)
    if rdpid:
        return rdpid
    return _sse4a_stub_with_length(code)


def plan_replacement(code, site, length, stub, stub_address):
    """Replace the instruction of `length` bytes at `site` (whose bytes start `code`) with `stub`.

    Returns (bytes for stub_address, bytes for site) or None. The site gets a 5-byte JMP rel32 padded
    with NOPs to an instruction boundary. The instructions it overwrites besides the replaced one
    are re-encoded for their new address behind the stub, followed by a jump back. Needs iced-x86
    only when instructions have to move.
    """
    total, moved = length, b""
    if length < 5:
        import iced_x86
        decoder = iced_x86.Decoder(64, bytes(code[length:]), ip=site + length)
        instructions = []
        while total < 5:
            if not decoder.can_decode:
                return None
            instruction = decoder.decode()
            if instruction.code == iced_x86.Code.INVALID:
                return None
            instructions.append(instruction)
            total += instruction.len
        instructions.append(iced_x86.Instruction.create_branch(iced_x86.Code.JMP_REL32_64, site + total))
        encoder = iced_x86.BlockEncoder(64)
        encoder.add_many(instructions)
        moved = encoder.encode(stub_address + len(stub))
    else:
        back = site + total - (stub_address + len(stub) + 5)
        if not -2**31 <= back < 2**31:
            return None
        moved = b"\xe9" + struct.pack("<i", back)
    forward = stub_address - (site + 5)
    if not -2**31 <= forward < 2**31:
        return None
    return stub + moved, b"\xe9" + struct.pack("<i", forward) + b"\x90" * (total - 5)


# ---- SHA extensions ------------------------------------------------------------------------------
# No stub: the startup debugger performs these on the stopped thread's context at every hit
# (sha_apply). 128-bit values are Python integers; dword 0 is bits 31:0, as in an XMM register.
# The operations follow the Intel SDM's pseudocode for each instruction.
M32 = 0xFFFFFFFF


def _dwords(value):
    return [value >> (32 * i) & M32 for i in range(4)]


def _pack(dwords):
    return sum((d & M32) << (32 * i) for i, d in enumerate(dwords))


def _rol(value, count):
    return (value << count | value >> (32 - count)) & M32


def _ror(value, count):
    return (value >> count | value << (32 - count)) & M32


def sha1rnds4(dest, source, function):
    d, c, b, a = _dwords(dest)
    w = _dwords(source)[::-1]  # W0 (with E added) first
    constant = (0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xCA62C1D6)[function & 3]

    def f(x, y, z):
        if function & 3 == 0:
            return (x & y) ^ (~x & z)
        if function & 3 == 2:
            return (x & y) ^ (x & z) ^ (y & z)
        return x ^ y ^ z

    e = 0
    for i in range(4):
        a, b, c, d, e = (f(b, c, d) + _rol(a, 5) + w[i] + e + constant) & M32, a, _rol(b, 30), c, d
    return _pack([d, c, b, a])


def sha1nexte(dest, source):
    s = _dwords(source)
    return _pack([s[0], s[1], s[2], s[3] + _rol(_dwords(dest)[3], 30)])


def sha1msg1(dest, source):
    w3, w2, w1, w0 = _dwords(dest)
    s = _dwords(source)
    w5, w4 = s[2], s[3]
    return _pack([w5 ^ w3, w4 ^ w2, w3 ^ w1, w2 ^ w0])


def sha1msg2(dest, source):
    d = _dwords(dest)
    w15, w14, w13 = _dwords(source)[:3]
    w16 = _rol(d[3] ^ w13, 1)
    w17 = _rol(d[2] ^ w14, 1)
    w18 = _rol(d[1] ^ w15, 1)
    w19 = _rol(d[0] ^ w16, 1)
    return _pack([w19, w18, w17, w16])


def sha256rnds2(dest, source, xmm0):
    h, g, d, c = _dwords(dest)
    f, e, b, a = _dwords(source)
    for wk in _dwords(xmm0)[:2]:
        t1 = (h + (_ror(e, 6) ^ _ror(e, 11) ^ _ror(e, 25)) + ((e & f) ^ (~e & g)) + wk) & M32
        t2 = ((_ror(a, 2) ^ _ror(a, 13) ^ _ror(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) & M32
        a, b, c, d, e, f, g, h = (t1 + t2) & M32, a, b, c, (d + t1) & M32, e, f, g
    return _pack([f, e, b, a])


def _sigma0(x):
    return _ror(x, 7) ^ _ror(x, 18) ^ x >> 3


def _sigma1(x):
    return _ror(x, 17) ^ _ror(x, 19) ^ x >> 10


def sha256msg1(dest, source):
    w = _dwords(dest) + [_dwords(source)[0]]
    return _pack([w[i] + _sigma0(w[i + 1]) for i in range(4)])


def sha256msg2(dest, source):
    d = _dwords(dest)
    s = _dwords(source)
    w16 = (d[0] + _sigma1(s[2])) & M32
    w17 = (d[1] + _sigma1(s[3])) & M32
    return _pack([w16, w17, d[2] + _sigma1(w16), d[3] + _sigma1(w17)])


def sha_apply(code, rip, read_gpr, read_xmm, read_memory):
    """Performs the SHA instruction at the start of code. Returns (length, xmm index, new value), or
    None when it is not one, the operands are not the legacy forms, or a memory operand cannot be
    read. read_gpr(n) and read_xmm(n) return register values; read_memory(address, 16) returns 16
    bytes or fewer. Needs iced-x86."""
    import iced_x86
    instruction = iced_x86.Decoder(64, bytes(code), ip=rip).decode()
    operations = {iced_x86.Mnemonic.SHA1RNDS4: sha1rnds4, iced_x86.Mnemonic.SHA1NEXTE: sha1nexte,
                  iced_x86.Mnemonic.SHA1MSG1: sha1msg1, iced_x86.Mnemonic.SHA1MSG2: sha1msg2,
                  iced_x86.Mnemonic.SHA256RNDS2: sha256rnds2, iced_x86.Mnemonic.SHA256MSG1: sha256msg1,
                  iced_x86.Mnemonic.SHA256MSG2: sha256msg2}
    operation = operations.get(instruction.mnemonic)
    if operation is None or instruction.op0_kind != iced_x86.OpKind.REGISTER:
        return None
    xmm0, rax, rip_register = iced_x86.Register.XMM0, iced_x86.Register.RAX, iced_x86.Register.RIP
    dest = instruction.op0_register - xmm0
    if not 0 <= dest <= 15:
        return None
    if instruction.op1_kind == iced_x86.OpKind.REGISTER:
        source = read_xmm(instruction.op1_register - xmm0)
    elif instruction.op1_kind == iced_x86.OpKind.MEMORY:
        if instruction.segment_prefix != iced_x86.Register.NONE:
            return None  # fs: and gs: bases are not known here
        address = instruction.memory_displacement  # absolute already for a rip-relative operand
        base, index = instruction.memory_base, instruction.memory_index
        if base not in (iced_x86.Register.NONE, rip_register):
            if not 0 <= base - rax <= 15:
                return None
            address += read_gpr(base - rax)
        if index != iced_x86.Register.NONE:
            if not 0 <= index - rax <= 15:
                return None
            address += read_gpr(index - rax) * instruction.memory_index_scale
        raw = read_memory(address & (1 << 64) - 1, 16)
        if raw is None or len(raw) != 16:
            return None
        source = int.from_bytes(raw, "little")
    else:
        return None
    value = read_xmm(dest)
    if instruction.mnemonic == iced_x86.Mnemonic.SHA1RNDS4:
        result = sha1rnds4(value, source, instruction.immediate8)
    elif instruction.mnemonic == iced_x86.Mnemonic.SHA256RNDS2:
        result = sha256rnds2(value, source, read_xmm(0))
    else:
        result = operation(value, source)
    return instruction.len, dest, result
