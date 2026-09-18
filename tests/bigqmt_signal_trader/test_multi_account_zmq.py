# coding: utf-8
"""方式一 multi-account on zmq (#334, @simonfantasy).

``_build_secondary`` had one branch, redis: on a zmq deployment it built a
RedisTransport around a redis client that was None (or, on the no-redis
single-file build, could not be imported at all), and the secondary's
listener threads died with ``'NoneType' object has no attribute 'pubsub'``
-- or, with redis importable, sat on a 127.0.0.1:6379 nobody ran.

Now the secondary gets its own zmq endpoint: port derived from ITS
account_id (which is how a zmq client configured with that account already
derives the address it connects to), host inherited from the primary's
bind_address. The exec-event poller (#320) publishes over the quote push
channel there, since there is no redis sink.
"""

import os
import sys
import types
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import multi_account  # noqa: E402
from bigqmt_signal_trader.transports.zmq_transport import _default_zmq_port  # noqa: E402


def _primary():
    primary = mock.MagicMock()
    primary.account_id = "111"
    primary.redis = None
    primary.listen_redis = None
    primary.handlers = types.SimpleNamespace(order_gateway=object(), quote_subscription_manager=None)
    return primary


class SecondaryZmqConfigTest(unittest.TestCase):
    def test_the_primarys_endpoint_is_not_inherited(self):
        cfg = multi_account._secondary_zmq_config(
            {"zmq": {"bind_address": "tcp://0.0.0.0:15561", "port": 15561, "account_id": "111",
                     "connect_address": "tcp://127.0.0.1:15561", "io_threads": 2}}, "222")
        self.assertNotIn("bind_address", cfg)
        self.assertNotIn("port", cfg)
        self.assertNotIn("account_id", cfg)
        self.assertNotIn("connect_address", cfg)
        self.assertEqual(2, cfg["io_threads"])

    def test_the_host_is_inherited_from_the_primarys_bind_address(self):
        cfg = multi_account._secondary_zmq_config({"zmq": {"bind_address": "tcp://0.0.0.0:15561"}}, "222")
        self.assertEqual("0.0.0.0", cfg["host"])

    def test_an_explicit_host_wins(self):
        cfg = multi_account._secondary_zmq_config(
            {"zmq": {"bind_address": "tcp://0.0.0.0:15561", "host": "192.168.1.5"}}, "222")
        self.assertEqual("192.168.1.5", cfg["host"])

    def test_no_zmq_block_at_all(self):
        self.assertEqual({}, multi_account._secondary_zmq_config({}, "222"))


class SecondaryOnZmqTest(unittest.TestCase):
    def test_a_zmq_deployment_builds_a_zmq_secondary_on_its_own_port(self):
        service = multi_account._build_secondary(
            _primary(), "222", {"rpc": {"transport": "zmq", "zmq": {"bind_address": "tcp://0.0.0.0:15561"}}})

        self.assertIsNotNone(service, "no secondary on zmq")
        transport = service._transport
        self.assertEqual("zmq", transport.name)
        self.assertEqual("tcp://0.0.0.0:%d" % _default_zmq_port("222"), transport.bind_address)
        self.assertNotEqual(_default_zmq_port("111"), _default_zmq_port("222"))
        self.assertEqual("222", service.account_id)
        self.assertTrue(service.background_threads)

    def test_the_secondarys_port_is_what_a_client_for_that_account_derives(self):
        """The client side derives its connect address from ITS account_id;
        the server must bind exactly that."""
        from bigqmt_signal_trader.transports.zmq_transport import _default_zmq_address
        service = multi_account._build_secondary(_primary(), "222", {"rpc": {"transport": "zmq"}})
        self.assertEqual(_default_zmq_address("222"), service._transport.bind_address)

    def test_the_secondary_shares_the_primarys_handlers_under_its_own_account(self):
        primary = _primary()
        service = multi_account._build_secondary(primary, "222", {"rpc": {"transport": "zmq"}})
        self.assertIsInstance(service.handlers, multi_account.SecondaryHandlersProxy)
        self.assertEqual("222", service.handlers._secondary_account_id)

    def test_no_redis_client_is_touched_on_zmq(self):
        with mock.patch("bigqmt_signal_trader.adapters.redis_common.build_redis_client",
                        side_effect=AssertionError("redis must not be built on zmq")):
            service = multi_account._build_secondary(_primary(), "222", {"rpc": {"transport": "zmq"}})
        self.assertIsNotNone(service)

    def test_a_transport_without_per_account_endpoints_is_refused_not_broken(self):
        for name in ("pipe", "mysql", "shm"):
            self.assertIsNone(multi_account._build_secondary(
                _primary(), "222", {"rpc": {"transport": name}}), name)

    def test_redis_deployments_are_untouched(self):
        with mock.patch("bigqmt_signal_trader.adapters.redis_common.build_redis_client",
                        return_value=mock.MagicMock()) as built:
            service = multi_account._build_secondary(_primary(), "222", {"rpc": {"transport": "redis"}})
        self.assertIsNotNone(service)
        self.assertEqual("redis", service._transport.name)
        built.assert_called_once()


class ExecPollerOnZmqTest(unittest.TestCase):
    def test_the_poller_publishes_over_the_push_channel_when_there_is_no_redis(self):
        published = []

        class Gateway(object):
            def query_native_rows(self, account_id, kind, strategy_name=""):
                return []

        manager = types.SimpleNamespace(_on_push_publisher=lambda topic, data: published.append((topic, data)))
        primary = types.SimpleNamespace(
            redis=None, handlers=types.SimpleNamespace(order_gateway=Gateway(), quote_subscription_manager=manager))

        poller = multi_account._build_secondary_exec_poller(None, primary, ["222"], {"rpc": {}})

        self.assertIsNotNone(poller, "poller refused on zmq")
        row = types.SimpleNamespace(m_strOrderSysID="1", m_nOrderStatus=50, m_nVolumeTraded=0,
                                    m_strAccountID="222", m_strInstrumentID="600000",
                                    m_strExchangeID="SH", m_nOffsetFlag=48)
        poller.publish("order", "222", row)
        self.assertEqual(1, len(published))
        topic, event = published[0]
        self.assertEqual("exec:order", topic)
        self.assertEqual("222", event["account_id"])

    def test_without_redis_or_a_push_channel_the_poller_is_not_built(self):
        primary = types.SimpleNamespace(
            redis=None, handlers=types.SimpleNamespace(order_gateway=object(), quote_subscription_manager=None))
        self.assertIsNone(multi_account._build_secondary_exec_poller(None, primary, ["222"], {"rpc": {}}))


if __name__ == "__main__":
    unittest.main()
