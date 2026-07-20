import csv
import json
import os
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_backtest.data_feed import CsvBarFeed
from bigqmt_backtest.engine import BacktestConfig, BacktestEngine


FIELDS = (
    "datetime",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "prev_close",
)


def _write_bars(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _rows():
    return [
        {
            "datetime": "2026-01-05 09:30:00",
            "symbol": "600000.SH",
            "open": 10.00,
            "high": 10.10,
            "low": 9.95,
            "close": 10.05,
            "volume": 100000,
            "prev_close": 9.90,
        },
        {
            "datetime": "2026-01-05 09:31:00",
            "symbol": "600000.SH",
            "open": 10.10,
            "high": 10.20,
            "low": 10.00,
            "close": 10.15,
            "volume": 100000,
            "prev_close": 9.90,
        },
        {
            "datetime": "2026-01-05 09:32:00",
            "symbol": "600000.SH",
            "open": 10.20,
            "high": 10.25,
            "low": 10.10,
            "close": 10.18,
            "volume": 100000,
            "prev_close": 9.90,
        },
        {
            "datetime": "2026-01-06 09:30:00",
            "symbol": "600000.SH",
            "open": 10.30,
            "high": 10.35,
            "low": 10.20,
            "close": 10.25,
            "volume": 100000,
            "prev_close": 10.18,
        },
        {
            "datetime": "2026-01-06 09:31:00",
            "symbol": "600000.SH",
            "open": 10.25,
            "high": 10.30,
            "low": 10.15,
            "close": 10.20,
            "volume": 100000,
            "prev_close": 10.18,
        },
    ]


def _config(run_id, output_dir):
    return BacktestConfig(
        run_id=run_id,
        output_dir=output_dir,
        initial_cash=100000,
        buy_commission_rate=0.0003,
        sell_commission_rate=0.0003,
        min_commission=5,
        stamp_tax_rate=0.0005,
        transfer_fee_rate=0.00001,
        max_volume_participation=1.0,
        slippage_bps=0,
    )


class CsvBarFeedTest(unittest.TestCase):
    def test_loads_chronologically_and_never_exposes_future_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bars.csv")
            rows = list(reversed(_rows()))
            _write_bars(path, rows)

            feed = CsvBarFeed(path)

            self.assertEqual(len(feed), 5)
            self.assertEqual(feed.frame(0)["datetime"], "2026-01-05 09:30:00")
            self.assertEqual(
                list(feed.history("600000.SH", end_index=1, count=10, fields=["close"]))[-1]["close"],
                10.15,
            )
            self.assertNotIn(10.18, [item["close"] for item in feed.history("600000.SH", 1, 10)])
            self.assertEqual(len(feed.data_hash), 64)

    def test_duplicate_symbol_timestamp_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bars.csv")
            rows = _rows()
            _write_bars(path, rows + [dict(rows[0])])

            with self.assertRaisesRegex(ValueError, "duplicate bar"):
                CsvBarFeed(path)


class BacktestEngineTest(unittest.TestCase):
    def _engine(self, tmp, run_id="run-a"):
        path = os.path.join(tmp, "bars.csv")
        _write_bars(path, _rows())
        return BacktestEngine(CsvBarFeed(path), _config(run_id, os.path.join(tmp, run_id)))

    def test_order_is_filled_at_next_bar_open_not_current_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(tmp)
            started = engine.start()
            order = engine.submit_order(
                {"symbol": "600000.SH", "side": "BUY", "quantity": 100, "order_type": "MARKET"}
            )

            self.assertEqual(started["frame_index"], 0)
            self.assertEqual(order["status"], "PENDING")
            self.assertEqual(engine.state()["positions"], {})

            advanced = engine.next_bar()

            self.assertEqual(advanced["frame_index"], 1)
            self.assertEqual(advanced["fills"][0]["price"], 10.10)
            self.assertEqual(advanced["fills"][0]["commission"], 5.0)
            self.assertEqual(advanced["fills"][0]["transfer_fee"], 0.01)
            self.assertEqual(advanced["cash"], 98984.99)
            self.assertEqual(advanced["positions"]["600000.SH"]["quantity"], 100)
            self.assertEqual(advanced["positions"]["600000.SH"]["available"], 0)

    def test_t_plus_one_rejects_same_day_sell_and_allows_next_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(tmp)
            engine.start()
            engine.submit_order(
                {"symbol": "600000.SH", "side": "BUY", "quantity": 100, "order_type": "MARKET"}
            )
            engine.next_bar()

            rejected = engine.submit_order(
                {"symbol": "600000.SH", "side": "SELL", "quantity": 100, "order_type": "MARKET"}
            )
            self.assertEqual(rejected["status"], "REJECTED")
            self.assertEqual(rejected["reject_reason"], "t_plus_one_unavailable")

            engine.next_bar()
            next_day = engine.next_bar()
            self.assertEqual(next_day["positions"]["600000.SH"]["available"], 100)
            accepted = engine.submit_order(
                {"symbol": "600000.SH", "side": "SELL", "quantity": 100, "order_type": "MARKET"}
            )
            self.assertEqual(accepted["status"], "PENDING")
            filled = engine.next_bar()
            self.assertEqual(filled["fills"][0]["side"], "SELL")
            self.assertEqual(filled["fills"][0]["stamp_tax"], 0.51)
            self.assertEqual(filled["total_fees"], 10.53)
            self.assertEqual(filled["cash"], 100004.47)
            self.assertEqual(filled["positions"], {})

    def test_limit_locked_bar_does_not_assume_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "locked.csv")
            rows = _rows()[:2]
            rows[1].update({"open": 10.89, "high": 10.89, "low": 10.89, "close": 10.89, "prev_close": 9.90})
            _write_bars(path, rows)
            engine = BacktestEngine(CsvBarFeed(path), _config("locked", os.path.join(tmp, "locked")))
            engine.start()
            engine.submit_order(
                {"symbol": "600000.SH", "side": "BUY", "quantity": 100, "order_type": "MARKET"}
            )

            state = engine.next_bar()

            self.assertEqual(state["fills"], [])
            self.assertEqual(engine.orders()[0]["status"], "EXPIRED")
            self.assertEqual(engine.orders()[0]["reject_reason"], "limit_up_locked")

    def test_client_order_id_is_idempotent_but_cannot_change_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(tmp)
            engine.start()
            payload = {
                "client_order_id": "stable-1",
                "symbol": "600000.SH",
                "side": "BUY",
                "quantity": 100,
                "order_type": "MARKET",
            }
            first = engine.submit_order(payload)
            repeated = engine.submit_order(dict(payload))

            self.assertEqual(first["order_id"], repeated["order_id"])
            self.assertEqual(len(engine.orders()), 1)
            changed = dict(payload, quantity=200)
            with self.assertRaisesRegex(ValueError, "different order payload"):
                engine.submit_order(changed)

    def test_volume_participation_is_shared_across_orders_on_the_same_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "volume.csv")
            rows = _rows()[:2]
            rows[1]["volume"] = 1000
            _write_bars(path, rows)
            config = _config("volume", os.path.join(tmp, "volume"))
            config.max_volume_participation = 0.1
            engine = BacktestEngine(CsvBarFeed(path), config)
            engine.start()
            for order_id in ("first", "second"):
                engine.submit_order(
                    {
                        "client_order_id": order_id,
                        "symbol": "600000.SH",
                        "side": "BUY",
                        "quantity": 100,
                        "order_type": "MARKET",
                    }
                )

            advanced = engine.next_bar()

            self.assertEqual(sum(fill["quantity"] for fill in advanced["fills"]), 100)
            self.assertEqual(engine.orders()[1]["status"], "EXPIRED")
            self.assertEqual(engine.orders()[1]["reject_reason"], "volume_participation_exhausted")

    def test_finish_writes_evidence_and_is_deterministic(self):
        signatures = []
        with tempfile.TemporaryDirectory() as tmp:
            for run_id in ("det-a", "det-b"):
                engine = self._engine(tmp, run_id=run_id)
                engine.start()
                engine.submit_order(
                    {
                        "client_order_id": "buy-1",
                        "symbol": "600000.SH",
                        "side": "BUY",
                        "quantity": 100,
                        "order_type": "MARKET",
                    }
                )
                while not engine.next_bar()["done"]:
                    pass
                result = engine.finish()
                signatures.append(result["deterministic_signature"])
                output_dir = os.path.join(tmp, run_id)
                for name in ("meta.json", "result.json", "orders.csv", "fills.csv", "equity.csv", "positions.csv"):
                    self.assertTrue(os.path.isfile(os.path.join(output_dir, name)), name)
                with open(os.path.join(output_dir, "meta.json"), encoding="utf-8") as handle:
                    meta = json.load(handle)
                self.assertEqual(meta["data_hash"], engine.feed.data_hash)
                self.assertFalse(meta["live_ready"])

        self.assertEqual(signatures[0], signatures[1])


if __name__ == "__main__":
    unittest.main()
