import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider


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


class _IndexedFrame(object):
    """Stand-in for the date-indexed frame ContextInfo.get_local_data returns.

    The bar date lives on the INDEX, not in a column -- that is what makes the
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


def _local_frame(rows):
    """rows: [(stime, open, high, low, close, volume, amount), ...]"""
    index = [row[0] for row in rows]
    columns = [(name, [row[1 + i] for row in rows])
               for i, name in enumerate(_SIX)]
    return _IndexedFrame(index, columns)


class SynthPeriodTerminalContext(object):
    """A Guojin 2.0.8.0-shaped terminal (#237).

    Everything that goes through the C++ ``context.get_market_data2`` answers
    ZERO rows for the synthesized periods -- empty field_list, the 6-column
    list and the 11-column #219 retry alike -- while ``get_local_data`` on the
    same process answers the same bars fine. 1w/1d are unaffected there.
    """

    def __init__(self, local_rows=None, broken_periods=("1mon", "1q", "1hy", "1y"),
                 primary_rows=None):
        self.broken_periods = set(broken_periods)
        self.local_rows = local_rows or []
        self.primary_rows = primary_rows or []
        self.ori_calls = []
        self.local_calls = []

    def get_market_data_ex_ori(self, fields=None, stock_code=None, period="1d",
                               **kwargs):
        self.ori_calls.append({"fields": fields, "period": period})
        code = list(stock_code or ["600519.SH"])[0]
        if period in self.broken_periods:
            return {code: []}
        return {code: list(self.primary_rows)}

    def get_local_data(self, fields=None, stock_code=None, period="1d", **kwargs):
        self.local_calls.append({"fields": fields, "period": period})
        code = list(stock_code or ["600519.SH"])[0]
        return {code: _local_frame(self.local_rows)}


# 600519.SH 1mon, the real values this terminal answers on the working path.
_REAL_MONTHLY = [
    ("20260731", 1330.0, 1372.0, 1300.0, 1350.6, 500000.0, 6.6e10),
    ("20260831", 1350.6, 1363.35, 1270.33, 1299.52, 722678.0, 9.5029006404e10),
    ("20260930", 1295.0, 1338.86, 1286.1, 1290.88, 191146.0, 2.5011666655e10),
]


class SynthPeriodLocalDataFallbackTest(unittest.TestCase):
    """#237: on Guojin 2.0.8.0 every get_market_data2 path answers 0 rows for
    1mon/1q/1hy/1y while get_local_data answers the same bars. Fall back --
    narrowly, and only to the columns get_local_data actually serves."""

    def test_broken_build_falls_back_to_get_local_data(self):
        context = SynthPeriodTerminalContext(local_rows=_REAL_MONTHLY)
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1mon", count=10,
            dividend_type="none", fill_data=False)

        frame = data["600519.SH"]
        self.assertEqual(3, len(frame["records"]),
                         "the primary answered 0 rows; get_local_data has 3")
        self.assertEqual(["stime"] + list(_SIX), frame["columns"])
        self.assertEqual(
            ["20260731", "20260831", "20260930"],
            [row[0] for row in frame["records"]])
        self.assertEqual(1290.88, frame["records"][-1][4])
        # Primary tried twice (empty field_list, then the #219 11-column
        # retry) before the fallback ran.
        self.assertEqual(2, len(context.ori_calls))
        self.assertEqual(1, len(context.local_calls))

    def test_working_build_never_calls_get_local_data(self):
        """A 2.1.19.0-shaped terminal: the primary has rows, so the fallback
        must be a pure no-op -- no second RPC, no extra latency."""
        context = SynthPeriodTerminalContext(
            broken_periods=(), primary_rows=[[1790697600000, 1290.88]])
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=["close"], stock_list=["600519.SH"], period="1mon",
            count=10)

        self.assertEqual([[1790697600000, 1290.88]],
                         data["600519.SH"]["records"])
        self.assertEqual(0, len(context.local_calls))

    def test_daily_period_never_falls_back(self):
        """Narrowness: an empty daily/minute answer is usually truthful, and a
        second RPC per empty call is not free."""
        context = SynthPeriodTerminalContext(
            local_rows=_REAL_MONTHLY, broken_periods=("1d", "1m"))
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1d", count=10)

        self.assertEqual([], data["600519.SH"]["records"])
        self.assertEqual(0, len(context.local_calls))

    def test_explicit_six_columns_also_fall_back(self):
        """The reporter's terminal answers 0 for the 6-column list too, so the
        rescue cannot be limited to the empty-field_list case."""
        context = SynthPeriodTerminalContext(local_rows=_REAL_MONTHLY)
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=["close", "volume"], stock_list=["600519.SH"],
            period="1mon", count=10)

        frame = data["600519.SH"]
        self.assertEqual(["stime", "close", "volume"], frame["columns"])
        self.assertEqual([1350.6, 1299.52, 1290.88],
                         [row[1] for row in frame["records"]])
        # No #219 retry for an explicit field list -- one primary call only.
        self.assertEqual(1, len(context.ori_calls))

    def test_columns_get_local_data_cannot_serve_keep_the_empty_answer(self):
        """get_local_data serves 6 of the 11 columns. A caller who asked only
        for one it cannot serve gets the honest empty answer, not a frame
        missing the column they asked for."""
        context = SynthPeriodTerminalContext(local_rows=_REAL_MONTHLY)
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=["preClose"], stock_list=["600519.SH"], period="1mon",
            count=10)

        self.assertEqual([], data["600519.SH"]["records"])
        self.assertEqual(0, len(context.local_calls))

    def test_count_padding_rows_are_dropped(self):
        """get_local_data pads the HEAD to reach `count` with the first real
        bar's price in all four OHLC slots and zero volume/amount. Measured on
        600519.SH 1y count=10: seven fabricated rows stamped 20171231..20231231
        at 1524.0 flat. Shipping those as bars would be a silent wrong answer;
        dropping them is what makes the rescued answer identical to what a
        working terminal returns."""
        padded = [
            ("20171231", 1524.0, 1524.0, 1524.0, 1524.0, 0.0, 0.0),
            ("20181231", 1524.0, 1524.0, 1524.0, 1524.0, 0.0, 0.0),
            ("20241231", 1426.79, 1910.0, 1245.83, 1524.0, 3606262.0, 5.51526790141e11),
            ("20251231", 1524.0, 1657.99, 1377.17, 1377.18, 7988263.0, 1.177662933065e12),
        ]
        context = SynthPeriodTerminalContext(local_rows=padded)
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1y", count=10)

        records = data["600519.SH"]["records"]
        self.assertEqual(["20241231", "20251231"], [row[0] for row in records])

    def test_all_padding_still_means_no_data(self):
        """Nothing but padding is not a rescue -- keep the empty answer."""
        padding_only = [
            ("20171231", 1524.0, 1524.0, 1524.0, 1524.0, 0.0, 0.0),
            ("20181231", 1524.0, 1524.0, 1524.0, 1524.0, 0.0, 0.0),
        ]
        context = SynthPeriodTerminalContext(local_rows=padding_only)
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1y", count=10)

        self.assertEqual([], data["600519.SH"]["records"])

    def test_a_positional_index_is_refused_rather_than_stamped_as_time(self):
        """No usable time axis -> keep the empty answer. Bars stamped "0", "1"
        would be a fabricated time axis, worse than the 0 rows they replace."""
        context = SynthPeriodTerminalContext(local_rows=_REAL_MONTHLY)
        frame = _local_frame(_REAL_MONTHLY)
        frame.index = list(range(len(_REAL_MONTHLY)))

        def positional(fields=None, stock_code=None, period="1d", **kwargs):
            context.local_calls.append({"fields": fields, "period": period})
            return {list(stock_code or ["600519.SH"])[0]: frame}

        context.get_local_data = positional
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1mon", count=10)

        self.assertEqual([], data["600519.SH"]["records"])

    def test_get_local_data_raising_keeps_the_empty_answer(self):
        context = SynthPeriodTerminalContext(local_rows=_REAL_MONTHLY)

        def boom(*_args, **_kwargs):
            raise RuntimeError("no local data service")

        context.get_local_data = boom
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=[], stock_list=["600519.SH"], period="1mon", count=10)

        self.assertEqual([], data["600519.SH"]["records"])


if __name__ == "__main__":
    unittest.main()
