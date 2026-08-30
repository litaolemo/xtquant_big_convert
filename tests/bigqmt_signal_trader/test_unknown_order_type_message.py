"""An unrecognised order_type must say so, not claim none was given (#92).

A reporter passed order_type=27 and got back "action or order_type is
required". They had passed one, so the message sent them looking at their own
call. The real cause was that the package deployed *inside QMT* predated the
credit order types -- and a client-side ``pip install --upgrade`` cannot fix
that, because this code runs in QMT, not in the caller's process.

Nothing about the old message pointed there. It could not: it did not
distinguish "no order_type given" from "this order_type is not one I know".
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from xtquant import xtconstant
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers


def _handlers():
    return BigQmtRpcHandlers.__new__(BigQmtRpcHandlers)


def _error(params):
    try:
        _handlers()._order_action_from_params(params)
    except ValueError as exc:
        return str(exc)
    raise AssertionError("expected a ValueError for %r" % (params,))


class MissingTest(unittest.TestCase):
    """Nothing supplied: the original message is still the right one."""

    def test_no_parameters(self):
        self.assertEqual(_error({}), "action or order_type is required")

    def test_an_empty_order_type(self):
        self.assertEqual(_error({"order_type": ""}),
                         "action or order_type is required")

    def test_an_explicit_none(self):
        self.assertEqual(_error({"order_type": None}),
                         "action or order_type is required")


class UnrecognisedTest(unittest.TestCase):
    """Supplied but unknown: say that, and say where to look."""

    def test_it_does_not_claim_one_was_required(self):
        message = _error({"order_type": 9999})

        self.assertNotIn("is required", message)

    def test_it_quotes_the_value_it_rejected(self):
        self.assertIn("9999", _error({"order_type": 9999}))

    def test_it_names_the_deployed_version(self):
        """Which build rejected it is the whole question."""
        from bigqmt_signal_trader.version import __version__

        self.assertIn(__version__, _error({"order_type": 9999}))

    def test_it_says_a_pip_upgrade_will_not_help(self):
        """The trap: the fix is a file copy into QMT, not a client upgrade."""
        message = _error({"order_type": 9999})

        self.assertIn("pip", message)
        self.assertIn("sync_deployment", message)

    def test_it_is_ascii_only(self):
        """QMT's log writer drops non-ASCII -- a Chinese install path came
        back mangled, and a mangled error message helps nobody."""
        message = _error({"order_type": 9999})

        self.assertTrue(all(ord(char) < 128 for char in message), message)

    def test_the_version_lookup_never_raises(self):
        """This only runs while building an error message; failing there would
        replace a useful error with a confusing one."""
        self.assertIsInstance(BigQmtRpcHandlers._deployed_version(), str)


class StillWorksTest(unittest.TestCase):
    """The recognised paths are untouched."""

    def test_a_credit_type_still_resolves(self):
        handlers = _handlers()

        self.assertEqual(
            handlers._order_action_from_params(
                {"order_type": xtconstant.CREDIT_FIN_BUY}), "BUY")

    def test_cash_repayment_still_asks_for_an_action(self):
        message = _error({"order_type": xtconstant.CREDIT_DIRECT_CASH_REPAY})

        self.assertIn("no implicit buy/sell side", message)

    def test_an_explicit_action_still_wins(self):
        handlers = _handlers()

        self.assertEqual(
            handlers._order_action_from_params(
                {"order_type": 9999, "action": "SELL"}), "SELL")


if __name__ == "__main__":
    unittest.main()
