"""subscribe_quote has to keep firing (issue #95).

It used to invoke the callback exactly once and never again -- a one-shot fetch
wearing a subscription's name. That is worse than not implementing it: a caller
written against MiniQMT sees data arrive and concludes it works, then waits
forever for the second update.

Ticks now ride the whole-quote push channel (a single code is a one-element
code list). K-lines have no server-side push -- the bridge only exposes
ContextInfo.subscribe_whole_quote, which carries ticks -- so they are polled
and emitted when the newest bar changes.
"""

import os
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import xtquant_compat as compat


class FakeSession(object):
    def __init__(self):
        self.active = set()
        self.subscribed = []
        self.started = False

    def start(self):
        self.started = True

    def subscribe_whole_quote(self, code_list, callback=None):
        self.subscribed.append((list(code_list), callback))
        sub_id = 700 + len(self.subscribed)
        self.active.add(sub_id)
        return sub_id

    def unsubscribe_quote(self, sub_id):
        self.active.discard(sub_id)
        return 0

    def has_subscription(self, sub_id):
        return sub_id in self.active

    def push(self, payload):
        """Deliver an update the way the real channel would."""
        for _codes, callback in self.subscribed:
            if callback is not None:
                callback(payload)


class FakeClient(object):
    def __init__(self, bars=None):
        self.account_id = "acct"
        self.local_cache_config = {}
        self.full_tick_cache_config = {"bar_poll_interval_seconds": 0.02}
        self.transport_name = "redis"
        self.calls = []
        self._bars = bars if bars is not None else [[{"time": 1, "close": 1.0}]]
        self._index = 0

    def call(self, method, params=None, **kwargs):
        self.calls.append(method)
        if method == "get_full_tick":
            return {c: {"lastPrice": 1.0} for c in (params or {}).get("codes") or []}
        if method == "get_market_data_ex":
            bars = self._bars[min(self._index, len(self._bars) - 1)]
            self._index += 1
            return {(params or {}).get("stock_list", ["x"])[0]: bars}
        return {}

    def save_quote_subscription(self, seq, payload, active=True):
        pass

    def publish_event(self, event_type, payload, **kwargs):
        pass

    def _redis(self):
        raise AssertionError("redis must not be needed here")


def _xtdata(client=None, session=None):
    data = compat.BigQmtXtData(client or FakeClient())
    if session is not None:
        data._quote_session_factory = lambda: session
    return data


class TickSubscriptionTest(unittest.TestCase):
    def test_tick_period_uses_the_push_session(self):
        session = FakeSession()
        data = _xtdata(session=session)

        data.subscribe_quote("600000.SH", period="tick")

        self.assertTrue(session.started)
        self.assertEqual(session.subscribed[0][0], ["600000.SH"])

    def test_the_callback_fires_more_than_once(self):
        """The whole point. Before, it fired once and stopped."""
        session = FakeSession()
        data = _xtdata(session=session)
        seen = []

        data.subscribe_quote("600000.SH", period="tick", callback=seen.append)
        session.push({"600000.SH": {"lastPrice": 10.1}})
        session.push({"600000.SH": {"lastPrice": 10.2}})

        self.assertGreaterEqual(len(seen), 3)  # 1 priming snapshot + 2 pushes

    def test_the_subscriber_is_primed_with_a_snapshot(self):
        """Whole-quote pushes only changed symbols, so a fresh subscriber would
        otherwise see nothing until the instrument next ticks."""
        session = FakeSession()
        data = _xtdata(session=session)
        seen = []

        data.subscribe_quote("600000.SH", period="tick", callback=seen.append)

        self.assertEqual(len(seen), 1)
        self.assertIn("600000.SH", seen[0])

    def test_unsubscribe_releases_the_session_handle(self):
        session = FakeSession()
        data = _xtdata(session=session)

        seq = data.subscribe_quote("600000.SH", period="tick")
        data.unsubscribe_quote(seq)

        self.assertNotIn(seq, session.active)

    def test_full_tick_is_treated_as_a_tick_period(self):
        session = FakeSession()
        data = _xtdata(session=session)

        data.subscribe_quote("600000.SH", period="full_tick")

        self.assertEqual(len(session.subscribed), 1)


class BarSubscriptionTest(unittest.TestCase):
    def _wait(self, predicate, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_a_new_bar_reaches_the_callback(self):
        client = FakeClient(bars=[
            [{"time": 1, "close": 1.0}],
            [{"time": 1, "close": 1.0}, {"time": 2, "close": 1.1}],
        ])
        data = _xtdata(client)
        seen = []

        seq = data.subscribe_quote("600000.SH", period="1m", callback=seen.append)
        try:
            self.assertTrue(self._wait(lambda: len(seen) >= 2),
                            "second bar never delivered: %r" % (seen,))
        finally:
            data.unsubscribe_quote(seq)

    def test_an_unchanged_bar_is_not_re_emitted(self):
        client = FakeClient(bars=[[{"time": 1, "close": 1.0}]])
        data = _xtdata(client)
        seen = []

        seq = data.subscribe_quote("600000.SH", period="1m", callback=seen.append)
        try:
            time.sleep(0.3)   # several poll intervals
            self.assertEqual(len(seen), 1, "re-emitted an unchanged bar")
        finally:
            data.unsubscribe_quote(seq)

    def test_unsubscribe_stops_the_polling_thread(self):
        client = FakeClient()
        data = _xtdata(client)

        seq = data.subscribe_quote("600000.SH", period="1m", callback=lambda d: None)
        data.unsubscribe_quote(seq)
        before = len(client.calls)
        time.sleep(0.3)

        self.assertEqual(len(client.calls), before, "poller kept running")

    def test_a_raising_callback_does_not_end_the_subscription(self):
        """A subscriber's bug must not silently unsubscribe them."""
        client = FakeClient(bars=[
            [{"time": 1}], [{"time": 2}], [{"time": 3}],
        ])
        data = _xtdata(client)
        calls = []

        def boom(_data):
            calls.append(1)
            raise RuntimeError("subscriber bug")

        seq = data.subscribe_quote("600000.SH", period="1m", callback=boom)
        try:
            self.assertTrue(self._wait(lambda: len(calls) >= 2),
                            "polling stopped after the first raise")
        finally:
            data.unsubscribe_quote(seq)

    def test_a_failing_fetch_does_not_end_the_subscription(self):
        class _Flaky(FakeClient):
            def call(self, method, params=None, **kwargs):
                self.calls.append(method)
                if method == "get_market_data_ex" and len(self.calls) < 3:
                    raise RuntimeError("transport hiccup")
                return FakeClient.call(self, method, params, **kwargs)

        client = _Flaky(bars=[[{"time": 9, "close": 2.0}]])
        data = _xtdata(client)
        seen = []

        seq = data.subscribe_quote("600000.SH", period="1d", callback=seen.append)
        try:
            self.assertTrue(self._wait(lambda: len(seen) >= 1),
                            "never recovered from a transient fetch failure")
        finally:
            data.unsubscribe_quote(seq)

    def test_poll_interval_comes_from_config(self):
        client = FakeClient()
        client.full_tick_cache_config = {"bar_poll_interval_seconds": 7.5}

        self.assertEqual(_xtdata(client)._bar_poll_interval_seconds(), 7.5)

    def test_poll_interval_falls_back_to_the_default(self):
        client = FakeClient()
        client.full_tick_cache_config = {}
        saved = os.environ.pop("BIGQMT_BAR_POLL_INTERVAL_SECONDS", None)
        try:
            self.assertEqual(_xtdata(client)._bar_poll_interval_seconds(),
                             compat.DEFAULT_BAR_POLL_INTERVAL_SECONDS)
        finally:
            if saved is not None:
                os.environ["BIGQMT_BAR_POLL_INTERVAL_SECONDS"] = saved

    def test_pollers_run_as_daemon_threads(self):
        """They must never hold the process open."""
        client = FakeClient()
        data = _xtdata(client)
        before = set(threading.enumerate())

        seq = data.subscribe_quote("600000.SH", period="1m", callback=lambda d: None)
        try:
            new = [t for t in threading.enumerate() if t not in before]
            self.assertTrue(new, "no polling thread started")
            self.assertTrue(all(t.daemon for t in new))
        finally:
            data.unsubscribe_quote(seq)

    def test_stop_all_subscriptions_clears_every_poller(self):
        data = _xtdata(FakeClient())
        for _ in range(3):
            data.subscribe_quote("600000.SH", period="1m", callback=lambda d: None)

        self.assertEqual(data.stop_all_subscriptions(), 3)
        self.assertEqual(data.stop_all_subscriptions(), 0)


class BookkeepingTest(unittest.TestCase):
    def test_a_redis_failure_does_not_break_the_subscription(self):
        """Bookkeeping needs a Redis client; a zmq deployment has none, and
        nothing on the server consumes these events anyway."""
        class _Hostile(FakeClient):
            def save_quote_subscription(self, seq, payload, active=True):
                raise RuntimeError("no redis here")

            def publish_event(self, event_type, payload, **kwargs):
                raise RuntimeError("no redis here")

        session = FakeSession()
        data = _xtdata(_Hostile(), session)

        seq = data.subscribe_quote("600000.SH", period="tick")

        self.assertIn(seq, session.active)
        self.assertEqual(data.unsubscribe_quote(seq), 0)


if __name__ == "__main__":
    unittest.main()


class NoDataDiagnosticTest(unittest.TestCase):
    """A period this terminal has no bars for produces a live subscription that
    never fires. Found live: 1m and 5m return empty frames here while 1d and
    tick return data, so a caller subscribing to 1m sees exactly what a broken
    subscription looks like.
    """

    def _wait(self, predicate, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_an_empty_period_is_reported(self):
        client = FakeClient(bars=[[]])
        data = _xtdata(client)
        import logging

        with self.assertLogs("bigqmt.xtquant_compat", level=logging.WARNING) as caught:
            # Other asynchronous pollers share this logger. Their warnings
            # must not end the wait and cancel this subscription prematurely.
            logging.getLogger("bigqmt.xtquant_compat").warning("unrelated fixture warning")
            seq = data.subscribe_quote("600000.SH", period="1m",
                                       callback=lambda d: None)
            try:
                self.assertTrue(self._wait(lambda: any(
                    "no 1m bars for 600000.SH" in message for message in caught.output)))
            finally:
                data.unsubscribe_quote(seq)

        message = "".join(caught.output)
        self.assertIn("no 1m bars", message)
        self.assertIn("600000.SH", message)

    def test_it_is_reported_once_not_every_poll(self):
        poller = compat._BarPoller(lambda: {}, None, 0.02,
                                   on_no_data=lambda: reports.append(1))
        reports = []
        poller.start()
        try:
            time.sleep(0.3)   # many intervals
        finally:
            poller.stop()

        self.assertEqual(len(reports), 1, "reported %d times" % len(reports))

    def test_data_appearing_later_still_fires_the_callback(self):
        """The subscription must stay live through the quiet period."""
        client = FakeClient(bars=[[], [], [{"time": 1, "close": 1.0}]])
        data = _xtdata(client)
        seen = []

        seq = data.subscribe_quote("600000.SH", period="1m", callback=seen.append)
        try:
            self.assertTrue(self._wait(lambda: len(seen) >= 1),
                            "never recovered once data appeared")
        finally:
            data.unsubscribe_quote(seq)

    def test_a_period_with_data_reports_nothing(self):
        client = FakeClient(bars=[[{"time": 1, "close": 1.0}]])
        data = _xtdata(client)
        reports = []

        poller = compat._BarPoller(
            lambda: client.call("get_market_data_ex", {"stock_list": ["x"]}),
            None, 0.02, on_no_data=lambda: reports.append(1))
        poller.start()
        try:
            time.sleep(0.2)
        finally:
            poller.stop()

        self.assertEqual(reports, [])
