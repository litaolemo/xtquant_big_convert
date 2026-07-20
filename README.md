# xtquant_big_convert

大 QMT 运行环境里的 RPC 桥接包：把大 QMT 内置 Python（行情查询、交易、持仓）封装成**可远程调用的服务**，并兼容一组 MiniQMT 方法名，让外部程序无需 XtQuantServer 权限就能驱动大 QMT。

支持 **Redis / ZMQ / MySQL / 共享内存** 四种可插拔传输，切换只需改一个配置字段。

---

## 功能一览

### RPC 接口（远程可调用）

通过 RPC 可调用的大 QMT 能力（**白名单 117 个只读方法 + 2 个下单方法 + 12 个 MiniQMT 风格别名**，覆盖官方文档全部交易/查询函数）：

| 类别 | 方法 |
|------|------|
| **系统** | `ping` |
| **行情快照** | `get_ticks` / `get_full_tick`（五档盘口）|
| **合约/品种** | `get_instrument` / `get_instrument_type` / `get_stock_name` / `get_stock_type` / `get_last_close` / `get_last_volume` / `get_open_date` / `get_contract_expire_date` / `get_contract_multiplier` / `get_float_caps` / `get_total_share` / `get_turn_over_rate` / `get_weight_in_index` / `get_svol` / `get_bvol` / `get_risk_free_rate` / `is_stock_type` / `get_cb_info` |
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
| **新股/打新** | `get_ipo_data` / `get_new_purchase_limit` |
| **融资融券** | `get_assure_contract`（担保品）/ `get_enable_short_contract`（融券标的）/ `get_unclosed_compacts`（未平仓）/ `get_closed_compacts`（已平仓）/ `get_debt_contract`（负债）—— 需两融权限，普通账户降级为空 |
| **期权持仓** | `get_option_subject_position`（标的持仓）/ `get_comb_option`（组合期权）|
| **持仓同步** | `sync_positions`（写回 Redis 供客户端缓存）|
| **下单/撤单** | `submit_order` / `cancel_order`（默认关闭，需显式开启）|

> 客户端兼容层 `BigQmtXtData` 对常用方法有显式封装（`xtdata.get_longhubang(...)`、`xtdata.bsm_price(...)` 等），其余通过万能入口 `xtdata.call_method("get_float_caps", stockcode="000001.SZ")` 调用。

> `*` 标记的方法在大 QMT（完整交易端）环境下用 **fallback** 实现（非原生数据）：`get_sector_list` 返回常用板块名清单，`get_holidays` 从交易日历反推，`get_markets` 返回固定市场集合，`get_market_last_trade_date` 从日历派生。详见 [docs/RPC_API_REFERENCE.md](docs/RPC_API_REFERENCE.md) 第 8 节「大 QMT 环境的能力边界」。

### 客户端兼容层

- `bigqmt_signal_trader.xtquant_compat`：把旧代码的 `xt_trader` / `xtdata` 调用转成 RPC，无需改业务代码。
- 兼容 MiniQMT 方法名：`query_stock_asset` / `query_stock_positions` / `query_stock_orders` / `get_full_tick` / `order_stock` 等。

### 可插拔传输层

| 传输 | 同机 p50 | 跨机 | 适用场景 |
|------|---------|------|---------|
| **redis**（默认）| ~13ms | ✅ | 生产默认，稳定 |
| **zmq** | ~0.7ms* | ✅ | 同机低延迟 |
| **mysql** | ~105ms | ✅ | 兼容兜底 |
| **shm** | — | ❌ | 接口预留（未实现）|

*zmq fast-path；约 30% 请求会撞 QMT 的 GIL 调度尖峰（~500ms）。

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

---

## 环境要求与依赖安装

### 大 QMT 端（服务端）

QMT 自带 Python 3.6（`bin.x64/python.exe`），需要按所选传输安装依赖：

| 传输 | 必需依赖 | 安装方式 |
|------|---------|---------|
| **redis**（默认）| `redis`（QMT 通常已内置）| 无需额外安装 |
| **zmq** | `pyzmq` | 见下 |
| **mysql** | `pymysql` + `DBUtils` | 见下 |

**安装 pyzmq / pymysql / DBUtils 到 QMT 的 Python：**

QMT 的 Python 3.6 用旧 OpenSSL，pip 直连 HTTPS 镜像会报 SSL 错误。推荐从开发机拷贝纯 Python 包（pyzmq 有 C 扩展需对应版本，pymysql/DBUtils 是纯 Python 可直接拷）：

```powershell
# 方法 A：拷贝纯 Python 包（pymysql / DBUtils，推荐）
# 在开发机（已装这些包）执行：
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

> pyzmq 包含 C 扩展，Python 3.6 需装 `pyzmq==19.0.2`（最后一个支持 3.6 的版本）。如果 SSL 装不上，可下载对应 wheel 手动 `pip install xxx.whl`。

### 客户端（外部程序）

客户端用你的开发 Python（3.8+ 推荐）：

```powershell
pip install redis          # redis 传输必需
pip install pyzmq          # zmq 传输（可选）
pip install pymysql DBUtils  # mysql 传输（可选）
```

---

## 快速开始

### 第 1 步：同步代码到 QMT 的 python 目录

把以下内容复制到大 QMT 的 `python` 目录（如 `D:\国金证券QMT交易端\python\`）：

```
src/bigqmt_signal_trader/          （整个核心包，含 transports/）
src/bigqmt_signal_trader_strategy.py
src/bigqmt_signal_trader_redis_rpc_runtime.py
src/BIGQMT_REDIS_DRYRUN.py         （★ QMT 编辑器入口，GBK 编码，在 QMT 里加载这个）
```

> **在 QMT 策略编辑器里只加载 `BIGQMT_REDIS_DRYRUN.py` 一个文件**。它会自动 import 上面其余文件。其余 `.py`（`bigqmt_signal_trader_*`）是它依赖的模块，不是直接运行的入口。

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

### 第 3 步：在 QMT 里运行策略（BIGQMT_REDIS_DRYRUN.py）

**入口文件是 `src/BIGQMT_REDIS_DRYRUN.py`**（GBK 编码，QMT 友好）。在 QMT 策略编辑器加载并运行它。

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

> **为什么不直接用 `bigqmt_signal_trader_redis_rpc_runtime.py`？** 那个文件是纯逻辑入口，不包含 reload 和 QMT API 绑定。`BIGQMT_REDIS_DRYRUN.py` 是给 QMT 编辑器专用的外壳，处理了 QMT 进程不退出导致模块缓存、API 绑定等坑。在 QMT 里**只加载 `BIGQMT_REDIS_DRYRUN.py`**。

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
├── xtquant_compat.py              客户端兼容层（xt_trader / xtdata）
├── full_tick_cache.py             全市场行情快照缓存（可选降载）
├── strategy.py 之类               策略骨架、风控、价格引擎等
src/xtquant/                       可选 xtquant import shim
src/bigqmt_signal_trader_strategy.py        策略入口（init/handlebar/adjust）
src/bigqmt_signal_trader_redis_rpc_runtime.py  Redis RPC runtime 入口
src/BIGQMT_REDIS_DRYRUN.py                  QMT 编辑器加载入口（GBK）
src/BIGQMT_ZMQ_BACKTEST.py                  独立 QMT 回测 ZMQ 入口（GBK）
src/bigqmt_backtest/                        独立历史驱动、模拟撮合、ZMQ 协议与客户端
tests/bigqmt_signal_trader/        单元测试（无 QMT 环境可跑）
tests/bigqmt_backtest/             回测、确定性、隔离和 ZMQ 往返测试
docs/                              详细文档
bench_latency.py / bench_transports.py  延迟基准脚本
```

---

## 本地测试

```powershell
python -m pytest tests/bigqmt_signal_trader/ -q
```

当前覆盖 **77 个用例**（含传输层往返、Redis RPC、客户端兼容、持仓/行情/下单 handlers）。

---

## 安全默认值

- `rpc_allow_order_methods` 默认 `False`：远程 `order_stock` / `cancel_order` 被拒绝。确认接入方、账号、风控后再显式开启。
- 回测桥接永久 `live_ready=false`，协议中没有真实账户和实盘下单方法。
- 配置文件含资金账号和密码，`bigqmt_signal_trader_local_config.py` / `bigqmt_signal_trader_client_config.py` 已在 `.gitignore`，**不要提交**。
- 请求负载经过 base64 + 数字混淆编码（`encode_rpc_request_payload`），避免 QMT 的 Redis 客户端拦截含股票代码的明文。

---

## 相关文档

- [docs/RPC_API_REFERENCE.md](docs/RPC_API_REFERENCE.md) — **全部 RPC 方法参考**（参数、返回值、别名、大 QMT 能力边界）
- [docs/BIG_QMT_REDIS_RPC.md](docs/BIG_QMT_REDIS_RPC.md) — Redis RPC 协议与入口脚本详解
- [docs/RPC_TRANSPORTS.md](docs/RPC_TRANSPORTS.md) — 可插拔传输层完整说明
- [docs/XTQUANT_COMPAT_REPLACEMENT.md](docs/XTQUANT_COMPAT_REPLACEMENT.md) — 用兼容层替换旧 xtquant 的步骤
- [docs/BIG_QMT_SIGNAL_TRADER_RUNBOOK.md](docs/BIG_QMT_SIGNAL_TRADER_RUNBOOK.md) — 信号交易运行手册
- [docs/ZMQ_BACKTEST_BRIDGE.md](docs/ZMQ_BACKTEST_BRIDGE.md) — 独立 ZMQ 回测协议、撮合规则和 QMT 入口

---

## 为什么不直接连大 QMT

官方 `xtquant.xttrader.XtQuantTrader` 依赖客户端侧 XtQuantServer 通道。当前国金大 QMT 环境中直接连 `connect()` 返回 `-1`；大 QMT 的 `58600` 端口是 FormulaServer 不是行情服务。

因此本仓库把真实接口调用放在大 QMT 内部策略进程，外部通过 RPC 驱动。如果后续券商开通 XtQuantServer 权限且 `connect()==0`，可再加直连模式。
