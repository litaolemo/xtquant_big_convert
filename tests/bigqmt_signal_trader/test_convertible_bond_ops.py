# coding: utf-8
"""Convertible bonds: a ``cbond`` tick-type alias, and 转股 / 回售.

Asked for by a user with a convertible-bond strategy: (1) ``get_full_tick``
by market token with ``types=["cbond"]`` -- the sector filter already had
``convertible``; the alias is what people type; (2) 转股 (convert to shares)
and 回售 (sell back to the issuer), which MiniQMT has no API for but big QMT
places through ``passorder`` opType 80-83 (普通户 80/81, 信用户 82/83).

MiniQMT's ``OPT_CONVERT_BONDS = 51`` is an order-record operation code; as a
passorder opType 51 is 卖出平仓 (ETF option), so it is NOT accepted here as
an alias -- the big-QMT numbers are the contract, and two xttrader methods
pick them from the account type so callers never touch the numbers.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import SECTOR_BY_TYPE  # noqa: E402
from bigqmt_signal_trader.adapters.order_bigqmt import (  # noqa: E402
    BigQmtOrderGateway,
    CONVERTIBLE_OP_TYPES,
    convertible_optype_for,
    convertible_optype_of,
    is_sideless_order_type,
)
from bigqmt_signal_trader.models import OrderRequest  # noqa: E402
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers  # noqa: E402
from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader, StockAccount  # noqa: E402


class TickTypeAliasTest(unittest.TestCase):
    def test_cbond_is_the_convertible_sector(self):
        for alias in ("cbond", "cb", "convertible_bond", "convertible"):
            self.assertEqual("沪深转债", SECTOR_BY_TYPE[alias], alias)

    def test_the_market_token_expands_through_the_alias(self):
        from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider

        provider = BigQmtMarketDataProvider.__new__(BigQmtMarketDataProvider)
        asked = []

        def sector_codes(sector):
            asked.append(sector)
            return ["113050.SH", "128136.SZ", "110088.SH"]

        provider._sector_codes = sector_codes
        codes = provider._expand_market_token("SH", ["cbond"])
        self.assertEqual(["沪深转债"], asked)
        self.assertEqual(["113050.SH", "110088.SH"], codes)


class OpTypeTableTest(unittest.TestCase):
    def test_the_four_numbers(self):
        self.assertEqual({80, 81, 82, 83}, set(CONVERTIBLE_OP_TYPES))
        self.assertEqual(80, convertible_optype_for("convert", "STOCK"))
        self.assertEqual(81, convertible_optype_for("sell_back", "STOCK"))
        self.assertEqual(82, convertible_optype_for("convert", "CREDIT"))
        self.assertEqual(83, convertible_optype_for("sell_back", "credit"))
        self.assertEqual(80, convertible_optype_for("转股", ""))
        self.assertEqual(81, convertible_optype_for("回售", None))

    def test_an_unknown_action_is_refused(self):
        with self.assertRaises(ValueError):
            convertible_optype_for("buy", "STOCK")

    def test_miniqmts_51_52_are_not_convertible_types_here(self):
        """51/52 are ETF-option opTypes in passorder numbering."""
        self.assertIsNone(convertible_optype_of(51))
        self.assertIsNone(convertible_optype_of(52))
        self.assertEqual(82, convertible_optype_of(82))

    def test_they_have_no_side(self):
        for value in CONVERTIBLE_OP_TYPES:
            self.assertTrue(is_sideless_order_type(value), value)


class _Recorder(object):
    def __init__(self):
        self.calls = []

    def __call__(self, op_type, combo, account, code, price_type, price,
                 volume, strategy, quick, remark, context=None, *args, **kwargs):
        self.calls.append(dict(op_type=op_type, account=account, code=code,
                               price_type=price_type, price=price, volume=volume))


class GatewayForwardsTheOpTypeTest(unittest.TestCase):
    def _submit(self, order_type, account="acct"):
        recorder = _Recorder()
        gateway = BigQmtOrderGateway(account_id=account, passorder_func=recorder,
                                     context_info=object())
        gateway.submit(OrderRequest(
            signal_id="s", account_id=account, stock_code="113050.SH", action="SELL",
            volume=10, price=0.0, price_type="LIMIT", strategy_name="s",
            order_type=order_type))
        return recorder.calls[0]

    def test_each_number_reaches_passorder_untouched(self):
        for op in (80, 81, 82, 83):
            call = self._submit(op)
            self.assertEqual(op, call["op_type"])
            self.assertEqual(10, call["volume"])
            self.assertEqual(0.0, call["price"])
            self.assertEqual("113050.SH", call["code"])

    def test_the_stock_account_gate_does_not_apply(self):
        """Unlike futures/option opTypes, 80-83 need no FUTURE/STOCK_OPTION
        account: a STOCK account is exactly where 转股 happens."""
        self.assertEqual(80, self._submit(80)["op_type"])


class RpcAcceptsTheTypeTest(unittest.TestCase):
    def test_it_is_forwarded_and_sideless(self):
        handlers = BigQmtRpcHandlers.__new__(BigQmtRpcHandlers)
        for op in (80, 81, 82, 83):
            self.assertEqual(op, handlers._forwarded_order_type({"order_type": op}))
            self.assertIn(handlers._order_action_from_params({"order_type": op}), ("BUY", "SELL"))

    def test_end_to_end_through_order_stock(self):
        from test_redis_rpc import FakeMarketData, FakePositionProvider
        recorder = _Recorder()
        gateway = BigQmtOrderGateway(account_id="acct", passorder_func=recorder,
                                     context_info=object())
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True, order_settle_timeout_seconds=0.0,
            settle_orders_inline=True)
        handlers.handle("order_stock", {
            "stock_code": "128136.SZ", "order_type": 81, "order_volume": 20,
            "price_type": 11, "price": 0, "order_remark": "sellback"})
        self.assertEqual(81, recorder.calls[0]["op_type"])
        self.assertEqual(20, recorder.calls[0]["volume"])


class _Client(object):
    account_id = "acct"
    timeout_seconds = 5.0

    def __init__(self):
        self.calls = []

    def call_tracked(self, method, params=None, account_id=None, request_id=None, timeout_seconds=None):
        self.calls.append((method, dict(params or {})))
        return {"order_sys_id": "777", "user_order_id": params.get("order_remark")}


class XtTraderMethodsTest(unittest.TestCase):
    def _trader(self):
        trader = BigQmtXtTrader(account_id="acct")
        trader.client = _Client()
        return trader

    def test_convert_bond_on_a_stock_account_is_80(self):
        trader = self._trader()
        order_id = trader.convert_bond(StockAccount("acct", "STOCK"), "113050.SH", 10, "s", "r1")
        method, params = trader.client.calls[0]
        self.assertEqual("order_stock", method)
        self.assertEqual(80, params["order_type"])
        self.assertEqual(10, params["order_volume"])
        self.assertEqual(0.0, params["price"])
        self.assertEqual("113050.SH", params["stock_code"])
        self.assertEqual("r1", params["order_remark"])
        self.assertEqual(777, int(order_id))

    def test_convert_bond_on_a_credit_account_is_82(self):
        trader = self._trader()
        trader.convert_bond(StockAccount("acct", "CREDIT"), "113050.SH", 10)
        self.assertEqual(82, trader.client.calls[0][1]["order_type"])

    def test_sell_back_bond_is_81_or_83(self):
        trader = self._trader()
        trader.sell_back_bond(StockAccount("acct", "STOCK"), "128136.SZ", 5)
        trader.sell_back_bond(StockAccount("acct", "CREDIT"), "128136.SZ", 5)
        self.assertEqual([81, 83], [p["order_type"] for _, p in trader.client.calls])

    def test_order_stock_with_the_raw_number_also_works(self):
        """A caller who prefers big QMT's own numbers can pass them."""
        trader = self._trader()
        trader.order_stock(StockAccount("acct", "STOCK"), "113050.SH", 80, 10, 11, 0.0, "s", "r")
        self.assertEqual(80, trader.client.calls[0][1]["order_type"])


if __name__ == "__main__":
    unittest.main()
