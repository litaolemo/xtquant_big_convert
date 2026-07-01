"""MiniQMT-style client objects backed by Big QMT Redis RPC.

This module is the replacement edge for existing code that already calls
``xt_trader.query_stock_positions(...)`` or ``xtdata.get_full_tick(...)``.
The Big QMT process remains the only place that touches QMT runtime APIs.
"""

import os
import time
from typing import Any, Dict, Iterable, List, Optional

from .redis_rpc import call_redis_rpc


STOCK_BUY = 23
STOCK_SELL = 24
FIX_PRICE = 11
LATEST_PRICE = 5
MARKET_PEER_PRICE_FIRST = 44
MARKET_SH_CONVERT_5_LIMIT = 43
MARKET_SZ_CONVERT_5_CANCEL = 47
SZ_MARKET = 1
SH_MARKET = 0

ORDER_UNREPORTED = 48
ORDER_WAIT_REPORTING = 49
ORDER_REPORTED = 50
ORDER_REPORTED_CANCEL = 51
ORDER_PARTSUCC_CANCEL = 52
ORDER_PART_CANCEL = 53
ORDER_CANCELED = 54
ORDER_PART_SUCC = 55
ORDER_SUCCEEDED = 56
ORDER_JUNK = 57
ORDER_UNKNOWN = 255


class CompatObject:
    """Small attribute object matching xtquant's object-style returns."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __repr__(self):
        items = ", ".join("%s=%r" % (key, value) for key, value in sorted(self.__dict__.items()))
        return "%s(%s)" % (self.__class__.__name__, items)


class StockAccount:
    def __init__(self, account_id, account_type="STOCK"):
        self.account_id = str(account_id or "")
        self.account_type = str(account_type or "STOCK")


class XtQuantTraderCallback:
    def on_disconnected(self):
        pass

    def on_stock_order(self, order):
        pass

    def on_stock_trade(self, trade):
        pass

    def on_order_error(self, order_error):
        pass

    def on_cancel_error(self, cancel_error):
        pass

    def on_order_stock_async_response(self, response):
        pass

    def on_account_status(self, status):
        pass


def _env_int(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return int(value)


def _env_float(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return float(value)


def _account_id(account, fallback=""):
    if account is None:
        return str(fallback or "")
    if isinstance(account, str):
        return account
    for name in ("account_id", "m_strAccountID", "id"):
        value = getattr(account, name, None)
        if value:
            return str(value)
    if isinstance(account, dict):
        return str(account.get("account_id") or account.get("id") or fallback or "")
    return str(fallback or "")


def _action_to_order_type(action):
    text = str(action or "").upper()
    if text in ("BUY", str(STOCK_BUY)):
        return STOCK_BUY
    if text in ("SELL", str(STOCK_SELL)):
        return STOCK_SELL
    return 0


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list):
        return value
    return [value]


def _normalize_code_for_filter(code):
    text = str(code or "").strip().upper()
    if "." not in text:
        return text
    return text.split(".", 1)[0]


def _is_hs_a_share(code):
    text = str(code or "").strip().upper()
    pure = _normalize_code_for_filter(text)
    if not (len(pure) == 6 and pure.isdigit()):
        return False
    if text.endswith(".SH"):
        return pure.startswith(("600", "601", "603", "605", "688", "689"))
    if text.endswith(".SZ"):
        return pure.startswith(("000", "001", "002", "003", "300", "301"))
    return pure.startswith(
        ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")
    )


class BigQmtRpcClient:
    def __init__(
        self,
        account_id=None,
        redis_client=None,
        redis_config=None,
        timeout_seconds=None,
    ):
        redis_config = dict(redis_config or {})
        self.account_id = str(
            account_id
            or redis_config.get("account_id")
            or os.environ.get("BIGQMT_ACCOUNT_ID")
            or ""
        )
        self.redis_client = redis_client
        self.redis_config = {
            "host": redis_config.get("host") or os.environ.get("BIGQMT_REDIS_HOST", "127.0.0.1"),
            "port": int(redis_config.get("port") or _env_int("BIGQMT_REDIS_PORT", 6379)),
            "db": int(redis_config.get("db") or _env_int("BIGQMT_REDIS_DB", 5)),
            "username": redis_config.get("username", os.environ.get("BIGQMT_REDIS_USERNAME") or ""),
            "password": redis_config.get("password", os.environ.get("BIGQMT_REDIS_PASSWORD") or ""),
        }
        self.timeout_seconds = float(
            timeout_seconds
            if timeout_seconds is not None
            else _env_float("BIGQMT_RPC_TIMEOUT_SECONDS", 6.0)
        )

    def _redis(self):
        if self.redis_client is None:
            import redis

            cfg = dict(self.redis_config)
            if not cfg.get("username"):
                cfg.pop("username", None)
            if not cfg.get("password"):
                cfg.pop("password", None)
            self.redis_client = redis.Redis(**cfg)
        return self.redis_client

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        target_account = str(account_id or self.account_id or "")
        if not target_account:
            raise ValueError("Big QMT account_id is required")
        response = call_redis_rpc(
            self._redis(),
            account_id=target_account,
            method=method,
            params=params or {},
            timeout_seconds=self.timeout_seconds if timeout_seconds is None else timeout_seconds,
        )
        if not response.get("ok"):
            raise RuntimeError(response.get("error") or "Big QMT RPC failed: %s" % method)
        return response.get("data")


class BigQmtXtData:
    def __init__(self, client):
        self.client = client

    def get_full_tick(self, code_list):
        codes = list(code_list or [])
        if not codes:
            return {}
        return self.client.call("get_full_tick", {"codes": codes}) or {}

    def get_instrument_detail(self, stock_code):
        return self.client.call("get_instrument_detail", {"code": stock_code}) or {}

    def get_instrumentdetail(self, stock_code):
        return self.get_instrument_detail(stock_code)

    def get_stock_list_in_sector(self, sector_name):
        name = str(sector_name or "")
        if name in ("沪深A股", "沪深A股".encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")):
            ticks = self.get_full_tick(["SH", "SZ"])
            return sorted(code for code in ticks.keys() if _is_hs_a_share(code))
        raise NotImplementedError("sector is not supported by BigQMT compat: %s" % sector_name)

    def subscribe_whole_quote(self, code_list, callback=None):
        data = self.get_full_tick(code_list)
        if callback is not None:
            callback(data)
        return data

    def unsubscribe_quote(self, *args, **kwargs):
        return 0


class BigQmtXtTrader:
    def __init__(
        self,
        path=None,
        session_id=None,
        account_id=None,
        redis_client=None,
        redis_config=None,
        timeout_seconds=None,
    ):
        self.path = path
        self.session_id = session_id
        self.client = BigQmtRpcClient(
            account_id=account_id,
            redis_client=redis_client,
            redis_config=redis_config,
            timeout_seconds=timeout_seconds,
        )
        self.callback = None

    def register_callback(self, callback):
        self.callback = callback
        return 0

    def start(self):
        return 0

    def connect(self):
        if self.client.account_id:
            self.client.call("ping")
        return 0

    def subscribe(self, account):
        if not self.client.account_id:
            self.client.account_id = _account_id(account)
        return 0

    def stop(self):
        return 0

    def run_forever(self):
        while True:
            time.sleep(3600)

    def query_stock_asset(self, account):
        account_id = _account_id(account, self.client.account_id)
        data = self.client.call("query_stock_asset", {"account_id": account_id}, account_id=account_id) or {}
        cash = data.get("cash")
        total_asset = data.get("total_asset")
        market_value = data.get("market_value")
        if market_value is None and cash is not None and total_asset is not None:
            market_value = _safe_float(total_asset) - _safe_float(cash)
        return CompatObject(
            account_id=account_id,
            cash=_safe_float(cash, 0.0) if cash is not None else None,
            available_cash=_safe_float(cash, 0.0) if cash is not None else None,
            total_asset=_safe_float(total_asset, 0.0) if total_asset is not None else None,
            market_value=_safe_float(market_value, 0.0) if market_value is not None else 0.0,
        )

    def query_stock_positions(self, account):
        account_id = _account_id(account, self.client.account_id)
        data = self.client.call("query_stock_positions", {"account_id": account_id}, account_id=account_id) or {}
        positions = []
        for item in _as_list(data):
            stock_code = str(item.get("stock_code") or "")
            volume = _safe_int(item.get("volume"))
            available = _safe_int(item.get("available", item.get("can_use_volume")))
            cost = _safe_float(item.get("cost", item.get("avg_price")))
            positions.append(
                CompatObject(
                    account_id=account_id,
                    stock_code=stock_code,
                    stock_name=str(item.get("stock_name") or ""),
                    volume=volume,
                    can_use_volume=available,
                    enable_amount=available,
                    available_amount=available,
                    avg_price=cost,
                    price=cost,
                    open_price=cost,
                    cost_price=cost,
                    yesterday_volume=_safe_int(item.get("yesterday_volume"), volume),
                )
            )
        return positions

    def query_stock_position(self, account, stock_code):
        account_id = _account_id(account, self.client.account_id)
        data = self.client.call(
            "query_stock_position",
            {"account_id": account_id, "stock_code": stock_code},
            account_id=account_id,
        )
        if not data:
            return None
        return [
            CompatObject(
                account_id=account_id,
                stock_code=str(item.get("stock_code") or ""),
                stock_name=str(item.get("stock_name") or ""),
                volume=_safe_int(item.get("volume")),
                can_use_volume=_safe_int(item.get("available", item.get("can_use_volume"))),
                enable_amount=_safe_int(item.get("available", item.get("can_use_volume"))),
                available_amount=_safe_int(item.get("available", item.get("can_use_volume"))),
                avg_price=_safe_float(item.get("cost", item.get("avg_price"))),
                price=_safe_float(item.get("cost", item.get("avg_price"))),
                open_price=_safe_float(item.get("cost", item.get("avg_price"))),
                cost_price=_safe_float(item.get("cost", item.get("avg_price"))),
                yesterday_volume=_safe_int(item.get("yesterday_volume"), _safe_int(item.get("volume"))),
            )
            for item in [data]
        ][0]

    def query_stock_orders(self, account, cancelable_only=False, strategy_name="bigqmt_signal_trader"):
        account_id = _account_id(account, self.client.account_id)
        data = self.client.call(
            "query_stock_orders",
            {
                "account_id": account_id,
                "cancelable_only": bool(cancelable_only),
                "strategy_name": strategy_name,
            },
            account_id=account_id,
        ) or []
        return [self._order_from_dict(account_id, item) for item in _as_list(data)]

    def query_stock_order(self, account, order_id):
        order_id = str(order_id or "")
        for order in self.query_stock_orders(account, cancelable_only=False):
            if str(order.order_id) == order_id or str(order.order_sysid) == order_id:
                return order
        return None

    def query_stock_trades(self, account, strategy_name="bigqmt_signal_trader"):
        account_id = _account_id(account, self.client.account_id)
        data = self.client.call(
            "query_stock_trades",
            {"account_id": account_id, "strategy_name": strategy_name},
            account_id=account_id,
        ) or []
        return [self._trade_from_dict(account_id, item) for item in _as_list(data)]

    def order_stock(
        self,
        account,
        stock_code,
        order_type,
        order_volume,
        price_type,
        price,
        strategy_name,
        order_remark,
    ):
        account_id = _account_id(account, self.client.account_id)
        data = self.client.call(
            "order_stock",
            {
                "account_id": account_id,
                "stock_code": stock_code,
                "order_type": order_type,
                "order_volume": order_volume,
                "price_type": price_type,
                "price": price,
                "strategy_name": strategy_name,
                "order_remark": order_remark,
            },
            account_id=account_id,
        ) or {}
        return data.get("order_sys_id") or data.get("user_order_id") or -1

    def order_stock_async(self, *args, **kwargs):
        return self.order_stock(*args, **kwargs)

    def cancel_order_stock_sysid(self, account, market, order_sysid):
        account_id = _account_id(account, self.client.account_id)
        data = self.client.call(
            "cancel_order_stock_sysid",
            {
                "account_id": account_id,
                "market": market,
                "order_sysid": order_sysid,
            },
            account_id=account_id,
        ) or {}
        return bool(data.get("success", data))

    def cancel_order_stock(self, account, order_id):
        return self.cancel_order_stock_sysid(account, "", order_id)

    def query_ipo_data(self):
        return {}

    def query_new_purchase_limit(self, account):
        return {}

    def _order_from_dict(self, account_id, item):
        action = item.get("action")
        order_type = _action_to_order_type(action)
        order_sysid = str(item.get("order_sys_id") or item.get("order_sysid") or item.get("order_id") or "")
        return CompatObject(
            account_id=account_id,
            stock_code=str(item.get("stock_code") or ""),
            order_type=order_type,
            order_status=_safe_int(item.get("status", item.get("order_status")), ORDER_UNKNOWN),
            order_volume=_safe_int(item.get("volume", item.get("order_volume"))),
            traded_volume=_safe_int(item.get("traded_volume")),
            price=_safe_float(item.get("price")),
            order_sysid=order_sysid,
            order_id=order_sysid or str(item.get("user_order_id") or ""),
            strategy_name=str(item.get("strategy_name") or ""),
            order_remark=str(item.get("remark") or item.get("user_order_id") or ""),
        )

    def _trade_from_dict(self, account_id, item):
        action = item.get("action")
        order_type = _action_to_order_type(action)
        order_sysid = str(item.get("order_sys_id") or item.get("order_sysid") or "")
        trade_id = str(item.get("trade_id") or "")
        return CompatObject(
            account_id=account_id,
            stock_code=str(item.get("stock_code") or ""),
            order_type=order_type,
            order_sysid=order_sysid,
            order_id=order_sysid,
            trade_id=trade_id,
            traded_volume=_safe_int(item.get("volume", item.get("traded_volume"))),
            traded_price=_safe_float(item.get("price", item.get("traded_price"))),
            traded_at=str(item.get("traded_at") or ""),
        )


XtQuantTrader = BigQmtXtTrader


_default_client = None
xt_trader = None
xtdata = None


def configure(account_id=None, redis_client=None, redis_config=None, timeout_seconds=None):
    global _default_client, xt_trader, xtdata
    _default_client = BigQmtRpcClient(
        account_id=account_id,
        redis_client=redis_client,
        redis_config=redis_config,
        timeout_seconds=timeout_seconds,
    )
    if xt_trader is None:
        xt_trader = BigQmtXtTrader(account_id=_default_client.account_id, redis_client=_default_client.redis_client)
    xt_trader.client = _default_client
    if xtdata is None:
        xtdata = BigQmtXtData(_default_client)
    else:
        xtdata.client = _default_client
    return xt_trader, xtdata


def get_default_client():
    global _default_client
    if _default_client is None:
        configure()
    return _default_client


configure()


__all__ = [
    "BigQmtRpcClient",
    "BigQmtXtData",
    "BigQmtXtTrader",
    "CompatObject",
    "StockAccount",
    "XtQuantTrader",
    "XtQuantTraderCallback",
    "configure",
    "get_default_client",
    "xt_trader",
    "xtdata",
]
