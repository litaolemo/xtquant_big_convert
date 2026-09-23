# coding: utf-8
"""Client exec-event channel selection must mirror the server's sink choice
(issue #144).

The server publishes order/trade events to Redis FIRST whenever it can build
a Redis client -- even when the RPC transport is zmq -- and only falls to the
quote push channel after repeated Redis publish failures (strategy
_exec_event_sink, issue #145). The client listener used to choose by
transport instead: zmq -> push channel only. So a zmq deployment with a
working Redis had every event published to Redis while the client listened
on the push channel -- on_stock_order/on_stock_trade never fired.

Reproduced live 2026-09-02 on Guojin 2.1.19.0 (zmq transport + reachable
Redis): the day's order events sat in the Redis stream
bigqmt:order_events:<account> while a zmq-transport XtQuantTrader callback
received nothing.

The listener now re-selects per reconnect round: reachable Redis -> the
per-account Redis channels; otherwise the push channel (zmq) or a retry
(redis transport with Redis down).
"""

import json
import os
import sys
import threading
import time
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import exec_events
from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader, XtQuantTraderCallback


ORDER_EVENT = {
    "event_type": "order", "account_id": "acct", "stock_code": "601398.SH",
    "action": "BUY", "order_sys_id": "sys-1",
}


class _Recorder(XtQuantTraderCallback):
    def __init__(self):
        self.seen = []
        self.lock = threading.Lock()

    def on_stock_order(self, order):
        with self.lock:
            self.seen.append(("order", order.stock_code))

    def names(self):
        with self.lock:
            return [item[0] for item in self.seen]


class _FakePubSub(object):
    """Redis pub/sub stand-in: replays queued messages, then stops the loop."""

    def __init__(self, messages, stop):
        self.subscribed = []
        self.closed = False
        self._messages = list(messages)
        self._stop = stop

    def subscribe(self, *channels):
        self.subscribed.extend(channels)

    def get_message(self, timeout=1.0):
        if self._messages:
            return {"type": "message", "data": self._messages.pop(0)}
        self._stop()
        return None

    def close(self):
        self.closed = True


class _FakeRedisClient(object):
    def __init__(self, messages=(), ping_ok=True, stop=None):
        self._messages = list(messages)
        self._ping_ok = ping_ok
        self._stop = stop or (lambda: None)
        self.pings = 0
        self.pubsubs = []

    def ping(self):
        self.pings += 1
        if not self._ping_ok:
            raise RuntimeError("redis unreachable")
        return True

    def pubsub(self, ignore_subscribe_messages=True):
        ps = _FakePubSub(self._messages, self._stop)
        self.pubsubs.append(ps)
        return ps


class _FakePushChannel(object):
    def __init__(self, stop):
        self.topics = None
        self.stopped = False
        self._stop = stop

    def start_subscriber(self, topics, on_msg):
        self.topics = list(topics)
        self._stop()  # one round is enough for the assertion

    def stop(self):
        self.stopped = True


def _trader(transport, redis_client):
    trader = BigQmtXtTrader(account_id="acct")
    trader.client = types.SimpleNamespace(
        transport_name=transport,
        account_id="acct",
        _redis=lambda: redis_client,
    )
    recorder = _Recorder()
    trader.register_callback(recorder)
    return trader, recorder


class _patched_channel_builder(object):
    """Patch the module-level push-channel builder (#366).

    These tests used to monkeypatch ``trader._build_quote_push_channel`` onto
    the instance -- which is exactly how #366 stayed invisible: the real
    trader class never had that method, so the zmq exec listener raised
    AttributeError into a silent retry loop while every test greened. The
    builder is module-level now; patch there.
    """

    def __init__(self, channel=None, raiser=None):
        self._channel = channel
        self._raiser = raiser
        self._real = None
        self._mod = None

    def __enter__(self):
        import bigqmt_signal_trader.xtquant_compat as compat
        self._mod = compat
        self._real = compat._build_quote_push_channel
        if self._raiser is not None:
            compat._build_quote_push_channel = lambda client: self._raiser()
        else:
            compat._build_quote_push_channel = lambda client: self._channel
        return self

    def __exit__(self, *exc):
        self._mod._build_quote_push_channel = self._real
        return False


class ChannelSelectionTest(unittest.TestCase):
    def test_zmq_transport_with_working_redis_subscribes_redis_channels(self):
        """The #144 shape: zmq transport, Redis reachable -> Redis channels."""
        trader, recorder = _trader("zmq", None)
        redis_client = _FakeRedisClient(
            messages=[json.dumps(ORDER_EVENT).encode("utf-8")],
            stop=lambda: setattr(trader, "_event_running", False),
        )
        trader.client._redis = lambda: redis_client
        # Recorded rather than self.fail() so the pre-fix code (which goes
        # straight to the push channel) fails cleanly instead of hanging.
        push = _FakePushChannel(
            stop=lambda: setattr(trader, "_event_running", False))

        trader._event_running = True
        with _patched_channel_builder(push):
            trader._event_loop()

        self.assertIsNone(push.topics,
                          "push channel must not be built when Redis is reachable")
        ps = redis_client.pubsubs[0]
        self.assertEqual(
            set(ps.subscribed),
            {exec_events.order_channel("acct"),
             exec_events.trade_channel("acct"),
             exec_events.order_error_channel("acct"),
             exec_events.cancel_error_channel("acct")})
        self.assertEqual(recorder.names(), ["order"])
        self.assertTrue(ps.closed)

    def test_zmq_transport_without_redis_uses_push_channel(self):
        trader, _ = _trader("zmq", None)
        redis_client = _FakeRedisClient(ping_ok=False)
        trader.client._redis = lambda: redis_client
        push = _FakePushChannel(
            stop=lambda: setattr(trader, "_event_running", False))

        trader._event_running = True
        with _patched_channel_builder(push):
            trader._event_loop()

        self.assertEqual(
            set(push.topics or []),
            {"exec:order", "exec:trade", "exec:order_error", "exec:cancel_error"})
        self.assertTrue(push.stopped)

    def test_redis_transport_with_working_redis_still_uses_redis(self):
        trader, recorder = _trader("redis", None)
        redis_client = _FakeRedisClient(
            messages=[json.dumps(ORDER_EVENT).encode("utf-8")],
            stop=lambda: setattr(trader, "_event_running", False),
        )
        trader.client._redis = lambda: redis_client

        trader._event_running = True
        with _patched_channel_builder(raiser=lambda: self.fail(
                "redis transport must not build a push channel")):
            trader._event_loop()

        self.assertEqual(len(redis_client.pubsubs), 1)
        self.assertEqual(recorder.names(), ["order"])

    def test_redis_transport_with_redis_down_keeps_retrying_redis(self):
        trader, _ = _trader("redis", None)
        # stop= lets the PRE-fix code (no ping, subscribes a dead redis and
        # loops on empty polls) exit instead of hanging the reverse check.
        redis_client = _FakeRedisClient(
            ping_ok=False, stop=lambda: setattr(trader, "_event_running", False))
        trader.client._redis = lambda: redis_client

        def stop_after_first_retry():
            if redis_client.pings >= 1:
                trader._event_running = False

        push = _FakePushChannel(stop=stop_after_first_retry)

        real_sleep = time.sleep
        def fast_sleep(seconds):
            stop_after_first_retry()
            real_sleep(0.01)

        trader._event_running = True
        with _patched_sleep(fast_sleep):
            with _patched_channel_builder(push):
                trader._event_loop()

        self.assertGreaterEqual(redis_client.pings, 1)
        self.assertIsNone(push.topics, "redis transport must not fall to the "
                                      "push channel -- nothing would publish there")

    def test_redis_dying_mid_round_reselects_push_next_round(self):
        """A server that demotes Redis mid-session (issue #145) must be
        followed: the Redis round dies with the connection, the next round
        finds Redis unreachable and selects the push channel."""
        trader, _ = _trader("zmq", None)

        class DyingRedis(_FakeRedisClient):
            """Round 1 reachable but the subscription dies in use; from then
            on unreachable -- the realistic mid-session death."""

            def ping(self):
                if self.pings >= 1:
                    raise RuntimeError("redis unreachable")
                return super().ping()

        redis_client = DyingRedis(stop=lambda: None)
        trader.client._redis = lambda: redis_client

        class DyingPubSub(_FakePubSub):
            def get_message(self, timeout=1.0):
                raise ConnectionError("redis went away mid-session")

        redis_client.pubsub = lambda ignore_subscribe_messages=True: DyingPubSub([], lambda: None)

        push = _FakePushChannel(
            stop=lambda: setattr(trader, "_event_running", False))

        real_sleep = time.sleep
        def fast_sleep(seconds):
            real_sleep(0.01)

        trader._event_running = True
        with _patched_sleep(fast_sleep):
            with _patched_channel_builder(push):
                trader._event_loop()

        self.assertIsNotNone(push.topics)


class PushChannelBuilderResolutionTest(unittest.TestCase):
    """#366: BigQmtXtTrader never had `_build_quote_push_channel` -- it lives
    on BigQmtXtData. The exec-event listener calling it as `self.<method>`
    raised AttributeError into a silent retry loop, so a zmq deployment
    without Redis got ZERO exec callbacks (single-file zmq deploys hit this
    from day one). The builder is module-level now; this test drives the
    listener with no instance monkeypatching -- the thing the old tests did
    everywhere, which is exactly how the bug stayed invisible since #76."""

    def test_the_trader_class_has_no_such_method(self):
        self.assertFalse(hasattr(BigQmtXtTrader, "_build_quote_push_channel"))

    def test_the_listener_resolves_the_module_level_builder(self):
        trader, _ = _trader("zmq", None)
        push = _FakePushChannel(
            stop=lambda: setattr(trader, "_event_running", False))

        trader._event_running = True
        with _patched_channel_builder(push):
            trader._event_loop_push_channel()

        self.assertEqual(
            set(push.topics or []),
            {"exec:order", "exec:trade", "exec:order_error", "exec:cancel_error"})
        self.assertTrue(push.stopped)


class _patched_sleep(object):
    def __init__(self, fn):
        self._fn = fn
        self._real = None

    def __enter__(self):
        import bigqmt_signal_trader.xtquant_compat as compat
        self._mod = compat
        self._real = compat.time.sleep
        compat.time.sleep = self._fn
        return self

    def __exit__(self, *exc):
        self._mod.time.sleep = self._real
        return False


if __name__ == "__main__":
    unittest.main()
