import json
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway
from bigqmt_signal_trader.models import AssetSnapshot, OrderSnapshot, PositionSnapshot
from bigqmt_signal_trader.redis_rpc import (
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


def _service_with_order_gateway(order_gateway, allow_order_methods=False):
    redis_client = FakeRedis()
    handlers = BigQmtRpcHandlers(
        account_id="acct",
        market_data=FakeMarketData(),
        position_provider=FakePositionProvider(),
        order_gateway=order_gateway,
        allow_order_methods=allow_order_methods,
    )
    return redis_client, RedisPubSubRpcService(redis_client, handlers, account_id="acct")


class RedisRpcTest(unittest.TestCase):
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

    def test_process_in_listener_wildcard_handles_read_methods_but_queues_stateful_methods(self):
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

        response = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:direct-tick"])
        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(response["data"]["600000.SH"]["lastPrice"], 10.5)
        self.assertEqual(service.drain_pending(), 0)

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


if __name__ == "__main__":
    unittest.main()
