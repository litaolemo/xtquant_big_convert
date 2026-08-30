"""Stopping a backtest must free its port (issue #109).

The reporter's sequence: run the in-QMT backtest script, press the stop button
next to the chart, run it again -- and the second run fails to bind.

Two things kept the port held. ``stop()`` told the engine the run was over and
left the ZMQ service running, so nothing ever asked the socket to close. And
``stop_server()`` only set a flag: the serving thread notices up to poll_ms
later and closes the socket after that, while ``init()`` goes straight on to
bind the next one. The endpoint is a fixed port (16662), so there is no
fallback to a free one -- it is EADDRINUSE or nothing.
"""

import os
import sys
import threading
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from bigqmt_backtest.zmq_server import ZmqBacktestServer


class FakeProtocol(object):
    class engine(object):
        class config(object):
            run_id = "test-run"

    def handle(self, request):
        return {"ok": True}


def _server(endpoint):
    return ZmqBacktestServer(FakeProtocol(), endpoint=endpoint, poll_ms=10)


def _serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


class StoppedEventTest(unittest.TestCase):
    """The signal a re-run has to wait on."""

    def test_it_is_not_set_while_serving(self):
        server = _server("tcp://127.0.0.1:0")
        _serve(server)
        self.addCleanup(server.stop)
        self.assertTrue(server.wait_until_ready(5.0))

        self.assertFalse(server.wait_until_stopped(0.05))

    def test_it_is_set_once_the_socket_is_closed(self):
        server = _server("tcp://127.0.0.1:0")
        thread = _serve(server)
        self.assertTrue(server.wait_until_ready(5.0))

        server.stop()

        self.assertTrue(server.wait_until_stopped(5.0))
        thread.join(5.0)
        self.assertFalse(thread.is_alive())

    def test_a_server_that_never_bound_still_reports_stopped(self):
        """Two servers on one port: the loser must not leave a caller waiting
        forever for a socket it never opened."""
        first = _server("tcp://127.0.0.1:0")
        _serve(first)
        self.assertTrue(first.wait_until_ready(5.0))
        self.addCleanup(first.stop)

        second = _server(first.actual_endpoint)
        _serve(second)

        self.assertTrue(second.wait_until_stopped(5.0))

    def test_a_failed_bind_keeps_its_reason(self):
        """"failed to bind" on its own sends people looking in the wrong
        place; "Address in use" says which run to go and stop."""
        first = _server("tcp://127.0.0.1:0")
        _serve(first)
        self.assertTrue(first.wait_until_ready(5.0))
        self.addCleanup(first.stop)

        second = _server(first.actual_endpoint)
        _serve(second)
        self.assertTrue(second.wait_until_stopped(5.0))

        self.assertIsNotNone(second.bind_error)
        self.assertIsNone(second.actual_endpoint)


class RebindTest(unittest.TestCase):
    """The reporter's sequence, end to end."""

    def test_the_port_is_free_after_a_stop(self):
        first = _server("tcp://127.0.0.1:0")
        _serve(first)
        self.assertTrue(first.wait_until_ready(5.0))
        endpoint = first.actual_endpoint

        first.stop()
        self.assertTrue(first.wait_until_stopped(5.0))

        second = _server(endpoint)
        _serve(second)
        self.addCleanup(second.stop)

        self.assertTrue(second.wait_until_ready(5.0),
                        "rebinding %s failed after a clean stop" % endpoint)
        self.assertEqual(second.actual_endpoint, endpoint)


class StopHookTest(unittest.TestCase):
    """QMT's stop button reaches stop(); it has to reach the server too."""

    def test_stop_calls_stop_server(self):
        import bigqmt_backtest.qmt_runtime as runtime

        calls = []

        class FakeRuntime(object):
            def on_qmt_stop(self):
                calls.append("engine")

            def stop_server(self, timeout_seconds=5.0):
                calls.append("server")

        saved = runtime._RUNTIME
        try:
            runtime._RUNTIME = FakeRuntime()
            runtime.stop(None)
        finally:
            runtime._RUNTIME = saved

        self.assertEqual(calls, ["engine", "server"])

    def test_after_backtest_stops_it_too(self):
        import bigqmt_backtest.qmt_runtime as runtime

        calls = []

        class FakeRuntime(object):
            def on_qmt_stop(self):
                calls.append("engine")

            def stop_server(self, timeout_seconds=5.0):
                calls.append("server")

        saved = runtime._RUNTIME
        try:
            runtime._RUNTIME = FakeRuntime()
            runtime.after_backtest(None)
        finally:
            runtime._RUNTIME = saved

        self.assertIn("server", calls)

    def test_stop_without_a_runtime_is_harmless(self):
        import bigqmt_backtest.qmt_runtime as runtime

        saved = runtime._RUNTIME
        try:
            runtime._RUNTIME = None
            runtime.stop(None)          # must not raise
        finally:
            runtime._RUNTIME = saved


if __name__ == "__main__":
    unittest.main()
