import csv
import os
import socket
import sys
import tempfile
import threading
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_backtest.client import BacktestZmqClient
from bigqmt_backtest.data_feed import CsvBarFeed
from bigqmt_backtest.engine import BacktestConfig, BacktestEngine
from bigqmt_backtest.protocol import BacktestBridgeProtocol
from bigqmt_backtest.qmt_runtime import QmtNativeBacktestSession
from bigqmt_backtest.zmq_server import ZmqBacktestServer


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _feed(tmp):
    path = os.path.join(tmp, "bars.csv")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("datetime", "symbol", "open", "high", "low", "close", "volume", "prev_close"),
        )
        writer.writeheader()
        writer.writerows(
            [
                {
                    "datetime": "2026-01-05 09:30:00",
                    "symbol": "000001.SZ",
                    "open": 10,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10,
                    "volume": 10000,
                    "prev_close": 9.9,
                },
                {
                    "datetime": "2026-01-05 09:31:00",
                    "symbol": "000001.SZ",
                    "open": 10.1,
                    "high": 10.2,
                    "low": 10,
                    "close": 10.15,
                    "volume": 10000,
                    "prev_close": 9.9,
                },
            ]
        )
    return CsvBarFeed(path)


class BacktestProtocolTest(unittest.TestCase):
    def test_run_and_client_identity_are_enforced_and_requests_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = BacktestEngine(
                _feed(tmp),
                BacktestConfig(run_id="identity", output_dir=os.path.join(tmp, "out")),
            )
            protocol = BacktestBridgeProtocol(engine)
            start = {
                "schema_version": 1,
                "request_id": "req-start",
                "run_id": "identity",
                "client_id": "client-a",
                "method": "start",
                "params": {},
            }
            first = protocol.handle(start)
            repeated = protocol.handle(dict(start))

            self.assertTrue(first["ok"])
            self.assertEqual(first, repeated)

            reused = dict(start, method="state")
            reused_response = protocol.handle(reused)
            self.assertFalse(reused_response["ok"])
            self.assertIn("different payload", reused_response["error"])

            wrong_run = dict(start, request_id="wrong-run", run_id="other", method="state")
            self.assertFalse(protocol.handle(wrong_run)["ok"])
            wrong_client = dict(start, request_id="wrong-client", client_id="client-b", method="next_bar")
            self.assertFalse(protocol.handle(wrong_client)["ok"])


class ZmqRoundTripTest(unittest.TestCase):
    def test_external_client_can_complete_a_backtest_over_zmq(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = BacktestEngine(
                _feed(tmp),
                BacktestConfig(
                    run_id="zmq-run",
                    output_dir=os.path.join(tmp, "out"),
                    max_volume_participation=1.0,
                ),
            )
            endpoint = "tcp://127.0.0.1:%d" % _free_port()
            server = ZmqBacktestServer(
                BacktestBridgeProtocol(engine), endpoint=endpoint, exit_on_finish=True
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.assertTrue(server.wait_until_ready(3.0))

            client = BacktestZmqClient(
                endpoint=endpoint,
                run_id="zmq-run",
                client_id="external-strategy",
                timeout_seconds=3.0,
            )
            started = client.start()
            order = client.submit_order(
                symbol="000001.SZ", side="BUY", quantity=100, order_type="MARKET"
            )
            advanced = client.next_bar()
            history = client.history("000001.SZ", count=10, fields=["close"])
            result = client.finish()
            client.close()
            thread.join(timeout=3.0)

            self.assertEqual(started["frame_index"], 0)
            self.assertEqual(order["status"], "PENDING")
            self.assertEqual(advanced["fills"][0]["price"], 10.1)
            self.assertEqual([row["close"] for row in history], [10.0, 10.15])
            self.assertEqual(result["run_id"], "zmq-run")
            self.assertFalse(thread.is_alive())

    def test_qmt_native_service_bridges_orders_to_qmt_matching(self):
        endpoint = "tcp://127.0.0.1:%d" % _free_port()
        calls = []
        holder = {}

        class Context(object):
            do_back_test = True
            stockcode = "600000"
            market = "SH"
            period = "1m"

            def __init__(self, barpos, close):
                self.barpos = barpos
                self.close_value = close

            def set_account(self, account_id):
                self.account_id = account_id

            def get_bar_timetag(self, barpos):
                return int((1704072600 + barpos * 60) * 1000)

            def get_history_data(self, count, period, field):
                values = {
                    "open": self.close_value,
                    "high": self.close_value + 0.1,
                    "low": self.close_value - 0.1,
                    "close": self.close_value,
                    "volume": 10000,
                    "amount": self.close_value * 10000,
                    "preClose": self.close_value - 0.1,
                }
                return {"600000.SH": [values[field]]}

        def fake_passorder(*args):
            calls.append((threading.get_ident(), args))
            session = holder["session"]
            session.on_order({
                "m_strOrderSysID": "qmt-order-1",
                "m_strRemark": args[-2],
                "m_strInstrumentID": "600000",
                "m_strExchangeID": "SH",
                "m_nOffsetFlag": 48,
                "m_nVolumeTotalOriginal": args[6],
                "m_nVolumeTraded": args[6],
                "m_nOrderStatus": "FILLED",
            })
            session.on_trade({
                "m_strTradeID": "qmt-fill-1",
                "m_strOrderSysID": "qmt-order-1",
                "m_strRemark": args[-2],
                "m_strInstrumentID": "600000",
                "m_strExchangeID": "SH",
                "m_nOffsetFlag": 48,
                "m_nVolume": args[6],
                "m_dPrice": 10.1,
                "m_strTradeTime": "09:31:00",
            })
            return "qmt-order-1"

        session = QmtNativeBacktestSession(
            config={
                "run_id": "qmt-zmq-native",
                "account_id": "backtest-account",
                "bar_wait_timeout_seconds": 2,
            },
            qmt_api={"passorder": fake_passorder},
        )
        holder["session"] = session
        server = ZmqBacktestServer(
            BacktestBridgeProtocol(session), endpoint=endpoint, exit_on_finish=True
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.assertTrue(server.wait_until_ready(2))

        def qmt_backtest_loop():
            session.bind_context(Context(0, 10.0))
            session.on_bar(Context(0, 10.0))
            session.on_bar(Context(1, 10.1))
            session.on_qmt_stop()

        qmt_thread = threading.Thread(target=qmt_backtest_loop)
        qmt_thread.start()

        client = BacktestZmqClient(endpoint, run_id="", timeout_seconds=2)
        description = client.describe()
        first = client.start()
        queued = client.submit_order("600000.SH", "BUY", 100, client_order_id="native-buy-1")
        second = client.next_bar()
        done = client.next_bar()
        result = client.finish()
        client.close()
        qmt_thread.join(timeout=2)
        server_thread.join(timeout=2)

        self.assertEqual(description["engine_owner"], "QMT")
        self.assertEqual(description["matching_owner"], "QMT")
        self.assertEqual(client.run_id, "qmt-zmq-native")
        self.assertEqual(first["frame_index"], 0)
        self.assertEqual(queued["status"], "QUEUED")
        self.assertEqual(second["frame_index"], 1)
        self.assertEqual(second["fills"][0]["fill_id"], "qmt-fill-1")
        self.assertTrue(done["done"])
        self.assertEqual(result["result_owner"], "QMT")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], qmt_thread.ident)
        self.assertFalse(qmt_thread.is_alive())
        self.assertFalse(server_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
