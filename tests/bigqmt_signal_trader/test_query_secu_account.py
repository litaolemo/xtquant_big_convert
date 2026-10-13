# coding: utf-8
"""query_secu_account must exist and answer the asset object (#406).

Reported against 光大金阳光QMT 2.1.29.0 + 0.3.62: the terminal probes the
trader object for query_secu_account on the order-validation path and on
account-session probes (exact AttributeError, three independent call sites:
one order blocked in production, weekend session probes, post-liquidation
periodic retries). The name is not in the public xtquant docs and appears in
no client or bridge source -- it is a runtime attribute probe. The shim now
answers it as an alias of query_stock_asset: the same account-info data an
asset probe asks for.
"""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader  # noqa: E402


class _Client(object):
    account_id = "acct"

    def __init__(self):
        self.calls = []

    def call(self, method, params=None, **kwargs):
        self.calls.append(method)
        if method == "query_stock_asset":
            return {"cash": 12345.0, "frozen_cash": 5.0,
                    "total_asset": 22345.0, "fetch_balance": 100.0}
        raise AssertionError("unexpected rpc: %s" % method)


class QuerySecuAccountTest(unittest.TestCase):
    def _trader(self):
        trader = BigQmtXtTrader.__new__(BigQmtXtTrader)
        trader.client = _Client()
        trader._server_account_type = None
        trader._declared_account_type = None
        trader._account_cache = {}
        return trader

    def test_attribute_exists_and_returns_the_asset_object(self):
        trader = self._trader()

        asset = trader.query_secu_account("acct")

        self.assertEqual(asset.account_id, "acct")
        self.assertEqual(asset.cash, 12345.0)
        self.assertEqual(asset.frozen_cash, 5.0)
        self.assertEqual(asset.total_asset, 22345.0)
        # The m_ aliases ride the same object (native-field access).
        self.assertEqual(asset.m_strAccountID, "acct")
        self.assertEqual(asset.m_dCash, 12345.0)
        # market_value derived: total - cash - frozen
        self.assertEqual(asset.market_value, 22345.0 - 12345.0 - 5.0)
        self.assertIn("query_stock_asset", trader.client.calls)

    def test_async_variant_exists_and_forwards(self):
        trader = self._trader()

        seq = trader.query_secu_account_async("acct")

        self.assertIsNotNone(seq)
        self.assertIn("query_stock_asset", trader.client.calls)


if __name__ == "__main__":
    unittest.main()
