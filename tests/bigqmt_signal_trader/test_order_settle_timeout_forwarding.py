# coding: utf-8
"""order_settle_timeout_seconds must reach the strategy when configured (#396).

Reported against 0.3.60: the strategy reads
``rpc_config.get("order_settle_timeout_seconds", 3.0)``, but the runtime's
``_apply_config`` rpc block never carried the key and
``configure_runtime_redis`` never read it from the redis config -- so a
value set in ``local_config`` silently never took effect and every settle
window stayed at the 3.0s default.

The chain now mirrors drain_budget_seconds: a module global (default None =
the strategy/handlers default owns it), read from the redis config, and
forwarded into the rpc block only when explicitly named -- forwarding None
would crash the strategy's ``float(...)``.
"""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import bigqmt_signal_trader_redis_rpc_runtime as runtime


class OrderSettleTimeoutForwardingTest(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "RPC_ORDER_SETTLE_TIMEOUT_SECONDS": runtime.RPC_ORDER_SETTLE_TIMEOUT_SECONDS,
            "configure": runtime.configure,
            "set_account_id": runtime.set_account_id,
        }
        self._captured = {}
        runtime.configure = lambda **kwargs: self._captured.update(kwargs)
        runtime.set_account_id = lambda account_id: None

    def tearDown(self):
        for key, value in self._saved.items():
            setattr(runtime, key, value)

    def _rpc_block(self):
        runtime._apply_config("TESTACCOUNT")
        return self._captured["rpc"]

    def test_absent_when_not_configured(self):
        runtime.RPC_ORDER_SETTLE_TIMEOUT_SECONDS = None

        self.assertNotIn("order_settle_timeout_seconds", self._rpc_block())

    def test_forwarded_when_configured(self):
        runtime.RPC_ORDER_SETTLE_TIMEOUT_SECONDS = 10.0

        self.assertEqual(
            self._rpc_block().get("order_settle_timeout_seconds"), 10.0)

    def test_configure_runtime_redis_reads_the_key(self):
        runtime.RPC_ORDER_SETTLE_TIMEOUT_SECONDS = None

        runtime.configure_runtime_redis({"order_settle_timeout_seconds": 7.5})

        self.assertEqual(runtime.RPC_ORDER_SETTLE_TIMEOUT_SECONDS, 7.5)
        self.assertEqual(
            self._rpc_block().get("order_settle_timeout_seconds"), 7.5)

    def test_configure_runtime_redis_keeps_default_when_key_absent(self):
        runtime.RPC_ORDER_SETTLE_TIMEOUT_SECONDS = None

        runtime.configure_runtime_redis({"host": "127.0.0.1"})

        self.assertIsNone(runtime.RPC_ORDER_SETTLE_TIMEOUT_SECONDS)
        self.assertNotIn("order_settle_timeout_seconds", self._rpc_block())


if __name__ == "__main__":
    unittest.main()
