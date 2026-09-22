# coding: utf-8
"""#345: an order the terminal refuses BEFORE creating any record (insufficient
funds -- a dialog on the QMT screen, no callback) must still reach the caller;
#330: 直接还款 leaves no order row, and query_stock_orders must report a credit
order as its credit order_type, not 23/24.

#345, three parts:

* async orders (order_stock_async, wait_settlement=False) keep a SHADOW
  settlement after the reply went out; when the deadline passes with no row,
  the server pushes an order_error the caller's on_order_error receives;
* a client on a transport with no push path (pipe / mysql) cannot receive
  that push, so its async worker waits for the server's settlement instead
  and turns the server_error into on_order_error itself;
* the "not found in system" text names the pre-record refusal.

#330, two parts:

* a cash repayment that never shows in the ORDER list is not a failure:
  no id, no server_error -- order_stock answers -1 without raising;
* the terminal's m_nOpType rides on the order/trade snapshots and the
  client maps it back to the MiniQMT credit order_type.
"""

import json
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from xtquant import xtconstant  # noqa: E402
from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway  # noqa: E402
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway  # noqa: E402
from bigqmt_signal_trader.models import OrderSnapshot  # noqa: E402
from bigqmt_signal_trader.redis_rpc import (  # noqa: E402
    BigQmtRpcHandlers,
    RedisPubSubRpcService,
    to_jsonable,
)
from bigqmt_signal_trader.xtquant_compat import (  # noqa: E402
    BigQmtXtTrader,
    _credit_order_type_from_op,
)

from test_redis_rpc import FakeMarketData, FakePositionProvider, FakeRedis  # noqa: E402


class _EventSink(object):
    """A redis stand-in for the exec-event channels."""

    def __init__(self):
        self.events = []

    def xadd(self, key, fields, maxlen=None, approximate=True):
        self.events.append(("stream", key, json.loads(fields["payload"])))

    def publish(self, channel, raw):
        self.events.append(("pubsub", channel, json.loads(raw)))

    def expire(self, key, seconds):
        pass

    def get(self, key):
        return None

    def order_errors(self):
        return [e for kind, _c, e in self.events if kind == "pubsub" and e.get("event_type") == "order_error"]


class _Gateway(DryRunOrderGateway):
    """Rows appear ``reveal_after`` lookups later, or never."""

    def __init__(self, reveal_after=None):
        super(_Gateway, self).__init__()
        self.reveal_after = reveal_after
        self.lookups = 0

    def query_orders(self, account_id, strategy_name):
        self.lookups += 1
        if self.reveal_after is None or self.lookups < self.reveal_after:
            return []
        return [OrderSnapshot(order_sys_id="sys-1", user_order_id=r.remark, stock_code=r.stock_code,
                              action=r.action, volume=r.volume, traded_volume=0, status="50",
                              price=r.price) for r in self.submitted]


def _service(gateway, timeout=0.0):
    redis_client = FakeRedis()
    handlers = BigQmtRpcHandlers(
        account_id="acct", market_data=FakeMarketData(),
        position_provider=FakePositionProvider(), order_gateway=gateway,
        allow_order_methods=True, order_settle_timeout_seconds=timeout)
    handlers.order_identity_redis_client = _EventSink()
    return redis_client, handlers, RedisPubSubRpcService(redis_client, handlers, account_id="acct")


def _async_order(request_id="a1", remark="async-1"):
    return {"request_id": request_id, "account_id": "acct", "method": "order_stock",
            "params": {"stock_code": "600000.SH", "order_type": 23, "order_volume": 100,
                       "price_type": 11, "price": 10.0, "order_remark": remark,
                       "wait_settlement": False}}


class ShadowSettlementTest(unittest.TestCase):
    def test_the_reply_is_immediate_and_the_order_is_still_watched(self):
        redis_client, handlers, service = _service(_Gateway(reveal_after=None), timeout=5.0)
        service.enqueue_payload(_async_order())

        service.drain_pending()

        self.assertIn("bigqmt:rpc:resp:acct:a1", redis_client.kv, "async reply was held")
        self.assertEqual(0, service.pending_settlement_count(), "a shadow must not count as a held reply")
        self.assertEqual(1, service.shadow_settlement_count())

    def test_a_refusal_before_any_record_becomes_on_order_error(self):
        """The reported case: insufficient funds, dialog on the terminal,
        no order row ever, no callback. Deadline 0 -> refused on this tick."""
        redis_client, handlers, service = _service(_Gateway(reveal_after=None), timeout=0.0)
        service.enqueue_payload(_async_order(remark="broke"))

        service.drain_pending()

        errors = handlers.order_identity_redis_client.order_errors()
        self.assertEqual(1, len(errors), "no order_error pushed")
        event = errors[0]
        self.assertEqual("broke", event["order_remark"])
        self.assertEqual("600000.SH", event["stock_code"])
        self.assertEqual("acct", event["account_id"])
        self.assertIn("not found in system", event["error_msg"])
        self.assertIn("insufficient funds", event["error_msg"])
        self.assertEqual("settlement", event["source"])
        self.assertEqual(0, service.shadow_settlement_count())

    def test_an_order_that_does_show_up_pushes_nothing(self):
        redis_client, handlers, service = _service(_Gateway(reveal_after=1), timeout=5.0)
        service.enqueue_payload(_async_order())

        service.drain_pending()

        self.assertEqual([], handlers.order_identity_redis_client.order_errors())
        self.assertEqual(0, service.shadow_settlement_count())

    def test_every_item_of_a_batch_is_shadowed_and_the_batch_reply_is_not_held(self):
        redis_client, handlers, service = _service(_Gateway(reveal_after=None), timeout=5.0)
        service.enqueue_payload({
            "request_id": "b1", "account_id": "acct", "method": "order_stock_batch",
            "params": {"orders": [
                {"stock_code": "600000.SH", "order_type": 23, "order_volume": 100,
                 "price_type": 11, "price": 10.0, "order_remark": "b-1"},
                {"stock_code": "600519.SH", "order_type": 23, "order_volume": 100,
                 "price_type": 11, "price": 10.0, "order_remark": "b-2"}]}})

        service.drain_pending()

        self.assertIn("bigqmt:rpc:resp:acct:b1", redis_client.kv)
        self.assertEqual(0, service.pending_settlement_count())
        self.assertEqual(2, service.shadow_settlement_count())

    def test_a_synchronous_order_is_unchanged(self):
        """wait_settlement=True still parks the reply, no shadow."""
        redis_client, handlers, service = _service(_Gateway(reveal_after=None), timeout=5.0)
        payload = _async_order()
        del payload["params"]["wait_settlement"]
        service.enqueue_payload(payload)

        service.drain_pending()

        self.assertNotIn("bigqmt:rpc:resp:acct:a1", redis_client.kv)
        self.assertEqual(1, service.pending_settlement_count())
        self.assertEqual(0, service.shadow_settlement_count())

    def test_without_any_sink_the_refusal_is_dropped_not_raised(self):
        redis_client, handlers, service = _service(_Gateway(reveal_after=None), timeout=0.0)
        handlers.order_identity_redis_client = None
        service.enqueue_payload(_async_order())
        service.drain_pending()          # must not raise
        self.assertEqual(0, service.shadow_settlement_count())


class NotFoundMessageTest(unittest.TestCase):
    def test_it_names_the_pre_record_refusal(self):
        redis_client, handlers, service = _service(_Gateway(reveal_after=None), timeout=0.0)
        payload = _async_order()
        del payload["params"]["wait_settlement"]
        service.enqueue_payload(payload)
        service.drain_pending()
        reply = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:a1"])
        self.assertIn("BEFORE creating a record", reply["server_error"])
        self.assertIn("insufficient funds", reply["server_error"])
        self.assertIn("运行模式", reply["server_error"])


class CashRepayNoRowTest(unittest.TestCase):
    def _repay(self, wait=True):
        redis_client, handlers, service = _service(_Gateway(reveal_after=None), timeout=0.0)
        payload = {"request_id": "r1", "account_id": "acct", "method": "order_stock",
                   "params": {"stock_code": "600000.SH", "order_type": xtconstant.CREDIT_DIRECT_CASH_REPAY,
                              "order_volume": 100, "price_type": 11, "price": 0,
                              "order_remark": "repay-100"}}
        if not wait:
            payload["params"]["wait_settlement"] = False
        service.enqueue_payload(payload)
        service.drain_pending()
        return redis_client, handlers, service

    def test_a_repay_with_no_order_row_is_not_an_error(self):
        """The partial repay from the report: 100 of 1000 repaid, no row
        with the remark ever appears. No id, no server_error."""
        redis_client, handlers, service = self._repay()
        reply = json.loads(redis_client.kv["bigqmt:rpc:resp:acct:r1"])
        self.assertTrue(reply["ok"])
        self.assertEqual("", reply["server_error"])
        self.assertIsNone(reply["data"]["order_sys_id"])
        self.assertIn("query_credit_detail", reply["data"]["message"])

    def test_the_compat_layer_answers_minus_one_without_raising(self):
        trader = BigQmtXtTrader(account_id="acct")

        class Client(object):
            account_id = "acct"

            def call_tracked(self_, method, params=None, account_id=None, request_id=None, timeout_seconds=None):
                return {"order_sys_id": None, "user_order_id": "repay-100",
                        "message": "直接还款 submitted; ... verify with query_credit_detail"}

        trader.client = Client()
        self.assertEqual(-1, trader.order_stock("acct", "600000.SH", xtconstant.CREDIT_DIRECT_CASH_REPAY,
                                                100, xtconstant.FIX_PRICE, 0, "s", "repay-100"))

    def test_an_async_repay_pushes_no_order_error(self):
        redis_client, handlers, service = self._repay(wait=False)
        self.assertEqual([], handlers.order_identity_redis_client.order_errors())

    def test_an_ordinary_order_with_no_row_still_errors(self):
        redis_client, handlers, service = _service(_Gateway(reveal_after=None), timeout=0.0)
        payload = _async_order()
        del payload["params"]["wait_settlement"]
        service.enqueue_payload(payload)
        service.drain_pending()
        self.assertIn("not found in system", json.loads(redis_client.kv["bigqmt:rpc:resp:acct:a1"])["server_error"])


class PushlessClientTest(unittest.TestCase):
    def _trader(self, transport):
        trader = BigQmtXtTrader(account_id="acct")

        class Client(object):
            account_id = "acct"
            transport_name = transport

        trader.client = Client()
        return trader

    def test_which_transports_have_a_push_path(self):
        for name, expected in (("redis", True), ("", True), ("zmq", True),
                               ("pipe", False), ("mysql", False), ("shm", False)):
            self.assertEqual(expected, self._trader(name)._has_push_channel(), name)

    def test_an_async_order_waits_for_the_settlement_when_nothing_can_push(self):
        seen = {}
        trader = self._trader("pipe")
        trader.order_stock_result = lambda *a, **kw: seen.setdefault("wait", kw.get("wait_settlement")) or {"order_sys_id": "1"}
        trader._enqueue_async_outcome = lambda unit: None
        trader._submit_async_single(1, ("acct", "600000.SH", 23, 100, 11, 10.0, "s", "r"), {})
        self.assertTrue(seen["wait"])

    def test_an_async_order_on_redis_keeps_the_fast_path(self):
        seen = {}
        trader = self._trader("redis")
        trader.order_stock_result = lambda *a, **kw: seen.setdefault("wait", kw.get("wait_settlement")) or {"order_sys_id": "1"}
        trader._enqueue_async_outcome = lambda unit: None
        trader._submit_async_single(1, ("acct", "600000.SH", 23, 100, 11, 10.0, "s", "r"), {})
        self.assertFalse(seen["wait"])

    def test_a_server_error_on_the_waiting_path_becomes_an_error_outcome(self):
        from bigqmt_signal_trader.xtquant_compat import RpcServerRepliedError
        units = []
        trader = self._trader("pipe")

        def boom(*a, **kw):
            raise RpcServerRepliedError("Big QMT order_stock server_error: passorder submitted but order not found in system")

        trader.order_stock_result = boom
        trader._enqueue_async_outcome = units.append
        trader._submit_async_single(7, ("acct", "600000.SH", 23, 100, 11, 10.0, "s", "r"), {})
        self.assertEqual("error", units[0]["kind"])
        self.assertIn("not found in system", units[0]["error_msg"])


class _Row(object):
    def __init__(self, **fields):
        self.__dict__.update(fields)


class CreditOrderTypeTest(unittest.TestCase):
    def test_the_map(self):
        self.assertEqual(27, _credit_order_type_from_op(27, 23))
        self.assertEqual(31, _credit_order_type_from_op(31, 24))
        self.assertEqual(xtconstant.CREDIT_BUY, _credit_order_type_from_op(33, 23))
        self.assertEqual(xtconstant.CREDIT_SELL, _credit_order_type_from_op(34, 24))
        self.assertEqual(xtconstant.CREDIT_FIN_BUY_SPECIAL, _credit_order_type_from_op(70, 23))
        self.assertEqual(xtconstant.CREDIT_DIRECT_CASH_REPAY_SPECIAL, _credit_order_type_from_op(75, 24))
        self.assertEqual(80, _credit_order_type_from_op(80, 24))
        # not a credit opType: the side-based answer stands
        self.assertEqual(23, _credit_order_type_from_op(23, 23))
        self.assertEqual(24, _credit_order_type_from_op(None, 24))
        self.assertEqual(24, _credit_order_type_from_op("x", 24))

    def test_the_gateway_carries_the_terminals_op_type(self):
        rows = [_Row(m_strOrderSysID="1", m_strRemark="r", m_strInstrumentID="600000",
                     m_strExchangeID="SH", m_nOffsetFlag=48, m_nOpType=27,
                     m_nVolumeTotalOriginal=100, m_nVolumeTraded=0, m_nOrderStatus=50,
                     m_dLimitPrice=10.0)]
        gateway = BigQmtOrderGateway(account_id="acct", passorder_func=None, context_info=object(),
                                     get_trade_detail_data_func=lambda *a: rows)
        snap = gateway.query_orders("acct", "")[0]
        self.assertEqual(27, snap.op_type)
        self.assertEqual("BUY", snap.action)
        self.assertEqual(27, to_jsonable(snap)["op_type"])

    def test_query_stock_orders_reports_a_margin_buy_as_credit_fin_buy(self):
        trader = BigQmtXtTrader(account_id="acct")

        class Client(object):
            account_id = "acct"

            def call(self_, method, params=None, account_id=None, timeout_seconds=None):
                return [{"order_sys_id": "1", "stock_code": "600000.SH", "action": "BUY",
                         "volume": 100, "traded_volume": 0, "status": 50, "price": 10.0,
                         "op_type": 27},
                        {"order_sys_id": "2", "stock_code": "600000.SH", "action": "BUY",
                         "volume": 100, "traded_volume": 0, "status": 50, "price": 10.0}]

        trader.client = Client()
        orders = trader.query_stock_orders("acct")
        self.assertEqual(xtconstant.CREDIT_FIN_BUY, orders[0].order_type)
        self.assertEqual(xtconstant.STOCK_BUY, orders[1].order_type, "no op_type: side-based as before")

    def test_a_trade_row_maps_too(self):
        trader = BigQmtXtTrader(account_id="acct")

        class Client(object):
            account_id = "acct"

            def call(self_, method, params=None, account_id=None, timeout_seconds=None):
                return [{"order_sys_id": "1", "trade_id": "t1", "stock_code": "600000.SH",
                         "action": "SELL", "volume": 100, "price": 10.0, "op_type": 31}]

        trader.client = Client()
        self.assertEqual(xtconstant.CREDIT_SELL_SECU_REPAY, trader.query_stock_trades("acct")[0].order_type)


if __name__ == "__main__":
    unittest.main()
