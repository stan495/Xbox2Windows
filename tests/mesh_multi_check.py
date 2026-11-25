"""Translate a source-built mesh program that allocates several vertices and triangles.

One gfx1013 program reads its allocation word from the geometry binding. Thread i
exports vertex i at column i // 2 and row i % 2 of a skewed strip, and primitive
(i, i + 1, i + 2) for each i below the primitive count. Three allocations are drawn:
a two-triangle quad, a ten-triangle strip and a 126-triangle strip that spans both
waves. An exact rational rasterizer, independent of the translator, gives the
expected 32x32 target and export trace.
"""

from fractions import Fraction
from pathlib import Path
import json
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from mesh_entry_aot import lower

SIDE = 32
# Declared PSV0 output bounds of the binary container: large enough for every case.
LIMITS = (128, 126)
PROGRAM = """
s_mov_b32 s10, s17
s_mov_b32 s11, 0
s_load_dwordx4 s[24:27], s[10:11], 0x20
s_load_dwordx4 s[16:19], s[10:11], 0x60
s_waitcnt lgkmcnt(0)
s_buffer_load_dword s15, s[24:27], null
s_buffer_load_dwordx4 s[28:31], s[16:19], null
s_buffer_load_dword s32, s[16:19], 0x10
s_waitcnt lgkmcnt(0)
s_and_b32 s6, s15, 0xfff
s_lshr_b32 s7, s15, 12
s_mov_b32 s40, 0x100401
s_mov_b32 s41, 0x200400
v_mbcnt_lo_u32_b32 v0, -1, 0
v_mbcnt_hi_u32_b32 v0, -1, v0
s_bfe_u32 s8, s3, 0x40018
v_lshl_add_u32 v0, s8, 6, v0
s_mov_b64 s[22:23], exec
v_cmpx_eq_u32_e32 0, v0
s_cbranch_execz 2
s_mov_b32 m0, s15
s_sendmsg sendmsg(MSG_GS_ALLOC_REQ)
s_mov_b64 exec, s[22:23]
v_cmpx_gt_u32_e32 s7, v0
v_mad_u32_u24 v9, v0, s40, s41
exp prim, v9, off, off, off done
s_mov_b64 exec, s[22:23]
v_cmpx_gt_u32_e32 s6, v0
v_lshrrev_b32_e32 v6, 1, v0
v_and_b32_e32 v7, 1, v0
v_cvt_f32_u32_e32 v6, v6
v_cvt_f32_u32_e32 v7, v7
v_fma_f32 v1, v6, s29, s28
v_fma_f32 v2, v7, s31, s30
v_fma_f32 v2, v6, s32, v2
v_mov_b32_e32 v3, 0.5
v_mov_b32_e32 v4, 1.0
exp pos0, v1, v2, v3, v4 done
s_endpgm
""".strip().splitlines()

# name: (vertices, primitives, x0, dx, y0, dy, skew) in clip space, all exact binary32 values.
CASES = {
    "quad": (4, 2, -0.671875, 1.25, -0.53125, 1.0, 0.125),
    "strip": (12, 10, -0.796875, 0.3125, -0.609375, 1.0, 0.09375),
    "long_strip": (128, 126, -0.84521484375, 0.02587890625, -0.8671875, 1.1162109375, 0.00341796875),
}
PIXEL = "float4 main() : SV_Target { return float4(0,1,0,1); }"


def word(case):
    vertices, primitives = CASES[case][:2]
    return vertices | primitives << 12


def constants(case):
    data = bytearray(1024)
    struct.pack_into("<5f", data, 0, *CASES[case][2:])
    return bytes(data)


def vertices(case):
    count, _, x0, dx, y0, dy, skew = (Fraction(v) for v in CASES[case])
    out = []
    for i in range(int(count)):
        column, row = i // 2, i % 2
        x, y = x0 + column * dx, y0 + row * dy + column * skew
        for value in (x, y):
            if Fraction(struct.unpack("<f", struct.pack("<f", float(value)))[0]) != value:
                raise AssertionError("Case position is not an exact binary32 value")
        out.append((x, y, Fraction(1, 2), Fraction(1)))
    return out


def primitives(case):
    return [(i, i + 1, i + 2) for i in range(CASES[case][1])]


def raster(points, triangles):
    """Cover pixel centers on the 32x32 viewport. No center may lie on any edge."""
    screen = [((x + 1) * SIDE / 2, (1 - y) * SIDE / 2) for x, y, _, _ in points]
    covered = set()
    for tri in triangles:
        a, b, c = (screen[i] for i in tri)
        if (b[0] - a[0]) * (c[1] - a[1]) == (b[1] - a[1]) * (c[0] - a[0]):
            continue  # A zero-area triangle covers nothing.
        # Only centers inside the bounding box can be covered or lie on an edge.
        xs, ys = sorted(v[0] for v in (a, b, c)), sorted(v[1] for v in (a, b, c))
        for py in range(max(0, int(ys[0] - 1)), min(SIDE, int(ys[2] + 1))):
            for px in range(max(0, int(xs[0] - 1)), min(SIDE, int(xs[2] + 1))):
                p = (Fraction(2 * px + 1, 2), Fraction(2 * py + 1, 2))
                sides = [
                    (q[0] - s[0]) * (p[1] - s[1]) - (q[1] - s[1]) * (p[0] - s[0])
                    for s, q in ((a, b), (b, c), (c, a))
                ]
                if all(v >= 0 for v in sides) or all(v <= 0 for v in sides):
                    if 0 in sides:
                        # The fill rule would decide this sample; keep every case away from it.
                        raise AssertionError(f"Pixel center {px},{py} lies on an edge")
                    covered.add((px, py))
    return covered


def image(covered):
    return b"".join(
        bytes((0, 255, 0, 255) if (x, y) in covered else (0, 0, 0, 255))
        for y in range(SIDE)
        for x in range(SIDE)
    )


def trace(points, triangles, counts=None, traced=True):
    size = 16 + 16 * len(points) + 12 * len(triangles)
    data = bytearray([0xCD] * (size + -size % 16))
    if traced:
        struct.pack_into("<2I", data, 0, *(counts or (len(points), len(triangles))))
        for i, point in enumerate(points):
            struct.pack_into("<4f", data, 16 + 16 * i, *map(float, point))
        for i, tri in enumerate(triangles):
            struct.pack_into("<3I", data, 16 + 16 * len(points) + 12 * i, *tri)
    return bytes(data)


def fixture(case, *, triangles=None, counts=None, traced=True, coverage=None):
    points = vertices(case)
    triangles = primitives(case) if triangles is None else triangles
    covered = raster(points, triangles if coverage is None else coverage)
    return constants(case) + trace(points, triangles, counts, traced) + image(covered)


def mutate(*changes):
    program = PROGRAM.copy()
    for old, new in zip(changes[::2], changes[1::2]):
        program[program.index(old) : program.index(old) + 1] = new.split("\n")
    return program


def pcs(program):
    # The only branch skips two four-byte scalar instructions.
    return [(4 * i, line) for i, line in enumerate(program)]


def refuse(program, words, limits=LIMITS, **kwargs):
    try:
        lower(pcs(program), words, limits, **kwargs)
    except ValueError as error:
        return str(error)
    raise AssertionError("Invalid multi-primitive mesh accepted")


def source():
    """Translator acceptances and refusals, without a GPU."""
    texts = {case: lower(pcs(PROGRAM), [word(case)], LIMITS) for case in CASES}
    for case, text in texts.items():
        count, prims = CASES[case][:2]
        if f"SetMeshOutputCounts({count},{prims});" not in text:
            raise AssertionError(f"{case}: wrong output counts")
        if f"out vertices Vertex v[{count}], out indices uint3 p[{prims}]" not in text:
            raise AssertionError(f"{case}: wrong output arrays")
        for i, tri in enumerate(primitives(case)):
            if f"if(id=={i}) {{ uint3 t=uint3({','.join(map(str, tri))}); p[{i}]=t;" not in text:
                raise AssertionError(f"{case}: primitive {i} is not {tri}")
        if texts[case].count("v[") != count + 1:
            raise AssertionError(f"{case}: expected one position write per vertex")
    conflict = "Conflicting mesh counts, vertices or primitive indices"
    fallback = "The compute fallback translates only a single three-vertex triangle"
    refusals = [
        # A primitive missing: only half of the allocated primitives are exported.
        (mutate("s_lshr_b32 s7, s15, 12", "s_lshr_b32 s7, s15, 13"), [word("strip")], LIMITS, {}, conflict),
        # The allocation declares one vertex more than the program exports.
        (mutate("s_lshr_b32 s7, s15, 12", "s_lshr_b32 s7, s15, 12\ns_add_u32 s9, s15, 1", "s_mov_b32 m0, s15",
                "s_mov_b32 m0, s9"), [word("quad")], LIMITS, {}, conflict),
        # One vertex exported beyond the allocation.
        (mutate("s_and_b32 s6, s15, 0xfff", "s_and_b32 s6, s15, 0xfff\ns_add_u32 s6, s6, 1"),
         [word("quad")], LIMITS, {}, conflict),
        # A primitive index outside the allocation: the last triangle reaches vertex 4.
        (PROGRAM, [4 | 3 << 12], LIMITS, {}, conflict),
        # Counts above the declared PSV0 bounds.
        (PROGRAM, [word("strip")], (11, 10), {}, conflict),
        (PROGRAM, [word("strip")], (12, 9), {}, conflict),
        # More vertices than the 128 threads can export.
        (PROGRAM, [129 | 126 << 12], (256, 256), {}, conflict),
        # The compute fallback keeps its single triangle.
        (PROGRAM, [word("quad")], LIMITS, {"compute_fallback": True}, fallback),
    ]
    for program, words, limits, kwargs, message in refusals:
        got = refuse(program, words, limits, **kwargs)
        if got != message:
            raise AssertionError(f"Unexpected refusal: {got}")
    # The single triangle still has its compute fallback.
    lower(pcs(PROGRAM), [3 | 1 << 12], LIMITS, compute_fallback=True)
    # The reference itself: every case covers pixels, and the last primitive in the
    # first wave and in the second wave each own at least one pixel center.
    for case in CASES:
        points, triangles = vertices(case), primitives(case)
        whole = raster(points, triangles)
        if not whole or any(raster(points, triangles[:k] + triangles[k + 1 :]) == whole for k in owned(case)):
            raise AssertionError(f"{case}: a dropped primitive would change no pixel")
    return texts


def owned(case):
    """Primitives whose removal must change the image: the last of each wave that has one."""
    count = CASES[case][1]
    return sorted({min(count, 64) - 1, count - 1})


def compile_shaders(dxc, tmp, text):
    (tmp / "pixel.hlsl").write_text(PIXEL)
    subprocess.run([dxc, "-T", "ps_6_0", "-E", "main", "-Fo", tmp / "pixel.dxil", tmp / "pixel.hlsl"], check=True)
    (tmp / "mesh.hlsl").write_text(text)
    for traced in (False, True):
        flags = ["-DTRACE_EXPORTS"] if traced else []
        out = tmp / ("mesh_traced.dxil" if traced else "mesh.dxil")
        subprocess.run([dxc, "-T", "ms_6_5", "-E", "main", *flags, "-Fo", out, tmp / "mesh.hlsl"], check=True)


def run(renderer, adapter, tmp, mesh, data):
    path = tmp / "draw.bin"
    path.write_bytes(data)
    result = subprocess.run(
        [renderer, adapter, "--mesh-multi", mesh, tmp / "pixel.dxil", path], capture_output=True, text=True
    )
    sys.stdout.write(result.stdout)
    return result


def expect(result, adapter, code, mismatches, trace_byte, label):
    out = result.stdout
    lines = out.splitlines()
    # The runner prints the debug-layer summary only when the layer is available.
    debug = f"backend={adapter} debug_layer=enabled" in lines
    ok = (
        result.returncode == code
        and f"pixels=1024 mismatches={mismatches}\n" in out
        and (not debug or "debug_errors=0 debug_warnings=0" in lines)
        and (trace_byte is None) == ("first_mesh_export_mismatch_byte" not in out)
        and (trace_byte is None or f"first_mesh_export_mismatch_byte={trace_byte}\n" in out)
    )
    if not ok:
        sys.stderr.write(result.stderr)
        raise AssertionError(f"{label}: unexpected result (exit {result.returncode})")
    print(f"{label}: exit={code} mismatches={mismatches} trace_byte={trace_byte}")


def first_difference(a, b):
    return next(i for i, (x, y) in enumerate(zip(a, b)) if x != y) - 1024


def draw(dxc, renderer, adapter, tmp):
    texts = source()
    for case, text in texts.items():
        compile_shaders(dxc, tmp, text)
        for traced in (False, True):
            mesh = tmp / ("mesh_traced.dxil" if traced else "mesh.dxil")
            result = run(renderer, adapter, tmp, mesh, fixture(case, traced=traced))
            if result.returncode == 77:
                return 77
            covered = len(raster(vertices(case), primitives(case)))
            expect(result, adapter, 0, 0, None, f"{case} traced={int(traced)} covered={covered}")
        mesh = tmp / "mesh_traced.dxil"
        good = fixture(case)
        triangles = primitives(case)
        # Negative controls against the correct draw: each reference is wrong in one way.
        # The first index of the first primitive trades places with the last index of the last one.
        swapped = [list(t) for t in triangles]
        swapped[0][0], swapped[-1][2] = swapped[-1][2], swapped[0][0]
        controls = [
            ("swapped index", fixture(case, triangles=[tuple(t) for t in swapped])),
            *((f"missing primitive {k}", fixture(case, coverage=triangles[:k] + triangles[k + 1 :]))
              for k in owned(case)),
            ("count mismatch", fixture(case, counts=(CASES[case][0], CASES[case][1] - 1))),
        ]
        for label, bad in controls:
            want = sum(bad[-4096 + 4 * i : -4096 + 4 * i + 4] != good[-4096 + 4 * i : -4096 + 4 * i + 4]
                       for i in range(1024))
            trace_bytes = bad[1024:-4096] != good[1024:-4096]
            if not want and not trace_bytes:
                raise AssertionError(f"{case} {label}: the control changes nothing")
            result = run(renderer, adapter, tmp, mesh, bad)
            expect(result, adapter, 1, want, first_difference(bad, good) if trace_bytes else None,
                   f"{case} {label}")
        # A translated program whose primitive keeps its vertices but swaps two indices draws the
        # same pixels; only the export trace sees it.
        rotated = lower(pcs(mutate("s_mov_b32 s41, 0x200400", "s_mov_b32 s41, 0x100800")), [word(case)], LIMITS)
        compile_shaders(dxc, tmp, rotated)
        result = run(renderer, adapter, tmp, mesh, good)
        expect(result, adapter, 1, 0, 16 + 16 * CASES[case][0] + 4, f"{case} swapped winding")
    return 0


def container(tmp, assembler, objcopy, program, limits):
    from pixel_aot_check import envelope
    from pixel_aot import _read_named_container

    src, obj, raw = (tmp / name for name in ("multi.s", "multi.o", "multi.bin"))
    src.write_text("\n".join(s.replace("prim,", "prim").replace("pos0,", "pos0") for s in program) + "\n")
    subprocess.run(
        [assembler, "-triple=amdgcn--amdpal", "-mcpu=gfx1013", "-mattr=+wavefrontsize64,-wavefrontsize32",
         "-filetype=obj", src, "-o", obj],
        check=True,
    )
    subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, raw], check=True)
    code = raw.read_bytes()
    padded = code + bytes.fromhex("00009fbf") * (4 - (len(code) // 4 + 10) % 4)
    record = envelope(padded, directory_end=(40 + len(padded), 288 + len(padded)), compute_width=128,
                      mesh_limits=limits)
    path = tmp / f"multi_{limits[0]}_{limits[1]}.dxbc"
    path.write_bytes(_read_named_container(record, len(b"fixture") + 2))
    print(f"assembled {len(code)} bytes")
    return path


def binary(tmp, assembler, objcopy, library):
    """Assemble the program with LLVM 15 and translate the machine code through the CLI."""
    texts = source()
    tool = Path(__file__).resolve().parents[1] / "tools" / "mesh_entry_aot.py"
    declared = container(tmp, assembler, objcopy, PROGRAM, LIMITS)

    def translate(path, case, *extra):
        geometry = tmp / "geometry.json"
        geometry.write_text(json.dumps([word(case)]))
        out = tmp / "out.hlsl"
        result = subprocess.run(
            [sys.executable, "-B", tool, path, "--llvm", library, "--geometry-words", geometry, "--output", out,
             *extra],
            capture_output=True, text=True,
        )
        return result, out

    for case in CASES:
        result, out = translate(declared, case)
        if result.returncode or out.read_text() != texts[case]:
            sys.stderr.write(result.stderr)
            raise AssertionError(f"{case}: machine code translates differently from the source text")
        print(f"{case}: binary translation matches the source text")
    refusals = [
        (declared, "quad", ["--compute-fallback"],
         "The compute fallback translates only a single three-vertex triangle"),
        (container(tmp, assembler, objcopy, PROGRAM, (11, 10)), "strip", [],
         "Conflicting mesh counts, vertices or primitive indices"),
    ]
    for path, case, extra, message in refusals:
        result, _ = translate(path, case, *extra)
        if result.returncode == 0 or f"ValueError: {message}" not in result.stderr:
            sys.stderr.write(result.stderr)
            raise AssertionError(f"{case} {extra}: expected refusal {message!r}")
        print(f"{case} {' '.join(extra) or 'PSV0 (11, 10)'}: refused")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        source()
        print(f"{len(CASES)} multi-primitive translations and their refusals checked")
    else:
        with tempfile.TemporaryDirectory() as directory:
            if sys.argv[1] == "binary":
                binary(Path(directory), *sys.argv[2:])
            else:
                sys.exit(draw(*sys.argv[1:], Path(directory)))
