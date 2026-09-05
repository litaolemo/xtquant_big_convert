# coding: utf-8
"""issue #181: a queued async-order backlog goes out as one batch RPC, and
user callbacks run on a dispatcher thread so they never throttle submits.

Live observation that motivated it: the client can queue 300 orders in a
second, but the worker then spent the round trip AND the user callback inline
per order -- submit, wait, callback, next submit, ... -- so a burst drained
at ~1-2 orders per second and a slow callback made it worse.
"""
import os
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import (
    BigQmtXtTrader, RpcServerRepliedError, XtQuantTraderCallback,
)


class Recorder(XtQuantTraderCallback):
    def __init__(self):
        self.responses = []
        self.errors = []
        self.lock = threading.Lock()

    def on_order_stock_async_response(self, r):
        with self.lock:
            self.responses.append(r)

    def on_order_error(self, e):
        with self.lock:
            self.errors.append(e)


def _order_kwargs(code, remark):
    return dict(stock_code=code, order_type=23, order_volume=100,
                price_type=11, price=10.0, strategy_name="s",
                order_remark=remark)


def _ok_batch(account, orders, batch_id="", idempotent=True):
    return [{
        "index": index, "success": True,
        "order_sys_id": "sys-%s" % item.get("order_remark"),
        "user_order_id": str(item.get("order_remark") or ""),
        "code": 0, "error": "",
    } for index, item in enumerate(orders)]


class BatchSelectionTest(unittest.TestCase):
    def _trader(self):
        trader = BigQmtXtTrader(account_id="acct")
        recorder = Recorder()
        trader.register_callback(recorder)
        calls = {"single": [], "batch": []}
        trader.order_stock_result = (
            lambda *a, **k: calls["single"].append(a) or
            {"order_sys_id": "sys-single", "user_order_id": ""})
        trader.order_stock_batch = (
            lambda account, orders, batch_id="", idempotent=True:
            calls["batch"].append((account, orders)) or _ok_batch(account, orders))
        return trader, recorder, calls

    def test_a_backlog_goes_out_as_one_batch(self):
        trader, rec, calls = self._trader()
        seqs = [trader.order_stock_async("acct", "60%04d.SH" % i, 23, 100, 11,
                                         10.0, "s", "r-%d" % i)
                for i in range(3)]

        self.assertTrue(trader.wait_async_orders(timeout=5.0))

        self.assertEqual(len(calls["batch"]), 1)
        self.assertEqual(calls["single"], [])
        _account, orders = calls["batch"][0]
        self.assertEqual(len(orders), 3)
        for item in orders:
            self.assertIs(item.get("wait_settlement"), False)
        self.assertEqual([r.seq for r in rec.responses], seqs)

    def test_a_lone_order_takes_the_single_path(self):
        trader, rec, calls = self._trader()
        trader.order_stock_async("acct", "601398.SH", 23, 100, 11, 10.0, "s", "r")

        self.assertTrue(trader.wait_async_orders(timeout=5.0))

        self.assertEqual(len(calls["single"]), 1)
        self.assertEqual(calls["batch"], [])
        self.assertEqual(len(rec.responses), 1)

    def test_a_server_refused_batch_falls_back_to_single_submits(self):
        """The server answered with an error: the handler raises before its
        per-item loop, so nothing ran and resubmitting is safe."""
        trader, rec, calls = self._trader()
        calls_log = []
        def refused_batch(account, orders, batch_id="", idempotent=True):
            calls_log.append(len(orders))
            raise RpcServerRepliedError("order_gateway is not configured")
        trader.order_stock_batch = refused_batch
        seqs = [trader.order_stock_async("acct", "60%04d.SH" % i, 23, 100, 11,
                                         10.0, "s", "r-%d" % i)
                for i in range(3)]

        self.assertTrue(trader.wait_async_orders(timeout=5.0))

        self.assertEqual(calls_log, [3])
        self.assertEqual(len(calls["single"]), 3, "batch loss must not lose orders")
        self.assertEqual([r.seq for r in rec.responses], seqs)

    def test_a_timed_out_batch_is_never_resubmitted(self):
        """A timeout means the batch may STILL BE RUNNING on the server.
        Resubmitting doubles the orders (live: a 100-item batch outlived the
        timeout, the fallback resubmitted, 200 orders landed). Report each
        item as unknown-outcome instead, and say the orders may be live."""
        trader, rec, calls = self._trader()

        def slow_batch(account, orders, batch_id="", idempotent=True):
            raise TimeoutError("redis rpc timeout: order_stock_batch")

        trader.order_stock_batch = slow_batch
        seqs = [trader.order_stock_async("acct", "60%04d.SH" % i, 23, 100, 11,
                                         10.0, "s", "r-%d" % i)
                for i in range(3)]

        self.assertTrue(trader.wait_async_orders(timeout=5.0))

        self.assertEqual(calls["single"], [],
                         "a timed-out batch must NOT be resubmitted")
        self.assertEqual(len(rec.errors), 3)
        self.assertEqual([e.seq for e in rec.errors], seqs)
        for err in rec.errors:
            self.assertIn("MAY BE LIVE", err.error_msg)
            self.assertNotIn("not found", err.error_msg.lower())

    def test_an_empty_batch_result_falls_back_to_single_submits(self):
        trader, rec, calls = self._trader()
        trader.order_stock_batch = lambda account, orders, batch_id="", idempotent=True: []
        seqs = [trader.order_stock_async("acct", "60%04d.SH" % i, 23, 100, 11,
                                         10.0, "s", "r-%d" % i)
                for i in range(2)]

        self.assertTrue(trader.wait_async_orders(timeout=5.0))

        # No results cannot mean "all failed" -- a retrying caller would
        # duplicate every order. They go out one at a time instead.
        self.assertEqual(len(calls["single"]), 2)
        self.assertEqual([r.seq for r in rec.responses], seqs)
        self.assertEqual(rec.errors, [])

    def test_a_failed_item_errors_alone(self):
        trader, rec, _calls = self._trader()

        def mixed_batch(account, orders, batch_id="", idempotent=True):
            results = _ok_batch(account, orders)
            results[1] = {"index": 1, "success": False, "code": -1,
                          "error": "ORDER_REJECTED", "user_order_id": "r-1"}
            return results

        trader.order_stock_batch = mixed_batch
        seqs = [trader.order_stock_async("acct", "60%04d.SH" % i, 23, 100, 11,
                                         10.0, "s", "r-%d" % i)
                for i in range(3)]

        self.assertTrue(trader.wait_async_orders(timeout=5.0))

        self.assertEqual([r.seq for r in rec.responses], [seqs[0], seqs[2]])
        self.assertEqual(len(rec.errors), 1)
        self.assertEqual(rec.errors[0].seq, seqs[1])
        self.assertIn("ORDER_REJECTED", rec.errors[0].error_msg)

    def test_a_mixed_account_backlog_is_not_batched_across_accounts(self):
        trader, rec, _calls = self._trader()
        batches = []
        singles = []

        def tracking_batch(account, orders, batch_id="", idempotent=True):
            batches.append((account, list(orders)))
            return _ok_batch(account, orders)

        trader.order_stock_batch = tracking_batch
        trader.order_stock_result = (
            lambda *a, **k: singles.append(a) or
            {"order_sys_id": "sys-1", "user_order_id": ""})
        # Two accounts interleaved: a batch under either account would place
        # the other account's orders wrong, so each account gets its own RPC.
        for account in ("acct-A", "acct-B"):
            for i in range(2):
                trader.order_stock_async(account, "60%04d.SH" % i, 23, 100, 11,
                                         10.0, "s", "%s-r-%d" % (account, i))

        self.assertTrue(trader.wait_async_orders(timeout=5.0))

        self.assertEqual(sorted(len(orders) for _a, orders in batches), [2, 2])
        self.assertEqual({a for a, _o in batches}, {"acct-A", "acct-B"})
        for account, orders in batches:
            for item in orders:
                self.assertTrue(item["order_remark"].startswith(account),
                                "an order landed in another account's batch")
        self.assertEqual(singles, [])
        self.assertEqual(len(rec.responses), 4)


class BatchTimeoutScalingTest(unittest.TestCase):
    """The batch RPC's wait must scale with N: the server runs items serially
    and per-item cost swings from ~ms to ~300ms (counter disconnected), so a
    flat 30s default turns a slow-but-alive 100-item batch into a client
    timeout -- and a retried batch doubles orders."""

    def _trader_with_capturing_client(self):
        trader = BigQmtXtTrader(account_id="acct")
        captured = []

        class StubClient:
            account_id = "acct"
            timeout_seconds = 30.0

            def call(self, method, params=None, account_id=None, timeout_seconds=None):
                captured.append((method, params, timeout_seconds))
                return []

        trader.client = StubClient()
        return trader, captured

    def test_timeout_scales_with_item_count(self):
        trader, captured = self._trader_with_capturing_client()
        orders = [{"stock_code": "60%04d.SH" % i, "order_type": 23,
                   "order_volume": 100, "price_type": 11, "price": 10.0,
                   "order_remark": "t-%d" % i} for i in range(100)]
        trader.order_stock_batch("acct", orders)

        _method, _params, timeout = captured[-1]
        self.assertGreaterEqual(timeout, 15.0 + 0.5 * 100)

    def test_small_batches_keep_the_plain_default(self):
        trader, captured = self._trader_with_capturing_client()
        orders = [{"stock_code": "600000.SH", "order_type": 23,
                   "order_volume": 100, "price_type": 11, "price": 10.0,
                   "order_remark": "t"}]
        trader.order_stock_batch("acct", orders)

        _method, _params, timeout = captured[-1]
        self.assertEqual(timeout, 30.0)

    def test_an_explicit_timeout_wins_over_the_scaling(self):
        trader, captured = self._trader_with_capturing_client()
        orders = [{"stock_code": "600000.SH", "order_type": 23,
                   "order_volume": 100, "price_type": 11, "price": 10.0,
                   "order_remark": "t"}]
        trader.order_stock_batch("acct", orders, timeout_seconds=7.0)

        _method, _params, timeout = captured[-1]
        self.assertEqual(timeout, 7.0)


class CallbackOffTheSubmitPathTest(unittest.TestCase):
    def test_a_slow_callback_does_not_hold_up_the_next_submit(self):
        trader = BigQmtXtTrader(account_id="acct")
        entered = threading.Event()
        release = threading.Event()
        submitted = []

        class SlowCallback(XtQuantTraderCallback):
            def on_order_stock_async_response(self, r):
                entered.set()
                release.wait(5.0)

        trader.register_callback(SlowCallback())

        def quick_single(*args, **kwargs):
            remark = kwargs.get("order_remark") or (args[7] if len(args) > 7 else "")
            submitted.append(remark)
            return {"order_sys_id": "sys-%s" % remark, "user_order_id": ""}

        trader.order_stock_result = quick_single
        # One at a time (the gate keeps the first submit from batching with
        # the second): submit A, wait for its callback to start blocking the
        # dispatcher, then submit B. B's submit must not wait for A's callback.
        trader.order_stock_async("acct", "600000.SH", 23, 100, 11, 10.0, "s", "first")
        self.assertTrue(entered.wait(5.0), "first callback never fired")
        trader.order_stock_async("acct", "600001.SH", 23, 100, 11, 10.0, "s", "second")

        deadline = time.time() + 2.0
        while "second" not in submitted and time.time() < deadline:
            time.sleep(0.005)
        release.set()
        self.assertIn("second", submitted,
                      "the next submit waited for the previous callback")
        self.assertTrue(trader.wait_async_orders(timeout=5.0))

    def test_stop_flushes_the_callback_dispatcher(self):
        trader = BigQmtXtTrader(account_id="acct")
        rec = Recorder()
        trader.register_callback(rec)
        trader.order_stock_batch = _ok_batch
        for i in range(3):
            trader.order_stock_async("acct", "60%04d.SH" % i, 23, 100, 11,
                                     10.0, "s", "flush-%d" % i)

        trader.stop()

        self.assertEqual(len(rec.responses), 3,
                         "stop returned with callbacks still queued")


if __name__ == "__main__":
    unittest.main()
