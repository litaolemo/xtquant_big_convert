# 大 QMT Redis Queue RPC 说明

更新时间：2026-07-02

## 目标

在大 QMT 策略进程内启动一个 Redis RPC 服务，用来远程调用少量白名单方法。实盘默认使用 Redis list queue + QMT `run_time("adjust", ...)` 调度 drain；请求 payload 会做安全编码，避免大 QMT 内置 Redis 客户端读取包含股票代码的 JSON 时触发 `Sensitive Data Detected`。

- `ping`
- `get_ticks`
- `get_instrument`
- `get_market_data` / `get_market_data_ex` / `get_local_data`
- `get_stock_list_in_sector` / `get_sector_list` / `get_sector_info`
- `get_divid_factors` / `download_history_data` / `download_history_data2`
- `get_trading_dates` / `get_holidays` / `download_holiday_data`
- `get_ipo_info` / `get_etf_info` / `get_option_list`
- `get_financial_data` / `download_financial_data`
- `call_formula` / `subscribe_formula` / `unsubscribe_formula` / `get_formula_result` / `gen_factor_index`
- `get_positions`
- `get_asset`
- `query_orders`
- `query_trades`
- `sync_positions`

下单类方法 `submit_order`、`cancel_order` 默认关闭，只有显式配置 `rpc_allow_order_methods=True` 后才会开放。

## MiniQMT 兼容方法名

RPC 服务端会把以下 MiniQMT 常用方法名映射到大 QMT 适配器：

| MiniQMT 方法名 | RPC 内部方法 | 说明 |
|---|---|---|
| `query_stock_asset` | `get_asset` | 查询账户资产 |
| `query_stock_positions` | `get_positions` | 查询全部持仓 |
| `query_stock_position` | `query_stock_position` | 查询单只持仓，按 `stock_code` 过滤 |
| `query_stock_orders` | `query_orders` | 查询委托；支持 `cancelable_only` 过滤 |
| `query_stock_trades` | `query_trades` | 查询成交 |
| `get_full_tick` | `get_ticks` | 默认直接 RPC 调用；可选开启 Redis 快照缓存降载 |
| `get_instrument_detail` / `get_instrumentdetail` | `get_instrument` | 查询合约详情 |
| `order_stock` / `order_stock_async` | `submit_order` | 买卖下单；默认关闭 |
| `cancel_order_stock` / `cancel_order_stock_sysid` | `cancel_order` | 撤单；默认关闭 |

`order_stock` 参数兼容 `stock_code`、`order_type`、`order_volume`、`price_type`、`price`、`strategy_name`、`order_remark`。其中 `order_type=23/STOCK_BUY` 映射为买入，`order_type=24/STOCK_SELL` 映射为卖出。

`price_type` 会透传到大 QMT `passorder()`，常用值包括 `11/FIX_PRICE`、`5/LATEST_PRICE`、`44/MARKET_PEER_PRICE_FIRST`、`43/MARKET_SH_CONVERT_5_LIMIT`、`47/MARKET_SZ_CONVERT_5_CANCEL`。

`get_full_tick/get_ticks` 的 `codes` 参数支持两种写法：传合约代码如 `["600000.SH", "000001.SZ"]` 查询指定标的；传市场代码如 `["SH", "SZ"]` 查询全市场全推快照。

注意：兼容层的 `xtdata.get_full_tick(codes)` 默认走 Redis RPC 现调大 QMT。若需要降低全市场行情的大 payload 压力，可在客户端和 QMT 本地配置里显式打开 `full_tick_cache_enabled=True` / `BIGQMT_FULL_TICK_CACHE_CONFIG["enabled"]=True`，改为 Redis 需求驱动快照。

## 实现文件

- `src/bigqmt_signal_trader/redis_rpc.py`：RPC 协议、Redis queue 服务、外部客户端 helper。
- `src/bigqmt_signal_trader/xtquant_compat.py`：MiniQMT 风格客户端兼容层。
- `src/xtquant/`：可选的 `xtquant` import shim，用于最终替换老 import。
- `src/bigqmt_signal_trader_strategy.py`：在 `init` 中启动 RPC；默认由 QMT `run_time("adjust", ...)` drain Redis queue，避免大 QMT 冻结自建后台线程。
- `src/bigqmt_signal_trader_redis_rpc_runtime.py`：大 QMT 策略入口，默认不消费交易信号，只启用 RPC 和持仓同步。
- `tests/bigqmt_signal_trader/test_redis_rpc.py`：RPC 单测。

## 运行方式

把源码同步到 QMT 的 `python` 目录：

```powershell
$srcPkg = '<REPO_ROOT>\src\bigqmt_signal_trader'
$dstPkg = '<QMT_PYTHON_DIR>\bigqmt_signal_trader'
Get-ChildItem -LiteralPath $srcPkg -Force | ForEach-Object {
  Copy-Item -LiteralPath $_.FullName -Destination $dstPkg -Recurse -Force
}

Copy-Item -LiteralPath '<REPO_ROOT>\src\bigqmt_signal_trader_strategy.py' `
  -Destination '<QMT_PYTHON_DIR>\bigqmt_signal_trader_strategy.py' `
  -Force

Copy-Item -LiteralPath '<REPO_ROOT>\src\bigqmt_signal_trader_redis_rpc_runtime.py' `
  -Destination '<QMT_PYTHON_DIR>\bigqmt_signal_trader_redis_rpc_runtime.py' `
  -Force
```

QMT 本地私有配置文件：

```python
# <QMT_PYTHON_DIR>\bigqmt_signal_trader_local_config.py
# coding: utf-8

BIGQMT_ACCOUNT_ID = "你的资金账号"

BIGQMT_REDIS_CONFIG = {
    "host": "YOUR_REDIS_HOST",
    "port": 6379,
    "db": 5,
    "username": "",
    "password": "...",
    "rpc_allow_order_methods": False,
    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    "rpc_background_threads": False,
    "schedule_adjust": True,
    "schedule_adjust_interval": "500nMilliSecond",
    "full_tick_cache_enabled": False,
    "full_tick_demand_ttl_seconds": 10,
    "full_tick_cache_ttl_seconds": 10,
    "full_tick_refresh_interval_seconds": 3,
    "full_tick_max_requests": 8,
}
```

这个文件含账号和 Redis 密码，只放 QMT 本地目录，不提交。

QMT 策略编辑器内容：

```python
#coding:gbk
import sys
import os
import importlib

_qmt_path = os.path.dirname(os.path.abspath(globals().get('__file__', '')))
if not _qmt_path:
    _qmt_path = 'D:/YOUR_QMT_PYTHON_DIR'
if _qmt_path not in sys.path:
    sys.path.insert(0, _qmt_path)

try:
    import bigqmt_signal_trader.redis_rpc as _redis_rpc
    _redis_rpc = importlib.reload(_redis_rpc)
except Exception:
    pass

try:
    import bigqmt_signal_trader_strategy as _strategy
    try:
        _strategy.reset_app()
    except Exception:
        pass
    _strategy = importlib.reload(_strategy)
except Exception:
    pass

import bigqmt_signal_trader_redis_rpc_runtime as _runtime
_runtime = importlib.reload(_runtime)

try:
    from bigqmt_signal_trader_local_config import BIGQMT_REDIS_CONFIG
    _runtime.configure_runtime_redis(BIGQMT_REDIS_CONFIG)
except Exception:
    pass

try:
    from bigqmt_signal_trader_local_config import BIGQMT_ACCOUNT_ID
    _runtime.configure_runtime_account(BIGQMT_ACCOUNT_ID)
except Exception:
    pass

try:
    _runtime.bind_runtime_api(
        passorder_func=passorder,
        cancel_func=cancel,
        get_trade_detail_data_func=get_trade_detail_data,
    )
except NameError:
    pass

init = _runtime.init
handlebar = _runtime.handlebar
adjust = _runtime.adjust
order_callback = _runtime.order_callback
deal_callback = _runtime.deal_callback
```

不要勾选“启动本地 python”。

## Redis 协议

### RPC 请求/响应

请求 channel：

```text
bigqmt:rpc:req:{account_id}
```

请求 payload：

```json
{
  "schema_version": 1,
  "request_id": "req-001",
  "account_id": "YOUR_ACCOUNT_ID",
  "method": "get_positions",
  "params": {},
  "reply_channel": "bigqmt:rpc:resp:YOUR_ACCOUNT_ID:req-001",
  "reply_key": "bigqmt:rpc:resp:YOUR_ACCOUNT_ID:req-001",
  "ttl_seconds": 60
}
```

响应会同时写入：

```text
bigqmt:rpc:resp:{account_id}:{request_id}
```

并 publish 到同名 channel。

响应格式：

```json
{
  "schema_version": 1,
  "request_id": "req-001",
  "account_id": "YOUR_ACCOUNT_ID",
  "method": "get_positions",
  "ok": true,
  "data": {},
  "error": "",
  "handled_at": "2026-07-01 10:30:00"
}
```

### 可选：get_full_tick 需求驱动缓存

默认情况下，`xtdata.get_full_tick(codes)` 直接走 RPC。只有显式打开 `full_tick_cache_enabled=True` / `BIGQMT_FULL_TICK_CACHE_CONFIG["enabled"]=True` 时，客户端才会写入需求：

```text
bigqmt:full_tick:demand:{account_id}
```

其中 hash field 是规范化代码集合的 request id，value 包含：

```json
{
  "request_id": "...",
  "codes": ["SH", "SZ"],
  "requested_at_ts": 1780000000.0,
  "expires_at_ts": 1780000010.0,
  "cache_ttl_seconds": 10
}
```

大 QMT 每轮刷新后写入快照：

```text
bigqmt:full_tick:cache:{account_id}:{request_id}
```

快照 Redis key 的 TTL 默认是 10 秒；客户端还会校验 `updated_at_ts`，超过 `cache_ttl_seconds` 的快照不会返回。第一次调用如果还没有快照，客户端默认最多等待 `3.5s` 等下一轮大 QMT 刷新；**个股列表**仍然没有新快照时回退一次 live RPC(`get_full_tick`)以避免冷启动硬停；**市场代码**(`SH/SZ/BJ/HK`)则抛出超时、不回退 live 拉全市场。

## 外部调用示例

```python
import sys
import redis

sys.path.insert(0, r"<REPO_ROOT>\src")

from bigqmt_signal_trader.redis_rpc import call_redis_rpc

r = redis.Redis(
    host="YOUR_REDIS_HOST",
    port=6379,
    db=5,
    username="",
    password="...",
)

response = call_redis_rpc(
    r,
    account_id="YOUR_ACCOUNT_ID",
    method="get_positions",
    params={},
    timeout_seconds=3,
)

print(response)
```

## 延迟模式

默认实盘模式下，客户端把安全编码后的请求写入 Redis list queue；大 QMT 侧通过 `run_time("adjust", ...)` 高频调度 drain 队列，并在 QMT 官方回调线程里同步调用 handler 和写回 Redis 响应。实测 500ms 调度下，连续请求通常会被同一轮 drain 批量处理，ping/持仓/get_full_tick 多数在十几毫秒返回，最坏会碰到一次调度边界。

当前推荐配置：

```python
BIGQMT_REDIS_CONFIG = {
    "rpc_process_in_listener": True,
    "rpc_background_threads": False,
    "schedule_adjust": True,
    "schedule_adjust_interval": "500nMilliSecond",
}
```

不推荐在大 QMT 中启用自建后台线程。实测部分版本会冻结 daemon thread，且内置 Redis 客户端读取包含股票代码的原始 JSON 会触发 `Sensitive Data Detected`；本仓库客户端 helper 已默认对请求做安全编码。

### 当前真机延迟

最近一次大 QMT 真机测试，`schedule_adjust_interval="500nMilliSecond"`：

| 接口 | 成功率 | 平均 | P50 | P90 | 最大 |
|---|---:|---:|---:|---:|---:|
| `ping` | 30/30 | 20.2ms | 13.3ms | 13.9ms | 226.4ms |
| `query_stock_asset` | 10/10 | 37.2ms | 15.0ms | 16.1ms | 237.2ms |
| `query_stock_positions` | 10/10 | 13.4ms | 13.2ms | 14.5ms | 14.5ms |
| `get_full_tick(["000001.SZ"])` | 20/20 | 24.9ms | 13.6ms | 14.8ms | 239.4ms |
| `get_full_tick` 三只票 | 10/10 | 37.5ms | 17.2ms | 18.4ms | 225.2ms |

结论：常态请求多在 12-18ms；偶发 200ms+ 主要来自 500ms 调度边界。队列测试后无残留，QMT 日志无新增 DataError/Traceback。

## 安全约束

- 默认生产模式不在自建线程里调用 QMT API；QMT API 调用在 `adjust/handlebar` 中处理。
- 默认只读，远程下单关闭。
- 账号不匹配会拒绝请求。
- 响应写 Redis key 并设置 TTL，方便调用端超时后排查。

## 本地测试

```powershell
cd <REPO_ROOT>
python -B -m unittest discover -s tests\bigqmt_signal_trader
```

当前结果：

```text
Ran 68 tests
OK
```

