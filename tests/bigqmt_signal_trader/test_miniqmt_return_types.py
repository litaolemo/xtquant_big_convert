"""Return types a MiniQMT caller depends on (issue #113).

The bridge's whole promise is that code written against MiniQMT keeps working
against Big QMT. Three returns broke that promise, and each broke it quietly --
no exception, just a value of the wrong kind:

    order_stock          MiniQMT: int (委托编号, or -1)   here: str 合同编号
    cancel_order_stock   MiniQMT: int (0 ok, -1 fail)     here: bool
    XtOrder.order_id     MiniQMT: int                     here: str

The cancel one is the dangerous member of the family. MiniQMT code checks
``if trader.cancel_order_stock(...) == 0``, and ``False == 0`` is True in
Python -- so a *failed* cancel read as success, and a successful one read as
failure. Silent, inverted, on the order path.

Big QMT has no int order id to give: get_trade_detail_data returns
``m_strOrderSysID``, a string. So the id here is both at once -- see OrderId.
"""

import json
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.order_id import OrderId, int_value_of
from bigqmt_signal_trader.xtquant_compat import (
    BigQmtXtTrader, FIX_PRICE, STOCK_BUY, StockAccount)


ACCOUNT = "acct"


class FakeClient(object):
    """Answers the two order RPCs; records what was sent."""

    def __init__(self, order_sys_id="123456", cancel_ok=True):
        self.account_id = ACCOUNT
        self.calls = []
        self.local_cache_config = {}
        self.full_tick_cache_config = {}
        self._order_sys_id = order_sys_id
        self._cancel_ok = cancel_ok

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        self.calls.append((method, dict(params or {})))
        if method == "order_stock":
            return {"order_sys_id": self._order_sys_id, "status": "SUBMITTED"}
        if method == "cancel_order_stock_sysid":
            return {"success": self._cancel_ok}
        return {}

    def _redis(self):
        raise AssertionError("redis not expected here")


def _trader(**kwargs):
    trader = BigQmtXtTrader(account_id=ACCOUNT)
    trader.client = FakeClient(**kwargs)
    return trader


def _order(trader):
    return trader.order_stock(StockAccount(ACCOUNT), "600000.SH", STOCK_BUY,
                              100, FIX_PRICE, 10.0, "s", "")


class OrderStockReturnTest(unittest.TestCase):
    def test_it_is_an_int(self):
        self.assertIsInstance(_order(_trader()), int)

    def test_a_numeric_id_keeps_its_number(self):
        """The usual case: the 合同编号 is digits, so both halves agree."""
        order_id = _order(_trader(order_sys_id="123456"))

        self.assertEqual(order_id, 123456)
        self.assertEqual(str(order_id), "123456")

    def test_comparisons_a_miniqmt_caller_writes(self):
        order_id = _order(_trader())

        self.assertGreater(order_id, 0)
        self.assertNotEqual(order_id, -1)

    def test_the_broker_string_survives(self):
        """An id that is not a number at all still has to reach a cancel."""
        order_id = _order(_trader(order_sys_id="SYS-A1"))

        self.assertEqual(str(order_id), "SYS-A1")
        self.assertIsInstance(order_id, int)
        self.assertGreater(order_id, 0)

    def test_a_failed_submit_is_minus_one_as_an_int(self):
        """The server sends "-1" as a *string*. Returned as one it is truthy
        and never equal to -1, so the rejection read as success."""
        order_id = _order(_trader(order_sys_id="-1"))

        self.assertEqual(order_id, -1)
        self.assertIsInstance(order_id, int)
        self.assertNotIsInstance(order_id, str)

    def test_an_empty_id_is_minus_one_too(self):
        self.assertEqual(_order(_trader(order_sys_id="")), -1)

    def test_it_formats_as_the_broker_id(self):
        """f-strings and .format go through __format__, not __str__."""
        order_id = _order(_trader(order_sys_id="SYS-A1"))

        self.assertEqual("{}".format(order_id), "SYS-A1")
        self.assertEqual("%s" % order_id, "SYS-A1")

    def test_it_survives_json(self):
        order_id = _order(_trader(order_sys_id="123456"))

        self.assertEqual(json.loads(json.dumps({"id": order_id}))["id"], 123456)


class CancelReturnTest(unittest.TestCase):
    """0 on success, -1 on failure -- and never a bool."""

    def test_success_is_zero(self):
        trader = _trader(cancel_ok=True)

        self.assertEqual(trader.cancel_order_stock(StockAccount(ACCOUNT), "x"), 0)

    def test_failure_is_minus_one(self):
        trader = _trader(cancel_ok=False)

        self.assertEqual(trader.cancel_order_stock(StockAccount(ACCOUNT), "x"), -1)

    def test_it_is_not_a_bool(self):
        """False == 0 is True, so a bool return inverts every `== 0` check."""
        ok = _trader(cancel_ok=True).cancel_order_stock(StockAccount(ACCOUNT), "x")
        bad = _trader(cancel_ok=False).cancel_order_stock(StockAccount(ACCOUNT), "x")

        self.assertNotIsInstance(ok, bool)
        self.assertNotIsInstance(bad, bool)

    def test_sysid_variant_matches(self):
        trader = _trader(cancel_ok=True)

        self.assertEqual(
            trader.cancel_order_stock_sysid(StockAccount(ACCOUNT), "SH", "x"), 0)


class CancelSendsTheBrokerIdTest(unittest.TestCase):
    """Whatever int we derived, QMT must be given its own string back."""

    def _sent_sysid(self, trader):
        for method, params in reversed(trader.client.calls):
            if method == "cancel_order_stock_sysid":
                return params["order_sysid"]
        raise AssertionError("no cancel call recorded")

    def test_an_order_id_resolves_to_its_broker_string(self):
        trader = _trader(order_sys_id="SYS-A1")
        order_id = _order(trader)

        trader.cancel_order_stock(StockAccount(ACCOUNT), order_id)

        self.assertEqual(self._sent_sysid(trader), "SYS-A1")

    def test_a_round_tripped_int_still_resolves(self):
        """The caller stored the id in a database and got a plain int back."""
        trader = _trader(order_sys_id="SYS-A1")
        order_id = _order(trader)
        bare_int = int(order_id)

        trader.cancel_order_stock(StockAccount(ACCOUNT), bare_int)

        self.assertEqual(self._sent_sysid(trader), "SYS-A1")

    def test_a_plain_string_is_passed_through(self):
        """Callers who never saw an OrderId keep working."""
        trader = _trader()

        trader.cancel_order_stock(StockAccount(ACCOUNT), "SYS-ELSEWHERE")

        self.assertEqual(self._sent_sysid(trader), "SYS-ELSEWHERE")

    def test_the_memory_is_bounded(self):
        from bigqmt_signal_trader.xtquant_compat import _ORDER_ID_MEMORY

        trader = _trader()
        for index in range(_ORDER_ID_MEMORY + 50):
            trader._order_object_id("SYS-%d" % index)

        self.assertLessEqual(len(trader._order_sys_ids), _ORDER_ID_MEMORY)


class OrderObjectIdTest(unittest.TestCase):
    """XtOrder.order_id / XtTrade.order_id are ints in MiniQMT too."""

    def test_an_order_carries_both_forms(self):
        order = _trader()._order_from_dict(
            ACCOUNT, {"order_sys_id": "SYS-9", "stock_code": "600000.SH"})

        self.assertIsInstance(order.order_id, int)
        self.assertEqual(str(order.order_id), "SYS-9")
        self.assertEqual(order.order_sysid, "SYS-9")   # still the plain string

    def test_a_trade_carries_both_forms(self):
        trade = _trader()._trade_from_dict(
            ACCOUNT, {"order_sys_id": "SYS-9", "stock_code": "600000.SH"})

        self.assertIsInstance(trade.order_id, int)
        self.assertEqual(str(trade.order_id), "SYS-9")

    def test_a_missing_id_does_not_become_a_number(self):
        order = _trader()._order_from_dict(ACCOUNT, {"stock_code": "600000.SH"})

        self.assertEqual(int(order.order_id), 0)


class LifecycleReturnTest(unittest.TestCase):
    """These were already right; pinning them so they stay that way."""

    def test_zero_means_success(self):
        trader = _trader()

        self.assertEqual(trader.start(), 0)
        self.assertEqual(trader.connect(), 0)
        self.assertEqual(trader.subscribe(StockAccount(ACCOUNT)), 0)
        self.assertEqual(trader.unsubscribe(StockAccount(ACCOUNT)), 0)
        trader.stop()


class SurrogateTest(unittest.TestCase):
    def test_it_is_stable(self):
        """Same string, same number, in any process -- a caller comparing ids
        across a restart still matches."""
        self.assertEqual(int_value_of("SYS-A1"), int_value_of("SYS-A1"))

    def test_it_is_positive(self):
        for text in ("SYS-A1", "a", "0", "-5", "000123"):
            self.assertGreater(int_value_of(text), 0, text)

    def test_unicode_digits_are_not_parsed_as_a_number(self):
        """int('１２３') == 123, which is not an id the broker ever issued."""
        self.assertNotEqual(int_value_of(u"１２３"), 123)

    def test_a_leading_zero_id_keeps_its_string(self):
        self.assertEqual(str(OrderId("000123")), "000123")

    def test_an_empty_id_has_no_number(self):
        self.assertEqual(int_value_of(""), 0)


if __name__ == "__main__":
    unittest.main()
