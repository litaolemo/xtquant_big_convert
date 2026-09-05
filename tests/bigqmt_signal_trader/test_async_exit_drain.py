# coding: utf-8
"""Queued async orders must not die silently with the process (issue #156).

@kingtsi: looped order_stock_async calls -- only the first order went out,
unless a sleep kept the process alive. Reproduced live on Guojin 2.1.19.0
(2026-09-03, three deep-price 601398.SH orders):

  - exit immediately after queueing: 1 of 3 orders reached QMT
  - wait_async_orders() + grace:      3 of 3 reached QMT

The async worker is a daemon thread; interpreter exit kills it mid-queue.
stop() and an atexit hook now drain the queue first (bounded), and give
armed barriers a short grace so in-flight responses can fire.
"""

import os
import sys
import time
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader


def _queue_three(trader, slow_seconds=0.25, prefix="drain"):
    submitted = []

    def slow_order(*args, **kwargs):
        time.sleep(slow_seconds)
        remark = args[7] if len(args) > 7 else kwargs.get("order_remark")
        submitted.append(remark)
        return {"order_sys_id": "sys-%d" % len(submitted), "user_order_id": ""}

    trader.order_stock_result = slow_order

    def slow_batch(account, orders, batch_id="", idempotent=True):
        # The worker batches a backlog of >=2 into one order_stock_batch
        # (issue #181); route each item through the same slow stub so the
        # drain contract is exercised whichever path the worker took.
        results = []
        for index, item in enumerate(orders):
            single = slow_order(
                account, item.get("stock_code"), item.get("order_type"),
                item.get("order_volume"), item.get("price_type"),
                item.get("price"), item.get("strategy_name"),
                item.get("order_remark"))
            results.append({
                "index": index, "success": True,
                "order_sys_id": str(single.get("order_sys_id") or ""),
                "user_order_id": str(single.get("user_order_id") or ""),
                "code": 0, "error": "",
            })
        return results

    trader.order_stock_batch = slow_batch
    for i in range(3):
        trader.order_stock_async(
            "acct", "601398.SH", 23, 100, 5, 7.91, "strat", "%s-%d" % (prefix, i))
    return submitted


class StopDrainsQueueTest(unittest.TestCase):
    def test_stop_submits_everything_queued(self):
        trader = BigQmtXtTrader(account_id="acct")
        submitted = _queue_three(trader)

        trader.stop()

        self.assertEqual(submitted, ["drain-0", "drain-1", "drain-2"])

    def test_stop_with_nothing_queued_is_instant(self):
        trader = BigQmtXtTrader(account_id="acct")
        t0 = time.time()
        trader.stop()
        self.assertLess(time.time() - t0, 0.5)

    def test_drain_never_raises(self):
        trader = BigQmtXtTrader(account_id="acct")
        trader.wait_async_orders = mock.Mock(side_effect=RuntimeError("boom"))
        trader._drain_async_orders_on_exit()  # must not raise


class ExitGraceTest(unittest.TestCase):
    def test_grace_waits_for_armed_barriers_but_is_bounded(self):
        trader = BigQmtXtTrader(account_id="acct")
        trader._arm_order_barrier("never-answered", seq=99)

        t0 = time.time()
        trader._drain_async_orders_on_exit()
        elapsed = time.time() - t0

        self.assertGreaterEqual(
            elapsed, trader.ASYNC_EXIT_CALLBACK_GRACE_SECONDS - 0.5)
        self.assertLess(
            elapsed, trader.ASYNC_EXIT_CALLBACK_GRACE_SECONDS + 1.0)

    def test_grace_ends_early_once_barriers_release(self):
        trader = BigQmtXtTrader(account_id="acct")
        trader._arm_order_barrier("answered", seq=1)
        trader._release_order_barrier("answered", seq=1)

        t0 = time.time()
        trader._drain_async_orders_on_exit()
        self.assertLess(time.time() - t0, 1.0)

    def test_atexit_registered_once(self):
        trader = BigQmtXtTrader(account_id="acct")
        with mock.patch("atexit.register") as reg:
            trader._register_exit_drain()
            trader._register_exit_drain()
        self.assertEqual(reg.call_count, 1)


if __name__ == "__main__":
    unittest.main()
