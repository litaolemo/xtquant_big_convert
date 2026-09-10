"""issue #48 (order_time missing) and issue #47 (get_market_data_ex chunking).

Both are gaps between what Big QMT hands us and what the MiniQMT-shaped API
promises: the ORDER row carries a submit time we never read, and a wide
stock_list shares one RPC timeout so it either fits or loses everything.
"""

import os
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters import order_bigqmt
from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway, _order_time_seconds
from bigqmt_signal_trader.exec_events import date_time_seconds
from bigqmt_signal_trader.models import OrderSnapshot
from bigqmt_signal_trader.xtquant_compat import BigQmtXtData, BigQmtXtTrader, StockAccount


class Row(object):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _order_row(**overrides):
    base = dict(
        m_strOrderSysID="sys-1",
        m_strRemark="tag-1",
        m_strInstrumentID="601398",
        m_strExchangeID="SH",
        m_nOffsetFlag=48,
        m_nVolumeTotalOriginal=100,
        m_nVolumeTraded=0,
        m_nOrderStatus=50,
        m_dLimitPrice=7.66,
        m_strStrategyName="s1",
    )
    base.update(overrides)
    return Row(**base)


def _gateway(rows):
    return BigQmtOrderGateway(
        context_info=None,
        passorder_func=None,
        cancel_func=None,
        get_trade_detail_data_func=lambda acct, atype, dtype, sname="": (
            list(rows) if dtype == "ORDER" else []
        ),
    )


class OrderTimeParsingTest(unittest.TestCase):
    def setUp(self):
        del order_bigqmt._missing_order_time_reported[:]

    def _expected(self, text):
        return int(time.mktime(time.strptime(text, "%Y%m%d%H%M%S")))

    def test_parses_split_date_and_time(self):
        row = _order_row(m_strInsertDate="20260819", m_strInsertTime="093015")
        self.assertEqual(_order_time_seconds(row), self._expected("20260819093015"))

    def test_hhmmss_preserves_seconds_and_afternoon_hours(self):
        for clock in ("93000", "93003", "93004", "93010", "93015", "93016", "93017",
                      "94000", "95959", "100000", "113000", "130000", "140000", "145500", "150000"):
            for raw in (clock, int(clock)):
                expected = self._expected("20260910" + clock.zfill(6))
                self.assertEqual(date_time_seconds("20260910", raw), expected)
                row = _order_row(m_strInsertDate="20260910", m_strInsertTime=raw)
                self.assertEqual(_order_time_seconds(row), expected)

    def test_tolerates_separators(self):
        row = _order_row(m_strInsertDate="2026-08-19", m_strInsertTime="09:30:15")
        self.assertEqual(_order_time_seconds(row), self._expected("20260819093015"))

    def test_drops_sub_second_precision(self):
        row = _order_row(m_strInsertDate="20260819", m_strInsertTime="09:30:15.123")
        self.assertEqual(_order_time_seconds(row), self._expected("20260819093015"))

    def test_accepts_alternate_field_spellings(self):
        for date_field, time_field in (("m_strInsertDate", "m_strInsertTime"),
                                       ("m_strOrderDate", "m_strOrderTime"),
                                       ("insert_date", "insert_time")):
            row = _order_row(**{date_field: "20260819", time_field: "093015"})
            self.assertEqual(_order_time_seconds(row), self._expected("20260819093015"),
                             "%s/%s" % (date_field, time_field))

    def test_passes_through_an_epoch_value(self):
        stamp = self._expected("20260819093015")
        self.assertEqual(_order_time_seconds(_order_row(m_strInsertTime=stamp)), stamp)

    def test_normalizes_millisecond_epoch(self):
        stamp = self._expected("20260819093015")
        self.assertEqual(_order_time_seconds(_order_row(m_strInsertTime=stamp * 1000)), stamp)

    def test_missing_fields_yield_zero_not_epoch_start(self):
        """0 means "not reported"; 1970-01-01 would look like a real timestamp."""
        self.assertEqual(_order_time_seconds(_order_row()), 0)

    def test_missing_fields_report_what_the_row_actually_has(self):
        import contextlib
        import io as _io

        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _order_time_seconds(_order_row(m_strSomethingElse="x"))
        output = buffer.getvalue()

        self.assertIn("order_time not found", output)
        self.assertIn("m_strSomethingElse", output)

    def test_missing_field_reported_once_not_per_row(self):
        import contextlib
        import io as _io

        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            for _ in range(5):
                _order_time_seconds(_order_row())

        self.assertEqual(buffer.getvalue().count("order_time not found"), 1)

    def test_unparseable_date_yields_zero(self):
        self.assertEqual(
            _order_time_seconds(_order_row(m_strInsertDate="oops", m_strInsertTime="093015")), 0)


class OrderSnapshotTest(unittest.TestCase):
    def setUp(self):
        del order_bigqmt._missing_order_time_reported[:]

    def test_defaults_keep_existing_positional_callers_working(self):
        snapshot = OrderSnapshot("sys", "tag", "601398.SH", "BUY", 100, 0, "50")
        self.assertEqual(snapshot.order_time, 0)

    def test_gateway_fills_order_time(self):
        rows = [_order_row(m_strInsertDate="20260819", m_strInsertTime="093015")]
        order = _gateway(rows).query_orders("acct", "")[0]

        self.assertEqual(order.order_time,
                         int(time.mktime(time.strptime("20260819093015", "%Y%m%d%H%M%S"))))
        self.assertEqual(order.stock_code, "601398.SH")


class ClientOrderTimeTest(unittest.TestCase):
    """The reported symptom: XtOrder.order_time simply is not there."""

    def _trader(self, payload):
        trader = BigQmtXtTrader(account_id="acct")
        trader.client.call = lambda method, params=None, account_id=None, **kw: payload
        return trader

    def test_order_time_is_exposed(self):
        stamp = int(time.mktime(time.strptime("20260819093015", "%Y%m%d%H%M%S")))
        orders = self._trader([{
            "stock_code": "601398.SH", "action": "BUY", "order_sys_id": "sys-1",
            "volume": 100, "order_time": stamp,
        }]).query_stock_orders(StockAccount("acct"))

        self.assertEqual(orders[0].order_time, stamp)

    def test_missing_order_time_defaults_to_zero(self):
        """A server predating this field must not raise AttributeError."""
        orders = self._trader([{
            "stock_code": "601398.SH", "action": "BUY", "order_sys_id": "sys-1", "volume": 100,
        }]).query_stock_orders(StockAccount("acct"))

        self.assertEqual(orders[0].order_time, 0)


class ChunkingClient(object):
    """Records the code count of each request, and can fail chosen codes."""

    def __init__(self, fail_codes=(), max_codes_per_call=None):
        self.account_id = "acct"
        self.batches = []
        self.local_cache_config = {"enabled": False}
        self.fail_codes = set(fail_codes)
        self.max_codes_per_call = max_codes_per_call

    def _redis(self):
        return None

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        codes = list((params or {}).get("stock_list") or [])
        self.batches.append(codes)
        if self.max_codes_per_call is not None and len(codes) > self.max_codes_per_call:
            raise TimeoutError("rpc timeout: %d codes" % len(codes))
        if self.fail_codes & set(codes):
            raise TimeoutError("rpc timeout")
        import pandas as pd

        return {c: pd.DataFrame({"stime": ["20260819"], "close": [7.66]}) for c in codes}


class MarketDataChunkingTest(unittest.TestCase):
    """issue #47 comment: one request carries every code and one timeout, so a
    wide stock_list times out and loses the whole pull."""

    def _xt(self, **kwargs):
        return BigQmtXtData(ChunkingClient(**kwargs))

    def test_wide_list_is_split(self):
        xt = self._xt()
        codes = ["C%03d.SH" % i for i in range(250)]

        data = xt.get_market_data_ex(stock_list=codes, period="1d")

        self.assertEqual([len(b) for b in xt.client.batches], [100, 100, 50])
        self.assertEqual(len(data), 250)

    def test_small_list_stays_a_single_request(self):
        xt = self._xt()
        xt.get_market_data_ex(stock_list=["600000.SH", "601398.SH"], period="1d")

        self.assertEqual(len(xt.client.batches), 1)

    def test_chunk_size_zero_restores_single_request(self):
        xt = self._xt()
        codes = ["C%03d.SH" % i for i in range(250)]

        xt.get_market_data_ex(stock_list=codes, period="1d", chunk_size=0)

        self.assertEqual(len(xt.client.batches), 1)

    def test_a_wide_pull_that_would_time_out_now_succeeds(self):
        """The actual report: the server cannot answer 250 codes at once."""
        xt = self._xt(max_codes_per_call=120)
        codes = ["C%03d.SH" % i for i in range(250)]

        data = xt.get_market_data_ex(stock_list=codes, period="1d")

        self.assertEqual(len(data), 250)

    def test_one_failed_batch_does_not_lose_the_others(self):
        xt = self._xt(fail_codes=["C150.SH"])
        codes = ["C%03d.SH" % i for i in range(250)]

        data = xt.get_market_data_ex(stock_list=codes, period="1d")

        # The 100-code batch holding C150 is lost; the other 150 survive.
        self.assertEqual(len(data), 150)
        self.assertNotIn("C150.SH", data)
        self.assertIn("C000.SH", data)

    def test_total_failure_raises_rather_than_returning_empty(self):
        xt = self._xt(max_codes_per_call=0)
        codes = ["C%03d.SH" % i for i in range(250)]

        with self.assertRaises(TimeoutError):
            xt.get_market_data_ex(stock_list=codes, period="1d")


if __name__ == "__main__":
    unittest.main()
