"""A blocked handler has to be visible while it is blocked.

The slow-handler line the transport already printed is measured after
deliver() returns, so a handler that blocks says nothing at all until it
finishes. One did, for 346 seconds -- get_financial_data on the first call
after a restart -- and every request queued behind it timed out. The only clue
in the log arrived once it was already over.

From the client, a stalled bridge and a dead one are the same thing: a
timeout. This is what tells them apart, at the time it matters.

Handlers are served one at a time, so a single in-flight slot is enough.
"""

import os
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.transports.zmq_transport import ZmqTransport


class Recorder(object):
    def __init__(self):
        self.lines = []

    def __call__(self, text):
        self.lines.append(str(text))

    def matching(self, needle):
        return [line for line in self.lines if needle in line]


def _transport(**kwargs):
    settings = dict(account_id="acct", stall_warn_seconds=0.05,
                    stall_check_seconds=0.5)
    settings.update(kwargs)
    return ZmqTransport(**settings)


class InFlightTest(unittest.TestCase):
    """What the watchdog reads."""

    def test_it_is_empty_when_idle(self):
        self.assertIsNone(_transport()._in_flight)

    def test_a_handler_publishes_itself(self):
        transport = _transport()
        seen = {}

        def deliver(request):
            seen["slot"] = transport._in_flight

        transport.deliver = deliver
        transport._deliver_request({"method": "get_financial_data"})

        method, started, thread_name = seen["slot"]
        self.assertEqual(method, "get_financial_data")
        self.assertIsInstance(started, float)
        self.assertTrue(thread_name)

    def test_the_slot_is_cleared_afterwards(self):
        transport = _transport()
        transport.deliver = lambda request: None

        transport._deliver_request({"method": "ping"})

        self.assertIsNone(transport._in_flight)

    def test_it_is_cleared_even_when_the_handler_raises(self):
        """A handler that blows up must not leave the bridge looking stuck."""
        transport = _transport()

        def deliver(request):
            raise RuntimeError("boom")

        transport.deliver = deliver
        transport._deliver_request({"method": "ping"})

        self.assertIsNone(transport._in_flight)


class WatchdogTest(unittest.TestCase):
    """What it prints, and when."""

    def _run_watchdog(self, transport, seconds=1.2):
        transport._running = True
        thread = threading.Thread(target=transport._stall_watchdog_loop, daemon=True)
        thread.start()
        time.sleep(seconds)
        transport._running = False
        thread.join(2.0)

    def test_it_says_nothing_while_idle(self):
        transport = _transport()
        recorder = Recorder()
        transport.print_prefix = "[t]"

        import builtins
        saved = builtins.print
        try:
            builtins.print = recorder
            self._run_watchdog(transport, seconds=0.8)
        finally:
            builtins.print = saved

        self.assertEqual(recorder.matching("STILL RUNNING"), [])

    def test_it_reports_a_handler_that_is_still_running(self):
        transport = _transport()
        transport._in_flight = ("get_financial_data", time.perf_counter() - 300.0,
                                "bigqmt-zmq-rpc")
        recorder = Recorder()

        import builtins
        saved = builtins.print
        try:
            builtins.print = recorder
            self._run_watchdog(transport, seconds=0.8)
        finally:
            builtins.print = saved

        reports = recorder.matching("STILL RUNNING")
        self.assertTrue(reports, recorder.lines)
        self.assertIn("get_financial_data", reports[0])

    def test_the_report_says_blocked_not_dead(self):
        """The distinction the client cannot make on its own."""
        transport = _transport()
        transport._in_flight = ("get_financial_data", time.perf_counter() - 300.0,
                                "bigqmt-zmq-rpc")
        recorder = Recorder()

        import builtins
        saved = builtins.print
        try:
            builtins.print = recorder
            self._run_watchdog(transport, seconds=0.8)
        finally:
            builtins.print = saved

        self.assertIn("blocked, not dead", recorder.matching("STILL RUNNING")[0])

    def test_a_quick_handler_is_not_reported(self):
        transport = _transport(stall_warn_seconds=30.0)
        transport._in_flight = ("ping", time.perf_counter() - 0.01, "t")
        recorder = Recorder()

        import builtins
        saved = builtins.print
        try:
            builtins.print = recorder
            self._run_watchdog(transport, seconds=0.8)
        finally:
            builtins.print = saved

        self.assertEqual(recorder.matching("STILL RUNNING"), [])


class ConfigTest(unittest.TestCase):
    def test_the_default_fires_before_a_client_gives_up(self):
        """A client's default timeout is 30s; the log has to name the culprit
        before the caller has already concluded the bridge is dead."""
        from bigqmt_signal_trader.xtquant_compat import DEFAULT_RPC_TIMEOUT_SECONDS

        self.assertLess(_transport(stall_warn_seconds=20.0).stall_warn_seconds,
                        DEFAULT_RPC_TIMEOUT_SECONDS)

    def test_it_clears_the_slowest_healthy_call(self):
        """A whole-market snapshot measures 7.7s and is not a stall."""
        self.assertGreater(ZmqTransport(account_id="a").stall_warn_seconds, 7.7)

    def test_zero_disables_it(self):
        self.assertEqual(_transport(stall_warn_seconds=0).stall_warn_seconds, 0.0)

    def test_the_check_interval_has_a_floor(self):
        """A tight loop would cost more than the problem it reports."""
        self.assertGreaterEqual(_transport(stall_check_seconds=0.0).stall_check_seconds,
                                0.5)


if __name__ == "__main__":
    unittest.main()
