# coding: utf-8
"""get_history_trade_detail_data: grouped shape and error contract (#395).

Reported by @shengyy with an offline repro. Two defects on v0.3.61:

- The documented answer is ``[(timetag, [deal, ...]), ...]`` (the official
  example iterates ``for time, data in obj_list``), but the handler ran it
  through ``_normalize_detail_rows``, which treated each outer tuple as one
  detail object and answered ``[{}]`` -- grouping and every deal lost.
- ``_call_qmt_global`` degrades "not bound" and any native raise to ``[]``,
  so function-unavailable, native-error and genuine-empty-history all came
  back ``ok=True, data=[]`` -- a caller could not tell "no fills" from "no
  function".

Now: grouped answers keep their timetag and recursively serialized details;
unbound raises "not bound" (RPC ok=False); a native raise propagates; only
a genuine native empty means empty history.
"""
import os
import sys
import unittest
from types import SimpleNamespace


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import (  # noqa: E402
    BigQmtRpcHandlers,
    RedisPubSubRpcService,
)

import bigqmt_signal_trader.redis_rpc as _rpc_module  # noqa: E402

# Lazy: the fix introduces this function; importing it at module level would
# make the pre-fix run a collection error instead of the behavioral failures
# it should exhibit.
_normalize_grouped_detail_rows = getattr(
    _rpc_module, "_normalize_grouped_detail_rows", None)

from test_redis_rpc import (  # noqa: E402  -- the established fakes
    FakeMarketData,
    FakePositionProvider,
    FakeRedis,
)


def _deal(trade_id, code="600000"):
    return SimpleNamespace(
        m_strTradeID=trade_id,
        m_strInstrumentID=code,
        m_strExchangeID="SH",
        m_dPrice=10.5,
        m_nVolume=100,
    )


def _handlers(qmt_api):
    handlers = BigQmtRpcHandlers(
        account_id="acct",
        market_data=FakeMarketData(),
        position_provider=FakePositionProvider(),
        order_gateway=None,
        allow_order_methods=False,
        qmt_api=qmt_api,
    )
    return handlers


def _rpc_call(handlers):
    """Through the service layer, so the ok/error contract is what the
    client actually sees."""
    service = RedisPubSubRpcService(FakeRedis(), handlers, account_id="acct")
    return service.process_request({
        "request_id": "r1", "account_id": "acct",
        "method": "get_history_trade_detail_data",
        "params": {"detail_type": "DEAL",
                   "start_date": "20260101", "end_date": "20260201"},
    })


class GroupedShapeTest(unittest.TestCase):
    def setUp(self):
        if _normalize_grouped_detail_rows is None:
            self.fail("_normalize_grouped_detail_rows is missing -- the "
                      "grouped-shape fix (#395) is not in this build")

    def test_grouped_answer_keeps_grouping_and_deals(self):
        raw = [
            (20260105, [_deal("T1"), _deal("T2", "600519")]),
            (20260106, [_deal("T3")]),
        ]
        out = _normalize_grouped_detail_rows(raw)

        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["timetag"], 20260105)
        self.assertEqual(len(out[0]["details"]), 2)
        self.assertEqual(out[0]["details"][0]["m_strTradeID"], "T1")
        self.assertEqual(out[0]["details"][1]["m_strInstrumentID"], "600519")
        self.assertEqual(out[1]["timetag"], 20260106)
        self.assertEqual(out[1]["details"][0]["m_strTradeID"], "T3")

    def test_flat_answer_still_normalizes(self):
        # A build that answers flat rows keeps working.
        out = _normalize_grouped_detail_rows([_deal("T1"), _deal("T2")])

        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["m_strTradeID"], "T1")

    def test_empty_answer_stays_empty(self):
        self.assertEqual(_normalize_grouped_detail_rows([]), [])
        self.assertEqual(_normalize_grouped_detail_rows(None), [])

    def test_handler_returns_grouped_serialization(self):
        qmt_api = {"get_history_trade_detail_data":
                   lambda *args: [(20260105, [_deal("T1")])]}
        out = _handlers(qmt_api).handle(
            "get_history_trade_detail_data",
            {"detail_type": "DEAL", "start_date": "20260101", "end_date": "20260201"})

        self.assertEqual(out, [{"timetag": 20260105,
                                "details": [_normalize_detail_row_helper()]}])

    @staticmethod
    def _normalize_detail_row_helper():
        from bigqmt_signal_trader.redis_rpc import _normalize_detail_rows
        return _normalize_detail_rows([_deal("T1")])[0]


class ErrorContractTest(unittest.TestCase):
    def test_unbound_function_is_an_error_not_empty_history(self):
        resp = _rpc_call(_handlers({}))

        self.assertFalse(resp["ok"])
        self.assertIn("not bound", resp["error"])

    def test_native_raise_propagates(self):
        def boom(*args):
            raise RuntimeError("synthetic history unavailable")

        resp = _rpc_call(_handlers({"get_history_trade_detail_data": boom}))

        self.assertFalse(resp["ok"])
        self.assertIn("synthetic history unavailable", resp["error"])

    def test_genuine_empty_is_ok_with_empty_data(self):
        resp = _rpc_call(_handlers({"get_history_trade_detail_data": lambda *a: []}))

        self.assertTrue(resp["ok"])
        self.assertEqual(resp["data"], [])


_normalize_detail_row_helper = GroupedShapeTest._normalize_detail_row_helper


if __name__ == "__main__":
    unittest.main()
