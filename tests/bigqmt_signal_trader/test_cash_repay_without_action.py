# coding: utf-8
"""归还融资 through the MiniQMT-compatible order_stock (#314).

    xt_trader.order_stock(acc, code, xtconstant.CREDIT_DIRECT_CASH_REPAY,
                          cash_avail, xtconstant.FIX_PRICE, repay_money, ...)
    -> RpcServerRepliedError: ValueError: order_type 32 has no implicit
       buy/sell side; pass action explicitly

#103 made 直接还款 (32 / 45) demand an explicit action because a cash
repayment has no securities leg and guessing a side felt wrong. But the
MiniQMT ``order_stock`` signature has no action parameter, so a caller
using the API this bridge exists to be compatible with could never satisfy
that demand: 归还融资 was unreachable. The same held for 行权 / 锁定 (56-59).

The side was only ever bookkeeping -- passorder gets the raw opType either
way. So sideless types now get a recorded default, and the settlement
lookup stops filtering them by side (the terminal's row may report either).
"""

import os
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from xtquant import xtconstant  # noqa: E402
from bigqmt_signal_trader.adapters.order_bigqmt import (  # noqa: E402
    BigQmtOrderGateway,
    SIDELESS_DEFAULT_ACTION,
    is_sideless_order_type,
)
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers  # noqa: E402

from test_redis_rpc import FakeMarketData, FakePositionProvider  # noqa: E402


class _Passorder(object):
    def __init__(self):
        self.calls = []

    def __call__(self, op_type, combo, account, code, price_type, price,
                 volume, strategy, quick, remark, context=None, *args, **kwargs):
        self.calls.append({"op_type": op_type, "code": code, "price_type": price_type,
                           "price": price, "volume": volume, "remark": remark})


class _Row(object):
    """A native ORDER row: attributes, the way get_trade_detail_data answers."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


class _RowsAfterSubmit(object):
    """The terminal's ORDER list after a submit: the repay row, reported
    with whatever side the terminal chooses -- here the opposite of the
    bridge's bookkeeping default, to prove the lookup no longer cares."""

    def __init__(self, gateway, side):
        self.gateway = gateway
        self.side = side

    def __call__(self, account_id, account_type, kind, strategy_name):
        rows = []
        for call in self.gateway.calls:
            rows.append(_Row(m_strOrderSysID="repay-1", m_strRemark=call["remark"],
                             m_strInstrumentID=call["code"].split(".")[0],
                             m_strExchangeID="SZ",
                             m_nOffsetFlag=48 if self.side == "BUY" else 49,
                             m_nVolumeTotalOriginal=call["volume"], m_nVolumeTraded=0,
                             m_nOrderStatus=50, m_dLimitPrice=call["price"],
                             m_strInsertTime=time.strftime("%H%M%S")))
        return rows


def _handlers(passorder, query=None):
    gateway = BigQmtOrderGateway(account_id="acct", passorder_func=passorder,
                                 get_trade_detail_data_func=query, context_info=object())
    return BigQmtRpcHandlers(
        account_id="acct", market_data=FakeMarketData(),
        position_provider=FakePositionProvider(), order_gateway=gateway,
        allow_order_methods=True, order_settle_timeout_seconds=0.0,
        settle_orders_inline=True)


class SidelessTypesTest(unittest.TestCase):
    def test_which_types_have_no_side(self):
        for value in (xtconstant.CREDIT_DIRECT_CASH_REPAY,
                      xtconstant.CREDIT_DIRECT_CASH_REPAY_SPECIAL, 56, 57, 58, 59):
            self.assertTrue(is_sideless_order_type(value), value)
        for value in (xtconstant.CREDIT_FIN_BUY, xtconstant.CREDIT_SELL_SECU_REPAY,
                      23, 24, 50, 55, None, "", "x"):
            self.assertFalse(is_sideless_order_type(value), value)

    def test_the_default_is_a_real_side(self):
        self.assertIn(SIDELESS_DEFAULT_ACTION, ("BUY", "SELL"))


class CashRepayReachesPassorderTest(unittest.TestCase):
    def test_the_reported_call_places_a_32_with_its_amount(self):
        """The caller's exact shape: volume and price forwarded untouched,
        opType 32, no action anywhere in the request."""
        passorder = _Passorder()
        handlers = _handlers(passorder)

        result = handlers.handle("order_stock", {
            "stock_code": "000001.SZ", "order_type": xtconstant.CREDIT_DIRECT_CASH_REPAY,
            "order_volume": 10000, "price_type": xtconstant.FIX_PRICE, "price": 0,
            "strategy_name": "s", "order_remark": "repay"})

        self.assertEqual(1, len(passorder.calls))
        call = passorder.calls[0]
        self.assertEqual(32, call["op_type"])
        self.assertEqual(10000, call["volume"])
        self.assertEqual(0.0, call["price"])
        self.assertEqual("000001.SZ", call["code"])
        self.assertEqual("repay", result.user_order_id)

    def test_the_special_repay_is_renumbered_like_the_rest_of_its_family(self):
        passorder = _Passorder()
        handlers = _handlers(passorder)
        handlers.handle("order_stock", {
            "stock_code": "000001.SZ",
            "order_type": xtconstant.CREDIT_DIRECT_CASH_REPAY_SPECIAL,
            "order_volume": 5000, "price_type": xtconstant.FIX_PRICE, "price": 0})
        self.assertEqual(75, passorder.calls[0]["op_type"])

    def test_an_explicit_action_still_wins(self):
        handlers = BigQmtRpcHandlers.__new__(BigQmtRpcHandlers)
        self.assertEqual("BUY", handlers._order_action_from_params(
            {"order_type": xtconstant.CREDIT_DIRECT_CASH_REPAY, "action": "BUY"}))

    def test_exercise_and_lock_no_longer_demand_an_action(self):
        handlers = BigQmtRpcHandlers.__new__(BigQmtRpcHandlers)
        for value in (56, 57, 58, 59):
            self.assertIn(handlers._order_action_from_params({"order_type": value}),
                          ("BUY", "SELL"), value)

    def test_sided_types_are_untouched(self):
        handlers = BigQmtRpcHandlers.__new__(BigQmtRpcHandlers)
        self.assertEqual("BUY", handlers._order_action_from_params(
            {"order_type": xtconstant.CREDIT_FIN_BUY}))
        self.assertEqual("SELL", handlers._order_action_from_params(
            {"order_type": xtconstant.CREDIT_SELL_SECU_REPAY}))
        with self.assertRaises(ValueError):
            handlers._order_action_from_params({"order_type": 9999})


class SettlementIgnoresTheSideTest(unittest.TestCase):
    def _settle(self, terminal_side):
        passorder = _Passorder()
        query = _RowsAfterSubmit(passorder, terminal_side)
        handlers = _handlers(passorder, query)
        result = handlers.handle("order_stock", {
            "stock_code": "000001.SZ", "order_type": xtconstant.CREDIT_DIRECT_CASH_REPAY,
            "order_volume": 10000, "price_type": xtconstant.FIX_PRICE, "price": 0,
            "order_remark": "repay-x"})
        return result, handlers._last_server_error

    def test_a_row_reported_on_either_side_settles_the_repay(self):
        """The bridge's bookkeeping default is one side; the terminal may
        report the other. Before this, that mismatch meant 'not found in
        system' after the settle timeout -- for an order that was placed."""
        for side in ("BUY", "SELL"):
            result, server_error = self._settle(side)
            self.assertEqual("repay-1", result.order_sys_id, side)
            self.assertEqual("", server_error, side)

    def test_a_sided_order_still_filters_by_side(self):
        """The #299 guard is untouched for ordinary orders."""
        passorder = _Passorder()
        query = _RowsAfterSubmit(passorder, "SELL")
        handlers = _handlers(passorder, query)
        result = handlers.handle("order_stock", {
            "stock_code": "000001.SZ", "order_type": xtconstant.STOCK_BUY,
            "order_volume": 100, "price_type": xtconstant.FIX_PRICE, "price": 10.0,
            "order_remark": "buy-x"})
        self.assertIsNone(result.order_sys_id)


if __name__ == "__main__":
    unittest.main()


class AmountSlotTest(unittest.TestCase):
    """#330: the caller put the repayment amount in price and a dummy in
    volume. passorder carries 直接还款's amount in the VOLUME slot and
    ignores price -- the error must say so, in terms of the caller's
    parameters, instead of a bare "volume must be positive"."""

    def test_a_zero_volume_names_the_right_slot(self):
        handlers = _handlers(_Passorder())
        with self.assertRaises(ValueError) as caught:
            handlers.handle("order_stock", {
                "stock_code": "600000.SH", "order_type": xtconstant.CREDIT_DIRECT_CASH_REPAY,
                "order_volume": 0, "price_type": xtconstant.FIX_PRICE, "price": 8077.0})
        message = str(caught.exception)
        self.assertIn("order_volume", message)
        self.assertIn("price is ignored", message)
        self.assertIn("8077.0", message)

    def test_a_fractional_cash_balance_as_volume_is_the_same_mistake(self):
        """cash_avail=0.85 -> int -> 0: what the report hit first."""
        handlers = _handlers(_Passorder())
        with self.assertRaises(ValueError) as caught:
            handlers.handle("order_stock", {
                "stock_code": "600000.SH", "order_type": xtconstant.CREDIT_DIRECT_CASH_REPAY,
                "order_volume": 0.85, "price_type": xtconstant.FIX_PRICE, "price": 8077.0})
        self.assertIn("integer yuan", str(caught.exception))

    def test_the_amount_in_volume_goes_through_and_price_is_dropped(self):
        passorder = _Passorder()
        handlers = _handlers(passorder)
        handlers.handle("order_stock", {
            "stock_code": "600000.SH", "order_type": xtconstant.CREDIT_DIRECT_CASH_REPAY,
            "order_volume": 8077, "price_type": xtconstant.FIX_PRICE, "price": 1923.85})
        self.assertEqual(8077, passorder.calls[0]["volume"])

    def test_an_ordinary_order_keeps_the_plain_message(self):
        handlers = _handlers(_Passorder())
        with self.assertRaises(ValueError) as caught:
            handlers.handle("order_stock", {
                "stock_code": "600000.SH", "order_type": xtconstant.STOCK_BUY,
                "order_volume": 0, "price_type": xtconstant.FIX_PRICE, "price": 10.0})
        self.assertEqual("volume must be positive", str(caught.exception))
