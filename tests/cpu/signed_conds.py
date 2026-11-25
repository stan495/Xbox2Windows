"""Compare native and translated signed and flag-conditional operations:
CMOVcc, SETcc, Jcc, NEG, and CWD."""
import ctypes
from pathlib import Path
import sys

import pefile
from iced_x86 import Decoder

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from cpu_leaf_aot import export_rva
from cpu_ranges import function_ranges
from ffx_version_query import INTEGER_HEADER, lower, region

U64 = ctypes.c_uint64

COND_SUFFIXES = ("l", "ge", "le", "g", "s", "ns", "o", "no")
CMOV_EXPORTS = tuple(f"cmov{s}{b}" for s in COND_SUFFIXES for b in (32, 64))
SET_EXPORTS = tuple(f"set{s}_op" for s in COND_SUFFIXES)
BRANCH_EXPORTS = tuple(f"j{s}_branch" for s in COND_SUFFIXES)
NEG_EXPORTS = ("neg32", "neg64")
MIXED_PRODUCER_2ARG = ("xadd_sets", "xadd_jl", "shld_c0_sets", "andn_sets", "bzhi_sets")
MIXED_PRODUCER_3ARG = ("cmpxchg_sets", "shld_sets", "shrd_sets")
MIXED_PRODUCER_1ARG = ("blsi_sets", "popcnt_sets")
MIXED_EXPORTS = MIXED_PRODUCER_2ARG + MIXED_PRODUCER_3ARG + MIXED_PRODUCER_1ARG
ALL_EXPORTS = CMOV_EXPORTS + SET_EXPORTS + BRANCH_EXPORTS + NEG_EXPORTS + ("cwd_op",) + MIXED_EXPORTS


def emit(image, output):
    parts = [INTEGER_HEADER, """
extern "C" __declspec(dllexport) unsigned signed_conds_missing_features() {
    int regs[4]{};
    __cpuid(regs, 0);
    const int max_leaf = regs[0];
    unsigned missing = 7;
    if (max_leaf >= 1) {
        __cpuid(regs, 1);
        if (regs[2] & (1 << 23)) missing &= ~4u;  // POPCNT
    }
    if (max_leaf >= 7) {
        __cpuidex(regs, 7, 0);
        if (regs[1] & (1 << 3)) missing &= ~1u;  // BMI1
        if (regs[1] & (1 << 8)) missing &= ~2u;  // BMI2
    }
    return missing;
}
"""]
    with pefile.PE(str(image), fast_load=True) as pe:
        for name in CMOV_EXPORTS:
            (bounds,) = function_ranges(pe, export_rva(pe, name))
            body = region(pe, *bounds)
            code = lower(body, ["rcx = a; rdx = b; r8 = alt;"], {}, {}, {})
            parts.append(f'extern "C" __declspec(dllexport) word {name}(word a, word b, word alt) {{\n{code}\n}}\n')
        for name in SET_EXPORTS:
            (bounds,) = function_ranges(pe, export_rva(pe, name))
            body = region(pe, *bounds)
            code = lower(body, ["rcx = a; rdx = b;"], {}, {}, {})
            parts.append(f'extern "C" __declspec(dllexport) word {name}(word a, word b) {{\n{code}\n}}\n')
        for name in BRANCH_EXPORTS:
            (bounds,) = function_ranges(pe, export_rva(pe, name))
            body = region(pe, *bounds)
            code = lower(body, ["rcx = a; rdx = b;"], {}, {}, {})
            parts.append(f'extern "C" __declspec(dllexport) word {name}(word a, word b) {{\n{code}\n}}\n')
        for name in NEG_EXPORTS:
            (bounds,) = function_ranges(pe, export_rva(pe, name))
            body = region(pe, *bounds)
            code = lower(body, ["rcx = a; rdx = out;"], {}, {}, {})
            parts.append(f'extern "C" __declspec(dllexport) word {name}(word a, word out) {{\n{code}\n}}\n')
        (bounds,) = function_ranges(pe, export_rva(pe, "cwd_op"))
        body = region(pe, *bounds)
        code = lower(body, ["rcx = a;"], {}, {}, {})
        parts.append(f'extern "C" __declspec(dllexport) word cwd_op(word a) {{\n{code}\n}}\n')
        for name in MIXED_PRODUCER_2ARG:
            (bounds,) = function_ranges(pe, export_rva(pe, name))
            body = region(pe, *bounds)
            code = lower(body, ["rcx = a; rdx = b;"], {}, {}, {})
            parts.append(f'extern "C" __declspec(dllexport) word {name}(word a, word b) {{\n{code}\n}}\n')
        for name in MIXED_PRODUCER_3ARG:
            (bounds,) = function_ranges(pe, export_rva(pe, name))
            body = region(pe, *bounds)
            code = lower(body, ["rcx = a; rdx = b; r8 = c;"], {}, {}, {})
            parts.append(f'extern "C" __declspec(dllexport) word {name}(word a, word b, word c) {{\n{code}\n}}\n')
        for name in MIXED_PRODUCER_1ARG:
            (bounds,) = function_ranges(pe, export_rva(pe, name))
            body = region(pe, *bounds)
            code = lower(body, ["rcx = a;"], {}, {}, {})
            parts.append(f'extern "C" __declspec(dllexport) word {name}(word a) {{\n{code}\n}}\n')
    output.write_text("".join(parts), encoding="ascii")


def load_lib(path):
    lib = ctypes.CDLL(str(Path(path).resolve()))
    for name in CMOV_EXPORTS:
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = [U64, U64, U64], U64
    for name in SET_EXPORTS:
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = [U64, U64], U64
    for name in BRANCH_EXPORTS:
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = [U64, U64], U64
    for name in NEG_EXPORTS:
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = [U64, ctypes.POINTER(U64)], U64
    lib.cwd_op.argtypes, lib.cwd_op.restype = [U64], U64
    for name in MIXED_PRODUCER_2ARG:
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = [U64, U64], U64
    for name in MIXED_PRODUCER_3ARG:
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = [U64, U64, U64], U64
    for name in MIXED_PRODUCER_1ARG:
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = [U64], U64
    return lib


def to_signed(v, bits):
    sign = 1 << (bits - 1)
    mask = (1 << bits) - 1
    v = v & mask
    return v - (1 << bits) if v >= sign else v


def ref_pred(cc, a, b, bits):
    sa = to_signed(a, bits)
    sb = to_signed(b, bits)
    diff = sa - sb
    sign_bit = 1 << (bits - 1)
    max_val = sign_bit - 1
    min_val = -sign_bit
    overflow = diff < min_val or diff > max_val
    s_flag = bool((a - b) & sign_bit)

    if cc == "l":
        return sa < sb
    elif cc == "ge":
        return sa >= sb
    elif cc == "le":
        return sa <= sb
    elif cc == "g":
        return sa > sb
    elif cc == "s":
        return s_flag
    elif cc == "ns":
        return not s_flag
    elif cc == "o":
        return overflow
    elif cc == "no":
        return not overflow
    raise ValueError(cc)


def ref_neg(a, bits):
    mask = (1 << bits) - 1
    sign_bit = 1 << (bits - 1)
    a_masked = a & mask
    res = (0 - a_masked) & mask
    cf = 1 if a_masked != 0 else 0
    of = 1 if a_masked == sign_bit else 0
    sf = 1 if (res & sign_bit) != 0 else 0
    zf = 1 if res == 0 else 0
    packed_flags = cf | (of << 1) | (sf << 2) | (zf << 3)
    return res, packed_flags


def ref_cwd(a):
    ax = a & 0xffff
    sign = (ax & 0x8000) != 0
    dx = 0xffff if sign else 0
    return (dx << 16) | ax


def check(native_path, lifted_path):
    native, lifted = load_lib(native_path), load_lib(lifted_path)
    both = ((native, "native"), (lifted, "lifted"))
    slot = U64(0)
    alt = 0xabcdef0123456789

    vals = (
        0, 1, 2, 0x7f, 0x80, 0xff, 0x100, 0x7fff, 0x8000, 0xffff,
        0x7fffffff, 0x80000000, 0x80000001, 0xffffffff,
        0xdeadbeef00000000, 0xdeadbeef00000001,
        0x7fffffffffffffff, 0x8000000000000000, 0x8000000000000001, 0xffffffffffffffff,
    )

    cases = 0

    # 1. CMOVcc across 32-bit and 64-bit widths
    for suffix in COND_SUFFIXES:
        for bits, init in ((32, 0x11), (64, 0x1122334455667788)):
            name = f"cmov{suffix}{bits}"
            mask = (1 << bits) - 1
            for a in vals:
                for b in vals:
                    want = (alt & mask) if ref_pred(suffix, a, b, bits) else init
                    for lib, label in both:
                        got = getattr(lib, name)(a, b, alt)
                        if got != want:
                            raise AssertionError(f"{label} {name}({a:#x}, {b:#x}) = {got:#x}, expected {want:#x}")
                    cases += 1

    # 2. SETcc (64-bit comparison, byte destination preserves upper 56 bits)
    for suffix in COND_SUFFIXES:
        name = f"set{suffix}_op"
        for a in vals:
            for b in vals:
                want = 0x123456789abcde00 | (1 if ref_pred(suffix, a, b, 64) else 0)
                for lib, label in both:
                    got = getattr(lib, name)(a, b)
                    if got != want:
                        raise AssertionError(f"{label} {name}({a:#x}, {b:#x}) = {got:#x}, expected {want:#x}")
                cases += 1

    # 3. Jcc branches (64-bit comparison, returns 1 if branch taken, 0 otherwise)
    for suffix in COND_SUFFIXES:
        name = f"j{suffix}_branch"
        for a in vals:
            for b in vals:
                want = 1 if ref_pred(suffix, a, b, 64) else 0
                for lib, label in both:
                    got = getattr(lib, name)(a, b)
                    if got != want:
                        raise AssertionError(f"{label} {name}({a:#x}, {b:#x}) = {got:#x}, expected {want:#x}")
                cases += 1

    # 4. NEG (32-bit and 64-bit with packed CF, OF, SF, ZF flags)
    for name, bits in (("neg32", 32), ("neg64", 64)):
        for a in vals:
            want_res, want_flags = ref_neg(a, bits)
            for lib, label in both:
                slot.value = 0xdeadbeef
                got = getattr(lib, name)(a, ctypes.byref(slot))
                if (got, slot.value) != (want_res, want_flags):
                    raise AssertionError(
                        f"{label} {name}({a:#x}) = ({got:#x}, {slot.value:#x}), expected ({want_res:#x}, {want_flags:#x})"
                    )
            cases += 1

    # 5. CWD (AX sign-extended into DX:AX)
    for a in vals:
        want = ref_cwd(a)
        for lib, label in both:
            got = lib.cwd_op(a)
            if got != want:
                raise AssertionError(f"{label} cwd_op({a:#x}) = {got:#x}, expected {want:#x}")
        cases += 1

    # 6. Mixed-producer regression cases (SF updated or preserved across producers)
    # xadd_sets & xadd_jl
    for a in vals:
        for b in vals:
            sum_v = (a + b) & 0xffffffffffffffff
            sf = (sum_v >> 63) & 1
            of = ((~(a ^ b) & (a ^ sum_v)) >> 63) & 1
            want_sets = sf
            want_jl = 1 if sf != of else 0
            for lib, label in both:
                got = lib.xadd_sets(a, b)
                if got != want_sets:
                    raise AssertionError(f"{label} xadd_sets({a:#x}, {b:#x}) = {got:#x}, expected {want_sets:#x}")
                got = lib.xadd_jl(a, b)
                if got != want_jl:
                    raise AssertionError(f"{label} xadd_jl({a:#x}, {b:#x}) = {got:#x}, expected {want_jl:#x}")
            cases += 2

    # cmpxchg_sets
    for a in vals:
        for b in vals:
            diff = (a - b) & 0xffffffffffffffff
            want = (diff >> 63) & 1
            for lib, label in both:
                got = lib.cmpxchg_sets(a, b, 0)
                if got != want:
                    raise AssertionError(f"{label} cmpxchg_sets({a:#x}, {b:#x}) = {got:#x}, expected {want:#x}")
            cases += 1

    # shld_sets & shrd_sets
    for a, b, c in (
        (0x4000000000000000, 0, 1),
        (0x4000000000000000, 0, 2),
        (0x8000000000000000, 0, 0),
        (0, 0x8000000000000000, 1),
        (0, 1, 63),
    ):
        cnt = c & 0x3f
        if cnt != 0:
            shld_res = ((a << cnt) | (b >> (64 - cnt))) & 0xffffffffffffffff
            want_shld = (shld_res >> 63) & 1
        else:
            want_shld = 0  # prior SF was 0
        for lib, label in both:
            got = lib.shld_sets(a, b, c)
            if got != want_shld:
                raise AssertionError(f"{label} shld_sets({a:#x}, {b:#x}, {c}) = {got:#x}, expected {want_shld:#x}")
        cases += 1

    for a, b, c in (
        (0, 1, 1),
        (0, 0, 1),
        (0x8000000000000000, 0, 1),
        (0x0000000000000001, 0, 1),
    ):
        cnt = c & 0x3f
        if cnt != 0:
            shrd_res = ((a >> cnt) | (b << (64 - cnt))) & 0xffffffffffffffff
            want_shrd = (shrd_res >> 63) & 1
        else:
            want_shrd = 0
        for lib, label in both:
            got = lib.shrd_sets(a, b, c)
            if got != want_shrd:
                raise AssertionError(f"{label} shrd_sets({a:#x}, {b:#x}, {c}) = {got:#x}, expected {want_shrd:#x}")
        cases += 1

    # shld_c0_sets (count 0 preserves prior SF = 1)
    for a, b in ((0, 0), (1, 2), (0x7fffffffffffffff, 0x8000000000000000)):
        want = 1
        for lib, label in both:
            got = lib.shld_c0_sets(a, b)
            if got != want:
                raise AssertionError(f"{label} shld_c0_sets({a:#x}, {b:#x}) = {got:#x}, expected {want:#x}")
        cases += 1

    missing_features = lifted.signed_conds_missing_features
    missing_features.argtypes, missing_features.restype = [], ctypes.c_uint
    missing = missing_features()
    if missing:
        names = ", ".join(name for bit, name in enumerate(("BMI1", "BMI2", "POPCNT")) if missing & (1 << bit))
        print(f"{cases} baseline cases passed. Optional mixed-producer checks require {names}.")
        return 77

    # andn_sets
    for a in vals:
        for b in (0, 1, 0x8000000000000000, 0xffffffffffffffff):
            res = ((~a) & b) & 0xffffffffffffffff
            want = (res >> 63) & 1
            for lib, label in both:
                got = lib.andn_sets(a, b)
                if got != want:
                    raise AssertionError(f"{label} andn_sets({a:#x}, {b:#x}) = {got:#x}, expected {want:#x}")
            cases += 1

    # blsi_sets
    for a in vals:
        res = ((0 - a) & a) & 0xffffffffffffffff
        want = (res >> 63) & 1
        for lib, label in both:
            got = lib.blsi_sets(a)
            if got != want:
                raise AssertionError(f"{label} blsi_sets({a:#x}) = {got:#x}, expected {want:#x}")
        cases += 1

    # bzhi_sets
    for a in (0, 1, 0x7fffffffffffffff, 0x8000000000000000, 0xffffffffffffffff):
        for b in (0, 1, 63, 64, 65):
            idx = b & 0xff
            res = a if idx >= 64 else (a & ((1 << idx) - 1))
            want = (res >> 63) & 1
            for lib, label in both:
                got = lib.bzhi_sets(a, b)
                if got != want:
                    raise AssertionError(f"{label} bzhi_sets({a:#x}, {b}) = {got:#x}, expected {want:#x}")
            cases += 1

    # popcnt_sets (POPCNT unconditionally clears SF to 0, despite prior SF = 1)
    for a in vals:
        want = 0
        for lib, label in both:
            got = lib.popcnt_sets(a)
            if got != want:
                raise AssertionError(f"{label} popcnt_sets({a:#x}) = {got:#x}, expected {want:#x}")
        cases += 1

    print(f"{cases} signed condition and flag-op cases (CMOVcc, SETcc, Jcc, NEG, CWD, mixed producers) matched native and translated DLLs")
    return 0


def check_negative_controls():
    # 1. Unconditional volatile read for memory-operand CMOVcc
    # 41 0F 4C 00 = cmovl eax, dword ptr [r8]
    emitted = lower(list(Decoder(64, bytes.fromhex("39 c9 41 0f 4c 00 c3"), ip=0x1000)), [], {}, {}, {})
    if "load_always<uint32_t>(r8 + 0ull)" not in emitted:
        raise AssertionError(f"expected unconditional volatile read for CMOV memory source:\n{emitted}")

    # 2. Rejection of unhandled / malformed operations
    # UD0 should be rejected cleanly with ValueError (UD2 is lowered since #1021 and UD1 since #1607; UD0 stays refused)
    try:
        lower(list(Decoder(64, bytes.fromhex("0f ff c0 c3"), ip=0x1000)), [], {}, {}, {})
    except ValueError as exc:
        if "unsupported query operation" not in str(exc):
            raise AssertionError(f"unexpected diagnostic: {exc}") from exc
    else:
        raise AssertionError("accepted UD0 instruction")


if __name__ == "__main__":
    mode, native, other = sys.argv[1:]
    if mode == "emit":
        emit(Path(native), Path(other))
    elif mode == "check":
        check_negative_controls()
        sys.exit(check(native, other))
    else:
        raise ValueError(mode)
