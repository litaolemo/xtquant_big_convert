# coding: utf-8
"""A burst of order_stock calls must not place orders their callers gave up on.

Reported over zmq: 32 threads, one order_stock each, 6s client timeout.
passorder runs one at a time on the QMT strategy thread at ~200ms, and
drain_pending took twenty requests per tick:

    conc=16   all OK, every reply at 3.0-4.0s   (16 x 200ms, replied together)
    conc=32   twenty at 1-5.6s, twelve timed out at 6.0s ...

... and those twelve were then placed anyway, seconds after the caller had
logged them as failed. Three things were wrong, and this file pins each:

1. Nothing on the server knew the caller's deadline. Now the envelope
   carries ``timeout_seconds``; the server stamps receipt time and refuses,
   without dispatching, any request it only reaches after the deadline.
   Cancels are exempt (a late cancel is harmless and still wanted).
2. The batch held the strategy thread for the whole twenty (4s), so the
   first order's reply -- ready at 0.2s -- left with the twentieth's, and
   QMT's callbacks could not land in between. ``drain_pending`` now takes a
   time budget per tick and settles mid-batch.
3. Each pending settlement ran its own get_trade_detail_data every tick.
   One settle pass now shares one snapshot per account.

And a caller whose order_stock did time out can ask ``get_request_outcome``
what became of it, so "timed out" becomes "not placed" or "here is the id".
"""

import json
import os
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.models import OrderSnapshot, OrderSubmitResult  # noqa: E402
from bigqmt_signal_trader.redis_rpc import (  # noqa: E402
    READ_METHODS,
    BigQmtRpcHandlers,
    RedisPubSubRpcService,
    call_redis_rpc,
)
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway  # noqa: E402

from test_redis_rpc import (  # noqa: E402  -- the established fakes
    FakeMarketData,
    FakePositionProvider,
    FakeRedis,
)


class _SlowGateway(DryRunOrderGateway):
    """passorder takes ``passorder_seconds``; the row (with its id) is in the
    order list ``reveal_seconds`` later. Counts every query."""

    def __init__(self, passorder_seconds=0.0, reveal_seconds=0.0):
        super(_SlowGateway, self).__init__()
        self.passorder_seconds = passorder_seconds
        self.reveal_seconds = reveal_seconds
        self.rows = []
        self.queries = 0

    def submit(self, request):
        if self.passorder_seconds:
            time.sleep(self.passorder_seconds)
        self.submitted.append(request)
        sysid = "sys-%d" % len(self.submitted)
        self.rows.append((time.time() + self.reveal_seconds, OrderSnapshot(
            order_sys_id=sysid, user_order_id=request.remark,
            stock_code=request.stock_code, action=request.action,
            volume=request.volume, traded_volume=0, status="50",
            price=request.price, order_time=int(time.time()))))
        return OrderSubmitResult(status="SUBMITTED", user_order_id=request.remark,
                                 order_sys_id=None, message="")

    def query_orders(self, account_id, strategy_name):
        self.queries += 1
        now = time.time()
        return [row for at, row in self.rows if at <= now]


def _service(gateway, settle_timeout=5.0, **kwargs):
    redis_client = FakeRedis()
    handlers = BigQmtRpcHandlers(
        account_id="acct", market_data=FakeMarketData(),
        position_provider=FakePositionProvider(), order_gateway=gateway,
        allow_order_methods=True, order_settle_timeout_seconds=settle_timeout)
    return redis_client, RedisPubSubRpcService(redis_client, handlers, account_id="acct", **kwargs)


def _order(request_id, timeout_seconds=None, received_at=None, stock="600000.SH", method="order_stock"):
    payload = {"request_id": request_id, "account_id": "acct", "method": method,
               "params": {"stock_code": stock, "order_type": 23, "order_volume": 100,
                          "price_type": 11, "price": 10.0, "strategy_name": "",
                          "order_remark": request_id}}
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds
    if received_at is not None:
        payload["_received_at"] = received_at
    return payload


def _reply(redis_client, request_id):
    raw = redis_client.kv.get("bigqmt:rpc:resp:acct:%s" % request_id)
    return json.loads(raw) if raw else None


class ExpiredRequestIsRefusedTest(unittest.TestCase):
    def test_a_request_past_its_deadline_is_refused_not_dispatched(self):
        """The reported outcome: timed out at 6s, placed at 7s. Never again."""
        gateway = _SlowGateway()
        redis_client, service = _service(gateway)
        # Received 7s ago by the server clock, caller waited 6s.
        service.enqueue_payload(_order("late", timeout_seconds=6.0, received_at=time.time() - 7.0))

        service.drain_pending()

        self.assertEqual([], gateway.submitted, "an expired order was placed")
        reply = _reply(redis_client, "late")
        self.assertFalse(reply["ok"])
        self.assertIn("RequestExpired", reply["error"])
        self.assertIn("NOT dispatched", reply["error"])

    def test_the_margin_refuses_a_little_early(self):
        """A dispatch that starts with 0.9s left will reply after the caller
        has gone (passorder + one settle tick). Refuse it while the caller
        can still hear the refusal."""
        gateway = _SlowGateway()
        redis_client, service = _service(gateway, expire_margin_seconds=1.0)
        service.enqueue_payload(_order("edge", timeout_seconds=6.0, received_at=time.time() - 5.1))

        service.drain_pending()

        self.assertEqual([], gateway.submitted)
        self.assertIn("RequestExpired", _reply(redis_client, "edge")["error"])

    def test_the_margin_never_eats_a_short_timeout(self):
        """A 2s timeout with a 1s margin would refuse at 1s; cap it at a
        quarter of the timeout."""
        gateway = _SlowGateway()
        redis_client, service = _service(gateway, expire_margin_seconds=1.0)
        service.enqueue_payload(_order("short", timeout_seconds=2.0, received_at=time.time() - 1.2))

        service.drain_pending()

        self.assertEqual(1, len(gateway.submitted), "refused with 0.8s of a 2s wait left")

    def test_a_request_inside_its_deadline_runs(self):
        gateway = _SlowGateway()
        redis_client, service = _service(gateway)
        service.enqueue_payload(_order("fresh", timeout_seconds=6.0, received_at=time.time() - 1.0))

        service.drain_pending()

        self.assertEqual(1, len(gateway.submitted))

    def test_a_request_that_states_no_timeout_is_never_refused(self):
        """Clients older than this change send none; they keep the old
        contract rather than gaining a refusal they cannot interpret."""
        gateway = _SlowGateway()
        redis_client, service = _service(gateway)
        service.enqueue_payload(_order("old-client", received_at=time.time() - 3600.0))

        service.drain_pending()

        self.assertEqual(1, len(gateway.submitted))

    def test_receipt_is_stamped_at_enqueue(self):
        """A request off the wire carries no stamp; enqueue_payload ages it
        from now, so a fresh one runs and one that then waits too long does
        not."""
        gateway = _SlowGateway()
        redis_client, service = _service(gateway)
        service.enqueue_payload(_order("stamped", timeout_seconds=6.0))

        queued = service.pending.queue[0]
        self.assertAlmostEqual(time.time(), queued["_received_at"], delta=1.0)
        service.drain_pending()
        self.assertEqual(1, len(gateway.submitted))

    def test_a_late_cancel_still_runs(self):
        """Refusing a cancel protects nothing; running it late is exactly
        what the caller who gave up on it still wants."""
        gateway = _SlowGateway()
        redis_client, service = _service(gateway)
        service.enqueue_payload({
            "request_id": "late-cancel", "account_id": "acct",
            "method": "cancel_order_stock_sysid", "params": {"order_sysid": "sys-1"},
            "timeout_seconds": 6.0, "_received_at": time.time() - 60.0})

        service.drain_pending()

        self.assertEqual(["sys-1"], [str(getattr(c, "order_sys_id", c)) for c in gateway.cancelled])

    def test_the_refusal_is_the_remembered_outcome(self):
        gateway = _SlowGateway()
        redis_client, service = _service(gateway)
        service.enqueue_payload(_order("late", timeout_seconds=6.0, received_at=time.time() - 7.0))
        service.drain_pending()

        outcome = service._request_outcome({"request_id": "late"})
        self.assertEqual("refused", outcome["state"])
        self.assertFalse(outcome["response"]["ok"])


class RequestOutcomeTest(unittest.TestCase):
    def test_it_is_a_read_so_it_answers_on_the_listener_thread(self):
        """The adjust thread is exactly what is busy when a caller needs
        this; the answer must not queue behind the batch."""
        self.assertIn("get_request_outcome", READ_METHODS)

    def test_unknown_before_the_server_saw_it(self):
        gateway = _SlowGateway()
        redis_client, service = _service(gateway)
        self.assertEqual("unknown", service._request_outcome({"request_id": "nope"})["state"])

    def test_dispatched_while_the_id_is_still_being_looked_up(self):
        gateway = _SlowGateway(reveal_seconds=60.0)
        redis_client, service = _service(gateway)
        service.enqueue_payload(_order("ord"))
        service.drain_pending()

        outcome = service._request_outcome({"request_id": "ord"})
        self.assertEqual("dispatched", outcome["state"])
        self.assertIsNone(outcome["response"]["data"]["order_sys_id"])

    def test_settled_carries_the_reply_the_caller_missed(self):
        gateway = _SlowGateway()
        redis_client, service = _service(gateway)
        service.enqueue_payload(_order("ord"))
        service.drain_pending()

        outcome = service._request_outcome({"request_id": "ord"})
        self.assertEqual("settled", outcome["state"])
        self.assertEqual("sys-1", outcome["response"]["data"]["order_sys_id"])

    def test_it_is_served_through_process_request(self):
        gateway = _SlowGateway()
        redis_client, service = _service(gateway)
        service.enqueue_payload(_order("ord"))
        service.drain_pending()

        service.process_request({"request_id": "q", "account_id": "acct",
                                 "method": "get_request_outcome",
                                 "params": {"request_id": "ord"}})
        reply = _reply(redis_client, "q")
        self.assertTrue(reply["ok"], reply["error"])
        self.assertEqual("settled", reply["data"]["state"])

    def test_it_requires_a_request_id(self):
        gateway = _SlowGateway()
        redis_client, service = _service(gateway)
        service.process_request({"request_id": "q", "account_id": "acct",
                                 "method": "get_request_outcome", "params": {}})
        self.assertFalse(_reply(redis_client, "q")["ok"])


class DrainBudgetTest(unittest.TestCase):
    def test_the_batch_stops_at_the_budget_and_leaves_the_rest_queued(self):
        gateway = _SlowGateway(passorder_seconds=0.05)
        redis_client, service = _service(gateway)
        for i in range(10):
            service.enqueue_payload(_order("o%d" % i))

        started = time.monotonic()
        processed = service.drain_pending(max_items=20, budget_seconds=0.12)
        elapsed = time.monotonic() - started

        self.assertLess(processed, 10, "the budget did not bound the batch")
        self.assertGreaterEqual(processed, 1)
        self.assertLess(elapsed, 0.6, "held the thread %.2fs" % elapsed)
        self.assertEqual(10 - processed, service.pending.qsize(), "requests were lost, not queued")

    def test_at_least_one_request_runs_however_small_the_budget(self):
        gateway = _SlowGateway(passorder_seconds=0.02)
        redis_client, service = _service(gateway)
        service.enqueue_payload(_order("o1"))
        service.enqueue_payload(_order("o2"))

        self.assertEqual(1, service.drain_pending(max_items=20, budget_seconds=0.0))
        self.assertEqual(1, service.pending.qsize())

    def test_no_budget_keeps_the_old_unbounded_batch(self):
        gateway = _SlowGateway(passorder_seconds=0.01)
        redis_client, service = _service(gateway)
        for i in range(5):
            service.enqueue_payload(_order("o%d" % i))

        self.assertEqual(5, service.drain_pending(max_items=20, budget_seconds=None))
        self.assertEqual(0, service.pending.qsize())

    def test_early_orders_reply_while_the_batch_is_still_running(self):
        """Ready at 0.05s, the first order used to leave with the last."""
        gateway = _SlowGateway(passorder_seconds=0.05)
        redis_client, service = _service(gateway, settle_interval_seconds=0.1)
        for i in range(6):
            service.enqueue_payload(_order("o%d" % i))
        replied_at = {}
        original = service._publish_response

        def publish(request, response):
            replied_at[request["request_id"]] = time.monotonic()
            original(request, response)

        service._publish_response = publish
        started = time.monotonic()
        service.drain_pending(max_items=20)
        finished = time.monotonic()

        self.assertIn("o0", replied_at)
        self.assertLess(replied_at["o0"] - started, (finished - started) * 0.75,
                        "the first reply waited for the whole batch")


class SharedSnapshotTest(unittest.TestCase):
    def test_one_settle_pass_queries_once_for_every_pending_order(self):
        gateway = _SlowGateway(reveal_seconds=60.0)     # never lands: every one polls
        redis_client, service = _service(gateway)
        for i in range(8):
            service.enqueue_payload(_order("o%d" % i))
        service.drain_pending()
        self.assertEqual(8, service.pending_settlement_count())
        gateway.queries = 0

        service.settle_pending_orders()

        self.assertEqual(1, gateway.queries, "one query per pending order per tick again")
        self.assertEqual(8, service.pending_settlement_count())

    def test_the_shared_snapshot_still_settles_each_order_to_its_own_id(self):
        gateway = _SlowGateway()
        redis_client, service = _service(gateway)
        for i in range(4):
            service.enqueue_payload(_order("o%d" % i, stock="60000%d.SH" % i))

        service.drain_pending()

        self.assertEqual(0, service.pending_settlement_count())
        for i in range(4):
            self.assertEqual("sys-%d" % (i + 1), _reply(redis_client, "o%d" % i)["data"]["order_sys_id"])

    def test_a_final_lookup_reads_fresh(self):
        """'Not in the system' is a verdict; it must not rest on a list
        fetched for another order earlier in the pass."""
        gateway = _SlowGateway()
        redis_client, service = _service(gateway, settle_timeout=0.0)
        service.enqueue_payload(_order("o0"))
        service.enqueue_payload(_order("o1"))
        service.drain_pending()

        # Both settled on their (fresh, final) lookups: two queries, not one.
        self.assertEqual(0, service.pending_settlement_count())
        self.assertEqual("sys-1", _reply(redis_client, "o0")["data"]["order_sys_id"])
        self.assertEqual("sys-2", _reply(redis_client, "o1")["data"]["order_sys_id"])
        self.assertGreaterEqual(gateway.queries, 2)

    def test_multi_account_settlements_do_not_share_a_snapshot(self):
        from bigqmt_signal_trader.models import OrderRequest, OrderSubmitResult
        from bigqmt_signal_trader.redis_rpc import OrderSettlement
        gateway = _SlowGateway()
        redis_client, service = _service(gateway)
        seen = []
        original = gateway.query_orders

        def query(account_id, strategy_name):
            seen.append(account_id)
            return original(account_id, strategy_name)

        gateway.query_orders = query
        cache = {}
        for acct in ("a1", "a2"):
            request = OrderRequest(signal_id="s", account_id=acct, action="BUY",
                                   stock_code="600000.SH", volume=100, price=1.0,
                                   price_type="LIMIT", strategy_name="", remark="r-" + acct)
            result = OrderSubmitResult(status="SUBMITTED", user_order_id="r-" + acct,
                                       order_sys_id=None, message="")
            service.handlers._apply_order_lookup(
                OrderSettlement(request, result, 0.0), final=False, orders_cache=cache)

        self.assertEqual(["a1", "a2"], seen)


class ClientEnvelopeTest(unittest.TestCase):
    def test_the_helper_client_states_its_wait(self):
        class QueueRedis(object):
            def __init__(self):
                self.queued = []
                self.kv = {"bigqmt:rpc:resp:acct:rid-1": json.dumps({"ok": True, "data": 1})}

            def rpush(self, key, value):
                self.queued.append(value)

            def expire(self, key, seconds):
                pass

            def get(self, key):
                return self.kv.get(key)

        redis_client = QueueRedis()
        call_redis_rpc(redis_client, "acct", "ping", timeout_seconds=4.5, request_id="rid-1")

        from bigqmt_signal_trader.redis_rpc import decode_rpc_request_payload
        request = json.loads(decode_rpc_request_payload(redis_client.queued[0]))
        self.assertEqual(4.5, request["timeout_seconds"])
        self.assertEqual("rid-1", request["request_id"])

    def test_the_rpc_client_states_its_wait_over_a_transport(self):
        from bigqmt_signal_trader.xtquant_compat import BigQmtRpcClient
        sent = []

        class Transport(object):
            def send_request(self, request, timeout_seconds):
                sent.append((dict(request), timeout_seconds))
                return {"ok": True, "data": None, "request_id": request["request_id"]}

        client = BigQmtRpcClient(account_id="acct", timeout_seconds=7.0)
        client._transport = lambda: Transport()
        client.call_tracked("ping", {}, request_id="rid-2")

        request, waited = sent[0]
        self.assertEqual(7.0, request["timeout_seconds"])
        self.assertEqual(7.0, waited)
        self.assertEqual("rid-2", request["request_id"])


class OrderStockAfterTimeoutTest(unittest.TestCase):
    """What the compat layer does with a timed-out order_stock."""

    def _trader(self, outcomes):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader

        class Client(object):
            account_id = "acct"
            timeout_seconds = 1.0

            def __init__(self):
                self.calls = []
                self.outcomes = list(outcomes)

            def call_tracked(self, method, params=None, account_id=None, request_id=None,
                             timeout_seconds=None):
                self.calls.append((method, request_id))
                raise TimeoutError("zmq rpc timeout: order_stock")

            def call(self, method, params=None, account_id=None, timeout_seconds=None):
                self.calls.append((method, params.get("request_id")))
                if method != "get_request_outcome":
                    raise AssertionError(method)
                outcome = self.outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        trader = BigQmtXtTrader(account_id="acct")
        trader.client = Client()
        trader.ORDER_TIMEOUT_FOLLOWUP_SECONDS = 0.3
        trader.ORDER_TIMEOUT_FOLLOWUP_INTERVAL_SECONDS = 0.01
        return trader

    def _order(self, trader):
        return trader.order_stock("acct", "600000.SH", 23, 100, 11, 10.0, "s", "r-1")

    def test_a_refused_order_says_not_placed(self):
        trader = self._trader([{"state": "refused", "response": {"ok": False, "error": "RequestExpired"}}])
        with self.assertRaises(TimeoutError) as ctx:
            self._order(trader)
        self.assertIn("did NOT place", str(ctx.exception))
        self.assertIn("refused as expired", str(ctx.exception))

    def test_an_unseen_order_says_not_placed(self):
        trader = self._trader([{"state": "unknown", "response": None}])
        with self.assertRaises(TimeoutError) as ctx:
            self._order(trader)
        self.assertIn("did NOT place", str(ctx.exception))

    def test_a_settled_order_returns_the_reply_it_missed(self):
        trader = self._trader([{"state": "settled", "response": {
            "ok": True, "server_error": "", "data": {"order_sys_id": "9", "status": "SUBMITTED"}}}])
        self.assertEqual(int(self._order(trader)), 9)

    def test_a_settled_failure_raises_like_the_original_reply_would(self):
        from bigqmt_signal_trader.xtquant_compat import RpcServerRepliedError
        trader = self._trader([{"state": "settled", "response": {
            "ok": True, "server_error": "passorder submitted but order not found in system", "data": {}}}])
        with self.assertRaises(RpcServerRepliedError):
            self._order(trader)

    def test_a_dispatched_order_is_waited_for_then_reported_live(self):
        trader = self._trader([{"state": "dispatched", "response": {"ok": True, "data": {}}}] * 100)
        with self.assertRaises(TimeoutError) as ctx:
            self._order(trader)
        self.assertIn("it is live", str(ctx.exception))
        self.assertGreater(len(trader.client.calls), 2, "did not keep asking")

    def test_a_dispatched_order_that_settles_meanwhile_returns_its_id(self):
        trader = self._trader([
            {"state": "dispatched", "response": {"ok": True, "data": {}}},
            {"state": "settled", "response": {"ok": True, "server_error": "",
                                              "data": {"order_sys_id": "3"}}}])
        self.assertEqual(int(self._order(trader)), 3)

    def test_an_old_server_falls_back_to_the_old_message(self):
        """No get_request_outcome there: the query fails, and the answer is
        the honest old one -- unknown, query before retrying."""
        trader = self._trader([RuntimeError("unknown method: get_request_outcome")])
        with self.assertRaises(TimeoutError) as ctx:
            self._order(trader)
        self.assertIn("Query orders/trades before retrying", str(ctx.exception))
        self.assertNotIn("did NOT place", str(ctx.exception))

    def test_the_outcome_is_asked_under_the_orders_own_request_id(self):
        trader = self._trader([{"state": "unknown", "response": None}])
        with self.assertRaises(TimeoutError):
            self._order(trader)
        (_, order_rid), (_, asked_rid) = trader.client.calls[:2]
        self.assertTrue(order_rid)
        self.assertEqual(order_rid, asked_rid)


if __name__ == "__main__":
    unittest.main()
