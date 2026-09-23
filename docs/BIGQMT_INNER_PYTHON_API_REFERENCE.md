# QMT 内置 Python API 参考文档（精简版）

> 依据迅投官方文档《内置Python》章节整理（https://dict.thinktrader.net/innerApi/ ）。本版仅保留 Python API 相关内容：运行机制、变量约定、数据结构、系统/行情/交易/回调/引用/绘图函数、枚举常量与典型用法；界面操作、安装与故障排查等非 API 内容已删除，官方文档中影响使用的笔误已加注说明。
>
> 整理日期：2026-08-20

## 目录

1. 运行机制与策略骨架
2. 变量约定（代码 / 周期 / 复权 / 账号类型 / ContextInfo 属性）
3. 数据结构（行情对象与交易对象字段表）
4. 系统函数（生命周期 / 定时器 / 板块）
5. 行情函数（下载 / 行情 / 财务 / 合约 / 期权 / 除权 / 成分股 / 交易日）
6. 交易函数（下单 / 撤单 / 查询 / 两融 / 回测专用）
7. 成交回报实时主推函数（回调）
8. 引用函数（扩展数据 / 因子 / VBA）
9. 绘图函数
10. 枚举常量（opType / orderType / prType / quickTrade 等）
11. 典型示例速查
12. 行情数据概念与关键行为说明

---

## 1. 运行机制与策略骨架

### 1.1 三种运行机制

| 机制 | 分类 | 特点 | 匹配需求 |
| --- | --- | --- | --- |
| 逐 K 线运行（handlebar） | 事件驱动 | 支持历史回测与盘中模拟逐 K 线效果 | 实盘中模拟逐 K 线运行 |
| 订阅推送（subscribe） | 事件驱动 | 盘中行情分笔触发回调 | 盘中随分笔行情判断交易 |
| 定时运行（run_time / schedule_run） | 定时任务 | 固定间隔触发回调 | 盘中固定时间间隔判断交易 |

- **handlebar**：运行开始时主图历史 K 线从左向右每根触发一次 `handlebar`；盘中主图品种每个新分笔（tick）到达触发一次（股票分笔约 3 秒一个）。盘中分笔驱动但逐 K 线生效（见 1.3）。非交易时段 handlebar 也可能被调用（行情服务重启后补推最新数据），策略可按交易时间过滤。
- **subscribe**：`ContextInfo.subscribe_quote` / `subscribe_whole_quote` 订阅指定品种，新分笔到达触发指定回调函数。
- **定时**：`ContextInfo.run_time`（旧版）/ `ContextInfo.schedule_run`（新版），固定间隔持续触发回调。

### 1.2 策略生命周期

系统按以下顺序调用用户定义的函数（均为系统函数，不可被手动调用）：

1. `init(C)`：整个策略开始时调用一次，用于初始订阅行情、账号信息。init 执行完成前部分接口不可用（如 `get_trading_dates`）；`get_market_data_ex` 等 gmd 系列函数不建议在 init 中运行（此时仅能取到本地数据）。
2. `after_init(C)`：init 完成后、handlebar 之前调用一次，适合放一次性触发的下单、取数操作；init 里不支持的函数可放这里。
3. `handlebar(C)`：每根 K 线一次；实时行情下先逐根历史 K 线触发，再每个 tick 驱动一次。
4. `stop(C)`：策略停止前调用。注意 stop 被调用时交易连接已断开，不能在其中报单/撤单。

其他约束：

- 脚本首行必须写 `#coding:gbk`；缩进需统一（全部 4 空格或全部 Tab）。
- 在策略交易（模型交易）界面运行时，全局变量 `account` / `accountType` 自动赋值为策略配置的账号与账号类型；编辑器界面运行需手动赋值，且编辑器里执行的下单函数不会产生实际委托。
- 客户端勾选「独立 python 进程」后，代码作为 main 脚本执行，**不会触发 init、handlebar 等函数**。
- QMT 内 Python 无法使用多线程/多进程，且所有策略在同一线程中执行，策略中应避免阻塞写法（死循环/sleep/加锁），否则会影响所有策略。需要多线程/多进程时使用极简模式配合 xtquant 库。
- 回测必须以副图模式执行；回测/模拟信号模式下 `passorder` 不产生实际委托。

### 1.3 ContextInfo 逐 K 线保存机制（重要）

`ContextInfo` 由底层维护并传给 init/handlebar 等系统函数，做了逐 K 线更新设计：

- 同一根 bar 内 `ContextInfo` 本质上是同一个变量；每次 `handlebar` 调用前会对其做深拷贝，若下一分笔不是新 K 线的第一个分笔，则对象被回退为之前深拷贝的版本。即：**只有 K 线结束的最后一个分笔触发的 handlebar 中对 ContextInfo 的修改才会保留**，其余分笔的修改全部丢弃。
- 影响一：在 ContextInfo 中存数据会导致每次分笔到达都被深拷贝，拖慢策略运行。
- 影响二：ContextInfo 适合记录逐 K 线生效的交易信号（`quickTrade=0`）；立即下单（`quickTrade=2`）的委托状态必须用普通全局变量保存，不能存在 ContextInfo 属性里。

推荐用法（自定义全局对象保存状态）：

```python
class G(): pass
g = G()

def init(C):
    g.stock_list = ['000001.SZ']

def handlebar(C):
    g.stock_list.append('600000.SH')   # ✅ 修改保留
    # C.stock_list.append(...)         # ❌ 修改会在下一分笔回滚
```

### 1.4 quickTrade（快速交易）语义

`passorder` 的 `quickTrade` 参数默认 `0`：

| 取值 | 行为 |
| --- | --- |
| 0 | 只在 K 线结束的分笔时调用才产生有效信号，其他调用不产生信号（日线及以上周期等于全天不委托） |
| 1 | 当前 K 线为最新 K 线（`is_last_bar()` 为 True）时调用即产生信号，历史 K 线不产生 |
| 2 | 任何情况调用都立即产生信号，不判断 bar 状态，历史 bar 上也会下单，谨慎使用 |

- 在定时器回调、行情回调、`after_init` 中调用下单函数必须传 `2`，确保不漏单。
- `passorder` 以外的下单函数不能指定 quickTrade，效果与传 `0` 一致。
- 场景建议：handlebar 逐 K 线下单传 0；handlebar 盘中触发立即下单传 1；定时器/after_init/各类回调内下单传 2。通常不建议传 2（历史 bar 重放会重复下单）。

### 1.5 撮合与交易规则

- **回测撮合**：指定价格在当前 K 线高低点间按指定价撮合；超过高低点按当根收盘价撮合；委托量大于可用量按可用量撮合。
- **实盘**：以交易所为准。股票价格超 2% 价格笼子废单；数量超可用数量废单。
- **下单异步**：交易接口是异步的，下单函数调用后立即返回，不等待委托回报、不阻塞线程。
- **回报与查询为本地缓存**：委托/成交/持仓/账号信息在客户端后台更新，`get_trade_detail_data` 与交易回调函数均读取本地缓存（有交易主推的柜台约 50ms 刷新一次，无主推的 1-6 秒一次），**不能认为查询结果与柜台完全一致**——如卖出委托后立刻查询，查不到对应委托、可用资金也不会变多。
- **委托状态管理建议**：实盘策略需自行维护委托状态。常见做法：用全局字典保存，每笔委托用独立「投资备注」（userOrderId）作 key、状态作 value；下单后默认置「待报」，查到委托后更新；某品种存在待报状态委托时暂停该品种后续报单，防止超单。
- 交易所委托数量规则：科创板连续交易限价单笔最大 10 万股、市价 5 万股、盘后定价 100 万股，200 股起 1 股递增；创业板限价 30 万股、市价 15 万股，100 股起 100 股递增；主板（6/0 开头）单笔最大 100 万股，100 股起 100 股递增。

### 1.6 策略骨架示例

#### 回测（handlebar + 本地数据，双均线）

```python
#coding:gbk
import numpy as np

def init(C):
    C.stock = C.stockcode + '.' + C.market        # 主图品种
    C.line1, C.line2 = 10, 20                    # 快/慢均线期数
    C.accountid = "testS"                        # 回测资金账号可填任意字符串

def handlebar(C):
    bar_date = timetag_to_datetime(C.get_bar_timetag(C.barpos), '%Y%m%d%H%M%S')
    # 回测用本地数据（subscribe=False）更快；多品种回测需先下载对应周期历史数据
    local_data = C.get_market_data_ex(['close'], [C.stock], end_time=bar_date,
                                      period=C.period, count=max(C.line1, C.line2), subscribe=False)
    close_list = list(local_data[C.stock].iloc[:, 0])
    if len(close_list) < 1:
        print(bar_date, '行情不足 跳过')
    line1_mean = round(np.mean(close_list[-C.line1:]), 2)
    line2_mean = round(np.mean(close_list[-C.line2:]), 2)
    account = get_trade_detail_data('test', 'stock', 'account')[0]
    available_cash = int(account.m_dAvailable)
    holdings = get_trade_detail_data('test', 'stock', 'position')
    holdings = {i.m_strInstrumentID + '.' + i.m_strExchangeID: i.m_nVolume for i in holdings}
    holding_vol = holdings.get(C.stock, 0)
    if holding_vol == 0 and line1_mean > line2_mean:            # 金叉买入 8 成仓
        vol = int(available_cash / close_list[-1] / 100) * 100
        passorder(23, 1101, C.accountid, C.stock, 5, -1, vol, C)
        C.draw_text(1, 1, '开')
    elif holding_vol > 0 and line1_mean < line2_mean:           # 死叉全平
        passorder(24, 1101, C.accountid, C.stock, 5, -1, holding_vol, C)
        C.draw_text(1, 1, '平')
```

#### 实盘（handlebar + 全局变量存状态，双均线）

```python
#coding:gbk
import numpy as np, datetime

class a(): pass
A = a()                                          # 全局对象保存委托状态（不能存 ContextInfo）

def init(C):
    A.stock = C.stockcode + '.' + C.market
    A.acct = account                             # 模型交易界面选择的账号/类型（自动注入）
    A.acct_type = accountType
    A.amount = 10000                             # 单笔买入金额
    A.line1, A.line2 = 17, 27
    A.waiting_list = []                          # 未查到委托列表，存在未查到委托时暂停报单防超单
    A.buy_code = 23 if A.acct_type == 'STOCK' else 33   # 区分股票与两融账号的买卖代码
    A.sell_code = 24 if A.acct_type == 'STOCK' else 34

def handlebar(C):
    if not C.is_last_bar():                      # 跳过历史 K 线
        return
    now_time = datetime.datetime.now().strftime('%H%M%S')
    if now_time < '093000' or now_time > '150000':
        return
    account_ = get_trade_detail_data(A.acct, A.acct_type, 'account')
    if len(account_) == 0:
        print(f'账号{A.acct} 未登录 请检查'); return
    available_cash = int(account_[0].m_dAvailable)
    if A.waiting_list:                           # 用投资备注对账，确认委托已可查
        deals = get_trade_detail_data(A.acct, A.acct_type, 'deal')
        found = [d.m_strRemark for d in deals if d.m_strRemark in A.waiting_list]
        A.waiting_list = [i for i in A.waiting_list if i not in found]
    if A.waiting_list:
        print(f"当前有未查到委托 {A.waiting_list} 暂停后续报单"); return
    holdings = get_trade_detail_data(A.acct, A.acct_type, 'position')
    holdings = {i.m_strInstrumentID + '.' + i.m_strExchangeID: i.m_nCanUseVolume for i in holdings}
    data = C.get_market_data_ex(["close"], [A.stock], period='1d', count=max(A.line1, A.line2)+1)
    close_list = data[A.stock].values
    if len(close_list) < max(A.line1, A.line2)+1:
        print('行情长度不足(新上市或最近有停牌) 跳过运行'); return
    pre1, pre2 = np.mean(close_list[-A.line1-1:-1]), np.mean(close_list[-A.line2-1:-1])
    cur1, cur2 = np.mean(close_list[-A.line1:]), np.mean(close_list[-A.line2:])
    vol = int(A.amount / close_list[-1] / 100) * 100
    if A.amount < available_cash and vol >= 100 and A.stock not in holdings \
       and pre1 < pre2 and cur1 > cur2:          # 金叉买入，立即下单 quickTrade=2
        msg = f"双均线实盘 {A.stock} 上穿均线 买入 {vol}股"
        passorder(A.buy_code, 1101, A.acct, A.stock, 14, -1, vol, '双均线实盘', 2, msg, C)
        A.waiting_list.append(msg)
    if A.stock in holdings and holdings[A.stock] > 0 and pre1 > pre2 and cur1 < cur2:
        msg = f"双均线实盘 {A.stock} 下穿均线 卖出 {holdings[A.stock]}股"
        passorder(A.sell_code, 1101, A.acct, A.stock, 14, -1, holdings[A.stock], '双均线实盘', 2, msg, C)
        A.waiting_list.append(msg)
```

#### 事件驱动（subscribe）

```python
#coding:gbk
class a(): pass
A = a()
A.bought_list = []
account = 'testaccount'

def init(C):
    # 回调函数在 init 中定义可闭包引用 C（下单函数需要 ContextInfo）
    def callback_func(data):
        for stock in data:
            ratio = data[stock]['close'] / data[stock]['preClose'] - 1
            print(stock, C.get_stock_name(stock), '当前涨幅', ratio)
            if ratio > 0 and stock not in A.bought_list:
                # passorder(23, 1101, account, stock, 5, -1, 100, '订阅下单示例', 2, msg, C)
                A.bought_list.append(stock)
    for stock in ['600000.SH', '000001.SZ']:
        C.subscribe_quote(stock, period='1d', callback=callback_func)
```

#### 定时任务（run_time）

```python
#coding:gbk
import time, datetime

class a(): pass
A = a()

def init(C):
    A.hsa = C.get_stock_list_in_sector('沪深A股')
    A.vol_dict = {s: C.get_last_volume(s) for s in A.hsa}
    A.bought_list = []
    C.run_time("f", "1nSecond", "2019-10-14 13:20:00")   # 历史时间=立即启动，此后每秒触发

def f(C):
    t0 = time.time()
    full_tick = C.get_full_tick(A.hsa)
    total_mv, total_ratio = 0, 0
    for stock in A.hsa:
        ratio = full_tick[stock]['lastPrice'] / full_tick[stock]['lastClose'] - 1
        if ratio > 0.09 and stock not in A.bought_list:
            # passorder(23, 1101, account, stock, 5, -1, 100, '示例策略', 2, msg, C)
            A.bought_list.append(stock)
        mv = full_tick[stock]['lastPrice'] * A.vol_dict[stock]
        total_ratio += ratio * mv
        total_mv += mv
    print(f"{datetime.datetime.now()} A股加权涨幅 {round(total_ratio/total_mv*100, 2)}% "
          f"耗时{round(time.time()-t0, 5)}秒")
```

---

## 2. 变量约定

### 2.1 函数命名规则

- `get_` 开头：数据来源于客户端内存。
- `query_` 开头：向服务器查询（异步，需配合回调）。

### 2.2 账号类型（strAccountType 取值）

| 取值 | 说明 |
|---|---|
| `'FUTURE'` | 期货账号 |
| `'STOCK'` | 股票账号 |
| `'CREDIT'` | 信用账号 |
| `'FUTURE_OPTION'` | 期货期权 |
| `'STOCK_OPTION'` | 股票期权 |
| `'HUGANGTONG'` | 沪港通 |
| `'SHENGANGTONG'` | 深港通 |

注：部分接口（如 `get_trade_detail_data`、`cancel`）示例中也使用小写 `'stock'`/`'future'` 等（官方示例两种写法混用）。

### 2.3 symbol_code 代码表示

总格式：「交易标的代码.市场代码」，如 `000001.SZ`，不区分大小写（期货 symbol 除外）。

| 交易所 | 迅投简称 | 显示后缀 |
|---|---|---|
| 上海证券交易所 | SH | SH |
| 深圳证券交易所 | SZ | SZ |
| 北京证券交易所 | BJ | BJ |
| 香港证券交易所 | HK | HK |
| 沪港通 | HGT | HGT |
| 深港通 | SGT | SGT |
| 中国金融期货交易所 | IF | CFFEX |
| 上海期货交易所 | SF | SHFE |
| 大连商品交易所 | DF | DCE |
| 郑州商品交易所 | ZF | CZCE |
| 上海国际能源交易中心 | INE | INE |
| 广州期货交易所 | GF | GFEX |

symbol 示例：`600000.SH` 浦发银行；`000001.SZ` 平安银行；`830779.BJ` 武汉蓝电；`IC2311.IF` 中证500股指期货；`rb2311.SF` 螺纹钢；`m2311.DF` 豆粕；`FG305.ZF` 玻璃；`sc2311.INE` 原油；`lc2405.GF` 碳酸锂；`10005334.SHO` 上证期权；`90002114.SZO` 深证期权；`290001.BKZS` 板块指数。

- **期货代码严格区分大小写**：`AP401.ZF` 不能写 `ap401.ZF`，`rb2401.SF` 不能写 `RB2401.SF`。
- **主力连续合约**：合约代码以 `00` 结尾（如 `rb00.SF`），量价简单拼接未平滑，仅回测可交易。
- **加权连续合约**：以 `JQ00` 结尾（如 `rbJQ00.SF`），加权合成更平滑，仅回测可交易。

### 2.4 period 周期取值（通用）

- 分笔：`'tick'`
- 分钟：`'1m'` `'3m'` `'5m'` `'10m'` `'15m'` `'30m'` `'1h'`（=60m）`'2h'` `'3h'` `'4h'`
- 日以上：`'1d'` `'2d'` `'3d'` `'5d'` `'1w'` `'1mon'` `'1q'` `'1hy'` `'1y'`
- Level-2：`'l2quote'`（快照）`'l2quoteaux'`（快照补充）`'l2order'`（逐笔委托）`'l2transaction'`（逐笔成交）`'l2transactioncount'`（大单统计）`'l2orderqueue'`（委买委卖队列）
- 旧接口（get_local_data）另有：`'realtime'` 实时线、`'md'` 多日线、`'mm'` 多分钟线、`'mh'` 多小时线

**合成规则**：基础存储周期为 tick / 1m / 5m / 1d。3m 由 1m 合成；10m/15m/30m/1h/2h/3h/4h 由 5m 合成；2d/3d/5d/1w/1mon/1q/1hy/1y 由 1d 合成。取合成周期**历史**数据需先下载其基础周期（取 15m 需下载 5m；同时用 5m 和 15m 只需下载 5m）；取**实时**可直接订阅原始周期。

### 2.5 dividend_type 复权取值（通用）

`'none'` 不复权；`'front'` 前复权；`'back'` 后复权；`'front_ratio'` 等比前复权；`'back_ratio'` 等比后复权。旧接口（get_history_data）用整数 0/1/2/3/4 对应同一次序。回测推荐等比前复权（避免配股增发造成的价格跳变）。

### 2.6 运行模式

四种模式需在界面手动选择（无代码取值）：调试运行（编辑器「运行」，实时行情运算、不记录信号）、回测（编辑器「回测」，按设定区间运算并记录绩效）、模拟信号（策略交易界面「模拟」，下单函数仅记录信号不实际委托）、实盘交易（策略交易界面「实盘」，实际下单并记录信号）。运行模式的「模拟/实盘」与账号本身是模拟柜台还是真实柜台无关。

### 2.7 ContextInfo 常用属性

| 属性 | 读写 | 类型 | 说明 |
|---|---|---|---|
| `start` / `end` | 写，仅回测模式，仅在 init 中设置生效 | str | 回测起止时间，`'%Y-%m-%d %H:%M:%S'`；缺省为编辑器设定值，两处同时设置以代码为准；end ≤ start 时计算范围为空 |
| `capital` | 读写，仅回测 | float | 回测初始资金，默认 1000000；与编辑器同时设置以代码为准 |
| `period` | 只读 | str | 当前周期（见 2.4） |
| `barpos` | 只读 | int | 当前运行到的 K 线索引号，从 0 开始 |
| `time_tick_size` | 只读 | int | 当前图 K 线数量 |
| `stockcode` | 只读 | str | 主图代码（如 `000300`） |
| `market` | 只读 | str | 主图市场（如 `SH`）；完整代码 = `stockcode + '.' + market` |
| `dividend_type` | 只读 | str | 复权方式（见 2.5） |
| `benchmark` | 只读，仅回测 | str | 回测基准代码（如 `000300.SH`） |
| `do_back_test` | 只读 | bool | 是否回测模式，默认 False |

方法（`is_last_bar` / `is_new_bar` / `schedule_run` / `run_time` 等）见第 4 章；**不要给 ContextInfo 添加自定义属性**（见 1.3）。

---

## 3. 数据结构

> 行情类与交易类对象字段表。字段中引用的枚举（EEntrustBS、EEntrustStatus、EOffset_Flag_Type 等）取值见第 10 章。`l2orderqueue` 官方文档仅有标题无字段定义。

### 3.1 Tick — 行情快照

#### get_market_data_ex / get_full_tick / subscribe 回调对象

| 字段名 | 类型 | 含义 |
|---|---|---|
| time | int | 时间戳（毫秒） |
| stime | string | 时间戳字符串形式 |
| lastPrice | float | 最新价 |
| open / high / low | float | 开/高/低 |
| lastClose | float | 前收盘价 |
| amount | float | 成交总额 |
| volume | int | 成交总量（手） |
| pvolume | int | 原始成交总量（未经股手转换）【不推荐使用】 |
| stockStatus | int | 证券状态 |
| openInt | int | 股票时为股票状态（见 3.24），非股票为持仓量 |
| transactionNum | float | 成交笔数（期货没有，单独计算） |
| lastSettlementPrice | float | 前结算（股票为 0） |
| settlementPrice | float | 今结算（股票为 0） |
| askPrice / askVol | list | 多档委卖价 / 委卖量 |
| bidPrice / bidVol | list | 多档委买价 / 委买量 |

#### get_market_data 返回对象（旧接口）

与上表差异：`timetag`（string，`%Y%m%d %H:%M:%S`）替代 time/stime；volume/pvolume/openInt 为 float；`stockStatus` 已作废以 openInt 为准；独有 `pe`（股票为市盈率，ETF 为 iopv）；无 transactionNum。

### 3.2 Bar — K 线对象

| 字段 | 类型 | 含义 |
|---|---|---|
| time | int | 时间 |
| open / high / low / close | float | 开/高/低/收 |
| volume | float | 成交量 |
| amount | float | 成交额 |
| settelementPrice | float | 今结算（字段名官方原文如此拼写） |
| openInterest | float | 持仓量 |
| preClose | float | 前收盘价 |
| suspendFlag | int | 停牌：1 停牌，0 不停牌 |

get_market_data_ex 返回 DataFrame 的列同本表（另含 stime、timeEx）。

### 3.3 l2quote — Level2 行情快照

字段与 3.1 第一表一致（time/stime/lastPrice/open/high/low/amount/volume/pvolume/stockStatus/openInt(持仓量)/transactionNum/lastClose/lastSettlementPrice/settlementPrice/askPrice/askVol/bidPrice/bidVol）。

### 3.4 l2quoteaux — Level2 行情快照补充

| 字段名 | 类型 | 解释 |
|---|---|---|
| time | int | 时间戳 |
| stime | string | 时间戳字符串形式 |
| avgBidPrice | float | 委买均价 |
| totalBidQuantity | int | 委买总量 |
| avgOffPrice | float | 委卖均价 |
| totalOffQuantity | int | 委卖总量 |
| withdrawBidQuantity | int | 买入撤单总量 |
| withdrawBidAmount | float | 买入撤单总额 |
| withdrawOffQuantity | int | 卖出撤单总量 |
| withdrawOffAmount | float | 卖出撤单总额 |

### 3.5 l2order — Level2 逐笔委托

| 字段名 | 类型 | 解释 |
|---|---|---|
| time | int | 时间戳 |
| stime | float | 时间戳浮点数形式 |
| price | float | 委托价 |
| volume | int | 委托量 |
| entrustNo | int | 委托号 |
| entrustType | int | 委托类型（见 3.23） |
| entrustDirection | int | 委托方向：0 未知；1 买入；2 卖出；3 撤买（上交所）；4 撤卖（上交所）。上交所撤单信息在委托方向中区分撤买撤卖 |

### 3.6 l2transaction — Level2 逐笔成交

| 字段名 | 类型 | 解释 |
|---|---|---|
| time | int | 时间戳 |
| stime | string | 时间戳字符串形式 |
| price | float | 成交价 |
| volume | int | 成交量 |
| amount | float | 成交额 |
| tradeIndex | int | 成交记录号 |
| buyNo | int | 买方委托号 |
| sellNo | int | 卖方委托号 |
| tradeType | int | 成交类型（官方未给出取值表） |
| tradeFlag | int | 成交标志：0 未知；1 外盘/主买；2 内盘/主卖；3 撤单（深交所逐笔成交撤单标志无方向） |

### 3.7 l2transactioncount — Level2 逐笔成交统计（大单统计）

基础字段：

| 字段名 | 类型 | 含义 |
|---|---|---|
| time / stime | int / string | 时间戳 |
| bidNumber / offNumber | int | 主买/主卖单总单数 |
| ddx / ddy / ddz | float | 大单动向 / 涨跌动因 / 大单差分 |
| netOrder | int | 净挂单量 |
| netWithdraw | int | 净撤单量 |
| withdrawBid / withdrawOff | int | 总撤买量 / 总撤卖量 |
| bidNumberDx / offNumberDx | int | 主买/主卖单总单数增量 |
| transactionNumber | int | 成交笔数增量 |

成交额/成交量字段按固定模式命名（官方字段表即按此展开）：

- 前缀 × 方向档位 × 后缀：
  - 方向前缀：`bid`（主买）、`off`（主卖）、`unactiveBid`（被动买）、`unactiveOff`（被动卖）、`netInflow`（净流入，lv1 数据不支持计算返回 0）
  - 档位：`Most`（特大）、`Big`（大）、`Medium`（中）、`Small`（小）、`Total`（累计；netInflow 无 Total）
  - 后缀：`Amount`（成交额，float）、`Volume`（成交量，int）、`AmountDx`（成交额增量）、`VolumeDx`（成交量增量）
- 例：`bidMostAmount` 主买特大单成交额、`unactiveOffSmallVolumeDx` 被动卖小单成交量增量、`netInflowBigAmount` 净流入大单成交额。

### 3.8 l2orderqueue — Level2 委买委卖队列

官方文档该节只有标题无内容。订阅回调实测字段：`time`、`stime`、`bidLevelPrice`（买一价）、`bidLevelVolume`（买一档各单量 list）、`offerLevelPrice`（卖一价）、`offerLevelVolume`（卖一档各单量 list）。

### 3.9 Account — 账户对象

| 字段名 | 类型 | 解释 |
|---|---|---|
| m_strAccountID | str | 资金账号 |
| m_nBrokerType | int | 账号类型 |
| m_dMaxMarginRate | float | 保证金比率（通常用于期货账号） |
| m_dFrozenMargin | float | 冻结保证金 |
| m_dFrozenCash | float | 冻结金额 |
| m_dFrozenCommission | float | 冻结手续费 |
| m_dRisk | float | 风险度 |
| m_dNav | float | 单位净值 |
| m_dPreBalance | float | 期初权益 |
| m_dBalance | float | 总资产 |
| m_dAvailable | float | 可用金额 |
| m_dCommission | float | 手续费（旧版本为 m_dComission） |
| m_dPositionProfit | float | 持仓盈亏 |
| m_dCloseProfit | float | 平仓盈亏（期货） |
| m_dCashIn | float | 出入金净值 |
| m_dCurrMargin | float | 当前使用的保证金金额 |
| m_dInitBalance | float | 初始权益 |
| m_strStatus | str | 账户当前状态 |
| m_dInitCloseMoney | float | 期初平仓盈亏 |
| m_dInstrumentValue | float | 总市值 |
| m_dDeposit / m_dWithdraw | float | 入金 / 出金 |
| m_dPreCredit / m_dPreMortgage | float | 上次信用额度 / 上次质押 |
| m_dMortgage / m_dCredit | float | 质押 / 信用额度 |
| m_dAssetBalance | float | 证券初始资金 |
| m_strOpenDate | str | 起始日期 |
| m_dFetchBalance | float | 可取金额 |
| m_strTradingDate | str | 交易日 |
| m_dStockValue | float | 股票总市值 |
| m_dLoanValue | float | 债券总市值 |
| m_dFundValue | float | 基金总市值（含 ETF 和封基） |
| m_dRepurchaseValue | float | 回购总市值 |
| m_dLongValue / m_dShortValue | float | 多单 / 空单总市值 |
| m_dNetValue | float | 净持仓总市值（多 - 空） |
| m_dAssureAsset | float | 净资产 |
| m_dTotalDebit | float | 总负债 |
| m_dEntrustAsset | float | 可信资产（校对资金准确性） |
| m_dInstrumentValueRMB | float | 总市值（人民币，沪港通） |
| m_dSubscribeFee | float | 申购费 |
| m_dGoldValue / m_dGoldFrozen | float | 黄金库存市值 / 现货冻结 |
| m_dMargin | float | 占用保证金（维持保证金） |
| m_strMoneyType | str | 币种 |
| m_dPurchasingPower | float | 购买力 |
| m_dRawMargin | float | 原始保证金（期货） |
| m_dBuyWaitMoney / m_dSellWaitMoney | float | 买入 / 卖出待交收金额（元） |
| m_dReceiveInterestTotal | float | 本期间应计利息 |
| m_dRoyalty / m_dFrozenRoyalty | float | 权利金收支 / 冻结权利金（期货期权） |
| m_dRealUsedMargin | float | 实时占用保证金（股票期权） |
| m_dRealRiskDegree | float | 实时风险度（股票期权） |

### 3.10 Order — 委托对象

| 字段 | 类型 | 含义 |
|---|---|---|
| m_strAccountID | str | 资金账号 |
| m_strExchangeID / m_strExchangeName | str | 证券市场 / 交易市场 |
| m_strProductID / m_strProductName | str | 品种代码 / 品种名称 |
| m_strInstrumentID / m_strInstrumentName | str | 证券代码 / 证券名称（合约名称） |
| m_nRef | int | 订单编号 |
| m_strOrderRef | str | 内部委托号（下单引用） |
| m_nOrderPriceType | int | EBrokerPriceType 价格类型（市价单/限价单等） |
| m_nDirection | int | EEntrustBS 操作/多空；期货区分多空，股票买卖该值永远 48 |
| m_nOffsetFlag | int | EOffset_Flag_Type 买卖/开平；用此字段区分股票买卖、期货开平仓、期权买卖 |
| m_nHedgeFlag | int | EHedge_Flag_Type 投保 |
| m_dLimitPrice | float | 委托价格（限价单的限价） |
| m_nVolumeTotalOriginal | int | 委托数量（最初委托量） |
| m_nOrderSubmitStatus | int | EEntrustSubmitStatus 报单/提交状态（股票不需要） |
| m_strOrderSysID | str | 合同编号（委托号） |
| m_nOrderStatus | int | EEntrustStatus 委托状态 |
| m_nVolumeTraded | int | 已成交数量 |
| m_nVolumeTotal | int | 委托剩余量（股票：总委托量 - 成交量） |
| m_nErrorID / m_strErrorMsg | int / str | 状态 ID / 状态信息 |
| m_nTaskId | int | 任务号 |
| m_dFrozenMargin / m_dFrozenCommission | float | 冻结金额（保证金）/ 冻结手续费 |
| m_strInsertDate / m_strInsertTime | str | 委托日期 / 委托时间 |
| m_dTradedPrice | float | 成交均价（股票） |
| m_dCancelAmount | float | 已撤数量 |
| m_strOptName | str | 买卖标记（中文） |
| m_dTradeAmount | float | 成交金额；期货 = 均价×数量×合约乘数 |
| m_eEntrustType | int | EEntrustTypes 委托类别 |
| m_strCancelInfo | str | 废单原因 |
| m_strUnderCode | str | 标的证券代码 |
| m_eCoveredFlag | int | 备兑标记：'0' 非备兑，'1' 备兑 |
| m_dOrderPriceRMB / m_dTradeAmountRMB | float | 委托价 / 成交金额（人民币，港股通） |
| m_dReferenceRate | float | 汇率（港股通） |
| m_strCompactNo | str | 合约编号 |
| m_eCashgroupProp | int | EXTCompactBrushSource 头寸来源 |
| m_dShortOccupedMargin | float | 预估在途占用保证金（期权） |
| m_strXTTrade | str | 是否迅投交易 |
| m_strAccountKey | str | 账号 key（唯一区别不同账号） |
| m_strRemark | str | 投资备注（对应 passorder 的 userOrderId） |

### 3.11 Deal — 成交对象

| 字段 | 类型 | 解释 |
|---|---|---|
| m_strAccountID | str | 资金账号 |
| m_strExchangeID / m_strExchangeName | str | 证券市场 / 交易市场 |
| m_strProductID / m_strProductName | str | 品种代码 / 品种名称 |
| m_strInstrumentID / m_strInstrumentName | str | 证券代码 / 证券名称 |
| m_strTradeID | str | 成交编号 |
| m_strOrderRef | str | 下单引用（内部委托号） |
| m_strOrderSysID | str | 合同编号 / 委托号（与委托列表一致） |
| m_nDirection | int | EEntrustBS；股票该值始终 48 |
| m_nOffsetFlag | int | EOffset_Flag_Type 买卖/开平 |
| m_nHedgeFlag | int | EHedge_Flag_Type 投保 |
| m_dPrice | float | 成交均价 |
| m_nVolume | int | 成交量（期货手，股票股） |
| m_strTradeDate / m_strTradeTime | str | 成交日期 / 成交时间 |
| m_dCommission | float | 手续费（旧版本为 m_dComission） |
| m_dTradeAmount | float | 成交额；期货 = 均价×量×合约乘数 |
| m_nTaskId | int | 任务号 |
| m_nOrderPriceType | int | EBrokerPriceType |
| m_strOptName | str | 买卖标记（中文） |
| m_eEntrustType | int | EEntrustTypes 委托类别 |
| m_eFutureTradeType | int | EFutureTradeType 成交类型 |
| m_nRealOffsetFlag | int | EOffset_Flag_Type 实际开平（区分平今/平昨） |
| m_eCoveredFlag | int | 备兑标记 '0' 非备兑，'1' 备兑 |
| m_nCloseTodayVolume | int | 平今量（不显示） |
| m_dOrderPriceRMB / m_dPriceRMB / m_dTradeAmountRMB | float | 委托价 / 成交价 / 成交额（人民币，港股通） |
| m_dReferenceRate | float | 汇率（港股通） |
| m_strXTTrade | str | 是否迅投交易 |
| m_strCompactNo | str | 合约编号 |
| m_dCloseProfit | float | 平仓盈亏（外盘） |
| m_strRemark | str | 投资备注 |
| m_strAccountKey | str | 账号 key |
| m_nRef | int | 订单编号 |

### 3.12 Position — 持仓对象

| 字段名 | 类型 | 含义 |
|---|---|---|
| m_strAccountID | string | 资金账号 |
| m_strExchangeID / m_strExchangeName | string | 证券市场 / 市场名称 |
| m_strProductID / m_strProductName | string | 品种代码 / 品种名称 |
| m_strInstrumentID / m_strInstrumentName | string | 证券代码 / 证券名称 |
| m_nHedgeFlag | int | EHedge_Flag_Type 投保（股票不适用） |
| m_nDirection | int | EEntrustBS；股票该值始终 48 |
| m_strOpenDate | string | 开仓日期（股票无效） |
| m_strTradeID | string | 最初开仓成交号 |
| m_nVolume | int | 当前持仓量 |
| m_dOpenPrice | float | 持仓成本 =（总买入金额 - 总卖出金额）/ 剩余数量 |
| m_strTradingDay | string | 实盘为当前交易日；回测为最后交易日期 |
| m_dMargin | float | 使用保证金（股票不适用） |
| m_dOpenCost | float | 开仓成本 = 成本价×首次建仓量（不含手续费，股票不适用） |
| m_dSettlementPrice | float | 最新结算价 / 当前价 |
| m_nCloseVolume / m_dCloseAmount | int / float | 平仓量 / 平仓额（股票不适用） |
| m_dFloatProfit | float | 浮动盈亏 |
| m_dCloseProfit | float | 平仓盈亏（股票不适用） |
| m_dMarketValue | float | 市值 / 合约价值 |
| m_dPositionCost | float | 持仓成本（股票不适用） |
| m_dPositionProfit | float | 持仓盈亏（股票不适用） |
| m_dLastSettlementPrice | float | 最新结算价（股票不适用） |
| m_dInstrumentValue | float | 合约价值（股票不适用） |
| m_bIsToday | bool | 是否今仓 |
| m_strStockHolder | string | 股东账号 |
| m_nFrozenVolume | int | 冻结数量 |
| m_nCanUseVolume | int | 可用数量 |
| m_nOnRoadVolume | int | 在途股份 |
| m_nYesterdayVolume | int | 昨夜拥股 |
| m_dLastPrice | float | 最新价 / 当前价 |
| m_dAvgOpenPrice | float | 开仓均价（股票不适用） |
| m_dProfitRate | float | 盈亏比例 |
| m_eFutureTradeType | int | EFutureTradeType 成交类型 |
| m_strExpireDate | string | 到期日（逆回购） |
| m_strComTradeID | string | 组合成交号 |
| m_nLegId | int | 组合序号 |
| m_dTotalCost / m_dSingleCost | float | 累计 / 单股成本（自定义，股票信用用） |
| m_nCoveredVolume | int | 备兑数量（个股期权） |
| m_eSideFlag | int | 持仓类型（个股期权）：'0' 权利，'1' 义务，'2' 备兑 |
| m_dReferenceRate | float | 汇率（港股通） |
| m_dStructFundVol / m_dRedemptionVolume | float | 分级基金可分拆合并 / 可赎回量 |
| m_nPREnableVolume | int | 申赎可用量 |
| m_dRealUsedMargin | float | 实时占用保证金（期权） |
| m_dRoyalty | float | 权利金 |
| m_dStockLastPrice | float | 标的证券最新价（期权） |
| m_dStaticHoldMargin | float | 静态持仓占用保证金（期权） |
| m_nOptCombUsedVolume | int | 期权组合占用数量 |
| m_nEnableExerciseVolume | int | 可行权数量（个股期权） |
| m_strAccountKey | string | 账号 key |

### 3.13 PositionStatistics — 持仓统计对象

| 字段名 | 类型 | 描述 |
|---|---|---|
| m_strAccountID | string | 账号 |
| m_strExchangeID / m_strExchangeName | string | 市场代码 / 名称 |
| m_strProductID | string | 品种代码 |
| m_strInstrumentID / m_strInstrumentName | string | 合约代码 / 名称 |
| m_nDirection | int | 多空 |
| m_nHedgeFlag | int | 投保 |
| m_nPosition | int | 持仓 |
| m_nYestodayPosition / m_nTodayPosition | int | 昨仓 / 今仓 |
| m_nCanCloseVol | int | 可平 |
| m_dPositionCost | float | 持仓成本 |
| m_dAvgPrice | float | 持仓均价 |
| m_dPositionProfit / m_dFloatProfit | float | 持仓盈亏 / 浮动盈亏 |
| m_dOpenPrice | float | 开仓均价 |
| m_dUsedMargin / m_dUsedCommission | float | 已使用保证金 / 手续费 |
| m_dFrozenMargin / m_dFrozenCommission | float | 冻结保证金 / 手续费 |
| m_dInstrumentValue | float | 市值 / 合约价值 |
| m_nOpenTimes / m_nOpenVolume | int | 开仓次数 / 总开仓量（中间平仓不减） |
| m_nCancelTimes | int | 撤单次数 |
| m_dLastPrice | float | 最新价 |
| m_dRiseRatio | float | 当日涨幅 |
| m_strProductName | string | 产品名称 |
| m_dRoyalty | float | 权利金市值 |
| m_strExpireDate | string | 到期日 |
| m_dAssestWeight | float | 资产占比 |
| m_dIncreaseBySettlement | float | 当日涨幅（结） |
| m_dMarginRatio | float | 保证金占比 |
| m_dFloatProfitDivideByUsedMargin | float | 浮盈比例（保证金） |
| m_dFloatProfitDivideByBalance | float | 浮盈比例（动态权益） |
| m_dTodayProfitLoss | float | 当日盈亏（结） |
| m_nYestodayInitPosition | int | 昨日持仓 |
| m_dFrozenRoyalty | float | 冻结权利金 |
| m_dTodayCloseProfitLoss | float | 当日盈亏（收） |
| m_dCloseProfit | float | 平仓盈亏 |
| m_strFtProductName | string | 品种名称 |
| m_dOpenCost | float | 开仓成本 |

### 3.14 CCreditAccountDetail — 信用账号对象（非查柜台）

字段与 Account（3.9）大量同名同义（m_nBrokerType：1 期货、2 股票、3 信用、5 期货期权、6 股票期权、7 沪港通、11 深港通），此处仅列出信用特有的字段：

| 字段名 | 类型 | 解释 |
|---|---|---|
| m_dPerAssurescaleValue | float | 个人维持担保比例 |
| m_dEnableBailBalance / m_dUsedBailBalance | float | 可用 / 已用保证金 |
| m_dAssureEnbuyBalance | float | 可买担保品资金 |
| m_dFinEnbuyBalance | float | 可买标的券资金 |
| m_dSloEnrepaidBalance | float | 可还券资金 |
| m_dFinEnrepaidBalance | float | 可还款资金 |
| m_dFinMaxQuota / m_dFinEnableQuota / m_dFinUsedQuota | float | 融资授信 / 可用 / 已用额度 |
| m_dFinUsedBail | float | 融资已用保证金额 |
| m_dFinCompactBalance / m_dFinCompactFare / m_dFinCompactInterest | float | 融资合约金额 / 费用 / 利息 |
| m_dFinMarketValue / m_dFinIncome | float | 融资市值 / 合约盈亏 |
| m_dSloMaxQuota / m_dSloEnableQuota / m_dSloUsedQuota | float | 融券授信 / 可用 / 已用额度 |
| m_dSloUsedBail | float | 融券已用保证金额 |
| m_dSloCompactBalance / m_dSloCompactFare / m_dSloCompactInterest | float | 融券合约金额 / 费用 / 利息 |
| m_dSloMarketValue / m_dSloIncome | float | 融券市值 / 合约盈亏 |
| m_dOtherFare | float | 其它费用 |
| m_dUnderlyMarketValue | float | 标的证券市值 |
| m_dFinEnableBalance | float | 可融资金额 |
| m_dDiffEnableBailBalance | float | 可用保证金调整值 |
| m_dBuySecuRepayFrozenMargin / m_dBuySecuRepayFrozenCommission | float | 买券还券冻结资金 / 手续费 |
| m_dSpecialEnableBalance | float | 专项可融金额 |
| m_dEncumberedAssets | float | 担保资产 |
| m_dSloSellBalance / m_dUsedSloSellBalance | float | 融券卖出资金 / 已用融券卖出资金 |
| m_dDiffAssureEnbuyBalance / m_dDiffFinEnbuyBalance / m_dDiffFinEnrepaidBalance | float | 可买担保品 / 可买标的券 / 可还款资金调整值 |
| m_dOtherRealCompactBalance / m_dOtherRealCompactInterest | float | 其他负债合约金额 / 利息金额 |
| m_dFetchAssetBalance | float | 可提出资产总额 |
| m_dTotalEnableQuota / m_dTotalUsedQuota | float | 可用 / 已用总信用额度 |
| m_dDebtProfit / m_dDebtLoss | float | 负债总浮盈 / 浮亏 |
| m_nContractEndDate | int | 合同到期日期 |
| m_dFinDebt | float | 融资负债 |
| m_dFinProfitAmortized | float | 融资浮盈折算 |
| m_dSloProfit / m_dSloProfitAmortized | float | 融券浮盈 / 折算 |
| m_dFinLoss / m_dSloLoss | float | 融资 / 融券浮亏 |

### 3.15 CCreditDetail — 两融资金信息（查柜台，credit_account_callback 返回）

| 字段名 | 类型 | 解释 |
|---|---|---|
| m_dPerAssurescaleValue | float | 维持担保比例 |
| m_dBalance | float | 总资产 |
| m_dTotalDebt | float | 总负债 |
| m_dAssureAsset | float | 净资产 |
| m_dMarketValue | float | 总市值 |
| m_dEnableBailBalance | float | 可用保证金 |
| m_dAvailable | float | 可用资金 |
| m_dFinDebt | float | 融资负债 |
| m_dFinDealAvl / m_dFinFee | float | 融资本金 / 融资息费 |
| m_dSloDebt | float | 融券负债 |
| m_dSloMarketValue / m_dSloFee | float | 融券市值 / 融券息费 |
| m_dOtherFare | float | 其它费用 |
| m_dFinMaxQuota / m_dFinEnableQuota / m_dFinUsedQuota | float | 融资授信 / 可用 / 冻结额度 |
| m_dSloMaxQuota / m_dSloEnableQuota / m_dSloUsedQuota | float | 融券授信 / 可用 / 冻结额度 |
| m_dSloSellBalance / m_dUsedSloSellBalance / m_dSurplusSloSellBalance | float | 融券卖出资金 / 已用 / 剩余 |
| m_dStockValue / m_dFundValue | float | 股票 / 基金市值 |
| error | string | 错误信息 |

### 3.16 CreditSloEnableAmount — 可融券明细对象

（m_dSloRatio、m_dSloStatus 字段 2021 年 9 月已移除，改用 get_assure_contract 获取。）

| 字段名 | 类型 | 解释 |
|---|---|---|
| m_nPlatformID | int | 平台号 |
| m_strBrokerID / m_strBrokerName | string | 经纪公司编号 / 名称 |
| m_strAccountID | string | 资金账号 |
| m_strExchangeID | string | 交易所 |
| m_strInstrumentID | string | 证券代码 |
| m_nEnableAmount | int | 融券可融数量 |
| m_eQuerySloType | enum | EXTSloTypeQueryMode 查询类型（48 普通 / 49 专项） |

### 3.17 StkCompacts — 负债合约对象

| 字段名 | 类型 | 解释 |
|---|---|---|
| m_strAccountID | string | 资金账号 |
| m_strExchangeID / m_strExchangeName | string | 交易所 / 名称 |
| m_strInstrumentID / m_strInstrumentName | string | 证券代码 / 股票名称 |
| m_nOpenDate / m_nOpenTime | int | 合约开仓日期 / 时间 |
| m_strCompactId | string | 合约编号 |
| m_dCrdtRatio | float | 融资融券保证金比例 |
| m_strEntrustNo | string | 委托编号 |
| m_dEntrustPrice / m_nEntrustVol | float / int | 委托价格 / 数量 |
| m_nBusinessVol | int | 合约开仓数量 |
| m_dBusinessBalance / m_dBusinessFare | float | 合约开仓金额 / 费用 |
| m_eCompactType | enum | EXTCompactType 合约类型 |
| m_eCompactStatus | enum | EXTCompactStatus 合约状态 |
| m_dRealCompactBalance / m_nRealCompactVol | float / int | 未还合约金额 / 数量 |
| m_dRealCompactFare / m_dRealCompactInterest | float | 未还合约费用 / 利息 |
| m_dRepaidInterest / m_nRepaidVol / m_dRepaidBalance | float / int / float | 已还利息 / 数量 / 金额 |
| m_dCompactInterest | float | 合约总利息 |
| m_dUsedBailBalance | float | 占用保证金 |
| m_dYearRate | float | 合约年利率 |
| m_nRetEndDate | int | 归还截止日 |
| m_strDateClear | string | 了结日期 |
| m_strPositionStr | string | 定位串 |
| m_dPrice | float | 最新价 |
| m_nCancelVol | int | 合约撤单数量 |
| m_eCashgroupProp | enum | EXTCompactBrushSource 头寸来源 |
| m_dUnRepayBalance | float | 负债金额 |
| m_nRepayPriority | int | 偿还优先级 |
| m_dRealDefaultInterest | float | 未还罚息 |
| m_dOtherRealCompactBalance / m_dOtherRealCompactInterest | float | 其他负债合约金额 / 利息金额 |

### 3.18 StkSubjects — 担保标的对象

| 字段名 | 类型 | 解释 |
|---|---|---|
| m_nPlatformID | int | 平台号（区别不同行情源） |
| m_strBrokerID / m_strBrokerName | string | 经纪公司编号 / 名称 |
| m_strExchangeID | string | 交易所 |
| m_strInstrumentID | string | 证券代码 |
| m_dSloRatio | float | 融券保证金比例 |
| m_eSloStatus | enum | EXTSubjectsStatus 融券状态 |
| m_dFinRatio | float | 融资保证金比例 |
| m_eFinStatus | enum | EXTSubjectsStatus 融资状态（48 正常即可融资） |
| m_strAccountID | string | 资金账号 |
| m_eCreditFundCtl | enum | EXTCreditFundCtl 融资交易控制 |
| m_eCreditStkCtl | enum | EXTCreditStkCtl 融券交易控制 |
| m_eAssureStatus | enum | EXTSubjectsStatus 是否可做担保 |
| m_dAssureRatio | float | 担保品折算比例 |

### 3.19 PassorderArguments — 下单参数对象（orderError_callback 入参）

| 字段名 | 类型 | 解释 |
|---|---|---|
| opType | int | passorder 的 opType |
| orderType | int | passorder 的 orderType |
| accountID | string | 资金账号 |
| orderCode | string | 交易代码 |
| prType | int | 价格类型 |
| modelPrice | float | 下单价格 |
| modelVolume | int | 下单量（手数或股数） |
| strategyName | string | 策略名 `_&&&_` 投资备注 |

### 3.20 CTaskDetail — 任务对象

| 字段名 | 类型 | 解释 |
|---|---|---|
| m_nTaskId | int | 任务号 |
| m_eStatus | enum | ETaskStatus 任务状态 |
| m_strMsg | string | 任务状态消息 |
| m_startTime / m_endTime / m_cancelTime | int | 任务开始 / 结束 / 取消时间（时间戳） |
| m_nBusinessNum | int | 已成交量 |
| m_nGroupId | int | 组合 Id |
| m_stockCode | string | 下单代码（不针对组合下单） |
| m_strAccountID | string | 下单用户（单用户下单） |
| m_eOperationType | enum | EOperationType 下单操作（开平、多空等） |
| m_eOrderType | enum | EOrderType 算法/普通交易 |
| m_ePriceType | enum | EPriceType 报价方式 |
| m_dFixPrice | float | 委托价 |
| m_nNum | int | 委托量 |
| m_strRemark | string | 投资备注 |

### 3.21 CLockPosition — 期权标的持仓

| 字段名 | 类型 | 解释 |
|---|---|---|
| m_strAccountID | string | 账号名 |
| m_strExchangeID / m_strExchangeName | string | 交易所 / 名称 |
| m_strInstrumentID / m_strInstrumentName | string | 标的代码 / 名称 |
| m_totalVol | int | 总持仓量 |
| m_lockVol | int | 可用锁定量 |
| m_unlockVol | int | 未锁定量 |
| m_coveredVol | int | 备兑量 |
| m_nOnRoadcoveredVol | int | 在途备兑量 |

### 3.22 CStkOptCombPositionDetail — 期权组合持仓

| 字段名 | 类型 | 解释 |
|---|---|---|
| m_strAccountID | string | 账号名 |
| m_strExchangeID / m_strExchangeName | string | 交易所 / 名称 |
| m_strContractAccount | string | 合约账号 |
| m_strCombID | string | 组合编号 |
| m_strCombCode / m_strCombCodeName | string | 组合策略编码 / 名称 |
| m_nVolume / m_nFrozenVolume / m_nCanUseVolume | int | 持仓量 / 冻结 / 可用数量 |
| m_strFirstCode / m_strFirstCodeName | string | 合约一 / 名称 |
| m_eFirstCodeType | enum | 合约一类型 认购:48，认沽:49 |
| m_eFirstCodePosType | enum | 合约一持仓类型 权利:48，义务:49，备兑:50 |
| m_nFirstCodeAmt | int | 合约一数量 |
| m_strSecondCode / m_strSecondCodeName | string | 合约二 / 名称 |
| m_eSecondCodeType | enum | 合约二类型 认购:48，认沽:49 |
| m_eSecondCodePosType | enum | 合约二持仓类型 权利:48，义务:49，备兑:50 |
| m_nSecondCodeAmt | int | 合约二数量 |
| m_dCombBailBalance | float | 占用保证金 |

### 3.23 entrustType — 委托类型（l2order.entrustType）

0 未知；1 正常交易业务；2 即时成交剩余撤销；3 ETF 基金申报；4 最优五档即时成交剩余撤销；5 全额成交或撤销；6 本方最优价格；7 对手方最优价格。

### 3.24 openInt — 证券状态（股票）

| 值 | 含义 | 值 | 含义 |
|---|---|---|---|
| 0, 10 | 未知（默认） | 16 | 波动性中断 V（熔断临时停牌） |
| 1 | 停牌 | 17 | 临时停牌 P |
| 11 | 开盘前 S | 18 | 收盘集合竞价 U |
| 12 | 集合竞价时段 C | 19 | 盘中集合竞价 M |
| 13 | 连续交易 T | 20 | 暂停交易至闭市 N |
| 14 | 休市 B | 21 | 获取字段异常 |
| 15 | 闭市 E | 22 / 23 | 盘后固定价格行情 / 完毕 |

期货：0 未知；1 开盘前 S；2 集合竞价 C；3 连续交易 T；4 休市 B；5 闭市 E。

沪市时间段对照：9:15-9:25 盘前集合竞价(12)；9:25-14:57 连续竞价(13)；14:57-15:00 盘后集合竞价(18)；15:00 收盘(15)；15:05-15:30 盘后定价(22)；15:30 结束(23)。深市另有 9:25-9:30、11:30-13:00 休市(14)。

---

## 4. 系统函数

### 4.1 init / after_init / handlebar / stop

见 1.2。签名均为 `def init(C)` / `def after_init(C)` / `def handlebar(C)` / `def stop(C)`，入参为 ContextInfo，无返回值。init 中可订阅行情（回调函数定义在 init 内可闭包引用 C）：

```python
def init(C):
    def my_callback(data):      # 入参为数据字典
        print(data)
    C.subscribe_quote('600000.SH', period='5m', callback=my_callback)
```

### 4.2 ContextInfo.schedule_run — 设置定时器（新版）

```python
ContextInfo.schedule_run(func, time_point, repeat_times=0, interval=None, name='')
```

| 名称 | 类型 | 描述 |
|---|---|---|
| func | Callable | 回调函数，入参为 ContextInfo，无需返回值 |
| time_point | datetime / str | 首次触发时间；str 格式 `'yyyymmddHHMMSS'`（需满足 `strptime(x,'%Y%m%d%H%M%S')`）；已过期则立即执行 |
| repeat_times | int | 首次触发后按 interval 再触发次数；`-1` 不限制 |
| interval | datetime.timedelta | 后续重复间隔 |
| name | str | 任务组名；同名任务不互相覆盖、计入同组，按组取消时全部取消 |

返回：int 定时任务号（全局唯一，可用于取消）。

```python
import datetime as dt
def on_timer(C):
    print('hello world')
def init(C):
    tid = C.schedule_run(on_timer, '20231231235959', -1, dt.timedelta(minutes=1), 'my_timer')
```

### 4.3 ContextInfo.cancel_schedule_run — 取消定时任务

```python
ContextInfo.cancel_schedule_run(key)   # key: int 任务号 或 str 任务组名（取消组内全部）
```

返回 bool（是否找到并取消目标任务）。

### 4.4 ContextInfo.run_time — 设置定时器（旧版）

```python
ContextInfo.run_time(funcName, period, startTime)
```

| 名称 | 描述 |
|---|---|
| funcName | 回调函数名（字符串），回调入参为 ContextInfo |
| period | 间隔：`'5nSecond'` 每 5 秒；`'5nDay'` 每 5 天；`'500nMilliSecond'` 每 500 毫秒（单位有 nMilliSecond / nSecond / Day） |
| startTime | 首次启动时间；填历史时间则立即启动 |

注意：回测时无效；定时器无结束方法，随策略结束而结束；部分周期首次运行前会先等待一个 period。

```python
def init(C):
    C.run_time("f", "5nSecond", "2019-10-14 13:20:00")
def f(C):
    print('hello world')
```

### 4.5 K 线判定与信息函数

| 用法 | 返回 | 说明 |
|---|---|---|
| `C.is_last_bar()` | bool | 是否最新（右侧）一根 K 线 |
| `C.is_new_bar()` | bool | 该 K 线的第一个 tick 返回 True，其后 tick 返回 False（历史 K 线每根均为 True） |
| `C.get_stock_name('stockcode')` | str（GBK） | 代码查名称（已弃用，建议 `C.get_instrument_detail(code)["InstrumentName"]`）；缺省 '' 为主图 |
| `C.get_open_date('stockcode')` | number | 上市时间，如 19910403 |
| `C.get_bar_timetag(C.barpos)` | int | 当前 K 线时间戳，配合 `timetag_to_datetime(ts, '%Y%m%d%H%M%S')` 转字符串 |

### 4.6 ContextInfo.set_output_index_property — 设定指标绘制属性

```python
C.set_output_index_property(index_name, draw_style=0, color='white', noaxis=False, nodraw=False, noshow=False)
```

index_name 不可缺省；draw_style/color 同 paint；noaxis 无坐标；nodraw 不画线；noshow 不展示。例：`C.set_output_index_property('单位净值', nodraw=True)`。

### 4.7 板块函数

| 用法 | 返回 | 说明 |
|---|---|---|
| `create_sector(parent_node, sector_name, overwrite)` | str 实际板块名 | 创建板块；parent_node 为 '' 时在「我的」；目标已存在时 overwrite=True 跳过、False 则在名称后自增编号（注意与参数名字面含义相反，以实际行为为准） |
| `create_sector_folder(parent_node, folder_name, overwrite)` | str 实际节点名 | 创建板块目录节点，规则同上 |
| `get_sector_list(node)` | [[板块名...],[目录名...]] | 获取板块目录信息；node 为 '' 时取顶层 |
| `reset_sector_stock_list(sector, stock_list)` | bool | 重置板块成分股 |
| `add_stock_to_sector(sector, stock_code)` | bool | 添加成分股 |
| `remove_stock_from_sector(sector, stock_code)` | bool | 移除成分股 |

```python
create_sector('我的', '新建板块', False)            # -> '新建板块'
get_sector_list('我的')                             # -> [['我的自选', ...], []]
reset_sector_stock_list('我的自选', ['000001.SZ', '600000.SH'])
```

> **实测：这一族在 2.1.19.0 终端上一个都调不通**（issue #143，2026-09-02）。
> 三条通道全部枚举过（`probe_capabilities` 的 `sector_probe` 块）：
>
> | | 有什么 |
> |---|---|
> | `ContextInfo` | `create_sector`、`get_sector`、`get_stock_list_in_sector` |
> | QMT 注入的全局函数 | **一个都没有** |
> | 原生 xtdata SDK | `add_sector`、`remove_sector`、`get_sector_list`、`get_stock_list_in_sector`、`download_sector_data` |
>
> 也就是说：上表六个里，`create_sector_folder` / `reset_sector_stock_list` /
> `add_stock_to_sector` / `remove_stock_from_sector` **在三条通道上都不存在**；
> `create_sector` 只在 `ContextInfo` 上有，而且签名是 `(sector_name, stock_list)`
> 两参数，**调用后返回 None、什么都不做**（板块数量前后都是 13）。
> 原生 SDK 的 `add_sector` / `remove_sector` 存在，但在大 QMT 进程内
> `无法连接行情服务！`。
>
> 本仓库据此把这一族按 `add_sector(name, stock_list)` 的形状实现（读-合并-写），
> 并在**每次写入后回读校验**，写不进去就抛错 —— 而不是沿用上表那个在本终端
> 不存在的三参数签名。`get_sector_list` 同理：拿不到真实板块时抛错，
> `allow_fallback=True` 才返回那 13 个常用名。

---

## 5. 行情函数

### 5.1 数据下载

#### download_history_data - 下载历史行情

```python
download_history_data(stockcode, period, startTime, endTime, incrementally=None)
```

| 参数 | 类型 | 解释 |
|---|---|---|
| stockcode | string | 'stkcode.market'，如 '600000.SH' |
| period | string | 'tick' / '1d' / '1m' / '5m' 等基础周期（合成周期需下载其基础周期，见 2.4） |
| startTime / endTime | string | '20200101' 或 '20200101093000'，可为空 |
| incrementally | bool | 默认 None；True 为从本地最后一条往后增量下载，部分版本客户端不支持 |

返回：无。

```python
def init(C):
    download_history_data("000001.SZ", "1d", "20230101", "")
    download_history_data("000001.SZ", "1d", "20230101", "", incrementally=True)
```

批量下载辅助（先按合成规则换算成基础周期再逐个下载）：

```python
def my_download(stock_list, period, start_date='', end_date=''):
    if "d" in period: period = "1d"
    elif "m" in period: period = "1m" if int(period[0]) < 5 else "5m"
    n = 1
    for i in stock_list:
        print(f"当前正在下载{n}/{len(stock_list)}")
        download_history_data(i, period, start_date, end_date)
        n += 1
```

### 5.2 获取行情数据

#### ContextInfo.get_market_data_ex - 获取行情数据（主推接口）

获取实时与历史行情；还可取特色数据（资金流向、订单流等，见官方数据字典）。**不建议在 init 中运行**（只能取到本地数据）。

```python
C.get_market_data_ex(fields=[], stock_code=[], period='follow', start_time='', end_time='',
                     count=-1, dividend_type='follow', fill_data=True, subscribe=True)
```

| 名称 | 类型 | 描述 |
|---|---|---|
| fields | list | 数据字段，[] 为全部；字段表见下 |
| stock_code | list | 合约代码列表（建议按位置传参） |
| period | str | 数据周期，见 2.4；'follow' 跟随主图 |
| start_time / end_time | str | '%Y%m%d' 或 '%Y%m%d%H%M%S'；'' 为最早 / 最新一天 |
| count | int | 数据个数（-1 不限） |
| dividend_type | str | 复权方式，见 2.5；'follow' 跟随主图 |
| fill_data | bool | 是否填充数据（停牌填充） |
| subscribe | bool | True（默认）订阅数据可取动态行情（受订阅数量上限限制，超限返回前值填充的重复数据）；False 只读本地已下载数据，不受限制 |

K 线周期 fields 可选：time / open / high / low / close / volume / amount / settle（今结算）/ openInterest / preClose / suspendFlag。

tick 周期 fields 可选：time / lastPrice / lastClose / open / high / low / close / volume / amount / settle / openInterest / stockStatus（结构见 3.1）。

Level-2 周期字段见第 3 章数据结构。

返回：`dict {stock_code: pd.DataFrame}`，DataFrame 的 index 为时间（stime），columns 为字段，各标的维度相同。subscribe=True 时客户端会自动订阅传入品种（无订阅号，只能停止策略释放订阅数）。

```python
def init(C):
    C.stock_list = ["000001.SZ", "600519.SH", "510050.SH"]
def handlebar(C):
    data1 = C.get_market_data_ex([], C.stock_list, period="1d", count=1)                    # 多股多字段一条
    data2 = C.get_market_data_ex([], C.stock_list, period="1d",
                                 start_time="20230901", end_time="20231101")               # 指定区间
    data4 = C.get_market_data_ex(["close", "open"], C.stock_list, period="15m",
                                 start_time="20230901", end_time="20231101")               # 指定字段
    tick = C.get_market_data_ex([], C.stock_list, period="tick",
                                start_time="20230901", end_time="20231101")                # 历史 tick
    l2q = C.get_market_data_ex([], ["rb2405.SF"], period="l2quote", count=1)                # 期货五档盘口
    print(data2["000001.SZ"].tail(), tick["000001.SZ"], l2q)
    # data4["000001.SZ"].to_csv("your_path")  # 导出 csv
```

#### ContextInfo.get_full_tick - 获取全推快照

获取客户端缓存中的最新全推分笔数据。**不能用于回测；只能取最新，不能取历史**。无需订阅、无品种数量限制、盘中约 50ms 更新。

```python
C.get_full_tick(stock_code=[])    # 不指定时为当前主图合约
```

返回：`dict {stock_code: {字段: 值}}`，字段见 3.1 第一表。注意：全推/订阅的分笔是否含五档盘口取决于行情源的全推级别设置（只有最新价时需在行情配置中调高级别）。

```python
def handlebar(C):
    tick = C.get_full_tick(["000001.SZ", "600519.SH"])
    print(tick["000001.SZ"]['lastPrice'], tick["000001.SZ"]['askPrice'])
```

#### ContextInfo.subscribe_quote - 订阅行情

```python
C.subscribe_quote(stock_code, period='follow', dividend_type='follow', result_type='', callback=None)
```

| 参数 | 类型 | 解释 |
|---|---|---|
| stock_code | string | 'stkcode.market' |
| period | string | K 线周期（见 2.4）；分笔周期返回数据均为不复权 |
| dividend_type | string | 复权方式，见 2.5 |
| result_type | string | 返回格式：`'DataFrame'` 或 `''`（默认）返回 {code: DataFrame}；`'dict'` 返回 {code: {字段: 值}}；`'list'` 返回 {code: {字段: [值]}} |
| callback | function | 推送回调，只能有一个位置参数 |

返回：int 订阅号（用于反订阅）。非 VIP 用户有订阅数量限制（同一品种订阅不同周期累加计数；复数策略订阅同一品种不累加；Level-2 订阅受限但与 Level-1 互不影响）。

```python
def call_back(data):
    print(data)
def init(C):
    C.subID = C.subscribe_quote("000001.SZ", "1d", callback=call_back)
```

#### ContextInfo.subscribe_whole_quote - 订阅全推

```python
C.subscribe_whole_quote(code_list, callback=None)
```

code_list：市场代码列表或品种代码列表，如 `['SH','SZ']` 或 `['600000.SH','000001.SZ']`。全推只有分笔周期，每次增量推送有变化的品种。返回 int 订阅号。回调数据结构同 get_full_tick（见 3.1）。

#### ContextInfo.unsubscribe_quote - 反订阅

```python
C.unsubscribe_quote(subId)    # 配合 subscribe_quote / subscribe_whole_quote 使用
```

#### subscribe_formula / unsubscribe_formula / call_formula / call_formula_batch - VBA 模型调用

```python
subscribe_formula(formula_name, stock_code, period, start_time="", end_time="", count=-1,
                  dividend_type="none", extend_param={}, callback=None)
unsubscribe_formula(subID)    # 返回 bool
call_formula(formula_name, stock_code, period, start_time="", end_time="", count=-1,
             dividend_type="none", extend_param={})
call_formula_batch(formula_names, stock_codes, period, start_time="", end_time="", count=-1,
                   dividend_type="none", extend_params=[])
```

- 使用前需补充本地 K 线或分笔数据；period/dividend_type 取值见 2.4 / 2.5。
- extend_param：模型入参 dict；组合模型可加 `'__basket': {stock: weight}`；嵌套模型改参可传 `{'模型2:参数': 值}`。
- subscribe_formula 返回订阅号（失败 -1），callback 收到 `{timelist, outputs:{变量名: 值}}`。
- call_formula 返回 `{'dbt': 0, 'timelist': [...], 'outputs': {'var1': [...], ...}}`。
- call_formula_batch 返回 `list[dict]`，元素为 `{'formula', 'stock', 'argument', 'result'}`，result 同 call_formula。

```python
def init(C):
    basket = {'600000.SH': 0.06, '000001.SZ': 0.01}
    subID = subscribe_formula('单股模型示范', '000300.SH', '1d', '20240101', '20240201',
                              -1, "none", {'a': 100, '__basket': basket}, callback)
def handlebar(C):
    ret = call_formula('单股模型示范', '000300.SH', '1d', '20240101', '20240201', -1, "none",
                       {'a': 100, '__basket': basket})
```

#### ContextInfo.get_svol / get_bvol - 内盘 / 外盘成交量

```python
C.get_svol(stockcode)   # int 内盘成交量
C.get_bvol(stockcode)   # int 外盘成交量
```

stockcode 缺省 '' 为主图代码。

#### ContextInfo.get_turnover_rate - 换手率

```python
C.get_turnover_rate(stock_list, startTime, endTime)   # -> pandas.DataFrame（index 日期，columns 代码）
```

使用前需下载财务数据（股本）与日线数据；不补充股本数据时用最新流通股本计算历史换手率，历史值可能不正确。

#### ContextInfo.get_longhubang - 龙虎榜

```python
C.get_longhubang(stock_list, startTime, endTime)   # startTime/endTime 如 '20170101'/'20180101'
```

返回 pandas.DataFrame，字段：stockCode、stockName、date（上榜日期 datetime）、reason（上榜原因）、close、SpreadRate（涨跌幅）、TurnoverVolume、Turnover_Amount、buyTraderBooth / sellTraderBooth（买卖席位 DataFrame：traderName、buyAmount、buyPercent、sellAmount、sellPercent、totalAmount、rank、direction）。

#### ContextInfo.get_north_finance_change - 北向资金数据

```python
C.get_north_finance_change(period)   # -> dict{时间戳: {字段: 值}}
```

字段：hgtNorthBuyMoney / hgtNorthSellMoney（HGT 北向买/卖资金）、hgtSouthBuyMoney / hgtSouthSellMoney（HGT 南向）、sgtNorthBuyMoney / sgtNorthSellMoney（SGT 北向）、sgtSouthBuyMoney / sgtSouthSellMoney（SGT 南向）、hgtNorthNetInFlow / hgtSouthNetInFlow / sgtNorthNetInFlow / sgtSouthNetInFlow（净流入）、hgtNorthBalanceByDay / hgtSouthBalanceByDay / sgtNorthBalanceByDay / sgtSouthBalanceByDay（当日余额）。

#### ContextInfo.get_hkt_details / get_hkt_statistics - 北向持股明细 / 统计

```python
C.get_hkt_details(stockcode)      # -> dict{时间戳: {...}} 机构明细
C.get_hkt_statistics(stockcode)   # -> dict{时间戳: {...}} 品种统计
```

明细字段：stockCode、ownSharesCompany（机构名称）、ownSharesAmount（持股数量）、ownSharesMarketValue（持股市值）、ownSharesRatio（持股占比）、ownSharesNetBuy（净买入 = 当日持股 - 前一日持股）。统计字段：stockCode、ownSharesAmount（股）、ownSharesMarketValue（元）、ownSharesRatio（%）、ownSharesNetBuy（元）。

#### get_etf_info / get_etf_iopv - ETF 申赎清单 / IOPV

```python
get_etf_info(stockcode)    # 多层嵌套 dict，每日盘前更新
get_etf_iopv(stockcode)    # -> float 基金份额参考净值
```

get_etf_info 顶层字段：etfCode、etfExchID、prCode、cashBalance（现金差额）、maxCashRatio、reportUnit（最小申赎单位）、navPerCU、nav（净值）、ecc、enableCreation / enableRedemption、creationLimit / redemptionLimit、type、tradingDay、preTradingDay、stocks（成分股 dict：{code: {componentExchID, componentCode, componentVolume, ReplaceFlag, ReplaceRatio, ReplaceBalance}}）。

#### ContextInfo.get_local_data - 本地行情【不推荐】

```python
C.get_local_data(stock_code='', start_time='', end_time='', period='1d', divid_type='none', count=-1)
```

仅取本地历史数据（需先 download_history_data），盘中不更新、速度快、适合回测。period 取值见 2.4（另支持 realtime/md/mm/mh 多周期写法）。count ≥ 0 时以 end_time 为基准向前取 count 条；全部缺省取本地全部。

返回 `dict{timetag: valuedict}`；period='tick' 时 valuedict 字段见 3.1（get_market_data 表）；其他周期为 open/high/low/close/volume/amount。

#### ContextInfo.get_history_data - 历史行情【不推荐】

```python
C.get_history_data(len, period, field, dividend_type=0, skip_paused=True)
```

使用前需先 `C.set_universe(stock_list)` 设定股票池（该订阅无订阅号、无法反订阅，已不推荐）。field 可选 'open'/'high'/'low'/'close'/'quoter'；dividend_type 用整数 0-4。返回 `dict{code.market: list}`，list[0] 为最早价格。

#### ContextInfo.get_market_data - 行情【不推荐】

```python
C.get_market_data(fields, stock_code=[], start_time='', end_time='', skip_paused=True,
                  period='follow', dividend_type='follow', count=-1)
```

fields 可选 'open'/'high'/'low'/'close'/'volume'/'amount'/'settle'/'quoter'；skip_paused=True 停牌日用未停牌前价格填充，False 为 NaN。count 与起止时间的组合规则：

| count | 时间设置 | 效果 |
|---|---|---|
| ≥ 0 | 生效 | 返回时间为交集内的 count 条 |
| -1 | 都设 | 区间内取值 |
| -1 | 都不设 | 当前最新 bar 一条 |
| -1 | 只设开始 | 开始时间至今 |
| -1 | 只设结束 | 上市首根至结束时间 |

返回类型随参数变化：单字段单股票单时间点为 float；单股票多字段为 Series；多股票或 DataFrame/Panel（多股票多字段多时间为 pandas.Panel）。

### 5.3 获取财务数据

财务数据读取本地下载的数据（界面端「数据管理-财务数据」下载），建议使用英文表名与迅投英文字段（表名不区分大小写）；除公告日期 m_anntime 与报告截止日 m_timetag 为毫秒时间戳外，单位为元或 %。

#### ContextInfo.get_financial_data - 财务数据（两种用法）

用法 1（批量）：

```python
C.get_financial_data(fieldList, stockList, startDate, endDate, report_type='announce_time')
```

| 参数 | 说明 |
|---|---|
| fieldList | 字段列表，如 `['ASHAREBALANCESHEET.fix_assets', '利润表.净利润']` |
| stockList | 股票列表 |
| startDate / endDate | '20171209' |
| report_type | `'announce_time'`（默认，按公告日期，不会取到未来数据）/ `'report_time'`（按报告期，**可能取到未来数据**） |

返回类型：1 代码 × 1 时间 → Series；1 代码 × 多时间 → DataFrame；多代码 × 1 时间 → DataFrame；多代码 × 多时间 → Panel。

用法 2（单值）：

```python
C.get_financial_data(tabname, colname, market, code, report_type='report_time', barpos)
# 例：C.get_financial_data('ASHAREBALANCESHEET', 'fix_assets', 'SH', '600000', index)  -> float
```

#### ContextInfo.get_raw_financial_data - 原始财务数据

```python
C.get_raw_financial_data(fieldList, stockList, startDate, endDate, report_type='announce_time')
```

与 get_financial_data 相比不按交易日填充，返回 `dict{stock: {字段: {时间戳: 值}}}`。

#### ContextInfo.get_last_volume / get_total_share - 股本

```python
C.get_last_volume(stockcode)   # -> int 最新流通股本
C.get_total_share(stockcode)   # -> int 总股数（缺省 '' 为主图）
```

#### 财务数据字段表

##### 资产负债表（ASHAREBALANCESHEET）

| 中文 | 迅投字段 | 中文 | 迅投字段 |
|---|---|---|---|
| 货币资金 | cash_equivalents | 应收利息 | int_rcv |
| 应收票据 | bill_receivable | 可供出售金融资产 | fin_assets_avail_for_sale |
| 应收账款 | account_receivable | 持有至到期投资 | held_to_mty_invest |
| 预付账款 | advance_payment | 长期股权投资 | long_term_eqy_invest |
| 其他应收款 | other_receivable | 固定资产 | fix_assets |
| 其他流动资产 | other_current_assets | 无形资产 | intang_assets |
| 流动资产合计 | total_current_assets | 递延所得税资产 | deferred_tax_assets |
| 存货 | inventories | 资产总计 | tot_assets |
| 在建工程 | constru_in_process | 交易性金融负债 | tradable_fin_liab |
| 工程物资 | construction_materials | 应付职工薪酬 | empl_ben_payable |
| 长期待摊费用 | long_deferred_expense | 应交税费 | taxes_surcharges_payable |
| 非流动资产合计 | total_non_current_assets | 应付利息 | int_payable |
| 短期借款 | shortterm_loan | 应付债券 | bonds_payable |
| 应付股利 | dividend_payable | 递延所得税负债 | deferred_tax_liab |
| 其他应付款 | other_payable | 负债合计 | tot_liab |
| 一年内到期的非流动负债 | non_current_liability_in_one_year | 实收资本(或股本) | cap_stk |
| 其他流动负债 | other_current_liability | 资本公积金 | cap_rsrv |
| 长期应付款 | longterm_account_payable | 盈余公积金 | surplus_rsrv |
| 应付账款 | accounts_payable | 未分配利润 | undistributed_profit |
| 预收账款 | advance_peceipts | 归属于母公司股东权益合计 | tot_shrhldr_eqy_excl_min_int |
| 流动负债合计 | total_current_liability | 少数股东权益 | minority_int |
| 应付票据 | notes_payable | 负债和股东权益总计 | tot_liab_shrhldr_eqy |
| 长期借款 | long_term_loans | 所有者权益合计 | total_equity |
| 专项应付款 | grants_received | 商誉 | goodwill |
| 其他非流动负债 | other_non_current_liabilities | 专项储备 | specific_reserves |
| 非流动负债合计 | non_current_liabilities | 报告截止日 / 公告日 | m_timetag / m_anntime |

##### 利润表（ASHAREINCOME）

| 中文 | 迅投字段 |
|---|---|
| 营业总收入 | revenue |
| 营业收入 | revenue_inc |
| 营业总成本 | total_operating_cost |
| 营业成本 | total_expense |
| 营业利润 | oper_profit |
| 利润总额 | tot_profit |
| 所得税 | inc_tax |
| 净利润 | net_profit_incl_min_int_inc |
| 归母净利润 | net_profit_excl_min_int_inc |
| 投资收益 | plus_net_invest_inc |
| 联营/合营企业投资收益 | incl_inc_invest_assoc_jv_entp |
| 营业税金及附加 | less_taxes_surcharges_ops |
| 资产减值损失 | less_impair_loss_assets |
| 营业外收入 / 营业外支出 | plus_non_oper_rev / less_non_oper_exp |
| 管理费用 / 销售费用 / 财务费用 | less_gerl_admin_exp / sale_expense / financial_expense |
| 综合收益总额 | total_income |
| 归属于少数股东的综合收益总额 | total_income_minority |
| 公允价值变动收益 | change_income_fair_value |
| 已赚保费 | earned_premium |
| 报告截止日 / 公告日 | m_timetag / m_anntime |

##### 现金流量表（ASHARECASHFLOW）

| 中文 | 迅投字段 |
|---|---|
| 销售商品、提供劳务收到的现金 | goods_sale_and_service_render_cash |
| 收到的税费与返还 | tax_levy_refund |
| 收到其他与经营活动有关的现金 | other_cash_recp_ral_oper_act |
| 经营活动现金流入小计 | stot_cash_inflows_oper_act |
| 购买商品、接受劳务支付的现金 | goods_and_services_cash_paid |
| 支付给职工以及为职工支付的现金 | cash_pay_beh_empl |
| 支付的各项税费 | pay_all_typ_tax |
| 支付其他与经营活动有关的现金 | other_cash_pay_ral_oper_act |
| 经营活动现金流出小计 | stot_cash_outflows_oper_act |
| 经营活动产生的现金流量净额 | net_cash_flows_oper_act |
| 取得投资收益所收到的现金 | cash_recp_return_invest |
| 处置固定资产、无形资产和其他长期投资收到的现金 | net_cash_recp_disp_fiolta |
| 处置子公司及其他收到的现金 | net_cash_deal_subcompany |
| 其中子公司吸收现金 | cash_from_mino_s_invest_sub |
| 投资活动现金流入小计 | stot_cash_inflows_inv_act |
| 投资支付的现金 | cash_paid_invest |
| 购建固定资产、无形资产和其他长期投资支付的现金 | cash_pay_acq_const_fiolta |
| 处置固定资产、无形资产和其他长期资产支付的现金净额 | fix_intan_other_asset_dispo_cash_payment |
| 支付其他与投资的现金 | other_cash_pay_ral_inv_act |
| 投资活动产生的现金流出小计 | stot_cash_outflows_inv_act |
| 投资活动产生的现金流量净额 | net_cash_flows_inv_act |
| 吸收投资收到的现金 | cash_recp_cap_contrib |
| 取得借款收到的现金 | cash_recp_borrow |
| 收到其他与筹资活动有关的现金 | other_cash_recp_ral_fnc_act |
| 筹资活动现金流入小计 | stot_cash_inflows_fnc_act |
| 偿还债务支付现金 | cash_prepay_amt_borr |
| 分配股利、利润或偿付利息支付的现金 | cash_pay_dist_dpcp_int_exp |
| 支付其他与筹资的现金 | other_cash_pay_ral_fnc_act |
| 筹资活动现金流出小计 | stot_cash_outflows_fnc_act |
| 筹资活动产生的现金流量净额 | net_cash_flows_fnc_act |
| 汇率变动对现金的影响 | eff_fx_flu_cash |
| 现金及现金等价物净增加额 | net_incr_cash_cash_equ |
| 报告截止日 / 公告日 | m_timetag / m_anntime |

##### 股本表（CAPITALSTRUCTURE）

| 中文 | 迅投字段 |
|---|---|
| 总股本 | total_capital |
| 已上市流通A股 | circulating_capital |
| 自由流通股本 | free_float_capital（旧版本为 freeFloatCapital） |
| 限售流通股份 | restrict_circulating_capital |
| 变动日期 / 公告日 | m_timetag / m_anntime |

##### 主要指标（PERSHAREINDEX）

| 中文 | 迅投字段 | 中文 | 迅投字段 |
|---|---|---|---|
| 每股经营活动现金流量 | s_fa_ocfps | 加权净资产收益率 | equity_roe |
| 每股净资产 | s_fa_bps | 摊薄净资产收益率 | net_roe |
| 基本每股收益 | s_fa_eps_basic | 摊薄总资产收益率 | total_roe |
| 稀释每股收益 | s_fa_eps_diluted | 毛利率 | gross_profit |
| 每股未分配利润 | s_fa_undistributedps | 净利率 | net_profit |
| 每股资本公积金 | s_fa_surpluscapitalps | 实际税率 | actual_tax_rate |
| 扣非每股收益 | adjusted_earnings_per_share | 预收款营业收入 | pre_pay_operate_income |
| 净资产收益率 | du_return_on_equity | 销售现金流营业收入 | sales_cash_flow |
| 销售毛利率 | sales_gross_profit | 资产负债比率 | gear_ratio |
| 主营收入同比增长 | inc_revenue_rate | 存货周转率 | inventory_turnover |
| 净利润同比增长 | du_profit_rate | 归母净利润同比增长 | inc_net_profit_rate |
| 扣非净利润同比增长 | adjusted_net_profit_rate | 营业总收入滚动环比增长 | inc_total_revenue_annual |
| 归属净利润滚动环比增长 | inc_net_profit_to_shareholders_annual | 扣非净利润滚动环比增长 | adjusted_profit_to_profit_annual |

##### 十大股东 / 十大流通股东（TOP10HOLDER / TOP10FLOWHOLDER）

公告披露数量大于 10 条的保留原始数据。字段：declareDate（公告日期）、endDate（截止日期）、name（股东名称）、type（股东类型）、quantity（持股数量）、reason（变动原因）、ratio（持股比例）、nature（股份性质）、rank（持股排名）。

##### 股东数（SHAREHOLDER）

字段：declareDate、endDate、shareholder（股东总数）、shareholderA / shareholderB / shareholderH（A/B/H 股东户数）、shareholderFloat（已流通股东户数）、shareholderOther（未流通股东户数）。

### 5.4 获取合约信息

#### ContextInfo.get_instrument_detail - 合约详细信息

```python
C.get_instrument_detail(stockcode, iscomplete=False)
```

（旧版本客户端函数名为 `get_instrumentdetail`，不支持 iscomplete。）

| 名称 | 类型 | 描述 |
|---|---|---|
| ExchangeID / InstrumentID / InstrumentName | string | 市场 / 代码 / 名称 |
| ProductID / ProductName | string | 品种 ID / 名称（期货） |
| ProductType | int | 合约类型，默认 -1（见下方说明） |
| ExchangeCode / UniCode | string | 交易所代码 / 统一规则代码 |
| CreateDate / OpenDate | str / int | 创建日期 / 上市日期（特殊值：19700101 新股、19700102 老股东增发、19700103 新债、19700104 可转债、19700105 配股、19700106 配号） |
| ExpireDate | int | 退市日/到期日（0 或 99999999 表示无） |
| PreClose / SettlementPrice | float | 前收 / 前结 |
| UpStopPrice / DownStopPrice | float | 当日涨停 / 跌停价 |
| FloatVolume / TotalVolume | float | 流通股本 / 总股本（单位股；部分低等级客户端字段名为 FloatVolumn / TotalVolumn） |
| LongMarginRatio / ShortMarginRatio | float | 多头 / 空头保证金率 |
| PriceTick | float | 最小价格变动单位 |
| VolumeMultiple | int | 合约乘数（非期货默认 1） |
| MainContract | int | 主力合约标记：1/2/3 为第一/二/三主力 |
| LastVolume | int | 昨日持仓量 |
| InstrumentStatus | int | 停牌状态（≤0 正常（-1 复牌），≥1 停牌天数） |
| IsTrading / IsRecent | bool | 是否可交易 / 近月合约 |
| ChargeType | int | 期货期权手续费方式 |
| ChargeOpen / ChargeClose / ChargeTodayOpen / ChargeTodayClose | float | 开/平/开今/平今手续费（率） |
| OptionType | int | 期权类型 |
| OpenInterestMultiple | int | 交割月持仓倍数 |

ProductType（股票以外）：国内期货市场 1 期货、2 期权(DF/SF/ZF/INE/GF)、3 组合套利、4 即期、5 期转现、6 期权(IF)、7 结算价交易(tas)；沪深股票期权 0 认购、1 认沽；外盘 1-100 期货、101-200 现货、201-300 股票相关。

#### get_st_status / ContextInfo.get_his_st_data - 历史 ST 状态

```python
get_st_status(stockcode)        # stockcode 可为空取主图
C.get_his_st_data(stockcode)    # 'stkcode.market'
```

均需先下载历史 ST 数据（过期合约数据）。返回 `dict{'ST': [[起,止],...], '*ST': [[起,止],...], 'PT': ...}`，从未 ST 返回 `{}`。

#### ContextInfo.get_main_contract - 期货主力合约

```python
C.get_main_contract(codemarket)                              # 当前主力
C.get_main_contract(codemarket, date="")                     # 指定日期主力
C.get_main_contract(codemarket, startDate="", endDate="")    # 区间内主力（返回 pandas.Series）
```

codemarket 为品种名加 00，如 `IF00.IF`、`zn00.SF`。取历史主力需先下载「历史主力合约」数据（过期合约数据）。返回 str 合约代码。

#### ContextInfo.get_contract_multiplier / get_contract_expire_date / get_his_contract_list

```python
C.get_contract_multiplier(contractcode)   # -> int 合约乘数，如 get_contract_multiplier('rb2401.SF') == 10
C.get_contract_expire_date(codemarket)    # -> str 到期日，如 '20231117'
C.get_his_contract_list(market)           # -> list 已退市合约（需手动补充过期合约列表），market 如 SH/SZ/SHO/SZO/IF
```

### 5.5 获取期权信息

#### ContextInfo.get_option_detail_data - 期权合约详情

```python
C.get_option_detail_data(optioncode)   # 空字符串默认主图期权
```

返回 dict：ExchangeID、InstrumentID、ProductID（标的产品 ID）、OpenDate（发行日期）、ExpireDate（到期日）、PreClose、SettlementPrice（前结）、UpStopPrice / DownStopPrice、LongMarginRatio / ShortMarginRatio、PriceTick、VolumeMultiple（合约乘数）、MaxMarketOrderVolume / MinMarketOrderVolume（涨跌停价最大/最小下单量）、MaxLimitOrderVolume / MinLimitOrderVolume（限价单最大/最小下单量）、OptUnit（合约单位）、MarginUnit（单位保证金）、OptUndlCode / OptUndlMarket（标的代码/市场）、OptExercisePrice（行权价）、NeeqExeType、OptUndlRiskFreeRate（标的无风险利率）、OptUndlHistoryRate（标的历史波动率）、EndDelivDate（行权终止日）、optType（'CALL'/'PUT'）。

#### ContextInfo.get_option_list - 期权列表

```python
C.get_option_list(undl_code, dedate, opttype='', isavailable=True)
```

| 参数 | 说明 |
|---|---|
| undl_code | 期权标的代码，如 '510300.SH' |
| dedate | 'YYYYMM' 按到期月取；'YYYYMMDD' 取当日交易的期权 |
| opttype | 'CALL' / 'PUT'，空则都取 |
| isavailable | dedate 为 'YYYYMMDD' 时：True 当前可用；False 当前和历史（含退市，需先下载过期合约列表） |

返回 list 期权合约列表。

#### ContextInfo.get_option_undl_data - 标的对应的期权品种列表

```python
C.get_option_undl_data(undl_code_ref)   # '' 返回全部标的的 dict；指定标的返回 list
```

#### ContextInfo.bsm_price / bsm_iv - BS 模型定价 / 隐含波动率

```python
C.bsm_price(optionType, objectPrices, strikePrice, riskFree, sigma, days, dividend)
C.bsm_iv(optionType, objectPrices, strikePrice, optionPrice, riskFree, days, dividend)
```

optionType：'C' 认购 / 'P' 认沽；objectPrices 可为 float 或 list（返回对应 float/list）；bsm_price 结果最小 0.0001、保留 4 位小数、非法参数返回 nan；bsm_iv 返回隐含波动率 double。

```python
def after_init(C):
    prices = C.bsm_price('C', list(np.arange(3, 4, 0.01)), 3.5, 0.03, 0.23, 15, 0)
    price = C.bsm_price('C', 3.51, 3.5, 0.03, 0.23, 15, 0)     # -> 0.0725
    iv = C.bsm_iv('C', 3.51, 3.5, 0.0725, 0.03, 15, 0)         # -> 0.2299
```

### 5.6 除复权信息

#### ContextInfo.get_divid_factors - 除权除息日与复权因子

```python
C.get_divid_factors(stock.market)   # 如 '600000.SH'
```

返回 `dict{时间戳: [每股红利, 每股送转, 每转赠, 配股, 配股价, 是否股改, 复权系数]}`；输入非法日期返回空 dict。

### 5.7 指数权重

#### ContextInfo.get_weight_in_index - 个股在指数中的权重

```python
C.get_weight_in_index(indexcode, stockcode)   # -> float，单位 %，如 1.6134 表示 1.6134%
```

### 5.8 成分股信息

#### ContextInfo.get_stock_list_in_sector - 板块成分股

```python
C.get_stock_list_in_sector(sectorname, realtime=None)
```

sectorname 支持客户端板块列表中任意板块（含自定义），如 '沪深A股'、'沪深京A股'、'上证50'、'我的自选'；realtime 为毫秒时间戳。返回 `list['stockcode.market', ...]`。

### 5.9 交易日信息

#### ContextInfo.get_trading_dates - 交易日列表

**只能在 after_init / handlebar 中运行**。

```python
C.get_trading_dates(stockcode='', start_date='', end_date='', count=-1, period='1d')
```

stockcode 缺省为主图；end_date 缺省为当前 bar 时间；count > 0 时取含 end_date 往前 count 个（不早于 start_date）。返回 list，日线周期如 `['20170101', ...]`，其他周期如 `['20170101010000', ...]`。

```python
def after_init(C):
    print(C.get_trading_dates('600000.SH', '', '', 30, '1d'))
```

---

## 6. 交易函数

### 6.1 passorder — 综合下单函数（主推）

股票、期货、期权下单及新股新债申购、融资融券等综合下单，可覆盖多品种。

```python
passorder(opType, orderType, accountid, orderCode, prType, price, volume,
          strategyName, quickTrade, userOrderId, ContextInfo)
```

| 参数名 | 类型 | 说明 |
|---|---|---|
| opType | int | 交易类型（买/卖、开平仓等），完整枚举见 10.1 |
| orderType | int | 下单方式（按股数/金额/比例，单账号/账号组/组合），见 10.2；期货不支持 1102/1202 |
| accountID | string | 资金账号（可多个）、账号组名或套利组名（`'股票账户名, 期货账号'`） |
| orderCode | string | 单股/单期货/港股填合约代码；组合交易填篮子名称；组合套利填 `'篮子名, 期货合约名'` |
| prType | int | 下单选价类型，见 10.3。套利时 prType 只对篮子生效 |
| price | float | 下单价格。单股时 prType 为 11（指定价）/49（盘后定价）才有效，其他情况可填 -1、0 等占位；市价类型时为保护限价（0 表示涨跌停价）；组合套利时作套利比例 |
| volume | int | 下单数量，单位由 orderType 末位决定，见 10.4 |
| strategyName | string | 自定义策略名，区分不同策略的委托/成交；配合 get_trade_detail_data、get_last_order_id 过滤；只对当前客户端本地下单有效 |
| quickTrade | int | 快速下单，见 1.4 / 10.5 |
| userOrderId | string | 用户自设委托 ID（投资备注，长度 < 24），对应 Order/Deal 的 `m_strRemark`；传入时 strategyName 与 quickTrade 也需填写 |
| ContextInfo | class | 上下文对象 |

返回：无（异步，见 1.5）。

```python
#coding:gbk
account = "test"
def init(C): pass

def handlebar(C):
    if not C.is_last_bar():
        return
    # 期货最新价开多 10 手（prType=5 最新价，price 占位 -1）
    passorder(0, 1101, account, "rb2405.SF", 5, -1, 10, "示例", 1, "投资备注", C)
    # 期货指定价 3000 开多 10 手
    passorder(0, 1101, account, "rb2405.SF", 11, 3000, 10, "示例", 1, "投资备注", C)
    # 股票最新价买入 100 股
    passorder(23, 1101, account, "000001.SZ", 5, 0, 100, "示例", 1, "投资备注", C)
    # 股票指定价 7 元买入 100 股
    passorder(23, 1101, account, "000001.SZ", 11, 7, 100, "示例", 1, "投资备注", C)
```

账号组与 ETF 申购（部分参数可省略时的短签名）：

```python
passorder(23, 1202, 'testS', '000001.SZ', 5, -1, 50000, C)   # 账号组内所有账号买入 5 万元市值
passorder(60, 1101, 'test', '510050.SH', 5, -1, 1, 2, C)     # 申购 1 个单位(900000股) 华夏上证50ETF
```

### 6.2 algo_passorder — 算法下单（拆单）

按固定时间间隔和规则把目标量拆分成多次下单。参数同 passorder，另在 quickTrade 后多一个可选的 `userOrderParam`：

```python
algo_passorder(opType, orderType, accountid, orderCode, prType, price, volume,
               [strategyName, quickTrade, userOrderId, userOrderParam], ContextInfo)
```

- 使用「交易面板-程序交易-函数交易」中设置的下单类型；未修改默认值时与 passorder 一致。
- prType 赋值则优先使用；prType=-1 时使用 userOrderParam 内的 PriceType；userOrderParam 未赋值则用界面函数交易参数的报价方式。

userOrderParam（dict，全部可选）：

| Key | 类型 | Value |
|---|---|---|
| OrderType | int | 0 普通 / 1 算法 / 2 随机量交易 |
| PriceType | int | 报价方式，数值同 passorder prType |
| MaxOrderCount | int | 最大下单次数 |
| SinglePriceRange | int | 波动区间是否单向：0 否 / 1 是 |
| PriceRangeType | int | 波动区间类型：0 按比例 / 1 按数值 |
| PriceRangeValue / PriceRangeRate | float / float | 波动区间（数值）/（比例 0-1） |
| SuperPriceType | int | 单笔超价类型：0 按比例 / 1 按数值 |
| SuperPriceRate / SuperPriceValue | float | 单笔超价（比例 0-1 / 数值） |
| VolumeType | int | 单笔基准量类型：0-4 卖5..卖1量；5-9 买1..买5量；10 目标量；11 目标剩余量；12 持仓数量 |
| VolumeRate | float | 单笔下单比率 0-1 |
| SingleNumMin / SingleNumMax | float | 单笔下单量最小 / 最大值 |
| ValidTimeType | int | 0 按持续时间 / 1 按时间区间 |
| ValidTimeElapse | int | 有效持续时间（type=0 时生效） |
| ValidTimeStart / ValidTimeEnd | int | 有效起 / 止时间偏移（type=1 时生效） |
| UndealtEntrustRule | int | 未成委托处理，数值同 prType |
| PlaceOrderInterval | int | 下撤单时间间隔 |
| UseTrigger | int | 是否触价：0 否 / 1 是 |
| TriggerType | int | 触价类型：1 最新价大于 / 2 最新价小于 |
| TriggerPrice | float | 触价价格 |
| SuperPriceEnable | int | 超价启用笔数 |

```python
userparam = {"OrderType": 1, "MaxOrderCount": 20, "SuperPriceType": 1, "SuperPriceValue": 1.12}
algo_passorder(23, 1101, accid, '000001.SZ', 5, 15, 1000, '', 1, 'strReMark', userparam, C)
# 最大委托次数 20、单笔超价 1.12 元，其余同界面函数交易参数
```

### 6.3 smart_algo_passorder — 智能算法（VWAP/TWAP 等）

需【智能算法】权限。

调用方法一：

```python
smart_algo_passorder(opType, orderType, accountid, orderCode, prType, price, volume,
                     strageName, quickTrade, userOrderId, smartAlgoType, limitOverRate,
                     minAmountPerOrder, [targetPriceLevel, startTime, endTime, limitControl], ContextInfo)
```

（签名中 `strageName` 为官方原文拼写。）

| 参数 | 类型 | 说明 |
|---|---|---|
| prType | int | 11 限价（仅单股）/ 12 市价 |
| smartAlgoType | str | 算法类型，如 'VWAP'、'TWAP' |
| limitOverRate | int | 量比 0-100（algoParam 中填写时为 0-1 小数）；网格算法无此项 |
| minAmountPerOrder | int | 最小委托金额 0-100000 |
| targetPriceLevel | int | 目标价格：1-5 己方盘口 1-5；6 最新价；7 对方盘口。仅冰山算法用，无效值按 1 |
| startTime / endTime | str | 'HH:MM:SS'，缺省 09:30:00 / 15:30:00 |
| limitControl | int | 涨跌停控制，默认 1：1 涨停不卖跌停不买；0 无限制 |

调用方法二（algoParam 版，参数均不可缺省）：

```python
smart_algo_passorder(opType, orderType, accountid, orderCode, prType, modelprice, volume,
                     strageName, quickTrade, userid, smartAlgoType, startTime, endTime,
                     algoParam, ContextInfo)
```

```python
# 先查参数定义
print(get_smart_algo_param(['VWAP']))
algoParam = {
    'm_dLimitOverRate': 0.25,       # 量比 25%（单位为 % 时值填小数）
    'm_dMinAmountPerOrder': 0,      # 委托最小金额
    'm_dMaxAmountPerOrder': 10000,  # 委托最大金额
    'm_nStopTradeForOwnHiLow': 1,   # 涨跌停控制 0/1
    'm_dMulitAccountRate': 0.30,    # 多账号总量比
    'm_strCmdRemark': '投资备注1',
}
smart_algo_passorder(23, 1101, account, '600000.SH', 12, 0, 10000, '', 2, '投资备注',
                     'VWAP', "10:25:00", "14:50:00", algoParam, C)
```

### 6.4 get_smart_algo_param — 获取智能算法参数配置

```python
get_smart_algo_param(algoList)   # algoList 为算法名列表，空则查全部有权限的算法
```

返回 `dict{算法名: [参数dict...]}`，参数 dict 字段：key（algoParam 需传的键）、name、dataType、valueRange、defaultValue、enumName、enumValue、unit（单位为 % 时值要填小数而非百分数）、valueRangeByName、defaultValueByName。

### 6.5 cancel — 撤销委托

```python
cancel(orderId, accountId, accountType, ContextInfo)
```

orderId：委托号（`m_strOrderSysID`）；accountType 见 2.2。返回 bool（是否发出撤单信号）。

```python
def handlebar(C):
    if C.is_last_bar():
        orderid = get_last_order_id(C.accid, 'stock', 'order')
        print(cancel(orderid, C.accid, 'stock', C))
```

标准下单-查单-撤单流程：passorder 下单 → `get_last_order_id` 取最新委托号（自行保存）→ `get_value_by_order_id` 查状态 → 状态变化后用 cancel 撤单。委托列表与成交列表中的委托号均为 `m_strOrderSysID`。

### 6.6 cancel_task / pause_task / resume_task — 任务操作

```python
cancel_task(taskId, accountId, accountType, ContextInfo)   # 撤销，返回 bool
pause_task(taskId, accountId, accountType, ContextInfo)    # 暂停智能算法任务，返回 bool
resume_task(taskId, accountId, accountType, ContextInfo)   # 继续已暂停任务，返回 bool
```

taskId 从 `get_trade_detail_data(accid, type, 'task')` 返回对象的 `m_nTaskId` 获取。

```python
def handlebar(C):
    if C.is_last_bar():
        for obj in get_trade_detail_data(C.accid, 'stock', 'task'):
            cancel_task(str(obj.m_nTaskId), C.accid, 'stock', C)
```

### 6.7 get_basket / set_basket — 股票篮子

```python
set_basket(basketDict)    # {'name': 篮子名, 'stocks': [{'stock': 代码, 'weight': 权重,
                          #   'quantity': 数量, 'optType': 交易类型(23买/24卖)}]}
get_basket(basketName)
```

```python
table = [{'stock': '600000.SH', 'weight': 0.11, 'quantity': 100, 'optType': 23},
         {'stock': '600028.SH', 'weight': 0.11, 'quantity': 200, 'optType': 24}]
set_basket({'name': 'basket1', 'stocks': table})
# 一键买卖 2 份（2101 = 按篮子 quantity 字段），即 600000.SH 买 200 股、600028.SH 卖 400 股
passorder(35, 2101, C.accid, 'basket1', 5, -1, 2, 'basketOrder', 2, 'basketOrder', C)
# 按权重下单（2102），总额 10000 元
passorder(35, 2102, C.accid, 'basket2', 5, -1, 10000, '', 2, 'strReMark', C)
```

### 6.8 get_trade_detail_data — 查询账号资金 / 委托 / 成交 / 持仓 / 任务

```python
get_trade_detail_data(accountID, strAccountType, strDatatype[, strategyName])
```

| 参数 | 说明 |
|---|---|
| accountID | 资金账号 |
| strAccountType | 账号类型，见 2.2 |
| strDatatype | `'ACCOUNT'` 账号（Account / CCreditAccountDetail）；`'POSITION'` 持仓；`'POSITION_STATISTICS'` 持仓统计；`'ORDER'` 委托；`'DEAL'` 成交；`'TASK'` 任务 |
| strategyName | 选填，只对委托/成交有效；传入与下单时相同的 strategyName 只返回该策略的子集 |

返回 list，元素为对应 Python 对象（字段见第 3 章，`dir(obj)` 可列出属性）。注意读取的是本地缓存（见 1.5）。

```python
def handlebar(C):
    if not C.is_last_bar():
        return
    orders = get_trade_detail_data(account, 'stock', 'order')
    for o in orders:
        print(o.m_strInstrumentID, o.m_nOffsetFlag,          # 48 买入 / 49 卖出
              o.m_nVolumeTotalOriginal, o.m_dTradedPrice)
    positions = get_trade_detail_data(account, 'stock', 'position')
    for p in positions:
        print(p.m_strInstrumentID, p.m_nVolume, p.m_nCanUseVolume, p.m_dOpenPrice)
    acct = get_trade_detail_data(account, 'stock', 'account')[0]
    print(acct.m_dBalance, acct.m_dAvailable)
```

### 6.9 get_history_trade_detail_data — 查询历史交易明细

```python
get_history_trade_detail_data(accountID, strAccountType, strDatatype, startDate, endDate)
```

strDatatype：'POSITION' / 'ORDER' / 'DEAL'；日期如 '20240513'。返回 `[(timetag, [obj, ...]), ...]`。

### 6.10 get_ipo_data / get_new_purchase_limit — 新股新债

```python
get_ipo_data(type="")          # "" 新股新债 / "STOCK" 新股 / "BOND" 新债
get_new_purchase_limit(accid)  # -> dict 上海主板/深圳市场/上海科创板申购额度（股票或信用账号）
```

get_ipo_data 返回 dict：申购代码、申购名称、issuePrice（发行价）、maxPurchaseNum（最大申购数量）等。

```python
def init(C):
    for stock, info in get_ipo_data("STOCK").items():
        passorder(23, 1101, account, stock, 11, info['issuePrice'],
                  info['maxPurchaseNum'], '新股申购', 2, stock, C)
```

### 6.11 get_value_by_order_id / get_last_order_id — 委托号查询

```python
get_value_by_order_id(orderId, accountID, strAccountType, strDatatype)
# strDatatype: 'ORDER' / 'DEAL'，返回委托或成交对象

get_last_order_id(accountID, strAccountType, strDatatype[, strategyName])
# 返回最新委托号 str，未找到返回 '-1'
```

注意：下单后需一段不确定时间才能查到本次委托号；委托废单时只能查到上次成功下单的委托号。

```python
def handlebar(C):
    orderid = get_last_order_id(C.accid, 'stock', 'order')
    obj = get_value_by_order_id(orderid, C.accid, 'stock', 'order')
    print(obj.m_strInstrumentID)
```

### 6.12 get_assure_contract / get_enable_short_contract — 两融标的查询

```python
get_assure_contract(accId)          # -> list[StkSubjects] 担保标的明细（见 3.18）
get_enable_short_contract(accId)    # -> list[CreditSloEnableAmount] 可融券明细（见 3.16）
```

```python
def init(C):
    r = get_assure_contract('123456789')
    finable = [o.m_strInstrumentID + '.' + o.m_strExchangeID
               for o in r if o.m_eFinStatus == 48]   # 融资状态 48 = 正常
    print('可融资买入标的:', finable)
```

### 6.13 query_credit_account — 查询信用账户明细（异步）

从服务器查询，**建议间隔 ≥180s，不可频繁调用**；同时只能有一个查询，前一个未完成后查询会提前返回；必须配合 `credit_account_callback` 回调使用。

```python
query_credit_account(accountId, seq, ContextInfo)   # seq: int 查询序列号，建议唯一
def credit_account_callback(ContextInfo, seq, result):   # result: CCreditAccountDetail（见 3.14/3.15）
    print(result.m_dPerAssurescaleValue, result.m_dTotalDebt)
```

### 6.14 query_credit_opvolume — 查询两融最大可下单量（异步）

一次最多查 200 只股票；同时只能有一个查询；建议间隔 ≥180s；必须配合 `credit_opvolume_callback`。

```python
query_credit_opvolume(accountId, stockCode, opType, prType, price, seq, ContextInfo)
```

- stockCode 可为 list（多只）；price 为报价（非限价单可任意填），stockCode 为 list 时 price 需等长 list。
- opType / prType 同 passorder。

回调（ret 状态码：1 成功、-1 查询中、-2 账号非法、-3 参数非法、-4 超时/报错）：

```python
def credit_opvolume_callback(ContextInfo, accid, seq, ret, result):
    if ret == 1:
        print(result)   # 例：{'000001.SZ': 0}
```

### 6.15 get_option_subject_position / get_comb_option — 期权持仓查询

```python
get_option_subject_position(accountID)   # -> list[CLockPosition]（见 3.21）
get_comb_option(accountID)               # -> list[CStkOptCombPositionDetail]（见 3.22）
```

```python
for obj in get_comb_option('880399990383'):
    print(obj.m_strCombCodeName, obj.m_strCombID, obj.m_nVolume, obj.m_nFrozenVolume)
```

### 6.16 get_unclosed_compacts / get_closed_compacts — 负债合约查询

```python
get_unclosed_compacts(accountID, accountType)   # 未了结负债；accountType 填 'CREDIT'
get_closed_compacts(accountID, accountType)     # 已了结负债
```

返回 list[负债合约对象]，未了结对象字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| m_strAccountID | string | 账号 ID |
| m_nBrokerType | int | 1 期货 / 2 股票 / 3 信用 / 5 期货期权 / 6 股票期权 / 7 沪港通 / 11 深港通 |
| m_strExchangeID / m_strInstrumentID | string | 市场 / 证券代码 |
| m_eCompactType | int | 32 不限制 / 48 融资 / 49 融券 |
| m_eCashgroupProp | int | 32 不限制 / 48 普通头寸 / 49 专项头寸 |
| m_nOpenDate | int | 开仓日期（如 20201231） |
| m_nBusinessVol | int | 合约证券数量 |
| m_nRealCompactVol | int | 未还合约数量 |
| m_nRetEndDate | int | 到期日 |
| m_dBusinessBalance / m_dBusinessFare | float | 合约金额 / 息费 |
| m_dRealCompactBalance / m_dRealCompactFare | float | 未还合约金额 / 息费 |
| m_dRepaidFare / m_dRepaidBalance | float | 已还息费 / 金额 |
| m_strCompactId / m_strEntrustNo | string | 合约编号 / 委托编号 |
| m_nRepayPriority | int | 偿还优先级 |
| m_strPositionStr | string | 定位串 |
| m_eCompactRenewalStatus | int | 展期状态：48 可申请 / 49 已申请 / 50 审批通过 / 51 不通过 / 52 不可申请 / 53 已执行 / 54 已取消 |
| m_nDeferTimes | int | 展期次数 |

已了结对象另含 m_nDateClear（了结日期）、m_nEntrustVol / m_dEntrustBalance（委托数量/金额），无 m_nRealCompactVol、展期字段。

### 6.17 get_debt_contract — 两融负债合约【已弃用】

```python
get_debt_contract(accId)   # 已弃用，改用 get_unclosed_compacts / get_closed_compacts
```

### 6.18 get_hkt_exchange_rate — 沪深港通汇率

```python
get_hkt_exchange_rate(accountID, accountType)   # accountType 须为 HUGANGTONG / SHENGANGTONG
```

返回 dict：bidReferenceRate（买入参考汇率）、askReferenceRate（卖出参考汇率）、dayBuyRiseRate / daySaleRiseRate（日间买/卖参考汇率浮动比例）。

### 6.19 回测专用交易函数（仅回测生效，实盘/模拟盘不可用）

统一签名（`[]` 内可选）：

```python
func(stockcode, amount[, style, price], ContextInfo[, accId])
```

style 下单选价类型（默认 `'LATEST'` 最新）：`'LATEST'` 最新、`'FIX'` 指定（须传有效 price）、`'HANG'` 挂单（己方盘口一档）、`'COMPETE'` 对手、`'MARKET'` 市价、`'SALE5'`-`'SALE1'` 卖 5-1 价、`'BUY1'`-`'BUY5'` 买 1-5 价。股票六函数支持全部 15 种；期货六函数仅支持 LATEST/FIX/HANG/COMPETE/MARKET/SALE1/BUY1 七种。

| 函数 | 释义 |
|---|---|
| order_lots(stockcode, lots, ...) | 指定手数买卖，正买负卖 |
| order_shares(stockcode, shares, ...) | 指定股数买卖（最常用） |
| order_value(stockcode, value, ...) | 按金额买卖（元），股数取整到 100 的倍数；资金不足不下单 |
| order_percent(stockcode, percent, ...) | 按组合价值百分比（0-1 小数）买卖 |
| order_target_value(stockcode, tar_value, ...) | 调仓至目标市值（非负） |
| order_target_percent(stockcode, tar_percent, ...) | 调仓至目标占比（0-1） |
| buy_open(stockcode, amount, ...) | 期货买入开仓 |
| buy_close_tdayfirst / buy_close_ydayfirst | 期货买入平仓，平今优先 / 平昨优先 |
| sell_open(stockcode, amount, ...) | 期货卖出开仓 |
| sell_close_tdayfirst / sell_close_ydayfirst | 期货卖出平仓，平今优先 / 平昨优先 |

```python
def handlebar(C):
    order_lots('000002.SZ', 1, C, '600000248')                    # 最新价买 1 手
    order_lots('000002.SZ', -1, 'COMPETE', C, '600000248')        # 对手价卖 1 手
    order_lots('000002.SZ', -2, 'fix', 37.5, C, '600000248')      # 指定价卖 2 手
    order_value('000002.SZ', 10000, C, '600000248')               # 最新价买 10000 元
    order_target_percent('000002.SZ', 0.051, C, '600000248')      # 调仓至 5.1%
    buy_open('IF1805.IF', 1, C, '110476')                         # 期货最新价开多 1 手
```

---

## 7. 成交回报实时主推函数（回调）

> 通用约束（前 6 个回调共有）：**仅在实盘运行模式下生效；需先在 init 里调用 `ContextInfo.set_account(account)` 后生效**。回调对象字段见第 3 章，枚举见第 10 章。

| 回调签名 | 触发 | 第二参数对象 |
|---|---|---|
| `account_callback(ContextInfo, accountInfo)` | 资金账号状态变化 | Account / CCreditAccountDetail（3.9 / 3.14） |
| `task_callback(ContextInfo, taskInfo)` | 任务状态变化 | CTaskDetail（3.20） |
| `order_callback(ContextInfo, orderInfo)` | 委托状态变化 | Order（3.10） |
| `deal_callback(ContextInfo, dealInfo)` | 成交状态变化 | Deal（3.11） |
| `position_callback(ContextInfo, positonInfo)` | 持仓状态变化 | Position（3.12） |
| `orderError_callback(ContextInfo, orderArgs, errMsg)` | 下单异常 | orderArgs 为 PassorderArguments（3.19），errMsg 为错误信息 str |
| `credit_account_callback(ContextInfo, seq, result)` | query_credit_account 回执 | CCreditDetail（3.15），seq 为查询序列号 |
| `credit_opvolume_callback(ContextInfo, accid, seq, ret, result)` | query_credit_opvolume 回执 | ret 状态码见 6.14 |

（`position_callback` 参数名 `positonInfo` 为官方原文拼写。）

通用示例（以 order_callback 为例，其余替换回调名与对象即可）：

```python
#coding:gbk
def init(ContextInfo):
    ContextInfo.set_account(account)          # 必须先设置账号（策略交易界面运行）

def after_init(ContextInfo):
    # quickTrade=2 立即下单；编辑器界面运行不产生实际委托
    passorder(23, 1101, account, "000001.SZ", 5, 0, 100, "示例", 2, "投资备注", ContextInfo)

def order_callback(ContextInfo, orderInfo):
    print(orderInfo.m_strInstrumentID, orderInfo.m_nOrderStatus, orderInfo.m_strRemark)

def deal_callback(ContextInfo, dealInfo):
    print(dealInfo.m_strInstrumentID, dealInfo.m_dPrice, dealInfo.m_strRemark)
```

对象转 dict 的通用辅助（遍历 m_ 前缀属性）：

```python
def show_data(data):
    tdata = {}
    for ar in dir(data):
        if ar[:2] != 'm_':
            continue
        try:
            tdata[ar] = data.__getattribute__(ar)
        except:
            tdata[ar] = '<CanNotConvert>'
    return tdata
```

orderError_callback 触发示例（指定价但价格无效）：

```python
def after_init(ContextInfo):
    passorder(23, 1101, account, "000001.SZ", 11, 0, 100, "示例", 2, "投资备注", ContextInfo)

def orderError_callback(ContextInfo, orderArgs, errMsg):
    print(orderArgs.opType, orderArgs.prType, orderArgs.modelVolume)
    print(errMsg)   # 例：[函数交易] 函数: passorder, 证券 [SZ000001] 指定价 无效, 无法下单!
```

---

## 8. 引用函数

### 8.1 ext_data 系列 — 扩展数据

| 调用方法 | 返回 | 说明 |
|---|---|---|
| `ext_data(extdataname, stockcode, deviation, ContextInfo)` | number | 扩展数据当前值；deviation 为 K 线偏移（0 不偏移，N 向右，-N 向左） |
| `ext_data_rank(extdataname, stockcode, deviation, ContextInfo)` | number | 数值在所有品种中的排名 |
| `ext_data_rank_range(extdataname, stockcode, begintime, endtime, ContextInfo)` | dict | 指定时间区间内所有品种中的排名；时间格式 '2016-08-02 12:12:30'（含端点） |
| `ext_data_range(extdataname, stockcode, begintime, endtime, ContextInfo)` | dict | 指定时间区间内的值 |

```python
def init(C):
    print(ext_data('CR', '600000.SH', 0, C))
```

### 8.2 get_factor_value / get_factor_rank — 因子数据

| 调用方法 | 返回 |
|---|---|
| `get_factor_value(factorname, stockcode, deviation, ContextInfo)` | number |
| `get_factor_rank(factorname, stockcode, deviation, ContextInfo)` | 排名 |

### 8.3 call_vba / get_vba_func_result — VBA 模型结果

```python
call_vba(factorname, stockcode, [period, dividend_type, barpos], ContextInfo)   # 券商版，不推荐；返回 number
get_vba_func_result(func, stock_code, period='1d', start_time='', end_time='',
                    count=-1, dividend_type=None, extend_param={}, subscribe=True)   # 投研版，推荐
```

- func 可为 str 或 list[str]（vba 函数或公式代码）；period/dividend_type 见 2.4/2.5；extend_param 同 call_formula（含 `__basket`）；subscribe：True 历史加实时 / False 仅历史。
- 使用前需补充本地 K 线或分笔数据。
- get_vba_func_result 返回带日期索引的表格（DataFrame 风格）。

```python
def init(C):
    fml = "基准：=-1；档位：=1；bb:getoptcodebyno('','C',1,档位，基准，1，0,6); ..."
    d = get_vba_func_result(fml, '510050.SH', '1d', count=10)
    print(d)
```

---

## 9. 绘图函数

均为 ContextInfo 方法，返回无。颜色可选：blue / brown / cyan / green / magenta / red / white / yellow。

| 调用方法 | 参数说明 |
|---|---|
| `C.paint(name, value, index, line_style, color='white', limit='')` | name 指标名；value 数值；index 显示索引（-1 按主图索引）；line_style 线型（0 曲线 / 42 柱状线）；limit 画线控制（'noaxis' 不影响坐标 / 'nodraw' 不画线） |
| `C.draw_text(condition, position, text)` | 条件为真时在 position 位置显示文字 |
| `C.draw_number(cond, height, number, precision)` | 显示数字，precision 小数位数 0-7 |
| `C.draw_vertline(cond, number1, number2, color='', limit='')` | 在 number1 与 number2 之间绘垂直线 |
| `C.draw_icon(cond, height, type)` | 绘制图标，type 1 椭圆 / 0 矩形 |

```python
def init(C):
    realtimetag = C.get_bar_timetag(C.barpos)
    value = C.get_close_price('', '', realtimetag)
    C.paint('close', value, -1, 0, 'white', 'noaxis')
    C.draw_text(1, 10, '文字')
    C.draw_icon(1 > 0, value, 0)
```

---

## 10. 枚举常量

### 10.1 opType - 操作类型（passorder 第 1 参数）

期货/股指期权/商品期权——六键：0 开多；1 平昨多；2 平今多；3 开空；4 平昨空；5 平今空。
四键：6 平多（优先平今）；7 平多（优先平昨）；8 平空（优先平今）；9 平空（优先平昨）。
两键：10 卖出（有多仓优先平今，余量开空）；11 卖出（优先平昨）；12 买入（有空仓优先平今，余量开多）；13 买入（优先平昨）；14 买入（不优先平仓）；15 卖出（不优先平仓）。

股票/ETF/可转债（含港股通）：23 买入；24 卖出。

融资融券：27 融资买入；28 融券卖出；29 买券还券；30 直接还券；31 卖券还款；32 直接还款；33 担保品买入；34 担保品卖出。

组合交易：25 组合买入；26 组合卖出；35 普通账号一键买卖；36 信用账号一键买卖；40 期货组合开多；43 期货组合开空；46 期货组合平多（优先平今）；47 期货组合平多（优先平昨）；48 期货组合平空（优先平今）；49 期货组合平空（优先平昨）。（27/28/29/31/33/34 同两融。）

ETF 期权：50 买入开仓；51 卖出平仓；52 卖出开仓；53 买入平仓；54 备兑开仓；55 备兑平仓；56 认购行权；57 认沽行权；58 证券锁定；59 证券解锁。

ETF 申赎：60 申购；61 赎回。

专项两融：70 专项融资买入；71 专项融券卖出；72 专项买券还券；73 专项直接还券；74 专项卖券还款；75 专项直接还款。

可转债转股/回售：80 普通账户转股；81 普通账户回售；82 信用账户转股；83 信用账户回售。

### 10.2 orderType - 下单方式（passorder 第 2 参数）

期货不支持 1102/1202；对账号组的操作相当于对组内每个账号做同样操作。

单股（单账号）：1101 股/手方式；1102 金额（元）方式（只支持股票）；1113 总资产比例 [0~1]；1123 可用资金比例 [0~1]。

单股（账号组）：1201 股/手；1202 金额（只支持股票）；1213 总资产比例；1223 可用比例。

组合（单账号）：2101 按组合股票数量（volume 单位为篮子份）；2102 按组合股票权重（volume 单位为元）；2103 按账号可用（按可用资金比例 + 篮子权重分配，未填权重按等权；只对股票篮子）。

组合（账号组）：2201 按数量；2202 按权重；2203 按账号可用。

组合套利特殊设置：2331 按合约价值自动套利、按组合股票数量；2332 按权重；2333 按账号可用。（套利时 accountID 填 `'股票账号, 期货账号'`，orderCode 填 `'篮子名, 期货合约名'`，price 作套利比例 0~2。）

### 10.3 prType - 下单选价类型（passorder 第 5 参数）

| 数值 | 描述 |
|---|---|
| -1 | 无效（只对 algo_passorder 起作用） |
| 0-4 | 卖 5 / 卖 4 / 卖 3 / 卖 2 / 卖 1 价 |
| 5 | 最新价 |
| 6-10 | 买 1 - 买 5 价（组合不支持） |
| 11 | 指定价（只对单股，组合不支持） |
| 12 | 涨跌停价（对手方最远端价格） |
| 13 | 挂单价（本方一档价格） |
| 14 | 对手价（对方一档价格） |
| 18 | 市价最优价 [郑商所][期货]（不支持模拟交易） |
| 19 | 市价即成剩撤 [大商所][期货] |
| 20 | 市价全额成交或撤 [大商所][期货] |
| 21 | 市价最优一档即成剩撤 [中金所][期货] |
| 22 | 市价最优五档即成剩撤 [中金所][期货] |
| 23 | 市价最优一档即成剩转 [中金所][期货] |
| 24 | 市价最优五档即成剩转 [中金所][期货] |
| 26 | 限价即时全部成交否则撤单 [上/深交所][期权] |
| 27 | 市价即成剩撤 [上交所][期权] |
| 28 | 市价即全成否则撤 [上交所][期权] |
| 29 | 市价剩转限价 [上交所][期权] |
| 42 | 最优五档即时成交剩余撤销申报 [上交所/北交所][股票] |
| 43 | 最优五档即时成交剩转限价申报 [上交所/北交所][股票] |
| 44 | 对手方最优价格委托 [上交所/深交所/北交所][股票、期权] |
| 45 | 本方最优价格委托 [上交所/深交所/北交所][股票、期权] |
| 46 | 即时成交剩余撤销委托 [深交所][股票、期权] |
| 47 | 最优五档即时成交剩余撤销委托 [深交所][股票、期权] |
| 48 | 全额成交或撤销委托 [深交所][股票、期权] |
| 49 | 盘后定价 |

（18-29、42-48 不支持模拟交易中使用；原文无 15/16/17、25、30-41 等值。）

市价指令说明：上交所/北交所（42-45）市价类型时 price 为保护限价（0-9999），买入不高于、卖出不低于该价，price=0 时取对应涨跌停价；融券卖出与集合竞价阶段不允许市价指令；深交所市价申报只适用于有涨跌幅限制证券。

### 10.4 volume - 下单数量单位

由 orderType 值最后一位决定：

| 末位 | 单股下单 | 组合下单 |
|---|---|---|
| 1 | 股 / 手（股票: 股，股票期权: 张，期货: 手，可转债: 张，基金: 份） | 按组合股票数量（份） |
| 2 | 金额（元） | 按组合股票权重（元） |
| 3 | 比例（%） | 按账号可用（%） |

### 10.5 quickTrade - 快速下单

0 否；1 是（最新 bar 上调用即触发）；2 是（任何情况调用即触发）。详见 1.4。

### 10.6 enum_EEntrustBS - 买卖方向

48 买入/多；49 卖出/空；81 质押入库；66 质押出库。

### 10.7 EEntrustSubmitStatus - 报单状态

48 已提交；49 撤单已提交；50 修改已提交；51 已接受；52 报单被拒绝；53 撤单被拒绝；54 改单被拒绝。

### 10.8 enum_EEntrustTypes - 委托类别

48 买卖；49 查询；50 撤单；51 补单；52 确认；53 大宗；54 融资委托；55 融券委托；56 信用平仓；57 信用普通委托；58 撤单补单；59 行权；60 锁定；61 解锁；62 报价回购；63 放弃行权；64 协议回购；65 组合行权；66 构建组合策略持仓；67 解除组合策略持仓；68 转融通出借；69 转融通出借展期；70 转融通出借提前了结；71 跨市场场内；72 跨市场场外。

### 10.9 enum_EEntrustStatus - 委托状态

49 待报；50 已报（已报出到柜台待成交）；51 已报待撤；52 部成待撤；53 部撤（部分成交，剩余已撤）；54 已撤；55 部成；56 已成；57 废单（原因见 m_strCancelInfo）。

### 10.10 enum_EHedge_Flag_Type - 投保类型

49 投机；50 套利；51 套保。

### 10.11 enum_EFutureTradeType - 成交类型

48 普通成交；49 期权成交；50 OTC 成交；51 期转现衍生成交；52 组合衍生成交。

### 10.12 enum_EBrokerPriceType - 柜台价格类型

49 市价；50 限价；51 最优价；52 配股；53 转托；54 申购；55 回购；56 配售；57 指定；58 转股；59 回售；60 股息；68 深圳配售确认；69 配售放弃；70 无冻质押；71 冻结质押；72 无冻解押；73 解冻解押；75 投票；77 预售要约解除；78 基金设红；79 基金申赎；80 跨市转托；81 ETF 申购；83 权证行权；84 对手方最优价格；85 最优五档即时成交剩余转限价；86 本方最优价格（原文 MIME_PRICE_FIRST）；87 即时成交剩余撤销；88 最优五档即时成交剩余撤销；89 全额成交并撤单；90 基金拆合；91 债转股；92 港股通竞价限价；93 港股通增强限价；94 港股通零股限价；101 直接还券；107 担保品划转；'j' 增发；'w'-'z' 全国股转定价/成交确认/互报成交确认/限价。

### 10.13 enum_EOffset_Flag_Type - 开平方向（m_nOffsetFlag）

-1 无效；48 买入/开仓；49 卖出/平仓；50 强平；51 平今；52 平昨；53 强减；54 本地强平；81 质押入库；66 质押出库；67 股票配股。（股票买卖识别：48 买 / 49 卖。）

### 10.14 两融相关枚举

- EXTSubjectsStatus（融资融券状态）：48 正常；49 暂停；50 作废。
- EXTCreditFundCtl（融资交易控制）：48 只允许融资买入；49 只允许卖券还款；50 都允许；51 都不允许。
- EXTCreditStkCtl（融券交易控制）：48 只允许融券卖出；49 只允许买券还券；50 都允许；51 都不允许。
- EXTSloTypeQueryMode（查询类型）：48 普通；49 专项。
- EXTCompactType（合约类型）：32 不限制；48 融资；49 融券。
- EXTCompactStatus（合约状态）：32 不限制；48 未归还；49 部分归还；50 已归还；51 自行了结；52 手工了结；53 未形成负债；54 已过期。
- EXTCompactBrushSource（头寸来源）：32 不限制；48 普通头寸；49 专项头寸。
- EXTSpecialAssure（担保品买入是否可用融券资金）：48 不允许；49 允许。

### 10.15 enum_EOperationType - 下单操作类型（CTaskDetail.m_eOperationType）

常用交易操作（0-74）：

| 值 | 描述 | 值 | 描述 |
|---|---|---|---|
| 0 | 开多 | 32 | 买入开仓（个股期权） |
| 1 | 平昨多 | 33 | 卖出平仓（个股期权） |
| 2 | 平今多 | 34 | 卖出开仓（个股期权） |
| 3 | 开空 | 35 | 买入平仓（个股期权） |
| 4 | 平昨空 | 36 | 备兑开仓（个股期权） |
| 5 | 平今空 | 37 | 备兑平仓（个股期权） |
| 6 | 优先平今多 | 38 | 认购行权（个股期权） |
| 7 | 优先平昨多 | 39 | 认沽行权（个股期权） |
| 8 | 平空优先平今 | 40 | 证券锁定（个股期权） |
| 9 | 平空优先平昨 | 41 | 证券解锁（个股期权） |
| 10 | 卖出优先平今 | 42-47 | 协议转让定价/成交确认/互报成交确认 买卖 |
| 11 | 卖出优先平昨 | 48 / 49 | 全国股转限价买入 / 卖出 |
| 12 | 买入优先平今 | 50 | 期货期权行权 |
| 13 | 买入优先平昨 | 51 / 52 | 可转债转股 / 回售 |
| 14 | 平多 | 53 / 54 | 股票配股 / 增发 |
| 15 | 平空 | 55 / 56 | 担保品划入 / 划出 |
| 16 / 17 | 开仓 / 平仓 | 57-64 | 大宗意向/定价/成交/盘后定价 买卖 |
| 18 / 19 | 买入 / 卖出 | 65-68 | 黄金交割、中立仓买卖 |
| 20 | 融资买入 | 69 | 组合交易一键买卖 |
| 21 | 融券卖出 | 70 / 71 | 组合交易港股通买入 / 卖出 |
| 22 | 买券还券 | 72 | 零股卖出 |
| 23 | 直接还券 | 73 / 74 | ETF 成份股买入 / 卖出 |
| 24 | 卖券还款 | 200-204 | 场外基金认购/申购/赎回/转换/分红方式变更 |
| 25 | 直接还款 | 205-210 | 场外协议/非协议存款及询价、活期、存单支取 |
| 26 / 27 | 基金申购 / 赎回 | 230 / 231 | 网下询价 / 申购 |
| 28 / 29 | 基金合并 / 分拆 | 1001-1003 | 场外转账入金/出金/互转 |
| 30 / 31 | 质押入库 / 出库 | 1004 / 1005 | ETF 申购 / 赎回 |

其余为场外/固收/银行间等机构业务操作码（1006-1157）：外盘买卖（1006-1009）、专项两融（1010-1012、1022-1024）、全国股转两网及退市/盘后（1013-1014、1027-1030）、ETF 套利（1031）、报价回购（1032-1036）、大宗成交申报配对（1037-1038）、期货期权放弃行权（1039）、一键划转（1040-1042）、盘后定价买卖（1043-1044）、协议回购系列（1045-1077）、现券买卖（1052-1053）、买断式回购（1054-1057）、分销买入（1058）、利率互换（1059-1060）、银行间转托管（1061-1062）、协议回购意向申报系列（1063-1076）、债券分销（1078）、优先股竞价（1079-1080）、转托管（1081-1082）、同业拆借（1083-1086）、理财产品申赎/认购（1087-1088、1098）、期权组合行权/构建/解除（1089-1091）、协议回购逆回购系列（1092-1096）、债券投标（1097）、北交所交易（1099-1104）、转融通出借（1105-1108）、跨市场 ETF 申赎（1109-1112）、券源预约（1113）、网下申购（1114-1117）、债券回售（1118）、债券借贷（1119-1123）、融券通预约融入/融出（1124-1125）、固收业务点击成交/协商成交/询价成交/竞买成交系列（1126-1153）、期权优先平仓（1154-1155）、资金划入/划出（1156-1157）。

### 10.16 enum_EOrderType - 交易类型（CTaskDetail.m_eOrderType）

0 常规；1 算法交易；2 随机量交易；3 算法交易3；4 中信建投算法；5 隔时交易；6 普通交易触价单笔委托；7 算法交易触价单笔委托；8 中信证券算法；9 金纳算法；10 爵士算法；11 智能VWAP；12 智能TWAP；13 智能算法；14 华创算法；15 华润算法；16 回转算法；17 主动算法；18 广发算法。

### 10.17 enum_EPriceType - 报价方式（CTaskDetail.m_ePriceType）

0-10 卖5-卖1、最新价、买1-买5（同 prType 0-10）；11 指定价；12 市价_涨跌停价；13 挂单价；14 对手价；15 自动盘口；16 昨收价；17 大宗加权平均价；18 市价_最优价；19 市价_即成剩撤；20 市价_全额成交或撤；21 市价_最优1档即成剩撤；22 市价_最优5档即成剩撤；23 市价_最优1档即成剩转；24 市价_最优5档即成剩转；25 询价；26 限价即时全部成交否则撤单；27 市价即时成交剩余撤单；28 市价即时全部成交否则撤单；29 市价剩余转限价；30-34 卖6-卖10；35-39 买6-买10；40 涨停价；41 跌停价；42 最优五档即时成交剩余撤销；43 最优五档即时成交剩转限价；44 对手方最优价格委托；45 本方最优价格委托；46 即时成交剩余撤销委托；47 最优五档即时成交剩余撤销委托；48 全额成交或撤销委托；49 盘后定价申报。

### 10.18 enum_ETaskStatus - 任务状态

0 未知；1 等待；2 提交中；3 执行中；4 暂停；5/6 撤销中/异常撤销中（已弃用）；7 完成；8 已撤；9 打回；10 异常终止；11 放弃（组合交易放弃补单）；12 强制终止（已弃用）。

---

## 11. 典型示例速查

### 11.1 passorder 各品种下单速查

```python
#coding:gbk
def handlebar(C):
    if not C.is_last_bar():
        return
    # 股票：最新价买/卖 100 股；沪市市价 42（price 为保护限价，0=涨跌停价）；京市按 101 股整数倍
    passorder(23, 1101, 'test', '600000.SH', 5, 0, 100, '', 1, '', C)
    passorder(24, 1101, 'test', '600000.SH', 42, 0, 100, '', 1, '', C)
    passorder(23, 1101, 'test', '430047.BJ', 5, 0, 101, '', 1, '', C)
    # ETF：最新价买入 2000 份
    passorder(23, 1101, 'test', '510050.SH', 5, -1, 2000, C)
    # 可转债：最新价买入 20 张
    passorder(23, 1101, 'test', '128123.SZ', 5, -1, 10, 1, C)
    # 基金申赎：申购/赎回 1 个单位中证500ETF
    passorder(60, 1101, 'test', '510030.SH', 5, 0, 1, 2, C)
    passorder(61, 1101, 'test', '510030.SH', 5, 0, 1, 2, C)
    # 两融：担保品买入（33）/ 融资买入（27），指定价 7 元
    passorder(33, 1101, 'test', '000001.SZ', 11, 7, 100, C)
    passorder(27, 1101, 'test', '000001.SZ', 11, 7, 100, C)
    # 期货：开多 rb2401 10 手（最新价）；指定价开空 MA401；四键平多 IF2311 优先平今
    passorder(0, 1101, 'test', 'rb2401.SF', 5, -1, 10, 1, C)
    passorder(3, 1101, 'test', 'MA401.ZF', 11, 3000, 10, 1, C)
    passorder(6, 1101, 'test', 'IF2311.IF', 5, -1, 2, 1, C)
    # 期权：最新价买入开仓 / 卖出平仓 2 张
    passorder(50, 1101, 'test', '10005330.SHO', 5, -1, 2, 1, C)
    passorder(51, 1101, 'test', '10005330.SHO', 5, -1, 2, 1, C)
    # 两融直接还款：金额 10000，代码任意占位
    passorder(32, 1101, account, '000001.SZ', 5, 0, 10000, 2, C)
```

### 11.2 quickTrade 三种写法对比

```python
c = 0
def init(C):
    # 立即下单：最新价买 100 股并指定投资备注
    passorder(23, 1101, account, s, 5, 0, 100, '1', 2, 'tzbz', C)

def handlebar(C):
    if not C.is_last_bar():
        return                      # 历史 K 线不发实盘信号
    global c
    c += 1
    if c == 1:
        passorder(23, 1101, account, s, 11, 14.00, 100, 1, C)     # 指定价 14 元，最新K线立即下单
        passorder(23, 1101, account, s, 5, -1, 100, 0, C)         # 最新价，K线走完下单
        passorder(23, 1102, account, s, 5, 0, 1000, 2, C)         # 按金额 1000 元，立即下单
```

### 11.3 集合竞价下单（定时器 + 立即下单）

```python
import time
c = 0
s = '000001.SZ'
def init(C):
    C.run_time("myHandlebar", "5nSecond", "2019-10-14 13:20:00")
def myHandlebar(C):
    global c
    now = time.strftime('%H%M%S')
    if c == 0 and '092500' >= now >= '091500':
        c += 1
        passorder(23, 1101, account, s, 11, 14.00, 100, 2, C)
def handlebar(C):
    return
```

### 11.4 投资备注（userOrderId）用法

有且只有 passorder / algo_passorder / smart_algo_passorder 支持投资备注（任意字符串，长度 < 24），用于匹配委托与成交：

```python
def init(C):
    C.set_account(account)
    passorder(23, 1101, account, '000001.SZ', 5, 0, 100, '', 2, get_new_note(), C)
    orders = get_trade_detail_data(account, accountType, 'order')
    print([o.m_strRemark for o in orders])       # 投资备注
    print([o.m_strOrderSysID for o in orders])   # 委托号

def order_callback(C, O):
    print(O.m_strRemark, O.m_strOrderSysID)
def deal_callback(C, D):
    print(D.m_strRemark, D.m_strOrderSysID)
```

### 11.5 Level2 数据订阅

```python
#coding:gbk
def l2_quote_callback(data):
    for s in data:
        print('lv2快照', s, data[s])

def init(C):
    C.stock = C.stockcode + '.' + C.market
    C.sub_nums = []
    for field, cb in [('l2quote', l2_quote_callback), ('l2quoteaux', cb2),
                      ('l2transaction', cb3), ('l2order', cb4),
                      ('l2transactioncount', cb5), ('l2orderqueue', cb6)]:
        C.sub_nums.append(C.subscribe_quote(C.stock, field, result_type='dict', callback=cb))

def stop(C):
    for num in C.sub_nums:          # 不再需要时反订阅释放资源
        C.unsubscribe_quote(num)
```

查询式取 L2（get_market_data_ex 定期查最新）：

```python
def handlebar(C):
    if not C.is_last_bar():
        return
    df = C.get_market_data_ex([], [C.stock], period='l2transaction', count=10)[C.stock]
    for t, row in df.to_dict('index').items():
        print(t, row['price'], row['volume'], row['buyNo'], row['sellNo'], row['tradeFlag'])
```

### 11.6 止盈止损 / 涨停开板监控（handlebar + get_full_tick）

```python
def init(C):
    C.sell_code = 24 if accountType == 'STOCK' else 34
    C.spare_list = C.get_stock_list_in_sector('不卖品种')

def handlebar(C):
    if not C.is_last_bar():
        return
    holdings = get_trade_detail_data(account, accountType, 'position')
    stock_list = [h.m_strInstrumentID + '.' + h.m_strExchangeID for h in holdings]
    if not stock_list:
        return
    full_tick = C.get_full_tick(stock_list)
    for h in holdings:
        stock = h.m_strInstrumentID + '.' + h.m_strExchangeID
        rate, volume = h.m_dProfitRate, h.m_nCanUseVolume
        if volume < 100 or stock in C.spare_list:
            continue
        if rate < -0.1:                                   # 低于买入价 10% 止损
            msg = f'{stock} 盈亏比例 {rate} 小于-10% 卖出 {volume}股'
            passorder(C.sell_code, 1101, account, stock, 14, -1, volume, '减仓模型', 2, msg, C)
            continue
        if stock in full_tick:
            q = full_tick[stock]
            stop_price = round(q['lastClose'] * (1.2 if stock[:2] in ['30', '68'] else 1.1), 2)
            ask3 = q['bidPrice'][2]
            if not ask3:
                continue                                  # 无五档行情时跳过
            if q['high'] == stop_price and q['lastPrice'] < stop_price:   # 涨停后开板
                passorder(C.sell_code, 1101, account, stock, 14, -1, volume,
                          '减仓模型', 2, f'{stock} 涨停开板 卖出', C)
```

### 11.7 定时器统计市场涨跌（schedule_run）

```python
import datetime as dt

def on_timer(C):
    ticks = C.get_full_tick(globals().get("stock_list"))
    up = [s for s in ticks if ticks[s]["lastPrice"] > ticks[s]["lastClose"] and ticks[s]["openInt"] != 1]
    down = [s for s in ticks if ticks[s]["lastPrice"] < ticks[s]["lastClose"] and ticks[s]["openInt"] != 1]
    print(f"{dt.datetime.now():%Y%m%d %H:%M:%S}: 涨{len(up)} 跌{len(down)}")

def init(C):
    globals()["stock_list"] = get_stock_list_in_sector("沪深京A股")
    C.schedule_run(on_timer, '20231231235959', -1, dt.timedelta(seconds=60), 'my_timer')
    # C.cancel_schedule_run('my_timer')   # 取消任务组
```

### 11.8 python 写入扩展数据（投研版）

```python
def init(C):
    C.ext_name = create_extend_data('扩展数据', 'test', True)   # (父节点, 名称, 是否覆盖)

def handlebar(C):
    if C.is_last_bar():
        data = {'SH600177': 0.43, 'SZ000767': 0.18, ...}       # {市场+代码: 值}
        reset_extend_data_stock_list(C.ext_name, list(data.keys()))
        set_extend_data_value(C.ext_name, C.get_bar_timetag(C.barpos), data)
```

---

## 12. 行情数据概念与关键行为说明

### 12.1 三类行情数据

1. **本地数据**：下载到本地的加密行情文件（历史数据），适合回测。对应 `get_market_data_ex(subscribe=False)`、`get_local_data`。
2. **全推数据**：客户端启动后自动接收的全市场最新快照（含日线开高低收、成交量额与五档盘口，取决于行情源全推级别），只有最新值无历史，服务器即时转发增量。对应 `get_full_tick`（一次取出）、`subscribe_whole_quote`（回调增量）。受「交易中心」连接影响。
3. **订阅数据**：向行情服务器订阅指定品种，支持四种基本周期（分笔/1分钟/5分钟/日线，有 L2 权限可订 L2），当日数据实时更新，历史需下载。有最大订阅数量限制（超限返回前值填充的重复数据），需 VIP 提升上限。对应 `subscribe_quote`、`get_market_data_ex(subscribe=True)`。受「行情中心」连接影响。

### 12.2 行情函数选型对比

| 函数 | 说明 |
|---|---|
| download_history_data | 下载指定区间数据到本地；开始时间不填为增量下载 |
| get_local_data | 取本地数据，盘中不更新，速度快，回测用【不推荐，用 get_market_data_ex(subscribe=False)】 |
| get_full_tick | 取客户端缓存最新全推快照；无历史、免订阅、无数量限制、约 50ms 更新 |
| subscribe_quote | 向服务器订阅，盘中实时更新；初次订阅耗时长，数量受限；unsubscribe_quote 释放 |
| get_market_data_ex | 取订阅 + 本地数据；subscribe=False 纯本地。股票池大时可用 download_history_data + get_local_data + get_full_tick 拼接替代 |
| set_universe / get_history_data / get_market_data | 早期股票池订阅接口，无订阅号无法反订阅，**已不推荐** |

注意事项：

- gmd 系列函数（get_market_data / get_market_data_ex）在 init 中只能读本地数据，不建议在 init 中调用。
- 全推/订阅分笔默认可能只有最新价无五档，需在行情源设置中调高全推级别；passorder 对手价（prType=14）报「对手价无效」同此原因。
- handlebar 由主图品种分笔驱动：主图为股票时约 3 秒一次；期货高频需求应把主图设为期货，或改用 subscribe/run_time。
- 主力合约/过期合约相关函数（get_main_contract 历史模式、get_his_contract_list、get_st_status 等）需先在数据管理中补充「过期合约数据 / 历史主力合约 / 过期合约列表」。

### 12.3 其他关键行为

- 下单失败排查：确认在模型交易界面以实盘模式运行（模拟模式只显示信号）；确认 quickTrade 取值（默认 0 在日线周期全天不委托）；查看客户端左下角消息提示的报错。
- 回测复权建议：等比前复权（front_ratio），避免配股增发造成价格异常波动。
- `get_instrument_detail` 报「获取合约乘数和最小变动价位失败」：需下载过期合约列表数据。
- 回测模式下 run_time 定时器无效；subscribe 订阅类盘中逻辑亦不适用于回测遍历。



