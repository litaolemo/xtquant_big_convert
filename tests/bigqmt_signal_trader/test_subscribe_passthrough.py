# coding: utf-8
"""subscribe must reach big QMT when the caller gives it, and stay absent when
they don't (#361).

Big QMT's own signature ends with it:

    C.get_market_data_ex(fields=[], stock_code=[], period='follow',
                         start_time='', end_time='', count=-1,
                         dividend_type='follow', fill_data=True, subscribe=True)

and subscribe=True (the terminal default) moves every queried instrument into
a resident in-memory subscription pool: batch-pulling 1m bars across hundreds
of codes grows the QMT process monotonically until it crashes or stops
answering (#361's repro, 500 codes). Callers doing historical backfills need
subscribe=False ("read local data only"); callers who say nothing must keep
the terminal default so nothing about the existing behaviour moves.

Like fill_data (#167), subscribe rides in a shape of its own ahead of the bare
one: _call_first_supported falls through only on TypeError, so a terminal whose
signature lacks subscribe still reaches a working shape.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider
from bigqmt_signal_trader.xtquant_compat import BigQmtXtData


class Recorder(object):
    """A ContextInfo that records how it was called.

    ``reject`` names kwargs this terminal's signature does not have; passing
    one raises TypeError, exactly as a real mismatch would.
    """

    def __init__(self, reject=()):
        self.reject = set(reject)
        self.calls = []

    def _record(self, name, args, kwargs):
        bad = self.reject.intersection(kwargs)
        self.calls.append({"method": name, "args": args, "kwargs": dict(kwargs),
                           "rejected": sorted(bad)})
        if bad:
            raise TypeError("unexpected keyword argument %r" % sorted(bad)[0])
        return {"600000.SH": {"close": [1.0]}}

    def get_market_data_ex(self, *args, **kwargs):
        return self._record("get_market_data_ex", args, kwargs)


class _CaptureClient(object):
    """Client stand-in: records RPC params, answers empty."""

    account_id = "acct"

    def __init__(self):
        self.calls = []

    def call(self, method, params=None, **kwargs):
        self.calls.append((method, dict(params or {})))
        return {}


class ServerSideTest(unittest.TestCase):
    def test_subscribe_false_is_sent_in_the_first_shape(self):
        context = Recorder()
        BigQmtMarketDataProvider(context_info=context).get_market_data_ex(
            field_list=[], stock_list=["600000.SH"], period="1m",
            count=240, subscribe=False)

        first = context.calls[0]
        self.assertIn("subscribe", first["kwargs"])
        self.assertFalse(first["kwargs"]["subscribe"])
        self.assertEqual(len(context.calls), 1, "should not need a fallback")

    def test_subscribe_true_is_sent_explicitly(self):
        context = Recorder()
        BigQmtMarketDataProvider(context_info=context).get_market_data_ex(
            field_list=[], stock_list=["600000.SH"], period="1d",
            subscribe=True)

        self.assertTrue(context.calls[0]["kwargs"]["subscribe"])

    def test_unsaid_means_no_shape_carries_it(self):
        """The terminal default (True) must stay the terminal's own choice."""
        provider = BigQmtMarketDataProvider(context_info=Recorder())
        shapes = provider._market_data_shapes(
            "get_market_data_ex", field_list=[], stock_list=["600000.SH"])

        for _name, _args, kwargs in shapes:
            self.assertNotIn("subscribe", kwargs)

    def test_a_terminal_rejecting_subscribe_falls_back(self):
        context = Recorder(reject={"subscribe"})

        result = BigQmtMarketDataProvider(context_info=context).get_market_data_ex(
            field_list=[], stock_list=["600000.SH"], period="1m",
            count=240, subscribe=False)

        self.assertTrue(result, "the call must still succeed")
        self.assertEqual(context.calls[0]["rejected"], ["subscribe"])
        self.assertNotIn("subscribe", context.calls[1]["kwargs"])

    def test_the_big_qmt_key_names_are_kept(self):
        context = Recorder()
        BigQmtMarketDataProvider(context_info=context).get_market_data_ex(
            field_list=["close"], stock_list=["600000.SH"], period="1m",
            count=240, subscribe=False)

        kwargs = context.calls[0]["kwargs"]
        self.assertEqual(kwargs["fields"], ["close"])
        self.assertEqual(kwargs["stock_code"], ["600000.SH"])


class ClientSideTest(unittest.TestCase):
    def test_subscribe_false_reaches_the_rpc_params(self):
        client = _CaptureClient()
        xt = BigQmtXtData(client)
        xt.get_market_data_ex(
            field_list=["close"], stock_list=["600000.SH"], period="1m",
            count=240, subscribe=False)

        md_calls = [params for method, params in client.calls
                    if method == "get_market_data_ex"]
        self.assertTrue(md_calls, "no RPC went out")
        for params in md_calls:
            self.assertIs(params["subscribe"], False)

    def test_unsaid_stays_absent_from_the_rpc_params(self):
        client = _CaptureClient()
        xt = BigQmtXtData(client)
        xt.get_market_data_ex(
            field_list=["close"], stock_list=["600000.SH"], period="1d",
            count=5)

        md_calls = [params for method, params in client.calls
                    if method == "get_market_data_ex"]
        self.assertTrue(md_calls, "no RPC went out")
        for params in md_calls:
            self.assertNotIn("subscribe", params)


if __name__ == "__main__":
    unittest.main()
