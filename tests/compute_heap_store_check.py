"""A gfx1013 formatted store selects the byte range of a fixed u0 through a run-time view record."""
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

import compute_store_check as base


GROUP = (4, 1, 1)
WORDS = 320
TABLE_BYTE = 256
UINT4 = 0x0104BFAC  # gfx10 format 75, identity XYZW selectors and RESOURCE_LEVEL set
FLOAT4 = 0x0104DFAC
STORE = "buffer_store_format_xyzw v[10:13], v8, s[20:23], 0 idxen offset:4"
D16_STORE = "buffer_store_format_d16_xyzw v[10:11], v8, s[20:23], 0 idxen offset:4"
SETUP = [
    "s_mov_b32 s12, s7", "s_mov_b32 s13, 0", "s_load_dwordx4 s[20:23], s[12:13], 0x30",
    "s_load_dwordx4 s[16:19], s[12:13], 0x40", "v_lshl_add_u32 v8, s11, 2, v0",
    "s_waitcnt lgkmcnt(0)", "s_buffer_load_dwordx4 s[24:27], s[16:19], null", "s_waitcnt lgkmcnt(0)",
]
PROGRAM = SETUP + [
    "v_mov_b32_e32 v10, v8", "v_mov_b32_e32 v11, 0x12345678", "v_mov_b32_e32 v12, 0x80000001",
    "v_mov_b32_e32 v13, 0x3f800001", STORE, "s_endpgm",
]
D16_PROGRAM = SETUP + [
    "v_xor_b32_e32 v10, 0xc0003c00, v8", "v_mov_b32_e32 v11, 0x80000001", D16_STORE, "s_endpgm",
]
# 0x3c00..0x3c03 are consecutive binary16 values starting at 1.0.
D16_X = (0x3f800000, 0x3f802000, 0x3f804000, 0x3f806000)
# V# words 0..3 and live state, then the expected *word addresses* of the four component stores.
# The addresses are literal to keep the reference independent of the generated address calculation.
CASES = (
    ("four records", (12, 24 << 16, 4, UINT4, 1), (4, 10, 16, 22)),
    ("second base and short record count", (116, 32 << 16, 2, UINT4, 1), (30, 38)),
    ("released view", (12, 24 << 16, 4, UINT4, 2), ()),
    ("last complete texel in u0", (492, 20 << 16, 4, UINT4, 1), (124,)),
    ("partial texel at end of u0", (496, 20 << 16, 4, UINT4, 1), ()),
    ("wrong four-channel format", (12, 24 << 16, 4, FLOAT4, 1), ()),
    ("offset plus texel wider than stride", (12, 16 << 16, 4, UINT4, 1), ()),
    ("misaligned base", (14, 24 << 16, 4, UINT4, 1), ()),
    ("misaligned stride", (12, 22 << 16, 4, UINT4, 1), ()),
    ("unsupported high address", (12, (24 << 16) | 1, 4, UINT4, 1), ()),
    ("base past the declared u0 extent", (640, 24 << 16, 4, UINT4, 1), ()),
    ("live slot beyond declared count", (12, 24 << 16, 4, UINT4, 1), ()),
)
VIEW = (0, TABLE_BYTE, len(CASES) - 1, WORDS * 4, 512)
BINDING = dict(table_reg=7, descriptor_offset=0x30, constant_descriptor=0x40, source_descriptor=0x50, group_reg=11)


def lower(program=PROGRAM, **options):
    return base.aot.lower(program, GROUP, **(BINDING | dict(heap_store_view=VIEW) | options))


def controls():
    assert base.aot.descriptor_format_word(75) == UINT4
    # Pin the translator call here. draw() exercises the generated guards against actual UAV contents.
    assert "store_heap_words4(params[0].x, tid.x, 4u, false, uint4(tid.x," in lower()
    assert "store_heap_words4(params[0].x, tid.x, 4u, true," in lower(D16_PROGRAM)
    refuses = base.rejects
    refuses("Unsupported formatted buffer store target or address mode", lower, heap_store_view=None)
    refuses("Invalid heap store view binding", lower, stride=24)
    refuses("Invalid heap store view binding", lower, heap_view=VIEW)
    refuses("Invalid heap view binding", lower, source_descriptor=None)
    refuses("Invalid heap view binding", lower, heap_store_view=(0, TABLE_BYTE, 11, TABLE_BYTE + 32, 512))
    refuses("Invalid heap store view output size", lower, heap_store_view=(*VIEW[:4], 511))
    refuses("Heap store view requires exactly one formatted UAV store", lower,
            PROGRAM[:-1] + [STORE.replace("offset:4", "offset:24"), "s_endpgm"])
    refuses("Heap store view slot constant is not ready", lower, PROGRAM[:7] + PROGRAM[8:])
    refuses("Heap store view requires buffer_store_format_xyzw", lower,
            base.variant(PROGRAM, (STORE, STORE.replace("xyzw v[10:13]", "xy v[10:11]"))))
    refuses("Heap store view requires buffer_store_format_xyzw", lower,
            base.variant(D16_PROGRAM, (D16_STORE, D16_STORE.replace("d16_xyzw", "d16_xyz"))))
    refuses("Unverified raw UAV store", lower,
            base.variant(D16_PROGRAM, (D16_STORE, D16_STORE.replace("v[10:11]", "v[10:13]"))))
    refuses("Unsupported formatted buffer store target or address mode", lower,
            base.variant(PROGRAM, (STORE, STORE.replace(" idxen", " offen"))))
    refuses("Unverified raw UAV store", lower, base.variant(PROGRAM, (STORE, STORE.replace("s[20:23]", "s[16:19]"))))
    refuses("Heap store view needs a formatted store indexed by dispatch thread ID", lower,
            base.variant(PROGRAM, (STORE, STORE.replace(", v8,", ", v12,"))))


def verify_binary(directory, code, library, program=PROGRAM):
    decoded = base.aot.labelled(base.disassemble(code, library, offsets=True, cpu=b"gfx1013"))
    expected = lower(program)
    assert lower(decoded) == expected
    container, output = directory / "store.dxbc", directory / "store.hlsl"
    container.write_bytes(base.store_container(code, GROUP))
    subprocess.run([sys.executable, "-B", base.aot.__file__, container, "--output", output, "--llvm", library,
                    *base.binding_cli(BINDING), "--heap-store-view", *map(str, VIEW)], check=True)
    assert output.read_text(encoding="ascii") == expected


def draw(directory, compiler, renderer, backend, d16):
    source = lower(D16_PROGRAM if d16 else PROGRAM)
    table = [0] * WORDS
    for slot, (_, record, _) in enumerate(CASES):
        start = TABLE_BYTE // 4 + slot * 8
        table[start:start + 5] = record
        if d16:
            table[start + 3] = FLOAT4 if record[3] == UINT4 else UINT4
    hlsl, cso = directory / "store.hlsl", directory / "store.cso"

    def compile_shader(text):
        hlsl.write_text(text, encoding="ascii")
        subprocess.run([compiler, "-T", "cs_6_0", "-E", "main", "-Fo", cso, hlsl], check=True)

    def run(slot):
        result = [base.SENTINEL] * WORDS
        for thread, start in enumerate(CASES[slot][2]):
            # D16 y, z, w are -2, the smallest positive binary16 subnormal, and negative zero.
            result[start:start + 4] = ([D16_X[thread], 0xc0000000, 0x33800000, 0x80000000] if d16 else
                                       [thread, 0x12345678, 0x80000001, 0x3F800001])
        case = directory / "store.case"
        case.write_bytes(struct.pack("<321I", 1, *result) + struct.pack("<16I", slot, *([0] * 15))
                         + struct.pack("<320I", *table))
        return subprocess.run([renderer, backend, "--compute-store-case", cso, case], capture_output=True, text=True)

    compile_shader(source)
    for slot, (label, _, _) in enumerate(CASES):
        result = run(slot)
        if result.returncode == 77 and backend == "hardware":
            return 77
        assert result.returncode == 0, (label, result.stderr)
    mutations = (
        ("view.x + index * stride + offset", "index * stride + offset", 0),
        (" || index >= view.z", "", 1),
        ("uint stride = view.y >> 16u;", "uint stride = 24u;", 1),
        ("const uint bytes = 512u;", "const uint bytes = 1280u;", 4),
        ("if (index > (bytes - view.x - offset - 16u) / stride) return;", "", 3),
        ("view.w != formatWord || ", "", 5),
        ("!= 1u) return;", "== 0u) return;", 2),
        (f"slot >= {VIEW[2]}u", f"slot > {VIEW[2]}u", VIEW[2]),
    )
    if d16:
        mutations += (
            ("value.x >> 16u", "value.x", 0),
            ("value.y >> 16u", "value.x >> 16u", 0),
            ("if (d16) value =", "if (false) value =", 0),
        )
    for old, new, slot in mutations:
        assert source.count(old) == 1, old
        compile_shader(source.replace(old, new))
        result = run(slot)
        assert result.returncode == 1 and "Compute readback differs" in result.stderr, (old, result.stderr)
    kind = "D16-to-FLOAT4" if d16 else "UINT4"
    print(f"{backend} {kind}: {len(CASES)} heap-store cases and {len(mutations)} shader mutations checked")
    return 0


if __name__ == "__main__":
    controls()
    mode, *args = sys.argv[1:] or ["controls"]
    if mode == "controls" and not args:
        print("heap-store controls passed")
    elif mode in ("binary", "draw") and len(args) == 3:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            if mode == "draw":
                for d16 in (False, True):
                    result = draw(directory, *args, d16)
                    if result:
                        sys.exit(result)
                sys.exit(0)
            assembler, objcopy, library = args
            for program in (PROGRAM, D16_PROGRAM):
                code = base.assemble(directory, assembler, objcopy, program, cpu="gfx1013")
                verify_binary(directory, code, library, program)
    else:
        sys.exit("usage: compute_heap_store_check.py controls | binary <llvm-mc> <llvm-objcopy> <libLLVM> "
                 "| draw <dxc> <offscreen_check> <backend>")
