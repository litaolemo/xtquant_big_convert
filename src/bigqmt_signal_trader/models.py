"""交易信号、委托请求和账户快照的数据模型。"""

import datetime as _dt
from enum import Enum
from typing import Any, Dict, List, Optional


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    CLEAR = "CLEAR"
    CANCEL = "CANCEL"


class SignalStatus(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    SUBMITTED = "SUBMITTED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    FILLED = "FILLED"


def parse_datetime(value: Any, field_name: str) -> _dt.datetime:
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return _dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise ValueError(f"{field_name} must use format YYYY-MM-DD HH:MM:SS") from exc
    raise ValueError(f"{field_name} is required")


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in ("1", "true", "yes", "y", "on")


class TradeSignal:
    def __init__(
        self,
        signal_id,
        account_id,
        action,
        created_at,
        expire_at,
        schema_version,
        stock_code="",
        stock_name="",
        amount=None,
        percentage=None,
        price_type="AUTO_LIMIT",
        price=None,
        strategy_name="bigqmt_signal_trader",
        remark="",
        source="",
        source_type="auto",
        force=False,
        bypass_stop_buy=False,
        bypass_stop_sell=False,
        bypass_daily_limit=False,
        status=SignalStatus.PENDING,
        raw_payload=None,
    ):
        self.signal_id = signal_id
        self.account_id = account_id
        self.action = action
        self.created_at = created_at
        self.expire_at = expire_at
        self.schema_version = schema_version
        self.stock_code = stock_code
        self.stock_name = stock_name
        self.amount = amount
        self.percentage = percentage
        self.price_type = price_type
        self.price = price
        self.strategy_name = strategy_name
        self.remark = remark
        self.source = source
        self.source_type = source_type
        self.force = force
        self.bypass_stop_buy = bypass_stop_buy
        self.bypass_stop_sell = bypass_stop_sell
        self.bypass_daily_limit = bypass_daily_limit
        self.status = status
        self.raw_payload = dict(raw_payload or {})

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "TradeSignal":
        required = ("signal_id", "account_id", "action", "created_at", "expire_at", "schema_version")
        for field_name in required:
            if payload.get(field_name) in (None, ""):
                raise ValueError(f"{field_name} is required")

        try:
            action = SignalAction(str(payload["action"]).upper())
        except ValueError as exc:
            raise ValueError(f"unsupported action: {payload.get('action')}") from exc

        amount = _optional_int(payload.get("amount"))
        percentage = _optional_float(payload.get("percentage"))
        stock_code = str(payload.get("stock_code") or "").strip().upper()

        if action == SignalAction.BUY:
            if not stock_code:
                raise ValueError("stock_code is required for BUY")
            if amount is None or amount <= 0:
                raise ValueError("amount must be positive for BUY")
        elif action == SignalAction.SELL:
            if not stock_code:
                raise ValueError("stock_code is required for SELL")
            if amount is None and percentage is None:
                raise ValueError("amount or percentage is required for SELL")
        elif action == SignalAction.CLEAR and percentage is None:
            percentage = 100.0

        return cls(
            signal_id=str(payload["signal_id"]),
            account_id=str(payload["account_id"]),
            action=action,
            stock_code=stock_code,
            stock_name=str(payload.get("stock_name") or ""),
            amount=amount,
            percentage=percentage,
            price_type=str(payload.get("price_type") or "AUTO_LIMIT").upper(),
            price=_optional_float(payload.get("price")),
            strategy_name=str(payload.get("strategy_name") or "bigqmt_signal_trader"),
            remark=str(payload.get("remark") or ""),
            source=str(payload.get("source") or ""),
            source_type=str(payload.get("source_type") or "auto"),
            force=_bool_value(payload.get("force", False)),
            bypass_stop_buy=_bool_value(payload.get("bypass_stop_buy", False)),
            bypass_stop_sell=_bool_value(payload.get("bypass_stop_sell", False)),
            bypass_daily_limit=_bool_value(payload.get("bypass_daily_limit", False)),
            created_at=parse_datetime(payload.get("created_at"), "created_at"),
            expire_at=parse_datetime(payload.get("expire_at"), "expire_at"),
            schema_version=int(payload["schema_version"]),
            raw_payload=dict(payload),
        )

    def is_expired(self, now: _dt.datetime) -> bool:
        return now > self.expire_at


class PositionSnapshot:
    def __init__(self, stock_code, volume, available, cost=0.0, stock_name=""):
        self.stock_code = stock_code
        self.volume = volume
        self.available = available
        self.cost = cost
        self.stock_name = stock_name


class AssetSnapshot:
    def __init__(self, account_id, cash=None, total_asset=None):
        self.account_id = account_id
        self.cash = cash
        self.total_asset = total_asset


class AccountSnapshot:
    def __init__(self, account_id, asset, positions, reason, updated_at):
        self.account_id = account_id
        self.asset = asset
        self.positions = positions
        self.reason = reason
        self.updated_at = updated_at


class OrderRequest:
    def __init__(
        self,
        signal_id,
        account_id,
        action,
        stock_code,
        volume,
        price,
        price_type,
        strategy_name,
        remark="",
    ):
        self.signal_id = signal_id
        self.account_id = account_id
        self.action = action
        self.stock_code = stock_code
        self.volume = volume
        self.price = price
        self.price_type = price_type
        self.strategy_name = strategy_name
        self.remark = remark


class OrderSubmitResult:
    def __init__(self, status, user_order_id, order_sys_id=None, message=""):
        self.status = status
        self.user_order_id = user_order_id
        self.order_sys_id = order_sys_id
        self.message = message


class OrderSnapshot:
    def __init__(
        self,
        order_sys_id,
        user_order_id,
        stock_code,
        action,
        volume,
        traded_volume,
        status,
        price=0.0,
        strategy_name="",
        remark="",
    ):
        self.order_sys_id = order_sys_id
        self.user_order_id = user_order_id
        self.stock_code = stock_code
        self.action = action
        self.volume = volume
        self.traded_volume = traded_volume
        self.status = status
        self.price = price
        self.strategy_name = strategy_name
        self.remark = remark


class TradeSnapshot:
    def __init__(self, trade_id, order_sys_id, stock_code, action, volume, price,
                 traded_at="", user_order_id=""):
        self.trade_id = trade_id
        self.order_sys_id = order_sys_id
        self.stock_code = stock_code
        self.action = action
        self.volume = volume
        self.price = price
        self.traded_at = traded_at
        self.user_order_id = user_order_id


class OrderRef:
    def __init__(self, order_sys_id, user_order_id=""):
        self.order_sys_id = order_sys_id
        self.user_order_id = user_order_id


class CancelResult:
    def __init__(self, success, message=""):
        self.success = success
        self.message = message
