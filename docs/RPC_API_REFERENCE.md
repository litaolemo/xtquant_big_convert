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
| 行情/K线/基本面（转发适配器）| 84 | 见下表 |
| 账户/持仓/委托 | 6 | `get_asset` / `get_positions` / `query_stock_position` / `query_orders` / `query_trades` / `describe_trade_detail_fields` |
| 运维 | 2 | `reload_deployment` / `reload_status` |
| 交易扩展查询（官方函数）| 13 | `get_value_by_order_id` / `get_last_order_id` / `get_ipo_data` / `get_new_purchase_limit` / `get_history_trade_detail_data` / 融资融券5个 / 期权持仓2个 / 港股通汇率 |
| 持仓同步 | 1 | `sync_positions` |
| 下单/撤单 | 2 | `submit_order` / `cancel_order`（默认关闭）|
| **合计** | **117 只读 + 2 下单 = 119** | |

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
- **实现**：默认透传 `ContextInfo.get_full_tick(code_list)`。部分完整大 QMT 版本会省略显式 `.SHO/.SZO` 合约；仅对这些缺失期权从 `get_market_data_ex(period="tick", count=1)` 取最新行补齐，股票/ETF 路径不变。
- **注意**：整市场快照（`["SH"]`）数据量大，建议配合客户端 `full_tick_cache` 降载。

### `get_instrument`
- **别名**：`get_instrument_detail` / `get_instrumentdetail`
- **参数**：`code`（str，必填）：股票代码
- **返回**：`dict`，合约详情（名称、上市日、合约乘数、最小变动价位等约 30 个字段）。
- **实现**：`ContextInfo.get_instrumentdetail(code)`。

---

## 2.5 全推行情订阅（server 推送，对齐 miniqmt `subscribe_whole_quote`）

这三个方法只管理**订阅生命周期与心跳**；行情数据本身走独立的 server→client 推送通道（zmq PUB/SUB 或 redis pub/sub，msgpack 编码、json 兜底），**不经过 RPC 响应**。多 client 订阅同一组合（`frozenset` 规范化）共享一个服务端订阅组：普通代码使用 `ContextInfo.subscribe_whole_quote`，显式 `.SHO/.SZO` 合约逐只使用 `ContextInfo.subscribe_quote(..., result_type="list")`。引用计数归零（全部退订或全部心跳超时）才退订组内全部大 QMT 句柄。详见 `docs/SUBSCRIBE_WHOLE_QUOTE_PUSH.md`。

### `subscribe_whole_quote`
- **参数**：`client_id`（str，必填，client 进程级稳定 id）、`sub_id`（str，必填，client 侧订阅号）、`codes`（list[str]，必填）：市场代码（`["SH","SZ"]`）或品种代码列表。
- **返回**：`{"combo_key": str, "topic": str, "push_endpoint": str}`。`topic` 即推送通道的过滤主题；`push_endpoint` 为 zmq PUB 地址（redis 推送时为空，client 本地推导 channel 名）。
- **语义**：首个 client 订阅该组合时建立大 QMT 订阅；同组合后续 client 共享。幂等（重复 subscribe 不重复建订阅，用于 server 重启后 client 重放恢复）。

### `unsubscribe_whole_quote`
- **参数**：`client_id`、`sub_id`（均 str，必填）。
- **返回**：`{}`。
- **语义**：移除该 `(client_id, sub_id)` 的引用；该组合最后一个 client 离开时退订大 QMT。未知 sub_id 为 no-op。

### `quote_keepalive`
- **参数**：`client_id`、`sub_id`（均 str，必填）。
- **返回**：`{}`。
- **语义**：刷新该订阅的 `last_seen`。client 每 `heartbeat_interval`（默认 3s）发送一次；server 端某 client 超过 `heartbeat_timeout_seconds`（默认 30s = 10 个心跳周期）无心跳则被 reaper 移除，组合清空后退订大 QMT。

---

## 3. 行情 / K线 / 板块 / 日历 / 下载 / 财务 / 期权 / 龙虎榜 / 资金流 / 因子

下列 84 个方法统一通过 `_handle_market_data_method` **按方法名转发给 `BigQmtMarketDataProvider` 的同名方法**，参数字典直接 `**kwargs` 展开。调用方按下方签名传参即可。客户端兼容层对常用方法有显式封装，其余用 `xtdata.call_method(name, **params)`。

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

**合成周期回落与「这一份不全」标记（#237）**

部分终端构建（实测国金 **2.0.8.0**）上，凡是走 C++ `context.get_market_data2`
的路径对 `1mon/1q/1hy/1y` 恒返回 0 行，而同一进程里 `ContextInfo.get_market_data`
给得出来。主路径（含 #219 的 11 列重试）全空且周期属于合成周期时，桥会**逐只代码**
改走 `ContextInfo.get_market_data` 取数。

它只服务 `open/high/low/close/volume/amount` 六列，签名里也没有 `fill_data`（是
`skip_paused`）。所以这种应答**不是**调用方要的那一份，桥会在每个代码的 DataFrame
外层加一个 `__bigqmt_partial__`：

| 字段 | 含义 |
|------|------|
| `reason` | `synth_period_primary_empty`（主路径空）或 `synth_rescue_only`（诊断参数强制）|
| `period` | 触发的周期 |
| `source` | 真正应答的终端函数，如 `ContextInfo.get_market_data` |
| `requested` / `served` / `missing` | 请求的列 / 实际给出的列 / **缺的列** |
| `fill_data_dropped` | `true` = 调用方的 `fill_data` 没送达终端 |
| `padding_rows_dropped` | 丢掉的 `count` 补齐行数（四价相同、量额为 0 的假 bar）|

客户端 `xtquant_compat` 把它还原到 `DataFrame.attrs["bigqmt_partial"]`，并按
（原因, 周期, 来源, 缺列）去重发一条 `warnings.warn`。旧客户端会直接忽略这个键，
应答形状不变。

**诊断参数 `synth_fallback_only`（只读）**

正常终端上主路径从不返回空，回落路径因此**够不到**，也就无法证明它还活着。
`synth_fallback_only=True` 让合成周期**跳过主路径**、直接走回落（其他周期忽略此参数；
回落取不到数据时返回诚实的空应答，绝不拿主路径的结果顶替）。它刻意不在客户端
`xtdata.get_market_data_ex` 的公开签名里——诊断走 `xtdata.call_method` / `client.call`：

```python
from xtquant import xtdata
xtdata.call_method("get_market_data_ex", field_list=[], stock_list=["600519.SH"],
                   period="1mon", count=10, synth_fallback_only=True)
```

FormulaServer 直连不认这个参数，带上它会强制回落到 RPC 桥（否则「回落通了」会
是假象——回落根本没跑）。

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

### 3.8 龙虎榜 / 股东 / 换手率 / 行业

| 方法 | 参数 | 说明 |
|------|------|------|
| `get_longhubang` | `stock_list`(list) `start_time` `end_time` `count`(int) | 龙虎榜明细（DataFrame）|
| `get_top10_share_holder` | `stock_list`(list) `data_name`("holder"/"flow_holder") `start_time` `end_time` `report_type`("report_time"/"announce_time") | 十大股东 |
| `get_holder_num` | `stock_list`(list) `start_time` `end_time` `report_type` | 股东户数 |
| `get_turnover_rate` | `stock_code`(list) `start_time` `end_time`（均 8 位 YYYYMMDD）| 区间换手率（DataFrame）|
| `get_industry` | `industry_name`(str) | 行业成分股 |
| `get_his_st_data` | `stock_code`(str) | 历史 ST 状态 |

### 3.9 期权定价 / 隐含波动率

| 方法 | 参数 | 说明 |
|------|------|------|
| `bsm_price` | `opt_type`("C"/"P") `target_price`(数值或 list) `strike_price` `risk_free` `sigma` `days` `dividend`(默认0) | B-S-M 期权定价（可批量）|
| `bsm_iv` | `opt_type` `target_price` `strike_price` `option_price` `risk_free` `days` `dividend` | 隐含波动率反推 |
| `get_option_iv` | `opt_code`(str) | 单只期权隐含波动率 |
| `get_option_detail_data` | `stockcode`(str) | 期权合约详情 |
| `get_option_undl_data` | `undl_code_ref`(str，空=全市场) | 标的下所有期权 |
| `get_option_undl` | `opt_code`(str) | 期权的标的代码 |

客户端另提供不依赖 QMT 原生 IV 返回值的本地分析层：

- `xtdata.get_option_analytics(opt_code, ...)`：用合约详情与期权/标的价格计算 IV、Delta、Gamma、Vega、Theta、Rho；默认以一次 `get_market_data_ex(field_list=["close"], count=1)` 补齐两个价格，也可显式传盘口中间价。
- `xtdata.get_option_chain_analytics(undl_code, dedate, ...)`：整条到期月份批量计算。缺价或违反无套利边界的合约带 `analytics_error`，其余合约继续返回。

这是客户端纯数学扩展，不加入 RPC 白名单，也不改变 `get_option_iv` 的原生兼容语义。利率与波动率均用小数；结果同时给出 `vega`/`rho`（每 1.00 变化）及 `vega_1pct`/`rho_1pct`（每 1 个百分点），Theta 同时给出年/日口径。

### 3.10 财务扩展 / 因子库

| 方法 | 参数 | 说明 |
|------|------|------|
| `get_raw_financial_data` | `field_list`(list) `stock_list`(list) `start_time` `end_time` `report_type` `data_type`("dict"/"frame") | 原始财务（未字段对齐）|
| `get_factor_data` | `field_list`(list) `stock_list`(list) `start_date` `end_date` | 因子库数据 |
| `get_his_index_data` | `stock_code`(str) | 历史指数权重 |

### 3.11 期货 / 合约 / 资金流

| 方法 | 参数 | 说明 |
|------|------|------|
| `get_main_contract` | `code_market`(str) | 主力合约 |
| `get_his_contract_list` | `market`(str) | 历史合约列表 |
| `get_date_location` | `date` | 日期在交易日历的位置 |
| `get_ETF_list` | `market` `stock_code` `type_list`(list) | ETF 列表 |
| `get_north_finance_change` | `period` | 北向资金流入流出 |
| `get_hkt_statistics` | `stock_code` | 港股通统计 |
| `get_hkt_details` | `stock_code` | 港股通明细 |

### 3.12 板块管理 / 基础查询

| 方法 | 参数 | 说明 |
|------|------|------|
| `create_sector` | `sector_name` `stock_list`(list) | 创建/更新自定义板块（写操作）|
| `get_stock_name` | `stock` | 股票名称（如「平安银行」）|
| `get_stock_type` | `stock` | 股票类型 |
| `get_last_close` | `stock` | 昨收价 |
| `get_last_volume` | `stock` | 昨量 |
| `get_open_date` | `stock` | 上市日期 |
| `get_contract_expire_date` | `stock` | 到期日（股票返回 99999999）|
| `get_contract_multiplier` | `stockcode` | 合约乘数 |
| `get_float_caps` | `stockcode` | 流通市值 |
| `get_total_share` | `stockcode` | 总股本 |
| `get_turn_over_rate` | `stockcode` | 换手率（单值版）|
| `get_weight_in_index` | `mtkindexcode` `stockcode` | 指数中权重 |
| `get_svol` | `stock` | | 
| `get_bvol` | `stock` | |
| `get_risk_free_rate` | `index`(int, 默认-1) | 无风险利率 |
| `get_close_price` | `market` `stock_code` `real_timetag` `period`(默认86400000) `divid_type`(默认0) | 指定时点收盘价 |

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
- **参数**：`account_id`(可选) `strategy_name`(str, 默认 `""` 返回全部) `cancelable_only`(bool)
- **返回**：`list[OrderSnapshot]`，每项含 `order_sys_id`/`user_order_id`/`stock_code`/`action`/`volume`/`traded_volume`/`status`/`price` 等。
- **实现**：`get_trade_detail_data(account, type, "ORDER", strategy)`。
- **strategy_name 陷阱（重要）**：`get_trade_detail_data` 按 `strategy_name` 过滤委托——下单时用的 strategy_name 必须和查询时一致。默认传 `""` 返回全部委托（不按 strategy_name 过滤）。如需过滤，显式传 `strategy_name`。
- **容错**：QMT 上下文未绑定时报错，降级为 `[]`。

### `query_trades`
- **参数**：`account_id`(可选) `strategy_name`(str, 默认 `""` 返回全部)
- **返回**：成交明细 `list`。
- **strategy_name 陷阱**：同 `query_orders`，默认 `""` 返回全部成交。
- **strategy_name 取值**（issue #133）：优先取 DEAL 行自己的 `m_strStrategyName`，
  取不到才回填查询过滤参数。以前只有后一半，所以不过滤查全部（默认）时
  这个字段恒为空字符串。

### `reload_deployment` / `reload_status`
- **参数**：`reload_deployment` 接 `reason`(str, 可选)；`reload_status` 无参数
- **返回**：`{"scheduled": True, "version_before": "...", "note": "..."}`；
  `reload_status` 返回 `{"ok": ..., "pending": ..., "modules_purged": N,
  "version_before": ..., "version_after": ..., "seconds": ..., "error": ""}`
- **用途**：同步了包代码之后，**不重启策略**就让它生效。做法是把所有
  `bigqmt_signal_trader.*` 从 `sys.modules` 里清掉、重新绑定策略模块在 import
  时持有的引用、再跑一次 `init()` 重建对象图。
- **只是「已排期」**：执行它要调 `reset_app()`，那会停掉正在应答这个请求的 RPC
  服务，所以回复必须先发出去。真正的重载在下一个 adjust tick 上做，
  轮询 `reload_status` 看结果。期间会有约 1 秒查询超时（服务正在重建）。
- **刷新不了的**：`bigqmt_signal_trader_strategy.py` 和 `BIGQMT_REDIS_DRYRUN.py`
  —— QMT 自己 exec 这两个文件，**模块没法 reload 自己所在的模块**。改这两个
  仍然要重启策略。
- **客户端**：`xt_trader.reload_deployment("why")` / `xt_trader.reload_status()`

### `describe_trade_detail_fields`
- **参数**：`account_id`(可选) `detail_types`(list, 默认 `["ORDER", "DEAL"]`)
- **返回**：`{"ORDER": {"rows": n, "attributes": [...], "error": ""}, "DEAL": {...}}`
- **用途**：诊断工具，不属于 MiniQMT 接口。某个字段为空时，它回答“是终端没给，
  还是桥没转发”—— 这两种从客户端看完全一样，而每次分辨都要一次
  部署 + 重启（#113、#130、#133）。
- **只返回属性名，不返回值**：委托/成交行里有价格、数量、柜台编号，
  而这条 RPC 和其他一样走公共通道。
- **客户端**：`xt_trader.describe_trade_detail_fields(account)`

---

## 4.5 官方交易查询函数（Big QMT 运行时注入）

这些函数和 `passorder` 一样由 Big QMT 进程在运行时注入全局命名空间，**不在 ContextInfo 桩里**。函数名严格按官方文档（`trading_function.html`）。无对应权限（如两融账户）时降级为空列表。

| 方法 | 参数 | 说明 |
|------|------|------|
| `get_value_by_order_id` | `order_id`（必填）| 按 order_id 查委托详情 |
| `get_last_order_id` | `account_id`(可选) | 最近委托号 |
| `get_ipo_data` | `account_id`(可选) | 新股数据 |
| `get_new_purchase_limit` | `account_id`(可选) | 新股申购额度 |
| `get_history_trade_detail_data` | `account_id`(可选) `detail_type`("DEAL"/"ORDER") `start_date` `end_date` | 历史成交明细 |
| `get_assure_contract` | `account_id`(可选) | 融资标的（担保品）合约 |
| `get_enable_short_contract` | `account_id`(可选) | 融券标的合约 |
| `get_unclosed_compacts` | `account_id`(可选) | 未平仓合约（负债）|
| `get_closed_compacts` | `account_id`(可选) | 已平仓合约 |
| `get_debt_contract` | `account_id`(可选) | 负债合约 |
| `get_option_subject_position` | `account_id`(可选) | 期权标的持仓 |
| `get_comb_option` | `account_id`(可选) | 组合期权 |
| `get_hkt_exchange_rate` | 无 | 港股通汇率 |

> **融资融券查询的正确方式**：`get_trade_detail_data(accountID, strAccountType, strDatatype, strategyName)` 有**两个轴**，别混在一起：
>
> - **`strDatatype`（查什么数据）** 只有 6 个合法值：`ACCOUNT` / `POSITION` / `POSITION_STATISTICS` / `ORDER` / `DEAL` / `TASK`。**别往这里传 `"CREDIT"`** —— 负债合约、担保标的、可融券这些要用上表的独立函数。
> - **`strAccountType`（查哪本账）** 才是填 `'CREDIT'` 的地方。官方 `strDatatype` 说明里写着 `ACCOUNT：账号对象**或信用账号对象**` —— 所以信用账户明细就是 `get_trade_detail_data(accId, 'CREDIT', 'ACCOUNT')`，返回 `CCreditAccountDetail`。`query_credit_detail` 走的正是这条（#201 之前它错调了已弃用的 `get_debt_contract`，两融账户因此恒为空）。
>
> **信用账户明细有两份，字段名还不一样**：
>
> | | RPC | 大 QMT | 对象 | 特点 |
> |---|---|---|---|---|
> | **终端缓存（默认用这条）** | `query_credit_detail` | `get_trade_detail_data(accId,'CREDIT','ACCOUNT')` | `CCreditAccountDetail`（3.14，标注「非查柜台」）| 同步、无限流、不用重启 |
> | 查柜台（备用） | `query_credit_account` | `query_credit_account` + `credit_account_callback` | `CCreditDetail`（3.15）| 异步、权威；官方建议 30s 一次；要重启策略 |
>
> **先用同步那条。** 维护者实测确认 `get_trade_detail_data('<信用账号>', 'credit', 'account', '')` 在大 QMT 里直接取得到信用账户信息，返回 list，取 `[0]`；账号类型和数据类型大小写都不敏感（`'credit'` / `'CREDIT'` 均可）。异步那条只是备用：3.14 那份官方标注「非查柜台」，是终端本地缓存，万一换一家券商的终端不填它，那就只剩查柜台这一条路。不需要的话完全不用理会 `query_credit_account`，不调它就什么都不会发生。
>
> 总负债在缓存那份叫 `m_dTotalDebit`，在柜台那份叫 `m_dTotalDebt` —— 只差一个字母，别看错。

### `query_credit_account`（备用，一般用不上）
- **什么时候才需要它**：`query_credit_detail` 在你的终端上取不到时。同步那条是主路径，实测可用；这条是为「终端不填 3.14 那份缓存」的券商准备的后路。
- **参数**：`account_id`(可选) `wait_seconds`(可选，默认 1.5，上限 10)
- **返回**：`{"rows": [...], "count": n, "query_issued": bool, "not_issued_reason": str, "fresh": bool, "stale": bool, "age_seconds": float|null, "seq": int, "error": str, "callback_bound": bool}`
- **为什么不是裸列表**：大 QMT 那边 `query_credit_account(accId, seq, ContextInfo)` 立刻返回，结果只从 `credit_account_callback` 出来。桥替你发查询、等回调、缓存结果，所以这条 RPC 本身是同步的 —— 但**空列表有好几种成因**（没绑上 / 被限流 / 发了没等到回调 / 真没数据），只回一个 `[]` 分不开。看 `query_issued`、`fresh`、`callback_bound`，别只看 `rows`。
- **限流**：官方参考 6.13 写明「只能有一个查询」「建议 30s 一次，不可频繁调用」。桥按 30 秒最小间隔挡住过密的查询，直接返回上一次的结果并标 `stale=true`，不会替你去打柜台。
- **`callback_bound=false` 有两种成因，别白重启一次**：
  - **桥自己没捕获到它。** QMT 只往被挂载的入口文件的命名空间注入全局函数，入口必须用 `capture_qmt_injected_funcs(globals())` 从策略模块的唯一名单里捕获。入口文件曾经手抄过一份名单并漏了 `query_credit_account`，桥于是拿不到它 —— 而终端明明有。**这类情况下 `global_namespace` 也会是 `False`，所以不能据此断言终端没有这个函数**（#202 就是这么误判的）。改入口文件后必须**真重启策略**，`reload_deployment()` 刷不了入口。
  - **这台终端确实没有这个函数** —— 那就只能走同步那条。

  两者从桥这边分不开。**判断方法：在大 QMT 的策略里直接写一行 `query_credit_account(accId, int(time.time()), ContextInfo)` 配 `credit_account_callback`,能打印出维持担保比例就说明终端有,问题在桥。**
  - **有这个函数但没重启策略** —— `credit_account_callback` 定义在入口文件命名空间里（QMT 只往被挂载的那个文件回调），而入口文件 `reload_deployment()` 刷不了，必须真重启。

  `probe_capabilities` 的 `global_namespace["query_credit_account"]` 分得开这两种，体检报告也会直接告诉你是哪一种。
- **客户端**：`xt_trader.query_credit_account(account, wait_seconds=None)`

> **体检工具**：`python tools/credit_api_report.py` 把上面每个两融接口都只读调一遍，导出一份可以直接贴到 issue 的报告（账号打码、金额不带原值、持仓代码不进报告）。维护者没有两融账户，两融那一片只能靠有两融账户的用户跑一次回报。

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
| 龙虎榜/股东/换手率（`get_longhubang` 等）| ✅ ContextInfo | ❌ xtdata 无 |
| 期权定价（`bsm_price`/`bsm_iv`/`get_option_iv`）| ✅ ContextInfo | ❌ xtdata 无 |
| 北向资金/港股通（`get_north_finance_change` 等）| ✅ ContextInfo | ❌ xtdata 无 |
| 基础查询（`get_stock_name`/`get_float_caps` 等）| ✅ ContextInfo | ❌ xtdata 无 |
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
