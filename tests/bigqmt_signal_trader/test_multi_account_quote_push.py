# coding: utf-8
"""方式一 multi-account: the whole-quote push must reach every account (#315).

Reported as "subscribed successfully, no callbacks" on a single-instance
multi-account bridge (BIGQMT_ACCOUNT_TYPE_MAP). The push channel is keyed by
account: the server published to ``bigqmt:quote_push:<primary>:<topic>``
only, while a client configured with the secondary account subscribed
``bigqmt:quote_push:<secondary>:<topic>``. Its subscribe RPC rode its own
request channel to the shared handlers and returned a seq, so nothing
failed -- the pushes just went to a channel nobody was listening on.

Market data is not account-specific: the publisher now speaks for every
account the bridge serves, one publish per account (redis) or one extra
PUB bind per account (zmq). The client role is untouched.
"""

import os
import sys
import types
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.account_type_map import reload as reload_map  # noqa: E402
from bigqmt_signal_trader.quote_push_channel import (  # noqa: E402
    RedisQuotePushChannel,
    ZmqQuotePushChannel,
    decode_push_payload,
)
from bigqmt_signal_trader.quote_subscription_manager import (  # noqa: E402
    _served_account_ids,
    build_quote_subscription_service,
)


class _Redis(object):
    def __init__(self):
        self.published = []

    def publish(self, channel, value):
        self.published.append((channel, value))
        return 1


class _Ctx(object):
    def __init__(self):
        self._next = 0

    def subscribe_whole_quote(self, code_list, callback=None):
        self._next += 1
        self.callback = callback
        return self._next

    def unsubscribe_quote(self, sub_id):
        return 0


def _with_map(mapping):
    fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
    fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = dict(mapping)
    return mock.patch.dict("sys.modules", {"bigqmt_signal_trader_local_config": fake_cfg})


class RedisChannelFansOutTest(unittest.TestCase):
    def test_one_publish_per_account(self):
        redis = _Redis()
        channel = RedisQuotePushChannel(redis, account_id="primary", account_ids=["primary", "second"])

        channel.publish("600000.SH", {"lastPrice": 1.0})

        channels = [c for c, _ in redis.published]
        self.assertEqual(["bigqmt:quote_push:primary:600000.SH",
                          "bigqmt:quote_push:second:600000.SH"], channels)
        for _, raw in redis.published:
            self.assertEqual({"lastPrice": 1.0}, decode_push_payload(raw)["data"])

    def test_the_secondarys_channel_is_the_one_its_client_subscribes(self):
        """The client side derives the channel from its own account; the
        server side must produce exactly that name."""
        server = RedisQuotePushChannel(_Redis(), account_id="primary", account_ids=["second"])
        client = RedisQuotePushChannel(_Redis(), account_id="second")

        self.assertEqual(client._channel("t"), server._channel("t", "second"))

    def test_a_single_account_publishes_once_as_before(self):
        redis = _Redis()
        RedisQuotePushChannel(redis, account_id="only").publish("t", {})
        self.assertEqual(["bigqmt:quote_push:only:t"], [c for c, _ in redis.published])

    def test_duplicates_and_blanks_in_the_list_collapse(self):
        channel = RedisQuotePushChannel(_Redis(), account_id="a", account_ids=["a", "", "b", "b", None])
        self.assertEqual(["a", "b"], channel.account_ids)

    def test_one_failing_publish_does_not_stop_the_others(self):
        class Flaky(_Redis):
            def publish(self, channel, value):
                if ":a:" in channel:
                    raise RuntimeError("boom")
                return super(Flaky, self).publish(channel, value)

        redis = Flaky()
        RedisQuotePushChannel(redis, account_id="a", account_ids=["b"]).publish("t", {})
        self.assertEqual(["bigqmt:quote_push:b:t"], [c for c, _ in redis.published])


class ServedAccountsTest(unittest.TestCase):
    def test_primary_first_then_the_map(self):
        with _with_map({"ACCT2": "FUTURE", "ACCT1": "STOCK"}):
            reload_map()
            self.assertEqual(["ACCT1", "ACCT2"], _served_account_ids("ACCT1"))
        reload_map()

    def test_an_explicit_list_wins_over_the_map(self):
        with _with_map({"ACCT1": "STOCK", "ACCT2": "FUTURE"}):
            reload_map()
            self.assertEqual(["ACCT1", "X"], _served_account_ids("ACCT1", ["X"]))
        reload_map()

    def test_no_map_means_the_primary_alone(self):
        sys.modules.pop("bigqmt_signal_trader_local_config", None)
        reload_map()
        self.assertEqual(["ACCT1"], _served_account_ids("ACCT1"))


class BuilderWiresTheMapTest(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop("bigqmt_signal_trader_local_config", None)
        reload_map()

    def test_a_push_reaches_the_secondary_account(self):
        """End to end through the manager: a ContextInfo push for a
        subscribed combo lands on both accounts' channels."""
        redis = _Redis()
        with _with_map({"ACCT1": "STOCK", "ACCT2": "FUTURE"}):
            reload_map()
            ctx = _Ctx()
            manager, channel = build_quote_subscription_service(
                ctx, transport_name="redis", account_id="ACCT1", redis_client=redis, enabled=True)
        self.assertEqual(["ACCT1", "ACCT2"], channel.account_ids)

        manager.subscribe("issue315", "s1", ["600000.SH"])
        ctx.callback({"600000.SH": {"lastPrice": 3.69}})

        published = sorted(c for c, _ in redis.published)
        self.assertEqual(2, len(published), published)
        self.assertTrue(any(":ACCT2:" in c for c in published), published)
        self.assertTrue(any(":ACCT1:" in c for c in published), published)

    def test_zmq_binds_every_accounts_endpoint(self):
        with _with_map({"ACCT1": "STOCK", "ACCT2": "FUTURE"}):
            reload_map()
            _manager, channel = build_quote_subscription_service(
                _Ctx(), transport_name="zmq", account_id="ACCT1",
                zmq_bind_address="tcp://127.0.0.1:15561", enabled=True)
        self.assertIsInstance(channel, ZmqQuotePushChannel)
        self.assertEqual("tcp://127.0.0.1:15561", channel.bind_address)
        from bigqmt_signal_trader.quote_subscription_manager import _default_quote_push_zmq_bind
        self.assertEqual([_default_quote_push_zmq_bind("ACCT2")], channel.extra_bind_addresses)

    def test_the_single_account_shape_is_unchanged(self):
        sys.modules.pop("bigqmt_signal_trader_local_config", None)
        reload_map()
        redis = _Redis()
        manager, channel = build_quote_subscription_service(
            _Ctx(), transport_name="redis", account_id="ACCT1", redis_client=redis, enabled=True)
        self.assertEqual(["ACCT1"], channel.account_ids)


if __name__ == "__main__":
    unittest.main()
