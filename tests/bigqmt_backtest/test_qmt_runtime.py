import datetime as dt
import os
import sys
import tempfile
import threading
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from bigqmt_backtest.data_feed import StreamingBarFeed
from bigqmt_backtest.engine import BacktestConfig, StreamingBacktestEngine
from bigqmt_backtest.qmt_runtime import QmtBarExtractor, QmtNativeBacktestSession


class FakeQmtContext(object):
    stockcode = "600000"
    market = "SH"
    period = "1m"
    barpos = 0
    do_back_test = True

    values = {
        "open": [10.0],
        "high": [10.2],
        "low": [9.9],
        "close": [10.1],
        "volume": [10000],
        "amount": [101000],
        "preClose": [9.8],
    }

    def get_bar_timetag(self, barpos):
        value = dt.datetime(2026, 1, 5, 9, 30)
        return int(value.timestamp() * 1000)

    def get_history_data(self, count, period, field):
        return {"600000.SH": self.values.get(field, [])}

    def set_account(self, account_id):
        self.account_id = account_id


class QmtBarExtractorTest(unittest.TestCase):
    def test_extracts_qmt_bar_without_live_account_or_order_api(self):
        row = QmtBarExtractor().extract(FakeQmtContext())

        self.assertEqual(row["symbol"], "600000.SH")
        self.assertEqual(row["datetime"], "2026-01-05 09:30:00")
        self.assertEqual(row["open"], 10.0)
        self.assertEqual(row["prev_close"], 9.8)

    def test_qmt_entry_is_isolated_and_binds_native_order_callbacks(self):
        path = os.path.join(SRC, "BIGQMT_ZMQ_BACKTEST.py")
        with open(path, "r", encoding="gbk") as handle:
            source = handle.read()

        self.assertNotIn("bigqmt_signal_trader", source)
        self.assertIn("passorder", source)
        self.assertIn("get_trade_detail_data", source)
        self.assertIn("order_callback", source)
        self.assertIn("deal_callback", source)
        self.assertNotIn("_importlib.import_module =", source)

        runtime_path = os.path.join(SRC, "bigqmt_backtest", "qmt_runtime.py")
        with open(runtime_path, "r", encoding="utf-8") as handle:
            runtime_source = handle.read()
        self.assertNotIn("StreamingBacktestEngine", runtime_source)
        self.assertNotIn("SimulatedBroker", runtime_source)

    def test_native_session_executes_passorder_on_qmt_callback_thread(self):
        calls = []

        def fake_passorder(*args):
            calls.append((threading.get_ident(), args))
            return "qmt-order-1"

        session = QmtNativeBacktestSession(
            config={
                "run_id": "qmt-native-test",
                "account_id": "test-account",
                "bar_wait_timeout_seconds": 1,
            },
            qmt_api={"passorder": fake_passorder},
        )
        qmt_thread = threading.Thread(target=session.on_bar, args=(FakeQmtContext(),))
        qmt_thread.start()

        state = session.start()
        queued = session.submit_order({
            "symbol": "600000.SH",
            "side": "BUY",
            "quantity": 100,
            "order_type": "MARKET",
            "client_order_id": "external-1",
        })
        session.finish()
        qmt_thread.join(timeout=2)

        self.assertFalse(qmt_thread.is_alive())
        self.assertEqual(state["execution_backend"], "QMT_NATIVE")
        self.assertEqual(queued["status"], "QUEUED")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], qmt_thread.ident)
        self.assertEqual(calls[0][1][0], 23)
        self.assertEqual(calls[0][1][2], "test-account")
        self.assertEqual(calls[0][1][-2], "external-1")
        self.assertEqual(session.orders()[0]["status"], "SUBMITTED")

    def test_native_session_rejects_non_backtest_qmt_context(self):
        context = FakeQmtContext()
        context.do_back_test = False
        session = QmtNativeBacktestSession(config={"run_id": "guard"})

        with self.assertRaisesRegex(RuntimeError, "outside QMT backtest mode"):
            session.bind_context(context)


class StreamingEngineTest(unittest.TestCase):
    def test_stream_does_not_report_done_until_qmt_closes_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            feed = StreamingBarFeed()
            feed.append(
                {
                    "datetime": "2026-01-05 09:30:00",
                    "symbol": "600000.SH",
                    "open": 10,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10,
                    "volume": 10000,
                    "prev_close": 9.9,
                }
            )
            engine = StreamingBacktestEngine(
                feed,
                BacktestConfig(run_id="stream", output_dir=os.path.join(tmp, "out")),
                bar_wait_timeout_seconds=0.1,
            )

            self.assertFalse(engine.start()["done"])
            feed.append(
                {
                    "datetime": "2026-01-05 09:31:00",
                    "symbol": "600000.SH",
                    "open": 10.1,
                    "high": 10.2,
                    "low": 10,
                    "close": 10.15,
                    "volume": 10000,
                    "prev_close": 9.9,
                }
            )
            self.assertFalse(engine.next_bar()["done"])
            feed.close()
            self.assertTrue(engine.state()["done"])


if __name__ == "__main__":
    unittest.main()
