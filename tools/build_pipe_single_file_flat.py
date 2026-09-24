# -*- coding: utf-8 -*-
"""Build BIGQMT_DRYRUN_PIPE_FLAT_ALL_IN_ONE.py.

与 build_no_redis_single_file_flat.py 同机制（flat 真实代码内嵌），但强制
**命名管道传输**——给「禁 socket、禁 pip、EDR 抓到 connect() 就杀进程」的
券商沙箱用（2026-09-24 实盘报告：pipe 模式下桥仍外发 socket 连接被杀，
根因是 redis 块默认还在、exec 事件链路的懒 client 首个命令就拨号）。

与 no-redis 版的差异：
- 不替换 zmq_transport（pipe_transport 本来就在包里，ctypes/kernel32 零依赖）；
- 不内嵌 bigqmt_no_redis/；
- 强制 transport=pipe + redis_enabled=False + quote_push 关（pipe 没有推送线，
  执行回调由客户端轮询合成，#372）+ download_jobs / full_tick_cache 关
  （这两个需要 redis）。

用法：python tools/build_pipe_single_file_flat.py，产物在
src/BIGQMT_DRYRUN_PIPE_FLAT_ALL_IN_ONE.py（或用 BIGQMT_BUILD_OUT 指定）。
"""
import os

import build_single_file as bsf
import build_no_redis_single_file_flat as flat

ROOT = flat.ROOT
OUT_PATH = os.environ.get(
    "BIGQMT_BUILD_OUT", os.path.join(ROOT, "src", "BIGQMT_DRYRUN_PIPE_FLAT_ALL_IN_ONE.py"))

PIPE_TEMPLATE = flat.FLAT_TEMPLATE

# --- docstring: no-redis 措辞换成 pipe 措辞 ---
PIPE_TEMPLATE = PIPE_TEMPLATE.replace(
    '"""Single-file self-contained Big QMT dry-run strategy (no-redis version).',
    '"""Single-file self-contained Big QMT dry-run strategy (named-pipe version).')
PIPE_TEMPLATE = PIPE_TEMPLATE.replace(
    """All custom modules referenced by bigqmt_no_redis/DRYRUN_no_redis.py are
embedded into this file and loaded in-memory at runtime:

    * bigqmt_signal_trader package (all submodules, with
      transports/zmq_transport overridden by the self-contained no-redis
      version that inlines the encoding helpers)
    * bigqmt_signal_trader_strategy
    * bigqmt_signal_trader_redis_rpc_runtime
    * bigqmt_signal_trader_local_config  (generated from the config block below)

The RPC transport is forced to ZMQ; the third-party redis package is never
imported and the zmq transport path never touches a redis-named module, so
this file loads cleanly in QMT sandboxes whose whitelist rejects redis.

This file never imports any other custom module from disk. It only uses the
standard library plus third-party libraries (zmq, pandas, ...). The embedded
loader below resolves relative imports against the in-memory modules, so you
can copy this one file to a QMT python directory and run it as a strategy
without shipping the package alongside.

Edit BIGQMT_ACCOUNT_ID / BIGQMT_REDIS_CONFIG below before running. The redis
connection fields in the config are ignored in no-redis mode.
""",
    """All custom modules the bridge needs are embedded into this file and
loaded in-memory at runtime:

    * bigqmt_signal_trader package (all submodules)
    * bigqmt_signal_trader_strategy
    * bigqmt_signal_trader_redis_rpc_runtime
    * bigqmt_signal_trader_local_config  (generated from the config block below)

The RPC transport is forced to the Windows named pipe (ctypes/kernel32 only).
No redis, no zmq, no outbound socket connections anywhere: this build is for
QMT sandboxes that kill the process when it dials out. The redis connection
fields in the config below are ignored in pipe mode.

This file never imports any other custom module from disk. The embedded
loader below resolves relative imports against the in-memory modules, so you
can copy this one file to a QMT python directory and run it as a strategy
without shipping the package alongside.

Callbacks: the pipe has no push channel. on_stock_order / on_stock_trade /
on_order_error are synthesized client-side by polling queries (#372, default
1s, BIGQMT_EXEC_POLL_SECONDS). Whole-quote push (subscribe_whole_quote) is
disabled in this build -- poll get_full_tick instead.

Edit BIGQMT_ACCOUNT_ID below before running.
""")

# --- config block：redis 字段后加 pipe 传输与子配置 ---
PIPE_TEMPLATE = PIPE_TEMPLATE.replace(
    '''    "redis_enabled": False,
    "rpc_allow_order_methods": False,''',
    '''    "redis_enabled": False,
    "rpc_allow_order_methods": False,
    # 命名管道传输（本机同主机；ctypes/kernel32，零第三方依赖，不碰 socket）。
    "transport": "pipe",
    "pipe": {
        "pipe_name": "bigqmt_rpc",   # 管道名前缀，账号 id 自动拼在后面；客户端要一致
    },''')

# --- 强制块：zmq 换成 pipe ---
PIPE_TEMPLATE = PIPE_TEMPLATE.replace(
    '''    # Force zmq transport (this is the no-redis version).
    BIGQMT_REDIS_CONFIG = dict(BIGQMT_REDIS_CONFIG or {})
    BIGQMT_REDIS_CONFIG["transport"] = "zmq"
    # setdefault: an explicit False is the #183 drain opt-in, worth 4-6x lower
    # latency on zmq, and assignment overwrote it (#188).
    BIGQMT_REDIS_CONFIG.setdefault("rpc_background_threads", True)
    # Nothing here can reach redis, so say so instead of letting the runtime
    # fill in 127.0.0.1:6379 from its defaults (issues #145 / #147).
    BIGQMT_REDIS_CONFIG["redis_enabled"] = False
    print("[bigqmt_shell] no-redis mode: transport=zmq background_threads=%s "
          "redis_enabled=False" % BIGQMT_REDIS_CONFIG["rpc_background_threads"])''',
    '''    # Force pipe transport (this is the named-pipe build: no socket anywhere).
    BIGQMT_REDIS_CONFIG = dict(BIGQMT_REDIS_CONFIG or {})
    BIGQMT_REDIS_CONFIG["transport"] = "pipe"
    # drain 轮询（adjust 拍），后台线程在这类沙箱里只有 GIL 代价没有收益。
    BIGQMT_REDIS_CONFIG.setdefault("rpc_background_threads", False)
    # Nothing here may dial out: redis 块不下发（懒 client 首个命令就 connect，
    # EDR 抓到就杀进程，2026-09-24 实盘）。
    BIGQMT_REDIS_CONFIG["redis_enabled"] = False
    # pipe 没有推送线：全推推送关掉（客户端轮询 get_full_tick / 执行回调由
    # 客户端轮询合成 #372）；下载队列和快照缓存需要 redis，一并关。
    BIGQMT_REDIS_CONFIG["quote_push"] = {"enabled": False}
    BIGQMT_REDIS_CONFIG.setdefault("download_jobs_enabled", False)
    BIGQMT_REDIS_CONFIG.setdefault("full_tick_cache_enabled", False)
    print("[bigqmt_shell] pipe mode: transport=pipe background_threads=%s "
          "redis_enabled=False quote_push=False" % BIGQMT_REDIS_CONFIG["rpc_background_threads"])''')

assert "transport=pipe" in PIPE_TEMPLATE and '"transport": "pipe",' in PIPE_TEMPLATE, \
    "template replacements did not land"


def main():
    top_dir, package_dir = bsf.resolve_source_dirs()
    sources = bsf.collect_package(package_dir, top_dir)

    extra = {}
    for name in ("bigqmt_signal_trader_strategy.py",
                 "bigqmt_signal_trader_redis_rpc_runtime.py"):
        full = os.path.join(top_dir, name)
        with open(full, "rb") as f:
            extra[name] = f.read()

    funcs_block = flat.module_funcs_block(sources, extra)
    template = PIPE_TEMPLATE.replace("__MODULE_FUNCS_BLOCK__", funcs_block)

    total = sum(len(v) for v in sources.values()) + sum(len(v) for v in extra.values())
    with open(OUT_PATH, "w", encoding="gbk", newline="\n") as f:
        f.write(template)
    print("WROTE %s" % OUT_PATH)
    print("embedded files: %d package + %d top-level" % (len(sources), len(extra)))
    print("embedded raw bytes: %d (flat real code, not string/base64)" % total)


if __name__ == "__main__":
    main()
