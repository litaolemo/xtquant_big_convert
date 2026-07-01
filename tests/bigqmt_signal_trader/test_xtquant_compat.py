import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import (
    FIX_PRICE,
    MARKET_PEER_PRICE_FIRST,
    SH_MARKET,
    STOCK_BUY,
    STOCK_SELL,
    SZ_MARKET,
    BigQmtXtData,
    BigQmtXtTrader,
    StockAccount,
    configure,
    xt_trader,
)


class FakeRpcClient:
    def __init__(self):
        self.account_id = "acct"
        self.calls = []

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        self.calls.append((method, params or {}, account_id))
        if method == "query_stock_asset":
            return {"account_id": "acct", "cash": 100.5, "total_asset": 1000.5}
        if method == "query_stock_positions":
            return {
                "600000.SH": {
                    "stock_code": "600000.SH",
                    "volume": 1000,
                    "available": 800,
                    "cost": 10.2,
                    "stock_name": "PF Bank",
                }
            }
        if method == "query_stock_position":
            return {
                "stock_code": "600000.SH",
                "volume": 1000,
                "available": 800,
                "cost": 10.2,
            }
        if method == "query_stock_orders":
            return [
                {
                    "order_sys_id": "sys-1",
                    "user_order_id": "remark-1",
                    "stock_code": "600000.SH",
                    "action": "SELL",
                    "volume": 300,
                    "traded_volume": 100,
                    "status": "50",
                    "price": 10.1,
                }
            ]
        if method == "query_stock_trades":
            return [
                {
                    "trade_id": "trade-1",
                    "order_sys_id": "sys-1",
                    "stock_code": "600000.SH",
                    "action": "BUY",
                    "volume": 100,
                    "price": 10.0,
                }
            ]
        if method == "order_stock":
            return {"status": "SUBMITTED", "user_order_id": "bq:1", "order_sys_id": "sys-2"}
        if method == "cancel_order_stock_sysid":
            return {"success": True}
        if method == "get_full_tick":
            codes = params.get("codes") or []
            if codes == ["SH", "SZ"]:
                return {
                    "000001.SH": {"lastPrice": 3000},
                    "000001.SZ": {"lastPrice": 10},
                    "600000.SH": {"lastPrice": 10},
                    "510300.SH": {"lastPrice": 4},
                    "300001.SZ": {"lastPrice": 20},
                    "113001.SH": {"lastPrice": 100},
                }
            return {codes[0]: {"lastPrice": 10, "bidPrice": [9.9], "askPrice": [10.1]}}
        if method == "get_instrument_detail":
            return {"InstrumentStatus": 0, "code": params.get("code")}
        if method == "ping":
            return {"pong": True}
        raise AssertionError("unexpected method: %s" % method)


class XtquantCompatTest(unittest.TestCase):
    def _trader(self):
        trader = BigQmtXtTrader(account_id="acct")
        trader.client = FakeRpcClient()
        return trader

    def _xtdata(self):
        return BigQmtXtData(FakeRpcClient())

    def test_trader_read_methods_return_miniqmt_style_objects(self):
        trader = self._trader()
        acc = StockAccount("acct")

        asset = trader.query_stock_asset(acc)
        positions = trader.query_stock_positions(acc)
        single = trader.query_stock_position(acc, "600000")

        self.assertEqual(asset.cash, 100.5)
        self.assertEqual(asset.market_value, 900.0)
        self.assertEqual(positions[0].stock_code, "600000.SH")
        self.assertEqual(positions[0].can_use_volume, 800)
        self.assertEqual(positions[0].avg_price, 10.2)
        self.assertEqual(single.stock_code, "600000.SH")

    def test_orders_trades_order_and_cancel_are_miniqmt_shaped(self):
        trader = self._trader()
        acc = StockAccount("acct")

        orders = trader.query_stock_orders(acc, cancelable_only=False)
        trades = trader.query_stock_trades(acc)
        order_id = trader.order_stock(
            acc,
            "600000.SH",
            STOCK_BUY,
            100,
            MARKET_PEER_PRICE_FIRST,
            0,
            "strategy",
            "remark",
        )
        cancelled = trader.cancel_order_stock_sysid(acc, SH_MARKET, "sys-2")

        self.assertEqual(orders[0].order_type, STOCK_SELL)
        self.assertEqual(orders[0].order_status, 50)
        self.assertEqual(orders[0].order_volume, 300)
        self.assertEqual(trades[0].order_type, STOCK_BUY)
        self.assertEqual(trades[0].traded_price, 10.0)
        self.assertEqual(order_id, "sys-2")
        self.assertTrue(cancelled)
        self.assertEqual(trader.client.calls[-2][1]["price_type"], MARKET_PEER_PRICE_FIRST)

    def test_xtdata_read_methods_and_sector_filter(self):
        xtdata = self._xtdata()

        ticks = xtdata.get_full_tick(["600000.SH"])
        detail = xtdata.get_instrument_detail("600000.SH")
        sector_codes = xtdata.get_stock_list_in_sector("沪深A股")

        self.assertEqual(ticks["600000.SH"]["bidPrice"], [9.9])
        self.assertEqual(detail["InstrumentStatus"], 0)
        self.assertEqual(sector_codes, ["000001.SZ", "300001.SZ", "600000.SH"])

    def test_optional_xtquant_shim_imports_constants_and_classes(self):
        from xtquant import xtconstant
        from xtquant.xttrader import XtQuantTrader
        from xtquant.xttype import StockAccount as ShimStockAccount

        self.assertEqual(xtconstant.STOCK_BUY, STOCK_BUY)
        self.assertEqual(xtconstant.FIX_PRICE, FIX_PRICE)
        self.assertEqual(xtconstant.SZ_MARKET, SZ_MARKET)
        self.assertIs(XtQuantTrader, BigQmtXtTrader)
        self.assertEqual(ShimStockAccount("acct").account_id, "acct")

    def test_configure_updates_imported_xt_trader_object_in_place(self):
        original = xt_trader
        configure(account_id="acct-new", redis_client=FakeRpcClient())

        self.assertIs(xt_trader, original)
        self.assertEqual(xt_trader.client.account_id, "acct-new")


if __name__ == "__main__":
    unittest.main()
