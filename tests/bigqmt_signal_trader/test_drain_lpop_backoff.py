# coding: utf-8
"""An adjust-thread LPOP that times out must not be retried on the next tick.

With ``rpc_background_threads: False`` (the default since 0.3.51) the
strategy's adjust callback polls the Redis request list with one LPOP per
tick. When the Redis host is down -- not refusing, down -- that LPOP holds
the thread for the whole socket_connect_timeout (1.5s) and the next tick
does it again. Live, 2026-09-17 12:53 to 16:25 (192.168.8.13 unreachable):

    [bigqmt_rpc] ERROR drain timeout on LPOP queue=...; skip this tick
    [adjust_phase] drain 1501ms
    adjust cadence: ticks=7 avg=1.509s min=1.502s max=1.521s over 11s

for 1212 consecutive ten-second windows. The strategy's own tick_app ran
at 1.5s instead of 100ms for three and a half hours, over a Redis it was
not even using at the time.

After a timeout the drain now pauses -- 5s, doubling to a 30s cap while
the timeouts continue -- and the first LPOP that answers resets it.
"""

import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import bigqmt_signal_trader.transports.redis_transport as module  # noqa: E402
from bigqmt_signal_trader.transports.redis_transport import RedisTransport  # noqa: E402


class _RedisTimeoutError(Exception):
    """Looks like redis.exceptions.TimeoutError to _is_redis_timeout."""


_RedisTimeoutError.__module__ = "redis.exceptions"
_RedisTimeoutError.__name__ = "TimeoutError"


class _RedisConnectionError(Exception):
    """A refused connect: instant, not a stall -- must keep raising."""


_RedisConnectionError.__module__ = "redis.exceptions"
_RedisConnectionError.__name__ = "ConnectionError"


class _ListenRedis(object):
    def __init__(self):
        self.lpop_calls = 0
        self.outcomes = []  # per call: "timeout" | "refused" | None | payload

    def lpop(self, key):
        self.lpop_calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome == "timeout":
            raise _RedisTimeoutError("Timeout connecting to server")
        if outcome == "refused":
            raise _RedisConnectionError("Error 10061 connecting")
        return outcome


class _Clock(object):
    def __init__(self):
        self.now = 5000.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class DrainLpopBackoffTest(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self._time = module.time
        module.time = types.SimpleNamespace(
            monotonic=self.clock.monotonic, time=self._time.time,
            sleep=self._time.sleep, perf_counter=self._time.perf_counter)
        self.listen = _ListenRedis()
        self.transport = RedisTransport(self.listen, account_id="acct")
        self.delivered = []
        self.transport.on_raw_payload = lambda raw, source: self.delivered.append((raw, source))

    def tearDown(self):
        module.time = self._time

    def test_timeout_pauses_the_drain_for_five_seconds(self):
        self.listen.outcomes = ["timeout"]
        self.assertEqual(self.transport.drain_request_queue(max_items=20), 0)
        self.assertEqual(self.listen.lpop_calls, 1)
        # The next ticks do not touch Redis at all.
        for _ in range(10):
            self.clock.advance(0.1)
            self.assertEqual(self.transport.drain_request_queue(max_items=20), 0)
        self.assertEqual(self.listen.lpop_calls, 1)
        # 5s later the drain probes again.
        self.clock.advance(4.5)
        self.listen.outcomes = [b"payload-1", None]
        self.assertEqual(self.transport.drain_request_queue(max_items=20), 1)
        self.assertEqual(self.listen.lpop_calls, 3)
        self.assertEqual(self.delivered, [(b"payload-1", "queue-drain")])

    def test_consecutive_timeouts_double_up_to_the_cap(self):
        pauses = []
        for _ in range(6):
            self.listen.outcomes = ["timeout"]
            self.transport.drain_request_queue(max_items=20)
            pauses.append(self.transport._drain_backoff_seconds)
            self.clock.advance(self.transport._drain_backoff_seconds + 0.01)
        self.assertEqual(pauses, [5.0, 10.0, 20.0, 30.0, 30.0, 30.0])
        self.assertEqual(self.listen.lpop_calls, 6)

    def test_a_successful_lpop_resets_the_backoff(self):
        self.listen.outcomes = ["timeout"]
        self.transport.drain_request_queue(max_items=20)
        self.clock.advance(5.01)
        self.listen.outcomes = ["timeout"]
        self.transport.drain_request_queue(max_items=20)
        self.assertEqual(self.transport._drain_backoff_seconds, 10.0)
        self.clock.advance(10.01)
        self.listen.outcomes = [None]  # Redis is back, queue empty
        self.transport.drain_request_queue(max_items=20)
        self.assertEqual(self.transport._drain_timeouts, 0)
        self.assertEqual(self.transport._drain_backoff_seconds, 0.0)
        # A later timeout starts again from 5s, not from 20s.
        self.listen.outcomes = ["timeout"]
        self.transport.drain_request_queue(max_items=20)
        self.assertEqual(self.transport._drain_backoff_seconds, 5.0)

    def test_a_refused_connection_still_raises(self):
        # Refused is instant; it is not the stall this guards, and hiding
        # it would hide a misconfigured host. adjust's phase guard logs it.
        self.listen.outcomes = ["refused"]
        with self.assertRaises(_RedisConnectionError):
            self.transport.drain_request_queue(max_items=20)
        self.assertEqual(self.transport._drain_backoff_until, 0.0)
        self.listen.outcomes = [None]
        self.assertEqual(self.transport.drain_request_queue(max_items=20), 0)
        self.assertEqual(self.listen.lpop_calls, 2)

    def test_background_brpop_still_owns_the_queue(self):
        # #321 is untouched: a live BRPOP thread means no adjust LPOP, and
        # therefore no timeout bookkeeping either.
        class _Alive(object):
            def is_alive(self):
                return True

        self.transport._queue_thread = _Alive()
        self.listen.outcomes = ["timeout"]
        self.assertEqual(self.transport.drain_request_queue(max_items=20), 0)
        self.assertEqual(self.listen.lpop_calls, 0)


if __name__ == "__main__":
    unittest.main()
