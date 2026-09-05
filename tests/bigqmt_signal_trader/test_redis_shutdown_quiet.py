# coding: utf-8
"""#189: a normal redis shutdown must not print a failure traceback.

stop() closes the pubsub while the listener thread is parked in
get_message(timeout=1.0), so the socket dies under it. On Windows that surfaces
as OSError WinError 10038 wrapped in redis.exceptions.ConnectionError, and the
loop's bare `except Exception` printed the whole traceback -- three per restart
on the live terminal.

Nothing breaks, but the noise costs something real: a normal restart looks
exactly like a failure, so a reader learns to skip the traceback -- including
the one that means redis is actually down. That is the CLAUDE.md rule about a
failed operation looking exactly like one that never ran, pointed the other way.

The loop must stay loud while it is supposed to be running.
"""
import os
import sys
import threading
import unittest

try:
    from io import StringIO
except ImportError:                       # pragma: no cover - py2 safety
    from StringIO import StringIO


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.transports.redis_transport import RedisTransport


class DyingPubSub(object):
    """get_message raises the way a closed socket does."""

    def __init__(self, on_call):
        self.on_call = on_call

    def subscribe(self, channel):
        pass

    def get_message(self, timeout=None):
        self.on_call()
        raise IOError("Error while reading from socket: (10038, ...)")

    def close(self):
        pass


class DyingRedis(object):
    def __init__(self, on_call):
        self.on_call = on_call

    def pubsub(self, ignore_subscribe_messages=False):
        return DyingPubSub(self.on_call)

    def brpop(self, key, timeout=None):
        self.on_call()
        raise IOError("Error while reading from socket: (10038, ...)")


def _run(loop_name, running_when_it_raises):
    """Run one iteration of a listener loop and capture what it printed."""
    calls = []

    def on_call():
        calls.append(1)
        if not running_when_it_raises:
            transport._running = False    # stop() got here first
        elif len(calls) >= 2:
            # Let the first raise happen while running (it must be reported),
            # then end the loop so the test does not sit through its retries.
            transport._running = False

    transport = RedisTransport(DyingRedis(on_call), account_id="acct")
    transport._running = True
    captured = StringIO()
    saved, sys.stdout = sys.stdout, captured
    try:
        thread = threading.Thread(target=getattr(transport, loop_name))
        thread.start()
        thread.join(8.0)
        alive = thread.is_alive()
        transport._running = False
        thread.join(3.0)
    finally:
        sys.stdout = saved
    return captured.getvalue(), alive, len(calls)


class ShutdownIsQuietTest(unittest.TestCase):
    def test_pubsub_loop_prints_nothing_when_stopping(self):
        out, alive, _calls = _run("_listen_loop", running_when_it_raises=False)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("listener failed", out)
        self.assertFalse(alive, "the loop should have exited, not retried")

    def test_queue_loop_prints_nothing_when_stopping(self):
        out, alive, _calls = _run("_queue_loop", running_when_it_raises=False)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("queue listener failed", out)
        self.assertFalse(alive)


class RealFailuresStayLoudTest(unittest.TestCase):
    """The negative control -- silencing shutdown must not silence outages."""

    def test_pubsub_loop_still_reports_a_failure_while_running(self):
        out, _alive, calls = _run("_listen_loop", running_when_it_raises=True)
        self.assertIn("listener failed", out)
        self.assertIn("Traceback", out)
        self.assertGreaterEqual(calls, 1)

    def test_queue_loop_still_reports_a_failure_while_running(self):
        out, _alive, calls = _run("_queue_loop", running_when_it_raises=True)
        self.assertIn("queue listener failed", out)
        self.assertIn("Traceback", out)
        self.assertGreaterEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
