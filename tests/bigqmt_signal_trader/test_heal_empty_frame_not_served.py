# coding: utf-8
"""An empty frame in the answer is not a served code -- heal must see it.

Issue #387, reported by @shengyy with an offline repro: the server's
``_raw_market_data_payload`` fills an envelope for every *requested* code,
so a code the native read failed on (e.g. ErrorID 210000 subscription-limit
rejections on minute periods) comes back as an empty DataFrame, not a
missing key. ``_served_codes`` counted dict keys, so 14 of 17 natively
failed codes still read as fully served, the none-adjusted missing-majority
guard computed ``missing == 0``, and ``_heal_adjusted`` returned without
ever submitting the raw download that would have recovered them.

The fix counts rows, not keys. The majority guard is unchanged on purpose:
a handful of genuinely suspended/delisted codes in a full-market read must
not become a per-call download, and placeholder frames (#339) still count
as served -- they carry rows, and the placeholder branch owns them.
"""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import pandas as pd  # noqa: E402

from bigqmt_signal_trader.xtquant_compat import BigQmtXtData  # noqa: E402


def _frame(rows):
    if not rows:
        return pd.DataFrame(columns=["stime", "open", "close", "volume", "suspendFlag"])
    return pd.DataFrame(rows)


def _bar(code_index=0, when="20260928093100"):
    return dict(stime=when, open=10.0, high=10.0, low=10.0,
                close=10.0, volume=1, amount=1000.0, suspendFlag=0)


def _placeholder():
    # The #339 no-trade placeholder: flat zeros flagged suspended, one row.
    return pd.DataFrame([dict(stime="20260928", open=0.0, high=0.0, low=0.0,
                              close=0.0, volume=0, amount=0.0, suspendFlag=1)])


def _xt():
    xt = BigQmtXtData.__new__(BigQmtXtData)
    xt.client = mock.Mock()
    return xt


def _params(codes):
    return dict(stock_list=list(codes), period="1m", dividend_type="none",
                start_time="20260928000000", end_time="20260928235959")


class ServedCodesRowCounting(unittest.TestCase):
    def test_code_keyed_empty_frames_are_not_served(self):
        data = {code: _frame([]) for code in ("000001.SZ", "000002.SZ")}
        self.assertEqual(BigQmtXtData._served_codes(data), set())

    def test_code_keyed_counts_only_frames_with_rows(self):
        data = {"000001.SZ": _frame([_bar()]),
                "000002.SZ": _frame([]),
                "000003.SZ": _frame([_bar()])}
        self.assertEqual(BigQmtXtData._served_codes(data),
                         {"000001.SZ", "000003.SZ"})

    def test_field_keyed_counts_codes_with_any_nonempty_field(self):
        data = {"stime": {"000001.SZ": ["20260928093100"], "000002.SZ": []},
                "close": {"000001.SZ": [10.0], "000002.SZ": []}}
        self.assertEqual(BigQmtXtData._served_codes(data), {"000001.SZ"})

    def test_field_keyed_all_empty_serves_nobody(self):
        # The old heuristic fell back to the top-level keys here, which are
        # field names -- harmless for the missing count only by accident.
        data = {"stime": {"000001.SZ": []}, "close": {"000001.SZ": []}}
        self.assertEqual(BigQmtXtData._served_codes(data), set())

    def test_placeholder_frames_still_count_as_served(self):
        data = {"000001.SZ": _placeholder(), "000002.SZ": _frame([])}
        self.assertEqual(BigQmtXtData._served_codes(data), {"000001.SZ"})

    def test_non_dict_and_empty_dict(self):
        self.assertEqual(BigQmtXtData._served_codes(None), set())
        self.assertEqual(BigQmtXtData._served_codes({}), set())


class HealFiresOnEmptyFrames(unittest.TestCase):
    """The report's exact scenario, through _heal_adjusted end to end."""

    def _run_heal(self, xt, data, codes):
        with mock.patch("bigqmt_signal_trader.xtquant_compat.time.sleep"):
            return xt._heal_adjusted("get_market_data_ex", _params(codes),
                                     data, timeout_seconds=10)

    def test_majority_empty_frames_trigger_download_and_retry(self):
        codes = ["%06d.SZ" % i for i in range(1, 18)]
        served = {code: _frame([_bar()]) for code in codes[:3]}
        for code in codes[3:]:
            served[code] = _frame([])  # native failure -> empty envelope
        xt = _xt()
        xt.client.call.return_value = {code: _frame([_bar()]) for code in codes}

        healed = self._run_heal(xt, served, codes)

        submitted = [c for c in xt.client.call.call_args_list
                     if c[0][0] == "download_history_data2"]
        self.assertEqual(len(submitted), 1,
                         "14/17 empty envelopes must read as missing, not served")
        retries = [c for c in xt.client.call.call_args_list
                   if c[0][0] == "get_market_data_ex"]
        self.assertEqual(len(retries), 1)
        self.assertEqual(len(healed), 17)

    def test_minority_empty_does_not_heal(self):
        codes = ["%06d.SZ" % i for i in range(1, 18)]
        data = {code: _frame([_bar()]) for code in codes}
        data[codes[0]] = _frame([])  # one suspended stock, not an outage
        xt = _xt()

        result = self._run_heal(xt, data, codes)

        self.assertIs(result, data)
        xt.client.call.assert_not_called()

    def test_all_empty_frames_heal(self):
        codes = ["%06d.SZ" % i for i in range(1, 4)]
        data = {code: _frame([]) for code in codes}
        xt = _xt()
        xt.client.call.return_value = data

        self._run_heal(xt, data, codes)

        submitted = [c for c in xt.client.call.call_args_list
                     if c[0][0] == "download_history_data2"]
        self.assertEqual(len(submitted), 1)

    def test_heal_false_means_no_implicit_download(self):
        # The reporter's workaround -- explicit download, then read with
        # heal=False -- must stay zero-download on the read itself.
        xt = _xt()
        params = _params(["000001.SZ"])
        with mock.patch.object(BigQmtXtData, "_heal_adjusted") as heal:
            xt._get_market_data_ex_batch(params, heal=False)
        heal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
