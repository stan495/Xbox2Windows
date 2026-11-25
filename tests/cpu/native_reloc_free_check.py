"""Place a relocation-free DLL with its preferred base reserved, with Windows and with pe_map."""

import argparse
import ctypes
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory

import pefile

from direct_check import input_pairs, observe, reference, register_probe
from native_map_check import Mapper

MEM_RESERVE, MEM_RELEASE, PAGE_NOACCESS = 0x2000, 0x8000, 0x01
SEARCH_DLL_LOAD_DIR_AND_DEFAULT = 0x100 | 0x1000
DYNAMIC_BASE = 0x40
RELOCS_STRIPPED = 0x0001
ERROR_INVALID_ADDRESS = 487
MAP_RELOCATION = 4  # MapStatus in tools/pe_map.cpp


class MemoryInfo(ctypes.Structure):  # MEMORY_BASIC_INFORMATION on x64
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", ctypes.c_uint32), ("PartitionId", ctypes.c_uint16),
                ("RegionSize", ctypes.c_size_t), ("State", ctypes.c_uint32),
                ("Protect", ctypes.c_uint32), ("Type", ctypes.c_uint32)]


def kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]
    kernel.VirtualAlloc.restype = ctypes.c_void_p
    kernel.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
    kernel.VirtualFree.restype = ctypes.c_int
    kernel.VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.POINTER(MemoryInfo), ctypes.c_size_t]
    kernel.VirtualQuery.restype = ctypes.c_size_t
    kernel.LoadLibraryExW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32]
    kernel.LoadLibraryExW.restype = ctypes.c_void_p
    kernel.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    kernel.GetProcAddress.restype = ctypes.c_void_p
    return kernel


def choose_preferred(kernel, image):
    """Give the temporary relocation-free copy a preferred base reserved in this child."""
    with pefile.PE(image, fast_load=True) as pe:
        size = pe.OPTIONAL_HEADER.SizeOfImage
        base_at = pe.OPTIONAL_HEADER.get_field_absolute_offset("ImageBase")
    preferred = kernel.VirtualAlloc(None, size, MEM_RESERVE, PAGE_NOACCESS)
    if preferred is None:
        raise ctypes.WinError(ctypes.get_last_error())
    info = MemoryInfo()
    if not kernel.VirtualQuery(ctypes.c_void_p(preferred), ctypes.byref(info), ctypes.sizeof(info)):
        raise AssertionError("VirtualQuery failed on the preferred base")
    if (info.State != MEM_RESERVE or info.BaseAddress != preferred or
            info.AllocationBase != preferred or info.RegionSize < size):
        raise AssertionError(f"reservation does not cover the image: state {info.State:#x}, "
                             f"allocation base {info.AllocationBase}, region {info.RegionSize:#x}")
    with open(image, "r+b") as copy:
        copy.seek(base_at)
        copy.write(struct.pack("<Q", preferred))
    return preferred


def run(loader, image, provider, mapper_path, probe_path, reserve_base):
    """Child process: optionally reserve the preferred base, load the image, call call_mix on every pair."""
    kernel = kernel32()
    probe = register_probe(probe_path)
    preferred = choose_preferred(kernel, image)
    if reserve_base == "no-reserve" and not kernel.VirtualFree(preferred, 0, MEM_RELEASE):
        raise ctypes.WinError(ctypes.get_last_error())
    if loader == "windows":
        base = kernel.LoadLibraryExW(image, None, SEARCH_DLL_LOAD_DIR_AND_DEFAULT)
        error = None if base else ctypes.get_last_error()
        function = kernel.GetProcAddress(base, b"call_mix") if base else None
    else:
        mapper = Mapper(mapper_path)
        bindings = {Path(provider).name.casefold(): ctypes.CDLL(provider)}
        base, status, detail, _ = mapper(Path(image), bindings)
        error = None if base else [status, detail]
        function = mapper.export(base, b"call_mix") if base else None
    pairs = input_pairs()
    matches = sum(observe(probe, function, x, y) == reference(x, y, caller=True) for x, y in pairs) if function else 0
    print(json.dumps({"base": base, "preferred": preferred, "error": error, "pairs": len(pairs), "matches": matches}))


def child(loader, image, provider, mapper, probe, reserve_base="reserve"):
    result = subprocess.run([sys.executable, "-B", __file__, "--run", loader, str(image), str(provider),
                             str(mapper), str(probe), reserve_base], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AssertionError(f"{loader} {Path(image).name}: child exited {result.returncode:#x}: {result.stderr}")
    return json.loads(result.stdout)


def write_copy(folder, image, provider, patch):
    folder.mkdir()
    data = bytearray(image.read_bytes())
    patch(data)
    (folder / image.name).write_bytes(data)
    return folder / image.name, Path(shutil.copy2(provider, folder))


def check(mapper, probe, image, provider):
    with pefile.PE(str(image)) as pe:
        relocs = pe.OPTIONAL_HEADER.DATA_DIRECTORY[5]
        flags = pe.FILE_HEADER.Characteristics
        flags_at = pe.FILE_HEADER.get_field_absolute_offset("Characteristics")
        dll_flags_at = pe.OPTIONAL_HEADER.get_field_absolute_offset("DllCharacteristics")
        dll_flags = pe.OPTIONAL_HEADER.DllCharacteristics
        has_dir64 = any(e.type == pefile.RELOCATION_TYPE["IMAGE_REL_BASED_DIR64"]
                        for block in getattr(pe, "DIRECTORY_ENTRY_BASERELOC", []) for e in block.entries)
    if relocs.Size or flags & RELOCS_STRIPPED or has_dir64 or not dll_flags & DYNAMIC_BASE:
        raise AssertionError(f"{image.name}: expected an empty relocation directory, stripped flag clear, "
                             "no DIR64 entries and DYNAMIC_BASE set")
    fixed = lambda data: struct.pack_into("<H", data, dll_flags_at, dll_flags & ~DYNAMIC_BASE)
    with TemporaryDirectory() as temp:
        root = Path(temp)
        variants = {"as built": write_copy(root / "as-built", image, provider, lambda data: None),
                    "DYNAMIC_BASE cleared": write_copy(root / "fixed", image, provider, fixed)}

        # Control: without a reservation Windows keeps the DYNAMIC_BASE-cleared copy at its preferred
        # base, so the move below is caused by the reserved range, not by ASLR.
        copy, copied_provider = variants["DYNAMIC_BASE cleared"]
        free = child("windows", copy, copied_provider, mapper, probe, "no-reserve")
        if free["base"] != free["preferred"]:
            raise AssertionError(f"DYNAMIC_BASE cleared / windows: unreserved control failed: {free}")
        print(f"DYNAMIC_BASE cleared / windows, nothing reserved: loaded at its preferred base {free['base']:#x}")

        for name, (copy, copied_provider) in variants.items():
            for loader in ("windows", "pe_map"):
                observed = child(loader, copy, copied_provider, mapper, probe)
                base, preferred = observed["base"], observed["preferred"]
                if not base or observed["error"] is not None:
                    raise AssertionError(f"{name} / {loader}: load failed with {observed['error']}")
                if base == preferred:
                    raise AssertionError(f"{name} / {loader}: loaded at the reserved preferred base {preferred:#x}")
                if observed["matches"] != observed["pairs"]:
                    raise AssertionError(f"{name} / {loader}: {observed['matches']} of {observed['pairs']} "
                                         "call_mix results match the reference")
                print(f"{name} / {loader}: preferred {preferred:#x} reserved, loaded at {base:#x}, "
                      f"{observed['matches']} of {observed['pairs']} call_mix results match")

        # Control: with IMAGE_FILE_RELOCS_STRIPPED set and the base reserved, neither loader may place it.
        stripped = lambda data: struct.pack_into("<H", data, flags_at, flags | RELOCS_STRIPPED)
        copy, copied_provider = write_copy(root / "stripped", image, provider, stripped)
        free = child("windows", copy, copied_provider, mapper, probe, "no-reserve")
        if (free["base"] != free["preferred"] or free["error"] is not None or
                free["matches"] != free["pairs"]):
            raise AssertionError(f"RELOCS_STRIPPED / windows: unreserved control failed: {free}")
        windows = child("windows", copy, copied_provider, mapper, probe)
        mapped = child("pe_map", copy, copied_provider, mapper, probe)
        if windows["base"] or windows["error"] != ERROR_INVALID_ADDRESS:
            raise AssertionError(f"RELOCS_STRIPPED / windows: base {windows['base']} error {windows['error']}")
        if mapped["base"] or mapped["error"] != [MAP_RELOCATION, "relocations stripped"]:
            raise AssertionError(f"RELOCS_STRIPPED / pe_map: {mapped['error']}")
        print(f"RELOCS_STRIPPED set: Windows loaded and called the unreserved copy at its preferred base, "
              f"then refused the reserved case with error {windows['error']}; "
              "pe_map rejected it as relocations stripped")
    print(f"{image.name} has an empty relocation directory; Windows and pe_map both load it away from its "
          "reserved preferred base, with or without DYNAMIC_BASE, and it runs correctly there")


if __name__ == "__main__":
    if sys.argv[1] == "--run":
        run(*sys.argv[2:8])
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        for arg in ("mapper", "probe", "image", "provider"):
            parser.add_argument(arg, type=Path)
        args = parser.parse_args()
        check(args.mapper, args.probe, args.image, args.provider)
