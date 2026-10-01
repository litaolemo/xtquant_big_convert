# coding: utf-8
"""A reused order remark must never rename rows already reported (#393).

Reported by @kingtsi: orders read back from ``query_stock_orders`` suddenly
carried the QMT-side strategy name (the bridge process's registered name)
instead of the strategy_name passed at submit time. Three bridge-side
paths produce that flip, all fixed together:

- an unconditional overwrite of the identity record -- a later same-remark
  submit with a blank ``strategy_name`` erased the name, and rows fell back
  to the QMT process name (``m_strSource``);
- a same-remark submit under a *different* strategy renamed rows that were
  already reported under the first name;
- a 24h TTL on both stores renamed everything still queryable the next day;
  and a reload wiped the in-process journal on no-Redis deployments.

The rule now, in both stores: the first NAMED record wins. A blank submit
never erases a name; an unnamed record yields to a later named one.
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import exec_events  # noqa: E402
from bigqmt_signal_trader.exec_events import (  # noqa: E402
    order_identity_key,
    order_identity_map,
    remember_order_identity,
)
from bigqmt_signal_trader.models import OrderSnapshot  # noqa: E402
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers  # noqa: E402

from test_redis_rpc import FakeMarketData, FakePositionProvider  # noqa: E402
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway  # noqa: E402


class FakeRedis(object):
    def __init__(self):
        self.kv = {}
        self.ttls = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        self.ttls[key] = ex
        return True

    def setex(self, key, ttl, value):
        self.kv[key] = value
        self.ttls[key] = ttl
        return True

    def setnx(self, key, value):
        if key in self.kv:
            return False
        self.kv[key] = value
        return True

    def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    def get(self, key):
        return self.kv.get(key)

    def mget(self, keys):
        return [self.kv.get(k) for k in keys]


def _name_in(redis_client, account_id="acct", remark="sig-1"):
    identities = order_identity_map(redis_client, account_id, [remark])
    identity = identities.get(remark) or {}
    return identity.get("strategy_name")


class RedisIdentityFirstNamedWinsTest(unittest.TestCase):
    def test_blank_submit_does_not_erase_a_named_record(self):
        redis_client = FakeRedis()
        remember_order_identity(redis_client, "acct", "sig-1", "my_strat")

        result = remember_order_identity(redis_client, "acct", "sig-1", "")

        self.assertIsNone(result)
        self.assertEqual(_name_in(redis_client), "my_strat")

    def test_conflicting_name_keeps_the_first(self):
        redis_client = FakeRedis()
        remember_order_identity(redis_client, "acct", "sig-1", "strat_a")

        remember_order_identity(redis_client, "acct", "sig-1", "strat_b")

        self.assertEqual(_name_in(redis_client), "strat_a")

    def test_unnamed_record_yields_to_a_later_named_submit(self):
        redis_client = FakeRedis()
        remember_order_identity(redis_client, "acct", "sig-1", "")

        remember_order_identity(redis_client, "acct", "sig-1", "my_strat")

        self.assertEqual(_name_in(redis_client), "my_strat")

    def test_same_name_resubmit_is_a_quiet_no_op(self):
        redis_client = FakeRedis()
        remember_order_identity(redis_client, "acct", "sig-1", "my_strat")

        remember_order_identity(redis_client, "acct", "sig-1", "my_strat")

        self.assertEqual(_name_in(redis_client), "my_strat")

    def test_ttl_is_seven_days_so_next_day_reads_keep_the_name(self):
        redis_client = FakeRedis()
        remember_order_identity(redis_client, "acct", "sig-1", "my_strat")

        key = order_identity_key("acct", "sig-1")
        self.assertEqual(redis_client.ttls.get(key), 7 * 86400)
        self.assertEqual(exec_events.ORDER_IDENTITY_TTL_SECONDS, 7 * 86400)


class _QueryGateway(DryRunOrderGateway):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def query_orders(self, account_id, strategy_name):
        return list(self.rows)


def _row(user_order_id="", strategy_name=""):
    return OrderSnapshot(
        order_sys_id="sys-1",
        user_order_id=user_order_id,
        stock_code="601398.SH",
        action="BUY",
        volume=100,
        traded_volume=0,
        status="50",
        strategy_name=strategy_name,
    )


def _handlers(rows):
    return BigQmtRpcHandlers(
        account_id="acct",
        market_data=FakeMarketData(),
        position_provider=FakePositionProvider(),
        order_gateway=_QueryGateway(rows),
        allow_order_methods=True,
    )


def _submit(handlers, remark, strategy_name):
    handlers._handle_submit_order({
        "stock_code": "601398.SH", "action": "BUY", "volume": 100,
        "price": 8.0, "remark": remark, "strategy_name": strategy_name,
        "wait_settlement": False,
    })


class LocalJournalFirstNamedWinsTest(unittest.TestCase):
    def test_blank_submit_does_not_erase_the_name(self):
        handlers = _handlers([_row(user_order_id="sig-1")])
        self.assertIsNone(handlers._identity_redis())  # no-Redis shape

        _submit(handlers, "sig-1", "my_strat")
        _submit(handlers, "sig-1", "")
        rows = handlers._handle_query_orders({})

        self.assertEqual(rows[0].strategy_name, "my_strat")

    def test_conflicting_name_keeps_the_first(self):
        handlers = _handlers([_row(user_order_id="sig-1")])

        _submit(handlers, "sig-1", "strat_a")
        _submit(handlers, "sig-1", "strat_b")
        rows = handlers._handle_query_orders({})

        self.assertEqual(rows[0].strategy_name, "strat_a")

    def test_unnamed_record_yields_to_a_later_named_submit(self):
        handlers = _handlers([_row(user_order_id="sig-1")])

        _submit(handlers, "sig-1", "")
        _submit(handlers, "sig-1", "my_strat")
        rows = handlers._handle_query_orders({})

        self.assertEqual(rows[0].strategy_name, "my_strat")

    def test_journal_ttl_matches_the_redis_store(self):
        self.assertEqual(BigQmtRpcHandlers._ORDER_IDENTITY_LOCAL_TTL_SECONDS,
                         exec_events.ORDER_IDENTITY_TTL_SECONDS)


if __name__ == "__main__":
    unittest.main()
