"""Unit and CLI validation suite for tools/pe_inventory.py."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "pe_inventory.py"
sys.path.insert(0, str(ROOT / "tools"))
import pe_inventory  # noqa: E402


IMAGE_BASE = 0x180000000
RAW_OFFSET = 0x200
SECTION_RVA = 0x1000
SECTION_SIZE = 0x1000


def build_synthetic_pe(imports: list[tuple[str, str, str]]) -> bytes:
    """Build a minimal synthetic AMD64 PE32+ image with normal/delay imports."""
    rdata = bytearray(SECTION_SIZE)
    cursor = 0

    def alloc(size: int, align: int = 2) -> int:
        nonlocal cursor
        cursor = (cursor + align - 1) & ~(align - 1)
        rva = SECTION_RVA + cursor
        if cursor + size > len(rdata):
            raise AssertionError("synthetic .rdata overflow")
        cursor += size
        return rva

    def put(rva: int, blob: bytes) -> None:
        offset = rva - SECTION_RVA
        rdata[offset:offset + len(blob)] = blob

    module_rvas = {}
    for module in sorted({m for _, m, _ in imports}):
        raw = module.encode("ascii") + b"\0"
        rva = alloc(len(raw), 2)
        put(rva, raw)
        module_rvas[module] = rva

    hint_rvas = {}
    for kind, module, symbol in imports:
        raw = struct.pack("<H", 0) + symbol.encode("ascii") + b"\0"
        rva = alloc(len(raw), 2)
        put(rva, raw)
        hint_rvas[(kind, module, symbol)] = rva

    groups: dict[tuple[str, str], list[str]] = {}
    for kind, module, symbol in imports:
        groups.setdefault((kind, module), []).append(symbol)

    tables = {}
    for key in sorted(groups):
        values = [hint_rvas[(key[0], key[1], sym)] for sym in groups[key]]
        blob = b"".join(struct.pack("<Q", v) for v in [*values, 0])
        lookup = alloc(len(blob), 8)
        put(lookup, blob)
        iat = alloc(len(blob), 8)
        put(iat, blob)
        tables[key] = (lookup, iat)

    normal_groups = sorted(k for k in groups if k[0] == "normal")
    delay_groups = sorted(k for k in groups if k[0] == "delay")
    normal_rva = normal_size = delay_rva = delay_size = 0

    if normal_groups:
        normal_size = 20 * (len(normal_groups) + 1)
        normal_rva = alloc(normal_size, 4)
        for idx, key in enumerate(normal_groups):
            lookup, iat = tables[key]
            put(normal_rva + idx * 20, struct.pack("<5I", lookup, 0, 0, module_rvas[key[1]], iat))

    if delay_groups:
        delay_size = 32 * (len(delay_groups) + 1)
        delay_rva = alloc(delay_size, 4)
        for idx, key in enumerate(delay_groups):
            lookup, iat = tables[key]
            put(delay_rva + idx * 32, struct.pack("<8I", 1, module_rvas[key[1]], 0, iat, lookup, 0, 0, 0))

    pe_off = 0x80
    buf = bytearray(RAW_OFFSET + SECTION_SIZE)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 60, pe_off)
    buf[pe_off:pe_off + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", buf, pe_off + 4, 0x8664, 1, 0, 0, 0, 0xF0, 0x2022)
    opt = pe_off + 24
    struct.pack_into(
        "<HBBIIIIIQIIHHHHHHIIIIHHQQQQII",
        buf,
        opt,
        0x20B, 14, 0, 0, SECTION_SIZE, 0, 0, SECTION_RVA, IMAGE_BASE,
        0x1000, 0x200, 6, 0, 0, 0, 6, 0, 0, 0x2000, RAW_OFFSET,
        0, 3, 0, 0x100000, 0x1000, 0x100000, 0x1000, 0, 16,
    )
    dd = opt + 112
    if normal_groups:
        struct.pack_into("<II", buf, dd + 1 * 8, normal_rva, normal_size)
    if delay_groups:
        struct.pack_into("<II", buf, dd + 13 * 8, delay_rva, delay_size)

    sec_off = opt + 0xF0
    struct.pack_into(
        "<8sIIIIIIHHI",
        buf,
        sec_off,
        b".rdata\0\0", SECTION_SIZE, SECTION_RVA, SECTION_SIZE, RAW_OFFSET, 0, 0, 0, 0, 0x40000040,
    )
    buf[RAW_OFFSET:RAW_OFFSET + SECTION_SIZE] = rdata
    return bytes(buf)


class PeInventoryPositiveTests(unittest.TestCase):
    def test_changes_detects_all_diff_categories(self):
        old = {
            "schema": 1,
            "images": {
                "app.exe": {"sha256": "aaa"},
                "kept.dll": {"sha256": "bbb"},
                "gone.dll": {"sha256": "ccc"},
            },
            "imports": [
                {"image": "app.exe", "kind": "normal", "dll": "kernel32.dll", "symbol": "ExitProcess"},
                {"image": "gone.dll", "kind": "normal", "dll": "oldmod.dll", "symbol": "OldApi"},
            ],
        }
        new = {
            "schema": 1,
            "images": {
                "app.exe": {"sha256": "aaa-changed"},
                "kept.dll": {"sha256": "bbb"},
                "added.dll": {"sha256": "ddd"},
            },
            "imports": [
                {"image": "app.exe", "kind": "normal", "dll": "kernel32.dll", "symbol": "Sleep"},
                {"image": "added.dll", "kind": "delay", "dll": "newmod.dll", "symbol": "NewApi"},
            ],
        }
        diff = pe_inventory.changes(old, new)
        self.assertEqual(diff["added_images"], ["added.dll"])
        self.assertEqual(diff["removed_images"], ["gone.dll"])
        self.assertEqual(diff["changed_images"], ["app.exe"])
        self.assertEqual(
            diff["added_edges"],
            [
                ("added.dll", "delay", "newmod.dll", "NewApi"),
                ("app.exe", "normal", "kernel32.dll", "Sleep"),
            ],
        )
        self.assertEqual(
            diff["removed_edges"],
            [
                ("app.exe", "normal", "kernel32.dll", "ExitProcess"),
                ("gone.dll", "normal", "oldmod.dll", "OldApi"),
            ],
        )
        self.assertEqual(diff["newly_required_modules"], ["newmod.dll"])
        self.assertEqual(
            diff["newly_required_symbols"],
            [("kernel32.dll", "Sleep"), ("newmod.dll", "NewApi")],
        )

    def test_main_atomic_json_output_and_baseline_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "pkg"
            pkg.mkdir()
            out_dir = root / "reports"
            baseline_path = out_dir / "baseline.json"
            updated_path = out_dir / "updated.json"

            (pkg / "app.exe").write_bytes(build_synthetic_pe([
                ("normal", "kernel32.dll", "ExitProcess"),
            ]))
            (pkg / "removed.dll").write_bytes(build_synthetic_pe([
                ("normal", "kernel32.dll", "GetLastError"),
            ]))

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                rc = pe_inventory.main([str(pkg), "--output", str(baseline_path)])
            self.assertEqual(rc, 0)
            self.assertTrue(baseline_path.exists())
            self.assertIn("2 images | 2 import edges", stdout.getvalue())
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            self.assertEqual(baseline["schema"], 1)
            self.assertEqual(sorted(baseline["images"]), ["app.exe", "removed.dll"])
            self.assertNotIn("diff", baseline)

            # Mutate the package: change app.exe, remove removed.dll, add extra.dll.
            (pkg / "removed.dll").unlink()
            (pkg / "app.exe").write_bytes(build_synthetic_pe([
                ("normal", "kernel32.dll", "Sleep"),
            ]))
            (pkg / "extra.dll").write_bytes(build_synthetic_pe([
                ("delay", "synthetic_extra.dll", "ExtraEntry"),
            ]))
            # Pre-seed updated_path to confirm atomic replacement.
            updated_path.write_text("stale content", encoding="utf-8")

            stdout2 = io.StringIO()
            with redirect_stdout(stdout2):
                rc2 = pe_inventory.main([
                    str(pkg),
                    "--output", str(updated_path),
                    "--baseline", str(baseline_path),
                ])
            self.assertEqual(rc2, 0)
            updated = json.loads(updated_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["schema"], 1)
            self.assertEqual(sorted(updated["images"]), ["app.exe", "extra.dll"])
            self.assertEqual(updated["diff"]["added_images"], ["extra.dll"])
            self.assertEqual(updated["diff"]["removed_images"], ["removed.dll"])
            self.assertEqual(updated["diff"]["changed_images"], ["app.exe"])
            self.assertEqual(updated["diff"]["newly_required_modules"], ["synthetic_extra.dll"])
            self.assertEqual(
                updated["diff"]["newly_required_symbols"],
                [["kernel32.dll", "Sleep"], ["synthetic_extra.dll", "ExtraEntry"]],
            )
            self.assertEqual(
                sorted(p.name for p in out_dir.iterdir()),
                ["baseline.json", "updated.json"],
            )


class PeInventoryNegativeTests(unittest.TestCase):
    def _run_cli_subprocess(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(TOOL), *args],
            capture_output=True, text=True, timeout=30,
        )

    def _run_main_in_process(self, argv: list[str]) -> tuple[int | str | None, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exc:
                pe_inventory.main(argv)
        return exc.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_changes_schema_and_structure_validation_raises_value_error(self):
        base = {"schema": 1, "images": {}, "imports": []}
        other = {"schema": 2, "images": {}, "imports": []}
        with self.assertRaisesRegex(ValueError, "Inventory schema mismatch"):
            pe_inventory.changes(base, other)
        with self.assertRaisesRegex(ValueError, "Inventory schema mismatch"):
            pe_inventory.changes({}, base)
        with self.assertRaisesRegex(ValueError, "Inventory schema mismatch"):
            pe_inventory.changes([], base)
        for bad in (
            {"schema": 1, "images": [], "imports": []},
            {"schema": 1, "images": {}, "imports": {}},
            {"schema": 1},
        ):
            with self.assertRaisesRegex(ValueError, "Invalid inventory structure"):
                pe_inventory.changes(bad, base)
            with self.assertRaisesRegex(ValueError, "Invalid inventory structure"):
                pe_inventory.changes(base, bad)

    def test_missing_empty_and_malformed_input_dirs_exit_1_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_json = root / "out.json"

            # Missing directory (subprocess)
            proc_missing = self._run_cli_subprocess(str(root / "missing_dir"), "--output", str(out_json))
            self.assertEqual(proc_missing.returncode, 1, proc_missing.stderr)
            self.assertNotIn("Traceback", proc_missing.stderr)
            self.assertTrue(proc_missing.stderr.strip())
            self.assertFalse(out_json.exists())

            # Regular file instead of directory
            not_a_dir = root / "not_a_dir.txt"
            not_a_dir.write_text("file", encoding="utf-8")
            code_file, _, err_file = self._run_main_in_process([str(not_a_dir), "--output", str(out_json)])
            self.assertEqual(code_file, 1)
            self.assertNotIn("Traceback", err_file)
            self.assertIn("Not a directory:", err_file)
            self.assertFalse(out_json.exists())

            # Directory with no DLLs/EXEs
            empty_pkg = root / "empty_pkg"
            empty_pkg.mkdir()
            (empty_pkg / "readme.txt").write_text("not a pe", encoding="utf-8")
            code_empty, _, err_empty = self._run_main_in_process([str(empty_pkg), "--output", str(out_json)])
            self.assertEqual(code_empty, 1)
            self.assertNotIn("Traceback", err_empty)
            self.assertIn("No DLLs or EXEs", err_empty)
            self.assertFalse(out_json.exists())

            # Malformed PE file
            bad_pkg = root / "bad_pkg"
            bad_pkg.mkdir()
            (bad_pkg / "broken.dll").write_bytes(b"not a valid PE binary")
            proc_bad = self._run_cli_subprocess(str(bad_pkg), "--output", str(out_json))
            self.assertEqual(proc_bad.returncode, 1, proc_bad.stderr)
            self.assertNotIn("Traceback", proc_bad.stderr)
            self.assertTrue(proc_bad.stderr.strip())
            self.assertFalse(out_json.exists())

    def test_baseline_errors_exit_1_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "pkg"
            pkg.mkdir()
            (pkg / "app.exe").write_bytes(build_synthetic_pe([
                ("normal", "kernel32.dll", "ExitProcess"),
            ]))
            out_json = root / "out.json"

            # Missing --baseline
            code_miss, _, err_miss = self._run_main_in_process([
                str(pkg), "--output", str(out_json), "--baseline", str(root / "absent.json"),
            ])
            self.assertEqual(code_miss, 1)
            self.assertNotIn("Traceback", err_miss)
            self.assertFalse(out_json.exists())

            # Invalid JSON --baseline
            bad_json = root / "bad_baseline.json"
            bad_json.write_text("{not valid json", encoding="utf-8")
            code_json, _, err_json = self._run_main_in_process([
                str(pkg), "--output", str(out_json), "--baseline", str(bad_json),
            ])
            self.assertEqual(code_json, 1)
            self.assertNotIn("Traceback", err_json)
            self.assertFalse(out_json.exists())

            # Schema mismatch --baseline (subprocess)
            mismatch_json = root / "mismatch_baseline.json"
            mismatch_json.write_text(json.dumps({"schema": 999, "images": {}, "imports": []}), encoding="utf-8")
            proc_mm = self._run_cli_subprocess(
                str(pkg), "--output", str(out_json), "--baseline", str(mismatch_json),
            )
            self.assertEqual(proc_mm.returncode, 1, proc_mm.stderr)
            self.assertNotIn("Traceback", proc_mm.stderr)
            self.assertIn("Inventory schema mismatch", proc_mm.stderr)
            self.assertFalse(out_json.exists())

    def test_malformed_baseline_structure_is_refused_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "pkg"
            pkg.mkdir()
            (pkg / "app.exe").write_bytes(build_synthetic_pe([
                ("normal", "kernel32.dll", "ExitProcess"),
            ]))
            base = pe_inventory.scan(pkg)
            out_json = root / "out.json"
            image_name = next(iter(base["images"]))
            malformed = (
                dict(base, images=[]),
                dict(base, imports={}),
                dict(base, images={image_name: {}}),
                dict(base, imports=[{}]),
                dict(base, imports=[dict(base["imports"][0], symbol=[])]),
            )
            for idx, bad in enumerate(malformed):
                bad_baseline = root / f"bad_structure_{idx}.json"
                bad_baseline.write_text(json.dumps(bad), encoding="utf-8")
                proc = self._run_cli_subprocess(
                    str(pkg), "--output", str(out_json), "--baseline", str(bad_baseline),
                )
                with self.subTest(index=idx):
                    self.assertEqual(proc.returncode, 1, proc.stderr)
                    self.assertNotIn("Traceback", proc.stderr)
                    self.assertIn("Invalid inventory structure", proc.stderr)
                    self.assertFalse(out_json.exists())

    def test_output_inside_input_or_aliasing_baseline_exits_2_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "pkg"
            pkg.mkdir()
            (pkg / "app.exe").write_bytes(build_synthetic_pe([
                ("normal", "kernel32.dll", "ExitProcess"),
            ]))

            # --output inside input directory
            inside_out = pkg / "inventory.json"
            proc_inside = self._run_cli_subprocess(str(pkg), "--output", str(inside_out))
            self.assertEqual(proc_inside.returncode, 2, proc_inside.stderr)
            self.assertNotIn("Traceback", proc_inside.stderr)
            self.assertFalse(inside_out.exists())

            # --output equal to --baseline
            baseline = root / "baseline.json"
            baseline.write_text('{"schema": 1, "images": {}, "imports": []}', encoding="utf-8")
            proc_alias = self._run_cli_subprocess(
                str(pkg), "--output", str(baseline), "--baseline", str(baseline),
            )
            self.assertEqual(proc_alias.returncode, 2, proc_alias.stderr)
            self.assertNotIn("Traceback", proc_alias.stderr)
            self.assertEqual(
                baseline.read_text(encoding="utf-8"),
                '{"schema": 1, "images": {}, "imports": []}',
            )


if __name__ == "__main__":
    unittest.main()
