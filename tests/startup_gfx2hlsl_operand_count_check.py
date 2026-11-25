"""Operand counts of the binary32 table (VEC_F) in tools/startup/gfx2hlsl.py.

usage: startup_gfx2hlsl_operand_count_check.py

Every VEC_F operation translates with its destination and exactly its number of sources. One source short must be
refused with Unsupported, not raise IndexError, and one source too many must be refused instead of being dropped from
the translation. The integer table (VEC_U) already checks this; the same rule is applied to its entries here as a
control. The last check loads the translator with the VEC_F count check removed and requires the old failures
(IndexError for a short form, an accepted long form) to come back, so the check cannot pass without the guard.
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'tools' / 'startup'))
sys.path.insert(0, str(HERE.parent / 'tools'))
import gfx2hlsl as G  # noqa: E402

GUARD = """            if len(ops) != arity + 1:
                raise Unsupported('%s with %d operands, expected %d' % (base, len(ops), arity + 1))
            args = [self.f(o) for o in ops[1:1 + arity]]"""
failures = []


def check(label, condition, detail=''):
    print('%s: %s' % (label, 'ok' if condition else 'MISMATCH  ' + detail))
    if not condition:
        failures.append(label)


def probe(module):
    class Probe(module.Stage):
        """A Stage with no pipeline: enough state for one instruction to be translated."""

        def __init__(self):
            self.lines, self.sym = [], {}
            self.used_s, self.used_v, self.uniform_v = set(), set(), set()
            self.exec_known = True
    return Probe


def outcome(module, op, sources):
    """'ok', 'refused' or the name of another exception for op with the given number of sources."""
    line = '%s v1, %s' % (op, ', '.join('v%d' % (2 + i) for i in range(sources))) if sources else '%s v1' % op
    try:
        probe(module)().one(op, line)
    except module.Unsupported:
        return 'refused'
    except Exception as error:  # noqa: BLE001  the point is to see which exception a malformed form raises
        return type(error).__name__
    return 'ok'


def table_counts(module, table):
    wrong = []
    for op, (arity, _) in getattr(module, table).items():
        got = [outcome(module, op, n) for n in (arity - 1, arity, arity + 1)]
        if got != ['refused', 'ok', 'refused']:
            wrong.append((op, got))
    return wrong


for table in ('VEC_F', 'VEC_U'):
    wrong = table_counts(G, table)
    check('%s: each operation translates with exactly its sources and refuses one fewer or one more (%d operations)'
          % (table, len(getattr(G, table))), not wrong, repr(wrong[:4]))

# The forms the table spells with a constant or an accumulator keep translating with their usual operands.
for line in ('v_fmac_f32 v1, v2, v3', 'v_mac_f32_e64 v1, v2, v3', 'v_fmamk_f32 v1, v2, 0x40000000, v3',
             'v_fmaak_f32 v1, v2, v3, 0x40000000', 'v_madmk_f32 v1, v2, 0x3f000000, v3',
             'v_madak_f32 v1, v2, v3, 0x3f000000', 'v_rcp_f32_e64 v1, v2 clamp', 'v_med3_f32 v1, v2, 1.0, v3'):
    stage = probe(G)()
    stage.one(line.split(' ', 1)[0], line)
    check('translates: %s' % line, stage.lines and stage.lines[-1].startswith('v1 = '), repr(stage.lines))

# Negative control: without the guard, a short form raises IndexError and a long form translates.
source = Path(G.__file__).read_text(encoding='utf-8')
check('control: the guard is in the translator once', source.count(GUARD) == 1)
spec = importlib.util.spec_from_loader('gfx2hlsl_unguarded', loader=None)
unguarded = importlib.util.module_from_spec(spec)
unguarded.__dict__['__file__'] = G.__file__
exec(compile(source.replace(GUARD, '            args = [self.f(o) for o in ops[1:1 + arity]]'), G.__file__, 'exec'),
     unguarded.__dict__)
check('control: without the guard v_add_f32 with one source raises IndexError',
      outcome(unguarded, 'v_add_f32', 1) == 'IndexError', outcome(unguarded, 'v_add_f32', 1))
check('control: without the guard v_med3_f32 with four sources translates',
      outcome(unguarded, 'v_med3_f32', 4) == 'ok', outcome(unguarded, 'v_med3_f32', 4))
check('control: without the guard the VEC_F table check fails', bool(table_counts(unguarded, 'VEC_F')))

print('operand count checks: %d failed' % len(failures))
sys.exit(1 if failures else 0)
