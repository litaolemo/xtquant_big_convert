# coding: utf-8
"""Tests for the pluggable transport layer.

Covers:
* ``FakeTransport`` round-trip (validates the abstract contract).
* ZMQ transport round-trip on tcp loopback (the main low-latency path).
* MySQL transport round-trip against an in-memory sqlite3 DB (driver-agnostic).
* Factory dispatch: name → transport class, unknown-name rejection, shm raising.
"""

import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.transports import (  # noqa: E402
    RpcTransport,
    TransportError,
    TransportTimeout,
    build_transport,
)
from bigqmt_signal_trader.transports.base import RpcTransport as Base  # noqa: E402


class FakeTransport(RpcTransport):
    """In-process transport: a thread-safe queue pair. Validates the contract."""

    name = "fake"

    def __init__(self, account_id="", print_prefix="[fake]"):
        super(FakeTransport, self).__init__(account_id=account_id, print_prefix=print_prefix)
        self._req_q = []  # inbound requests waiting for the server
        self._resp_q = {}  # request_id -> response
        self._lock = threading.Lock()

    def send_request(self, request, timeout_seconds):
        rid = request["request_id"]
        # Hand the request to the server (call on_request), then collect reply.
        with self._lock:
            self._req_q.append(request)
        # Server processing: the registered callback handles it immediately.
        callback = self._on_request
        if callback is None:
            raise TransportError("no server registered")
        response = callback(request) or {}
        with self._lock:
            self._resp_q[rid] = response
        return response

    def send_response(self, request, response):
        rid = response.get("request_id") or request.get("request_id")
        with self._lock:
            self._resp_q[rid] = response

    def start_receiving(self, on_request, background_threads=True):
        super(FakeTransport, self).start_receiving(on_request)


def _build_request(method="ping", params=None, account_id="acct"):
    import uuid

    return {
        "schema_version": 1,
        "request_id": uuid.uuid4().hex,
        "account_id": account_id,
        "method": method,
        "params": params or {},
        "reply_channel": "",
        "reply_list": "",
        "reply_key": "",
        "ttl_seconds": 5,
    }


class FakeTransportTest(unittest.TestCase):
    def test_round_trip(self):
        server = FakeTransport(account_id="acct")
        server.start_receiving(lambda req: {"ok": True, "request_id": req["request_id"], "data": {"echo": req["params"]}})
        client = FakeTransport(account_id="acct")
        client._on_request = server._on_request  # share the handler
        req = _build_request(params={"x": 1})
        resp = client.send_request(req, 2.0)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["request_id"], req["request_id"])
        self.assertEqual(resp["data"], {"echo": {"x": 1}})

    def test_deliver_auto_sends_response(self):
        server = FakeTransport(account_id="acct")
        sent = []
        server.send_response = lambda req, resp: sent.append(resp)  # capture
        server.start_receiving(lambda req: {"ok": True, "request_id": req["request_id"]})
        server.deliver(_build_request())
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["ok"])

    def test_deliver_turns_handler_exception_into_error_envelope(self):
        server = FakeTransport(account_id="acct")
        sent = []
        server.send_response = lambda req, resp: sent.append(resp)
        def boom(req):
            raise ValueError("handler exploded")
        server.start_receiving(boom)
        server.deliver(_build_request())
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]["ok"])
        self.assertIn("handler exploded", sent[0]["error"])


class ZmqSendBackpressureTest(unittest.TestCase):
    """出站堆积自动清理：peer 水位满（Again）时让位进队列，不阻塞 router 线程。"""

    def test_muted_peer_falls_back_to_queue(self):
        import threading
        from bigqmt_signal_trader.transports.zmq_transport import ZmqTransport

        t = ZmqTransport(account_id="acct")
        calls = []

        class _Again(Exception):
            pass

        class FakeZmq:
            DONTWAIT = 1
            Again = _Again

        class FakeRouter:
            def send_multipart(self, frames, flags=0):
                calls.append(flags)
                raise _Again("peer muted")

        t._zmq = FakeZmq()
        t._router = FakeRouter()
        t._router_thread = threading.current_thread()  # 触发内联路径
        t._pending_identities["rid-1"] = b"peer-1"

        t.send_response({"request_id": "rid-1"},
                        {"request_id": "rid-1", "method": "ping", "ok": True, "data": {}})

        # 内联发送用 DONTWAIT 尝试过一次，失败后让位进队列（不阻塞不丢）
        self.assertEqual(calls, [1])
        self.assertEqual(t._response_queue.qsize(), 1)

    def test_queue_overflow_drops_oldest(self):
        import threading
        from bigqmt_signal_trader.transports.zmq_transport import ZmqTransport

        t = ZmqTransport(account_id="acct")
        t._max_queued_responses = 3

        class FakeZmq:
            DONTWAIT = 1

            class Again(Exception):
                pass

        t._zmq = FakeZmq()
        t._router = None
        for i in range(5):
            t._pending_identities["rid-%d" % i] = b"peer"
            t._queue_response(b"peer", b"payload-%d" % i, reason="test")

        # 5 条进、上限 3，丢 2 条最旧，剩 3 条
        self.assertEqual(t._response_queue.qsize(), 3)
        self.assertEqual(t._dropped_response_count, 2)


class FactoryTest(unittest.TestCase):
    def test_unknown_transport_rejected(self):
        with self.assertRaises(ValueError):
            build_transport("nonsense", {}, account_id="x")

    def test_shm_raises_on_use(self):
        shm = build_transport("shm", {}, account_id="x")
        with self.assertRaises(TransportError):
            shm.send_request(_build_request(), 1.0)

    def test_redis_factory_with_injected_clients(self):
        sentinel_listen = object()
        sentinel_resp = object()
        t = build_transport(
            "redis",
            {"redis_client": sentinel_listen, "response_redis_client": sentinel_resp},
            account_id="acct",
        )
        self.assertIs(t.listen_redis, sentinel_listen)
        self.assertIs(t.redis, sentinel_resp)
        self.assertEqual(t.account_id, "acct")


class ZmqTransportTest(unittest.TestCase):
    """Round-trip over tcp loopback. Skipped if pyzmq isn't installed."""

    def setUp(self):
        try:
            import zmq  # noqa: F401
        except ImportError:
            self.skipTest("pyzmq not installed")
        # Find a free port to avoid collisions between test runs.
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.address = "tcp://127.0.0.1:%d" % self.port

    def test_round_trip(self):
        from bigqmt_signal_trader.transports.zmq_transport import ZmqTransport

        server = ZmqTransport(
            bind_address=self.address, account_id="acct", recv_timeout_seconds=0.3
        )

        def on_req(req):
            return {
                "schema_version": 1,
                "request_id": req["request_id"],
                "account_id": "acct",
                "method": req["method"],
                "ok": True,
                "data": {"pong": True},
                "error": "",
                "handled_at": "now",
            }

        server.start_receiving(on_req, background_threads=True)
        try:
            time.sleep(0.4)
            client = ZmqTransport(connect_address=self.address, account_id="acct")
            time.sleep(0.3)
            req = _build_request()
            resp = client.send_request(req, timeout_seconds=3.0)
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["request_id"], req["request_id"])
            self.assertTrue(resp["data"]["pong"])
            client.stop()
        finally:
            server.stop()

    def test_main_thread_drain_round_trip(self):
        from bigqmt_signal_trader.transports.zmq_transport import ZmqTransport

        server = ZmqTransport(
            bind_address=self.address, account_id="acct", recv_timeout_seconds=0.01
        )
        server.start_receiving(
            lambda request: {
                "request_id": request["request_id"],
                "account_id": "acct",
                "method": request["method"],
                "ok": True,
                "data": {"main_thread": True},
                "error": "",
            },
            background_threads=False,
        )
        client = ZmqTransport(connect_address=self.address, account_id="acct")
        result = {}

        def call_client():
            result["response"] = client.send_request(_build_request(), timeout_seconds=2.0)

        try:
            thread = threading.Thread(target=call_client)
            thread.start()
            deadline = time.time() + 1.0
            while thread.is_alive() and time.time() < deadline:
                server.drain_request_queue(max_items=10)
                time.sleep(0.01)
            thread.join(1.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result["response"]["data"], {"main_thread": True})
        finally:
            client.stop()
            server.stop()

    def test_duplicate_server_address_fails_without_port_fallback(self):
        from bigqmt_signal_trader.transports.zmq_transport import ZmqTransport

        first = ZmqTransport(bind_address=self.address, account_id="acct")
        duplicate = ZmqTransport(
            bind_address=self.address, account_id="acct", port_scan_range=50
        )
        first.start_receiving(lambda _request: {}, background_threads=False)
        try:
            with self.assertRaisesRegex(TransportError, "ZMQ_BIND_CONFLICT"):
                duplicate.start_receiving(
                    lambda _request: {}, background_threads=False
                )
            self.assertIsNone(duplicate._actual_bind_address)
        finally:
            duplicate.stop()
            first.stop()

    def test_deferred_response_returns_through_router_thread(self):
        from bigqmt_signal_trader.transports.zmq_transport import ZmqTransport

        pending = []
        server = ZmqTransport(
            bind_address=self.address, account_id="acct", recv_timeout_seconds=0.05
        )
        server.start_receiving(lambda request: pending.append(request), background_threads=True)
        client = ZmqTransport(connect_address=self.address, account_id="acct")
        result = {}

        def call_client():
            result["response"] = client.send_request(_build_request(), timeout_seconds=2.0)

        try:
            thread = threading.Thread(target=call_client)
            thread.start()
            deadline = time.time() + 1.0
            while not pending and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(pending)
            request = pending[0]
            server.send_response(
                request,
                {
                    "request_id": request["request_id"],
                    "ok": True,
                    "data": {"deferred": True},
                    "error": "",
                },
            )
            thread.join(2.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result["response"]["data"], {"deferred": True})
        finally:
            client.stop()
            server.stop()

    def test_timeout_raises(self):
        from bigqmt_signal_trader.transports.zmq_transport import ZmqTransport

        # Client connects to a port with no server → times out.
        client = ZmqTransport(connect_address=self.address, account_id="acct")
        time.sleep(0.2)
        with self.assertRaises(TransportTimeout):
            client.send_request(_build_request(), timeout_seconds=0.5)
        client.stop()


class MysqlTransportTest(unittest.TestCase):
    """Round-trip over sqlite3 (the transport is driver-agnostic)."""

    def setUp(self):
        try:
            import sqlite3  # noqa: F401
        except ImportError:
            self.skipTest("sqlite3 not available")
        self.db_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "_test_rpc_%s.sqlite" % os.getpid()
        )
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.unlink(self.db_path)
            except Exception:
                pass

    def test_round_trip(self):
        from bigqmt_signal_trader.transports.mysql_transport import MysqlTransport

        cfg = {
            "driver": "sqlite3",
            "connect_kwargs": {"database": self.db_path, "check_same_thread": False},
            "account_id": "acct",
            "poll_interval_seconds": 0.01,
            # sqlite connections are thread-bound; keep them single-threaded in
            # the pool. Real MySQL doesn't need this.
            "pool_config": {"mincached": 1, "maxcached": 2, "maxshared": 0, "maxconnections": 2},
        }
        server = MysqlTransport.from_config(cfg, account_id="acct")
        server._ensure_schema()

        def on_req(req):
            return {
                "schema_version": 1,
                "request_id": req["request_id"],
                "account_id": "acct",
                "method": req["method"],
                "ok": True,
                "data": {"pong": True},
                "error": "",
                "handled_at": "now",
            }

        server.start_receiving(on_req, background_threads=True)
        try:
            client = MysqlTransport.from_config(cfg, account_id="acct")
            req = _build_request()
            resp = client.send_request(req, timeout_seconds=3.0)
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["request_id"], req["request_id"])
            self.assertTrue(resp["data"]["pong"])
            client.stop()
        finally:
            server.stop()


class RedisAdjustDrainTest(unittest.TestCase):
    def test_redis_adjust_lpop_skipped_when_brpop_thread_alive(self):
        """Background BRPOP owns the list; adjust LPOP must not steal it."""
        from bigqmt_signal_trader.transports.redis_transport import RedisTransport

        class _AliveThread(object):
            def is_alive(self):
                return True

        class _FailRedis(object):
            def lpop(self, key):
                raise AssertionError("adjust LPOP should not run while BRPOP owns %s" % key)

        transport = RedisTransport.__new__(RedisTransport)
        transport.print_prefix = "[test]"
        transport.account_id = "acct"
        transport.request_queue_template = "bigqmt:rpc:queue:{account_id}"
        transport.listen_redis = _FailRedis()
        transport.on_raw_payload = None
        transport._queue_thread = _AliveThread()
        self.assertEqual(0, transport.drain_request_queue(max_items=20))

    def test_redis_adjust_lpop_runs_when_brpop_thread_dead(self):
        """If the BRPOP thread is gone, LPOP remains the adjust fallback."""
        from bigqmt_signal_trader.transports.redis_transport import RedisTransport

        class _DeadThread(object):
            def is_alive(self):
                return False

        class _EmptyRedis(object):
            def lpop(self, key):
                return None

        transport = RedisTransport.__new__(RedisTransport)
        transport.print_prefix = "[test]"
        transport.account_id = "acct"
        transport.request_queue_template = "bigqmt:rpc:queue:{account_id}"
        transport.listen_redis = _EmptyRedis()
        transport.on_raw_payload = None
        transport._queue_thread = _DeadThread()
        self.assertEqual(0, transport.drain_request_queue(max_items=20))


if __name__ == "__main__":
    unittest.main()
