"""Output modifiers on float-result conversions in tools/startup/gfx2hlsl.py (issue #1696).

usage: startup_gfx2hlsl_vcvt_omod_check.py

A VOP3 conversion can carry an output modifier (clamp, mul:2, mul:4, div:2). The translator refused any
modifier on the table-driven conversions with `Unsupported: <name> with [...]`, even though the same
modifiers were already implemented for other float results. That refusal covered 5 of the 29 refused shaders
in the first main-game loading-screen cache (#870).

The instruction bytes are **constructed here** from the field layout the checked-in decoder reads
(`tools/disasm_rdna2.py`, VOP3A branch), then decoded and translated:
  w0: [31:26] = 0b110101, [25:16] opcode (VOP1 e64 = 0x180 + VOP1 opcode), [15] clamp, [7:0] vdst
  w1: [8:0] src0, [17:9] src1, [26:18] src2, [28:27] omod, [31:29] neg
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


VOP1_E64_BASE = 0x180
VOP1_CVT_F32_I32 = 0x05
VOP1_CVT_F32_U32 = 0x06
VOP1_CVT_F16_F32 = 0x0a
VOP1_CVT_F32_F16 = 0x0b
OMOD = {None: 0, 'mul:2': 1, 'mul:4': 2, 'div:2': 3}
V1 = 0x101  # VGPR operands are encoded from 256 upwards


def encode_vop1_e64(op, vdst, src0, *, omod=None, clamp=0, neg0=0):
    """Encode a VOP1 instruction in its VOP3A (e64) form."""
    w0 = ((0b110101 << 26) | ((VOP1_E64_BASE + op) << 16) | ((clamp & 1) << 15) | (vdst & 0xff))
    w1 = ((src0 & 0x1ff) | ((OMOD[omod] & 0x3) << 27) | ((neg0 & 1) << 29))
    return struct.pack('<II', w0, w1)


class Pipeline:
    """Just enough pipeline state for a conversion."""

    def __init__(self):
        self.resources, self.notes, self.modified, self.sampler_heap = {}, set(), set(), False


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


# 1. The encoder produces what the decoder reads; a layout change shows up here first.
code = encode_vop1_e64(VOP1_CVT_F32_I32, 1, V1, omod='mul:2')
asm, emitted = translate(code)
check('encodes and decodes mul:2', asm == ['v_cvt_f32_i32_e64 v1, v1 mul:2'], repr(asm))

# 2. Without a modifier the output is exactly what the table-driven path emitted before the split.
asm, emitted = translate(encode_vop1_e64(VOP1_CVT_F32_I32, 1, V1))
check('no modifier is unchanged', emitted == 'v1 = U(float(int(v1)));', emitted)

# 3. Each output modifier wraps the converted value.
asm, emitted = translate(encode_vop1_e64(VOP1_CVT_F32_I32, 1, V1, omod='mul:2'))
check('mul:2 doubles', 'U((float(int(v1))) * 2.0)' in emitted, emitted)
asm, emitted = translate(encode_vop1_e64(VOP1_CVT_F32_I32, 1, V1, omod='mul:4'))
check('mul:4 quadruples', 'U((float(int(v1))) * 4.0)' in emitted, emitted)
asm, emitted = translate(encode_vop1_e64(VOP1_CVT_F32_I32, 1, V1, omod='div:2'))
check('div:2 halves', 'U((float(int(v1))) * 0.5)' in emitted, emitted)
asm, emitted = translate(encode_vop1_e64(VOP1_CVT_F32_I32, 1, V1, clamp=1))
check('clamp saturates', 'U(saturate(float(int(v1))))' in emitted, emitted)
# With both, the hardware applies the output modifier first and clamps the result; the disassembler
# prints `clamp` before `mul:2`, so a translation that follows that text order would clamp early.
asm, emitted = translate(encode_vop1_e64(VOP1_CVT_F32_I32, 1, V1, omod='mul:2', clamp=1))
check('clamp applies after the scaling', 'U(saturate((float(int(v1))) * 2.0))' in emitted, emitted)
asm, emitted = translate(encode_vop1_e64(VOP1_CVT_F32_I32, 1, V1, omod='div:2', clamp=1))
check('clamp applies after the halving', 'U(saturate((float(int(v1))) * 0.5))' in emitted, emitted)

# 4. The other float-result conversions take the same modifiers.
asm, emitted = translate(encode_vop1_e64(VOP1_CVT_F32_U32, 2, V1, omod='mul:2'))
check('u32 conversion doubles', asm == ['v_cvt_f32_u32_e64 v2, v1 mul:2'] and '* 2.0' in emitted, emitted)
asm, emitted = translate(encode_vop1_e64(VOP1_CVT_F32_F16, 3, V1, omod='mul:2'))
check('f16 conversion doubles', '* 2.0' in emitted and 'f16tof32(v1)' in emitted, emitted)


# 5. Refusals that must not widen: a uint-result conversion keeps refusing modifiers, and a source modifier
#    on a conversion is still refused rather than ignored.
check('uint-result conversion refuses mul:2',
      refused(encode_vop1_e64(VOP1_CVT_F16_F32, 1, V1, omod='mul:2')))
check('a source negation is still refused', refused(encode_vop1_e64(VOP1_CVT_F32_I32, 1, V1, neg0=1)))

if failures:
    print('FAIL')
    for text in failures:
        print('  ' + text)
    sys.exit(1)
print('PASS')