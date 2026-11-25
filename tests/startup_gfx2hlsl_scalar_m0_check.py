"""Scalar writes to and symbolic tracking of m0 in tools/startup/gfx2hlsl.py.

usage: startup_gfx2hlsl_scalar_m0_check.py [controls]
       startup_gfx2hlsl_scalar_m0_check.py binary <llvm-mc>
       startup_gfx2hlsl_scalar_m0_check.py <dxc> <offscreen_check> <warp|hardware> [fxc]

Previously, Stage.scalar() returned early whenever dname == 'm0', silently dropping every scalar
ALU write (and its SCC update) to m0 even though u('m0') and op_s_buffer_load_dword() read m0 at
run time. In addition, scalar_select(), op_s_bfm_b32(), op_s_and_saveexec_b64() and
op_s_andn2_wrexec_b64() subscripted reg_range(ops[0]) directly, raising TypeError on m0, and
64-bit scalar ops accepted or crashed on m0 (sdst/ssrc 124).

All instruction bytes below are constructed from the RDNA 2 / tools/disasm_rdna2.py bit layouts.
With no arguments (or 'controls'), this script checks decoding, refusals, symbolic pointer/
descriptor propagation through m0 and live monkeypatch controls without a GPU. Given DXC and
offscreen_check, it runs two compute-wrapped vertex buffer-load fixtures on WARP or hardware
(plus FXC SM 5.1 compilation when FXC is supplied) and checks that mutations and wrong references
fail readback.
"""
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'tools'))
sys.path.insert(0, str(HERE.parent / 'tools' / 'startup'))
import disasm_rdna2 as D  # noqa: E402
import gfx2hlsl as G  # noqa: E402

SENTINEL = 0xc5c5c5c5
DATA = b''.join(struct.pack('<I', (0x8b02a7f1 + i * 0x413b1) & 0xffffffff) for i in range(320))

SOP1_MOV_B32 = 0x03
SOP1_MOV_B64 = 0x04
SOP1_AND_SAVEEXEC_B64 = 0x24
SOP1_ANDN2_WREXEC_B64 = 0x3a
SOP1_AND_SAVEEXEC_B32 = 0x3c
SOP1_ANDN2_WREXEC_B32 = 0x47

SOP2_ADD_U32 = 0x00
SOP2_CSELECT_B32 = 0x0a
SOP2_CSELECT_B64 = 0x0b
SOP2_AND_B32 = 0x0e
SOP2_AND_B64 = 0x0f
SOP2_LSHL_B32 = 0x1e
SOP2_BFM_B32 = 0x24

SOPK_MOVK_I32 = 0x00

SMEM_LOAD_DWORDX4 = 0x02
SMEM_BUFFER_LOAD_DWORD = 0x08
SMEM_BUFFER_LOAD_DWORDX2 = 0x09
SMEM_BUFFER_LOAD_DWORDX4 = 0x0a

M0 = 124


def encode_sop1(op, sdst, ssrc0):
    """Construct a 4-byte RDNA 2 SOP1 instruction."""
    w0 = 0xbe800000 | ((sdst & 0x7f) << 16) | ((op & 0xff) << 8) | (ssrc0 & 0xff)
    return struct.pack('<I', w0)


def encode_sop2(op, sdst, ssrc0, ssrc1):
    """Construct a 4-byte RDNA 2 SOP2 instruction."""
    w0 = 0x80000000 | ((op & 0x7f) << 23) | ((sdst & 0x7f) << 16) | ((ssrc1 & 0xff) << 8) | (ssrc0 & 0xff)
    return struct.pack('<I', w0)


def encode_sopk(op, sdst, simm16):
    """Construct a 4-byte RDNA 2 SOPK instruction."""
    w0 = 0xb0000000 | ((op & 0x1f) << 23) | ((sdst & 0x7f) << 16) | (simm16 & 0xffff)
    return struct.pack('<I', w0)


def encode_smem(op, sdata, sbase, *, soffset=125, imm=0):
    """Construct an 8-byte RDNA 2 SMEM instruction."""
    w0 = (0b111101 << 26) | ((op & 0xff) << 18) | ((sdata & 0x7f) << 6) | ((sbase >> 1) & 0x3f)
    w1 = ((soffset & 0x7f) << 25) | (imm & 0x1fffff)
    return struct.pack('<II', w0, w1)


def encode_vop1(op, vdst, src0):
    """Construct a 4-byte RDNA 2 VOP1 instruction."""
    w0 = 0x7e000000 | ((vdst & 0xff) << 17) | ((op & 0xff) << 9) | (src0 & 0x1ff)
    return struct.pack('<I', w0)


def root_with_one_constant():
    """Minimal DXBC RTS0 container declaring one 32-bit root constant (placed at s8)."""
    header = struct.pack('<6I', 2, 1, 24, 0, 0, 0)
    param = struct.pack('<3I', 1, 0, 36)
    const = struct.pack('<3I', 0, 0, 1)
    rts0 = header + param + const
    part = b'RTS0' + struct.pack('<I', len(rts0)) + rts0
    return b'DXBC' + bytes(16) + struct.pack('<III', 1, 36 + len(part), 1) + struct.pack('<I', 36) + part


def root_with_pointer_at_s8():
    """DXBC RTS0 container declaring one root SRV (ptype=3, placed at s8:s9)."""
    header = struct.pack('<6I', 2, 1, 24, 0, 0, 0)
    param = struct.pack('<3I', 3, 0, 36)
    desc = struct.pack('<2I', 0, 0)
    rts0 = header + param + desc
    part = b'RTS0' + struct.pack('<I', len(rts0)) + rts0
    return b'DXBC' + bytes(16) + struct.pack('<III', 1, 36 + len(part), 1) + struct.pack('<I', 36) + part


class DummyPipeline:
    """Minimal Pipeline stand-in for isolated Stage instruction checks."""

    def __init__(self):
        self.resources = {}
        self.uds = [('rect',)]
        self.notes = set()
        self.modified = set()
        self.sampler_heap = False

    def ud(self, source):
        if source not in self.uds:
            self.uds.append(source)
        return self.uds.index(source)

    def resource(self, key, kind):
        entry = self.resources.setdefault(key, dict(index=len(self.resources), kind=kind))
        return 'r%d' % entry['index']


class ProbeStage(G.Stage):
    """Isolated Stage with root constant s8, constant s5, root pointer s[10:11] and descriptor s[16:19]."""

    def __init__(self):
        self.pipe = DummyPipeline()
        self.kind = 'vs'
        self.lines = []
        self.sym = {
            5: ('const', 4),
            8: ('ud', self.pipe.ud(('rootconst', 0, 0))),
            10: ('ptr', ('root', 1)),
            11: ('ptrhi', ('root', 1)),
            **{16 + i: ('desc', ('vbtab',), 16, i) for i in range(4)},
        }
        self.used_s, self.used_v, self.uniform_v = set(), set(), set()
        self.exec_known = True
        self.exports, self.attrs, self.masks = {}, {}, set()


def run_seq(*chunks):
    stage = ProbeStage()
    for code in chunks:
        _, name, asm, _ = D.decode_instruction(code, 0)
        stage.one(name, asm)
    return stage


def must_refuse_seq(reason, *chunks):
    try:
        run_seq(*chunks)
    except G.Unsupported as exc:
        assert reason in str(exc), f'expected {reason!r} in {str(exc)!r}'
    else:
        raise AssertionError(f'expected Unsupported({reason!r}), but translation succeeded')


def controls():
    # 1. 32-bit scalar writes to m0 emit m0 assignments (and scc when defined) instead of vanishing.
    st_mov = run_seq(encode_sop1(SOP1_MOV_B32, M0, 8))
    assert any(line.startswith('m0 = ') and 'ud1' in line for line in st_mov.lines), st_mov.lines

    st_movk = run_seq(encode_sopk(SOPK_MOVK_I32, M0, 0x30))
    assert any(line.startswith('m0 = ') and '0x00000030u' in line for line in st_movk.lines), st_movk.lines

    st_lshl = run_seq(encode_sop2(SOP2_LSHL_B32, M0, 8, 128 + 4))
    assert any(line.startswith('m0 = ') for line in st_lshl.lines), st_lshl.lines
    assert any(line == 'scc = m0 != 0;' for line in st_lshl.lines), st_lshl.lines

    st_csel = run_seq(encode_sop2(SOP2_CSELECT_B32, M0, 8, 128 + 48))
    assert any(line.startswith('m0 = scc ? ') for line in st_csel.lines), st_csel.lines

    st_bfm_const = run_seq(encode_sop2(SOP2_BFM_B32, M0, 5, 128 + 2))
    assert st_bfm_const.lines == ['m0 = 0x0000003cu;'], st_bfm_const.lines

    st_bfm_dyn = run_seq(encode_sop2(SOP2_BFM_B32, M0, 8, 128 + 2))
    assert any(line.startswith('m0 = ') and 'ud1' in line for line in st_bfm_dyn.lines), st_bfm_dyn.lines

    # m0 as a 32-bit wrexec source is accepted.
    st_wrexec_src = run_seq(encode_sop1(SOP1_ANDN2_WREXEC_B32, 2, M0))
    assert any('m0' in line and 'exec =' in line for line in st_wrexec_src.lines), st_wrexec_src.lines

    # 2. Pixel interpolation prologue moving a pointer-slot SGPR into m0 stays benign when m0 is not read,
    #    while any numeric read of m0 holding a pointer/descriptor is refused (and cleared on numeric overwrite).
    vs_interp = b''.join((
        encode_vop1(0x01, 4, 242),                                   # v_mov_b32_e32 v4, 1.0
        encode_vop1(0x01, 5, 128),                                   # v_mov_b32_e32 v5, 0
        struct.pack('<2I', 0xf80008cf, 0x04050505),                  # exp pos0 v5, v5, v5, v4 done
        struct.pack('<2I', 0xf800020f, 0x04040404),                  # exp param0 v4, v4, v4, v4
        struct.pack('<I', 0xbf810000),                               # s_endpgm
    ))
    ps_interp = b''.join((
        encode_sop1(SOP1_MOV_B32, M0, 8),                            # s_mov_b32 m0, s8 (s8 holds ('ptr', ('root', 0)))
        bytes.fromhex('020002c8' '0f1800f800000000' '000081bf'),     # v_interp_mov_f32 v0, p0, attr0.x; exp mrt0; end
    ))
    pipe_interp = G.Pipeline(vs_interp, ps_interp, root_with_pointer_at_s8())
    assert 'i.fparam0.x' in pipe_interp.hlsl_ps()

    must_refuse_seq('numeric use of m0 holding ptr',
                    encode_sop1(SOP1_MOV_B32, M0, 10),
                    encode_smem(SMEM_BUFFER_LOAD_DWORD, 20, 16, soffset=M0))
    must_refuse_seq('numeric use of m0 holding ptr',
                    encode_sop1(SOP1_MOV_B32, M0, 10),
                    encode_vop1(0x01, 4, M0))
    must_refuse_seq('numeric use of m0 holding descnum',
                    encode_sop1(SOP1_MOV_B32, M0, 16),
                    encode_sop2(SOP2_ADD_U32, M0, M0, 128 + 4),
                    encode_vop1(0x01, 4, M0))
    for width, shift in ((M0, 128 + 2), (128 + 5, M0)):
        must_refuse_seq('numeric use of m0 holding ptr',
                        encode_sop1(SOP1_MOV_B32, M0, 10),
                        encode_sop2(SOP2_BFM_B32, M0, width, shift))
    must_refuse_seq('scc from descriptor data',
                    encode_sop2(SOP2_AND_B32, M0, 16, 128 + 1))

    # Copying m0 into an SGPR propagates the symbolic pointer, and overwriting m0 numerically clears the tag.
    st_prop = run_seq(encode_sop1(SOP1_MOV_B32, M0, 10), encode_sop1(SOP1_MOV_B32, 4, M0))
    assert st_prop.sym.get(4) == ('ptr', ('root', 1)), st_prop.sym

    for writer in (
        encode_sop1(SOP1_MOV_B32, M0, 8),
        encode_sop2(SOP2_CSELECT_B32, M0, 8, 128 + 48),
        encode_sop2(SOP2_BFM_B32, M0, 5, 128 + 2),
        encode_sop2(SOP2_BFM_B32, M0, 8, 128 + 2),
    ):
        st_clear = run_seq(encode_sop1(SOP1_MOV_B32, M0, 10), writer, encode_vop1(0x01, 4, M0))
        assert 'm0' not in st_clear.sym and any('v4 = m0;' in line for line in st_clear.lines), st_clear.lines

    # 3. 64-bit scalar ops and saveexec/wrexec destinations reject m0 with Unsupported (never TypeError).
    for code in (
        encode_sop1(SOP1_MOV_B64, M0, 2),
        encode_sop1(SOP1_MOV_B64, 2, M0),
        encode_sop2(SOP2_AND_B64, M0, 2, 4),
        encode_sop2(SOP2_AND_B64, 2, M0, 4),
        encode_sop2(SOP2_CSELECT_B64, M0, 2, 4),
        encode_sop2(SOP2_CSELECT_B64, 2, M0, 4),
    ):
        must_refuse_seq('64-bit scalar operand m0', code)

    for op in (SOP1_AND_SAVEEXEC_B64, SOP1_AND_SAVEEXEC_B32):
        must_refuse_seq('saveexec destination m0', encode_sop1(op, M0, 2))
    for op in (SOP1_ANDN2_WREXEC_B64, SOP1_ANDN2_WREXEC_B32):
        must_refuse_seq('wrexec destination m0', encode_sop1(op, M0, 2))

    # 4. Live in-process control: restoring the pre-fix early return in Stage.scalar drops the m0 write.
    real_scalar = G.Stage.scalar

    def dropped_m0_scalar(self, base, ops):
        if ops[0] == 'm0':
            return None
        return real_scalar(self, base, ops)

    G.Stage.scalar = dropped_m0_scalar
    try:
        st_dropped = run_seq(encode_sop1(SOP1_MOV_B32, M0, 8))
        assert not st_dropped.lines, st_dropped.lines
    finally:
        G.Stage.scalar = real_scalar

    # 5. All 32-bit words exported through float4 pos in the GPU buffer cases are normal IEEE-754 floats.
    for s8 in (0, 2, 3):
        for words in (expected_lshl_cselect_m0(s8), expected_movk_bfm_m0(s8)):
            for word in words[:64]:
                assert 0 < ((word >> 23) & 0xff) < 0xff, hex(word)


# ---- D3D12 compute-wrapped VS readback cases -----------------------------------------------------------

def build_lshl_cselect_m0_pipeline():
    """VS: m0 = s8 << 4 (sets scc); m0 = scc ? m0 : 48; m0 += 8; s_buffer_load_dwordx4 s[20:23], s[16:19], m0."""
    vs = b''.join((
        encode_smem(SMEM_LOAD_DWORDX4, 16, 0, soffset=125, imm=0x10),
        encode_sop2(SOP2_LSHL_B32, M0, 8, 128 + 4),                  # s_lshl_b32 m0, s8, 4 (writes m0 and scc)
        encode_sop2(SOP2_CSELECT_B32, M0, M0, 128 + 48),             # s_cselect_b32 m0, m0, 48
        encode_sop2(SOP2_ADD_U32, M0, M0, 128 + 8),                  # s_add_u32 m0, m0, 8
        encode_smem(SMEM_BUFFER_LOAD_DWORDX4, 20, 16, soffset=M0),
        encode_vop1(0x01, 4, 20),
        encode_vop1(0x01, 5, 21),
        encode_vop1(0x01, 6, 22),
        encode_vop1(0x01, 7, 23),
        struct.pack('<3I', 0xf80008cf, 0x07060504, 0xbf810000),
    ))
    ps = struct.pack('<3I', 0xf800180f, 0, 0xbf810000)
    return G.Pipeline(vs, ps, root_with_one_constant())


def build_movk_bfm_m0_pipeline():
    """VS: s_movk_i32 m0, 0x20 -> loadx2; s_bfm_b32 m0, s8, 2 -> loadx2."""
    vs = b''.join((
        encode_smem(SMEM_LOAD_DWORDX4, 16, 0, soffset=125, imm=0x10),
        encode_sopk(SOPK_MOVK_I32, M0, 0x20),                        # m0 = 32
        encode_smem(SMEM_BUFFER_LOAD_DWORDX2, 20, 16, soffset=M0),
        encode_sop2(SOP2_BFM_B32, M0, 8, 128 + 2),                   # m0 = ((1 << s8) - 1) << 2
        encode_smem(SMEM_BUFFER_LOAD_DWORDX2, 22, 16, soffset=M0),
        encode_vop1(0x01, 4, 20),
        encode_vop1(0x01, 5, 21),
        encode_vop1(0x01, 6, 22),
        encode_vop1(0x01, 7, 23),
        struct.pack('<3I', 0xf80008cf, 0x07060504, 0xbf810000),
    ))
    ps = struct.pack('<3I', 0xf800180f, 0, 0xbf810000)
    return G.Pipeline(vs, ps, root_with_one_constant())


def expected_lshl_cselect_m0(s8, *, zero_m0=False, drop_scc=False):
    if zero_m0:
        addr = 0
    else:
        shifted = (s8 << 4) & 0xffffffff
        scc = False if drop_scc else (shifted != 0)
        selected = shifted if scc else 48
        addr = (selected + 8) & 0xffffffff
    quad = list(struct.unpack_from('<4I', DATA, addr))
    return quad * 16 + [SENTINEL] * (320 - 64)


def expected_movk_bfm_m0(s8, *, zero_m0=False):
    addr_a = 0 if zero_m0 else 0x20
    addr_b = 0 if zero_m0 else ((((1 << (s8 & 31)) - 1) << 2) & 0xffffffff)
    quad = list(struct.unpack_from('<2I', DATA, addr_a)) + list(struct.unpack_from('<2I', DATA, addr_b))
    return quad * 16 + [SENTINEL] * (320 - 64)


def wrapper():
    return '''
RWByteAddressBuffer outputWords : register(u0);
[numthreads(16, 1, 1)]
void readback(uint id : SV_GroupIndex) {
    float4 p = body(id).pos;
    outputWords.Store4(id * 16, asuint(p));
}
'''


def run_case(directory, compiler, consumer, adapter, pipe, vs_source, words, *, s8=0, fxc=None):
    hlsl, binary, case = (directory / n for n in ('m0.hlsl', 'm0.cso', 'm0.case'))
    constants = [0] * 16
    if ('rootconst', 0, 0) in pipe.uds:
        constants[pipe.uds.index(('rootconst', 0, 0))] = s8
    case.write_bytes(struct.pack('<337I', 1, *words, *constants))
    hlsl.write_text(vs_source, encoding='ascii')
    if fxc:
        ps_hlsl = directory / 'm0_ps.hlsl'
        ps_hlsl.write_text(pipe.hlsl_ps(), encoding='ascii')
        subprocess.run([fxc, '/nologo', '/T', 'vs_5_1', '/E', 'main', '/Fo', str(directory / 'vs51.cso'), str(hlsl)],
                       check=True, capture_output=True, text=True)
        subprocess.run([fxc, '/nologo', '/T', 'ps_5_1', '/E', 'main', '/Fo', str(directory / 'ps51.cso'), str(ps_hlsl)],
                       check=True, capture_output=True, text=True)
    subprocess.run([compiler, '-T', 'vs_6_0', '-E', 'main', '-Fo', str(binary), str(hlsl)], check=True)
    with hlsl.open('a', encoding='ascii') as handle:
        handle.write(wrapper())
    subprocess.run([compiler, '-T', 'cs_6_0', '-E', 'readback', '-Fo', str(binary), str(hlsl)], check=True)
    return subprocess.run([consumer, adapter, '--compute-store-case', str(binary), str(case)],
                          capture_output=True, text=True)


def check_binary(llvm_mc):
    cases = (
        ('s_mov_b32 m0, s8', encode_sop1(SOP1_MOV_B32, M0, 8)),
        ('s_movk_i32 m0, 0x30', encode_sopk(SOPK_MOVK_I32, M0, 0x30)),
        ('s_lshl_b32 m0, s8, 4', encode_sop2(SOP2_LSHL_B32, M0, 8, 128 + 4)),
        ('s_cselect_b32 m0, m0, 48', encode_sop2(SOP2_CSELECT_B32, M0, M0, 128 + 48)),
        ('s_add_u32 m0, m0, 8', encode_sop2(SOP2_ADD_U32, M0, M0, 128 + 8)),
        ('s_bfm_b32 m0, s8, 2', encode_sop2(SOP2_BFM_B32, M0, 8, 128 + 2)),
        ('s_andn2_wrexec_b32 s2, m0', encode_sop1(SOP1_ANDN2_WREXEC_B32, 2, M0)),
    )
    source = '\n'.join(asm for asm, _ in cases) + '\n'
    want = b''.join(raw for _, raw in cases)
    proc = subprocess.run([llvm_mc, '-arch=amdgcn', '-mcpu=gfx1013', '-show-encoding'],
                          input=source, capture_output=True, text=True, check=True)
    built = bytearray()
    for line in proc.stdout.splitlines():
        m = re.search(r'encoding: \[([^\]]*)\]', line)
        if m:
            built += bytes(int(b, 16) for b in m.group(1).split(','))
    if bytes(built) != want:
        raise AssertionError(f'llvm-mc encoding mismatch: got {bytes(built).hex()}, expected {want.hex()}')
    print('scalar m0 encodings match llvm-mc')
    return 0


def check_gpu(directory, compiler, consumer, adapter, fxc=None):
    # 1. s_lshl_b32 (setting m0 + scc) -> s_cselect_b32 -> s_add_u32 -> s_buffer_load_dwordx4 at s8=0 and s8=2.
    p_sel = build_lshl_cselect_m0_pipeline()
    src_sel = p_sel.hlsl_vs()
    for s8 in (0, 2):
        ref = expected_lshl_cselect_m0(s8)
        res = run_case(directory, compiler, consumer, adapter, p_sel, src_sel, ref,
                       s8=s8, fxc=fxc if s8 == 0 else None)
        if res.returncode == 77:
            print(res.stdout, res.stderr)
            return 77
        if res.returncode:
            raise RuntimeError(f'lshl+cselect+add m0 (s8={s8}):\n' + res.stdout + res.stderr)
        print(f'm0 lshl+cselect+add (s8={s8}):',
              [l for l in res.stdout.splitlines() if 'mismatches=' in l][0])

    # 2. s_movk_i32 m0, 0x20 and runtime s_bfm_b32 m0, s8, 2 (s8=3 -> m0=28).
    p_bfm = build_movk_bfm_m0_pipeline()
    src_bfm = p_bfm.hlsl_vs()
    ref_bfm = expected_movk_bfm_m0(3)
    res_bfm = run_case(directory, compiler, consumer, adapter, p_bfm, src_bfm, ref_bfm, s8=3, fxc=fxc)
    if res_bfm.returncode:
        raise RuntimeError('movk+bfm m0:\n' + res_bfm.stdout + res_bfm.stderr)
    print('m0 movk+bfm (s8=3):', [l for l in res_bfm.stdout.splitlines() if 'mismatches=' in l][0])

    # 3. Wrong references (pre-fix m0==0 or dropped scc update) must fail at their first differing word.
    ref_sel_2 = expected_lshl_cselect_m0(2)
    for label, pipe, src, s8, good, bad in (
        ('lshl+cselect reference with m0 stuck at 0', p_sel, src_sel, 2,
         ref_sel_2, expected_lshl_cselect_m0(2, zero_m0=True)),
        ('lshl+cselect reference without scc update', p_sel, src_sel, 2,
         ref_sel_2, expected_lshl_cselect_m0(2, drop_scc=True)),
        ('movk+bfm reference with m0 stuck at 0', p_bfm, src_bfm, 3,
         ref_bfm, expected_movk_bfm_m0(3, zero_m0=True)),
    ):
        first = next(i for i, (a, b) in enumerate(zip(bad, good)) if a != b)
        fail = run_case(directory, compiler, consumer, adapter, pipe, src, bad, s8=s8)
        if fail.returncode != 1 or f'word={first} expected=' not in fail.stderr:
            raise AssertionError(f'{label} escaped detection:\n{fail.stderr}')
        print(f'control {label!r}: fails at word {first} as expected')

    # 4. Translation mutations (dropping all m0 assignments or dropping scc = m0 != 0) must fail readback.
    no_m0_writes = re.sub(r'^\s*m0 = .*;\n', '', src_sel, flags=re.M)
    assert no_m0_writes != src_sel
    no_scc_write = src_sel.replace('scc = m0 != 0;', '', 1)
    assert no_scc_write != src_sel
    for label, mutant in (
        ('pre-fix dropped m0 assignments', no_m0_writes),
        ('dropped scc update on s_lshl_b32 m0', no_scc_write),
    ):
        fail = run_case(directory, compiler, consumer, adapter, p_sel, mutant, ref_sel_2, s8=2)
        if fail.returncode != 1 or 'Compute readback differs' not in fail.stderr:
            raise AssertionError(f'mutation {label!r} escaped detection:\n{fail.stderr}')
        print(f'mutation {label!r}: fails readback as expected')

    print(f'{adapter}: scalar m0 GPU readbacks, wrong references and mutations verified')
    return 0


def main():
    controls()
    if len(sys.argv) == 1 or sys.argv[1:] == ['controls']:
        print('startup scalar m0 controls passed')
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == 'binary':
        return check_binary(sys.argv[2])
    if len(sys.argv) not in (4, 5):
        raise SystemExit(__doc__)
    dxc, consumer, adapter = sys.argv[1:4]
    fxc = sys.argv[4] if len(sys.argv) == 5 else None
    with tempfile.TemporaryDirectory() as temp:
        return check_gpu(Path(temp), dxc, consumer, adapter, fxc=fxc)


if __name__ == '__main__':
    sys.exit(main())
