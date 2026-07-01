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

configure()

acc = StockAccount(xt_trader.client.account_id, "STOCK")
positions = xt_trader.query_stock_positions(acc)
ticks = xtdata.get_full_tick(["600000.SH"])
```

这类写法的优点是替换范围小，适合先在 `core/trader.py` 或独立测试脚本里验证查询链路。`configure()` 会原地更新已导入的 `xt_trader` / `xtdata` 对象，所以可以先 `from ... import xt_trader`，再调用 `configure()`。

## 接入方式二：用 `xtquant` shim 替换老 import

适合最终切换。把本仓库的 `src` 放到 `PYTHONPATH` 最前面后，老代码里的：

```python
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from xtquant import xtdata, xtconstant
```

会命中本仓库提供的 `src/xtquant/` shim。这样主业务代码基本不用改，只需要在本地私有配置文件里设置 Redis 和账号：

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

然后启动前只需要确认本仓库的 `src` 在 `PYTHONPATH` 最前面：

```powershell
$env:PYTHONPATH = "D:\gjzqqmt\xtquant_big_convert\src;$env:PYTHONPATH"
```

如果同一台机器仍然安装了真实 MiniQMT 的 `xtquant` 包，要确认 `D:\gjzqqmt\xtquant_big_convert\src` 位于 `PYTHONPATH` 最前面，否则 Python 会先加载真实 `xtquant`。

## 推荐落地步骤

1. 大 QMT 侧先运行 `BIGQMT_REDIS_DRYRUN` / `bigqmt_signal_trader_redis_rpc_runtime.py`，保持 `rpc_allow_order_methods=False`。
2. 原策略侧用显式导入方式跑查询自检：资产、持仓、单票五档行情、`["SH","SZ"]` 全市场行情。
3. 查询链路稳定后，把原项目中 `core/trader.py` 的初始化切到兼容层，但仍保持远程下单关闭。
4. 对比 MiniQMT 与大 QMT 返回的资产、持仓、委托、成交字段，确认业务字段都能读到。
5. 只在确认风控、账号、委托价型都正确后，在大 QMT 私有配置里打开 `rpc_allow_order_methods=True`。
6. 最终切换时再使用 `xtquant` shim，让旧 import 保持不变。

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
| `xtdata.get_full_tick(codes)` | 已兼容 | 默认直接 RPC 调用；支持单票、ETF、`["SH", "SZ"]` 全市场；可选打开 Redis 快照缓存 |
| `xtdata.get_instrument_detail(code)` | 已兼容 | 映射到大 QMT `get_instrumentdetail()` |
| `xtdata.get_instrument_type(code)` | 已接入 | 优先调大 QMT；不支持时按代码前缀做基础判断 |
| `xtdata.subscribe_quote(...)` / `subscribe_whole_quote(...)` | Redis 订阅兼容 | 写入 `bigqmt:quote_subscriptions:{account_id}`，并向 `bigqmt:quote_events:{account_id}` 发事件；callback 会收到一次当前快照/历史数据 |
| `xtdata.unsubscribe_quote(seq)` | Redis 事件兼容 | 不强依赖大 QMT 反订阅 API，直接删除 Redis 订阅表并推送 `unsubscribe_quote` 事件 |
| `xtdata.get_market_data(...)` | 已接入 RPC | 透传到大 QMT `ContextInfo.get_market_data`，返回 DataFrame/字典结构会自动 JSON 化再还原 |
| `xtdata.get_market_data_ex(...)` | 已接入 RPC | 大 QMT 不支持 `get_market_data_ex` 时回退到 `get_market_data` |
| `xtdata.get_local_data(...)` | 已接入 RPC | 大 QMT 不支持 `get_local_data` 时回退到 `get_market_data` |
| `xtdata.get_stock_list_in_sector(...)` | 已接入 RPC | 优先调大 QMT；失败时对 `"沪深A股"` 用 `get_full_tick(["SH","SZ"])` 过滤 |
| `xtdata.get_sector_list()` / `get_sector_info()` | 已接入 RPC | 依赖大 QMT `ContextInfo` 是否支持 |
| `xtdata.get_divid_factors(...)` | 已接入 RPC | 依赖大 QMT `ContextInfo` 是否支持 |
| `xtdata.download_history_data(...)` / `download_history_data2(...)` | 已接入 RPC | 依赖大 QMT `ContextInfo` 是否支持 |
| `xtdata.get_trading_dates(...)` / `get_holidays()` / `download_holiday_data()` | 已接入 RPC | 依赖大 QMT `ContextInfo` 是否支持 |
| `xtdata.get_ipo_info(...)` | 已接入 RPC | 行情侧新股资料；交易侧 `query_ipo_data()` 仍是占位 |
| `xtdata.get_etf_info()` / `download_etf_info()` | 已接入 RPC | 依赖大 QMT `ContextInfo` 是否支持 |
| `xtdata.get_option_list(...)` / 历史期权列表 | 已接入 RPC | 依赖大 QMT `ContextInfo` 是否支持 |
| `xtdata.get_financial_data(...)` / `download_financial_data(...)` | 已接入 RPC | 支持 DataFrame 返回值序列化 |
| `xtdata.call_formula(...)` / `subscribe_formula(...)` / `unsubscribe_formula(...)` / `get_formula_result(...)` | 已接入 RPC | 对应截图里的模型调用/订阅能力，依赖大 QMT `ContextInfo` 是否支持 |
| `xtdata.gen_factor_index(...)` | 已接入 RPC | 对应生成因子数据，依赖大 QMT `ContextInfo` 是否支持 |
| `query_ipo_data()` / `query_new_purchase_limit()` | 占位兼容 | 当前返回空结果，打新需要后续补大 QMT 等价能力 |

## 下单开关

大 QMT 本地配置默认关闭远程下单。要真正替换 MiniQMT 下单，需要在 QMT 本地私有配置中显式开启：

```python
BIGQMT_REDIS_CONFIG = {
    "host": "YOUR_REDIS_HOST",
    "port": 6379,
    "db": 5,
    "username": "",
    "password": "******",
    "rpc_allow_order_methods": True,
}
```

开启后，`price_type` 会从客户端透传到大 QMT `passorder()`，不会再固定成默认限价。

## 最小自检脚本

这个脚本只读，不会下单：

```python
from bigqmt_signal_trader.xtquant_compat import StockAccount, configure, xt_trader, xtdata

configure()

acc = StockAccount(xt_trader.client.account_id, "STOCK")

asset = xt_trader.query_stock_asset(acc)
positions = xt_trader.query_stock_positions(acc)
tick = xtdata.get_full_tick(["600000.SH"])
all_a = xtdata.get_stock_list_in_sector("沪深A股")

print("cash:", asset.cash)
print("total_asset:", asset.total_asset)
print("positions:", len(positions), positions[:3])
print("bid5:", tick["600000.SH"]["bidPrice"])
print("ask5:", tick["600000.SH"]["askPrice"])
print("hs_a_count:", len(all_a))
```

如果想验证最终 shim 方式：

```python
from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount
from xtquant import xtdata, xtconstant

trader = XtQuantTrader("", 12345)
acc = StockAccount(trader.client.account_id, "STOCK")

assert trader.connect() == 0
assert trader.subscribe(acc) == 0

print(xtconstant.STOCK_BUY)
print(trader.query_stock_asset(acc))
print(xtdata.get_full_tick(["600000.SH"]))
```

## 验证命令

```powershell
cd D:\gjzqqmt\xtquant_big_convert
python -B -m unittest discover -s tests\bigqmt_signal_trader
```

实盘前建议先只跑查询链路：

```python
from bigqmt_signal_trader.xtquant_compat import StockAccount, configure, xt_trader, xtdata

configure()

acc = StockAccount(xt_trader.client.account_id)
print(xt_trader.query_stock_asset(acc))
print(xt_trader.query_stock_positions(acc)[:3])
print(xtdata.get_full_tick(["600000.SH"]))
```

## 注意事项

- `subscribe_quote()` / `subscribe_whole_quote()` 当前通过 Redis 记录订阅意图，并给 callback 推一次当前数据；持续行情推送需要独立的 Redis 行情生产者消费 `bigqmt:quote_subscriptions:{account_id}`。
- `get_full_tick()` 默认直接 RPC 现拉；如果全市场 payload 过大，再在客户端和 QMT 本地配置里打开 Redis 快照缓存。
- `unsubscribe_quote(seq)` 当前按你的要求直接写 Redis：删除订阅表并推送 `unsubscribe_quote` 事件，不等待大 QMT 确认。
- `get_stock_list_in_sector("沪深A股")` 的本地兜底会通过 `get_full_tick(["SH", "SZ"])` 过滤 A 股，速度取决于大 QMT 全市场快照返回耗时。
- 历史行情、财务、ETF、期权、模型/因子等接口已经接到 RPC，但实际是否可用取决于大 QMT 策略环境里的 `ContextInfo` 是否暴露同名方法。
- `query_ipo_data()` / `query_new_purchase_limit()` 当前返回空结果，打新逻辑不能直接视为无损替换。
- RPC 下单默认关闭；打开前必须确认大 QMT 页面正在运行正确账号的 RPC 策略。
