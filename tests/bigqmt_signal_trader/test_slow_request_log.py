# coding: utf-8
"""#342: a request that holds the strategy thread for seconds must say so.

``[adjust_phase] drain 2016488ms`` (#342) and, on the maintainer's own
terminal, ``drain 3540677ms`` on 2026-09-16 -- 33 and 59 minutes with the
adjust loop frozen -- carried no clue about WHICH request the thread was
inside. The drain phase logs its total; the request that ate it did not
log at all. This pins a per-request line: any handler over
``slow_request_seconds`` logs the method, the seconds and the thread, so
the next report of a frozen adjust loop names its cause.
"""

import logging
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import (  # noqa: E402
    BigQmtRpcHandlers,
    RedisPubSubRpcService,
)

from test_redis_rpc import (  # noqa: E402  -- the established fakes
    FakeMarketData,
    FakePositionProvider,
    FakeRedis,
)


class _Sleepy(FakeMarketData):
    """get_ticks that takes as long as the test says (via a patched clock)."""

    def __init__(self, clock):
        super(_Sleepy, self).__init__()
        self.clock = clock

    def get_ticks(self, codes, *args, **kwargs):
        self.clock.append(5.0)  # the handler "took" five seconds
        return super(_Sleepy, self).get_ticks(codes)


class _Clock(object):
    """perf_counter stand-in: each append advances time by that many seconds."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def append(self, seconds):
        self.now += float(seconds)


def _service(market_data, **kwargs):
    handlers = BigQmtRpcHandlers(
        account_id="acct", market_data=market_data,
        position_provider=FakePositionProvider(), order_gateway=None,
        allow_order_methods=False)
    return RedisPubSubRpcService(FakeRedis(), handlers, account_id="acct", **kwargs)


class SlowRequestLogTest(unittest.TestCase):
    def setUp(self):
        import bigqmt_signal_trader.redis_rpc as module
        self.module = module
        self.clock = _Clock()
        self._perf_counter = module.time.perf_counter
        module.time.perf_counter = self.clock
        self.records = []
        handler = logging.Handler()
        handler.emit = lambda record: self.records.append(record)
        self.handler = handler
        logging.getLogger("bigqmt").addHandler(handler)

    def tearDown(self):
        self.module.time.perf_counter = self._perf_counter
        logging.getLogger("bigqmt").removeHandler(self.handler)

    def _slow_lines(self):
        return [r.getMessage() for r in self.records if "slow request" in r.getMessage()]

    def test_slow_handler_is_named(self):
        service = _service(_Sleepy(self.clock))
        service.process_request({"request_id": "r1", "account_id": "acct",
                                 "method": "get_full_tick",
                                 "params": {"codes": ["600000.SH"]}})
        lines = self._slow_lines()
        self.assertEqual(len(lines), 1, self.records)
        self.assertIn("method=get_full_tick", lines[0])
        self.assertIn("5.0s", lines[0])
        self.assertIn("thread=", lines[0])

    def test_fast_handler_is_silent(self):
        service = _service(FakeMarketData())
        service.process_request({"request_id": "r2", "account_id": "acct",
                                 "method": "get_full_tick",
                                 "params": {"codes": ["600000.SH"]}})
        self.assertEqual(self._slow_lines(), [])

    def test_threshold_is_configurable(self):
        service = _service(_Sleepy(self.clock), slow_request_seconds=10.0)
        service.process_request({"request_id": "r3", "account_id": "acct",
                                 "method": "get_full_tick",
                                 "params": {"codes": ["600000.SH"]}})
        self.assertEqual(self._slow_lines(), [])

    # -- settlement passes (#345 follow-up) --------------------------------
    # A settle pass that misses the callback fast path scans the terminal's
    # ORDER list, and drain_pending runs up to three passes per tick. Since
    # #345 every order_stock_async is watched too, so on a busy account this
    # is the adjust-thread cost that grew. It gets the same line as a slow
    # request, with the queue sizes it was working through.

    def _park(self, service, shadow=False):
        from bigqmt_signal_trader.redis_rpc import OrderSettlement

        class _Req(object):
            account_id = "acct"
            stock_code = "600000.SH"
            action = "BUY"
            remark = "tag-1"
            strategy_name = "s"
            order_type = 23
            price = 10.0
            volume = 100

        settlement = OrderSettlement(_Req(), {"order_id": -1}, deadline=1e12, shadow=shadow)
        settlement.request = {"request_id": "o1", "account_id": "acct"}
        settlement.response = {"request_id": "o1", "account_id": "acct", "ok": False}
        (service._shadow_settlements if shadow else service._pending_settlements).put(settlement)
        return settlement

    def test_slow_settle_pass_is_named_with_its_queue_sizes(self):
        service = _service(FakeMarketData())
        clock = self.clock

        def slow_lookup(settlement, final=False, orders_cache=None):
            clock.append(3.0)  # one ORDER scan "took" three seconds
            return True

        service.handlers._apply_order_lookup = slow_lookup
        self._park(service)
        self._park(service, shadow=True)
        self.assertEqual(service.settle_pending_orders(), 2)
        lines = self._slow_lines()
        self.assertEqual(len(lines), 1, self.records)
        self.assertIn("method=settle_pending_orders[pending=1 shadow=1]", lines[0])
        self.assertIn("6.0s", lines[0])

    def test_empty_settle_pass_costs_nothing_and_is_silent(self):
        service = _service(FakeMarketData())
        service.handlers._apply_order_lookup = lambda *a, **k: self.fail("no lookup on an empty pass")
        self.assertEqual(service.settle_pending_orders(), 0)
        self.assertEqual(self._slow_lines(), [])

    def test_fast_settle_pass_is_silent(self):
        service = _service(FakeMarketData())
        service.handlers._apply_order_lookup = lambda settlement, final=False, orders_cache=None: True
        self._park(service)
        self.assertEqual(service.settle_pending_orders(), 1)
        self.assertEqual(self._slow_lines(), [])


class SlowResponseSegmentsTest(unittest.TestCase):
    """#386: the time around the handler must be visible too.

    A 60-code 40-day 5m read spent ~11.5s between ``_t_recv`` and
    ``_t_reply`` while the handler itself was fast, and no log named where
    it went -- the slow-request note covers only ``handlers.handle``. The
    reply conversion (``to_jsonable`` on ~12.7MB of bars) and the publish
    now get a segmented line when their total crosses the threshold while
    the handler alone did not.
    """

    def setUp(self):
        import bigqmt_signal_trader.redis_rpc as module
        self.module = module
        self.clock = _Clock()
        self._perf_counter = module.time.perf_counter
        module.time.perf_counter = self.clock
        self._jsonable = module.to_jsonable
        self.records = []
        handler = logging.Handler()
        handler.emit = lambda record: self.records.append(record)
        self.handler = handler
        logging.getLogger("bigqmt").addHandler(handler)

    def tearDown(self):
        self.module.time.perf_counter = self._perf_counter
        self.module.to_jsonable = self._jsonable
        logging.getLogger("bigqmt").removeHandler(self.handler)

    def _messages(self, needle):
        return [r.getMessage() for r in self.records if needle in r.getMessage()]

    def _request(self, service, request_id="r1"):
        service.process_request({"request_id": request_id, "account_id": "acct",
                                 "method": "get_full_tick",
                                 "params": {"codes": ["600000.SH"]}})

    def _patch_jsonable(self, seconds):
        real = self._jsonable
        state = {"done": False}

        def slow(value):
            # to_jsonable recurses through the module global; advance the
            # clock once, on the outermost call, not per recursion level.
            if not state["done"]:
                state["done"] = True
                self.clock.append(seconds)
            return real(value)

        self.module.to_jsonable = slow

    def test_slow_to_jsonable_is_named_with_segments(self):
        self._patch_jsonable(5.0)
        service = _service(FakeMarketData())

        self._request(service)

        lines = self._messages("slow response")
        self.assertEqual(len(lines), 1, self.records)
        self.assertIn("method=get_full_tick", lines[0])
        self.assertIn("total=5.0s", lines[0])
        self.assertIn("handle=0.0s", lines[0])
        self.assertIn("to_jsonable=5.0s", lines[0])
        self.assertIn("publish=", lines[0])
        # The handler note must stay quiet -- the handler was not slow.
        self.assertEqual(self._messages("slow request"), [])

    def test_slow_handler_does_not_double_log(self):
        self._patch_jsonable(5.0)
        service = _service(_Sleepy(self.clock))  # 5s handler AND 5s jsonable

        self._request(service)

        self.assertEqual(len(self._messages("slow request")), 1)
        self.assertEqual(self._messages("slow response"), [])

    def test_total_below_threshold_is_silent(self):
        self._patch_jsonable(5.0)
        service = _service(FakeMarketData(), slow_request_seconds=10.0)

        self._request(service)

        self.assertEqual(self._messages("slow response"), [])
        self.assertEqual(self._messages("slow request"), [])

    def test_fast_request_is_silent(self):
        service = _service(FakeMarketData())

        self._request(service)

        self.assertEqual(self.records, [])


if __name__ == "__main__":
    unittest.main()
