# xtquant_big_convert

[![PyPI](https://img.shields.io/pypi/v/xtquant-big-convert.svg)](https://pypi.org/project/xtquant-big-convert/)
[![Python](https://img.shields.io/pypi/pyversions/xtquant-big-convert.svg)](https://pypi.org/project/xtquant-big-convert/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

大 QMT 运行环境里的 RPC 桥接包：把大 QMT 内置 Python（行情查询、交易、持仓）封装成**可远程调用的服务**，并兼容一组 MiniQMT 方法名，让外部程序无需 XtQuantServer 权限就能驱动大 QMT。

支持 **Redis / ZMQ / MySQL / 共享内存** 四种可插拔传输，切换只需改一个配置字段。

已发布 PyPI，客户端一行安装：`pip install xtquant-big-convert`（详见下文「环境要求与依赖安装」）。

另附 [qmt-trader skill](qmt-trader/)：让 Claude Code / ZCode / Cursor 等 AI 助手通过统一 CLI（46 个子命令）直接查行情、查持仓、下单撤单，详见下文「AI 助手 Skill：qmt-trader」。

想看跑在这座桥上的完整应用长什么样，见 [bigqmt-dashboard](https://github.com/litaolemo/bigqmt_dashboard)——一个多账号持仓监控与下单面板，详见下文「基于本项目的应用」。

---

### 配置向导：`bigqmt-init`

不想手动抄两份 `.example.py`、也不想搞清楚三十来个键里哪些真的要改，直接跑：

```bash
bigqmt-init
```

或者从源码检出运行：

```bash
python -m bigqmt_signal_trader.init_config
```

问几个问题——资金账号、账号类型、传输方式（redis / zmq）、地址端口、Redis 用户名密码、是否允许远程下单、部署方式——然后把配置写出来：

| 文件 | 位置 | 作用 |
|---|---|---|
| `bigqmt_signal_trader_local_config.py` | QMT 的 python 目录 | 服务端（QMT 内） |
| `bigqmt_signal_trader_client_config.py` | 你指定的目录 | 客户端（外部程序） |
| `BIGQMT_*_ALL_IN_ONE.py` | QMT 的 python 目录 | 选了单文件部署时，配置已烘焙进去 |

服务端和客户端两份配置由同一组答案生成，**连接参数不会对不上**。

几个不问、直接定死的选项：

- **`rpc_background_threads` 恒为 `False`** —— `get_trade_detail_data` 离开主策略线程返回空，这不是可选项
- **`rpc_allow_order_methods` 默认 `False`** —— 打开前会明确提示：任何能连上这条通道的程序都可以下单
- 选了**无 redis 单文件**会自动把传输改成 zmq，不会留下一份声称用 redis 的配置

已存在的文件会先问再覆盖（`--force` 跳过询问）。

> **密码分两类。** Redis 密码是服务凭据，写进配置文件（`.example.py` 本来就是这么记的），输入时不回显。**QMT 登录密码不落盘**——`qmt_launcher` 从环境变量 `BIGQMT_LOGIN_PASSWORD` 读，这样它不会出现在 `argv` 或磁盘文件里，`bigqmt-init` 沿用这个约定。
>
> 生成的文件带账号和凭据，**不要提交到版本库**。

## 功能一览

### RPC 接口（远程可调用）

通过 RPC 可调用的大 QMT 能力（**白名单 117 个只读方法 + 2 个下单方法 + 12 个 MiniQMT 风格别名**，覆盖官方文档全部交易/查询函数）：

| 类别 | 方法 |
|------|------|
| **系统** | `ping` |
| **行情快照** | `get_ticks` / `get_full_tick`（五档盘口）|| **合约/品种** | `get_instrument` / `get_instrument_type` / `get_stock_name` / `get_stock_type` / `get_last_close` / `get_last_volume` / `get_open_date` / `get_contract_expire_date` / `get_contract_multiplier` / `get_float_caps` / `get_total_share` / `get_turn_over_rate` / `get_weight_in_index` / `get_svol` / `get_bvol` / `get_risk_free_rate` / `is_stock_type` / `get_cb_info` |
| **K线/历史** | `get_market_data` / `get_market_data_ex` / `get_local_data` / `get_close_price` / `get_index_weight` |
| **L2 行情** | `get_l2_quote` / `get_l2_order` / `get_l2_transaction` / `subscribe_l2thousand`（需 L2 权限）|
| **板块** | `get_stock_list_in_sector` / `get_sector_list`* / `get_sector_info` / `create_sector` / `add_sector` / `remove_sector` |
| **交易日历/时段** | `get_trading_dates` / `get_holidays`* / `get_markets`* / `get_market_last_trade_date`* / `get_date_location` / `get_trading_calendar` / `get_trade_times` |
| **数据下载** | `download_history_data` / `download_history_data2` / `download_holiday_data` / `download_etf_info` / `download_cb_data` / `download_history_contracts` / `download_index_weight` / `download_sector_data` |
| **财务/因子** | `get_financial_data` / `download_financial_data` / `download_financial_data2` / `get_raw_financial_data` / `get_factor_data` |
| **ETF/期权/期货** | `get_etf_info` / `get_ipo_info` / `get_option_list` / `get_his_option_list` / `get_his_option_list_batch` / `get_option_detail_data` / `get_option_undl_data` / `get_option_undl` / `get_ETF_list` / `get_main_contract` / `get_his_contract_list` |
| **期权定价** | `bsm_price` / `bsm_iv` / `get_option_iv` |
| **龙虎榜/股东** | `get_longhubang` / `get_top10_share_holder` / `get_holder_num` / `get_turnover_rate`（区间换手率）/ `get_industry` / `get_his_st_data` / `get_his_index_data` |
| **资金流** | `get_north_finance_change`（北向）/ `get_hkt_statistics`（港股通）/ `get_hkt_details` / `get_hkt_exchange_rate` |
| **因子/模型** | `call_formula` / `subscribe_formula` / `unsubscribe_formula` / `get_formula_result` / `gen_factor_index` |
| **时间转换** | `datetime_to_timetag` / `timetag_to_datetime` / `timetagToDateTime`（纯本地计算）|
| **账户查询** | `get_asset`（资金）/ `get_positions`（持仓）/ `query_stock_position`（单股持仓）/ `query_orders`（委托）/ `query_trades`（成交）/ `get_history_trade_detail_data`（历史成交）/ `get_value_by_order_id` / `get_last_order_id` |
| **新股/打新** | `get_ipo_data`（返回以申购代码为键的 dict）/ `get_new_purchase_limit`；客户端另有 `query_ipo_data` / `ipo_subscribe` / `ipo_subscribe_all`，见下文 |
| **融资融券** | `get_assure_contract`（担保品）/ `get_enable_short_contract`（融券标的）/ `get_unclosed_compacts`（未平仓）/ `get_closed_compacts`（已平仓）/ `get_debt_contract`（负债）—— 需两融权限，普通账户降级为空 |
| **期权持仓** | `get_option_subject_position`（标的持仓）/ `get_comb_option`（组合期权）|
| **持仓同步** | `sync_positions`（写回 Redis 供客户端缓存）|
| **下单/撤单** | `submit_order` / `cancel_order`（默认关闭，需显式开启）|

> 客户端兼容层 `BigQmtXtData` 对常用方法有显式封装（`xtdata.get_longhubang(...)`、`xtdata.bsm_price(...)` 等），其余通过万能入口 `xtdata.call_method("get_float_caps", stockcode="000001.SZ")` 调用。

> `*` 标记的方法在大 QMT（完整交易端）环境下用 **fallback** 实现（非原生数据）：`get_sector_list` 返回常用板块名清单，`get_holidays` 从交易日历反推，`get_markets` 返回固定市场集合，`get_market_last_trade_date` 从日历派生。详见 [docs/RPC_API_REFERENCE.md](docs/RPC_API_REFERENCE.md) 第 8 节「大 QMT 环境的能力边界」。

### 客户端兼容层

- `bigqmt_signal_trader.xtquant_compat`：把旧代码的 `xt_trader` / `xtdata` 调用转成 RPC，无需改业务代码。
- 兼容 MiniQMT 方法名：`query_stock_asset` / `query_stock_positions` / `query_stock_orders` / `get_full_tick` / `order_stock` 等。
- 顶层 `xtquant.xtdata.get_stock_type(stock)` 显式转发到 RPC；完整 QMT 的交易日 ContextInfo fallback 会把 `SH/SZ` 转成代表指数代码。委托快照增量暴露 `price_type` / `traded_price`，旧 QMT 不提供时分别保持 `None` / `0.0`。
- **完整 xtconstant 枚举**（539 个常量，涵盖原生 MiniQMT 全部 90 个，值逐一比对无改动）：账号类型、委托类型（股票/期货/信用/期权）、报价类型、委托状态、账号状态、`ORDER_TYPE_SET`。

```python
# 旧代码零改动（自动命中 shim）
from xtquant.xtconstant import STOCK_BUY, FIX_PRICE, ORDER_SUCCEEDED

# 或直接从 compat 导入
from bigqmt_signal_trader.xtquant_compat import (
    SECURITY_ACCOUNT, STOCK_BUY, FIX_PRICE, CREDIT_FIN_BUY,
    FUTURE_OPEN, ACCOUNT_STATUS_OK, ORDER_SUCCEEDED,
)
```

### 异步回报回调（MiniQMT 风格，实盘验证）

客户端注册 `XtQuantTraderCallback` 子类，`connect()`/`subscribe()` 后实时接收委托/成交/错误回报（通过 Redis pubsub 推送）：

```python
from bigqmt_signal_trader.xtquant_compat import (
    StockAccount, XtQuantTraderCallback, configure, xt_trader,
)

class MyCallback(XtQuantTraderCallback):
    def on_stock_order(self, order):
        print("委托回报:", order.stock_code, order.order_status, order.order_sysid)

    def on_stock_trade(self, trade):
        print("成交回报:", trade.stock_code, trade.order_id, trade.traded_volume, trade.traded_price)

    def on_order_error(self, order_error):
        print("委托失败:", order_error.order_id, order_error.error_id, order_error.error_msg)

    def on_cancel_error(self, cancel_error):
        print("撤单失败:", cancel_error.order_id, cancel_error.error_id, cancel_error.error_msg)

    def on_order_stock_async_response(self, response):
        print("异步下单回报:", response.account_id, response.order_id, response.seq)

    def on_account_status(self, status):
        print("账户状态:", status.account_id, status.account_type, status.status)

configure()
xt_trader.register_callback(MyCallback())
acc = StockAccount(xt_trader.client.account_id, "STOCK")
xt_trader.connect()
xt_trader.subscribe(acc)

# 异步下单（返回 seq，回报走回调）
seq = xt_trader.order_stock_async(acc, "600654.SH", 23, 100, 11, 2.95, "rpc_test", "备注")
```

**完整的回调链**（对齐 MiniQMT 原生语义，实盘验证）：

| 回调 | 触发时机 | 已验证 |
|------|---------|--------|
| `on_account_status` | `connect()`/`subscribe()` 后 | ✅ |
| `on_order_stock_async_response(seq, resp)` | 异步下单提交成功 | ✅（实盘）|
| `on_stock_order(order)` | 委托状态变化（已报 50 / 已成 56 / 废单 57）| ✅（实盘）|
| `on_stock_trade(trade)` | 成交回报 | ✅ |
| `on_order_error(err)` | 废单/拒单（服务端检测 status=57 推送）| ✅（实盘）|
| `on_cancel_error(err)` | 撤单失败 | ✅ |
| `on_cancel_order_stock_async_response` | 异步撤单回报 | ✅ |

**异步下单的事件顺序**（Issue #51）：`on_order_stock_async_response`（异步下单工作线程）与 `on_stock_order` / `on_stock_trade`（Redis pub/sub 监听线程）走不同通道，服务端在 `order_callback` 里先推事件、后回 RPC，事件先于响应是**常态**而非偶发竞态。客户端按 `order_remark` 设屏障：命中待响应委托的事件先暂存，response（或 error）触发后按到达顺序放行；屏障 10 秒超时兜底——丢事件比顺序错乱更糟。延迟只加在 `order_stock_async` 路径上：手工下单、同步下单、无 remark 的委托一律直通。成交事件可能没有 remark，此时按委托事件学到的 `order_sys_id` 关联。`order_remark` 不强制唯一（网格类策略常复用）：同 remark 的后一笔下单会接管前一笔的屏障并先放行其暂存事件，response 按 seq 精确匹配，前一笔的 response 不会误放后一笔的屏障。已验证：单测（含反向验证）+ 盘后真实 Redis 注入实测（部署环境保序成立）。

**`*_async` 查询方法**（对齐 MiniQMT 签名，callback 可选）：

```python
# 方式 1：callback 接收结果（MiniQMT 原生语义，返回 None）
xt_trader.query_stock_asset_async(acc, lambda asset: print(asset.cash, asset.total_asset))
xt_trader.query_stock_positions_async(acc, lambda positions: print(len(positions)))

# 方式 2：不传 callback，返回 seq（我们的扩展）
seq = xt_trader.query_stock_orders_async(acc)
```

**注意**：QMT 必须运行在**实盘模式**（非模拟/模型交易）才能收到完整回报。模拟模式下委托进 QMT 界面但不在真实委托队列，`query_orders` 查不到、`order_stock` 返回 -1（触发 `on_order_error`）。

### 新股申购（打新）

```python
from bigqmt_signal_trader.xtquant_compat import xt_trader, StockAccount
acc = StockAccount("你的账号")

# 1) 看今天有什么可申购（只读）
for code, info in xt_trader.query_ipo_data(acc, stock_type="STOCK").items():
    print(code, info["name"], info["issuePrice"], info["maxPurchaseNum"])
# 301689.SZ  某某科技  16.0  12000

# 2) 先看计划，不下单
for row in xt_trader.ipo_subscribe_all(acc, dry_run=True):
    print(row)
# {'stock_code': '301689.SZ', 'action': 'planned', 'volume': 12000, 'price': 16.0, ...}

# 3) 真申购（沪深；北交所默认排除）
results = xt_trader.ipo_subscribe_all(acc)
```

每只返回 `action`（`subscribed` / `planned` / `skipped` / `failed`）与 `reason`，一只失败不影响其余。

**这是一个你主动调用的方法，不是桥自己会做的事。** 它会下真实委托，所以必须是当天有人明确要求，而不是升级后自动发生。要每天定时打新，请在你自己的程序里调度它。

因为走的是既有的 `order_stock` 通道，它自动获得：

| | |
|---|---|
| `rpc_allow_order_methods` | 和其他委托一样保持 opt-in，默认关 |
| `orderType 1101` / `prType 11` / `quickTrade 2` | 网关默认值 —— `quickTrade` 必须是 2，见 API 参考 1.4：定时器/回调中下单传 1 可能静默不发出 |
| 主线程执行、委托记账、exec 事件 | 与普通下单完全一致 |

**默认只打沪深。** 沪深打新是市值申购、不冻结资金；北交所需要冻结资金，因此默认排除。需要时显式打开：

```python
xt_trader.ipo_subscribe_all(acc, markets=("SH", "SZ", "BJ"))
```

**申购代码无法识别时会跳过，不会猜。** 申购代码有自己的编号（沪 `730/732/780/787/789`，深 `00/30`，北 `920/889/8/4`），认不出来的代码一律跳过——在一个会下单的路径上，猜错的代价不对称。

`query_new_purchase_limit(acc)` 返回各板块申购额度（dict）。

> 实盘验证到 `dry_run` 为止：`query_ipo_data` 与申购计划均已在大 QMT 上验证正确（2026-08-28，301689.SZ @ 16.0 × 12000）。**真实申购会下真实委托，未在本仓库验证过。**

### 全推行情订阅（subscribe_whole_quote 真推送）

`subscribe_whole_quote` 是**服务端真推送**——对齐 MiniQMT 全推行情订阅。服务端引用计数管理 `ContextInfo.subscribe_whole_quote` 回调，通过独立 PUB/SUB 通道向客户端**增量推送**行情（不是一次性快照）：

**架构（三通道）**：
1. **控制面 RPC**——`subscribe_whole_quote` / `unsubscribe_whole_quote` / `quote_keepalive` 方法（复用现有 transport）
2. **数据面推送**——`QuotePushChannel` 单向 PUB/SUB（redis pub/sub 或 zmq PUB/SUB，按部署 transport 选择；msgpack 编码 + json 兜底）
3. **Big-QMT 行情源**——`QuoteSubscriptionManager` 按组合键归一化共享（大写/去空格/排序），多客户端共享一个底层订阅

**关键设计**：
- **组合键去重**：不同客户端订阅相同标的组合，只占一个 big-QMT 订阅
- **引用计数**：按 `(client_id, sub_id)` 计数，全部退订或 30s keepalive 超时才销毁
- **客户端心跳**：周期 `quote_keepalive`；检测推送静默（默认 10 轮心跳）自动重放订阅，**服务端重启后自动恢复**
- **初始快照**：客户端用 `get_full_tick` 预拉快照（big-QMT 回调是增量的）

**用法**：

```python
from bigqmt_signal_trader.xtquant_compat import configure, xtdata

configure()

# 订阅全推行情（callback 收到增量推送）
def on_quote(data):
    for code, tick in data.items():
        print(code, tick.get("lastPrice"))

seq = xtdata.subscribe_whole_quote(["600000.SH", "000001.SZ"], callback=on_quote)

# 退订
xtdata.unsubscribe_quote(seq)
```

**验证**：实盘交易日验证 1/20/50/100 只标的，3s 推送节奏稳定，零丢失零乱序；多客户端共享/退订隔离/同客户端多 sub_id 全过；服务端重启恢复（42s 中断后验证两次）。详见 [docs/SUBSCRIBE_WHOLE_QUOTE_PUSH.md](docs/SUBSCRIBE_WHOLE_QUOTE_PUSH.md) 和 [docs/SUBSCRIBE_WHOLE_QUOTE_LIVE_VERIFICATION.md](docs/SUBSCRIBE_WHOLE_QUOTE_LIVE_VERIFICATION.md)。

### 全市场快照的品种过滤（`types`）

**市场令牌返回的是交易所挂牌的全部标的，股票只占一小部分。** 实测上交所 `"SH"` 共 **26744** 个标的，其中股票 **2315 只（8.7%）**，其余是债券（36%）、回购等。QMT 的耗时严格线性、约 **0.29ms/只**，所以全量要 7.4s，只取股票 0.9s。

从 0.2.15 起 **默认只取股票**：

```python
xtdata.get_full_tick(["SH"])                      # 2315 只   1.08s   ← 新默认
xtdata.get_full_tick(["SH"], types=["all"])       # 26744 只  7.39s   ← 旧行为
xtdata.get_full_tick(["SH", "SZ"])                # 5216 只   1.66s
xtdata.get_full_tick(["SH"], types=["stock","etf"])
xtdata.get_full_tick(["600000.SH"])               # 显式代码不受影响
```

> **这是破坏性变更。** 如果你的代码依赖 `get_full_tick(["SH"])` 返回债券 / 回购 / ETF，请显式传 `types=["all"]`。收窄发生时会打印一次提示，便于发现：
>
> ```
> [bigqmt_market] SH narrowed to 2315 stock; pass types=['all'] for every
> instrument the exchange lists
> ```

| `types` | 板块 | 约数 |
|---|---|---|
| `stock`（默认） | 上证A股 / 深证A股 / 京市A股 | 2315 / 2901 / 339 |
| `etf` | 沪深ETF | 1696 |
| `fund` | 沪深基金 | 2249 |
| `index` | 沪深指数 | 609 |
| `convertible` | 沪深转债 | 320 |
| `all` | 不收窄，返回交易所全部标的 | 26744（SH） |

**关键在于请求时就收窄，而不是拿回来再过滤**——事后过滤仍要付 QMT 对每个多余标的的 0.29ms。板块清单由 FormulaServer 直连提供（实测 13ms）并按运行缓存，相对省下的时间可以忽略。

**收窄失败时退回全量，不返回空**：板块查不到、类型不认识、该市场没有对应板块（如 `HK`），都保留市场令牌照旧请求。丢行情比慢更糟。

**显式传超大代码列表会超时**：26744 个代码显式传入会打爆单次 RPC 超时，而市场令牌可以。要全量请用令牌 + `types=["all"]`。

### `get_market_data_ex` 的 `field_list` 与速度

不传 `field_list` 表示"要全部字段"，返回 11 列，**只能走 RPC**：

```
field_list=[]                      0.97s   11 列（含 preClose / suspendFlag 等）
field_list=[open,high,low,close,volume,amount]
                                   0.03s    6 列   ← FormulaServer 直连，约 30 倍
```

**这不是可以自动优化掉的差距。** FormulaServer 只供那 6 列，其余 4 列返回 `NaN`，而 RPC 有真实值（实测 `preClose` 9.07 / 7.82 / 11.59，直连全部为 `nan`）。把默认路由到直连会静默把真实价格换成 `NaN`，所以默认保持走 RPC。

**只要 OHLCV 就显式写出来**，那 30 倍就到手了。首次不传 `field_list` 时会在 `bigqmt.log` 记一条说明。

### 启动预热与卡顿监控

**重启策略后，第一次调用 `get_financial_data` 可能要几分钟。** 实测过一次 **346 秒**——当时 QMT 自身完全健康（全推行情每几秒一批、线程池正常），主策略线程也空闲（adjust 每 10 秒 100 拍，每拍 < 2ms）。当天之后的所有调用都在 1 秒内，**包括从没查过的票和没查过的表**，所以这是一次性代价，不是按代码的缓存未命中。

问题在于它的传染性：**RPC 处理是串行的**，一个调用卡住，后面排队的全部超时。客户端看到的是一片超时，和「桥死了」完全一样。

#### 启动时自动预热（默认开启）

启动后会在**后台线程**上先跑一次这个调用，把这份等待提前付掉：

```
[bigqmt_warmup] get_financial_data: first call after a restart can take
minutes; running it now so a caller does not have to wait
[bigqmt_warmup] get_financial_data warm after 346.0s -- that wait is now paid
```

热了之后就是这样：

```
[bigqmt_warmup] get_financial_data warm in 0.31s
```

**预热不会让这个代价变便宜**，它只是把代价挪到一个确定的时刻、一个没人等待的线程上，并且留下一行说明——而不是让它以「第一个调用方莫名卡死」的形式出现。

> **为什么不放在 init 里？** 启动诊断（`_diag_startup`）跑在主线程的 `init()` 中。把一个可能 346 秒的调用加进去，会在 adjust 定时器都还没排上的时候冻住整个启动——比原问题更糟。所以预热走独立守护线程，`init()` 立即返回。

关掉它（服务端 local config）：

```python
BIGQMT_REDIS_CONFIG = {
    # ...
    "warm_context_data": False,
}
```

#### 卡顿监控：区分「桥卡住」和「桥死了」

handler 还在跑的时候就会报，不用等它结束：

```
[bigqmt_rpc] zmq handler STILL RUNNING method=get_financial_data 40s
thread=bigqmt-zmq-rpc queued=3 -- the bridge is blocked, not dead
```

- 默认 **20 秒**触发。比实测最慢的健康调用（整市场快照 7.7s）长得多，又短于客户端 30 秒的默认超时，**所以日志会在调用方放弃之前就点名**
- 指数退避，一次长阻塞不会把它自己要解释的日志淹掉
- 调整或关闭（注意它在 **`zmq` 子块**里，不是顶层）：

```python
BIGQMT_REDIS_CONFIG = {
    # ...
    "zmq": {
        "stall_warn_seconds": 45,   # 0 = 关闭
    },
}
```

**看到成片超时时，先在服务端日志里搜 `STILL RUNNING` 或 `slow handler`。** 有这两行之一，就说明桥没死，只是被一个慢调用堵住了——等它跑完，或者查那个方法。

> 注意 `slow handler` 是**事后**打的（handler 返回才计时），`STILL RUNNING` 才是进行中的。


### 版本检测与部署同步

部署到 QMT 是**文件拷贝**，而 QMT 跨策略重跑保留 `sys.modules`。所以「忘了拷」和「拷了但没被加载」从外部看**一模一样**——这是本项目最容易浪费时间的一类问题：本地修好了，实盘却像没修。

**启动时会打印实际加载的版本和目录：**

```
[bigqmt_shell] bigqmt_signal_trader 0.2.15 loaded from D:\...\python\bigqmt_signal_trader
```

**客户端可以直接问：**

```python
xtdata.get_deployment_info()
# {'version': '0.2.15',
#  'package_dir':    'D:\...\python\bigqmt_signal_trader',
#  'qmt_python_dir': 'D:\...\python',
#  'strategy_dir':   'D:\...\python',
#  'python_version': '3.6.8'}
```

**版本不一致时，连接会告警：**

```
[WARNING] version mismatch: this client is 0.2.15, the QMT-side bridge is 0.2.9.
A copy alone does not take effect -- QMT keeps modules across strategy re-runs,
so the strategy must be restarted too. Set BIGQMT_AUTO_SYNC=1 (or call
xt_trader.sync_deployment()) to push this client's package into the QMT python
directory.
```

#### 同步

```python
xt_trader.sync_deployment(dry_run=True)   # 先看会动哪些文件
xt_trader.sync_deployment()               # 真同步
```

目标目录来自 `get_deployment_info()`，**不必硬编码路径**。

设环境变量 `BIGQMT_AUTO_SYNC=1` 后，连接时检测到版本不一致会自动同步。**默认关闭**——往实盘终端写文件不该是"连接"的副作用，源码树里若有半成品会直接进实盘。

| 行为 | 说明 |
|---|---|
| **绝不写入配置文件** | `bigqmt_signal_trader_local_config.py` / `bigqmt_signal_trader_client_config.py` 存账号和凭据；对应的 `.example.py` 属文档，会更新 |
| **不新增顶层文件** | 只刷新部署里已有的模块，加上策略入口（全新部署需要它）。否则 QMT 目录会变得没人说得清 |
| **覆盖前备份** | 每个被覆盖的文件留 `.bak_<时间戳>` |
| **原子写入** | 先写临时文件再替换，中断不会留下半个模块 |

> **同步之后仍然必须手动重启策略。** QMT 跨重跑保留 `sys.modules`，拷贝本身不生效——每次同步结果都带 `restart_required` 并在日志里提示。

**同步逻辑跑在客户端，不在 QMT 里。** 让交易进程盘中改写自己的代码，等于把源码树里的任何东西（包括改到一半的）直接送上实盘。

### 可插拔传输层

| 传输 | 同机 p50 | 跨机 | 适用场景 |
|------|---------|------|---------|
| **redis**（默认）| ~13ms | ✅ | 生产默认，稳定 |
| **zmq** | ~0.7ms* | ✅ | 同机低延迟 |
| **mysql** | ~105ms | ✅ | 兼容兜底 |
| **shm** | — | ❌ | 接口预留（未实现）|

*zmq fast-path；约 30% 请求会撞 QMT 的 GIL 调度尖峰（~500ms）。

### FormulaServer 直连快速路径（只读行情，默认开启）

大 QMT 的 `58600` 端口是 **FormulaServer**——QMT 内置的 C++ 行情/参考数据服务（端口取自
`config/formulaserver/formulaserver.ini` 的 `[server_formula] address`）。QMT 自带 Python
的 `qmt_api` 包就是它的客户端。

客户端对这些方法会**绕开整条 RPC 链路**（不经过 QMT 的 python 策略线程，也不抢 GIL），
实测 **p50 0.07ms**，穿过完整客户端栈是 **0.145ms/次**：

| 对比 | p50 |
|------|-----|
| redis RPC | ~13ms |
| zmq RPC | ~0.7ms（30% 撞 500ms GIL 尖峰）|
| **FormulaServer 直连** | **0.07ms**（无 GIL 竞争）|

直连覆盖 10 个方法：`get_instrument` / `get_instrument_detail` / `get_instrumentdetail` /
`get_last_volume` / `get_total_share` / `get_contract_multiplier` / `get_main_contract` /
`get_weight_in_index` / `get_stock_list_in_sector` / `get_market_data_ex`。

**能力边界（重要）**：FormulaServer 只有行情/参考数据。所有账户、持仓、委托、成交、下单
方法一律返回 `ErrorID 200005 未找到该服务`，`getFullTick`/`getQuote` 也不存在。所以它是
**只读快速路径，不是 RPC 桥的替代品**——交易、账户查询、五档盘口仍然走 RPC。

以下方法**刻意不走**直连，因为参数语义与我们的调用方不一致，宁慢勿错：

- `get_trading_dates` —— FormulaServer 要**股票代码**（`000001.SZ`），传市场代码（`SH`）静默返回 `[]`，而我们的调用方传的是市场。
- `get_divid_factors` / `get_risk_free_rate` —— 参数语义不同（区间 vs 单日、index vs timetag）。
- **复权 K 线** —— 实测 `dividendType` 传 `none` 和 `front` 返回完全相同，复权未生效。因此只有
  `dividend_type="none"` 才走直连，其余回退 RPC，避免静默返回未复权价格。
  （复权数据还需**先在服务端下载原始数据**，见下文「复权数据下载陷阱」。）

配置（客户端侧，默认就是开启，通常不用写）：

```python
BIGQMT_REDIS_CONFIG = {
    "formula_server": {
        "enabled": True,              # 或环境变量 BIGQMT_FORMULA_ENABLED=0 关闭
        # "host": "127.0.0.1",        # 默认本机；FormulaServer 绑 0.0.0.0，跨机需放行防火墙
        # "port": 58600,              # 不写则从 qmt_root 的 ini 读，再退回 58600
        # "qmt_root": r"D:\国金证券QMT交易端",
        # "timeout_seconds": 3.0,
        # "methods": ["get_instrument"],       # 只路由白名单里的方法
        # "failure_cooldown_seconds": 30.0,    # 连不上后停用多久再重试
    },
}
```

**失败一律自动回退 RPC**：方法未映射、参数translate 不了、服务没起、连接断——都退回原路径，
所以连不上 58600 的客户端行为与改动前完全一致。BSON 编解码内置了无依赖实现（可选用
pymongo 的 `bson`，两者输出实测逐字节一致），客户端不需要额外装包。

### QMT 启停 / 自动重启（qmt_launcher）

大 QMT 基本每天早上要重启一次，卡点在登录框。两条路绕过它：

```bash
python -m bigqmt_signal_trader.qmt_launcher status  --dir "D:\国金证券QMT交易端_lemo"
python -m bigqmt_signal_trader.qmt_launcher restart --dir "D:\国金证券QMT交易端_lemo"
```

| mode | 做什么 | 需要登录框交互 |
|------|--------|---------------|
| `linkmini`（默认优先）| `XtMiniQmt.exe linkMini`，MiniQMT 免密启动 | 否 |
| `bat` | 跑指定批处理（如 `免密登录qmt.bat`）| 否 |
| `exe` | 直接起 `XtItClient.exe`，靠终端自身恢复会话 | 否 |
| `login` | 起 exe 后向登录框输入账号密码 | 是，需 pywin32 |

**`login` 模式需要未锁屏的交互式桌面。** 它用的是 `keybd_event` / `mouse_event`
物理输入（经 ctypes），不是 `SendMessage`——消息式输入投不到 Qt 对话框的焦点控件上，
当别的窗口在前台时会静默失败，什么也不输入。物理输入要求对话框在最前，所以启动前会
先把它置顶并核验；锁屏或 RDP 注销的会话直接抛 `QmtLauncherError` 而不是打一半密码。

> 需要**无人值守定时重启**的话，用 `linkmini` / `bat` / `exe` 三种模式，它们不碰登录框，
> 锁屏也能跑。只有 `login` 受这条限制。

密码从环境变量 `BIGQMT_LOGIN_USER` / `BIGQMT_LOGIN_PASSWORD` 读，不走命令行参数——argv
对同机任何进程可见。

两个设计要点：

- **按安装目录隔离**。同机常并行跑多个 QMT，`taskkill /im XtItClient.exe` 会误杀别人的
  实盘。这里只终结 `--dir` 对应 `bin.x64` 下的进程；拿不到 exe 路径的进程直接跳过而不是
  猜。
- **等就绪而不是 sleep 固定秒数**。启动完成的判据是 FormulaServer 端口（58600）能接受连接，
  超时抛 `QmtLauncherError` 而不是静默返回，避免定时任务在没起来的终端上继续跑。

`restart` 默认在关闭后等 5 秒再启动：ZMQ 传输是精确绑定配置端口（不扫描），socket 没
完全释放就重启会绑定失败。

### 独立 ZMQ 回测桥接

`bigqmt_backtest` 与实盘 RPC 桥接完全分离，提供两个明确隔离的后端：

- `QMT_NATIVE`：`BIGQMT_ZMQ_BACKTEST.py` 运行在 QMT 回测进程内。QMT 负责历史
  行情推进、资金持仓、`passorder/cancel` 和原生撮合；ZMQ 只桥接 Bar、订单意图及
  QMT 委托/成交结果。
- `LOCAL_SIM`：端口 `16661` 的独立 CSV 工具，仅用于脱离 QMT 验证协议和策略逻辑，
  使用本地撮合并输出本地结果文件。

QMT 原生入口使用独立端口 `16662`、独立 `run_id/client_id`，强制验证
`ContextInfo.do_back_test=true`，固定 `live_ready=false`，不会导入或修改
`bigqmt_signal_trader`。

启动 CSV 独立测试服务：

```powershell
python -m pip install -e .
python -m bigqmt_backtest.server `
  --data examples/backtest_bars.example.csv `
  --config examples/backtest_config.example.json `
  --run-id demo-001 `
  --bind tcp://127.0.0.1:16661
```

另开一个终端运行外部策略：

```powershell
python examples/zmq_backtest_strategy.py `
  --endpoint tcp://127.0.0.1:16661 `
  --run-id demo-001 `
  --symbol 600000.SH `
  --fast 2 `
  --slow 3
```

QMT 原生安装、逐 Bar 同步协议、CSV 备用模式和安全边界见
[docs/ZMQ_BACKTEST_BRIDGE.md](docs/ZMQ_BACKTEST_BRIDGE.md)。

### 无 redis 版本（QMT 沙箱拒绝 import redis 时用）

如果你的 QMT 环境**拒绝 `import redis`**（券商白名单拦截），用 `bigqmt_no_redis/` 目录下的无 redis 版本：

- `bigqmt_no_redis/zmq_transport.py` — 自包含的 ZMQ transport，内联所有编码函数，**完全不 import redis_common/redis_rpc**，去掉 redis 服务发现（用静态派生端口）
- `bigqmt_no_redis/DRYRUN_no_redis.py` — 无 redis 的 DRYRUN 入口，强制 `transport=zmq` + `background_threads=True`，只加载 zmq transport

**用法**：QMT 策略编辑器加载 `BIGQMT_DRYRUN_NO_REDIS.py`（同步到 QMT 目录时用这个文件名），RPC 走纯 ZMQ，零 redis 依赖。其余功能（行情/交易/持仓查询）与标准版一致。

### 单文件构建（QMT 沙箱禁止加载外部文件时用）

部分券商的 QMT 更严：**白名单 + 不能加载文件、不能 import 外部模块**，只有把所有代码放进**一个策略文件**才能跑（Issue #56）。`tools/` 下两个生成器负责把整个包打成一个自包含文件：

```bash
python tools/build_single_file.py
python tools/build_no_redis_single_file_flat.py
```

| 生成器 | 产物 | 内嵌方式 | 用于 |
|---|---|---|---|
| `build_single_file.py` | `src/BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py` | base64 | redis / zmq 均可 |
| `build_no_redis_single_file_flat.py` | `src/BIGQMT_DRYRUN_NO_REDIS_FLAT_ALL_IN_ONE.py` | **明文真实代码** | 沙箱拒绝 `import redis` 时，强制 ZMQ |

两者都内嵌 `bigqmt_signal_trader` 全部子模块 + `bigqmt_signal_trader_strategy` + `bigqmt_signal_trader_redis_rpc_runtime`，运行时用自定义 import 钩子从内存解析，**不从磁盘 import 任何自定义模块**；只依赖标准库和第三方库（redis / zmq / pandas）。

**flat 版**把每个模块缩进进 `def _mod_N():` 函数体、再用其 `__code__` 在独立模块命名空间里 exec，所以内嵌源码在生成文件里**可搜索、可阅读、可直接改**，IDE 也能高亮跳转。它处理了两个坑：用 tokenize 保护多行字符串内部不被缩进改动；用 AST 收集模块级绑定名并在函数体开头注入 `global`，否则被嵌套函数闭包引用的模块级名字会变成 cell 变量，与 `global` 更新的模块 dict 失去同步。

> 函数体 exec 也正是 `from X import *` 变成 `SyntaxError: import * only allowed at module level` 的原因（Issue #76）。整个包因此不允许出现星号导入，`tests/test_single_file_build.py` 会守住这条。

**用法**：编辑生成文件顶部的 config block（`BIGQMT_ACCOUNT_ID` / `BIGQMT_ACCOUNT_TYPE` / `BIGQMT_REDIS_CONFIG`），把这**一个文件**拷进 QMT 的 python 目录当策略加载即可，不需要一并拷贝整个包。默认值与 `src/bigqmt_signal_trader_local_config.example.py` 保持一致——**`rpc_allow_order_methods` 默认为 `False`**，需要远程下单/撤单时才显式打开。

产物约 900KB / 700KB，已加入 `.gitignore`——**用时重新生成，不要提交**。改动包内代码后需重新运行生成器。

感谢 @heimo88 提供这两个脚本并在其券商环境实测。

### 委托/成交查询的 strategy_name 陷阱（重要）

`get_trade_detail_data` 按 `strategy_name` 过滤委托/成交——**下单时用的 strategy_name 必须和查询时一致**，否则查不到。

- 下单时传 `strategy_name="rpc_test"` → 委托记在 `rpc_test` 下
- 查询时传 `strategy_name="bigqmt_signal_trader"` → 返回空（不匹配）

**修复**：`query_orders` / `query_trades` 默认传**空字符串 `""`**，返回该账户的**全部**委托/成交（不按 strategy_name 过滤）。如需过滤，显式传 `strategy_name`。

实测验证（`get_trade_detail_data` 探测）：
- `st=""` → ORDER=9, DEAL=9（全部）
- `st="rpc_test"` → ORDER=3, DEAL=1（只有 rpc_test 的）
- `st="bigqmt_signal_trader"` → ORDER=0, DEAL=0（空）

### 复权数据下载陷阱（重要）

**前/后复权 K 线必须先在服务端下载原始数据，否则返回全 0**。

Big QMT 的复权（`dividend_type='front'`/`'back'`）是**服务端现场计算**的——需要原始 K 线 + 除权因子已经在服务端存在。直接请求 front 而服务端没下载过原始数据时，返回的 close 全是 `0.0`（只有最后一根有价）。

实测复现（600654.SH / 600227.SH）：
- 直接 `get_market_data_ex(dividend_type='front')` → 634 行全 0
- 先 `download_history_data` 后再请求 → 真实复权价（front ≠ none，复权生效）

**已修复**：`xtdata.download_history_data2(codes, period, dividend_type='front')` 现在会**自动先触发服务端原始数据下载**（拉原始 K 线 + 除权因子），再拉复权数据到本地缓存。用法不变：

```python
# 前复权下载（自动先服务端下载原始数据 + 除权因子）
xtdata.download_history_data2(["600654.SH"], period="1d",
                               start_time="20240101", dividend_type="front")

# 之后本地读取（零 RPC）
xtdata.get_local_data(["close"], ["600654.SH"], period="1d",
                      start_time="20240101", dividend_type="front")
```

**读取类 API 也自愈**：`get_market_data_ex` / `get_market_data` 带复权参数时，若检测到返回全 0（服务端缺原始数据），会自动触发服务端下载、等待落盘、重试一次，拿到真实复权价。`get_local_data` 的 fallback 拉取同样受益。无需手动等待。

注意：QMT 服务端下载是**异步落盘**的，自愈路径内置了等待 + 一次重试；极端大区间若一次重试仍全 0，可稍后重读或先显式 `download_history_data2`。

### 实盘卖出方向误判修复（exec_events）

实盘发现：QMT 回调里 `m_nDirection` **恒为 48**（即使是卖出），导致卖出被误判为买入。

修复（`exec_events._extract_direction`）改为仲裁链：
1. `m_nOffsetFlag`（最可靠，匹配 `query_orders`）
2. `m_nDirection`（传统 EEntrustBS，但实盘可能恒为 48）
3. 当 direction≠offset（期货：卖+开仓=49+48），用 `m_nOpType`（23=买/24=卖）仲裁
4. `m_nOpType`/`order_type`（兜底）

对股票现货，direction=offset（48=买/49=卖）；对期货，direction≠offset，仲裁保正确。

### 多账号使用（股票+期货 / 普通+信用）

当前架构是**单账号单实例**——一个 QMT 策略进程绑定一个账号，RPC channel 按 `account_id` 隔离（`bigqmt:rpc:req:{account_id}`）。多账号场景（如股票+期货、普通+信用账户同时交易）的推荐方案是**在 QMT 里跑多个策略实例**，每个实例绑一个账号。

#### 方案：多策略实例（推荐，不改代码）

**服务端（QMT 内）**：为每个账号创建一个独立的配置文件和 DRYRUN 入口。

```python
# bigqmt_signal_trader_local_config_stock.py  — 股票账号
BIGQMT_ACCOUNT_ID = "你的股票账号"
BIGQMT_REDIS_CONFIG = {
    "host": "...", "port": 6379, "db": 5, "password": "...",
    "transport": "redis",          # 或 "zmq"
    "account_type": "STOCK",       # 股票
    # ...
}

# bigqmt_signal_trader_local_config_credit.py  — 信用账号
BIGQMT_ACCOUNT_ID = "你的信用账号"
BIGQMT_REDIS_CONFIG = {
    "host": "...", "port": 6379, "db": 5, "password": "...",
    "transport": "redis",
    "account_type": "CREDIT",      # 信用（两融）
    # ...
}
```

然后在 QMT 策略编辑器里加载两个 DRYRUN 文件（每个指向不同的配置），分别运行。两个实例的 RPC channel 自动隔离（按 account_id）。

> **zmq 模式注意**：每个实例的 zmq 端口从 account_id 派生（`15560 + account_id mod 100`），不同账号自动不冲突。

**客户端（外部程序）**：为每个账号创建独立的 client/trader 对象。

```python
from bigqmt_signal_trader.xtquant_compat import BigQmtRpcClient, BigQmtXtTrader, StockAccount

# 股票账号
stock_client = BigQmtRpcClient(account_id="股票账号", redis_config={...})
stock_trader = BigQmtXtTrader(account_id="股票账号", redis_client=stock_client.redis_client)
stock_acc = StockAccount("股票账号", "STOCK")

# 信用账号
credit_client = BigQmtRpcClient(account_id="信用账号", redis_config={...})
credit_trader = BigQmtXtTrader(account_id="信用账号", redis_client=credit_client.redis_client)
credit_acc = StockAccount("信用账号", "CREDIT")

# 分别查询/下单
stock_asset = stock_trader.query_stock_asset(stock_acc)
credit_positions = credit_trader.query_stock_positions(credit_acc)
```

> **跨账号隔离**：每个账号的 RPC channel、持仓查询、委托回报完全隔离（按 `account_id` 路由），互不影响。

---

## 与 MiniQMT 的兼容性对照

本项目的目标是让照着 MiniQMT (`xtquant`) 写的代码不改就能跑。下表列出**返回值契约**——类型不对不会报错，只会让判断悄悄反过来，所以单独列出来。

### 返回值：与 MiniQMT 一致

| 接口 | 返回 | 说明 |
|---|---|---|
| `order_stock()` | `int` | 成功为正数，失败 `-1` |
| `order_stock_async()` | `int` | 请求序号 seq，结果走 `on_order_stock_async_response` |
| `cancel_order_stock()` | `int` | **`0` 成功，`-1` 失败**（不是 True/False） |
| `cancel_order_stock_sysid()` | `int` | 同上 |
| `cancel_order_stock_async()` | `int` | seq |
| `connect()` / `start()` | `int` | `0` 成功 |
| `subscribe()` / `unsubscribe()` | `int` | `0` 成功 |
| `query_stock_asset()` | 对象 | `.cash` / `.total_asset` 等属性 |
| `query_stock_positions()` | `list[对象]` | |
| `query_stock_orders()` / `query_stock_trades()` | `list[对象]` | |
| `subscribe_quote()` / `subscribe_whole_quote()` | `int` | 订阅号，传给 `unsubscribe_quote()` |
| `get_full_tick()` | `dict` | `{code: {...}}` |
| `get_market_data_ex()` | `dict[str, DataFrame]` | |

### 订单号：既是 int 也是 str

MiniQMT 的 `order_id` 是 int（委托编号），`order_sysid` 是 str（柜台合同编号）。大 QMT **没有前者**——`get_trade_detail_data` 只给 `m_strOrderSysID` 这个字符串。

所以这里的 `order_id` 是一个 int 子类，两种形态同时成立：

```python
order_id = xt_trader.order_stock(acc, "600000.SH", 23, 100, 11, 10.0, "s", "")

isinstance(order_id, int)   # True —— MiniQMT 写法照常
order_id > 0                # True
order_id == -1              # 失败时才 True

str(order_id)               # '合同编号' —— 券商给的原始字符串
xt_trader.cancel_order_stock(acc, order_id)   # 撤单送回的是原始字符串
```

合同编号是纯数字时（多数券商），int 值就是那个数字，两种形态完全一致；不是纯数字时 int 是一个稳定的正数替身，而撤单、打印用的仍是真实编号。

把 order_id 存进数据库再取出来（变成普通 int）也能撤单——客户端记着最近 4096 个的对应关系。想要字符串就用 `.order_sysid`，它一直是 str。

同样的规则适用于 `XtOrder.order_id`、`XtTrade.order_id`，以及回调对象 `XtOrderError` / `XtCancelError` / `XtOrderResponse` 里的 `order_id`。

### 行为差异（不是返回值，但会咬人）

| 项目 | MiniQMT | 本项目 |
|---|---|---|
| `get_full_tick(["SH"])` | 全市场 | **默认只取股票**（1.08s）；要全部传 `types=["all"]`（7.4s，含地方债等 26744 只） |
| `get_instrument_detail()` 查不到 | `None` | `{}`（两者都是 falsy，`if not detail` 通用） |
| `download_history_data()` | 无返回 | 返回 `{"finished": n, "total": n}`（多给的信息，可忽略） |
| 账户类型 | `StockAccount(id, "CREDIT")` 即可 | 还需服务端 `BIGQMT_ACCOUNT_TYPE = "CREDIT"`，**客户端的类型不会传到服务端** |
| 委托类型常量 | `xtconstant.order_type` | 内部会翻译成 `passorder` 的 opType（两套编号，专项两融 40–45 → 70–75） |

### 本项目的扩展（MiniQMT 没有）

这些不是兼容项，是多出来的：`order_stock_result()`（返回完整 dict 而非单个 id）、`order_stock_batch()`、`wait_async_orders()`、`ipo_subscribe_all()`、`sync_deployment()`、`get_deployment_info()`、`query_execution_snapshot()`、`local_cache_stats()`。

---

## 环境要求与依赖安装

本系统分两部分，各自需要自己的 Python 环境和依赖：

| 部分 | 运行位置 | Python | 装什么 |
|------|---------|--------|--------|
| **客户端**（外部程序）| 你的开发机 | 3.8+（推荐）| `pip install xtquant-big-convert` |
| **服务端**（QMT 内）| QMT 的 `bin.x64/python.exe` | 3.6（QMT 自带）| 按传输装 1 个包 |

### A. 客户端（外部程序，推荐 pip 安装）

客户端就是**写策略/调接口的那台电脑**（也叫「开发机」）。直接 pip 安装：

```powershell
# 基础安装（含 pyzmq，zmq 传输必需）
pip install xtquant-big-convert

# 含 redis 支持（redis 传输）
pip install xtquant-big-convert[redis]

# 含 mysql 支持（mysql 传输）
pip install xtquant-big-convert[mysql]

# 开发环境（含测试工具）
pip install xtquant-big-convert[dev]

# 从源码安装（开发模式）
git clone https://github.com/litaolemo/xtquant_big_convert.git
cd xtquant_big_convert
pip install -e .
```

安装后可直接 import：

```python
from bigqmt_signal_trader.xtquant_compat import configure, xt_trader, xtdata
from bigqmt_signal_trader.transports.factory import build_transport

configure()
print(xtdata.get_full_tick(["000001.SZ"]))
```

### B. 服务端（QMT 内 Python 3.6）

> **前置：先在 QMT 界面里下载 Python 组件。** 全新安装的终端 `bin.x64\` 下**没有 `Lib\` 目录**，也没有 `python.exe`——那是 Python 组件带来的，不是终端自带的，**不要自己手动创建 `Lib\`**。在 QMT 客户端里下载安装该组件后，`bin.x64\Lib\site-packages\` 才会出现，下面的路径才成立。具体入口见迅投官方文档。

QMT 自带 Python 3.6（`bin.x64/python.exe`），**只需按你选的传输装对应依赖**：

| 传输 | 服务端需要的包 | 客户端需要的包 |
|------|--------------|--------------|
| **redis**（默认）| `redis`（QMT 通常已内置）| `redis` |
| **zmq** | `pyzmq` | `pyzmq`（基础安装已含）|
| **mysql** | `pymysql` + `DBUtils` | `pymysql` + `DBUtils` |

> ⚠️ **用 redis 传输就不需要装 pyzmq / pymysql / DBUtils**——下面的安装说明是按需的，你用什么传输装什么。

**安装到 QMT 的 Python（以 zmq / mysql 为例）：**

QMT 的 Python 3.6 用旧 OpenSSL，pip 直连 HTTPS 镜像会报 SSL 错误。有两种方法：

```powershell
# 方法 A：从开发机拷贝纯 Python 包（推荐，绕过 SSL 问题）
# pymysql / DBUtils 是纯 Python，可直接拷贝；在开发机（已装这些包）执行：
$QMT_SITE = "D:\国金证券QMT交易端\bin.x64\Lib\site-packages"
Copy-Item -Recurse "C:\Users\<你>\anaconda3\Lib\site-packages\pymysql" "$QMT_SITE\pymysql"
Copy-Item -Recurse "C:\Users\<你>\anaconda3\Lib\site-packages\dbutils" "$QMT_SITE\dbutils"

# 方法 B：用 QMT python pip 装（可能因 SSL 失败，需配置信任）
cd D:\国金证券QMT交易端
.\bin.x64\python.exe -m pip install --trusted-host mirrors.aliyun.com pymysql DBUtils
```

验证安装：
```powershell
.\bin.x64\python.exe -c "import pymysql; from dbutils.pooled_db import PooledDB; print('OK')"
```

> **pyzmq 特殊说明**：包含 C 扩展，不能直接拷贝。Python 3.6 需装 `pyzmq==19.0.2`（最后一个支持 3.6 的版本）。如果 SSL 装不上，可下载对应 wheel 手动 `pip install xxx.whl`。

---

## 快速开始

> 第一次部署、只想要最短路径？直接看 [docs/DEPLOY_QUICKSTART.md](docs/DEPLOY_QUICKSTART.md)（单账号五步跑通 + 常见问题表）。

> 前置：客户端已按上面「A. 客户端」装好包；服务端按「B. 服务端」装好所选传输的依赖。下面是从零跑通整套流程的步骤。
>
> 只想把配置生成出来的话，跑 [`bigqmt-init`](#配置向导bigqmt-init) 即可——第 3 步的两份配置它会替你写好，选单文件部署还会顺带把构建也做了。

### 第 1 步：同步代码到 QMT 的 python 目录

把以下内容复制到大 QMT 的 `python` 目录（如 `D:\国金证券QMT交易端\python\`）：

```
src/bigqmt_signal_trader/          （整个核心包，含 transports/）
src/bigqmt_signal_trader_strategy.py
src/bigqmt_signal_trader_redis_rpc_runtime.py
src/BIGQMT_REDIS_DRYRUN.py         （★ Redis/MySQL/SHM 等既有 transport 的 QMT 编辑器入口）
src/BIGQMT_ZMQ_DRYRUN.py           （★ 同机 ZMQ 专用入口，强制 ZMQ 并记录 bootstrap 异常）
```

> 同机 ZMQ 在 QMT“模型研究”中新建 Python 模型并加载 `BIGQMT_ZMQ_DRYRUN.py`；其它 transport 继续使用 `BIGQMT_REDIS_DRYRUN.py`。ZMQ 入口只复用原入口的加载逻辑，不会创建 Redis client。
>
> **纯 ZMQ 模式的能力边界**：入口会自动关闭所有依赖 Redis 的功能——`download_jobs`（下载任务队列）、`exec_events`（`on_stock_order`/`on_stock_trade`/`on_order_error` 推送）、`full_tick_cache`（全市场快照缓存）。即纯 ZMQ 下**没有执行回报推送**，委托状态需主动 `query_stock_orders` 轮询。行情查询、下单/撤单、持仓查询等 RPC 全部正常。

### 第 2 步：创建 QMT 端私有配置

在 QMT 的 `python` 目录创建 `bigqmt_signal_trader_local_config.py`（**不要提交此文件**）：

```python
# coding: utf-8
BIGQMT_ACCOUNT_ID = "你的资金账号"        # 如 "1234567890"

BIGQMT_REDIS_CONFIG = {
    "host": "你的Redis地址",              # 如 "192.168.1.100"
    "port": 6379,
    "db": 5,
    "password": "你的Redis密码",

    # === 传输选择（默认 redis，生产推荐）===
    # "transport": "redis",              # 不写就是 redis
    # 切 zmq（同机低延迟，实测 p50~0.3ms）：装了 pyzmq 后只需这一行。
    #   非 redis 传输会自动开 background_threads；端口按账号派生 127.0.0.1:1556x。
    # "transport": "zmq",
    # 切 mysql（兼容兜底）：需装 pymysql+DBUtils，同样自动开 background_threads。
    # "transport": "mysql",
    # "mysql": {"driver":"pymysql","host":"...","port":3306,"user":"root",
    #           "password":"...","database":"bigqmt_rpc","charset":"utf8mb4"},

    "rpc_allow_order_methods": False,    # 下单默认关闭
    "rpc_process_in_listener": True,     # 只读请求在收包线程直接处理（低延迟）
    "rpc_listener_methods": ("*",),      # * = 所有只读方法
    "rpc_background_threads": False,     # redis 用 QMT adjust 线程 drain
    "schedule_adjust": True,
    "schedule_adjust_interval": "500nMilliSecond",
}
```

> **重要**：切到 zmq 或 mysql 时，必须同时设 `"rpc_background_threads": True`（这两种传输用自己的后台线程，不走 QMT 回调 drain）。

### 第 3 步：在 QMT 里运行策略

同机 ZMQ 使用 `src/BIGQMT_ZMQ_DRYRUN.py`，其它 transport 使用 `src/BIGQMT_REDIS_DRYRUN.py`。两者都是 QMT 编辑器入口；ZMQ 入口会在正常 logger 初始化前失败时把 traceback 写入 `<QMT python>\logs\bigqmt-bootstrap-error.log`。部分券商 QMT 缺少标准 `importlib` 时，统一入口会注册仅包含 `import_module/reload` 的最小兼容模块。

#### 这个文件做什么

它是 QMT 编辑器入口的"外壳"（shell），按顺序做 5 件事：

1. **定位 python 目录**：把 QMT 的 `python` 目录加到 `sys.path`，让 `bigqmt_signal_trader` 包能 import。
2. **reload 模块**：`importlib.reload` 刷新 `redis_common` / `redis_rpc` / `strategy` / `runtime` —— QMT 在编辑器里重跑策略时，进程不退出，reload 确保新代码立即生效。
3. **注入 Redis 配置**：读 `bigqmt_signal_trader_local_config.py` 里的 `BIGQMT_REDIS_CONFIG`，调 `configure_runtime_redis()`。
4. **注入账号**：读 `BIGQMT_ACCOUNT_ID`，调 `configure_runtime_account()`。如果配置没给，fallback 用 QMT 全局变量 `account`。
5. **绑定 QMT 原生 API**：把 QMT 内置的 `passorder` / `cancel` / `get_trade_detail_data` 函数绑进 runtime（用 `try/except NameError` 包住，因为这些名字只在大 QMT 进程内存在）。
6. **导出 QMT 回调**：`init = _runtime.init` / `handlebar = _runtime.handlebar` / `adjust = _runtime.adjust` 等，让 QMT 能回调到我们的策略逻辑。

#### ⚠️ 硬编码路径（重要）

`BIGQMT_REDIS_DRYRUN.py` 里有**一处写死的 QMT python 目录路径**，作为 `__file__` 找不到时的 fallback：

```python
def _known_qmt_python_dir():
    root = "".join(chr(value) for value in (0x56fd, 0x91d1, 0x8bc1, 0x5238))   # 国金证券
    suffix = "".join(chr(value) for value in (0x4ea4, 0x6613, 0x7aef))          # 交易端
    return "D:\\" + root + "QMT" + suffix + "\\python"
    # 解码后 = D:\国金证券QMT交易端\python
```

- **`chr()` 编码**是为了规避 QMT 用 GBK 保存策略文件时中文乱码（用 Unicode 码点拼出"国金证券交易端"）。
- **路径优先级**：先用 `__file__` 所在目录（脚本实际位置），找不到才用这个硬编码 fallback。
- **如果你的 QMT 装在别的路径**（比如 `D:\华泰QMT\python`）：通常不用改，因为 `__file__` 优先。但如果你用 `exec` 方式加载（`__file__` 未定义），需要把 `_known_qmt_python_dir()` 改成你的路径，或直接硬编码：
  ```python
  def _known_qmt_python_dir():
      return r"D:\你的券商QMT\python"
  ```

#### 启动成功标志（QMT 输出面板）

```
[bigqmt_shell] reload entry paths=['D:\\国金证券QMT交易端\\python']
[bigqmt_shell] local redis config loaded keys=['host', 'port', 'db', ...]
[bigqmt_shell] local account config loaded=True
[bigqmt_rpc] transport=redis mode process_in_listener=True listener_methods=('*',) ...
[bigqmt_rpc] started channel=bigqmt:rpc:req:你的账号
[bigqmt_signal_trader] init ok
```

> **为什么是 GBK 编码？** QMT 的策略编辑器用本地代码页（中文 Windows 是 GBK）保存文件。文件头 `#coding:gbk` 声明编码，避免 QMT 保存时破坏 UTF-8 内容。源码本身是 ASCII（中文用 `chr()` 拼），所以实际不会乱码。

> **为什么不直接用 `bigqmt_signal_trader_redis_rpc_runtime.py`？** 那个文件是纯逻辑入口，不包含 reload 和 QMT API 绑定。QMT 编辑器应加载与 transport 对应的外壳：同机 ZMQ 使用 `BIGQMT_ZMQ_DRYRUN.py`，其它 transport 使用 `BIGQMT_REDIS_DRYRUN.py`；不要直接加载 runtime 文件。

### 第 4 步：客户端调用

**方式 A：用兼容层（推荐，旧代码零改动）**

客户端创建配置文件 `bigqmt_signal_trader_client_config.py`（与上面类似但用客户端视角），然后：

```python
from bigqmt_signal_trader.xtquant_compat import StockAccount, configure, xt_trader, xtdata

configure()

acc = StockAccount(xt_trader.client.account_id, "STOCK")

# 行情
ticks = xtdata.get_full_tick(["000001.SZ"])
print(ticks["000001.SZ"]["lastPrice"])

# 持仓 / 资金
positions = xt_trader.query_stock_positions(acc)
asset = xt_trader.query_stock_asset(acc)
print(asset.cash, asset.total_asset)

# K线（自动还原成 pandas DataFrame）
klines = xtdata.get_market_data_ex(
    field_list=["close"], stock_list=["000001.SZ"], period="1d", count=5
)
```

**方式 B：直接 RPC 调用**

```python
from bigqmt_signal_trader.redis_rpc import call_redis_rpc
import redis

r = redis.Redis(host="192.168.1.100", port=6379, db=5, password="...")
resp = call_redis_rpc(r, "你的账号", "get_full_tick", {"codes": ["000001.SZ"]})
print(resp["data"]["000001.SZ"]["lastPrice"])
```

**方式 C：无缝替换旧 xtquant（最终切换）**

把仓库 `src` 放到 `PYTHONPATH` 最前面，旧代码的 `from xtquant import xtdata` 自动命中本仓库 shim：

```powershell
$env:PYTHONPATH = "D:\gjzqqmt\xtquant_big_convert\src;$env:PYTHONPATH"
```

```python
# 旧代码完全不改
from xtquant import xtdata
ticks = xtdata.get_full_tick(["600000.SH"])  # 走 RPC 到大 QMT
```

---

## 切换传输层

### 只需改一个字段

服务端 + 客户端的配置文件里，`transport` 字段保持一致即可：

```python
BIGQMT_REDIS_CONFIG = {
    "transport": "zmq",                  # redis / zmq / mysql / shm
    "zmq": {"host": "127.0.0.1"},        # 各传输子配置
    # redis 配置保留（zmq 服务发现、mysql 不需要时的 fallback 都用它）
}
```

### 各传输配置示例

**Redis（默认）**：
```python
{"transport": "redis"}  # 或省略 transport 字段
```

**ZMQ**（同机低延迟，需 pyzmq）：
```python
{
    "transport": "zmq",
    "rpc_background_threads": True,        # 必须！
    "zmq": {
        "host": "127.0.0.1",              # 默认端口从 account_id 派生
        # "port": 5560,                   # 可显式指定
        # 端口冲突时自动找空闲端口 + 通过 Redis 服务发现告知客户端
    },
}
```

**MySQL**（兼容兜底，需 pymysql + DBUtils）：
```python
{
    "transport": "mysql",
    "rpc_background_threads": True,        # 必须！
    "mysql": {
        "driver": "pymysql",
        "host": "192.168.1.100", "port": 3306,
        "user": "root", "password": "...",
        "database": "bigqmt_rpc", "charset": "utf8mb4",
        "poll_interval_seconds": 0.01,
        "pool_config": {"mincached": 1, "maxcached": 3, "maxshared": 0, "maxconnections": 4},
    },
}
```

### ZMQ 端口与服务发现

- 默认端口从 account_id 派生：`15560 + (账号数字 mod 100)`，不同账号自动不冲突。
- 端口被占时，server 自动往上扫描找空闲端口，把真实地址写到 Redis key `bigqmt:zmq:addr:{account_id}`（TTL 300s）。
- 客户端连接时按优先级解析地址：显式 `connect_address` > Redis 服务发现 > 默认派生端口。
- server 退出时自动清理 discovery key。
- 服务发现是可选的（没配 Redis client 时退化为静态派生端口）。

完整传输层文档见 [docs/RPC_TRANSPORTS.md](docs/RPC_TRANSPORTS.md)。

---

## 实测延迟对比（真实直连 QMT）

三种传输全部实测，端到端连接真实 QMT 进程，n=15/方法：

| 传输 | ping p50 | get_full_tick p50 | 成功率 | 尖峰来源 |
|------|---------|------------------|--------|---------|
| **Redis** | 13ms | 15ms | 100% | 偶发 245ms（网络抖动）|
| **ZMQ** | 0.7ms* | 0.7ms* | 100% | 30% 撞 500ms（QMT adjust GIL）|
| **MySQL** | 104ms | 110ms | 100% | 轮询开销 |

*ZMQ fast-path（避开 GIL 尖峰的请求）；overall p90 ~498ms。

**生产推荐 Redis**：稳定、跨机、无 GIL 问题、QMT 端零额外依赖。ZMQ 理论最快但受 QMT 主线程 GIL 调度影响。MySQL 仅作兜底。

复现基准：
```powershell
python bench_latency.py        # Redis 单传输延迟
python bench_transports.py -n 100  # Redis vs ZMQ 对比
```

---

## 目录结构

```
src/bigqmt_signal_trader/
├── transports/                    可插拔传输层
│   ├── base.py                    RpcTransport 抽象接口
│   ├── redis_transport.py         Redis（默认，rpush/blpop/brpop）
│   ├── zmq_transport.py           ZMQ（ROUTER/DEALER + 服务发现）
│   ├── mysql_transport.py         MySQL（轮询 + DBUtils 连接池）
│   ├── shm_transport.py           共享内存（stub）
│   └── factory.py                 build_transport 工厂
├── adapters/                      QMT API 适配器
│   ├── market_bigqmt.py           行情（ContextInfo 封装）
│   ├── order_bigqmt.py            下单（passorder）
│   ├── position_bigqmt.py         持仓（get_trade_detail_data）
│   └── redis_common.py            Redis 连接/编解码
├── redis_rpc.py                   RPC 服务（handlers + service + transport 集成）
├── xtquant_compat.py              客户端兼容层（xt_trader / xtdata + 异步回调）
├── exec_events.py                 委托/成交/错误事件推送（Redis pubsub）
├── quote_push_channel.py          全推行情推送通道（redis/zmq PUB/SUB）
├── quote_subscription_manager.py  服务端全推订阅管理（引用计数 + 组合键去重）
├── whole_quote_session.py         客户端全推订阅会话（心跳 + 重启恢复）
├── full_tick_cache.py             全市场行情快照缓存（可选降载）
├── strategy.py 之类               策略骨架、风控、价格引擎等
bigqmt_no_redis/                   无 redis 版本（QMT 沙箱拒绝 import redis 时用）
│   ├── zmq_transport.py           自包含 ZMQ transport（内联编码，零 redis 依赖）
│   └── DRYRUN_no_redis.py         无 redis DRYRUN 入口
src/xtquant/                       可选 xtquant import shim
src/bigqmt_signal_trader_strategy.py        策略入口（init/handlebar/adjust + 启动诊断）
src/bigqmt_signal_trader_redis_rpc_runtime.py  Redis RPC runtime 入口
src/BIGQMT_REDIS_DRYRUN.py                  QMT 编辑器加载入口（GBK）
src/BIGQMT_ZMQ_DRYRUN.py                    同机 ZMQ QMT 编辑器入口（GBK）
src/BIGQMT_ZMQ_BACKTEST.py                  独立 QMT 回测 ZMQ 入口（GBK）
src/bigqmt_backtest/                        独立历史驱动、模拟撮合、ZMQ 协议与客户端
tests/bigqmt_signal_trader/        单元测试（无 QMT 环境可跑）
tests/bigqmt_backtest/             回测、确定性、隔离和 ZMQ 往返测试
qmt-trader/                        AI 助手 Skill（大模型直接操作 QMT，见下文专节）
│   ├── SKILL.md                   skill 说明书（命令速查 + 工作流 + 安全须知）
│   ├── scripts/qmt.py             统一 CLI（46 子命令 + rpc 兜底）
│   └── references/api_reference.md  完整 API 参考
docs/                              详细文档
test_all_apis.py                   端到端 API 测试（发现生产问题）
bench_latency.py / bench_transports.py  延迟基准脚本
```

---

## 本地测试

```powershell
python -m pytest tests/bigqmt_signal_trader/ -q
```

当前覆盖 **199 个用例**（含传输层往返、Redis RPC、客户端兼容、持仓/行情/下单 handlers、异步回调、执行事件）。

### 端到端 API 测试（发现生产问题）

`test_all_apis.py` 是**端到端验证**测试——不只测「调用成功」，还测「结果正确」，能发现这些生产问题：

| 验证项 | 检测什么 | 为什么重要 |
|--------|---------|-----------|
| **客户端/服务端一致性** | ping 超时 → transport 不匹配 | Issue #24 根因：客户端 redis / 服务端 zmq 连不上 |
| **持仓查询** | `get_positions` 返回空但账户有持仓 | 容错设计把「失败返回空」当成「正常」 |
| **委托查询** | `query_orders` 返回空 | strategy_name 不匹配（默认应为 `""` 返回全部） |
| **买入/卖出** | `submit_order` 成功但委托没进系统 | 静默失败（passorder 被 QMT 拒绝但没报错） |
| **server_error** | 显示 QMT 端拒绝原因 | 委托被 QMT 静默拒绝时返回具体原因 |

**用法**：
```powershell
# 方式 A：用环境变量
$env:BIGQMT_ACCOUNT_ID="你的账号"
$env:BIGQMT_REDIS_HOST="你的Redis地址"
$env:BIGQMT_REDIS_PORT="6379"
$env:BIGQMT_REDIS_DB="5"
$env:BIGQMT_REDIS_PASSWORD="你的密码"
python test_all_apis.py

# 方式 B：用 QMT 端配置（需 bigqmt_signal_trader_local_config.py 在 PYTHONPATH）
$env:PYTHONPATH="D:\国金证券QMT交易端\python;$env:PYTHONPATH"
python test_all_apis.py
```

**示例输出**（发现问题时）：
```
--- 端到端验证: 客户端/服务端一致性 ---
客户端配置 transport: redis
❌ ping 失败: redis rpc timeout: ping
   可能原因: 客户端 transport 和服务端不匹配
   - 客户端配置 transport=redis
   - 如果服务端是 zmq, 客户端也要设 transport=zmq

--- 端到端验证: 持仓查询 ---
⚠️  get_positions 返回空 — 账户可能真的没持仓, 或查询失败 (检查 QMT 上下文)

--- 端到端验证: 买入/卖出 ---
✅ submit_order OK
❌ 委托没进系统 — submit_order 成功但 query_orders 找不到
   这是静默失败 (passorder 被 QMT 拒绝但没报错)
   检查: 1) 价格是否超出范围 2) 账户权限 3) QMT 风控
```

---

## 日志与排错（出错去哪看）

系统自带**文件日志**——所有报错/异常同时写 QMT 输出面板和本地日志文件，重启/崩溃后也能回溯。

### 日志位置

| 环境 | 日志文件 |
|------|---------|
| **QMT 内（服务端）** | `<QMT python 目录>\logs\bigqmt.log`（如 `D:\国金证券QMT交易端_lemo\python\logs\bigqmt.log`）|
| **外部客户端** | `~\.cache\bigqmt\logs\bigqmt.log`（用户目录下）|

- **按天轮转**（午夜），**默认保留最近 7 天**。
- 每行带时间戳 + 级别 + 模块标签：`2026-08-14 21:45:59 [ERROR] [bigqmt.quote_push] publisher start failed: ...`

### 查看方式

```powershell
# 实时跟踪日志
Get-Content "D:\国金证券QMT交易端\lempython\logs\bigqmt.log" -Wait -Tail 50

# 只看错误
Get-Content "D:\...\python\logs\bigqmt.log" | Select-String "ERROR|WARN"
```

### 配置

| 环境变量 | 默认 | 说明 |
|---------|------|------|
| `BIGQMT_LOG_ENABLED` | `1` | 置 `0` 关闭文件日志 |
| `BIGQMT_LOG_TO_STDOUT` | `1` | 置 `0` 不输出到 QMT 面板 |
| `BIGQMT_LOG_RETENTION_DAYS` | `7` | 日志保留天数 |

> **排错首选看日志文件**：QMT 面板内容重启/清空后丢失，日志文件保留 7 天，包含启动诊断（`[bigqmt_diag]`）、崩溃原因、端口冲突等。

---

## 安全默认值

- `rpc_allow_order_methods` 默认 `False`：远程 `order_stock` / `cancel_order` 被拒绝。确认接入方、账号、风控后再显式开启。
- 回测桥接永久 `live_ready=false`，协议中没有真实账户和实盘下单方法。
- 配置文件含资金账号和密码，`bigqmt_signal_trader_local_config.py` / `bigqmt_signal_trader_client_config.py` 已在 `.gitignore`，**不要提交**。
- 请求负载经过 base64 + 数字混淆编码（`encode_rpc_request_payload`），避免 QMT 的 Redis 客户端拦截含股票代码的明文。

---

## AI 助手 Skill：qmt-trader（大模型直接操作 QMT）

仓库内置一个 **Agent Skill**——[qmt-trader/](qmt-trader/)，让支持 SKILL.md 约定的 AI 编程助手（Claude Code / ZCode / Cursor / Codex 等）**直接用命令行驱动 QMT 的全部交易与行情能力**，无需每次现场写 Python 调用代码。人也可以脱离 AI 手动执行其中的 CLI 脚本。

### 目录结构

```
qmt-trader/
├── SKILL.md                        skill 说明书（触发条件 + 命令速查 + 典型工作流 + 安全须知）
├── scripts/qmt.py                  统一 CLI 入口（46 个子命令 + 通用 rpc 兜底，约 1000 行）
└── references/api_reference.md     完整 API 参考（参数/返回值/常量/已知陷阱）
```

### 工作原理

- AI 助手匹配到 `SKILL.md` 里的 `description`（"查行情 / 查持仓 / 下单 / 龙虎榜 / 北向资金…时触发"）后自动加载本 skill；
- 之后助手调用 `python qmt-trader/scripts/qmt.py <子命令>` 执行**确定性命令**，不再临时生成 RPC 调用代码，避免参数写错；
- 所有命令默认输出 JSON（`ok` / `data` / `ts` 三字段，便于模型解析），加 `--table` 切换人类可读表格；出错时返回 `ok: false` + `error` / `detail` / `code`，退出码 1；
- `qmt.py` 自动把仓库 `src/` 加入 `sys.path`（开发模式免 pip install），并自动发现 QMT 的 python 目录读取客户端配置。

### 启用方式

**方式 A：安装到 AI 助手的 skills 目录**（推荐，全局生效）：

```powershell
# Claude Code
cp -r qmt-trader ~/.claude/skills/qmt-trader
# ZCode / 其他遵循 agents skills 约定的助手
cp -r qmt-trader ~/.agents/skills/qmt-trader
```

安装后正常提需求即可，例如"帮我看下工商银行最近的走势""我账户现在什么持仓"，助手会自动触发。

**方式 B：不安装，对话里显式指定**：

> 阅读 qmt-trader/SKILL.md，之后用里面的 qmt.py 命令帮我查行情 / 持仓 / 下单。

**方式 C：纯手动**（不经过 AI，人直接当 CLI 用）：

```powershell
python qmt-trader/scripts/qmt.py ping
python qmt-trader/scripts/qmt.py snapshot --table
```

### 前置条件

与「快速开始」的客户端一致：

1. QMT 端 RPC 服务已启动（同机 ZMQ 运行 `BIGQMT_ZMQ_DRYRUN.py`，其它 transport 运行 `BIGQMT_REDIS_DRYRUN.py`，输出面板/日志看到启动诊断 OK）；
2. 客户端配置就绪——环境变量（`BIGQMT_ACCOUNT_ID` / `BIGQMT_REDIS_HOST` / `BIGQMT_REDIS_PORT` / `BIGQMT_REDIS_DB` / `BIGQMT_REDIS_PASSWORD`）或配置文件；
3. 先 `ping` 确认连通：redis 约 13ms / zmq 约 0.7ms 为正常，超时说明 transport 或配置不匹配。

### 一分钟上手

```powershell
# 0. 连通性检测（含延迟测量）
python qmt-trader/scripts/qmt.py ping

# 1. 账户全景：资产 + 持仓 + 委托 + 成交（一次往返）
python qmt-trader/scripts/qmt.py snapshot

# 2. 实时五档盘口（含涨跌幅）
python qmt-trader/scripts/qmt.py tick 600000.SH

# 3. 前复权日 K 60 根（含 MA5/20/60 统计）
python qmt-trader/scripts/qmt.py kline 600000.SH --period 1d --count 60 --dividend front

# 4. 干跑下单（只打印不提交，确认参数）
python qmt-trader/scripts/qmt.py buy 600000.SH 100 --price 7.50 --dry-run
```

### 命令概览

| 分类 | 命令 |
|------|------|
| **连通/全景** | `ping` / `snapshot` |
| **账户** | `account`（资产）/ `positions`（持仓含浮动盈亏）/ `orders`（委托含语义化状态）/ `trades`（成交） |
| **行情** | `tick` / `kline` / `instrument` / `sector` / `trading-dates` / `north`（北向）/ `longhubang`（龙虎榜）/ `financial`（财务）/ `download`（历史数据下载）/ `quote-subscribe`（全推订阅） |
| **扩展查询（25 个快捷命令）** | `holiday` / `stock-name` / `instrument-type` / `divid-factors` / `market-times` / `trading-calendar` / `option-list` / `bsm-price` / `bsm-iv` / `hkt-stats` / `hkt-details` / `hkt-rate` / `top10-holder` / `holder-num` / `ipo` / `ipo-limit` / `credit-assure` / `credit-short` / `credit-debt` / `his-st` / `index-weight` / `industry` / `sector-info` / `local-data` / `timetag2dt` / `dt2timetag` |
| **交易** | `buy` / `sell` / `cancel`（均支持 `--dry-run`，buy/sell 支持 `--latest` / `--strategy` / `--remark`） |
| **通用兜底** | `rpc <method> [json]` — 调用白名单内**任意**方法（如 `rpc get_l2_quote '{"stock_code":"600000.SH"}'`），未列出的方法都能这样调 |

### 安全设计

- 下单三命令（`buy` / `sell` / `cancel`）受服务端白名单控制，`rpc_allow_order_methods` 默认 `False`，未显式开启时返回 `ORDER_DISABLED`；
- 下单前先用 `tick` 看价 + `--dry-run` 确认参数；
- 报 `ORDER_TIMEOUT` 时**不要直接重试**，先 `orders` 查询确认委托是否已进系统，避免重复下单；
- 下单的 `--strategy` 与查询的 `--strategy` 需一致；查全部委托用 `orders --strategy ""`（空 = 不过滤）。

完整命令表、四个典型工作流（行情分析 / 持仓监控 / 下单交易 / 批量分析）和 API 参数细节见 [qmt-trader/SKILL.md](qmt-trader/SKILL.md) 与 [qmt-trader/references/api_reference.md](qmt-trader/references/api_reference.md)。

---

## 基于本项目的应用：bigqmt-dashboard

[**bigqmt-dashboard**](https://github.com/litaolemo/bigqmt_dashboard) —— 大QMT 直连的多账号持仓监控与下单面板。浏览器里看持仓、资金曲线、买卖流水，点一下就把单子报进大QMT。

[![面板总览](https://raw.githubusercontent.com/litaolemo/bigqmt_dashboard/main/docs/screenshots/01-overview.png)](https://github.com/litaolemo/bigqmt_dashboard)

它是本项目目前最完整的下游使用者，几乎把这里的接口都跑了一遍——如果你想知道某个 API 在真实业务里怎么用，那边有现成的代码：

| 它用了什么 | 对应到本项目 |
|---|---|
| 每账号独立连接、可连不同机器上的大QMT | 直接构造 `BigQmtXtTrader(account_id=..., redis_config=...)`，**不用** `configure()` 的模块级单例 |
| 账户数据同步 | `query_stock_positions` / `query_stock_asset` / `query_execution_snapshot` |
| 实时委托与成交回报 | `register_callback` + `start()`，回报经 `exec_events` 推来 |
| 下单撤单 | `order_stock_result` / `cancel_order_stock`（需 `rpc_allow_order_methods=True`） |
| 实时行情与分钟线 | `get_full_tick` / `get_market_data_ex`（缺数据时先 `download_history_data2` 再重试） |
| 合约属性 | `get_instrument_detail` / `get_instrument_type`，走 FormulaServer 直连快速路径 |
| 打新债 | `ipo_subscribe_all(stock_type="BOND")` |
| 换传输不改代码 | 账号配置里的 `rpc` 段整包透传给 `BigQmtRpcClient`，`transport` 改 `redis`/`zmq` 即可 |

几个从对接中反馈回来、值得单独提一句的点：

- **可转债的下单规整要自己写。** `code_utils.min_lot()` 只认「688 开头 = 200，其余 = 100」，可转债最小 10 张会被 `(10 // 100) * 100` 规整成 **0**；`normalize_stock_code()` 对裸 6 位码按「5/6 开头 = 沪市」判断，沪市转债 `110xxx` 会被判到深市。面板那边重写了一份全品种规则（含科创板 200 股起按 1 股递增、ETF/转债 0.001 报价精度），并拿 `get_instrument_detail` 返回的 `PriceTick` 交叉验证过 9 个品种，全部吻合。
- **`get_market_data_ex` 读的是 QMT 本地库。** 没 `download_history_data2` 过的标的返回 0 根而不是报错——面板实测 10 只持仓全都没有 1m 数据，走势图整列是空的，加了「缺数据先下载再重试」才好。
- **`docs/XTQUANT_COMPAT_REPLACEMENT.md` 里「RPC 暂不推送回调」是旧文。** 代码里 `BigQmtXtTrader.start()` 会拉起执行事件监听线程，`on_stock_order` / `on_stock_trade` 是真的会触发的。

---

## 相关文档

- [CHANGELOG.md](CHANGELOG.md) — **版本变更记录**（新增/修复/变更）
- [docs/DEPLOY_QUICKSTART.md](docs/DEPLOY_QUICKSTART.md) — **单账号部署快速开始**（最短路径 + 部署期常见问题表）
- [docs/LATENCY_REPORT.md](docs/LATENCY_REPORT.md) — **延迟测试报告**（传输层对比、FormulaServer 直连、下单链路、方法论）
- [docs/RPC_API_REFERENCE.md](docs/RPC_API_REFERENCE.md) — **全部 RPC 方法参考**（参数、返回值、别名、大 QMT 能力边界）
- [docs/FORMULA_SERVER_FASTPATH.md](docs/FORMULA_SERVER_FASTPATH.md) — FormulaServer(58600) 直连快速路径：协议、映射表、能力边界与回退行为
- [docs/SUBSCRIBE_WHOLE_QUOTE_PUSH.md](docs/SUBSCRIBE_WHOLE_QUOTE_PUSH.md) — 全推行情订阅推送机制设计
- [docs/SUBSCRIBE_WHOLE_QUOTE_LIVE_VERIFICATION.md](docs/SUBSCRIBE_WHOLE_QUOTE_LIVE_VERIFICATION.md) — 全推行情实盘验证报告
- [docs/BIG_QMT_REDIS_RPC.md](docs/BIG_QMT_REDIS_RPC.md) — Redis RPC 协议与入口脚本详解
- [docs/RPC_TRANSPORTS.md](docs/RPC_TRANSPORTS.md) — 可插拔传输层完整说明
- [docs/XTQUANT_COMPAT_REPLACEMENT.md](docs/XTQUANT_COMPAT_REPLACEMENT.md) — 用兼容层替换旧 xtquant 的步骤
- [docs/BIG_QMT_SIGNAL_TRADER_RUNBOOK.md](docs/BIG_QMT_SIGNAL_TRADER_RUNBOOK.md) — 信号交易运行手册
- [docs/ZMQ_BACKTEST_BRIDGE.md](docs/ZMQ_BACKTEST_BRIDGE.md) — 独立 ZMQ 回测协议、撮合规则和 QMT 入口
- [qmt-trader/](qmt-trader/) — **QMT Trader skill**：AI 助手统一 CLI 驱动全部 QMT API（46 子命令 + 通用 rpc 兜底），用法见上文「AI 助手 Skill：qmt-trader」
- [bigqmt-dashboard](https://github.com/litaolemo/bigqmt_dashboard) — **基于本项目的持仓监控与下单面板**：多账号、服务端风控闸门、完整可转债支持，可当作接口的实际用法参考专节

---

## 为什么不直接连大 QMT

官方 `xtquant.xttrader.XtQuantTrader` 依赖客户端侧 XtQuantServer 通道。当前国金大 QMT 环境中直接连 `connect()` 返回 `-1`，**交易能力**因此必须放在大 QMT 内部策略进程里，外部通过 RPC 驱动。

**但只读行情不必走 RPC。** `58600` 是 FormulaServer，它同时就是行情/参考数据服务——QMT 自带 Python 里的 `qmt_api` 包（`bin.x64/Lib/site-packages/qmt_api`）正是它的客户端。本仓库已接入这条直连快速路径，见上文「FormulaServer 直连快速路径」。

如果后续券商开通 XtQuantServer 权限且 `connect()==0`，可再加交易直连模式。
