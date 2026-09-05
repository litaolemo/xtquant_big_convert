# coding: utf-8
"""Route 2: a raw passthrough to QMT's native passorder.

order_stock exposes a MiniQMT-shaped subset and runs safety nets (opType
validation, code normalization, settlement read-back). Some callers want the
native 11-arg passorder instead -- full control of orderType and quickTrade,
no nets. This is that hatch. It is gated behind allow_order_methods like every
other write, and deferred to the adjust thread like every other order.

These are order-placement tests and lead the file deliberately: a wrong answer
here costs real money.
"""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway
from bigqmt_signal_trader.redis_rpc import (
    BigQmtRpcHandlers, ORDER_METHODS, METHOD_ALIASES)
from test_redis_rpc import FakeMarketData, FakePositionProvider


class RecordingPassorder(object):
    """Stands in for QMT's injected passorder: records the exact call."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)


CTX = object()      # the raw QMT ContextInfo sentinel


def _gateway():
    rec = RecordingPassorder()
    gw = BigQmtOrderGateway(
        context_info=CTX, account_id="acct", passorder_func=rec,
        combo_type=1101, price_type=11, quick_trade=2)
    return gw, rec


def _handlers(gw):
    return BigQmtRpcHandlers(
        account_id="acct", market_data=FakeMarketData(),
        position_provider=FakePositionProvider(), order_gateway=gw,
        allow_order_methods=True)


class GatewayPassthroughTest(unittest.TestCase):
    def test_forwards_all_eleven_args_verbatim_with_context_last(self):
        gw, rec = _gateway()
        out = gw.passorder_passthrough(
            op_type=23, order_type=1101, account_id="acct",
            order_code="600000.SH", price_type=11, price=10.5, volume=100,
            strategy_name="s", quick_trade=2, user_order_id="tag1")
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(
            rec.calls[0],
            (23, 1101, "acct", "600000.SH", 11, 10.5, 100, "s", 2, "tag1", CTX))
        self.assertTrue(out["submitted"])

    def test_code_is_passed_raw_without_normalization(self):
        """RAW means raw: futures case is preserved, nothing is upper-cased."""
        gw, rec = _gateway()
        gw.passorder_passthrough(0, 1101, "acct", "cu2610.SF", 11, 50000, 1,
                                 "s", 2, "t")
        self.assertEqual(rec.calls[0][3], "cu2610.SF")

    def test_dry_run_places_nothing_and_echoes_the_tuple(self):
        gw, rec = _gateway()
        out = gw.passorder_passthrough(
            24, 1123, "acct", "000001.SZ", 5, -1, 200, "s", 1, "t",
            dry_run=True)
        self.assertEqual(rec.calls, [], "dry_run must not call passorder")
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["args"],
                         [24, 1123, "acct", "000001.SZ", 5, -1.0, 200, "s", 1, "t"])
        self.assertTrue(out["context_info_supplied"])

    def test_no_passorder_available_raises(self):
        gw = BigQmtOrderGateway(context_info=CTX, account_id="acct",
                                passorder_func=None)
        with self.assertRaises(RuntimeError):
            gw.passorder_passthrough(23, 1101, "acct", "600000.SH", 11, 1, 1,
                                     "s", 2, "t")


class HandlerRoutingTest(unittest.TestCase):
    def test_handler_forwards_native_and_spelled_names(self):
        gw, rec = _gateway()
        _handlers(gw).handle("passorder", {
            "opType": 23, "orderType": 1101, "account_id": "acct",
            "orderCode": "600000.SH", "prType": 11, "price": 10.5,
            "volume": 100, "strategyName": "s", "quickTrade": 2,
            "userOrderId": "tag1"})
        self.assertEqual(
            rec.calls[0],
            (23, 1101, "acct", "600000.SH", 11, 10.5, 100, "s", 2, "tag1", CTX))

    def test_handler_fills_order_type_and_quick_trade_from_config(self):
        gw, rec = _gateway()
        _handlers(gw).handle("passorder", {
            "op_type": 23, "account_id": "acct", "order_code": "600000.SH",
            "price_type": 11, "price": 10.5, "volume": 100})
        # order_type -> combo_type 1101, quick_trade -> 2, strategy -> default
        self.assertEqual(rec.calls[0][1], 1101)
        self.assertEqual(rec.calls[0][8], 2)

    def test_handler_dry_run(self):
        gw, rec = _gateway()
        out = _handlers(gw).handle("passorder", {
            "op_type": 23, "account_id": "acct", "order_code": "600000.SH",
            "price_type": 11, "price": 10.5, "volume": 100, "dry_run": True})
        self.assertEqual(rec.calls, [])
        self.assertTrue(out["dry_run"])

    def test_missing_required_args_raise_before_any_call(self):
        gw, rec = _gateway()
        h = _handlers(gw)
        with self.assertRaises(ValueError):
            h.handle("passorder", {"account_id": "acct", "order_code": "x",
                                   "price_type": 11, "price": 1})  # no volume
        with self.assertRaises(ValueError):
            h.handle("passorder", {"op_type": 23, "account_id": "acct",
                                   "price_type": 11, "price": 1, "volume": 1})
        self.assertEqual(rec.calls, [])


class GateTest(unittest.TestCase):
    def test_passorder_is_an_order_method(self):
        self.assertIn("passorder", ORDER_METHODS)

    def test_blocked_when_order_methods_disabled(self):
        """Same gate as submit_order: not in allowed_methods, so 'not allowed'
        fires before it can reach QMT."""
        gw, rec = _gateway()
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gw,
            allow_order_methods=False)
        with self.assertRaises(ValueError):
            handlers.handle("passorder", {
                "op_type": 23, "account_id": "acct", "order_code": "600000.SH",
                "price_type": 11, "price": 1, "volume": 100})
        self.assertEqual(rec.calls, [], "a blocked order must not reach QMT")

    def test_not_deferred_inline_on_listener_wildcard(self):
        """An order must not run on the listener thread even under '*'."""
        from bigqmt_signal_trader.redis_rpc import READ_METHODS
        self.assertNotIn("passorder", READ_METHODS)


if __name__ == "__main__":
    unittest.main()
