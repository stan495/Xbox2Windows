"""Offline synthetic PE32+/DXBC/RTS0 unit and refusal suite for tools/probe_root_devices.py.

Windows-only: probe_root_devices.main() queries sys.getwindowsversion().
"""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "probe_root_devices.py"
sys.path.insert(0, str(ROOT / "tools"))
import probe_root_devices  # noqa: E402


IMAGE_BASE = 0x180000000
SECTION_RVA = 0x1000
RAW_OFFSET = 0x200
RAW_SIZE = 0x1000


def make_dxbc_container(rts0_payload: bytes | None, *, dxil_tag_seed: bytes = b"\x00\x00\x00\x00") -> bytes:
    """Build a minimal synthetic DXBC container carrying DXIL and optional RTS0 parts."""
    program = struct.pack("<II4sIII", 0, 8, b"DXIL", 0, 16, 4) + b"BC\xc0\xde" + dxil_tag_seed
    parts_list = [
        b"DXIL" + struct.pack("<I", len(program)) + program,
    ]
    if rts0_payload is not None:
        parts_list.append(b"RTS0" + struct.pack("<I", len(rts0_payload)) + rts0_payload)
    table_end = 32 + 4 * len(parts_list)
    offsets = []
    cursor = table_end
    for part in parts_list:
        offsets.append(cursor)
        cursor += len(part)
    body = b"".join(parts_list)
    total = table_end + len(body)
    return (
        b"DXBC"
        + bytes(16)
        + struct.pack("<III", 1, total, len(parts_list))
        + struct.pack(f"<{len(parts_list)}I", *offsets)
        + body
    )


def build_pe32_plus(containers: list[bytes]) -> tuple[bytes, list[tuple[int, int]]]:
    """Build a synthetic AMD64 PE32+ image embedding the given DXBC containers in .rdata."""
    payload = bytearray(RAW_SIZE)
    cursor = 0
    layout = []
    for blob in containers:
        cursor = (cursor + 15) & ~15
        if cursor + len(blob) > len(payload):
            raise AssertionError("synthetic section overflow")
        rva = SECTION_RVA + cursor
        payload[cursor:cursor + len(blob)] = blob
        cursor += len(blob)
        layout.append((rva, len(blob)))

    vsize = max(1, cursor)
    size_of_image = SECTION_RVA + ((vsize + 0xFFF) // 0x1000) * 0x1000
    pe_offset = 0x80
    buf = bytearray(RAW_OFFSET + RAW_SIZE)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 60, pe_offset)
    buf[pe_offset:pe_offset + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", buf, pe_offset + 4, 0x8664, 1, 0, 0, 0, 240, 0x2022)
    opt = pe_offset + 24
    struct.pack_into(
        "<HBBIIIIIQIIHHHHHHIIIIHHQQQQII",
        buf,
        opt,
        0x20B, 0, 0, 0, RAW_SIZE, 0, 0, SECTION_RVA, IMAGE_BASE,
        0x1000, 0x200, 6, 0, 0, 0, 6, 0, 0, size_of_image, RAW_OFFSET,
        0, 3, 0, 0, 0, 0, 0, 0, 16,
    )
    sec = opt + 240
    struct.pack_into(
        "<8sIIIIIIHHI",
        buf,
        sec,
        b".rdata\0\0", vsize, SECTION_RVA, RAW_SIZE, RAW_OFFSET,
        0, 0, 0, 0, 0x40000040,
    )
    buf[RAW_OFFSET:RAW_OFFSET + RAW_SIZE] = payload
    return bytes(buf), layout


def _setup_controls(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "compute_root.cso").write_bytes(b"CTRL_ROOT")
    (bin_dir / "compute_tables.cso").write_bytes(b"CTRL_TABLES")


def _ok_row(*, warp_only: bool) -> dict[str, object]:
    row: dict[str, object] = {
        "cleared": 0,
        "remaining_flags": 0,
        "serialize": "0x00000000",
        "warp": "0x00000000",
    }
    if not warp_only:
        row["hardware"] = "0x00000000"
    return row


class ProbeRootDevicesPositiveTests(unittest.TestCase):
    def test_measure_parses_jsonl_stdout(self):
        expected = [{"cleared": 0, "remaining_flags": 0, "serialize": "0x00000000", "warp": "0x00000000"}]
        completed = subprocess.CompletedProcess(
            args=["probe.exe"],
            returncode=0,
            stdout=(json.dumps(expected[0]) + "\n").encode("utf-8"),
        )
        with patch("probe_root_devices.subprocess.run", return_value=completed) as mock_run:
            rows = probe_root_devices.measure(["probe.exe", "--warp-only"], b"DXBC_BLOB")
        self.assertEqual(rows, expected)
        mock_run.assert_called_once_with(
            ["probe.exe", "--warp-only"],
            input=b"DXBC_BLOB",
            stdout=subprocess.PIPE,
            check=True,
            timeout=30,
        )

    def test_controls_only_preserves_warp_only_and_dual_backend(self):
        for warp_only in (False, True):
            with self.subTest(warp_only=warp_only), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                bin_dir = base / "bin"
                _setup_controls(bin_dir)
                seen_commands = []

                def fake_run(cmd, *, input, stdout, check, timeout):
                    seen_commands.append(cmd)
                    row = _ok_row(warp_only=warp_only)
                    return subprocess.CompletedProcess(cmd, 0, (json.dumps(row) + "\n").encode("utf-8"))

                argv = ["--bin", str(bin_dir)]
                if warp_only:
                    argv.append("--warp-only")
                with (
                    patch.object(probe_root_devices, "PROJECT", base / "repo"),
                    patch("probe_root_devices.subprocess.run", side_effect=fake_run),
                ):
                    rc = probe_root_devices.main(argv)
                self.assertEqual(rc, 0)
                self.assertEqual(len(seen_commands), 2)
                for cmd in seen_commands:
                    self.assertEqual(cmd[0], bin_dir / "root_device_probe.exe")
                    if warp_only:
                        self.assertEqual(cmd[1:], ["--warp-only"])
                    else:
                        self.assertEqual(cmd[1:], [])
                self.assertFalse((base / "repo" / "out" / "calls").exists())

    def test_inventory_deduplicates_rts0_and_writes_atomic_report(self):
        rts0_a = struct.pack("<6I", 2, 3, 24, 1, 0, 0x10)
        rts0_b = struct.pack("<6I", 2, 4, 24, 0, 0, 0x20)
        rts0_c = struct.pack("<6I", 2, 1, 24, 0, 0, 0x00)
        c_a1 = make_dxbc_container(rts0_a, dxil_tag_seed=b"\x01\x00\x00\x00")
        c_a2 = make_dxbc_container(rts0_a, dxil_tag_seed=b"\x02\x00\x00\x00")  # duplicate RTS0, different DXIL
        c_b = make_dxbc_container(rts0_b)
        c_c = make_dxbc_container(rts0_c)

        pe0, layout0 = build_pe32_plus([c_a1, c_a2, c_b])
        pe2, layout2 = build_pe32_plus([c_c])

        names = probe_root_devices.NAMES
        inventory = {
            names[0]: {
                "sha256": hashlib.sha256(pe0).hexdigest(),
                "containers": [
                    {"rva": hex(rva), "bytes": size} for rva, size in layout0
                ],
            },
            names[1]: {
                "sha256": "0" * 64,
                "containers": [],
            },
            names[2]: {
                "sha256": hashlib.sha256(pe2).hexdigest(),
                "containers": [
                    {"rva": hex(layout2[0][0]), "bytes": layout2[0][1]},
                ],
            },
        }

        for warp_only in (True, False):
            with self.subTest(warp_only=warp_only), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                repo = base / "repo"
                calls_dir = repo / "out" / "calls"
                calls_dir.mkdir(parents=True)
                (calls_dir / "six-apis.json").write_text(json.dumps(inventory), encoding="utf-8")

                build_dir = base / "build"
                build_dir.mkdir()
                (build_dir / names[0]).write_bytes(pe0)
                (build_dir / names[2]).write_bytes(pe2)

                bin_dir = base / "bin"
                _setup_controls(bin_dir)

                seen_blobs = []

                def fake_run(cmd, *, input, stdout, check, timeout):
                    seen_blobs.append(input)
                    row = _ok_row(warp_only=warp_only)
                    return subprocess.CompletedProcess(cmd, 0, (json.dumps(row) + "\n").encode("utf-8"))

                argv = [str(build_dir), "--bin", str(bin_dir)]
                if warp_only:
                    argv.append("--warp-only")

                out_buf = io.StringIO()
                with (
                    patch.object(probe_root_devices, "PROJECT", repo),
                    patch("probe_root_devices.subprocess.run", side_effect=fake_run),
                    redirect_stdout(out_buf),
                ):
                    rc = probe_root_devices.main(argv)

                self.assertEqual(rc, 0)
                # 2 controls + 2 distinct RTS0 in pe0 + 1 distinct RTS0 in pe2 = 5 calls (not 6!)
                self.assertEqual(len(seen_blobs), 5)

                expected_name = "root-devices-warp.json" if warp_only else "root-devices.json"
                dest = calls_dir / expected_name
                self.assertTrue(dest.is_file())
                self.assertEqual([p.name for p in calls_dir.iterdir()], sorted([expected_name, "six-apis.json"]))

                report = json.loads(dest.read_text(encoding="utf-8"))
                self.assertIn("windows", report)
                self.assertEqual(set(report["controls"].keys()), {"compute_root", "compute_tables"})
                self.assertEqual(set(report["images"].keys()), {names[0], names[2]})

                img0 = report["images"][names[0]]
                self.assertEqual(img0["sha256"], hashlib.sha256(pe0).hexdigest())
                key_a = hashlib.sha256(rts0_a).hexdigest()
                key_b = hashlib.sha256(rts0_b).hexdigest()
                self.assertEqual(set(img0["layouts"].keys()), {key_a, key_b})
                self.assertEqual(img0["layouts"][key_a]["rvas"], [hex(layout0[0][0]), hex(layout0[1][0])])
                self.assertEqual(img0["layouts"][key_b]["rvas"], [hex(layout0[2][0])])


class ProbeRootDevicesNegativeTests(unittest.TestCase):
    def _run_main(self, argv: list[str], repo: Path, fake_run=None) -> tuple[int | str | None, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        patchers = [
            patch.object(probe_root_devices, "PROJECT", repo),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ]
        if fake_run is not None:
            patchers.append(patch("probe_root_devices.subprocess.run", side_effect=fake_run))
        with patchers[0], patchers[1], patchers[2]:
            if fake_run is not None:
                with patchers[3]:
                    with self.assertRaises(SystemExit) as exc:
                        probe_root_devices.main(argv)
            else:
                with self.assertRaises(SystemExit) as exc:
                    probe_root_devices.main(argv)
        return exc.exception.code, stderr.getvalue()

    def test_failed_report_replace_preserves_previous_report_without_temp_file(self):
        def fake_ok(cmd, *, input, stdout, check, timeout):
            return subprocess.CompletedProcess(
                cmd, 0, (json.dumps(_ok_row(warp_only=True)) + "\n").encode("utf-8")
            )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            calls = repo / "out" / "calls"
            calls.mkdir(parents=True)
            inventory = {name: {"containers": []} for name in probe_root_devices.NAMES}
            (calls / "six-apis.json").write_text(json.dumps(inventory), encoding="utf-8")
            report = calls / "root-devices-warp.json"
            report.write_text("previous report", encoding="utf-8")
            build_dir = base / "build"
            build_dir.mkdir()
            bin_dir = base / "bin"
            _setup_controls(bin_dir)
            with patch.object(Path, "replace", side_effect=PermissionError("destination locked")):
                code, err = self._run_main(
                    [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo, fake_run=fake_ok
                )

            self.assertEqual(code, 1)
            self.assertIn("destination locked", err)
            self.assertEqual(report.read_text(encoding="utf-8"), "previous report")
            self.assertEqual({file.name for file in calls.iterdir()}, {"six-apis.json", report.name})

    def test_control_failures_exit_1_cleanly(self):
        bad_rows = [
            {"cleared": 1, "remaining_flags": 0, "serialize": "0x00000000", "warp": "0x00000000", "hardware": "0x00000000"},
            {"cleared": 0, "remaining_flags": 2, "serialize": "0x00000000", "warp": "0x00000000", "hardware": "0x00000000"},
            {"cleared": 0, "remaining_flags": 0, "serialize": "0x80070057", "warp": "0x00000000", "hardware": "0x00000000"},
            {"cleared": 0, "remaining_flags": 0, "serialize": "0x00000000", "warp": "0x80004005", "hardware": "0x00000000"},
            {"cleared": 0, "remaining_flags": 0, "serialize": "0x00000000", "warp": "0x00000000", "hardware": "0x80004005"},
        ]
        for row in bad_rows:
            with self.subTest(row=row), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                bin_dir = base / "bin"
                _setup_controls(bin_dir)

                def fake_run(cmd, *, input, stdout, check, timeout, payload=row):
                    return subprocess.CompletedProcess(cmd, 0, (json.dumps(payload) + "\n").encode("utf-8"))

                code, err = self._run_main(["--bin", str(bin_dir)], base / "repo", fake_run=fake_run)
                self.assertEqual(code, 1)
                self.assertNotIn("Traceback", err)
                self.assertIn("control failed", err)

    def test_missing_and_mismatched_image_sha256_exit_1_cleanly(self):
        rts0 = struct.pack("<6I", 2, 1, 24, 0, 0, 0)
        pe_bytes, layout = build_pe32_plus([make_dxbc_container(rts0)])
        names = probe_root_devices.NAMES
        inventory = {
            names[0]: {
                "sha256": hashlib.sha256(pe_bytes).hexdigest(),
                "containers": [{"rva": hex(layout[0][0]), "bytes": layout[0][1]}],
            },
            names[1]: {"sha256": "0" * 64, "containers": []},
            names[2]: {"sha256": "0" * 64, "containers": []},
        }

        def fake_ok(cmd, *, input, stdout, check, timeout):
            return subprocess.CompletedProcess(
                cmd, 0, (json.dumps(_ok_row(warp_only=True)) + "\n").encode("utf-8")
            )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            calls_dir = repo / "out" / "calls"
            calls_dir.mkdir(parents=True)
            (calls_dir / "six-apis.json").write_text(json.dumps(inventory), encoding="utf-8")
            build_dir = base / "build"
            build_dir.mkdir()
            bin_dir = base / "bin"
            _setup_controls(bin_dir)

            # Missing image on disk
            code_missing, err_missing = self._run_main(
                [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo, fake_run=fake_ok
            )
            self.assertEqual(code_missing, 1)
            self.assertNotIn("Traceback", err_missing)
            self.assertTrue(err_missing.strip())

            # Present image with mismatched SHA-256
            (build_dir / names[0]).write_bytes(pe_bytes + b"\x00")
            code_bad_sha, err_bad_sha = self._run_main(
                [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo, fake_run=fake_ok
            )
            self.assertEqual(code_bad_sha, 1)
            self.assertNotIn("Traceback", err_bad_sha)
            self.assertIn("Regenerate six-apis.json", err_bad_sha)
            self.assertFalse((calls_dir / "root-devices-warp.json").exists())

    def test_missing_and_invalid_six_apis_json_exit_1_cleanly(self):
        def fake_ok(cmd, *, input, stdout, check, timeout):
            return subprocess.CompletedProcess(
                cmd, 0, (json.dumps(_ok_row(warp_only=True)) + "\n").encode("utf-8")
            )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            calls_dir = repo / "out" / "calls"
            calls_dir.mkdir(parents=True)
            build_dir = base / "build"
            build_dir.mkdir()
            bin_dir = base / "bin"
            _setup_controls(bin_dir)

            code_missing, err_missing = self._run_main(
                [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo, fake_run=fake_ok
            )
            self.assertEqual(code_missing, 1)
            self.assertNotIn("Traceback", err_missing)
            self.assertTrue(err_missing.strip())

            (calls_dir / "six-apis.json").write_text("{not json", encoding="utf-8")
            code_bad_json, err_bad_json = self._run_main(
                [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo, fake_run=fake_ok
            )
            self.assertEqual(code_bad_json, 1)
            self.assertNotIn("Traceback", err_bad_json)
            self.assertTrue(err_bad_json.strip())

    def test_non_pe_and_subprocess_errors_exit_1_cleanly(self):
        names = probe_root_devices.NAMES
        bad_pe = b"not a PE image"
        inventory = {
            names[0]: {
                "sha256": hashlib.sha256(bad_pe).hexdigest(),
                "containers": [{"rva": "0x1000", "bytes": 64}],
            },
            names[1]: {"sha256": "0" * 64, "containers": []},
            names[2]: {"sha256": "0" * 64, "containers": []},
        }

        def fake_ok(cmd, *, input, stdout, check, timeout):
            return subprocess.CompletedProcess(
                cmd, 0, (json.dumps(_ok_row(warp_only=True)) + "\n").encode("utf-8")
            )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            calls_dir = repo / "out" / "calls"
            calls_dir.mkdir(parents=True)
            (calls_dir / "six-apis.json").write_text(json.dumps(inventory), encoding="utf-8")
            build_dir = base / "build"
            build_dir.mkdir()
            (build_dir / names[0]).write_bytes(bad_pe)
            bin_dir = base / "bin"
            _setup_controls(bin_dir)

            code_pe, err_pe = self._run_main(
                [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo, fake_run=fake_ok
            )
            self.assertEqual(code_pe, 1)
            self.assertNotIn("Traceback", err_pe)
            self.assertTrue(err_pe.strip())

            def fake_fail(cmd, *, input, stdout, check, timeout):
                raise subprocess.CalledProcessError(2, cmd)

            code_sub, err_sub = self._run_main(
                ["--warp-only", "--bin", str(bin_dir)], repo, fake_run=fake_fail
            )
            self.assertEqual(code_sub, 1)
            self.assertNotIn("Traceback", err_sub)
            self.assertTrue(err_sub.strip())

            proc = subprocess.run(
                [sys.executable, "-B", str(TOOL), "--warp-only", "--bin", str(base / "missing-bin")],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertTrue(proc.stderr.strip())

    def test_container_missing_rts0_exits_1_cleanly(self):
        names = probe_root_devices.NAMES
        pe_bytes, layout = build_pe32_plus([make_dxbc_container(None)])
        inventory = {
            names[0]: {
                "sha256": hashlib.sha256(pe_bytes).hexdigest(),
                "containers": [{"rva": hex(layout[0][0]), "bytes": layout[0][1]}],
            },
            names[1]: {"sha256": "0" * 64, "containers": []},
            names[2]: {"sha256": "0" * 64, "containers": []},
        }

        def fake_ok(cmd, *, input, stdout, check, timeout):
            return subprocess.CompletedProcess(
                cmd, 0, (json.dumps(_ok_row(warp_only=True)) + "\n").encode("utf-8")
            )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            calls_dir = repo / "out" / "calls"
            calls_dir.mkdir(parents=True)
            (calls_dir / "six-apis.json").write_text(json.dumps(inventory), encoding="utf-8")
            build_dir = base / "build"
            build_dir.mkdir()
            (build_dir / names[0]).write_bytes(pe_bytes)
            bin_dir = base / "bin"
            _setup_controls(bin_dir)

            code, err = self._run_main(
                [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo, fake_run=fake_ok
            )
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("missing RTS0", err)
            self.assertFalse((calls_dir / "root-devices-warp.json").exists())

    def test_malformed_inventory_entry_exits_1_cleanly(self):
        names = probe_root_devices.NAMES
        inventory = {names[0]: {"containers": "bad"}}

        def fake_ok(cmd, *, input, stdout, check, timeout):
            return subprocess.CompletedProcess(
                cmd, 0, (json.dumps(_ok_row(warp_only=True)) + "\n").encode("utf-8")
            )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            calls_dir = repo / "out" / "calls"
            calls_dir.mkdir(parents=True)
            (calls_dir / "six-apis.json").write_text(json.dumps(inventory), encoding="utf-8")
            build_dir = base / "build"
            build_dir.mkdir()
            bin_dir = base / "bin"
            _setup_controls(bin_dir)

            code, err = self._run_main(
                [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo, fake_run=fake_ok
            )
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("Invalid six-apis.json entry", err)

    def test_refuses_report_destination_inside_build_dir(self):
        def fake_ok(cmd, *, input, stdout, check, timeout):
            return subprocess.CompletedProcess(
                cmd, 0, (json.dumps(_ok_row(warp_only=True)) + "\n").encode("utf-8")
            )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            calls_dir = repo / "out" / "calls"
            calls_dir.mkdir(parents=True)
            bin_dir = base / "bin"
            _setup_controls(bin_dir)

            code, err = self._run_main(
                [str(repo), "--warp-only", "--bin", str(bin_dir)], repo, fake_run=fake_ok
            )
            self.assertEqual(code, 2)
            self.assertNotIn("Traceback", err)
            self.assertIn("separate from the report directory", err)


if __name__ == "__main__":
    unittest.main()
