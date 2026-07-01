# MiniQMT 无损替换兼容层

更新时间：2026-07-01

## 目标

把原来依赖 MiniQMT 的调用：

```python
from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount
from xtquant import xtdata, xtconstant
```

替换为“大 QMT 策略进程 + Redis RPC”的远程调用，同时尽量保持业务代码继续使用：

```python
xt_trader.query_stock_positions(acc)
xt_trader.query_stock_asset(acc)
xt_trader.query_stock_orders(acc)
xt_trader.query_stock_trades(acc)
xt_trader.order_stock(...)
xt_trader.order_stock_async(...)
xt_trader.cancel_order_stock_sysid(...)
xtdata.get_full_tick(...)
```

## 接入方式一：显式导入新包

适合先灰度，不影响机器上的真实 `xtquant` 包。

```python
from bigqmt_signal_trader.xtquant_compat import (
    StockAccount,
    configure,
    xt_trader,
    xtdata,
)
from bigqmt_signal_trader import xtquant_compat as xtconstant

configure(
    account_id="1234567890",
    redis_config={
        "host": "192.168.1.100",
        "port": 63790,
        "db": 5,
        "username": "",
        "password": "******",
    },
    timeout_seconds=6,
)

acc = StockAccount("1234567890", "STOCK")
positions = xt_trader.query_stock_positions(acc)
ticks = xtdata.get_full_tick(["600000.SH"])
```

## 接入方式二：用 `xtquant` shim 替换老 import

适合最终切换。把本仓库的 `src` 放到 `PYTHONPATH` 最前面后，老代码里的：

```python
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from xtquant import xtdata, xtconstant
```

会命中本仓库提供的 `src/xtquant/` shim。这样主业务代码基本不用改，只需要确保启动前设置 Redis 和账号环境变量：

```powershell
$env:PYTHONPATH = "D:\gjzqqmt\xtquant_big_convert\src;$env:PYTHONPATH"
$env:BIGQMT_ACCOUNT_ID = "1234567890"
$env:BIGQMT_REDIS_HOST = "192.168.1.100"
$env:BIGQMT_REDIS_PORT = "63790"
$env:BIGQMT_REDIS_DB = "5"
$env:BIGQMT_REDIS_USERNAME = ""
$env:BIGQMT_REDIS_PASSWORD = "******"
```

## 当前已兼容的方法

| MiniQMT 调用 | 兼容状态 | 说明 |
|---|---|---|
| `XtQuantTrader(path, session_id)` | 已兼容 | 构造本地 RPC 客户端，不连接 MiniQMT |
| `register_callback()` | 已兼容 | 保存 callback；RPC 暂不推送回调 |
| `start()` / `connect()` / `subscribe()` | 已兼容 | 返回 `0`，`subscribe()` 会补账号 |
| `query_stock_asset(acc)` | 已兼容 | 返回对象含 `cash`、`available_cash`、`total_asset`、`market_value` |
| `query_stock_positions(acc)` | 已兼容 | 返回对象列表，含 `stock_code`、`volume`、`can_use_volume`、`avg_price`、`price` |
| `query_stock_position(acc, code)` | 已兼容 | 返回单只持仓对象或 `None` |
| `query_stock_orders(acc, cancelable_only=False)` | 已兼容 | 返回对象列表，含 `order_type`、`order_status`、`order_volume`、`traded_volume`、`order_sysid` |
| `query_stock_trades(acc)` | 已兼容 | 返回对象列表，含 `order_type`、`traded_volume`、`traded_price` |
| `order_stock()` / `order_stock_async()` | 已兼容 | 需要大 QMT 本地配置打开 `rpc_allow_order_methods=True` |
| `cancel_order_stock_sysid()` | 已兼容 | 需要大 QMT 本地配置打开 `rpc_allow_order_methods=True` |
| `xtdata.get_full_tick(codes)` | 已兼容 | 支持单票、ETF、`["SH", "SZ"]` 全市场；返回五档字段 |
| `xtdata.get_instrument_detail(code)` | 已兼容 | 映射到大 QMT `get_instrumentdetail()` |
| `xtdata.subscribe_whole_quote(codes, callback)` | 基础兼容 | 当前做一次 `get_full_tick` 并调用 callback，不是持续推送 |
| `xtdata.get_stock_list_in_sector("沪深A股")` | 基础兼容 | 通过 `get_full_tick(["SH","SZ"])` 结果过滤沪深 A 股 |
| `query_ipo_data()` / `query_new_purchase_limit()` | 占位兼容 | 当前返回空结果，打新需要后续补大 QMT 等价能力 |

## 下单开关

大 QMT 本地配置默认关闭远程下单。要真正替换 MiniQMT 下单，需要在 QMT 本地私有配置中显式开启：

```python
BIGQMT_REDIS_CONFIG = {
    "host": "192.168.1.100",
    "port": 63790,
    "db": 5,
    "username": "",
    "password": "******",
    "rpc_allow_order_methods": True,
}
```

开启后，`price_type` 会从客户端透传到大 QMT `passorder()`，不会再固定成默认限价。

## 验证命令

```powershell
cd D:\gjzqqmt\xtquant_big_convert
python -B -m unittest discover -s tests\bigqmt_signal_trader
```

实盘前建议先只跑查询链路：

```python
from bigqmt_signal_trader.xtquant_compat import StockAccount, configure, xt_trader, xtdata

configure(
    account_id="1234567890",
    redis_config={
        "host": "192.168.1.100",
        "port": 63790,
        "db": 5,
        "password": "******",
    },
)

acc = StockAccount("1234567890")
print(xt_trader.query_stock_asset(acc))
print(xt_trader.query_stock_positions(acc)[:3])
print(xtdata.get_full_tick(["600000.SH"]))
```
