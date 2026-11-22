"""Differential check for baseline 128-bit SSSE3 packed integer ALU and shuffle lowering."""
import ctypes
import random
import struct
import sys
from pathlib import Path

import pefile
from iced_x86 import Decoder

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from cpu_leaf_aot import export_rva
from cpu_ranges import function_ranges
from ffx_version_query import INTEGER_HEADER, lower, region

EXPORTS = (
    "pabsb_op", "pabsw_op", "pabsd_op",
    "psignb_op", "psignw_op", "psignd_op",
    "palignr_op", "pshufb_op",
    "phaddw_op", "phaddd_op", "phaddsw_op",
    "phsubw_op", "phsubd_op", "phsubsw_op",
)

CPUID_HELPER = """#include <intrin.h>
extern "C" __declspec(dllexport) uint32_t host_has_ssse3() {
    int info[4]{};
    __cpuid(info, 1);
    return (info[2] & (1 << 9)) != 0 ? 1u : 0u;
}
"""


def emit(native_dll: Path, output_cpp: Path) -> None:
    parts = [INTEGER_HEADER, CPUID_HELPER]
    with pefile.PE(str(native_dll), fast_load=True) as pe:
        for name in EXPORTS:
            ranges = function_ranges(pe, export_rva(pe, name))
            body = [ins for lo, hi in ranges for ins in region(pe, lo, hi)]
            code = lower(body, ["rcx = a; rdx = b; r8 = dst;"], {}, {}, {})
            parts.append(
                f'extern "C" __declspec(dllexport) uint64_t {name}(uint64_t a, uint64_t b, uint64_t dst) {{\n{code}\n}}'
            )
    output_cpp.write_text("\n".join(parts) + "\n", encoding="utf-8")


def check_rejections() -> None:
    # 64-bit MMX pabsb mm0, mm1 (0F 38 1C C1) followed by ret must be rejected.
    mmx_pabsb = list(Decoder(64, b"\x0f\x38\x1c\xc1\xc3", ip=0x1000))
    try:
        lower(mmx_pabsb, [], {}, {}, {})
    except ValueError as error:
        if "unsupported query operation at 0x1000" not in str(error):
            raise AssertionError(f"unexpected diagnostic: {error}") from error
    else:
        raise AssertionError("accepted 64-bit MMX pabsb")

    # 64-bit MMX pshufb mm0, mm1 (0F 38 00 C1) followed by ret must be rejected.
    mmx_pshufb = list(Decoder(64, b"\x0f\x38\x00\xc1\xc3", ip=0x1000))
    try:
        lower(mmx_pshufb, [], {}, {}, {})
    except ValueError as error:
        if "unsupported query operation at 0x1000" not in str(error):
            raise AssertionError(f"unexpected diagnostic: {error}") from error
    else:
        raise AssertionError("accepted 64-bit MMX pshufb")

    # Legacy XMM memory form without an alignment contract must be rejected.
    mem = list(Decoder(64, b"\x66\x0f\x38\x1c\x02\xc3", ip=0x1000))
    try:
        lower(mem, [], {}, {}, {})
    except ValueError as error:
        if "unsupported SSSE3 source at 0x1000" not in str(error):
            raise AssertionError(f"unexpected diagnostic: {error}") from error
    else:
        raise AssertionError("accepted memory operand for SSSE3 instruction")


def check(native_dll: Path, lifted_dll: Path) -> None:
    check_rejections()

    lifted = ctypes.CDLL(str(lifted_dll))
    lifted.host_has_ssse3.argtypes = []
    lifted.host_has_ssse3.restype = ctypes.c_uint32
    if not lifted.host_has_ssse3():
        print("SSSE3 instruction set not supported on this host (CPUID leaf 1 ECX bit 9)")
        sys.exit(77)

    native = ctypes.CDLL(str(native_dll))
    out_n = (ctypes.c_ubyte * 16)()
    out_l = (ctypes.c_ubyte * 16)()

    test_vectors = [
        (
            bytes([-128 & 0xff, -1 & 0xff, 0, 1, 127, -50 & 0xff, 50, 0,
                   -1 & 0xff, 127, -128 & 0xff, 42, -42 & 0xff, 10, -10 & 0xff, 100]),
            bytes([-128 & 0xff, 1, 0, -1 & 0xff, 1, -50 & 0xff, 51, -1 & 0xff,
                   0x80, 0x85, 0x00, 0x0f, 0x04, 0x0e, 0x82, 0x01]),
        ),
        (
            struct.pack("<8h", -32768, -1, 0, 1, 32767, -1000, 20000, 25000),
            struct.pack("<8h", 1, -1, 0, 32767, -32768, 1000, 20000, 25000),
        ),
        (
            struct.pack("<4i", -2147483648, -1, 0, 2147483647),
            struct.pack("<4i", 1, -1, 100, 2147483647),
        ),
        (
            bytes([0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77,
                   0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff]),
            bytes([0x0f, 0x0e, 0x0d, 0x0c, 0x0b, 0x0a, 0x09, 0x08,
                   0x80, 0x81, 0x82, 0x83, 0x03, 0x02, 0x01, 0x00]),
        ),
    ]

    rng = random.Random(2026)
    for _ in range(6):
        test_vectors.append((rng.randbytes(16), rng.randbytes(16)))

    cases = 0
    for a_bytes, b_bytes in test_vectors:
        ba = ctypes.create_string_buffer(a_bytes, 16)
        bb = ctypes.create_string_buffer(b_bytes, 16)
        a_ptr = ctypes.addressof(ba)
        b_ptr = ctypes.addressof(bb)

        for name in EXPORTS:
            fn_n, fn_l = getattr(native, name), getattr(lifted, name)
            fn_n.argtypes = fn_l.argtypes = [ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64]
            fn_n.restype = fn_l.restype = ctypes.c_uint64

            ctypes.memset(out_n, 0xaa, 16)
            ctypes.memset(out_l, 0x55, 16)

            fn_n(a_ptr, b_ptr, ctypes.addressof(out_n))
            fn_l(a_ptr, b_ptr, ctypes.addressof(out_l))

            if bytes(out_n) != bytes(out_l):
                raise AssertionError(
                    f"{name}: lifted {bytes(out_l).hex()} != native {bytes(out_n).hex()} "
                    f"for a={a_bytes.hex()} b={b_bytes.hex()}"
                )
            cases += 1

    # Mutated negative control: mutating output byte must fail.
    mutated = bytearray(bytes(out_n))
    mutated[0] ^= 0x01
    if bytes(mutated) == bytes(out_l):
        raise AssertionError("mutation negative control failed to produce a mismatch")

    print(f"{cases} SSSE3 packed integer ALU cases matched native and translated DLLs")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise ValueError("missing action")
    mode = sys.argv[1]
    if mode == "emit":
        emit(Path(sys.argv[2]), Path(sys.argv[3]))
    elif mode == "check":
        check(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        raise ValueError(f"unknown mode: {mode}")
