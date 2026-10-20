# coding: utf-8
"""Batch unsubscribe by sub_ids: one RPC, one topic re-sync, honest report (#418).

Reported by @shengyy: exiting with 26 whole-quote handles paid 26 RPCs and
25 push-subscriber rebuilds and still had items left after ~5s. The new
path releases only this client's named handles server-side (shared combos
stay up for their other subscribers; quote_unsubscribe_all stays the last
resort), with unknown ids and native unsubscribe errors named in the report
instead of swallowed-as-released.
"""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bigqmt_signal_trader.quote_subscription_manager import (  # noqa: E402
    QuoteSubscriptionManager,
)
from test_whole_quote_e2e import (  # noqa: E402  -- the established fakes
    ClientPushChannel,
    FakeQuoteSource,
    InProcPushBus,
)
from bigqmt_signal_trader.whole_quote_session import WholeQuoteClientSession  # noqa: E402


class _CountingChannel(ClientPushChannel):
    def __init__(self, bus):
        super().__init__(bus)
        self.starts = 0
        self.stops = 0

    def start_subscriber(self, topics, on_msg):
        self.starts += 1
        super().start_subscriber(topics, on_msg)

    def stop(self):
        self.stops += 1


def _session(bus, rpc_calls, channel, client_id="clientA"):
    seen = set()  # what a real manager would hold

    def rpc_call(method, params):
        rpc_calls.append((method, dict(params)))
        if method == "unsubscribe_whole_quote_batch":
            released = [str(s) for s in (params.get("sub_ids") or []) if s in seen]
            unknown = [str(s) for s in (params.get("sub_ids") or []) if s not in seen]
            seen.difference_update(released)
            return {"released": released, "unknown": unknown, "native_errors": []}
        if method == "subscribe_whole_quote":
            seen.add(str(params.get("sub_id")))
        return {"topic": ",".join(params.get("codes") or [])}

    return WholeQuoteClientSession(
        rpc_call=rpc_call, push_channel=channel, client_id=client_id,
        heartbeat_interval_seconds=60.0)


class SessionBatchTest(unittest.TestCase):
    def test_n_handles_one_rpc_one_resync(self):
        bus = InProcPushBus()
        channel = _CountingChannel(bus)
        rpc_calls = []
        session = _session(bus, rpc_calls, channel)
        seqs = [session.subscribe_whole_quote(["60051%d.SH" % i], callback=lambda d: None)
                for i in range(5)]
        starts_before = channel.starts
        stops_before = channel.stops

        report = session.unsubscribe_quotes(seqs)

        batch_calls = [c for c in rpc_calls
                       if c[0] == "unsubscribe_whole_quote_batch"]
        self.assertEqual(len(batch_calls), 1)
        self.assertEqual(batch_calls[0][1]["sub_ids"], [str(s) for s in seqs])
        self.assertEqual(report["released"], [str(s) for s in seqs])
        # ONE re-sync for the whole batch, not one per item: with every topic
        # gone the sync is a single stop and no rebuild.
        self.assertEqual(channel.stops - stops_before, 1)
        self.assertEqual(channel.starts - starts_before, 0)
        for seq in seqs:
            self.assertFalse(session.has_subscription(seq))

    def test_empty_and_double_call_are_clean_noops(self):
        bus = InProcPushBus()
        channel = _CountingChannel(bus)
        session = _session(bus, [], channel)

        report = session.unsubscribe_quotes([])
        self.assertEqual(report, {"released": [], "unknown": [], "native_errors": []})
        report = session.unsubscribe_quotes([12345])
        self.assertEqual(report["released"], [])  # server sees it, names it unknown-able


class ManagerBatchTest(unittest.TestCase):
    def _manager(self, source=None):
        return QuoteSubscriptionManager(source or FakeQuoteSource()), source

    def test_shared_combo_closes_only_when_the_last_ref_leaves(self):
        source = FakeQuoteSource()
        manager, _ = self._manager(source)
        manager.subscribe("clientA", "1", ["600519.SH"])
        manager.subscribe("clientB", "2", ["600519.SH"])
        self.assertEqual(len(source.subscriptions), 1)

        report = manager.unsubscribe_batch("clientA", ["1"])
        self.assertEqual(report["released"], ["1"])
        self.assertEqual(source.unsubscribed, [],
                         "clientB still holds the combo -- no native unsubscribe yet")

        report = manager.unsubscribe_batch("clientB", ["2"])
        self.assertEqual(len(source.unsubscribed), 1)

    def test_unknown_ids_named_not_counted(self):
        manager, _ = self._manager()
        manager.subscribe("clientA", "1", ["600519.SH"])

        report = manager.unsubscribe_batch("clientA", ["1", "ghost"])

        self.assertEqual(report["released"], ["1"])
        self.assertEqual(report["unknown"], ["ghost"])

    def test_native_errors_are_reported_not_swallowed(self):
        class _RefusingSource(FakeQuoteSource):
            def unsubscribe_strict(self, handle):
                raise RuntimeError("terminal refused")

        source = _RefusingSource()
        manager, _ = self._manager(source)
        manager.subscribe("clientA", "1", ["600519.SH"])

        report = manager.unsubscribe_batch("clientA", ["1"])

        self.assertEqual(report["released"], ["1"])
        self.assertTrue(report["native_errors"], report)
        self.assertIn("terminal refused", report["native_errors"][0])

    def test_batch_releases_multiple_combos_once_each(self):
        source = FakeQuoteSource()
        manager, _ = self._manager(source)
        manager.subscribe("clientA", "1", ["600519.SH"])
        manager.subscribe("clientA", "2", ["000001.SZ"])
        manager.subscribe("clientA", "3", ["601398.SH"])

        report = manager.unsubscribe_batch("clientA", ["1", "2", "3"])

        self.assertEqual(sorted(report["released"]), ["1", "2", "3"])
        self.assertEqual(len(source.unsubscribed), 3)
        self.assertEqual(manager.status()["combos"], [])


class ReplayInterleaveTest(unittest.TestCase):
    def test_replay_does_not_resurrect_a_batch_released_handle(self):
        bus = InProcPushBus()
        channel = _CountingChannel(bus)
        rpc_calls = []
        session = _session(bus, rpc_calls, channel)
        s1 = session.subscribe_whole_quote(["600519.SH"], callback=lambda d: None)
        s2 = session.subscribe_whole_quote(["000001.SZ"], callback=lambda d: None)
        rpc_calls.clear()

        # The batch release lands after the replay snapshot, before the loop.
        session.unsubscribe_quotes([s2])
        session.replay_subscriptions()

        resubscribed = [c[1].get("sub_id") for c in rpc_calls
                        if c[0] == "subscribe_whole_quote"]
        self.assertIn(s1, resubscribed, "the kept handle must replay")
        self.assertNotIn(s2, resubscribed,
                         "a handle released before the replay loop must not be re-created")


if __name__ == "__main__":
    unittest.main()
