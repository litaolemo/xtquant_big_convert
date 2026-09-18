# coding: utf-8
"""Where the suspendFlag shape fill may and may not act.

The synth-fallback frame (``synth_period_primary_empty``, #237) carries only
time + OHLCV + amount: the rescue servant answers 0 rows the moment
suspendFlag is mixed into the request, so the server cannot serve it, while
MiniQMT's all-fields answer does carry it. Downstream code reading
``df["suspendFlag"]`` the MiniQMT way hit KeyError, and a consumer that
concat-fills the column itself gets NaN -- which explodes on int().

``_ensure_suspend_flag_column`` follows the scope rules ``278de3f``/#318
pinned for the preClose lag fill: a frame answers exactly the columns the
caller named; ``field_list=[]`` is the only "all fields". The value is 0 --
the shape-contract default, same choice the lag fill makes for suspended /
no-data frames; the true flag for a synthesized period needs the daily
aggregate and stays a follow-up, exactly like #166 vs the lag for preClose.
"""

import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import (  # noqa: E402
    _ensure_kline_columns,
    _ensure_suspend_flag_column,
    _normalize_market_data_result,
)


def _rescue_frame(close=(68.4, 66.75, 63.09)):
    """A #237-shaped frame: time + OHLCV + amount, nothing else."""
    return pd.DataFrame(
        {
            "time": [1, 2, 3],
            "open": [68.4, 69.2, 66.35],
            "high": [68.4, 74.88, 69.1],
            "low": [68.4, 63.66, 63.06],
            "close": list(close),
            "volume": [0, 29762103, 11750558],
            "amount": [0.0, 2.013740e11, 7.789590e10],
        }
    )


class ScopeTest(unittest.TestCase):
    def test_an_explicit_field_list_without_suspendflag_gets_no_extra_column(self):
        out = _ensure_suspend_flag_column(_rescue_frame(), field_list=["time", "close"])
        self.assertNotIn("suspendFlag", out.columns)

    def test_an_empty_field_list_is_all_fields_and_adds_it(self):
        out = _ensure_suspend_flag_column(_rescue_frame(), field_list=[])
        self.assertIn("suspendFlag", out.columns)
        self.assertEqual([0, 0, 0], list(out["suspendFlag"]))

    def test_a_field_list_naming_suspendflag_adds_it(self):
        out = _ensure_suspend_flag_column(
            _rescue_frame(), field_list=["close", "suspendFlag"]
        )
        self.assertIn("suspendFlag", out.columns)

    def test_a_frame_that_already_has_it_is_untouched(self):
        frame = _rescue_frame()
        frame["suspendFlag"] = [0, 1, 0]
        out = _ensure_suspend_flag_column(frame, field_list=[])
        self.assertEqual([0, 1, 0], list(out["suspendFlag"]))


class CompositionTest(unittest.TestCase):
    def test_kline_columns_adds_both_preclose_and_suspendflag(self):
        out = _ensure_kline_columns(_rescue_frame(), field_list=[], period="1mon")
        self.assertIn("preClose", out.columns)
        self.assertIn("suspendFlag", out.columns)

    def test_normalize_result_covers_the_rescue_path(self):
        data = {"601318.SH": _rescue_frame()}
        out = _normalize_market_data_result(data, field_list=[], period="1mon")
        frame = out["601318.SH"]
        self.assertIn("preClose", frame.columns)
        self.assertIn("suspendFlag", frame.columns)
        # int-safe: a downstream int(row["suspendFlag"]) must not see NaN
        self.assertEqual(0, int(frame["suspendFlag"].iloc[0]))

    def test_normalize_result_respects_an_explicit_field_list(self):
        data = {"601318.SH": _rescue_frame()}
        out = _normalize_market_data_result(
            data, field_list=["time", "close"], period="1mon"
        )
        self.assertNotIn("suspendFlag", out["601318.SH"].columns)


if __name__ == "__main__":
    unittest.main()
