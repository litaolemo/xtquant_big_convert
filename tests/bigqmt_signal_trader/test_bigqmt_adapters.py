import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapter_factory import build_app
from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider
from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway
from bigqmt_signal_trader.adapters.position_bigqmt import BigQmtPositionProvider
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


class BigQmtAdaptersTest(unittest.TestCase):
    def test_market_provider_normalizes_codes_before_context_call(self):
        context = FakeContext()
        provider = BigQmtMarketDataProvider(context)

        ticks = provider.get_ticks(["600000"])
        instrument = provider.get_instrument("sz000001")

        self.assertIn("600000.SH", ticks)
        self.assertEqual(context.tick_codes, [["600000.SH"]])
        self.assertEqual(context.instrument_codes, ["000001.SZ"])
        self.assertEqual(instrument["InstrumentStatus"], 0)

    def test_market_provider_passes_market_codes_to_full_tick(self):
        context = FakeContext()
        provider = BigQmtMarketDataProvider(context)

        provider.get_ticks(["SH", "sz"])

        self.assertEqual(context.tick_codes, [["SH", "SZ"]])

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
        self.assertEqual(calls[0][0:9], (23, 1101, "acct", "600000.SH", 44, 10.12, 300, "bigqmt_signal_trader", 2))
        self.assertEqual(calls[0][9], result.user_order_id)
        self.assertIs(calls[0][10], context)

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
                    m_nOffsetFlag=49,
                    m_nVolumeTotalOriginal=1000,
                    m_nVolumeTraded=200,
                    m_nOrderStatus=50,
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
        self.assertEqual(orders[0].traded_volume, 200)

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


if __name__ == "__main__":
    unittest.main()
