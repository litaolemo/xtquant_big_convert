import os
import sys
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import (
    BigQmtRpcClient,
    FIX_PRICE,
    MARKET_PEER_PRICE_FIRST,
    SH_MARKET,
    STOCK_BUY,
    STOCK_SELL,
    SZ_MARKET,
    BigQmtXtData,
    BigQmtXtTrader,
    StockAccount,
    configure,
    load_client_config,
    xt_trader,
)
from bigqmt_signal_trader.full_tick_cache import full_tick_demand_key, full_tick_request_id, write_full_tick_cache


class FakeRpcClient:
    def __init__(self):
        self.account_id = "acct"
        self.calls = []
        self.redis = FakeRedisEvents()
        self.full_tick_cache_config = {
            "enabled": True,
            "demand_ttl_seconds": 10,
            "cache_ttl_seconds": 10,
            "wait_seconds": 0.1,
            "poll_interval_seconds": 0.01,
        }

    def _redis(self):
        return self.redis

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        self.calls.append((method, params or {}, account_id, timeout_seconds))
        if method == "query_stock_asset":
            return {"account_id": "acct", "cash": 100.5, "total_asset": 1000.5}
        if method == "query_stock_positions":
            return {
                "600000.SH": {
                    "stock_code": "600000.SH",
                    "volume": 1000,
                    "available": 800,
                    "cost": 10.2,
                    "stock_name": "PF Bank",
                }
            }
        if method == "query_stock_position":
            return {
                "stock_code": "600000.SH",
                "volume": 1000,
                "available": 800,
                "cost": 10.2,
            }
        if method == "query_stock_orders":
            return [
                {
                    "order_sys_id": "sys-1",
                    "user_order_id": "remark-1",
                    "stock_code": "600000.SH",
                    "action": "SELL",
                    "volume": 300,
                    "traded_volume": 100,
                    "status": "50",
                    "price": 10.1,
                }
            ]
        if method == "query_stock_trades":
            return [
                {
                    "trade_id": "trade-1",
                    "order_sys_id": "sys-1",
                    "user_order_id": "remark-1",
                    "stock_code": "600000.SH",
                    "action": "BUY",
                    "volume": 100,
                    "price": 10.0,
                }
            ]
        if method == "query_execution_snapshot":
            return {
                "account_id": "acct",
                "server_time": "2026-07-15 10:00:00",
                "orders": [
                    {
                        "order_sys_id": "sys-1", "user_order_id": "remark-1",
                        "stock_code": "600000.SH", "action": "SELL", "volume": 300,
                        "traded_volume": 100, "status": "50", "price": 10.1,
                    }
                ],
                "trades": [
                    {
                        "trade_id": "trade-1", "order_sys_id": "sys-1",
                        "user_order_id": "remark-1", "stock_code": "600000.SH",
                        "action": "BUY", "volume": 100, "price": 10.0,
                    }
                ],
            }
        if method == "order_stock":
            return {"status": "SUBMITTED", "user_order_id": "bq:1", "order_sys_id": "sys-2"}
        if method == "order_stock_batch":
            return [{"success": True, "accepted": True, "user_order_id": "batch-tag"}]
        if method == "cancel_order_stock_sysid":
            return {"success": True}
        if method == "get_full_tick":
            codes = params.get("codes") or []
            if codes == ["SH", "SZ"]:
                return {
                    "000001.SH": {"lastPrice": 3000},
                    "000001.SZ": {"lastPrice": 10},
                    "600000.SH": {"lastPrice": 10},
                    "510300.SH": {"lastPrice": 4},
                    "300001.SZ": {"lastPrice": 20},
                    "113001.SH": {"lastPrice": 100},
                }
            return {codes[0]: {"lastPrice": 10, "bidPrice": [9.9], "askPrice": [10.1]}}
        if method == "get_instrument_detail":
            return {"InstrumentStatus": 0, "code": params.get("code")}
        if method == "get_market_data_ex":
            return {"600000.SH": {"close": [10.0]}}
        if method == "ping":
            return {"pong": True}
        raise AssertionError("unexpected method: %s" % method)

    def publish_event(self, event_type, payload, stream_template="bigqmt:quote_events:{account_id}"):
        return self.redis.publish_event(event_type, payload)

    def save_quote_subscription(self, seq, payload, active=True):
        if active:
            self.redis.hset("bigqmt:quote_subscriptions:%s" % self.account_id, str(seq), payload)
        else:
            self.redis.hdel("bigqmt:quote_subscriptions:%s" % self.account_id, str(seq))


class FakeRedisEvents:
    def __init__(self):
        self.kv = {}
        self.hashes = {}
        self.deleted = []
        self.events = []
        self.expired = []

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value
        return 1

    def hdel(self, key, field):
        self.deleted.append((key, field))
        self.hashes.setdefault(key, {}).pop(field, None)
        return 1

    def hgetall(self, key):
        return self.hashes.get(key, {})

    def expire(self, key, seconds):
        self.expired.append((key, seconds))
        return True

    def setex(self, key, seconds, value):
        self.kv[key] = value
        self.expired.append((key, seconds))
        return True

    def publish_event(self, event_type, payload):
        self.events.append((event_type, payload))
        return {"event_type": event_type, "payload": payload}

    def get(self, key):
        if key in self.kv:
            return self.kv[key]
        value = self.hashes.get(key)
        if value is None:
            return None
        import json

        return json.dumps(value).encode("utf-8")


class XtquantCompatTest(unittest.TestCase):
    def _with_fake_config(self, module_name="test_bigqmt_client_cfg"):
        module = types.ModuleType(module_name)
        module.BIGQMT_ACCOUNT_ID = "cfg-account"
        module.BIGQMT_RPC_TIMEOUT_SECONDS = 9
        module.BIGQMT_REDIS_CONFIG = {
            "host": "cfg-host",
            "port": 6380,
            "db": 6,
            "username": "cfg-user",
            "password": "cfg-pass",
        }
        old_env = os.environ.get("BIGQMT_CLIENT_CONFIG_MODULE")
        os.environ["BIGQMT_CLIENT_CONFIG_MODULE"] = module_name
        sys.modules[module_name] = module
        return module_name, old_env

    def _cleanup_fake_config(self, module_name, old_env):
        sys.modules.pop(module_name, None)
        if old_env is None:
            os.environ.pop("BIGQMT_CLIENT_CONFIG_MODULE", None)
        else:
            os.environ["BIGQMT_CLIENT_CONFIG_MODULE"] = old_env

    def _trader(self):
        trader = BigQmtXtTrader(account_id="acct")
        trader.client = FakeRpcClient()
        return trader

    def _xtdata(self):
        return BigQmtXtData(FakeRpcClient())

    def test_trader_read_methods_return_miniqmt_style_objects(self):
        trader = self._trader()
        acc = StockAccount("acct")

        asset = trader.query_stock_asset(acc)
        positions = trader.query_stock_positions(acc)
        single = trader.query_stock_position(acc, "600000")

        self.assertEqual(asset.cash, 100.5)
        self.assertEqual(asset.market_value, 900.0)
        self.assertEqual(positions[0].stock_code, "600000.SH")
        self.assertEqual(positions[0].can_use_volume, 800)
        self.assertEqual(positions[0].avg_price, 10.2)
        self.assertEqual(single.stock_code, "600000.SH")

    def test_orders_trades_order_and_cancel_are_miniqmt_shaped(self):
        trader = self._trader()
        acc = StockAccount("acct")

        orders = trader.query_stock_orders(acc, cancelable_only=False)
        trades = trader.query_stock_trades(acc)
        order_id = trader.order_stock(
            acc,
            "600000.SH",
            STOCK_BUY,
            100,
            MARKET_PEER_PRICE_FIRST,
            0,
            "strategy",
            "remark",
        )
        cancelled = trader.cancel_order_stock_sysid(acc, SH_MARKET, "sys-2")

        self.assertEqual(orders[0].order_type, STOCK_SELL)
        self.assertEqual(orders[0].order_status, 50)
        self.assertEqual(orders[0].order_volume, 300)
        self.assertEqual(trades[0].order_type, STOCK_BUY)
        self.assertEqual(trades[0].traded_price, 10.0)
        self.assertEqual(trades[0].order_remark, "remark-1")
        self.assertEqual(order_id, "sys-2")
        self.assertTrue(cancelled)
        self.assertEqual(trader.client.calls[-2][1]["price_type"], MARKET_PEER_PRICE_FIRST)

    def test_execution_snapshot_maps_orders_and_trades_with_one_rpc(self):
        trader = self._trader()
        acc = StockAccount("acct")

        snapshot = trader.query_execution_snapshot(
            acc, order_strategy_name="icestone_grid_600276", trade_strategy_name=""
        )

        self.assertEqual(snapshot["orders"][0].order_sysid, "sys-1")
        self.assertEqual(snapshot["trades"][0].trade_id, "trade-1")
        self.assertEqual(snapshot["server_time"], "2026-07-15 10:00:00")
        self.assertEqual(trader.client.calls[-1][0], "query_execution_snapshot")
        self.assertEqual(trader.client.calls[-1][1]["trade_strategy_name"], "")

    def test_order_stock_never_returns_user_tag_as_real_order_id(self):
        trader = self._trader()
        acc = StockAccount("acct")
        trader.client.call = lambda *_args, **_kwargs: {
            "status": "SUBMITTED", "user_order_id": "bq:request-only",
            "order_sys_id": None,
        }

        order_id = trader.order_stock(
            acc, "600000.SH", STOCK_BUY, 100, FIX_PRICE, 10.0,
            "strategy", "remark",
        )
        result = trader.order_stock_result(
            acc, "600000.SH", STOCK_BUY, 100, FIX_PRICE, 10.0,
            "strategy", "remark",
        )

        self.assertEqual(order_id, -1)
        self.assertEqual(result["user_order_id"], "bq:request-only")
        self.assertIsNone(result["order_sys_id"])

    def test_order_stock_batch_forwards_batch_identity(self):
        trader = self._trader()
        acc = StockAccount("acct")

        result = trader.order_stock_batch(
            acc,
            [{"stock_code": "600000.SH", "order_type": STOCK_BUY,
              "order_volume": 100, "price": 10.0,
              "order_remark": "batch-tag"}],
            batch_id="BATCH-IDENTITY-1",
        )

        method, params, account_id, _timeout = trader.client.calls[-1]
        self.assertEqual(method, "order_stock_batch")
        self.assertEqual(account_id, "acct")
        self.assertEqual(params["batch_id"], "BATCH-IDENTITY-1")
        self.assertEqual(params["orders"][0]["order_remark"], "batch-tag")
        self.assertTrue(result[0]["accepted"])

    def test_xtdata_read_methods_and_sector_filter(self):
        xtdata = self._xtdata()
        write_full_tick_cache(
            xtdata.client.redis,
            xtdata.client.account_id,
            ["600000.SH"],
            {"600000.SH": {"lastPrice": 10, "bidPrice": [9.9], "askPrice": [10.1]}},
        )
        write_full_tick_cache(
            xtdata.client.redis,
            xtdata.client.account_id,
            ["SH", "SZ"],
            {
                "000001.SH": {"lastPrice": 3000},
                "000001.SZ": {"lastPrice": 10},
                "600000.SH": {"lastPrice": 10},
                "510300.SH": {"lastPrice": 4},
                "300001.SZ": {"lastPrice": 20},
                "113001.SH": {"lastPrice": 100},
            },
        )

        ticks = xtdata.get_full_tick(["600000.SH"])
        detail = xtdata.get_instrument_detail("600000.SH")
        sector_codes = xtdata.get_stock_list_in_sector("沪深A股")
        market_data = xtdata.get_market_data_ex(["close"], ["600000.SH"], count=1)

        self.assertEqual(ticks["600000.SH"]["bidPrice"], [9.9])
        self.assertEqual(detail["InstrumentStatus"], 0)
        self.assertEqual(sector_codes, ["000001.SZ", "300001.SZ", "600000.SH"])
        self.assertEqual(market_data["600000.SH"]["close"], [10.0])

    def test_xtdata_full_tick_reads_redis_cache_and_renews_demand(self):
        xtdata = self._xtdata()
        write_full_tick_cache(
            xtdata.client.redis,
            xtdata.client.account_id,
            ["SZ", "SH"],
            {"600000.SH": {"lastPrice": 10, "bidPrice": [9.9], "askPrice": [10.1]}},
        )

        ticks = xtdata.get_full_tick(["SH", "SZ"])

        self.assertIn("600000.SH", ticks)
        self.assertFalse([call for call in xtdata.client.calls if call[0] == "get_full_tick"])
        demand_key = full_tick_demand_key(xtdata.client.account_id)
        self.assertIn(full_tick_request_id(["SH", "SZ"]), xtdata.client.redis.hashes[demand_key])

    def test_xtdata_full_tick_symbol_miss_falls_back_to_rpc(self):
        xtdata = self._xtdata()
        xtdata.client.full_tick_cache_config["wait_seconds"] = 0

        ticks = xtdata.get_full_tick(["600000.SH"])

        # A cold cache miss on a symbol list now falls back to a live RPC instead
        # of a hard wait_seconds stall, so the first call returns in ~ms.
        self.assertEqual(ticks["600000.SH"]["bidPrice"], [9.9])
        self.assertEqual([call[0] for call in xtdata.client.calls if call[0] == "get_full_tick"], ["get_full_tick"])
        demand_key = full_tick_demand_key(xtdata.client.account_id)
        self.assertIn(full_tick_request_id(["600000.SH"]), xtdata.client.redis.hashes[demand_key])

    def test_xtdata_full_market_tick_miss_raises_without_rpc(self):
        xtdata = self._xtdata()
        xtdata.client.full_tick_cache_config["wait_seconds"] = 0

        # Whole-market snapshots must stay on the demand cache; a miss must never
        # live-pull ~50k rows over RPC.
        with self.assertRaises(TimeoutError):
            xtdata.get_full_tick(["SH", "SZ"])

        self.assertFalse([call for call in xtdata.client.calls if call[0] == "get_full_tick"])
        demand_key = full_tick_demand_key(xtdata.client.account_id)
        self.assertIn(full_tick_request_id(["SH", "SZ"]), xtdata.client.redis.hashes[demand_key])

    def test_xtdata_full_market_tick_can_fall_back_to_rpc_when_cache_disabled(self):
        xtdata = self._xtdata()
        xtdata.client.full_tick_cache_config["enabled"] = False

        xtdata.get_full_tick(["SH", "SZ"])

        self.assertEqual(xtdata.client.calls[-1][0], "get_full_tick")
        self.assertEqual(xtdata.client.calls[-1][3], 30)

    def test_quote_subscribe_and_unsubscribe_write_redis_events(self):
        xtdata = self._xtdata()

        seq = xtdata.subscribe_quote("600000.SH", period="tick")
        result = xtdata.unsubscribe_quote(seq)

        key = "bigqmt:quote_subscriptions:acct"
        self.assertEqual(result, 0)
        self.assertNotIn(str(seq), xtdata.client.redis.hashes.get(key, {}))
        self.assertIn((key, str(seq)), xtdata.client.redis.deleted)
        self.assertEqual(xtdata.client.redis.events[0][0], "subscribe_quote")
        self.assertEqual(xtdata.client.redis.events[1][0], "unsubscribe_quote")

    def test_optional_xtquant_shim_imports_constants_and_classes(self):
        from xtquant import xtconstant
        from xtquant.xttrader import XtQuantTrader
        from xtquant.xttype import StockAccount as ShimStockAccount

        self.assertEqual(xtconstant.STOCK_BUY, STOCK_BUY)
        self.assertEqual(xtconstant.FIX_PRICE, FIX_PRICE)
        self.assertEqual(xtconstant.SZ_MARKET, SZ_MARKET)
        self.assertIs(XtQuantTrader, BigQmtXtTrader)
        self.assertEqual(ShimStockAccount("acct").account_id, "acct")

    def test_configure_updates_imported_xt_trader_object_in_place(self):
        original = xt_trader
        configure(account_id="acct-new", redis_client=FakeRpcClient())

        self.assertIs(xt_trader, original)
        self.assertEqual(xt_trader.client.account_id, "acct-new")

    def test_client_reads_account_and_redis_from_private_config(self):
        module_name, old_env = self._with_fake_config()
        try:
            config = load_client_config()
            client = BigQmtRpcClient()
        finally:
            self._cleanup_fake_config(module_name, old_env)

        self.assertEqual(config["account_id"], "cfg-account")
        self.assertEqual(client.account_id, "cfg-account")
        self.assertEqual(client.redis_config["host"], "cfg-host")
        self.assertEqual(client.redis_config["port"], 6380)
        self.assertEqual(client.redis_config["db"], 6)
        self.assertEqual(client.redis_config["username"], "cfg-user")
        self.assertEqual(client.redis_config["password"], "cfg-pass")
        self.assertEqual(client.timeout_seconds, 9)

    def test_explicit_client_params_override_private_config(self):
        module_name, old_env = self._with_fake_config()
        try:
            client = BigQmtRpcClient(
                account_id="explicit-account",
                redis_config={"host": "explicit-host", "password": ""},
                timeout_seconds=3,
            )
        finally:
            self._cleanup_fake_config(module_name, old_env)

        self.assertEqual(client.account_id, "explicit-account")
        self.assertEqual(client.redis_config["host"], "explicit-host")
        self.assertEqual(client.redis_config["port"], 6380)
        self.assertEqual(client.redis_config["password"], "")
        self.assertEqual(client.timeout_seconds, 3)

    def test_trader_falls_back_to_cached_positions_when_rpc_fails(self):
        class FailingRpcClient(FakeRpcClient):
            def call(self, method, params=None, account_id=None, timeout_seconds=None):
                if method in ("query_stock_asset", "query_stock_positions", "query_stock_position"):
                    raise RuntimeError("rpc down")
                return super().call(method, params, account_id, timeout_seconds)

        client = FailingRpcClient()
        client.redis.hashes["bigqmt:positions:acct"] = {
            "account_id": "acct",
            "asset": {"cash": 123.0, "total_asset": 456.0},
            "positions": {
                "600000.SH": {
                    "stock_code": "600000.SH",
                    "volume": 100,
                    "available": 80,
                    "cost": 10.5,
                    "stock_name": "cached",
                }
            },
        }
        trader = BigQmtXtTrader(account_id="acct")
        trader.client = client
        acc = StockAccount("acct")

        asset = trader.query_stock_asset(acc)
        positions = trader.query_stock_positions(acc)
        single = trader.query_stock_position(acc, "600000")

        self.assertEqual(asset.cash, 123.0)
        self.assertEqual(asset.total_asset, 456.0)
        self.assertEqual(positions[0].stock_code, "600000.SH")
        self.assertEqual(positions[0].can_use_volume, 80)
        self.assertEqual(single.stock_name, "cached")

    def test_zmq_query_failure_never_falls_back_to_redis_cache(self):
        class FailingZmqClient(FakeRpcClient):
            transport_name = "zmq"

            def call(self, method, params=None, account_id=None, timeout_seconds=None):
                raise RuntimeError("zmq timeout")

            def _redis(self):
                raise AssertionError("ZMQ query must not access Redis")

        trader = BigQmtXtTrader(account_id="acct")
        trader.client = FailingZmqClient()
        acc = StockAccount("acct")

        with self.assertRaisesRegex(RuntimeError, "zmq timeout"):
            trader.query_stock_asset(acc)
        with self.assertRaisesRegex(RuntimeError, "zmq timeout"):
            trader.query_stock_positions(acc)
        with self.assertRaisesRegex(RuntimeError, "zmq timeout"):
            trader.query_stock_position(acc, "600000.SH")

    def test_client_call_via_transport_builds_valid_request(self):
        # Regression: the swappable-transport path in BigQmtRpcClient.call() built
        # request_id with __import__("uuid").uuid.uuid4() (AttributeError), crashing
        # every non-redis transport on first call. This path had no coverage.
        captured = {}

        class _FakeTransport:
            def send_request(self, request, timeout_seconds):
                captured["request"] = request
                captured["timeout"] = timeout_seconds
                return {"ok": True, "data": {"pong": True}}

        client = BigQmtRpcClient(account_id="acct", redis_config={"host": "127.0.0.1"})
        client.transport_name = "zmq"
        client._transport_instance = _FakeTransport()

        result = client.call("ping", {"x": 1})

        self.assertEqual(result, {"pong": True})
        request = captured["request"]
        self.assertEqual(request["method"], "ping")
        self.assertEqual(request["account_id"], "acct")
        self.assertEqual(request["params"], {"x": 1})
        # request_id must be a real 32-char uuid hex, not a crash.
        self.assertEqual(len(request["request_id"]), 32)
        int(request["request_id"], 16)

    def test_zmq_with_explicit_address_never_builds_redis_discovery(self):
        client = BigQmtRpcClient(
            account_id="acct",
            redis_config={
                "transport": "zmq",
                "zmq": {"connect_address": "tcp://127.0.0.1:20146"},
            },
        )

        def forbidden_redis():
            raise AssertionError("ZMQ explicit-address mode must not touch Redis")

        client._redis = forbidden_redis
        transport = client._transport()

        self.assertEqual(transport.name, "zmq")
        self.assertEqual(transport.connect_address, "tcp://127.0.0.1:20146")
        self.assertIsNone(transport.discovery_redis_client)


if __name__ == "__main__":
    unittest.main()
