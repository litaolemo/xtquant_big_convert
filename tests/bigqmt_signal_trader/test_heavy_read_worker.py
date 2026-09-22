# coding: utf-8
"""#351: heavy reads leave the adjust thread; light ones keep their one tick.

In the adjust drain (rpc_background_threads False, the default) every
request runs on the strategy thread. That is what makes a read cost one
tick -- and what makes a 1.8s get_market_data_ex the strategy's own
tick_app stopped for 1.8s, which is why #321 took the LPOP off that thread
and, with it, the one-tick answers. The two were never in conflict:

  light read   -> adjust thread, answered inline, one tick
  heavy read   -> one worker thread; the reply is parked and SENT by the
                  adjust thread on its next tick (a zmq ROUTER socket / pipe
                  handle is not shared between threads)
  trade query  -> adjust thread, as always (LISTENER_DEFERRED_METHODS)

"Heavy" is by method (financial data, formulas) or by size (market token,
> threshold codes, tick period, date window). Downloads are NOT heavy here:
measured live they hold the GIL for the whole call, so the worker would buy
the tick nothing and cost the reply a tick or two.
"""

import json
import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import (  # noqa: E402
    HEAVY_READ_METHODS,
    LISTENER_DEFERRED_METHODS,
    ORDER_METHODS,
    READ_METHODS,
    BigQmtRpcHandlers,
    RedisPubSubRpcService,
)

from test_redis_rpc import (  # noqa: E402  -- the established fakes
    FakeMarketData,
    FakePositionProvider,
    FakeRedis,
)


class _ThreadNamingMarketData(FakeMarketData):
    """Records which thread each read ran on."""

    def __init__(self):
        self.threads = {}

    def get_market_data_ex(self, **kwargs):
        self.threads["get_market_data_ex"] = threading.current_thread().name
        return super(_ThreadNamingMarketData, self).get_market_data_ex(**kwargs)

    def get_ticks(self, codes):
        self.threads["get_ticks"] = threading.current_thread().name
        return super(_ThreadNamingMarketData, self).get_ticks(codes)

    def get_financial_data(self, *args, **kwargs):
        self.threads["get_financial_data"] = threading.current_thread().name
        return {"financial": True}


def _service(market_data=None, **kwargs):
    redis_client = FakeRedis()
    handlers = BigQmtRpcHandlers(
        account_id="acct", market_data=market_data or _ThreadNamingMarketData(),
        position_provider=FakePositionProvider(), order_gateway=None,
        allow_order_methods=False)
    options = dict(process_in_listener=True, listener_methods=("*",),
                   background_threads=False)
    options.update(kwargs)
    service = RedisPubSubRpcService(redis_client, handlers, account_id="acct", **options)
    return redis_client, service


def _request(method, params=None, request_id="r1", **extra):
    request = {"schema_version": 1, "request_id": request_id, "account_id": "acct",
               "method": method, "params": params or {},
               "reply_key": "resp:%s" % request_id}
    request.update(extra)
    return request


def _wait(predicate, seconds=5.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class HeavyClassificationTest(unittest.TestCase):
    def setUp(self):
        _, self.service = _service()

    def test_method_sets_are_reads_and_never_trade_context(self):
        self.assertTrue(HEAVY_READ_METHODS <= READ_METHODS)
        self.assertFalse(HEAVY_READ_METHODS & LISTENER_DEFERRED_METHODS)
        self.assertFalse(HEAVY_READ_METHODS & ORDER_METHODS)

    def test_financials_and_formulas_are_heavy_whatever_the_args(self):
        for method in ("get_financial_data", "get_raw_financial_data", "call_formula",
                       "gen_factor_index", "get_factor_data"):
            self.assertTrue(self.service.is_heavy_read(method, {"stock_list": ["600000.SH"]}), method)

    def test_downloads_stay_inline(self):
        # Measured live: download_history_data2 holds the GIL for its whole
        # 1.2s, so the worker buys the tick nothing and costs the reply a
        # tick or two. Inline, as in 0.3.52.
        for method in ("download_history_data2", "download_history_data",
                       "download_financial_data", "download_sector_data"):
            self.assertFalse(self.service.is_heavy_read(method, {"stock_list": ["SH"] * 100}), method)

    def test_cheap_methods_are_light(self):
        for method in ("ping", "get_instrument_detail", "get_trading_dates", "get_stock_name"):
            self.assertFalse(self.service.is_heavy_read(method, {}), method)

    def test_trade_context_and_orders_never_offload(self):
        big = {"stock_list": ["SH"] * 100, "period": "tick"}
        for method in ("query_stock_positions", "get_positions", "query_orders",
                       "submit_order", "order_stock", "cancel_order", "get_asset"):
            self.assertFalse(self.service.is_heavy_read(method, big), method)

    def test_get_full_tick_by_size(self):
        heavy = self.service.is_heavy_read
        self.assertFalse(heavy("get_full_tick", {"codes": ["600000.SH"]}))
        self.assertFalse(heavy("get_full_tick", {"codes": ["600000.SH", "000001.SZ"]}))
        self.assertTrue(heavy("get_full_tick", {"codes": ["SH"]}))
        self.assertTrue(heavy("get_full_tick", {"codes": ["SH", "SZ"], "types": ["stock"]}))
        self.assertTrue(heavy("get_full_tick", {"codes": ["600000.SH"], "types": ["all"]}))
        self.assertTrue(heavy("get_full_tick", {"codes": ["%06d.SH" % i for i in range(21)]}))
        self.assertFalse(heavy("get_full_tick", {"codes": ["%06d.SH" % i for i in range(20)]}))
        self.assertTrue(heavy("get_ticks", {"codes": ["SH"]}))  # canonical name

    def test_get_market_data_ex_by_size(self):
        heavy = self.service.is_heavy_read
        last5 = {"stock_list": ["600000.SH"], "period": "1d", "count": 5}
        self.assertFalse(heavy("get_market_data_ex", last5))
        self.assertFalse(heavy("get_market_data", last5))
        self.assertFalse(heavy("get_local_data", last5))
        self.assertTrue(heavy("get_market_data_ex", {"stock_list": ["600000.SH"], "period": "tick", "count": 5}))
        self.assertTrue(heavy("get_market_data_ex", {"stock_list": ["600000.SH"], "period": "1m",
                                                     "start_time": "20260601", "end_time": "20260922"}))
        self.assertTrue(heavy("get_market_data_ex", {"stock_list": ["600000.SH"], "period": "1m",
                                                     "start_time": "20260601", "count": -1}))
        self.assertFalse(heavy("get_market_data_ex", {"stock_list": ["600000.SH"], "period": "1m",
                                                      "start_time": "20260601", "count": 100}))
        self.assertTrue(heavy("get_market_data_ex", {"stock_list": ["%06d.SH" % i for i in range(30)],
                                                     "period": "1d", "count": 5}))

    def test_threshold_is_configurable(self):
        _, service = _service(heavy_codes_threshold=2)
        self.assertTrue(service.is_heavy_read("get_full_tick", {"codes": ["1.SH", "2.SH", "3.SH"]}))
        self.assertFalse(service.is_heavy_read("get_full_tick", {"codes": ["1.SH", "2.SH"]}))


class HeavyWorkerFlowTest(unittest.TestCase):
    def setUp(self):
        self.redis, self.service = _service()
        self.market_data = self.service.handlers.market_data
        self.service.start()
        self.assertIsNotNone(self.service._heavy_thread)
        self.assertTrue(self.service._heavy_thread.is_alive())

    def tearDown(self):
        self.service.stop()

    def test_heavy_read_runs_on_the_worker_and_replies_from_adjust(self):
        request = _request("get_financial_data",
                           {"stock_list": ["600000.SH"], "table_list": ["Balance"]}, request_id="h1")
        self.service.enqueue_payload(request)
        # Not answered inline, and not answered by the worker either: the
        # reply waits for the adjust thread.
        self.assertTrue(_wait(lambda: self.service._heavy_replies.qsize() == 1))
        self.assertNotIn("resp:h1", self.redis.kv)
        self.assertEqual(self.market_data.threads["get_financial_data"], "bigqmt-rpc-heavy")
        # The next adjust tick sends it.
        self.service.drain_pending()
        self.assertIn("resp:h1", self.redis.kv)
        response = json.loads(self.redis.kv["resp:h1"])
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["data"], {"financial": True})
        self.assertEqual(self.service._heavy_replies.qsize(), 0)

    def test_light_read_stays_inline_on_the_calling_thread(self):
        request = _request("get_full_tick", {"codes": ["600000.SH"]}, request_id="l1")
        self.service.enqueue_payload(request)
        # Synchronous: answered before enqueue_payload returned, on this thread.
        self.assertIn("resp:l1", self.redis.kv)
        self.assertEqual(self.market_data.threads["get_ticks"], threading.current_thread().name)
        self.assertEqual(self.service.heavy_queue_depth(), 0)

    def test_market_token_get_full_tick_goes_to_the_worker(self):
        request = _request("get_full_tick", {"codes": ["SH"]}, request_id="t1")
        self.service.enqueue_payload(request)
        self.assertTrue(_wait(lambda: self.service._heavy_replies.qsize() == 1))
        self.assertEqual(self.market_data.threads["get_ticks"], "bigqmt-rpc-heavy")
        self.service.drain_pending()
        self.assertIn("resp:t1", self.redis.kv)

    def test_expired_heavy_request_is_refused_on_the_worker_and_still_replied(self):
        request = _request("get_financial_data", {"stock_list": ["600000.SH"]},
                           request_id="e1", timeout_seconds=5.0)
        request["_received_at"] = time.time() - 60.0  # queued for a minute already
        self.service.enqueue_payload(request)
        self.assertTrue(_wait(lambda: self.service._heavy_replies.qsize() == 1))
        self.assertNotIn("get_financial_data", self.market_data.threads)  # never dispatched
        self.service.drain_pending()
        response = json.loads(self.redis.kv["resp:e1"])
        self.assertFalse(response["ok"])
        self.assertIn("RequestExpired", response["error"])

    def test_trade_query_never_goes_to_the_worker(self):
        request = _request("get_positions", {"account_id": "acct"}, request_id="p1")
        self.service.enqueue_payload(request)
        # Deferred to the adjust drain, exactly as before.
        self.assertEqual(self.service.heavy_queue_depth(), 0)
        self.assertEqual(self.service.pending.qsize(), 1)
        self.service.drain_pending()
        self.assertIn("resp:p1", self.redis.kv)

    def test_stop_joins_the_worker(self):
        thread = self.service._heavy_thread
        self.service.stop()
        self.assertTrue(_wait(lambda: not thread.is_alive(), 3.0))
        self.assertIsNone(self.service._heavy_thread)


class HeavyWorkerOffTest(unittest.TestCase):
    def test_heavy_offload_false_keeps_everything_inline(self):
        redis_client, service = _service(heavy_offload=False)
        service.start()
        try:
            self.assertIsNone(service._heavy_thread)
            request = _request("get_financial_data", {"stock_list": ["600000.SH"]}, request_id="i1")
            service.enqueue_payload(request)
            self.assertIn("resp:i1", redis_client.kv)
            self.assertEqual(service.handlers.market_data.threads["get_financial_data"],
                             threading.current_thread().name)
        finally:
            service.stop()

    def test_background_thread_mode_has_no_worker(self):
        # The receive thread already runs reads off the adjust thread there.
        _, service = _service(background_threads=True)
        self.assertFalse(service._should_offload(_request("get_financial_data")))
        self.assertIsNone(service._heavy_thread)


if __name__ == "__main__":
    unittest.main()
