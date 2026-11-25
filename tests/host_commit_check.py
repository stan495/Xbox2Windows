"""Windows commit-budget controls with fake OS counters; no title data or Windows required."""

import ctypes
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from host_commit import MIB, PerformanceInformation, read_host_commit, require_commit_budget, snapshot_from_pages
import pe_run_package


class NativeGetPerformance:
    def __init__(self, *, limit=1000, used=600, size=65536, success=True):
        self.limit, self.used, self.size, self.success = limit, used, size, success
        self.argtypes = None
        self.restype = None
        self.seen = []

    def __call__(self, pointer, capacity):
        info = ctypes.cast(pointer, ctypes.POINTER(PerformanceInformation)).contents
        self.seen.append((info.cb, capacity))
        if self.success:
            info.CommitLimit, info.CommitTotal, info.PageSize = self.limit, self.used, self.size
        return int(self.success)


class HostCommitChecks(unittest.TestCase):
    def test_page_budget_and_zero_floor(self):
        exact = snapshot_from_pages(128, 96, MIB)
        self.assertEqual(exact, dict(limit_mib=128, used_mib=96, available_mib=32,
                                     available_bytes=32 * MIB))
        self.assertTrue(require_commit_budget(32, exact)["meets_minimum"])
        with self.assertRaisesRegex(ValueError, "below the requested 33 MiB"):
            require_commit_budget(33, exact)
        self.assertEqual(snapshot_from_pages(2, 3, MIB)["available_bytes"], 0)
        self.assertEqual(snapshot_from_pages(3, 2, 4096)["available_mib"], 0)
        # 384 and 192 pages of 4 KiB are 1.5 and 0.75 MiB: headroom is 0.75 MiB, so it
        # floors to 0 MiB, not to the 1 MiB difference of the floored limit and use.
        partial = snapshot_from_pages(384, 192, 4096)
        self.assertEqual(partial, dict(limit_mib=1, used_mib=0, available_mib=0,
                                       available_bytes=192 * 4096))
        with self.assertRaisesRegex(ValueError, "headroom 0 MiB is below the requested 1 MiB"):
            require_commit_budget(1, partial)
        for args in ((0, 0, 4096), (1, 0, 0)):
            with self.assertRaisesRegex(ValueError, "invalid commit"):
                snapshot_from_pages(*args)

    def test_boundaries_and_invalid_requests(self):
        snapshot = snapshot_from_pages(4, 1, MIB)
        self.assertEqual(require_commit_budget(0, snapshot)["minimum_mib"], 0)
        self.assertEqual(require_commit_budget(3, snapshot)["available_mib"], 3)
        for invalid in (-1, 1 << 20 | 1, "64", 1.25):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                require_commit_budget(invalid, snapshot)
        # The range check alone refuses 1 MiB past the cap, even with headroom for it.
        huge = snapshot_from_pages((1 << 20) + 1, 0, MIB)
        self.assertEqual(require_commit_budget(1 << 20, huge)["minimum_mib"], 1 << 20)
        with self.assertRaisesRegex(ValueError, "between 0 and 1048576"):
            require_commit_budget((1 << 20) + 1, huge)

    def test_native_layout_and_winapi_arguments(self):
        native = NativeGetPerformance()
        with patch("host_commit.sys.platform", "win32"), \
                patch("host_commit.C.WinDLL", create=True) as dll:
            dll.return_value.GetPerformanceInfo = native
            result = read_host_commit()
        self.assertEqual(native.seen, [(ctypes.sizeof(PerformanceInformation),
                                        ctypes.sizeof(PerformanceInformation))])
        self.assertEqual(result["limit_mib"], 62)
        self.assertEqual(result["used_mib"], 37)
        self.assertEqual(result["available_mib"], 25)
        self.assertEqual(native.argtypes, [ctypes.POINTER(PerformanceInformation), ctypes.c_uint32])
        dll.assert_called_once_with("psapi.dll", winmode=0x800, use_last_error=True)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only live PSAPI smoke")
    def test_native_windows_budget_has_valid_counts(self):
        # A harmless real-API smoke: no images or allocations are involved.
        budget = read_host_commit()
        self.assertGreater(budget["limit_mib"], 0)
        self.assertGreaterEqual(budget["used_mib"], 0)
        self.assertGreaterEqual(budget["available_mib"], 0)
        self.assertLessEqual(budget["available_mib"], budget["limit_mib"])
        self.assertTrue(require_commit_budget(0, budget)["meets_minimum"])

    def test_winapi_failure_and_unsupported_host(self):
        with patch("host_commit.sys.platform", "linux"), \
                self.assertRaisesRegex(OSError, "Windows host"):
            read_host_commit()
        native = NativeGetPerformance(success=False)
        with patch("host_commit.sys.platform", "win32"), \
                patch("host_commit.C.WinDLL", create=True) as dll, \
                patch("host_commit.C.get_last_error", create=True, return_value=1455):
            dll.return_value.GetPerformanceInfo = native
            with self.assertRaisesRegex(OSError, "GetPerformanceInfo failed") as raised:
                read_host_commit()
        self.assertEqual(raised.exception.errno, 1455)

    def test_cli_fails_before_reading_package_or_loading_guest(self):
        with patch("pe_run_package.read_host_commit", return_value=snapshot_from_pages(4, 2, MIB)), \
                patch("pe_run_package.inspect") as inspect, \
                patch("pe_run_package.run") as runner, \
                patch.object(sys, "argv", ["pe_run_package.py", "unused.dll", "missing-directory",
                                           "app.exe", "--min-commit-mib", "3"]), \
                redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit) as result:
                pe_run_package.main()
        self.assertEqual(result.exception.code, 1)
        self.assertIn("commit headroom 2 MiB", errors.getvalue())
        inspect.assert_not_called()
        runner.assert_not_called()

    def test_successful_preflight_reports_snapshot_only_when_requested(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            args = ["pe_run_package.py", str(root / "mapper.dll"), str(root), "app.exe",
                    "--preflight-only"]
            with patch("pe_run_package.inspect", return_value={"entry": "app.exe"}), \
                    patch("pe_run_package.run", return_value={"entry_executed": False}) as runner, \
                    patch("pe_run_package.read_host_commit", return_value=snapshot_from_pages(9, 2, MIB)) as read:
                with patch.object(sys, "argv", [*args, "--min-commit-mib", "7"]), \
                        redirect_stdout(io.StringIO()) as out:
                    pe_run_package.main()
                self.assertEqual(json.loads(out.getvalue())["host_commit"],
                                 {"limit_mib": 9, "used_mib": 2, "available_mib": 7,
                                  "minimum_mib": 7, "meets_minimum": True})
                self.assertTrue(runner.call_args.kwargs["preflight_only"])
                with patch.object(sys, "argv", args), redirect_stdout(io.StringIO()) as out:
                    pe_run_package.main()
                self.assertEqual(json.loads(out.getvalue()), {"entry_executed": False})
                read.assert_called_once()


if __name__ == "__main__":
    unittest.main()
