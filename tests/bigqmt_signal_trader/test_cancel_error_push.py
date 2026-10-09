# coding: utf-8
"""A settle-confirmed failed cancel pushes a cancel_error event (#389).

Reported by @tokens-lin: they cancelled an order that had already filled;
QMT's own log shows 当前委托状态[已成]不可撤, but nothing reached the
strategy -- no native callback exists for a refused cancel, and the bridge
published none either, so the strategy kept re-cancelling a dead order.

The channel was already wired end to end (client subscribes and dispatches
on_cancel_error; exec_events normalizes and publishes) -- nothing fed it.
The bridge's cancel settle path is the one place that concludes the
failure, so it now synthesizes the event there and the strategy publishes
it through the exec hook, same sink and fallback as native order events.
"""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.models import CancelResult, OrderRef  # noqa: E402
from bigqmt_signal_trader.redis_rpc import (  # noqa: E402
    BigQmtRpcHandlers,
    CancelSettlement,
)

from test_redis_rpc import FakeMarketData, FakePositionProvider  # noqa: E402


class _Gateway(object):
    def __init__(self, rows):
        self.rows = rows

    def query_orders(self, account_id, strategy_name):
        return list(self.rows)


class _Row(object):
    def __init__(self, order_sys_id, status):
        self.order_sys_id = order_sys_id
        self.status = status


def _handlers(rows):
    handlers = BigQmtRpcHandlers(
        account_id="acct",
        market_data=FakeMarketData(),
        position_provider=FakePositionProvider(),
        order_gateway=_Gateway(rows),
        allow_order_methods=True,
    )
    return handlers


def _settlement(order_sys_id="645005092", remark="grid-7"):
    return CancelSettlement(
        OrderRef(order_sys_id, remark),
        "acct",
        CancelResult(True),
        1e12,
    )


class CancelErrorPushTest(unittest.TestCase):
    def _run(self, rows, final=True):
        handlers = _handlers(rows)
        events = []
        handlers.exec_event_publisher = (
            lambda account_id, event: events.append((account_id, event)))
        settlement = _settlement()
        settled = handlers._apply_cancel_lookup(settlement, final=final)
        return settled, settlement, events

    def test_filled_order_cancel_pushes_cancel_error(self):
        # The reporter's exact case: 已成 (56) is terminal -- nothing left
        # to cancel, and QMT fires no callback for the refusal.
        settled, settlement, events = self._run([_Row("645005092", "56")])

        self.assertTrue(settled)
        self.assertFalse(settlement.result.success)
        self.assertEqual(len(events), 1)
        account_id, event = events[0]
        self.assertEqual(account_id, "acct")
        self.assertEqual(event["event_type"], "cancel_error")
        self.assertEqual(event["order_sys_id"], "645005092")
        self.assertEqual(event["order_remark"], "grid-7")
        self.assertIn("56", event["error_msg"])

    def test_junk_order_cancel_also_pushes(self):
        settled, settlement, events = self._run([_Row("645005092", "57")])

        self.assertTrue(settled)
        self.assertFalse(settlement.result.success)
        self.assertEqual(len(events), 1)

    def test_successful_cancel_pushes_nothing(self):
        settled, settlement, events = self._run([_Row("645005092", "54")])

        self.assertTrue(settled)
        self.assertTrue(settlement.result.success)
        self.assertEqual(events, [])

    def test_not_found_at_deadline_pushes(self):
        settled, settlement, events = self._run([], final=True)

        self.assertTrue(settled)
        self.assertFalse(settlement.result.success)
        self.assertEqual(len(events), 1)
        self.assertIn("not found", events[0][1]["error_msg"])

    def test_unresolved_before_deadline_pushes_nothing(self):
        settled, settlement, events = self._run([], final=False)

        self.assertFalse(settled)
        self.assertEqual(events, [])

    def test_in_flight_cancel_is_not_an_error(self):
        # 51/52 = the exchange accepted the cancel; reporting an error here
        # is the #151 false negative through a narrower window.
        settled, settlement, events = self._run([_Row("645005092", "51")], final=True)

        self.assertTrue(settled)
        self.assertTrue(settlement.result.success)
        self.assertEqual(events, [])

    def test_no_hook_installed_still_settles(self):
        handlers = _handlers([_Row("645005092", "56")])
        settlement = _settlement()

        settled = handlers._apply_cancel_lookup(settlement, final=True)

        self.assertTrue(settled)
        self.assertFalse(settlement.result.success)

    def test_event_matches_client_dispatch_fields(self):
        # The client's on_cancel_error CompatObject reads exactly these.
        settled, settlement, events = self._run([_Row("645005092", "56")])

        event = events[0][1]
        for field in ("event_type", "account_id", "stock_code", "order_sys_id",
                      "order_id", "error_id", "order_remark", "user_order_id",
                      "error_msg", "created_at", "created_at_ts"):
            self.assertIn(field, event, field)


if __name__ == "__main__":
    unittest.main()
