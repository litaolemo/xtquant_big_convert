# coding: utf-8
"""Local private config example for the QMT python directory.

Copy this file to the QMT python directory as:

    bigqmt_signal_trader_local_config.py

Do not commit the real file. It may contain account ids and Redis credentials.
"""

BIGQMT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"

BIGQMT_REDIS_CONFIG = {
    "host": "127.0.0.1",
    "port": 6379,
    "db": 5,
    "username": "",
    "password": "",
    # Keep order RPC disabled unless you explicitly want remote order/cancel.
    "rpc_allow_order_methods": False,
    # Redis and ZMQ can both drain requests through QMT's official
    # run_time("adjust", ...) callback. This avoids GIL stalls in QMT's process.
    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    "rpc_background_threads": False,
    "schedule_adjust": True,
    "schedule_adjust_interval": "100nMilliSecond",
    # The default mode calls get_full_tick through RPC. Enable this cache only
    # if full-market payloads are too large for your latency/CPU budget.
    # When a client calls get_full_tick, it renews demand for 10 seconds.
    # Symbol-list demands refresh every full_tick_refresh_interval_seconds; whole-market
    # (SH/SZ/BJ/HK) demands refresh on the slower market interval so a ~50k row snapshot
    # is not pulled every fast tick.
    "full_tick_cache_enabled": False,
    "full_tick_demand_ttl_seconds": 10,
    "full_tick_cache_ttl_seconds": 10,
    "full_tick_refresh_interval_seconds": 0.5,
    "full_tick_market_refresh_interval_seconds": 3,
    # Wall-clock budget for one refresh round; keeps a slow round from stalling the
    # strategy thread (the in-flight demand always completes).
    "full_tick_refresh_max_wall_seconds": 0.3,
    "full_tick_max_requests": 8,
    # Async download jobs: clients submit download_history_data(2) as a job; the
    # strategy thread downloads download_job_chunk_size symbols per tick (capped by
    # download_job_max_wall_seconds), so a long download never blocks the RPC pump.
    # chunk_size is the smallest per-tick block — keep it modest if downloads are slow.
    # Disabled: the full terminal's xtdata SDK can't reach a data service to
    # download. Supplement history via the terminal's 数据管理/补充数据 UI, then read
    # it over RPC (get_market_data_ex/get_local_data). Enable only where a
    # MiniQMT/xtdata data service is connectable.
    "download_jobs_enabled": False,
    "download_job_chunk_size": 10,
    "download_job_max_wall_seconds": 0.5,
    "download_job_ttl_seconds": 3600,
    # Push order_callback/deal_callback details to Redis so clients get real-time
    # on_stock_order / on_stock_trade callbacks (MiniQMT style) instead of polling.
    "exec_events_enabled": True,
}
