"""Run the translated vertex fetch of every buffer format gfx2hlsl lowers and read all four components.

usage: startup_vertex_fetch_formats_check.py <dxc> <offscreen_check> <adapter>
       startup_vertex_fetch_formats_check.py controls

tools/startup/gfx2hlsl.py lowers tbuffer_load_format_* for the supported buffer formats from a table of HLSL
expressions: the load of the word or words, the field each component takes and its conversion, and the
values of the missing components. Only BUF_FMT_8_UINT and BUF_FMT_8_UNORM were run before
(startup_r8_uint_check.py). This drives each table entry the way that check does: a source-built vertex
stage with one tbuffer_load_format_xyzw from vertex buffer 1 is translated, its generated vertex body is
called from a compute wrapper, and the four position components of 16 vertices are read back through
offscreen_check --compute-store-case and compared with a reference that decodes the same bytes from the
format definitions (little-endian fields from the lowest byte, UNORM n / (2^bits - 1), binary16 widened to
binary32, missing X, Y and Z as 0 and a missing W as 1.0, or 1 for an integer format).

Each format with sub-dword fields uses a stride and offset that put elements at more than one byte
alignment, so a field that crosses a dword is read through ldu(); the 32-bit formats keep aligned elements. UNORM components are compared as round(v * (2^bits - 1)), so the
check does not depend on how many ulps the division is off by; every other component is compared bit for bit.
Wrong references (channels swapped, a shift off by one field, a missing W read as 0, UNORM over 2^bits)
and mutated translations must fail the readback.
"""
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from startup_sampler_heap_check import root_blob
from startup_stream_check import gfx2hlsl

SENTINEL = 0xc5c5c5c5
ONE = 0x3f800000
# offscreen_check --compute-store-case fills the raw t0 with this little-endian pattern.
DATA = b''.join(struct.pack('<I', (0x8b02a7f1 + i * 0x413b1) & 0xffffffff) for i in range(320))


def f16(bits):
    return struct.unpack('<I', struct.pack('<f', struct.unpack('<e', struct.pack('<H', bits))[0]))[0]


# name: (format code, stride, offset, field bits, field kind, number of fields). Kinds: 'unorm' (compared as
# round(v * max)), 'uint', 'f16' and 'f32' (compared as bits). The code is the gfx10 unified format number.
FORMATS = {
    'BUF_FMT_8_UNORM': (1, 3, 1, 8, 'unorm', 1),
    'BUF_FMT_8_UINT': (5, 3, 1, 8, 'uint', 1),
    'BUF_FMT_8_8_UNORM': (14, 3, 2, 8, 'unorm', 2),
    'BUF_FMT_16_16_UNORM': (23, 6, 2, 16, 'unorm', 2),
    'BUF_FMT_16_16_SNORM': (24, 6, 2, 16, 'snorm', 2),
    'BUF_FMT_16_16_FLOAT': (29, 10, 10, 16, 'f16', 2),
    'BUF_FMT_16_16_16_16_FLOAT': (71, 18, 10, 16, 'f16', 4),
    'BUF_FMT_8_8_8_8_UNORM': (56, 5, 3, 8, 'unorm', 4),
    'BUF_FMT_8_8_8_8_UINT': (60, 5, 3, 8, 'uint', 4),
    'BUF_FMT_32_FLOAT': (22, 8, 4, 32, 'f32', 1),
    'BUF_FMT_32_32_FLOAT': (64, 12, 0, 32, 'f32', 2),
    'BUF_FMT_32_32_32_FLOAT': (74, 16, 4, 32, 'f32', 3),
    'BUF_FMT_32_32_32_32_FLOAT': (77, 20, 8, 32, 'f32', 4),
}


def snorm_words(fields, unsigned=False, no_clamp=False):
    """SNORM fields as the compared integers round(v * 32767): two's complement, -32768 clamped to -32767."""
    out = []
    for f in fields:
        v = f if unsigned else f - 0x10000 if f & 0x8000 else f
        out.append((v if no_clamp else max(v, -32767)) & 0xffffffff)
    return out


def reference(name, vertex, *, swap=False, shift=False, w_zero=False, unorm_pow2=False, unsigned=False, no_clamp=False):
    """The four compared words of one vertex, from the format definition. The keywords give wrong references."""
    _, stride, offset, bits, kind, count = FORMATS[name]
    address, size = vertex * stride + offset, bits // 8
    fields = [int.from_bytes(DATA[address + size * i:address + size * (i + 1)], 'little') for i in range(count)]
    if shift and bits < 32:
        # Each field read one field higher, as a wrong shift would; the last reads past the element.
        fields = [int.from_bytes(DATA[address + size * (i + 1):address + size * (i + 2)], 'little') for i in range(count)]
    if swap and count > 1:
        fields[0], fields[1] = fields[1], fields[0]
    if kind == 'unorm' and unorm_pow2:
        # v = field / 2^bits, compared as round(v * (2^bits - 1)).
        fields = [round(f * ((1 << bits) - 1) / (1 << bits)) for f in fields]
    if kind == 'snorm':
        fields = snorm_words(fields, unsigned=unsigned, no_clamp=no_clamp)
    words = [f16(f) if kind == 'f16' else f for f in fields]
    one = 1 if kind == 'uint' else ONE
    missing = [0, 0, 0, 0 if w_zero else one]
    return words + missing[count:]


def compared(name):
    """Return the integer readback scale for each normalized component."""
    _, _, _, bits, kind, count = FORMATS[name]
    scale = {'unorm': (1 << bits) - 1, 'snorm': (1 << (bits - 1)) - 1}.get(kind)
    return [scale if i < count else None for i in range(4)]


def expected(name, **wrong):
    words = [SENTINEL] * 320
    for vertex in range(16):
        words[vertex * 4:vertex * 4 + 4] = reference(name, vertex, **wrong)
    return words


def pipeline(name):
    code, _, offset, _, _, _ = FORMATS[name]
    # s_load_dwordx4 s[16:19], s[0:1], 16  (vertex-buffer descriptor 1)
    # tbuffer_load_format_xyzw v[4:7], v0, s[16:19], 0 idxen offset:<offset> format:<code>
    # exp pos0 v4, v5, v6, v7 done
    words = (0xf4080400, 0xfa000010, 0xe8032000 | offset | code << 19, 0x80040400,
             0xf80008cf, 0x07060504, 0xbf810000)
    vs = struct.pack('<7I', *words)
    ps = struct.pack('<3I', 0xf800180f, 0, 0xbf810000)
    result = gfx2hlsl.Pipeline(vs, ps, root_blob(flags=0))
    assert 'srv 0 buf vb 1' in result.manifest().splitlines()
    return result


def wrapper(name):
    parts = ['bits.%s' % c if scale is None else '(uint)(int)round(p.%s * %d.0)' % (c, scale)
             for c, scale in zip('xyzw', compared(name))]
    return '''
RWByteAddressBuffer outputWords : register(u0);
[numthreads(16, 1, 1)]
void readback(uint id : SV_GroupIndex) {
    float4 p = body(id).pos;
    uint4 bits = asuint(p);
    outputWords.Store4(id * 16, uint4(%s));
}
''' % ', '.join(parts)


# Translations a wrong table entry would produce: (format, label, generated text, replacement).
MUTATIONS = (
    ('BUF_FMT_8_8_8_8_UNORM', 'second byte from the third', 'U(((w >> 8) & 255) / 255.0)', 'U(((w >> 16) & 255) / 255.0)'),
    ('BUF_FMT_8_8_8_8_UNORM', 'fourth byte as 255', 'U((w >> 24) / 255.0)', 'U(1.0)'),
    ('BUF_FMT_8_8_8_8_UINT', 'normalized green byte', 'v5 = (w >> 8) & 255u;', 'v5 = U(((w >> 8) & 255u) / 255.0);'),
    ('BUF_FMT_8_8_8_8_UINT', 'blue byte from green', 'v6 = (w >> 16) & 255u;', 'v6 = (w >> 8) & 255u;'),
    ('BUF_FMT_8_8_8_8_UINT', 'alpha byte from blue', 'v7 = w >> 24;', 'v7 = (w >> 16) & 255u;'),
    ('BUF_FMT_8_8_UNORM', 'aligned load instead of ldu', 'ldu(r0, a)', 'r0.Load(a & ~3u)'),
    ('BUF_FMT_16_16_UNORM', 'high half over 65536', 'U((w >> 16) / 65535.0)', 'U((w >> 16) / 65536.0)'),
    ('BUF_FMT_16_16_SNORM', 'low half read unsigned', 'float(int(w << 16) >> 16)', 'float((w << 16) >> 16)'),
    ('BUF_FMT_16_16_FLOAT', 'low half from the high half', 'v4 = U(f16tof32(w));', 'v4 = U(f16tof32(w >> 16));'),
    ('BUF_FMT_16_16_16_16_FLOAT', 'second dword aligned down',
     'v7 = U(f16tof32(ldu(r0, a + 4) >> 16));', 'v7 = U(f16tof32(r0.Load((a + 4) & ~3u) >> 16));'),
    ('BUF_FMT_32_32_32_FLOAT', 'missing W as 0', 'U(1.0)', '0u'),
    ('BUF_FMT_32_32_32_32_FLOAT', 'W from the third word', 'r0.Load(a + 12)', 'r0.Load(a + 8)'),
)
# Wrong references; each must fail the readback of every format where it changes a word.
WRONG = (('swapped channels', dict(swap=True)), ('fields one higher', dict(shift=True)),
         ('missing W as 0', dict(w_zero=True)), ('UNORM over 2^bits', dict(unorm_pow2=True)),
         ('SNORM read unsigned', dict(unsigned=True)))

# The generated SNORM expressions evaluated on fixed halves (the device fills t0 with a fixed pattern that
# holds no 0x8000, so the boundary values are checked here, on the text the translator emits).
HALVES = {0x8000: -1.0, 0x8001: -1.0, 0x7fff: 1.0, 0x0000: 0.0, 0xffff: -1.0 / 32767.0, 0x4000: 16384 / 32767.0}


class Word(int):
    """A uint: shifts left wrap at 32 bits."""
    def __lshift__(self, n):
        return Word((int(self) << n) & 0xffffffff)


def to_int(x):
    x = int(x) & 0xffffffff
    return x - (1 << 32) if x >> 31 else x


def snorm_value(expression, word):
    """Evaluate one generated component expression as HLSL would (uint w, int() as a bit cast)."""
    text = expression.replace('U(', 'identity(').replace('int(', 'to_int(')
    return eval(text, {'identity': lambda x: x, 'to_int': to_int, 'w': Word(word), 'max': max, 'float': float})


def snorm_boundaries(components):
    """Raise AssertionError unless both halves of the word decode every HALVES entry exactly."""
    for half, value in HALVES.items():
        for low_first in (True, False):
            other = 0x1234  # a nonzero neighbour: the other half must not leak in
            word = (other << 16 | half) if low_first else (half << 16 | other)
            got = snorm_value(components[0 if low_first else 1], word)
            assert abs(got - value) < 1e-6, (hex(half), low_first, got, value)


def controls():
    """Translation-side checks: every table entry is reached, and the reference is not degenerate."""
    for name in FORMATS:
        source = pipeline(name).hlsl_vs()
        assert 'v7' in source, name
    for name, label, old, new in MUTATIONS:
        source = pipeline(name).hlsl_vs()
        assert old in source, (name, label)
    # SNORM: the emitted expressions hit the boundaries, and each wrong form is caught by the same check.
    components = gfx2hlsl.FETCH_FORMATS['BUF_FMT_16_16_SNORM'][1][:2]
    snorm_boundaries(components)
    wrong_forms = (('read unsigned', lambda c: c.replace('int(w << 16) >> 16', '(w << 16) >> 16').replace('int(w) >> 16', 'w >> 16')),
                   ('no clamp', lambda c: c.replace('max(', '(').replace(', -1.0)', ')')),
                   ('divide by 32768', lambda c: c.replace('32767.0', '32768.0')),
                   ('low half only', lambda c: components[0]))
    for label, edit in wrong_forms:
        try:
            snorm_boundaries([edit(c) for c in components])
        except AssertionError:
            continue
        raise AssertionError('SNORM wrong form %r escaped detection' % label)
    # The reference reads every byte of each element: changing any byte of vertex 5 changes a word.
    for name, (_, stride, offset, bits, kind, count) in FORMATS.items():
        for label, wrong in WRONG:
            if expected(name, **wrong) != expected(name):
                break
        else:
            raise AssertionError('no wrong reference changes %s' % name)
    # 16-bit floats in the chosen elements are normal, so f16tof32 is exact on every adapter.
    for name, (_, stride, offset, _, kind, count) in FORMATS.items():
        if kind != 'f16':
            continue
        for vertex in range(16):
            address = vertex * stride + offset
            halves = struct.unpack_from('<' + 'H' * count, DATA, address)
            assert all(0 < ((half >> 10) & 31) < 31 for half in halves), (name, vertex, halves)
            if count == 4:
                assert len(set(halves)) == 4, (name, vertex, halves)
    # The words of the 32-bit formats are finite and normal, so the position passes through unchanged.
    for name in ('BUF_FMT_32_FLOAT', 'BUF_FMT_32_32_FLOAT', 'BUF_FMT_32_32_32_FLOAT', 'BUF_FMT_32_32_32_32_FLOAT'):
        _, _, _, _, _, count = FORMATS[name]
        for vertex in range(16):
            for word in reference(name, vertex)[:count]:
                assert 0 < (word >> 23) & 0xff < 0xff, (name, vertex, hex(word))
    # Several elements of each format with sub-dword fields sit at different byte alignments; the 32-bit
    # formats are read with aligned Load() and keep 4-byte aligned elements.
    for name, (_, stride, offset, bits, _, _) in FORMATS.items():
        alignments = {(v * stride + offset) % 4 for v in range(16)}
        assert len(alignments) > 1 if bits < 32 else alignments == {0}, name


def compile_fetch(directory, compiler, name, source):
    hlsl, binary = directory / 'fetch.hlsl', directory / 'fetch.cso'
    hlsl.write_text(source, encoding='ascii')
    # Compile the vertex entry as the startup tools do, then the same body in the compute wrapper.
    subprocess.run([compiler, '-T', 'vs_6_0', '-E', 'main', '-Fo', binary, hlsl], check=True)
    with hlsl.open('a', encoding='ascii') as handle:
        handle.write(wrapper(name))
    subprocess.run([compiler, '-T', 'cs_6_0', '-E', 'readback', '-Fo', binary, hlsl], check=True)
    return binary


def run_reference(directory, consumer, adapter, binary, name, words):
    case = directory / 'fetch.case'
    constants = [0] * 16
    constants[pipeline(name).uds.index(('stride', 0))] = FORMATS[name][1]
    case.write_bytes(struct.pack('<337I', 1, *words, *constants))
    return subprocess.run([consumer, adapter, '--compute-store-case', binary, case], capture_output=True, text=True)


def check(directory, compiler, consumer, adapter):
    for name in FORMATS:
        source = pipeline(name).hlsl_vs()
        binary = compile_fetch(directory, compiler, name, source)
        correct = expected(name)
        result = run_reference(directory, consumer, adapter, binary, name, correct)
        if result.returncode == 77:
            print(result.stdout, result.stderr)
            return 77
        if result.returncode:
            raise RuntimeError('%s: %s%s' % (name, result.stdout, result.stderr))
        print('%s: 16 vertices, stride %d, offset %d: %s' % (
            name, FORMATS[name][1], FORMATS[name][2],
            [line for line in result.stdout.splitlines() if 'mismatches=' in line][0]))
        for label, wrong in WRONG:
            words = expected(name, **wrong)
            if words == correct:
                continue
            first = next(i for i, (a, b) in enumerate(zip(words, correct)) if a != b)
            bad = run_reference(directory, consumer, adapter, binary, name, words)
            if bad.returncode != 1 or 'word=%d expected=' % first not in bad.stderr:
                raise AssertionError('%s: reference %r escaped detection\n%s' % (name, label, bad.stderr))
    for name, label, old, new in MUTATIONS:
        source = pipeline(name).hlsl_vs()
        binary = compile_fetch(directory, compiler, name, source.replace(old, new, 1))
        bad = run_reference(directory, consumer, adapter, binary, name, expected(name))
        if bad.returncode != 1 or 'Compute readback differs' not in bad.stderr:
            raise AssertionError('%s: mutation %r escaped detection\n%s' % (name, label, bad.stderr))
        print('%s: mutation %r fails' % (name, label))
    print('%s: %d buffer formats, 16 vertices each, every wrong reference and %d mutations fail'
          % (adapter, len(FORMATS), len(MUTATIONS)))
    return 0


if __name__ == '__main__':
    controls()
    if sys.argv[1:] == ['controls']:
        print('vertex fetch format controls passed')
        sys.exit(0)
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    with tempfile.TemporaryDirectory() as temp:
        sys.exit(check(Path(temp), *sys.argv[1:]))
