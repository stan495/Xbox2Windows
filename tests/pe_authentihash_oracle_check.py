"""Compare tools/pe_authentihash.py with the Windows catalog-admin SHA-256 image hash.

Windows only. The oracle is CryptCATAdminCalcHashFromFileHandle2 in wintrust.dll. It
opens no certificate store and does not verify a signature. Exit 77 (capability skip)
when the platform or the API is missing.

Usage: pe_authentihash_oracle_check.py [--exotic] [PE_FILE ...]
Without --exotic only layouts seen in Windows system and build files are compared
(the Windows hash may differ between builds for unusual layouts). The expected
digests are the constants in tests/pe_authentihash_check.py.
Extra files must be built from source (for example a fixture DLL from this build).
"""
import ctypes
from ctypes import wintypes
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "cpu"))
SKIP = 77


def load_oracle():
    if sys.platform != "win32":
        return None
    try:
        wintrust = ctypes.WinDLL("wintrust.dll", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        acquire = wintrust.CryptCATAdminAcquireContext2
        calc = wintrust.CryptCATAdminCalcHashFromFileHandle2
        release = wintrust.CryptCATAdminReleaseContext
    except (OSError, AttributeError):
        return None
    acquire.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD]
    acquire.restype = wintypes.BOOL
    calc.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.c_char_p, wintypes.DWORD]
    calc.restype = wintypes.BOOL
    release.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    release.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def oracle(path: Path) -> str:
        admin = wintypes.HANDLE()
        if not acquire(ctypes.byref(admin), None, "SHA256", None, 0):
            raise OSError(f"CryptCATAdminAcquireContext2 failed: {ctypes.get_last_error()}")
        # GENERIC_READ, share read, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL
        handle = kernel32.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
        try:
            if handle in (None, wintypes.HANDLE(-1).value):
                raise OSError(f"CreateFileW failed: {ctypes.get_last_error()}")
            size = wintypes.DWORD(64)
            buffer = ctypes.create_string_buffer(64)
            if not calc(admin, handle, ctypes.byref(size), buffer, 0):
                raise OSError(f"CryptCATAdminCalcHashFromFileHandle2 failed: {ctypes.get_last_error()}")
            return buffer.raw[:size.value].hex()
        finally:
            if handle not in (None, wintypes.HANDLE(-1).value):
                kernel32.CloseHandle(handle)
            release(admin, 0)

    return oracle


def main() -> int:
    oracle = load_oracle()
    if oracle is None:
        print("SKIP: Windows catalog-admin hash API is not available")
        return SKIP
    from pe_authentihash import authentihash
    from pe_authentihash_check import CERT_CONTENT, EXPECTED, LAYOUTS, PINNED, build, build_layout
    from synthetic_pe import build_leaf_dll

    with_exotic = "--exotic" in sys.argv[1:]
    failures = 0
    with TemporaryDirectory() as directory:
        cases: list[tuple[str, Path, str | None]] = []
        for plus in (False, True):
            for cert in (None, CERT_CONTENT):
                path = Path(directory) / f"synthetic_{'plus' if plus else '32'}_{'cert' if cert else 'plain'}.bin"
                path.write_bytes(bytes(build(plus, cert)))
                cases.append((path.name, path, EXPECTED["pe32plus" if plus else "pe32"]))
        for plus in (False, True):
            for name, (options, exotic) in LAYOUTS.items():
                if exotic and not with_exotic:
                    continue
                key = f"{'pe32plus' if plus else 'pe32'}_{name}"
                path = Path(directory) / f"{key}.bin"
                path.write_bytes(bytes(build_layout(plus, **options)))
                cases.append((path.name, path, PINNED[key]))
        leaf = Path(directory) / "leaf.dll"
        build_leaf_dll(leaf)
        cases.append((leaf.name, leaf, EXPECTED["leaf"]))
        signed_leaf = Path(directory) / "leaf_cert.dll"
        data = bytearray(leaf.read_bytes())
        struct.pack_into("<II", data, 0x58 + 112 + 32, len(data), len(CERT_CONTENT))
        signed_leaf.write_bytes(bytes(data) + CERT_CONTENT)
        cases.append((signed_leaf.name, signed_leaf, EXPECTED["leaf"]))
        cases += [(Path(name).name, Path(name), None) for name in sys.argv[1:] if not name.startswith("--")]
        for label, path, pinned in cases:
            expected = oracle(path)
            actual = authentihash(path.read_bytes())
            good = expected == actual and pinned in (None, expected)
            status = "ok" if good else "MISMATCH"
            failures += not good
            print(f"{status}: {label} oracle={expected} tool={actual}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
