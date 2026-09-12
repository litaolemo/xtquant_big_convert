# coding: utf-8
"""xtdata.get_divid_factors returns a DataFrame; the bridge returned the wire dict.

Measured on a live miniQMT, ``df.info()`` of the real xtdata answer is::

    Index: 11 entries, 19990823 to 20080707
    time, interest, stockBonus, stockGift, allotNum, allotPrice, gugai, dr
    dtypes: float64(8)

One row per ex-dividend day indexed by YYYYMMDD (``str``, checked with
``type(df.index[0])``), a ``time`` column carrying the day's ms timestamp,
the seven 除权数据 columns, everything float64. The
bridge's client passed the RPC answer through untouched, and the RPC answer
is big QMT's native shape:

    dict{毫秒时间戳: [每股红利, 每股送转, 每转赠, 配股, 配股价, 是否股改, 复权系数]}

Same seven values, same order, no names, and the day is an ms timestamp
rather than a date. A caller written against the real xtdata -- ``df["dr"]``,
``df.tail()``, ``df.loc["20260626"]`` -- got a KeyError or an AttributeError.
The wire stays as it is (JSON cannot carry a frame, and the raw-RPC alias
``getDividFactors`` keeps answering the dict); the client adds the day index,
the ``time`` column, the names and the dtype, the way ``get_market_data_ex``
already turns wire records into frames.

The ms keys are Shanghai midnight (all three fixtures satisfy
``(ms/1000 + 8h) % 86400 == 0``), so YYYYMMDD is ``ms + 8h`` by day, with a
fixed offset rather than the client machine's zone.

The fixtures are real answers from a Guojin 2.1.19.0 terminal on 2026-09-12:
600519.SH's last two ex-dividend days (cash dividends) and 000001.SZ's 2000
rights issue (allotNum 0.3 at 8.0), which is the row that proves the column
order -- the non-zero values land in allotNum and allotPrice, not in interest.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import (  # noqa: E402
    DIVID_FACTOR_COLUMNS,
    DIVID_FRAME_COLUMNS,
    BigQmtXtData,
    _divid_day_key,
    _divid_factors_frame,
)

FACTORS = ["interest", "stockBonus", "stockGift", "allotNum", "allotPrice", "gugai", "dr"]
COLUMNS = ["time"] + FACTORS

MOUTAI = {
    "1750867200000": [27.673, 0.0, 0.0, 0.0, 0.0, 0, 1.019649],
    "1782403200000": [28.02423, 0.0, 0.0, 0.0, 0.0, 0, 1.023663],
}
PINGAN_RIGHTS_ISSUE = {"973440000000": [0.0, 0.0, 0.0, 0.3, 8.0, 0, 1.14489]}


class _Client(object):
    def __init__(self, answer):
        self.account_id = "acct"
        self.answer = answer
        self.calls = []

    def call(self, method, params=None, **kw):
        self.calls.append((method, dict(params or {})))
        return self.answer


class ColumnContractTest(unittest.TestCase):
    def test_the_factor_columns_are_the_documented_seven_in_order(self):
        self.assertEqual(FACTORS, list(DIVID_FACTOR_COLUMNS))

    def test_the_frame_is_time_plus_the_seven(self):
        """The measured xtdata frame has 8 columns with ``time`` first."""
        self.assertEqual(COLUMNS, list(DIVID_FRAME_COLUMNS))


class DayKeyTest(unittest.TestCase):
    def test_a_shanghai_midnight_ms_key_becomes_that_day(self):
        self.assertEqual("20001106", _divid_day_key("973440000000"))
        self.assertEqual("20250626", _divid_day_key(1750867200000))
        self.assertEqual("20260626", _divid_day_key("1782403200000"))

    def test_the_offset_is_fixed_not_the_machines_zone(self):
        """16:00 UTC is already the next day in Shanghai; a UTC- or US-zoned
        client must still label the row with the Shanghai date."""
        self.assertEqual("20260626", _divid_day_key(1782403200000))

    def test_an_already_yyyymmdd_key_passes_through(self):
        self.assertEqual("20260626", _divid_day_key("20260626"))

    def test_a_non_numeric_key_is_left_alone(self):
        self.assertEqual("weird", _divid_day_key("weird"))


class FrameShapeTest(unittest.TestCase):
    def test_it_matches_the_measured_xtdata_frame(self):
        """Index YYYYMMDD, 8 columns with ``time`` first, all float64 --
        the ``df.info()`` a live miniQMT prints."""
        df = _divid_factors_frame(MOUTAI)
        import pandas as pd

        self.assertIsInstance(df, pd.DataFrame)
        self.assertEqual((2, 8), df.shape)
        self.assertEqual(["20250626", "20260626"], list(df.index))
        self.assertEqual(COLUMNS, list(df.columns))
        self.assertEqual({"float64"}, set(str(t) for t in df.dtypes))

    def test_the_index_is_str_not_int(self):
        """``type(df.index[0])`` on a live miniQMT is ``str``. An int index
        would make ``df.loc["20260626"]`` -- the way every QMT date is
        spelled -- a KeyError."""
        df = _divid_factors_frame(MOUTAI)

        self.assertIs(str, type(df.index[0]))
        self.assertAlmostEqual(1.023663, df.loc["20260626", "dr"])

    def test_time_carries_the_ms_timestamp(self):
        df = _divid_factors_frame(MOUTAI)

        self.assertEqual(1782403200000.0, df.loc["20260626", "time"])

    def test_gugai_is_float_like_the_official_frame(self):
        """The wire sends 0 as an int; the official frame is float64(8)."""
        df = _divid_factors_frame(MOUTAI)

        self.assertEqual("float64", str(df["gugai"].dtype))

    def test_columns_are_reachable_by_name(self):
        """The whole point: ``df["dr"]`` used to be a KeyError on a dict."""
        df = _divid_factors_frame(MOUTAI)

        self.assertAlmostEqual(1.023663, df["dr"].iloc[-1])
        self.assertAlmostEqual(28.02423, df["interest"].iloc[-1])

    def test_positions_map_to_the_right_names(self):
        """A rights issue: the two non-zero values must land in allotNum and
        allotPrice, which pins the positional order against the names."""
        df = _divid_factors_frame(PINGAN_RIGHTS_ISSUE)
        row = df.iloc[0]

        self.assertEqual(0.0, row["interest"])
        self.assertAlmostEqual(0.3, row["allotNum"])
        self.assertAlmostEqual(8.0, row["allotPrice"])
        self.assertAlmostEqual(1.14489, row["dr"])

    def test_row_order_is_the_servers(self):
        """No sorting: the official leaves dict order alone."""
        data = dict(PINGAN_RIGHTS_ISSUE)
        data.update(MOUTAI)
        df = _divid_factors_frame(data)

        self.assertEqual(["20001106", "20250626", "20260626"], list(df.index))

    def test_an_empty_answer_is_an_empty_frame_with_the_columns(self):
        df = _divid_factors_frame({})

        self.assertEqual((0, 8), df.shape)
        self.assertEqual(COLUMNS, list(df.columns))

    def test_a_named_dict_value_is_read_by_name(self):
        """If a server ever sends named fields, positions are not guessed,
        and an explicit ``time`` wins over the key."""
        df = _divid_factors_frame({"20260626": {"dr": 1.5, "interest": 2.0, "time": 7.0}})

        self.assertAlmostEqual(1.5, df["dr"].iloc[0])
        self.assertAlmostEqual(2.0, df["interest"].iloc[0])
        self.assertAlmostEqual(7.0, df["time"].iloc[0])
        self.assertTrue(df["allotNum"].isna().iloc[0])

    def test_a_short_list_is_padded_not_dropped(self):
        df = _divid_factors_frame({"1782403200000": [0.5, 0.0]})

        self.assertAlmostEqual(0.5, df["interest"].iloc[0])
        self.assertTrue(df["dr"].isna().iloc[0])

    def test_a_non_dict_answer_passes_through(self):
        """An error envelope must not become an empty frame."""
        self.assertIsNone(_divid_factors_frame(None))
        self.assertEqual("boom", _divid_factors_frame("boom"))


class ClientMethodTest(unittest.TestCase):
    def test_get_divid_factors_returns_the_frame(self):
        client = _Client(MOUTAI)
        xt = BigQmtXtData(client)

        df = xt.get_divid_factors("600519.SH", "20240101", "20260930")

        import pandas as pd
        self.assertIsInstance(df, pd.DataFrame)
        self.assertEqual(COLUMNS, list(df.columns))
        self.assertEqual([("get_divid_factors", {
            "stock_code": "600519.SH", "start_time": "20240101", "end_time": "20260930",
        })], client.calls)

    def test_the_wire_shape_is_unchanged(self):
        """Only the client-side container changes; the RPC still carries the
        dict, so raw-RPC callers and the getDividFactors alias see no change."""
        client = _Client(MOUTAI)
        BigQmtXtData(client).get_divid_factors("600519.SH")

        self.assertIs(MOUTAI, client.answer)


if __name__ == "__main__":
    unittest.main()
