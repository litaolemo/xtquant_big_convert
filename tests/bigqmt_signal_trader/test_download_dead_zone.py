# coding: utf-8
"""download_history_data2 must name the append-only dead zone (#397).

Reported by @yucejade with DAT-level proof: the terminal's injected download
only appends from its local last bar, so a window ending BEFORE that bar is
silently not written -- yet the task is accepted, the poll tolerated the
empty window (the suspended/delisted tolerance), and the call returned
``{'finished': N, 'total': N}`` with zero rows written. miniQMT semantics
(start_time non-empty = full-window download) cannot be honored by the
injected function on these builds.

What the bridge CAN do honestly: after the poll, a code whose window came
back empty gets one batched tail read. Later data present -> the dead zone,
named in ``result['skipped']`` (and NOT counted in finished) with a warning.
No data at all -> the genuinely data-less codes the tolerance exists for,
unchanged.
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import pandas as pd  # noqa: E402

from bigqmt_signal_trader.xtquant_compat import (  # noqa: E402
    BigQmtXtData,
    _time_compare_digits,
)


def _frame(index):
    if not index:
        return pd.DataFrame(columns=["close"])
    return pd.DataFrame({"close": [1.0] * len(index)}, index=index)


class _Client(object):
    account_id = "acct"
    local_cache_config = {"enabled": True, "dir": None, "fallback_rpc": False}

    def call(self, method, params=None, **kwargs):
        if method == "download_history_data2":
            return True
        raise AssertionError("unexpected rpc: %s" % method)


def _xt():
    xt = BigQmtXtData.__new__(BigQmtXtData)
    xt.client = _Client()
    xt._cache_obj = None
    return xt


class DeadZoneDetectionTest(unittest.TestCase):
    def _run(self, window_rows, tail_rows):
        """Drive download_history_data2 with scripted reads.

        window_rows: code -> index labels the window poll returns (empty for
        the dead zone). tail_rows: code -> index labels for the count=1 tail.
        """
        xt = _xt()
        calls = {"window": 0, "tail": 0}

        def fake_gmde(**kwargs):
            if kwargs.get("count") == 1 and kwargs.get("start_time", "") == "":
                calls["tail"] += 1
                return {code: _frame(idx) for code, idx in tail_rows.items()}
            calls["window"] += 1
            return {code: _frame(window_rows.get(code, []))
                    for code in kwargs["stock_list"]}

        with mock.patch.object(BigQmtXtData, "get_market_data_ex", side_effect=fake_gmde), \
             mock.patch("bigqmt_signal_trader.xtquant_compat.time.sleep"):
            result = xt.download_history_data2(
                stock_list=list(window_rows), period="1d",
                start_time="20250901", end_time="20250905",
                data_wait_seconds=1.0)
        return result, calls

    def test_dead_zone_codes_are_skipped_not_finished(self):
        # Both codes empty in the window; only A has later data (dead zone).
        result, calls = self._run(
            window_rows={"600051.SH": [], "600052.SH": []},
            tail_rows={"600051.SH": ["20260930"], "600052.SH": []})

        self.assertEqual(result["finished"], 1)   # the data-less code keeps tolerance
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["skipped"], ["600051.SH"])
        self.assertEqual(calls["tail"], 1, "one batched tail read, not per-code")

    def test_full_landing_reports_no_skipped_key(self):
        result, calls = self._run(
            window_rows={"600051.SH": ["20250903"], "600052.SH": ["20250904"]},
            tail_rows={})

        self.assertEqual(result, {"finished": 2, "total": 2})
        self.assertNotIn("skipped", result)
        self.assertEqual(calls["tail"], 0, "no tail read when the window landed")

    def test_all_data_less_codes_keep_the_old_tolerance(self):
        result, calls = self._run(
            window_rows={"600051.SH": [], "600052.SH": []},
            tail_rows={"600051.SH": [], "600052.SH": []})

        self.assertEqual(result, {"finished": 2, "total": 2})
        self.assertNotIn("skipped", result)

    def test_empty_start_time_disables_detection(self):
        xt = _xt()
        out = xt._download_skipped_dead_zone(
            ["600051.SH"], "1d", "", "")
        self.assertEqual(out, [])


class TimeCompareDigitsTest(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(_time_compare_digits("20250905"), "20250905000000")
        self.assertEqual(_time_compare_digits("2025-09-05 09:30:00"), "20250905093000")
        self.assertEqual(_time_compare_digits(""), "")
        self.assertEqual(_time_compare_digits(None), "")


if __name__ == "__main__":
    unittest.main()
