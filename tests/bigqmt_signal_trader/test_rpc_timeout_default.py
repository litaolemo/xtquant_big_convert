"""The client's default RPC timeout has to clear an ordinary QMT call.

Measured against a live bridge: query_orders 1.5s, get_asset 1.4s,
get_financial_data 0.8s warm, a whole-market get_full_tick 7.7s. The default
was 6s, under the cost of several of those.

Timing out here is worse than waiting. The request is already at the bridge and
the bridge keeps working on it; the client just stops listening. The next call
then queues behind work nobody is going to read, so one timeout breeds more --
in the session that found this, three calls in a row took over 45s each while
the bridge worked through requests an earlier probe had abandoned after 6s.

Nothing is silently mis-read when that happens: the transport matches responses
by request_id, so a late one is discarded rather than handed to the wrong
caller. It only costs time.
"""

import io
import os
import re
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import (
    BigQmtRpcClient, DEFAULT_RPC_TIMEOUT_SECONDS)


# Slowest call observed on a live bridge, whole-market snapshots aside.
SLOWEST_ORDINARY_CALL_SECONDS = 7.7


class DefaultValueTest(unittest.TestCase):
    def test_it_clears_the_slowest_ordinary_call(self):
        self.assertGreater(DEFAULT_RPC_TIMEOUT_SECONDS,
                           SLOWEST_ORDINARY_CALL_SECONDS)

    def test_it_still_fails_in_a_human_timeframe(self):
        """A dead bridge must not look like a hung program."""
        self.assertLessEqual(DEFAULT_RPC_TIMEOUT_SECONDS, 60.0)

    def test_a_client_with_no_config_uses_it(self):
        client = BigQmtRpcClient(account_id="acct", redis_config={"transport": "zmq"})

        self.assertEqual(client.timeout_seconds, DEFAULT_RPC_TIMEOUT_SECONDS)


class OverrideTest(unittest.TestCase):
    """Raising the default must not take the knobs away."""

    def test_an_explicit_argument_wins(self):
        client = BigQmtRpcClient(account_id="acct", timeout_seconds=3.0,
                                 redis_config={"transport": "zmq"})

        self.assertEqual(client.timeout_seconds, 3.0)

    def test_the_environment_variable_wins_over_the_default(self):
        saved = os.environ.get("BIGQMT_RPC_TIMEOUT_SECONDS")
        try:
            os.environ["BIGQMT_RPC_TIMEOUT_SECONDS"] = "12.5"
            client = BigQmtRpcClient(account_id="acct",
                                     redis_config={"transport": "zmq"})
            self.assertEqual(client.timeout_seconds, 12.5)
        finally:
            if saved is None:
                os.environ.pop("BIGQMT_RPC_TIMEOUT_SECONDS", None)
            else:
                os.environ["BIGQMT_RPC_TIMEOUT_SECONDS"] = saved


class TemplatesTest(unittest.TestCase):
    """A template shipping the old value would hand it straight back."""

    def _value_in(self, relative):
        path = os.path.join(ROOT, "src", relative)
        with io.open(path, encoding="utf-8") as handle:
            match = re.search(r"BIGQMT_RPC_TIMEOUT_SECONDS = ([0-9.]+)",
                              handle.read())
        self.assertIsNotNone(match, relative)
        return float(match.group(1))

    def test_the_example_client_config_agrees(self):
        self.assertEqual(self._value_in("bigqmt_signal_trader_client_config.example.py"),
                         DEFAULT_RPC_TIMEOUT_SECONDS)

    def test_the_generated_config_agrees(self):
        """bigqmt-init writes this file for every new user."""
        self.assertEqual(self._value_in("bigqmt_signal_trader/init_config.py"),
                         DEFAULT_RPC_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
