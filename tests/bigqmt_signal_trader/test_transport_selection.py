import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader_strategy import _is_redis_transport, _resolve_background_threads


class BackgroundThreadResolutionTest(unittest.TestCase):
    def test_redis_transport_keeps_configured_value(self):
        # Redis (default) honors the configured flag — adjust-drain path when off.
        self.assertFalse(_resolve_background_threads("redis", False))
        self.assertTrue(_resolve_background_threads("redis", True))
        self.assertFalse(_resolve_background_threads("", False))
        self.assertFalse(_resolve_background_threads("default", False))
        self.assertFalse(_resolve_background_threads(None, False))

    def test_zmq_transport_defaults_to_the_adjust_drain(self):
        # None = the local config never named rpc_background_threads (the
        # runtime only forwards the key when explicit). #343: that means the
        # drain now -- a router thread pays a tick per GIL acquisition
        # (deferred query 490ms vs 88ms measured live). An explicit True is
        # still honoured.
        self.assertFalse(_resolve_background_threads("zmq", None))
        self.assertFalse(_resolve_background_threads("ZMQ", None))
        self.assertTrue(_resolve_background_threads("zmq", True))

    def test_zmq_explicit_false_opts_into_adjust_drain(self):
        # An explicit rpc_background_threads=False selects the adjust-driven
        # drain: drain_request_queue polls the bound socket with a non-blocking
        # recv on each adjust tick, so requests ARE received without the
        # router thread -- and the round trip drops its cross-thread GIL
        # handoffs (~1 adjust tick each, #104).
        self.assertFalse(_resolve_background_threads("zmq", False))
        self.assertFalse(_resolve_background_threads("ZMQ", False))

    def test_mysql_explicit_false_opts_into_adjust_drain(self):
        # mysql also implements drain_request_queue, so the override stands.
        self.assertFalse(_resolve_background_threads("mysql", None))
        self.assertFalse(_resolve_background_threads("mysql", False))
        self.assertTrue(_resolve_background_threads("mysql", True))

    def test_drainless_transports_ignore_the_override(self):
        # shm has no drain_request_queue: without the receiver thread requests
        # would never be picked up, so even an explicit False cannot turn it
        # off here.
        self.assertTrue(_resolve_background_threads("shm", None))
        self.assertTrue(_resolve_background_threads("shm", False))
        self.assertTrue(_resolve_background_threads("shm", True))

    def test_is_redis_transport(self):
        for name in ("redis", "", "default", None, "REDIS", "Default"):
            self.assertTrue(_is_redis_transport(name), name)
        for name in ("zmq", "mysql", "shm", "ZMQ"):
            self.assertFalse(_is_redis_transport(name), name)


if __name__ == "__main__":
    unittest.main()
