"""Check the SMEM offset operand forms in tools/startup/gfx2hlsl.py (#1674).

usage: startup_gfx2hlsl_smem_offset_check.py

The offset operand 'off' names no offset at all, so the byte offset is zero. s_buffer_load_dword* and
s_load_dword* must translate it exactly as the literal 0, keep the literal and m0 forms as they were
(s_buffer_load reads m0 as a register offset, s_load refuses it) and still refuse any other operand.
The last check removes the 'off' handling and requires the accepted form to be refused again.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools', 'startup'))
import gfx2hlsl as G  # noqa: E402

failures = []


def check(label, condition, detail=''):
    print(f"{label}: {'ok' if condition else 'MISMATCH  ' + detail}")
    if not condition:
        failures.append(label)


class Pipe:
    """The resource registry of a pipeline, without one: every key gets the next r<N>."""

    sampler_heap = False

    def __init__(self):
        self.keys = []

    def resource(self, key, kind):
        if (key, kind) not in self.keys:
            self.keys.append((key, kind))
        return 'r%d' % self.keys.index((key, kind))


def stage(symbols):
    """A Stage with no machine code: only the scalar symbol state the SMEM handlers read."""
    s = G.Stage.__new__(G.Stage)
    s.pipe, s.sym, s.lines = Pipe(), dict(symbols), []
    s.used_s, s.used_v, s.uniform_v, s.exec_known = set(), set(), set(), True
    return s


# s[4:7] holds a buffer descriptor read from a root table; s[4:5] a root constant-buffer pointer
# (s_load reads that buffer directly) or a descriptor table pointer (s_load fetches descriptors).
BUFFER = {4: ('desc', ('table', 0), 0x20, 0), 5: ('desc', ('table', 0), 0x20, 1),
          6: ('desc', ('table', 0), 0x20, 2), 7: ('desc', ('table', 0), 0x20, 3)}
ROOT = {4: ('ptr', ('root', 1))}
TABLE = {4: ('ptr', ('table', 0))}


def run(symbols, op, operands, mods=()):
    """(emitted lines, symbols after) or the Unsupported message."""
    s = stage(symbols)
    try:
        getattr(s, 'op_' + op)(operands.split(', '), list(mods))
    except G.Unsupported as error:
        return str(error)
    return s.lines, s.sym


def same_as_zero(label, symbols, op, dst, base, want):
    got, zero = run(symbols, op, f'{dst}, {base}, off'), run(symbols, op, f'{dst}, {base}, 0')
    check(f'{label}: off accepted', isinstance(got, tuple), repr(got))
    check(f'{label}: same as the literal 0', got == zero, f'{got!r} vs {zero!r}')
    check(f'{label}: emitted', isinstance(got, tuple) and got[0] == want, repr(got))
    return got


# 1. The 'off' form of each SMEM load is the literal 0, including the fixed-word marking of s_buffer_load.
got = same_as_zero('s_buffer_load_dword', BUFFER, 's_buffer_load_dword', 's0', 's[4:7]',
                   ['s0 = r0.Load(0x00000000u + 0);'])
check('s_buffer_load_dword: fixed buffer word', isinstance(got, tuple) and got[1].get(0) == ('fixed_buffer_word',))
same_as_zero('s_buffer_load_dwordx2', BUFFER, 's_buffer_load_dwordx2', 's[2:3]', 's[4:7]',
             ['s2 = r0.Load(0x00000000u + 0);', 's3 = r0.Load(0x00000000u + 4);'])
same_as_zero('s_buffer_load_dwordx4', BUFFER, 's_buffer_load_dwordx4', 's[8:11]', 's[4:7]',
             ['s%d = r0.Load(0x00000000u + %d);' % (8 + i, 4 * i) for i in range(4)])
same_as_zero('s_load_dword from a root buffer', ROOT, 's_load_dword', 's0', 's[4:5]', ['s0 = r0.Load(0);'])
same_as_zero('s_load_dwordx2 from a root buffer', ROOT, 's_load_dwordx2', 's[2:3]', 's[4:5]',
             ['s2 = r0.Load(0);', 's3 = r0.Load(4);'])
got = same_as_zero('s_load_dwordx8 from a table', TABLE, 's_load_dwordx8', 's[8:15]', 's[4:5]', [])
check('s_load_dwordx8: descriptor at offset 0',
      isinstance(got, tuple) and [got[1][8 + i] for i in range(8)] == [('desc', ('table', 0), 0, i) for i in range(8)])

# 2. The literal and m0 forms keep their meaning.
check('s_buffer_load_dword literal 0x4 unchanged',
      run(BUFFER, 's_buffer_load_dword', 's0, s[4:7], 0x4')[0] == ['s0 = r0.Load(0x00000004u + 0);'])
check('s_load_dword literal 0x8 unchanged', run(ROOT, 's_load_dword', 's0, s[4:5], 0x8')[0] == ['s0 = r0.Load(8);'])
got = run(BUFFER, 's_buffer_load_dword', 's0, s[4:7], m0')
check('s_buffer_load_dword m0 stays a register offset',
      isinstance(got, tuple) and got[0] == ['s0 = r0.Load(m0 + 0);'] and 0 not in got[1], repr(got))
got = run(ROOT, 's_load_dword', 's0, s[4:5], m0')
check('s_load_dword m0 still refused', got == "s_load with a register offset through ('root', 1)", repr(got))

# 3. Other operands are still refused, not read as zero.
for operand in ('of', 'OFF', 'offset', 'null', 'off:0'):
    got = run(BUFFER, 's_buffer_load_dword', f's0, s[4:7], {operand}')
    check(f's_buffer_load_dword {operand} refused', got == 'operand %r' % operand, repr(got))
    got = run(ROOT, 's_load_dword', f's0, s[4:5], {operand}')
    check(f's_load_dword {operand} refused', isinstance(got, str), repr(got))

# 4. Negative control: without the 'off' handling the accepted forms are refused again.
original = G.smem_offset
G.smem_offset = lambda tok: tok
try:
    got = run(BUFFER, 's_buffer_load_dword', 's0, s[4:7], off')
    check('control: s_buffer_load_dword off refused without the handling', got == "operand 'off'", repr(got))
    got = run(ROOT, 's_load_dword', 's0, s[4:5], off')
    check('control: s_load_dword off refused without the handling',
          got == "s_load with a register offset through ('root', 1)", repr(got))
finally:
    G.smem_offset = original

print('SMEM offset checks: %d failed' % len(failures))
sys.exit(1 if failures else 0)
