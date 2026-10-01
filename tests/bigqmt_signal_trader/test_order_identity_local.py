# coding: utf-8
"""No-Redis deployments must still attribute strategy_name (issue #156 / #133).

QMT's ORDER/DEAL rows never carry the strategy name -- the terminal filters
by it but does not report it. The bridge answers that with an identity store
keyed by remark, but the store is Redis, so a zmq single-file deployment (no
Redis anywhere) read strategy_name as "" forever.

The handlers now keep an in-process journal as well: submit writes
(account, remark) -> strategy_name, query reads it back for rows QMT could
not name. Redis remains the primary store (survives restarts, covers other
processes); the local journal is the no-Redis floor.
"""

import os
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.models import OrderSnapshot
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers

from test_redis_rpc import (  # noqa: E402  -- reuse the established fakes
    FakeMarketData,
    FakePositionProvider,
)
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway


class _QueryGateway(DryRunOrderGateway):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def query_orders(self, account_id, strategy_name):
        return list(self.rows)


def _row(user_order_id="", strategy_name="", order_sys_id="sys-1"):
    return OrderSnapshot(
        order_sys_id=order_sys_id,
        user_order_id=user_order_id,
        stock_code="601398.SH",
        action="BUY",
        volume=100,
        traded_volume=0,
        status="50",
        strategy_name=strategy_name,
    )


def _handlers(gateway):
    return BigQmtRpcHandlers(
        account_id="acct",
        market_data=FakeMarketData(),
        position_provider=FakePositionProvider(),
        order_gateway=gateway,
        allow_order_methods=True,
    )


def _submit(handlers, remark, strategy_name):
    handlers._handle_submit_order({
        "stock_code": "601398.SH", "action": "BUY", "volume": 100,
        "price": 8.0, "remark": remark, "strategy_name": strategy_name,
        "wait_settlement": False,
    })


class LocalIdentityJournalTest(unittest.TestCase):
    def test_submitted_order_gets_strategy_name_back_without_redis(self):
        gateway = _QueryGateway([_row(user_order_id="sig-1")])
        handlers = _handlers(gateway)
        # No identity Redis anywhere -- the zmq single-file shape.
        self.assertIsNone(handlers._identity_redis())

        _submit(handlers, "sig-1", "my_strat")
        rows = handlers._handle_query_orders({})

        self.assertEqual(rows[0].strategy_name, "my_strat")

    def test_order_not_submitted_here_stays_unnamed(self):
        gateway = _QueryGateway([_row(user_order_id="manual-order")])
        handlers = _handlers(gateway)

        rows = handlers._handle_query_orders({})

        self.assertEqual(rows[0].strategy_name, "")

    def test_journal_is_scoped_by_account(self):
        gateway = _QueryGateway([_row(user_order_id="sig-1")])
        handlers = _handlers(gateway)
        _submit(handlers, "sig-1", "my_strat")

        rows = handlers._handle_query_orders({"account_id": "someone-else"})

        self.assertEqual(rows[0].strategy_name, "")

    def test_expired_entries_do_not_attribute(self):
        gateway = _QueryGateway([_row(user_order_id="sig-1")])
        handlers = _handlers(gateway)
        _submit(handlers, "sig-1", "my_strat")
        key = ("acct", "sig-1")
        ts, name = handlers._order_identity_local[key]
        # Past the 7-day journal TTL (#393 extended it from 24h so next-day
        # re-reads keep their attribution).
        handlers._order_identity_local[key] = (ts - 8 * 86400, name)

        rows = handlers._handle_query_orders({})

        self.assertEqual(rows[0].strategy_name, "")

    def test_journal_is_bounded(self):
        handlers = _handlers(_QueryGateway([]))
        for i in range(handlers._ORDER_IDENTITY_LOCAL_LIMIT + 50):
            handlers._remember_order_identity_local("acct", "r%d" % i, "s")

        self.assertEqual(
            len(handlers._order_identity_local),
            handlers._ORDER_IDENTITY_LOCAL_LIMIT)
        self.assertNotIn(("acct", "r0"), handlers._order_identity_local)
        self.assertIn(("acct", "r%d" % (handlers._ORDER_IDENTITY_LOCAL_LIMIT + 49)),
                      handlers._order_identity_local)

    def test_the_journal_identity_wins_over_the_rows_bridge_name(self):
        """#216 changed the precedence: the row's strategy-name field carries
        the QMT-side strategy's registered name (the bridge process), never
        the caller's -- so a submit-time identity record (this journal) is the
        authoritative one and WINS over the row's value."""
        gateway = _QueryGateway([_row(user_order_id="sig-1", strategy_name="from_redis")])
        handlers = _handlers(gateway)
        _submit(handlers, "sig-1", "from_local")

        rows = handlers._handle_query_orders({})

        self.assertEqual(rows[0].strategy_name, "from_local")


class FilteredQueryNamesRowsTest(unittest.TestCase):
    """A strategy_name-filtered order query must name the rows it returns.

    #156 follow-up: @kingtsi filtered query_stock_orders by strategy_name and
    got correctly filtered rows -- that all read strategy_name="". The trade
    builder had the filter fallback; the order builder did not.
    """

    def _gateway(self, rows):
        from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway

        gateway = BigQmtOrderGateway.__new__(BigQmtOrderGateway)
        gateway._rows = rows
        gateway._require_query_func = lambda: (
            lambda account_id, account_type, kind, strategy_name: rows)
        gateway._account_type_code = lambda account_id=None: 2
        gateway.account_type = "STOCK"
        gateway.context_info = None
        return gateway

    def _row(self):
        attrs = dict(
            m_strInstrumentID="601398", m_strExchangeID="SH", m_nOffsetFlag=48,
            m_nVolumeTotalOriginal=100, m_nVolumeTraded=0, m_nOrderStatus=50,
            m_dLimitPrice=7.9, m_strRemark="sig-1", m_strOrderSysID="S1",
            m_dTradedPrice=0.0,
        )
        return type("Row", (), attrs)

    def test_filtered_order_query_names_rows_with_the_filter(self):
        gateway = self._gateway([self._row()])
        rows = gateway.query_orders_strict("acct", "TEST")
        self.assertEqual(rows[0].strategy_name, "TEST")

    def test_unfiltered_order_query_stays_empty_for_the_journal(self):
        gateway = self._gateway([self._row()])
        rows = gateway.query_orders_strict("acct", "")
        self.assertEqual(rows[0].strategy_name, "")


class ProbeOrderIdentityTest(unittest.TestCase):
    """The probe must answer each link of the backfill chain (#156)."""

    def test_probe_reports_redis_and_local_hits(self):
        gateway = _QueryGateway([])
        handlers = _handlers(gateway)

        class FakeRedis(object):
            def get(self, key):
                return b'{"strategy_name": "TEST"}' if key.endswith(":sig-1") else None

        handlers.order_identity_redis_client = FakeRedis()
        handlers._remember_order_identity_local("acct", "sig-2", "local_strat")

        out = handlers._handle_probe_order_identity({"remark": "sig-1"})
        self.assertTrue(out["identity_redis_wired"])
        self.assertTrue(out["redis_hit"])
        self.assertFalse(out["local_hit"])
        self.assertEqual(out["identity_key"],
                         "bigqmt:order_identity:acct:sig-1")

        out = handlers._handle_probe_order_identity({"remark": "sig-2"})
        self.assertFalse(out["redis_hit"])
        self.assertTrue(out["local_hit"])
        self.assertEqual(out["local_strategy_name"], "local_strat")

    def test_probe_without_redis_says_so(self):
        handlers = _handlers(_QueryGateway([]))
        out = handlers._handle_probe_order_identity({"remark": "sig-x"})
        self.assertFalse(out["identity_redis_wired"])
        self.assertFalse(out["local_hit"])


if __name__ == "__main__":
    unittest.main()
