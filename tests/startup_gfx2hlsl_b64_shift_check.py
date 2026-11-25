"""The 64-bit vector shifts in tools/startup/gfx2hlsl.py (issue #1817).

usage: startup_gfx2hlsl_b64_shift_check.py

`v_lshlrev_b64`, `v_lshrrev_b64` and `v_ashrrev_i64` had no lowering, so the census form
`v_lshrrev_b64 v[10:11], 0, 0` was refused (#870, issue #1817). A 64-bit value is two 32-bit words, so the
lowering cannot be a single 32-bit shift: the count must never reach 32, and an in-place destination must keep
both original words. Both are handled by computing through a `uint2` helper that takes both words as values.

The instruction bytes are **constructed here** from the field layout the checked-in decoder reads
(`tools/disasm_rdna2.py`, VOP3A branch, opcodes 0x2ff/0x300/0x301): prefix `0b110101`, opcode in `w0[25:16]`,
the low destination register in `w0[7:0]` (the high word is the next register), src0 in `w1[8:0]` (the shift
count) and src1 in `w1[17:9]` (the 64-bit value).
"""
import os
import struct
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, '..', 'tools'))
sys.path.insert(0, os.path.join(here, '..', 'tools', 'startup'))
import gfx2hlsl as G  # noqa: E402

failures = []


def check(label, condition, detail=''):
    print(f"{label}: {'ok' if condition else 'MISMATCH'}")
    if not condition:
        failures.append(label)
        if detail:
            print('  ' + detail)


VOP3A_LSHLREV_B64 = 0x2FF
VOP3A_LSHRREV_B64 = 0x300
VOP3A_ASHRREV_I64 = 0x301
INLINE_ZERO = 128          # the inline integer constant 0, which the census form uses for both sources
INLINE_40 = 128 + 40       # the inline integer constant 40
V8, V10 = 0x108, 0x10A
S0 = 0x000


def encode_vop3a(op, dst_low, src0, src1):
    """Encode a 64-bit-shift VOP3A word; src codes 0..127 are SGPRs, 128..192 inline integers, 256+n VGPR n."""
    w0 = (0b110101 << 26) | ((op & 0x3FF) << 16) | (dst_low & 0xFF)
    w1 = (src0 & 0x1FF) | ((src1 & 0x1FF) << 9)
    return struct.pack('<II', w0, w1)


class Pipeline:
    """Just enough pipeline state for one shift."""

    def __init__(self):
        self.resources, self.notes, self.modified = {}, set(), set()


class Stage(G.Stage):
    """A Stage with no pipeline: enough state for a decoded instruction to be translated."""

    def __init__(self):
        self.lines, self.sym = [], {}
        self.used_s, self.used_v, self.uniform_v = set(), set(), set()
        self.exec_known = True
        self.exports = {}
        self.pipe = Pipeline()


def translate(code):
    decoded = G.decode(code)
    stage = Stage()
    for _offset, _size, name, asm in decoded:
        stage.one(name, asm)
    return [asm for _o, _s, _n, asm in decoded], ' '.join(stage.lines), stage


# 1. The encoder produces the form the decoder reads; a layout change shows up here first.
asm, emitted, _stage = translate(encode_vop3a(VOP3A_LSHRREV_B64, 10, V8, V8))
check('encodes and decodes the 64-bit right shift', asm == ['v_lshrrev_b64 v[10:11], v8, v[8:9]'], repr(asm))

# 2. The count is masked by the helper, and both destination words are written from one packed result, so an
#    in-place shift (the census shape) reads the original words.
check('the right shift uses the helper and writes both words',
      'shr64u(v8, v9, v8)' in emitted and 'v10 = t.x;' in emitted and 'v11 = t.y;' in emitted, emitted)

# 3. The left shift and the arithmetic right shift are separate helpers (different high-word fill).
asm, emitted, _stage = translate(encode_vop3a(VOP3A_LSHLREV_B64, 10, V8, V8))
check('the left shift uses its helper', 'shl64(v8, v9, v8)' in emitted, emitted)
asm, emitted, _stage = translate(encode_vop3a(VOP3A_ASHRREV_I64, 10, V8, V8))
check('the arithmetic right shift uses its helper', 'shr64i(v8, v9, v8)' in emitted, emitted)

# 4. The census form: both sources are the inline integer zero, so the result is a known zero pair.
asm, emitted, _stage = translate(encode_vop3a(VOP3A_LSHRREV_B64, 10, INLINE_ZERO, INLINE_ZERO))
check('encodes and decodes the census form', asm == ['v_lshrrev_b64 v[10:11], 0, 0'], repr(asm))
check('the census form lowers to a zero pair',
      'shr64u(0u, 0u, 0x00000000u)' in emitted, emitted)

# 5. An inline constant count reaches the helper as that constant, not as a register.
asm, emitted, _stage = translate(encode_vop3a(VOP3A_LSHRREV_B64, 10, INLINE_40, V8))
check('an inline constant count is read as a constant', 'sizeof' not in emitted and '0x00000028u' in emitted, emitted)

# 6. The three helpers are in the preamble the generated shader is built with, and the count is masked there
#    (a 32-bit HLSL shift by 32 or more is undefined, so the helpers must clamp it).
pipe = object.__new__(G.Pipeline)
pipe.uds, pipe.resources, pipe.samplers, pipe.uses_sampler_heap = [], {}, {}, False
common = ' '.join(pipe.common())
check('the shift helpers are emitted in the common preamble',
      all(name in common for name in ('uint2 shl64(', 'uint2 shr64u(', 'uint2 shr64i(')), common[:240])
check('the helpers mask the count', common.count('sh &= 63u;') == 3, common[:240])

if failures:
    print('FAIL')
    for text in failures:
        print('  ' + text)
    sys.exit(1)
print('PASS')