# 延迟测试报告

> 测试环境为同一台本地机器和同一套 QMT 环境，仅用于判断量级，不代表固定承诺。
> 最后更新：2026-09-08（v0.3.28）

## 方法论

- **端到端**：客户端发起 RPC → 传输层 → QMT 策略处理 → 返回，含全部编解码和网络开销。
- **样本量**：每种方法 n=15~100。
- **交易时段影响**：非交易时间、午间休市或 QMT 刚启动时，行情源、柜台连接和回调节奏不活跃，请求耗时明显高于交易时段。本文数字如未注明均为交易时段测得。
- **QMT 主线程 GIL**：读取类请求在 QMT 主线程（adjust/drain）上串行处理，与行情回调共享 GIL——这是尖峰的来源，不是网络问题。
- **跨线程交接的代价**：后台接收线程从阻塞调用返回后要重新拿 GIL，实测每次约一个 adjust tick（~100ms）。这是 zmq / pipe 在后台线程模式下慢的原因，`rpc_background_threads: False`（adjust 线程自己非阻塞轮询）就绕开了它。redis 不受影响——它的 `brpop` 唤醒是即时的。

## 传输层对比（端到端，真实 QMT 进程）

2026-09-08 盘中，同一台实盘终端，`schedule_adjust_interval: "100nMilliSecond"`。
**覆盖 100 个只读接口，每个跑 5 次取中位，再对全部方法取分位** —— 不是挑一两个
快的报数。每换一种配置都重启策略后现测。

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

**redis 是唯一后台线程更快的。** 它的 `brpop` 阻塞唤醒是即时的；zmq / pipe 的
后台线程都要付跨线程 GIL 交接的代价，每次交接约一个 adjust tick（~100ms）。
默认值已按传输分别选对，没有特别理由不要改。

所有渠道的「最快」都是 0.2~0.3ms —— **线本身都不是瓶颈**，差距全在唤醒机制上。

### 数据一致性

六种渠道返回的**数据完全一致**：100 个方法逐项比对结构指纹（字段名 + 嵌套形状）
零差异；另取 14 个方法做 sha256 全精度逐字节比对（zmq vs redis），也是零差异。
**选传输只影响延迟，不影响数据。**

**结论**：生产用 **redis + 后台线程**（默认值，最快、跨机、稳定）。装不了 redis
用 **zmq + drain**。连 zmq 都装不了（import 白名单拒 socket、禁止 pip）才用
**pipe** —— 它不是为了快，是为了在没有别的线时还能用。

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

> 这两个数是**早前一批**测的，别拿去跟上面那张表的 redis 3.4ms 直接算比值：
> 上面是 100 个只读方法的 p50（里面有不少本地就能答的便宜方法），这里是同一组
> 行情方法的对比。跨批次只有量级可比 —— 无论用哪个 redis 数，直连都快一到两个
> 数量级，这个结论不变。

## 下单链路

| 环节 | 耗时 | 说明 |
|------|------|------|
| `order_stock_async` 返回 seq | **<1ms** | 本地排队，不碰网络（#50） |
| 提交到 QMT（passorder 执行） | RPC 单跳（redis ~3ms / zmq+drain ~16ms）+ adjust 等待 | 下单 RPC 在 QMT 主线程串行处理 |
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
