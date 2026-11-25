"""Check gfx1013 non-fused binary32 multiply-adds from encoded pixel instructions.

No arguments runs the translation controls. Binary mode reassembles the source
with llvm-mc. Dispatch and preserve modes run the translated pixel body in a
compute wrapper with raw inputs and outputs. Preserve requests DXC's SM 6.2
denormal mode for the control that removes the software flushes.
"""
import struct
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

from startup_gfx2hlsl_bit_alu_check import G, POST_SOURCE, POST_WORDS, PRE_SOURCE, PRE_WORDS, vs_source, vs_words
from startup_flat_param_check import assemble, compile_hlsl, draw, root_blob

ONE = 0x3f800000
HALF = 0x3f000000
MIN_NORMAL = 0x00800000
SUBNORMAL = 0x00400000
INF = 0x7f800000
SIGN = 0x80000000
# The rounded product plus the addend is a tie toward the even 40000002.
# A fused operation keeps the product's low bit and rounds up to 40000003.
ROUNDING = (ONE + 1, ONE + 1, ONE + 3)
PRODUCT = (MIN_NORMAL, HALF, MIN_NORMAL)
ADDEND = (MIN_NORMAL, ONE, SUBNORMAL)
SUM = (MIN_NORMAL, ONE, SIGN | (MIN_NORMAL + 1))
LEGACY = (0, INF, ONE)
CASES = {
    'v_mad_f32': (ROUNDING, PRODUCT, ADDEND, SUM),
    'v_mac_f32': (ROUNDING, PRODUCT, ADDEND, SUM),
    'v_madmk_f32': (ROUNDING, PRODUCT, ADDEND, SUM),
    'v_madak_f32': (ROUNDING, PRODUCT, ADDEND, SUM),
    'v_mad_legacy_f32': (ROUNDING, PRODUCT, ADDEND, LEGACY),
}
BODY_WORDS = {
    'v_mad_f32': '0c0041d5000922040d0041d5010b26040e0041d5020d2a040f0041d5030f2e04',
    'v_mac_f32': '0803187e0009183e09031a7e010b1a3e0a031c7e020d1c3e0b031e7e030f1e3e',
    'v_madmk_f32': '001118400100803f01131a400000003f02151c400000803f03171e400000803f',
    'v_madak_f32': '000918420300803f010b1a4200008000020d1c4200004000030f1e4201008080',
    'v_mad_legacy_f32': '0c0040d5000922040d0040d5010b26040e0040d5020d2a040f0040d5030f2e04',
}


def power2(exponent):
    return Fraction(2) ** exponent


def decode_float(word):
    value, = struct.unpack('<f', struct.pack('<I', word))
    return Fraction(value)


def round_word(value):
    """Round an exact finite rational in the fixture's range to binary32, ties to even."""
    sign = SIGN if value < 0 else 0
    value = abs(value)
    if value == 0:
        return sign
    exponent = value.numerator.bit_length() - value.denominator.bit_length()
    if value < power2(exponent):
        exponent -= 1
    if exponent < -126:
        # Rounding the largest subnormal up also encodes the smallest normal.
        return sign | round(value * power2(149))
    significand = round(value / power2(exponent - 23))
    return sign | (((exponent + 127) << 23) + significand - (1 << 23))


def flush(word):
    return word & SIGN if (word & 0x7fffffff) < MIN_NORMAL else word


def reference(inputs, *, fused=False, ftz=True, legacy=False):
    narrow = flush if ftz else lambda word: word
    a, b, c = map(narrow, inputs)
    product = (Fraction(0) if legacy and (a in (0, SIGN) or b in (0, SIGN))
               else decode_float(a) * decode_float(b))
    if not fused:
        product = decode_float(narrow(round_word(product)))
    return narrow(round_word(product + decode_float(c)))


def body_source(op):
    lines = []
    for lane, (_, multiplier, addend) in enumerate(CASES[op]):
        dst, a, b, c = ('v%d' % (base + lane) for base in (12, 0, 4, 8))
        if op == 'v_mac_f32':
            lines.append(f'v_mov_b32 {dst}, {c}')
            operands = f'{a}, {b}'
        elif op == 'v_madmk_f32':
            operands = f'{a}, {multiplier:#x}, {c}'
        elif op == 'v_madak_f32':
            operands = f'{a}, {b}, {addend:#x}'
        else:
            operands = f'{a}, {b}, {c}'
        lines.append(f'{op} {dst}, {operands}')
    return '\n'.join(lines) + '\n'


def translate(op):
    ps = PRE_WORDS + bytes.fromhex(BODY_WORDS[op]) + POST_WORDS
    return G.Pipeline(vs_words(CASES[op]), ps, root_blob())


PRODUCT_LINE = '    precise float product = ftz32(legacy ? mul_legacy(a, b) : a * b);'
SUM_LINE = '    precise float sum = product + ftz32(c);'
PLAIN = '''float mad32(float a, float b, float c, bool legacy) {
    return (legacy ? mul_legacy(a, b) : a * b) + c;
}'''


def check_lowering(op, hlsl):
    for statement in ('    return asfloat(bits & mask);', '    a = ftz32(a);', '    b = ftz32(b);',
                      PRODUCT_LINE, SUM_LINE, '    return ftz32(sum);'):
        assert statement in hlsl, (op, statement, hlsl)
    for lane, (_, multiplier, addend) in enumerate(CASES[op]):
        a, b, c, dst = (f'v{base + lane}' for base in (0, 4, 8, 12))
        b = f'{multiplier:#010x}u' if op == 'v_madmk_f32' else b
        c = f'{addend:#010x}u' if op == 'v_madak_f32' else dst if op == 'v_mac_f32' else c
        legacy = 'true' if op == 'v_mad_legacy_f32' else 'false'
        statement = f'{dst} = U(mad32(F({a}), F({b}), F({c}), {legacy}));'
        assert statement in hlsl, (op, statement, hlsl)


def change_helper(hlsl, replacement):
    start = hlsl.index('float mad32(')
    end = hlsl.index('\n}', start) + len('\n}')
    return hlsl[:start] + replacement(hlsl[start:end]) + hlsl[end:]


def without_flush(helper):
    return (helper.replace('    a = ftz32(a);\n    b = ftz32(b);\n', '')
            .replace(PRODUCT_LINE, '    precise float product = legacy ? mul_legacy(a, b) : a * b;')
            .replace(SUM_LINE, '    precise float sum = product + c;')
            .replace('ftz32(sum)', 'sum'))


def check_controls():
    assert reference(ROUNDING) == 0x40000002
    assert reference(ROUNDING, fused=True) == 0x40000003
    assert reference(PRODUCT) == reference(ADDEND) == MIN_NORMAL
    assert reference(PRODUCT, ftz=False) == reference(ADDEND, ftz=False) == 0x00c00000
    assert reference(SUM) == SIGN and reference(SUM, ftz=False) == SIGN | 1
    assert reference(LEGACY, legacy=True) == ONE
    for op in CASES:
        hlsl = translate(op).hlsl_ps()
        check_lowering(op, hlsl)
        for mutation in (lambda _: PLAIN, without_flush):
            try:
                check_lowering(op, change_helper(hlsl, mutation))
            except AssertionError:
                continue
            raise AssertionError(f'{op}: accepted mutated HLSL')
    print('five encoded forms and both translation controls pass; separate/fused: 40000002/40000003')


def check_binary(llvm_mc):
    for op, cases in CASES.items():
        for stage, source, expect in (
            ('vertex', vs_source(cases), vs_words(cases)),
            ('pixel', PRE_SOURCE + body_source(op) + POST_SOURCE,
             PRE_WORDS + bytes.fromhex(BODY_WORDS[op]) + POST_WORDS),
        ):
            actual = assemble(llvm_mc, source)
            assert actual == expect, f'{op} {stage}: expected {expect.hex()}, assembled {actual.hex()}'
    print('vertex and pixel words match llvm-mc')


def dispatch(dxc, runner, adapter, hlsl, cases, directory, preserve):
    # The raw UAV fixture initializes each word using this pattern. Loading and XORing
    # the fill value makes the operands opaque to DXC and the driver.
    fill = [(i + 1) * 0x9e3779b1 & 0xffffffff | 1 for i in range(16)]
    inputs = [row[arg] for arg in range(3) for row in cases]
    words = [initial ^ value for initial, value in zip(fill, inputs)]
    params = '\n'.join('    i.fparam%d = data.Load4(%d) ^ uint4(%s);' %
                       (p, p * 16, ', '.join(f'{word:#x}u' for word in words[p * 4:p * 4 + 4]))
                       for p in range(3))
    hlsl = hlsl.replace('PSOut main(PSIn i)', 'PSOut translated(PSIn i)')
    source = hlsl + '''
RWByteAddressBuffer data : register(u0);
[numthreads(1, 1, 1)]
void main() {
    PSIn i = (PSIn)0;
''' + params + '''
    data.Store4(48, asuint(translated(i).mrt0));
}
'''
    src, cso, output = (directory / name for name in ('mad.hlsl', 'mad.cso', 'mad.bin'))
    src.write_text(source, encoding='ascii')
    target = ['-T', 'cs_6_2', '-denorm', 'preserve'] if preserve else ['-T', 'cs_6_0']
    subprocess.run([dxc, *target, '-E', 'main', '-Fo', str(cso), str(src)], check=True)
    command = '--raw-uav-dump-sm62' if preserve else '--raw-uav-dump'
    run = subprocess.run([runner, adapter, command, str(cso), '1', '64', str(output)],
                         capture_output=True, text=True)
    print(run.stdout, end='')
    if run.returncode == 77:
        sys.exit(77)
    if run.returncode or run.stderr:
        raise AssertionError(run.stderr or f'raw UAV dump exited with {run.returncode}')
    actual = struct.unpack('<16I', output.read_bytes())
    assert actual[:12] == tuple(fill[:12]), 'operand region changed'
    return actual[12:]


def check_dispatch(dxc, runner, adapter, fxc=None, *, preserve):
    runner = str(Path(runner).resolve())
    out = Path(__file__).resolve().parents[1] / 'out'
    out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        directory = Path(tmp)
        for op, cases in CASES.items():
            pipe = translate(op)
            hlsl = pipe.hlsl_ps()
            legacy = op == 'v_mad_legacy_f32'
            expect = tuple(reference(row, legacy=legacy) for row in cases)
            actual = dispatch(dxc, runner, adapter, hlsl, cases, directory, preserve)
            compare(op, actual, expect)
            if preserve:
                # Preserve mode keeps native FTZ from hiding the missing software flushes.
                control = change_helper(hlsl, without_flush)
                actual = dispatch(dxc, runner, adapter, control, cases, directory, True)
                unflushed = tuple(reference(row, legacy=legacy, ftz=False) for row in cases)
                assert all(actual[i] != expect[i] for i in (1, 2)), (op, actual, expect)
                compare(f'{op} without flush', actual, unflushed)
                print(f'{op}: baseline exact; no-flush product and addend {actual[1]:08x}/{actual[2]:08x}')
            else:
                control = change_helper(hlsl, lambda _: PLAIN)
                rounded = dispatch(dxc, runner, adapter, control, cases, directory, False)[0]
                separate, fused = (reference(ROUNDING, fused=flag) for flag in (False, True))
                assert rounded in (separate, fused), (op, rounded)
                print(f'{op}: baseline exact; plain rounding word {rounded:08x}')
                if fxc:
                    vs = compile_hlsl(fxc, 'fxc', pipe.hlsl_vs(), 'vs', 'vs_5_1', directory)
                    ps = compile_hlsl(fxc, 'fxc', hlsl, 'ps', 'ps_5_1', directory)
                    result = draw(runner, adapter, vs, ps, ','.join(f'{word:08x}' for word in expect))
                    assert result.returncode == 0, result.stderr


def compare(op, actual, expected):
    for index, (got, want) in enumerate(zip(actual, expected)):
        assert got == want, f'{op} lane {index}: expected {want:08x}, got {got:08x}'


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'binary':
        check_binary(sys.argv[2])
    elif len(sys.argv) in (5, 6) and sys.argv[1] in ('dispatch', 'preserve'):
        check_dispatch(*sys.argv[2:], preserve=sys.argv[1] == 'preserve')
    elif len(sys.argv) == 1:
        check_controls()
    else:
        sys.exit(__doc__)
