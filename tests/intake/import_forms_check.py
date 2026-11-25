"""Synthetic PE32+ controls for named, ordinal and delay-load import forms."""

from pathlib import Path
import sys

import pefile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
from compat_report_check import RAW_OFFSET, SECTION_RVA, build_image  # noqa: E402
from intake_exe import import_routes, routing_failures  # noqa: E402
from pe_inventory import import_tables  # noqa: E402
from pe_package_preflight import summarize  # noqa: E402
from pe_run_package import plan  # noqa: E402


CALLER = "forms.exe"
SOURCE_FORMS = [
    ("normal", "xmem.dll", "XMemAlloc"),
    ("normal", "xmem.dll", 77),
    ("delay", "xgameruntime.dll", "XUserGetId"),
    ("delay", "xgameruntime.dll", 9),
]
EXPECTED_FORMS = [
    ("normal", "xmem.dll", "XMemAlloc"),
    ("normal", "xmem.dll", "#77"),
    ("delay", "xgameruntime.dll", "XUserGetId"),
    ("delay", "xgameruntime.dll", "#9"),
]
PROVIDER_DEF = ROOT / "tests" / "intake" / "provider.def"
PROVIDER_EXPORTS = [
    {"ordinal": 1, "name": "named_value", "forwarder": None},
    {"ordinal": 77, "name": None, "forwarder": None},
]


def check_import_tables():
    image_bytes = build_image(SOURCE_FORMS)
    with pefile.PE(data=image_bytes, fast_load=True) as pe:
        rows = import_tables(pe, CALLER)
    assert rows == EXPECTED_FORMS, rows

    routes = import_routes(image_bytes, CALLER)
    assert [(row["kind"], row["dll"], row["symbol"]) for row in routes] == EXPECTED_FORMS
    keys = {"image", "kind", "dll", "symbol", "route", "reason", "route_kind",
            "resolution", "provider", "stub", "candidate_provider"}
    for row in routes:
        assert set(row) == keys
        assert row["route"] == ("hle" if row["dll"] == "xmem.dll" else "stub")
        assert row["route_kind"] == "candidate"
        assert row["resolution"] == "not_checked"
        assert row["provider"] is None
        assert row["stub"] == (row["route"] == "stub")
        assert row["candidate_provider"] == ("xmem.dll" if row["route"] == "hle" else None)
        assert "desktop_resolution" not in row and "package_resolution" not in row
    assert routing_failures(routes) == []


def check_module_names():
    # The loader looks for a lower-cased file name: .dll is appended to a name without an
    # extension, and a trailing dot stops the append and is dropped. 0x1234 = 4660 needs the
    # full 16-bit ordinal field.
    image_bytes = build_image([
        ("normal", "XMem", "XMemAlloc"),
        ("normal", "Provider.", 0x1234),
        ("delay", "XGameRuntime.DLL", "XUserGetId"),
    ])
    expected = [
        ("normal", "provider", "#4660"),
        ("normal", "xmem.dll", "XMemAlloc"),
        ("delay", "xgameruntime.dll", "XUserGetId"),
    ]
    with pefile.PE(data=image_bytes, fast_load=True) as pe:
        assert import_tables(pe, CALLER) == expected
    routes = import_routes(image_bytes, CALLER)
    assert [row["route"] for row in routes] == ["unsupported", "hle", "stub"], routes
    assert routing_failures(routes) == [routes[0]]


def thunk_slots(data, rva):
    """Return the file offsets of the 8-byte table entries before the null terminator."""
    offset = rva - SECTION_RVA + RAW_OFFSET
    slots = []
    while int.from_bytes(data[offset:offset + 8], "little"):
        slots.append(offset)
        offset += 8
    return slots


def check_descriptor_tables():
    image_bytes = build_image(SOURCE_FORMS)
    with pefile.PE(data=image_bytes, fast_load=True) as pe:
        directories = pe.OPTIONAL_HEADER.DATA_DIRECTORY
        normal, delay = (directories[index].VirtualAddress - SECTION_RVA + RAW_OFFSET
                         for index in (1, 13))
    # build_image writes one descriptor per module: xmem.dll is normal, xgameruntime.dll delay.

    # A bound IAT holds provider addresses and a delay IAT holds addresses of the image's own
    # load thunks. Names come from OriginalFirstThunk (normal +0) and the INT (delay +16), not
    # from FirstThunk (normal +16) or the delay IAT (delay +12).
    bound = bytearray(image_bytes)
    for field, address in ((normal + 16, 0x7FF812340000), (delay + 12, 0x180002000)):
        iat = int.from_bytes(bound[field:field + 4], "little")
        for number, slot in enumerate(thunk_slots(bound, iat)):
            bound[slot:slot + 8] = (address + 16 * number).to_bytes(8, "little")
    # Without OriginalFirstThunk the loader reads the names from the unbound FirstThunk table.
    first_thunk_only = bytearray(image_bytes)
    first_thunk_only[normal:normal + 4] = bytes(4)

    for variant in (bound, first_thunk_only):
        with pefile.PE(data=bytes(variant), fast_load=True) as pe:
            assert import_tables(pe, CALLER) == EXPECTED_FORMS


def image(name, exports=None, imports=()):
    return (dict(machine="IMAGE_FILE_MACHINE_AMD64", sha256=name,
                 exports=list(exports or []), parse_warnings=[]), tuple(imports))


def package_report(missing):
    images = {
        "app.exe": image("app", imports=[("normal", "intake_provider.dll", missing)]),
        "intake_provider.dll": image("provider", exports=PROVIDER_EXPORTS),
    }
    return summarize(images, "app.exe")


def check_missing_binding():
    # The synthetic provider mirrors the checked-in provider.def surface used here.
    provider_text = PROVIDER_DEF.read_text(encoding="utf-8")
    assert "named_value" in provider_text
    assert "ordinal_value @77 NONAME" in provider_text

    for present in ("named_value", "#77"):
        order, bindings = plan(package_report(present))
        assert order == ["intake_provider.dll", "app.exe"]
        assert bindings["app.exe"][("intake_provider.dll", present)]["provider"] == (
            "intake_provider.dll")

    for missing in ("missing_value", "#78"):
        report = package_report(missing)
        row, = report["imports"]
        assert row["status"] == "bundled_export_not_found"
        assert row["chain"] == [dict(dll="intake_provider.dll", provider="intake_provider.dll",
                                     symbol=missing, package=True)]
        try:
            plan(report)
        except ValueError as error:
            assert str(error) == (
                f"app.exe: intake_provider.dll!{missing}: bundled_export_not_found"), error
        else:
            raise AssertionError(f"{missing} was accepted by pe_run_package.plan")


def main():
    check_import_tables()
    check_module_names()
    check_descriptor_tables()
    check_missing_binding()
    print("named, ordinal and delay-load import form checks passed")


if __name__ == "__main__":
    main()
