"""Conditional routes must not overrule an ordinary import path or apply to unknown binaries."""

from pathlib import Path
import struct
import sys

import pefile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from dependency_paths import FFX_EXPORTS, FFX_LOADER, trace
from pe_inventory import import_tables, resolve_desktop


def check_desktop_forwarders():
    very_long_digits = "9" * 10000
    very_long_zeros = "0" * 10000
    tables = {
        "front.dll": {"Begin": "api-ms-fixture-l1-1-0.#77", "Broken": "middle.Missing",
                      "Loop": "MIDDLE.dll.Loop", "Bad": "missing_separator",
                      "OrdMin": "middle.#1", "OrdMax": "middle.#65535",
                      "OrdZero": "middle.#0", "OrdEmpty": "middle.#",
                      "OrdOverflow": "middle.#65536", "OrdSuffix": "middle.#12abc",
                      "OrdNeg": "middle.#-1", "OrdNonAscii": "middle.#\u0661",
                      "OrdLong": f"middle.#{very_long_digits}",
                      "OrdLongZero": f"middle.#{very_long_zeros}"},
        "middle.dll": {"#1": "last.Value", "#77": "last.Value", "#65535": "last.Value",
                       "#0": "last.Value", "Loop": "front.Loop"},
        "last.dll": {"Value": None},
    }
    contracts = {"api-ms-fixture-l1-1": {"": "absent.dll", "front.dll": "middle.dll"}}

    def resolve(dll, name):
        return resolve_desktop(dll, name, "entry.exe", contracts, tables.get)

    valid = resolve("front.dll", "Begin")
    assert valid["status"] == "export_present"
    assert [(s["provider"], s["symbol"]) for s in valid["chain"]] == [
        ("front.dll", "Begin"), ("middle.dll", "#77"), ("last.dll", "Value")]
    assert resolve("api-ms-fixture-l1-1-0.dll", "#77")["status"] == "provider_not_found"
    assert resolve("front.dll", "Loop")["status"] == "forwarder_cycle"
    assert resolve("front.dll", "Bad")["status"] == "invalid_forwarder"
    for sym, expected_ord in (("OrdMin", "#1"), ("OrdMax", "#65535")):
        res = resolve("front.dll", sym)
        assert res["status"] == "export_present", (sym, res)
        assert [(s["provider"], s["symbol"]) for s in res["chain"]] == [
            ("front.dll", sym), ("middle.dll", expected_ord), ("last.dll", "Value")], (sym, res)
    for bad_sym in ("OrdZero", "OrdEmpty", "OrdOverflow", "OrdSuffix",
                    "OrdNeg", "OrdNonAscii", "OrdLong", "OrdLongZero"):
        res = resolve("front.dll", bad_sym)
        assert res["status"] == "invalid_forwarder", (bad_sym, res)
        assert res["chain"][-1]["forwarder"] == tables["front.dll"][bad_sym], (bad_sym, res)
    broken = resolve("front.dll", "Broken")
    assert broken["status"] == "export_not_found" and broken["chain"][-1]["symbol"] == "Missing"

    # A packaged module shadows the System32 name. API-set contracts still select System32 hosts.
    package = {"shim.dll": (["bin/shim.dll"], {"Begin": "middle.#77", "Api": "api-ms-fixture-l1-1-0.#77"}),
               "middle.dll": (["bin/middle.dll"], {}), "twin.dll": (["a/twin.dll", "b/twin.dll"], None)}
    shadowed = resolve_desktop("shim.dll", "Begin", "entry.exe", contracts, tables.get,
                               lambda name: package.get(name, ((), None)))
    assert shadowed["status"] == "export_not_found" and shadowed["chain"][-1]["provider"] == "bin/middle.dll"
    contract = resolve_desktop("shim.dll", "Api", "entry.exe", contracts, tables.get,
                               lambda name: package.get(name, ((), None)))
    assert contract["status"] == "provider_not_found" and contract["chain"][-1]["provider"] == "absent.dll"
    twin = resolve_desktop("twin.dll", "Value", "entry.exe", contracts, tables.get, package.get)
    assert twin["status"] == "ambiguous_package_basename" and twin["chain"][-1]["provider"] is None

    # A KnownDLL name maps from System32 before the application directory, also at a forwarder hop.
    tables["known.dll"] = {"Value": None}
    package.update({"known.dll": (["bin/known.dll"], {"Value": None, "Extra": None}),
                    "relay.dll": (["bin/relay.dll"], {"Value": "KNOWN.Value", "Extra": "known.Extra"})})
    known = {"known.dll"}

    def packaged(dll, name, known_dlls=known):
        return resolve_desktop(dll, name, "entry.exe", contracts, tables.get,
                               lambda module: package.get(module, ((), None)), known_dlls)

    assert packaged("known.dll", "Extra", ())["chain"][0]["provider"] == "bin/known.dll"
    system = packaged("known.dll", "Value")
    assert system["status"] == "export_present"
    assert system["chain"] == [dict(dll="known.dll", provider="known.dll", symbol="Value", package=False,
                                    known_dll=True)]
    assert packaged("known.dll", "Extra")["status"] == "export_not_found"
    hop = packaged("relay.dll", "Extra")
    assert hop["status"] == "export_not_found"
    assert [(s["provider"], s["package"], s.get("known_dll")) for s in hop["chain"]] == [
        ("bin/relay.dll", True, None), ("known.dll", False, True)]
    assert packaged("relay.dll", "Value")["chain"][-1]["provider"] == "known.dll"
    # API-set contracts keep their own route even if a schema host name is also a KnownDLL.
    assert "known_dll" not in packaged("api-ms-fixture-l1-1-0.dll", "#77", {"absent.dll"})["chain"][0]

    row = dict(image="entry.exe", kind="normal", dll="front.dll", symbol="Broken",
               bundle_exports_in=[], desktop_export="export_present", desktop_resolution=broken)
    inventory = dict(images={"entry.exe": {"sha256": "fixture", "exports": []}}, imports=[row])
    finding, = trace(inventory, "entry.exe")["findings"]
    assert finding["problem"] == "export_not_found"
    assert finding["desktop_chain"][-1] == dict(dll="middle.dll", provider="middle.dll", symbol="Missing")
    del row["desktop_resolution"]
    assert not trace(inventory, "entry.exe")["findings"]

    # Each packaged module in a forwarder chain is loaded with the provider, including intermediate ones.
    modules = ("front.dll", "relay.dll", "last.dll")
    hops = [dict(dll=n, provider=n, symbol="Value", package=True) for n in modules]
    inventory["images"].update({n: {"sha256": "fixture", "exports": []} for n in modules})
    row.update(symbol="Value", bundle_exports_in=["front.dll"],
               package_resolution=dict(status="export_present", chain=hops))
    report = trace(inventory, "entry.exe")
    assert not report["findings"]
    assert report["reachable"]["relay.dll"]["reasons"] == ("normal", "forwarder")
    assert report["reachable"]["last.dll"]["path"] == ("entry.exe", "front.dll", "relay.dll", "last.dll")

    # Forwarded chains ending at an ambiguous package basename keep resolved hops without queuing None.
    ambiguous_pkg = {
        "front.dll": (["front.dll"], {"DirectTwin": "twin.Value", "Twin": "mid.Step"}),
        "mid.dll": (["bin/mid.dll"], {"Step": "twin.Value"}),
        "twin.dll": (["a/twin.dll", "b/twin.dll"], None),
    }
    direct_twin = resolve_desktop("front.dll", "DirectTwin", "entry.exe", contracts, tables.get,
                                  lambda name: ambiguous_pkg.get(name, ((), None)))
    relayed_twin = resolve_desktop("front.dll", "Twin", "entry.exe", contracts, tables.get,
                                   lambda name: ambiguous_pkg.get(name, ((), None)))
    for result in (direct_twin, relayed_twin):
        assert result["status"] == "ambiguous_package_basename"
        assert result["chain"][-1] == dict(dll="twin.dll", provider=None, symbol="Value", package=True)
    inventory["images"].update({n: {"sha256": "fixture", "exports": []}
                                for n in ("bin/mid.dll", "a/twin.dll", "b/twin.dll")})
    direct_row = dict(image="entry.exe", kind="normal", dll="front.dll", symbol="DirectTwin",
                      bundle_exports_in=["front.dll"], desktop_export="provider_not_found",
                      package_resolution=direct_twin)
    relayed_row = dict(image="entry.exe", kind="normal", dll="front.dll", symbol="Twin",
                       bundle_exports_in=["front.dll"], desktop_export="provider_not_found",
                       package_resolution=relayed_twin)
    for imports_case in ([direct_row], [row, direct_row, relayed_row]):
        inventory["imports"] = imports_case
        report = trace(inventory, "entry.exe")
        assert None not in report["reachable"]
        problems = {f["symbol"]: (f["problem"], f["package_chain"]) for f in report["findings"]}
        assert problems["DirectTwin"] == ("ambiguous_package_basename", direct_twin["chain"])
    assert report["reachable"]["bin/mid.dll"]["path"] == ("entry.exe", "front.dll", "bin/mid.dll")
    assert report["reachable"]["bin/mid.dll"]["reasons"] == ("normal", "forwarder")
    assert problems["Twin"] == ("ambiguous_package_basename", relayed_twin["chain"])

    inventory["imports"] = [row]
    # A package copy of a KnownDLL is not loaded. The System32 result decides the edge.
    inventory["images"]["known.dll"] = {"sha256": "fixture", "exports": [{"name": "Extra"}]}
    missing = packaged("known.dll", "Extra")
    row.update(dll="known.dll", symbol="Extra", bundle_exports_in=["known.dll"], desktop_export="export_not_found",
               desktop_resolution=resolve("known.dll", "Extra"), package_resolution=missing)
    report = trace(inventory, "entry.exe")
    assert "known.dll" in report["unlinked_in_this_graph"]
    finding, = report["findings"]
    assert finding["problem"] == "export_not_found" and finding["candidates"] == []
    assert finding["desktop_chain"] == [dict(dll="known.dll", provider="known.dll", symbol="Extra")]
    row.update(symbol="Value", desktop_export="export_present", desktop_resolution=resolve("known.dll", "Value"),
               package_resolution=packaged("known.dll", "Value"))
    report = trace(inventory, "entry.exe")
    assert not report["findings"] and "known.dll" in report["unlinked_in_this_graph"]

    # A forwarder in the System32 copy of a known DLL can name a packaged module. That module is loaded,
    # and the package chain's terminal status decides the edge, not the first hop's System32 table.
    tables["known.dll"].update({"Fwd": "target.Fwd", "Gap": "target.Gap"})
    tables["target.dll"] = {"Fwd": None, "Gap": None}
    package["target.dll"] = (["bin/target.dll"], {"Fwd": None})
    forward, gap = packaged("known.dll", "Fwd"), packaged("known.dll", "Gap")
    assert forward["status"] == "export_present" and gap["status"] == "export_not_found"
    assert [(s["provider"], s["package"]) for s in forward["chain"]] == [("known.dll", False), ("bin/target.dll", True)]
    assert resolve_desktop("known.dll", "Gap", "entry.exe", contracts, tables.get)["status"] == "export_present"
    inventory["images"]["bin/target.dll"] = {"sha256": "fixture", "exports": [{"name": "Fwd"}]}
    row.update(symbol="Fwd", desktop_export="export_present", desktop_resolution=resolve("known.dll", "Fwd"),
               package_resolution=forward)
    report = trace(inventory, "entry.exe")
    assert not report["findings"] and "known.dll" in report["unlinked_in_this_graph"]
    assert report["reachable"]["bin/target.dll"]["path"] == ("entry.exe", "bin/target.dll")
    assert report["reachable"]["bin/target.dll"]["reasons"] == ("forwarder",)
    row.update(symbol="Gap", desktop_resolution=resolve("known.dll", "Gap"), package_resolution=gap)
    report = trace(inventory, "entry.exe")
    finding, = report["findings"]
    assert finding["problem"] == "export_not_found" and finding["package_chain"] == gap["chain"]
    assert "bin/target.dll" in report["reachable"]
    assert not finding["conditional"]
    # The same broken known-DLL chain behind a delay import is a conditional finding.
    row["kind"] = "delay"
    finding, = trace(inventory, "entry.exe")["findings"]
    assert finding["conditional"] and finding["reasons"] == ("delay",)


def check_conditional_findings():
    """Each unusable FFX route names its cause, and only a delay import's finding is conditional."""
    loader = "amd_fidelityfx_loader_xbox.dll"
    # Package paths keep their case on disk; the basename match ignores it.
    upscaler = "pkg/AMD_FidelityFX_Upscaler_Xbox.dll"
    framegen = "amd_fidelityfx_framegeneration_xbox.dll"
    twins = [f"a/{framegen}", f"b/{framegen}"]
    images = {loader: {"sha256": FFX_LOADER, "exports": []},
              upscaler: {"sha256": "fixture", "exports": [{"name": "ffxQuery"}, {"name": "ffxDispatch"}]}}
    images.update({n: {"sha256": "fixture", "exports": [{"name": s} for s in FFX_EXPORTS]} for n in twins})
    imports = [dict(image=loader, kind=kind, dll=f"{kind}.dll", symbol="Value", bundle_exports_in=[],
                    desktop_export="provider_not_found") for kind in ("normal", "delay")]
    data = dict(images=images, imports=imports)

    report = trace(data, loader)
    findings = {f["dll"]: f for f in report["findings"]}
    assert set(findings) == {"normal.dll", "delay.dll", "amd_fidelityfx_upscaler_xbox.dll", framegen}, findings
    assert report["reachable"][upscaler]["reasons"] == ("ffx:0x10000",)
    # The upscaler exports two of the five FFX entry points; the other three are listed in sorted order.
    short = findings["amd_fidelityfx_upscaler_xbox.dll"]
    assert short["problem"] == "conditional_exports_missing" and short["conditional"]
    assert short["missing_exports"] == ["ffxConfigure", "ffxCreateContext", "ffxDestroyContext"]
    assert short["candidates"] == [upscaler]
    twin = findings[framegen]
    assert twin["problem"] == "ambiguous_package_basename" and twin["conditional"]
    assert twin["candidates"] == twins and twin["missing_exports"] == []
    assert twin["reasons"] == ("ffx:0x20000|0x30000",)
    assert findings["normal.dll"]["problem"] == findings["delay.dll"]["problem"] == "provider_not_found"
    assert not findings["normal.dll"]["conditional"] and findings["delay.dll"]["conditional"]

    for name in twins:
        del images[name]
    absent = {f["dll"]: f for f in trace(data, loader)["findings"]}[framegen]
    assert absent["problem"] == "conditional_module_absent_from_package"
    assert absent["candidates"] == [] and absent["missing_exports"] == []


def main():
    loader = "amd_fidelityfx_loader_xbox.dll"
    upscaler = "amd_fidelityfx_upscaler_xbox.dll"
    framegen = "amd_fidelityfx_framegeneration_xbox.dll"
    data = dict(images={n: {"sha256": "fixture", "exports": [{"name": s} for s in FFX_EXPORTS]}
                        for n in ("entry.exe", "bridge.dll", loader, upscaler)}, imports=[])
    data["images"][loader]["sha256"] = FFX_LOADER

    def edge(caller, target, kind="normal"):
        data["imports"].append(dict(image=caller, kind=kind, dll=target, symbol="ffxQuery",
                                    bundle_exports_in=[target], desktop_export="provider_not_found"))

    edge("entry.exe", loader)
    edge("entry.exe", upscaler, "delay")
    edge(loader, "bridge.dll")
    edge("bridge.dll", upscaler)
    edge(upscaler, "bridge.dll")
    report = trace(data, "entry.exe")
    assert report["reachable"][upscaler]["reasons"] == ("normal", "normal", "normal")
    finding, = report["findings"]
    assert finding["dll"] == framegen and finding["conditional"]
    # A different loader build has no conditional routes, and the report says why instead of staying silent.
    data["images"][loader]["sha256"] = "other build"
    unknown = trace(data, "entry.exe")
    finding, = unknown["findings"]
    assert finding["problem"] == "unverified_loader_hash", finding
    assert finding["sha256"] == "other build" and finding["expected"] == FFX_LOADER
    assert "ffx:0x10000" not in unknown["reachable"][upscaler]["reasons"]


def check_pe32plus_import_reserved_bits():
    """Reject PE32+ by-name import thunks with reserved bits 62-31 set."""
    def build_pe(thunk_val):
        pe_offset = 0x80
        section_rva = 0x1000
        raw_offset = 0x200
        raw_size = 0x200
        buf = bytearray(raw_offset + raw_size)
        buf[0:2] = b"MZ"
        struct.pack_into("<I", buf, 60, pe_offset)
        buf[pe_offset:pe_offset + 4] = b"PE\0\0"
        struct.pack_into("<HHIIIHH", buf, pe_offset + 4, 0x8664, 1, 0, 0, 0, 240, 0x2022)
        opt = pe_offset + 24
        struct.pack_into("<HBBIIIIIQIIHHHHHHIIIIHHQQQQII", buf, opt,
                         0x20B, 0, 0, 0, raw_size, 0, 0, section_rva, 0x180000000,
                         0x1000, 0x200, 6, 0, 0, 0, 6, 0, 0, 0x2000, raw_offset,
                         0, 3, 0, 0, 0, 0, 0, 0, 16)
        sec = opt + 240
        struct.pack_into("<8sIIIIIIHHI", buf, sec,
                         b".rdata\0\0", 0x200, section_rva, raw_size, raw_offset,
                         0, 0, 0, 0, 0x40000040)
        struct.pack_into("<II", buf, opt + 112 + 8, section_rva, 40)
        struct.pack_into("<5I", buf, raw_offset, section_rva + 0x30, 0, 0, section_rva + 0x50, 0)
        struct.pack_into("<QQ", buf, raw_offset + 0x30, thunk_val, 0)
        module_name = b"provider.dll\0"
        buf[raw_offset + 0x50:raw_offset + 0x50 + len(module_name)] = module_name
        struct.pack_into("<H", buf, raw_offset + 0x70, 0)
        func_name = b"test_func\0"
        buf[raw_offset + 0x72:raw_offset + 0x72 + len(func_name)] = func_name
        assert len(buf) == raw_offset + raw_size
        return bytes(buf)

    valid_pe = build_pe(0x1070)
    with pefile.PE(data=valid_pe, fast_load=True) as pe:
        rows = import_tables(pe, "synthetic.exe")
    assert rows == [("normal", "provider.dll", "test_func")]

    for bad_thunk in (0x1070 | (1 << 31), 0x1070 | (1 << 40), 0x1070 | (1 << 62)):
        invalid_pe = build_pe(bad_thunk)
        with pefile.PE(data=invalid_pe, fast_load=True) as pe:
            try:
                import_tables(pe, "synthetic.exe")
            except ValueError as error:
                assert "Reserved bits set in PE32+ import thunk" in str(error)
            else:
                raise AssertionError(f"import_tables accepted reserved bits in thunk {bad_thunk:#x}")


if __name__ == "__main__":
    check_desktop_forwarders()
    check_pe32plus_import_reserved_bits()
    check_conditional_findings()
    main()
