"""Pure-Python controls for pe_run_package planning, helpers, and CLI refusal paths."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "pe_run_package.py"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from compat_report_check import POSITIVE_IMPORTS, build_image  # noqa: E402
from pe_run_package import is_host_create_thread, plan, trap_key  # noqa: E402


def pkg_step(provider, symbol, dll=None):
    return {
        "provider": provider,
        "symbol": symbol,
        "dll": dll or provider,
        "package": True,
    }


def ext_step(dll, symbol):
    return {
        "provider": dll,
        "symbol": symbol,
        "dll": dll,
        "package": False,
    }


def import_row(image, dll, symbol, chain, status="bundled_export_present", kind="normal"):
    return {
        "image": image,
        "dll": dll,
        "symbol": symbol,
        "kind": kind,
        "status": status,
        "chain": chain,
    }


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-B", str(TOOL), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


class RunPackagePlanChecks(unittest.TestCase):
    def test_topological_load_order_across_multi_dll_chains(self):
        # Diamond + chain: app.exe -> (a.dll -> c.dll, b.dll -> c.dll -> d.dll)
        report = {
            "entry": "app.exe",
            "reachable": {
                "app.exe": {},
                "a.dll": {},
                "b.dll": {},
                "c.dll": {},
                "d.dll": {},
            },
            "imports": [
                import_row("app.exe", "a.dll", "InitA", [pkg_step("a.dll", "InitA")]),
                import_row("app.exe", "b.dll", "InitB", [
                    pkg_step("b.dll", "InitB"),
                    pkg_step("c.dll", "CoreC"),
                ]),
                import_row("a.dll", "c.dll", "CoreC", [pkg_step("c.dll", "CoreC")]),
                import_row("c.dll", "d.dll", "LeafD", [pkg_step("d.dll", "LeafD")]),
                import_row(
                    "d.dll",
                    "kernel32.dll",
                    "ExitProcess",
                    [ext_step("kernel32.dll", "ExitProcess")],
                    status="external_unverified",
                ),
                import_row(
                    "b.dll",
                    "shim.dll",
                    "FwdExt",
                    [pkg_step("c.dll", "FwdExt", dll="shim.dll"), ext_step("host.dll", "HostApi")],
                    status="forwarded_external_unverified",
                ),
            ],
        }
        order, routes = plan(report, allow_external=True)
        index = {name: i for i, name in enumerate(order)}
        self.assertEqual(set(order), {"app.exe", "a.dll", "b.dll", "c.dll", "d.dll"})
        self.assertEqual(order[-1], "app.exe")
        self.assertLess(index["d.dll"], index["c.dll"])
        self.assertLess(index["c.dll"], index["a.dll"])
        self.assertLess(index["c.dll"], index["b.dll"])
        self.assertLess(index["a.dll"], index["app.exe"])
        self.assertLess(index["b.dll"], index["app.exe"])
        self.assertEqual(routes["app.exe"][("b.dll", "InitB")]["provider"], "c.dll")
        self.assertEqual(routes["b.dll"][("shim.dll", "FwdExt")]["dll"], "host.dll")

    def test_is_host_create_thread_matches_expected_contracts(self):
        for dll in (
            "kernel32.dll",
            "KERNEL32.DLL",
            "kernelbase.dll",
            "KernelBase.dll",
            "api-ms-win-core-processthreads-l1-1-0.dll",
            "API-MS-WIN-CORE-PROCESSTHREADS-L1-1-2.DLL",
        ):
            with self.subTest(matching_dll=dll):
                self.assertTrue(is_host_create_thread(dll, "CreateThread"))

        for dll, symbol in (
            ("advapi32.dll", "CreateThread"),
            ("ntdll.dll", "CreateThread"),
            ("api-ms-win-core-synch-l1-1-0.dll", "CreateThread"),
            ("sub/kernel32.dll", "CreateThread"),
            ("kernel32.dll", "CreateRemoteThread"),
            ("kernelbase.dll", "CreateThreadEx"),
            ("kernel32.dll", "#12"),
        ):
            with self.subTest(dll=dll, symbol=symbol):
                self.assertFalse(is_host_create_thread(dll, symbol))

    def test_trap_key_valid_and_invalid_inputs(self):
        self.assertEqual(trap_key("User32.dll!MessageBoxW"), ("user32.dll", "MessageBoxW"))
        self.assertEqual(trap_key("KERNEL32.DLL!#42"), ("kernel32.dll", "#42"))
        self.assertEqual(trap_key("KERNEL32.DLL!#00042"), ("kernel32.dll", "#42"))
        self.assertEqual(trap_key("shim.dll!#65535"), ("shim.dll", "#65535"))

        for bad in (
            "no_separator",
            "!Symbol",
            "user32.dll!",
            "dir/user32.dll!MessageBoxW",
            "dir\\user32.dll!MessageBoxW",
            ".!Symbol",
            "..!Symbol",
            "user32.dll!#",
            "user32.dll!#abc",
            "user32.dll!#0",
            "user32.dll!#00000",
            "user32.dll!#" + "9" * 5000,
            "user32.dll!#65536",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                trap_key(bad)

    def test_plan_refuses_empty_reachable_graph(self):
        report = {"entry": "app.exe", "reachable": {}, "imports": []}
        with self.assertRaisesRegex(ValueError, "empty"):
            plan(report)

    def test_plan_refuses_empty_resolution_chain(self):
        report = {
            "entry": "app.exe",
            "reachable": {"app.exe": {}},
            "imports": [import_row("app.exe", "shim.dll", "Fn", [])],
        }
        with self.assertRaisesRegex(ValueError, "empty resolution chain"):
            plan(report)

    def test_plan_refuses_bundled_dependency_cycle(self):
        report = {
            "entry": "app.exe",
            "reachable": {"app.exe": {}, "a.dll": {}, "b.dll": {}},
            "imports": [
                import_row("app.exe", "a.dll", "A", [pkg_step("a.dll", "A")]),
                import_row("a.dll", "b.dll", "B", [pkg_step("b.dll", "B")]),
                import_row("b.dll", "a.dll", "A", [pkg_step("a.dll", "A")]),
            ],
        }
        with self.assertRaisesRegex(ValueError, "bundled dependency cycle: a.dll, app.exe, b.dll"):
            plan(report)

    def test_plan_refuses_self_import(self):
        report = {
            "entry": "app.exe",
            "reachable": {"app.exe": {}, "a.dll": {}},
            "imports": [
                import_row("app.exe", "a.dll", "A", [pkg_step("a.dll", "A")]),
                import_row("a.dll", "a.dll", "SelfFn", [pkg_step("a.dll", "SelfFn")]),
            ],
        }
        with self.assertRaisesRegex(ValueError, "self import"):
            plan(report)

    def test_plan_refuses_entry_as_dependency(self):
        report = {
            "entry": "app.exe",
            "reachable": {"app.exe": {}, "a.dll": {}},
            "imports": [
                import_row("a.dll", "app.exe", "HostHook", [pkg_step("app.exe", "HostHook")]),
            ],
        }
        with self.assertRaisesRegex(ValueError, "entry is a dependency"):
            plan(report)

    def test_plan_refuses_conflicting_bindings(self):
        report = {
            "entry": "app.exe",
            "reachable": {"app.exe": {}, "a.dll": {}, "b.dll": {}},
            "imports": [
                import_row("app.exe", "shim.dll", "Fn", [pkg_step("a.dll", "Fn", dll="shim.dll")]),
                import_row("app.exe", "SHIM.DLL", "Fn", [pkg_step("b.dll", "Fn", dll="shim.dll")]),
            ],
        }
        with self.assertRaisesRegex(ValueError, r"conflicting bindings for shim\.dll!Fn"):
            plan(report)

    def test_plan_strict_refuses_forwarded_external_unverified(self):
        report = {
            "entry": "app.exe",
            "reachable": {"app.exe": {}, "shim.dll": {}},
            "imports": [
                import_row(
                    "app.exe",
                    "shim.dll",
                    "Fwd",
                    [pkg_step("shim.dll", "Fwd"), ext_step("host.dll", "HostFn")],
                    status="forwarded_external_unverified",
                ),
            ],
        }
        with self.assertRaisesRegex(ValueError, r"forwarded_external_unverified"):
            plan(report, allow_external=False)

    def test_cli_refusal_paths_without_tracebacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pkg = base / "package"
            pkg.mkdir()
            (pkg / "app.exe").write_bytes(build_image(POSITIVE_IMPORTS))
            mapper = base / "mapper.dll"
            mapper.write_bytes(b"")

            cases = [
                ("system slash", ["--system", "sub/kernel32.dll", "--preflight-only"], 1, "basename"),
                ("system backslash", ["--system", "sub\\kernel32.dll", "--preflight-only"], 1, "basename"),
                ("system dotdot", ["--system", "..", "--preflight-only"], 1, "basename"),
                ("pin-shim without shim", ["--pin-shim", "kernel32.dll", "--preflight-only"], 1, "--shim"),
                ("min-commit negative", ["--min-commit-mib", "-1", "--preflight-only"], 1, "--min-commit-mib"),
                ("min-commit above max", ["--min-commit-mib", "1048577", "--preflight-only"], 1, "--min-commit-mib"),
                (
                    "trap-callable with preflight-only",
                    ["--trap-callable", "kernel32.dll!ExitProcess", "--preflight-only"],
                    2,
                    "not allowed with argument",
                ),
            ]
            for label, extra_args, expected_code, expected_text in cases:
                with self.subTest(case=label):
                    proc = run_cli(str(mapper), str(pkg), "app.exe", *extra_args)
                    self.assertEqual(proc.returncode, expected_code, proc.stderr)
                    self.assertEqual(proc.stdout, "")
                    self.assertNotIn("Traceback", proc.stderr)
                    self.assertIn(expected_text, proc.stderr)


if __name__ == "__main__":
    unittest.main()
