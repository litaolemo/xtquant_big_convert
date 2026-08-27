import os
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapter_factory import build_app
from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider
from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway
from bigqmt_signal_trader.adapters.position_bigqmt import BigQmtPositionProvider
from bigqmt_signal_trader.adapters.position_bigqmt import _full_code
from bigqmt_signal_trader.models import OrderRef, OrderRequest


class Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeContext:
    def __init__(self):
        self.tick_codes = []
        self.instrument_codes = []

    def get_full_tick(self, codes):
        self.tick_codes.append(list(codes))
        return {codes[0]: {"lastPrice": 10.0}}

    def get_instrumentdetail(self, code):
        self.instrument_codes.append(code)
        return {"InstrumentStatus": 0}


class FakeMarketDataContext(FakeContext):
    def __init__(self):
        super().__init__()
        self.market_calls = []

    def get_market_data_ex(
        self,
        fields=None,
        stock_code=None,
        period="1d",
        start_time="",
        end_time="",
        count=-1,
        dividend_type="none",
    ):
        self.market_calls.append(
            {
                "method": "get_market_data_ex",
                "fields": fields,
                "stock_code": stock_code,
                "period": period,
                "start_time": start_time,
                "end_time": end_time,
                "count": count,
                "dividend_type": dividend_type,
            }
        )
        return {"600000.SH": {"close": [10.0]}}


class FakeMarketDataFallbackContext(FakeContext):
    def get_market_data(self, fields=None, stock_code=None, period="1d", **kwargs):
        return {
            "method": "get_market_data",
            "fields": fields,
            "stock_code": stock_code,
            "period": period,
            "kwargs": kwargs,
        }


class BigQmtAdaptersTest(unittest.TestCase):

    def test_position_code_keeps_future_contract_code(self):
        # A futures row always carries the XunTou-short exchange token, so a
        # bare contract with an empty exchange is not futures -- normalize it
        # and let an unrecognizable code raise as malformed.
        with self.assertRaises(ValueError):
            _full_code("EG2609", "")
        with self.assertRaises(ValueError):
            _full_code("IF2609", "")
        self.assertEqual(_full_code("600000", "SH"), "600000.SH")

    def test_futures_exchange_short_token_appends_suffix_and_preserves_case(self):
        """Futures are classified by the exchange field (XunTou-short token),
        which is appended as the suffix. The symbol keeps the exchange's exact
        case (e.g. rb2401.SF lower, AP401.ZF upper) and must not be rewritten."""
        self.assertEqual(_full_code("rb2401", "SF"), "rb2401.SF")   # SHFE lower-case
        self.assertEqual(_full_code("AP401", "ZF"), "AP401.ZF")     # CZCE upper-case
        self.assertEqual(_full_code("a2609", "DF"), "a2609.DF")     # DCE lower-case
        self.assertEqual(_full_code("sc2401", "INE"), "sc2401.INE") # INE lower-case
        self.assertEqual(_full_code("IF2609", "IF"), "IF2609.IF")   # CFFEX
        self.assertEqual(_full_code("GF2609", "GF"), "GF2609.GF")   # GFEX

    def test_futures_display_exchange_id_raises(self):
        """A counter display ID (DCE/CFFEX/...) is an unexpected path: POSITION
        rows normally carry the XunTou-short token (DF/SF/...), so this signals
        a structure/vendor mismatch and must surface rather than degrade."""
        for exchange in ("DCE", "CFFEX"):
            with self.assertRaises(ValueError):
                _full_code("EG2609", exchange)

    def test_market_provider_normalizes_codes_before_context_call(self):
        context = FakeContext()
        provider = BigQmtMarketDataProvider(context)

        ticks = provider.get_ticks(["600000"])
        instrument = provider.get_instrument("sz000001")

        self.assertIn("600000.SH", ticks)
        self.assertEqual(context.tick_codes, [["600000.SH"]])
        self.assertEqual(context.instrument_codes, ["000001.SZ"])
        self.assertEqual(instrument["InstrumentStatus"], 0)

    def test_get_ticks_keys_keep_the_caller_case_for_futures(self):
        """issue #58: futures instrument codes are lower-case ('rb2708.SF'), but
        the keys came back upper-cased, so `code in result` failed for every
        futures code and the book looked missing."""
        class EchoContext:
            def __init__(self):
                self.tick_codes = []

            def get_full_tick(self, codes):
                self.tick_codes.append(list(codes))
                return dict((c, {"lastPrice": 1.0}) for c in codes)

        context = EchoContext()
        provider = BigQmtMarketDataProvider(context)

        ticks = provider.get_ticks(["rb2708.SF", "a2609.DF"])

        # QMT is still asked in upper case...
        self.assertEqual(context.tick_codes, [["RB2708.SF", "A2609.DF"]])
        # ...but the caller gets its own spelling back.
        self.assertEqual(sorted(ticks), ["a2609.DF", "rb2708.SF"])

    def test_get_ticks_still_completes_the_suffix(self):
        """Only case is restored. Completing '600000' to '600000.SH' is useful
        normalization that callers depend on, so it must survive."""
        context = FakeContext()
        provider = BigQmtMarketDataProvider(context)

        self.assertIn("600000.SH", provider.get_ticks(["600000"]))

    def test_get_ticks_passes_through_an_unrequested_key(self):
        """Dropping a quote QMT volunteered would be worse than an odd key."""
        class ExtraContext:
            def get_full_tick(self, codes):
                return {"RB2708.SF": {"lastPrice": 1.0}, "SURPRISE.SF": {"lastPrice": 2.0}}

        ticks = BigQmtMarketDataProvider(ExtraContext()).get_ticks(["rb2708.SF"])

        self.assertEqual(sorted(ticks), ["SURPRISE.SF", "rb2708.SF"])

    def test_market_provider_passes_market_codes_to_full_tick(self):
        context = FakeContext()
        provider = BigQmtMarketDataProvider(context)

        provider.get_ticks(["SH", "sz"])

        self.assertEqual(context.tick_codes, [["SH", "SZ"]])

    def test_market_provider_supports_bigqmt_market_data_ex_signature(self):
        context = FakeMarketDataContext()
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(field_list=["close"], stock_list=["600000.SH"], count=1)

        self.assertEqual(data["600000.SH"]["close"], [10.0])
        self.assertEqual(context.market_calls[0]["fields"], ["close"])
        self.assertEqual(context.market_calls[0]["stock_code"], ["600000.SH"])
        self.assertEqual(context.market_calls[0]["count"], 1)

    def test_market_provider_falls_back_to_market_data_when_ex_is_missing(self):
        context = FakeMarketDataFallbackContext()
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(field_list=["close"], stock_list=["600000.SH"], period="1m")

        self.assertEqual(data["method"], "get_market_data")
        self.assertEqual(data["fields"], ["close"])
        self.assertEqual(data["stock_code"], ["600000.SH"])
        self.assertEqual(data["period"], "1m")

    def test_position_provider_maps_qmt_position_objects(self):
        calls = []

        def fake_query(account, account_type, detail_type, *args):
            calls.append((account, account_type, detail_type, args))
            if detail_type == "POSITION":
                return [
                    Obj(
                        m_strInstrumentID="510300",
                        m_strExchangeID="SH",
                        m_nVolume=1000,
                        m_nCanUseVolume=800,
                        m_dOpenPrice=3.456,
                        m_dLastPrice=3.789,
                        m_dMarketValue=3789.0,
                        m_nFrozenVolume=200,
                        m_nOnRoadVolume=10,
                        m_nYesterdayVolume=900,
                        m_strInstrumentName="ETF",
                    )
                ]
            return []

        provider = BigQmtPositionProvider(fake_query)
        positions = provider.get_positions("acct")

        self.assertEqual(calls[0], ("acct", "STOCK", "POSITION", ()))
        self.assertEqual(positions["510300.SH"].volume, 1000)
        self.assertEqual(positions["510300.SH"].available, 800)
        self.assertEqual(positions["510300.SH"].cost, 3.456)
        self.assertEqual(positions["510300.SH"].price, 3.789)
        self.assertEqual(positions["510300.SH"].market_value, 3789.0)
        self.assertEqual(positions["510300.SH"].frozen_volume, 200)
        self.assertEqual(positions["510300.SH"].on_road_volume, 10)
        self.assertEqual(positions["510300.SH"].yesterday_volume, 900)

    def test_order_gateway_submit_uses_qmt_jq_trade_passorder_shape(self):
        calls = []

        def fake_passorder(*args):
            calls.append(args)

        context = object()
        gateway = BigQmtOrderGateway(context_info=context, passorder_func=fake_passorder)
        request = OrderRequest(
            signal_id="sig-001",
            account_id="acct",
            action="BUY",
            stock_code="600000",
            volume=300,
            price=10.12,
            price_type=44,
            strategy_name="bigqmt_signal_trader",
            remark="manual",
        )

        result = gateway.submit(request)

        self.assertEqual(result.status, "SUBMITTED")
        self.assertEqual(result.user_order_id, "manual")
        self.assertEqual(calls[0][0:9], (23, 1101, "acct", "600000.SH", 44, 10.12, 300, "bigqmt_signal_trader", 2))
        self.assertEqual(calls[0][9], result.user_order_id)
        self.assertIs(calls[0][10], context)

    def test_order_gateway_submit_preserves_credit_operation_types(self):
        cases = (
            (27, "BUY"),
            (31, "SELL"),
            (32, "REPAY"),
            (33, "BUY"),
            (34, "SELL"),
        )
        for order_type, action in cases:
            with self.subTest(order_type=order_type):
                calls = []
                gateway = BigQmtOrderGateway(
                    context_info=object(),
                    passorder_func=lambda *args: calls.append(args),
                )
                request = OrderRequest(
                    signal_id="sig-%s" % order_type,
                    account_id="acct",
                    action=action,
                    stock_code="600000.SH",
                    volume=100,
                    price=10.0,
                    price_type=11,
                    strategy_name="credit-strategy",
                    order_type=order_type,
                )

                gateway.submit(request)

                self.assertEqual(calls[0][0], order_type)

    def test_order_gateway_cancel_and_query_orders(self):
        cancel_calls = []

        def fake_cancel(*args):
            cancel_calls.append(args)
            return True

        def fake_query(account, account_type, detail_type, strategy_name):
            self.assertEqual((account, account_type, detail_type, strategy_name), ("acct", "STOCK", "ORDER", "s"))
            return [
                Obj(
                    m_strOrderSysID="ord1",
                    m_strRemark="remark1",
                    m_strInstrumentID="000001",
                    m_strExchangeID="SZ",
                    m_nOffsetFlag=48,
                    m_nOpType=31,
                    m_nVolumeTotalOriginal=1000,
                    m_nVolumeTraded=200,
                    m_nOrderStatus=50,
                    m_dLimitPrice=10.12,
                    m_dTradedPrice=10.05,
                )
            ]

        context = object()
        gateway = BigQmtOrderGateway(
            context_info=context,
            account_id="acct",
            cancel_func=fake_cancel,
            get_trade_detail_data_func=fake_query,
        )

        cancel_result = gateway.cancel(OrderRef("ord1"))
        orders = gateway.query_orders("acct", "s")

        self.assertTrue(cancel_result.success)
        self.assertEqual(cancel_calls, [("ord1", "acct", "STOCK", context)])
        self.assertEqual(orders[0].stock_code, "000001.SZ")
        self.assertEqual(orders[0].action, "SELL")
        self.assertEqual(orders[0].order_type, 31)
        self.assertEqual(orders[0].traded_volume, 200)
        self.assertEqual(orders[0].price, 10.12)
        self.assertEqual(orders[0].traded_price, 10.05)

    def test_query_trades_without_strategy_omits_strategy_filter(self):
        calls = []

        def fake_query(*args):
            calls.append(args)
            return [
                Obj(
                    m_strTradeID="manual-trade-1",
                    m_strOrderSysID="manual-order-1",
                    m_strInstrumentID="600276",
                    m_strExchangeID="SH",
                    m_nOffsetFlag=48,
                    m_nVolume=100,
                    m_dPrice=54.76,
                    m_strTradeTime="130524",
                    m_strRemark="",
                )
            ]

        gateway = BigQmtOrderGateway(
            context_info=object(),
            get_trade_detail_data_func=fake_query,
        )

        trades = gateway.query_trades_strict("acct", "")

        self.assertEqual(calls, [("acct", "STOCK", "DEAL")])
        self.assertEqual(trades[0].trade_id, "manual-trade-1")
        self.assertEqual(trades[0].stock_code, "600276.SH")
        self.assertEqual(trades[0].action, "BUY")
        self.assertEqual(trades[0].volume, 100)
        self.assertEqual(trades[0].price, 54.76)

    def test_query_trades_reads_official_deal_amount_time_and_strategy(self):
        # 官方文档 (dict.thinktrader.net data_structure): Deal 对象含
        # m_dTradeAmount(成交额)、m_strTradeDate+m_strTradeTime(成交日期/时间)、
        # m_strRemark(投资备注, 即 passorder 的 userOrderId)。
        def fake_query(account, account_type, detail_type, strategy_name):
            return [
                Obj(
                    m_strTradeID="t-1",
                    m_strOrderSysID="sys-1",
                    m_strInstrumentID="600000",
                    m_strExchangeID="SH",
                    m_nOffsetFlag=49,
                    m_nVolume=100,
                    m_dPrice=10.5,
                    m_dTradeAmount=1055.55,
                    m_strTradeDate="20260820",
                    m_strTradeTime="100001",
                    m_strRemark="rmk-1",
                )
            ]

        gateway = BigQmtOrderGateway(
            context_info=object(),
            get_trade_detail_data_func=fake_query,
        )

        trades = gateway.query_trades_strict("acct", "strat-a")

        self.assertEqual(trades[0].amount, 1055.55)
        expected_ts = time.mktime(time.strptime("20260820100001", "%Y%m%d%H%M%S"))
        self.assertEqual(trades[0].traded_time, int(expected_ts))
        # 按策略名过滤时返回集必属该策略 (官方: strategyName 仅对委托/成交查询有效)
        self.assertEqual(trades[0].strategy_name, "strat-a")

    def test_factory_bigqmt_mode_wires_real_adapters(self):
        app = build_app(
            FakeContext(),
            {
                "mode": "bigqmt",
                "account_id": "acct",
                "qmt_api": {
                    "passorder": lambda *args: None,
                    "cancel": lambda *args: True,
                    "get_trade_detail_data": lambda *args: [],
                },
            },
        )

        self.assertIsInstance(app.market_data, BigQmtMarketDataProvider)
        self.assertIsInstance(app.position_provider, BigQmtPositionProvider)
        self.assertIsInstance(app.order_gateway, BigQmtOrderGateway)


class FinancialFieldTranslateTest(unittest.TestCase):
    """Issue #52: MiniQMT table names must translate to Big QMT's dotted
    "BIGTABLE.field" fieldList before calling ContextInfo.get_financial_data."""

    def _provider(self):
        calls = []

        class _Ctx:
            def get_financial_data(self, field_list, stock_list, start_date, end_date, report_type):
                calls.append((field_list, stock_list, start_date, end_date, report_type))
                return {}

        return BigQmtMarketDataProvider(_Ctx()), calls

    def test_miniqmt_table_name_expands_to_full_dotted_field_list(self):
        provider, calls = self._provider()
        provider.get_financial_data(["600000.SH"], ["Balance"], "20260101", "20260819")
        field_list = calls[0][0]
        self.assertEqual(calls[0][1], ["600000.SH"])  # stockList second
        self.assertTrue(field_list)
        self.assertTrue(all(f.startswith("ASHAREBALANCESHEET.") for f in field_list))
        self.assertIn("ASHAREBALANCESHEET.tot_assets", field_list)
        self.assertIn("ASHAREBALANCESHEET.tot_liab", field_list)

    def test_bigqmt_bare_table_name_also_expands(self):
        provider, calls = self._provider()
        provider.get_financial_data(["600000.SH"], ["CAPITALSTRUCTURE"])
        field_list = calls[0][0]
        self.assertIn("CAPITALSTRUCTURE.total_capital", field_list)

    def test_dotted_entries_remap_prefix_and_bigqmt_dots_pass_through(self):
        provider, calls = self._provider()
        provider.get_financial_data(["600000.SH"], [
            "Balance.tot_assets",
            "ASHAREINCOME.net_profit_incl_min_int_inc",
        ])
        field_list = calls[0][0]
        self.assertEqual(field_list, [
            "ASHAREBALANCESHEET.tot_assets",
            "ASHAREINCOME.net_profit_incl_min_int_inc",
        ])

    def test_unknown_table_name_passes_through(self):
        provider, calls = self._provider()
        provider.get_financial_data(["600000.SH"], ["CustomTable"])
        self.assertEqual(calls[0][0], ["CustomTable"])




class UnparsableRowIsolationTest(unittest.TestCase):
    """A row _full_code cannot parse must cost that row, not the query.

    PR #68 made _full_code raise on a counter-style futures exchange ID, which
    is the right signal -- but every caller loops without per-row protection, so
    one odd row turned "one position missing" into "no positions at all". For a
    trading system that is the more dangerous failure.
    """

    class _Row(object):
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def _rows(self):
        return [
            self._Row(m_strInstrumentID="600000", m_strExchangeID="SH",
                      m_nVolume=100, m_nCanUseVolume=100,
                      m_nVolumeTotalOriginal=100, m_nVolumeTraded=0,
                      m_strTradeID="t1", m_nVolume_deal=1),
            # Counter-style display ID -- _full_code raises on this one.
            self._Row(m_strInstrumentID="rb2401", m_strExchangeID="SHFE",
                      m_nVolume=1, m_nCanUseVolume=1,
                      m_nVolumeTotalOriginal=1, m_nVolumeTraded=0,
                      m_strTradeID="t2"),
            self._Row(m_strInstrumentID="000001", m_strExchangeID="SZ",
                      m_nVolume=200, m_nCanUseVolume=200,
                      m_nVolumeTotalOriginal=200, m_nVolumeTraded=0,
                      m_strTradeID="t3"),
        ]

    def setUp(self):
        from bigqmt_signal_trader.adapters import position_bigqmt

        position_bigqmt._unparsable_rows_reported.clear()

    def test_positions_survive_one_unparsable_row(self):
        from bigqmt_signal_trader.adapters.position_bigqmt import BigQmtPositionProvider

        rows = self._rows()
        provider = BigQmtPositionProvider(lambda a, t, d: rows if d == "POSITION" else [])

        positions = provider.get_positions("acct")

        self.assertEqual(sorted(positions), ["000001.SZ", "600000.SH"])

    def test_orders_survive_one_unparsable_row(self):
        from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway

        rows = self._rows()
        gateway = BigQmtOrderGateway(
            context_info=None, passorder_func=None, cancel_func=None,
            get_trade_detail_data_func=lambda a, t, d, s="": rows if d == "ORDER" else [])

        orders = gateway.query_orders("acct", "")

        self.assertEqual(sorted(o.stock_code for o in orders), ["000001.SZ", "600000.SH"])

    def test_trades_survive_one_unparsable_row(self):
        from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway

        rows = self._rows()
        gateway = BigQmtOrderGateway(
            context_info=None, passorder_func=None, cancel_func=None,
            get_trade_detail_data_func=lambda a, t, d, s="": rows if d == "DEAL" else [])

        trades = gateway.query_trades("acct", "")

        self.assertEqual(sorted(t.stock_code for t in trades), ["000001.SZ", "600000.SH"])

    def test_the_skipped_row_is_reported_once(self):
        import contextlib
        import io as _io

        from bigqmt_signal_trader.adapters.position_bigqmt import BigQmtPositionProvider

        rows = self._rows()
        provider = BigQmtPositionProvider(lambda a, t, d: rows if d == "POSITION" else [])
        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            provider.get_positions("acct")
            provider.get_positions("acct")
        output = buffer.getvalue()

        self.assertEqual(output.count("skipping unparsable"), 1)
        self.assertIn("rb2401", output)
        self.assertIn("SHFE", output)
