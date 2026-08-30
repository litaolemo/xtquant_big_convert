"""Credit orders must reach passorder as credit orders (issue #103).

Two layers were wrong. The RPC rejected anything that was not 23/24, so
order_stock(acc, code, 27, ...) came back as "action or order_type is
required". And had it got past that, submit() maps action to opType, so BUY
would have become 23 -- an ordinary buy placed in place of a margin buy, which
is worse than the rejection: a real order, for the wrong thing.

The values themselves are the trap. MiniQMT's order_type (xtconstant) and
passorder's opType are two numberings that agree on 27-32 and diverge above it:

    meaning              xtconstant    passorder opType (API reference 10.1)
    融资买入 .. 直接还款    27-32         27-32
    担保品买入 / 卖出       --            33 / 34
    专项两融               40-45         70-75

So 40 forwarded unchanged reaches passorder as 期货组合开多. Every value here is
taken from xtconstant by name: PR #88 asserted them as literals and its tests
encoded the same mistake, which is why they passed while the mapping was wrong.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from xtquant import xtconstant
from bigqmt_signal_trader.adapters.order_bigqmt import (
    BigQmtOrderGateway, credit_action_of, credit_optype_of)
from bigqmt_signal_trader.models import OrderRequest
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers


class _Recorder(object):
    def __init__(self):
        self.op_type = None

    def __call__(self, op_type, order_type, account, code, price_type, price,
                 volume, strategy, quick, remark, context=None, *args, **kwargs):
        self.op_type = op_type


def _submit(order_type, action="BUY"):
    recorder = _Recorder()
    gateway = BigQmtOrderGateway(account_id="acct", passorder_func=recorder,
                                 context_info=object())
    gateway.submit(OrderRequest(
        signal_id="s", account_id="acct", stock_code="600000.SH", action=action,
        volume=100, price=10.0, price_type="LIMIT", strategy_name="s",
        order_type=order_type))
    return recorder.op_type


class OpTypeTranslationTest(unittest.TestCase):
    def test_the_plain_family_passes_through(self):
        for name, expected in (("CREDIT_FIN_BUY", 27), ("CREDIT_SLO_SELL", 28),
                               ("CREDIT_BUY_SECU_REPAY", 29),
                               ("CREDIT_DIRECT_SECU_REPAY", 30),
                               ("CREDIT_SELL_SECU_REPAY", 31),
                               ("CREDIT_DIRECT_CASH_REPAY", 32)):
            self.assertEqual(credit_optype_of(getattr(xtconstant, name)),
                             expected, name)

    def test_the_special_family_is_renumbered(self):
        """40 forwarded unchanged would be 期货组合开多 to passorder."""
        for name, expected in (("CREDIT_FIN_BUY_SPECIAL", 70),
                               ("CREDIT_SLO_SELL_SPECIAL", 71),
                               ("CREDIT_BUY_SECU_REPAY_SPECIAL", 72),
                               ("CREDIT_DIRECT_SECU_REPAY_SPECIAL", 73),
                               ("CREDIT_SELL_SECU_REPAY_SPECIAL", 74),
                               ("CREDIT_DIRECT_CASH_REPAY_SPECIAL", 75)):
            self.assertEqual(credit_optype_of(getattr(xtconstant, name)),
                             expected, name)

    def test_the_two_numberings_really_do_differ_there(self):
        """If they ever coincided this translation would look pointless."""
        self.assertNotEqual(xtconstant.CREDIT_FIN_BUY_SPECIAL,
                            credit_optype_of(xtconstant.CREDIT_FIN_BUY_SPECIAL))

    def test_a_plain_stock_order_type_is_not_a_credit_one(self):
        self.assertIsNone(credit_optype_of(xtconstant.STOCK_BUY))
        self.assertIsNone(credit_optype_of(xtconstant.STOCK_SELL))

    def test_junk_is_rejected_not_guessed(self):
        for value in (None, "", "abc", 99, -1, 9999):
            self.assertIsNone(credit_optype_of(value), repr(value))

    def test_every_credit_constant_is_covered(self):
        """A new one appearing in xtconstant should be a visible gap, not a
        silent passthrough."""
        missing = []
        for name in dir(xtconstant):
            if not name.startswith("CREDIT_") or name == "CREDIT_ACCOUNT":
                continue
            value = getattr(xtconstant, name)
            if not isinstance(value, int) or value in (xtconstant.CREDIT_BUY,
                                                       xtconstant.CREDIT_SELL):
                continue
            if credit_optype_of(value) is None:
                missing.append(name)
        self.assertEqual(missing, [])


class SideTest(unittest.TestCase):
    def test_buy_side_operations(self):
        for name in ("CREDIT_FIN_BUY", "CREDIT_BUY_SECU_REPAY",
                     "CREDIT_FIN_BUY_SPECIAL", "CREDIT_BUY_SECU_REPAY_SPECIAL"):
            self.assertEqual(credit_action_of(getattr(xtconstant, name)),
                             "BUY", name)

    def test_sell_side_operations(self):
        for name in ("CREDIT_SLO_SELL", "CREDIT_SELL_SECU_REPAY",
                     "CREDIT_DIRECT_SECU_REPAY", "CREDIT_SLO_SELL_SPECIAL"):
            self.assertEqual(credit_action_of(getattr(xtconstant, name)),
                             "SELL", name)

    def test_cash_repayment_has_no_side(self):
        """直接还款 moves cash, not securities. Guessing a side would be
        inventing a direction the operation does not have."""
        self.assertIsNone(credit_action_of(xtconstant.CREDIT_DIRECT_CASH_REPAY))
        self.assertIsNone(
            credit_action_of(xtconstant.CREDIT_DIRECT_CASH_REPAY_SPECIAL))


class SubmitTest(unittest.TestCase):
    """The layer that used to collapse everything into 23/24."""

    def test_a_margin_buy_is_not_placed_as_an_ordinary_buy(self):
        self.assertEqual(_submit(xtconstant.CREDIT_FIN_BUY), 27)

    def test_a_special_margin_buy_is_renumbered_on_the_way_out(self):
        self.assertEqual(_submit(xtconstant.CREDIT_FIN_BUY_SPECIAL), 70)

    def test_a_short_sell_reaches_passorder_as_one(self):
        self.assertEqual(_submit(xtconstant.CREDIT_SLO_SELL, action="SELL"), 28)

    def test_an_ordinary_order_is_untouched(self):
        self.assertEqual(_submit(None, action="BUY"), 23)
        self.assertEqual(_submit(None, action="SELL"), 24)

    def test_an_unrecognised_order_type_falls_back_to_the_action(self):
        """Not a credit type: behave as before rather than refusing."""
        self.assertEqual(_submit(9999, action="BUY"), 23)


class RpcAcceptanceTest(unittest.TestCase):
    """The layer that rejected these outright."""

    def _handlers(self):
        return BigQmtRpcHandlers.__new__(BigQmtRpcHandlers)

    def test_a_credit_type_is_accepted_and_given_a_side(self):
        handlers = self._handlers()
        for name, expected in (("CREDIT_FIN_BUY", "BUY"),
                               ("CREDIT_SLO_SELL", "SELL"),
                               ("CREDIT_FIN_BUY_SPECIAL", "BUY")):
            action = handlers._order_action_from_params(
                {"order_type": getattr(xtconstant, name)})
            self.assertEqual(action, expected, name)

    def test_cash_repayment_asks_for_an_explicit_action(self):
        handlers = self._handlers()

        with self.assertRaises(ValueError) as caught:
            handlers._order_action_from_params(
                {"order_type": xtconstant.CREDIT_DIRECT_CASH_REPAY})

        self.assertIn("no implicit buy/sell side", str(caught.exception))

    def test_an_explicit_action_still_wins(self):
        handlers = self._handlers()
        action = handlers._order_action_from_params(
            {"order_type": xtconstant.CREDIT_DIRECT_CASH_REPAY, "action": "SELL"})

        self.assertEqual(action, "SELL")

    def test_the_credit_type_is_forwarded_to_the_request(self):
        handlers = self._handlers()
        forwarded = handlers._credit_order_type_from_params(
            {"order_type": xtconstant.CREDIT_FIN_BUY})

        self.assertEqual(credit_optype_of(forwarded), 27)

    def test_a_plain_order_forwards_no_credit_type(self):
        handlers = self._handlers()

        self.assertIsNone(handlers._credit_order_type_from_params(
            {"order_type": xtconstant.STOCK_BUY}))

    def test_an_unknown_type_still_raises(self):
        """It must not be guessed at. The message changed in #92 -- it now
        names the value and the deployed version instead of claiming no
        order_type was given, which is what a reporter who had given one saw.
        Covered in detail by test_unknown_order_type_message.py."""
        handlers = self._handlers()

        with self.assertRaises(ValueError) as caught:
            handlers._order_action_from_params({"order_type": 9999})

        self.assertIn("9999", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
