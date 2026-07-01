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
    # Big QMT may freeze custom daemon threads after init and its bundled Redis
    # client rejects raw stock-code JSON read from Redis. The default production
    # path therefore uses an encoded Redis list queue and drains it from QMT's
    # official run_time("adjust", ...) callback.
    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    "rpc_background_threads": False,
    "schedule_adjust": True,
    "schedule_adjust_interval": "500nMilliSecond",
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
}
