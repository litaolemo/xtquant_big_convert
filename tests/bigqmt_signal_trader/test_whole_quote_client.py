import os
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.whole_quote_session import WholeQuoteClientSession


class FakeRpc:
    """Records control RPCs and returns canned subscribe responses."""

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, method, params):
        with self._lock:
            self.calls.append((method, dict(params)))
        if method == "subscribe_whole_quote":
            codes = sorted(str(c).upper() for c in params.get("codes") or [])
            return {"combo_key": ",".join(codes), "topic": ",".join(codes)}
        return {}

    def methods(self):
        with self._lock:
            return [m for m, _ in self.calls]


class FakeRpcWithRestart(FakeRpc):
    """Simulates a server restart: keepalive fails for a window of calls,
    then the server is back (subscribe succeeds again)."""

    def __init__(self, fail_start=0, fail_count=3):
        super().__init__()
        self.fail_start = fail_start
        self.fail_count = fail_count

    def __call__(self, method, params):
        with self._lock:
            self.calls.append((method, dict(params)))
            if method == "quote_keepalive":
                n = sum(1 for m, _ in self.calls if m == "quote_keepalive") - 1  # 本次
        if method == "quote_keepalive" and self.fail_start <= n < self.fail_start + self.fail_count:
            raise RuntimeError("server restarting")
        if method == "subscribe_whole_quote":
            codes = sorted(str(c).upper() for c in params.get("codes") or [])
            return {"combo_key": ",".join(codes), "topic": ",".join(codes)}
        return {}


class FakePushChannel:
    """Client-side push channel stand-in: lets tests inject server pushes."""

    def __init__(self):
        self.subscriptions = []  # list of (topics_tuple, on_msg)
        self.started = False
        self.stopped = False
        self._on_msg = None

    def start_subscriber(self, topics, on_msg):
        self.started = True
        self._on_msg = on_msg
        self.subscriptions.append(tuple(topics))

    def inject(self, topic, data):
        if self._on_msg is not None:
            self._on_msg(topic, data)

    def stop(self):
        self.stopped = True


class FakePushChannelWithTopics(FakePushChannel):
    """Like FakePushChannel but tracks the currently subscribed topic set, so
    the session can diff and reuse an existing subscriber."""

    def __init__(self):
        super().__init__()
        self.active_topics = frozenset()

    def start_subscriber(self, topics, on_msg):
        super().start_subscriber(topics, on_msg)
        self.active_topics = frozenset(topics)

    def stop(self):
        super().stop()
        self.active_topics = frozenset()


class WholeQuoteSessionTest(unittest.TestCase):
    def _session(self, **kwargs):
        rpc = FakeRpc()
        channel = FakePushChannel()
        session = WholeQuoteClientSession(
            rpc_call=rpc,
            push_channel=channel,
            client_id="client-test",
            heartbeat_interval_seconds=kwargs.pop("heartbeat_interval_seconds", 0.05),
            **kwargs,
        )
        return session, rpc, channel

    def test_subscribe_sends_rpc_and_returns_sub_id(self):
        session, rpc, _channel = self._session()
        sub_id = session.subscribe_whole_quote(["SH", "SZ"], callback=lambda d: None)
        self.assertIsNotNone(sub_id)
        self.assertIn("subscribe_whole_quote", rpc.methods())

    def test_subscribe_starts_push_channel_with_topic(self):
        session, _rpc, channel = self._session()
        session.subscribe_whole_quote(["SH", "SZ"], callback=lambda d: None)
        self.assertTrue(channel.started)
        self.assertIn(("SH,SZ",), channel.subscriptions)

    def test_incoming_push_invokes_callback(self):
        session, _rpc, channel = self._session()
        received = []
        session.subscribe_whole_quote(["SH"], callback=received.append)
        channel.inject("SH", {"000001.SZ": {"lastPrice": 10.5}})
        self.assertEqual(received, [{"000001.SZ": {"lastPrice": 10.5}}])

    def test_two_subscriptions_same_combo_share_one_push(self):
        session, _rpc, channel = self._session()
        got_a, got_b = [], []
        session.subscribe_whole_quote(["SH", "SZ"], callback=got_a.append)
        session.subscribe_whole_quote(["sz", "sh"], callback=got_b.append)
        channel.inject("SH,SZ", {"000001.SZ": {"lastPrice": 1.0}})
        self.assertEqual(len(got_a), 1)
        self.assertEqual(len(got_b), 1)

    def test_unsubscribe_stops_callback_and_sends_rpc(self):
        session, rpc, channel = self._session()
        received = []
        sub_id = session.subscribe_whole_quote(["SH"], callback=received.append)
        session.unsubscribe_quote(sub_id)
        self.assertIn("unsubscribe_whole_quote", rpc.methods())
        channel.inject("SH", {"x": 1})
        self.assertEqual(received, [])

    def test_keepalive_sent_for_active_subscriptions(self):
        session, rpc, _channel = self._session(heartbeat_interval_seconds=0.05)
        session.subscribe_whole_quote(["SH"], callback=lambda d: None)
        session.start()
        try:
            deadline = time.time() + 1.5
            while time.time() < deadline and rpc.methods().count("quote_keepalive") < 2:
                time.sleep(0.02)
        finally:
            session.stop()
        self.assertGreaterEqual(rpc.methods().count("quote_keepalive"), 2)

    def test_keepalive_stops_after_unsubscribe(self):
        session, rpc, _channel = self._session(heartbeat_interval_seconds=0.05)
        sub_id = session.subscribe_whole_quote(["SH"], callback=lambda d: None)
        session.start()
        time.sleep(0.15)
        session.unsubscribe_quote(sub_id)
        count_at_unsub = rpc.methods().count("quote_keepalive")
        time.sleep(0.2)
        session.stop()
        self.assertEqual(rpc.methods().count("quote_keepalive"), count_at_unsub)

    def test_replay_resubscribes_all_active(self):
        session, rpc, _channel = self._session()
        session.subscribe_whole_quote(["SH"], callback=lambda d: None)
        session.subscribe_whole_quote(["SZ"], callback=lambda d: None)
        subscribes_before = rpc.methods().count("subscribe_whole_quote")
        session.replay_subscriptions()
        self.assertEqual(rpc.methods().count("subscribe_whole_quote"), subscribes_before + 2)

    def test_client_id_used_in_rpc(self):
        session, rpc, _channel = self._session()
        session.subscribe_whole_quote(["SH"], callback=lambda d: None)
        sub_params = [p for m, p in rpc.calls if m == "subscribe_whole_quote"][0]
        self.assertEqual(sub_params["client_id"], "client-test")

    def test_auto_replay_after_server_restart(self):
        """服务端重启后, 心跳线程应检测到 keepalive 失败并在服务端恢复后
        自动重放订阅(否则推送永久中断)。"""
        rpc = FakeRpcWithRestart(fail_start=1, fail_count=3)  # 第1-3次keepalive失败
        channel = FakePushChannelWithTopics()
        session = WholeQuoteClientSession(
            rpc_call=rpc, push_channel=channel, client_id="client-test",
            heartbeat_interval_seconds=0.05,
        )
        session.subscribe_whole_quote(["SH"], callback=lambda d: None)
        session.start()
        try:
            deadline = time.time() + 2.0
            # 等待: keepalive 失败触发重放, 重放后又有新的 keepalive
            while time.time() < deadline:
                if rpc.methods().count("subscribe_whole_quote") >= 2 and \
                   rpc.methods().count("quote_keepalive") >= 5:
                    break
                time.sleep(0.02)
            subs = rpc.methods().count("subscribe_whole_quote")
            kps = rpc.methods().count("quote_keepalive")
            print("auto-replay: subscribe=%d keepalive=%d" % (subs, kps))
            self.assertGreaterEqual(subs, 2, "服务端恢复后应重放订阅")
            self.assertGreaterEqual(kps, 5)
        finally:
            session.stop()

    def test_no_replay_when_server_healthy(self):
        """服务端健康时不应反复重放订阅(只保留初始 subscribe)。"""
        session, rpc, _channel = self._session(heartbeat_interval_seconds=0.05)
        session.subscribe_whole_quote(["SH"], callback=lambda d: None)
        session.start()
        try:
            time.sleep(0.4)
            self.assertEqual(rpc.methods().count("subscribe_whole_quote"), 1)
        finally:
            session.stop()

    def test_replay_when_push_silent_after_restart(self):
        """服务端重启后 keepalive 可能不失败(redis 队列兜住),但推送会静默。
        客户端应在推送静默超过阈值后自动重放订阅。"""
        rpc = FakeRpc()  # keepalive 从不失败
        channel = FakePushChannelWithTopics()
        session = WholeQuoteClientSession(
            rpc_call=rpc, push_channel=channel, client_id="client-test",
            heartbeat_interval_seconds=0.05,
            push_silence_replay_heartbeats=2,  # 2 个心跳周期无推送即重放
        )
        session.subscribe_whole_quote(["SH"], callback=lambda d: None)
        session.start()
        try:
            deadline = time.time() + 1.5
            while time.time() < deadline:
                if rpc.methods().count("subscribe_whole_quote") >= 2:
                    break
                time.sleep(0.02)
            self.assertGreaterEqual(
                rpc.methods().count("subscribe_whole_quote"), 2,
                "推送静默超阈值应自动重放订阅",
            )
        finally:
            session.stop()

    def test_subscriber_reused_when_topic_set_unchanged(self):
        """订阅/退订不应为同一 topic 集合反复重建订阅线程(线程泄漏)。"""
        rpc = FakeRpc()
        channel = FakePushChannelWithTopics()
        session = WholeQuoteClientSession(
            rpc_call=rpc, push_channel=channel, client_id="client-test",
            heartbeat_interval_seconds=0.05,
        )
        sub1 = session.subscribe_whole_quote(["SH"], callback=lambda d: None)
        sub2 = session.subscribe_whole_quote(["SH"], callback=lambda d: None)
        # 两次同 topic 订阅:共享订阅线程,只 start 一次
        self.assertEqual(len(channel.subscriptions), 1)
        # 退订一个 sub_id:topic 集合不变,不应重启线程
        session.unsubscribe_quote(sub1)
        self.assertEqual(len(channel.subscriptions), 1)
        self.assertFalse(channel.stopped)
        # 退订最后一个 sub_id:topic 集合变空,应停掉线程
        session.unsubscribe_quote(sub2)
        self.assertTrue(channel.stopped)
        self.assertEqual(len(channel.subscriptions), 1)

    def test_subscriber_restarts_when_topic_set_changes(self):
        """topic 集合变化时重建订阅线程,但旧线程先 stop。"""
        rpc = FakeRpc()
        channel = FakePushChannelWithTopics()
        session = WholeQuoteClientSession(
            rpc_call=rpc, push_channel=channel, client_id="client-test",
            heartbeat_interval_seconds=0.05,
        )
        session.subscribe_whole_quote(["SH"], callback=lambda d: None)
        session.subscribe_whole_quote(["SZ"], callback=lambda d: None)
        # topic 集合从 {SH} 变成 {SH,SZ} -> 重建(但旧 stop)
        self.assertEqual(len(channel.subscriptions), 2)
        self.assertTrue(channel.stopped)
        # 新线程订阅 {SH,SZ}
        self.assertEqual(channel.active_topics, frozenset(["SH", "SZ"]))


if __name__ == "__main__":
    unittest.main()


class SubIdsAreUniqueAcrossProcessesTest(unittest.TestCase):
    """Two client processes on one machine share the persisted client_id by
    default; the server keys subscriptions by (client_id, sub_id), so
    per-process counters 1, 2, 3 collided and one process's unsubscribe
    tore down the other's. The pid is folded into the id."""

    def _session(self):
        from bigqmt_signal_trader.whole_quote_session import WholeQuoteClientSession
        calls = []

        def rpc(method, params):
            calls.append((method, dict(params)))
            return {"combo_key": "SH", "topic": "SH", "push_endpoint": ""}

        class Channel(object):
            def start_subscriber(self, topics, on_msg):
                pass

            def stop(self):
                pass

        return WholeQuoteClientSession(rpc, Channel(), client_id="shared"), calls

    def test_the_id_carries_the_pid_and_stays_an_int(self):
        import os
        session, calls = self._session()
        first = session.subscribe_whole_quote(["SH"], callback=lambda d: None)
        second = session.subscribe_whole_quote(["SZ"], callback=lambda d: None)
        self.assertIsInstance(first, int)
        self.assertEqual(os.getpid(), first // session.SUB_ID_PID_STRIDE)
        self.assertEqual(first + 1, second)
        self.assertEqual(first, calls[0][1]["sub_id"])

    def test_unsubscribe_sends_the_same_id(self):
        session, calls = self._session()
        sub_id = session.subscribe_whole_quote(["SH"], callback=lambda d: None)
        session.unsubscribe_quote(sub_id)
        self.assertEqual(("unsubscribe_whole_quote", sub_id),
                         (calls[-1][0], calls[-1][1]["sub_id"]))
