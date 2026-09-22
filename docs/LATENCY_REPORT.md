# 延迟测试报告

> 测试环境为同一台本地机器和同一套 QMT 环境，仅用于判断量级，不代表固定承诺。
> 最后更新：2026-09-22（v0.3.52，#343 四种组合对照；0.3.28 那张 100 方法表是在重启后的
> 回放窗口里测的，见「方法论」，保留在下面只为对照）

## 方法论

- **端到端**：客户端发起 RPC → 传输层 → QMT 策略处理 → 返回，含全部编解码和网络开销。
- **样本量**：每种方法 n=15~100。
- **交易时段影响**：非交易时间、午间休市或 QMT 刚启动时，行情源、柜台连接和回调节奏不活跃，请求耗时明显高于交易时段。本文数字如未注明均为交易时段测得。
- **QMT 主线程 GIL**：读取类请求在 QMT 主线程（adjust/drain）上串行处理，与行情回调共享 GIL——这是尖峰的来源，不是网络问题。
- **跨线程交接的代价**：QMT 主线程在两次回调之间不放 GIL，后台接收线程从阻塞调用返回后每拿一次 GIL 约等一个 adjust tick（~100ms），一次往返里每条 Redis / zmq 命令都是一次。这是所有传输在后台线程模式下慢的原因，`rpc_background_threads: False`（adjust 线程自己非阻塞轮询）就绕开了它。redis **也**受影响，见下一节。
- **重启后的回放窗口（测之前先看这条）**：策略每次启动，QMT 先用 `handlebar` 把历史 K 线回放一遍，adjust 跟着每根 K 线跑一次——终端日志 `adjust cadence: ticks=51429 avg=0.000s over 10s`（2026-09-22 08:55:17 启动后的第一个窗口），也就是 **每秒 5000 拍**。这个窗口里 drain 和 GIL 释放都是亚毫秒级的，什么 RPC 都能测出毫秒级；回放结束后 `run_time` 才是标准的 10Hz（之后每个窗口都是 `ticks=100~105`，`[adjust_source] handlebar=0`）。**「重启策略后立刻测」得到的数字不是稳态数**——0.3.28 那张表（redis + 后台线程 3.4ms、交易查询 4ms、195 次/秒）就是这么来的。等日志里 cadence 回到 `ticks=100` 再测。

## 四种组合对照（2026-09-22，v0.3.50，#343）

同一台国金实盘终端，`100nMilliSecond`，每种组合重启策略后同一组只读探针各跑 20 轮，
客户端 `time.time()` 对服务端回包里的 `_t_recv` / `_t_reply` 拆段。min / median / max（ms）：

| 方法 | redis + 后台线程 | zmq + 后台线程 | zmq + drain | redis + drain |
|---|---|---|---|---|
| ping | 199 / 407 / 605 | 98 / 103 / 303 | 10 / 87 / 108 | 23 / 102 / 106 |
| get_full_tick（1 只） | 199 / 338 / 473 | 8 / 196 / 306 | 8 / 90 / 107 | 26 / 102 / 107 |
| get_instrument_detail | 132 / 208 / 373 | 99 / 195 / 211 | 7 / 88 / 104 | 33 / 102 / 107 |
| query_stock_positions | 102 / 197 / 320 | 396 / 490 / 600 | 8 / 88 / 103 | 85 / 103 / 109 |
| query_stock_orders | 33 / 175 / 200 | 399 / 493 / 613 | 4 / 88 / 105 | 86 / 102 / 109 |

拆段：drain 下每个请求 `handle` 0–1ms、`return` 1–5ms，时间全在等下一个 tick 拾取，
所以 max 卡在 ~105ms；后台线程下 `wait` / `return` 都是 100 的整数倍——每段一次 GIL
交接。往返次数决定延迟：redis 回包 SETEX + RPUSH + EXPIRE + PUBLISH 在两个客户端各做一遍
是 8 次 ≈ 400ms；zmq 的 ping 1–2 次；zmq 上走 adjust 的交易查询（收→队列→adjust→回
router 线程发）4–5 次 ≈ 500ms。

**结论：`rpc_background_threads` 一律 `False`。** 传输之间在 drain 下没有可感知的差别
（redis 比 zmq 高的十几毫秒 median 是 LPOP 落在 tick 里而不是 socket 立刻可读）。

## 传输层对比（端到端，真实 QMT 进程）

2026-09-08 盘中，同一台实盘终端，`schedule_adjust_interval: "100nMilliSecond"`。
**覆盖 100 个只读接口，每个跑 5 次取中位，再对全部方法取分位** —— 不是挑一两个
快的报数。每换一种配置都重启策略后现测。

> ⚠️ **「重启后现测」正是问题所在**：这些数落在回放窗口里（见方法论），不是稳态。
> redis + 后台线程的 3.4ms、redis + drain 的 30.7ms 在 10Hz 稳态下都不存在——稳态见
> 上面 2026-09-22 那张表：drain 约一拍（~100ms），后台线程每条命令一拍。这张表留着只
> 为记录当时的做法和「六个渠道数据一致」的结论。

| 传输 + 模式 | p50 | p90 | 最快 | 跨机 |
|------|-----|-----|------|------|
| **redis + 后台线程**（默认）| **3.4ms** | 25.2ms | 0.3ms | ✅ |
| **zmq + drain** | 15.8ms | 94.7ms | 0.2ms | ✅ |
| redis + drain | 30.7ms | 93.4ms | 0.2ms | ✅ |
| pipe + drain | 94.4ms | 95.6ms | 0.2ms | ❌ |
| pipe + 后台线程 | 189.0ms | 296.8ms | 0.2ms | ❌ |
| zmq + 后台线程 | 592.9ms | 697.5ms | 0.2ms | ✅ |
| mysql | ~105ms | — | — | ✅ |

### 比「选哪个传输」更要紧的：模式配对了没有

同一个传输，`rpc_background_threads` 配错差 **4~37 倍**：

```
zmq    592.9ms -> 15.8ms   （drain 快 37 倍）
pipe   189.0ms -> 94.4ms   （drain 快 2 倍）
redis    3.4ms -> 30.7ms   （drain 反而慢 9 倍）
```

**这张表里 redis + 后台线程的 3.4ms 从来不是稳态数。** 0.3.51 的报告曾把它解释成
「adjust 线程 LPOP 抢到请求的那部分，#321 关掉之后就没了」——这个解释是错的：10Hz 的
LPOP 给不出 3.4ms 的中位，更给不出 195 次/秒的串行吞吐。真正的原因是测量落在重启后的
回放窗口里（方法论第五条），那时 adjust 每秒跑几千拍。9/14 起每一天的终端日志里，后台
线程模式下由后台线程回的包 `ping breakdown publish=` 都是 200–900ms（回包 4–8 条 Redis
命令 × 一拍），#321 前后一样。

#321 真正改变的是（#351）：0.3.46 及之前，后台线程模式下 adjust 每拍也 LPOP 一次，后台
线程正忙着等 GIL 的时候请求会被 adjust 拿走、在 adjust 线程上一拍答完——所以那时的体感
是 **~100ms 和 500–900ms 混着来**；#321 关掉这次 LPOP 后全部落到后台线程，就全是
500–900ms（#343 的报告）。drain 模式（`rpc_background_threads: False`，0.3.51 起默认）
就是把「adjust 拿走」变成全部：每个请求一拍，没有 900ms 的尾巴。**配置里显式写着
`True` 的（`bigqmt-init` 0.3.50 及之前给 redis 写的就是 True）不会自动切，改成 `False`
重启一次。** 两个客户端（一个读行情、一个报单）同时打也在同一拍里各自答完，drain 一拍
最多处理 20 条。

#321 担心的「重读拖住策略拍」在 drain 下同样成立，0.3.53 起单独处理：重的读请求（财务数据、
公式，以及市场令牌 `get_full_tick`、超过 20 个代码、`period="tick"`、日期窗口 K 线）交给一条
工作线程跑，回包由 adjust 下一拍发出——重读多付 1~2 拍；轻读和交易查询照旧一拍。能减多少
取决于 QMT 那个 C 接口放不放 GIL，实测（2026-09-22，100ms 拍，每种读在工作线程上连打，看终端 `adjust cadence` 的 avg / max）：
全市场 `get_full_tick(["SH","SZ"])` 读本身 ~200ms，拍 0.100 / 0.25–0.33s；三只 4 个月 1m 窗口
~500ms，拍 0.100 / 0.27–0.29s；`get_financial_data` 10 只 3 表 ~2.1s，拍 0.100 / ~0.5s——QMT 的
C 接口大部分时间放 GIL，拍的均值不变、最坏从整段读缩到一段。`download_history_data2` 5 只 ~1.2s
整段持 GIL，拍 0.56–0.63 / 1.3–1.5s，工作线程帮不上、回包还多付一两拍，所以 `download_*` 不列入，
照旧 adjust 线程。
`rpc_heavy_offload: False` 关掉。

所有渠道的「最快」都是 0.2~0.3ms —— **线本身都不是瓶颈**，差距全在唤醒机制上。

### 数据一致性

六种渠道返回的**数据完全一致**：100 个方法逐项比对结构指纹（字段名 + 嵌套形状）
零差异；另取 14 个方法做 sha256 全精度逐字节比对（zmq vs redis），也是零差异。
**选传输只影响延迟，不影响数据。**

**结论（2026-09-22 更新）**：生产用 **redis + drain**（跨机、回报有 stream 回放，
延迟和 zmq 同档）。装不了 redis 用 **zmq + drain**。连 zmq 都装不了（import 白名单拒
socket、禁止 pip）才用 **pipe** —— 它不是为了快，是为了在没有别的线时还能用。
四种都别开后台线程。

> **这张表在 0.3.21 之前是反的**，写着 zmq「同机低延迟 p50~0.7ms」、redis 13ms。
> 那个 0.7ms 是撞上 adjust 空窗的最好情况，不是 p50；redis 的 13ms 一直是准的。
> 旧版本还把 zmq 的尖峰归因为「GIL 调度」并当成它的固有代价 —— 实际上换成
> drain 模式就从 592.9ms 降到 15.8ms，那不是固有的，是**配置错了**。

## FormulaServer 直连快速路径（只读行情）

大 QMT 的 FormulaServer（58600 端口）可以绕过策略进程直接读行情：

| 路径 | 典型耗时 | 说明 |
|------|---------|------|
| FormulaServer 直连 | **~0.07ms** | 仅 dividend_type=none 的只读方法（10 个） |
| Redis RPC 同方法 | ~13ms | 走策略进程 |

同批测量里约 **180 倍**差距。默认开启，失败自动回落 RPC 桥。tick/L2 周期和复权
读取不在此路径（v0.2.9 起拒绝路由，由 RPC 桥正确回答）。

> 这两个数是**早前一批**测的，别拿去跟上面任何一张表直接算比值。稳态下 RPC 桥的
> 地板是一拍（~100ms），直连 0.07ms —— 无论用哪个 redis 数，直连都快两到三个
> 数量级，这个结论不变。

## 下单链路

| 环节 | 耗时 | 说明 |
|------|------|------|
| `order_stock_async` 返回 seq | **<1ms** | 本地排队，不碰网络（#50） |
| 提交到 QMT（passorder 执行） | RPC 单跳（drain 下约一拍 ~100ms）+ passorder 本身 ~200ms | 下单 RPC 在 QMT 主线程串行处理 |
| 委托号回填（结算） | 通常 <1s | 停放应答 + adjust tick 轮询（#44），有 deadline 兜底 |
| `on_order_stock_async_response` 回调 | 提交后 ~0.3~2s | 等屏障从推送事件学到委托号（#72，bounded 2s） |
| `on_stock_order`/`on_stock_trade` 推送 | 柜台回报后 ~ms 级 | Redis pub/sub，保序在 async_response 之后（#51） |

**注意**：下单类 RPC 无法并行化——`get_trade_detail_data` 离开 QMT 主线程返回空，结算只能在 adjust tick 上做。批量提交用 `order_stock_batch`；读密集流量用 `call_async`（v0.2.6+）叠加往返延迟。

## 复现

```powershell
python bench_latency.py            # Redis 单传输延迟
python bench_transports.py -n 100  # Redis vs ZMQ 对比
python bench_zmq_spike.py          # ZMQ GIL 尖峰分析
```

上面这张六渠道表的做法：改 `bigqmt_signal_trader_local_config.py` 的
`transport` 和 `rpc_background_threads`，**每换一次重启策略**（这两项是入口
exec 时读的，`reload_deployment()` 刷不了），然后对 100 个只读方法各调 5 次
取中位。判断模式是否真的生效**只能看日志**：

```
[bigqmt_rpc] started queue=... background_threads=False
```

配置写了 `False` 不代表生效——0.3.28 之前 `_resolve_background_threads` 里有一张
硬编码名单，不在名单上的传输会被强制回 `True`，而日志是唯一能发现这件事的地方。

## 与社区方案的量级参考

2026-09-08 实测 [cfquant](https://github.com/95ge/cfquant)（MIT，named pipe + 中继
hub），把它的 QMT 侧挂进同一台终端、用它自己的客户端打：

| 通道 | `query_stock_positions` / `get_positions` |
|------|------|
| cfquant（named pipe + hub）| 92.8ms |
| 本项目 `transport="pipe"` + drain | 94.8ms |
| 本项目 `transport="redis"`（默认）| **3.8ms** |

**同样走命名管道，两边基本打平（92.8 vs 94.8）**，差距不在实现好坏，在传输选型 ——
换成 redis 就快 24 倍。

而且两边**都不是管道本身的极限**：拿回显桩单测传输层，cfquant 0.822ms、本项目
0.207ms（它多一跳 hub），管道裸往返只要 0.012ms。也就是说 92ms 里有 99% 花在
**QMT 侧的唤醒调度**上，跟线缆无关 —— 这也解释了为什么两个独立实现会撞到同一个数。

测的是它的 **Pipe 普通桥**（日志 `Pipe 普通桥已启动`，即默认入口），它起一个后台
接收线程、`dispatch_on_qmt_thread=False`。**它的低延迟变体没有单独测** ——
`CFQUANT_TRADE_LOWLAT.py` 在 `init()` 里 `run_forever(sleep_seconds=0.001)`
**永不返回，用 1ms 轮询霸占主策略线程**，既拿到主线程上下文又避开 tick 节奏，
代价是那个策略不能再做别的事。上面 92.8ms 不代表它的最好成绩。

（另一个入口 `CFQUANT.py` 走 `schedule_run(interval=500ms)` 定时器，那条更慢，
也不是这次测的对象。）

它的价值不在速度：`ctypes` + `kernel32` 命名管道在**禁 socket、禁 pip 的券商
终端**上能跑通，这一点本项目 0.3.28 的 `transport="pipe"` 借鉴了同样的思路
（独立实现，见 PR #236）。
