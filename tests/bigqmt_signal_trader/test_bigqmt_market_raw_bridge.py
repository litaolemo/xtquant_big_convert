import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import (
    BigQmtMarketDataProvider,
    _market_data_answer_empty,
)


class RawMarketContext:
    def __init__(self, payload=None):
        self.payload = payload or {}
        self.calls = []

    def get_market_data_ex_ori(
        self,
        fields=None,
        stock_code=None,
        period="1d",
        start_time="",
        end_time="",
        count=-1,
        dividend_type="none",
    ):
        self.calls.append(
            {
                "fields": fields,
                "stock_code": stock_code,
                "period": period,
                "start_time": start_time,
                "end_time": end_time,
                "count": count,
                "dividend_type": dividend_type,
            }
        )
        return self.payload

    def get_market_data_ex(self, *args, **kwargs):
        raise AssertionError("DataFrame-producing QMT API must not be called")


class BigQmtRawMarketBridgeTest(unittest.TestCase):
    def test_market_data_ex_uses_raw_context_api(self):
        rows = [[1784014200000, 55.1], [1784014260000, 55.2]]
        context = RawMarketContext({"600276.SH": rows})
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=["close"], stock_list=["600276.SH"], period="1m", count=2
        )

        self.assertEqual("DataFrame", data["600276.SH"]["__bigqmt_type__"])
        self.assertEqual(["stime", "close"], data["600276.SH"]["columns"])
        self.assertEqual(rows, data["600276.SH"]["records"])
        self.assertEqual(["close"], context.calls[0]["fields"])
        self.assertEqual(["600276.SH"], context.calls[0]["stock_code"])

    def test_market_data_ex_returns_empty_frame_for_requested_symbol(self):
        context = RawMarketContext({})
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=["close"], stock_list=["600276.SH"], period="1m", count=2
        )

        self.assertEqual([], data["600276.SH"]["records"])
        self.assertEqual(["stime", "close"], data["600276.SH"]["columns"])


class _PlainFrame(object):
    """Duck-typed stand-in for the plain path's per-code DataFrame."""

    def __init__(self, rows):
        self.index = list(rows)


class PlainMarketContext(object):
    """No get_market_data_ex_ori -- the plain shape path, per-period answers."""

    def __init__(self, empty_periods=()):
        self.empty_periods = set(empty_periods)
        self.calls = []

    def get_market_data_ex(self, fields=None, stock_code=None, period="1d", **kwargs):
        self.calls.append({"fields": fields, "period": period})
        code = list(stock_code or ["000001.SZ"])[0]
        if period in self.empty_periods:
            return {code: _PlainFrame([])}
        return {code: _PlainFrame([[1, 2]])}


class SynthPeriodAllFieldsRetryTest(unittest.TestCase):
    """#219: a terminal answered 0 rows for 1mon+ when field_list=[] while the
    same bars read fine with an explicit field list. Retry once with the
    explicit K-line fields; an empty retry still means empty."""

    def test_empty_all_fields_synth_period_retries_with_explicit_fields(self):
        context = RawMarketContext({"000001.SZ": []})
        provider = BigQmtMarketDataProvider(context)
        # First call (fields=[]) empty, second (explicit) has rows: sequence.
        answers = [{"000001.SZ": []}, {"000001.SZ": [[1784014200000, 55.1]]}]
        context.payload = None
        original = context.get_market_data_ex_ori

        def sequenced(*args, **kwargs):
            context.calls.append(kwargs)
            return answers.pop(0) if answers else {"000001.SZ": []}

        context.get_market_data_ex_ori = sequenced

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["000001.SZ"], period="1mon", count=10)

        self.assertEqual(len(context.calls), 2)
        self.assertEqual(context.calls[1]["fields"],
                         list(BigQmtMarketDataProvider._KLINE_ALL_FIELDS))
        self.assertEqual(data["000001.SZ"]["records"], [[1784014200000, 55.1]])
        self.assertNotEqual(data["000001.SZ"]["columns"], [])
        context.get_market_data_ex_ori = original

    def test_empty_retry_keeps_the_original_empty_answer(self):
        context = RawMarketContext({"000001.SZ": []})
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["000001.SZ"], period="1q", count=10)

        self.assertEqual(data["000001.SZ"]["records"], [])
        self.assertEqual(len(context.calls), 2, "exactly one retry")

    def test_explicit_fields_never_retry(self):
        context = RawMarketContext({"000001.SZ": []})
        provider = BigQmtMarketDataProvider(context)

        provider.get_market_data_ex(
            field_list=["close"], stock_list=["000001.SZ"], period="1mon", count=10)

        self.assertEqual(len(context.calls), 1)

    def test_daily_period_never_retries(self):
        context = RawMarketContext({"000001.SZ": []})
        provider = BigQmtMarketDataProvider(context)

        provider.get_market_data_ex(
            field_list=[], stock_list=["000001.SZ"], period="1d", count=10)

        self.assertEqual(len(context.calls), 1)

    def test_a_nonempty_first_answer_never_retries(self):
        rows = [[1784014200000, 55.1]]
        context = RawMarketContext({"000001.SZ": rows})
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["000001.SZ"], period="1mon", count=10)

        self.assertEqual(len(context.calls), 1)
        self.assertEqual(data["000001.SZ"]["records"], rows)

    def test_plain_shape_path_retries_too(self):
        context = PlainMarketContext(empty_periods=("1mon",))
        provider = BigQmtMarketDataProvider(context)

        # fields=[] empties only when the period is empty AND fields are empty
        # on this fake; emulate "all-fields broken, explicit fine" by keying
        # on the fields actually passed.
        def by_fields(fields=None, stock_code=None, period="1d", **kwargs):
            context.calls.append({"fields": fields, "period": period})
            code = list(stock_code or ["000001.SZ"])[0]
            if period == "1mon" and not fields:
                return {code: _PlainFrame([])}
            return {code: _PlainFrame([[1, 2]])}

        context.get_market_data_ex = by_fields
        data = provider.get_market_data_ex(
            field_list=[], stock_list=["000001.SZ"], period="1mon", count=10)

        self.assertEqual(len(context.calls), 2)
        self.assertEqual(context.calls[1]["fields"],
                         list(BigQmtMarketDataProvider._KLINE_ALL_FIELDS))

    def test_trading_date_context_fallback_maps_market_to_representative_stock(self):
        """ContextInfo 路径必须把市场代码转换为其要求的证券代码。"""
        class CalendarContext:
            def __init__(self):
                self.calls = []

            def get_trading_dates(self, stock_code, start_time, end_time, count):
                self.calls.append((stock_code, start_time, end_time, count))
                return ["20260826"]

        class UnavailableNativeXtData:
            def get_trading_dates(self, *_args):
                raise RuntimeError("native quote service unavailable")

        context = CalendarContext()
        provider = BigQmtMarketDataProvider(context, native_xtdata=UnavailableNativeXtData())

        for market, expected_stock in (("SH", "000001.SH"), ("sz", "399001.SZ"), ("000300.SH", "000300.SH")):
            with self.subTest(market=market):
                self.assertEqual(["20260826"], provider.get_trading_dates(market, "20260801", "20260826", 3))
                self.assertEqual((expected_stock, "20260801", "20260826", 3), context.calls[-1])


# ---------------------------------------------------------------------------
# #237: the synthesized-period rescue.
#
# The fake below copies the SIGNATURES AND RETURN SHAPES from the terminal's
# own python/_PyContextInfo.py, because the first draft of this rescue was
# tested against an invented API and was dead code against the real one:
#
#   get_local_data(stock_code='', start_time='19700101', end_time='22010101',
#                  period='follow', divid_type='none', count=-1)
#       -- NO field list, and divid_type, not dividend_type. Every shape the
#       adapter builds for it therefore raises TypeError.
#
#   get_market_data(fields, stock_code=[], start_time='', end_time='',
#                   skip_paused=True, period='follow',
#                   dividend_type='follow', count=-1)
#       -- 1 code + count>=0  : a BARE frame (index = bar labels)
#          >1 codes + count>=0: a Panel (QMT ships pandas 0.22)
#       -- and no fill_data parameter at all.
#
# A fake that answers {code: frame} to a field-list call is the one thing this
# block must never go back to.
# ---------------------------------------------------------------------------


class _IndexedFrame(object):
    """A bare, date-indexed frame -- what get_market_data answers for 1 code.

    The bar date lives on the INDEX, not in a column, which is what makes the
    ordinary ``_frame_rows`` helper unusable here (it reads a pandas frame
    positionally, which is label lookup on a date index).
    """

    def __init__(self, index, columns):
        self.index = list(index)
        self.columns = [name for name, _values in columns]
        self._values = dict(columns)

    def __getitem__(self, name):
        return self._values[name]

    def __len__(self):
        return len(self.index)


_SIX = ("open", "high", "low", "close", "volume", "amount")
# Guojin 2.0.8.0 get_market_data_ex_ori empty 1mon: 12 keys, every list
# length 0 (peeked 2026-09-11 on 0.3.34, field_list=[]).
_ORI_EMPTY_COLUMNS = (
    "time", "stime", "open", "high", "low", "close", "volume", "amount",
    "settelementPrice", "openInterest", "preClose", "suspendFlag",
)


def _local_frame(rows, fields=None):
    """rows: [(stime, open, high, low, close, volume, amount), ...]"""
    names = [str(f) for f in (fields or _SIX)]
    index = [row[0] for row in rows]
    columns = [(name, [row[1 + _SIX.index(name)] for row in rows])
               for name in names]
    return _IndexedFrame(index, columns)


class TerminalContext(object):
    """A ContextInfo with the terminal's real signatures.

    ``broken_periods`` are the ones where everything routed through the C++
    ``context.get_market_data2`` answers zero rows -- Guojin build 2.0.8.0.
    """

    def __init__(self, bars=None, broken_periods=("1mon", "1q", "1hy", "1y"),
                 primary_rows=None, frame_for=None,
                 empty_ori_as_columns=False):
        self.broken_periods = set(broken_periods)
        self.bars = bars or []
        self.primary_rows = primary_rows or []
        self.frame_for = frame_for          # override the answered frame
        # Guojin 2.0.8.0 get_market_data_ex_ori empty shape: a 12-key dict
        # of length-0 arrays, not []. bool(that dict) is True.
        self.empty_ori_as_columns = empty_ori_as_columns
        self.ori_calls = []
        self.market_calls = []
        self.local_calls = []

    # -- exactly the terminal's signature ---------------------------------
    def get_market_data_ex_ori(self, fields=[], stock_code=[], period="follow",
                               start_time="", end_time="", count=-1,
                               dividend_type="none", fill_data=True,
                               subscribe=True):
        self.ori_calls.append({"fields": list(fields or []),
                               "stock_code": list(stock_code or []),
                               "period": period, "count": count})
        codes = list(stock_code or [])
        if period in self.broken_periods:
            if self.empty_ori_as_columns:
                empty = dict((name, []) for name in _ORI_EMPTY_COLUMNS)
                return dict((code, dict(empty)) for code in codes)
            return dict((code, []) for code in codes)
        return dict((code, list(self.primary_rows)) for code in codes)

    def get_local_data(self, stock_code="", start_time="19700101",
                       end_time="22010101", period="follow",
                       divid_type="none", count=-1):
        # Recorded only to prove it is NOT what serves the rescue: the adapter
        # never builds a shape this signature accepts.
        self.local_calls.append({"stock_code": stock_code, "period": period})
        raise AssertionError(
            "get_local_data cannot take a field list; the rescue must land on "
            "get_market_data")

    def get_market_data(self, fields, stock_code=[], start_time="", end_time="",
                        skip_paused=True, period="follow",
                        dividend_type="follow", count=-1):
        self.market_calls.append({"fields": list(fields or []),
                                  "stock_code": list(stock_code or []),
                                  "period": period, "count": count,
                                  "skip_paused": skip_paused,
                                  "dividend_type": dividend_type})
        if self.frame_for is not None:
            return self.frame_for(list(stock_code or []))
        if len(stock_code or []) > 1:
            raise AssertionError(
                "the rescue must ask one code at a time: >1 code answers a "
                "pandas.Panel here, which is not the shape it reads")
        return _local_frame(self.bars, fields)


# 600519.SH 1mon count=10 -- ten real bars, nothing padded.
_MONTHLY_10 = [
    ("20251231", 1420.0, 1470.0, 1390.0, 1455.0, 610000.0, 8.1e10),
    ("20260131", 1455.0, 1480.0, 1401.0, 1412.0, 540000.0, 7.4e10),
    ("20260228", 1412.0, 1440.5, 1360.0, 1377.0, 498000.0, 6.7e10),
    ("20260331", 1377.0, 1420.0, 1355.0, 1408.0, 522000.0, 7.0e10),
    ("20260430", 1408.0, 1466.0, 1398.0, 1450.0, 610500.0, 8.4e10),
    ("20260531", 1450.0, 1462.0, 1372.0, 1381.0, 470000.0, 6.3e10),
    ("20260630", 1381.0, 1399.0, 1310.0, 1330.0, 505000.0, 6.5e10),
    ("20260731", 1330.0, 1372.0, 1300.0, 1350.6, 500000.0, 6.6e10),
    ("20260831", 1350.6, 1363.35, 1270.33, 1299.52, 722678.0, 9.5029006404e10),
    ("20260930", 1295.0, 1338.86, 1286.1, 1290.88, 191146.0, 2.5011666655e10),
]

# 600519.SH 1y count=10 as the terminal really answers it: exactly ten rows,
# the first seven fabricated to reach count -- flat at the first real bar's
# close, zero turnover. Moutai did not trade at 1524 in 2017.
_YEARLY_PADDED_10 = [
    ("20171231", 1524.0, 1524.0, 1524.0, 1524.0, 0.0, 0.0),
    ("20181231", 1524.0, 1524.0, 1524.0, 1524.0, 0.0, 0.0),
    ("20191231", 1524.0, 1524.0, 1524.0, 1524.0, 0.0, 0.0),
    ("20201231", 1524.0, 1524.0, 1524.0, 1524.0, 0.0, 0.0),
    ("20211231", 1524.0, 1524.0, 1524.0, 1524.0, 0.0, 0.0),
    ("20221231", 1524.0, 1524.0, 1524.0, 1524.0, 0.0, 0.0),
    ("20231231", 1524.0, 1524.0, 1524.0, 1524.0, 0.0, 0.0),
    ("20241231", 1426.79, 1910.0, 1245.83, 1524.0, 3606262.0, 5.51526790141e11),
    ("20251231", 1524.0, 1657.99, 1377.17, 1377.18, 7988263.0, 1.177662933065e12),
    ("20261231", 1377.18, 1480.0, 1270.33, 1290.88, 4110000.0, 5.6e11),
]


def _partial_of(frame):
    return frame.get("__bigqmt_partial__")


class MarketDataAnswerEmptyTest(unittest.TestCase):
    """#237: 0 rows is a shape, not a Python truth value."""

    def test_envelope_with_empty_row_list_is_empty(self):
        answer = {"600519.SH": {
            "__bigqmt_type__": "DataFrame",
            "columns": ["stime", "close"],
            "records": [],
        }}
        self.assertTrue(_market_data_answer_empty(answer))

    def test_envelope_with_empty_column_dict_is_empty(self):
        records = dict((name, []) for name in _ORI_EMPTY_COLUMNS)
        answer = {"600519.SH": {
            "__bigqmt_type__": "DataFrame",
            "columns": [],
            "records": records,
        }}
        self.assertTrue(_market_data_answer_empty(answer))

    def test_envelope_with_populated_column_dict_is_not_empty(self):
        answer = {"600519.SH": {
            "__bigqmt_type__": "DataFrame",
            "columns": [],
            "records": {"time": [1, 2], "open": [10.0, 11.0]},
        }}
        self.assertFalse(_market_data_answer_empty(answer))

    def test_envelope_with_row_list_is_not_empty(self):
        answer = {"600519.SH": {
            "__bigqmt_type__": "DataFrame",
            "columns": ["stime", "close"],
            "records": [["20260930", 1290.88]],
        }}
        self.assertFalse(_market_data_answer_empty(answer))


class SynthPeriodRescueTest(unittest.TestCase):
    """#237: on Guojin 2.0.8.0 every get_market_data2 path answers 0 rows for
    1mon/1q/1hy/1y while a plain get_market_data answers the same bars. Rescue
    them -- narrowly, one code at a time, and say in the answer what is
    missing from it."""

    def _provider(self, **kwargs):
        context = TerminalContext(**kwargs)
        return context, BigQmtMarketDataProvider(context)

    # -- the blocker: does it fire against the REAL API at all? -----------
    def test_the_237_repro_is_rescued(self):
        context, provider = self._provider(bars=_MONTHLY_10)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1mon", count=10,
            dividend_type="none", fill_data=False)

        frame = data["600519.SH"]
        self.assertEqual(10, len(frame["records"]),
                         "the primary answered 0 rows; the terminal has 10")
        self.assertEqual(["stime"] + list(_SIX), frame["columns"])
        self.assertEqual("20260930", frame["records"][-1][0])
        self.assertEqual(1290.88, frame["records"][-1][4])
        # Primary tried twice (empty field_list, then the #219 11-column
        # retry) before the rescue ran.
        self.assertEqual(2, len(context.ori_calls))
        # It was get_market_data that served it -- get_local_data takes no
        # field list, so no shape the adapter builds can reach it.
        self.assertEqual(1, len(context.market_calls))
        self.assertEqual("ContextInfo.get_market_data",
                         _partial_of(frame)["source"])

    def test_ori_column_dict_of_empty_arrays_is_rescued(self):
        """The 2.0.8.0 empty answer is a 12-key dict of length-0 arrays.

        ``bool({time: [], open: [], ...})`` is True, so the old
        ``if records:`` treated 0 rows as data and never ran the rescue
        (issue #237, measured 2026-09-11 on tagged 0.3.34). ``[]`` already
        rescued; this is the shape that did not.
        """
        context, provider = self._provider(
            bars=_MONTHLY_10, empty_ori_as_columns=True)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1mon", count=10,
            dividend_type="none", fill_data=False)

        frame = data["600519.SH"]
        self.assertEqual(10, len(frame["records"]),
                         "column-dict-of-empty-arrays must count as 0 rows")
        self.assertEqual(["stime"] + list(_SIX), frame["columns"])
        self.assertEqual(1, len(context.market_calls))
        self.assertEqual("synth_period_primary_empty",
                         _partial_of(frame)["reason"])
        self.assertEqual("ContextInfo.get_market_data",
                         _partial_of(frame)["source"])

    def test_the_rescue_asks_one_code_at_a_time(self):
        """>1 code makes the terminal answer a pandas.Panel, which is not the
        shape this code reads. Asking one at a time keeps every call on the
        bare-DataFrame branch -- the fake asserts on a multi-code call."""
        context, provider = self._provider(bars=_MONTHLY_10)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH", "000001.SZ"],
            period="1mon", count=10)

        self.assertEqual([["600519.SH"], ["000001.SZ"]],
                         [call["stock_code"] for call in context.market_calls])
        for code in ("600519.SH", "000001.SZ"):
            self.assertEqual(10, len(data[code]["records"]))

    def test_a_bare_frame_is_unwrapped_not_discarded(self):
        """The regression that made the first draft dead code: the answer is a
        bare frame, and a ``not isinstance(answer, dict)`` guard threw every
        rescued row away."""
        context, provider = self._provider(bars=_MONTHLY_10)
        frame = _local_frame(_MONTHLY_10)
        context.frame_for = lambda codes: frame     # a bare frame, no dict

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1mon", count=10)

        self.assertEqual(10, len(data["600519.SH"]["records"]))

    def test_a_code_keyed_answer_still_works(self):
        """A build whose get_local_data does take a field list answers
        {code: frame}. Handled, but not relied on."""
        context, provider = self._provider(bars=_MONTHLY_10)
        context.frame_for = lambda codes: {codes[0]: _local_frame(_MONTHLY_10)}

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1mon", count=10)

        self.assertEqual(10, len(data["600519.SH"]["records"]))

    # -- narrowness -------------------------------------------------------
    def test_working_build_never_rescues(self):
        """A 2.1.19.0-shaped terminal: the primary has rows, so the rescue is
        a pure no-op -- no second read, no extra latency."""
        context, provider = self._provider(
            bars=_MONTHLY_10, broken_periods=(),
            primary_rows=[[1790697600000, 1290.88]])

        data = provider.get_market_data_ex(
            field_list=["close"], stock_list=["600519.SH"], period="1mon",
            count=10)

        self.assertEqual([[1790697600000, 1290.88]],
                         data["600519.SH"]["records"])
        self.assertEqual(0, len(context.market_calls))
        self.assertIsNone(_partial_of(data["600519.SH"]))

    def test_daily_period_never_rescues(self):
        """An empty daily/minute answer is usually truthful, and a second read
        per empty call is not free."""
        context, provider = self._provider(
            bars=_MONTHLY_10, broken_periods=("1d", "1m"))

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1d", count=10)

        self.assertEqual([], data["600519.SH"]["records"])
        self.assertEqual(0, len(context.market_calls))

    def test_explicit_six_columns_also_rescue(self):
        """The reporter's terminal answers 0 for the 6-column list too, so the
        rescue cannot be limited to the empty-field_list case."""
        context, provider = self._provider(bars=_MONTHLY_10)

        data = provider.get_market_data_ex(
            field_list=["close", "volume"], stock_list=["600519.SH"],
            period="1mon", count=10)

        frame = data["600519.SH"]
        self.assertEqual(["stime", "close", "volume"], frame["columns"])
        self.assertEqual(1290.88, frame["records"][-1][1])
        # No #219 retry for an explicit field list -- one primary call only.
        self.assertEqual(1, len(context.ori_calls))

    def test_columns_the_servant_cannot_serve_keep_the_empty_answer(self):
        """get_market_data serves 6 of the 11 columns. A caller who asked only
        for one it cannot serve gets the honest empty answer, not a frame
        missing the column they asked for."""
        context, provider = self._provider(bars=_MONTHLY_10)

        data = provider.get_market_data_ex(
            field_list=["preClose"], stock_list=["600519.SH"], period="1mon",
            count=10)

        self.assertEqual([], data["600519.SH"]["records"])
        self.assertEqual(0, len(context.market_calls))

    # -- the disclosure ---------------------------------------------------
    def test_missing_columns_are_named_in_the_marker(self):
        context, provider = self._provider(bars=_MONTHLY_10)

        data = provider.get_market_data_ex(
            field_list=["close", "preClose"], stock_list=["600519.SH"],
            period="1mon", count=10)

        frame = data["600519.SH"]
        self.assertEqual(["stime", "close"], frame["columns"])
        marker = _partial_of(frame)
        self.assertEqual(["close", "preClose"], marker["requested"])
        self.assertEqual(["close"], marker["served"])
        self.assertEqual(["preClose"], marker["missing"])
        self.assertEqual("synth_period_primary_empty", marker["reason"])
        self.assertEqual("1mon", marker["period"])

    def test_the_dropped_fill_data_is_disclosed(self):
        """ContextInfo.get_market_data has skip_paused, not fill_data, so the
        caller's fill_data never reaches the terminal. Saying so is the whole
        point of the marker -- a silently ignored argument is exactly the kind
        of quiet wrong answer this rescue must not add."""
        context, provider = self._provider(bars=_MONTHLY_10)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1mon", count=10,
            fill_data=False)

        self.assertTrue(_partial_of(data["600519.SH"])["fill_data_dropped"])
        self.assertNotIn("fill_data", context.market_calls[-1])

    # -- the padding ------------------------------------------------------
    def test_count_padding_rows_are_dropped(self):
        """Asked for 10 yearly bars the terminal answers exactly 10, seven of
        them fabricated at the first real close with zero turnover. Shipping
        those as bars would be a silent wrong answer."""
        context, provider = self._provider(bars=_YEARLY_PADDED_10)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1y", count=10)

        frame = data["600519.SH"]
        self.assertEqual(["20241231", "20251231", "20261231"],
                         [row[0] for row in frame["records"]])
        self.assertEqual(7, _partial_of(frame)["padding_rows_dropped"])

    def test_all_padding_still_means_no_data(self):
        """Nothing but padding is not a rescue -- keep the empty answer."""
        context, provider = self._provider(bars=_YEARLY_PADDED_10[:2])

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1y", count=2)

        self.assertEqual([], data["600519.SH"]["records"])

    def test_an_answer_short_of_count_is_never_trimmed(self):
        """Padding exists only to reach ``count``. An answer that fell short
        of it was never padded, so its leading zero-turnover bar is a real
        (whole-period-suspended) bar and trimming it would shorten a window
        for no reason."""
        short = [
            ("20260131", 9.87, 9.87, 9.87, 9.87, 0.0, 0.0),
            ("20260228", 9.90, 10.42, 9.71, 10.15, 811000.0, 8.1e9),
            ("20260331", 10.15, 10.88, 10.02, 10.71, 934000.0, 9.7e9),
        ]
        context, provider = self._provider(bars=short)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600000.SH"], period="1mon", count=10)

        frame = data["600000.SH"]
        self.assertEqual(["20260131", "20260228", "20260331"],
                         [row[0] for row in frame["records"]])
        self.assertEqual(0, _partial_of(frame)["padding_rows_dropped"])

    def test_a_one_price_bar_with_turnover_is_kept(self):
        limit_up = [
            ("20260131", 9.87, 9.87, 9.87, 9.87, 4400000.0, 4.3e7),
            ("20260228", 9.90, 10.42, 9.71, 10.15, 811000.0, 8.1e9),
        ]
        context, provider = self._provider(bars=limit_up)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600000.SH"], period="1mon", count=2)

        self.assertEqual(["20260131", "20260228"],
                         [row[0] for row in data["600000.SH"]["records"]])

    # -- refusals ---------------------------------------------------------
    def test_a_positional_index_is_refused_rather_than_stamped_as_time(self):
        """No usable time axis -> keep the empty answer. Bars stamped 0, 1, 2
        would be a fabricated time axis, worse than the 0 rows they replace."""
        frame = _local_frame(_MONTHLY_10)
        frame.index = list(range(len(_MONTHLY_10)))
        context, provider = self._provider(bars=_MONTHLY_10)
        context.frame_for = lambda codes: frame

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1mon", count=10)

        self.assertEqual([], data["600519.SH"]["records"])

    def test_the_servant_raising_keeps_the_empty_answer(self):
        context, provider = self._provider(bars=_MONTHLY_10)

        def boom(*_args, **_kwargs):
            raise RuntimeError("no local data service")

        context.get_market_data = boom
        context.get_local_data = boom

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1mon", count=10)

        self.assertEqual([], data["600519.SH"]["records"])


class SynthRescueOnlyParameterTest(unittest.TestCase):
    """#237's diagnostic bypass: on a terminal where the primary works the
    rescue is unreachable by a normal request, so there is no way to show it
    behaves. This skips the primary for a synthesized period."""

    def test_the_flag_skips_the_primary_and_answers_from_the_rescue(self):
        context = TerminalContext(bars=_MONTHLY_10, broken_periods=(),
                                  primary_rows=[[1790697600000, 1290.88]])
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1mon", count=10,
            synth_fallback_only=True)

        frame = data["600519.SH"]
        self.assertEqual(0, len(context.ori_calls),
                         "the primary must not run at all")
        self.assertEqual(["stime"] + list(_SIX), frame["columns"])
        self.assertEqual(10, len(frame["records"]))
        self.assertEqual("synth_rescue_only", _partial_of(frame)["reason"])

    def test_the_flag_is_ignored_for_a_daily_period(self):
        context = TerminalContext(bars=_MONTHLY_10, broken_periods=(),
                                  primary_rows=[[1790697600000, 1290.88]])
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=["close"], stock_list=["600519.SH"], period="1d",
            count=10, synth_fallback_only=True)

        self.assertEqual([[1790697600000, 1290.88]],
                         data["600519.SH"]["records"])
        self.assertEqual(1, len(context.ori_calls))
        self.assertEqual(0, len(context.market_calls))

    def test_the_flag_never_borrows_the_primary_answer(self):
        """A dead rescue must read as dead. Substituting the primary here is
        how a diagnostic reports success for a path that never ran."""
        context = TerminalContext(bars=_MONTHLY_10, broken_periods=(),
                                  primary_rows=[[1790697600000, 1290.88]])
        context.frame_for = lambda codes: None
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1mon", count=10,
            synth_fallback_only=True)

        self.assertEqual([], data["600519.SH"]["records"])
        self.assertEqual(0, len(context.ori_calls))

    def test_a_false_flag_reads_as_false(self):
        """Params arrive over JSON, where bool(\"false\") is True -- which is
        how a diagnostic switch turns itself permanently on."""
        context = TerminalContext(bars=_MONTHLY_10, broken_periods=(),
                                  primary_rows=[[1790697600000, 1290.88]])
        provider = BigQmtMarketDataProvider(context)

        for value in (False, "false", "0", "", None):
            with self.subTest(value=value):
                context.ori_calls = []
                data = provider.get_market_data_ex(
                    field_list=["close"], stock_list=["600519.SH"],
                    period="1mon", count=10, synth_fallback_only=value)
                self.assertEqual([[1790697600000, 1290.88]],
                                 data["600519.SH"]["records"])
                self.assertEqual(1, len(context.ori_calls))

    def test_the_flag_does_not_leak_into_the_terminal_call(self):
        context = TerminalContext(bars=_MONTHLY_10)
        provider = BigQmtMarketDataProvider(context)

        provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1mon", count=10,
            synth_fallback_only=True)

        self.assertNotIn("synth_fallback_only", context.market_calls[-1])


class SynthRescueRealPandasTest(unittest.TestCase):
    """The duck-typed frame above is convenient; a real pandas frame is what
    the terminal actually hands back."""

    def setUp(self):
        try:
            import pandas  # noqa: F401
        except ImportError:
            self.skipTest("pandas is not installed")

    def test_a_real_pandas_frame_is_rescued(self):
        import pandas as pd

        def frame_for(codes):
            times = [row[0] for row in _MONTHLY_10]
            values = [list(row[1:]) for row in _MONTHLY_10]
            return pd.DataFrame(values, index=times, columns=list(_SIX))

        context = TerminalContext(bars=_MONTHLY_10, frame_for=frame_for)
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1mon", count=10)

        frame = data["600519.SH"]
        self.assertEqual(10, len(frame["records"]))
        self.assertEqual("20260930", frame["records"][-1][0])
        self.assertEqual(1290.88, frame["records"][-1][4])


if __name__ == "__main__":
    unittest.main()
