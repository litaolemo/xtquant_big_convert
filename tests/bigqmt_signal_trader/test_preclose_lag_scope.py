# coding: utf-8
"""Where the preClose lag fill may and may not act.

``278de3f`` added ``_ensure_preclose_from_lag`` so a downstream 涨停 parser
reading ``preClose`` the MiniQMT way stops hitting KeyError on old cache
files and zero rows. Its first cut acted everywhere and broke eleven tests
that pin two older decisions:

* a frame answers exactly the columns the caller named (MiniQMT shape);
  ``field_list=[]`` is the only "all fields" -- and that default DOES
  carry preClose now, as MiniQMT's does;
* ``1w`` preClose is 0 from the terminal and is backfilled from daily bars
  with the exact value (#166); the lag is wrong on an ex-dividend week
  start, and filling the zero first hid the gap from the backfill.
"""

import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import (  # noqa: E402
    DEFAULT_DOWNLOAD_FIELDS,
    PRE_CLOSE_BACKFILL_PERIODS,
    _ensure_preclose_from_lag,
)


def _frame(pre_close=None, close=(10.0, 10.5, 11.0)):
    data = {"time": [1, 2, 3], "close": list(close)}
    if pre_close is not None:
        data["preClose"] = list(pre_close)
    return pd.DataFrame(data)


class ColumnIsAddedOnlyWhenAskedTest(unittest.TestCase):
    def test_an_explicit_field_list_without_preclose_gets_no_extra_column(self):
        out = _ensure_preclose_from_lag(_frame(), field_list=["time", "close"])
        self.assertEqual(["time", "close"], list(out.columns))

    def test_an_empty_field_list_is_all_fields_and_adds_it(self):
        out = _ensure_preclose_from_lag(_frame(), field_list=[])
        self.assertIn("preClose", out.columns)
        self.assertEqual([10.0, 10.0, 10.5], list(out["preClose"]))

    def test_a_field_list_naming_preclose_adds_it(self):
        out = _ensure_preclose_from_lag(_frame(), field_list=["close", "preClose"])
        self.assertIn("preClose", out.columns)

    def test_the_default_download_fields_carry_preclose(self):
        self.assertIn("preClose", DEFAULT_DOWNLOAD_FIELDS)


class ZeroRowsTest(unittest.TestCase):
    def test_a_daily_zero_row_is_filled_from_the_lag(self):
        out = _ensure_preclose_from_lag(_frame(pre_close=[9.8, 0.0, 10.5]), period="1d")
        self.assertEqual([9.8, 10.0, 10.5], list(out["preClose"]))

    def test_a_weekly_zero_is_left_for_the_exact_backfill(self):
        """#166's backfill reads the week's first daily preClose; a lag
        written first would hide the zero from it and be wrong on an
        ex-dividend week start."""
        for period in PRE_CLOSE_BACKFILL_PERIODS:
            out = _ensure_preclose_from_lag(_frame(pre_close=[0.0, 0.0, 0.0]), period=period)
            self.assertEqual([0.0, 0.0, 0.0], list(out["preClose"]), period)

    def test_a_weekly_frame_missing_the_column_still_gets_it_when_asked(self):
        """A column must exist for the backfill to fill; the add path is
        not the zero-fill path."""
        out = _ensure_preclose_from_lag(_frame(), field_list=[], period="1w")
        self.assertIn("preClose", out.columns)

    def test_real_values_are_never_touched(self):
        out = _ensure_preclose_from_lag(_frame(pre_close=[9.8, 10.0, 10.5]), period="1d")
        self.assertEqual([9.8, 10.0, 10.5], list(out["preClose"]))

    def test_a_frame_without_close_is_returned_as_is(self):
        df = pd.DataFrame({"time": [1], "open": [1.0]})
        self.assertIs(df, _ensure_preclose_from_lag(df, field_list=[]))


if __name__ == "__main__":
    unittest.main()
