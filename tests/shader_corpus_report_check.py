"""Check the deterministic shader corpus report on owned framing fixtures."""
import contextlib
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
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import shader_corpus_report as report_module
from shader_corpus_report import STAGE_ORDER, DecodeResult, build_document, render_markdown


P1_CODE = struct.pack("<II", 0x11111111, 0xBF810000)
C1_CODE = struct.pack("<II", 0x22222222, 0xBF810000)
V1_CODE = struct.pack("<II", 0x33333333, 0xBF810000)
ZERO_STAGES = ("geometry", "hull", "domain", "library", "mesh", "amplification")


def dxil_program(stage, code):
    """Build an owned DXIL part with one native XBCH entry."""
    image = bytearray(256 + len(code))
    image[:4] = b"FSSM"
    struct.pack_into("<I", image, 192, 216)  # code starts 40 bytes later, at 256
    image[256:] = code
    compressed = zlib.compress(bytes(image), 9)
    xbch = b"XBCH" + bytes(12) + struct.pack("<I", len(image)) + bytes(4) + compressed
    header = struct.pack("<II4sIII", (stage << 16) | 0x60, 7, b"DXIL", 0x100, 16, 4)
    table = 64
    native = 128
    program = header + bytes(table - len(header)) + b"XDAT" + struct.pack("<I", 1) + bytes(4)
    program += struct.pack("<3I", 13, native, len(xbch))
    program += bytes(native - len(program))
    return program + xbch


def container(stage, code, marker):
    program = dxil_program(stage, code)
    total = 36 + 8 + len(program)
    return (b"DXBC" + bytes([marker]) * 16 + struct.pack("<4I", 1, total, 1, 36)
            + b"DXIL" + struct.pack("<I", len(program)) + program)


def record(blob, name):
    encoder = zlib.compressobj(wbits=-15)
    payload = encoder.compress(blob) + encoder.flush()
    return (name.encode("ascii") + b"\0\0"
            + struct.pack("<4I", len(payload), 0, len(payload), len(blob)) + payload)


def fixture_archive():
    p1 = container(0, P1_CODE, 1)
    p2 = container(0, P1_CODE, 2)
    c1 = container(5, C1_CODE, 3)
    v1 = container(1, V1_CODE, 4)
    return (record(p1, "owned_p1") + record(p1, "owned_p1_again") + record(p2, "owned_p2")
            + record(c1, "owned_c1") + record(v1, "owned_v1"))


def bad_archive():
    bad = bytearray(container(0, P1_CODE, 5))
    struct.pack_into("<I", bad, 24, len(bad) - 1)
    return fixture_archive() + record(bytes(bad), "owned_bad")


def decoder(code):
    if code == P1_CODE:
        return DecodeResult(2, 2, ("v_foo_f32_e32 v1, v2, v3",))
    return DecodeResult(2, 2)


def cache_fixture():
    entries = [(16 * index, 16) for index in range(5)]
    index_start = 128 + 0x70 + 2 * 0x18 + 3 * 8
    code_base = index_start + len(entries) * 8
    data = bytearray(code_base + 96)
    data[:4] = b"PS\x00\x09"
    struct.pack_into("<4I", data, 80, 1, 2, 3, len(entries))
    for index, (offset, size) in enumerate(entries):
        struct.pack_into("<2I", data, index_start + index * 8, offset, size)
    return bytes(data), code_base


def run_cli(archive, output):
    return subprocess.run(
        [sys.executable, "-B", str(ROOT / "tools/shader_corpus_report.py"),
         str(archive), "--output-dir", str(output)],
        check=True, capture_output=True, text=True,
    )


class CorpusReportChecks(unittest.TestCase):
    def test_positive_totals_stages_and_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "fixture.archive"
            archive.write_bytes(fixture_archive())
            document = build_document(archive, decoder=decoder)
            corpus = document["sections"]["shader_corpus"]

            self.assertEqual(document["schema"], "xbox2windows.report/1")
            self.assertEqual(document["generated_by"], {"tool": "shader_corpus_report", "version": 1})
            self.assertEqual(document["attachments"], [])
            self.assertEqual(document["inputs"]["archive"]["name"], "fixture.archive")
            self.assertEqual(document["inputs"]["archive"]["size"], archive.stat().st_size)
            self.assertEqual(document["inputs"]["archive"]["sha256"],
                             hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertEqual(document["inputs"]["decoder"], {"kind": "callback"})
            self.assertEqual(document["inputs"]["filters"], {"stages": ["all"], "limit": None})
            self.assertEqual(corpus["totals"], {
                "records": 5,
                "records_with_native": 5,
                "records_without_native": 0,
                "programs": 4,
                "streams": 3,
                "native_bytes": 24,
                "code_dwords": 6,
                "decoded_dwords": 6,
                "instructions": 6,
            })
            self.assertEqual(corpus["decode"], {"status": "complete", "streams": 3, "failures": 0})
            self.assertEqual([row["stage"] for row in corpus["stages"]], list(STAGE_ORDER))
            self.assertEqual(corpus["stages"][0], {
                "stage": "pixel", "records": 3, "programs": 2, "streams": 1,
                "native_bytes": 8, "code_dwords": 2, "decoded_dwords": 2,
                "instructions": 2, "decode_failures": 0,
            })
            self.assertEqual(corpus["stages"][5], {
                "stage": "compute", "records": 1, "programs": 1, "streams": 1,
                "native_bytes": 8, "code_dwords": 2, "decoded_dwords": 2,
                "instructions": 2, "decode_failures": 0,
            })
            self.assertEqual(corpus["stages"][1], {
                "stage": "vertex", "records": 1, "programs": 1, "streams": 1,
                "native_bytes": 8, "code_dwords": 2, "decoded_dwords": 2,
                "instructions": 2, "decode_failures": 0,
            })
            for row in corpus["stages"]:
                if row["stage"] in ZERO_STAGES:
                    self.assertEqual(row, {
                        "stage": row["stage"], "records": 0, "programs": 0, "streams": 0,
                        "native_bytes": 0, "code_dwords": 0, "decoded_dwords": 0,
                        "instructions": 0, "decode_failures": 0,
                    })
            self.assertEqual(corpus["unknown_stage_kinds"], [])
            self.assertEqual(corpus["unsupported"]["forms"], [{
                "form": "v_foo_f32_e32", "family": "v_foo_f32", "encoding": "e32",
                "stages": ["pixel"], "blocked_streams": 1, "blocked_records": 3,
                "instructions": 1,
            }])
            self.assertEqual(corpus["rejections"], [])
            self.assertEqual(corpus["first_failures"], [])
            self.assertIsNone(corpus["cache"])

            first_json = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
            second_json = json.dumps(
                build_document(archive, decoder=decoder), sort_keys=True, indent=2, ensure_ascii=True
            ) + "\n"
            self.assertEqual(first_json, second_json)
            self.assertEqual(render_markdown(document), render_markdown(build_document(archive, decoder=decoder)))
            self.assertTrue(first_json.endswith("\n"))
            self.assertFalse(first_json.endswith("\n\n"))
            self.assertIn("Decode: complete (3 streams, 0 failures)", render_markdown(document))
            for text in (first_json, render_markdown(document)):
                self.assertNotIn(str(archive.parent), text)
                self.assertNotIn(str(archive), text)
                for name in ("owned_p1", "owned_p1_again", "owned_p2", "owned_c1", "owned_v1"):
                    self.assertNotIn(name, text)

    def test_cache_population_is_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "fixture.archive"
            archive.write_bytes(fixture_archive())
            cache, code_base = cache_fixture()
            document = build_document(
                archive, decoder=decoder, cache_blob=cache, cache_name="decoded.cache", code_base=code_base
            )
            corpus = document["sections"]["shader_corpus"]
            self.assertEqual(corpus["totals"]["records"], 5)
            self.assertEqual(corpus["totals"]["streams"], 3)
            self.assertEqual(corpus["cache"], {
                "source": "decoded PS/9 cache index",
                "name": "decoded.cache",
                "size": len(cache),
                "sha256": hashlib.sha256(cache).hexdigest(),
                "code_base": hex(code_base),
                "cache_programs": 5,
                "distinct_spans": 5,
                "aliased_entries": 0,
                "program_validation": "not performed",
            })

    def test_decoder_first_failure_is_counted(self):
        def failing(code):
            if code == C1_CODE:
                return DecodeResult(2, 2, first_failure="owned decode stop")
            return DecodeResult(2, 2)

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "fixture.archive"
            archive.write_bytes(fixture_archive())
            corpus = build_document(archive, decoder=failing)["sections"]["shader_corpus"]
            self.assertEqual(corpus["decode"], {"status": "partial", "streams": 3, "failures": 1})
            self.assertEqual(corpus["first_failures"], [
                {"stage": "compute", "reason": "owned decode stop", "streams": 1}
            ])
            compute = next(row for row in corpus["stages"] if row["stage"] == "compute")
            self.assertEqual(compute["decode_failures"], 1)
            self.assertEqual(compute["decoded_dwords"], 2)

    def test_unsupported_forms_split_encoding_and_rank(self):
        def unsupported(code):
            forms = {
                P1_CODE: ("V_ADD_F32_DPP v1, v2", "v_mov_b32_sdwa v0, v1", "s_nop 0", "   "),
                C1_CODE: ("v_add_f32_dpp v3, v4", "v_cmp_eq_u32_e64 s[0:1], v0, v1",
                          "v_cmp_eq_u32_e64 s[2:3], v2, v3"),
                V1_CODE: ("v_mov_b32_sdwa v2, v3",),
            }[code]
            return DecodeResult(2, 2, forms)

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "fixture.archive"
            archive.write_bytes(fixture_archive())
            document = build_document(archive, decoder=unsupported)
            corpus = document["sections"]["shader_corpus"]
            # The pixel stream backs 3 records; compute and vertex back 1 each.
            # The mixed-case DPP form merges with the compute one, and the blank
            # entry is skipped. Rows rank by blocked streams, then instructions
            # (v_cmp has 2 in one stream, s_nop 1), then form name (v_add < v_mov).
            self.assertEqual(corpus["unsupported"]["forms"], [
                {"form": "v_add_f32_dpp", "family": "v_add_f32", "encoding": "dpp",
                 "stages": ["pixel", "compute"], "blocked_streams": 2, "blocked_records": 4,
                 "instructions": 2},
                {"form": "v_mov_b32_sdwa", "family": "v_mov_b32", "encoding": "sdwa",
                 "stages": ["pixel", "vertex"], "blocked_streams": 2, "blocked_records": 4,
                 "instructions": 2},
                {"form": "v_cmp_eq_u32_e64", "family": "v_cmp_eq_u32", "encoding": "e64",
                 "stages": ["compute"], "blocked_streams": 1, "blocked_records": 1,
                 "instructions": 2},
                {"form": "s_nop", "family": "s_nop", "encoding": "plain",
                 "stages": ["pixel"], "blocked_streams": 1, "blocked_records": 3,
                 "instructions": 1},
            ])
            self.assertIn("| 1 | `v_add_f32_dpp` | `v_add_f32` | dpp | pixel, compute | 2 | 4 | 2 |",
                          render_markdown(document))

    def test_invalid_decoder_results_are_failures(self):
        def invalid(code):
            return {
                P1_CODE: DecodeResult(-1, 2),
                C1_CODE: DecodeResult(1, 3),  # each stream holds 2 code dwords
                V1_CODE: DecodeResult(True, 2),
            }[code]

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "fixture.archive"
            archive.write_bytes(fixture_archive())
            corpus = build_document(archive, decoder=invalid)["sections"]["shader_corpus"]
            self.assertEqual(corpus["decode"], {"status": "failed", "streams": 3, "failures": 3})
            self.assertEqual(corpus["first_failures"], [
                {"stage": "pixel", "reason": "ValueError: invalid instruction count", "streams": 1},
                {"stage": "vertex", "reason": "ValueError: invalid instruction count", "streams": 1},
                {"stage": "compute", "reason": "ValueError: invalid decoded dword count", "streams": 1},
            ])
            self.assertEqual(corpus["totals"]["decoded_dwords"], 0)
            self.assertEqual(corpus["totals"]["instructions"], 0)

            def raising(code):
                if code == V1_CODE:
                    raise RuntimeError("owned callback stop")
                return DecodeResult(2, 2)

            corpus = build_document(archive, decoder=raising)["sections"]["shader_corpus"]
            self.assertEqual(corpus["decode"], {"status": "partial", "streams": 3, "failures": 1})
            self.assertEqual(corpus["first_failures"], [
                {"stage": "vertex", "reason": "RuntimeError: owned callback stop", "streams": 1}
            ])
            self.assertEqual(corpus["totals"]["instructions"], 4)

    def test_negative_control_and_empty_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.archive"
            archive.write_bytes(bad_archive())
            first = build_document(archive, decoder=decoder)
            first_corpus = first["sections"]["shader_corpus"]
            self.assertEqual(first_corpus["totals"]["records"], 5)
            self.assertEqual(first_corpus["totals"]["programs"], 4)
            self.assertEqual(first_corpus["totals"]["streams"], 3)
            self.assertEqual(first_corpus["rejections"], [{"reason": "container_framing", "count": 1}])
            first_json = json.dumps(first, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
            second_json = json.dumps(
                build_document(archive, decoder=decoder), sort_keys=True, indent=2, ensure_ascii=True
            ) + "\n"
            self.assertEqual(first_json, second_json)

            empty = root / "empty.archive"
            empty.write_bytes(b"")
            output = root / "empty-out"
            failed = subprocess.run(
                [sys.executable, "-B", str(ROOT / "tools/shader_corpus_report.py"),
                 str(empty), "--output-dir", str(output)],
                capture_output=True, text=True,
            )
            self.assertEqual(failed.returncode, 1)
            self.assertFalse((output / "report.json").exists())
            self.assertFalse((output / "report.md").exists())
            self.assertIn("no accepted records", failed.stderr)

    def test_cli_is_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "fixture.archive"
            archive.write_bytes(fixture_archive())
            first, second = root / "first", root / "second"
            run_cli(archive, first)
            run_cli(archive, second)
            self.assertEqual((first / "report.json").read_bytes(), (second / "report.json").read_bytes())
            self.assertEqual((first / "report.md").read_bytes(), (second / "report.md").read_bytes())
            report = (first / "report.json").read_text(encoding="utf-8")
            markdown = (first / "report.md").read_text(encoding="utf-8")
            self.assertNotIn(str(archive), report)
            self.assertNotIn(str(archive), markdown)
            self.assertNotIn("owned_p1", report)
            self.assertNotIn("owned_p1", markdown)
            self.assertTrue(report.endswith("\n"))
            self.assertEqual(json.loads(report)["sections"]["shader_corpus"]["decode"]["status"], "not_run")

    def test_cli_cache_flags_and_usage_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "fixture.archive"
            archive.write_bytes(fixture_archive())
            cache, code_base = cache_fixture()
            cache_path = root / "decoded.cache"
            cache_path.write_bytes(cache)
            output = root / "with-cache"
            subprocess.run(
                [sys.executable, "-B", str(ROOT / "tools/shader_corpus_report.py"), str(archive),
                 "--output-dir", str(output), "--cache-blob", str(cache_path),
                 "--code-base", hex(code_base)],
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(
                json.loads((output / "report.json").read_text(encoding="utf-8"))
                ["sections"]["shader_corpus"]["cache"]["cache_programs"], 5
            )
            usage = root / "usage"
            failed = subprocess.run(
                [sys.executable, "-B", str(ROOT / "tools/shader_corpus_report.py"), str(archive),
                 "--output-dir", str(usage), "--cache-blob", str(cache_path)],
                capture_output=True, text=True,
            )
            self.assertEqual(failed.returncode, 2)
            self.assertFalse(usage.exists())

    def test_native_extractor_failure_is_not_silent(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "fixture.archive"
            archive.write_bytes(fixture_archive())
            with patch.object(
                report_module, "extract_native_rdna2_code",
                side_effect=RuntimeError("owned extractor failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "owned extractor failure"):
                    build_document(archive, decoder=decoder)

    def test_main_reports_unexpected_failure_without_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "fixture.archive"
            archive.write_bytes(fixture_archive())
            output = root / "unexpected-out"
            stderr = io.StringIO()
            with patch.object(
                report_module, "build_document",
                side_effect=RuntimeError("owned extractor failure"),
            ):
                with contextlib.redirect_stderr(stderr):
                    code = report_module.main([str(archive), "--output-dir", str(output)])
            self.assertEqual(code, 1)
            self.assertIn(
                "shader corpus: unexpected failure: RuntimeError: owned extractor failure",
                stderr.getvalue(),
            )
            self.assertFalse((output / "report.json").exists())
            self.assertFalse((output / "report.md").exists())


if __name__ == "__main__":
    unittest.main()
