"""When the long-list recovery cannot help, say what actually failed (#104).

The recovery added for a long explicit code list has its own failure path: the
direct call times out, the market re-read is tried, and that does not work
either. The code then did a bare ``raise`` -- but by then the ``except`` block
had already exited, so there was no active exception:

    File ".../xtquant_compat.py", line 1163, in get_full_tick
        raise
    RuntimeError: No active exception to reraise

which is what frank0532 got, in place of the timeout that actually happened.
The re-read swallowed its own reason too (``except Exception: return None``),
so nothing anywhere knew why the recovery had failed.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import xtquant_compat as compat
from bigqmt_signal_trader.xtquant_compat import LARGE_CODE_LIST


def _codes(count, market="SH"):
    return ["%06d.%s" % (600000 + index, market) for index in range(count)]


class FakeClient(object):
    """Fails the direct call, and optionally the market re-read too."""

    def __init__(self, direct_error, market_error=None, market_answer=None):
        self.account_id = "acct"
        self.local_cache_config = {}
        self.full_tick_cache_config = {}
        self.requests = []
        self._direct_error = direct_error
        self._market_error = market_error
        self._market_answer = market_answer or {}

    def call(self, method, params=None, timeout_seconds=None, **kwargs):
        params = params or {}
        codes = list(params.get("codes") or [])
        self.requests.append(codes)
        if len(codes) == 1 and codes[0] in ("SH", "SZ", "BJ", "HK"):
            if self._market_error is not None:
                raise self._market_error
            return dict(self._market_answer)
        raise self._direct_error

    def _redis(self):
        raise AssertionError("redis not expected here")


def _xtdata(client):
    return compat.BigQmtXtData(client)


class RaisesTheRealFailureTest(unittest.TestCase):
    def setUp(self):
        self.codes = _codes(LARGE_CODE_LIST + 1)

    def test_it_is_not_a_bare_reraise(self):
        client = FakeClient(TimeoutError("zmq rpc timeout: get_full_tick"),
                            market_error=TimeoutError("re-read timed out"))

        with self.assertRaises(Exception) as caught:
            _xtdata(client).get_full_tick(self.codes)

        self.assertNotIn("No active exception", str(caught.exception))

    def test_it_keeps_the_original_type(self):
        """Callers catching TimeoutError must keep catching it."""
        client = FakeClient(TimeoutError("zmq rpc timeout: get_full_tick"),
                            market_error=TimeoutError("re-read timed out"))

        with self.assertRaises(TimeoutError):
            _xtdata(client).get_full_tick(self.codes)

    def test_it_keeps_the_original_message(self):
        client = FakeClient(TimeoutError("zmq rpc timeout: get_full_tick"),
                            market_error=TimeoutError("re-read timed out"))

        with self.assertRaises(TimeoutError) as caught:
            _xtdata(client).get_full_tick(self.codes)

        self.assertIn("zmq rpc timeout", str(caught.exception))

    def test_the_re_read_reason_is_logged(self):
        """It is the half a reporter cannot see from the traceback."""
        client = FakeClient(TimeoutError("direct timed out"),
                            market_error=ValueError("re-read exploded"))

        with self.assertLogs(compat.log, level="WARNING") as logs:
            with self.assertRaises(TimeoutError):
                _xtdata(client).get_full_tick(self.codes)

        joined = "\n".join(logs.output)
        self.assertIn("re-read exploded", joined)
        self.assertIn("ValueError", joined)

    def test_a_short_recovery_returns_what_it_found(self):
        """Partial data beats an exception here: the codes that did come back
        are real, and the warning says how many did not."""
        client = FakeClient(TimeoutError("direct timed out"),
                            market_answer={self.codes[0]: {"lastPrice": 1.0}})

        with self.assertLogs(compat.log, level="WARNING") as logs:
            result = _xtdata(client).get_full_tick(self.codes)

        self.assertEqual(len(result), 1)
        self.assertIn("filtered to 1", " ".join(logs.output))


class StillRecoversTest(unittest.TestCase):
    """The happy path must not have been broken by any of this."""

    def test_a_working_re_read_still_returns_data(self):
        codes = _codes(LARGE_CODE_LIST + 1)
        answer = dict((code, {"lastPrice": 1.0}) for code in codes)
        client = FakeClient(TimeoutError("direct timed out"), market_answer=answer)

        result = _xtdata(client).get_full_tick(codes)

        self.assertEqual(len(result), len(codes))

    def test_a_short_list_failure_still_raises_untouched(self):
        client = FakeClient(TimeoutError("direct timed out"))

        with self.assertRaises(TimeoutError):
            _xtdata(client).get_full_tick(_codes(10))


if __name__ == "__main__":
    unittest.main()
