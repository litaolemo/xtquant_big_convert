# RPC API 参考

本文档列出大 QMT RPC 服务对外暴露的全部方法、参数、返回值，以及每个方法在大 QMT 内部的实现来源与注意事项。

> 方法集合的权威定义在 `src/bigqmt_signal_trader/redis_rpc.py`：
> `READ_METHODS`（只读白名单）、`ORDER_METHODS`（下单白名单）、`MARKET_DATA_METHODS`（转发给行情适配器）、`METHOD_ALIASES`（MiniQMT 风格别名）。

---

## 总览

| 类别 | 方法数 | 说明 |
|------|-------|------|
| 系统 | 1 | `ping` |
| 行情快照 | 2 | `get_ticks` / `get_instrument` |
| 行情/K线（转发适配器）| 29 | 见下表 |
| 账户/持仓/委托 | 5 | `get_asset` / `get_positions` / `query_stock_position` / `query_orders` / `query_trades` |
| 持仓同步 | 1 | `sync_positions` |
| 下单/撤单 | 2 | `submit_order` / `cancel_order`（默认关闭）|
| **合计** | **38 只读 + 2 下单 = 40** | |

另有 **12 个 MiniQMT 风格别名**（见末节），调用时自动映射到上表方法。

---

## 1. 系统

### `ping`
- **参数**：无
- **返回**：`{"pong": True, "account_id": "...", "server_time": "YYYY-MM-DD HH:MM:SS"}`
- **用途**：探活、确认 RPC 服务在线与归属账号。
- **实测延迟**：Redis ~13ms（p50）。

---

## 2. 行情快照

### `get_ticks`
- **别名**：`get_full_tick`
- **参数**：
  - `codes`（list[str]，必填）：股票代码列表，如 `["000001.SZ", "600000.SH"]`
  - 或 `code`（str）：单个代码（`codes` 优先）
  - 支持整市场快照：`codes=["SH"]` / `["SZ"]` / `["BJ"]` / `["HK"]`
- **返回**：`dict`，key 为股票代码，value 含五档盘口：
  ```python
  {"000001.SZ": {
      "lastPrice": 12.34, "open": 12.20, "high": 12.50, "low": 12.10,
      "lastClose": 12.25, "volume": 12345600, "amount": 1.5e8,
      "askPrice": [12.33, ...10档], "bidPrice": [12.32, ...10档],
      "askVol":  [...], "bidVol": [...],
      "pvolume": ..., "transactionNum": ..., "stockStatus": ...,
      "time": 1719...（毫秒时间戳）, "stime": "20240701 15:00:00"
  }}
  ```
- **实现**：透传 `ContextInfo.get_full_tick(code_list)`，原生返回什么字段就回传什么字段（不做转换）。
- **注意**：整市场快照（`["SH"]`）数据量大，建议配合客户端 `full_tick_cache` 降载。

### `get_instrument`
- **别名**：`get_instrument_detail` / `get_instrumentdetail`
- **参数**：`code`（str，必填）：股票代码
- **返回**：`dict`，合约详情（名称、上市日、合约乘数、最小变动价位等约 30 个字段）。
- **实现**：`ContextInfo.get_instrumentdetail(code)`。

---

## 3. 行情 / K线 / 板块 / 日历 / 下载 / 财务 / 期权 / 因子

下列 29 个方法统一通过 `_handle_market_data_method` **按方法名转发给 `BigQmtMarketDataProvider` 的同名方法**，参数字典直接 `**kwargs` 展开。调用方按下方签名传参即可。

### 3.1 品种/类型

| 方法 | 参数 | 说明 |
|------|------|------|
| `get_instrument_type` | `code`（str），可选 `variety_list`（list）| 返回 `{"stock":bool,"fund":bool,"etf":bool,"bond":bool,"index":bool}`；传 `variety_list` 则只返回指定品种的 bool |

### 3.2 K线/历史行情

| 方法 | 参数 | 返回 |
|------|------|------|
| `get_market_data` | `field_list`(list) `stock_list`(list) `period`("1d"/"1m"/"5m"/"tick") `start_time` `end_time` `count`(int) `dividend_type`("none"/"front"/"back") `fill_data`(bool) | DataFrame（自动还原）|
| `get_market_data_ex` | 同上 | `dict[code -> DataFrame]` |
| `get_local_data` | 同上 + 可选 `data_dir` | `dict[code -> DataFrame]` |

> DataFrame / Series 在 RPC 协议层用 `__bigqmt_type__` 标记序列化，客户端 `xtquant_compat` 自动还原为 pandas 对象。

### 3.3 板块

| 方法 | 参数 | 返回 | Big QMT 实现说明 |
|------|------|------|----------------|
| `get_stock_list_in_sector` | `sector_name`(str) 可选 `real_timetag`(int,默认-1) | `list[str]` 代码列表 | `ContextInfo.get_stock_list_in_sector` |
| `get_sector_list` | 无 | `list[str]` 板块名 | ⚠️ 见下方说明 |
| `get_sector_info` | `sector_name`(str) | 板块详情 | `ContextInfo.get_sector_info` |

**`get_sector_list` 在大 QMT 的实现说明（重要）**：
板块列表是**全局数据**，原生 `xtdata` SDK 的 `get_sector_list()`（SDK 第 784 行）才有，`ContextInfo` 没有此方法。但大 QMT（完整交易端）进程里，原生 `xtdata` SDK 的 `get_client()` **连不上行情服务**（报「无法连接行情服务」，因为没有 MiniQMT 进程写 `~/.xtquant/*/xtdata.cfg`）。

因此适配器按优先级降级：
1. 原生 `xtdata` SDK（MiniQMT 环境）→ 真实板块列表
2. `ContextInfo.get_sector_list`（不存在，跳过）
3. **fallback**：返回一组常用板块名（`沪深A股`/`沪市A股`/`深市A股`/`科创板`/`创业板`/`沪深ETF`/`上证期权`/`深证期权`/`中金所` 等 13 个），可继续驱动 `get_stock_list_in_sector(name)`。

### 3.4 交易日历 / 节假日

| 方法 | 参数 | 返回 | Big QMT 实现 |
|------|------|------|-------------|
| `get_trading_dates` | `market`(str 如 "SH") `start_time` `end_time` `count`(int) | `list` 日期（`YYYYMMDD` 字符串或毫秒时间戳）| `ContextInfo.get_trading_dates` ✅ |
| `get_holidays` | 无 | `list[str]` 假日（`YYYYMMDD`）| ⚠️ fallback 见下 |
| `get_markets` | 无 | `list[str]` = `["SH","SZ","BJ","HK"]` | 合成（Big QMT/xtdata 均无此函数）|
| `get_market_last_trade_date` | `market`(str) | 最后一交易日（`YYYYMMDD`）| 由 `get_trading_dates(market,count=1)` 派生 |

**`get_trading_dates` 参数说明（重要）**：
`ContextInfo` 桩签名是 `get_trading_dates(stockcode, ...)`，`xtdata` SDK 签名是 `get_trading_dates(market, ...)`——**第一参数语义不同**。本系统所有调用方传的都是 market（如 `"SH"`），走 ContextInfo 时 QMT 内部会从 stockcode 推 market，A 股日历各市场基本一致，故结果正确。

**`get_holidays` 在大 QMT 的实现说明（重要）**：
节假日列表同样是全局数据，只有原生 `xtdata` SDK 的 `get_holidays()`（SDK 第 1197 行）有。大 QMT 进程连不上 SDK 行情服务时，适配器**从交易日历反推**：取 `[去年1月1日, 今天]` 区间内所有工作日（周一至周五），凡是 `get_trading_dates("SH")` 里**没有的**就是假日。比 SDK 慢但结果正确。

### 3.5 数据下载

| 方法 | 参数 | 说明 |
|------|------|------|
| `download_history_data` | `stock_code` `period` `start_time` `end_time` 可选 `incrementally` | 下载单合约历史 |
| `download_history_data2` | `stock_list`(list) `period` `start_time` `end_time` 可选 `incrementally` | 批量下载 |
| `download_holiday_data` | `incrementally`(bool) | 下载假日数据 |
| `download_etf_info` | 无 | 下载 ETF 信息 |

### 3.6 财务 / ETF / 期权 / IPO

| 方法 | 参数 | 说明 |
|------|------|------|
| `get_financial_data` | `stock_list`(list) `table_list`(list) `start_time` `end_time` `report_type`("report_time") | 财务数据 |
| `download_financial_data` | 同上 + `incrementally` | 下载财务 |
| `download_financial_data2` | `stock_list` `table_list` `start_time` `end_time` | 批量下载财务 |
| `get_etf_info` | 无 | ETF 信息 |
| `get_ipo_info` | `start_time` `end_time` | IPO 信息 |
| `get_option_list` | `undl_code` `dedate` `opttype` `isavailavle`(bool) | 期权列表 |
| `get_his_option_list` | `undl_code` `dedate` | 历史期权 |
| `get_his_option_list_batch` | `undl_code` `start_time` `end_time` | 批量历史期权 |
| `get_divid_factors` | `stock_code` 可选 `start_time`/`end_time` | 除权除息因子 |

**`get_divid_factors` 参数说明（重要）**：
`ContextInfo` 桩签名是 `get_divid_factors(marketAndStock, date='')`——**只收 2 个参数**（代码 + 单个日期）。适配器接受 `start_time`/`end_time` 以保持接口兼容，但实际只把 `end_time`（或 `start_time`）作为单个 `date` 传入。

### 3.7 因子 / 模型

| 方法 | 参数 | 说明 |
|------|------|------|
| `call_formula` | `formula_name` `stock_code` `period` `start_time` `end_time` `count` `dividend_type` `extend_param`(dict) | 调用公式 |
| `subscribe_formula` | 同上 | 订阅公式 |
| `unsubscribe_formula` | `request_id` | 取消订阅 |
| `get_formula_result` | `request_id` `start_time` `end_time` `count` `timeout_second` | 取公式结果 |
| `gen_factor_index` | `data_name` `formula_name` `vars` `sector_list`(list) `start_time` `end_time` `period` `dividend_type` | 生成因子 |

---

## 4. 账户 / 持仓 / 委托

下列方法的 `account_id` 参数均可选（不传则用服务端配置的账号）。也接受 `account`（对象/dict）。

### `get_asset`
- **别名**：`query_stock_asset`
- **参数**：`account_id`(str, 可选)
- **返回**：`{"cash":..., "total_asset":..., "market_value":..., "account_id":...}`
- **实现**：`get_trade_detail_data(account, type, "ASSET")`。

### `get_positions`
- **别名**：`query_stock_positions`
- **参数**：`account_id`(str, 可选)
- **返回**：`dict[code -> {stock_code, stock_name, volume, available, cost, ...}]`
- **实现**：`get_trade_detail_data(account, type, "POSITION")`。
- **容错**：QMT 上下文未绑定时报错，适配器降级为返回 `{}`。

### `query_stock_position`
- **参数**：`account_id`(可选) `stock_code`(str, 必填) 或 `code`
- **返回**：单个持仓 dict（同上 value 结构），无持仓返回 `None`。

### `query_orders`
- **参数**：`account_id`(可选) `strategy_name`(str, 默认 "bigqmt_signal_trader") `cancelable_only`(bool)
- **返回**：`list[OrderSnapshot]`，每项含 `order_sys_id`/`user_order_id`/`stock_code`/`action`/`volume`/`traded_volume`/`status`/`price` 等。
- **实现**：`get_trade_detail_data(account, type, "ORDER", strategy)`。
- **容错**：QMT 上下文未绑定时报错，降级为 `[]`。

### `query_trades`
- **参数**：`account_id`(可选) `strategy_name`(str)
- **返回**：成交明细 `list`。

---

## 5. 持仓同步

### `sync_positions`
- **参数**：`account_id`(可选) `reason`(str, 默认 "rpc")
- **返回**：`AccountSnapshot`（含 asset + positions）
- **用途**：主动触发把当前持仓快照写入 Redis（key `bigqmt:positions:{account_id}`），供客户端缓存。
- **注意**：属 `LISTENER_DEFERRED_METHODS`，在 redis 传输 + listener 模式下会延迟到 adjust 线程执行（避免阻塞收包线程）。

---

## 6. 下单 / 撤单（默认关闭）

> ⚠️ 默认 `rpc_allow_order_methods=False`，调用会被 `PermissionError` 拒绝。确认账号/风控/接入方后，在配置里设 `"rpc_allow_order_methods": True` 开启。

### `submit_order`
- **别名**：`order_stock` / `order_stock_async`
- **参数**：
  - `stock_code`(str, 必填)
  - `action`(str)：`"BUY"` / `"SELL"`；或 `order_type`（`23`/`STOCK_BUY`/`BUY` 买，`24`/`STOCK_SELL`/`SELL` 卖）
  - `volume`(int, 必填) 或 `order_volume`
  - `price`(float)
  - `price_type`(str, 默认 `"LIMIT"`)：`LIMIT`(11)/`LATEST`(5)/对手价(44) 等
  - `account_id`(可选) `strategy_name` `signal_id` `remark`/`order_remark`
- **返回**：`{"order_sys_id":..., "user_order_id":...}`
- **实现**：`passorder(op_type, combo_type, account, code, price_type, price, volume, ..., quicktrade=2)`。

### `cancel_order`
- **别名**：`cancel_order_stock` / `cancel_order_stock_sysid`
- **参数**：`order_sys_id` 或 `order_sysid` 或 `order_id`（必填）可选 `user_order_id` `market`
- **返回**：撤单结果。

---

## 7. MiniQMT 风格别名

旧代码若用 MiniQMT 方法名，调用时自动映射（无需改业务代码）：

| 别名（MiniQMT）| 映射到 |
|----------------|--------|
| `get_full_tick` | `get_ticks` |
| `get_instrument_detail` / `get_instrumentdetail` | `get_instrument` |
| `getDividFactors` | `get_divid_factors` |
| `query_stock_asset` | `get_asset` |
| `query_stock_positions` | `get_positions` |
| `query_stock_orders` | `query_orders` |
| `query_stock_trades` | `query_trades` |
| `order_stock` / `order_stock_async` | `submit_order` |
| `cancel_order_stock` / `cancel_order_stock_sysid` | `cancel_order` |

> 客户端用 `xtquant_compat` 时，`xt_trader.query_stock_positions(acc)`、`xtdata.get_full_tick([...])` 等调用会自动走别名映射，最终命中上表方法。

---

## 8. 大 QMT 环境的能力边界（重要）

核对 QMT 官方文档（`trading_function.html` / `data_function.html`）、ContextInfo IDE 桩（`_PyContextInfo.py`）、原生 xtdata SDK（`bin.x64/.../xtquant/xtdata.py`）三处后，确认：

| 能力 | 大 QMT（完整交易端）| MiniQMT / xtdata SDK |
|------|--------------------|---------------------|
| 行情快照（`get_full_tick`）| ✅ ContextInfo | ✅ xtdata |
| K线（`get_market_data_ex` 等）| ✅ ContextInfo | ✅ xtdata |
| 合约详情（`get_instrumentdetail`）| ✅ ContextInfo | ✅ xtdata |
| 板块内股票（`get_stock_list_in_sector`）| ✅ ContextInfo | ✅ xtdata |
| 交易日历（`get_trading_dates`）| ✅ ContextInfo | ✅ xtdata |
| **板块列表**（`get_sector_list`）| ⚠️ fallback 常用板块 | ✅ xtdata（需连行情服务）|
| **节假日**（`get_holidays`）| ⚠️ 从日历反推 | ✅ xtdata（需连行情服务）|
| `get_markets` | 合成 4 市场 | 无此函数 |
| `get_market_last_trade_date` | 从日历派生 | 无此函数 |
| 交易（下单/撤单/查持仓）| ✅ passorder + get_trade_detail_data | ✅ XtQuantTrader |

**结论**：除「板块完整列表」「节假日原始数据」在大 QMT 端只能 fallback 外，其余 API 在大 QMT 环境下均能返回真实数据。需要原始板块/假日数据时，需额外跑一个 MiniQMT 进程（让 `xtdata.get_client()` 能连上）。

---

## 9. 错误约定

RPC 响应统一为 `{"ok": bool, "data": ..., "error": "..."}`：
- `ok=True`：`data` 为方法返回值（DataFrame/Series 已序列化，客户端自动还原）。
- `ok=False`：`error` 为错误信息。常见：
  - `rpc method is not allowed: X` —— 方法不在白名单（`rpc_listener_methods` 配置）。
  - `order rpc methods are disabled` —— 下单未开启。
  - `ContextInfo.X is not available` —— 该 ContextInfo 方法在当前 QMT 版本不存在。
  - `无法连接行情服务` —— 原生 xtdata SDK 连不上（仅 sector_list/holidays 的 SDK 路径）。
