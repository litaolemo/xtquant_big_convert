# coding: utf-8
"""A POSITION row missing a quantity is an error, never a fabricated 0 (#290).

@shengyy's offline repro on v0.3.38: a native POSITION row that lacked
``m_nVolume``, ``m_nCanUseVolume`` or ``m_nYesterdayVolume`` came out of
``BigQmtPositionProvider.get_positions()`` with that count as 0, and its
serialised form was byte-identical to a row where the terminal had said 0.
The row's "unknown" was lost in the adapter, so even the public raw RPC
could not recover it.

The three are unconditional ``int`` members of big QMT's POSITION struct
(docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md lists them with no 股票不适用
caveat, unlike the futures-only fields around them), and #81 cross-checked
them on six live stock positions. A row without one is malformed by the
terminal's own contract. Reporting it as 0 is the worst answer: a strategy
that reads 0 as "flat" buys again, one that reads 0 as "nothing sellable"
never sells.

So the adapter refuses: ``ValueError`` naming the code, the account and the
missing field. That reaches the caller through the same path #229/#230 gave
a failed native query -- ``ok=False, error=...`` on the RPC envelope, and an
exception from the compat client -- never as data. The three inputs the
report asked to be told apart:

* complete row               -> the counts, unchanged
* each field missing in turn -> error, naming that field
* native explicit 0          -> 0, still a normal answer

An empty native result is still an empty result.
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.position_bigqmt import BigQmtPositionProvider  # noqa: E402
from bigqmt_signal_trader.redis_rpc import (  # noqa: E402
    BigQmtRpcHandlers,
    RedisPubSubRpcService,
    to_jsonable,
)

from test_redis_rpc import FakeMarketData, FakeRedis  # noqa: E402  -- established fakes

BASE = {
    "m_strInstrumentID": "600000", "m_strExchangeID": "SH",
    "m_nVolume": 100, "m_nCanUseVolume": 100, "m_nYesterdayVolume": 100,
    "m_dOpenPrice": 10.0, "m_dLastPrice": 10.0,
}
FIELDS = {
    "volume": "m_nVolume",
    "available": "m_nCanUseVolume",
    "yesterday_volume": "m_nYesterdayVolume",
}


def _provider(*natives):
    rows = [SimpleNamespace(**n) for n in natives]
    return BigQmtPositionProvider(lambda *_: rows, account_type="STOCK")


def _snapshot(native):
    return to_jsonable(_provider(native).get_positions("acct"))["600000.SH"]


class AdapterContractTest(unittest.TestCase):
    def test_a_complete_row_reports_its_counts(self):
        row = _snapshot(BASE)

        self.assertEqual({"volume": 100, "available": 100, "yesterday_volume": 100},
                         {k: row[k] for k in FIELDS})

    def test_each_missing_count_is_an_error_naming_the_field(self):
        for field, native_name in FIELDS.items():
            missing = dict(BASE)
            del missing[native_name]

            with self.assertRaises(ValueError) as caught:
                _provider(missing).get_positions("acct")

            message = str(caught.exception)
            self.assertIn("600000.SH", message)
            self.assertIn("acct", message)
            self.assertIn(field, message, "error must say which count is missing")
            self.assertIn(native_name, message)

    def test_a_native_zero_is_still_a_zero(self):
        for field, native_name in FIELDS.items():
            row = _snapshot(dict(BASE, **{native_name: 0}))

            self.assertEqual(0, row[field], field)

    def test_missing_and_native_zero_no_longer_serialise_alike(self):
        """The report's exact assertion, inverted."""
        for field, native_name in FIELDS.items():
            missing = dict(BASE)
            del missing[native_name]
            explicit_zero = _snapshot(dict(BASE, **{native_name: 0}))

            with self.assertRaises(ValueError):
                _snapshot(missing)
            self.assertEqual(0, explicit_zero[field])

    def test_an_empty_native_result_is_still_empty(self):
        self.assertEqual({}, _provider().get_positions("acct"))

    def test_a_none_attribute_counts_as_missing(self):
        """``_attr`` skips None, so a present-but-None attribute is the same
        unknown as an absent one."""
        with self.assertRaises(ValueError):
            _provider(dict(BASE, m_nVolume=None)).get_positions("acct")


class RpcEnvelopeTest(unittest.TestCase):
    """The refusal must cross the wire as the #229/#230 error shape."""

    def _service(self, *natives):
        redis_client = FakeRedis()
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=_provider(*natives))
        service = RedisPubSubRpcService(redis_client, handlers, account_id="acct")
        return redis_client, service

    def _ask(self, service, redis_client, method, params=None):
        service.enqueue_payload({"request_id": "req-1", "account_id": "acct",
                                 "method": method, "params": params or {}})
        service.drain_pending()
        return json.loads(redis_client.kv["bigqmt:rpc:resp:acct:req-1"])

    def test_get_positions_answers_ok_false_with_the_reason(self):
        missing = dict(BASE)
        del missing["m_nVolume"]
        redis_client, service = self._service(missing)

        response = self._ask(service, redis_client, "get_positions")

        self.assertFalse(response["ok"])
        self.assertIsNone(response.get("data"))
        self.assertIn("carries no volume", response["error"])
        self.assertIn("600000.SH", response["error"])

    def test_query_stock_position_for_one_code_fails_the_same_way(self):
        missing = dict(BASE)
        del missing["m_nCanUseVolume"]
        redis_client, service = self._service(missing)

        response = self._ask(service, redis_client, "query_stock_position",
                             {"stock_code": "600000.SH"})

        self.assertFalse(response["ok"])
        self.assertIn("carries no available", response["error"])

    def test_a_complete_row_still_answers_ok_true(self):
        redis_client, service = self._service(BASE)

        response = self._ask(service, redis_client, "get_positions")

        self.assertTrue(response["ok"])
        self.assertEqual(100, response["data"]["600000.SH"]["volume"])


if __name__ == "__main__":
    unittest.main()
