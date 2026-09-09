import json
import time
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider
from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway
from bigqmt_signal_trader.adapters.position_bigqmt import BigQmtPositionProvider
from bigqmt_signal_trader.models import (
    AssetSnapshot,
    CancelResult,
    OrderSnapshot,
    PositionSnapshot,
    TradeSnapshot,
)
from bigqmt_signal_trader.redis_rpc import (
    RPC_REVISION,
    BigQmtRpcHandlers,
    RedisPubSubRpcService,
    decode_rpc_request_payload,
    encode_rpc_request_payload,
)


class FakeRedis:
    def __init__(self):
        self.kv = {}
        self.expired = []
        self.published = []

    def setex(self, key, seconds, value):
        self.kv[key] = value
        self.expired.append((key, seconds))
        return True

    def set(self, key, value):
        self.kv[key] = value
        return True

    def publish(self, channel, value):
        self.published.append((channel, value))
        return 1


class FakeMarketData:
    def get_ticks(self, codes):
        return {codes[0]: {"lastPrice": 10.5}}

    def get_instrument(self, code):
        return {"code": code, "InstrumentStatus": 0}

    def get_market_data_ex(self, **kwargs):
        return {"params": kwargs, "data": {"600000.SH": {"close": [10.0]}}}


class FakePositionProvider:
    def get_positions(self, account_id):
        return {
            "600000.SH": PositionSnapshot(
                stock_code="600000.SH",
                volume=1000,
                available=800,
                cost=10.0,
                stock_name="PF Bank",
            )
        }

    def get_asset(self, account_id):
        return AssetSnapshot(account_id=account_id, cash=100.0, total_asset=1000.0)


class CreditCompactQueryTest(unittest.TestCase):
    def test_compact_queries_pass_configured_account_type(self):
        calls = []

        def query(*args):
            calls.append(args)
            return []

        gateway = DryRunOrderGateway()
        gateway.account_type = "credit"
        handlers = BigQmtRpcHandlers(
            account_id="acct",
            market_data=FakeMarketData(),
            position_provider=FakePositionProvider(),
            order_gateway=gateway,
            qmt_api={
                "get_unclosed_compacts": query,
                "get_closed_compacts": query,
            },
        )

        handlers._handle_query_stk_compacts({})
        handlers._handle_get_unclosed_compacts({})
        handlers._handle_get_closed_compacts({})

        self.assertEqual(
            calls,
            [
                ("acct", "CREDIT"),
                ("acct", "CREDIT"),
                ("acct", "CREDIT"),
            ],
        )


def _service(allow_order_methods=False, process_in_listener=False):
    return _service_with_listener_methods(
        allow_order_methods=allow_order_methods,
        process_in_listener=process_in_listener,
        listener_methods=None,
    )


def _service_with_listener_methods(allow_order_methods=False, process_in_listener=False, listener_methods=None):
    redis_client = FakeRedis()
    order_gateway = DryRunOrderGateway()
    handlers = BigQmtRpcHandlers(
        account_id="acct",
        market_data=FakeMarketData(),
        position_provider=FakePositionProvider(),
        order_gateway=order_gateway,
        allow_order_methods=allow_order_methods,
        # DryRunOrderGateway never registers an order, so retrying until the
        # deadline would just stall every test by the full timeout.
        order_settle_timeout_seconds=0.0,
    )
    return redis_client, RedisPubSubRpcService(
        redis_client,
        handlers,
        account_id="acct",
        process_in_listener=process_in_listener,
        listener_methods=listener_methods,
    )


class FakeOrderGateway(DryRunOrderGateway):
    def query_orders(self, account_id, strategy_name):
        return [
            OrderSnapshot(
                order_sys_id="open-1",
                user_order_id="remark-1",
                stock_code="600000.SH",
                action="BUY",
                volume=100,
                traded_volume=0,
                status="50",
            ),
            OrderSnapshot(
                order_sys_id="done-1",
                user_order_id="remark-2",
                stock_code="600000.SH",
                action="BUY",
                volume=100,
                traded_volume=100,
                status="56",
            ),
        ]


class CountingOrderGateway(DryRunOrderGateway):
    def __init__(self, existing=None, query_error=None):
        super().__init__()
        self.existing = list(existing or [])
        self.query_error = query_error
        self.submit_count = 0
        self.query_count = 0

    def query_orders_strict(self, _account_id, _strategy_name):
        self.query_count += 1
        if self.query_error:
            raise self.query_error
        return list(self.existing)

    def submit(self, request):
        self.submit_count += 1
        return super().submit(request)


class CapturingTradeGateway(DryRunOrderGateway):
    def __init__(self):
        super().__init__()
        self.strategy_names = []

    def query_trades(self, account_id, strategy_name):
        self.strategy_names.append((account_id, strategy_name))
        return []


class LandingOrderGateway(DryRunOrderGateway):
    """模拟 QMT：passorder 异步落地，委托号稍后出现在查询结果里。"""

    def __init__(self, landed=True):
        super().__init__()
        self.landed = landed
        self.orders = []

    def submit(self, request):
        result = super().submit(request)
        if self.landed:
            self.orders.append(
                OrderSnapshot(
                    order_sys_id="sysid-1",
                    user_order_id=str(request.remark or ""),
                    stock_code=request.stock_code,
                    action=request.action,
                    volume=request.volume,
                    traded_volume=0,
                    status="50",
                )
            )
        return result

    def query_orders(self, account_id, strategy_name):
        return list(self.orders)


class CapturingExecutionGateway(CapturingTradeGateway):
    def __init__(self):
        super().__init__()
        self.order_strategy_names = []

    def query_orders(self, account_id, strategy_name):
        self.order_strategy_names.append((account_id, strategy_name))
        return [
            OrderSnapshot(
                order_sys_id="order-1", user_order_id="tag-1", stock_code="600276.SH",
                action="SELL", volume=100, traded_volume=0, status="50", price=55.0,
            )
        ]

    def query_trades(self, account_id, strategy_name):
        self.strategy_names.append((account_id, strategy_name))
        return [
            TradeSnapshot(
                trade_id="trade-1", order_sys_id="order-1", stock_code="600276.SH",
                action="SELL", volume=100, price=55.0,
            )
        ]


def _service_with_order_gateway(order_gateway, allow_order_methods=False):
    redis_client = FakeRedis()
    handlers = BigQmtRpcHandlers(
        account_id="acct",
        market_data=FakeMarketData(),
        position_provider=FakePositionProvider(),
        order_gateway=order_gateway,
        allow_order_methods=allow_order_methods,
        order_settle_timeout_seconds=0.0,
    )
    return redis_client, RedisPubSubRpcService(redis_client, handlers, account_id="acct")


class LateLandingOrderGateway(DryRunOrderGateway):
    """QMT assigns the order id asynchronously: the order only becomes visible
    after ``appear_after`` lookups."""

    def __init__(self, appear_after=2, never=False):
        super().__init__()
        self.appear_after = appear_after
        self.never = never
        self.lookups = 0
        self._request = None

    def submit(self, request):
        self._request = request
        return super().submit(request)

    def query_orders(self, account_id, strategy_name):
        self.lookups += 1
        if self.never or self.lookups < self.appear_after or self._request is None:
            return []
        return [
            OrderSnapshot(
                order_sys_id="sysid-late",
                user_order_id=str(self._request.remark or ""),
                stock_code=self._request.stock_code,
                action=self._request.action,
                volume=self._request.volume,
                traded_volume=0,
                status="50",
            )
        ]


class FalseyCancelGateway(DryRunOrderGateway):
    """Full QMT #148: native cancel is falsey before status confirms success."""

    def __init__(self, statuses, native_success=False, query_error=None):
        super().__init__()
        self.statuses = list(statuses)
        self.native_success = native_success
        self.query_error = query_error
        self.lookups = 0

    def cancel(self, order_ref, account_id=None):
        self.cancelled.append(order_ref)
        return CancelResult(
            success=self.native_success,
            message="" if self.native_success else "cancel returned false",
        )

    def query_orders(self, account_id, strategy_name):
        self.lookups += 1
        if self.query_error is not None:
            raise self.query_error
        if not self.statuses:
            return []
        status = self.statuses[min(self.lookups - 1, len(self.statuses) - 1)]
        return [
            OrderSnapshot(
                order_sys_id="cancel-1",
                user_order_id="remark-cancel-1",
                stock_code="510050.SH",
                action="BUY",
                volume=100,
                traded_volume=0,
                status=status,
            )
        ]


class AsyncCancelSettlementTest(unittest.TestCase):
    """issue #148: order status, not cancel() truthiness, is authoritative."""

    @staticmethod
    def _service(gateway, timeout=5.0):
        redis_client = FakeRedis()
        handlers = BigQmtRpcHandlers(
            account_id="acct",
            market_data=FakeMarketData(),
            position_provider=FakePositionProvider(),
            order_gateway=gateway,
            allow_order_methods=True,
            order_settle_timeout_seconds=timeout,
        )
        return redis_client, RedisPubSubRpcService(
            redis_client, handlers, account_id="acct")

    @staticmethod
    def _cancel(service, request_id="cancel-request-1"):
        service.enqueue_payload({
            "request_id": request_id,
            "account_id": "acct",
            "method": "cancel_order_stock_sysid",
            "params": {"order_sysid": "cancel-1"},
        })

    def test_falsey_native_return_waits_for_status_54_and_reports_success(self):
        gateway = FalseyCancelGateway(["50", "54"])
        redis_client, service = self._service(gateway)
        self._cancel(service)

        service.drain_pending()
        self.assertNotIn(
            "bigqmt:rpc:resp:acct:cancel-request-1", redis_client.kv)
        self.assertEqual(service.pending_settlement_count(), 1)

        service.drain_pending()

        response = json.loads(
            redis_client.kv["bigqmt:rpc:resp:acct:cancel-request-1"])
        self.assertTrue(response["ok"], response["error"])
        self.assertTrue(response["data"]["success"])
        self.assertEqual(response["data"]["message"], "")
        self.assertEqual(gateway.lookups, 2)
        self.assertEqual(len(gateway.cancelled), 1)

    def test_partial_cancel_status_53_also_reports_success(self):
        gateway = FalseyCancelGateway(["53"])
        redis_client, service = self._service(gateway)
        self._cancel(service)

        service.drain_pending()

        response = json.loads(
            redis_client.kv["bigqmt:rpc:resp:acct:cancel-request-1"])
        self.assertTrue(response["data"]["success"])

    def test_terminal_filled_status_does_not_become_cancel_success(self):
        gateway = FalseyCancelGateway(["56"])
        redis_client, service = self._service(gateway)
        self._cancel(service)

        service.drain_pending()

        response = json.loads(
            redis_client.kv["bigqmt:rpc:resp:acct:cancel-request-1"])
        self.assertTrue(response["ok"], response["error"])
        self.assertFalse(response["data"]["success"])
        self.assertIn("reached status 56", response["data"]["message"])

    def test_active_order_at_deadline_remains_cancel_failure(self):
        gateway = FalseyCancelGateway(["50"])
        redis_client, service = self._service(gateway, timeout=0.0)
        self._cancel(service)

        service.drain_pending()

        response = json.loads(
            redis_client.kv["bigqmt:rpc:resp:acct:cancel-request-1"])
        self.assertFalse(response["data"]["success"])
        self.assertIn("is still status 50", response["data"]["message"])

    def test_lookup_error_at_deadline_remains_cancel_failure(self):
        gateway = FalseyCancelGateway(
            ["54"], query_error=RuntimeError("QMT query unavailable"))
        redis_client, service = self._service(gateway, timeout=0.0)
        self._cancel(service)

        service.drain_pending()

        response = json.loads(
            redis_client.kv["bigqmt:rpc:resp:acct:cancel-request-1"])
        self.assertFalse(response["data"]["success"])
        self.assertIn("cancel status lookup failed", response["data"]["message"])

    def test_truthy_native_return_verified_by_immediate_lookup(self):
        # #151: truthy is no more trustworthy than falsey (a cancel of an
        # order that does not exist returns success=True). The fast path
        # survives, but only through one immediate status lookup -- not by
        # believing the native return.
        gateway = FalseyCancelGateway(["54"], native_success=True)
        redis_client, service = self._service(gateway)
        self._cancel(service)

        service.drain_pending()

        response = json.loads(
            redis_client.kv["bigqmt:rpc:resp:acct:cancel-request-1"])
        self.assertTrue(response["data"]["success"])
        self.assertEqual(gateway.lookups, 1)
        self.assertEqual(service.pending_settlement_count(), 0)

    def test_truthy_native_return_without_confirmation_is_not_success(self):
        # The #151 shape exactly: native success=True, order still active
        # at the deadline -> the reply must not be a bare success.
        gateway = FalseyCancelGateway(["50"], native_success=True)
        redis_client, service = self._service(gateway, timeout=0.0)
        self._cancel(service)

        service.drain_pending()

        response = json.loads(
            redis_client.kv["bigqmt:rpc:resp:acct:cancel-request-1"])
        self.assertFalse(response["data"]["success"])
        self.assertIn("is still status 50", response["data"]["message"])


class AsyncOrderSettlementTest(unittest.TestCase):
    """issue #44: passorder must not hold the QMT adjust thread.

    The old path slept 0.5s inline, serializing every other request behind each
    order and capping throughput at ~2 orders/sec.
    """

    def _service(self, gateway, timeout=5.0):
        redis_client = FakeRedis()
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True, order_settle_timeout_seconds=timeout,
        )
        return redis_client, RedisPubSubRpcService(redis_client, handlers, account_id="acct")

    def _submit(self, service, request_id="ord-1"):
        service.enqueue_payload({
            "request_id": request_id, "account_id": "acct", "method": "order_stock",
            "params": {"stock_code": "600000.SH", "order_type": 23, "order_volume": 100,
                       "price_type": 11, "price": 10.1, "order_remark": request_id},
        })

    def test_drain_does_not_sleep_waiting_for_the_order_id(self):
        """The whole point: no 0.5s block on the adjust thread."""
        gateway = LateLandingOrderGateway(appear_after=99)
        redis_client, service = self._service(gateway)
        self._submit(service)

        started = time.monotonic()
        service.drain_pending()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.2, "drain blocked for %.3fs" % elapsed)

    def test_unresolved_order_is_parked_not_answered(self):
        gateway = LateLandingOrderGateway(appear_after=99)
        redis_client, service = self._service(gateway)
        self._submit(service)
        service.drain_pending()

        self.assertNotIn("bigqmt:rpc:resp:acct:ord-1", redis_client.kv)
        self.assertEqual(service.pending_settlement_count(), 1)

    def test_later_tick_settles_and_backfills_the_sysid(self):
        gateway = LateLandingOrderGateway(appear_after=3)
        redis_client, service = self._service(gateway)
        self._submit(service)

        for _ in range(5):
            service.drain_pending()
            if "bigqmt:rpc:resp:acct:ord-1" in redis_client.kv:
                break

        response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:ord-1"])
        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(response["data"]["order_sys_id"], "sysid-late")
        self.assertEqual(response["server_error"], "")
        self.assertEqual(service.pending_settlement_count(), 0)

    def test_order_that_never_lands_reports_after_the_deadline(self):
        gateway = LateLandingOrderGateway(never=True)
        redis_client, service = self._service(gateway, timeout=0.0)
        self._submit(service)
        service.drain_pending()

        response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:ord-1"])
        self.assertTrue(response["ok"])
        self.assertIn("not found in system", response["server_error"])

    def test_native_lookup_error_keeps_submission_without_resubmitting(self):
        submissions = []
        queries = []

        def query(*args):
            queries.append(args)
            raise RuntimeError("simulated native ORDER query failure")

        gateway = BigQmtOrderGateway(
            context_info=None,
            passorder_func=lambda *args: submissions.append(args),
            get_trade_detail_data_func=query,
        )
        redis_client, service = self._service(gateway)
        self._submit(service)
        service.drain_pending()

        response_key = "bigqmt:rpc:resp:acct:ord-1"
        self.assertIn(response_key, redis_client.kv)
        response = json.loads(redis_client.kv[response_key])
        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(response["data"]["status"], "SUBMITTED")
        self.assertIsNone(response["data"]["order_sys_id"])
        self.assertEqual(response["data"]["message"], "passorder submitted")
        self.assertEqual(response["server_error"], "")
        self.assertEqual(service.pending_settlement_count(), 0)

        service.drain_pending()
        service.drain_pending()
        self.assertEqual(len(submissions), 1)
        self.assertEqual(queries, [("acct", "STOCK", "ORDER", "")])

    def test_settlement_error_does_not_leak_into_other_responses(self):
        """issue #43 again, by another route: settling happens long after the
        order left the handler, so its diagnostic must ride on the settlement
        rather than handlers._last_server_error."""
        gateway = LateLandingOrderGateway(never=True)
        redis_client, service = self._service(gateway, timeout=0.0)
        self._submit(service)
        service.drain_pending()
        self.assertIn("not found in system",
                      json.loads(redis_client.kv["bigqmt:rpc:resp:acct:ord-1"])["server_error"])

        service.enqueue_payload({"request_id": "later-ping", "account_id": "acct",
                                 "method": "ping", "params": {}})
        service.drain_pending()

        self.assertEqual(
            json.loads(redis_client.kv["bigqmt:rpc:resp:acct:later-ping"])["server_error"], "")

    def test_many_orders_drain_without_accumulating_delay(self):
        """Throughput was the reported symptom: 0.5s per order serialized."""
        gateway = LateLandingOrderGateway(appear_after=99)
        redis_client, service = self._service(gateway)
        for i in range(20):
            self._submit(service, request_id="ord-%d" % i)

        started = time.monotonic()
        service.drain_pending(max_items=50)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0, "20 orders took %.2fs" % elapsed)
        self.assertEqual(service.pending_settlement_count(), 20)


class RedisRpcTest(unittest.TestCase):
    def test_execution_snapshot_queries_orders_and_all_trades_once(self):
        gateway = CapturingExecutionGateway()
        handlers = BigQmtRpcHandlers(
            account_id="acct",
            market_data=FakeMarketData(),
            position_provider=FakePositionProvider(),
            order_gateway=gateway,
        )

        snapshot = handlers.handle(
            "query_execution_snapshot",
            {
                "account_id": "acct",
                "order_strategy_name": "icestone_grid_600276",
                "trade_strategy_name": "",
            },
        )

        self.assertEqual(gateway.order_strategy_names, [("acct", "icestone_grid_600276")])
        self.assertEqual(gateway.strategy_names, [("acct", "")])
        self.assertEqual(snapshot["orders"][0].order_sys_id, "order-1")
        self.assertEqual(snapshot["trades"][0].trade_id, "trade-1")
        self.assertEqual(snapshot["account_id"], "acct")

    def test_query_trades_preserves_explicit_empty_strategy_name(self):
        gateway = CapturingTradeGateway()
        handlers = BigQmtRpcHandlers(
            account_id="acct",
            market_data=FakeMarketData(),
            position_provider=FakePositionProvider(),
            order_gateway=gateway,
        )

        handlers.handle(
            "query_stock_trades",
            {"account_id": "acct", "strategy_name": ""},
        )

        self.assertEqual(gateway.strategy_names, [("acct", "")])

    def test_submit_orders_batch_returns_one_result_per_order(self):
        handlers = BigQmtRpcHandlers(
            account_id="acct",
            market_data=FakeMarketData(),
            position_provider=FakePositionProvider(),
            order_gateway=DryRunOrderGateway(),
            allow_order_methods=True,
        )

        results = handlers.handle(
            "order_stock_batch",
            {
                "orders": [
                    {"account_id": "acct", "stock_code": "600000.SH", "order_type": 23,
                     "order_volume": 100, "price": 10.0, "signal_id": "batch-1"},
                    {"account_id": "acct", "stock_code": "600000.SH", "order_type": 24,
                     "order_volume": 100, "price": 10.5, "signal_id": "batch-2"},
                ]
            },
        )

        self.assertEqual(len(results), 2)
        self.assertTrue(all(item["success"] for item in results))
        self.assertTrue(all(item["accepted"] for item in results))
        self.assertTrue(all(item["user_order_id"] for item in results))
        self.assertTrue(all(not item["order_sys_id"] for item in results))

    def test_submit_order_enriches_order_sys_id_by_remark(self):
        # issue #38: passorder 提交成功但委托号异步分配。服务端必须按唯一
        # user_order_id(remark) 匹配并回填 order_sys_id，客户端才不会把
        # 「已提交」误判成 -1 失败。
        gateway = LandingOrderGateway(landed=True)
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True,
            # Drive the lookup synchronously so these assertions stay about the
            # matching logic, not about when the settle pass runs.
            settle_orders_inline=True,
            order_settle_timeout_seconds=0.0,
        )
        result = handlers.handle("order_stock", {
            "account_id": "acct", "stock_code": "600000.SH", "order_type": 23,
            "order_volume": 100, "price_type": 11, "price": 10.0,
            "order_remark": "REMARK-38",
        })
        self.assertEqual(result.order_sys_id, "sysid-1")
        self.assertEqual(handlers._last_server_error, "")

    def test_submit_order_silent_rejection_sets_server_error(self):
        # 委托没进系统（静默拒绝）时记录 server_error，客户端据此收到真实原因。
        gateway = LandingOrderGateway(landed=False)
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True,
            # Drive the lookup synchronously so these assertions stay about the
            # matching logic, not about when the settle pass runs.
            settle_orders_inline=True,
            order_settle_timeout_seconds=0.0,
        )
        result = handlers.handle("order_stock", {
            "account_id": "acct", "stock_code": "600000.SH", "order_type": 23,
            "order_volume": 100, "price_type": 11, "price": 10.0,
            "order_remark": "REMARK-38",
        })
        self.assertIsNone(result.order_sys_id)
        self.assertIn("not found in system", handlers._last_server_error)

    def test_server_error_does_not_leak_into_later_requests(self):
        """issue #43: _last_server_error is instance state read by every response,
        so a failed order used to stamp its error onto every later read."""
        gateway = LandingOrderGateway(landed=False)
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True,
            # Drive the lookup synchronously so these assertions stay about the
            # matching logic, not about when the settle pass runs.
            settle_orders_inline=True,
            order_settle_timeout_seconds=0.0,
        )
        handlers.handle("order_stock", {
            "account_id": "acct", "stock_code": "600000.SH", "order_type": 23,
            "order_volume": 100, "price_type": 11, "price": 10.0,
            "order_remark": "REMARK-43",
        })
        self.assertIn("not found in system", handlers._last_server_error)

        # Any later request must start from a clean slot.
        handlers.handle("ping", {})
        self.assertEqual(handlers._last_server_error, "")
        handlers.handle("get_positions", {"account_id": "acct"})
        self.assertEqual(handlers._last_server_error, "")

    def test_server_error_cleared_even_when_method_is_rejected(self):
        """A rejected request must not carry the previous diagnostic either."""
        gateway = LandingOrderGateway(landed=False)
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True,
            # Drive the lookup synchronously so these assertions stay about the
            # matching logic, not about when the settle pass runs.
            settle_orders_inline=True,
            order_settle_timeout_seconds=0.0,
        )
        handlers.handle("order_stock", {
            "account_id": "acct", "stock_code": "600000.SH", "order_type": 23,
            "order_volume": 100, "price_type": 11, "price": 10.0,
            "order_remark": "REMARK-43b",
        })
        self.assertNotEqual(handlers._last_server_error, "")
        with self.assertRaises(ValueError):
            handlers.handle("no_such_method", {})
        self.assertEqual(handlers._last_server_error, "")

    def test_unrelated_same_stock_order_does_not_mask_a_silent_rejection(self):
        """issue #41: an unrelated order on the same stock+side used to suppress
        the warning, leaving order_sys_id unfilled with no signal at all."""
        gateway = LandingOrderGateway(landed=False)
        # Pre-existing order: same stock and side, different (unrelated) remark.
        gateway.orders.append(
            OrderSnapshot(
                order_sys_id="sysid-unrelated",
                user_order_id="SOMEONE-ELSE",
                stock_code="600000.SH",
                action="BUY",
                volume=200,
                traded_volume=0,
                status="50",
            )
        )
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True,
            # Drive the lookup synchronously so these assertions stay about the
            # matching logic, not about when the settle pass runs.
            settle_orders_inline=True,
            order_settle_timeout_seconds=0.0,
        )
        result = handlers.handle("order_stock", {
            "account_id": "acct", "stock_code": "600000.SH", "order_type": 23,
            "order_volume": 100, "price_type": 11, "price": 10.0,
            "order_remark": "REMARK-41",
        })

        self.assertIsNone(result.order_sys_id)
        self.assertIn("not found in system", handlers._last_server_error)

    def test_remark_match_still_backfills_sysid_with_other_orders_present(self):
        """The strict match must not regress the issue #38 backfill."""
        gateway = LandingOrderGateway(landed=True)
        gateway.orders.append(
            OrderSnapshot(
                order_sys_id="sysid-unrelated",
                user_order_id="SOMEONE-ELSE",
                stock_code="600000.SH",
                action="BUY",
                volume=200,
                traded_volume=0,
                status="50",
            )
        )
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True,
            # Drive the lookup synchronously so these assertions stay about the
            # matching logic, not about when the settle pass runs.
            settle_orders_inline=True,
            order_settle_timeout_seconds=0.0,
        )
        result = handlers.handle("order_stock", {
            "account_id": "acct", "stock_code": "600000.SH", "order_type": 23,
            "order_volume": 100, "price_type": 11, "price": 10.0,
            "order_remark": "REMARK-38b",
        })

        self.assertEqual(result.order_sys_id, "sysid-1")
        self.assertEqual(handlers._last_server_error, "")

    def test_submit_orders_batch_reuses_order_tag_without_resubmitting(self):
        gateway = CountingOrderGateway()
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True,
            # Drive the lookup synchronously so these assertions stay about the
            # matching logic, not about when the settle pass runs.
            settle_orders_inline=True,
            order_settle_timeout_seconds=0.0,
        )
        params = {
            "batch_id": "BATCH-1",
            "orders": [{"account_id": "acct", "stock_code": "600000.SH",
                        "order_type": 23, "order_volume": 100, "price": 10.0,
                        "order_remark": "GRID-TAG-1"}],
        }

        first = handlers.handle("order_stock_batch", params)
        second = handlers.handle("order_stock_batch", params)

        self.assertEqual(gateway.submit_count, 1)
        self.assertEqual(gateway.query_count, 0)
        self.assertFalse(first[0]["idempotent"])
        self.assertTrue(second[0]["idempotent"])

    def test_submit_orders_batch_rejects_missing_order_tag(self):
        gateway = CountingOrderGateway()
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True,
            # Drive the lookup synchronously so these assertions stay about the
            # matching logic, not about when the settle pass runs.
            settle_orders_inline=True,
            order_settle_timeout_seconds=0.0,
        )

        result = handlers.handle("order_stock_batch", {
            "orders": [{"account_id": "acct", "stock_code": "600000.SH",
                        "order_type": 23, "order_volume": 100, "price": 10.0}],
        })[0]

        self.assertEqual(gateway.submit_count, 0)
        self.assertTrue(result["explicit_failure"])
        self.assertEqual(result["error"], "ORDER_TAG_REQUIRED")

    def test_submit_orders_batch_recognizes_existing_qmt_order(self):
        existing = OrderSnapshot(
            order_sys_id="SYS-EXISTING", user_order_id="GRID-TAG-2",
            stock_code="600000.SH", action="BUY", volume=100,
            traded_volume=0, status="50",
        )
        gateway = CountingOrderGateway(existing=[existing])
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True,
            # Drive the lookup synchronously so these assertions stay about the
            # matching logic, not about when the settle pass runs.
            settle_orders_inline=True,
            order_settle_timeout_seconds=0.0,
        )

        result = handlers.handle("order_stock_batch", {
            "batch_id": "BATCH-2",
            "orders": [{"account_id": "acct", "stock_code": "600000.SH",
                        "order_type": 23, "order_volume": 100, "price": 10.0,
                        "order_remark": "GRID-TAG-2",
                        "require_idempotency_check": True}],
        })[0]

        self.assertEqual(gateway.submit_count, 0)
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["idempotent"])
        self.assertEqual(result["order_sys_id"], "SYS-EXISTING")

    def test_submit_orders_batch_recognizes_already_filled_trade(self):
        class FilledGateway(CountingOrderGateway):
            def query_submission_identities_strict(self, _account_id, _strategy_name):
                trade = type("Trade", (), {
                    "user_order_id": "GRID-TAG-FILLED",
                    "order_sys_id": "SYS-FILLED",
                })()
                return [], [trade]

        gateway = FilledGateway()
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True,
            # Drive the lookup synchronously so these assertions stay about the
            # matching logic, not about when the settle pass runs.
            settle_orders_inline=True,
            order_settle_timeout_seconds=0.0,
        )

        result = handlers.handle("order_stock_batch", {
            "orders": [{"account_id": "acct", "stock_code": "600000.SH",
                        "order_type": 23, "order_volume": 100, "price": 10.0,
                        "order_remark": "GRID-TAG-FILLED",
                        "require_idempotency_check": True}],
        })[0]

        self.assertEqual(gateway.submit_count, 0)
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["order_sys_id"], "SYS-FILLED")

    def test_submit_orders_batch_refuses_retry_when_lookup_is_unavailable(self):
        gateway = CountingOrderGateway(query_error=RuntimeError("qmt offline"))
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True,
            # Drive the lookup synchronously so these assertions stay about the
            # matching logic, not about when the settle pass runs.
            settle_orders_inline=True,
            order_settle_timeout_seconds=0.0,
        )

        result = handlers.handle("order_stock_batch", {
            "orders": [{"account_id": "acct", "stock_code": "600000.SH",
                        "order_type": 23, "order_volume": 100, "price": 10.0,
                        "order_remark": "GRID-TAG-3",
                        "require_idempotency_check": True}],
        })[0]

        self.assertEqual(gateway.submit_count, 0)
        self.assertFalse(result["explicit_failure"])
        self.assertEqual(result["error"], "IDEMPOTENCY_CHECK_UNAVAILABLE")

    def test_encoded_request_payload_hides_stock_codes_from_qmt_redis_guard(self):
        request = {
            "request_id": "encoded",
            "account_id": "acct",
            "method": "get_full_tick",
            "params": {"codes": ["000001.SZ", "600000.SH"]},
        }

        encoded = encode_rpc_request_payload(request)

        self.assertNotIn("000001", encoded)
        self.assertNotIn("600000", encoded)
        self.assertEqual(json.loads(decode_rpc_request_payload(encoded)), request)

    def test_readonly_rpc_writes_position_response_to_key_and_channel(self):
        redis_client, service = _service()

        processed = service.drain_pending()
        self.assertEqual(processed, 0)

        service.enqueue_payload(
            {
                "request_id": "req-1",
                "account_id": "acct",
                "method": "get_positions",
                "params": {},
            }
        )
        self.assertEqual(service.drain_pending(), 1)

        response_key = "bigqmt:rpc:resp:acct:req-1"
        response = json.loads(redis_client.kv[response_key])
        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["600000.SH"]["available"], 800)
        self.assertEqual(redis_client.published[0][0], "bigqmt:rpc:resp:acct:req-1")

    def test_native_query_failure_becomes_rpc_error(self):
        cases = (
            ("get_positions", "POSITION"), ("query_stock_positions", "POSITION"),
            ("query_orders", "ORDER"), ("query_stock_orders", "ORDER"),
            ("query_trades", "DEAL"), ("query_stock_trades", "DEAL"),
            ("query_execution_snapshot", "ORDER"), ("query_execution_snapshot", "DEAL"),
        )
        for method, failed_type in cases:
            with self.subTest(method=method, failed_type=failed_type):
                calls = []

                def query(account_id, account_type, detail_type, *args):
                    self.assertEqual((account_id, account_type), ("acct", "STOCK"))
                    calls.append(detail_type)
                    if detail_type == failed_type:
                        raise RuntimeError("simulated native %s query failure" % failed_type)
                    return []

                redis_client, service = _service()
                service.handlers.position_provider = BigQmtPositionProvider(query)
                service.handlers.order_gateway = BigQmtOrderGateway(
                    context_info=None, get_trade_detail_data_func=query)
                service.enqueue_payload({
                    "request_id": method,
                    "account_id": "acct",
                    "method": method,
                    "params": {},
                })
                self.assertEqual(service.drain_pending(), 1)
                response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:" + method])
                self.assertFalse(response["ok"])
                self.assertIsNone(response["data"])
                self.assertEqual(response["error"],
                                 "RuntimeError: simulated native %s query failure" % failed_type)
                expected_calls = (["ORDER", "DEAL"]
                                  if method == "query_execution_snapshot" and failed_type == "DEAL"
                                  else [failed_type])
                self.assertEqual(calls, expected_calls)

    def test_native_empty_queries_remain_successful_rpc_results(self):
        cases = (
            ("get_positions", ["POSITION"], {}),
            ("query_stock_positions", ["POSITION"], {}),
            ("query_orders", ["ORDER"], []), ("query_stock_orders", ["ORDER"], []),
            ("query_trades", ["DEAL"], []), ("query_stock_trades", ["DEAL"], []),
            ("query_execution_snapshot", ["ORDER", "DEAL"], None),
        )
        for method, expected_calls, empty_result in cases:
            with self.subTest(method=method):
                calls = []

                def query(account_id, account_type, detail_type, *args):
                    calls.append(detail_type)
                    if detail_type not in ("POSITION", "ORDER", "DEAL"):
                        raise ValueError("unsupported detail type: " + detail_type)
                    return []

                redis_client, service = _service()
                service.handlers.position_provider = BigQmtPositionProvider(query)
                service.handlers.order_gateway = BigQmtOrderGateway(
                    context_info=None, get_trade_detail_data_func=query)
                service.enqueue_payload({
                    "request_id": method,
                    "account_id": "acct",
                    "method": method,
                    "params": {},
                })
                self.assertEqual(service.drain_pending(), 1)
                response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:" + method])
                self.assertTrue(response["ok"], response["error"])
                if method == "query_execution_snapshot":
                    self.assertEqual(response["data"]["orders"], [])
                    self.assertEqual(response["data"]["trades"], [])
                else:
                    self.assertEqual(response["data"], empty_result)
                self.assertEqual(response["error"], "")
                self.assertEqual(calls, expected_calls)

    def test_process_in_listener_handles_request_without_waiting_for_drain(self):
        redis_client, service = _service(process_in_listener=True)

        service.enqueue_payload(
            {
                "request_id": "listener-req",
                "account_id": "acct",
                "method": "ping",
                "params": {},
            }
        )

        response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:listener-req"])
        self.assertTrue(response["ok"], response["error"])
        self.assertTrue(response["data"]["pong"])
        self.assertFalse(response["data"]["allow_order_methods"])
        self.assertEqual(response["data"]["rpc_revision"], RPC_REVISION)
        self.assertEqual(service.drain_pending(), 0)

    def test_process_in_listener_leaves_non_listener_methods_queued(self):
        redis_client, service = _service(process_in_listener=True)

        service.enqueue_payload(
            {
                "request_id": "queued-tick",
                "account_id": "acct",
                "method": "get_full_tick",
                "params": {"codes": ["600000.SH"]},
            }
        )

        self.assertNotIn("bigqmt:rpc:resp:acct:queued-tick", redis_client.kv)
        self.assertEqual(service.drain_pending(), 1)
        response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:queued-tick"])
        self.assertTrue(response["ok"], response["error"])

    def test_process_in_listener_wildcard_only_handles_ping_inline(self):
        # get_full_tick is a market-data read (thread-safe in embedded terminal),
        # so it stays inline for low latency and responds immediately.
        redis_client, service = _service_with_listener_methods(
            allow_order_methods=True,
            process_in_listener=True,
            listener_methods=("*",),
        )

        service.enqueue_payload(
            {
                "request_id": "direct-tick",
                "account_id": "acct",
                "method": "get_full_tick",
                "params": {"codes": ["600000.SH"]},
            }
        )

        # Inline: response written immediately, no pending drain needed.
        response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:direct-tick"])
        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(response["data"]["600000.SH"]["lastPrice"], 10.5)

        service.enqueue_payload(
            {
                "request_id": "queued-sync",
                "account_id": "acct",
                "method": "sync_positions",
                "params": {},
            }
        )

        self.assertNotIn("bigqmt:rpc:resp:acct:queued-sync", redis_client.kv)
        self.assertEqual(service.drain_pending(), 1)
        sync_response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:queued-sync"])
        self.assertTrue(sync_response["ok"], sync_response["error"])
        self.assertEqual(sync_response["data"]["positions"]["600000.SH"]["available"], 800)

        service.enqueue_payload(
            {
                "request_id": "queued-order",
                "account_id": "acct",
                "method": "order_stock",
                "params": {
                    "stock_code": "600000.SH",
                    "order_type": 23,
                    "order_volume": 100,
                    "price_type": 11,
                    "price": 10.1,
                },
            }
        )

        self.assertNotIn("bigqmt:rpc:resp:acct:queued-order", redis_client.kv)
        self.assertEqual(service.drain_pending(), 1)
        order_response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:queued-order"])
        self.assertTrue(order_response["ok"], order_response["error"])

    def test_listener_wildcard_defers_asset_query_to_strategy_thread(self):
        redis_client, service = _service_with_listener_methods(
            process_in_listener=True,
            listener_methods=("*",),
        )

        service.enqueue_payload(
            {
                "request_id": "queued-asset",
                "account_id": "acct",
                "method": "query_stock_asset",
                "params": {},
            }
        )

        self.assertNotIn("bigqmt:rpc:resp:acct:queued-asset", redis_client.kv)
        self.assertEqual(service.drain_pending(), 1)
        response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:queued-asset"])
        self.assertTrue(response["ok"], response["error"])

    def test_listener_wildcard_defers_execution_snapshot_to_strategy_thread(self):
        redis_client, service = _service_with_listener_methods(
            process_in_listener=True,
            listener_methods=("*",),
        )

        service.enqueue_payload(
            {
                "request_id": "queued-execution-snapshot",
                "account_id": "acct",
                "method": "query_execution_snapshot",
                "params": {"order_strategy_name": "", "trade_strategy_name": ""},
            }
        )

        response_key = "bigqmt:rpc:resp:acct:queued-execution-snapshot"
        self.assertNotIn(response_key, redis_client.kv)
        self.assertEqual(service.drain_pending(), 1)
        response = json.loads(redis_client.kv[response_key])
        self.assertTrue(response["ok"], response["error"])

    def test_account_mismatch_is_rejected(self):
        redis_client, service = _service()

        service.enqueue_payload(
            {
                "request_id": "req-2",
                "account_id": "other",
                "method": "get_asset",
                "params": {},
            }
        )
        service.drain_pending()

        response = json.loads(redis_client.kv["bigqmt:rpc:resp:other:req-2"])
        self.assertFalse(response["ok"])
        self.assertIn("account_id mismatch", response["error"])

    def test_order_rpc_is_disabled_by_default(self):
        redis_client, service = _service()

        service.enqueue_payload(
            {
                "request_id": "req-3",
                "account_id": "acct",
                "method": "submit_order",
                "params": {
                    "action": "BUY",
                    "stock_code": "600000",
                    "volume": 100,
                    "price": 10.1,
                },
            }
        )
        service.drain_pending()

        response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:req-3"])
        self.assertFalse(response["ok"])
        self.assertIn("not allowed", response["error"])

    def test_order_rpc_can_be_enabled_for_dryrun_gateway(self):
        redis_client, service = _service(allow_order_methods=True)

        service.enqueue_payload(
            {
                "request_id": "req-4",
                "account_id": "acct",
                "method": "submit_order",
                "params": {
                    "action": "BUY",
                    "stock_code": "600000",
                    "volume": 100,
                    "price": 10.1,
                },
            }
        )
        service.drain_pending()

        response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:req-4"])
        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["status"], "DRY_RUN")

    def test_miniqmt_read_aliases_are_accepted(self):
        redis_client, service = _service()

        for request_id, method in (
            ("alias-pos", "query_stock_positions"),
            ("alias-asset", "query_stock_asset"),
            ("alias-tick", "get_full_tick"),
        ):
            params = {"codes": ["600000.SH"]} if method == "get_full_tick" else {}
            service.enqueue_payload(
                {
                    "request_id": request_id,
                    "account_id": "acct",
                    "method": method,
                    "params": params,
                }
            )
            service.drain_pending()
            response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:%s" % request_id])
            self.assertTrue(response["ok"], response["error"])

        self.assertEqual(
            json.loads(redis_client.kv["bigqmt:rpc:resp:acct:alias-pos"])["data"]["600000.SH"]["volume"],
            1000,
        )
        self.assertEqual(
            json.loads(redis_client.kv["bigqmt:rpc:resp:acct:alias-asset"])["data"]["cash"],
            100.0,
        )
        self.assertEqual(
            json.loads(redis_client.kv["bigqmt:rpc:resp:acct:alias-tick"])["data"]["600000.SH"]["lastPrice"],
            10.5,
        )

    def test_miniqmt_single_position_alias_filters_by_stock_code(self):
        redis_client, service = _service()

        service.enqueue_payload(
            {
                "request_id": "alias-single-position",
                "account_id": "acct",
                "method": "query_stock_position",
                "params": {"stock_code": "600000"},
            }
        )
        service.drain_pending()

        response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:alias-single-position"])
        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(response["data"]["stock_code"], "600000.SH")

    def test_miniqmt_query_orders_alias_supports_cancelable_filter(self):
        redis_client, service = _service_with_order_gateway(FakeOrderGateway())

        service.enqueue_payload(
            {
                "request_id": "alias-orders",
                "account_id": "acct",
                "method": "query_stock_orders",
                "params": {"cancelable_only": True},
            }
        )
        service.drain_pending()

        response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:alias-orders"])
        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(len(response["data"]), 1)
        self.assertEqual(response["data"][0]["order_sys_id"], "open-1")

    def test_miniqmt_order_alias_is_disabled_by_default(self):
        redis_client, service = _service()

        service.enqueue_payload(
            {
                "request_id": "alias-order-disabled",
                "account_id": "acct",
                "method": "order_stock",
                "params": {
                    "stock_code": "600000.SH",
                    "order_type": 23,
                    "order_volume": 100,
                    "price_type": 11,
                    "price": 10.1,
                    "order_remark": "mini",
                },
            }
        )
        service.drain_pending()

        response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:alias-order-disabled"])
        self.assertFalse(response["ok"])
        self.assertTrue(
            "not allowed" in response["error"] or "disabled" in response["error"],
            response["error"],
        )

    def test_miniqmt_order_and_cancel_aliases_work_when_enabled(self):
        order_gateway = DryRunOrderGateway()
        redis_client, service = _service_with_order_gateway(order_gateway, allow_order_methods=True)

        service.enqueue_payload(
            {
                "request_id": "alias-order",
                "account_id": "acct",
                "method": "order_stock",
                "params": {
                    "stock_code": "600000.SH",
                    "order_type": 24,
                    "order_volume": 100,
                    "price_type": 11,
                    "price": 10.1,
                    "order_remark": "mini",
                },
            }
        )
        service.enqueue_payload(
            {
                "request_id": "alias-cancel",
                "account_id": "acct",
                "method": "cancel_order_stock_sysid",
                "params": {"account": {"account_id": "acct"}, "order_sysid": "sys-1"},
            }
        )
        self.assertEqual(service.drain_pending(max_items=2), 2)

        order_response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:alias-order"])
        cancel_response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:alias-cancel"])
        self.assertTrue(order_response["ok"], order_response["error"])
        self.assertTrue(cancel_response["ok"], cancel_response["error"])
        self.assertEqual(order_gateway.submitted[0].action, "SELL")
        self.assertEqual(order_gateway.submitted[0].volume, 100)
        self.assertEqual(order_gateway.submitted[0].remark, "mini")
        self.assertEqual(order_gateway.cancelled[0].order_sys_id, "sys-1")

    def test_market_data_method_is_whitelisted_and_dispatched(self):
        redis_client, service = _service()

        service.enqueue_payload(
            {
                "request_id": "market-data-ex",
                "account_id": "acct",
                "method": "get_market_data_ex",
                "params": {
                    "field_list": ["close"],
                    "stock_list": ["600000.SH"],
                    "period": "1d",
                    "count": 1,
                },
            }
        )
        service.drain_pending()

        response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:market-data-ex"])
        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(response["data"]["params"]["field_list"], ["close"])
        self.assertEqual(response["data"]["data"]["600000.SH"]["close"], [10.0])


class DownloadHistoryDataTest(unittest.TestCase):
    """Issue #32: download_history_data was routing to ContextInfo (which has
    no such method) instead of the QMT-injected global function."""

    def _handlers_with_qmt_global(self, func_name, func):
        return BigQmtRpcHandlers(
            account_id="acct",
            market_data=FakeMarketData(),
            position_provider=FakePositionProvider(),
            qmt_api={func_name: func},
        )

    def test_download_history_data_calls_qmt_global(self):
        calls = []

        def fake_download(stock_code, period, start_time, end_time):
            calls.append((stock_code, period, start_time, end_time))
            return True

        handlers = self._handlers_with_qmt_global("download_history_data", fake_download)
        result = handlers.handle("download_history_data", {
            "stock_code": "000001.SZ",
            "period": "1d",
            "start_time": "20230101",
            "end_time": "",
        })

        self.assertEqual(calls, [("000001.SZ", "1d", "20230101", "")])
        self.assertTrue(result)

    def test_download_history_data2_calls_qmt_global(self):
        calls = []

        def fake_download2(stock_list, period, start_time, end_time):
            calls.append((stock_list, period, start_time, end_time))
            return True

        handlers = self._handlers_with_qmt_global("download_history_data2", fake_download2)
        result = handlers.handle("download_history_data2", {
            "stock_list": ["000001.SZ", "600000.SH"],
            "period": "1d",
            "start_time": "20230101",
            "end_time": "",
        })

        self.assertEqual(calls[0][0], ["000001.SZ", "600000.SH"])
        self.assertEqual(calls[0][1], "1d")
        self.assertTrue(result)

    def test_download_history_data_fallback_to_adapter_when_no_global(self):
        """When qmt_api has no download_history_data (e.g. outside QMT),
        the handler falls back to the adapter path. With a FakeMarketData
        that lacks the method, the handler returns False (graceful, not crash)."""
        handlers = BigQmtRpcHandlers(
            account_id="acct",
            market_data=FakeMarketData(),
            position_provider=FakePositionProvider(),
            qmt_api={},
        )
        result = handlers.handle("download_history_data", {
            "stock_code": "000001.SZ",
            "period": "1d",
            "start_time": "20230101",
            "end_time": "",
        })
        # No global func and adapter lacks the method → returns False, not crash.
        self.assertFalse(result)

    def test_download_history_data_falls_back_to_down_history_data(self):
        # issue #54: QMT builds that only expose down_history_data must still work.
        calls = []

        def fake_down(stock_code, period, start_time, end_time):
            calls.append((stock_code, period, start_time, end_time))
            return True

        handlers = self._handlers_with_qmt_global("down_history_data", fake_down)
        result = handlers.handle("download_history_data", {
            "stock_code": "600000.SH",
            "period": "1d",
            "start_time": "20260815",
            "end_time": "20260819",
        })
        self.assertEqual(calls, [("600000.SH", "1d", "20260815", "20260819")])
        self.assertTrue(result)

    def test_download_history_data2_loops_per_code_with_single_stock_global(self):
        # issue #54: no download_history_data2 global — fall back to a per-code
        # loop over the single-stock global, dates included, so the requested
        # range actually reaches QMT.
        calls = []

        def fake_down(stock_code, period, start_time, end_time):
            calls.append((stock_code, period, start_time, end_time))
            return True

        handlers = self._handlers_with_qmt_global("down_history_data", fake_down)
        result = handlers.handle("download_history_data2", {
            "stock_list": ["600000.SH", "000001.SZ"],
            "period": "1d",
            "start_time": "20260815",
            "end_time": "20260819",
        })
        self.assertEqual(calls, [
            ("600000.SH", "1d", "20260815", "20260819"),
            ("000001.SZ", "1d", "20260815", "20260819"),
        ])
        self.assertTrue(result)


class ProbeCapabilitiesTest(unittest.TestCase):
    """probe_capabilities：部署后只读探测 QMT 暴露的 callable。"""

    def _handlers(self):
        calls = []

        def fake_credit(account_id):
            calls.append(account_id)
            return [{"a": 1}, {"b": 2}]

        class _Ctx:
            def get_full_tick(self, codes):
                return {}

            # get_market_data_ex 故意不提供，验证 False 分支

        return BigQmtRpcHandlers(
            account_id="acct",
            market_data=BigQmtMarketDataProvider(_Ctx()),
            position_provider=FakePositionProvider(),
            qmt_api={
                "passorder": lambda *a: None,
                "get_assure_contract": fake_credit,
                "get_enable_short_contract": lambda a: (_ for _ in ()).throw(RuntimeError("no credit")),
            },
        )

    def test_probe_reports_globals_context_and_credit(self):
        info = self._handlers().handle("probe_capabilities", {})

        self.assertTrue(info["qmt_globals"]["passorder"])
        self.assertFalse(info["qmt_globals"]["cancel"])
        self.assertTrue(info["contextinfo_methods"]["get_full_tick"])
        self.assertFalse(info["contextinfo_methods"]["get_market_data_ex"])
        # 信用探测：成功的带行数，报错的带原因，未绑定的标 unavailable
        self.assertEqual(info["credit_probe"]["get_assure_contract"]["rows"], 2)
        self.assertFalse(info["credit_probe"]["get_enable_short_contract"]["ok"])
        self.assertIn("no credit", info["credit_probe"]["get_enable_short_contract"]["error"])
        self.assertFalse(info["credit_probe"]["get_debt_contract"]["available"])

    def test_probe_is_read_only_and_in_whitelist(self):
        handlers = self._handlers()
        self.assertIn("probe_capabilities", handlers.allowed_methods)

    def test_probe_reports_the_raw_market_data_method(self):
        """#237: 适配层「有 get_market_data_ex_ori 就只走它」，两台终端因此可能
        跑在两条不同的代码路径上。名单里没有这个名字时 probe 永远不报它，报告人
        和维护者就都拿「两边都没有这个键」当「两边一样」的证据 —— 名单缺一个名字
        等于把差异藏起来。"""
        info = self._handlers().handle("probe_capabilities", {})

        self.assertIn("get_market_data_ex_ori", info["contextinfo_methods"])
        # 这个 fake 的 ContextInfo 没有原始接口，所以必须是 False，不是缺键。
        self.assertFalse(info["contextinfo_methods"]["get_market_data_ex_ori"])


class BatchSettlementTest(unittest.TestCase):
    """#181 顺带点名的隐患: 批量里的每一笔都往同一个结算单槽里塞。

    _handle_submit_orders_batch 把 item 原样交给 _handle_submit_order, 而后者
    的 wait_settlement 默认 True。_pending_settlement 是单槽, 于是被逐笔覆盖;
    服务层每个请求只 take 一次, 拿到的永远是最后一笔。

    后果不是丢单 (每一笔都提交出去了), 是三件别的事:

    * 整批应答被推迟到那一笔结算完或超时才发出 —— 批量存在的理由就是一次
      往返, 这等于把最贵的一笔的延迟加回来;
    * 那一笔的诊断挂到整批的应答上 (server_error 说 "order not found in
      system", 而它说的只是其中一笔);
    * 它回填的 order_sys_id 谁也读不到 —— 批量结果 dict 在结算之前就已经从
      result 上拷完了。

    批量应答本来就是逐项的 (每项带 index / order_sys_id / user_order_id),
    委托号照样从 order_callback 推送学得到, 所以这里不该等结算。
    """

    def _service(self, gateway, timeout=5.0):
        redis_client = FakeRedis()
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True, order_settle_timeout_seconds=timeout,
        )
        return redis_client, handlers, RedisPubSubRpcService(
            redis_client, handlers, account_id="acct")

    def _batch_params(self, count=2):
        return {"orders": [
            {"account_id": "acct", "stock_code": "600000.SH", "order_type": 23,
             "order_volume": 100, "price_type": 11, "price": 10.1,
             "order_remark": "batch-item-%d" % index}
            for index in range(count)
        ]}

    def test_the_batch_handler_leaves_no_settlement_behind(self):
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(),
            order_gateway=LateLandingOrderGateway(never=True),
            allow_order_methods=True,
        )

        results = handlers.handle("order_stock_batch", self._batch_params(3))

        self.assertEqual(len(results), 3)
        self.assertTrue(all(item["success"] for item in results))
        self.assertIsNone(handlers.take_pending_settlement())

    def test_a_batch_reply_is_not_parked_behind_one_item(self):
        redis_client, _handlers, service = self._service(
            LateLandingOrderGateway(never=True))
        service.enqueue_payload({
            "request_id": "bat-1", "account_id": "acct",
            "method": "order_stock_batch", "params": self._batch_params(2),
        })

        service.drain_pending()

        self.assertIn("bigqmt:rpc:resp:acct:bat-1", redis_client.kv)
        self.assertEqual(service.pending_settlement_count(), 0)

    def test_one_items_diagnostic_does_not_ride_the_whole_batch(self):
        """A batch of two is not "the order was not found" (issue #152 wording).

        With the deadline already passed, the parked settlement answers with
        the 模拟-run-mode warning -- which names one stock, one price and one
        volume, and would be attached to a reply covering every order in the
        batch.
        """
        redis_client, _handlers, service = self._service(
            LateLandingOrderGateway(never=True), timeout=0.0)
        service.enqueue_payload({
            "request_id": "bat-2", "account_id": "acct",
            "method": "order_stock_batch", "params": self._batch_params(2),
        })

        service.drain_pending()

        response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:bat-2"])
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(response.get("server_error", ""), "")
        self.assertEqual(len(response["data"]), 2)

    def test_a_single_order_still_waits_for_its_settlement(self):
        """Negative control: the fix must not disarm the single-order path.

        order_stock keeps parking its reply until the id lands (#44/#152) --
        that is the whole settlement machinery, and only the batch path had no
        way to use it.
        """
        redis_client, _handlers, service = self._service(
            LateLandingOrderGateway(never=True))
        service.enqueue_payload({
            "request_id": "ord-9", "account_id": "acct", "method": "order_stock",
            "params": {"stock_code": "600000.SH", "order_type": 23,
                       "order_volume": 100, "price_type": 11, "price": 10.1,
                       "order_remark": "ord-9"},
        })

        service.drain_pending()

        self.assertNotIn("bigqmt:rpc:resp:acct:ord-9", redis_client.kv)
        self.assertEqual(service.pending_settlement_count(), 1)


if __name__ == "__main__":
    unittest.main()
