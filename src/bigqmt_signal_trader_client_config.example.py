# coding: utf-8
"""Client-side private config example for MiniQMT-compatible replacement.

Copy this file to:

    src/bigqmt_signal_trader_client_config.py

Do not commit the real file. It may contain account ids and Redis credentials.
"""

BIGQMT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"
BIGQMT_RPC_TIMEOUT_SECONDS = 30.0
BIGQMT_DOWNLOAD_WAIT_SECONDS = 1800
BIGQMT_DOWNLOAD_POLL_INTERVAL_SECONDS = 0.5

BIGQMT_REDIS_CONFIG = {
    "host": "YOUR_REDIS_HOST",
    "port": 6379,
    "db": 5,
    "username": "",
    "password": "",
    # Transport selection. Must match the QMT-side server config. Default
    # "redis" works with the standard DRYRUN; use "zmq" when the server runs
    # with transport=zmq (e.g. the no-redis version or explicit zmq mode).
    "transport": "redis",
    # ZMQ-specific settings (only used when transport=zmq):
    # "zmq": {
    #     # Explicit connect address. The QMT-side server binds a port derived
    #     # from account_id (default 15563 for account 8886800503). If you know
    #     # the exact address, set it here to skip service discovery.
    #     "connect_address": "tcp://127.0.0.1:15563",
    #     # "host": "127.0.0.1",
    #     # "port": 15563,
    # },
}

# Default direct mode calls get_full_tick through RPC. Set enabled=True only when
# you want client-side get_full_tick to read demand-driven Redis snapshots.
BIGQMT_FULL_TICK_CACHE_CONFIG = {
    "enabled": False,
    "demand_ttl_seconds": 10,
    "cache_ttl_seconds": 10,
    "wait_seconds": 3.5,
    "poll_interval_seconds": 0.2,
}

# Client-side LOCAL market-data cache.
#   get_market_data_ex(...) writes returned bars under `dir`; get_local_data(...)
#   then reads them locally with NO RPC to Big QMT (for offline / repeated local
#   analysis). download_history_data* submits a server-side Big QMT download job.
#   - dir: cache folder (default ~/.bigqmt_cache), one pickle per (period, code).
#   - fallback_rpc: if True, get_local_data auto-fetches+caches a cache miss;
#     if False (default), a cache-missed code is simply omitted (download first).
BIGQMT_LOCAL_CACHE_CONFIG = {
    "enabled": True,
    "dir": None,            # None -> ~/.bigqmt_cache
    "fallback_rpc": False,
    # Storage format: "auto" (parquet if pyarrow installed, else pickle),
    # "parquet" (columnar/compressed/cross-language — recommended), or "pkl".
    # One file per (period, dividend_type, code); switching format auto-migrates.
    "format": "auto",
}

# FormulaServer direct read fast-path (port 58600).
#   Big QMT's built-in C++ quote/reference service. Routing reads straight to it
#   bypasses the RPC bridge AND the QMT python thread's GIL: ~0.07ms vs ~13ms
#   over redis. Enabled by default; you normally do not need this block.
#
#   Covers reference/history reads only. Account, position, order, trade and
#   五档 (get_full_tick) calls are NOT served by FormulaServer and always go over
#   RPC. Every miss — unmapped method, untranslatable params, server down —
#   falls back to RPC automatically, so an unreachable 58600 changes nothing.
BIGQMT_FORMULA_SERVER_CONFIG = {
    "enabled": True,        # or set BIGQMT_FORMULA_ENABLED=0 in the environment
    # "host": "127.0.0.1",  # FormulaServer binds 0.0.0.0, so cross-machine works
    #                       # if the firewall allows it
    # "port": 58600,        # unset -> read from qmt_root's formulaserver.ini,
    #                       # then fall back to 58600
    # "qmt_root": r"D:\国金证券QMT交易端",
    # "timeout_seconds": 3.0,
    # "methods": [...],     # restrict routing to a subset (default: all mapped)
    # "failure_cooldown_seconds": 30.0,  # pause routing this long after a failure
}

# Whole-quote PUSH subscription (xtdata.subscribe_whole_quote, aligned with MiniQMT).
#   Server pushes each incremental tick batch to every client subscribed to the
#   same combination; the RPC methods above only manage the subscription lifecycle.
#   Data flows over a separate push channel matching `transport` above
#   (redis pub/sub, or zmq PUB/SUB when transport="zmq"), msgpack-encoded
#   (install the `msgpack` extra; falls back to json if absent).
#
#   quote_client_id: process-stable subscriber id. The server counts references
#     per (client_id, sub_id) and only tears down the shared big-QMT subscription
#     after EVERY client of a combination unsubscribes or times out. Unset -> a
#     persisted id is created at ~/.cache/bigqmt/quote_client_id so a restarted
#     client is recognised as the same subscriber (needed for replay recovery).
#   Heartbeat: client sends quote_keepalive every BIGQMT_QUOTE_HEARTBEAT_SECONDS
#     (default 3.0). Server reaps a client after heartbeat_timeout_seconds
#     (default 30s = 10 periods, configured server-side).
BIGQMT_QUOTE_CLIENT_ID = None  # e.g. "my-strategy-1"; None -> persisted auto id
# BIGQMT_QUOTE_HEARTBEAT_SECONDS = 3.0  # env var; must be < server timeout/periods

