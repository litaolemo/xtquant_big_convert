# coding: utf-8
"""港股通 on a stock account: one account id, several account types.

The terminal keeps 沪港通 / 深港通 positions, orders and deals under
``get_trade_detail_data(acc, 'HUGANGTONG' | 'SHENGANGTONG', ...)`` -- the
same account id as the STOCK book, a different type. An id -> one type
table (BIGQMT_ACCOUNT_TYPE_MAP) could not say that, so:

  - BIGQMT_ACCOUNT_TYPE / a map value may be a LIST; the first is the default
  - the client sends StockAccount(id, "HUGANGTONG").account_type with every
    trade RPC as the ``account_type`` param
  - the server answers as that type for the request when the account is
    configured for it, and as the default (logged once) when it is not --
    the deployment's config still decides what the account trades as (#92)
  - an order's settlement lookup runs under the type it was placed as
"""

import os
import sys
import threading
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import bigqmt_signal_trader.account_type_map as atm  # noqa: E402
from bigqmt_signal_trader.account_type_map import (  # noqa: E402
    account_type_for,
    account_types_for,
    get_request_account_type,
    normalize_account_types,
    primary_account_type,
    request_account_type,
)
from bigqmt_signal_trader.adapters.position_bigqmt import BigQmtPositionProvider  # noqa: E402
from bigqmt_signal_trader.models import OrderSubmitResult  # noqa: E402
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers  # noqa: E402
from bigqmt_signal_trader.xtquant_compat import (  # noqa: E402
    BigQmtXtTrader,
    _with_account_type,
)
from xtquant.xttype import StockAccount  # noqa: E402

from test_redis_rpc import FakeMarketData, FakePositionProvider  # noqa: E402


def _config(**attrs):
    cfg = types.ModuleType("bigqmt_signal_trader_local_config")
    for key, value in attrs.items():
        setattr(cfg, key, value)
    return mock.patch.dict("sys.modules", {"bigqmt_signal_trader_local_config": cfg})


class NormalizeTest(unittest.TestCase):
    def test_names_codes_lists(self):
        self.assertEqual(normalize_account_types("stock"), ["STOCK"])
        self.assertEqual(normalize_account_types(["STOCK", "hugangtong", "STOCK"]),
                         ["STOCK", "HUGANGTONG"])
        self.assertEqual(normalize_account_types(7), ["HUGANGTONG"])
        self.assertEqual(normalize_account_types(("2", 11)), ["STOCK", "SHENGANGTONG"])
        self.assertEqual(normalize_account_types(None), [])
        self.assertEqual(normalize_account_types(""), [])
        self.assertEqual(primary_account_type(["credit", "x"]), "CREDIT")
        self.assertEqual(primary_account_type(None), "STOCK")


class ListValuedConfigTest(unittest.TestCase):
    def setUp(self):
        atm.reload()

    def tearDown(self):
        atm.reload()

    def test_map_value_list_default_is_first(self):
        with _config(BIGQMT_ACCOUNT_TYPE_MAP={"123": ["STOCK", "HUGANGTONG"]}):
            atm.reload()
            self.assertEqual(account_types_for("123", "CREDIT"), ["STOCK", "HUGANGTONG"])
            self.assertEqual(account_type_for("123", "CREDIT"), "STOCK")
            self.assertEqual(account_type_for("123", "CREDIT", requested="HUGANGTONG"), "HUGANGTONG")
            self.assertEqual(account_type_for("123", "CREDIT", requested="hugangtong"), "HUGANGTONG")
            self.assertEqual(account_type_for("123", "CREDIT", requested=7), "HUGANGTONG")
            # Not configured for it: the default answers.
            self.assertEqual(account_type_for("123", "CREDIT", requested="SHENGANGTONG"), "STOCK")

    def test_primary_list_without_a_map(self):
        with _config(BIGQMT_ACCOUNT_ID="123",
                     BIGQMT_ACCOUNT_TYPE=["STOCK", "HUGANGTONG", "SHENGANGTONG"]):
            atm.reload()
            self.assertEqual(account_types_for("123", "STOCK"),
                             ["STOCK", "HUGANGTONG", "SHENGANGTONG"])
            self.assertEqual(account_type_for("123", "STOCK"), "STOCK")
            self.assertEqual(account_type_for("123", "STOCK", requested="SHENGANGTONG"), "SHENGANGTONG")
            # Another account id is not the primary: only the default.
            self.assertEqual(account_types_for("999", "STOCK"), ["STOCK"])
            self.assertEqual(account_type_for("999", "STOCK", requested="HUGANGTONG"), "STOCK")

    def test_plain_string_config_is_unchanged(self):
        # No list anywhere: the old contract, default returned verbatim and
        # a client's declared type ignored (#92: the deployment decides).
        with _config(BIGQMT_ACCOUNT_ID="123", BIGQMT_ACCOUNT_TYPE="CREDIT"):
            atm.reload()
            self.assertEqual(account_type_for("123", "credit"), "credit")
            self.assertEqual(account_type_for("123", "CREDIT", requested="STOCK"), "CREDIT")
            self.assertEqual(account_type_for("", "STOCK", requested="HUGANGTONG"), "STOCK")

    def test_request_scope_is_per_thread_and_nested(self):
        with _config(BIGQMT_ACCOUNT_TYPE_MAP={"123": ["STOCK", "HUGANGTONG"]}):
            atm.reload()
            seen = {}

            def other_thread():
                seen["other"] = account_type_for("123", "STOCK")

            with request_account_type("HUGANGTONG"):
                self.assertEqual(account_type_for("123", "STOCK"), "HUGANGTONG")
                with request_account_type(None):
                    self.assertIsNone(get_request_account_type())
                    self.assertEqual(account_type_for("123", "STOCK"), "STOCK")
                self.assertEqual(get_request_account_type(), "HUGANGTONG")
                t = threading.Thread(target=other_thread)
                t.start()
                t.join()
            self.assertEqual(seen["other"], "STOCK")
            self.assertIsNone(get_request_account_type())
            self.assertEqual(account_type_for("123", "STOCK"), "STOCK")


class _RecordingQuery(object):
    def __init__(self):
        self.calls = []

    def __call__(self, account_id, account_type, detail_type, strategy_name=""):
        self.calls.append((account_id, account_type, detail_type))
        return []


class _RecordingGateway(object):
    """Just enough order_gateway for submit + settlement bookkeeping."""

    account_type = "STOCK"

    def __init__(self):
        self.queried = []
        self.get_trade_detail_data = None

    def _resolve_account_type(self, account_id):
        return account_type_for(account_id, self.account_type)

    def submit(self, request):
        return OrderSubmitResult(status="SUBMITTED", user_order_id=request.remark,
                                 order_sys_id=None, message="")

    def query_orders(self, account_id, strategy_name=""):
        self.queried.append((account_id, self._resolve_account_type(account_id)))
        return []

    def build_user_order_id(self, signal_id):
        return "bqrpc:%s" % signal_id


class ServerSideTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _config(BIGQMT_ACCOUNT_ID="123",
                           BIGQMT_ACCOUNT_TYPE=["STOCK", "HUGANGTONG", "SHENGANGTONG"])
        self.cfg.start()
        atm.reload()

    def tearDown(self):
        self.cfg.stop()
        atm.reload()

    def _handlers(self, gateway=None, provider=None):
        return BigQmtRpcHandlers(
            account_id="123", market_data=FakeMarketData(),
            position_provider=provider or FakePositionProvider(),
            order_gateway=gateway, allow_order_methods=gateway is not None)

    def test_query_reads_the_book_the_request_named(self):
        query = _RecordingQuery()
        provider = BigQmtPositionProvider(query, account_type="STOCK")
        handlers = self._handlers(provider=provider)
        handlers.handle("query_stock_positions", {"account_id": "123", "account_type": "HUGANGTONG"})
        handlers.handle("query_stock_positions", {"account_id": "123"})
        handlers.handle("query_stock_positions", {"account_id": "123", "account_type": "FUTURE"})
        self.assertEqual([c[1] for c in query.calls], ["HUGANGTONG", "STOCK", "STOCK"])
        self.assertIsNone(get_request_account_type())  # scope released

    def test_ping_lists_every_type(self):
        handlers = self._handlers(gateway=_RecordingGateway())
        pong = handlers.handle("ping", {"account_id": "123"})
        self.assertEqual(pong["account_type"], "STOCK")
        self.assertEqual(pong["account_types"], ["STOCK", "HUGANGTONG", "SHENGANGTONG"])

    def test_order_settles_under_the_type_it_was_placed_as(self):
        gateway = _RecordingGateway()
        handlers = self._handlers(gateway=gateway)
        handlers.handle("submit_order", {
            "account_id": "123", "account_type": "HUGANGTONG", "stock_code": "00700.HK",
            "action": "BUY", "volume": 100, "price": 300.0, "wait_settlement": True})
        settlement = handlers.take_pending_settlement()
        self.assertIsNotNone(settlement)
        self.assertEqual(settlement.order_request.account_type, "HUGANGTONG")
        # The lookup later, on the adjust thread, outside any request scope.
        self.assertIsNone(get_request_account_type())
        handlers._apply_order_lookup(settlement, final=False)
        self.assertEqual(gateway.queried, [("123", "HUGANGTONG")])
        self.assertIsNone(get_request_account_type())

    def test_a_stock_order_still_settles_as_stock(self):
        gateway = _RecordingGateway()
        handlers = self._handlers(gateway=gateway)
        handlers.handle("submit_order", {
            "account_id": "123", "stock_code": "600000.SH",
            "action": "BUY", "volume": 100, "price": 10.0, "wait_settlement": True})
        settlement = handlers.take_pending_settlement()
        self.assertEqual(settlement.order_request.account_type, "STOCK")
        handlers._apply_order_lookup(settlement, final=False)
        self.assertEqual(gateway.queried, [("123", "STOCK")])


class _Client(object):
    def __init__(self):
        self.account_id = "123"
        self.timeout_seconds = 30.0
        self.calls = []

    def call(self, method, params=None, account_id=None, timeout_seconds=None, **kwargs):
        self.calls.append((method, dict(params or {})))
        if method == "query_stock_positions":
            return []
        if method == "query_stock_asset":
            return {"cash": 1.0}
        return {}


class ClientSideTest(unittest.TestCase):
    def test_with_account_type(self):
        self.assertEqual(_with_account_type({"account_id": "123"}, StockAccount("123", "HUGANGTONG")),
                         {"account_id": "123", "account_type": "HUGANGTONG"})
        self.assertEqual(_with_account_type({"account_id": "123"}, StockAccount("123")),
                         {"account_id": "123", "account_type": "STOCK"})
        self.assertEqual(_with_account_type({"account_id": "123"}, "123"), {"account_id": "123"})
        self.assertEqual(_with_account_type({"account_id": "123"}, None), {"account_id": "123"})

    def test_trade_rpcs_carry_the_declared_type(self):
        client = _Client()
        trader = BigQmtXtTrader(account_id="123")
        trader.client = client
        hk = StockAccount("123", "SHENGANGTONG")
        trader.query_stock_positions(hk)
        trader.query_stock_asset(hk)
        trader.query_stock_orders(hk)
        trader.query_stock_trades(hk)
        trader.cancel_order_stock(hk, "sys-1")
        sent = {m: p for m, p in client.calls}
        for method in ("query_stock_positions", "query_stock_asset", "query_stock_orders",
                       "query_stock_trades", "cancel_order_stock_sysid"):
            self.assertEqual(sent[method].get("account_type"), "SHENGANGTONG", method)
            self.assertEqual(sent[method].get("account_id"), "123", method)

    def test_bare_id_sends_no_type(self):
        client = _Client()
        trader = BigQmtXtTrader(account_id="123")
        trader.client = client
        trader.query_stock_positions("123")
        self.assertNotIn("account_type", client.calls[-1][1])

    def test_mismatch_warning_respects_the_server_list(self):
        trader = BigQmtXtTrader(account_id="123")
        trader._declared_account_type = "HUGANGTONG"
        with mock.patch("bigqmt_signal_trader.xtquant_compat.log") as fake_log:
            trader._note_server_account_type(
                {"account_type": "STOCK", "account_types": ["STOCK", "HUGANGTONG"]})
            self.assertFalse(fake_log.warning.called)
            trader._note_server_account_type({"account_type": "STOCK", "account_types": ["STOCK"]})
            self.assertTrue(fake_log.warning.called)


if __name__ == "__main__":
    unittest.main()
