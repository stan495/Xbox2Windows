"""Map source-built x64 images without LoadLibrary, bind imports to host shims and compare calls."""

import argparse
import ctypes
from ctypes import wintypes
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory

import pefile

from direct_check import input_pairs, observe, reference, register_probe

Resolver = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
STATUS = {
    "ok": 0, "format": 2, "relocation": 4, "unresolved": 5,
    "unsupported": 6, "function_table": 8, "init_failed": 9,
}  # MapStatus in tools/pe_map.cpp
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
kernel.GetProcAddress.restype = ctypes.c_void_p
kernel.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
kernel.LoadLibraryExW.restype = wintypes.HMODULE
kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
lookup_entry = kernel.RtlLookupFunctionEntry
lookup_entry.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64), ctypes.c_void_p]
lookup_entry.restype = ctypes.c_void_p


class Mapper:
    def __init__(self, path):
        lib = ctypes.CDLL(str(path))
        self.map = lib.pe_map_image
        self.map.argtypes = [ctypes.c_wchar_p, Resolver, ctypes.POINTER(ctypes.c_uint32),
                             ctypes.c_char_p, ctypes.c_size_t]
        self.map.restype = ctypes.c_void_p
        self.export = lib.pe_map_export
        self.export.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self.export.restype = ctypes.c_void_p
        self.resolved = lib.pe_map_export_resolved
        self.resolved.argtypes = [ctypes.c_void_p, ctypes.c_char_p, Resolver]
        self.resolved.restype = ctypes.c_void_p
        self.release = lib.pe_map_release
        self.release.argtypes = [ctypes.c_void_p]
        self.release.restype = ctypes.c_int  # 1 released, 0 unknown or already released

    def __call__(self, image, bindings):
        """Map image; bindings maps a DLL name to a host module or a dictionary of mapped export addresses."""
        requests = []

        def resolve(dll, name, ordinal):
            requests.append((dll.decode(), name.decode() if name else ordinal))
            module = bindings.get(dll.decode().casefold())
            if module is None:
                return None
            if isinstance(module, dict):
                return module.get(name if name else ordinal)
            return kernel.GetProcAddress(module._handle, name if name else ordinal)

        callback = Resolver(resolve)
        status = ctypes.c_uint32(0xffffffff)
        detail = ctypes.create_string_buffer(256)
        base = self.map(str(image), callback, ctypes.byref(status), detail, len(detail))
        return base, status.value, detail.value.decode(), requests


def describe(image):
    with pefile.PE(str(image)) as pe:
        exports = {e.name.decode(): e.address for e in pe.DIRECTORY_ENTRY_EXPORT.symbols if e.name}
        dir64 = [e.rva for block in getattr(pe, "DIRECTORY_ENTRY_BASERELOC", [])
                 for e in block.entries if e.type == pefile.RELOCATION_TYPE["IMAGE_REL_BASED_DIR64"]]
        stored = {rva: pe.get_qword_at_rva(rva) for rva in dir64}
        iat = {}
        for desc in pe.DIRECTORY_ENTRY_IMPORT:
            for i, imp in enumerate(desc.imports):
                iat[desc.struct.FirstThunk + 8 * i] = imp.name.decode() if imp.name else imp.ordinal
        return pe.OPTIONAL_HEADER.ImageBase, exports, stored, iat


def check_mapping(mapper, case, image, provider, symbol, probe, natives, shim=None):
    preferred, exports, stored, iat = describe(image)
    dll = provider.name.casefold()
    module = ctypes.CDLL(str(shim or provider))
    base, status, detail, requests = mapper(image, {dll: module})
    if status != STATUS["ok"] or not base:
        raise AssertionError(f"{case}: map failed with {status}: {detail}")
    try:
        if base == preferred:
            raise AssertionError(f"{case}: mapped at the preferred base {preferred:#x}")
        if [(name.casefold(), item) for name, item in requests] != [(dll, symbol)]:
            raise AssertionError(f"{case}: unexpected import requests {requests}")
        bound = kernel.GetProcAddress(module._handle, symbol.encode() if isinstance(symbol, str) else symbol)
        slot, = iat
        if ctypes.c_uint64.from_address(base + slot).value != bound:
            raise AssertionError(f"{case}: IAT slot {slot:#x} does not hold the shim export")
        header = ctypes.c_uint32.from_address(base + 0x3c).value  # e_lfanew
        if ctypes.c_uint64.from_address(base + header + 0x30).value != base:  # OptionalHeader.ImageBase
            raise AssertionError(f"{case}: mapped headers keep the preferred ImageBase")
        if case.startswith(("named", "ordinal")) and stored:
            raise AssertionError(f"{case}: import fixture unexpectedly has DIR64 relocations")
        for rva, value in stored.items():
            relocated = ctypes.c_uint64.from_address(base + rva).value
            if relocated != value - preferred + base:
                raise AssertionError(f"{case}: DIR64 at {rva:#x} holds {relocated:#x}")
        fn = mapper.export(base, b"call_mix")
        if fn != base + exports["call_mix"]:
            raise AssertionError(f"{case}: export lookup returned {fn}")
        image_base = ctypes.c_uint64()
        if not lookup_entry(fn, ctypes.byref(image_base), None) or image_base.value != base:
            raise AssertionError(f"{case}: .pdata entry for call_mix is not registered")
        inputs = input_pairs()
        for x, y in inputs:
            actual = observe(probe, fn, x, y)
            expected = reference(x, y, caller=True, altered=shim is not None)
            if actual != expected:
                raise AssertionError(f"{case}({x:#x}, {y:#x}): {actual=} {expected=}")
            if shim is None:
                for name, native in natives:
                    if observe(probe, native, x, y) != actual:
                        raise AssertionError(f"{case}({x:#x}, {y:#x}): {name} differs from mapped image")
    finally:
        mapper.release(base)
    if lookup_entry(fn, ctypes.byref(image_base), None):
        raise AssertionError(f"{case}: function table survived release")
    against = ", ".join(name for name, _ in natives) if shim is None else "altered shim reference"
    print(f"{case}: mapped at {base:#x} (preferred {preferred:#x}), {len(stored)} DIR64 relocation(s), "
          f"{len(inputs)} argument pairs match {against}")


def check_rejections(mapper, image, provider, symbol):
    identity = f"{provider.name}!" + (symbol if isinstance(symbol, str) else f"#{symbol}")
    base, status, detail, _ = mapper(image, {})
    if base or status != STATUS["unresolved"] or detail != f"unresolved import {identity}":
        raise AssertionError(f"unbound import: {base=} {status=} {detail=}")
    with pefile.PE(str(image)) as pe:
        entry = pe.OPTIONAL_HEADER.get_field_absolute_offset("AddressOfEntryPoint")
        imports = pe.OPTIONAL_HEADER.DATA_DIRECTORY[1]
        address = imports.get_field_absolute_offset("VirtualAddress")
        length = imports.get_field_absolute_offset("Size")
        size = pe.OPTIONAL_HEADER.SizeOfImage
        named_thunk = None
        if isinstance(symbol, str):
            pe.parse_data_directories(directories=[1])
            desc, = pe.DIRECTORY_ENTRY_IMPORT
            lookup = desc.struct.OriginalFirstThunk or desc.struct.FirstThunk
            thunk_value = pe.get_qword_at_rva(lookup)
            if len(desc.imports) != 1 or thunk_value >> 31:
                raise AssertionError(f"{image.name}: unexpected named thunk {thunk_value:#x}")
            named_thunk = (pe.get_offset_from_rva(lookup), thunk_value)
    bindings = {provider.name.casefold(): ctypes.CDLL(str(provider))}
    cases = [
        (entry, "<I", size + 0x1000, "format", "entry point outside executable section"),
        (address, "<I", size - 4, "format", "import directory outside image"),
    ]
    if named_thunk is not None:
        thunk_offset, thunk_value = named_thunk
        for bit in (31, 32):
            cases.append((thunk_offset, "<Q", thunk_value | (1 << bit), "format",
                          f"import name for {provider.name} outside image"))
    for field, fmt, value, expected, message in cases:
        data = bytearray(image.read_bytes())
        struct.pack_into(fmt, data, field, value)
        with TemporaryDirectory() as temp:
            damaged = Path(temp) / image.name
            damaged.write_bytes(data)
            base, status, detail, requests = mapper(damaged, bindings)
        if base or status != STATUS[expected] or detail != message or requests:
            raise AssertionError(f"{message} control: {base=} {status=} {detail=} {requests=}")
    print(f"{image.name}: rejected unbound {identity}, an entry point, an import directory outside the image"
          + (", and reserved bits in a named import thunk" if named_thunk is not None else ""))


LOAD_SEARCH = 0x00000100 | 0x00001000  # LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS
kernel.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
kernel.LoadLibraryExW.restype = wintypes.HMODULE
kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
kernel.GetModuleHandleExW.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.HMODULE)]
FROM_ADDRESS_UNCHANGED = 0x4 | 0x2  # GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | _UNCHANGED_REFCOUNT


def bound_symbol(value, slots):
    """Return (dll, symbol) of the slot entry whose provider export equals value, or None."""
    owner = wintypes.HMODULE()
    if not kernel.GetModuleHandleExW(FROM_ADDRESS_UNCHANGED, ctypes.c_void_p(value), ctypes.byref(owner)):
        return None
    for dll, symbol in slots.values():
        if kernel.GetProcAddress(owner, symbol.encode()) == value:
            return dll, symbol
    return None


def check_import_walk(mapper, image, providers):
    """Patch the import directory Size of a two-descriptor image and compare pe_map with Windows.

    The Windows loader finds the import table by address and walks to the null descriptor, so a Size
    that is zero, below one descriptor or ends after the first descriptor still binds every slot.
    """
    with pefile.PE(str(image)) as pe:
        imports = pe.OPTIONAL_HEADER.DATA_DIRECTORY[1]
        length = imports.get_field_absolute_offset("Size")
        built = imports.Size
        slots = {d.struct.FirstThunk + 8 * i: (d.dll.decode().casefold(), imp.name.decode())
                 for d in pe.DIRECTORY_ENTRY_IMPORT for i, imp in enumerate(d.imports)}
    if sorted(dll for dll, _ in slots.values()) != sorted(p.name.casefold() for p in providers):
        raise AssertionError(f"{image.name}: expected one import from each provider, got {slots}")
    descriptor = 20  # sizeof(IMAGE_IMPORT_DESCRIPTOR)
    if built != 3 * descriptor:
        raise AssertionError(f"{image.name}: import directory Size {built}, expected two descriptors and a terminator")
    modules = {p.name.casefold(): ctypes.CDLL(str(p)) for p in providers}
    for value in (built, 0, descriptor - 1, descriptor):
        data = bytearray(image.read_bytes())
        struct.pack_into("<I", data, length, value)
        with TemporaryDirectory() as temp:
            copy = Path(temp) / image.name
            copy.write_bytes(data)
            for p in providers:
                (Path(temp) / p.name).write_bytes(p.read_bytes())
            # Windows: only the IAT is read after the load; nothing in the image runs (no entry point).
            module = kernel.LoadLibraryExW(str(copy), None, LOAD_SEARCH)
            if not module:
                raise AssertionError(f"Size {value}: LoadLibraryExW failed with {ctypes.get_last_error()}")
            try:
                windows = {slot: bound_symbol(ctypes.c_uint64.from_address(module + slot).value, slots)
                           for slot in slots}
            finally:
                kernel.FreeLibrary(module)
            base, status, detail, requests = mapper(copy, modules)
        if status != STATUS["ok"] or not base:
            raise AssertionError(f"Size {value}: pe_map failed with {status}: {detail}")
        try:
            mapped = {slot: bound_symbol(ctypes.c_uint64.from_address(base + slot).value, slots) for slot in slots}
        finally:
            mapper.release(base)
        if sorted((dll.casefold(), item) for dll, item in requests) != sorted(slots.values()):
            raise AssertionError(f"Size {value}: unexpected import requests {requests}")
        if windows != slots or mapped != slots:
            raise AssertionError(f"Size {value}: expected {slots}, Windows bound {windows}, pe_map bound {mapped}")
    print(f"{image.name}: import directory Size {built}, 0, {descriptor - 1} and {descriptor} bind both slots "
          "under LoadLibraryExW and pe_map")


LOAD_ALTERED_SEARCH = 0x8  # LOAD_WITH_ALTERED_SEARCH_PATH: dependencies from the image's own folder


def check_delay_directory(mapper, image, provider):
    """The Windows loader does not read the delay-import directory; pe_map separates its cases.

    An address of 0 or an empty table maps like Windows, a directory outside the image is malformed,
    and a descriptor that names a DLL is unsupported. The named fixture has no delay imports.
    """
    with pefile.PE(str(image)) as pe:
        delay = pe.OPTIONAL_HEADER.DATA_DIRECTORY[13]
        address, size = (delay.get_field_absolute_offset(f) for f in ("VirtualAddress", "Size"))
        if delay.VirtualAddress or delay.Size:
            raise AssertionError(f"{image.name}: fixture must have no delay-import directory")
        imports = pe.OPTIONAL_HEADER.DATA_DIRECTORY[1].VirtualAddress
        image_size = pe.OPTIONAL_HEADER.SizeOfImage
        align = pe.OPTIONAL_HEADER.SectionAlignment
        rdata = next(s for s in pe.sections if s.Name.rstrip(b"\0") == b".rdata")
    # The rest of the .rdata page past VirtualSize is mapped and zero under both loaders: an empty table.
    empty = (rdata.VirtualAddress + rdata.Misc_VirtualSize + 7) & ~7
    if empty + 32 > (rdata.VirtualAddress + rdata.Misc_VirtualSize + align - 1) & ~(align - 1):
        raise AssertionError(f"{image.name}: no room for an empty delay descriptor in .rdata")
    # 8 bytes into the import descriptor, ForwarderChain (0) and Name line up with a delay
    # descriptor's Attributes and DllNameRVA: a descriptor that names the provider DLL.
    named = imports + 8
    cases = (
        ("empty table, Size 0", [(address, empty)], "ok", ""),
        ("empty table, Size 32", [(address, empty), (size, 32)], "ok", ""),
        ("address 0, Size 32", [(size, 32)], "ok", ""),
        ("address past the image", [(address, image_size + 0x1000), (size, 32)], "format",
         "delay import directory outside image"),
        ("descriptor naming a DLL, Size 0", [(address, named)], "unsupported",
         "delay imports without RVA attributes"),
    )
    bindings = {provider.name.casefold(): ctypes.CDLL(str(provider))}
    for name, edits, expected, message in cases:
        data = bytearray(image.read_bytes())
        for offset, value in edits:
            struct.pack_into("<I", data, offset, value)
        with TemporaryDirectory() as temp:
            copy = Path(temp) / image.name
            copy.write_bytes(data)
            (Path(temp) / provider.name).write_bytes(provider.read_bytes())
            # Windows: the copy has no entry point, so loading it runs nothing in the image.
            module = kernel.LoadLibraryExW(str(copy), None, LOAD_ALTERED_SEARCH)
            if not module:
                raise AssertionError(f"delay {name}: LoadLibraryExW failed with {ctypes.get_last_error()}")
            kernel.FreeLibrary(module)
            base, status, detail, requests = mapper(copy, bindings)
        if base:
            mapper.release(base)
        if status != STATUS[expected] or detail != message or bool(base) != (expected == "ok") \
                or bool(requests) != (expected == "ok"):
            raise AssertionError(f"delay {name}: {base=} {status=} {detail=} {requests=}")
    print(f"{image.name}: Windows loads every delay-import variant; pe_map maps an address of 0 and an empty "
          "table, and rejects a directory past the image (format) and a descriptor naming a DLL without RVA "
          "attributes (unsupported)")


kernel.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
kernel.VirtualAlloc.restype = ctypes.c_void_p
kernel.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]


def check_reloc_address_zero(mapper, image, provider, *, oversized):
    """Compare a zero directory RVA with the linked size or a size larger than the image."""
    preferred, _, stored, _ = describe(image)
    (rva, value), = stored.items()
    with pefile.PE(str(image)) as pe:
        relocs = pe.OPTIONAL_HEADER.DATA_DIRECTORY[5]
        address = relocs.get_field_absolute_offset("VirtualAddress")
        size_field = relocs.get_field_absolute_offset("Size")
        built = relocs.Size
        size = pe.OPTIONAL_HEADER.SizeOfImage
    if not built:
        raise AssertionError(f"{image.name}: fixture must have a relocation directory")
    data = bytearray(image.read_bytes())
    struct.pack_into("<I", data, address, 0)
    if oversized:
        built = size + 1
        struct.pack_into("<I", data, size_field, built)
    bindings = {provider.name.casefold(): ctypes.CDLL(str(provider))}
    with TemporaryDirectory() as temp:
        copy = Path(temp) / image.name
        copy.write_bytes(data)
        (Path(temp) / provider.name).write_bytes(provider.read_bytes())
        # Hold the preferred base so neither loader can map the copy there. If the reservation fails,
        # part of that range is already in use, which blocks it just as well; the bases are checked below.
        held = kernel.VirtualAlloc(preferred, size, 0x2000, 0x01)  # MEM_RESERVE, PAGE_NOACCESS
        try:
            # Windows: the copy has no entry point, so loading it runs nothing in the image.
            module = kernel.LoadLibraryExW(str(copy), None, LOAD_ALTERED_SEARCH)
            if not module:
                raise AssertionError(f"relocations at address 0: LoadLibraryExW failed with {ctypes.get_last_error()}")
            try:
                windows = module, ctypes.c_uint64.from_address(module + rva).value
            finally:
                kernel.FreeLibrary(module)
            base, status, detail, _ = mapper(copy, bindings)
        finally:
            if held:
                kernel.VirtualFree(held, 0, 0x8000)  # MEM_RELEASE
    if status != STATUS["ok"] or not base:
        raise AssertionError(f"relocations at address 0, Size {built}: pe_map failed with {status}: {detail}")
    try:
        mapped = base, ctypes.c_uint64.from_address(base + rva).value
    finally:
        mapper.release(base)
    for name, (at, slot) in (("LoadLibraryExW", windows), ("pe_map", mapped)):
        if at == preferred or slot != value:
            raise AssertionError(f"relocations at address 0: {name} mapped at {at:#x} (preferred {preferred:#x}) "
                                 f"with DIR64 at {rva:#x} holding {slot:#x}, expected the linked {value:#x}")
    print(f"{image.name}: relocation directory at address 0 with Size {built} is ignored by LoadLibraryExW and "
          f"pe_map; both map away from {preferred:#x} and leave the DIR64 entry at {value:#x}")


def check_empty_export_slot(mapper, image):
    """Zero one export address table slot: its name must no longer resolve, the other export still must."""
    with pefile.PE(str(image)) as pe:
        exports = pe.DIRECTORY_ENTRY_EXPORT
        table = exports.struct.AddressOfFunctions
        by_name = {e.name.decode(): e for e in exports.symbols if e.name}
        slot = pe.get_offset_from_rva(table + 4 * (by_name["clobber_control"].ordinal - exports.struct.Base))
        kept = by_name["probe"].address
    data = bytearray(image.read_bytes())
    struct.pack_into("<I", data, slot, 0)
    with TemporaryDirectory() as temp:
        damaged = Path(temp) / image.name
        damaged.write_bytes(data)
        base, status, detail, requests = mapper(damaged, {})
    if not base or status != STATUS["ok"] or requests:
        raise AssertionError(f"empty export slot image: {base=} {status=} {detail=} {requests=}")
    try:
        empty, valid = mapper.export(base, b"clobber_control"), mapper.export(base, b"probe")
    finally:
        mapper.release(base)
    if empty is not None or valid != base + kept:
        raise AssertionError(f"export lookup: empty slot gave {empty}, valid export gave {valid}")
    print(f"{image.name}: empty export slot returns null, probe still resolves")


def check_unreadable_export(mapper, image):
    """Map a copy whose export section cannot be read: lookup must return null, not a pointer into it.

    A section can be mapped without read access by its characteristics. The export directory, its
    three tables and each name string must be checked against the live protection before they are
    read, so a lookup returns null instead of faulting or returning a pointer into an unreadable page.
    """
    with pefile.PE(str(image)) as pe:
        export = pe.OPTIONAL_HEADER.DATA_DIRECTORY[0]
        span = lambda s: max(s.Misc_VirtualSize, s.SizeOfRawData)
        section = next(s for s in pe.sections
                       if s.VirtualAddress <= export.VirtualAddress < s.VirtualAddress + span(s))
        offset = (pe.OPTIONAL_HEADER.get_file_offset() + pe.FILE_HEADER.SizeOfOptionalHeader
                  + pe.sections.index(section) * 40 + 36)  # Characteristics in IMAGE_SECTION_HEADER
        characteristics = section.Characteristics
    cases = (("no read", characteristics & ~0x40000000),
             ("execute only", (characteristics & ~0x40000000) | 0x20000000))
    for name, value in cases:
        data = bytearray(image.read_bytes())
        struct.pack_into("<I", data, offset, value)
        with TemporaryDirectory() as temp:
            copy = Path(temp) / image.name
            copy.write_bytes(data)
            base, status, detail, requests = mapper(copy, {})
        if not base or status != STATUS["ok"] or requests:
            raise AssertionError(f"unreadable export {name}: map failed with {status}: {detail}")
        try:
            resolved = [mapper.export(base, symbol) for symbol in (b"probe", b"clobber_control")]
        finally:
            mapper.release(base)
        if any(resolved):
            raise AssertionError(f"unreadable export {name}: lookup returned {resolved}")
    print(f"{image.name}: export lookup returns null when the export section has no read access")


def check_reloc_and_pdata_bounds(mapper, image, provider):
    """Record native loader observations and check the mapper against its supported directory bounds."""
    with pefile.PE(str(image)) as pe:
        relocs = pe.OPTIONAL_HEADER.DATA_DIRECTORY[5]
        reloc_size = relocs.get_field_absolute_offset("Size")
        block_size = pe.get_offset_from_rva(relocs.VirtualAddress) + 4
        orig_block_len = pe.get_dword_at_rva(relocs.VirtualAddress + 4)
        pdata = pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]
        pdata_size = pdata.get_field_absolute_offset("Size")
        export_rva = next(e.address for e in pe.DIRECTORY_ENTRY_EXPORT.symbols if e.name == b"call_mix")
    bindings = {provider.name.casefold(): ctypes.CDLL(str(provider))}
    cases = (
        ("reloc Size=4", [(reloc_size, 4)], "relocation", "bad relocation block", False),
        ("reloc trailing +4", [(reloc_size, relocs.Size + 4)], "relocation", "bad relocation block", False),
        ("reloc odd SizeOfBlock leaving 1 byte", [(block_size, orig_block_len - 1)],
         "relocation", "bad relocation block", False),
        ("reloc odd SizeOfBlock matching Size", [(block_size, orig_block_len - 1), (reloc_size, orig_block_len - 1)],
         "relocation", "bad relocation block", False),
        ("pdata Size=0 with non-zero RVA", [(pdata_size, 0)], "ok", "", True),
        ("pdata Size=11", [(pdata_size, 11)], "function_table", "cannot register .pdata", True),
        ("pdata Size=20", [(pdata_size, 20)], "function_table", "cannot register .pdata", True),
    )
    preferred, _, _, _ = describe(image)
    for name, edits, expected, message, want_requests in cases:
        data = bytearray(image.read_bytes())
        for offset, value in edits:
            struct.pack_into("<I", data, offset, value)
        with TemporaryDirectory() as temp:
            copy = Path(temp) / image.name
            copy.write_bytes(data)
            (Path(temp) / provider.name).write_bytes(provider.read_bytes())
            ctypes.set_last_error(0)
            module = kernel.LoadLibraryExW(str(copy), None, LOAD_SEARCH)
            err = ctypes.get_last_error()
            try:
                found = None
                if module:
                    image_base = ctypes.c_uint64()
                    found = bool(lookup_entry(module + export_rva, ctypes.byref(image_base), None))
                print(f"{name}: Windows build {sys.getwindowsversion().build}, module={module}, "
                      f"preferred={preferred:#x}, error={err}, function_entry={found}")
            finally:
                if module:
                    kernel.FreeLibrary(module)
            base, status, detail, requests = mapper(copy, bindings)
        if base:
            try:
                if expected == "ok":
                    owner = ctypes.c_uint64()
                    if lookup_entry(base + export_rva, ctypes.byref(owner), None):
                        raise AssertionError(f"{name}: unexpectedly registered an unwind entry")
            finally:
                mapper.release(base)
        if bool(base) != (expected == "ok") or status != STATUS[expected] or detail != message or bool(requests) != want_requests:
            raise AssertionError(f"{name}: {base=} {status=} {detail=} {requests=}")
    print(f"{image.name}: compared trailing relocation bytes, odd SizeOfBlock and unaligned .pdata "
          "under LoadLibraryExW and pe_map")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mapper", type=Path)
    parser.add_argument("probe", type=Path)
    parser.add_argument("--named", type=Path, nargs=4, metavar=("ORIGINAL", "LIFTED", "PROVIDER", "ALTERED"),
                        required=True)
    parser.add_argument("--ordinal", type=Path, nargs=4, metavar=("ORIGINAL", "LIFTED", "PROVIDER", "ALTERED"),
                        required=True)
    parser.add_argument("--reloc", type=Path, nargs=3, metavar=("ORIGINAL", "PROVIDER", "ALTERED"), required=True)
    parser.add_argument("--two", type=Path, nargs=3, metavar=("IMAGE", "PROVIDER", "SECOND_PROVIDER"), required=True)
    args = parser.parse_args()
    mapper = Mapper(args.mapper)
    probe = register_probe(str(args.probe))
    check_empty_export_slot(mapper, args.probe)
    check_unreadable_export(mapper, args.probe)
    check_delay_directory(mapper, args.named[0], args.named[2])
    for oversized in (False, True):
        check_reloc_address_zero(mapper, args.reloc[0], args.reloc[1], oversized=oversized)
    for case, paths, symbol in (("named", args.named, "mix_args"), ("ordinal", args.ordinal, 77)):
        original, lifted, provider, altered = paths
        natives = [("native LoadLibrary", ctypes.CDLL(str(original)).call_mix),
                   ("lifted DLL", ctypes.CDLL(str(lifted)).lifted)]
        check_mapping(mapper, case, original, provider, symbol, probe, natives)
        check_mapping(mapper, f"{case} with altered shim", original, provider, symbol, probe, [], altered)
        check_rejections(mapper, original, provider, symbol)
    original, provider, altered = args.reloc
    natives = [("native LoadLibrary", ctypes.CDLL(str(original)).call_mix)]
    if len(describe(original)[2]) != 1:
        raise AssertionError("relocation fixture must have exactly one DIR64 entry")
    check_mapping(mapper, "relocated", original, provider, "mix_args", probe, natives)
    check_mapping(mapper, "relocated with altered shim", original, provider, "mix_args", probe, [], altered)
    check_reloc_and_pdata_bounds(mapper, original, provider)
    image, *providers = args.two
    check_import_walk(mapper, image, providers)


if __name__ == "__main__":
    sys.exit(main())
