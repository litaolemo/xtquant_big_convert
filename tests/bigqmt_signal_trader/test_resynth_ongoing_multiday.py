# coding: utf-8
"""#226: an ongoing multi-day bar must be rebuilt from the period's daily
bars, not truncated to the request window.

Reported live (国金模拟, 2026-09-04 09:40): a 1w read with start=end=today
answered the in-progress weekly bar with TODAY's volume (the week's earlier
days lost); a window covering the whole week answered correctly; after close
the narrow window answered correctly (the bar is finalized). MiniQMT's
get_local_data semantics -- start/end filter the returned rows, never the
synthesis material -- is what this rebuild restores.
"""
import datetime as dt
import os
import sys
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import xtquant_compat
from bigqmt_signal_trader.xtquant_compat import (
    BigQmtXtData, _halfyear_start_label, _in_trading_session,
    _iso_week_start_label, _month_start_label, _period_end_label,
    _quarter_start_label, _year_start_label,
)

# A Tuesday in session: the last 1w bar (label = the week's Sunday in big
# QMT, 20260913 here covers Mon 09-07..Sun 09-13) is in progress on 09-08.
TODAY = dt.datetime(2026, 9, 8, 13, 30)
WEEK_LABEL = "20260913"   # the Sunday of the week containing TODAY
WEEK_START = "20260907"   # Monday


def _frame(labels, columns):
    """A pandas frame the way the client holds one."""
    import pandas as pd
    data = {name: values for name, values in columns.items()}
    return pd.DataFrame(data, index=labels)


def _weekly_frame(volume, high, low, open_, close, amount):
    return _frame(
        [WEEK_LABEL],
        {"open": [open_], "high": [high], "low": [low], "close": [close],
         "volume": [volume], "amount": [amount]},
    )


def _daily_frame():
    """Monday + Tuesday of the ongoing week."""
    return _frame(
        ["20260907", "20260908"],
        {"open": [10.0, 11.0], "high": [10.8, 11.9], "low": [9.9, 10.9],
         "close": [10.5, 11.5], "volume": [1000, 2000], "amount": [10500.0, 22600.0]},
    )


class _XtData(BigQmtXtData):
    """Just enough wiring for the resynth method: a stub inner read."""

    def __init__(self, daily_frame):
        self._daily_frame = daily_frame
        self.daily_calls = []

    def get_market_data_ex(self, **kwargs):
        self.daily_calls.append(kwargs)
        return {"000001.SZ": self._daily_frame}


class TriggerGatesTest(unittest.TestCase):
    def _resynth(self, data, **kwargs):
        xt = _XtData(_daily_frame())
        params = dict(period="1w", start_time="20260908", end_time="20260908",
                      dividend_type="none")
        params.update(kwargs)
        with mock.patch.object(xtquant_compat, "_in_trading_session", lambda now: True):
            with mock.patch.object(xtquant_compat, "_now", lambda: TODAY):
                return xt._resynth_ongoing_multiday_bars(data, **params), xt.daily_calls

    def test_an_ongoing_bar_with_a_cut_window_is_rebuilt(self):
        data = {"000001.SZ": _weekly_frame(6763, 11.0, 10.2, 10.9, 10.8, 7000.0)}

        out, calls = self._resynth(data)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["period"], "1d")
        self.assertEqual(calls[0]["start_time"], WEEK_START)
        self.assertEqual(calls[0]["end_time"], "20260908")
        frame = out["000001.SZ"]
        self.assertEqual(list(frame["volume"]), [3000])          # 1000+2000
        self.assertEqual(list(frame["amount"]), [33100.0])       # summed
        self.assertEqual(list(frame["high"]), [11.9])            # week high
        self.assertEqual(list(frame["low"]), [9.9])              # week low
        self.assertEqual(list(frame["open"]), [10.0])            # Monday's open
        self.assertEqual(list(frame["close"]), [11.5])           # latest close

    def test_a_window_covering_the_period_still_reconciles(self):
        # #307: a window covering the whole period is no longer an exemption
        # -- the covered answer was measured wrong too (partial-days and the
        # zero-volume form). 6763 != 1000+2000, so the bar is rebuilt.
        data = {"000001.SZ": _weekly_frame(6763, 11.0, 10.2, 10.9, 10.8, 7000.0)}

        out, calls = self._resynth(data, start_time="20260907")

        self.assertEqual(len(calls), 1)
        self.assertEqual(list(out["000001.SZ"]["volume"]), [3000])

    def test_a_covered_bar_that_already_agrees_is_left_untouched(self):
        # The daily sum is 3000 and the bar already says 3000: no rewrite.
        data = {"000001.SZ": _weekly_frame(3000, 11.9, 9.9, 10.0, 11.5, 33100.0)}

        out, calls = self._resynth(data, start_time="20260907")

        self.assertEqual(len(calls), 1)          # the reconciliation read
        self.assertEqual(list(out["000001.SZ"]["volume"]), [3000])
        self.assertEqual(list(out["000001.SZ"]["open"]), [10.0])

    def test_a_zero_volume_ongoing_bar_is_rebuilt(self):
        # #307: the 09:26 rescue answer carries no period accumulation.
        data = {"000001.SZ": _weekly_frame(0, 11.9, 9.9, 10.0, 11.5, 0.0)}

        out, calls = self._resynth(data, start_time="20260907")

        self.assertEqual(len(calls), 1)
        self.assertEqual(list(out["000001.SZ"]["volume"]), [3000])

    def test_a_frame_without_a_volume_column_falls_back_to_rebuild(self):
        # Cannot prove agreement without the column: rebuild (the original
        # #226 behaviour for such frames).
        frame = _frame([WEEK_LABEL], {"close": [10.8]})
        data = {"000001.SZ": frame}

        out, calls = self._resynth(data, start_time="20260907")

        self.assertEqual(len(calls), 1)
        self.assertEqual(list(out["000001.SZ"]["close"]), [11.5])

    def test_a_missing_ongoing_bar_is_appended(self):
        # #307 mid-session form: the answer drops the current period's bar
        # entirely (the last row is finalized history) while the window
        # covers the period -- append the ongoing bar from the dailies.
        frame = _weekly_frame(6763, 11.0, 10.2, 10.9, 10.8, 7000.0)
        frame.index = ["20260906"]              # last week, finalized
        data = {"000001.SZ": frame}

        out, calls = self._resynth(data, start_time="20260907")

        self.assertEqual(len(calls), 1)
        frame = out["000001.SZ"]
        self.assertEqual(list(frame.index), ["20260906", WEEK_LABEL])
        self.assertEqual(list(frame["volume"]), [6763, 3000])
        self.assertEqual(list(frame["close"]), [10.8, 11.5])

    def test_a_missing_ongoing_bar_is_not_appended_when_the_window_skips_it(self):
        # A window that starts inside the current period asked for a slice,
        # not the period: keep the honest finalized answer.
        frame = _weekly_frame(6763, 11.0, 10.2, 10.9, 10.8, 7000.0)
        frame.index = ["20260906"]
        data = {"000001.SZ": frame}

        out, calls = self._resynth(data, start_time="20260908")

        self.assertEqual(calls, [])
        self.assertEqual(list(out["000001.SZ"].index), ["20260906"])

    def test_a_finalized_bar_does_not_trigger(self):
        # Last week's bar (label 20260906) does not contain today.
        frame = _weekly_frame(6763, 11.0, 10.2, 10.9, 10.8, 7000.0)
        frame.index = ["20260906"]
        data = {"000001.SZ": frame}

        out, calls = self._resynth(data)

        self.assertEqual(calls, [])
        self.assertEqual(list(out["000001.SZ"]["volume"]), [6763])

    def test_no_start_time_never_triggers(self):
        data = {"000001.SZ": _weekly_frame(6763, 11.0, 10.2, 10.9, 10.8, 7000.0)}

        out, calls = self._resynth(data, start_time="")

        self.assertEqual(calls, [])

    def test_daily_read_failure_keeps_the_terminal_answer(self):
        xt = _XtData(None)
        xt.get_market_data_ex = None

        def raising(**kwargs):
            raise RuntimeError("redis down")

        xt.get_market_data_ex = raising
        data = {"000001.SZ": _weekly_frame(6763, 11.0, 10.2, 10.9, 10.8, 7000.0)}
        with mock.patch.object(xtquant_compat, "_in_trading_session", lambda now: True):
            with mock.patch.object(xtquant_compat, "_now", lambda: TODAY):
                out = xt._resynth_ongoing_multiday_bars(
                    data, period="1w", start_time="20260908",
                    end_time="20260908", dividend_type="none")

        self.assertEqual(list(out["000001.SZ"]["volume"]), [6763])

    def test_a_daily_read_with_no_bars_keeps_the_terminal_answer(self):
        xt = _XtData(_daily_frame().iloc[0:0])
        data = {"000001.SZ": _weekly_frame(6763, 11.0, 10.2, 10.9, 10.8, 7000.0)}
        with mock.patch.object(xtquant_compat, "_in_trading_session", lambda now: True):
            with mock.patch.object(xtquant_compat, "_now", lambda: TODAY):
                out = xt._resynth_ongoing_multiday_bars(
                    data, period="1w", start_time="20260908",
                    end_time="20260908", dividend_type="none")

        self.assertEqual(list(out["000001.SZ"]["volume"]), [6763])

    def test_out_of_session_never_triggers(self):
        data = {"000001.SZ": _weekly_frame(6763, 11.0, 10.2, 10.9, 10.8, 7000.0)}
        xt = _XtData(_daily_frame())
        with mock.patch.object(xtquant_compat, "_in_trading_session", lambda now: False):
            out = xt._resynth_ongoing_multiday_bars(
                data, period="1w", start_time="20260908", end_time="20260908",
                dividend_type="none")
        self.assertEqual(list(out["000001.SZ"]["volume"]), [6763])
        self.assertEqual(xt.daily_calls, [])

    def test_minute_and_daily_periods_never_trigger(self):
        data = {"000001.SZ": _weekly_frame(6763, 11.0, 10.2, 10.9, 10.8, 7000.0)}
        xt = _XtData(_daily_frame())
        with mock.patch.object(xtquant_compat, "_in_trading_session", lambda now: True):
            out = xt._resynth_ongoing_multiday_bars(
                data, period="1d", start_time="20260908", end_time="20260908",
                dividend_type="none")
        self.assertEqual(list(out["000001.SZ"]["volume"]), [6763])
        self.assertEqual(xt.daily_calls, [])


class PeriodSpanHelpersTest(unittest.TestCase):
    def test_period_starts(self):
        self.assertEqual(_iso_week_start_label("20260913"), "20260907")
        self.assertEqual(_month_start_label("20260930"), "20260901")
        self.assertEqual(_quarter_start_label("20261120"), "20261001")
        self.assertEqual(_quarter_start_label("20260205"), "20260101")
        self.assertEqual(_halfyear_start_label("20260315"), "20260101")
        self.assertEqual(_halfyear_start_label("20260915"), "20260701")
        self.assertEqual(_year_start_label("20261231"), "20260101")

    def test_period_ends(self):
        self.assertEqual(_period_end_label("1w", "20260913"), "20260913")
        self.assertEqual(_period_end_label("1mon", "20260930"), "20260930")
        self.assertEqual(_period_end_label("1q", "20261120"), "20261231")
        self.assertEqual(_period_end_label("1hy", "20260315"), "20260630")
        self.assertEqual(_period_end_label("1hy", "20260915"), "20261231")
        self.assertEqual(_period_end_label("1y", "20261231"), "20261231")

    def test_unparseable_labels_answer_none_not_a_guess(self):
        self.assertIsNone(_month_start_label("not-a-date"))
        self.assertIsNone(_period_end_label("1mon", "not-a-date"))


class SessionGateTest(unittest.TestCase):
    def test_the_gate(self):
        self.assertTrue(_in_trading_session(TODAY))                       # Tue 13:30
        self.assertTrue(_in_trading_session(dt.datetime(2026, 9, 8, 9, 30)))
        self.assertTrue(_in_trading_session(dt.datetime(2026, 9, 8, 9, 26)))   # #307
        self.assertTrue(_in_trading_session(dt.datetime(2026, 9, 8, 9, 15)))
        self.assertFalse(_in_trading_session(dt.datetime(2026, 9, 8, 15, 6)))
        self.assertFalse(_in_trading_session(dt.datetime(2026, 9, 5, 13, 30)))  # Sat
        self.assertFalse(_in_trading_session(dt.datetime(2026, 9, 8, 9, 14)))


if __name__ == "__main__":
    unittest.main()
