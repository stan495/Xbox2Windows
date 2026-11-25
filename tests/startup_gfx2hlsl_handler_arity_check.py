"""Verify scalar operand counts through the startup translator's Stage entry point."""
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'tools'))
sys.path.insert(0, str(HERE.parent / 'tools' / 'startup'))
import gfx2hlsl as G  # noqa: E402


def translate(name, operands):
    pipe = SimpleNamespace(root={'params': [], 'flags': 0})
    text = ' '.join((name, ', '.join(operands))).strip()
    refusals = []
    stage = G.Stage(pipe, 'ps', b'', refusals=refusals, decoded=[(0, 4, name, text)])
    return refusals, stage.blocks


def check(name, operands, expected_output):
    refusals, blocks = translate(name, operands)
    assert not refusals, (name, operands, refusals)
    assert any(expected_output in line for _, lines, _ in blocks for line in lines), (name, blocks)

    for invalid in ((), operands[:-1], (*operands, 's18')):
        refusals, blocks = translate(name, invalid)
        expected = 'with %d operands, expected %d' % (len(invalid), len(operands))
        assert len(refusals) == 1 and expected in refusals[0][0], (name, invalid, refusals)
        assert not blocks, (name, invalid, blocks)


def main():
    for condition in ('eq', 'lg', 'gt', 'ge', 'lt', 'le'):
        for kind in ('i32', 'u32'):
            check(f's_cmp_{condition}_{kind}', ('s12', 's14'), 'scc = ')
            check(f's_cmpk_{condition}_{kind}', ('s12', '5'), 'scc = ')

    for width, dst, src in ((32, 's12', 's14'), (64, 's[12:13]', 's[14:15]')):
        for operation in ('and_saveexec', 'or_saveexec', 'andn2_wrexec'):
            check(f's_{operation}_b{width}', (dst, src), 'exec = ')
        check(f's_cselect_b{width}', (dst, src, '1'), 'scc ? ')
    check('s_andn2_saveexec_b64', ('s[12:13]', 's[14:15]'), 'exec = ')
    print('startup gfx2hlsl scalar arity: 33 accepted instructions and 99 refusals')


if __name__ == '__main__':
    main()
