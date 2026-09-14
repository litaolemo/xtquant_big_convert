# coding: utf-8
"""The reported burst, end to end over real zmq sockets (#303).

Real ROUTER/DEALER on loopback, the real service and handlers, a gateway
whose passorder takes 50ms and whose order id shows up 30ms later, and an
adjust tick every 30ms with a 100ms drain budget. Twenty-four callers fire
order_stock at once with a 1s timeout -- more than the serial passorder can
place in a second, exactly like 32 x 200ms against 6s.

What must hold, and did not before:

  * no caller is left with a bare timeout: each gets its order id, or a
    RequestExpired refusal, before its own deadline;
  * every order that WAS placed was answered to its caller as placed --
    the count of passorders equals the count of OK replies, so nothing was
    placed behind a caller's back;
  * the adjust thread was never held for the whole batch.

No QMT, no network beyond loopback, no real orders.
"""
import os
import sys
import threading
import time
import unittest
import uuid


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.models import OrderSnapshot, OrderSubmitResult  # noqa: E402
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers, RedisPubSubRpcService  # noqa: E402
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway  # noqa: E402
from bigqmt_signal_trader.transports.zmq_transport import ZmqTransport  # noqa: E402

from test_redis_rpc import FakeMarketData, FakePositionProvider  # noqa: E402

try:
    import zmq  # noqa: F401
except ImportError:  # pragma: no cover - pyzmq is optional for the suite
    zmq = None

CALLERS = 24
CLIENT_TIMEOUT = 1.0
PASSORDER = 0.05
REVEAL = 0.03
TICK = 0.03
BUDGET = 0.10


class _Gateway(DryRunOrderGateway):
    def __init__(self):
        super(_Gateway, self).__init__()
        self.rows = []
        self.lock = threading.Lock()

    def submit(self, request):
        time.sleep(PASSORDER)
        with self.lock:
            self.submitted.append(request)
            sysid = "%d" % (1000 + len(self.submitted))
            self.rows.append((time.time() + REVEAL, OrderSnapshot(
                order_sys_id=sysid, user_order_id=request.remark,
                stock_code=request.stock_code, action=request.action,
                volume=request.volume, traded_volume=0, status="50",
                price=request.price, order_time=int(time.time()))))
        return OrderSubmitResult(status="SUBMITTED", user_order_id=request.remark,
                                 order_sys_id=None, message="")

    def query_orders(self, account_id, strategy_name):
        now = time.time()
        with self.lock:
            return [row for at, row in self.rows if at <= now]


@unittest.skipIf(zmq is None, "pyzmq not installed")
class ZmqOrderBurstTest(unittest.TestCase):
    def setUp(self):
        self.address = "tcp://127.0.0.1:%d" % (17000 + os.getpid() % 900)
        self.gateway = _Gateway()
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=self.gateway,
            allow_order_methods=True)
        self.server = ZmqTransport(bind_address=self.address, account_id="acct", print_prefix="[srv]")
        self.service = RedisPubSubRpcService(
            redis_client=None, handlers=handlers, account_id="acct",
            process_in_listener=True, listener_methods=("*",), background_threads=True,
            transport=self.server, expire_margin_seconds=0.2)
        self.service.start()
        self.stop = threading.Event()
        self.tick_ms = []
        self.adjust = threading.Thread(target=self._adjust_loop)
        self.adjust.daemon = True
        self.adjust.start()
        time.sleep(0.2)
        self.client = ZmqTransport(connect_address=self.address, account_id="acct", print_prefix="[cli]")

    def tearDown(self):
        self.stop.set()
        self.adjust.join(2.0)
        self.client.stop()
        self.service.stop()

    def _adjust_loop(self):
        while not self.stop.is_set():
            started = time.perf_counter()
            self.service.drain_request_queue(max_items=20)
            self.service.drain_pending(max_items=20, budget_seconds=BUDGET)
            self.tick_ms.append((time.perf_counter() - started) * 1000.0)
            time.sleep(TICK)

    def _one(self, index, results):
        request = {"schema_version": 1, "request_id": uuid.uuid4().hex, "account_id": "acct",
                   "method": "order_stock", "ttl_seconds": 60,
                   "timeout_seconds": CLIENT_TIMEOUT,
                   "params": {"stock_code": "600000.SH", "order_type": 23, "order_volume": 100,
                              "price_type": 11, "price": 10.0, "strategy_name": "",
                              "order_remark": "burst_%d" % index}}
        started = time.perf_counter()
        try:
            response = self.client.send_request(request, CLIENT_TIMEOUT)
        except Exception as exc:
            results[index] = ("timeout", None, time.perf_counter() - started, str(exc))
            return
        elapsed = time.perf_counter() - started
        if response.get("ok"):
            results[index] = ("ok", (response.get("data") or {}).get("order_sys_id"), elapsed, "")
        else:
            results[index] = ("refused", None, elapsed, str(response.get("error")))

    def test_every_caller_is_answered_and_nothing_is_placed_behind_its_back(self):
        results = {}
        threads = [threading.Thread(target=self._one, args=(i, results)) for i in range(CALLERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        time.sleep(0.5)          # anything the server would still place, it places now

        kinds = [results[i][0] for i in range(CALLERS)]
        ok = kinds.count("ok")
        refused = kinds.count("refused")
        timeouts = [results[i] for i in range(CALLERS) if results[i][0] == "timeout"]

        self.assertEqual([], timeouts, "callers left with a bare timeout: %r" % timeouts)
        self.assertGreater(refused, 0, "the burst did not overload the serial passorder")
        self.assertEqual(ok + refused, CALLERS)
        self.assertEqual(ok, len(self.gateway.submitted),
                         "orders placed without an OK reply to their caller")
        for i in range(CALLERS):
            kind, sysid, _elapsed, error = results[i]
            if kind == "ok":
                self.assertTrue(sysid, "an OK reply without an order id")
            else:
                self.assertIn("RequestExpired", error)
                self.assertIn("NOT dispatched", error)
        ids = [results[i][1] for i in range(CALLERS) if results[i][0] == "ok"]
        self.assertEqual(len(ids), len(set(ids)), "two callers got the same order id")

    def test_the_first_reply_does_not_wait_for_the_batch(self):
        """Early orders answer while later ones are still running."""
        results = {}
        threads = [threading.Thread(target=self._one, args=(i, results)) for i in range(CALLERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        ok_latencies = sorted(results[i][2] for i in range(CALLERS) if results[i][0] == "ok")
        self.assertGreaterEqual(len(ok_latencies), 4)
        self.assertLess(ok_latencies[0], ok_latencies[-1] * 0.5,
                        "first %.0fms, last %.0fms: replies left together"
                        % (ok_latencies[0] * 1000, ok_latencies[-1] * 1000))
        self.assertLess(max(self.tick_ms), (BUDGET + PASSORDER) * 1000 + 250,
                        "an adjust tick held the thread %.0fms" % max(self.tick_ms))


if __name__ == "__main__":
    unittest.main()
