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


if __name__ == "__main__":
    unittest.main()
