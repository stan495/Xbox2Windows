"""Unit and CLI contract controls for tools/xbox_intrinsic_gate.py."""

import contextlib
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
TOOL = ROOT / "tools" / "xbox_intrinsic_gate.py"
sys.path.insert(0, str(ROOT / "tools"))
import probe_roots  # noqa: E402
import xbox_intrinsic_gate as gate  # noqa: E402


ORDINARY_IR = "define void @main() {\n  ret void\n}\n"
DECLARED_IR = "declare i32 @xbox.V_ALIGNBYTE_B32(i32, i32, i32)\n" + ORDINARY_IR
CALLED_IR = (
    "declare i32 @xbox.V_ALIGNBYTE_B32(i32, i32, i32)\n"
    "define i32 @main(i32 %a, i32 %b) {\n"
    "  %v = call i32 @xbox.V_ALIGNBYTE_B32(i32 %a, i32 %b, i32 3)\n"
    "  ret i32 %v\n"
    "}\n"
)
DEBUG_ONLY_IR = (
    "declare void @llvm.dbg.value(metadata, i64, metadata, metadata)\n"
    "define void @main() {\n"
    "  call void @llvm.dbg.value(metadata ptr @xbox.dbg_sym, i64 0, metadata !0, metadata !1)\n"
    "  ret void\n"
    "}\n"
    "!0 = !{}\n"
    "!1 = !{}\n"
)

REFERENCE_CASES = [
    ("alias", "@alias_bfi = alias i32 (i32), ptr @xbox.BFI\n", "BFI"),
    (
        "store",
        "define void @main(ptr %slot) {\n  store ptr @xbox.StoreSym, ptr %slot\n  ret void\n}\n",
        "StoreSym",
    ),
    (
        "bitcast",
        "define void @main() {\n  %fp = bitcast ptr @xbox.BitcastSym to ptr\n  call void %fp()\n  ret void\n}\n",
        "BitcastSym",
    ),
    ("global-initializer", "@table = global ptr @xbox.Med3_F32\n", "Med3_F32"),
    (
        "call-operand",
        "declare void @consume(ptr)\ndefine void @main() {\n  call void @consume(ptr @xbox.OpSym)\n  ret void\n}\n",
        "OpSym",
    ),
]


def make_dxbc(parts_list):
    """Build a minimal synthetic DXBC container from (fourcc, payload) pairs."""
    encoded = [tag + struct.pack("<I", len(payload)) + payload for tag, payload in parts_list]
    table_end = 32 + 4 * len(encoded)
    offsets = []
    cursor = table_end
    for item in encoded:
        offsets.append(cursor)
        cursor += len(item)
    body = b"".join(encoded)
    total = table_end + len(body)
    return (b"DXBC" + bytes(16) + struct.pack("<III", 1, total, len(encoded))
            + struct.pack(f"<{len(encoded)}I", *offsets) + body)


def run_cli_inproc(argv, *, stdin_text=None):
    stdout = io.StringIO()
    stderr = io.StringIO()
    stdin = io.StringIO(stdin_text or "")
    saved_stdin = sys.stdin
    try:
        if stdin_text is not None:
            sys.stdin = stdin
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = gate.main(argv)
            except SystemExit as exc:
                code = 0 if exc.code is None else int(exc.code)
    finally:
        sys.stdin = saved_stdin
    return code, stdout.getvalue(), stderr.getvalue()


def parse_cli_decision(stdout_text):
    _, _, json_text = stdout_text.partition("\n")
    return json.loads(json_text)


class XboxIntrinsicGateChecks(unittest.TestCase):
    def test_not_checked_schema_parity_with_decide(self):
        unchecked = gate.not_checked("module-not-disassembled")
        allow = gate.check_text(ORDINARY_IR)
        reject = gate.check_text(CALLED_IR)

        self.assertEqual(set(unchecked.keys()), set(allow.keys()))
        self.assertEqual(set(unchecked.keys()), set(reject.keys()))
        self.assertEqual(unchecked["verdict"], gate.NOT_CHECKED)
        self.assertIs(unchecked["checked"], False)
        self.assertEqual(unchecked["reason"], "module-not-disassembled")
        self.assertEqual(unchecked["calls"], {})
        self.assertEqual(unchecked["declarations"], [])
        self.assertEqual(unchecked["metadata_only"], [])
        self.assertEqual(unchecked["references"], {})
        self.assertEqual(unchecked["debug_only"], [])
        self.assertEqual(unchecked["blocking"], [])
        self.assertEqual(unchecked["blocking_calls"], [])
        self.assertEqual(unchecked["blocking_references"], [])
        self.assertEqual(unchecked["reported_names"], [])

    def test_check_text_cli_file_and_stdin_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            ir_file = Path(tmp) / "allowed.ll"
            for label, content in (("ordinary", ORDINARY_IR), ("declared", DECLARED_IR), ("debug", DEBUG_ONLY_IR)):
                with self.subTest(label=label):
                    ir_file.write_text(content, encoding="utf-8")
                    proc = subprocess.run(
                        [sys.executable, "-B", str(TOOL), "check-text", str(ir_file)],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertTrue(proc.stdout.startswith("ALLOW:"), proc.stdout)
                    decision = parse_cli_decision(proc.stdout)
                    self.assertEqual(decision["verdict"], gate.ALLOW)
                    self.assertIs(decision["checked"], True)

        stdin_proc = subprocess.run(
            [sys.executable, "-B", str(TOOL), "check-text", "-"],
            input=DECLARED_IR, capture_output=True, text=True,
        )
        self.assertEqual(stdin_proc.returncode, 0, stdin_proc.stderr)
        self.assertEqual(parse_cli_decision(stdin_proc.stdout)["verdict"], gate.ALLOW)

    def test_check_container_cli_with_mocked_validate_shader_allow(self):
        dxil_payload = struct.pack("<II4sIII", 0, 8, b"DXIL", 0, 16, 4) + b"BC\xc0\xde" + bytes(4)
        blob = make_dxbc([(b"DXIL", dxil_payload), (b"NOTE", b"test")])
        fake_checks = {
            "container": {"status": "0x0"},
            "module": {"status": "0x0"},
            "ir": {
                "target_triple": "dxil-ms-dx",
                "xbox_intrinsics": {
                    "calls": {},
                    "declarations": ["xbox.unused"],
                    "metadata_only": [],
                    "references": {},
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dxbc_path = base / "shader.dxbc"
            probe_path = base / "root_probe.exe"
            dxbc_path.write_bytes(blob)
            probe_path.write_bytes(b"")
            with patch.object(probe_roots, "validate_shader", return_value=fake_checks) as mock_val:
                code, stdout, stderr = run_cli_inproc(["check", str(dxbc_path), "--probe", str(probe_path)])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        mock_val.assert_called_once_with(probe_path.resolve(), blob, dxil_payload, optional_modes=())
        decision = parse_cli_decision(stdout)
        self.assertEqual(decision["verdict"], gate.ALLOW)
        self.assertEqual(decision["declarations"], ["xbox.unused"])
        self.assertEqual(decision["validation"], fake_checks)

    def test_reject_calls_and_references_even_when_advisory_lowered(self):
        with tempfile.TemporaryDirectory() as tmp:
            ir_file = Path(tmp) / "reject.ll"
            ir_file.write_text(CALLED_IR, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-B", str(TOOL), "check-text", str(ir_file)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertTrue(proc.stdout.startswith("REJECT:"), proc.stdout)
            decision = parse_cli_decision(proc.stdout)
            self.assertEqual(decision["verdict"], gate.REJECT)
            self.assertEqual(decision["reported_names"], ["xbox.V_ALIGNBYTE_B32"])

        advisory = gate.status_table(["V_ALIGNBYTE_B32", *(sym for _, _, sym in REFERENCE_CASES)])
        call_decision = gate.check_text(CALLED_IR, advisory)
        self.assertEqual(call_decision["verdict"], gate.REJECT)
        self.assertEqual(call_decision["blocking_calls"][0]["advisory"], gate.LOWERED)
        self.assertIn("advisory reports LOWERED", call_decision["message"])

        for form, ir_text, builtin in REFERENCE_CASES:
            with self.subTest(form=form):
                stdin_proc = subprocess.run(
                    [sys.executable, "-B", str(TOOL), "check-text", "-"],
                    input=ir_text, capture_output=True, text=True,
                )
                self.assertEqual(stdin_proc.returncode, 1, stdin_proc.stderr)
                cli_dec = parse_cli_decision(stdin_proc.stdout)
                self.assertEqual(cli_dec["verdict"], gate.REJECT)
                self.assertEqual(cli_dec["blocking_references"][0]["form"], form)

                advised = gate.check_text(ir_text, advisory)
                self.assertEqual(advised["verdict"], gate.REJECT)
                self.assertEqual(advised["blocking_references"][0]["advisory"], gate.LOWERED)
                self.assertIn("advisory reports LOWERED", advised["message"])
                self.assertEqual(advised["blocking_references"][0]["builtin"], builtin)

    def test_main_returns_exit_1_for_not_checked(self):
        dxil_payload = struct.pack("<II4sIII", 0, 8, b"DXIL", 0, 16, 4) + b"BC\xc0\xde" + bytes(4)
        blob = make_dxbc([(b"DXIL", dxil_payload)])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dxbc_path = base / "shader.dxbc"
            probe_path = base / "root_probe.exe"
            dxbc_path.write_bytes(blob)
            probe_path.write_bytes(b"")
            with patch.object(gate, "check_container", return_value=gate.not_checked("module-not-disassembled")):
                code, stdout, stderr = run_cli_inproc(["check", str(dxbc_path), "--probe", str(probe_path)])
        self.assertEqual(code, 1, stderr)
        self.assertTrue(stdout.startswith("NOT_CHECKED:"), stdout)
        self.assertEqual(parse_cli_decision(stdout)["verdict"], gate.NOT_CHECKED)

    def test_cli_usage_and_input_errors_return_exit_2(self):
        no_cmd = subprocess.run([sys.executable, "-B", str(TOOL)], capture_output=True, text=True)
        self.assertEqual(no_cmd.returncode, 2)
        self.assertNotIn("Traceback", no_cmd.stderr)

        bad_cmd = subprocess.run([sys.executable, "-B", str(TOOL), "unknown"], capture_output=True, text=True)
        self.assertEqual(bad_cmd.returncode, 2)
        self.assertNotIn("Traceback", bad_cmd.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            no_dxil = base / "no_dxil.dxbc"
            no_dxil.write_bytes(make_dxbc([(b"NOTE", b"only-note")]))
            probe = base / "probe.exe"
            probe.write_bytes(b"")

            no_probe = subprocess.run(
                [sys.executable, "-B", str(TOOL), "check", str(no_dxil)],
                capture_output=True, text=True,
            )
            self.assertEqual(no_probe.returncode, 2)
            self.assertIn("check requires --probe", no_probe.stderr)
            self.assertNotIn("Traceback", no_probe.stderr)

            missing_dxil_part = subprocess.run(
                [sys.executable, "-B", str(TOOL), "check", str(no_dxil), "--probe", str(probe)],
                capture_output=True, text=True,
            )
            self.assertEqual(missing_dxil_part.returncode, 2)
            self.assertIn("no DXIL part", missing_dxil_part.stderr)
            self.assertNotIn("Traceback", missing_dxil_part.stderr)

            missing_text = subprocess.run(
                [sys.executable, "-B", str(TOOL), "check-text", str(base / "absent.ll")],
                capture_output=True, text=True,
            )
            self.assertEqual(missing_text.returncode, 2)
            self.assertIn("error:", missing_text.stderr)
            self.assertNotIn("Traceback", missing_text.stderr)

            missing_dxbc = subprocess.run(
                [sys.executable, "-B", str(TOOL), "check", str(base / "absent.dxbc"), "--probe", str(probe)],
                capture_output=True, text=True,
            )
            self.assertEqual(missing_dxbc.returncode, 2)
            self.assertIn("error:", missing_dxbc.stderr)
            self.assertNotIn("Traceback", missing_dxbc.stderr)

    def test_gate_submission_zero_call_guarantee(self):
        submitted = []

        def submit(module_text):
            submitted.append(module_text)

        advisory = gate.status_table(["V_ALIGNBYTE_B32", *(sym for _, _, sym in REFERENCE_CASES)])
        for label, decision in [
            ("call-reject", gate.check_text(CALLED_IR, advisory)),
            *((form, gate.check_text(ir_text, advisory)) for form, ir_text, _ in REFERENCE_CASES),
            ("not-checked", gate.not_checked("module-not-disassembled")),
        ]:
            with self.subTest(label=label):
                submitted.clear()
                gated = gate.gate_submission(decision, CALLED_IR, submit)
                self.assertIs(gated["submitted"], False)
                self.assertEqual(submitted, [])

        submitted.clear()
        allowed = gate.gate_submission(gate.check_text(DECLARED_IR), DECLARED_IR, submit)
        self.assertIs(allowed["submitted"], True)
        self.assertEqual(submitted, [DECLARED_IR])


if __name__ == "__main__":
    unittest.main()
