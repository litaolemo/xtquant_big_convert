# coding: utf-8
"""Every object the bridge hands a caller must carry its xttype contract.

#133 established the rule: a field the MiniQMT type declares has to be present
with a sensible default, because the alternative is the caller hitting
AttributeError on a field the real API answers. It fixed order / trade /
position. An audit of the remaining delivery sites against the xttype classes
shipped with the terminal found seven that were still short:

    on_order_error (push)          account_type, account_id
    on_cancel_error (push)         account_type, account_id, market
    query_stock_asset              account_type
    on_order_error (async)         account_type, account_id, strategy_name
    on_order_stock_async_response  account_type
    on_cancel_error (async)        account_type, account_id, market
    on_cancel_order_stock_async..  account_type

Separately, the account family (`query_account_status` / `query_account_infos`
/ `query_credit_detail`) returned plain dicts. MiniQMT's *sync* queries return
the terminal's own objects -- ``xttrader`` builds an xttype object only on the
push path, where ``on_push_AccountStatus`` reads ``m_nStatus`` and converts --
so `.m_nStatus` answered there and raised AttributeError here. The names the
bridge relayed were already right; only the container was wrong, which is why
the rows are now a dict subclass: the subscript access that works today keeps
working.
"""

import json
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import (  # noqa: E402
    BigQmtXtTrader,
    CompatRow,
    _market_of,
)


class _RecordingCallback(object):
    def __init__(self):
        self.order_errors = []
        self.cancel_errors = []
        self.order_responses = []
        self.cancel_responses = []

    def on_order_error(self, err):
        self.order_errors.append(err)

    def on_cancel_error(self, err):
        self.cancel_errors.append(err)

    def on_order_stock_async_response(self, resp):
        self.order_responses.append(resp)

    def on_cancel_order_stock_async_response(self, resp):
        self.cancel_responses.append(resp)


class _FakeClient(object):
    def __init__(self, payloads=None):
        self.account_id = "acct"
        self.payloads = payloads or {}

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        return self.payloads.get(method)


def _trader(callback=None, payloads=None):
    trader = BigQmtXtTrader(account_id="acct")
    trader.client = _FakeClient(payloads)
    if callback is not None:
        trader.callback = callback
    return trader


# The account family arrives with the terminal's own native names.
_STATUS_ROW = {"m_strAccountID": "acct", "m_nAccountType": 2, "m_nStatus": 1}


class CompatRowTest(unittest.TestCase):
    def test_it_answers_attribute_access(self):
        self.assertEqual(CompatRow(_STATUS_ROW).m_nStatus, 1)

    def test_it_is_still_a_dict(self):
        """Callers written against today's behaviour must not break."""
        row = CompatRow(_STATUS_ROW)
        self.assertIsInstance(row, dict)
        self.assertEqual(row["m_nStatus"], 1)
        self.assertEqual(row.get("m_nStatus"), 1)
        self.assertEqual(sorted(row.keys()), sorted(_STATUS_ROW.keys()))

    def test_it_still_json_encodes(self):
        self.assertEqual(json.loads(json.dumps(CompatRow(_STATUS_ROW))), _STATUS_ROW)

    def test_an_absent_field_raises_attribute_error_not_key_error(self):
        with self.assertRaises(AttributeError):
            CompatRow(_STATUS_ROW).m_nNoSuchField


class AccountFamilyShapeTest(unittest.TestCase):
    """query_account_status / _infos / query_credit_detail return rows, not dicts."""

    def _rows(self, method, call):
        trader = _trader(payloads={method: [dict(_STATUS_ROW)]})
        return call(trader)

    def test_account_status_rows_read_by_attribute(self):
        rows = self._rows("query_account_status", lambda t: t.query_account_status())
        self.assertEqual(rows[0].m_nStatus, 1)

    def test_account_infos_rows_read_by_attribute(self):
        rows = self._rows("query_account_infos", lambda t: t.query_account_infos())
        self.assertEqual(rows[0].m_strAccountID, "acct")

    def test_credit_detail_rows_read_by_attribute(self):
        rows = self._rows("query_credit_detail", lambda t: t.query_credit_detail("acct"))
        self.assertEqual(rows[0].m_nAccountType, 2)

    def test_the_subscript_path_still_works(self):
        rows = self._rows("query_account_status", lambda t: t.query_account_status())
        self.assertEqual(rows[0]["m_nStatus"], 1)

    def test_an_empty_answer_is_still_an_empty_list(self):
        trader = _trader(payloads={"query_account_status": []})
        self.assertEqual(trader.query_account_status(), [])

    def test_a_non_dict_row_passes_through_untouched(self):
        trader = _trader(payloads={"query_account_status": ["raw", 7]})
        self.assertEqual(trader.query_account_status(), ["raw", 7])


class MarketOfTest(unittest.TestCase):
    def test_it_reads_the_suffix(self):
        self.assertEqual(_market_of("600000.SH"), 0)
        self.assertEqual(_market_of("000001.SZ"), 1)

    def test_an_unknown_code_is_not_reported_as_shanghai(self):
        """SH_MARKET is 0, so a 0 default would impersonate 上海."""
        self.assertEqual(_market_of(""), -1)
        self.assertEqual(_market_of(None), -1)
        self.assertEqual(_market_of("600000"), -1)


class PushCallbackContractTest(unittest.TestCase):
    def _fire(self, event_type, **extra):
        cb = _RecordingCallback()
        event = {"event_type": event_type, "account_id": "acct",
                 "stock_code": "600000.SH", "error_id": -1, "error_msg": "boom"}
        event.update(extra)
        _trader(cb)._deliver_event(event)
        return cb

    def test_order_error_names_the_account(self):
        err = self._fire("order_error").order_errors[0]
        self.assertEqual(err.account_id, "acct")
        self.assertIsInstance(err.account_type, int)

    def test_cancel_error_names_the_account_and_market(self):
        err = self._fire("cancel_error").cancel_errors[0]
        self.assertEqual(err.account_id, "acct")
        self.assertIsInstance(err.account_type, int)
        self.assertEqual(err.market, 0)

    def test_cancel_error_market_follows_the_code(self):
        err = self._fire("cancel_error", stock_code="000001.SZ").cancel_errors[0]
        self.assertEqual(err.market, 1)


class AsyncCallbackContractTest(unittest.TestCase):
    def _unit(self, kind, **extra):
        unit = {"kind": kind, "seq": 7, "remark": "r-1", "error_id": -1,
                "error_msg": "boom", "stock_code": "600000.SH",
                "order_remark": "tag-1", "user_order_id": "u-1",
                "order_sys_id": "sys-1", "strategy_name": "my_book"}
        unit.update(extra)
        return unit

    def test_async_order_error_names_the_account_and_strategy(self):
        cb = _RecordingCallback()
        _trader(cb)._fire_async_outcome(self._unit("error"))
        err = cb.order_errors[0]
        self.assertEqual(err.account_id, "acct")
        self.assertIsInstance(err.account_type, int)
        self.assertEqual(err.strategy_name, "my_book")

    def test_async_order_response_names_the_account_type(self):
        cb = _RecordingCallback()
        _trader(cb)._fire_async_outcome(self._unit("response"))
        self.assertIsInstance(cb.order_responses[0].account_type, int)

    def test_async_cancel_error_names_the_account_and_market(self):
        cb = _RecordingCallback()
        _trader(cb)._fire_async_outcome(self._unit("cancel_error", order_id="o-1"))
        err = cb.cancel_errors[0]
        self.assertEqual(err.account_id, "acct")
        self.assertIsInstance(err.account_type, int)
        # This path carries no stock_code, so the market is honestly unknown.
        self.assertEqual(err.market, -1)

    def test_async_cancel_response_names_the_account_type(self):
        cb = _RecordingCallback()
        _trader(cb)._fire_async_outcome(
            self._unit("cancel_response", order_id="o-1", success=True))
        self.assertIsInstance(cb.cancel_responses[0].account_type, int)


class AssetContractTest(unittest.TestCase):
    def _asset(self):
        payload = {"cash": 100.0, "total_asset": 300.0,
                   "frozen_cash": 0.0, "market_value": 200.0}
        return _trader(payloads={"query_stock_asset": payload}).query_stock_asset("acct")

    def test_it_carries_account_type(self):
        """xttype.XtAsset declares it; only order/trade/position got it in #133."""
        self.assertIsInstance(self._asset().account_type, int)

    def test_the_native_alias_agrees_with_it(self):
        asset = self._asset()
        self.assertEqual(asset.m_nAccountType, asset.account_type)

    def test_the_existing_fields_are_untouched(self):
        asset = self._asset()
        self.assertEqual(asset.cash, 100.0)
        self.assertEqual(asset.total_asset, 300.0)
        self.assertEqual(asset.m_dTotalAsset, 300.0)
