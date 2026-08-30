"""Contract tests: MiniQMT field parity for callback objects (issue: field parity).

Covers the MiniQMT-facing contract on the bridge side:

- trade objects carry traded_id / traded_time / traded_amount / strategy_name
- order objects carry traded_price and a real order_time (server sends created_at_ts only)
- order-error objects carry order_remark (best effort)
- cancel-error objects carry order_id on the local-exception path too
- cancel responses carry cancel_result / error_msg (MiniQMT semantics)
- user callback exceptions are logged, not silently swallowed
"""

import os
import sys
import time as _time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader
from bigqmt_signal_trader.models import OrderSnapshot, TradeSnapshot
from bigqmt_signal_trader.redis_rpc import to_jsonable


class _RecordingCallback:
    def __init__(self):
        self.orders = []
        self.trades = []
        self.order_errors = []
        self.cancel_errors = []
        self.order_responses = []
        self.cancel_responses = []

    def on_stock_order(self, order):
        self.orders.append(order)

    def on_stock_trade(self, trade):
        self.trades.append(trade)

    def on_order_error(self, err):
        self.order_errors.append(err)

    def on_cancel_error(self, err):
        self.cancel_errors.append(err)

    def on_order_stock_async_response(self, resp):
        self.order_responses.append(resp)

    def on_cancel_order_stock_async_response(self, resp):
        self.cancel_responses.append(resp)


class _RaisingTradeCallback(_RecordingCallback):
    def on_stock_trade(self, trade):
        raise AttributeError("boom: missing traded_id")


class _CancelFakeClient:
    """Minimal RPC stub for the cancel paths (success / failure / exception)."""

    def __init__(self, cancel_result=None, cancel_exc=None):
        self.account_id = "acct"
        self.cancel_result = cancel_result
        self.cancel_exc = cancel_exc

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        if self.cancel_exc is not None:
            raise self.cancel_exc
        return self.cancel_result


class TradeObjectContractTest(unittest.TestCase):
    def _trader(self, client=None):
        trader = BigQmtXtTrader(account_id="acct")
        trader.client = client if client is not None else _CancelFakeClient()
        return trader

    def _trade_event(self, **overrides):
        item = {
            "event_type": "trade",
            "account_id": "acct",
            "trade_id": "t-1",
            "order_sys_id": "sys-1",
            "stock_code": "600000.SH",
            "action": "BUY",
            "volume": 100,
            "price": 10.5,
            "amount": 1050.0,
            "traded_at": "2026-08-20 10:00:00",
            "user_order_id": "rmk-1",
            "strategy_name": "strat-a",
            "created_at_ts": 1755655200.0,
        }
        item.update(overrides)
        return item

    def test_trade_object_provides_traded_id_time_amount_strategy(self):
        trader = self._trader()
        trade = trader._trade_from_dict("acct", self._trade_event())
        self.assertEqual(trade.traded_id, "t-1")
        self.assertEqual(trade.traded_time, 1755655200)
        self.assertEqual(trade.traded_amount, 1050.0)
        self.assertEqual(trade.strategy_name, "strat-a")
        self.assertEqual(trade.order_remark, "rmk-1")
        # 既有字段保持
        self.assertEqual(trade.trade_id, "t-1")
        self.assertEqual(trade.traded_volume, 100)
        self.assertEqual(trade.traded_price, 10.5)

    def test_trade_amount_falls_back_to_price_times_volume(self):
        trader = self._trader()
        trade = trader._trade_from_dict("acct", self._trade_event(amount=None))
        self.assertEqual(trade.traded_amount, 1050.0)

    def test_trade_time_falls_back_to_traded_at_string(self):
        trader = self._trader()
        item = self._trade_event(created_at_ts=None)
        trade = trader._trade_from_dict("acct", item)
        expected = int(
            _time.mktime(_time.strptime("2026-08-20 10:00:00", "%Y-%m-%d %H:%M:%S"))
        )
        self.assertEqual(trade.traded_time, expected)

    def test_trade_time_defaults_to_zero_when_missing(self):
        trader = self._trader()
        item = self._trade_event(created_at_ts=None, traded_at="")
        trade = trader._trade_from_dict("acct", item)
        self.assertEqual(trade.traded_time, 0)

    def test_order_time_prefers_order_time_field(self):
        trader = self._trader()
        order = trader._order_from_dict("acct", {"order_time": 111, "created_at_ts": 222.0})
        self.assertEqual(order.order_time, 111)

    def test_order_time_falls_back_to_created_at_ts(self):
        trader = self._trader()
        order = trader._order_from_dict("acct", {"created_at_ts": 1755655200.0})
        self.assertEqual(order.order_time, 1755655200)

    def test_order_object_provides_traded_price_without_overloading_limit_price(self):
        trader = self._trader()
        order = trader._order_from_dict(
            "acct",
            {"price": 10.1, "traded_price": 10.05, "traded_volume": 100},
        )
        self.assertEqual(order.price, 10.1)
        self.assertEqual(order.traded_price, 10.05)
        self.assertEqual(order.traded_volume, 100)


class CallbackContractTest(unittest.TestCase):
    def _trader(self, callback, client=None):
        trader = BigQmtXtTrader(account_id="acct")
        trader.client = client if client is not None else _CancelFakeClient()
        trader.callback = callback
        return trader

    def test_order_error_callback_object_has_order_remark(self):
        cb = _RecordingCallback()
        trader = self._trader(cb)
        trader._deliver_event(
            {
                "event_type": "order_error",
                "account_id": "acct",
                "order_sys_id": "sys-1",
                "error_id": -1,
                "error_msg": "bad",
                "remark": "rmk-x",
            }
        )
        self.assertEqual(len(cb.order_errors), 1)
        self.assertEqual(cb.order_errors[0].order_remark, "rmk-x")

    def test_order_error_callback_object_order_remark_empty_when_absent(self):
        cb = _RecordingCallback()
        trader = self._trader(cb)
        trader._deliver_event(
            {
                "event_type": "order_error",
                "account_id": "acct",
                "order_sys_id": "sys-1",
                "error_id": -1,
                "error_msg": "bad",
            }
        )
        self.assertEqual(len(cb.order_errors), 1)
        self.assertEqual(cb.order_errors[0].order_remark, "")

    def test_cancel_error_local_exception_carries_order_id(self):
        cb = _RecordingCallback()
        client = _CancelFakeClient(cancel_exc=RuntimeError("boom"))
        trader = self._trader(cb, client)
        trader.cancel_order_stock_async("acct", "sys-9")
        self.assertEqual(len(cb.cancel_errors), 1)
        self.assertEqual(str(cb.cancel_errors[0].order_id), "sys-9")

    def test_cancel_response_success_carries_cancel_result_and_error_msg(self):
        cb = _RecordingCallback()
        client = _CancelFakeClient(cancel_result={"success": True})
        trader = self._trader(cb, client)
        trader.cancel_order_stock_async("acct", "sys-9")
        self.assertEqual(len(cb.cancel_responses), 1)
        resp = cb.cancel_responses[0]
        self.assertEqual(resp.cancel_result, 0)
        self.assertEqual(resp.error_msg, "")
        self.assertEqual(str(resp.order_id), "sys-9")

    def test_cancel_response_failure_carries_cancel_result_and_error_msg(self):
        cb = _RecordingCallback()
        client = _CancelFakeClient(cancel_result={"success": False})
        trader = self._trader(cb, client)
        trader.cancel_order_stock_async("acct", "sys-9")
        self.assertEqual(len(cb.cancel_responses), 1)
        resp = cb.cancel_responses[0]
        self.assertNotEqual(resp.cancel_result, 0)
        self.assertNotEqual(resp.error_msg, "")

    def test_user_callback_exception_is_logged_not_silent(self):
        trader = self._trader(_RaisingTradeCallback())
        item = {
            "event_type": "trade",
            "account_id": "acct",
            "trade_id": "t-1",
            "order_sys_id": "sys-1",
            "stock_code": "600000.SH",
            "action": "BUY",
            "volume": 100,
            "price": 10.5,
            "created_at_ts": 1755655200.0,
        }
        with self.assertLogs("bigqmt.xtquant_compat", level="ERROR") as cm:
            trader._deliver_event(item)
        self.assertTrue(
            any("on_stock_trade" in msg for msg in cm.output),
            "logged messages: %r" % (cm.output,),
        )


class QueryStockTradesDefaultContractTest(unittest.TestCase):
    """客户端默认不过滤策略名。

    服务端语义 (redis_rpc._handle_query_trades): 空策略名返回账户全部成交。
    默认 "bigqmt_signal_trader" 会把用其他策略名下的成交全部过滤掉,
    无参调用 query_stock_trades(account) 依赖默认值, 应返回全部成交。
    """

    def test_default_strategy_name_is_empty_string(self):
        captured = {}

        class _Client:
            account_id = "acct"

            def call(self, method, params=None, account_id=None, timeout_seconds=None):
                captured["method"] = method
                captured["params"] = params
                return []

        trader = BigQmtXtTrader(account_id="acct")
        trader.client = _Client()
        trader.query_stock_trades("acct")

        self.assertEqual(captured["params"]["strategy_name"], "")

    def test_explicit_strategy_name_still_passed_through(self):
        captured = {}

        class _Client:
            account_id = "acct"

            def call(self, method, params=None, account_id=None, timeout_seconds=None):
                captured["params"] = params
                return []

        trader = BigQmtXtTrader(account_id="acct")
        trader.client = _Client()
        trader.query_stock_trades("acct", strategy_name="strat-a")

        self.assertEqual(captured["params"]["strategy_name"], "strat-a")


class QueryPathSerializationContractTest(unittest.TestCase):
    """服务端快照 -> RPC 序列化 -> 客户端映射 的端到端字段契约。

    锁定两条真实路径的字段取值依据：查询路径（adapters/order_bigqmt.py
    构建 OrderSnapshot/TradeSnapshot，redis_rpc.to_jsonable 序列化后到
    达客户端）；实时路径由 exec_events.normalize_*_event 的 dict 直接
    覆盖（本文件其它用例的 event dict 即按该结构构造）。
    """

    def _trader(self):
        trader = BigQmtXtTrader(account_id="acct")
        trader.client = _CancelFakeClient()
        return trader

    def test_query_trade_snapshot_survives_rpc_roundtrip(self):
        snapshot = TradeSnapshot(
            trade_id="t-9",
            order_sys_id="sys-9",
            stock_code="600000.SH",
            action="BUY",
            volume=100,
            price=10.5,
            traded_at="2026-08-20 10:00:00",
            user_order_id="rmk-9",
        )
        item = to_jsonable(snapshot)
        trade = self._trader()._trade_from_dict("acct", item)
        self.assertEqual(trade.traded_id, "t-9")
        self.assertEqual(trade.traded_volume, 100)
        self.assertEqual(trade.traded_price, 10.5)
        # 快照不带 amount -> 客户端按 价格 * 数量 估算
        self.assertEqual(trade.traded_amount, 1050.0)
        self.assertEqual(trade.order_remark, "rmk-9")
        # 快照不带 strategy_name -> 契约字段存在但为空串
        self.assertEqual(trade.strategy_name, "")

    def test_query_trade_time_parses_snapshot_traded_at(self):
        snapshot = TradeSnapshot(
            trade_id="t-9",
            order_sys_id="sys-9",
            stock_code="600000.SH",
            action="BUY",
            volume=100,
            price=10.5,
            traded_at="2026-08-20 10:00:00",
            user_order_id="",
        )
        item = to_jsonable(snapshot)
        trade = self._trader()._trade_from_dict("acct", item)
        expected = int(
            _time.mktime(_time.strptime("2026-08-20 10:00:00", "%Y-%m-%d %H:%M:%S"))
        )
        self.assertEqual(trade.traded_time, expected)

    def test_query_order_snapshot_survives_rpc_roundtrip(self):
        snapshot = OrderSnapshot(
            order_sys_id="sys-9",
            user_order_id="rmk-9",
            stock_code="600000.SH",
            action="SELL",
            volume=300,
            traded_volume=100,
            status="50",
            price=10.1,
            strategy_name="strat-a",
            remark="rmk-9",
            order_time=1755655200,
            traded_price=10.05,
            price_type=11,
        )
        item = to_jsonable(snapshot)
        order = self._trader()._order_from_dict("acct", item)
        self.assertEqual(str(order.order_id), "sys-9")
        self.assertIsInstance(order.order_id, int)      # issue #113
        self.assertEqual(order.order_time, 1755655200)
        self.assertEqual(order.traded_price, 10.05)
        self.assertEqual(order.strategy_name, "strat-a")
        self.assertEqual(order.order_remark, "rmk-9")
        self.assertEqual(order.price_type, 11)


    def test_query_trade_snapshot_with_official_fields_reaches_client(self):
        # 官方 Deal 字段经 TradeSnapshot(amount/strategy_name/traded_time) 到客户端。
        snapshot = TradeSnapshot(
            trade_id="t-9",
            order_sys_id="sys-9",
            stock_code="600000.SH",
            action="SELL",
            volume=100,
            price=10.5,
            traded_at="2026-08-20 10:00:01",
            user_order_id="rmk-9",
            amount=1055.55,
            strategy_name="strat-a",
            traded_time=1755655201,
        )
        item = to_jsonable(snapshot)
        trade = self._trader()._trade_from_dict("acct", item)
        self.assertEqual(trade.traded_amount, 1055.55)  # 真实成交额, 非估算
        self.assertEqual(trade.traded_time, 1755655201)  # 真实成交时间
        self.assertEqual(trade.strategy_name, "strat-a")


if __name__ == "__main__":
    unittest.main()
