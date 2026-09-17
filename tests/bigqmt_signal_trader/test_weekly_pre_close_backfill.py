# coding: utf-8
"""Weekly bars come back with preClose == 0.0, so fill it client-side (#166).

Measured on 国金 2.1.19.0 / 0.3.19 (read-only, no orders): of the periods the
terminal serves, only ``1w`` is affected --

    period  rows  preClose non-zero
    1d       649  649
    1w       138    0        <-- this one
    1mon      33   33
    1q        11   11
    1hy        6    6
    1y         3    3

and it is 0.0 for every code tried (000001.SZ, 600000.SH, 600519.SH) under
both ``fill_data`` settings, so nothing the caller passes recovers it.

The correct value, which @yucejade named in the issue, is **the daily preClose
of the week's first trading day** -- not ``close[i-1]``. The two agree in
almost every week, which is exactly what makes ``close[i-1]`` dangerous: it is
wrong only on the weeks nobody has a sample of, and wrong in the silent
"looks like a price" way this repo keeps getting burned by. Two cases separate
them, and both are pinned below:

  * the FIRST bar of a window -- ``close[i-1]`` has no previous bar to read;
  * a week whose ex-dividend day IS its first trading day -- then the daily
    preClose is the adjusted reference price and last week's close is not.

The second case has no natural sample: scanning six liquid names since
2023-01-01 found ex-dividend days on Wed/Thu/Fri only, never on a week's first
trading day. So it is constructed here. A test built only from real weeks
would pass against both implementations -- the #88 shape, where the test
encodes the same premise as the code.

Bar labels: a ``1w`` label is the **Sunday** of its ISO week, verified against
the terminal -- weekly close equals the last daily close in Monday..Sunday for
19/19 weeks, including two holiday-shortened ones (20260503, 20260510).
"""

import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import (  # noqa: E402
    BigQmtXtData,
    PRE_CLOSE_BACKFILL_PERIODS,
    _iso_week_start_label,
)


# Real bars pulled from the live terminal (000001.SZ, dividend_type=none).
# preClose is 0.0 on every weekly bar, which is the bug.
WEEKLY = [
    # (stime,     close,  terminal preClose)
    ("20260621", 10.52, 0.0),
    ("20260628", 10.23, 0.0),
    ("20260705", 10.29, 0.0),
]

# The daily bars those weeks cover. Only the first trading day of each week
# matters to the fix, but the rest are here so the "pick the first" step is
# actually exercised rather than trivially satisfied.
DAILY = [
    # week 20260621 -> Mon 20260615..Thu 20260618 (20260619 was a holiday)
    ("20260615", 11.30, 11.24),
    ("20260616", 11.05, 11.30),
    ("20260617", 10.80, 11.05),
    ("20260618", 10.52, 10.80),
    # week 20260628 -> Mon 20260622..Fri 20260626
    ("20260622", 10.60, 10.52),
    ("20260623", 10.44, 10.60),
    ("20260624", 10.38, 10.44),
    ("20260625", 10.30, 10.38),
    ("20260626", 10.23, 10.30),
    # week 20260705 -> Mon 20260629..Fri 20260703
    ("20260629", 10.31, 10.23),
    ("20260630", 10.28, 10.31),
    ("20260701", 10.35, 10.28),
    ("20260702", 10.30, 10.35),
    ("20260703", 10.29, 10.30),
]


def _frame(rows, columns=("close", "preClose")):
    """A server-shaped frame: stime column, one row per bar."""
    data = {"stime": [r[0] for r in rows]}
    if "close" in columns:
        data["close"] = [r[1] for r in rows]
    if "preClose" in columns:
        data["preClose"] = [r[2] for r in rows]
    return pd.DataFrame(data)


class RecordingClient(object):
    """Answers get_market_data_ex per period and records every request.

    ``daily_error`` makes the daily lookup raise, to check the fill degrades
    to "leave the bars alone" rather than taking the whole call down with it.
    """

    def __init__(self, weekly=None, daily=None, codes=("000001.SZ",),
                 daily_error=None, monthly=None):
        self.account_id = "acct"
        self.calls = []
        self.codes = list(codes)
        self.weekly = WEEKLY if weekly is None else weekly
        self.daily = DAILY if daily is None else daily
        self.monthly = monthly
        self.daily_error = daily_error
        self.local_cache_config = {"enabled": False}

    def requests_for(self, period):
        return [params for method, params in self.calls
                if method == "get_market_data_ex" and params.get("period") == period]

    def call(self, method, params=None, account_id=None, timeout_seconds=None,
             **kwargs):
        params = params or {}
        self.calls.append((method, dict(params)))
        if method != "get_market_data_ex":
            return {}
        period = params.get("period")
        if period == "1d":
            if self.daily_error is not None:
                raise self.daily_error
            rows = self._window(self.daily, params)
            return {code: _frame(rows) for code in self.codes}
        if period == "1w":
            rows = self._window(self.weekly, params)
            return {code: _frame(rows) for code in self.codes}
        if period == "1mon" and self.monthly is not None:
            return {code: _frame(self.monthly) for code in self.codes}
        return {}

    @staticmethod
    def _window(rows, params):
        start = str(params.get("start_time") or "")
        end = str(params.get("end_time") or "")
        out = []
        for row in rows:
            if start and row[0] < start:
                continue
            if end and row[0] > end:
                continue
            out.append(row)
        return out


def _xtdata(**kwargs):
    return BigQmtXtData(RecordingClient(**kwargs))


def _pre_close(frame):
    return {str(idx): float(val) for idx, val in frame["preClose"].items()}


class WeeklyPreCloseTest(unittest.TestCase):

    def test_weekly_pre_close_is_the_first_trading_days_daily_pre_close(self):
        """The bug in one line: every one of these used to be 0.0."""
        xtdata = _xtdata()

        data = xtdata.get_market_data_ex(
            field_list=["close", "preClose"], stock_list=["000001.SZ"],
            period="1w", start_time="20260615", end_time="20260705",
            dividend_type="none")

        self.assertEqual(
            _pre_close(data["000001.SZ"]),
            {
                "20260621": 11.24,   # daily preClose of Mon 20260615
                "20260628": 10.52,   # daily preClose of Mon 20260622
                "20260705": 10.23,   # daily preClose of Mon 20260629
            },
        )

    def test_first_bar_of_the_window_is_filled(self):
        """A close[i-1] implementation cannot answer this one at all.

        Asking from 20260701 still returns the week labelled 20260628 (it is
        the ISO week 20260622..20260628). There is no weekly bar before it in
        the frame, so the only way to a real number is the daily lookup.
        """
        xtdata = _xtdata()

        data = xtdata.get_market_data_ex(
            field_list=["close", "preClose"], stock_list=["000001.SZ"],
            period="1w", start_time="20260628", end_time="20260705",
            dividend_type="none")

        filled = _pre_close(data["000001.SZ"])
        self.assertEqual(sorted(filled), ["20260628", "20260705"])
        self.assertEqual(filled["20260628"], 10.52)

    def test_ex_dividend_on_the_weeks_first_trading_day(self):
        """Constructed, because the market has not supplied one.

        Monday 20260831 goes ex-dividend: its daily preClose is the adjusted
        reference 11.60, while the previous week closed at 12.00. The daily
        preClose is the right answer; last week's close is the plausible
        wrong one.
        """
        weekly = [("20260830", 12.00, 0.0), ("20260906", 11.90, 0.0)]
        daily = [
            ("20260824", 11.85, 11.80),
            ("20260828", 12.00, 11.85),
            ("20260831", 11.72, 11.60),   # <-- ex-dividend, reference != 12.00
            ("20260904", 11.90, 11.72),
        ]
        xtdata = _xtdata(weekly=weekly, daily=daily)

        data = xtdata.get_market_data_ex(
            field_list=["close", "preClose"], stock_list=["000001.SZ"],
            period="1w", start_time="20260824", end_time="20260906",
            dividend_type="none")

        filled = _pre_close(data["000001.SZ"])
        self.assertEqual(filled["20260906"], 11.60)
        self.assertNotEqual(filled["20260906"], 12.00)

    def test_the_daily_lookup_keeps_the_callers_price_basis(self):
        """preClose from a 前复权 read is meaningless against 不复权 bars."""
        xtdata = _xtdata()

        xtdata.get_market_data_ex(
            field_list=["close", "preClose"], stock_list=["000001.SZ"],
            period="1w", start_time="20260615", end_time="20260705",
            dividend_type="front")

        daily = xtdata.client.requests_for("1d")
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]["dividend_type"], "front")
        # Filled rows carry no real preClose, and would be picked as a week's
        # "first trading day" if they were returned.
        self.assertIs(daily[0]["fill_data"], False)

    def test_the_daily_lookup_covers_the_first_weeks_monday(self):
        """Clipping the range at the first LABEL loses that week's own days."""
        xtdata = _xtdata()

        xtdata.get_market_data_ex(
            field_list=["close", "preClose"], stock_list=["000001.SZ"],
            period="1w", start_time="20260615", end_time="20260705",
            dividend_type="none")

        daily = xtdata.client.requests_for("1d")[0]
        self.assertLessEqual(daily["start_time"], "20260615")
        self.assertGreaterEqual(daily["end_time"], "20260705")


class ItStaysOutOfTheWayTest(unittest.TestCase):

    def test_no_daily_lookup_when_the_terminal_already_answers(self):
        """The extra request must not be a tax on terminals that work (#104)."""
        weekly = [("20260628", 10.23, 10.52), ("20260705", 10.29, 10.23)]
        xtdata = _xtdata(weekly=weekly)

        data = xtdata.get_market_data_ex(
            field_list=["close", "preClose"], stock_list=["000001.SZ"],
            period="1w", start_time="20260628", end_time="20260705",
            dividend_type="none")

        self.assertEqual(xtdata.client.requests_for("1d"), [])
        self.assertEqual(_pre_close(data["000001.SZ"])["20260628"], 10.52)

    def test_no_daily_lookup_when_pre_close_was_not_asked_for(self):
        xtdata = _xtdata()

        xtdata.get_market_data_ex(
            field_list=["close"], stock_list=["000001.SZ"], period="1w",
            start_time="20260615", end_time="20260705", dividend_type="none")

        self.assertEqual(xtdata.client.requests_for("1d"), [])

    def test_only_the_measured_period_is_touched(self):
        """1mon carries a real preClose here; a zero there is not this bug.

        Scope follows the measurement: 1w is what was observed broken, on this
        terminal build. Widening it needs the same read-only scan first, not an
        assumption that every multi-day period behaves alike.
        """
        self.assertEqual(tuple(PRE_CLOSE_BACKFILL_PERIODS), ("1w",))

        monthly = [("20260731", 11.63, 0.0), ("20260831", 11.65, 0.0)]
        xtdata = _xtdata(monthly=monthly)

        data = xtdata.get_market_data_ex(
            field_list=["close", "preClose"], stock_list=["000001.SZ"],
            period="1mon", start_time="20260701", end_time="20260831",
            dividend_type="none")

        self.assertEqual(xtdata.client.requests_for("1d"), [])
        # No daily lookup happened. The zero itself is now patched by the
        # row-level lag fill (278de3f: previous bar's close, for periods
        # without an exact source) -- a different mechanism from this one,
        # which is why 20260831 reads last month's close, not a daily value.
        self.assertEqual(_pre_close(data["000001.SZ"])["20260831"], 11.63)

    def test_it_can_be_turned_off(self):
        xtdata = _xtdata()

        data = xtdata.get_market_data_ex(
            field_list=["close", "preClose"], stock_list=["000001.SZ"],
            period="1w", start_time="20260615", end_time="20260705",
            dividend_type="none", backfill_pre_close=False)

        self.assertEqual(xtdata.client.requests_for("1d"), [])
        self.assertEqual(_pre_close(data["000001.SZ"])["20260628"], 0.0)

    def test_a_failing_daily_lookup_does_not_take_the_weekly_bars_down(self):
        """Degrade to the old answer. Raising would be a regression."""
        xtdata = _xtdata(daily_error=RuntimeError("bridge timed out"))

        data = xtdata.get_market_data_ex(
            field_list=["close", "preClose"], stock_list=["000001.SZ"],
            period="1w", start_time="20260615", end_time="20260705",
            dividend_type="none")

        self.assertEqual(list(data["000001.SZ"]["close"])[-1], 10.29)
        self.assertEqual(_pre_close(data["000001.SZ"])["20260628"], 0.0)

    def test_a_week_with_no_daily_bars_is_left_at_zero(self):
        """No evidence -> no number. Inventing one is the failure mode here."""
        xtdata = _xtdata(daily=[("20260622", 10.60, 10.52),
                                ("20260626", 10.23, 10.30)])

        data = xtdata.get_market_data_ex(
            field_list=["close", "preClose"], stock_list=["000001.SZ"],
            period="1w", start_time="20260615", end_time="20260705",
            dividend_type="none")

        filled = _pre_close(data["000001.SZ"])
        self.assertEqual(filled["20260628"], 10.52)   # has daily bars
        self.assertEqual(filled["20260621"], 0.0)     # has none
        self.assertEqual(filled["20260705"], 0.0)     # has none


class IsoWeekStartTest(unittest.TestCase):

    def test_a_sunday_label_maps_to_its_own_monday(self):
        self.assertEqual(_iso_week_start_label("20260628"), "20260622")
        self.assertEqual(_iso_week_start_label("20260705"), "20260629")

    def test_a_holiday_shortened_week_still_starts_on_monday(self):
        # 20260619 was a holiday; the week still runs 20260615..20260621.
        self.assertEqual(_iso_week_start_label("20260621"), "20260615")

    def test_a_label_that_is_not_a_sunday_maps_to_its_iso_week(self):
        # Never observed, but the rule should not depend on the label's
        # weekday -- Monday..Sunday is the week either way.
        self.assertEqual(_iso_week_start_label("20260626"), "20260622")

    def test_junk_is_refused_rather_than_guessed(self):
        self.assertIsNone(_iso_week_start_label(""))
        self.assertIsNone(_iso_week_start_label("not-a-date"))


if __name__ == "__main__":
    unittest.main()
