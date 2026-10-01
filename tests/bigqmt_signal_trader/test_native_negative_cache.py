# coding: utf-8
"""A dead native xtdata SDK must not be re-dialed on every call (#160).

@heimo88's zmq slow-handler log: ``get_trading_dates 21644ms`` on the first
call after a strategy start. Measured on the live Guojin bridge: even warm,
EVERY get_trading_dates call cost ~2.1s because the handler re-tried the
native SDK, which blocks dialing its (nonexistent) quote service before
raising, then falls back to ContextInfo anyway.

In Big QMT the SDK never gains a quote service mid-process, so a native
failure is now remembered per function for NATIVE_FAILURE_CACHE_SECONDS;
calls in the window go straight to ContextInfo, and a success clears the
mark.
"""

import os
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider


class _FakeNativeModule(object):
    """Stands in for the xtdata SDK: records calls, raises or answers."""

    def __init__(self, fail=True):
        self.fail = fail
        self.calls = []

    def get_trading_dates(self, *args):
        self.calls.append(args)
        if self.fail:
            raise RuntimeError("无法连接行情服务")
        return ["20260901", "20260902"]


class _FakeContext(object):
    def get_trading_dates(self, *args):
        return ["20260901", "20260902", "20260903"]


class NativeNegativeCacheTest(unittest.TestCase):
    def _provider(self, native):
        return BigQmtMarketDataProvider(_FakeContext(), native_xtdata=native)

    def test_failed_native_is_not_retried_within_the_window(self):
        native = _FakeNativeModule(fail=True)
        provider = self._provider(native)

        first = provider.get_trading_dates("SH", "", "", 5)
        second = provider.get_trading_dates("SH", "", "", 5)

        self.assertEqual(first, ["20260901", "20260902", "20260903"])
        self.assertEqual(second, ["20260901", "20260902", "20260903"])
        self.assertEqual(len(native.calls), 1,
                         "the dead SDK must be dialed once, not per call")

    def test_cache_is_per_function(self):
        native = _FakeNativeModule(fail=True)
        native.get_holidays = lambda: ["20261001"]
        provider = self._provider(native)

        provider.get_trading_dates("SH", "", "", 5)   # fails -> marked
        holidays = provider.get_holidays()            # different fn -> tried

        self.assertEqual(holidays, ["20261001"])
        self.assertEqual(len(native.calls), 1)

    def test_success_clears_the_mark(self):
        native = _FakeNativeModule(fail=True)
        provider = self._provider(native)

        provider.get_trading_dates("SH", "", "", 5)   # fails -> marked
        native.fail = False
        # Pretend the window expired so the native path is re-probed.
        provider._native_dead_marks_dict["get_trading_dates"] -= (
            provider.NATIVE_FAILURE_CACHE_SECONDS + 1)

        result = provider.get_trading_dates("SH", "", "", 5)

        self.assertEqual(result, ["20260901", "20260902"])
        self.assertNotIn("get_trading_dates", provider._native_dead_marks_dict)

    def test_window_expiry_reprobes(self):
        native = _FakeNativeModule(fail=True)
        provider = self._provider(native)

        provider.get_trading_dates("SH", "", "", 5)
        provider._native_dead_marks_dict["get_trading_dates"] -= (
            provider.NATIVE_FAILURE_CACHE_SECONDS + 1)
        provider.get_trading_dates("SH", "", "", 5)

        self.assertEqual(len(native.calls), 2, "an expired mark must re-probe")

    def test_no_sdk_goes_straight_to_context_without_marking(self):
        provider = BigQmtMarketDataProvider(_FakeContext(), native_xtdata=None)
        provider._native_xtdata = None
        # _native() would try a real import; force "no SDK" by stubbing it.
        provider._native = lambda: None

        result = provider.get_trading_dates("SH", "", "", 5)

        self.assertEqual(result, ["20260901", "20260902", "20260903"])
        self.assertEqual(provider._native_dead_marks(), {})


if __name__ == "__main__":
    unittest.main()


class BothPathsErrorChainingTest(unittest.TestCase):
    """When both paths fail, the SDK's own reason must not be buried (#277)."""

    def test_both_failures_are_chained(self):
        native = _FakeNativeModule(fail=True)
        native.get_trading_dates = lambda *a: (_ for _ in ()).throw(
            ConnectionError("无法连接行情服务"))
        provider = BigQmtMarketDataProvider(
            type("C", (), {"get_trading_dates": lambda self, *a: (_ for _ in ()).throw(
                NotImplementedError("ContextInfo.download_financial_data is not available"))})(),
            native_xtdata=native)

        with self.assertRaises(RuntimeError) as cm:
            provider._native_or_context(
                "get_trading_dates",
                provider._call_context)

        message = str(cm.exception)
        self.assertIn("无法连接行情服务", message)
        self.assertIn("ContextInfo", message)

    def test_native_success_skips_the_chain(self):
        native = _FakeNativeModule(fail=False)
        provider = BigQmtMarketDataProvider(
            type("C", (), {"get_trading_dates": lambda self, *a: ["ctx"]})(),
            native_xtdata=native)
        out = provider._native_or_context(
            "get_trading_dates", provider._call_context, "SH", "", "", 5)
        self.assertEqual(out, ["20260901", "20260902"])

    def test_context_only_failure_keeps_the_plain_error(self):
        provider = BigQmtMarketDataProvider(_FakeContext(), native_xtdata=None)
        provider._native = lambda: None
        with self.assertRaises(NotImplementedError):
            provider._native_or_context(
                "get_trading_dates",
                lambda: (_ for _ in ()).throw(NotImplementedError("no such method")))


class EmptyNativeCalendarCrossCheckTest(unittest.TestCase):
    """#391: an empty SDK calendar is ambiguous -- cross-check ContextInfo.

    Reported 2026-09-29 09:00: both markets were asked for the same single
    (trading) day; SH came back with the date, SZ with []. The SDK calendar
    for one market was not loaded, and the empty list read as 休市 to the
    caller's executor, which then blocked SZ jobs for the whole session.
    The ContextInfo index-calendar path is empty on the same holidays, so
    cross-checking it only ever speaks up when the empty was data-not-ready.
    """

    class _EmptyNative(object):
        def __init__(self):
            self.calls = 0

        def get_trading_dates(self, *args):
            self.calls += 1
            return []

    class _CountingContext(object):
        def __init__(self, answer):
            self.calls = 0
            self.answer = answer

        def get_trading_dates(self, *args):
            self.calls += 1
            return list(self.answer)

    def test_empty_native_falls_to_the_index_calendar(self):
        native = self._EmptyNative()
        context = self._CountingContext(["20260929"])
        provider = BigQmtMarketDataProvider(context, native_xtdata=native)

        result = provider.get_trading_dates("SZ", "20260929", "20260929")

        self.assertEqual(result, ["20260929"])
        self.assertEqual(native.calls, 1)
        self.assertEqual(context.calls, 1)
        # The diagnostic names the path and the row count (#391's ask).
        note = provider._last_trading_dates_answer
        self.assertEqual(note["path"], "context(empty-native)")
        self.assertEqual(note["rows"], 1)
        self.assertEqual(note["market"], "SZ")

    def test_both_empty_stays_empty_and_costs_no_error(self):
        # A holiday: the index calendar is empty too, so empty stands.
        native = self._EmptyNative()
        context = self._CountingContext([])
        provider = BigQmtMarketDataProvider(context, native_xtdata=native)

        result = provider.get_trading_dates("SZ", "20261003", "20261003")

        self.assertEqual(result, [])

    def test_nonempty_native_does_not_pay_for_a_second_path(self):
        native = _FakeNativeModule(fail=False)
        context = self._CountingContext(["ctx-must-not-appear"])
        provider = BigQmtMarketDataProvider(context, native_xtdata=native)

        result = provider.get_trading_dates("SH", "", "", 5)

        self.assertEqual(result, ["20260901", "20260902"])
        self.assertEqual(context.calls, 0)
        self.assertEqual(provider._last_trading_dates_answer["path"], "native/sdk")
