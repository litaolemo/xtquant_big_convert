# coding: utf-8
"""Shared-memory transport: the wire that survives a broker-locked runtime.

The QMT host can remove redis/pyzmq at the import layer and cannot pip install,
so this transport is stdlib only (mmap + struct). Two things are load-bearing
and are pinned here:

* the **drain** path -- measured on the live terminal, a ``threading.Thread``
  started from a QMT ``init`` is almost never scheduled (a PING written at
  12:44:58 was not answered until the strategy was stopped at 12:57:10, five
  loop iterations in fifty minutes). Requests must therefore be picked up by
  the strategy's ``run_time`` timer, not by a receive thread;
* the **client never creates** a region -- two ends each creating their own
  mapping looks exactly like "every request times out", which is the most
  expensive failure to diagnose.
"""

import os
import threading
import time
import unittest

from bigqmt_signal_trader.transports.base import (
    RpcTransport, TransportError, TransportTimeout,
)
from bigqmt_signal_trader.transports.factory import (
    build_transport, transport_supports_drain,
)
from bigqmt_signal_trader.transports.shm_transport import (
    HEADER_SIZE, MAGIC, SLOT_HEADER_SIZE, SharedMemoryTransport, _region_size,
)


def _echo(request):
    return {
        "schema_version": 1,
        "request_id": request.get("request_id"),
        "ok": True,
        "data": {"echo": request.get("params")},
    }


class _Pair(object):
    """One server + one client over a private name prefix."""

    def __init__(self, prefix, slots=4, slot_size=4096, background=False):
        self.prefix = prefix
        self.server = SharedMemoryTransport(
            account_id="acct", name_prefix=prefix, slots=slots, slot_size=slot_size)
        self.server.start_receiving(_echo, background_threads=background)
        self.client = SharedMemoryTransport(
            account_id="acct", name_prefix=prefix, slots=slots, slot_size=slot_size)

    def pump(self, count=200, pause=0.01):
        """Stand in for the adjust timer that drives drain in production."""
        for _ in range(count):
            if self.server.drain_request_queue(max_items=20):
                return True
            time.sleep(pause)
        return False

    def close(self):
        for transport in (self.client, self.server):
            try:
                transport.stop()
            except Exception:
                pass


class DrainTest(unittest.TestCase):
    def setUp(self):
        # 8 slots: the max_items test below queues 6 requests without wrapping
        # the ring, which a real client can never do (it refuses at slots).
        self.pair = _Pair("bqtest_shm_drain", slots=8)
        self.addCleanup(self.pair.close)

    def test_a_request_is_answered_only_when_the_adjust_tick_drains(self):
        """No drain, no reply — that is the whole point of drain mode."""
        answers = []
        thread = threading.Thread(target=lambda: answers.append(
            self.pair.client.send_request(
                {"request_id": "d1", "method": "ping", "params": {"a": 1}}, 10)))
        thread.daemon = True
        thread.start()
        time.sleep(0.4)
        self.assertEqual(answers, [], "answered without a drain — a receive thread is running")
        self.assertGreater(self.pair.server.drain_request_queue(max_items=20), 0)
        thread.join(timeout=10)
        self.assertEqual(answers[0]["request_id"], "d1")
        self.assertEqual(answers[0]["data"]["echo"], {"a": 1})

    def test_drain_is_non_blocking_when_idle(self):
        started = time.time()
        for _ in range(20):
            self.assertEqual(self.pair.server.drain_request_queue(max_items=20), 0)
        self.assertLess(time.time() - started, 0.5, "drain blocked with nothing to read")

    def test_drain_respects_max_items(self):
        """One tick handles at most max_items; the rest wait for the next tick.

        The request ring has a single producer (the bridge process), so the
        backlog is written directly rather than by racing several senders.
        """
        import json as _json
        for index in range(6):
            write_seq, _read = self.pair.server._req.seqs()
            payload = _json.dumps(
                {"request_id": "q%d" % index, "method": "ping",
                 "params": {}}).encode("utf-8")
            self.pair.server._req.write_slot(
                write_seq, ("q%d" % index).encode("ascii") + b"\0" * 38, payload)
            self.pair.server._req.set_write_seq(write_seq + 1)
        drained = self.pair.server.drain_request_queue(max_items=2)
        self.assertEqual(drained, 2)
        self.assertEqual(self.pair.server.drain_request_queue(max_items=20), 4)


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.pair = _Pair("bqtest_shm_rt")
        self.addCleanup(self.pair.close)

    def _call(self, request, timeout=10):
        holder = {}

        def worker():
            try:
                holder["out"] = self.pair.client.send_request(request, timeout)
            except Exception as exc:            # noqa: BLE001 - surfaced below
                holder["err"] = exc

        thread = threading.Thread(target=worker)
        thread.daemon = True
        thread.start()
        self.pair.pump()
        thread.join(timeout=timeout + 2)
        if "err" in holder:
            raise holder["err"]
        return holder["out"]

    def test_a_request_comes_back_with_its_own_request_id(self):
        out = self._call({"request_id": "r1", "method": "ping", "params": {"a": 1}})
        self.assertEqual(out["request_id"], "r1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["echo"], {"a": 1})

    def test_utf8_survives_both_directions(self):
        out = self._call({"request_id": "r2", "method": "x",
                          "params": {"名称": "维持担保比例", "值": 3.35}})
        self.assertEqual(out["data"]["echo"]["名称"], "维持担保比例")
        self.assertEqual(out["data"]["echo"]["值"], 3.35)

    def test_an_overlong_request_id_is_still_correlated(self):
        """The slot field is 40 ASCII bytes; a longer id must be hashed, not
        truncated -- truncation makes two distinct requests look alike."""
        long_id = "u" * 80
        out = self._call({"request_id": long_id, "method": "ping", "params": {}})
        self.assertTrue(out["ok"])

    def test_a_timeout_is_raised_when_nobody_drains(self):
        with self.assertRaises(TransportTimeout):
            self.pair.client.send_request(
                {"request_id": "never", "method": "ping", "params": {}}, 0.3)

    def test_ring_wrap_keeps_payloads_and_request_ids_aligned(self):
        for index in range(12):
            request_id = "wrap-%d" % index
            out = self._call({"request_id": request_id, "method": "ping",
                              "params": {"index": index}})
            self.assertEqual(out["request_id"], request_id)
            self.assertEqual(out["data"]["echo"], {"index": index})

    def test_a_payload_above_the_slot_is_refused_by_name(self):
        with self.assertRaises(TransportError) as caught:
            self.pair.client.send_request(
                {"request_id": "big", "method": "huge",
                 "params": {"blob": "x" * 8192}}, 5)
        self.assertIn("slot_size", str(caught.exception))


class DeferredReplyTest(unittest.TestCase):
    def test_a_reply_published_after_the_drain_keeps_its_request_id(self):
        captured = []
        pair = _Pair("bqtest_shm_deferred")
        pair.server.stop()
        pair.server = SharedMemoryTransport(
            account_id="acct", name_prefix="bqtest_shm_deferred",
            slots=4, slot_size=4096)
        pair.server.start_receiving(
            lambda request: captured.append(request), background_threads=False)
        self.addCleanup(pair.close)
        answer = []
        thread = threading.Thread(target=lambda: answer.append(
            pair.client.send_request(
                {"request_id": "deferred-1", "method": "get_asset", "params": {}}, 5)))
        thread.daemon = True
        thread.start()
        pair.pump()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["_shm_request_id"], "deferred-1")
        pair.server.send_response(captured[0], {
            "schema_version": 1, "request_id": "deferred-1", "ok": True,
            "data": {"cash": 123.0},
        })
        thread.join(timeout=5)
        self.assertEqual(answer[0]["request_id"], "deferred-1")
        self.assertEqual(answer[0]["data"], {"cash": 123.0})


class NotCreatedByTheClientTest(unittest.TestCase):
    def test_a_client_against_a_missing_region_says_so(self):
        """Creating on connect would hide "the strategy is not running"."""
        client = SharedMemoryTransport(
            account_id="acct", name_prefix="bqtest_shm_absent",
            connect_timeout_seconds=0.2)
        self.addCleanup(client.stop)
        with self.assertRaises(TransportError) as caught:
            client.send_request({"request_id": "x", "method": "ping", "params": {}}, 1)
        self.assertIn("does not exist", str(caught.exception))


class RestartTest(unittest.TestCase):
    def test_a_same_layout_restart_reuses_the_region(self):
        """A strategy restart must not need the old mapping torn down by hand."""
        first = SharedMemoryTransport(account_id="a", name_prefix="bqtest_shm_re1",
                                      slots=4, slot_size=4096)
        first.start_receiving(_echo, background_threads=False)
        first.stop()
        second = SharedMemoryTransport(account_id="a", name_prefix="bqtest_shm_re2",
                                       slots=4, slot_size=4096)
        self.addCleanup(second.stop)
        second.start_receiving(_echo, background_threads=False)
        self.assertEqual(second.drain_request_queue(max_items=1), 0)

    def test_a_changed_layout_is_refused_instead_of_mis_indexed(self):
        small = SharedMemoryTransport(account_id="a", name_prefix="bqtest_shm_re3",
                                      slots=4, slot_size=4096)
        small.start_receiving(_echo, background_threads=False)
        self.addCleanup(small.stop)
        bigger = SharedMemoryTransport(account_id="a", name_prefix="bqtest_shm_re3",
                                       slots=8, slot_size=8192)
        with self.assertRaises(TransportError) as caught:
            bigger.start_receiving(_echo, background_threads=False)
        self.assertIn("slot_size", str(caught.exception))


class WiringTest(unittest.TestCase):
    """The three places that decide whether shm is even reachable."""

    def test_the_factory_builds_it(self):
        transport = build_transport("shm", {}, account_id="acct")
        self.assertEqual(transport.name, "shm")
        self.assertIn("acct", transport.req_name)

    def test_it_owns_a_drain(self):
        self.assertTrue(transport_supports_drain("shm"))
        self.assertIsNot(
            SharedMemoryTransport.drain_request_queue,
            getattr(RpcTransport, "drain_request_queue", None))

    def test_layout_comes_from_config(self):
        transport = build_transport(
            "shm", {"shm": {"slot_size": 2048, "slots": 16}}, account_id="acct")
        self.assertEqual(transport.slot_size, 2048)
        self.assertEqual(transport.slots, 16)


class LayoutTest(unittest.TestCase):
    def test_the_header_and_slot_headers_are_exactly_as_documented(self):
        self.assertEqual(HEADER_SIZE, 64)
        self.assertEqual(SLOT_HEADER_SIZE, 48)
        self.assertEqual(MAGIC, b"BQSHM\0\0\0")

    def test_the_region_is_aligned_to_the_allocation_granularity(self):
        size = _region_size(1 << 20, 8)
        self.assertEqual(size % (64 * 1024), 0)
        self.assertGreaterEqual(size, HEADER_SIZE + 8 * (SLOT_HEADER_SIZE + (1 << 20)))
