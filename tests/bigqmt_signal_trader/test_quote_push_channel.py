import json
import os
import sys
import threading
import time
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.quote_push_channel import (
    _HAS_MSGPACK,
    RedisQuotePushChannel,
    ZmqQuotePushChannel,
    decode_push_payload,
    encode_push_payload,
)


def _msgpack_packb(payload):
    import msgpack

    return msgpack.packb(payload, use_bin_type=True)


class FakePubSub:
    def __init__(self, redis_client):
        self._redis = redis_client
        self._channels = []
        self._closed = False

    def subscribe(self, *channels):
        self._channels.extend(channels)

    def get_message(self, timeout=0.1):
        # Pull one queued message for a subscribed channel, if any.
        for _ in range(50):
            for channel in self._channels:
                queue = self._redis.messages.setdefault(channel, [])
                if queue:
                    return {"type": "message", "channel": channel, "data": queue.pop(0)}
            time.sleep(0.01)
        return None

    def close(self):
        self._closed = True


class FakeRedis:
    def __init__(self):
        self.messages = {}

    def publish(self, channel, value):
        self.messages.setdefault(channel, []).append(value)
        return 1

    def pubsub(self, ignore_subscribe_messages=True):
        return FakePubSub(self)


class PayloadCodecTest(unittest.TestCase):
    def test_roundtrip(self):
        payload = {"combo_key": "SH,SZ", "data": {"000001.SZ": {"lastPrice": 10.5}}, "ts": 1.5}
        blob = encode_push_payload(payload)
        self.assertEqual(decode_push_payload(blob), payload)

    def test_binary_blob(self):
        # Wire encoding must be bytes (msgpack or utf-8 json), not str.
        blob = encode_push_payload({"a": 1})
        self.assertIsInstance(blob, (bytes, bytearray))

    def test_decode_json_wire_with_msgpack_available(self):
        # 服务端未装 msgpack 时用 json 兜底编码;若客户端装了 msgpack,
        # msgpack.unpackb 会把 json 文本首字节 '{'(0x7B) 当整数解析并抛
        # ExtraData("unpack(b) received extra data")——真实联调中触发。
        # decode 必须识别并回退到 json。
        payload = {"combo_key": "000001.SZ", "data": {"000001.SZ": {"lastPrice": 11.19}}}
        wire = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.assertIsInstance(wire, bytes)
        self.assertEqual(decode_push_payload(wire), payload)

    def test_decode_msgpack_wire_without_msgpack_available(self):
        # 反向:服务端装了 msgpack、客户端未装(仅 json 可用)。msgpack 字节流
        # 通常不是合法 utf-8,json 解码会失败——此时应显式尝试 msgpack 解码,
        # 而不是吞掉异常返回 None。
        payload = {"combo_key": "SH,SZ", "data": {"600000.SH": {"lastPrice": 9.9}}}
        if not _HAS_MSGPACK:
            self.skipTest("msgpack not installed on this client")
        wire = _msgpack_packb(payload)
        self.assertEqual(decode_push_payload(wire), payload)


class ZmqPushChannelTest(unittest.TestCase):
    def test_pub_sub_roundtrip_and_topic_filter(self):
        zmq = __import__("zmq")
        ctx = zmq.Context.instance()
        pub_addr = "inproc://quote-push-test-%d" % id(self)

        server = ZmqQuotePushChannel(bind_address=pub_addr, context=ctx)
        server.start_publisher()

        received = []
        done = threading.Event()

        client = ZmqQuotePushChannel(connect_address=pub_addr, context=ctx)

        def on_msg(topic, data):
            received.append((topic, data))
            done.set()

        client.start_subscriber(["SH,SZ"], on_msg)
        try:
            # Give the SUB a moment to connect + apply the subscription filter.
            time.sleep(0.2)
            server.publish("SH,SZ", {"000001.SZ": {"lastPrice": 10.5}})
            server.publish("SH", {"600000.SH": {"lastPrice": 9.9}})  # filtered out
            self.assertTrue(done.wait(2.0), "subscriber did not receive the SH,SZ push")
        finally:
            client.stop()
            server.stop()

        self.assertEqual(len(received), 1)
        topic, data = received[0]
        self.assertEqual(topic, "SH,SZ")
        self.assertEqual(data["000001.SZ"]["lastPrice"], 10.5)

    def test_stop_from_foreign_thread_while_sub_active_does_not_crash(self):
        """Regression: stop() used to close the SUB socket from the calling
        thread while the sub thread was still polling it -> Windows ZMQ
        signaler abort -> QMT process crash ("auto-exit"). The sub thread must
        close its own socket; repeated stop/start (topic changes) must be safe."""
        zmq = __import__("zmq")
        ctx = zmq.Context.instance()
        pub_addr = "inproc://quote-push-stop-%d" % id(self)

        server = ZmqQuotePushChannel(bind_address=pub_addr, context=ctx)
        server.start_publisher()
        client = ZmqQuotePushChannel(connect_address=pub_addr, context=ctx)
        try:
            for i in range(5):
                # Simulate _sync_subscriber_locked topic changes: start a
                # subscriber, immediately stop it from this (foreign) thread.
                client.start_subscriber(["T%d" % i], lambda t, d: None)
                time.sleep(0.05)
                client.stop()  # must not raise / abort
        finally:
            client.stop()
            server.stop()
        # Reaching here without an abort/crash is the assertion.
        self.assertTrue(True)


class ZmqSubscriberLeakTest(unittest.TestCase):
    """#403: a subscriber whose join times out must still die.

    Pre-fix, _sub_loop keyed on the shared self._running: stop() set it
    False, the next start_subscriber() set it back True, and a thread whose
    join timed out (blocked inside on_msg) saw True again and lived forever
    on its OLD cumulative topic set. WholeQuoteClientSession re-subscribes
    on every topic-set change, so N batches left up to N-1 zombies and early
    chunks arrived once per zombie (measured 1.7x-7x, stair-stepped).

    Now every subscriber captures its own Event; a join timeout means "still
    in its callback", not "leaked" -- the set Event kills it the moment the
    callback returns.
    """

    def test_resubscribe_does_not_duplicate_after_slow_callback(self):
        zmq = __import__("zmq")
        ctx = zmq.Context.instance()
        pub_addr = "inproc://quote-push-leak-%d" % id(self)

        server = ZmqQuotePushChannel(bind_address=pub_addr, context=ctx)
        server.start_publisher()
        client = ZmqQuotePushChannel(connect_address=pub_addr, context=ctx)

        block = threading.Event()        # holds the first callback hostage
        first_entered = threading.Event()
        first_calls = []
        second_calls = []

        def slow_first(topic, data):
            first_entered.set()
            block.wait(3.0)              # stays inside on_msg past the join
            first_calls.append((topic, data))

        if hasattr(client, "subscriber_join_seconds"):
            client.subscriber_join_seconds = 0.2
        client.start_subscriber(["A"], slow_first)
        try:
            time.sleep(0.2)              # let the SUB filter propagate
            server.publish("A", {"n": 1})
            self.assertTrue(first_entered.wait(2.0), "first subscriber never fired")

            # The session's pattern: the topic set changed, stop + start.
            # The old thread is still inside slow_first, so the join times
            # out here -- pre-fix it was abandoned and then revived by
            # start's shared-flag True.
            client.stop()
            client.start_subscriber(["A", "B"],
                                    lambda t, d: second_calls.append((t, d)))
            time.sleep(0.2)

            self.assertEqual(first_calls, [], "zombie callback still blocked?")
            block.set()                  # the zombie's callback returns now

            # Post-fix the zombie's own Event was set by _stop_subscriber:
            # it exits and closes its socket. Pre-fix it loops back into
            # poll() and keeps receiving topic A forever.
            active = getattr(client, "active_subscribers", None)
            if active is not None:
                deadline = time.time() + 3.0
                while time.time() < deadline and active() > 1:
                    time.sleep(0.05)
                self.assertEqual(active(), 1,
                                 "the superseded subscriber did not exit")

            server.publish("A", {"n": 3})
            time.sleep(0.4)
            self.assertEqual([d["n"] for _, d in second_calls if d.get("n") == 3], [3],
                             "msg 3 must arrive exactly once on the live subscriber")
            self.assertEqual([d for _, d in first_calls if d.get("n") == 3], [],
                             "the zombie must not receive after its stop event was set")
        finally:
            block.set()
            client.stop()
            server.stop()

    def test_active_subscribers_is_zero_or_one_in_health(self):
        zmq = __import__("zmq")
        ctx = zmq.Context.instance()
        pub_addr = "inproc://quote-push-active-%d" % id(self)

        server = ZmqQuotePushChannel(bind_address=pub_addr, context=ctx)
        server.start_publisher()
        client = ZmqQuotePushChannel(connect_address=pub_addr, context=ctx)
        try:
            self.assertEqual(client.active_subscribers(), 0)
            client.start_subscriber(["A"], lambda t, d: None)
            time.sleep(0.1)
            self.assertEqual(client.active_subscribers(), 1)
            client.stop()
            self.assertEqual(client.active_subscribers(), 0)
        finally:
            client.stop()
            server.stop()


class RedisPushChannelTest(unittest.TestCase):
    def test_connection_failures_reconnect_and_deliver_without_changing_topics(self):
        for stage in ("pubsub", "subscribe", "get_message"):
            with self.subTest(stage=stage):
                redis_client = FakeRedis()
                failed = FakePubSub(redis_client)
                replacement = FakePubSub(redis_client)
                if stage != "pubsub":
                    setattr(failed, stage, mock.Mock(side_effect=ConnectionError("offline failure")))
                redis_client.pubsub = mock.Mock(side_effect=[
                    ConnectionError("offline failure") if stage == "pubsub" else failed, replacement])
                client = RedisQuotePushChannel(redis_client, account_id="acct")
                received, done = [], threading.Event()

                def callback(topic, data):
                    received.append((topic, data))
                    done.set()

                redis_client.publish(client._channel("SH"), encode_push_payload({"data": {"x": 1}}))
                client.start_subscriber(["SH"], callback)
                try:
                    self.assertTrue(done.wait(3.0), "connection recovered but callbacks did not")
                    self.assertEqual(redis_client.pubsub.call_count, 2)
                    self.assertEqual(received, [("SH", {"x": 1})])
                    if stage != "pubsub":
                        self.assertTrue(failed._closed)
                finally:
                    client.stop()
                self.assertTrue(replacement._closed)

    def test_stop_during_reconnect_backoff_does_not_open_another_connection(self):
        redis_client = FakeRedis()
        failed = FakePubSub(redis_client)
        failed.get_message = mock.Mock(side_effect=ConnectionError("offline failure"))
        closed = threading.Event()
        failed.close = closed.set
        redis_client.pubsub = mock.Mock(return_value=failed)
        client = RedisQuotePushChannel(redis_client)
        client.start_subscriber(["SH"], lambda *_: None)
        thread = client._thread
        try:
            self.assertTrue(closed.wait(2.0))
        finally:
            client.stop()
        self.assertFalse(thread.is_alive())
        self.assertEqual(redis_client.pubsub.call_count, 1)

    def test_pub_sub_roundtrip(self):
        redis_client = FakeRedis()
        server = RedisQuotePushChannel(redis_client, account_id="acct")
        server.start_publisher()

        received = []
        done = threading.Event()
        client = RedisQuotePushChannel(redis_client, account_id="acct")

        def on_msg(topic, data):
            received.append((topic, data))
            done.set()

        client.start_subscriber(["SH,SZ"], on_msg)
        try:
            time.sleep(0.1)
            server.publish("SH,SZ", {"000001.SZ": {"lastPrice": 10.5}})
            self.assertTrue(done.wait(2.0), "redis subscriber did not receive the push")
        finally:
            client.stop()
            server.stop()

        self.assertEqual(received[0][0], "SH,SZ")
        self.assertEqual(received[0][1]["000001.SZ"]["lastPrice"], 10.5)

    def test_channel_name_scoped_by_account(self):
        redis_client = FakeRedis()
        server = RedisQuotePushChannel(redis_client, account_id="acct")
        server.start_publisher()
        server.publish("SH", {"x": 1})
        server.stop()
        self.assertIn("bigqmt:quote_push:acct:SH", redis_client.messages)


if __name__ == "__main__":
    unittest.main()
