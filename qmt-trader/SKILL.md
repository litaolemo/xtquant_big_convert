---
name: qmt-trader
description: "通过统一 CLI 脚本驱动大 QMT 迅投量化交易端的全部能力，含实时行情查询、K线历史数据、账户资产与持仓查询、委托与成交查询、买入卖出下单、撤单、板块龙虎榜北向资金财务数据等，并内置 xtquant_big_convert 桥接服务的安装部署引导（装包/同步 QMT 端文件/配置/启动验证/排错）。适用于大模型辅助量化交易分析、行情研判、持仓监控、半自动下单等场景。当用户需要查看股票行情、分析K线、查询持仓资产、查看今日委托成交、下单买卖、撤单、查询北向资金龙虎榜财务数据，或需要安装部署 QMT RPC 桥接服务时触发此 skill。"
---

# QMT Trader — 大模型驱动的 QMT 交易/行情工具

## 概述

本 skill 提供一个确定性 CLI 脚本 `scripts/qmt.py`，让大模型通过命令行调用大 QMT 的全部
交易与行情能力，避免每次现场写 Python 代码。所有命令默认输出 JSON（便于解析），加 `--table`
切换人类可读表格。

**前置条件**：本 skill 依赖 xtquant_big_convert 桥接服务已部署运行。若 `ping` 失败或用户尚未部署，
先按下文「首次部署」引导完成：装包 → 同步 QMT 端文件 → 写配置 → QMT 里运行入口 → 验证。

## 首次部署（只需一次，AI 逐步引导用户完成）

部署分两端：**客户端**（跑本 skill/策略的开发机）和**服务端**（大 QMT 客户端内置 Python）。

### 第 1 步：客户端安装包

```bash
pip install "xtquant-big-convert[redis]"   # redis 传输（默认，推荐）
# 或 zmq 同机低延迟：pip install xtquant-big-convert（基础版已含 pyzmq）
```

> 没发布到 PyPI 的私有 fork 用源码安装：`git clone <repo> && cd xtquant_big_convert && pip install -e .[redis]`

### 第 2 步：把服务端文件同步到 QMT 的 python 目录

需要拷 4 项到大 QMT 的 `python` 目录（如 `D:\国金证券QMT交易端\python\`）：

```
bigqmt_signal_trader/                  （整个包，pip 装的在 site-packages 里）
bigqmt_signal_trader_strategy.py
bigqmt_signal_trader_redis_rpc_runtime.py
BIGQMT_REDIS_DRYRUN.py                 （★ QMT 编辑器入口，GBK 编码）
```

pip 安装后的文件位置可以用这条命令定位（输出目录里就有全部 4 项）：

```bash
python -c "import bigqmt_signal_trader_strategy as m, os; print(os.path.dirname(m.__file__))"
```

> QMT 沙箱若拒绝 `import redis`（部分券商白名单拦截），改用仓库里的 `bigqmt_no_redis/` 无 redis 版本（自包含 ZMQ 传输）。

### 第 3 步：创建 QMT 端私有配置

在 QMT 的 `python` 目录创建 `bigqmt_signal_trader_local_config.py`（含账号密码，**不要提交 git**）：

```python
# coding: utf-8
BIGQMT_ACCOUNT_ID = "资金账号"
BIGQMT_REDIS_CONFIG = {
    "host": "Redis地址", "port": 6379, "db": 5, "password": "Redis密码",
    "rpc_allow_order_methods": False,     # 下单开关，默认关闭；确认风控后改 True
    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    "rpc_background_threads": True,       # redis 用后台收包线程最快；zmq/pipe 要改 False
    "schedule_adjust": True,
    "schedule_adjust_interval": "100nMilliSecond",
}
```

> **这个开关按传输选，选反了差几十倍**（实测见 README 的传输对比表）：
> redis 用 `True`（3.4ms），zmq / pipe 用 `False` 走 adjust drain（zmq 15.8ms）。
> zmq 配 `True` 是 592.9ms —— 慢 37 倍。原因是 zmq / pipe 的后台线程每次都要付
> 跨线程 GIL 交接（约一个 adjust tick），redis 的 `brpop` 唤醒没有这一步。
>
> 切 zmq：配置里加 `"transport": "zmq"` 并把 `rpc_background_threads` 改 `False`
> （QMT 端需装 pyzmq 19.0.2，Python 3.6 最后支持的版本）。

### 第 4 步：在 QMT 策略编辑器运行入口

QMT 策略编辑器里**只加载运行 `BIGQMT_REDIS_DRYRUN.py` 一个文件**（它自动 import 其余模块）。
装在非默认路径不用改任何代码：入口先用自己 `__file__` 所在目录，取不到才回落
到扫 `sys.path`。（旧版文档教人改 `_known_qmt_python_dir()` 的写死路径，
那会让你多背一个源码补丁，现在不需要了。）

启动成功标志（QMT 输出面板）：

```
[bigqmt_shell] local redis config loaded keys=[...]
[bigqmt_shell] local account config loaded=True
[bigqmt_rpc] started channel=bigqmt:rpc:req:你的账号
[bigqmt_signal_trader] init ok
```

### 第 5 步：客户端配置 + 验证

客户端用环境变量（或 `bigqmt_signal_trader_client_config.py`）指向同一套 Redis/账号：

```powershell
$env:BIGQMT_ACCOUNT_ID="资金账号"
$env:BIGQMT_REDIS_HOST="Redis地址"; $env:BIGQMT_REDIS_PORT="6379"
$env:BIGQMT_REDIS_DB="5"; $env:BIGQMT_REDIS_PASSWORD="Redis密码"
```

然后验证（redis ~3ms / zmq+drain ~16ms 为正常，实测口径见 README 传输对比表）：

```bash
python scripts/qmt.py ping
```

### 部署排错速查

| 现象 | 排查 |
|------|------|
| `ping` 超时 | 客户端/服务端 transport 不一致（一边 redis 一边 zmq）；QMT 端服务没启动；Redis 地址/密码/db 不一致 |
| QMT 面板报 `import redis` 被拒 | 换 `bigqmt_no_redis/` 无 redis 版本 |
| 启动了但查询全空 | 账号没对上：服务端 `BIGQMT_ACCOUNT_ID` vs 客户端 `BIGQMT_ACCOUNT_ID`；QMT 需在实盘模式 |
| 下单报 `ORDER_DISABLED` | 正常保护，服务端配置 `rpc_allow_order_methods` 改 `True` 才放行 |
| 详细错误日志 | QMT python 目录下 `logs/bigqmt_*.log`（保留 7 天），排错首选 |

## 快速开始

### 第 0 步：确认连通性

```bash
python scripts/qmt.py ping
```

返回 `ok: true` 且 `latency_ms` 合理（redis ~3ms / zmq+drain ~16ms）即表示服务端就绪。

### 第 1 步：一键快照（资产+持仓+委托+成交）

```bash
python scripts/qmt.py snapshot
```

一次 RPC 往返返回账户全景，适合快速了解当前状态。

## 命令速查

### 行情分析

| 命令 | 用途 | 示例 |
|------|------|------|
| `tick <codes...>` | 实时五档盘口 | `tick 600000.SH 000001.SZ` |
| `kline <code>` | K线/历史行情 | `kline 600000.SH --period 1d --count 60 --dividend front` |
| `instrument <code>` | 合约详情 | `instrument 600000.SH` |
| `sector [name]` | 板块成分股/板块列表 | `sector "沪深A股"` |
| `trading-dates` | 交易日历 | `trading-dates --count 10` |
| `north` | 北向资金 | `north --period 1d` |
| `longhubang <code>` | 龙虎榜 | `longhubang 600000.SH --count 5` |
| `financial <codes...>` | 财务数据 | `financial 000001.SZ --tables Capital.CAPITAL` |
| `download <codes...>` | 下载历史数据 | `download 600654.SH --period 1d --dividend front` |
| `quote-subscribe <codes...>` | 实时全推订阅 | `quote-subscribe SH SZ --max 10` |

### 账户/持仓/委托

| 命令 | 用途 | 示例 |
|------|------|------|
| `account` | 账户资产 | `account` |
| `positions [code]` | 持仓列表 | `positions` / `positions 600000.SH` |
| `orders` | 今日委托 | `orders --cancelable` |
| `trades` | 今日成交 | `trades` |
| `snapshot` | 一键全景 | `snapshot` |

### 下单/撤单

| 命令 | 用途 | 示例 |
|------|------|------|
| `buy <code> <volume>` | 买入 | `buy 600000.SH 100 --price 7.50` |
| `sell <code> <volume>` | 卖出 | `sell 600000.SH 100 --price 7.50` |
| `cancel <order_id>` | 撤单 | `cancel 12345 --market SH` |

> 下单命令支持 `--dry-run`（只打印不下单）、`--latest`（最新价）、`--strategy`、`--remark`。

### 扩展查询（高频）

| 命令 | 用途 | 示例 |
|------|------|------|
| `holiday` | 节假日列表 | `holiday` |
| `stock-name <code>` | 股票名称 | `stock-name 600000.SH` |
| `instrument-type <code>` | 品种类型 | `instrument-type 600000.SH` |
| `divid-factors <code>` | 除权除息因子 | `divid-factors 600000.SH` |
| `market-times [market]` | 日内交易时段 | `market-times SH` |
| `trading-calendar [market]` | 交易日历(含时段) | `trading-calendar SH` |
| `option-list <code>` | 期权列表 | `option-list 510050.SH` |
| `option-greeks <code>` | 本地 IV + Delta/Gamma/Vega/Theta/Rho | `option-greeks 10010975.SHO` |
| `option-greeks <underlying> --expiry <yyyymm>` | 整条到期月份 Greeks | `option-greeks 510050.SH --expiry 202609` |
| `bsm-price ...` | BSM 期权定价 | `bsm-price C 3.0 2.8 0.03 0.3 30` |
| `bsm-iv ...` | BSM 隐含波动率 | `bsm-iv C 3.0 2.8 0.25 0.03 30` |
| `hkt-stats <code>` | 港股通统计 | `hkt-stats 600000.SH` |
| `hkt-details <code>` | 港股通明细 | `hkt-details 600000.SH` |
| `hkt-rate` | 港股通汇率 | `hkt-rate` |
| `top10-holder <code>` | 十大股东 | `top10-holder 600000.SH` |
| `holder-num <code>` | 股东户数 | `holder-num 600000.SH` |
| `ipo` / `ipo-limit` | 新股数据/申购额度 | `ipo` |
| `credit-assure` | 融资担保品合约 | `credit-assure` |
| `credit-short` | 融券标的合约 | `credit-short` |
| `credit-debt` | 负债合约 | `credit-debt` |
| `his-st <code>` | 历史 ST 数据 | `his-st 600000.SH` |
| `index-weight <index>` | 指数权重 | `index-weight 000300.SH` |
| `industry <name>` | 行业成分 | `industry 银行` |
| `sector-info [name]` | 板块详情 | `sector-info 沪深A股` |
| `local-data <code>` | 本地缓存数据 | `local-data 600000.SH` |
| `timetag2dt <ms>` | 毫秒时间戳转日期 | `timetag2dt 1751353200000` |
| `dt2timetag <dt>` | 日期转毫秒时间戳 | `dt2timetag 20250701150000` |

### 通用 RPC（兜底所有方法）

`rpc <method> [json_params]` 可调用**任意白名单方法**（含未列出的，如 `get_l2_quote` / `call_formula` / `get_raw_financial_data` 等）：

```bash
python scripts/qmt.py rpc get_holidays
python scripts/qmt.py rpc get_stock_name '{"stock":"600000.SH"}'
python scripts/qmt.py rpc get_l2_quote '{"stock_code":"600000.SH","count":5}'
python scripts/qmt.py rpc call_formula '{"formula_name":"MA","stock_code":"600000.SH","period":"1d"}'
```

## 典型工作流

### 场景一：行情分析

分析某只股票的技术面：

```bash
# 1. 看实时盘口
python scripts/qmt.py tick 600000.SH

# 2. 拉最近 60 根日 K（前复权），输出含 MA5/MA20/MA60 统计
python scripts/qmt.py kline 600000.SH --period 1d --count 60 --dividend front

# 3. 看合约详情（名称、上市日、最小变动价位等）
python scripts/qmt.py instrument 600000.SH

# 4. 看近期龙虎榜
python scripts/qmt.py longhubang 600000.SH --count 5
```

### 场景二：持仓监控

```bash
# 一键看全景
python scripts/qmt.py snapshot

# 只看持仓（含浮动盈亏）
python scripts/qmt.py positions

# 看可撤委托
python scripts/qmt.py orders --cancelable
```

### 场景三：下单交易

```bash
# 0. 先看当前价
python scripts/qmt.py tick 600000.SH

# 1. 干跑确认参数
python scripts/qmt.py buy 600000.SH 100 --price 7.50 --dry-run

# 2. 真实下单（限价 7.50 买 100 股）
python scripts/qmt.py buy 600000.SH 100 --price 7.50 --strategy my_strat

# 3. 确认委托进了系统
python scripts/qmt.py orders

# 4. 需要时撤单
python scripts/qmt.py cancel <order_sysid> --market SH
```

### 场景四：批量行情分析

```bash
# 同时看多只股票的盘口
python scripts/qmt.py tick 600000.SH 000001.SZ 600519.SH

# 看板块成分股
python scripts/qmt.py sector "沪深A股"

# 看北向资金流向
python scripts/qmt.py north
```

## 安全须知

1. **下单默认关闭**：服务端 `rpc_allow_order_methods` 默认 `False`。必须由人工在服务端配置中
   显式开启后才能下单，否则 `buy`/`sell`/`cancel` 会报 `ORDER_DISABLED` 错误。

2. **下单前先看价**：始终先用 `tick` 确认当前价格，避免下出明显不合理的委托。

3. **超时防重复**：如果 `buy`/`sell` 报 `ORDER_TIMEOUT`，委托可能已提交。**先用 `orders` 查询确认**，
   不要直接重试，避免重复下单。

4. **strategy_name 一致性**：下单时的 `--strategy` 和查询时的 `--strategy` 必须一致。
   查全部委托用 `orders --strategy ""`（空字符串=不过滤）。

5. **实盘模式**：QMT 必须运行在实盘模式（非模拟/模型交易）才能收到完整回报。

## 脚本说明

### scripts/qmt.py

统一 CLI 入口，包含以下子命令：

**基础查询**：
- `ping` — 连通性检测（含延迟测量）
- `account` — 查询账户资产（现金/冻结/总资产/市值）
- `positions [code]` — 查询持仓（含浮动盈亏计算）
- `orders [--cancelable] [--strategy ""]` — 查询今日委托（含语义化状态名）
- `trades [--strategy ""]` — 查询今日成交
- `snapshot` — 一键全景（资产+持仓+委托+成交）

**行情**：
- `tick <codes...>` — 实时五档盘口（含涨跌幅计算）
- `kline <code> [--period 1d] [--count N] [--dividend front]` — K线（含 MA5/20/60 统计）
- `instrument <code>` — 合约详情
- `sector [name]` — 板块成分股/板块列表
- `trading-dates [--count N]` — 交易日历
- `north [--period 1d]` — 北向资金
- `longhubang <code> [--count N]` — 龙虎榜
- `financial <codes...> [--tables T1,T2]` — 财务数据
- `download <codes...>` — 下载历史数据到服务端
- `quote-subscribe <codes...> [--max N] [--timeout S]` — 实时全推行情订阅

**扩展查询**：
- `holiday` — 节假日列表
- `stock-name <code>` — 股票名称
- `instrument-type <code>` — 品种类型
- `divid-factors <code>` — 除权除息因子
- `market-times [market]` — 日内交易时段
- `trading-calendar [market]` — 交易日历（含时段）
- `option-list <code>` — 期权列表
- `option-greeks <option_code>` — 从合约元数据和最新 close 本地计算 IV、Delta/Gamma/Vega/Theta/Rho；可传 `--option-price` / `--underlying-price` 使用盘口中间价
- `option-greeks <underlying> --expiry <yyyymm>` — 批量计算整条到期月份；坏价保留为逐合约 `analytics_error`
- `bsm-price` / `bsm-iv` — BSM 期权定价/隐含波动率
- `hkt-stats` / `hkt-details` / `hkt-rate` — 港股通统计/明细/汇率
- `top10-holder <code>` / `holder-num <code>` — 十大股东/股东户数
- `ipo` / `ipo-limit` — 新股数据/申购额度
- `credit-assure` / `credit-short` / `credit-debt` — 融资融券查询
- `his-st <code>` — 历史 ST 数据
- `index-weight <index>` — 指数权重
- `industry <name>` — 行业成分
- `sector-info [name]` — 板块详情
- `local-data <code>` — 本地缓存数据
- `timetag2dt` / `dt2timetag` — 时间戳转换

**交易**：
- `buy <code> <volume> [--price P] [--latest]` — 买入下单
- `sell <code> <volume> [--price P] [--latest]` — 卖出下单
- `cancel <order_id> [--market SH]` — 撤单

**通用兜底**：
- `rpc <method> [json_params]` — 调用任意白名单方法（未列出的方法都能这样调）

**配置自动发现**：脚本会自动把仓库 `src/` 加入 `sys.path`（开发模式直接运行，无需 pip install），并自动发现 QMT 的 python 目录（读 `local_config.py` 里的 transport 配置）。配置从环境变量（`BIGQMT_ACCOUNT_ID`/`BIGQMT_REDIS_HOST` 等）或配置文件读取。

**输出格式**：默认 JSON（`ok`/`data`/`ts` 三字段），加 `--table` 切换表格输出。错误返回 `ok: false` + `error`/`detail`/`code`，退出码 1。

## 参考

详细的 API 参数、返回值结构、常量定义和已知陷阱见 `references/api_reference.md`。
当命令速查不够用时（如需要直接 RPC 调用、查看信用交易类型、了解回调系统等），查阅该文件。
