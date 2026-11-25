"""Drive the standalone audio/input alignment CLI with generated packet statistics."""

import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).resolve().parents[1] / "tools" / "startup_audio_timeline.py"


def run_timeline(rows, controls, *options):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        packets = root / "packets.csv"
        with packets.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["process_s", "frames", "rms", "peak", "flags"])
            writer.writerows(rows)
        inputs = root / "controls.jsonl"
        inputs.write_text("\n".join(json.dumps(row) for row in controls), encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-B", str(TOOL), str(packets), str(inputs),
             "--process-origin-qpc", "100", *options],
            capture_output=True, text=True,
        )


class TimelineCheck(unittest.TestCase):
    def test_clock_silence_and_missing_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packets = root / "packets.csv"
            with packets.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["process_s", "frames", "rms", "peak", "flags"])
                writer.writerows([
                    [4.0, 4800, 0.5, 0.75, 0],
                    [4.1, 4800, 0.25, 0.5, 0],
                    [5.0, 9600, 0, 0, 2],
                    [6.0, 4800, 0, 0, 1],
                    [6.1, 4800, 0, 0, 4],  # Invalid timestamp leaves a gap.
                ])
            inputs = root / "controls.jsonl"
            inputs.write_text("\n".join(json.dumps({"action": action, "qpc": at}) for action, at in [
                ("positive", 104.1),
                ("quiet", 105.1),
                ("gap", 106.1),
                ("missing", 107.1),
                ("ignored", 108.1),
            ]), encoding="utf-8")
            output = subprocess.run(
                [sys.executable, "-B", str(TOOL), str(packets), str(inputs),
                 "--process-origin-qpc", "100", "--before", "0.1", "--after", "0.1",
                 "--action", "positive", "--action", "quiet", "--action", "gap", "--action", "missing"],
                capture_output=True, text=True, check=True,
            )
            events = {row["action"]: row for row in json.loads(output.stdout)["events"]}
        self.assertEqual(set(events), {"positive", "quiet", "gap", "missing"})
        self.assertAlmostEqual(events["positive"]["coverage"], 1)
        self.assertAlmostEqual(events["positive"]["rms_estimate"], ((0.5**2 + 0.25**2) / 2)**0.5)
        self.assertEqual(events["positive"]["packet_peak"], 0.75)
        self.assertAlmostEqual(events["quiet"]["coverage"], 1)
        self.assertEqual(events["quiet"]["rms_estimate"], 0)
        self.assertAlmostEqual(events["gap"]["coverage"], 0.5)
        self.assertEqual(events["gap"]["discontinuities"], 1)
        self.assertEqual(events["missing"]["coverage"], 0)
        self.assertIsNone(events["missing"]["rms_estimate"])

    def test_unsorted_overlap_and_asymmetric_window(self):
        # At 1000 Hz, 200 frames last 0.2 s. The window is [qpc - 0.2, qpc + 0.1].
        output = run_timeline(
            [
                [3.0, 100, 0.5, 0.5, 0],  # Listed first, starts after the overlap window.
                [1.0, 200, 0, 0, 2],  # Silent, 101.0..101.2.
                [1.1, 200, 0.6, 0.9, 1],  # Discontinuity, 101.1..101.3, overlaps the previous packet.
            ],
            [{"action": "overlap", "qpc": 101.2}, {"action": "partial", "qpc": 103.2}],
            "--sample-rate", "1000", "--before", "0.2", "--after", "0.1",
            "--action", "overlap", "--action", "partial",
        )
        self.assertEqual(output.returncode, 0, output.stderr)
        events = {row["action"]: row for row in json.loads(output.stdout)["events"]}
        overlap, partial = events["overlap"], events["partial"]
        # The overlap counts once: 0.2 s of silence, then 0.1 s at 0.6 over the 0.3 s window.
        self.assertAlmostEqual(overlap["start_qpc"], 101.0)
        self.assertAlmostEqual(overlap["end_qpc"], 101.3)
        self.assertAlmostEqual(overlap["coverage"], 1)
        self.assertAlmostEqual(overlap["rms_estimate"], (0.6**2 * 0.1 / 0.3)**0.5)
        self.assertEqual(overlap["packets"], 2)
        self.assertEqual(overlap["discontinuities"], 1)
        self.assertEqual(overlap["packet_peak"], 0.9)
        # 103.0..103.1 covers a third of the window; RMS is averaged over covered time only.
        self.assertAlmostEqual(partial["coverage"], 1 / 3)
        self.assertAlmostEqual(partial["rms_estimate"], 0.5)
        self.assertEqual(partial["packets"], 1)

    def test_invalid_rows_and_options_are_refused(self):
        control = [{"action": "a", "qpc": 104.0}]
        cases = [
            ("negative packet flags", [[4.0, 4800, 0, 0, -1]], control, ()),
            ("invalid frame count", [[4.0, 0, 0, 0, 0]], control, ()),
            ("invalid frame count", [[4.0, 4800, 0.5, 0.25, 0]], control, ()),  # RMS above peak.
            ("action must be a string", [[4.0, 4800, 0, 0, 0]], [{"action": 5, "qpc": 104.0}], ("--action", "5")),
            ("total window length", [[4.0, 4800, 0, 0, 0]], control, ("--before", "0", "--after", "0")),
        ]
        for message, rows, controls, options in cases:
            with self.subTest(message=message, rows=rows, options=options):
                output = run_timeline(rows, controls, "--action", "a", *options)
                self.assertEqual(output.returncode, 2, output.stdout)
                self.assertIn(message, output.stderr)


if __name__ == "__main__":
    unittest.main()
