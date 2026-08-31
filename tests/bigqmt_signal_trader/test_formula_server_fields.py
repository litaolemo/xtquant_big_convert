"""FormulaServer must not answer with NaN where RPC has the data (#104).

The direct path accepts any field name. For the four it does not have it
returns a column of NaN rather than an error, so a request looked answered and
was not. Measured against a live terminal:

    field_list=[...all 11 names...]   0.015s   preClose=nan  suspendFlag=nan
    field_list=[]                     (RPC)    preClose=9.0  suspendFlag=0

Same columns, same shape, twelve times faster, silently wrong -- and reaching
for it is exactly what someone does after being told that naming fields is what
unlocks the fast path.

The four are daily metadata (settelementPrice, openInterest, preClose,
suspendFlag), not bar data, which is why this path lacks them. preClose cannot
be reconstructed from close either: on an ex-dividend day it is the *adjusted*
previous close, measured at 8.89 against a previous close of 9.31 on
600000.SH -- a 4.5% gap on precisely the day a wrong value matters most.

The rule here is the one the dividend_type guard already followed: a request
this path cannot answer honestly goes to RPC.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.formula_server import (
    SERVED_FIELDS, _market_data_params)


SIX = ["open", "high", "low", "close", "volume", "amount"]
NAN_FIELDS = ("settelementPrice", "openInterest", "preClose", "suspendFlag")


def _params(fields, **extra):
    base = {"field_list": list(fields), "stock_list": ["600000.SH"],
            "period": "1d", "count": 2}
    base.update(extra)
    return base


def _refusal(fields, **extra):
    try:
        _market_data_params(_params(fields, **extra))
    except ValueError as exc:
        return str(exc)
    raise AssertionError("expected a refusal for %r" % (fields,))


class ServedTest(unittest.TestCase):
    """What the direct path answers with real numbers, measured live."""

    def test_the_six_bar_columns_route(self):
        wire = _market_data_params(_params(SIX))

        self.assertEqual(wire["fields"], SIX)

    def test_time_routes_alongside_them(self):
        """Measured: ['time','close'] came back with time=1788105600000."""
        wire = _market_data_params(_params(SIX + ["time"]))

        self.assertIn("time", wire["fields"])

    def test_stime_routes_too(self):
        wire = _market_data_params(_params(["stime", "close"]))

        self.assertIn("stime", wire["fields"])

    def test_the_served_set_is_exactly_what_was_measured(self):
        self.assertEqual(
            set(SERVED_FIELDS),
            {"open", "high", "low", "close", "volume", "amount", "time", "stime"})


class RefusedTest(unittest.TestCase):
    """The four that come back as NaN."""

    def test_each_one_is_refused_on_its_own(self):
        for field in NAN_FIELDS:
            message = _refusal(SIX + [field])
            self.assertIn(field, message, field)

    def test_naming_all_eleven_is_refused(self):
        """The tempting move: write every column name and keep the fast path.
        It used to work, at the cost of four NaN columns."""
        all_eleven = ["time"] + SIX + list(NAN_FIELDS)

        message = _refusal(all_eleven)

        self.assertIn("preClose", message)

    def test_the_refusal_says_where_the_data_is(self):
        message = _refusal(SIX + ["preClose"])

        self.assertIn("NaN", message)
        self.assertIn("RPC", message)

    def test_one_bad_field_refuses_the_whole_request(self):
        """Serving the rest and dropping the one would be the same silent
        hole in a different shape."""
        _refusal(SIX + ["preClose"])


class WhitelistTest(unittest.TestCase):
    """Unknown names go to RPC: slow is recoverable, wrong is not."""

    def test_an_unfamiliar_field_is_refused(self):
        _refusal(["close", "someFutureFieldName"])

    def test_it_is_not_a_blacklist_of_the_known_four(self):
        """A blacklist would let a newly added NaN field slip straight
        through -- exactly how this one got here."""
        message = _refusal(["close", "turnoverRate"])

        self.assertIn("turnoverRate", message)


class ExistingGuardsTest(unittest.TestCase):
    """The rule this one follows was already here; keep it working."""

    def test_adjusted_bars_are_still_refused(self):
        message = _refusal(SIX, dividend_type="front")

        self.assertIn("dividend_type", message)

    def test_tick_period_is_still_refused(self):
        message = _refusal(SIX, period="tick")

        self.assertIn("tick", message)

    def test_an_empty_field_list_is_still_refused(self):
        """"every field" is exactly what this path cannot do."""
        _refusal([])


if __name__ == "__main__":
    unittest.main()
