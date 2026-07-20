"""Deterministic bar-by-bar backtest engine."""

import csv
import datetime as dt
import hashlib
import json
import math
import os

from .broker import ACTIVE_ORDER_STATUSES, SimulatedBroker
from .models import decimal_value, normalize_symbol


ENGINE_VERSION = "1.0.0"
PROTOCOL_VERSION = 1


class BacktestConfig(object):
    def __init__(
        self,
        run_id,
        output_dir,
        initial_cash=1000000,
        initial_positions=None,
        buy_commission_rate=0.0003,
        sell_commission_rate=0.0003,
        min_commission=5,
        stamp_tax_rate=0.0005,
        transfer_fee_rate=0.00001,
        slippage_bps=0,
        max_volume_participation=0.1,
        price_limit_rate=0.10,
        lot_size=100,
        time_in_force="NEXT_BAR",
        seed=0,
        strategy_name="external_zmq_strategy",
        parameters=None,
        fee_schedule="a_share_2023_08_28",
        market_rules_version="a_share_v1",
    ):
        self.run_id = str(run_id or "").strip()
        if not self.run_id:
            raise ValueError("run_id is required")
        self.output_dir = os.path.abspath(output_dir)
        self.initial_cash = decimal_value(initial_cash)
        if self.initial_cash < 0:
            raise ValueError("initial_cash cannot be negative")
        self.initial_positions = dict(initial_positions or {})
        self.buy_commission_rate = decimal_value(buy_commission_rate)
        self.sell_commission_rate = decimal_value(sell_commission_rate)
        self.min_commission = decimal_value(min_commission)
        self.stamp_tax_rate = decimal_value(stamp_tax_rate)
        self.transfer_fee_rate = decimal_value(transfer_fee_rate)
        self.slippage_bps = decimal_value(slippage_bps)
        self.max_volume_participation = float(max_volume_participation)
        if not 0 < self.max_volume_participation <= 1:
            raise ValueError("max_volume_participation must be in (0, 1]")
        self.price_limit_rate = decimal_value(price_limit_rate)
        self.lot_size = int(lot_size)
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        self.time_in_force = str(time_in_force or "NEXT_BAR").upper()
        if self.time_in_force not in ("NEXT_BAR", "DAY"):
            raise ValueError("time_in_force must be NEXT_BAR or DAY")
        self.seed = int(seed)
        self.strategy_name = str(strategy_name or "external_zmq_strategy")
        self.parameters = dict(parameters or {})
        self.fee_schedule = str(fee_schedule or "custom")
        self.market_rules_version = str(market_rules_version or "custom")

    def to_dict(self, include_paths=True, include_identity=True):
        payload = {
            "initial_cash": float(self.initial_cash),
            "initial_positions": self.initial_positions,
            "buy_commission_rate": float(self.buy_commission_rate),
            "sell_commission_rate": float(self.sell_commission_rate),
            "min_commission": float(self.min_commission),
            "stamp_tax_rate": float(self.stamp_tax_rate),
            "transfer_fee_rate": float(self.transfer_fee_rate),
            "slippage_bps": float(self.slippage_bps),
            "max_volume_participation": self.max_volume_participation,
            "price_limit_rate": float(self.price_limit_rate),
            "lot_size": self.lot_size,
            "time_in_force": self.time_in_force,
            "seed": self.seed,
            "strategy_name": self.strategy_name,
            "parameters": self.parameters,
            "fee_schedule": self.fee_schedule,
            "market_rules_version": self.market_rules_version,
        }
        if include_identity:
            payload["run_id"] = self.run_id
        if include_paths:
            payload["output_dir"] = self.output_dir
        return payload


class BacktestEngine(object):
    def __init__(self, feed, config):
        self.feed = feed
        self.config = config
        self.created_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.started = False
        self.finished = False
        self.current_index = -1
        self.current_frame = None
        self.last_fills = []
        self.equity_curve = []
        self.position_rows = []
        self._result = None
        self.broker = SimulatedBroker(config)

    def _record_state(self):
        snapshot = self.broker.snapshot(self.current_frame["bars"])
        equity_row = {
            "frame_index": self.current_index,
            "datetime": self.current_frame["datetime"],
            "cash": snapshot["cash"],
            "market_value": snapshot["market_value"],
            "total_asset": snapshot["total_asset"],
        }
        self.equity_curve.append(equity_row)
        for symbol, position in snapshot["positions"].items():
            row = {"frame_index": self.current_index, "datetime": self.current_frame["datetime"]}
            row.update(position)
            self.position_rows.append(row)

    def start(self):
        if self.started:
            return self.state()
        self.started = True
        self.current_index = 0
        self.current_frame = self.feed.frame(0)
        self.broker._settle_trading_day(self.current_frame["datetime"][:10])
        self._record_state()
        return self.state()

    def _require_started(self):
        if not self.started:
            raise RuntimeError("backtest has not started")
        if self.finished:
            raise RuntimeError("backtest is already finished")

    def submit_order(self, payload):
        self._require_started()
        return self.broker.submit(
            payload,
            frame_index=self.current_index,
            submitted_at=self.current_frame["datetime"],
        ).to_dict()

    def cancel_order(self, order_id):
        self._require_started()
        return self.broker.cancel(order_id).to_dict()

    def next_bar(self):
        self._require_started()
        if self.current_index >= len(self.feed) - 1:
            return self.state()
        self.current_index += 1
        self.current_frame = self.feed.frame(self.current_index)
        self.last_fills = self.broker.advance(self.current_index, self.current_frame)
        self._record_state()
        return self.state()

    def history(self, symbol, count=100, fields=None):
        self._require_started()
        return self.feed.history(normalize_symbol(symbol), self.current_index, count=count, fields=fields)

    def orders(self):
        return self.broker.orders()

    def fills(self):
        return self.broker.fills()

    def state(self):
        if not self.started:
            return {
                "run_id": self.config.run_id,
                "started": False,
                "finished": self.finished,
                "done": False,
                "frame_index": -1,
                "frame_count": len(self.feed),
            }
        portfolio = self.broker.snapshot(self.current_frame["bars"])
        return {
            "run_id": self.config.run_id,
            "started": True,
            "finished": self.finished,
            "done": self.current_index >= len(self.feed) - 1,
            "frame_index": self.current_index,
            "frame_count": len(self.feed),
            "datetime": self.current_frame["datetime"],
            "bars": {key: dict(value) for key, value in self.current_frame["bars"].items()},
            "fills": [fill.to_dict() for fill in self.last_fills],
            "cash": portfolio["cash"],
            "market_value": portfolio["market_value"],
            "total_asset": portfolio["total_asset"],
            "positions": portfolio["positions"],
            "total_fees": portfolio["total_fees"],
            "turnover": portfolio["turnover"],
        }

    def _metrics(self):
        assets = [float(row["total_asset"]) for row in self.equity_curve]
        initial = assets[0] if assets else float(self.config.initial_cash)
        final = assets[-1] if assets else initial
        peak = None
        max_drawdown = 0.0
        for value in assets:
            peak = value if peak is None else max(peak, value)
            if peak > 0:
                max_drawdown = min(max_drawdown, value / peak - 1.0)
        dates = sorted(set(row["datetime"][:10] for row in self.equity_curve))
        total_return = 0.0 if initial == 0 else final / initial - 1.0
        annualized = None
        if len(dates) > 1 and initial > 0 and final > 0:
            annualized = math.pow(final / initial, 252.0 / len(dates)) - 1.0
        filled_orders = len([order for order in self.broker.orders_list if order.filled_quantity > 0])
        rejected_orders = len([order for order in self.broker.orders_list if order.status == "REJECTED"])
        return {
            "initial_total_asset": round(initial, 2),
            "final_total_asset": round(final, 2),
            "total_return": round(total_return, 10),
            "annualized_return": None if annualized is None else round(annualized, 10),
            "max_drawdown": round(-max_drawdown, 10),
            "trading_days": len(dates),
            "bar_count": len(self.equity_curve),
            "order_count": len(self.broker.orders_list),
            "filled_order_count": filled_orders,
            "rejected_order_count": rejected_orders,
            "fill_count": len(self.broker.fills_list),
            "total_fees": round(float(self.broker.total_fees), 2),
            "turnover": round(float(self.broker.turnover), 2),
        }

    def _signature_payload(self, metrics):
        orders = []
        for item in self.orders():
            clean = dict(item)
            clean.pop("client_order_id", None)
            orders.append(clean)
        return {
            "engine_version": ENGINE_VERSION,
            "data_hash": self.feed.data_hash,
            "config": self.config.to_dict(include_paths=False, include_identity=False),
            "orders": orders,
            "fills": self.fills(),
            "equity": self.equity_curve,
            "metrics": metrics,
        }

    def finish(self):
        if self._result is not None:
            return dict(self._result)
        if not self.started:
            self.start()
        self.broker.expire_open_orders()
        metrics = self._metrics()
        signature_json = json.dumps(
            self._signature_payload(metrics), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        signature = hashlib.sha256(signature_json.encode("utf-8")).hexdigest()
        self.finished = True
        final_state = self.state()
        self._result = {
            "schema_version": 1,
            "engine_version": ENGINE_VERSION,
            "run_id": self.config.run_id,
            "strategy_name": self.config.strategy_name,
            "data_hash": self.feed.data_hash,
            "deterministic_signature": signature,
            "metrics": metrics,
            "final_state": final_state,
        }
        self._write_artifacts()
        return dict(self._result)

    @staticmethod
    def _write_json(path, payload):
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")

    @staticmethod
    def _write_csv(path, rows, fieldnames):
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    def _write_artifacts(self):
        output_dir = self.config.output_dir
        os.makedirs(output_dir, exist_ok=True)
        meta = {
            "schema_version": 1,
            "engine_version": ENGINE_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "run_id": self.config.run_id,
            "created_at": self.created_at,
            "data_source": self.feed.source,
            "data_hash": self.feed.data_hash,
            "frame_count": len(self.feed),
            "config": self.config.to_dict(),
            "live_ready": False,
            "execution_channel": "backtest_zmq_only",
        }
        self._write_json(os.path.join(output_dir, "meta.json"), meta)
        self._write_json(os.path.join(output_dir, "result.json"), self._result)
        self._write_csv(
            os.path.join(output_dir, "orders.csv"),
            self.orders(),
            (
                "order_id", "client_order_id", "symbol", "side", "quantity", "filled_quantity",
                "remaining_quantity", "order_type", "limit_price", "submitted_index", "submitted_at",
                "time_in_force", "status", "reject_reason",
            ),
        )
        self._write_csv(
            os.path.join(output_dir, "fills.csv"),
            self.fills(),
            (
                "fill_id", "order_id", "client_order_id", "symbol", "side", "quantity", "price",
                "amount", "commission", "stamp_tax", "transfer_fee", "total_fee", "filled_index", "filled_at",
            ),
        )
        self._write_csv(
            os.path.join(output_dir, "equity.csv"),
            self.equity_curve,
            ("frame_index", "datetime", "cash", "market_value", "total_asset"),
        )
        self._write_csv(
            os.path.join(output_dir, "positions.csv"),
            self.position_rows,
            (
                "frame_index", "datetime", "symbol", "quantity", "available", "today_buy", "avg_cost",
                "realized_pnl", "mark_price", "market_value",
            ),
        )


class StreamingBacktestEngine(BacktestEngine):
    """Backtest engine whose bars arrive from a QMT backtest callback thread."""

    def __init__(self, feed, config, bar_wait_timeout_seconds=60.0):
        super(StreamingBacktestEngine, self).__init__(feed, config)
        self.bar_wait_timeout_seconds = float(bar_wait_timeout_seconds)

    def start(self):
        if not self.started and not self.feed.wait_for_index(0, self.bar_wait_timeout_seconds):
            raise TimeoutError("timed out waiting for the first QMT backtest bar")
        return super(StreamingBacktestEngine, self).start()

    def next_bar(self):
        self._require_started()
        target = self.current_index + 1
        if not self.feed.wait_for_index(target, self.bar_wait_timeout_seconds):
            if self.feed.closed:
                return self.state()
            raise TimeoutError("timed out waiting for QMT backtest bar index %d" % target)
        self.current_index = target
        self.current_frame = self.feed.frame(target)
        self.last_fills = self.broker.advance(self.current_index, self.current_frame)
        self._record_state()
        return self.state()

    def state(self):
        state = super(StreamingBacktestEngine, self).state()
        if state.get("started"):
            state["done"] = bool(self.feed.closed and self.current_index >= len(self.feed) - 1)
        return state
