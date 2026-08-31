"""The first ContextInfo call after a restart is paid at startup, not by a caller.

get_financial_data was measured at 346 seconds on its first call after a
restart -- while QMT itself was healthy and the main strategy thread idle.
Every later call that day took under a second, including codes and tables
never asked for before, so it is a one-time cost rather than a per-code miss.

That block lands on the RPC listener thread, which serves one request at a
time, so it takes the whole bridge down with it.

Warming does not make the cost cheaper. It moves it to a known moment, onto a
thread nobody is waiting on, with a log line saying what is happening. And it
must NOT be on the main thread: _diag_startup runs there during init, and a
346-second call in init would freeze startup before the adjust timer is even
scheduled -- worse than the problem it set out to fix.
"""

import datetime
import io
import os
import re
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

STRATEGY = os.path.join(SRC, "bigqmt_signal_trader_strategy.py")


def _strategy_namespace():
    """Exec just the warmup helpers out of the strategy entry.

    The module as a whole expects QMT globals; the pieces under test are
    self-contained.
    """
    with io.open(STRATEGY, encoding="utf-8") as handle:
        source = handle.read()
    start = source.index("def _warm_financial_data(")
    end = source.index("def _pump_download_jobs(", start)
    namespace = {"time": time, "threading": threading, "datetime": datetime}
    exec(compile(source[start:end], STRATEGY, "exec"), namespace)
    return namespace


class FakeContext(object):
    """Mirrors what the live terminal does: an empty date range returns None."""

    def __init__(self, delay=0.0, blow_up=False, rows=159):
        self.calls = []
        self._delay = delay
        self._blow_up = blow_up
        self._rows = rows

    def get_financial_data(self, fields, codes, start_time, end_time, report_type):
        self.calls.append((fields, codes, start_time, end_time, report_type))
        if self._delay:
            time.sleep(self._delay)
        if self._blow_up:
            raise RuntimeError("QMT said no")
        if not start_time or not end_time:
            return None            # measured: empty range fetches nothing
        return list(range(self._rows))


class RealRangeTest(unittest.TestCase):
    """The first version passed "" for both dates and warmed nothing."""

    def test_the_probe_asks_for_a_real_date_range(self):
        namespace = _strategy_namespace()
        context = FakeContext()

        namespace["_warm_financial_data"](context)

        _fields, _codes, start, end, _report = context.calls[0]
        self.assertTrue(start, "start_time was empty -- fetches nothing")
        self.assertTrue(end, "end_time was empty -- fetches nothing")

    def test_the_range_is_eight_digit_dates(self):
        namespace = _strategy_namespace()
        context = FakeContext()

        namespace["_warm_financial_data"](context)

        _fields, _codes, start, end, _report = context.calls[0]
        self.assertRegex(start, r"^\d{8}$")
        self.assertRegex(end, r"^\d{8}$")

    def test_the_range_is_not_empty(self):
        namespace = _strategy_namespace()
        context = FakeContext()

        namespace["_warm_financial_data"](context)

        _fields, _codes, start, end, _report = context.calls[0]
        self.assertLess(start, end)

    def test_it_actually_brings_rows_back(self):
        namespace = _strategy_namespace()
        context = FakeContext()

        result = namespace["_warm_financial_data"](context)

        self.assertEqual(namespace["_warmup_row_count"](result), 159)


class EmptyResultIsReportedTest(unittest.TestCase):
    """A warmup that warms nothing must not report success."""

    def _run(self, context):
        namespace = _strategy_namespace()
        recorder = []
        import builtins
        saved = builtins.print
        try:
            builtins.print = lambda *a: recorder.append(" ".join(str(x) for x in a))
            namespace["_context_warmup_loop"](context)
        finally:
            builtins.print = saved
        return recorder

    def test_nothing_fetched_says_so(self):
        class Empty(FakeContext):
            def get_financial_data(self, *args):
                self.calls.append(args)
                return None

        lines = self._run(Empty())

        self.assertTrue([l for l in lines if "returned NOTHING" in l], lines)

    def test_a_real_fetch_reports_the_row_count(self):
        lines = self._run(FakeContext())

        warm = [l for l in lines if "warm in" in l or "warm after" in l]
        self.assertTrue(warm, lines)
        self.assertIn("159", warm[0])

    def test_row_count_survives_an_object_without_len(self):
        namespace = _strategy_namespace()

        self.assertEqual(namespace["_warmup_row_count"](object()), -1)
        self.assertEqual(namespace["_warmup_row_count"](None), 0)


class ProbeTest(unittest.TestCase):
    def test_it_warms_get_financial_data(self):
        namespace = _strategy_namespace()
        names = [name for name, _ in namespace["CONTEXT_WARMUP_PROBES"]]

        self.assertIn("get_financial_data", names)

    def test_the_probe_actually_calls_it(self):
        namespace = _strategy_namespace()
        context = FakeContext()

        namespace["_context_warmup_loop"](context)

        self.assertEqual(len(context.calls), 1)

    def test_a_probe_failure_does_not_stop_the_rest(self):
        namespace = _strategy_namespace()
        context = FakeContext(blow_up=True)

        namespace["_context_warmup_loop"](context)   # must not raise

        self.assertEqual(len(context.calls), 1)


class OffTheMainThreadTest(unittest.TestCase):
    """The point of the whole thing: init must return immediately."""

    def test_starting_it_does_not_block(self):
        namespace = _strategy_namespace()
        context = FakeContext(delay=1.5)

        started = time.time()
        namespace["_start_context_warmup"](context, {})
        elapsed = time.time() - started

        self.assertLess(elapsed, 0.5, "warmup blocked the caller for %.2fs" % elapsed)

    def test_it_runs_on_a_daemon_thread(self):
        """A strategy stop must not wait on it."""
        namespace = _strategy_namespace()
        before = set(t.name for t in threading.enumerate())

        namespace["_start_context_warmup"](FakeContext(delay=0.5), {})
        time.sleep(0.1)
        new = [t for t in threading.enumerate() if t.name not in before]

        warmers = [t for t in new if "warmup" in t.name]
        self.assertTrue(warmers, [t.name for t in new])
        self.assertTrue(warmers[0].daemon)

    def test_it_can_be_turned_off(self):
        namespace = _strategy_namespace()
        context = FakeContext()

        namespace["_start_context_warmup"](
            context, {"rpc": {"warm_context_data": False}})
        time.sleep(0.2)

        self.assertEqual(context.calls, [])

    def test_a_string_false_turns_it_off_too(self):
        """Config files hand these through as text often enough."""
        namespace = _strategy_namespace()
        context = FakeContext()

        namespace["_start_context_warmup"](
            context, {"rpc": {"warm_context_data": "false"}})
        time.sleep(0.2)

        self.assertEqual(context.calls, [])

    def test_it_is_on_by_default(self):
        namespace = _strategy_namespace()
        context = FakeContext()

        namespace["_start_context_warmup"](context, {})
        time.sleep(0.3)

        self.assertEqual(len(context.calls), 1)


class WiringTest(unittest.TestCase):
    def test_init_starts_it_after_the_diagnostics(self):
        with io.open(STRATEGY, encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("_start_context_warmup(ContextInfo, config)", source)
        self.assertLess(source.index("_diag_startup(ContextInfo, config)"),
                        source.index("_start_context_warmup(ContextInfo, config)"))

    def test_a_start_failure_cannot_kill_init(self):
        with io.open(STRATEGY, encoding="utf-8") as handle:
            source = handle.read()
        start = source.index("_start_context_warmup(ContextInfo, config)")

        self.assertIn("except Exception", source[start:start + 260])

    def test_the_diagnostics_still_do_not_call_it_on_the_main_thread(self):
        """_diag_startup runs in init; a 346s probe there would freeze
        startup before the adjust timer is scheduled."""
        with io.open(STRATEGY, encoding="utf-8") as handle:
            source = handle.read()
        start = source.index("def _diag_startup(")
        # The next top-level thing after it is the warmup block, not a def.
        end = min(source.index("\ndef ", start + 1),
                  source.index("\n# ContextInfo families", start + 1))

        self.assertNotIn("get_financial_data", source[start:end])


if __name__ == "__main__":
    unittest.main()
