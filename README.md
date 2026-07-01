# xtquant_big_convert

大 QMT 运行环境里的 Redis RPC 桥接包，用于把大 QMT 内置 Python 能力封装成可替换的交易/查询适配层，并兼容一组常用 MiniQMT 方法名。

## 能力

- 在大 QMT 策略进程中启动 Redis queue RPC 服务。
- 查询资产、持仓、委托、成交、tick 和合约详情。
- 透传行情/历史/财务/ETF/期权/模型/因子等 xtdata 风格接口到大 QMT `ContextInfo`。
- 兼容 `query_stock_asset`、`query_stock_positions`、`query_stock_orders`、`query_stock_trades`、`get_full_tick`、`order_stock` 等 MiniQMT 常用方法名。
- 默认生产模式：客户端写入安全编码后的 Redis queue，请求由大 QMT `run_time("adjust", ...)` 调度 drain，避免自建后台线程被冻结。
- `get_full_tick` 默认直接 RPC 调用；仍保留 Redis 需求驱动快照缓存作为全市场行情的可选降载模式。
- 提供 `bigqmt_signal_trader.xtquant_compat` 客户端兼容层，可把原来的 `xt_trader` / `xtdata` 调用转成 Redis RPC。
- 提供可选 `src/xtquant/` shim，可让旧代码的 `from xtquant import xtdata, xtconstant` 命中本仓库兼容实现。
- 行情订阅/反订阅状态写入 Redis：`bigqmt:quote_subscriptions:{account_id}` 和 `bigqmt:quote_events:{account_id}`。
- 默认只读，`order_stock` / `cancel_order_stock_sysid` 等下单撤单接口默认关闭。
- 提供 dry-run 信号消费、Redis 状态写回和持仓同步骨架。

## 目录

- `src/bigqmt_signal_trader/`：核心包和适配器。
- `src/bigqmt_signal_trader/xtquant_compat.py`：MiniQMT 风格客户端兼容层。
- `src/xtquant/`：可选的 `xtquant` import shim。
- `src/bigqmt_signal_trader_strategy.py`：大 QMT 策略基础入口。
- `src/bigqmt_signal_trader_redis_rpc_runtime.py`：只启用 Redis RPC 的大 QMT 入口。
- `src/bigqmt_signal_trader_redis_dryrun.py`：Redis 信号 dry-run 入口。
- `tests/bigqmt_signal_trader/`：无 QMT 环境也能运行的单元测试。
- `docs/`：运行说明和 RPC 协议。

## 快速用法

### 1. 大 QMT 侧

把 `src/bigqmt_signal_trader/`、`src/bigqmt_signal_trader_strategy.py`、`src/bigqmt_signal_trader_redis_rpc_runtime.py` 同步到大 QMT 的 `python` 目录。

在大 QMT 本地创建私有配置文件：

```python
# <QMT_PYTHON_DIR>\bigqmt_signal_trader_local_config.py
BIGQMT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"

BIGQMT_REDIS_CONFIG = {
    "host": "YOUR_REDIS_HOST",
    "port": 6379,
    "db": 5,
    "username": "",
    "password": "******",
    "rpc_allow_order_methods": False,
    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    "rpc_background_threads": False,
    "schedule_adjust": True,
    "schedule_adjust_interval": "500nMilliSecond",
    "full_tick_cache_enabled": False,
    "full_tick_demand_ttl_seconds": 10,
    "full_tick_cache_ttl_seconds": 10,
    "full_tick_refresh_interval_seconds": 0.5,
    "full_tick_market_refresh_interval_seconds": 3,
    "full_tick_refresh_max_wall_seconds": 0.3,
}
```

然后在大 QMT 策略编辑器里运行 `bigqmt_signal_trader_redis_rpc_runtime.py` 对应入口。详细入口脚本见 [docs/BIG_QMT_REDIS_RPC.md](docs/BIG_QMT_REDIS_RPC.md)。

### 2. 原策略侧灰度接入

灰度阶段建议显式导入兼容层，不覆盖机器上的真实 `xtquant`：

```python
from bigqmt_signal_trader.xtquant_compat import StockAccount, configure, xt_trader, xtdata

configure()

acc = StockAccount(xt_trader.client.account_id, "STOCK")
asset = xt_trader.query_stock_asset(acc)
positions = xt_trader.query_stock_positions(acc)
ticks = xtdata.get_full_tick(["600000.SH"])
```

### 3. 最终无损替换

最终切换时，先准备客户端私有配置，再把本仓库 `src` 放到 `PYTHONPATH` 最前面：

先在客户端本地创建私有配置文件：

```python
# D:\gjzqqmt\xtquant_big_convert\src\bigqmt_signal_trader_client_config.py
BIGQMT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"
BIGQMT_RPC_TIMEOUT_SECONDS = 6.0

BIGQMT_REDIS_CONFIG = {
    "host": "YOUR_REDIS_HOST",
    "port": 6379,
    "db": 5,
    "username": "",
    "password": "******",
}

BIGQMT_FULL_TICK_CACHE_CONFIG = {
    "enabled": False,
    "demand_ttl_seconds": 10,
    "cache_ttl_seconds": 10,
    "wait_seconds": 3.5,
}
```

真实配置文件不要提交。最终切换时，只需要把本仓库 `src` 放到 `PYTHONPATH` 最前面：

```powershell
$env:PYTHONPATH = "D:\gjzqqmt\xtquant_big_convert\src;$env:PYTHONPATH"
```

这样旧代码里的下面这些 import 可以保持不变：

```python
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from xtquant import xtdata, xtconstant
```

完整替换说明见 [docs/XTQUANT_COMPAT_REPLACEMENT.md](docs/XTQUANT_COMPAT_REPLACEMENT.md)。

## 为什么不直接连大 QMT 目录

官方 `xtquant.xttrader.XtQuantTrader` 仍然依赖客户端侧的 XtQuantServer/xtquant 通道。当前国金大 QMT 环境中，直接把大 QMT 安装根目录、`userdata`、账户目录或 `XtTradeData` 传给 `XtQuantTrader`，实测 `connect()` 返回 `-1`；大 QMT 监听的 `58600` 端口是 FormulaServer，不是 `xtdata` 行情服务端口，直接对它调用 `get_full_tick` 会返回“未找到处理函数”。

因此本仓库默认不走“外部 Python 直连大 QMT 目录”的模式，而是把真实接口调用放在大 QMT 内部策略进程中执行：

- 大 QMT 内部运行 `bigqmt_signal_trader_redis_rpc_runtime.py`。
- 内部策略使用 `passorder`、`get_trade_detail_data`、`ContextInfo` 行情/调度能力。
- 外部旧系统继续使用 MiniQMT 风格的 `xt_trader` / `xtdata` API，由本仓库兼容层转换为 Redis RPC；全市场行情可按需打开 Redis 快照缓存。

如果后续券商环境明确开通 XtQuantServer 权限，并且 `XtQuantTrader(...).connect() == 0`，可以再增加直连模式；当前生产默认路径仍是“大 QMT 内部策略 + Redis/RPC”。

## 本地测试

```powershell
python -B -m unittest discover -s tests\bigqmt_signal_trader
```

当前测试覆盖 68 个用例。

## 当前实测延迟

大 QMT 端使用 Redis queue + `run_time("adjust", "500nMilliSecond")` drain。最近一次真机测试结果：

| 接口 | 成功率 | 平均 | P50 | P90 | 最大 |
|---|---:|---:|---:|---:|---:|
| `ping` | 30/30 | 20.2ms | 13.3ms | 13.9ms | 226.4ms |
| `query_stock_asset` | 10/10 | 37.2ms | 15.0ms | 16.1ms | 237.2ms |
| `query_stock_positions` | 10/10 | 13.4ms | 13.2ms | 14.5ms | 14.5ms |
| `get_full_tick(["000001.SZ"])` | 20/20 | 24.9ms | 13.6ms | 14.8ms | 239.4ms |
| `get_full_tick` 三只票 | 10/10 | 37.5ms | 17.2ms | 18.4ms | 225.2ms |

常态请求多在 12-18ms；偶发 200ms+ 主要来自 500ms 调度边界。队列测试后无残留，QMT 日志无新增 DataError/Traceback。

## QMT 本地配置

复制配置样例到 QMT 的 `python` 目录，并改成真实配置：

```text
src/bigqmt_signal_trader_local_config.example.py
```

目标文件名：

```text
bigqmt_signal_trader_local_config.py
```

真实配置文件不要提交。里面可能包含资金账号和 Redis 密码。

## 运行入口

在大 QMT 策略编辑器里建议使用 `docs/BIG_QMT_REDIS_RPC.md` 中的 reload 入口脚本。这样更新包文件后，重新运行策略即可刷新 `redis_rpc` 子模块，避免 QMT 进程缓存旧白名单。

## 安全默认值

`rpc_allow_order_methods` 默认为 `False`。此时远程调用 `order_stock` 会被拒绝，适合先上线查询和持仓同步链路。只有确认接入方、账号和风控后，再在本地私有配置里显式打开。
