"""QMT-native backtest service exposed to external strategies over ZMQ.

QMT remains the only backtest engine and matching system.  The ZMQ listener
thread only queues commands; every QMT API call is executed by ``handlebar`` on
QMT's callback thread.
"""

import datetime as dt
import threading
import uuid

from .data_feed import StreamingBarFeed, parse_datetime
from .models import normalize_symbol
from .protocol import BacktestBridgeProtocol
from .zmq_server import ZmqBacktestServer


_CONFIG = {}
_QMT_API = {}
_RUNTIME = None


def configure(**kwargs):
    _CONFIG.update(kwargs)


def bind_qmt_api(passorder_func=None, cancel_func=None, get_trade_detail_data_func=None):
    if passorder_func is not None:
        _QMT_API["passorder"] = passorder_func
    if cancel_func is not None:
        _QMT_API["cancel"] = cancel_func
    if get_trade_detail_data_func is not None:
        _QMT_API["get_trade_detail_data"] = get_trade_detail_data_func


def _sequence(value):
    if value is None:
        return []
    if isinstance(value, dict):
        for item in value.values():
            result = _sequence(item)
            if result:
                return result
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if hasattr(value, "tolist"):
        try:
            result = value.tolist()
            return result if isinstance(result, list) else [result]
        except Exception:
            pass
    if hasattr(value, "values"):
        try:
            return list(value.values)
        except Exception:
            pass
    return [value]


def _last_value(value):
    values = _sequence(value)
    return values[-1] if values else None


def _attr(value, names, default=None):
    for name in names:
        if isinstance(value, dict) and name in value:
            result = value.get(name)
        else:
            result = getattr(value, name, None)
        if result is not None:
            return result
    return default


def _json_number(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _full_symbol(row):
    code = str(_attr(row, ("m_strInstrumentID", "instrument_id", "stock_code", "symbol"), "") or "")
    market = str(_attr(row, ("m_strExchangeID", "exchange_id", "market"), "") or "").upper()
    if "." not in code and market in ("SH", "SZ", "BJ"):
        code = code + "." + market
    return normalize_symbol(code) if code else ""


def _side_from_offset(value):
    try:
        return "BUY" if int(value or 0) == 48 else "SELL"
    except (TypeError, ValueError):
        return str(value or "")


def _is_qmt_backtest(context):
    value = getattr(context, "do_back_test", None)
    if callable(value):
        try:
            value = value()
        except Exception:
            value = None
    if bool(value):
        return True
    for name in ("is_backtest", "is_back_test", "backtest"):
        value = getattr(context, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                value = None
        if bool(value):
            return True
    return False


class QmtBarExtractor(object):
    def __init__(self):
        self.previous_close = {}

    @staticmethod
    def _symbol(context):
        raw = ""
        for name in ("stock", "symbol", "stockcode"):
            value = getattr(context, name, None)
            if value:
                raw = str(value)
                break
        if not raw:
            raise ValueError("QMT ContextInfo has no stock symbol")
        if "." not in raw:
            market = str(getattr(context, "market", "") or "").upper()
            if market in ("SH", "SZ", "BJ"):
                raw = raw + "." + market
        return normalize_symbol(raw)

    @staticmethod
    def _timestamp(context):
        barpos = getattr(context, "barpos", getattr(context, "bar_index", None))
        getter = getattr(context, "get_bar_timetag", None)
        if callable(getter) and barpos is not None:
            return parse_datetime(getter(barpos)).strftime("%Y-%m-%d %H:%M:%S")
        for name in ("bar_time", "datetime", "timestamp"):
            value = getattr(context, name, None)
            if value not in (None, ""):
                return parse_datetime(value).strftime("%Y-%m-%d %H:%M:%S")
        raise ValueError("QMT ContextInfo has no deterministic bar timestamp")

    @staticmethod
    def _periods(context):
        result = []
        for value in (getattr(context, "period", None), "1m", "1d"):
            text = str(value or "").strip()
            if text and text not in result:
                result.append(text)
        return result

    def _history_value(self, context, field):
        getter = getattr(context, "get_history_data", None)
        if not callable(getter):
            return None
        for period in self._periods(context):
            for call in (
                lambda period=period: getter(1, period, field),
                lambda period=period: getter(field, 1, period),
                lambda: getter(field, 1),
            ):
                try:
                    value = _last_value(call())
                    if value not in (None, ""):
                        return value
                except Exception:
                    continue
        return None

    def _field(self, context, field, aliases=()):
        for name in (field,) + tuple(aliases):
            value = _last_value(getattr(context, name, None))
            if value not in (None, ""):
                return value
        for name in (field,) + tuple(aliases):
            value = self._history_value(context, name)
            if value not in (None, ""):
                return value
        return None

    def extract(self, context):
        symbol = self._symbol(context)
        close = self._field(context, "close")
        row = {
            "datetime": self._timestamp(context),
            "symbol": symbol,
            "open": self._field(context, "open"),
            "high": self._field(context, "high"),
            "low": self._field(context, "low"),
            "close": close,
            "volume": self._field(context, "volume", ("vol",)) or 0,
            "amount": self._field(context, "amount") or 0,
            "prev_close": self._field(context, "prev_close", ("preClose", "lastClose")),
        }
        if row["prev_close"] in (None, ""):
            row["prev_close"] = self.previous_close.get(symbol)
        self.previous_close[symbol] = close
        return row


class NativeSessionConfig(object):
    def __init__(self, run_id, strategy_name, account_id):
        self.run_id = str(run_id)
        self.strategy_name = str(strategy_name)
        self.account_id = str(account_id)


class QmtNativeBacktestSession(object):
    """Engine-shaped adapter whose actual engine and broker are both QMT."""

    engine_version = "qmt-native-1.0.0"
    execution_backend = "QMT_NATIVE"
    fill_timing = "qmt_native_matching"

    def __init__(self, config=None, qmt_api=None):
        options = dict(config or {})
        run_id = str(options.get("run_id") or ("qmt-native-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")))
        self.config = NativeSessionConfig(
            run_id=run_id,
            strategy_name=options.get("strategy_name") or "ZMQ_BACKTEST",
            account_id=options.get("account_id") or "",
        )
        self.account_type = str(options.get("account_type") or "STOCK")
        self.combo_type = int(options.get("combo_type") or 1101)
        self.quick_trade = int(options.get("quick_trade") if options.get("quick_trade") is not None else 2)
        self.market_price_type = int(options.get("market_price_type") or 5)
        self.limit_price_type = int(options.get("limit_price_type") or 11)
        self.bar_wait_timeout = float(options.get("bar_wait_timeout_seconds") or 60.0)
        self.require_backtest = bool(options.get("require_qmt_backtest", True))
        self.qmt_api = dict(qmt_api or {})
        self.feed = StreamingBarFeed(source="qmt_native_backtest")
        self.extractor = QmtBarExtractor()
        self.created_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.started = False
        self.finished = False
        self.qmt_completed = False
        self.current_index = -1
        self.current_frame = None
        self._released_index = -1
        self._condition = threading.Condition()
        self._pending_commands = []
        self._orders = {}
        self._fills = {}
        self._current_frame_fills = []
        self._published_fill_keys = set()
        self._positions = {}
        self._asset = {"cash": None, "total_asset": None}
        self._context = None
        self._failure = ""

    def bind_context(self, context):
        if self.require_backtest and not _is_qmt_backtest(context):
            raise RuntimeError("QMT native bridge refused to run outside QMT backtest mode")
        if not self.config.account_id:
            raise RuntimeError("account_id is required for the QMT native backtest bridge")
        self._context = context
        if self.config.account_id and hasattr(context, "set_account"):
            context.set_account(self.config.account_id)

    def _require_api(self, name):
        func = self.qmt_api.get(name)
        if func is None:
            raise RuntimeError("QMT runtime API is unavailable: %s" % name)
        return func

    def _require_started(self):
        if not self.started:
            raise RuntimeError("external strategy has not attached")
        if self.finished:
            raise RuntimeError("external strategy session is already finished")

    def start(self):
        with self._condition:
            if not self.started:
                self.started = True
                self._condition.notify_all()
            if self.current_index < 0 and not self.qmt_completed:
                ready = self._condition.wait_for(
                    lambda: self.current_index >= 0 or self.qmt_completed or bool(self._failure),
                    timeout=self.bar_wait_timeout,
                )
                if not ready:
                    raise TimeoutError("timed out waiting for QMT's first backtest bar")
            if self._failure:
                raise RuntimeError(self._failure)
            return self._state_unlocked()

    def _queue_command(self, command):
        with self._condition:
            self._require_started()
            if self.qmt_completed or self.current_index < 0:
                raise RuntimeError("QMT backtest has no active bar")
            if self._released_index >= self.current_index:
                raise RuntimeError("current QMT bar has already been released")
            command = dict(command)
            command["frame_index"] = self.current_index
            self._pending_commands.append(command)
            return command

    def submit_order(self, payload):
        payload = dict(payload or {})
        side = str(payload.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        quantity = int(payload.get("quantity") or 0)
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        symbol = normalize_symbol(payload.get("symbol"))
        order_type = str(payload.get("order_type") or "MARKET").upper()
        if order_type not in ("MARKET", "LIMIT"):
            raise ValueError("order_type must be MARKET or LIMIT")
        limit_price = payload.get("limit_price")
        if order_type == "LIMIT" and limit_price in (None, ""):
            raise ValueError("limit_price is required for LIMIT order")
        client_order_id = str(payload.get("client_order_id") or ("zmq:" + uuid.uuid4().hex[:20]))
        record = {
            "order_id": client_order_id,
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "filled_quantity": 0,
            "order_type": order_type,
            "limit_price": None if limit_price in (None, "") else float(limit_price),
            "status": "QUEUED",
            "reject_reason": "",
            "submitted_index": self.current_index,
            "submitted_at": (self.current_frame or {}).get("datetime", ""),
            "execution_backend": self.execution_backend,
        }
        with self._condition:
            self._orders[client_order_id] = record
        self._queue_command({"kind": "submit", "client_order_id": client_order_id})
        return dict(record)

    def cancel_order(self, order_id):
        order_id = str(order_id or "").strip()
        if not order_id:
            raise ValueError("order_id is required")
        command = self._queue_command({"kind": "cancel", "order_id": order_id})
        return {"order_id": order_id, "status": "CANCEL_QUEUED", "frame_index": command["frame_index"]}

    def _execute_submit(self, command, context):
        client_order_id = command["client_order_id"]
        with self._condition:
            record = dict(self._orders[client_order_id])
        if not self.config.account_id:
            raise RuntimeError("account_id is required for QMT native passorder")
        passorder = self._require_api("passorder")
        side = record["side"]
        order_type = record["order_type"]
        price_type = self.limit_price_type if order_type == "LIMIT" else self.market_price_type
        price = float(record["limit_price"] or 0)
        result = passorder(
            23 if side == "BUY" else 24,
            self.combo_type,
            self.config.account_id,
            record["symbol"],
            price_type,
            price,
            int(record["quantity"]),
            self.config.strategy_name,
            self.quick_trade,
            client_order_id,
            context,
        )
        with self._condition:
            target = self._orders[client_order_id]
            target["status"] = "SUBMITTED"
            if result not in (None, ""):
                target["qmt_order_id"] = str(result)

    def _execute_cancel(self, command, context):
        cancel = self._require_api("cancel")
        order_id = command["order_id"]
        with self._condition:
            record = self._orders.get(order_id)
            qmt_order_id = (
                (record or {}).get("qmt_order_id")
                or (record or {}).get("order_id")
                or order_id
            )
        result = cancel(qmt_order_id, self.config.account_id, self.account_type, context)
        with self._condition:
            record = self._orders.get(order_id)
            if record is not None:
                record["status"] = "CANCEL_SUBMITTED" if result is not False else "CANCEL_REJECTED"

    def _execute_commands(self, commands, context):
        for command in commands:
            try:
                if command["kind"] == "submit":
                    self._execute_submit(command, context)
                elif command["kind"] == "cancel":
                    self._execute_cancel(command, context)
            except Exception as exc:
                key = command.get("client_order_id") or command.get("order_id")
                with self._condition:
                    record = self._orders.get(key)
                    if record is not None:
                        record["status"] = "REJECTED"
                        record["reject_reason"] = "%s: %s" % (exc.__class__.__name__, exc)
                print("[bigqmt_backtest] QMT command failed kind=%s error=%s" % (command.get("kind"), exc))

    def on_bar(self, context):
        self.bind_context(context)
        self._refresh_qmt_state()
        row = self.extractor.extract(context)
        appended = self.feed.append(row)
        if not appended:
            return False
        with self._condition:
            self.current_index = len(self.feed) - 1
            self.current_frame = self.feed.frame(self.current_index)
            new_fill_keys = [key for key in self._fills if key not in self._published_fill_keys]
            self._current_frame_fills = [dict(self._fills[key]) for key in new_fill_keys]
            self._published_fill_keys.update(new_fill_keys)
            index = self.current_index
            self._condition.notify_all()
            released = self._condition.wait_for(
                lambda: self._released_index >= index or self.finished or bool(self._failure),
                timeout=self.bar_wait_timeout,
            )
            if not released:
                self._failure = "external strategy timed out on QMT bar index %d" % index
                self._condition.notify_all()
                raise TimeoutError(self._failure)
            commands = [item for item in self._pending_commands if item.get("frame_index") == index]
            self._pending_commands = [item for item in self._pending_commands if item.get("frame_index") != index]
        self._execute_commands(commands, context)
        self._refresh_qmt_state()
        print(
            "[bigqmt_backtest] QMT native bar released index=%d datetime=%s symbol=%s commands=%d"
            % (index, row["datetime"], row["symbol"], len(commands))
        )
        return True

    def next_bar(self):
        with self._condition:
            self._require_started()
            previous = self.current_index
            if self.qmt_completed:
                return self._state_unlocked()
            self._released_index = max(self._released_index, previous)
            self._condition.notify_all()
            ready = self._condition.wait_for(
                lambda: self.current_index > previous or self.qmt_completed or bool(self._failure),
                timeout=self.bar_wait_timeout,
            )
            if not ready:
                raise TimeoutError("timed out waiting for QMT backtest bar after index %d" % previous)
            if self._failure:
                raise RuntimeError(self._failure)
            return self._state_unlocked()

    def history(self, symbol, count=100, fields=None):
        with self._condition:
            self._require_started()
            end_index = self.current_index
        return self.feed.history(symbol, end_index, count=count, fields=fields)

    def orders(self):
        with self._condition:
            return [dict(value) for value in self._orders.values()]

    def fills(self):
        with self._condition:
            return [dict(value) for value in self._fills.values()]

    def _state_unlocked(self):
        if self.current_frame is None:
            return {
                "run_id": self.config.run_id,
                "started": self.started,
                "finished": self.finished,
                "done": self.qmt_completed,
                "frame_index": -1,
                "frame_count": len(self.feed),
                "execution_backend": self.execution_backend,
            }
        return {
            "run_id": self.config.run_id,
            "started": self.started,
            "finished": self.finished,
            "done": self.qmt_completed,
            "frame_index": self.current_index,
            "frame_count": len(self.feed),
            "datetime": self.current_frame["datetime"],
            "bars": {key: dict(value) for key, value in self.current_frame["bars"].items()},
            "fills": [dict(value) for value in self._current_frame_fills],
            "cash": self._asset.get("cash"),
            "total_asset": self._asset.get("total_asset"),
            "positions": {key: dict(value) for key, value in self._positions.items()},
            "execution_backend": self.execution_backend,
            "qmt_completed": self.qmt_completed,
            "failure": self._failure,
        }

    def state(self):
        with self._condition:
            return self._state_unlocked()

    def finish(self):
        with self._condition:
            if self.finished:
                return self._result_unlocked()
            self._released_index = max(self._released_index, self.current_index)
            self.finished = True
            self._condition.notify_all()
            return self._result_unlocked()

    def _result_unlocked(self):
        return {
            "schema_version": 1,
            "engine_version": self.engine_version,
            "run_id": self.config.run_id,
            "strategy_name": self.config.strategy_name,
            "execution_backend": self.execution_backend,
            "qmt_completed": self.qmt_completed,
            "order_count": len(self._orders),
            "fill_count": len(self._fills),
            "final_state": self._state_unlocked(),
            "result_owner": "QMT",
        }

    def on_qmt_stop(self):
        self.feed.close()
        with self._condition:
            new_fill_keys = [key for key in self._fills if key not in self._published_fill_keys]
            self._current_frame_fills = [dict(self._fills[key]) for key in new_fill_keys]
            self._published_fill_keys.update(new_fill_keys)
            self.qmt_completed = True
            self._condition.notify_all()
        print("[bigqmt_backtest] QMT native backtest completed bars=%d" % len(self.feed))

    def on_order(self, order):
        item = {
            "order_id": str(_attr(order, ("m_strOrderSysID", "order_sys_id", "order_id"), "") or ""),
            "client_order_id": str(_attr(order, ("m_strRemark", "remark", "user_order_id"), "") or ""),
            "symbol": _full_symbol(order),
            "side": _side_from_offset(_attr(order, ("m_nOffsetFlag", "offset_flag"), 0)),
            "quantity": int(_attr(order, ("m_nVolumeTotalOriginal", "volume", "quantity"), 0) or 0),
            "filled_quantity": int(_attr(order, ("m_nVolumeTraded", "traded_volume", "filled_quantity"), 0) or 0),
            "price": _json_number(_attr(order, ("m_dLimitPrice", "m_dPrice", "price"))),
            "status": str(_attr(order, ("m_nOrderStatus", "status"), "") or ""),
        }
        key = item["client_order_id"] or item["order_id"] or ("order:" + uuid.uuid4().hex)
        with self._condition:
            existing = self._orders.get(key, {})
            existing.update(item)
            self._orders[key] = existing
            self._condition.notify_all()
        return dict(existing)

    def on_trade(self, trade):
        item = {
            "fill_id": str(_attr(trade, ("m_strTradeID", "trade_id", "fill_id"), "") or ""),
            "order_id": str(_attr(trade, ("m_strOrderSysID", "order_sys_id", "order_id"), "") or ""),
            "client_order_id": str(_attr(trade, ("m_strRemark", "remark", "user_order_id"), "") or ""),
            "symbol": _full_symbol(trade),
            "side": _side_from_offset(_attr(trade, ("m_nOffsetFlag", "offset_flag"), 0)),
            "quantity": int(_attr(trade, ("m_nVolume", "volume", "quantity"), 0) or 0),
            "price": _json_number(_attr(trade, ("m_dPrice", "m_dTradePrice", "price"))),
            "filled_at": str(_attr(trade, ("m_strTradeTime", "trade_time", "filled_at"), "") or ""),
        }
        key = item["fill_id"] or "%s:%s:%s" % (item["order_id"], item["quantity"], item["price"])
        with self._condition:
            self._fills[key] = item
            self._condition.notify_all()
        return dict(item)

    def _query(self, detail_type):
        query = self.qmt_api.get("get_trade_detail_data")
        if query is None or not self.config.account_id:
            return []
        calls = []
        if detail_type in ("ORDER", "DEAL", "TRADE"):
            calls.append(lambda: query(
                self.config.account_id, self.account_type, detail_type, self.config.strategy_name
            ))
        calls.append(lambda: query(self.config.account_id, self.account_type, detail_type))
        last_error = None
        for call in calls:
            try:
                return list(call() or [])
            except Exception as exc:
                last_error = exc
        print("[bigqmt_backtest] QMT query failed type=%s error=%s" % (detail_type, last_error))
        return []

    def _refresh_qmt_state(self):
        positions = {}
        for row in self._query("POSITION"):
            symbol = _full_symbol(row)
            if not symbol:
                continue
            positions[symbol] = {
                "symbol": symbol,
                "quantity": int(_attr(row, ("m_nVolume", "volume", "quantity"), 0) or 0),
                "available": int(_attr(row, ("m_nCanUseVolume", "available", "can_use_volume"), 0) or 0),
                "avg_cost": _json_number(_attr(row, ("m_dOpenPrice", "m_dCostPrice", "cost", "avg_cost"))),
            }
        asset_rows = self._query("ACCOUNT") or self._query("ASSET")
        asset = {"cash": None, "total_asset": None}
        if asset_rows:
            row = asset_rows[0]
            asset = {
                "cash": _json_number(_attr(row, ("m_dAvailable", "m_dAvailableCash", "available_cash", "cash"))),
                "total_asset": _json_number(_attr(row, ("m_dBalance", "m_dAsset", "total_asset", "asset"))),
            }
        order_rows = self._query("ORDER")
        trade_rows = self._query("DEAL") or self._query("TRADE")
        with self._condition:
            self._positions = positions
            self._asset = asset
        for row in order_rows:
            self.on_order(row)
        for row in trade_rows:
            self.on_trade(row)


class QmtBacktestBridgeRuntime(object):
    def __init__(self, config=None, qmt_api=None):
        config = dict(config or {})
        bind_endpoint = str(config.pop("bind_endpoint", "tcp://127.0.0.1:16662"))
        self.engine = QmtNativeBacktestSession(config=config, qmt_api=qmt_api)
        self.protocol = BacktestBridgeProtocol(self.engine)
        self.server = ZmqBacktestServer(self.protocol, endpoint=bind_endpoint, exit_on_finish=True)
        self.server_thread = None

    def start(self, context):
        if self.server_thread is not None:
            return
        self.engine.bind_context(context)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            name="bigqmt-native-backtest-zmq",
            daemon=True,
        )
        self.server_thread.start()
        if not self.server.wait_until_ready(5.0) or not self.server.actual_endpoint:
            raise RuntimeError("QMT native backtest ZMQ service failed to bind")
        print(
            "[bigqmt_backtest] QMT native service started run_id=%s endpoint=%s account=%s live_ready=False"
            % (self.engine.config.run_id, self.server.actual_endpoint, self.engine.config.account_id)
        )

    def on_bar(self, context):
        return self.engine.on_bar(context)

    def on_order(self, order):
        return self.engine.on_order(order)

    def on_trade(self, trade):
        return self.engine.on_trade(trade)

    def on_qmt_stop(self):
        self.engine.on_qmt_stop()

    def stop_server(self):
        self.server.stop()


def reset_runtime():
    global _RUNTIME
    if _RUNTIME is not None:
        _RUNTIME.stop_server()
    _RUNTIME = None


def get_runtime():
    return _RUNTIME


def init(ContextInfo):
    global _RUNTIME
    reset_runtime()
    _RUNTIME = QmtBacktestBridgeRuntime(_CONFIG, _QMT_API)
    _RUNTIME.start(ContextInfo)
    return _RUNTIME


def handlebar(ContextInfo):
    if _RUNTIME is None:
        init(ContextInfo)
    return _RUNTIME.on_bar(ContextInfo)


def order_callback(ContextInfo, orderInfo):
    if _RUNTIME is not None:
        return _RUNTIME.on_order(orderInfo)
    return None


def deal_callback(ContextInfo, dealInfo):
    if _RUNTIME is not None:
        return _RUNTIME.on_trade(dealInfo)
    return None


def stop(ContextInfo=None):
    if _RUNTIME is not None:
        _RUNTIME.on_qmt_stop()


def after_backtest(ContextInfo=None):
    return stop(ContextInfo)
