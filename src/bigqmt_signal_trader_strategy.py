# coding: utf-8
"""ThinkTrader Big QMT strategy entry.

Keep this entry file ASCII-only because QMT's strategy editor may save the
generated strategy file with a local code page while preserving this coding
header. Business logic stays in the importable package.
"""

import datetime
import importlib as _importlib
import sys
import threading
import time

# The DRYRUN entry reloads strategy/runtime/redis_rpc/redis_common but NOT the
# other package submodules. Without this, the "from adapter_factory import build_app"
# below re-binds the STALE cached module on every strategy re-run, so edits to
# adapter_factory never take effect until a full terminal restart. Force-reload it
# (only it -- reloading the adapter classes would break isinstance elsewhere) so a
# plain strategy re-run picks up build_app fixes. build_app imports the adapter
# classes lazily, so their identity is preserved.
_af_mod = sys.modules.get("bigqmt_signal_trader.adapter_factory")
if _af_mod is not None:
    try:
        _importlib.reload(_af_mod)
    except Exception as _reload_err:
        print("[bigqmt_signal_trader] reload adapter_factory failed: %s" % _reload_err)

try:
    _load_bridge_module = __bigqmt_load_local_module
except NameError:
    _load_bridge_module = None

if _load_bridge_module is not None:
    _adapter_factory = _load_bridge_module("bigqmt_signal_trader.adapter_factory")
    _runner = _load_bridge_module("bigqmt_signal_trader.runner")
    _runtime_bigqmt = _load_bridge_module("bigqmt_signal_trader.runtime_bigqmt")
    _default_build_app = _adapter_factory.build_app
    forward_order_event = _runner.forward_order_event
    forward_trade_event = _runner.forward_trade_event
    init_app = _runner.init_app
    _reset_runner_app = _runner.reset_app
    sync_positions_app = _runner.sync_positions_app
    tick_app = _runner.tick_app
    BigQmtRuntimeAdapter = _runtime_bigqmt.BigQmtRuntimeAdapter
else:
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
# Latency tuning / diagnostics (server side, in the Big QMT process).
#  - switch interval: hand the GIL to the background RPC thread ~5x more often
#    than the 5ms default so it is not starved as long during Python contention.
#  - GIL probe: a heartbeat thread that measures how long the interpreter was
#    unable to run it (i.e. the process was stalled), independent of any request.
_GIL_SWITCH_INTERVAL = 0.001
_LATENCY_PROBE_ENABLED = False
_LATENCY_PROBE_THRESHOLD_MS = 50.0
_latency_probe_started = False
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


def bind_qmt_api(passorder_func=None, cancel_func=None, get_trade_detail_data_func=None,
                 extra_funcs=None):
    if passorder_func is not None:
        _qmt_api["passorder"] = passorder_func
    if cancel_func is not None:
        _qmt_api["cancel"] = cancel_func
    if get_trade_detail_data_func is not None:
        _qmt_api["get_trade_detail_data"] = get_trade_detail_data_func
    # 捕获 QMT 运行时注入的额外全局函数（融资融券查询、IPO、期权持仓等）。
    # 这些函数和 passorder 一样由 Big QMT 进程在运行时注入到全局命名空间，
    # 不在 _PyContextInfo.py 桩里，需在 DRYRUN 入口捕获后传入。
    if extra_funcs:
        for name, func in extra_funcs.items():
            if func is not None:
                _qmt_api[name] = func


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


# Official Big QMT runtime-injected global functions (like passorder) that we
# expose over RPC. These are not ContextInfo methods and not in the IDE stub;
# QMT injects them into the process global namespace at startup. We resolve
# them lazily so the module imports cleanly outside QMT (tests/dev).
_EXTRA_QMT_GLOBAL_FUNCS = (
    "get_history_trade_detail_data",  # 历史成交明细
    "get_value_by_order_id",          # 按 order_id 查委托详情
    "get_last_order_id",              # 最近委托号
    "get_ipo_data",                   # 新股数据
    "get_new_purchase_limit",         # 新股申购额度
    "get_assure_contract",            # 融资标的（担保品）合约
    "get_enable_short_contract",      # 融券标的合约
    "get_unclosed_compacts",          # 未平仓合约（负债）
    "get_closed_compacts",            # 已平仓合约
    "get_debt_contract",              # 负债合约
    "get_option_subject_position",    # 期权标的持仓
    "get_comb_option",                # 组合期权
    "get_hkt_exchange_rate",          # 港股通汇率
)


def _build_config():
    config = dict(_config)
    if _account_id:
        config["account_id"] = _account_id
    qmt_api = dict(config.get("qmt_api") or {})
    qmt_api.setdefault("passorder", _resolve_runtime_name("passorder"))
    qmt_api.setdefault("cancel", _resolve_runtime_name("cancel"))
    qmt_api.setdefault("get_trade_detail_data", _resolve_runtime_name("get_trade_detail_data"))
    # 解析其余官方全局函数（存在则注入，不存在保持 None）。
    for name in _EXTRA_QMT_GLOBAL_FUNCS:
        qmt_api.setdefault(name, _resolve_runtime_name(name))
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


_REDIS_TRANSPORT_NAMES = ("redis", "", "default")


def _is_redis_transport(transport_name):
    return str(transport_name or "redis").lower() in _REDIS_TRANSPORT_NAMES


def _resolve_background_threads(transport_name, configured):
    """Decide whether the RPC service runs its own background receive threads.

    Non-redis transports (zmq/mysql/shm) own their receive threads and have no
    QMT adjust-drain fallback, so they MUST run background threads or they bind
    but never receive. Force it on so switching transport is a one-liner
    (``transport: "zmq"``) — the user needn't also set rpc_background_threads.
    Redis keeps the configured value (default False = adjust-drain path).
    """
    if not _is_redis_transport(transport_name):
        return True
    return bool(configured)


def _build_rpc_service(context_info, app, config):
    rpc_config = dict(config.get("rpc") or {})
    enabled = _config_bool(config.get("enable_rpc"), False) or _config_bool(rpc_config.get("enabled"), False)
    if not enabled:
        return None
    transport_name = str(rpc_config.get("transport") or "redis").lower()
    redis_transport = transport_name in ("redis", "", "default")

    import importlib
    if _load_bridge_module is not None:
        _market_bigqmt = _load_bridge_module("bigqmt_signal_trader.adapters.market_bigqmt")
        _position_bigqmt = _load_bridge_module("bigqmt_signal_trader.adapters.position_bigqmt")
        _redis_rpc = _load_bridge_module("bigqmt_signal_trader.redis_rpc")
        BigQmtRpcHandlers = _redis_rpc.BigQmtRpcHandlers
        RedisPubSubRpcService = _redis_rpc.RedisPubSubRpcService
    else:
        from bigqmt_signal_trader.adapters import market_bigqmt as _market_bigqmt
        from bigqmt_signal_trader.adapters import position_bigqmt as _position_bigqmt
        from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers, RedisPubSubRpcService

    # QMT keeps strategy modules in the same process between editor reruns.
    # Reload adapters here so synced local package fixes take effect immediately.
    _market_bigqmt = importlib.reload(_market_bigqmt)
    _position_bigqmt = importlib.reload(_position_bigqmt)
    # Reload the lazily-imported helper modules too, so edits to them take effect on
    # an editor rerun (QMT persists sys.modules across reruns; a plain lazy import
    # would otherwise keep the stale cached version).
    for _mod_name in (
        "bigqmt_signal_trader.full_tick_cache",
        "bigqmt_signal_trader.download_jobs",
        "bigqmt_signal_trader.exec_events",
    ):
        try:
            importlib.reload(importlib.import_module(_mod_name))
        except Exception as _reload_err:
            print("[bigqmt_rpc] reload %s failed: %s" % (_mod_name, _reload_err))
    BigQmtMarketDataProvider = _market_bigqmt.BigQmtMarketDataProvider
    BigQmtPositionProvider = _position_bigqmt.BigQmtPositionProvider

    qmt_api = dict(config.get("qmt_api") or {})
    redis_client = None
    response_redis_client = None
    if redis_transport:
        if _load_bridge_module is not None:
            _redis_common = _load_bridge_module("bigqmt_signal_trader.adapters.redis_common")
        else:
            from bigqmt_signal_trader.adapters import redis_common as _redis_common

        _redis_common = importlib.reload(_redis_common)
        redis_config = dict(config.get("redis") or {})
        redis_config.update(dict(rpc_config.get("redis") or {}))
        listen_redis_config = dict(redis_config)
        listen_redis_config["socket_timeout"] = None
        redis_client = rpc_config.get("redis_client") or config.get("redis_client") or _redis_common.build_redis_client(listen_redis_config)
        response_redis_client = (
            rpc_config.get("response_redis_client")
            or config.get("response_redis_client")
            or _redis_common.build_redis_client(redis_config)
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
        qmt_api=qmt_api,
    )
    process_in_listener = _config_bool(rpc_config.get("process_in_listener"), True)
    listener_methods = rpc_config.get("listener_methods") or ("*",)
    configured_bg = _config_bool(rpc_config.get("background_threads"), False)
    background_threads = _resolve_background_threads(transport_name, configured_bg)
    if background_threads and not configured_bg:
        print("[bigqmt_rpc] transport=%s -> background_threads auto-enabled" % transport_name)
    # Build the transport. Redis is the default and reuses the existing clients/
    # templates (zero behavior change). zmq/mysql/shm go through the factory and
    # bypass the Redis clients entirely.
    transport = None
    if transport_name not in ("redis", "", "default"):
        if _load_bridge_module is not None:
            build_transport = _load_bridge_module("bigqmt_signal_trader.transports.factory").build_transport
        else:
            from bigqmt_signal_trader.transports.factory import build_transport

        factory_config = dict(rpc_config)
        factory_config["account_id"] = account_id
        factory_config["print_prefix"] = "[bigqmt_rpc]"
        transport = build_transport(transport_name, factory_config, account_id=account_id, print_prefix="[bigqmt_rpc]")
    print(
        "[bigqmt_rpc] transport=%s mode process_in_listener=%s listener_methods=%s allow_order_methods=%s background_threads=%s"
        % (transport_name, process_in_listener, listener_methods, allow_order_methods, background_threads)
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
        transport=transport,
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
    from bigqmt_signal_trader.full_tick_cache import refresh_full_tick_cache

    refreshed = 0
    # Symbol and market refreshes are throttled independently, so each advances its
    # own timestamp and runs in its own try: a symbol-refresh error must not starve
    # the market refresh nor leave it retrying every fast tick (unthrottled).
    if do_symbol:
        _last_full_tick_refresh_at = now
        try:
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
        except Exception as exc:
            print("[bigqmt_full_tick_cache] symbol refresh failed: %s" % exc)
    if do_market:
        _last_full_tick_market_refresh_at = now
        try:
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
            print("[bigqmt_full_tick_cache] market refresh failed: %s" % exc)
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


def _gil_probe_loop():
    """Heartbeat: sleep 5ms in a loop and measure the ACTUAL elapsed time. sleep()
    releases the GIL; if returning from it takes much longer than 5ms, the thread
    was starved -- i.e. the interpreter (this whole process) was stalled holding
    the GIL elsewhere. Summarize gaps over a 10s window so we can see how often /
    how long the process freezes, independent of any RPC request."""
    step = 0.005
    threshold = _LATENCY_PROBE_THRESHOLD_MS / 1000.0
    window_start = time.time()
    gaps = []
    while True:
        t0 = time.time()
        time.sleep(step)
        gap = time.time() - t0 - step
        if gap > threshold:
            gaps.append(gap * 1000.0)
        now = time.time()
        if now - window_start >= 10.0:
            if gaps:
                gaps.sort()
                print(
                    "[gil_probe] over %.0fs: %d stalls>%.0fms  max=%.0fms p50=%.0fms total=%.0fms"
                    % (now - window_start, len(gaps), _LATENCY_PROBE_THRESHOLD_MS,
                       gaps[-1], gaps[len(gaps) // 2], sum(gaps))
                )
            else:
                print("[gil_probe] over %.0fs: 0 stalls>%.0fms (clean)" % (now - window_start, _LATENCY_PROBE_THRESHOLD_MS))
            window_start = now
            gaps = []


def _start_latency_probe():
    global _latency_probe_started
    if _latency_probe_started or not _LATENCY_PROBE_ENABLED:
        return
    _latency_probe_started = True
    t = threading.Thread(target=_gil_probe_loop, name="bigqmt-gil-probe", daemon=True)
    t.start()
    print("[gil_probe] started (threshold=%.0fms)" % _LATENCY_PROBE_THRESHOLD_MS)


def _apply_gil_tuning():
    try:
        sys.setswitchinterval(_GIL_SWITCH_INTERVAL)
        print("[bigqmt_signal_trader] gil switch interval set to %.4fs" % _GIL_SWITCH_INTERVAL)
    except Exception as exc:
        print("[bigqmt_signal_trader] setswitchinterval failed: %s" % exc)


def init(ContextInfo):
    detected_account_id = _detect_account_id(ContextInfo)
    if detected_account_id and not _account_id:
        set_account_id(detected_account_id)
    if _account_id and hasattr(ContextInfo, "set_account"):
        ContextInfo.set_account(_account_id)
    _apply_gil_tuning()
    _start_latency_probe()
    config = _build_config()
    runtime = BigQmtRuntimeAdapter(ContextInfo)
    app = init_app(runtime, _build_app)
    _start_rpc_service(ContextInfo, app, config)
    _schedule_adjust_if_needed(ContextInfo, config)
    print("[bigqmt_signal_trader] init ok")
    return app


def _pump_download_jobs(context_info, config):
    """Advance any queued async download job by a bounded slice on this thread."""
    job_config = dict(config.get("download_jobs") or {})
    if not _config_bool(job_config.get("enabled"), True):
        return None
    account_id = str(job_config.get("account_id") or config.get("account_id") or _account_id or "")
    if not account_id:
        return None
    redis_client = getattr(_rpc_service, "redis", None)
    if redis_client is None:
        redis_config = dict(config.get("redis") or {})
        if not redis_config:
            return None
        from bigqmt_signal_trader.adapters.redis_common import build_redis_client

        redis_client = build_redis_client(redis_config)
    market_data = getattr(getattr(_rpc_service, "handlers", None), "market_data", None)
    if market_data is None:
        from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider

        market_data = BigQmtMarketDataProvider(context_info)
    try:
        from bigqmt_signal_trader.download_jobs import pump_download_jobs

        return pump_download_jobs(
            redis_client,
            market_data,
            account_id,
            chunk_size=int(job_config.get("chunk_size") or 10),
            max_wall_seconds=float(job_config.get("max_wall_seconds") or 0.5),
            job_ttl_seconds=int(job_config.get("job_ttl_seconds") or 3600),
        )
    except Exception as exc:
        print("[bigqmt_download_jobs] pump failed: %s" % exc)
        return None


def _adjust_phase(name, fn, *args):
    """Time one adjust phase; log only if it exceeds 50ms. Pinpoints which part of
    the 500ms adjust cycle holds the GIL (the gil_probe shows the stall exists;
    this shows WHERE). The finally-log never alters the call's result/exception."""
    t0 = time.perf_counter()
    try:
        return fn(*args)
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        if ms > 50.0:
            print("[adjust_phase] %s %.0fms" % (name, ms))


def adjust(ContextInfo):
    global _adjust_logged
    _record_adjust_tick()
    config = _build_config()
    _adjust_phase("drain", _drain_rpc_service, config)
    _adjust_phase("full_tick", _refresh_full_tick_cache, ContextInfo, config)
    _adjust_phase("download", _pump_download_jobs, ContextInfo, config)
    if hasattr(ContextInfo, "is_last_bar") and not ContextInfo.is_last_bar():
        return None
    if not _adjust_logged:
        print("[bigqmt_signal_trader] adjust ok")
        _adjust_logged = True
    return _adjust_phase("tick_app", tick_app, ContextInfo, datetime.datetime.now())


def handlebar(ContextInfo):
    """Standard Big QMT bar callback."""
    return adjust(ContextInfo)


def _publish_exec_event(kind, obj):
    """Push a normalized order/trade event to Redis for real-time client callbacks."""
    config = _build_config()
    event_config = dict(config.get("exec_events") or {})
    if not _config_bool(event_config.get("enabled"), True):
        return
    account_id = str(event_config.get("account_id") or config.get("account_id") or _account_id or "")
    if not account_id:
        return
    redis_client = getattr(_rpc_service, "redis", None)
    if redis_client is None:
        redis_config = dict(config.get("redis") or {})
        if not redis_config:
            return
        from bigqmt_signal_trader.adapters.redis_common import build_redis_client

        redis_client = build_redis_client(redis_config)
    try:
        from bigqmt_signal_trader import exec_events

        if kind == "trade":
            exec_events.publish_trade_event(
                redis_client, account_id, exec_events.normalize_trade_event(obj, account_id)
            )
        else:
            exec_events.publish_order_event(
                redis_client, account_id, exec_events.normalize_order_event(obj, account_id)
            )
    except Exception as exc:
        print("[bigqmt_exec_events] publish %s failed: %s" % (kind, exc))


def on_order(ContextInfo, order):
    _publish_exec_event("order", order)
    return forward_order_event(BigQmtRuntimeAdapter.to_order_event(order))


def on_trade(ContextInfo, trade):
    _publish_exec_event("trade", trade)
    return forward_trade_event(BigQmtRuntimeAdapter.to_trade_event(trade))


def order_callback(ContextInfo, orderInfo):
    """Standard Big QMT order callback."""
    return on_order(ContextInfo, orderInfo)


def deal_callback(ContextInfo, dealInfo):
    """Standard Big QMT deal callback."""
    return on_trade(ContextInfo, dealInfo)


def sync_positions(ContextInfo):
    return sync_positions_app("manual")
