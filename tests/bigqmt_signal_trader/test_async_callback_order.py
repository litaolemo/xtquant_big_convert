"""issue #51 A: an order's async_response must arrive before its order/trade.

The two callbacks travel different paths -- async_response fires on the submit
worker, order/trade come off the Redis pub/sub listener -- and the server pushes
the event before it answers the RPC, so the inversion is the normal case rather
than a race that shows up occasionally.

The barrier only applies to orders submitted through order_stock_async. Manual
orders, synchronous orders, and anything without a remark pass straight through.
"""

import json
import os
import queue
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader, XtQuantTraderCallback


class Recorder(XtQuantTraderCallback):
    """Records callback names in arrival order."""

    def __init__(self):
        self.seen = []
        self.lock = threading.Lock()

    def _add(self, name, obj):
        with self.lock:
            self.seen.append((name, obj))

    def on_order_stock_async_response(self, r):
        self._add("response", r)

    def on_order_error(self, e):
        self._add("error", e)

    def on_stock_order(self, o):
        self._add("order", o)

    def on_stock_trade(self, t):
        self._add("trade", t)

    def names(self):
        with self.lock:
            return [n for n, _ in self.seen]


def _event(event_type, remark="", order_sys_id="", **extra):
    payload = {
        "event_type": event_type,
        "account_id": "acct",
        "stock_code": "601398.SH",
        "order_sys_id": order_sys_id,
        "remark": remark,
        "user_order_id": remark,
        "action": "BUY",
    }
    payload.update(extra)
    return json.dumps(payload).encode("utf-8")


class AsyncCallbackOrderTest(unittest.TestCase):
    def _trader(self, submit=None):
        trader = BigQmtXtTrader(account_id="acct")
        recorder = Recorder()
        trader.register_callback(recorder)
        gate = threading.Event()

        def default_submit(*args, **kwargs):
            gate.wait(5.0)
            return {"order_sys_id": "sys-1", "user_order_id": "TAG-1"}

        trader.order_stock_result = submit or default_submit
        return trader, recorder, gate

    def _submit(self, trader, remark="TAG-1"):
        return trader.order_stock_async(
            "acct", "601398.SH", 23, 100, 11, 6.80, "s", remark)

    def test_order_event_waits_for_the_response(self):
        trader, rec, gate = self._trader()
        self._submit(trader)

        # The push beats the RPC reply -- the normal case, not a rare race.
        trader._dispatch_event(_event("order", remark="TAG-1", order_sys_id="sys-1"))
        self.assertEqual(rec.names(), [], "order was delivered before the response")

        gate.set()
        trader.wait_async_orders(timeout=5.0)

        self.assertEqual(rec.names(), ["response", "order"])

    def test_order_error_event_waits_for_the_response(self):
        # 服务端在应答 RPC 之前就推送废单事件（order_error）——不拦的话它比
        # async_response 先到（实盘实测倒挂）。order_error 走同一个屏障。
        trader, rec, gate = self._trader()
        self._submit(trader)

        trader._dispatch_event(_event("order_error", remark="TAG-1", order_sys_id="sys-1",
                                      error_msg="[COUNTER] rejected", status=57))
        self.assertEqual(rec.names(), [], "order_error was delivered before the response")

        gate.set()
        trader.wait_async_orders(timeout=5.0)

        self.assertEqual(rec.names(), ["response", "error"])

    def test_response_carries_real_order_id_learned_from_events(self):
        # issue #72: 委托号异步分配，RPC 应答时通常还没有（order_sys_id 空）。
        # response 触发前等屏障从暂存的委托事件学到委托号，order_id 必须是真实
        # 委托号而不是 remark。
        trader, rec, gate = self._trader(
            submit=lambda *a, **k: (gate.wait(5.0), {"order_sys_id": "", "user_order_id": "TAG-1"})[1]
        )
        self._submit(trader)

        # 委托事件先到（携带委托号），被屏障暂存并学到 sysid
        trader._dispatch_event(_event("order", remark="TAG-1", order_sys_id="xt-real-1"))
        gate.set()
        trader.wait_async_orders(timeout=5.0)

        self.assertEqual(rec.names(), ["response", "order"])
        resp = rec.seen[0][1]
        self.assertEqual(str(resp.order_id), "xt-real-1")
        self.assertEqual(resp.order_sysid, "xt-real-1")

    def test_response_falls_back_to_remark_when_no_id_learned(self):
        # 没有任何事件（极端慢/拒单无推送）：限时后按原样发，order_id 回落 remark
        trader, rec, gate = self._trader(
            submit=lambda *a, **k: (gate.wait(5.0), {"order_sys_id": "", "user_order_id": "TAG-1"})[1]
        )
        trader.ASYNC_SYSID_WAIT_SECONDS = 0.3  # 测试加速
        self._submit(trader)
        gate.set()
        trader.wait_async_orders(timeout=5.0)

        resp = rec.seen[0][1]
        self.assertEqual(rec.names(), ["response"])
        self.assertEqual(str(resp.order_id), "TAG-1")
        self.assertEqual(resp.order_sysid, "")



    def test_trade_without_remark_is_matched_through_the_order_sys_id(self):
        """QMT's deal row may not carry the remark, so the trade is correlated
        through the id learned from the order event."""
        trader, rec, gate = self._trader()
        self._submit(trader)

        trader._dispatch_event(_event("order", remark="TAG-1", order_sys_id="sys-1"))
        trader._dispatch_event(_event("trade", remark="", order_sys_id="sys-1"))
        self.assertEqual(rec.names(), [])

        gate.set()
        trader.wait_async_orders(timeout=5.0)

        self.assertEqual(rec.names(), ["response", "order", "trade"])

    def test_held_events_keep_their_arrival_order(self):
        trader, rec, gate = self._trader()
        self._submit(trader)

        trader._dispatch_event(_event("order", remark="TAG-1", order_sys_id="sys-1",
                                      status=50))
        trader._dispatch_event(_event("trade", remark="TAG-1", order_sys_id="sys-1",
                                      trade_id="t1"))
        trader._dispatch_event(_event("order", remark="TAG-1", order_sys_id="sys-1",
                                      status=56))

        gate.set()
        trader.wait_async_orders(timeout=5.0)

        self.assertEqual(rec.names(), ["response", "order", "trade", "order"])

    def test_unrelated_orders_are_not_held(self):
        """A manual order, or one from another process, must not be delayed."""
        trader, rec, gate = self._trader()
        self._submit(trader, remark="TAG-1")

        trader._dispatch_event(_event("order", remark="SOMEONE-ELSE",
                                      order_sys_id="sys-other"))

        self.assertEqual(rec.names(), ["order"], "an unrelated order was held")
        gate.set()
        trader.wait_async_orders(timeout=5.0)

    def test_no_barrier_without_a_remark(self):
        """Nothing to correlate on -- deliver rather than hold blindly."""
        trader, rec, gate = self._trader()
        trader.order_stock_async("acct", "601398.SH", 23, 100, 11, 6.80, "s", "")

        trader._dispatch_event(_event("order", remark="", order_sys_id="sys-1"))

        self.assertEqual(rec.names(), ["order"])
        gate.set()
        trader.wait_async_orders(timeout=5.0)

    def test_events_are_released_when_the_submit_fails(self):
        """A failed submit must not strand its events: losing them would be
        worse than delivering them out of order."""
        def failing(*args, **kwargs):
            raise RuntimeError("rpc down")

        trader, rec, _ = self._trader(submit=failing)
        self._submit(trader)
        trader.wait_async_orders(timeout=5.0)

        trader._dispatch_event(_event("order", remark="TAG-1", order_sys_id="sys-1"))

        self.assertEqual(rec.names(), ["error", "order"])

    def test_barrier_expires_so_events_are_never_stranded(self):
        trader, rec, gate = self._trader()
        trader.ASYNC_BARRIER_TIMEOUT_SECONDS = 0.2
        self._submit(trader)

        trader._dispatch_event(_event("order", remark="TAG-1", order_sys_id="sys-1"))
        self.assertEqual(rec.names(), [])

        time.sleep(0.3)
        # Any later event triggers the sweep, which releases the expired hold.
        trader._dispatch_event(_event("order", remark="OTHER", order_sys_id="sys-9"))

        self.assertIn("order", rec.names())
        self.assertEqual(len(rec.names()), 2)
        gate.set()
        trader.wait_async_orders(timeout=5.0)

    def test_response_still_first_when_the_push_arrives_late(self):
        """The already-correct ordering must not regress."""
        trader, rec, gate = self._trader()
        self._submit(trader)
        gate.set()
        trader.wait_async_orders(timeout=5.0)

        trader._dispatch_event(_event("order", remark="TAG-1", order_sys_id="sys-1"))

        self.assertEqual(rec.names(), ["response", "order"])

    def test_a_reused_remark_releases_the_previous_hold(self):
        """Remarks are not unique (grid-style strategies reuse them). Arming a
        second barrier on the same remark must release the first order's held
        events rather than drop them -- losing events is worse than order.
        And the second order's barrier must stay armed until ITS response."""
        trader, rec, gate = self._trader()
        self._submit(trader, remark="TAG-1")

        # The first order's event arrives before its response and is held.
        trader._dispatch_event(_event("order", remark="TAG-1", order_sys_id="sys-A"))
        self.assertEqual(rec.names(), [])

        # A second order reuses the remark; the held event must be released
        # immediately instead of being discarded with the superseded barrier.
        self._submit(trader, remark="TAG-1")
        self.assertEqual(rec.names(), ["order"])

        # The second order's own event still waits for ITS response: the
        # first order's response must not release a barrier it does not own.
        trader._dispatch_event(_event("order", remark="TAG-1", order_sys_id="sys-B"))
        self.assertEqual(rec.names(), ["order"])

        gate.set()
        trader.wait_async_orders(timeout=5.0)

        self.assertEqual(rec.names(), ["order", "response", "response", "order"])


if __name__ == "__main__":
    unittest.main()
