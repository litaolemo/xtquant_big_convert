# coding: utf-8
"""方式一 multi-account: a secondary account's on_stock_order / on_stock_trade
(#320 by @JinHaoran, #322 by @shihaibi -- the same defect, twice).

Two gates kept the secondary's events from its client, and #322 named both:

  1. QMT's order_callback / deal_callback fire for the bound (primary)
     account only. Nothing in the process ever sees a secondary fill.
  2. Even an event that did arrive was published under the CONFIGURED
     account -- the channel is per account, so the secondary's client, and
     the primary's, both heard nothing.

Gate 2 is a routing fix: the event's own account picks the channel. Gate 1
has no callback to fix, so the secondaries are polled: get_trade_detail_data
answers for any account, on the adjust thread, and what changed between
polls becomes the events the callbacks would have carried.
"""

import os
import sys
import types
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import exec_events  # noqa: E402
from bigqmt_signal_trader.secondary_exec_poll import (  # noqa: E402
    SecondaryExecPoller,
    build_row_publisher,
)


class _Row(object):
    def __init__(self, **fields):
        self.__dict__.update(fields)


def _order(sysid, status=50, traded=0, account="SEC", code="600000", remark="r"):
    return _Row(m_strOrderSysID=sysid, m_nOrderStatus=status, m_nVolumeTraded=traded,
                m_strAccountID=account, m_strInstrumentID=code, m_strExchangeID="SH",
                m_strRemark=remark, m_nVolumeTotalOriginal=100, m_dLimitPrice=10.0,
                m_nOffsetFlag=48, m_strInsertTime="093000")


def _deal(trade_id, sysid="1", account="SEC", volume=100, price=10.0):
    return _Row(m_strTradeID=trade_id, m_strOrderSysID=sysid, m_strAccountID=account,
                m_strInstrumentID="600000", m_strExchangeID="SH", m_nVolume=volume,
                m_dPrice=price, m_nOffsetFlag=48, m_strTradeTime="093001")


class _Terminal(object):
    """get_trade_detail_data stand-in: per-account ORDER / DEAL lists."""

    def __init__(self):
        self.rows = {}
        self.calls = []

    def __call__(self, account, kind):
        self.calls.append((account, kind))
        return list(self.rows.get((account, kind), []))


class _Clock(object):
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _poller(terminal, accounts=("SEC",), interval=1.0):
    published = []
    clock = _Clock()
    poller = SecondaryExecPoller(
        accounts, terminal, lambda kind, acct, row: published.append((kind, acct, row)),
        interval_seconds=interval, time_func=clock)
    return poller, published, clock


class PollerTest(unittest.TestCase):
    def test_the_first_poll_is_a_baseline_and_publishes_nothing(self):
        """A restart must not replay the day's orders as fresh callbacks."""
        terminal = _Terminal()
        terminal.rows[("SEC", "ORDER")] = [_order("1", status=56, traded=100)]
        terminal.rows[("SEC", "DEAL")] = [_deal("t1")]
        poller, published, _clock = _poller(terminal)

        self.assertEqual(0, poller.poll())
        self.assertEqual([], published)

    def test_a_new_order_after_the_baseline_is_published(self):
        terminal = _Terminal()
        poller, published, clock = _poller(terminal)
        poller.poll()
        terminal.rows[("SEC", "ORDER")] = [_order("11355")]
        clock.t += 1.0

        self.assertEqual(1, poller.poll())
        kind, acct, row = published[0]
        self.assertEqual(("order", "SEC", "11355"), (kind, acct, row.m_strOrderSysID))

    def test_a_status_change_is_published_once_per_change(self):
        terminal = _Terminal()
        poller, published, clock = _poller(terminal)
        poller.poll()
        terminal.rows[("SEC", "ORDER")] = [_order("1", status=50)]
        clock.t += 1; poller.poll()
        clock.t += 1; poller.poll()                      # unchanged: nothing
        terminal.rows[("SEC", "ORDER")] = [_order("1", status=55, traded=40)]
        clock.t += 1; poller.poll()
        terminal.rows[("SEC", "ORDER")] = [_order("1", status=56, traded=100)]
        clock.t += 1; poller.poll()

        self.assertEqual([("order", 50), ("order", 55), ("order", 56)],
                         [(k, r.m_nOrderStatus) for k, _, r in published])

    def test_a_fill_is_published_exactly_once(self):
        terminal = _Terminal()
        poller, published, clock = _poller(terminal)
        poller.poll()
        terminal.rows[("SEC", "DEAL")] = [_deal("t1")]
        clock.t += 1; poller.poll()
        terminal.rows[("SEC", "DEAL")] = [_deal("t1"), _deal("t2")]
        clock.t += 1; poller.poll()
        clock.t += 1; poller.poll()

        self.assertEqual(["t1", "t2"], [r.m_strTradeID for _, _, r in published])

    def test_a_fill_without_a_trade_id_is_keyed_by_what_it_is(self):
        terminal = _Terminal()
        poller, published, clock = _poller(terminal)
        poller.poll()
        fill = _deal("", sysid="9", volume=100, price=10.0)
        terminal.rows[("SEC", "DEAL")] = [fill]
        clock.t += 1; poller.poll()
        clock.t += 1; poller.poll()
        self.assertEqual(1, len(published))

    def test_a_row_without_a_sysid_waits_for_the_next_poll(self):
        """The pre-sysid twin (#152 / #161): not emitted, not remembered."""
        terminal = _Terminal()
        poller, published, clock = _poller(terminal)
        poller.poll()
        terminal.rows[("SEC", "ORDER")] = [_order("")]
        clock.t += 1; poller.poll()
        self.assertEqual([], published)
        terminal.rows[("SEC", "ORDER")] = [_order("77")]
        clock.t += 1; poller.poll()
        self.assertEqual(["77"], [r.m_strOrderSysID for _, _, r in published])

    def test_the_interval_is_honoured(self):
        terminal = _Terminal()
        poller, published, clock = _poller(terminal, interval=1.0)
        poller.poll()
        clock.t += 0.3
        self.assertEqual(0, poller.poll())
        self.assertEqual(2, len(terminal.calls), "polled again inside the interval")
        clock.t += 0.8
        poller.poll()
        self.assertEqual(4, len(terminal.calls))

    def test_each_secondary_is_polled_and_routed_by_its_own_account(self):
        terminal = _Terminal()
        poller, published, clock = _poller(terminal, accounts=("SEC1", "SEC2"))
        poller.poll()
        terminal.rows[("SEC1", "ORDER")] = [_order("a", account="SEC1")]
        terminal.rows[("SEC2", "ORDER")] = [_order("b", account="SEC2")]
        clock.t += 1; poller.poll()
        self.assertEqual([("SEC1", "a"), ("SEC2", "b")],
                         sorted((acct, r.m_strOrderSysID) for _, acct, r in published))

    def test_a_failing_query_is_logged_and_does_not_stop_the_others(self):
        terminal = _Terminal()
        logs = []

        def flaky(account, kind):
            if account == "SEC1":
                raise RuntimeError("boom")
            return terminal(account, kind)

        clock = _Clock()
        published = []
        poller = SecondaryExecPoller(("SEC1", "SEC2"), flaky,
                                     lambda k, a, r: published.append((k, a, r)),
                                     time_func=clock, log=logs.append)
        poller.poll()
        terminal.rows[("SEC2", "ORDER")] = [_order("b", account="SEC2")]
        clock.t += 1; poller.poll()
        self.assertEqual(["b"], [r.m_strOrderSysID for _, _, r in published])
        self.assertTrue(logs and "SEC***" in logs[0] or "SEC" in logs[0])

    def test_a_failing_publish_does_not_lose_the_next_event(self):
        terminal = _Terminal()
        clock = _Clock()
        seen = []

        def publish(kind, acct, row):
            seen.append(row.m_strOrderSysID)
            if row.m_strOrderSysID == "bad":
                raise RuntimeError("sink down")

        poller = SecondaryExecPoller(("SEC",), terminal, publish, time_func=clock)
        poller.poll()
        terminal.rows[("SEC", "ORDER")] = [_order("bad"), _order("good")]
        clock.t += 1
        self.assertEqual(1, poller.poll())
        self.assertEqual(["bad", "good"], seen)

    def test_status_is_readable(self):
        terminal = _Terminal()
        poller, _published, _clock = _poller(terminal, accounts=("SECRET1",))
        status = poller.status()
        self.assertEqual(["SEC***"], status["accounts"])
        self.assertEqual(0, status["published"])


class _Sink(object):
    """A redis stand-in that records channel names only."""

    def __init__(self):
        self.channels = []

    def xadd(self, key, fields, maxlen=None, approximate=True):
        self.channels.append(("stream", key))

    def publish(self, channel, payload):
        self.channels.append(("pubsub", channel))

    def expire(self, key, seconds):
        pass

    def get(self, key):
        return None


class RowPublisherTest(unittest.TestCase):
    def test_an_order_row_lands_on_the_secondarys_channels(self):
        sink = _Sink()
        publish = build_row_publisher(sink)
        publish("order", "SEC", _order("1"))
        names = {c for _, c in sink.channels}
        self.assertTrue(any(":SEC" in c for c in names), names)
        self.assertFalse(any(":PRI" in c for c in names), names)

    def test_a_trade_row_is_a_trade_event(self):
        sink = _Sink()
        events = []
        with mock.patch.object(exec_events, "publish_exec_event",
                               lambda s, a, e: events.append((a, e))):
            build_row_publisher(sink)("trade", "SEC", _deal("t9", sysid="1"))
        account, event = events[0]
        self.assertEqual("SEC", account)
        self.assertEqual(exec_events.EVENT_TRADE, event["event_type"])
        self.assertEqual("t9", event["trade_id"])
        self.assertEqual("poll", event["source"])

    def test_a_junk_order_also_raises_on_order_error(self):
        events = []
        with mock.patch.object(exec_events, "publish_exec_event",
                               lambda s, a, e: events.append(e["event_type"])):
            build_row_publisher(_Sink())("order", "SEC", _order("1", status=57))
        self.assertEqual([exec_events.EVENT_ORDER, exec_events.EVENT_ORDER_ERROR], events)

    def test_the_instrument_name_comes_from_the_context(self):
        class Ctx(object):
            def get_stock_name(self, code):
                return "浦发银行"

        events = []
        with mock.patch.object(exec_events, "publish_exec_event",
                               lambda s, a, e: events.append(e)):
            build_row_publisher(_Sink(), context_info=Ctx())("order", "SEC", _order("1"))
        self.assertEqual("浦发银行", events[0]["instrument_name"])


class StrategyRoutesByTheEventsAccountTest(unittest.TestCase):
    """Gate 2: the callback path publishes under the row's own account."""

    def setUp(self):
        import bigqmt_signal_trader_strategy as strategy
        self.strategy = strategy
        self.saved = {name: getattr(strategy, name) for name in
                      ("_build_config", "_exec_event_sink", "_exec_events", "_account_id")}
        self.published = []

        class Fake(object):
            normalize_order_event = staticmethod(exec_events.normalize_order_event)
            normalize_trade_event = staticmethod(exec_events.normalize_trade_event)
            normalize_order_error_event = staticmethod(exec_events.normalize_order_error_event)
            format_raw_snapshot = staticmethod(exec_events.format_raw_snapshot)
            raw_field_snapshot = staticmethod(exec_events.raw_field_snapshot)

            def publish_exec_event(self_, sink, account_id, event):
                self.published.append((account_id, event["account_id"]))

        strategy._build_config = lambda: {"account_id": "PRI", "exec_events": {"enabled": True, "account_id": "PRI"}}
        strategy._exec_event_sink = lambda config: object()
        strategy._exec_events = Fake()
        strategy._account_id = "PRI"

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(self.strategy, name, value)

    def test_a_secondary_accounts_callback_goes_to_the_secondarys_channel(self):
        self.strategy._publish_exec_event("trade", _deal("t1", account="SEC"))
        self.assertEqual([("SEC", "SEC")], self.published)

    def test_the_primarys_own_events_are_unchanged(self):
        self.strategy._publish_exec_event("trade", _deal("t1", account="PRI"))
        self.assertEqual([("PRI", "PRI")], self.published)

    def test_a_row_without_an_account_falls_back_to_the_configured_one(self):
        row = _deal("t1")
        del row.m_strAccountID
        self.strategy._publish_exec_event("trade", row)
        self.assertEqual([("PRI", "PRI")], self.published)


class ManagerWiringTest(unittest.TestCase):
    def test_the_manager_polls_after_draining(self):
        from bigqmt_signal_trader.multi_account import MultiAccountRpcServiceManager

        class Service(object):
            account_id = "PRI"
            redis = listen_redis = object()

            def drain_pending(self, max_items=20, budget_seconds=None):
                return 0

        polls = []

        class Poller(object):
            def poll(self):
                polls.append(1)

            def status(self):
                return {"x": 1}

        manager = MultiAccountRpcServiceManager([Service()], handlers=object(), exec_poller=Poller())
        manager.drain_pending(20)
        manager.drain_pending(20, budget_seconds=0.5)
        self.assertEqual(2, len(polls))
        self.assertEqual({"x": 1}, manager.secondary_exec_poll_status())

    def test_the_builder_wires_the_secondaries_into_a_poller(self):
        from bigqmt_signal_trader.account_type_map import reload as reload_map
        from bigqmt_signal_trader.multi_account import build_multi_account_rpc_service

        class Gateway(object):
            def query_native_rows(self, account_id, kind, strategy_name=""):
                return []

        class Handlers(object):
            order_gateway = Gateway()

        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"PRI": "STOCK", "SEC": "CREDIT"}
        with mock.patch.dict("sys.modules", {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            primary = mock.MagicMock()
            primary.account_id = "PRI"
            primary.redis = _Sink()
            primary.listen_redis = "listen"
            primary.handlers = Handlers()
            with mock.patch("bigqmt_signal_trader.multi_account._build_secondary",
                            side_effect=lambda p, aid, cfg: types.SimpleNamespace(account_id=aid)):
                manager = build_multi_account_rpc_service(
                    None, None, {"rpc": {"secondary_exec_poll_seconds": 0.5}}, lambda c, a, cfg: primary)
        reload_map()
        self.assertIsNotNone(manager.exec_poller)
        self.assertEqual(["SEC"], manager.exec_poller.accounts)
        self.assertEqual(0.5, manager.exec_poller.interval_seconds)

    def test_a_zero_interval_disables_the_poller(self):
        from bigqmt_signal_trader.multi_account import _build_secondary_exec_poller
        primary = types.SimpleNamespace(redis=_Sink(), handlers=types.SimpleNamespace(order_gateway=object()))
        self.assertIsNone(_build_secondary_exec_poller(
            None, primary, ["SEC"], {"rpc": {"secondary_exec_poll_seconds": 0}}))

    def test_exec_events_off_disables_the_poller(self):
        from bigqmt_signal_trader.multi_account import _build_secondary_exec_poller
        primary = types.SimpleNamespace(redis=_Sink(), handlers=types.SimpleNamespace(order_gateway=object()))
        self.assertIsNone(_build_secondary_exec_poller(
            None, primary, ["SEC"], {"exec_events": {"enabled": False}}))


class GatewayNativeRowsTest(unittest.TestCase):
    def test_it_asks_the_terminal_with_the_accounts_type(self):
        from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway
        calls = []
        gateway = BigQmtOrderGateway(
            account_id="PRI", passorder_func=None, context_info=object(),
            get_trade_detail_data_func=lambda *a: calls.append(a) or ["row"])
        rows = gateway.query_native_rows("SEC", "deal")
        self.assertEqual(["row"], rows)
        account, account_type, kind, name = calls[0]
        self.assertEqual(("SEC", "DEAL", ""), (account, kind, name))
        self.assertTrue(account_type)


if __name__ == "__main__":
    unittest.main()
