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

    def test_zmq_transport_keeps_configured_value(self):
        self.assertFalse(_resolve_background_threads("zmq", False))
        self.assertFalse(_resolve_background_threads("ZMQ", False))
        self.assertTrue(_resolve_background_threads("zmq", True))

    def test_other_non_redis_transports_force_background_threads_on(self):
        for name in ("mysql", "shm"):
            self.assertTrue(_resolve_background_threads(name, False), name)
            self.assertTrue(_resolve_background_threads(name, True), name)

    def test_is_redis_transport(self):
        for name in ("redis", "", "default", None, "REDIS", "Default"):
            self.assertTrue(_is_redis_transport(name), name)
        for name in ("zmq", "mysql", "shm", "ZMQ"):
            self.assertFalse(_is_redis_transport(name), name)


if __name__ == "__main__":
    unittest.main()
