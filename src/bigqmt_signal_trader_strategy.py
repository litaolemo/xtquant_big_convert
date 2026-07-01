# coding: utf-8
"""ThinkTrader Big QMT strategy entry.

Keep this entry file ASCII-only because QMT's strategy editor may save the
generated strategy file with a local code page while preserving this coding
header. Business logic stays in the importable package.
"""

import datetime
import time

from bigqmt_signal_trader.adapter_factory import build_app as _default_build_app
from bigqmt_signal_trader.runner import (
    forward_order_event,
    forward_trade_event,
    init_app,
    reset_app as _reset_runner_app,
    sync_positions_app,
    tick_app,
)
from bigqmt_signal_trader.runtime_bigqmt import BigQmtRuntimeAdapter


_app_factory = None
_account_id = ""
_config = {}
_qmt_api = {}
_adjust_logged = False
_rpc_service = None
_scheduled_adjust = False
_last_full_tick_refresh_at = 0.0
_last_full_tick_market_refresh_at = 0.0
# Observed adjust cadence, so a mis-scheduled run_time (e.g. clamped to bar
# cadence) is visible in the logs instead of silently costing latency.
_adjust_tick_stats = {"last_ts": 0.0, "count": 0, "window_start": 0.0, "sum": 0.0, "min": 0.0, "max": 0.0}


def set_app_factory(factory):
    global _app_factory
    _app_factory = factory


def set_account_id(account_id):
    global _account_id
    _account_id = str(account_id or "")


def configure(**kwargs):
    _config.update(kwargs)


def bind_qmt_api(passorder_func=None, cancel_func=None, get_trade_detail_data_func=None):
    if passorder_func is not None:
        _qmt_api["passorder"] = passorder_func
    if cancel_func is not None:
        _qmt_api["cancel"] = cancel_func
    if get_trade_detail_data_func is not None:
        _qmt_api["get_trade_detail_data"] = get_trade_detail_data_func


def reset_app():
    global _adjust_logged, _rpc_service, _scheduled_adjust, _last_full_tick_refresh_at, _last_full_tick_market_refresh_at
    _adjust_logged = False
    _scheduled_adjust = False
    _last_full_tick_refresh_at = 0.0
    _last_full_tick_market_refresh_at = 0.0
    _adjust_tick_stats.update({"last_ts": 0.0, "count": 0, "window_start": 0.0, "sum": 0.0, "min": 0.0, "max": 0.0})
    if _rpc_service is not None:
        try:
            _rpc_service.stop()
        except Exception:
            pass
    _rpc_service = None
    _reset_runner_app()


def _resolve_runtime_name(name):
    if name in _qmt_api:
        return _qmt_api[name]
    if name in globals():
        return globals()[name]
    try:
        import builtins
        return getattr(builtins, name)
    except Exception:
        return None


def _detect_account_id(context_info=None):
    if _account_id:
        return _account_id
    try:
        import importlib
        import bigqmt_signal_trader_local_config as _local_config

        _local_config = importlib.reload(_local_config)
        value = str(
            getattr(_local_config, "BIGQMT_ACCOUNT_ID", "")
            or (getattr(_local_config, "BIGQMT_REDIS_CONFIG", {}) or {}).get("account_id")
            or ""
        )
        if value:
            return value
    except Exception:
        pass
    for name in ("account", "account_id", "accountID"):
        value = _resolve_runtime_name(name)
        if value:
            return str(value)
    if context_info is not None:
        for name in ("account", "account_id", "accountID", "m_strAccountID"):
            value = getattr(context_info, name, None)
            if value:
                return str(value)
        for name in ("get_account", "get_account_id", "getAccountID"):
            func = getattr(context_info, name, None)
            if callable(func):
                try:
                    value = func()
                except Exception:
                    value = None
                if value:
                    return str(value)
    return ""


def _build_config():
    config = dict(_config)
    if _account_id:
        config["account_id"] = _account_id
    qmt_api = dict(config.get("qmt_api") or {})
    qmt_api.setdefault("passorder", _resolve_runtime_name("passorder"))
    qmt_api.setdefault("cancel", _resolve_runtime_name("cancel"))
    qmt_api.setdefault("get_trade_detail_data", _resolve_runtime_name("get_trade_detail_data"))
    config["qmt_api"] = qmt_api
    return config


def _build_app(context_info):
    if _app_factory is not None:
        return _app_factory(context_info)
    return _default_build_app(context_info, _build_config())


def _config_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _build_rpc_service(context_info, app, config):
    rpc_config = dict(config.get("rpc") or {})
    enabled = _config_bool(config.get("enable_rpc"), False) or _config_bool(rpc_config.get("enabled"), False)
    if not enabled:
        return None

    import importlib
    from bigqmt_signal_trader.adapters import redis_common as _redis_common
    from bigqmt_signal_trader.adapters import market_bigqmt as _market_bigqmt
    from bigqmt_signal_trader.adapters import position_bigqmt as _position_bigqmt
    from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers, RedisPubSubRpcService

    # QMT keeps strategy modules in the same process between editor reruns.
    # Reload adapters here so synced local package fixes take effect immediately.
    _redis_common = importlib.reload(_redis_common)
    _market_bigqmt = importlib.reload(_market_bigqmt)
    _position_bigqmt = importlib.reload(_position_bigqmt)
    build_redis_client = _redis_common.build_redis_client
    BigQmtMarketDataProvider = _market_bigqmt.BigQmtMarketDataProvider
    BigQmtPositionProvider = _position_bigqmt.BigQmtPositionProvider

    qmt_api = dict(config.get("qmt_api") or {})
    redis_config = dict(config.get("redis") or {})
    redis_config.update(dict(rpc_config.get("redis") or {}))
    listen_redis_config = dict(redis_config)
    listen_redis_config["socket_timeout"] = None
    redis_client = rpc_config.get("redis_client") or config.get("redis_client") or build_redis_client(listen_redis_config)
    response_redis_client = (
        rpc_config.get("response_redis_client")
        or config.get("response_redis_client")
        or build_redis_client(redis_config)
    )
    account_id = str(rpc_config.get("account_id") or config.get("account_id") or _account_id or "")
    if not account_id:
        print("[bigqmt_rpc] disabled: account_id is empty")
        return None
    allow_order_methods = _config_bool(rpc_config.get("allow_order_methods"), False)
    handlers = BigQmtRpcHandlers(
        account_id=account_id,
        market_data=BigQmtMarketDataProvider(context_info),
        position_provider=BigQmtPositionProvider(
            get_trade_detail_data_func=qmt_api.get("get_trade_detail_data"),
            account_type=config.get("account_type", "STOCK"),
        ),
        order_gateway=getattr(app, "order_gateway", None),
        position_sync_sink=getattr(app, "position_sync_sink", None),
        allow_order_methods=allow_order_methods,
        allowed_methods=rpc_config.get("allowed_methods"),
    )
    process_in_listener = _config_bool(rpc_config.get("process_in_listener"), True)
    listener_methods = rpc_config.get("listener_methods") or ("*",)
    background_threads = _config_bool(rpc_config.get("background_threads"), False)
    print(
        "[bigqmt_rpc] mode process_in_listener=%s listener_methods=%s allow_order_methods=%s background_threads=%s"
        % (process_in_listener, listener_methods, allow_order_methods, background_threads)
    )
    return RedisPubSubRpcService(
        redis_client=redis_client,
        response_redis_client=response_redis_client,
        handlers=handlers,
        account_id=account_id,
        request_channel_template=rpc_config.get("request_channel_template", "bigqmt:rpc:req:{account_id}"),
        response_channel_template=rpc_config.get("response_channel_template", "bigqmt:rpc:resp:{account_id}:{request_id}"),
        response_key_template=rpc_config.get("response_key_template", "bigqmt:rpc:resp:{account_id}:{request_id}"),
        response_ttl_seconds=int(rpc_config.get("response_ttl_seconds", 60)),
        max_queue_size=int(rpc_config.get("max_queue_size", 200)),
        process_in_listener=process_in_listener,
        listener_methods=listener_methods,
        background_threads=background_threads,
        debug_log_limit=int(rpc_config.get("debug_log_limit", 5)),
    )


def _start_rpc_service(context_info, app, config):
    global _rpc_service
    if _rpc_service is not None:
        return _rpc_service
    _rpc_service = _build_rpc_service(context_info, app, config)
    if _rpc_service is not None:
        _rpc_service.start()
    return _rpc_service


def _drain_rpc_service(config):
    if _rpc_service is None:
        return 0
    rpc_config = dict(config.get("rpc") or {})
    max_items = int(rpc_config.get("drain_max_items", 20))
    processed = 0
    if hasattr(_rpc_service, "drain_request_queue"):
        processed += _rpc_service.drain_request_queue(max_items=max_items)
    processed += _rpc_service.drain_pending(max_items=max_items)
    return processed


def _refresh_full_tick_cache(context_info, config):
    global _last_full_tick_refresh_at, _last_full_tick_market_refresh_at
    cache_config = dict(config.get("full_tick_cache") or {})
    if not _config_bool(cache_config.get("enabled"), True):
        return 0
    account_id = str(cache_config.get("account_id") or config.get("account_id") or _account_id or "")
    if not account_id:
        return 0
    # Symbol-list demands are cheap and refresh on the fast interval; whole-market
    # (SH/SZ/BJ/HK) demands are heavy and refresh on a slower cadence so a ~50k row
    # snapshot is not pulled every fast tick.
    symbol_interval = float(cache_config.get("refresh_interval_seconds") or 0.5)
    market_interval = float(cache_config.get("market_refresh_interval_seconds") or 3.0)
    max_wall = cache_config.get("refresh_max_wall_seconds")
    max_wall = float(max_wall) if max_wall else None
    now = time.time()
    do_symbol = now - _last_full_tick_refresh_at >= symbol_interval
    do_market = now - _last_full_tick_market_refresh_at >= market_interval
    if not do_symbol and not do_market:
        return 0
    redis_client = getattr(_rpc_service, "redis", None)
    if redis_client is None:
        redis_config = dict(config.get("redis") or {})
        if not redis_config:
            return 0
        from bigqmt_signal_trader.adapters.redis_common import build_redis_client

        redis_client = build_redis_client(redis_config)
    demand_ttl = float(cache_config.get("demand_ttl_seconds") or 10)
    cache_ttl = float(cache_config.get("cache_ttl_seconds") or 10)
    max_requests = int(cache_config.get("max_requests") or 8)
    refreshed = 0
    try:
        from bigqmt_signal_trader.full_tick_cache import refresh_full_tick_cache

        if do_symbol:
            _last_full_tick_refresh_at = now
            refreshed += refresh_full_tick_cache(
                redis_client,
                context_info,
                account_id,
                demand_ttl_seconds=demand_ttl,
                cache_ttl_seconds=cache_ttl,
                max_requests=max_requests,
                kind="symbol",
                max_wall_seconds=max_wall,
            )
        if do_market:
            _last_full_tick_market_refresh_at = now
            refreshed += refresh_full_tick_cache(
                redis_client,
                context_info,
                account_id,
                demand_ttl_seconds=demand_ttl,
                cache_ttl_seconds=cache_ttl,
                max_requests=max_requests,
                kind="market",
                max_wall_seconds=max_wall,
            )
    except Exception as exc:
        print("[bigqmt_full_tick_cache] refresh failed: %s" % exc)
    return refreshed


def _schedule_adjust_if_needed(context_info, config):
    global _scheduled_adjust
    if _scheduled_adjust:
        return
    if not _config_bool(config.get("schedule_adjust"), False):
        return
    interval = str(config.get("schedule_adjust_interval") or "3000nMilliSecond")
    if not hasattr(context_info, "run_time"):
        print(
            "[bigqmt_signal_trader] WARNING: ContextInfo.run_time unavailable; RPC drain "
            "falls back to bar cadence (requested interval=%s not applied)" % interval
        )
        return
    start_time = (datetime.datetime.now() + datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        context_info.run_time("adjust", interval, start_time)
        _scheduled_adjust = True
        print(
            "[bigqmt_signal_trader] scheduled adjust interval=%s "
            "(verify observed cadence in the 'adjust cadence' log line)" % interval
        )
    except Exception as exc:
        print(
            "[bigqmt_signal_trader] WARNING: schedule adjust failed (%s); RPC drain falls back "
            "to bar cadence, requested interval=%s not applied" % (exc, interval)
        )


def _record_adjust_tick():
    """Track and periodically log the real interval between adjust triggers."""
    stats = _adjust_tick_stats
    now = time.time()
    last = stats["last_ts"]
    stats["last_ts"] = now
    if last <= 0:
        stats["window_start"] = now
        return
    delta = now - last
    stats["count"] += 1
    stats["sum"] += delta
    stats["min"] = delta if stats["min"] <= 0 else min(stats["min"], delta)
    stats["max"] = max(stats["max"], delta)
    if now - stats["window_start"] >= 10.0 and stats["count"] > 0:
        avg = stats["sum"] / stats["count"]
        print(
            "[bigqmt_signal_trader] adjust cadence: ticks=%d avg=%.3fs min=%.3fs max=%.3fs over %.0fs"
            % (stats["count"], avg, stats["min"], stats["max"], now - stats["window_start"])
        )
        stats.update({"count": 0, "sum": 0.0, "min": 0.0, "max": 0.0, "window_start": now})


def init(ContextInfo):
    detected_account_id = _detect_account_id(ContextInfo)
    if detected_account_id and not _account_id:
        set_account_id(detected_account_id)
    if _account_id and hasattr(ContextInfo, "set_account"):
        ContextInfo.set_account(_account_id)
    config = _build_config()
    runtime = BigQmtRuntimeAdapter(ContextInfo)
    app = init_app(runtime, _build_app)
    _start_rpc_service(ContextInfo, app, config)
    _schedule_adjust_if_needed(ContextInfo, config)
    print("[bigqmt_signal_trader] init ok")
    return app


def adjust(ContextInfo):
    global _adjust_logged
    _record_adjust_tick()
    config = _build_config()
    _drain_rpc_service(config)
    _refresh_full_tick_cache(ContextInfo, config)
    if hasattr(ContextInfo, "is_last_bar") and not ContextInfo.is_last_bar():
        return None
    if not _adjust_logged:
        print("[bigqmt_signal_trader] adjust ok")
        _adjust_logged = True
    return tick_app(ContextInfo, datetime.datetime.now())


def handlebar(ContextInfo):
    """Standard Big QMT bar callback."""
    return adjust(ContextInfo)


def on_order(ContextInfo, order):
    return forward_order_event(BigQmtRuntimeAdapter.to_order_event(order))


def on_trade(ContextInfo, trade):
    return forward_trade_event(BigQmtRuntimeAdapter.to_trade_event(trade))


def order_callback(ContextInfo, orderInfo):
    """Standard Big QMT order callback."""
    return on_order(ContextInfo, orderInfo)


def deal_callback(ContextInfo, dealInfo):
    """Standard Big QMT deal callback."""
    return on_trade(ContextInfo, dealInfo)


def sync_positions(ContextInfo):
    return sync_positions_app("manual")
