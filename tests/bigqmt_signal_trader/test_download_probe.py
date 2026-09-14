# coding: utf-8
"""probe_capabilities must separate "download API exposed" from "update usable" (#277).

The reporter's terminal (Guojin 2.1.19.0, miniQMT closed by hand so the 58610
xtdata service was gone) looked like this:

    native xtdata SDK       download_financial_data / download_financial_data2
                            both importable and callable
    get_financial_data      Capital rows for 605090.SH still non-empty
    download_financial_data raises `无法连接行情服务` from the SDK, then
                            NotImplementedError from ContextInfo

A capability table built from callable() alone reports that terminal as
"download available". It is not: the library is readable and cannot be
refreshed. The probe now makes one small real download call and reports the
two facts under different keys, with a verdict that names the reporter's
case explicitly.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers


SERVICE_DOWN = Exception("无法连接行情服务！")


class Context(object):
    """Big QMT's ContextInfo: reads the on-disk financial rows, has no download."""

    accid = "acct"

    def __init__(self, rows=3):
        self.rows = rows
        self.get_calls = []

    def get_financial_data(self, field_list, stock_list, start, end, report_type="report_time"):
        self.get_calls.append((list(field_list), list(stock_list), start, end))
        return [{"total_capital": 1.0}] * self.rows


class NativeSdk(object):
    """The bundled xtdata: download functions exist; whether they work is `fail`."""

    def __init__(self, fail=None, exposed=("download_financial_data", "download_financial_data2")):
        self.fail = fail
        self.calls = []
        for name in exposed:
            setattr(self, name, self._make(name))

    def _make(self, name):
        def download(**kwargs):
            self.calls.append((name, kwargs))
            if self.fail is not None:
                raise self.fail
            return None                  # what the real SDK returns on success
        return download


def _provider(context=None, native=None):
    provider = BigQmtMarketDataProvider(context_info=context if context is not None else Context())
    provider._native = lambda: native
    return provider


class ReporterCaseTest(unittest.TestCase):
    """The #277 terminal: exposed, readable, not updatable."""

    def setUp(self):
        self.context = Context(rows=3)
        self.native = NativeSdk(fail=SERVICE_DOWN)
        self.report = _provider(self.context, self.native).probe_download_channels()

    def test_the_verdict_names_the_case(self):
        for name in ("download_financial_data", "download_financial_data2"):
            entry = self.report["functions"][name]
            self.assertTrue(entry["sdk_exposed"], name)
            self.assertFalse(entry["contextinfo_exposed"], name)
            self.assertEqual(entry["verdict"], "exposed_but_service_unreachable", name)

    def test_the_sdk_error_is_kept_verbatim(self):
        call = self.report["sdk_call"]
        self.assertTrue(call["attempted"])
        self.assertFalse(call["ok"])
        self.assertIn("无法连接行情服务", call["error"])
        self.assertIsInstance(call["seconds"], float)

    def test_existing_rows_are_reported_under_their_own_key(self):
        readback = self.report["readback_existing_rows"]
        self.assertTrue(readback["ok"])
        self.assertEqual(readback["rows"], 3)
        # The key name and the note both say what this does NOT prove.
        self.assertIn("updated", readback["note"])

    def test_only_one_dial_is_paid(self):
        """Both functions sit on one data service; a second timeout proves nothing."""
        self.assertEqual(len(self.native.calls), 1)
        self.assertEqual(self.native.calls[0][0], "download_financial_data")

    def test_the_dial_is_small(self):
        name, kwargs = self.native.calls[0]
        self.assertEqual(kwargs["stock_list"], ["000001.SZ"])
        self.assertEqual(kwargs["table_list"], ["Capital"])
        self.assertEqual(len(kwargs["start_time"]), 8)
        self.assertEqual(len(kwargs["end_time"]), 8)
        self.assertLess(kwargs["start_time"], kwargs["end_time"])


class ServiceUpTest(unittest.TestCase):
    def test_a_download_that_returns_is_update_usable(self):
        report = _provider(Context(), NativeSdk()).probe_download_channels()
        self.assertTrue(report["sdk_call"]["ok"])
        for name in ("download_financial_data", "download_financial_data2"):
            self.assertEqual(report["functions"][name]["verdict"], "update_usable")


class NothingExposedTest(unittest.TestCase):
    def test_no_sdk_and_no_contextinfo_download_is_not_exposed(self):
        class Bare(object):
            accid = "acct"

        report = _provider(Bare(), None).probe_download_channels()
        self.assertFalse(report["native_xtdata_loaded"])
        self.assertFalse(report["sdk_call"]["attempted"])
        for name in ("download_financial_data", "download_financial_data2"):
            self.assertEqual(report["functions"][name]["verdict"], "not_exposed")
        # No get_financial_data either: reported, not raised.
        self.assertFalse(report["readback_existing_rows"]["ok"])
        self.assertIn("NotImplementedError", report["readback_existing_rows"]["error"])

    def test_an_sdk_missing_only_the_dialled_function_is_still_reported(self):
        native = NativeSdk(exposed=("download_financial_data2",))
        report = _provider(Context(), native).probe_download_channels()
        self.assertFalse(report["sdk_call"]["attempted"])
        self.assertEqual(report["functions"]["download_financial_data"]["verdict"], "not_exposed")
        self.assertEqual(report["functions"]["download_financial_data2"]["verdict"], "exposed_untested")


class DialOptOutTest(unittest.TestCase):
    def test_dial_false_reports_exposure_without_calling(self):
        native = NativeSdk(fail=SERVICE_DOWN)
        report = _provider(Context(), native).probe_download_channels(dial=False)
        self.assertEqual(native.calls, [])
        self.assertFalse(report["sdk_call"]["attempted"])
        self.assertIn("download_probe", report["sdk_call"]["reason"])
        self.assertEqual(report["functions"]["download_financial_data"]["verdict"], "exposed_untested")


class DeadMarkCacheTest(unittest.TestCase):
    """The probe measures now; the 600s failure cache is an earlier measurement."""

    def test_a_cached_failure_does_not_stop_the_probe_from_dialling(self):
        native = NativeSdk()
        provider = _provider(Context(), native)
        provider._native_dead_marks()["download_financial_data"] = __import__("time").time()

        report = provider.probe_download_channels()

        self.assertEqual(len(native.calls), 1)
        self.assertTrue(report["sdk_call"]["ok"])
        # ...and a success clears the stale mark for the next real caller.
        self.assertNotIn("download_financial_data", provider._native_dead_marks())

    def test_a_failed_dial_marks_the_function_so_callers_do_not_pay_twice(self):
        provider = _provider(Context(), NativeSdk(fail=SERVICE_DOWN))
        provider.probe_download_channels()
        self.assertTrue(provider._native_known_dead("download_financial_data"))


class RowCountTest(unittest.TestCase):
    def test_none_and_empty_frames_count_as_zero(self):
        class EmptyFrame(object):
            empty = True

            def __len__(self):
                return 0

        count = BigQmtMarketDataProvider._count_probe_rows
        self.assertEqual(count(None), 0)
        self.assertEqual(count(EmptyFrame()), 0)
        self.assertEqual(count([1, 2]), 2)
        self.assertEqual(count(object()), 1)


def _handlers(provider):
    return BigQmtRpcHandlers(
        account_id="acct",
        market_data=provider,
        position_provider=None,
        order_gateway=DryRunOrderGateway(),
        qmt_api={},
    )


class OverRpcTest(unittest.TestCase):
    def test_probe_capabilities_carries_the_download_probe(self):
        provider = _provider(Context(), NativeSdk(fail=SERVICE_DOWN))
        out = _handlers(provider).handle("probe_capabilities", {})
        probe = out["download_probe"]
        self.assertEqual(probe["functions"]["download_financial_data"]["verdict"],
                         "exposed_but_service_unreachable")
        self.assertEqual(probe["readback_existing_rows"]["rows"], 3)

    def test_download_probe_false_skips_the_dial_over_rpc(self):
        native = NativeSdk(fail=SERVICE_DOWN)
        out = _handlers(_provider(Context(), native)).handle(
            "probe_capabilities", {"download_probe": "false"})
        self.assertEqual(native.calls, [])
        self.assertFalse(out["download_probe"]["sdk_call"]["attempted"])

    def test_a_probe_that_blows_up_does_not_take_the_whole_report_down(self):
        provider = _provider(Context(), NativeSdk())
        provider.probe_download_channels = lambda dial=True: (_ for _ in ()).throw(RuntimeError("boom"))
        out = _handlers(provider).handle("probe_capabilities", {})
        self.assertIn("RuntimeError: boom", out["download_probe"]["error"])
        self.assertIn("sector_probe", out)          # the rest still answered


if __name__ == "__main__":
    unittest.main()
