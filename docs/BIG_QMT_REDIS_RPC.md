# 大 QMT Redis Pub/Sub RPC 说明

更新时间：2026-07-01

## 目标

在大 QMT 策略进程内启动一个 Redis Pub/Sub 订阅器，用来远程调用少量白名单方法：

- `ping`
- `get_ticks`
- `get_instrument`
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
| `get_full_tick` | `get_ticks` | 查询实时 tick |
| `get_instrument_detail` / `get_instrumentdetail` | `get_instrument` | 查询合约详情 |
| `order_stock` / `order_stock_async` | `submit_order` | 买卖下单；默认关闭 |
| `cancel_order_stock` / `cancel_order_stock_sysid` | `cancel_order` | 撤单；默认关闭 |

`order_stock` 参数兼容 `stock_code`、`order_type`、`order_volume`、`price_type`、`price`、`strategy_name`、`order_remark`。其中 `order_type=23/STOCK_BUY` 映射为买入，`order_type=24/STOCK_SELL` 映射为卖出。

`price_type` 会透传到大 QMT `passorder()`，常用值包括 `11/FIX_PRICE`、`5/LATEST_PRICE`、`44/MARKET_PEER_PRICE_FIRST`、`43/MARKET_SH_CONVERT_5_LIMIT`、`47/MARKET_SZ_CONVERT_5_CANCEL`。

`get_full_tick/get_ticks` 的 `codes` 参数支持两种写法：传合约代码如 `["600000.SH", "000001.SZ"]` 查询指定标的；传市场代码如 `["SH", "SZ"]` 查询全市场全推快照。

## 实现文件

- `src/bigqmt_signal_trader/redis_rpc.py`：RPC 协议、订阅服务、外部客户端 helper。
- `src/bigqmt_signal_trader/xtquant_compat.py`：MiniQMT 风格客户端兼容层。
- `src/xtquant/`：可选的 `xtquant` import shim，用于最终替换老 import。
- `src/bigqmt_signal_trader_strategy.py`：在 `init` 中启动 RPC，在 `adjust/handlebar` 中处理请求队列。
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

## 安全约束

- Pub/Sub 线程只负责接收消息，不直接调用 QMT API。
- QMT API 调用在 `adjust/handlebar` 中通过队列处理，避免在 Redis 订阅线程里碰 QMT 对象。
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
Ran 41 tests
OK
```

