"""`v_cmp_class_f32` in tools/startup/gfx2hlsl.py (issue #1797).

usage: startup_gfx2hlsl_cmp_class_check.py

The binary32 class comparison had no lowering, so both census forms were refused - `v_cmp_class_f32_e64 s32,
-1, v4` and the `_e32` spelling - 2 of the remaining refused shaders in the main-game loading-screen cache
(#870). `tools/pixel_aot.py` already carries the ten-class helper; this port lowers the compare through it.

The instruction bytes are **constructed here** from the field layout the checked-in decoder reads
(`tools/disasm_rdna2.py`, VOP3A branch): prefix `0b110101`, opcode in `w0[25:16]`, scalar destination in
`w0[7:0]`, src0 in `w1[8:0]`, src1 in `w1[17:9]`, and source 255 followed by a literal dword.
"""
import os
import re
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


VOPC_CMP_CLASS_F32 = 0x88
VOPC_CMPX_CLASS_F32 = 0x98
VCC_LO = 106
V0, V4 = 0x100, 0x104
INLINE_MINUS_ONE = 193   # inline constant -1, the mask the census shader uses
LITERAL_SOURCE = 255


def encode_vopc_e32(op, src0, vsrc1, literal=None):
    """Encode a VOPC comparison in its 32-bit form; a cmpx writes EXEC implicitly."""
    w0 = ((0b0111110 << 25) | ((op & 0xFF) << 17) | ((vsrc1 & 0xFF) << 9) | (src0 & 0x1FF))
    code = struct.pack('<I', w0)
    if literal is not None:
        code += struct.pack('<I', literal)
    return code

def encode_vopc(op, sdst, src0, src1, literal=None, abs_src0=0, neg_src0=0):
    """Encode a VOPC comparison in its VOP3A form; a source of 255 is followed by its literal dword."""
    w0 = ((0b110101 << 26) | ((op & 0x3FF) << 16) | (sdst & 0xFF)
          | ((abs_src0 & 1) << 8))
    w1 = ((src0 & 0x1FF) | ((src1 & 0x1FF) << 9) | ((neg_src0 & 1) << 29))
    code = struct.pack('<II', w0, w1)
    if literal is not None:
        code += struct.pack('<I', literal)
    return code


class Pipeline:
    """Just enough pipeline state for one comparison."""

    def __init__(self):
        self.resources, self.notes, self.modified = {}, set(), set()


class Stage(G.Stage):
    """A Stage with no pipeline: enough state for a decoded instruction to be translated."""

    def __init__(self):
        self.lines, self.sym = [], {}
        self.used_s, self.used_v, self.uniform_v = set(), set(), set()
        self.exec_known = True
        self.exports = {}
        self.masks = set()
        self.pipe = Pipeline()


def translate(code):
    decoded = G.decode(code)
    stage = Stage()
    for _offset, _size, name, asm in decoded:
        stage.one(name, asm)
    return [asm for _o, _s, _n, asm in decoded], ' '.join(stage.lines), stage


# 1. The encoder produces the form the decoder reads; a layout change shows up here first.
asm, emitted, stage = translate(encode_vopc(VOPC_CMP_CLASS_F32, VCC_LO, V4, INLINE_MINUS_ONE))
check('encodes and decodes the class compare',
      asm == ['v_cmp_class_f32_e64 vcc_lo, v4, -1'], repr(asm))

# 2. The predicate is the class of the value against the mask, written to VCC for the non-exec form.
check('the all-classes mask matches everything', 'class32(F(v4))' in emitted and '0xffffffffu' in emitted, emitted)
check('the result is written to vcc', emitted.startswith('vcc = '), emitted)

# 3. The execution-mask form writes EXEC and stops the straight-line assumption.
asm, emitted, stage = translate(encode_vopc(VOPC_CMPX_CLASS_F32, VCC_LO, V4, INLINE_MINUS_ONE))
check('the exec-mask class compare writes exec', emitted.startswith('exec = '), emitted)
check('the exec-mask form clears exec_known', stage.exec_known is False)

# 4. A literal mask and a register value reach the helper as an integer and a float operand.
asm, emitted, _stage = translate(encode_vopc(VOPC_CMP_CLASS_F32, VCC_LO, V4, LITERAL_SOURCE,
                                             literal=0x00000300))
check('a literal mask is read as an integer', '0x00000300u' in emitted, emitted)
asm, emitted, _stage = translate(encode_vopc(VOPC_CMP_CLASS_F32, VCC_LO, V4, V0))
check('a register mask is read as an integer', 'class32(F(v4)) & v0' in emitted, emitted)

# 5. The helper is in the preamble the generated shader is built with, with the ten classes from the encoding.
pipe = object.__new__(G.Pipeline)
pipe.uds, pipe.resources, pipe.samplers, pipe.uses_sampler_heap = [], {}, {}, False
common = ' '.join(pipe.common())
check('the class helper is emitted in the common preamble', 'uint class32(float value)' in common, common[:200])
check('the helper separates quiet from signaling NaN', '0x00400000u' in common, common[:200])


# 6. The _e32 cmpx spelling prints no destination; it still writes EXEC, and the value stays in SRC0.
asm, emitted, stage = translate(encode_vopc_e32(VOPC_CMPX_CLASS_F32, V4, V0))
check('encodes and decodes the e32 exec-mask form', asm == ['v_cmpx_class_f32_e32 v4, v0'], repr(asm))
check('the e32 exec-mask form lowers', emitted.startswith('exec = ') and 'class32(F(v4)) & v0' in emitted, emitted)
check('the e32 exec-mask form clears exec_known', stage.exec_known is False)

# 7. A narrowed EXEC stays narrowed: the predicate is intersected with it, and the non-exec form does not
#    reactivate lanes either.
stage = Stage()
stage.exec_known = False
decoded = G.decode(encode_vopc(VOPC_CMP_CLASS_F32, VCC_LO, V4, INLINE_MINUS_ONE))
for _o, _s, name, asm_text in decoded:
    stage.one(name, asm_text)
check('a narrowed exec is intersected with the predicate', 'exec & ' in ' '.join(stage.lines), ' '.join(stage.lines))

# 8. Abs and neg on the value arrive as operand text (`|v4|`, `-v4`), not as a modifier list, so the class
#    helper must see the modified value bits.
_asm, emitted, _stage = translate(encode_vopc(VOPC_CMP_CLASS_F32, VCC_LO, V4, INLINE_MINUS_ONE, abs_src0=1))
check('an absolute value reaches the helper as abs', 'class32(abs(F(v4)))' in emitted, emitted)
_asm, emitted, _stage = translate(encode_vopc(VOPC_CMP_CLASS_F32, VCC_LO, V4, INLINE_MINUS_ONE, neg_src0=1))
check('a negated value reaches the helper as a negation', 'class32((-F(v4)))' in emitted, emitted)


# ---- value-level check of the emitted class helper -------------------------------------------------------
# The reference below is written from the IEEE binary32 layout, not from the lowering's expressions: a wrong
# class bit must fail even though the emitted text still looks plausible.
def ref_class32(bits):
    """The LLVM class bit of a binary32 pattern: sNaN qNaN -inf -normal -subnormal -0 +0 +subnormal +normal +inf."""
    sign = (bits >> 31) & 1
    exponent = (bits >> 23) & 0xFF
    mantissa = bits & 0x7FFFFF
    if exponent == 0xFF:
        if mantissa == 0:
            return 0x4 if sign else 0x200
        return 0x2 if mantissa & 0x400000 else 0x1
    if exponent == 0:
        if mantissa == 0:
            return 0x20 if sign else 0x40
        return 0x10 if sign else 0x80
    return 0x8 if sign else 0x100


TOKEN = re.compile(r'\s*(0x[0-9a-fA-F]+u?|\d+u?|[A-Za-z_]\w*|>=|<=|==|!=|&&|\|\||[&|<>?:()!~^+-])')


def tokens(source):
    out, position = [], 0
    while position < len(source):
        match = TOKEN.match(source, position)
        if not match:
            raise ValueError('cannot tokenise %r' % source[position:position + 20])
        out.append(match.group(1))
        position = match.end()
    return out


class Expr:
    """A recursive-descent evaluator for the subset of HLSL the helper uses (32-bit integer bits)."""

    LEVELS = [('||',), ('&&',), ('|',), ('^',), ('&',), ('==', '!='), ('>=', '<=', '>', '<'), ('+', '-')]

    def __init__(self, source, variables):
        self.items, self.at, self.vars = tokens(source), 0, variables

    def peek(self):
        return self.items[self.at] if self.at < len(self.items) else ''

    def take(self):
        item = self.peek()
        self.at += 1
        return item

    def ternary(self):
        condition = self.binary(0)
        if self.peek() == '?':
            self.take()
            yes = self.ternary()
            if self.take() != ':':
                raise ValueError('missing :')
            return yes if condition else self.ternary()
        return condition

    def binary(self, level):
        if level == len(self.LEVELS):
            return self.unary()
        value = self.binary(level + 1)
        while self.peek() in self.LEVELS[level]:
            operator = self.take()
            rhs = self.binary(level + 1)
            if operator == '&':
                value &= rhs
            elif operator == '|':
                value |= rhs
            elif operator == '^':
                value ^= rhs
            elif operator == '==':
                value = int(value == rhs)
            elif operator == '!=':
                value = int(value != rhs)
            elif operator == '>=':
                value = int(value >= rhs)
            elif operator == '<=':
                value = int(value <= rhs)
            elif operator == '>':
                value = int(value > rhs)
            elif operator == '<':
                value = int(value < rhs)
            elif operator == '&&':
                value = int(bool(value) and bool(rhs))
            elif operator == '||':
                value = int(bool(value) or bool(rhs))
            elif operator == '+':
                value = (value + rhs) & 0xFFFFFFFF
            elif operator == '-':
                value = (value - rhs) & 0xFFFFFFFF
        return value

    def unary(self):
        item = self.take()
        if item == '!':
            return int(not self.unary())
        if item == '~':
            return (~self.unary()) & 0xFFFFFFFF
        if item == '(':
            value = self.ternary()
            if self.take() != ')':
                raise ValueError('missing )')
            return value
        if re.fullmatch(r'0x[0-9a-fA-F]+u?', item):
            return int(item.rstrip('u'), 16)
        if re.fullmatch(r'\d+u?', item):
            return int(item.rstrip('u'))
        if item == 'asuint':
            return self.call()
        return self.vars[item]

    def call(self):
        if self.take() != '(':
            raise ValueError('missing (')
        value = self.ternary()
        if self.take() != ')':
            raise ValueError('missing )')
        return value & 0xFFFFFFFF


def evaluate_class32(helper, bits):
    """Run the emitted helper body for one bit pattern, so what is checked is the value it returns."""
    body = helper[helper.index('{') + 1:helper.rindex('}')]
    variables = {'value': bits}
    for statement in (piece.strip() for piece in body.split(';')):
        if not statement:
            continue
        if statement.startswith('uint '):
            for part in statement[4:].split(','):
                name, expr = (piece.strip() for piece in part.split('=', 1))
                variables[name] = Expr(expr, variables).ternary() & 0xFFFFFFFF
        elif statement.startswith('bool '):
            name, expr = (piece.strip() for piece in statement[5:].split('=', 1))
            variables[name] = int(bool(Expr(expr, variables).ternary()))
        elif statement.startswith('if ') and ') return ' in statement:
            condition, consequence = statement[3:].split(') return ', 1)
            if Expr(condition[1:], variables).ternary():
                return Expr(consequence, variables).ternary() & 0xFFFFFFFF
        elif statement.startswith('return '):
            return Expr(statement[7:], variables).ternary() & 0xFFFFFFFF
    raise ValueError('the helper returned nothing for %#x' % bits)


_pipe = object.__new__(G.Pipeline)
_pipe.uds, _pipe.resources, _pipe.samplers, _pipe.uses_sampler_heap = [], {}, {}, False
class32_text = [entry for entry in _pipe.common() if 'uint class32(' in entry][0]
grid = [0x00000000, 0x80000000, 0x00000001, 0x80000001, 0x007FFFFF, 0x807FFFFF, 0x00800000,
        0x80800000, 0x7F7FFFFF, 0xFF7FFFFF, 0x7F800000, 0xFF800000, 0x7F800001, 0xFF800001,
        0x7FC00000, 0xFFC00000, 0x7FFFFFFF, 0xFFFFFFFF]
for _sign in (0, 1):
    for _exponent in range(256):
        for _mantissa in (0, 1, 0x3FFFFF, 0x400000, 0x7FFFFE, 0x7FFFFF):
            grid.append((_sign << 31) | (_exponent << 23) | _mantissa)
grid += list(range(0, 1 << 32, 0x10001))
_mismatches = [(bits, ref_class32(bits), evaluate_class32(class32_text, bits)) for bits in grid
               if evaluate_class32(class32_text, bits) != ref_class32(bits)]
check('the emitted helper agrees with the binary32 classifier on %d patterns' % len(grid),
      not _mismatches, repr(_mismatches[:4]))

if failures:
    print('FAIL')
    for text in failures:
        print('  ' + text)
    sys.exit(1)
print('PASS')