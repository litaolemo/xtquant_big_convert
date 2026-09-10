# coding: utf-8
"""An explicitly empty strategy_name must stay empty on the order path.

#154 handed the caller the string that lands in QMT's 报单来源 column, and the
empty string is a real answer there: it leaves the column blank, the way a
hand-placed order looks. The config-level default honoured that from the
start. The two submit sites did not -- they resolved the per-call value with
``or``::

    strategy_name=str(params.get("strategy_name") or self.default_strategy_name)

``""`` is falsy, so a caller asking for a blank column got "bigqmt_rpc" on
every order instead: exactly the string they were trying to remove. Only a
value the caller never supplied (``None`` or absent) may reach the default.

The batch site resolved the same way, and there the name also selects what the
idempotency lookup queries with. Under a blank-name deployment that lookup
searched for orders under "bigqmt_rpc", matched none of its own, and so could
not recognise a retry.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import (  # noqa: E402
    DEFAULT_ORDER_STRATEGY_NAME,
    BigQmtRpcHandlers,
)
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway  # noqa: E402

from test_redis_rpc import (  # noqa: E402  -- reuse the established fakes
    FakeMarketData,
    FakePositionProvider,
)


class _RecordingGateway(DryRunOrderGateway):
    """Also records the strategy_name each idempotency lookup queries with."""

    def __init__(self):
        super(_RecordingGateway, self).__init__()
        self.queried_names = []

    def query_orders(self, account_id, strategy_name):
        self.queried_names.append(strategy_name)
        return []


def _handlers(gateway, **kwargs):
    return BigQmtRpcHandlers(
        account_id="acct",
        market_data=FakeMarketData(),
        position_provider=FakePositionProvider(),
        order_gateway=gateway,
        allow_order_methods=True,
        **kwargs
    )


def _order(**overrides):
    params = {
        "stock_code": "601398.SH", "action": "BUY", "volume": 100,
        "price": 8.0, "remark": "tag-1", "wait_settlement": False,
    }
    params.update(overrides)
    return params


class SingleOrderTest(unittest.TestCase):
    def test_an_explicit_empty_name_stays_empty(self):
        """The reported bug: asking for a blank column produced "bigqmt_rpc"."""
        gateway = _RecordingGateway()
        _handlers(gateway)._handle_submit_order(_order(strategy_name=""))
        self.assertEqual(gateway.submitted[0].strategy_name, "")

    def test_an_absent_name_still_takes_the_default(self):
        """Existing deployments must not be renamed by this fix."""
        gateway = _RecordingGateway()
        _handlers(gateway)._handle_submit_order(_order())
        self.assertEqual(gateway.submitted[0].strategy_name,
                         DEFAULT_ORDER_STRATEGY_NAME)

    def test_an_explicit_none_still_takes_the_default(self):
        """None is "the caller said nothing", which is not the same as ""."""
        gateway = _RecordingGateway()
        _handlers(gateway)._handle_submit_order(_order(strategy_name=None))
        self.assertEqual(gateway.submitted[0].strategy_name,
                         DEFAULT_ORDER_STRATEGY_NAME)

    def test_a_named_strategy_still_wins(self):
        gateway = _RecordingGateway()
        _handlers(gateway)._handle_submit_order(_order(strategy_name="my_book"))
        self.assertEqual(gateway.submitted[0].strategy_name, "my_book")

    def test_empty_beats_a_non_empty_configured_default(self):
        """Per-call has always won; "" is a call, not the absence of one."""
        gateway = _RecordingGateway()
        handlers = _handlers(gateway, default_strategy_name="my_book")
        handlers._handle_submit_order(_order(strategy_name=""))
        self.assertEqual(gateway.submitted[0].strategy_name, "")


class BatchTest(unittest.TestCase):
    def test_a_per_order_empty_name_stays_empty(self):
        gateway = _RecordingGateway()
        _handlers(gateway)._handle_submit_orders_batch(
            {"orders": [_order(strategy_name="")]})
        self.assertEqual(gateway.submitted[0].strategy_name, "")

    def test_the_idempotency_lookup_uses_the_empty_name(self):
        """Searching under "bigqmt_rpc" would never match its own orders."""
        gateway = _RecordingGateway()
        _handlers(gateway)._handle_submit_orders_batch({
            "strategy_name": "",
            "orders": [_order(strategy_name="",
                              require_idempotency_check=True)],
        })
        self.assertEqual(gateway.queried_names, [""])

    def test_an_absent_batch_name_still_looks_up_the_default(self):
        gateway = _RecordingGateway()
        _handlers(gateway)._handle_submit_orders_batch({
            "orders": [_order(require_idempotency_check=True)],
        })
        self.assertEqual(gateway.queried_names, [DEFAULT_ORDER_STRATEGY_NAME])
