# coding: utf-8
"""A window Big QMT has no local bars for must heal even when the placeholder
carries the previous close instead of zeros.

Issue #339 (@wolfeee, 华泰 terminal, bridge 0.3.40): ``get_market_data_ex``
for 002594.SZ 1m from 20260918 with ``dividend_type="front"`` answered 150
bars dated 20260919 -- a Saturday -- every one open=high=low=close=84.3
(Friday's close), volume 0, amount 0, suspendFlag 1. The terminal had no 1m
bars for the window and filled placeholders.

The client already self-heals that situation (raw download + retry), but
its detector was written against the 国金 terminal, whose placeholder is a
frame of zeros (measured 2026-09-21: 300750.SZ 1m 20260918 raw RPC = 241
rows all 0.0 / suspendFlag 1, and the client path then served 241 real
bars). Nonzero prices slip past ``_is_all_zero_any``, so on a build that
fills the previous close the heal never fires and the caller gets a day of
flat fake bars with nothing said.

The new signal is the one both builds share: every bar volume 0 and
suspendFlag 1. It is required of *every* served frame, so one genuinely
suspended stock inside a portfolio read does not turn the read into a
per-call download.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import xtquant_compat  # noqa: E402
from bigqmt_signal_trader.xtquant_compat import BigQmtXtData  # noqa: E402


def _placeholder(n=3, price=84.3, day="20260919"):
    """The reporter's shape: flat at the previous close, no trades."""
    stimes = ["%s%02d3000" % (day, 9 + i) for i in range(n)]
    return pd.DataFrame({
        "stime": stimes,
        "open": [price] * n, "high": [price] * n, "low": [price] * n,
        "close": [price] * n,
        "volume": [0] * n, "amount": [0.0] * n,
        "openInterest": [15] * n, "preClose": [0.0] * n, "suspendFlag": [1] * n,
    })


def _real(n=3, day="20260918"):
    stimes = ["%s%02d3000" % (day, 9 + i) for i in range(n)]
    return pd.DataFrame({
        "stime": stimes,
        "open": [84.0 + i for i in range(n)], "high": [84.5 + i for i in range(n)],
        "low": [83.5 + i for i in range(n)], "close": [84.2 + i for i in range(n)],
        "volume": [1000 + i for i in range(n)], "amount": [84000.0] * n,
        "openInterest": [0] * n, "preClose": [84.97] * n, "suspendFlag": [0] * n,
    })


class PlaceholderThenRealClient(object):
    """First ``get_market_data_ex`` read answers ``first``; later reads
    answer ``then``. Every download submit is counted."""

    def __init__(self, cache_dir, first, then):
        self.account_id = "acct"
        self.first = first
        self.then = then
        self.reads = 0
        self.download_submits = []
        self.local_cache_config = {"enabled": True, "dir": cache_dir, "fallback_rpc": False}

    def _redis(self):
        return None

    def call(self, method, params=None, account_id=None, timeout_seconds=None, **kw):
        params = params or {}
        if method == "download_history_data2":
            self.download_submits.append(dict(params))
            return True
        if method == "get_market_data_ex":
            self.reads += 1
            frames = self.first if self.reads == 1 else self.then
            return dict((c, f.copy()) for c, f in frames.items())
        raise AssertionError("unexpected rpc: %s" % method)


def _no_sleep(*_args, **_kwargs):
    return None


class PlaceholderBarsHealTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _read(self, first, then, codes, dividend_type="front"):
        client = PlaceholderThenRealClient(self.dir, first, then)
        xt = BigQmtXtData(client)
        with mock.patch.object(xtquant_compat.time, "sleep", _no_sleep):
            out = xt.get_market_data_ex(
                field_list=[], stock_list=codes, period="1m",
                start_time="20260918", count=-1, dividend_type=dividend_type,
            )
        return client, out

    def test_previous_close_placeholder_heals_and_serves_real_bars(self):
        """The #339 shape: nonzero flat prices, volume 0, suspendFlag 1."""
        client, out = self._read(
            {"002594.SZ": _placeholder()}, {"002594.SZ": _real()}, ["002594.SZ"])

        self.assertEqual(1, len(client.download_submits),
                         "heal did not fire on the previous-close placeholder")
        self.assertEqual(["002594.SZ"], client.download_submits[0]["stock_list"])
        self.assertEqual("1m", client.download_submits[0]["period"])
        df = out["002594.SZ"]
        self.assertGreater(int(df["volume"].sum()), 0, "still serving placeholder bars")
        self.assertTrue(all(str(i).startswith("20260918") for i in df.index),
                        "still serving the Saturday placeholder: %s" % list(df.index))

    def test_none_adjusted_read_heals_too(self):
        """The placeholder is a raw-store answer, so ``dividend_type="none"``
        gets the same flat bars -- and those codes count as served, which is
        why the missing-majority guard alone never catches them."""
        client, out = self._read(
            {"002594.SZ": _placeholder()}, {"002594.SZ": _real()}, ["002594.SZ"],
            dividend_type="none")

        self.assertEqual(1, len(client.download_submits))
        self.assertGreater(int(out["002594.SZ"]["volume"].sum()), 0)

    def test_one_traded_bar_is_not_a_placeholder(self):
        """A thin but traded window is real data: no heal, no download."""
        thin = _placeholder()
        thin.loc[1, "volume"] = 100
        thin.loc[1, "suspendFlag"] = 0
        client, _ = self._read({"002594.SZ": thin}, {"002594.SZ": _real()}, ["002594.SZ"])

        self.assertEqual([], client.download_submits)
        self.assertEqual(1, client.reads)

    def test_one_suspended_stock_in_a_portfolio_does_not_heal(self):
        """A genuinely suspended stock answers the same shape truthfully;
        it must not cost the whole portfolio read a download every call."""
        first = {"002594.SZ": _real(), "600000.SH": _placeholder()}
        client, out = self._read(first, first, ["002594.SZ", "600000.SH"])

        self.assertEqual([], client.download_submits)
        self.assertEqual(1, client.reads)
        self.assertIn("600000.SH", out)

    def test_every_code_flat_is_the_unpopulated_store_signal(self):
        first = {"002594.SZ": _placeholder(), "600000.SH": _placeholder(price=9.1)}
        then = {"002594.SZ": _real(), "600000.SH": _real()}
        client, out = self._read(first, then, ["002594.SZ", "600000.SH"])

        self.assertEqual(1, len(client.download_submits))
        self.assertEqual(sorted(["002594.SZ", "600000.SH"]),
                         sorted(client.download_submits[0]["stock_list"]))
        for code in ("002594.SZ", "600000.SH"):
            self.assertGreater(int(out[code]["volume"].sum()), 0, code)

    def test_placeholder_that_survives_the_retry_is_logged(self):
        """The download changed nothing (no data to download, or the
        download RPC itself is dead on that terminal): say so, do not hand
        back a flat frame silently."""
        first = {"002594.SZ": _placeholder()}
        client = PlaceholderThenRealClient(self.dir, first, first)
        xt = BigQmtXtData(client)
        with mock.patch.object(xtquant_compat.time, "sleep", _no_sleep), \
                mock.patch.object(xtquant_compat.log, "warning") as warn:
            xt.get_market_data_ex(field_list=[], stock_list=["002594.SZ"], period="1m",
                                  start_time="20260918", count=-1, dividend_type="front")

        self.assertEqual(1, len(client.download_submits))
        self.assertEqual(2, client.reads, "expected exactly one retry")
        self.assertTrue(warn.called, "placeholder after retry went unreported")
        text = " ".join(str(a) for a in warn.call_args[0])
        self.assertIn("placeholder", text)
        self.assertIn("suspendFlag", text)

    def test_frames_without_suspendflag_never_match(self):
        """The FormulaServer six-column path has no suspendFlag; a quiet
        window there (volume 0) must not be mistaken for a placeholder."""
        six = _placeholder().drop(columns=["suspendFlag", "openInterest", "preClose",
                                           "amount"])
        self.assertFalse(BigQmtXtData._is_placeholder_frame(six))
        self.assertFalse(BigQmtXtData._is_placeholder_all({"002594.SZ": six}))
        self.assertFalse(BigQmtXtData._is_placeholder_all({}))
        self.assertFalse(BigQmtXtData._is_placeholder_all(None))


if __name__ == "__main__":
    unittest.main()
