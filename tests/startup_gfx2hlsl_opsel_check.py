"""`op_sel` on a translated float operation in tools/startup/gfx2hlsl.py (issue #1754).

usage: startup_gfx2hlsl_opsel_check.py

`Stage.set_vf()` used to treat an `op_sel` modifier as a no-op: it accepted the modifier and emitted the same
text as without it. `op_sel` selects which half of a packed source an operation reads, so dropping it silently
changes the operand. The path now refuses it.

The test also pins the exact expressions of the byte-extracting conversions, whose shift amounts were not
covered by mutation checks before (review on the conversion-modifier change).

The instruction bytes are **constructed here** from the field layout the checked-in decoder reads
(`tools/disasm_rdna2.py`, VOP1 / VOP2 / VOP3A branches):
  VOP3A w0: [31:26] = 0b110101, [25:16] opcode, [15] clamp, [14:11] op_sel[3:0], [10:8] abs, [7:0] vdst
  VOP2-as-VOP3 w0 as above with opcode 0x100 + VOP2 opcode, [14:11] op_sel[3:0]
  VOP1-as-VOP3 w0 as above with opcode 0x180 + VOP1 opcode
  w1: [8:0] src0, [17:9] src1, [26:18] src2
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


VOP2_E64_BASE = 0x100
VOP1_E64_BASE = 0x180
VOP2_ADD_F32 = 0x03
VOP3A_MAD_F32 = 0x141
VOP1_CVT_F32_UBYTE0 = 0x11
VOP1_CVT_F32_UBYTE1 = 0x12
VOP1_CVT_F32_UBYTE2 = 0x13
VOP1_CVT_F32_UBYTE3 = 0x14
VOP1_CVT_F32_U32 = 0x06
VOP1_CVT_F32_I32 = 0x05
V1 = 0x101
V2 = 0x102


def _word0(opcode, vdst, *, op_sel=(0, 0, 0, 0), abs_bits=(0, 0, 0), clamp=0):
    w0 = ((0b110101 << 26) | ((opcode & 0x3FF) << 16) | ((clamp & 1) << 15) | (vdst & 0xFF))
    for i, bit in enumerate(op_sel):
        w0 |= (bit & 1) << (11 + i)
    for i, bit in enumerate(abs_bits):
        w0 |= (bit & 1) << (8 + i)
    return w0


def encode_vop2(op, vdst, src0, src1, *, op_sel=(0, 0, 0, 0), abs_bits=(0, 0, 0)):
    """Encode a VOP2 instruction in its VOP3A (e64) form."""
    w1 = (src0 & 0x1FF) | ((src1 & 0x1FF) << 9)
    return struct.pack('<II', _word0(VOP2_E64_BASE + op, vdst, op_sel=op_sel, abs_bits=abs_bits), w1)


def encode_vop1(op, vdst, src0, *, op_sel=(0, 0, 0, 0), abs_bits=(0, 0, 0)):
    """Encode a VOP1 instruction in its VOP3A (e64) form."""
    return struct.pack('<II', _word0(VOP1_E64_BASE + op, vdst, op_sel=op_sel, abs_bits=abs_bits), src0 & 0x1FF)


def encode_vopc(op, sdst, src0, src1, *, op_sel=(0, 0, 0, 0), clamp=0, omod=0):
    """Encode a VOPC comparison in its VOP3A form; sdst is the scalar destination (vcc)."""
    w0 = _word0(op, sdst, op_sel=op_sel, clamp=clamp)
    w1 = ((src0 & 0x1FF) | ((src1 & 0x1FF) << 9) | ((omod & 0x3) << 27))
    return struct.pack('<II', w0, w1)


def encode_vop3a(op, vdst, srcs, *, op_sel=(0, 0, 0, 0), abs_bits=(0, 0, 0)):
    """Encode a native VOP3A instruction (three sources)."""
    w1 = 0
    for i, src in enumerate(srcs):
        w1 |= (src & 0x1FF) << (9 * i)
    return struct.pack('<II', _word0(op, vdst, op_sel=op_sel, abs_bits=abs_bits), w1)


class Pipeline:
    """Just enough pipeline state for one vector operation."""

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
    return [asm for _o, _s, _n, asm in decoded], ' '.join(stage.lines)


def refused(code):
    try:
        translate(code)
    except G.Unsupported:
        return True
    return False


# 1. The encoders produce what the decoder reads; a layout change shows up here first.
asm, emitted = translate(encode_vop2(VOP2_ADD_F32, 1, V1, V2))
check('encodes a plain float add', asm == ['v_add_f32_e64 v1, v1, v2'], repr(asm))
check('plain add still translates', emitted == 'v1 = U(F(v1) + F(v2));', emitted)

# 2. An op_sel selection must not be dropped: the operation would read a different half of the source.
check('an op_sel on the float add is refused',
      refused(encode_vop2(VOP2_ADD_F32, 1, V1, V2, op_sel=(0, 0, 1))))
check('an op_sel on a three-source float op is refused',
      refused(encode_vop3a(VOP3A_MAD_F32, 1, [V1, V2, V2], op_sel=(0, 0, 0, 1))))
check('an op_sel on a conversion is refused',
      refused(encode_vop1(VOP1_CVT_F32_U32, 1, V1, op_sel=(0, 0, 1))))
check('a source abs on a conversion is refused',
      refused(encode_vop1(VOP1_CVT_F32_UBYTE0, 1, V1, abs_bits=(1, 0, 0))))

# The comparison path receives the modifiers too: an op_sel there would change which half is compared,
# and the predicate would be computed from the unselected operands.
VOPC_LT_F32 = 0x01
VOPC_EQ_F32 = 0x02
asm, emitted = translate(encode_vopc(VOPC_LT_F32, 106, V1, V2))
check('a plain float compare still translates',
      emitted == 'vcc = ((F(v1) < F(v2)) ? 1u : 0u);', emitted)
check('an op_sel on a float compare is refused',
      refused(encode_vopc(VOPC_LT_F32, 106, V1, V2, op_sel=(0, 0, 1, 0))))
check('an op_sel on another float compare is refused',
      refused(encode_vopc(VOPC_EQ_F32, 106, V1, V2, op_sel=(0, 0, 0, 1))))
check('a clamp on a float compare is refused',
      refused(encode_vopc(VOPC_LT_F32, 106, V1, V2, clamp=1)))

# 3. The byte-extracting conversions keep their exact shift amounts and masks; moving one has to fail.
expected = {
    VOP1_CVT_F32_UBYTE0: 'v1 = U(float(v1 & 255));',
    VOP1_CVT_F32_UBYTE1: 'v1 = U(float((v1 >> 8) & 255));',
    VOP1_CVT_F32_UBYTE2: 'v1 = U(float((v1 >> 16) & 255));',
    VOP1_CVT_F32_UBYTE3: 'v1 = U(float(v1 >> 24));',
    VOP1_CVT_F32_U32: 'v1 = U(float(v1));',
    VOP1_CVT_F32_I32: 'v1 = U(float(int(v1)));',
}
for op, text in expected.items():
    _asm, emitted = translate(encode_vop1(op, 1, V1))
    check('conversion expression is pinned: %s' % text, emitted == text, emitted)

if failures:
    print('FAIL')
    for text in failures:
        print('  ' + text)
    sys.exit(1)
print('PASS')