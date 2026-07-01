"""MiniQMT-style client objects backed by Big QMT Redis RPC.

This module is the replacement edge for existing code that already calls
``xt_trader.query_stock_positions(...)`` or ``xtdata.get_full_tick(...)``.
The Big QMT process remains the only place that touches QMT runtime APIs.
"""

import os
import json
import time
import importlib
from typing import Any, Dict, Iterable, List, Optional

from .full_tick_cache import request_full_tick_cache, wait_full_tick_cache
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

CLIENT_CONFIG_MODULE_ENV = "BIGQMT_CLIENT_CONFIG_MODULE"
DEFAULT_CLIENT_CONFIG_MODULES = (
    "bigqmt_signal_trader_client_config",
    "bigqmt_signal_trader_local_config",
)

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


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _bool_value(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _import_optional_module(module_name):
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise


def load_client_config(module_name=None):
    """Load local private client config without requiring environment variables."""
    candidates = []
    selected = module_name or os.environ.get(CLIENT_CONFIG_MODULE_ENV)
    if selected:
        candidates.append(str(selected))
    candidates.extend(name for name in DEFAULT_CLIENT_CONFIG_MODULES if name not in candidates)

    for candidate in candidates:
        module = _import_optional_module(candidate)
        if module is None:
            continue
        redis_config = dict(getattr(module, "BIGQMT_REDIS_CONFIG", {}) or {})
        account_id = getattr(module, "BIGQMT_ACCOUNT_ID", None) or redis_config.get("account_id")
        timeout_seconds = getattr(module, "BIGQMT_RPC_TIMEOUT_SECONDS", None)
        if timeout_seconds is None:
            timeout_seconds = redis_config.get("rpc_timeout_seconds")
        full_tick_cache_config = dict(getattr(module, "BIGQMT_FULL_TICK_CACHE_CONFIG", {}) or {})
        for key in (
            "full_tick_cache_enabled",
            "full_tick_demand_ttl_seconds",
            "full_tick_cache_ttl_seconds",
            "full_tick_wait_seconds",
            "full_tick_poll_interval_seconds",
        ):
            if key in redis_config:
                full_tick_cache_config[key] = redis_config[key]
        return {
            "module": candidate,
            "account_id": account_id,
            "redis_config": redis_config,
            "timeout_seconds": timeout_seconds,
            "full_tick_cache_config": full_tick_cache_config,
        }
    return {}


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


def _restore_jsonable(value):
    if isinstance(value, dict):
        marker = value.get("__bigqmt_type__")
        if marker == "DataFrame":
            try:
                import pandas as pd

                return pd.DataFrame(value.get("records") or [], columns=value.get("columns") or None)
            except Exception:
                return value.get("records") or []
        if marker == "Series":
            try:
                import pandas as pd

                return pd.Series(value.get("data") or {})
            except Exception:
                return value.get("data") or {}
        return {key: _restore_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_jsonable(item) for item in value]
    return value


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
        client_config = load_client_config()
        config_redis = dict(client_config.get("redis_config") or {})
        redis_config = dict(redis_config or {})
        merged_redis_config = dict(config_redis)
        merged_redis_config.update(redis_config)
        self.account_id = str(
            account_id
            or merged_redis_config.get("account_id")
            or client_config.get("account_id")
            or os.environ.get("BIGQMT_ACCOUNT_ID")
            or ""
        )
        self.redis_client = redis_client
        self.redis_config = {
            "host": merged_redis_config.get("host") or os.environ.get("BIGQMT_REDIS_HOST", "127.0.0.1"),
            "port": int(merged_redis_config.get("port") or _env_int("BIGQMT_REDIS_PORT", 6379)),
            "db": int(merged_redis_config.get("db") or _env_int("BIGQMT_REDIS_DB", 5)),
            "username": merged_redis_config.get("username", os.environ.get("BIGQMT_REDIS_USERNAME") or ""),
            "password": merged_redis_config.get("password", os.environ.get("BIGQMT_REDIS_PASSWORD") or ""),
        }
        config_timeout = client_config.get("timeout_seconds")
        self.timeout_seconds = float(
            timeout_seconds
            if timeout_seconds is not None
            else config_timeout
            if config_timeout is not None
            else _env_float("BIGQMT_RPC_TIMEOUT_SECONDS", 6.0)
        )
        full_tick_cache_config = dict(client_config.get("full_tick_cache_config") or {})
        self.full_tick_cache_config = {
            "enabled": _bool_value(
                full_tick_cache_config.get("enabled", full_tick_cache_config.get("full_tick_cache_enabled")),
                _env_bool("BIGQMT_FULL_TICK_CACHE_ENABLED", False),
            ),
            "demand_ttl_seconds": float(
                full_tick_cache_config.get("demand_ttl_seconds")
                or full_tick_cache_config.get("full_tick_demand_ttl_seconds")
                or _env_float("BIGQMT_FULL_TICK_DEMAND_TTL_SECONDS", 10.0)
            ),
            "cache_ttl_seconds": float(
                full_tick_cache_config.get("cache_ttl_seconds")
                or full_tick_cache_config.get("full_tick_cache_ttl_seconds")
                or _env_float("BIGQMT_FULL_TICK_CACHE_TTL_SECONDS", 10.0)
            ),
            "wait_seconds": float(
                full_tick_cache_config.get("wait_seconds")
                or full_tick_cache_config.get("full_tick_wait_seconds")
                or _env_float("BIGQMT_FULL_TICK_WAIT_SECONDS", 3.5)
            ),
            "poll_interval_seconds": float(
                full_tick_cache_config.get("poll_interval_seconds")
                or full_tick_cache_config.get("full_tick_poll_interval_seconds")
                or _env_float("BIGQMT_FULL_TICK_POLL_INTERVAL_SECONDS", 0.2)
            ),
        }

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
        return _restore_jsonable(response.get("data"))

    def publish_event(self, event_type, payload, stream_template="bigqmt:quote_events:{account_id}"):
        account_id = str(self.account_id or "")
        event = {
            "event_type": str(event_type),
            "account_id": account_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "payload": payload or {},
        }
        raw = json.dumps(event, ensure_ascii=False, default=str)
        stream_key = stream_template.format(account_id=account_id)
        redis_client = self._redis()
        try:
            redis_client.xadd(stream_key, {"payload": raw}, maxlen=1000, approximate=True)
        except Exception:
            pass
        try:
            redis_client.publish(stream_key, raw)
        except Exception:
            pass
        return event

    def save_quote_subscription(self, seq, payload, active=True):
        account_id = str(self.account_id or "")
        key = "bigqmt:quote_subscriptions:%s" % account_id
        redis_client = self._redis()
        if active:
            value = json.dumps(payload or {}, ensure_ascii=False, default=str)
            try:
                redis_client.hset(key, str(seq), value)
            except Exception:
                pass
        else:
            try:
                redis_client.hdel(key, str(seq))
            except Exception:
                pass


class BigQmtXtData:
    def __init__(self, client):
        self.client = client
        self._subscribe_seq = int(time.time() * 1000)

    def _next_seq(self):
        self._subscribe_seq += 1
        return self._subscribe_seq

    def _call(self, method, **params):
        return self.client.call(method, params)

    def get_full_tick(self, code_list):
        codes = list(code_list or [])
        if not codes:
            return {}
        cache_config = dict(getattr(self.client, "full_tick_cache_config", {}) or {})
        if _bool_value(cache_config.get("enabled"), False):
            redis_client = self.client._redis()
            request_full_tick_cache(
                redis_client,
                self.client.account_id,
                codes,
                demand_ttl_seconds=cache_config.get("demand_ttl_seconds", 10),
                cache_ttl_seconds=cache_config.get("cache_ttl_seconds", 10),
            )
            data = wait_full_tick_cache(
                redis_client,
                self.client.account_id,
                codes,
                max_age_seconds=cache_config.get("cache_ttl_seconds", 10),
                wait_seconds=cache_config.get("wait_seconds", 3.5),
                poll_interval_seconds=cache_config.get("poll_interval_seconds", 0.2),
            )
            if data is not None:
                return data
            upper_codes = {str(code).strip().upper() for code in codes}
            if upper_codes & {"SH", "SZ", "BJ", "HK"}:
                # Whole-market snapshots must stay on the demand cache. A live RPC
                # here would ship ~50k rows on every miss, so surface the timeout.
                raise TimeoutError("full tick redis cache timeout: %s" % ",".join(str(code) for code in codes))
            # Symbol-list miss (cold start / expired snapshot): fall back to a live
            # RPC so the first call is ~ms instead of a hard wait_seconds stall.
            return self.client.call("get_full_tick", {"codes": codes}) or {}
        upper_codes = {str(code).strip().upper() for code in codes}
        timeout_seconds = 30 if upper_codes & {"SH", "SZ", "BJ", "HK"} else None
        return self.client.call("get_full_tick", {"codes": codes}, timeout_seconds=timeout_seconds) or {}

    def get_instrument_detail(self, stock_code):
        return self.client.call("get_instrument_detail", {"code": stock_code}) or {}

    def get_instrumentdetail(self, stock_code):
        return self.get_instrument_detail(stock_code)

    def get_instrument_type(self, stock_code, variety_list=None):
        return self._call("get_instrument_type", code=stock_code, variety_list=variety_list)

    def get_stock_list_in_sector(self, sector_name, real_timetag=-1):
        name = str(sector_name or "")
        try:
            return self._call("get_stock_list_in_sector", sector_name=sector_name, real_timetag=real_timetag) or []
        except Exception:
            pass
        if name in ("沪深A股", "沪深A股".encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")):
            ticks = self.get_full_tick(["SH", "SZ"])
            return sorted(code for code in ticks.keys() if _is_hs_a_share(code))
        raise NotImplementedError("sector is not supported by BigQMT compat: %s" % sector_name)

    def get_market_data(
        self,
        field_list=None,
        stock_list=None,
        period="1d",
        start_time="",
        end_time="",
        count=-1,
        dividend_type="none",
        fill_data=True,
    ):
        return self._call(
            "get_market_data",
            field_list=list(field_list or []),
            stock_list=list(stock_list or []),
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            fill_data=fill_data,
        )

    def get_market_data_ex(
        self,
        field_list=None,
        stock_list=None,
        period="1d",
        start_time="",
        end_time="",
        count=-1,
        dividend_type="none",
        fill_data=True,
    ):
        return self._call(
            "get_market_data_ex",
            field_list=list(field_list or []),
            stock_list=list(stock_list or []),
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            fill_data=fill_data,
        )

    def get_local_data(
        self,
        field_list=None,
        stock_list=None,
        period="1d",
        start_time="",
        end_time="",
        count=-1,
        dividend_type="none",
        fill_data=True,
        data_dir=None,
    ):
        return self._call(
            "get_local_data",
            field_list=list(field_list or []),
            stock_list=list(stock_list or []),
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            fill_data=fill_data,
            data_dir=data_dir,
        )

    def subscribe_quote(self, stock_code, period="1d", start_time="", end_time="", count=0, callback=None):
        seq = self._next_seq()
        payload = {
            "seq": seq,
            "stock_code": stock_code,
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "count": count,
        }
        self.client.save_quote_subscription(seq, payload, active=True)
        self.client.publish_event("subscribe_quote", payload)
        if callback is not None:
            try:
                if str(period).lower() in ("tick", "full_tick"):
                    callback(self.get_full_tick([stock_code]))
                else:
                    callback(
                        self.get_market_data_ex(
                            stock_list=[stock_code],
                            period=period,
                            start_time=start_time,
                            end_time=end_time,
                            count=count,
                        )
                    )
            except Exception:
                pass
        return seq

    def subscribe_quote2(self, stock_code, period="1d", start_time="", end_time="", count=0, dividend_type=None, callback=None):
        return self.subscribe_quote(
            stock_code=stock_code,
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            callback=callback,
        )

    def subscribe_whole_quote(self, code_list, callback=None):
        seq = self._next_seq()
        payload = {"seq": seq, "code_list": list(code_list or []), "period": "full_tick"}
        self.client.save_quote_subscription(seq, payload, active=True)
        self.client.publish_event("subscribe_whole_quote", payload)
        if callback is not None:
            callback(self.get_full_tick(code_list))
        return seq

    def unsubscribe_quote(self, seq):
        payload = {"seq": seq}
        self.client.save_quote_subscription(seq, payload, active=False)
        self.client.publish_event("unsubscribe_quote", payload)
        return 0

    def run(self):
        while True:
            time.sleep(3600)

    def get_divid_factors(self, stock_code, start_time="", end_time=""):
        return self._call("get_divid_factors", stock_code=stock_code, start_time=start_time, end_time=end_time)

    def download_history_data(self, stock_code, period, start_time="", end_time="", incrementally=None):
        return self._call(
            "download_history_data",
            stock_code=stock_code,
            period=period,
            start_time=start_time,
            end_time=end_time,
            incrementally=incrementally,
        )

    def download_history_data2(self, stock_list, period, start_time="", end_time="", callback=None, incrementally=None):
        result = self._call(
            "download_history_data2",
            stock_list=list(stock_list or []),
            period=period,
            start_time=start_time,
            end_time=end_time,
            incrementally=incrementally,
        )
        if callback is not None:
            callback(result)
        return result

    def get_trading_dates(self, market, start_time="", end_time="", count=-1):
        return self._call("get_trading_dates", market=market, start_time=start_time, end_time=end_time, count=count)

    def get_holidays(self):
        return self._call("get_holidays")

    def download_holiday_data(self, incrementally=True):
        return self._call("download_holiday_data", incrementally=incrementally)

    def get_ipo_info(self, start_time="", end_time=""):
        return self._call("get_ipo_info", start_time=start_time, end_time=end_time)

    def get_etf_info(self):
        return self._call("get_etf_info")

    def download_etf_info(self):
        return self._call("download_etf_info")

    def get_option_list(self, undl_code, dedate, opttype="", isavailavle=False):
        return self._call("get_option_list", undl_code=undl_code, dedate=dedate, opttype=opttype, isavailavle=isavailavle)

    def get_his_option_list(self, undl_code, dedate):
        return self._call("get_his_option_list", undl_code=undl_code, dedate=dedate)

    def get_his_option_list_batch(self, undl_code, start_time="", end_time=""):
        return self._call("get_his_option_list_batch", undl_code=undl_code, start_time=start_time, end_time=end_time)

    def get_financial_data(self, stock_list, table_list=None, start_time="", end_time="", report_type="report_time"):
        return self._call(
            "get_financial_data",
            stock_list=list(stock_list or []),
            table_list=list(table_list or []),
            start_time=start_time,
            end_time=end_time,
            report_type=report_type,
        )

    def download_financial_data(self, stock_list, table_list=None, start_time="", end_time="", incrementally=None):
        return self._call(
            "download_financial_data",
            stock_list=list(stock_list or []),
            table_list=list(table_list or []),
            start_time=start_time,
            end_time=end_time,
            incrementally=incrementally,
        )

    def download_financial_data2(self, stock_list, table_list=None, start_time="", end_time="", callback=None):
        result = self._call(
            "download_financial_data2",
            stock_list=list(stock_list or []),
            table_list=list(table_list or []),
            start_time=start_time,
            end_time=end_time,
        )
        if callback is not None:
            callback(result)
        return result

    def get_sector_list(self):
        return self._call("get_sector_list")

    def get_sector_info(self, sector_name=""):
        return self._call("get_sector_info", sector_name=sector_name)

    def get_markets(self):
        return self._call("get_markets")

    def get_market_last_trade_date(self, market):
        return self._call("get_market_last_trade_date", market=market)

    def call_formula(self, formula_name, stock_code, period, start_time="", end_time="", count=-1, dividend_type=None, extend_param=None):
        return self._call(
            "call_formula",
            formula_name=formula_name,
            stock_code=stock_code,
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            extend_param=extend_param or {},
        )

    def subscribe_formula(self, formula_name, stock_code, period, start_time="", end_time="", count=-1, dividend_type=None, extend_param=None, callback=None):
        result = self._call(
            "subscribe_formula",
            formula_name=formula_name,
            stock_code=stock_code,
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            extend_param=extend_param or {},
        )
        if callback is not None:
            callback(result)
        return result

    def unsubscribe_formula(self, request_id):
        return self._call("unsubscribe_formula", request_id=request_id)

    def get_formula_result(self, request_id, start_time="", end_time="", count=-1, timeout_second=-1):
        return self._call(
            "get_formula_result",
            request_id=request_id,
            start_time=start_time,
            end_time=end_time,
            count=count,
            timeout_second=timeout_second,
        )

    def gen_factor_index(self, data_name, formula_name, vars, sector_list, start_time="", end_time="", period="1d", dividend_type="none"):
        return self._call(
            "gen_factor_index",
            data_name=data_name,
            formula_name=formula_name,
            vars=vars,
            sector_list=list(sector_list or []),
            start_time=start_time,
            end_time=end_time,
            period=period,
            dividend_type=dividend_type,
        )


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

    def _cached_position_snapshot(self, account_id):
        key = "bigqmt:positions:%s" % str(account_id or self.client.account_id or "")
        try:
            raw = self.client._redis().get(key)
        except Exception:
            return {}
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw))
        except Exception:
            return {}

    def _cached_positions(self, account_id):
        snapshot = self._cached_position_snapshot(account_id)
        positions = snapshot.get("positions") if isinstance(snapshot, dict) else None
        if isinstance(positions, dict):
            return positions
        if isinstance(positions, list):
            return {str(item.get("stock_code") or idx): item for idx, item in enumerate(positions)}
        return {}

    def _cached_asset(self, account_id):
        snapshot = self._cached_position_snapshot(account_id)
        asset = snapshot.get("asset") if isinstance(snapshot, dict) else None
        return asset if isinstance(asset, dict) else {}

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
        try:
            data = self.client.call("query_stock_asset", {"account_id": account_id}, account_id=account_id) or {}
        except Exception:
            data = self._cached_asset(account_id)
            if not data:
                raise
        if data.get("cash") is None and data.get("total_asset") is None:
            data = self._cached_asset(account_id) or data
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
        try:
            data = self.client.call("query_stock_positions", {"account_id": account_id}, account_id=account_id) or {}
        except Exception:
            data = self._cached_positions(account_id)
            if not data:
                raise
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
        try:
            data = self.client.call(
                "query_stock_position",
                {"account_id": account_id, "stock_code": stock_code},
                account_id=account_id,
            )
        except Exception:
            normalized = str(stock_code or "").strip().upper()
            data = None
            for code, item in self._cached_positions(account_id).items():
                if str(code).upper() == normalized or str(code).split(".", 1)[0].upper() == normalized:
                    data = item
                    break
            if data is None:
                raise
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
    "load_client_config",
    "xt_trader",
    "xtdata",
]
