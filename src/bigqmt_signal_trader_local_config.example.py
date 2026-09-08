# coding: utf-8
"""Local private config example for the QMT python directory.

Copy this file to the QMT python directory as:

    bigqmt_signal_trader_local_config.py

Do not commit the real file. It may contain account ids and Redis credentials.
"""

BIGQMT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"

# STOCK(股票) / CREDIT(信用两融) / FUTURE(期货) / STOCK_OPTION(股票期权) ...
# 信用账户务必设为 CREDIT：按 STOCK 查询不会报错，get_trade_detail_data 会返回
# 一行全 0 的资产，表现为「信用账户资产全是 0」而日志里毫无线索（issue #92）。
# 也可以写在下面 BIGQMT_REDIS_CONFIG 的 "account_type" 里，两处都认；解析结果
# 会在启动时打印，冲突也会指出来。
BIGQMT_ACCOUNT_TYPE = "STOCK"

BIGQMT_REDIS_CONFIG = {
    # 这台机器上有没有 redis 可用。默认 True，仅对非 redis 传输生效。
    #
    # 设 False 时整个 redis 块不再下发给策略，于是委托身份库、异步下载任务、
    # 全推快照缓存、exec 事件推送都不会去连 redis —— 一次都不试。用在券商 QMT
    # 的 import 白名单不含 redis、或纯 zmq 部署本来就没装 redis 的场景
    # （issue #145 / #147）。下面的 host/port 有默认值，所以不设这一项时
    # 「配了 redis」和「什么都没写」是分辨不出来的。
    #
    # 代价：查询里的 strategy_name 回填失效（那份记录只存在 redis 里，见 #133）；
    # 异步下载任务和全推快照缓存不可用（这两个在大 QMT 上本来就默认关闭）。
    # 委托/成交回调不受影响 —— 它们会走 zmq 推送通道。
    # "redis_enabled": False,
    "host": "127.0.0.1",
    "port": 6379,
    "db": 5,
    "username": "",
    "password": "",
    # Keep order RPC disabled unless you explicitly want remote order/cancel.
    "rpc_allow_order_methods": False,
    # 未指定 strategy_name 的委托带的 投资备注，默认 "bigqmt_rpc"。
    # 注意这不是内部字段：QMT 把它显示在 委托 列表的「报单来源」列里
    # （issue #154），任何看那个界面的人都能看到。设成自己的名字，或者设成
    # "" 让该列留空 —— 和手动下单一样。单次调用传 strategy_name= 始终优先。
    # "rpc_default_strategy_name": "",
    # Trade-context methods (LISTENER_DEFERRED_METHODS) always run on QMT's
    # run_time("adjust", ...) callback -- get_trade_detail_data returns EMPTY
    # off the main strategy thread. That is enforced when the listener list is
    # expanded, so no value below can move them (#244).
    #
    # rpc_background_threads is therefore a pure latency choice, and it differs
    # per transport (100 read methods, live terminal -- docs/LATENCY_REPORT.md):
    #     redis  True 3.4ms   / False 30.7ms   <- this file ships redis
    #     zmq    True 592.9ms / False 15.8ms
    #     pipe   True 189.0ms / False 94.4ms
    # Only redis wants True: its brpop wake is immediate, while zmq/pipe
    # background threads pay a cross-thread GIL handoff (~1 adjust tick) per
    # round trip. Switching transport? Switch this too.
    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    "rpc_background_threads": True,
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
    # Dump the raw order_callback/deal_callback object fields to the QMT output
    # panel, and attach them to the published event as "raw_fields". Prints on
    # every callback, so keep it off outside a diagnosis window. Turn it on to
    # observe what m_nDirection / m_nOffsetFlag actually carry in live callbacks
    # — the buy/sell mapping in exec_events.py currently assumes 48/49 there.
    "exec_events_debug_raw_fields": False,
}
