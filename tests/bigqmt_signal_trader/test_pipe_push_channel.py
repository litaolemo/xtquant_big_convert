# coding: utf-8
"""#408: pipe deployments get real server->client event push.

Before this, a pipe bridge answered RPCs but pushed nothing: exec events
went to a NullQuotePushChannel and the client synthesized callbacks by
polling (#372). The dedicated events pipe carries one direction (server
writes, clients read), so order/trade/order_error/cancel_error and
whole-quote frames now ride it natively.

These tests run real named pipes on this Windows host -- no mock sockets:
the publisher accepts genuine CreateFile connections and the subscriber
loops genuine ReadFile calls, which is exactly the deployment topology.
"""
import json
import os
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.transports.pipe_push_channel import (  # noqa: E402
    PipeQuotePushChannel,
    pipe_events_path,
)


def _paths():
    return pipe_events_path("bigqmt_rpc_test_%d" % os.getpid(), "acct")


def _pair(path=None):
    path = path or _paths()
    server = PipeQuotePushChannel(bind_address=path)
    server.start_publisher()
    client = PipeQuotePushChannel(connect_address=path)
    return server, client


class PipePushChannelTest(unittest.TestCase):

    def test_roundtrip_and_topic_filter(self):
        server, client = _pair()
        received = []
        done = threading.Event()

        def on_msg(topic, data):
            received.append((topic, data))
            done.set()

        client.start_subscriber(["exec:order"], on_msg)
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and server.publisher_subscriber_count() < 1:
                time.sleep(0.05)
            server.publish("exec:order", {"order_sys_id": "S1"})
            server.publish("exec:trade", {"trade_id": "T1"})  # filtered out
            self.assertTrue(done.wait(2.0), "subscriber did not receive the exec:order push")
        finally:
            client.stop()
            server.stop()

        self.assertEqual(received, [("exec:order", {"order_sys_id": "S1"})])

    def test_fanout_to_two_subscribers(self):
        server, client_a = _pair()
        client_b = PipeQuotePushChannel(connect_address=client_a.connect_address)
        got_a, got_b = [], []
        done = threading.Event()
        done2 = threading.Event()
        client_a.start_subscriber(["q"], lambda t, d: (got_a.append(d), done.set()))
        client_b.start_subscriber(["q"], lambda t, d: (got_b.append(d), done2.set()))
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and server.publisher_subscriber_count() < 2:
                time.sleep(0.05)
            self.assertEqual(server.publisher_subscriber_count(), 2)
            server.publish("q", {"n": 1})
            self.assertTrue(done.wait(2.0) and done2.wait(2.0),
                            "both subscribers must receive the frame")
        finally:
            client_a.stop()
            client_b.stop()
            server.stop()
        self.assertEqual(got_a, [{"n": 1}])
        self.assertEqual(got_b, [{"n": 1}])

    def test_publish_with_no_subscribers_is_a_noop(self):
        path = _paths()
        server = PipeQuotePushChannel(bind_address=path)
        server.start_publisher()
        try:
            # No subscriber at all: pub/sub semantics -- dropped, no error.
            server.publish("exec:order", {"order_sys_id": "S1"})
        finally:
            server.stop()

    def test_subscriber_reconnects_after_server_restart(self):
        path = _paths()
        server = PipeQuotePushChannel(bind_address=path)
        server.start_publisher()
        client = PipeQuotePushChannel(connect_address=path)
        received = []
        client.start_subscriber(["q"], lambda t, d: received.append(d))
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and server.publisher_subscriber_count() < 1:
                time.sleep(0.05)
            server.publish("q", {"n": 1})
            server.stop()

            server2 = PipeQuotePushChannel(bind_address=path)
            server2.start_publisher()
            try:
                deadline = time.time() + 3.0
                while time.time() < deadline and server2.publisher_subscriber_count() < 1:
                    time.sleep(0.05)
                server2.publish("q", {"n": 2})
                deadline = time.time() + 2.0
                while time.time() < deadline and len(received) < 2:
                    time.sleep(0.05)
            finally:
                server2.stop()
        finally:
            client.stop()

        self.assertEqual(received, [{"n": 1}, {"n": 2}])

    def test_large_frame_survives_the_buffer(self):
        # A whole-quote snapshot frame is far larger than one ReadFile chunk:
        # the ERROR_MORE_DATA assembly must put it back together.
        server, client = _pair()
        received = []
        done = threading.Event()

        def on_msg(topic, data):
            received.append(data)
            done.set()

        client.start_subscriber(["big"], on_msg)
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and server.publisher_subscriber_count() < 1:
                time.sleep(0.05)
            big = {"codes": ["%06d.SH" % i for i in range(20000)],
                   "blob": "x" * (3 * 1024 * 1024)}
            server.publish("big", big)
            self.assertTrue(done.wait(5.0), "large frame did not arrive")
        finally:
            client.stop()
            server.stop()
        self.assertEqual(received[0]["blob"], "x" * (3 * 1024 * 1024))
        self.assertEqual(len(received[0]["codes"]), 20000)

    def test_stop_start_subscriber_lifecycle(self):
        # #403 lifecycle, pipe edition: stop + start with a changed topic set
        # leaves exactly one live subscriber and delivers to the new one.
        server, client = _pair()
        client.start_subscriber(["old"], lambda t, d: None)
        time.sleep(0.2)
        client.stop()
        received = []
        done = threading.Event()
        client.start_subscriber(["new"], lambda t, d: (received.append(d), done.set()))
        try:
            self.assertEqual(client.active_subscribers(), 1)
            # Wait for THIS subscriber to be connected: the publisher's count
            # includes not-yet-reaped stale handles from the old connection,
            # so it cannot say when the new one is on. The client side knows.
            deadline = time.time() + 3.0
            while time.time() < deadline and client._client_handle is None:
                time.sleep(0.05)
            time.sleep(0.2)  # let the accept loop register the handle
            server.publish("new", {"n": 1})
            self.assertTrue(done.wait(2.0), "the new subscriber must receive after re-start")
        finally:
            client.stop()
            server.stop()
        self.assertEqual(client.active_subscribers(), 0)
        self.assertEqual(received, [{"n": 1}])

    def test_pipe_events_path_derives_from_rpc_name(self):
        self.assertEqual(
            pipe_events_path("bigqmt_rpc", "888"),
            "\\\\.\\pipe\\bigqmt_rpc_events_888")
        self.assertEqual(
            pipe_events_path(None, ""),
            "\\\\.\\pipe\\bigqmt_rpc_events")


if __name__ == "__main__":
    unittest.main()


class PipePushWiringTest(unittest.TestCase):
    """Both ends of the wire must pick the pipe channel (#408)."""

    def test_server_build_uses_pipe_channel_with_derived_path(self):
        from bigqmt_signal_trader.quote_subscription_manager import (
            build_quote_subscription_service,
        )

        class _Ctx(object):
            def subscribe_whole_quote(self, codes, callback=None):
                return 0

        manager, channel = build_quote_subscription_service(
            _Ctx(), transport_name="pipe", account_id="888",
            pipe_name="bigqmt_rpc")
        try:
            self.assertIsInstance(channel, PipeQuotePushChannel)
            self.assertEqual(channel.bind_address,
                             "\\\\.\\pipe\\bigqmt_rpc_events_888")
        finally:
            channel.stop()

    def test_client_build_uses_pipe_channel_with_derived_path(self):
        from bigqmt_signal_trader.xtquant_compat import _build_quote_push_channel

        client = type("C", (), {
            "transport_name": "pipe",
            "account_id": "888",
            "pipe_config": {"pipe_name": "bigqmt_rpc"},
        })()
        channel = _build_quote_push_channel(client)
        self.assertIsInstance(channel, PipeQuotePushChannel)
        self.assertEqual(channel.connect_address,
                         "\\\\.\\pipe\\bigqmt_rpc_events_888")

    def test_event_loop_routes_pipe_to_push_channel(self):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader

        trader = BigQmtXtTrader.__new__(BigQmtXtTrader)
        trader.client = type("C", (), {"transport_name": "pipe"})()
        self.assertTrue(trader._exec_transport_uses_push_channel())

        trader.client = type("C", (), {"transport_name": "mysql"})()
        self.assertFalse(trader._exec_transport_uses_push_channel())

    def test_server_pipe_channel_serves_exec_events_end_to_end(self):
        """The exact #408 ask: a QMT-side exec event reaches the client."""
        from bigqmt_signal_trader.exec_events import (
            exec_topic, normalize_order_event,
        )

        server, client = _pair()
        received = []
        done = threading.Event()

        def on_msg(topic, data):
            received.append((topic, data))
            done.set()

        client.start_subscriber([exec_topic("order")], on_msg)
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and client._client_handle is None:
                time.sleep(0.05)
            time.sleep(0.2)

            class _Order(object):
                m_strAccountID = "acct"
                m_strInstrumentID = "600000"
                m_strExchangeID = "SH"
                m_strOrderSysID = "S1"
                m_strRemark = "probe"

            event = normalize_order_event(_Order(), "acct")
            # What the strategy's _publish_one does with a push-channel sink:
            server.publish(exec_topic("order"), event)
            self.assertTrue(done.wait(2.0), "exec event did not ride the pipe")
        finally:
            client.stop()
            server.stop()
        topic, data = received[0]
        self.assertEqual(topic, exec_topic("order"))
        self.assertEqual(data["order_sys_id"], "S1")
