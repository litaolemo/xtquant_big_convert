# coding: utf-8
"""The callback-fed order watch table (issue #164).

Measured why: 135 get_trade_detail_data rounds in 3.6s to settle one cancel
of a nonexistent order (#151), 62/64 rounds for one submit (#122) -- all on
the adjust thread. QMT's order_callback was pushing the remark, the contract
id and the status the whole time. Settlement now consults the table first
and only falls back to the snapshot poll on a miss.
"""

import json
import os
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.order_watch import OrderWatchTable
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers, RedisPubSubRpcService

from test_redis_rpc import (  # noqa: E402
    FakeMarketData,
    FakePositionProvider,
    FakeRedis,
    FalseyCancelGateway,
)
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway


class CountingGateway(DryRunOrderGateway):
    def __init__(self, statuses=None):
        super().__init__()
        self.statuses = list(statuses or [])
        self.queries = 0

    def query_orders(self, account_id, strategy_name):
        self.queries += 1
        return []


class CallbackOnSubmitGateway(CountingGateway):
    """passorder returns, then QMT's order_callback lands -- #164's real order.

    The settlement lookup runs on the tick after the submit, so the table
    entry it finds was always written after passorder. A test that noted the
    event before submitting modelled an impossible sequence, and #299's
    not-before guard rightly rejects an entry older than the submit.
    """

    def __init__(self, table, event):
        super().__init__()
        self.table = table
        self.event = event

    def submit(self, request):
        result = super().submit(request)
        self.table.note(self.event)
        return result


class TableSemanticsTest(unittest.TestCase):
    def test_learns_both_directions(self):
        table = OrderWatchTable()
        table.note({"user_order_id": "sig-1", "order_sys_id": "635076953",
                    "status": "50"})
        self.assertEqual(table.sysid_for_remark("sig-1"), "635076953")
        self.assertEqual(table.status_for_sysid("635076953"), "50")

    def test_a_presysid_event_teaches_nothing(self):
        table = OrderWatchTable()
        table.note({"user_order_id": "sig-1", "order_sys_id": "", "status": "50"})
        self.assertIsNone(table.sysid_for_remark("sig-1"))

    def test_expired_entries_read_as_missing(self):
        table = OrderWatchTable()
        table.note({"user_order_id": "sig-1", "order_sys_id": "S1", "status": "50"})
        with table._lock:
            table._by_remark["sig-1"] = (time.time() - 90000, "S1")
            table._status_by_sysid["S1"] = (time.time() - 90000, "50")
        self.assertIsNone(table.sysid_for_remark("sig-1"))
        self.assertIsNone(table.status_for_sysid("S1"))

    def test_bounded(self):
        table = OrderWatchTable()
        for i in range(OrderWatchTable.MAX_ENTRIES + 100):
            table.note({"user_order_id": "r%d" % i,
                        "order_sys_id": "s%d" % i, "status": "50"})
        self.assertEqual(len(table._by_remark), OrderWatchTable.MAX_ENTRIES)
        self.assertEqual(len(table._status_by_sysid), OrderWatchTable.MAX_ENTRIES)

    def test_never_raises_on_garbage(self):
        table = OrderWatchTable()
        table.note(None)
        table.note({"user_order_id": object(), "order_sys_id": object()})


class _Service(unittest.TestCase):
    def _service(self, gateway, timeout=5.0):
        redis_client = FakeRedis()
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True, order_settle_timeout_seconds=timeout)
        return redis_client, RedisPubSubRpcService(
            redis_client, handlers, account_id="acct"), handlers

    def _response(self, redis_client, request_id):
        key = "bigqmt:rpc:resp:acct:%s" % request_id
        return json.loads(redis_client.kv[key]) if key in redis_client.kv else None


class SubmitFastPathTest(_Service):
    def test_settles_from_the_table_without_polling(self):
        table = OrderWatchTable()
        gateway = CallbackOnSubmitGateway(table, {
            "user_order_id": "ord-1", "order_sys_id": "635076953", "status": "50",
            "stock_code": "601398.SH", "action": "BUY"})
        redis_client, service, handlers = self._service(gateway)
        handlers.order_watch_table = table

        service.enqueue_payload({
            "request_id": "ord-1", "account_id": "acct", "method": "order_stock",
            "params": {"stock_code": "601398.SH", "order_type": 23,
                       "order_volume": 100, "price_type": 11, "price": 7.9,
                       "order_remark": "ord-1"}})
        service.drain_pending()

        response = self._response(redis_client, "ord-1")
        self.assertIsNotNone(response)
        self.assertEqual(response["data"]["order_sys_id"], "635076953")
        self.assertEqual(gateway.queries, 0, "the poll must not run at all")

    def test_a_table_miss_falls_back_to_polling(self):
        gateway = CountingGateway()
        redis_client, service, handlers = self._service(gateway, timeout=0.0)
        handlers.order_watch_table = OrderWatchTable()  # empty

        service.enqueue_payload({
            "request_id": "ord-2", "account_id": "acct", "method": "order_stock",
            "params": {"stock_code": "601398.SH", "order_type": 23,
                       "order_volume": 100, "price_type": 11, "price": 7.9,
                       "order_remark": "ord-2"}})
        service.drain_pending()

        self.assertGreaterEqual(gateway.queries, 1)


class CancelFastPathTest(_Service):
    def _cancel(self, service, sysid="cancel-1"):
        service.enqueue_payload({
            "request_id": "c-1", "account_id": "acct",
            "method": "cancel_order_stock_sysid",
            "params": {"order_sysid": sysid}})

    def test_cancel_resolves_from_the_table_without_polling(self):
        gateway = FalseyCancelGateway([], native_success=True)
        gateway.queries = 0
        orig_query = gateway.query_orders
        gateway.query_orders = lambda *a: (setattr(gateway, "queries", gateway.queries + 1),
                                           orig_query(*a))[1]
        redis_client, service, handlers = self._service(gateway)
        handlers.order_watch_table = OrderWatchTable()
        handlers.order_watch_table.note({
            "user_order_id": "x", "order_sys_id": "cancel-1", "status": "54"})

        self._cancel(service)
        service.drain_pending()

        response = self._response(redis_client, "c-1")
        self.assertTrue(response["data"]["success"])
        self.assertEqual(gateway.queries, 0)

    def test_cancel_falls_back_when_the_table_does_not_know(self):
        gateway = FalseyCancelGateway(["54"], native_success=True)
        redis_client, service, handlers = self._service(gateway)
        handlers.order_watch_table = OrderWatchTable()  # empty

        self._cancel(service)
        service.drain_pending()

        response = self._response(redis_client, "c-1")
        self.assertTrue(response["data"]["success"])
        self.assertGreaterEqual(gateway.lookups, 1)

    def test_terminal_status_from_the_table_reports_failure(self):
        gateway = FalseyCancelGateway([], native_success=True)
        redis_client, service, handlers = self._service(gateway)
        handlers.order_watch_table = OrderWatchTable()
        handlers.order_watch_table.note({
            "user_order_id": "x", "order_sys_id": "cancel-1", "status": "56"})

        self._cancel(service)
        service.drain_pending()

        response = self._response(redis_client, "c-1")
        self.assertFalse(response["data"]["success"])
        self.assertIn("reached status 56", response["data"]["message"])


class StrategyWiringTest(unittest.TestCase):
    def test_order_callback_notes_into_the_table(self):
        import bigqmt_signal_trader_strategy as strategy

        strategy._held_presysid_orders.clear()  # unrelated state, keep clean
        obj = type("FakeOrder", (), dict(
            m_strInstrumentID="601398", m_strExchangeID="SH", m_nOffsetFlag=48,
            m_nVolumeTotalOriginal=100, m_nVolumeTotal=100, m_nVolumeTraded=0,
            m_strOrderSysID="635076953", m_strRemark="sig-9",
            m_nOrderStatus=50, m_dLimitPrice=7.9, m_dTradedPrice=0.0,
            m_strAccountID="acct",
        ))
        strategy._note_order_watch(obj)
        table = strategy._order_watch_table
        try:
            self.assertEqual(table.sysid_for_remark("sig-9"), "635076953")
            self.assertEqual(table.status_for_sysid("635076953"), "50")
        finally:
            with table._lock:
                table._by_remark.pop("sig-9", None)
                table._status_by_sysid.pop("635076953", None)


if __name__ == "__main__":
    unittest.main()
