# FormulaServer 直连快速路径（58600）

## 这是什么

大 QMT 的 `58600` 端口是 **FormulaServer** —— QMT 内置的 C++ 行情/参考数据服务。端口取自
QMT 安装目录的 `config/formulaserver/formulaserver.ini`：

```ini
[server_formula]
address = 0.0.0.0:58600
```

QMT 自带 Python 里就有它的官方客户端：`bin.x64/Lib/site-packages/qmt_api`。协议是
**BSON over TCP**，帧格式在 `qmt_api/net/RPCBase.py`：

```
| packLen(uint32 BE) | seq(uint32 BE) | cmd(uint16 BE) | tag(uint16 BE) | BSON body |
```

`cmd = 3`（`NET_CMD_RPC`），body 是 `{"func": <名字>, "params": {...}}`，
响应 `{"status": 0, "params": {...}}`，`status != 0` 时 `params` 里带 `ErrorID`/`ErrorMsg`。
`tag & 7` 标记 zlib 压缩，`tag >> 8` 的低 4 位是 seq 的高位。

## 为什么值得接

原来所有只读请求都要绕一整圈：

```
客户端 → redis/zmq → QMT python 策略线程 → ContextInfo → 原路返回
```

这不只是慢，还要和策略自己抢 QMT 主线程的 GIL —— zmq 传输约 30% 的请求会撞上 ~500ms 的
调度尖峰，就是这么来的。

直连 FormulaServer 完全绕开策略进程：

| 路径 | p50 |
|------|-----|
| redis RPC | ~13ms |
| zmq RPC | ~0.7ms（30% 撞 500ms GIL 尖峰）|
| **FormulaServer 直连** | **0.07ms**，无 GIL 竞争 |

穿过完整客户端栈（`BigQmtRpcClient.call`）实测 **0.145ms/次**，比 redis 快约 90 倍。

## 能力边界

**FormulaServer 只有行情/参考数据。** 实测所有账户/交易类方法一律返回
`ErrorID 200005 未找到该服务`：

```
getAsset / getPositions / getAccountDetail / passorder   -> 200005
getFullTick / getQuote                                   -> 200005
```

所以它是**只读快速路径，不是 RPC 桥的替代品**。交易、账户查询、持仓、委托、成交、
五档盘口全部仍然走 RPC。

### 已接入的方法（10 个）

| 我们的方法 | FormulaServer func |
|---|---|
| `get_instrument` / `get_instrument_detail` / `get_instrumentdetail` | `getInstrumentDetail` |
| `get_last_volume` | `getLastVolume` |
| `get_total_share` | `getTotalShare` |
| `get_contract_multiplier` | `getContractMultiplier` |
| `get_main_contract` | `getMainContract` |
| `get_weight_in_index` | `getWeightInIndex` |
| `get_stock_list_in_sector` | `getStockListInSector` |
| `get_market_data_ex` | `getMarketData` |

### 刻意不接的方法，以及原因

宁可慢，不能悄悄给错数据。以下几项参数语义与我们的调用方不一致：

- **`get_trading_dates`** —— FormulaServer 要的是**股票代码**。实测：
  ```
  {'stockCode': 'SH',        ...} -> {'result': []}          # 静默空
  {'stockCode': '000001.SZ', ...} -> ['20260630', '20260701', ...]
  ```
  而 `market_bigqmt.get_trading_dates(market, ...)` 的调用方传的是市场代码。传错了不报错、
  只给空列表，交易日历错了后果太重。

- **`get_divid_factors`** —— 我们是 `(stock_code, start_time, end_time)` 区间，
  FormulaServer 是 `(stockCode, date)` 单日。

- **`get_risk_free_rate`** —— 我们传 `index=-1`，FormulaServer 要 `timetag`。语义不同。

- **复权 K 线** —— 实测 `dividendType` 传 `none` 和 `front` 返回**完全相同**的价格，
  说明复权没有生效。因此只有 `dividend_type="none"`（或空）才走直连，其他复权类型直接
  判为 unroutable 回退 RPC。否则策略要前复权、拿到的却是不复权价格，且毫无提示。

### 字段名坑

FormulaServer 的 `getInstrumentDetail` 返回 **`FloatVolumn` / `TotalVolumn`**（官方拼写错误），
而原生 xtdata SDK 用的是 `FloatVolume` / `TotalVolume`。下游代码按 SDK 拼写读，直接透传会
静默读到 `None`。所以 `_instrument_result` 做了别名归一化，两种拼写都保留。

### 尚未验证

`qmt_api/api.py` 的 `getMarketData` 支持 `fields=['quoter']`，注释说会返回
`askPrice/askVol/bidPrice/bidVol`（level1 五档 / level2 十档）。**如果这在盘中可用，
`get_full_tick` 也能走直连** —— 这是热路径，收益很大。

但收盘时段实测返回空，无法确认。需要**盘中**再测一次：

```python
c.request('getMarketData', {'fields': ['quoter'], 'stockCodes': ['000001.SZ'],
                            'startTime': '', 'endTime': '', 'period': 'tick',
                            'dividendType': 'none', 'count': -1})
```

在确认之前不要接 —— 没验证就上映射，正是订单方向判定踩过的坑。

## 失败行为

**任何失败都自动回退 RPC**，所以连不上 58600 的客户端行为与改动前完全一致：

| 情况 | 行为 |
|---|---|
| 方法不在映射表 | `supports()` 返回 False，直接走 RPC |
| 参数 translate 不了 | `Unroutable`，走 RPC，**不**触发熔断（这是单次调用的问题） |
| 服务连不上 / IO 失败 | `Unroutable` + 熔断 `failure_cooldown_seconds`（默认 30s），期间全部走 RPC |
| 服务端回 `200005` | 该方法永久标记 unimplemented，只停这一个方法，不影响其他 |
| socket 断了（QMT 重启） | 自动重连重试一次 |

## 依赖

**不需要装任何东西。** BSON 编解码内置了无依赖实现；如果环境里有 pymongo 的 `bson`
或 QMT 的 `xtquant.xtbson`，会优先用（更快、更久经考验）。两条路径的输出实测逐字节一致，
测试里有对拍用例。

## 配置

客户端侧，默认开启，通常不用写：

```python
BIGQMT_FORMULA_SERVER_CONFIG = {
    "enabled": True,            # 或环境变量 BIGQMT_FORMULA_ENABLED=0 关闭
    # "host": "127.0.0.1",     # 绑的是 0.0.0.0，跨机可达（需放行防火墙）
    # "port": 58600,           # 不写则从 qmt_root 的 ini 读，再退回 58600
    # "qmt_root": r"D:\QMT交易端",
    # "timeout_seconds": 3.0,
    # "methods": ["get_instrument"],      # 只路由白名单
    # "failure_cooldown_seconds": 30.0,
}
```

也可以写在 `BIGQMT_REDIS_CONFIG["formula_server"]` 里，后者优先级更高。

## 排查

```python
client._formula_router().stats()
# {'enabled': True, 'hits': 202, 'misses': 0, 'available': True,
#  'unimplemented': [], 'methods': [...]}
```

启动时会打一行：

```
[bigqmt_formula] active at 127.0.0.1:58600 (10 methods routed direct)
```

熔断时：

```
[bigqmt_formula] unavailable, falling back to RPC for 30s: connect 127.0.0.1:58600 failed: ...
```
