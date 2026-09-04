# coding: utf-8
"""#186: the zmq client serialized every request on one DEALER socket.

send_request held _client_lock for the whole send/poll/recv cycle, because a
zmq socket is not thread-safe and there was exactly one. So concurrent callers
took turns. Measured on the live terminal with 4 threads:

    zmq   ping 2.4 -> 2.7 requests/sec      (drain mode: 10.2 -> 10.2)
    redis ping 20.7 -> 143.3 requests/sec   (no such lock; a pool per thread)

Threads bought nothing on zmq and 7x on redis. The fix is zmq's own answer --
one socket per thread, each with its own IDENTITY so the ROUTER routes replies
back to the right one -- rather than a lock around a shared one.
"""
import json
import os
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.transports import zmq_transport as zt
from bigqmt_signal_trader.redis_rpc import (
    decode_rpc_request_payload, encode_rpc_request_payload)


class FakeSocket(object):
    """Answers whatever request_id it was sent, after ``delay`` seconds."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.identity = None
        self.closed = False
        self.linger_on_close = None
        self._pending = None

    def setsockopt(self, option, value):
        if option == "IDENTITY":
            self.identity = value

    def connect(self, address):
        self.address = address

    def send(self, payload):
        # Same wire encoding the transport uses -- b64s-prefixed, not raw JSON.
        text = decode_rpc_request_payload(payload.decode("utf-8"))
        self._pending = json.loads(text)["request_id"]

    def recv_multipart(self):
        reply = encode_rpc_request_payload({"request_id": self._pending, "ok": True})
        return [reply.encode("utf-8")]

    def close(self, linger=0):
        self.closed = True
        self.linger_on_close = linger


class FakePoller(object):
    def __init__(self):
        self._socks = []

    def register(self, sock, flags):
        self._socks.append(sock)

    def poll(self, timeout=None):
        sock = self._socks[0]
        time.sleep(sock.delay)          # the wait a real round trip costs
        return [(sock, 1)]


class FakeZmq(object):
    DEALER = "DEALER"
    IDENTITY = "IDENTITY"
    LINGER = "LINGER"
    POLLIN = 1
    Poller = FakePoller


class FakeContext(object):
    def __init__(self, delay=0.0):
        self.delay = delay
        self.sockets = []

    def socket(self, kind):
        sock = FakeSocket(self.delay)
        self.sockets.append(sock)
        return sock


def _client(delay=0.0):
    transport = zt.ZmqTransport(account_id="8886800503",
                                connect_address="tcp://127.0.0.1:15999")
    ctx = FakeContext(delay)
    transport._ensure_zmq = lambda: (FakeZmq, ctx)
    return transport, ctx


class EachThreadGetsItsOwnSocketTest(unittest.TestCase):
    def test_two_threads_do_not_share_a_dealer(self):
        transport, ctx = _client()
        seen = {}

        def call(name):
            transport.send_request({"method": "ping"}, 5.0)
            seen[name] = transport._ensure_dealer()

        threads = [threading.Thread(target=call, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(ctx.sockets), 2)
        self.assertIsNot(seen[0], seen[1])
        self.assertNotEqual(seen[0].identity, seen[1].identity,
                            "same IDENTITY would make ROUTER misroute replies")

    def test_one_thread_reuses_its_socket(self):
        transport, ctx = _client()
        transport.send_request({"method": "ping"}, 5.0)
        transport.send_request({"method": "ping"}, 5.0)
        self.assertEqual(len(ctx.sockets), 1)


class ConcurrentCallsOverlapTest(unittest.TestCase):
    """The actual regression: two calls must not take twice one call's time."""

    def test_four_threads_overlap_instead_of_queueing(self):
        delay = 0.3
        transport, _ctx = _client(delay)
        errors = []

        def call():
            try:
                transport.send_request({"method": "ping"}, 5.0)
            except Exception as exc:            # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(4)]
        started = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.time() - started

        self.assertEqual(errors, [])
        # Serialized would be 4 * 0.3 = 1.2s; overlapped is ~0.3s.
        self.assertLess(elapsed, delay * 2,
                        "4 concurrent calls took %.2fs for a %.2fs round trip "
                        "-- they are still serialized" % (elapsed, delay))


class SocketLifecycleTest(unittest.TestCase):
    def test_stop_closes_every_thread_s_socket(self):
        transport, ctx = _client()
        done = threading.Event()

        def call():
            transport.send_request({"method": "ping"}, 5.0)
            done.set()

        worker = threading.Thread(target=call)
        worker.start()
        worker.join()
        transport.send_request({"method": "ping"}, 5.0)   # this thread's own

        self.assertTrue(done.is_set())
        self.assertEqual(len(ctx.sockets), 2)
        transport.stop()
        self.assertTrue(all(s.closed for s in ctx.sockets),
                        [s.closed for s in ctx.sockets])

    def test_a_dead_thread_s_socket_is_reaped_not_leaked(self):
        """One socket per thread must not mean one leak per thread.

        A caller that spawns a thread per request would otherwise hold a TCP
        connection to the ROUTER for every thread it ever created.
        """
        transport, ctx = _client()
        for _ in range(5):
            worker = threading.Thread(
                target=lambda: transport.send_request({"method": "ping"}, 5.0))
            worker.start()
            worker.join()

        live = [sock for sock in ctx.sockets if not sock.closed]
        self.assertLessEqual(len(live), 1,
                             "%d sockets still open after 5 threads exited"
                             % len(live))


if __name__ == "__main__":
    unittest.main()
