import json
import time
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway
from bigqmt_signal_trader.models import AssetSnapshot, OrderSnapshot, PositionSnapshot, TradeSnapshot
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


class CreditOrderTypeRpcTest(unittest.TestCase):
    def test_submit_order_forwards_explicit_credit_operation_type(self):
        gateway = DryRunOrderGateway()
        handlers = BigQmtRpcHandlers(
            account_id="acct",
            market_data=FakeMarketData(),
            position_provider=FakePositionProvider(),
            order_gateway=gateway,
            allow_order_methods=True,
        )

        handlers._handle_submit_order(
            {
                "stock_code": "600000.SH",
                "order_type": 31,
                "order_volume": 100,
                "price": 10.0,
                "price_type": 11,
                "signal_id": "credit-sell-repay",
                "wait_settlement": False,
            }
        )

        self.assertEqual(gateway.submitted[0].action, "SELL")
        self.assertEqual(gateway.submitted[0].order_type, 31)


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


if __name__ == "__main__":
    unittest.main()
