# coding: utf-8
"""A reused remark must not make order_stock return another order's sysid.

Issue #299, @pujfei, with the exec-event stream as evidence. A serial script
placed three stocks under remarks like ``串行买入600股``, reused across
stocks and across batches. All three filled; ``order_stock`` returned, for
each, the contract id of the most recent OTHER order carrying that remark:

    submitted                     returned   real (from the event stream)
    600999.SH BUY 600 @17.78      9879       11355   (9879 = 600418, 13 min earlier)
    600519.SH BUY 100 @1300.83    9877       11367   (9877 = 600519, earlier batch)
    600418.SH BUY 600 @19.40      11355      11387   (11355 = this batch's 600999)

The caller matched callbacks by that id, so every real event was filtered
out and three fills were reported as "still in flight"; a retry there is a
duplicate order.

Both lookup paths keyed on the remark alone. The watch table (#164) holds one
slot per remark, so the last same-remark order's callback answers the next
order's settlement before its own callback lands. The poll took
``by_remark[0]`` -- the oldest row -- with no stock, side or time filter.

The settlement now records the instant before passorder, and both paths
require the candidate to be the request's stock and side and to postdate
that instant. The remark's meaning is unchanged; it just stops being
sufficient on its own.
"""

import os
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.models import OrderSnapshot  # noqa: E402
from bigqmt_signal_trader.order_watch import OrderWatchTable  # noqa: E402
from bigqmt_signal_trader.redis_rpc import (  # noqa: E402
    BigQmtRpcHandlers,
    OrderSettlement,
    _rows_for_this_submit,
)
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway  # noqa: E402

from test_redis_rpc import (  # noqa: E402  -- the established fakes
    FakeMarketData,
    FakePositionProvider,
)

REMARK = "串行买入600股"


def _row(sysid, code, action="BUY", order_time=0, remark=REMARK, price=10.0):
    return OrderSnapshot(order_sys_id=sysid, user_order_id=remark, stock_code=code,
                         action=action, volume=600, traded_volume=0, status="50",
                         price=price, order_time=order_time)


class _RowsGateway(DryRunOrderGateway):
    def __init__(self, rows):
        super(_RowsGateway, self).__init__()
        self.rows = rows
        self.queries = 0

    def query_orders(self, account_id, strategy_name):
        self.queries += 1
        return list(self.rows)


def _handlers(gateway, table=None):
    handlers = BigQmtRpcHandlers(
        account_id="acct", market_data=FakeMarketData(),
        position_provider=FakePositionProvider(), order_gateway=gateway,
        allow_order_methods=True)
    handlers.order_watch_table = table
    return handlers


def _settle(handlers, code, submitted_at, action="BUY"):
    """Run one lookup for a submit of ``code`` made at ``submitted_at``."""
    from bigqmt_signal_trader.models import OrderRequest, OrderSubmitResult
    request = OrderRequest(signal_id="s", account_id="acct", action=action,
                           stock_code=code, volume=600, price=10.0,
                           price_type="LIMIT", strategy_name="", remark=REMARK)
    result = OrderSubmitResult(status="SUBMITTED", user_order_id=REMARK,
                               order_sys_id=None, message="")
    settlement = OrderSettlement(request, result, 0.0, submitted_at=submitted_at)
    done = handlers._apply_order_lookup(settlement, final=False)
    return done, result.order_sys_id


class RowFilterTest(unittest.TestCase):
    """_rows_for_this_submit, the poll path's selector, in isolation."""

    def test_the_reported_batch_picks_each_order_its_own_row(self):
        """Three stocks, one remark. Stock alone separates them."""
        now = int(time.time())
        rows = [
            _row("9879", "600418.SH", order_time=now - 800),   # earlier batch
            _row("9877", "600519.SH", order_time=now - 800),   # earlier batch
            _row("11355", "600999.SH", order_time=now - 5),
            _row("11367", "600519.SH", order_time=now - 3),
            _row("11387", "600418.SH", order_time=now - 1),
        ]
        pick = lambda code: _rows_for_this_submit(rows, REMARK, code, "BUY", now - 10)[0].order_sys_id
        self.assertEqual("11355", pick("600999.SH"))
        self.assertEqual("11367", pick("600519.SH"))
        self.assertEqual("11387", pick("600418.SH"))

    def test_same_stock_reuse_is_split_by_the_submit_instant(self):
        """A grid re-buying the same stock under the same remark: the row
        from before this submit is not this submit's."""
        now = int(time.time())
        rows = [_row("A", "600000.SH", order_time=now - 60),
                _row("B", "600000.SH", order_time=now)]
        got = _rows_for_this_submit(rows, REMARK, "600000.SH", "BUY", now - 1)
        self.assertEqual(["B"], [r.order_sys_id for r in got])

    def test_earliest_after_the_submit_wins_not_latest(self):
        """The next rung placed while this one settles must not shadow it."""
        now = int(time.time())
        rows = [_row("B", "600000.SH", order_time=now),
                _row("C", "600000.SH", order_time=now + 5)]
        got = _rows_for_this_submit(rows, REMARK, "600000.SH", "BUY", now)
        self.assertEqual("B", got[0].order_sys_id)

    def test_a_different_side_is_not_this_submit(self):
        rows = [_row("S", "600000.SH", action="SELL", order_time=int(time.time()))]
        self.assertEqual([], _rows_for_this_submit(rows, REMARK, "600000.SH", "BUY", 0))

    def test_a_row_without_a_side_is_not_rejected_on_side(self):
        """Old servers send no action; that must not turn a real row into a
        'not in the system' verdict."""
        rows = [_row("X", "600000.SH", action="", order_time=int(time.time()))]
        self.assertEqual(["X"], [r.order_sys_id for r in _rows_for_this_submit(rows, REMARK, "600000.SH", "BUY", 0)])

    def test_a_row_without_a_time_is_kept_but_after_timed_ones(self):
        """#267 gave order_time real values; before that it was 0. A 0 cannot
        prove staleness, so the row stays a candidate, ordered last."""
        now = int(time.time())
        rows = [_row("T0", "600000.SH", order_time=0),
                _row("T1", "600000.SH", order_time=now)]
        got = _rows_for_this_submit(rows, REMARK, "600000.SH", "BUY", now - 1)
        self.assertEqual(["T1", "T0"], [r.order_sys_id for r in got])

    def test_stock_codes_are_compared_normalized(self):
        rows = [_row("N", "600000.SH", order_time=int(time.time()))]
        self.assertEqual(1, len(_rows_for_this_submit(rows, REMARK, "600000.SH", "BUY", 0)))
        self.assertEqual(1, len(_rows_for_this_submit(rows, REMARK, "sh600000", "BUY", 0)))


class WatchTableGuardTest(unittest.TestCase):
    def _table(self, code="600418.SH", action="BUY"):
        table = OrderWatchTable()
        table.note({"user_order_id": REMARK, "order_sys_id": "9879", "status": "50",
                    "stock_code": code, "action": action})
        return table

    def test_an_entry_from_before_the_submit_is_not_this_submits(self):
        """The reported fast-path failure: 9879 learned 13 minutes earlier
        answered 600999's settlement."""
        table = self._table()
        learned_at = table._by_remark[REMARK][0]

        self.assertIsNone(table.sysid_for_remark(REMARK, not_before=learned_at + 1.0))
        self.assertEqual("9879", table.sysid_for_remark(REMARK, not_before=learned_at))

    def test_a_different_stock_is_not_this_submits(self):
        table = self._table(code="600418.SH")
        self.assertIsNone(table.sysid_for_remark(REMARK, stock_code="600999.SH"))
        self.assertEqual("9879", table.sysid_for_remark(REMARK, stock_code="600418.SH"))

    def test_a_different_side_is_not_this_submits(self):
        table = self._table(action="BUY")
        self.assertIsNone(table.sysid_for_remark(REMARK, action="SELL"))

    def test_an_entry_learned_without_dimensions_still_answers(self):
        """An older server's callback carries no stock/side; the remark and
        the not-before guard are all there is, and that must still work."""
        table = OrderWatchTable()
        table.note({"user_order_id": REMARK, "order_sys_id": "Z", "status": "50"})
        self.assertEqual("Z", table.sysid_for_remark(REMARK, stock_code="600000.SH", action="BUY"))

    def test_the_old_two_tuple_shape_still_reads(self):
        table = OrderWatchTable()
        table._by_remark[REMARK] = (time.time(), "OLD")
        self.assertEqual("OLD", table.sysid_for_remark(REMARK))


class EndToEndLookupTest(unittest.TestCase):
    """_apply_order_lookup with the reported state on both paths."""

    def test_fast_path_ignores_the_earlier_orders_entry_and_polls(self):
        table = OrderWatchTable()
        table.note({"user_order_id": REMARK, "order_sys_id": "9879", "status": "50",
                    "stock_code": "600418.SH", "action": "BUY"})
        now = time.time()
        gateway = _RowsGateway([_row("11355", "600999.SH", order_time=int(now) + 1)])
        handlers = _handlers(gateway, table)

        done, sysid = _settle(handlers, "600999.SH", submitted_at=now + 0.5)

        self.assertTrue(done)
        self.assertEqual("11355", sysid, "took the earlier order's id from the table")
        self.assertEqual(1, gateway.queries, "the table miss must fall through to the poll")

    def test_poll_path_picks_this_stocks_row_not_by_remark_zero(self):
        now = int(time.time())
        gateway = _RowsGateway([
            _row("9879", "600418.SH", order_time=now - 800),
            _row("11355", "600999.SH", order_time=now),
        ])
        handlers = _handlers(gateway, table=None)

        done, sysid = _settle(handlers, "600999.SH", submitted_at=now - 1)

        self.assertTrue(done)
        self.assertEqual("11355", sysid)

    def test_only_a_stale_same_stock_row_means_not_yet(self):
        """Same stock, same remark, row older than this submit: keep waiting,
        do not settle on the earlier order's id."""
        now = int(time.time())
        gateway = _RowsGateway([_row("A", "600000.SH", order_time=now - 60)])
        handlers = _handlers(gateway, table=None)

        done, sysid = _settle(handlers, "600000.SH", submitted_at=now)

        self.assertFalse(done)
        self.assertIsNone(sysid)

    def test_settlement_records_the_submit_instant(self):
        from bigqmt_signal_trader.models import OrderRequest, OrderSubmitResult
        request = OrderRequest(signal_id="s", account_id="acct", action="BUY",
                               stock_code="600000.SH", volume=100, price=1.0,
                               price_type="LIMIT", strategy_name="", remark="r")
        result = OrderSubmitResult(status="SUBMITTED", user_order_id="r",
                                   order_sys_id=None, message="")
        before = time.time()
        settlement = OrderSettlement(request, result, 0.0)
        self.assertGreaterEqual(settlement.submitted_at, before)
        self.assertEqual(7.0, OrderSettlement(request, result, 0.0, submitted_at=7.0).submitted_at)


if __name__ == "__main__":
    unittest.main()
