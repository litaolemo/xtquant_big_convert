# coding: utf-8
"""Big QMT Redis Pub/Sub RPC strategy entry.

This entry does not consume trade signals. RPC order methods are disabled by
default; read-only methods and position sync are enabled.
"""

import os
import sys


# QMT loads strategy scripts via exec, so __file__ may be undefined. Build a
# list of candidate directories (script dir guesses + cwd) and put any that
# holds bigqmt_signal_trader_strategy.py on sys.path[0]. This keeps the package
# and bigqmt_signal_trader_local_config importable regardless of how QMT
# invokes the script.
_CANDIDATE_DIRS = []
try:
    _CANDIDATE_DIRS.append(os.path.dirname(os.path.abspath(__file__)))
except Exception:
    pass
_CANDIDATE_DIRS.append(os.getcwd())
for _up in (".", ".."):
    _CANDIDATE_DIRS.append(os.path.abspath(os.path.join(os.getcwd(), _up)))
for _dir in _CANDIDATE_DIRS:
    if os.path.exists(os.path.join(_dir, "bigqmt_signal_trader_strategy.py")):
        if _dir not in sys.path:
            sys.path.insert(0, _dir)
        break


try:
    _load_bridge_module = __bigqmt_load_local_module
except NameError:
    _load_bridge_module = None

if _load_bridge_module is not None:
    _strategy_module = _load_bridge_module("bigqmt_signal_trader_strategy")
    adjust = _strategy_module.adjust
    bind_qmt_api = _strategy_module.bind_qmt_api
    configure = _strategy_module.configure
    deal_callback = _strategy_module.deal_callback
    handlebar = _strategy_module.handlebar
    init = _strategy_module.init
    on_order = _strategy_module.on_order
    on_trade = _strategy_module.on_trade
    order_callback = _strategy_module.order_callback
    set_account_id = _strategy_module.set_account_id
    sync_positions = _strategy_module.sync_positions
else:
    from bigqmt_signal_trader_strategy import (  # noqa: E402
        adjust,
        bind_qmt_api,
        configure,
        deal_callback,
        handlebar,
        init,
        on_order,
        on_trade,
        order_callback,
        set_account_id,
        sync_positions,
    )


ACCOUNT_ID = ""
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_DB = 5
REDIS_USERNAME = ""
REDIS_PASSWORD = ""
RPC_ALLOW_ORDER_METHODS = False
RPC_PROCESS_IN_LISTENER = True
RPC_BACKGROUND_THREADS = False
# "*" expands to read-only RPC methods only. Order/cancel/sync methods still go
# through the queue fallback and require schedule_adjust=True when enabled.
RPC_LISTENER_METHODS = ("*",)
# Transport selection. Default "redis" (zero behavior change). Set to "zmq" /
# "mysql" / "shm" in the local config to switch the wire. zmq/mysql sub-config
# (bind/connect address, pool sizing, ...) is forwarded verbatim to the factory.
RPC_TRANSPORT = "redis"
RPC_ZMQ_CONFIG = {}
RPC_MYSQL_CONFIG = {}
SCHEDULE_ADJUST_ENABLED = True
# How often the strategy thread drains the RPC queue (via adjust). Lower = less
# queue wait for read RPCs. Verify on the live box that run_time honors sub-3s
# intervals (see the adjust cadence log) before trusting a low value.
SCHEDULE_ADJUST_INTERVAL = "500nMilliSecond"
FULL_TICK_CACHE_ENABLED = False
FULL_TICK_DEMAND_TTL_SECONDS = 10
FULL_TICK_CACHE_TTL_SECONDS = 10
# Symbol-list demands refresh fast; whole-market (SH/SZ/BJ/HK) demands refresh on
# a slower cadence so a ~50k row snapshot is not pulled on every fast tick.
FULL_TICK_REFRESH_INTERVAL_SECONDS = 0.5
FULL_TICK_MARKET_REFRESH_INTERVAL_SECONDS = 3.0
# Wall-clock budget for one refresh round to avoid stalling the strategy thread.
FULL_TICK_REFRESH_MAX_WALL_SECONDS = 0.3
FULL_TICK_MAX_REQUESTS = 8
# Async download jobs: the strategy thread drains one queued job at a time,
# downloading DOWNLOAD_JOB_CHUNK_SIZE symbols per tick (capped by the wall-clock
# budget), so a long download never blocks the RPC pump. chunk_size is the
# smallest per-tick block, so keep it modest if per-symbol downloads are slow.
# DISABLED by default: the full Big QMT terminal's embedded xtdata SDK has no
# reachable data service to download through (raises "无法连接行情服务"). Supplement
# history via the terminal's 数据管理/补充数据 UI, then read it over RPC with
# get_market_data_ex / get_local_data. Re-enable only where a MiniQMT/xtdata data
# service is connectable (set download_jobs_enabled=True in the local config).
DOWNLOAD_JOBS_ENABLED = False
DOWNLOAD_JOB_CHUNK_SIZE = 10
DOWNLOAD_JOB_MAX_WALL_SECONDS = 0.5
DOWNLOAD_JOB_TTL_SECONDS = 3600
# Push order_callback/deal_callback details to Redis so clients get real-time
# on_stock_order/on_stock_trade callbacks (MiniQMT style) instead of polling.
EXEC_EVENTS_ENABLED = True

try:
    from bigqmt_signal_trader_local_config import BIGQMT_ACCOUNT_ID, BIGQMT_REDIS_CONFIG
except Exception:
    BIGQMT_ACCOUNT_ID = ""
    BIGQMT_REDIS_CONFIG = {}

ACCOUNT_ID = str(BIGQMT_ACCOUNT_ID or ACCOUNT_ID or "")
REDIS_HOST = BIGQMT_REDIS_CONFIG.get("host", REDIS_HOST)
REDIS_PORT = int(BIGQMT_REDIS_CONFIG.get("port", REDIS_PORT))
REDIS_DB = int(BIGQMT_REDIS_CONFIG.get("db", REDIS_DB))
REDIS_USERNAME = BIGQMT_REDIS_CONFIG.get("username", REDIS_USERNAME)
REDIS_PASSWORD = BIGQMT_REDIS_CONFIG.get("password", REDIS_PASSWORD)
RPC_ALLOW_ORDER_METHODS = bool(BIGQMT_REDIS_CONFIG.get("rpc_allow_order_methods", RPC_ALLOW_ORDER_METHODS))
RPC_PROCESS_IN_LISTENER = bool(
    BIGQMT_REDIS_CONFIG.get("rpc_process_in_listener", RPC_PROCESS_IN_LISTENER and not RPC_ALLOW_ORDER_METHODS)
)
RPC_BACKGROUND_THREADS = bool(BIGQMT_REDIS_CONFIG.get("rpc_background_threads", RPC_BACKGROUND_THREADS))
RPC_LISTENER_METHODS = tuple(BIGQMT_REDIS_CONFIG.get("rpc_listener_methods", RPC_LISTENER_METHODS))
SCHEDULE_ADJUST_ENABLED = bool(BIGQMT_REDIS_CONFIG.get("schedule_adjust", SCHEDULE_ADJUST_ENABLED))
if not RPC_BACKGROUND_THREADS:
    SCHEDULE_ADJUST_ENABLED = True
SCHEDULE_ADJUST_INTERVAL = str(BIGQMT_REDIS_CONFIG.get("schedule_adjust_interval", SCHEDULE_ADJUST_INTERVAL))
FULL_TICK_CACHE_ENABLED = bool(BIGQMT_REDIS_CONFIG.get("full_tick_cache_enabled", FULL_TICK_CACHE_ENABLED))
FULL_TICK_DEMAND_TTL_SECONDS = float(
    BIGQMT_REDIS_CONFIG.get("full_tick_demand_ttl_seconds", FULL_TICK_DEMAND_TTL_SECONDS)
)
FULL_TICK_CACHE_TTL_SECONDS = float(
    BIGQMT_REDIS_CONFIG.get("full_tick_cache_ttl_seconds", FULL_TICK_CACHE_TTL_SECONDS)
)
FULL_TICK_REFRESH_INTERVAL_SECONDS = float(
    BIGQMT_REDIS_CONFIG.get("full_tick_refresh_interval_seconds", FULL_TICK_REFRESH_INTERVAL_SECONDS)
)
FULL_TICK_MARKET_REFRESH_INTERVAL_SECONDS = float(
    BIGQMT_REDIS_CONFIG.get("full_tick_market_refresh_interval_seconds", FULL_TICK_MARKET_REFRESH_INTERVAL_SECONDS)
)
FULL_TICK_REFRESH_MAX_WALL_SECONDS = float(
    BIGQMT_REDIS_CONFIG.get("full_tick_refresh_max_wall_seconds", FULL_TICK_REFRESH_MAX_WALL_SECONDS)
)
FULL_TICK_MAX_REQUESTS = int(BIGQMT_REDIS_CONFIG.get("full_tick_max_requests", FULL_TICK_MAX_REQUESTS))
DOWNLOAD_JOBS_ENABLED = bool(BIGQMT_REDIS_CONFIG.get("download_jobs_enabled", DOWNLOAD_JOBS_ENABLED))
DOWNLOAD_JOB_CHUNK_SIZE = int(BIGQMT_REDIS_CONFIG.get("download_job_chunk_size", DOWNLOAD_JOB_CHUNK_SIZE))
DOWNLOAD_JOB_MAX_WALL_SECONDS = float(
    BIGQMT_REDIS_CONFIG.get("download_job_max_wall_seconds", DOWNLOAD_JOB_MAX_WALL_SECONDS)
)
DOWNLOAD_JOB_TTL_SECONDS = int(BIGQMT_REDIS_CONFIG.get("download_job_ttl_seconds", DOWNLOAD_JOB_TTL_SECONDS))
EXEC_EVENTS_ENABLED = bool(BIGQMT_REDIS_CONFIG.get("exec_events_enabled", EXEC_EVENTS_ENABLED))


def _apply_config(account_id):
    account_id = str(account_id or "")
    if account_id:
        set_account_id(account_id)
    configure(
        mode="bigqmt",
        account_id=account_id,
        position_sync_type="redis" if RPC_TRANSPORT in ("redis", "", "default") else "",
        enable_rpc=True,
        schedule_adjust=SCHEDULE_ADJUST_ENABLED,
        schedule_adjust_interval=SCHEDULE_ADJUST_INTERVAL,
        redis={
            "host": REDIS_HOST,
            "port": REDIS_PORT,
            "db": REDIS_DB,
            "username": REDIS_USERNAME,
            "password": REDIS_PASSWORD,
            "position_key_template": "bigqmt:positions:{account_id}",
            "position_event_stream_template": "bigqmt:position_events:{account_id}",
        },
        rpc={
            "enabled": True,
            "account_id": account_id,
            "allow_order_methods": RPC_ALLOW_ORDER_METHODS,
            "request_channel_template": "bigqmt:rpc:req:{account_id}",
            "response_channel_template": "bigqmt:rpc:resp:{account_id}:{request_id}",
            "response_key_template": "bigqmt:rpc:resp:{account_id}:{request_id}",
            "response_ttl_seconds": 60,
            "drain_max_items": 20,
            "process_in_listener": RPC_PROCESS_IN_LISTENER,
            "listener_methods": RPC_LISTENER_METHODS,
            "background_threads": RPC_BACKGROUND_THREADS,
            # Transport selection (default redis). Forwarded from the local
            # config so the factory can pick zmq/mysql/shm.
            "transport": RPC_TRANSPORT,
            "zmq": RPC_ZMQ_CONFIG,
            "mysql": RPC_MYSQL_CONFIG,
        },
        full_tick_cache={
            "enabled": FULL_TICK_CACHE_ENABLED,
            "account_id": account_id,
            "demand_ttl_seconds": FULL_TICK_DEMAND_TTL_SECONDS,
            "cache_ttl_seconds": FULL_TICK_CACHE_TTL_SECONDS,
            "refresh_interval_seconds": FULL_TICK_REFRESH_INTERVAL_SECONDS,
            "market_refresh_interval_seconds": FULL_TICK_MARKET_REFRESH_INTERVAL_SECONDS,
            "refresh_max_wall_seconds": FULL_TICK_REFRESH_MAX_WALL_SECONDS,
            "max_requests": FULL_TICK_MAX_REQUESTS,
        },
        download_jobs={
            "enabled": DOWNLOAD_JOBS_ENABLED,
            "account_id": account_id,
            "chunk_size": DOWNLOAD_JOB_CHUNK_SIZE,
            "max_wall_seconds": DOWNLOAD_JOB_MAX_WALL_SECONDS,
            "job_ttl_seconds": DOWNLOAD_JOB_TTL_SECONDS,
        },
        exec_events={
            "enabled": EXEC_EVENTS_ENABLED,
            "account_id": account_id,
        },
    )


def configure_runtime_account(account_id):
    _apply_config(account_id)


def configure_runtime_redis(redis_config):
    global REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_USERNAME, REDIS_PASSWORD, RPC_ALLOW_ORDER_METHODS, RPC_PROCESS_IN_LISTENER, RPC_BACKGROUND_THREADS, RPC_LISTENER_METHODS, SCHEDULE_ADJUST_ENABLED, SCHEDULE_ADJUST_INTERVAL, FULL_TICK_CACHE_ENABLED, FULL_TICK_DEMAND_TTL_SECONDS, FULL_TICK_CACHE_TTL_SECONDS, FULL_TICK_REFRESH_INTERVAL_SECONDS, FULL_TICK_MARKET_REFRESH_INTERVAL_SECONDS, FULL_TICK_REFRESH_MAX_WALL_SECONDS, FULL_TICK_MAX_REQUESTS, RPC_TRANSPORT, RPC_ZMQ_CONFIG, RPC_MYSQL_CONFIG, DOWNLOAD_JOBS_ENABLED, DOWNLOAD_JOB_CHUNK_SIZE, DOWNLOAD_JOB_MAX_WALL_SECONDS, DOWNLOAD_JOB_TTL_SECONDS, EXEC_EVENTS_ENABLED
    redis_config = dict(redis_config or {})
    REDIS_HOST = redis_config.get("host", REDIS_HOST)
    REDIS_PORT = int(redis_config.get("port", REDIS_PORT))
    REDIS_DB = int(redis_config.get("db", REDIS_DB))
    REDIS_USERNAME = redis_config.get("username", REDIS_USERNAME)
    REDIS_PASSWORD = redis_config.get("password", REDIS_PASSWORD)
    RPC_ALLOW_ORDER_METHODS = bool(redis_config.get("rpc_allow_order_methods", RPC_ALLOW_ORDER_METHODS))
    RPC_PROCESS_IN_LISTENER = bool(
        redis_config.get("rpc_process_in_listener", RPC_PROCESS_IN_LISTENER and not RPC_ALLOW_ORDER_METHODS)
    )
    RPC_BACKGROUND_THREADS = bool(redis_config.get("rpc_background_threads", RPC_BACKGROUND_THREADS))
    RPC_LISTENER_METHODS = tuple(redis_config.get("rpc_listener_methods", RPC_LISTENER_METHODS))
    RPC_TRANSPORT = str(redis_config.get("transport", RPC_TRANSPORT)).lower()
    RPC_ZMQ_CONFIG = dict(redis_config.get("zmq", RPC_ZMQ_CONFIG))
    RPC_MYSQL_CONFIG = dict(redis_config.get("mysql", RPC_MYSQL_CONFIG))
    # schedule_adjust must stay ON for ALL transports — including zmq.
    # run_time("adjust", interval) is what THROTTLES QMT's strategy callback: with
    # it, adjust fires on the configured cadence (e.g. 500ms); WITHOUT it QMT calls
    # adjust in a hot loop (~2500/s) that pegs the GIL and starves the zmq ROUTER
    # background thread (RPC then times out entirely). It also sets the GIL-release
    # rhythm the background transport threads rely on. So keep the original rule:
    # honor an explicit value, default on, and force on when not background-threaded.
    SCHEDULE_ADJUST_ENABLED = bool(redis_config.get("schedule_adjust", SCHEDULE_ADJUST_ENABLED))
    if not RPC_BACKGROUND_THREADS:
        SCHEDULE_ADJUST_ENABLED = True
    SCHEDULE_ADJUST_INTERVAL = str(redis_config.get("schedule_adjust_interval", SCHEDULE_ADJUST_INTERVAL))
    FULL_TICK_CACHE_ENABLED = bool(redis_config.get("full_tick_cache_enabled", FULL_TICK_CACHE_ENABLED))
    FULL_TICK_DEMAND_TTL_SECONDS = float(
        redis_config.get("full_tick_demand_ttl_seconds", FULL_TICK_DEMAND_TTL_SECONDS)
    )
    FULL_TICK_CACHE_TTL_SECONDS = float(redis_config.get("full_tick_cache_ttl_seconds", FULL_TICK_CACHE_TTL_SECONDS))
    FULL_TICK_REFRESH_INTERVAL_SECONDS = float(
        redis_config.get("full_tick_refresh_interval_seconds", FULL_TICK_REFRESH_INTERVAL_SECONDS)
    )
    FULL_TICK_MARKET_REFRESH_INTERVAL_SECONDS = float(
        redis_config.get("full_tick_market_refresh_interval_seconds", FULL_TICK_MARKET_REFRESH_INTERVAL_SECONDS)
    )
    FULL_TICK_REFRESH_MAX_WALL_SECONDS = float(
        redis_config.get("full_tick_refresh_max_wall_seconds", FULL_TICK_REFRESH_MAX_WALL_SECONDS)
    )
    FULL_TICK_MAX_REQUESTS = int(redis_config.get("full_tick_max_requests", FULL_TICK_MAX_REQUESTS))
    DOWNLOAD_JOBS_ENABLED = bool(redis_config.get("download_jobs_enabled", DOWNLOAD_JOBS_ENABLED))
    DOWNLOAD_JOB_CHUNK_SIZE = int(redis_config.get("download_job_chunk_size", DOWNLOAD_JOB_CHUNK_SIZE))
    DOWNLOAD_JOB_MAX_WALL_SECONDS = float(
        redis_config.get("download_job_max_wall_seconds", DOWNLOAD_JOB_MAX_WALL_SECONDS)
    )
    DOWNLOAD_JOB_TTL_SECONDS = int(redis_config.get("download_job_ttl_seconds", DOWNLOAD_JOB_TTL_SECONDS))
    EXEC_EVENTS_ENABLED = bool(redis_config.get("exec_events_enabled", EXEC_EVENTS_ENABLED))
    _apply_config(ACCOUNT_ID)


def bind_runtime_api(passorder_func=None, cancel_func=None, get_trade_detail_data_func=None,
                     extra_funcs=None):
    bind_qmt_api(
        passorder_func=passorder_func,
        cancel_func=cancel_func,
        get_trade_detail_data_func=get_trade_detail_data_func,
        extra_funcs=extra_funcs,
    )


_apply_config(ACCOUNT_ID)
