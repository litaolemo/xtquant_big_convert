# coding: utf-8
"""#231: a replay RPC failure must never kill the heartbeat loop, and start()
must recover a dead one.

Reported against 0.3.26: replay_subscriptions() lets RPC exceptions escape
_heartbeat_loop; the thread dies but _started stays True, so start() never
restarts it. Whole-quote subscriptions then receive no keepalives and age
out server-side while the client believes it is still subscribed.
"""
import threading
import time
import unittest
from unittest import mock

import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.whole_quote_session import WholeQuoteClientSession


def _session_with_failing_rpc():
    calls = []

    def offline_rpc(method, params):
        calls.append(method)
        raise RuntimeError("QMT unavailable (offline stub)")

    session = WholeQuoteClientSession(offline_rpc, None, "offline-test")
    session._subscriptions[1] = {"topic": "TEST", "codes": ["TEST"], "callback": None}
    return session, calls


class ReplayFailureDoesNotKillTheLoopTest(unittest.TestCase):
    def test_partial_replay_retries_even_while_another_subscription_pushes(self):
        attempts = {"A": 0, "B": 0}
        active = set()
        rounds = [0]

        def rpc(method, params):
            code = params["sub_id"]
            if method == "subscribe_whole_quote":
                attempts[code] += 1
                if code == "B" and attempts[code] == 1:
                    raise ConnectionError("B replay temporarily unavailable")
                active.add(code)
            if "A" in active:
                session._on_push("A", {"A": {"lastPrice": 1}})
            return {}

        session = WholeQuoteClientSession(rpc, None, "offline-test", push_silence_replay_heartbeats=2)
        session._subscriptions = {code: {"topic": code, "codes": [code], "callback": None} for code in attempts}
        # A server reset requires replay of both existing subscriptions.
        with self.assertRaises(ConnectionError):
            session.replay_subscriptions()

        def fake_sleep(_seconds):
            rounds[0] += 1
            if rounds[0] >= 10:
                session._started = False

        session._started = True
        with mock.patch("time.sleep", fake_sleep):
            session._heartbeat_loop()
        self.assertEqual(active, {"A", "B"})
        self.assertEqual(attempts, {"A": 2, "B": 2})  # Stop replaying once the batch succeeds.

    def test_the_loop_survives_a_failing_replay(self):
        session, calls = _session_with_failing_rpc()
        rounds = [0]

        def fake_sleep(_seconds):
            rounds[0] += 1
            if rounds[0] >= 25:
                session._started = False       # end the loop after enough rounds

        session._started = True
        with mock.patch("time.sleep", fake_sleep):
            session._heartbeat_loop()          # must NOT raise out

        self.assertGreater(calls.count("quote_keepalive"), 0)
        self.assertGreaterEqual(calls.count("subscribe_whole_quote"), 1,
                                "the replay was attempted (and failed safely)")

    def test_start_recovers_a_dead_heartbeat_thread(self):
        session, _calls = _session_with_failing_rpc()
        session._started = True
        session._heartbeat_thread = threading.Thread(
            target=lambda: None, name="bigqmt-quote-keepalive", daemon=True)
        session._heartbeat_thread.start()
        time.sleep(0.05)
        self.assertFalse(session._heartbeat_thread.is_alive())

        with mock.patch("time.sleep", lambda _s: setattr(session, "_started", False)):
            session.start()
            self.assertTrue(session._heartbeat_thread.is_alive())
            session._started = False
            session._heartbeat_thread.join(timeout=2.0)

    def test_start_is_a_no_op_on_a_live_thread(self):
        session, _calls = _session_with_failing_rpc()
        session._started = True
        marker = []
        session._heartbeat_thread = threading.Thread(
            target=lambda: marker.append(1) or time.sleep(1),
            name="bigqmt-quote-keepalive", daemon=True)
        session._heartbeat_thread.start()
        first = session._heartbeat_thread

        session.start()

        self.assertIs(session._heartbeat_thread, first)
        session._started = False


if __name__ == "__main__":
    unittest.main()
