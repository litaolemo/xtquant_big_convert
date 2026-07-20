import datetime
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import bigqmt_signal_trader_strategy as strategy_module


class FakeApp:
    def __init__(self):
        self.inited = 0
        self.ticks = []
        self.orders = []
        self.trades = []
        self.sync_reasons = []

    def on_init(self, runtime):
        self.inited += 1

    def tick(self, now=None):
        self.ticks.append(now)

    def on_order_event(self, event):
        self.orders.append(event)

    def on_trade_event(self, event):
        self.trades.append(event)

    def sync_positions(self, reason):
        self.sync_reasons.append(reason)


class FakeContext:
    def __init__(self):
        self.accounts = []

    def set_account(self, account_id):
        self.accounts.append(account_id)


class FakeHistoryContext(FakeContext):
    def is_last_bar(self):
        return False


class FakeRpcService:
    def __init__(self):
        self.drained = []

    def drain_pending(self, max_items=20):
        self.drained.append(max_items)
        return 0

    def stop(self):
        pass


class BigQmtStrategyRunnerTest(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        strategy_module.reset_app()
        strategy_module.set_app_factory(lambda context: self.app)

    def tearDown(self):
        strategy_module.reset_app()
        strategy_module.set_app_factory(None)
        strategy_module.set_account_id("")

    def test_init_builds_app_and_calls_on_init(self):
        strategy_module.init(FakeContext())

        self.assertEqual(self.app.inited, 1)

    def test_init_sets_bigqmt_account_when_configured(self):
        context = FakeContext()
        strategy_module.set_account_id("test-account")

        strategy_module.init(context)

        self.assertEqual(context.accounts, ["test-account"])

    def test_init_detects_bigqmt_account_from_runtime_global(self):
        context = FakeContext()
        strategy_module.account = "runtime-account"
        try:
            strategy_module.init(context)
        finally:
            delattr(strategy_module, "account")

        self.assertEqual(context.accounts, ["runtime-account"])

    def test_adjust_forwards_to_app_tick(self):
        strategy_module.init(FakeContext())
        strategy_module.adjust(FakeContext())

        self.assertEqual(len(self.app.ticks), 1)
        self.assertIsInstance(self.app.ticks[0], datetime.datetime)

    def test_handlebar_forwards_to_app_tick(self):
        strategy_module.init(FakeContext())
        strategy_module.handlebar(FakeContext())

        self.assertEqual(len(self.app.ticks), 1)
        self.assertIsInstance(self.app.ticks[0], datetime.datetime)

    def test_adjust_skips_history_bars_when_bigqmt_exposes_is_last_bar(self):
        strategy_module.init(FakeContext())

        strategy_module.adjust(FakeHistoryContext())

        self.assertEqual(self.app.ticks, [])

    def test_adjust_drains_rpc_even_when_not_last_bar(self):
        rpc_service = FakeRpcService()
        strategy_module._rpc_service = rpc_service

        strategy_module.adjust(FakeHistoryContext())

        self.assertEqual(rpc_service.drained, [20])
        self.assertEqual(self.app.ticks, [])

    def test_zmq_rpc_build_does_not_create_redis_clients(self):
        config = {
            "account_id": "acct",
            "enable_rpc": True,
            "rpc": {
                "enabled": True,
                "account_id": "acct",
                "transport": "zmq",
                "zmq": {"connect_address": "tcp://127.0.0.1:20146"},
                "background_threads": True,
            },
            "qmt_api": {},
        }
        app = SimpleNamespace(order_gateway=None, position_sync_sink=None)

        with mock.patch(
            "bigqmt_signal_trader.adapters.redis_common.build_redis_client",
            side_effect=AssertionError("ZMQ mode must not build Redis clients"),
        ):
            service = strategy_module._build_rpc_service(FakeContext(), app, config)

        self.assertIsNone(service.listen_redis)
        self.assertIsNone(service.redis)
        self.assertEqual(service._transport.name, "zmq")

    def test_order_and_trade_callbacks_forward_to_app(self):
        strategy_module.init(FakeContext())
        order = object()
        trade = object()

        strategy_module.on_order(FakeContext(), order)
        strategy_module.on_trade(FakeContext(), trade)

        self.assertEqual(self.app.orders, [order])
        self.assertEqual(self.app.trades, [trade])

    def test_bigqmt_named_callbacks_forward_to_app(self):
        strategy_module.init(FakeContext())
        order = object()
        trade = object()

        strategy_module.order_callback(FakeContext(), order)
        strategy_module.deal_callback(FakeContext(), trade)

        self.assertEqual(self.app.orders, [order])
        self.assertEqual(self.app.trades, [trade])

    def test_manual_sync_forwards_to_app(self):
        strategy_module.init(FakeContext())

        strategy_module.sync_positions(FakeContext())

        self.assertEqual(self.app.sync_reasons, ["manual"])


if __name__ == "__main__":
    unittest.main()
