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

### 异步下载任务（download jobs）

`download_history_data` / `download_history_data2` 是耗时的长调用：如果走同步 RPC，服务端会在**策略线程**上一直下载，冻结整个 RPC pump（且客户端 6s 就超时崩）。因此这两个方法改为**异步分块任务**：客户端把任务写入 Redis 队列立即返回，大 QMT 的策略线程每个 tick 只下载 `download_job_chunk_size` 只（受 `download_job_max_wall_seconds` 墙钟预算约束），永不长时间阻塞。

Redis 布局（按账户）：

```text
bigqmt:download:queue:{account_id}          # 待处理 job_id 列表（RPUSH/LPOP）
bigqmt:download:job:{account_id}:{job_id}    # job JSON（含 state/done/total/error 进度）
bigqmt:download:current:{account_id}         # 当前正在处理的 job_id（串行，一次一个）
```

job 状态：`pending → running → done | failed`。历史 K 线下载到**大 QMT 机器**的本地库；客户端随后用 `get_local_data` / `get_market_data` 快读取回。

客户端用法：

```python
# 非阻塞：提交后轮询
job = xtdata.submit_download_history_data2(["600000.SH", "000001.SZ"], "1d")
status = xtdata.get_download_status(job["job_id"])   # {state, done, total, error}
status = xtdata.wait_download(job["job_id"])          # 阻塞轮询到 done/failed（仅客户端阻塞）

# 兼容：download_history_data2(...) 仍可直接调用 = 提交 + 等待（默认最多 1800s）；
# 超时会抛 TimeoutError（任务在服务端继续跑，可继续轮询）。大批量建议用 submit + 轮询。
```

服务端开关：`download_jobs_enabled`、`download_job_chunk_size`（默认 10，每 tick 最小下载块）、`download_job_max_wall_seconds`（默认 0.5s，每 tick 墙钟预算）、`download_job_ttl_seconds`（默认 3600）。

### 实时成交/委托回调推送（exec events）

大 QMT 的 `order_callback(ContextInfo, orderInfo)` / `deal_callback(ContextInfo, dealInfo)` 在策略进程内触发。服务端把 QMT 对象的 ThinkTrader `m_*` 字段规范化后 publish 到 Redis，客户端后台线程订阅并回调 —— 无需轮询即可**实时**拿到成交/委托。

Redis 频道（同名 stream，xadd + publish，供短时回放）：

```text
bigqmt:order_events:{account_id}
bigqmt:trade_events:{account_id}
```

成交事件字段（由 `deal_callback` 的 `m_*` 映射）：`stock_code`(`m_strInstrumentID`)、`trade_id`(`m_strTradeID`)、`order_sys_id`(`m_strOrderSysID`)、`volume`(`m_nVolume`)、`price`(`m_dPrice`)、`amount`(`m_dTradeAmount`)、`commission`(`m_dComssion`)、`direction`(`m_nDirection`) 及 `action`(尽力映射 BUY/SELL)、`traded_at`(`m_strTradeTime`)。委托事件类似（`m_nOrderStatus`→`status`、`m_nVolumeTotal`→`order_volume`、`m_nVolumeTraded`→`traded_volume`、`m_dLimitPrice`→`price`、`m_dTradedPrice`→`traded_price`、`m_dTradeAmount`→`trade_amount`）。委托的 `trade_amount` 是柜台的精确成交金额；取不到时为 0.0，不会用 `traded_price × traded_volume` 估算顶替（#173）。

客户端用法（MiniQMT 风格，回调实时触发）：

```python
class MyCallback(XtQuantTraderCallback):
    def on_stock_trade(self, trade):   # 成交实时回调
        print(trade.stock_code, trade.trade_id, trade.traded_volume, trade.traded_price)
    def on_stock_order(self, order):   # 委托状态实时回调
        print(order.stock_code, order.order_status, order.traded_volume)

xt_trader.register_callback(MyCallback())
xt_trader.start()          # 启动后台监听线程（订阅上面两个频道）
xt_trader.subscribe(acc)   # 账号确定后会自动重订阅到该账号频道
```

服务端开关：`exec_events_enabled`（默认 True）。`action` 由 `m_nDirection` 尽力映射（48/23→BUY，49/24→SELL），未知时为空但 `direction` 原值始终保留。

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

### 两档处理模型(重要)

同一进程只有一个 GIL,方法按处理线程分两档:

- **inline 档(后台接收线程直接处理)**:`ping`、行情类(`get_full_tick`/`get_market_data_ex`/
  `get_instrument_detail`)、`query_stock_asset`。中位数**亚毫秒**,但会撞上大 QMT 终端占 GIL
  的尾延迟(见下)。
- **deferred 档(推迟到主策略线程,经 adjust drain)**:所有走 `get_trade_detail_data` 的**交易
  查询**——持仓/委托/成交、信用/账户明细,以及下单/撤单。**原因**:`get_trade_detail_data` 在后台
  线程上返回空(账户实有持仓也查出 0),必须在 QMT 主线程上下文里跑。这些方法登记在
  `LISTENER_DEFERRED_METHODS`,由 `run_time("adjust", interval)` 每拍 `drain_pending()` 在主
  线程执行。(`get_asset` 例外,走另一个 QMT 调用,后台线程即可,保持 inline 低延迟。)

> `adjust` 不是 QMT 内置回调。QMT 只自动调 `init`/`handlebar`;`handlebar` 里 `return
> adjust(...)`,加上我们 `run_time("adjust", interval)` 注册的定时器,构成 RPC 队列的 drain 节奏。

### 每拍 drain 的时间预算与过期拒绝（#303）

下单在 adjust 线程上串行跑（`passorder` 实测 ~200ms/笔）。一拍取 20 笔一口气跑完再结算,
第一笔 0.2s 就完成、回复却和第 20 笔一起 4s 后才发,线程被占住的这 4s 里 QMT 的
`order_callback` 也落不下来。所以:

- **时间预算**:`drain_pending(budget_seconds=...)` 跑够预算就把剩下的留到下一拍(每拍至少
  跑一笔)。策略文件默认取**一个 adjust 间隔、最少 0.5s**;`rpc.drain_budget_seconds` 可改,
  `0` 关。批内每 0.25s 结算一次并发回复,早完成的单先走。
- **过期拒绝**:客户端 `call` 在信封里带 `timeout_seconds`,服务端收到时打时间戳。轮到执行
  时已过客户端期限(留 1s 余量,最多期限的 1/4)的请求**拒绝而不执行**,回 `RequestExpired`。
  下单类请求这条最要紧——客户端 6s 放弃了、桥第二拍照样 `passorder`,就是「记成失败的单
  几秒后出现在柜台」。撤单不拒(晚到的撤单无害);不带 `timeout_seconds` 的旧客户端不受影响。
- **结算共用快照**:一次结算遍历按账号只查一次 `get_trade_detail_data`(~1s),N 笔待结算不再
  是 N 次。
- **超时后问结果**:`get_request_outcome(request_id)` 是只读方法、跑在收包线程,adjust 忙着
  也能答;客户端 `order_stock` 超时后自动问,把「不知道下没下」变成「没下,可重试」或
  「下了,编号在这」。

### 尾延迟 = 大 QMT 终端占 GIL(不是本代码)

`gil_probe` 探针显示进程周期性被卡 ~490ms,但 `adjust_phase` 每段都 <50ms —— 即**尾延迟来自
QMT 终端自身的 C++ 主循环占着 GIL**,`setswitchinterval`/精简 adjust 都 preempt 不了。唯一根治
是把 serving 挪出该进程(sidecar 独立 GIL)。

### schedule_adjust_interval 调这个数压尾延迟

`run_time` 间隔 = 后台线程拿到主线程 GIL 窗口的节奏源;间隔越小,inline 尾越低:

| interval | adjust 频率 | inline 尾(p90/max) | CPU | 说明 |
|---|---|---|---|---|
| `500nMilliSecond` | ~2.4/s | ~490 / 510ms | 极低 | 默认省电 |
| `200nMilliSecond` | 折中 | ~200ms 量级 | 中 | **推荐平衡点** |
| `100nMilliSecond` | 10/s（稳态；启动后回放窗口里几千/s 是历史 K 线回放，不是这个定时器） | ~92 / 108ms | 低 | 现默认 |

**deferred 交易查询的下限是一拍，上限看 `get_trade_detail_data` 自己**：2026-09-04 实测
p50 1012~1013ms（与 interval 无关），2026-09-22 同一终端 drain 下 103ms —— 差的是柜台查询
自身的开销（当天委托/成交行数），调 interval 无效。要低延迟拿持仓,走**客户端 redis
缓存**(position_sync 已在写)而非每次实时查。

### zmq 真机实测(同机 localhost)

- inline:`ping` p50 **0.4-0.5ms**;`get_market_data_ex` 因 handler 较重几乎必吃满一个尾窗口
  (500ms 档 p50≈495ms,100ms 档 p50≈96ms)。
- deferred:持仓/委托/成交 p50 **~1s**。
- 下单/撤单已在**实盘**验证:`order_stock` 挂单(status=50 已报)→ `query_stock_orders` 拿到
  sysid → `cancel_order_stock` 成功、无残留。

### 传输与后台线程

- **redis**(默认,跨机):`rpc_process_in_listener=True`。
- **zmq**(同机免 Redis):只加 `transport="zmq"` 一行,端口按账号派生 `tcp://127.0.0.1:1556x`。
- **`rpc_background_threads` 一律 `False`**(adjust drain)。后台收包线程每拿一次 GIL 付一个
  adjust tick,redis 回包 8 次往返就是 ~400ms(#343);drain 下所有传输都是 ≤1 tick。不写这个
  键时能 drain 的传输默认就是 drain,只有没有 drain 实现的保留收包线程。
- **重读走工作线程**(`rpc_heavy_offload`,默认 `True`,#351)。drain 下所有请求都在 adjust 线程上
  跑,一次 1.8s 的 `get_market_data_ex` 就是策略自己的 `tick_app` 停 1.8s——这正是 #321 把 LPOP
  从 adjust 上拿掉的原因。现在按方法(`get_financial_data` / `get_raw_financial_data`、
  `call_formula` / `gen_factor_index` 等)或按大小(市场令牌的 `get_full_tick`、超过
  `rpc_heavy_codes_threshold`=20 个代码、`period="tick"`、按日期窗口取 K 线)判定为重的读请求交给
  一条工作线程,回包由 adjust 线程在下一拍发出(zmq 的 ROUTER socket 不能跨线程用)。
  重读的往返多付 1~2 拍;轻读和所有交易查询照旧一拍、照旧在 adjust 线程。
  实测（2026-09-22，100ms 拍，每种读在工作线程上连打，看终端 `adjust cadence` 的 avg / max）：
  全市场 `get_full_tick(["SH","SZ"])` 读本身 ~200ms，拍 0.100 / 0.25–0.33s；三只 4 个月 1m 窗口
  ~500ms，拍 0.100 / 0.27–0.29s；`get_financial_data` 10 只 3 表 ~2.1s，拍 0.100 / ~0.5s——QMT 的
  C 接口大部分时间放 GIL，拍的均值不变、最坏从整段读缩到一段。`download_history_data2` 5 只 ~1.2s
  整段持 GIL，拍 0.56–0.63 / 1.3–1.5s，工作线程帮不上、回包还多付一两拍，所以 `download_*` 不列入，
  照旧 adjust 线程。
  `probe_capabilities` 的 `thread_routing.heavy_sample` 报每类走哪边。后台线程模式下没有这条线程
  (收包线程本来就在 adjust 之外跑)。
- QMT 编辑器可直接加载 `BIGQMT_ZMQ_DRYRUN.py`；该入口强制使用 ZMQ，并复用原有 Bridge 加载逻辑。
- 内置 Redis 客户端读取含股票代码的原始 JSON 会触发 `Sensitive Data Detected`;客户端 helper 默认
  对请求做安全编码。

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

