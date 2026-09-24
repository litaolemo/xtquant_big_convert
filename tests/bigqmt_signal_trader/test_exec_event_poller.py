# coding: utf-8
"""pipe / mysql 没有推送通道时，客户端轮询合成执行回调 (#372)。

通道选择本来就是每轮重探：redis 可达走 redis 频道，zmq 走推送通道，剩下
（pipe / mysql、或 redis 传输的 redis 宕了）0.3.56 之前什么都不干——
on_stock_order 一条都到不了。现在剩下那条路走查询轮询：按 sysid+status
diff 委托、按 trade_id diff 成交，合成与推送完全同形的事件。首轮只打底
不补发；查询连续失败退回外层重选。
"""

import json
import os
import sys
import threading
import time
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader, XtQuantTraderCallback  # noqa: E402


class _Row(object):
    def __init__(self, **fields):
        self.__dict__.update(fields)


class Recorder(XtQuantTraderCallback):
    def __init__(self, stop_after=None, trader=None):
        self.events = []
        self.lock = threading.Lock()
        self._stop_after = stop_after
        self._trader = trader

    def _note(self, item):
        with self.lock:
            self.events.append(item)
        if self._stop_after and len(self.events) >= self._stop_after and self._trader:
            self._trader._event_running = False

    def on_stock_order(self, order):
        self._note(("order", order.stock_code, str(order.order_status)))

    def on_stock_trade(self, trade):
        self._note(("trade", trade.stock_code, trade.trade_id))

    def on_order_error(self, err):
        self._note(("order_error", str(err.order_id), err.error_msg))

    def items(self):
        with self.lock:
            return list(self.events)


class PollClient(object):
    """query_orders / query_trades 按轮给出剧本；redis 探活恒失败。"""

    account_id = "acct"
    transport_name = "pipe"

    def __init__(self, rounds, fail=False):
        self._rounds = list(rounds)
        self._fail = fail
        self.queries = 0

    def _redis(self):
        raise RuntimeError("no redis")

    def call(self_, method, params=None, **kwargs):
        self_.queries += 1
        if self_._fail:
            raise RuntimeError("synthetic query failure")
        round_index = min((self_.queries - 1) // 2, len(self_._rounds) - 1)
        orders, trades = self_._rounds[round_index]
        if method in ("query_stock_orders", "query_orders"):
            return [dict(o) for o in orders]
        if method in ("query_stock_trades", "query_trades"):
            return [dict(t) for t in trades]
        raise AssertionError("unexpected rpc: %s" % method)


def _trader(client, recorder):
    trader = BigQmtXtTrader(account_id="acct")
    trader.client = client
    trader.callback = recorder
    return trader


O = lambda sysid, status, code="600000.SH", **kw: dict(
    {"order_sys_id": sysid, "stock_code": code, "action": "BUY", "status": status,
     "order_remark": "rmk", "price": 10.0, "volume": 100, "traded_volume": 0,
     "offset_flag": 48, **kw})
T = lambda tid, sysid, code="600000.SH": {
    "trade_id": tid, "order_sys_id": sysid, "stock_code": code, "action": "BUY",
    "volume": 100, "price": 10.0, "offset_flag": 48}


class PollSynthTest(unittest.TestCase):
    def test_first_round_is_silent_then_diff_fires(self):
        client = PollClient([
            ([O("s1", "50")], []),                        # 打底：已有 s1 不补发
            ([O("s1", "50"), O("s2", "50")], []),         # 新委托 s2
            ([O("s1", "56"), O("s2", "50")], [T("t1", "s1")]),  # s1 已成 + 成交
        ])
        recorder = Recorder(stop_after=3)
        trader = _trader(client, recorder)
        recorder._trader = trader

        trader._event_running = True
        trader._event_loop_poll()

        self.assertEqual(
            [("order", "600000.SH", "50"), ("order", "600000.SH", "56"), ("trade", "600000.SH", "t1")],
            recorder.items())

    def test_status_57_fires_order_and_order_error(self):
        client = PollClient([
            ([O("s1", "50")], []),
            ([O("s1", "57", status_msg="[COUNTER] 可用资金不足")], []),
        ])
        recorder = Recorder(stop_after=2)
        trader = _trader(client, recorder)
        recorder._trader = trader

        trader._event_running = True
        trader._event_loop_poll()

        kinds = [item[0] for item in recorder.items()]
        self.assertEqual(["order", "order_error"], kinds)
        self.assertIn("资金不足", recorder.items()[1][2])

    def test_query_failures_are_tolerated_then_return_to_reselect(self):
        client = PollClient([], fail=True)
        recorder = Recorder()
        trader = _trader(client, recorder)

        trader._event_running = True
        trader._event_loop_poll()   # 必须自己退出来（连续失败 -> 外层重选）

        self.assertEqual(5, client.queries, "5 次连败后退回外层重选通道")
        self.assertEqual([], recorder.items())


class PollSelectionTest(unittest.TestCase):
    def test_pipe_without_redis_selects_the_poller(self):
        client = PollClient([
            ([O("s1", "50")], []),
            ([O("s1", "56")], [T("t1", "s1")]),
        ])
        recorder = Recorder(stop_after=2)
        trader = _trader(client, recorder)
        recorder._trader = trader

        trader._event_running = True
        trader._event_loop()      # 通道选择：pipe 无 redis -> 轮询

        self.assertEqual(
            [("order", "600000.SH", "56"), ("trade", "600000.SH", "t1")],
            recorder.items())


if __name__ == "__main__":
    unittest.main()
