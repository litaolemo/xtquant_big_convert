# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/) 和 [语义化版本](https://semver.org/)。


## [0.3.22] - 2026-09-05

### 新增

- **`passorder` 原生透传（路 2）**：新增客户端 `xt.passorder(opType, orderType, account, orderCode, prType, price, volume, strategyName, quickTrade, userOrderId)`，参数顺序与 QMT 原生 `passorder` 一致，ContextInfo 由服务端补（它只在 QMT 里存在，过不了 RPC）。和 `order_stock` 的区别是**暴露了 `orderType` 和 `quickTrade`**，并且**不跑任何安全网** —— 无 opType 校验（融资/期货 opType 由调用方负责，见 #103）、无代码大小写归一化（#95）、无结算回读。要的就是裸 API，契约归调用方。

  `orderType` / `quickTrade` 留空时回落到服务端配置（1101 / 2），所以常见情形写起来仍和 `order_stock` 一样。passorder 异步、不返回委托号（QMT 稍后在 order_callback 推送里给），同 `order_stock_async`。

  `dry_run=True` 返回服务端**将要**传给 passorder 的 11 元组而不下单 —— 用来核对参数映射，也是本条的实盘验证方式（不下真单）。

  服务端 `passorder` 走 `ORDER_METHODS`：和其它下单一样受 `rpc_allow_order_methods` 门禁、并 defer 到 adjust 主线程。新增 `tests/bigqmt_signal_trader/test_passorder_passthrough.py`（11 例，下单类放文件最前，修复前 8 例红）。实盘 dry_run 四项验证通过，未下任何单。

### 性能

- **zmq 客户端不再把并发请求排成一队**（#186）：`send_request` 此前在**整个** send/poll/recv 周期内持 `_client_lock`，因为客户端只有一个 DEALER socket 而 zmq socket 不是线程安全的。于是多线程调用只能轮流来。实盘 4 并发实测：zmq 的 ping 从 2.4 只到 2.7 次每秒（drain 模式 10.2 到 10.2，纹丝不动），而没有这把锁的 redis 从 20.7 到 **143.3**。线程数在 zmq 上买不到任何东西。

  改成 zmq 自己的答案 —— **每个调用线程一个 socket**，各自随机 IDENTITY，服务端 ROUTER 把回复路由回发起的那个线程 —— 而不是给共享 socket 加锁。`_client_lock` 现在只保护注册表。

  一个线程一个 socket 的风险是泄漏：按请求开线程的调用方会给每个线程留一条到 ROUTER 的 TCP 连接。所以建新 socket 时顺带回收已退出线程的（它们的 owner 已经消失，不可能有人正在其中收发，这正是可以从别的线程关掉它们的理由），`stop()` 关掉全部。

  **实盘未验证**：这是纯客户端改动，而当前部署跑的是 redis transport，根本不经过这段代码；要验它得把终端换成 zmq 再重启。离线用假 socket 钉住了真正的回归 —— 4 个并发调用在一次 0.3 秒往返里必须重叠而不是排队（修复前 4 例红）。

### 修复

- **批量 RPC 超时后不再回退重提 —— 双倍下单（#195，实盘 100 单变 200 单）**：`order_stock_async` 的积压走一个批量 RPC，服务端在 adjust 线程上串行执行（柜台断连时 ~300ms/项，100 项 >30s 默认超时）。客户端超时放弃后，旧兜底「逐笔重提」启动 —— 但**服务端的批量还在跑而且会跑完**，于是 100 + 100 = 200 单。超时不等于没执行。

  现在 `call()` 对**服务端已应答**的错误改抛 `RpcServerRepliedError`（`RuntimeError` 子类，向后兼容）：批量 handler 只在逐项循环之前抛错，所以「被拒绝 = 一笔没跑」，只有它和「返回空结果」允许回退逐笔。超时/断连一律视为**生死未知**：逐项回调 `on_order_error`，信息明说「orders MAY BE LIVE -- query orders before retrying」，**不再自动重提**。

  `order_stock_batch` 的等待时长随 N 缩放（`max(客户端默认, 15s + 0.5s×N)`，显式 `timeout_seconds` 优先）——让客户端比服务端最慢的合法批量活得久，而不是中途放弃它。服务端新增每个批量一行日志（items/placed/failed/耗时）：超时后仍跑完的批量此前在服务端无迹可寻。

  **实盘 A/B 复现**（周六，废单场景，200 笔逆回购）：0.3.21 旧客户端 → 身份库铁证 **400/200**（双倍原样复现）；修复版客户端同场景 → **零重提**（200 个 MAY-BE-LIVE 错误），服务端最终只执行了原始的 200。顺带修了三个 #185 时期测试 fake 缺 `idempotent` 形参的间歇性失败。

- **多账户 `stop()` 时关闭辅助 listen_redis 连接**（#194）：`MultiAccountRpcServiceManager.stop()` 此前漏关多账户管理器为监听建的第二个 redis 连接，进程退出时可能挂住。

- **runtime 入口转发 quote_push 配置**（#198）：`BIGQMT_REDIS_CONFIG` 里的 `quote_push` 块此前被 runtime 丢弃，策略端永远拿到默认值。

### 测试

- **#190 报告场景固化为端到端回归**（#192）：20 笔无 remark 的 `order_stock_async` 紧凑循环，经真实 worker/批量/回调分发 + 真实服务端 handler（dry-run 网关），钉住 20/20 进入提交路径、回调按 seq 序、自动补的 tag 不塌缩。已验证对修复前代码为红。

- **redis transport 停止时不再甩完整 traceback**（#189）：`stop()` 在监听线程正停在 `get_message(timeout=1.0)` 时把 pubsub 关掉，socket 在它脚下失效 —— Windows 上是 `WinError 10038` 包在 `redis.exceptions.ConnectionError` 里，而循环的裸 `except Exception` 把整个栈打出来，实盘一次重启三条。

  纯噪声，但代价是真的：**正常关闭和真故障长得一模一样**，读的人学会忽略这个 traceback 之后，真出问题那次也会被忽略。现在 `except` 里先看 `self._running`，已经在停就安静退出；队列循环同样处理。

  **实盘前后对照**：同一台桥、同一个 reload 操作，只换代码 —— 拆旧代码的那次 **3 个 traceback**（正是报告里的数量），拆新代码的那次 **0 个**。离线四例覆盖两个循环的安静退出，以及**运行中仍然大声报错**的负面对照 —— 让关闭安静下来不能顺带让故障也安静。

## [0.3.21] - 2026-09-04

### 性能

- **zmq / mysql 现在也能走 adjust 线程 drain，实盘延迟降 4~6 倍**（#183，#104）：显式把 `rpc_background_threads` 设成 `False`，zmq/mysql 不再起自己的接收线程，改由 adjust 每 tick 直接收/处理/发，消掉全部跨线程 GIL 交接。配置里没写这个键时保持历史默认（开后台线程），所以不改配置的部署行为不变。

  **实盘四格实测**（0.3.20，收盘后，`schedule_adjust_interval: "100nMilliSecond"`，同一套只读脚本，每格重启策略后现测）：

  | 配置 | ping p50 / p90 | positions p50 / p90 | 串行吞吐（ping / positions） |
  |---|---|---|---|
  | zmq + 后台线程（本次改动前） | 404.5 / 408.0ms | 607.0 / 704.7ms | 2.4 / 1.7 次每秒 |
  | **zmq + drain（本次）** | **95.1 / 109.8ms** | **95.2 / 110.2ms** | **10.2 / 10.0 次每秒** |
  | redis + drain | 12.6 / 14.7ms | 31.1 / 33.2ms | 76.3 / 21.0 次每秒 |
  | redis + 后台线程 | 10.1 / 105.0ms | 4.1 / 4.8ms | 20.7 / 195.3 次每秒 |

  除了变快，分布也变稳了：95~110ms 正好是一个 adjust tick，改动前那种 400ms 尖峰消失。持仓答的是 10 行真实数据（总资产对得上），不是空壳 —— 按 `get_trade_detail_data` 离开主线程必返回空这条铁律，这证明 drain 确实跑在主线程上。

  **redis 不要跟着设 `False`**。实测 redis 上开 drain 是负优化：交易查询从 4.1ms 掉到 31.1ms、吞吐从 195 掉到 21 次每秒。原因是 drain 每 tick 只轮询一次，请求要等下一个 tick；redis 的阻塞 `brpop` 是请求一落队列就推回来。resolver 对 redis 保持 `bool(configured)` 的原有语义没变，但配置里默认值应当继续是后台线程。

  **同一批实测暴露的三件事，都已单独记录**：zmq 客户端 `send_request` 全程持单 socket 锁，并发拿不到任何吞吐收益（#186，zmq 上 2.4→2.7、10.2→10.2，而 redis 上 20.7→143.3）；README 的 transport 性能表与实测相反，正把用户引向最慢的配置（#187）；纯 zmq / 单文件入口强制 `rpc_background_threads = True`，最需要低延迟的那批部署吃不到本次改动（#188）。

  **未验证**：只在 zmq 和 redis 上量过，mysql 和 shm 没测；只在收盘后量过，盘中 QMT 主线程更忙时的数字可能不同；只覆盖了 `ping`（inline）和 `query_stock_positions`（deferred）两个方法，没有下单路径 —— 本次测量不下单。

- **纯 zmq / 单文件部署不再被入口强制开后台线程**（#188）：#183 让 zmq/mysql 可以用显式 `rpc_background_threads: False` 换到 adjust drain，但 `BIGQMT_ZMQ_DRYRUN.py` 设 `BIGQMT_FORCE_TRANSPORT = "zmq"` 之后，入口无条件把这个键赋成 `True` —— **最想要低延迟的那批部署恰好是拿不到的那批**，而且用户在自己配置里写的 `False` 被静默忽略。改成 `setdefault`：没写这个键仍然沿用历史默认 `True`，写了就听用户的。单文件构建器同样处理，它那条 `background_threads=True` 的横幅也改成报告实际用到的值 —— 一条永远说 True 的日志比没有日志更坏，那是「drain 怎么没生效」时第一个会去看的地方。

  实盘验不了这条（要换成纯 zmq 入口再重启策略），所以按仓库对入口文件一贯的做法做源码级钉子；「显式 False 能一路传到 resolver」那一半由 `test_transport_selection.py` 覆盖。

### 文档

- **README 与 docs 的 transport 性能表是反的，已按实测重写**（#187）：此前写着 zmq「同机低延迟 p50~0.7ms」、redis 13ms，还有三处叫用户切 zmq 时「必须」设 `rpc_background_threads: True`。实测 **redis 比 zmq 快 8~60 倍**（ping 10ms vs 95ms，交易查询 4ms vs 95ms），那个 0.7ms 是撞上 adjust 空窗的最好情况而不是 p50，redis 的 13ms 反倒一直是准的。这不只是文档不准 —— 它把追求低延迟的用户直接引到最慢的配置上，而且没有任何反馈能让他们发现选错了；本仓库自己的实盘部署就一直跑在 zmq 上。

  README 的传输表、FormulaServer 对比表、基准表、配置注释、排查指引，以及 `docs/RPC_TRANSPORTS.md` 的传输表和实测段全部更新，并注明「0.3.21 之前这张表是反的」。三处「必须设 True」改成推荐 `False`。顺带记上 zmq 客户端并发无效（#186）—— 只有 redis 上多线程能提升吞吐。

### 修复

- **异步下单被批量端点吞掉，而且吞掉的单返回 success**（#190，实盘报告，#181 的回归）：0.3.20 正常，之后的 main 上「循环里单独调用下单一单也下不出去，插一个别的调用就又能下」，以及「同时下很多单、回调回来一堆、委托列表里只有一单」。两条是同一个根因。

  #181 让 `order_stock_async` 的积压走 `order_stock_batch` 省往返，于是异步下单继承了一份它从没答应过的契约。`_handle_submit_order` 在没有 remark 时自动补 `bqrpc:<uuid>` 且从不去重；`_handle_submit_orders_batch` 两样都相反 —— 缺 tag 直接答 `ORDER_TAG_REQUIRED`（**没写 remark 是异步的常态**，于是一单不下），tag 见过就答 `success: True, idempotent: True` 却根本不到 gateway（**remark 重复对异步完全合法**，于是只下一单）。「插一个别的调用就能下」是批量阈值：队列里只剩一笔时走单笔路径，把循环放慢就把 bug 藏起来了。

  最危险的是被吞掉的单**返回 success**，调用方无从分辨。离线复现的原文是 `reported 2 successes for 1 placed orders`。

  修法是让异步路径**显式退出**幂等契约，`order_stock_batch` 的默认行为一个字不动 —— 它的幂等是防重试重复下单的保险丝，不是可以顺手拆掉的东西。服务端接受 `idempotent`（默认 `True`）；为 `False` 时缺 tag 照单笔路径补 uuid、不去重、也不写 journal（异步的 remark 不是身份，记下来会压掉后面真正的批量单）。客户端 `_submit_async_batch` 传 `False`，并给每项补唯一 `signal_id` —— 对着还不认这个开关的旧服务端，无 remark 的情况也能靠 tag 回落到 signal_id 而不撞去重。

  **实盘前后对照，未下任何单**：报告人的场景原样复现 —— 紧凑 while 循环调 `order_stock_async`、不插查询、不带 remark，每项 `order_volume=0`，在 `_handle_submit_order` 的 `volume must be positive` 就退出，到不了 passorder。同一台桥、同一个脚本，只换服务端与客户端版本：

  | 场景 | 修复前 | 修复后 |
  |---|---|---|
  | 无 remark，8 笔 | **0/8 到达提交路径，8 笔全被 `ORDER_TAG_REQUIRED` 拦** | **8/8** |
  | 无 remark 走默认契约 | `ORDER_TAG_REQUIRED` | `ORDER_TAG_REQUIRED`（未变） |

  「连第一单都下不出去」在这里看得很清楚：批次里 index 0 和其余各笔一样被拒，不是「第一单成功、后面被去重」。

  **去重那一半没有实盘证据，只有离线测试**。这个探针证不了它：幂等去重靠 `_submit_journal`，而 journal 只在提交成功之后才写；`volume=0` 让每笔都在写 journal 之前抛错，于是去重根本没机会触发 —— 修复前修复后同样是 8/8。要在实盘证它必须真下单。离线用 DryRun gateway 提交会成功、journal 会写，那里 3 笔同 remark 修复前只下 1 笔、修复后 3 笔全下。

  **未验证**：没真下过单，所以证不到「循环里 N 笔异步单最终在委托列表里出现 N 行」。

  **影响范围**：`66c344a`（#181）不在任何 tag 里，这个 bug 从未发布到 PyPI，只影响从源码部署 main 的人。

  新增 `tests/bigqmt_signal_trader/test_async_batch_swallows_orders.py`，下单/批量放在文件最前面 —— 这里答错要花真钱，别的用例都没有这个性质。10 例里 5 例在修复前是红的（`git stash` 验证过）。#181 原有的批量测试从来测不到这个：它的 `_order_kwargs` 每笔都带 remark，还把 `order_stock_batch` 整个打了桩，服务端处理器根本没被跑到。

- **策略名一直就在回调里，读错字段了**（#174，@sumo225270 提供 raw_fields）：#174 此前的结论是「大 QMT 的委托/成交行不带策略名，只能靠下单时记、回调时反查」。那是从一个**正确的观察**得出的**错误结论** —— `m_strStrategyName` 确实不存在（实盘列全部属性：ORDER 120 个、DEAL 47 个，两边都没有），但名字在 `m_strSource`（**报单来源**）里，就是 `passorder` 第 8 个参数 `strategyName` 原样回来。

  仓库自己早就记着这件事，只是从来没读回来：`docs/MiniQMT_2_BigQMT-Skill/api_mapping.md` 里 `order.strategy_name` 就映射到 `o.m_strSource`；#154 更是在实盘量过 —— 手工下的 13 行 `m_strSource` 为空，本桥下的 3 行带着策略名，而 `m_strStrategyName` 16 行全空。`rpc_default_strategy_name` 这个配置项存在的理由就是它会显示在 QMT 的报单来源列里。

  报告人的 dump 里，委托和成交回调**都**带着 `m_strSource: '大QMT桥接器'`。所以本桥下的单现在**在回调里就能自报家门**，不需要 Redis、不需要进程内 journal、也不需要按 remark 对键 —— 顺带覆盖了那三者都覆盖不了的情况：本进程启动**之前**下的单。

  四处一起改，回调路径和查询路径用同一份候选字段表，免得同一笔单在事件里和在查询结果里说法不一：`normalize_order_event`、`normalize_trade_event`、以及 `order_bigqmt` 的委托/成交两个构造器。

  取值改成**取第一个非空**而不是第一个非 None：`_attr` 遇到 `""` 就停（空串不是 None），终端只要把 `m_strStrategyName` 建成空串，就会挡住后面的 `m_strSource`，答一个「无名」而名字明明在对象上。

  身份补全（journal → Redis）保留作兜底，覆盖行本身答不了的：终端不填报单来源的情况，以及只有桥接器知道名字的单。

  **顺带澄清 #174 的两条旧怀疑，都不成立**：`m_strRemark` **没有被截断**（报告人下单原串、委托回调、成交回调三处都是 len=50，逐字一致）；那台终端的 **Redis 是通的**（846 条委托事件、738 条成交事件都进了 db5）。

  **未验证**：本机当日 14 笔委托 / 17 笔成交全是手工单，只读探针测到 `m_strSource` 在这些行上**全为空** —— 这只证到了负面对照（手工单不会被安上策略名），证不到「本桥下的单在这台终端上能端到端带回策略名」。那要开盘时经由本桥真下一单，本任务不下单。正面证据来自 #154 的实盘测量和报告人的 dump。

- **MiniQMT 历史数据语义与纯 ZMQ 交易回调不再意外缩水**：客户端本地缓存填充时保留 MiniQMT K 线字段中的 `time` / `openInterest`，并默认允许“下载原始数据后按另一复权方式读取”时回源 Big QMT；缓存同时存在日期索引和毫秒 `time` 列时，优先用日期索引做窗口过滤。这些都是 MiniQMT 可观察行为，不绑定某个上层框架。

  纯 ZMQ 入口不再强制关闭 `exec_events`：现有 ZMQ PUB 通道可承载 `on_stock_order` / `on_stock_trade` / `on_order_error` MiniQMT 风格回调。仍无 Redis Stream 的短时回放，且 `download_jobs` / `full_tick_cache` 依然在纯 ZMQ 入口中关闭。

- **`qmt.py trades` 现在带上 `strategy_name`**（#174 的后续，客户端工具）：#174 给的绕行办法是「回调拿不到策略名时用查询兜一下」—— 查询路径确实是补全过的（`_attribute_to_strategies`），可照着这个办法用本仓库自己的 CLI 去查成交的人，看到的是查询路径**也**没有策略名。

  字段是在最后一步被丢掉的，不是没送到：只读核对了实盘应答，服务端发的成交行**带** `strategy_name` 这个键（当日 17 笔，17 项俱全），是 CLI 的 `_trade_to_dict` 没把它列进去 —— 委托那侧的 `_order_to_dict` 一直列着。

  取不到时给 `None` 而不是 `""`，和 `trade_amount`（#173）同一个道理：「服务端没发这个字段（该升级了）」和「这笔单确实没有策略名（终端里手工下的，没有备注可查）」是两回事，兜底成空串会把它们抹平。

  **未验证**：这台终端当日 17 笔成交全部是手工单（备注为空），所以实盘只能证到**键在、值为空**，证不到「本桥下的单能查回非空策略名」—— 那要开盘时经由本桥下一单才看得到，而本任务不下单。

- **`order_stock_batch` 的应答不再被其中一笔的结算扣住**（#181 顺带点名的隐患，服务端）：`_handle_submit_orders_batch` 把每个 item 原样交给 `_handle_submit_order`，而后者的 `wait_settlement` 默认 `True`，结算又只有**一个单槽**（`_pending_settlement`），服务层每个请求只 `take` 一次。于是批里的 N 笔逐笔覆盖，只有最后一笔活下来。

  **不丢单**（每一笔都提交出去了），丢的是别的三样：整批应答被推迟到那一笔结算完或超时才发出（批量存在的理由就是一次往返，这等于把最贵的一笔的延迟加回来）；那一笔的诊断挂到整批头上 —— 离线复现出来的原文是 `passorder submitted but order not found in system (stock=600000.SH action=BUY price=10.10 volume=100, 1 lookup(s))`，一个说的是**一笔**的消息，挂在覆盖**两笔**的应答上；而它回填的 `order_sys_id` 谁也读不到 —— 批量结果 dict 在结算之前就已经从 result 上拷完了。

  批量处理器现在对每一项显式置 `wait_settlement=False`。不损失什么：批量应答本来就是逐项的（每项带 `index` / `order_sys_id` / `user_order_id`），委托号照样从 `order_callback` 推送学得到，和 `order_stock_async` 一样（#50）。要真按项支持 `wait_settlement`，得把单槽改成挂在一次应答上的结算队列，那是 #181 的另一半。

  **未验证**：这是服务端改动，要部署 + 重启策略才生效，本任务不部署也不下单，所以实盘只证到桥接服务健康（只读门禁 10 项全过），证不到「真发一批委托时应答立刻回来」。离线侧四条用例覆盖：批量不留结算、应答不被扣住、单笔诊断不外溢，以及**单笔 `order_stock` 仍然照常等结算**（负面对照，#44/#152 的机制没被这次改动碰到）。

## [0.3.20] - 2026-09-04

### 新增

- **ORDER 委托行透出成交金额 `trade_amount`**（#173，@feel-think 请求）：ccxt 适配层（QmtExchange）要 order dict 的 `cost`。ORDER 行原生带 `m_dTradeAmount`，但 `OrderSnapshot` 取了 `price_type` / `traded_price`，唯独没取成交金额 —— 金额此前只在 DEAL 行以 `amount` 透出。

  于是拿一笔委托的 cost 只有两条路，都不好：按 `order_sysid` 聚合 DEAL 行（多一次 RPC），或者调用方自己拿 `traded_price × traded_volume` 去算。

  查询路径（`query_orders`）和推送路径（`normalize_order_event`）**同时**透出，否则走回调的调用方拿不到 cost；客户端 `xtquant_compat._order_from_dict` 同步传递。

  **取不到时留 0.0，不拿 `traded_price × traded_volume` 兜底。** 让估算值冒充柜台金额正好是这个 issue 要避开的事，而且 0.0 对未成交委托本来就是对的。旧部署不发这个键时客户端也给 0.0 而不是 AttributeError（#133 就是那个形态）。

  **动手前先查了这台终端到底带不带这个字段。** 只读调 `describe_trade_detail_fields`（0.3.19，当日 14 笔委托）：ORDER 行共 120 个属性，`m_dTradeAmount` 在其中；掩码形状显示 13 笔是四到五位数的金额，唯一一笔 `0.0` 正是 `m_nVolumeTraded` 为 0 的未成交委托。所以它不是一个「存在但恒为空」的字段 —— #133 里的 `m_strShareholderID` 就是那个下场（属性表里根本没有）。

  **已实盘验证**（0.3.19 部署 + `reload_deployment`，只读查询）：当日 14 笔委托全部带上 `trade_amount`，且与按 `order_sysid` 聚合的 DEAL 金额 **14/14 逐笔相等** —— 这是一个独立来源，验的是这个数**确实是成交金额**，不只是"字段出现了"。客户端对象路径（ccxt 取 `cost` 的那条）同样确认：14 个 order 对象都有 `.trade_amount`，合计 117546.00。

  **有一条 issue 里的前提没能复现，如实记下**：「分笔成交时成交均价有舍入，会和柜台差几分」在这台终端上**不成立**。`m_dTradedPrice` 不是两位小数，它带完整精度 —— 当日唯一一笔分价成交（55.14 / 55.13 两笔）报的是 `55.13666666666666`，所以 `traded_price × traded_volume` 与柜台金额**差值为 0**。取这个字段的理由因此不是「修好了差几分」，而是它是**柜台的原值**：不依赖某台终端的 `m_dTradedPrice` 精度，也不用多发一次 RPC 去聚合 DEAL。别的券商的 QMT 若把均价截成两位，估算才会开始漂。

  **未验证**：期货（金额 = 均价×数量×合约乘数）和港股通的 `m_dTradeAmountRMB` 未测；推送路径（`normalize_order_event` 的 `trade_amount`）只有离线用例，当日收盘后没有新委托回调可看，没有实盘样本。

### 修复

- **成交回调终于带上 `strategy_name`，委托回调不再只靠 Redis**（#174，@sumo225270 报告）：`on_stock_order` / `on_stock_trade` 拿到的策略名恒为空，而下单时传的是非空串；`instrument_name` / `order_remark` 一切正常。查下来是**两个**不同的原因。

  **成交事件根本没有这个键** —— 不是空字符串，是不存在，所以客户端 `item.get("strategy_name") or ""` 只可能答 `""`。而且发布路径上只有委托分支做了身份补全：

  ```python
  if kind == "trade":
      event = normalize_trade_event(...)     # 没有补全
  else:
      event = normalize_order_event(...)
      event = enrich_order_identity(...)     # 只有这边有
  ```

  这条**跟配置无关，任何部署下都是空的**。现在 `normalize_trade_event` 产出这个键，两个分支都走同一个补全。

  **委托那条则是大 QMT 自己不给。** 实盘列了全部属性：ORDER 行 120 个、DEAL 行 47 个，**`m_strStrategyName` 两边都没有**（和 #133 的 `m_strShareholderID` 同一形态）；查询路径实测也是 14 笔委托策略名全空。所以只能靠下单时记、回调时按 remark 反查 —— 而这一步此前**只读 Redis**。zmq 部署上 Redis 是可选件，「配置了 ≠ 连得上」（#145 那个坑）时静默失败，名字就永远补不回来。

  现在**先查进程内的 journal（#156，查询路径本来就在用），Redis 作兜底**。这个顺序是有意的：补全跑在 QMT 的 C++ 回调线程上，本进程下的单在 journal 里已经存着和 Redis 一模一样的字符串，先问 Redis 什么也换不来，却要付一次网络往返 —— Redis 配了连不上时更是每个事件都付满超时。Redis 留作兜底是因为它能认出**别的进程**下的单，那是更少见的情况。

  已验证：全量测试 `1499 passed`（112 文件 = 112 模块），13 条新用例修复前 11 条红（另外 2 条是回归保护）；用终端自己的 `xtquant`（91 个名字）预检通过，`bigqmt_signal_trader_strategy` 能干净导入；两个改动文件 AST 无 3.7+ 语法（QMT 只有 Python 3.6）；只读实盘门禁 10/10。

  **未验证**：这是服务端改动，要部署 + **重启策略**（`reload_deployment` 刷不了 `bigqmt_signal_trader_strategy.py`，QMT exec 的就是它）。而且**要等开盘有真实成交回调才能实盘确认** —— 收盘后没有委托/成交回调可看，本次只有离线用例。报告人那侧「remark 是否被 QMT 截断导致键对不上」的怀疑仍未验证，等他贴 `raw_fields`。

- **交易查询 1500ms → 605ms：回复入队后不再空等 router 的接收超时**（issue #104，@sumo225270）：用两侧墙钟时间戳分解（同机，时钟可直接比）后发现，`get_trade_detail_data` 本身**只要 1 毫秒**，1300ms 全在回复构造完之后。

  ```
  get_positions   等待 201ms | 处理 1.0ms | 回程 1300ms | 总 1500ms
  ```

  交易查询必须在 adjust 线程上跑，于是 `send_response` 判定「当前线程不是 router 线程」→ 回复入队；而队列**只在 router 循环顶部**排空，那之前隔着一个最多 `RCVTIMEO`（1 秒）的阻塞 `recv`。实测滞留 **10 次平均 1149ms**。

  现在回复入队时通过一条 inproc 管道唤醒 router 线程，循环同时等两端。唤醒失败被吞掉（漏一次只损失延迟，不损失正确性），管道开不起来就保留原阻塞路径。

  实盘：`get_positions` 1500→605ms、`query_orders` 1499→607ms、`get_asset` 1500→608ms，回复滞留 1149→117ms。

  **代价（有意接受）**：`ping` 从 297ms 变 404ms（p50，n=40）—— router 循环从「一次阻塞 recv」变成「poll + 非阻塞 recv」，多的字节码正好落在应答 ping 的那条线程上。交易路径省 ~900ms，而绝大多数只读方法本来走 FormulaServer 直连（0.2ms）不经此路。

  `ping` 一直看不到这个问题（它在 router 线程上就地应答、回复直接发出），我因此一度以为该机制已被证伪 —— 计数验证才翻案：10 次交易查询 + 10 次 ping，入队回复正好 10 条。

- **`qmt.py --table` 不再只画一行空白**（读 CLI，随 #173 的实盘确认一起发现）：`orders --table` 画出表头、分隔线，然后一行纯空格 —— 不管返回了多少数据。同一个查询不加 `--table` 是好的（`count=14`，字段齐全），所以数据没问题，坏的只有渲染。

  子命令交给 `_ok` 的是**为 JSON 设计的形状**，而那通常是个包装：

  ```python
  _ok({"orders": rows, "count": 14}, table=..., headers=[...])
  ```

  而 `_ok` 里是 `_print_table(data if isinstance(data, list) else [data], headers)` —— 包装是 dict 不是 list，于是被再包一层当成**一行**，每个表头（`stock_code` …）在它身上都查不到，全渲染成空字符串。

  实测受影响的是 **positions / orders / trades / tick / kline 五个**；**account 和 instrument 本来就是对的**，它们确实只返回一条扁平记录，一行才是正确答案。所以修法不能是"永远取 values"，那会把这两个也弄坏。

  现在 `_ok` 多一个 `table_key` 参数指明包装里哪个键是表格数据（`positions` / `orders` / `trades` / `bars`）；没给 `table_key` 时，**值全是 dict** 的字典按"按键索引"处理、行取 values（`tick` 是 `{code: {...}}`），其余仍当作单条扁平记录。

  已实盘验证（只读）：account 1 行、positions 10 行、orders 14 行、trades 17 行、tick 2 行、kline 3 行，行数与实际数据逐一对上。新测试 10 条，修复前 8 条红 —— 另外 2 条正是 account / instrument 的回归保护，修复前后都该绿。

- **周线（1w）的 preClose 不再恒为 0.0**（#166，@yucejade 报告）：大 QMT 对 `1w` 的每一根 bar 都返回 `preClose = 0.0`。实测（国金 2.1.19.0 / 0.3.19，只读）受影响的**只有 1w**：

  ```
  period  rows  preClose 非零
  1d       649  649
  1w       138    0      <-- 只有它
  1mon      33   33
  1q        11   11
  1hy        6    6
  1y         3    3
  ```

  三个标的（000001.SZ / 600000.SH / 600519.SH）、`fill_data` 两种取值下都是 0.0，调用方没有任何参数能把它拿回来。

  现在客户端用**该周首个交易日的日线 preClose** 补齐（`xtquant_compat.get_market_data_ex`）—— 这正是 MiniQMT 的口径，除权日当天它是除权参考价。

  **注意它不等价于 `close[i-1]`。** 两者只在「除权日恰好是当周首个交易日」时分叉，而扫了 6 个流动性好的票 2023-01-01 以来的除权日，**一个这样的用例都没有**（全落在周三/四/五）。所以只拿真实周写的测试会对两种实现都给绿—— 就是 #88 那种「测试和错误前提同源」的形态。因此测试里**构造**了首日除权的周，并另外钉了一个现成判别用例：**窗口里的第一根周线**，`close[i-1]` 对它根本无解。实验过：把实现换成 `close[i-1]`，这些测试 4 条变红。

  周线 bar 的标签是该 ISO 周（周一~周日）的**周日**，实测周线 close 逐周等于该周最后一个交易日的日线 close，19/19 命中（含两个节假缩短周）。所以周首是从标签算出来的，不依赖上一根 bar —— 这才使第一根也能被填上。

  代价和退路：只在「请求了 preClose **且** 整列返回 0.0」时多发一次日线请求（单标的八个月实测 84ms → 614ms；多标的共用同一次日线请求）。终端本来就答得对的周期（包括 1mon/1q）**不付任何额外代价**，没要 preClose 的调用方也不付（#104）。日线请求失败就退回终端原值并告警，不会把整个读取拖下水；某周拿不到日线就**维持 0.0 而不编一个数**。`backfill_pre_close=False` 可关掉，拿回终端原始 bar。

  范围按实测钉死在 `PRE_CLOSE_BACKFILL_PERIODS = ("1w",)`：其他周期在这台终端上本就是对的，要扩到新周期得先做同样的只读实测，不能假定“多日周期都一样”。

  已验证（只读实盘，0.3.19 部署版本一致）：三个标各 23/23 根周线的 preClose 与**独立拉取**的日线 preClose 逐根相等。**未验证**：首日除权周只有构造用例，没有真实市场样本；前/后复权（`dividend_type=front/back`）只验了参数透传，没有逐值校对；期货/期权标的未测。


## [0.3.19] - 2026-09-04

### 新增

- **多账号 RPC 服务：一个策略实例、两条 channel，同时服务两个账号**（PR #171，@fancyfanfan）：0.3.18 的 `BIGQMT_ACCOUNT_TYPE_MAP`（PR #135）解决的是「读」—— 查询按请求的 `account_id` 解析 `account_type`。这一条解决「写 + 架构」：

  ```
  MAP 有多条时：
    primary service   → adjust 主线程（background_threads=False）
    secondary service → 后台线程收，交易类请求 defer 到主线程 drain
    两者共享同一个 BigQmtRpcHandlers
    SecondaryHandlersProxy 把 secondary 的 account_id 注入 params
  ```

  **主线程那条硬约束没有被破坏。** `submit_order` / `submit_orders_batch` / `cancel_order` 不在 `READ_METHODS` 里，所以哪怕 secondary 的 `listener_methods=("*",)`，`_expand_listener_methods` 展开后也不包含它们；交易类查询（`LISTENER_DEFERRED_METHODS`）则在展开时被显式减掉。两类都落进 secondary 自己的 `pending` 队列，由 `MultiAccountRpcServiceManager.drain_pending()` 在 adjust 主线程统一执行 —— `get_trade_detail_data` 仍然只在主线程被调用。

  **MAP 为空或只有一条时零行为变化**：`build_multi_account_rpc_service` 直接返回原来那个 service，不做任何包装。

### 修复

- **撤单不再永远用网关自己的账号**（#168，随 PR #171 一起）：`cancel()` 原来两个值都取自网关自身 ——

  ```python
  account_type = self._resolve_account_type(self.account_id)   # 网关的，不是这笔委托的
  ok = cancel_func(order_ref.order_sys_id, self.account_id, account_type, self.context_info)
  ```

  而 `_handle_cancel_order` 第一件事就是 `account_id = self._request_account_id(params)` —— **算出来了，但没往下传**。双账号部署里撤一笔期货委托，会用股票账号的 id 和类型发出去；好一点是撤不掉，差一点是撤到同名的另一笔。又因为 `cancel` 的原生返回值**两个方向都不可信**（#148 返回 false 其实撤成了，#151 返回 true 而委托根本不存在），这个错误未必当场暴露。

  现在 `cancel(order_ref, account_id=None)`，`aid = account_id or self.account_id` —— 单账号传 `None` 时与原来完全等价。

- **部署脚本拒绝把包拷进它自己里面**（PR #169，@karlthas007）：step 2 用 `import bigqmt_signal_trader_strategy` 解析源目录。从 QMT 的 python 目录本身启动脚本时，这个 import 命中的是**已经部署好的文件**，于是 `src == dst`，`Copy-Item` 把包拷进自己：

  ```
  python\bigqmt_signal_trader\bigqmt_signal_trader\
  ```

  更麻烦的是外层那份旧包还在原地，而顶层文件被更新了 —— 混出一棵版本不一致的树，直到 RPC 启动才以 `__init__() got an unexpected keyword argument 'default_strategy_name'` 的形式炸出来。现在这种情况直接抛错，并告诉你换个目录跑。

  同一个 PR 还给两处 github 下载失败（redis zip、miniconda 安装包）的报错补上了 `-Proxy` 提示 —— 很多服务器环境直连不到 github.com，而 `-Proxy` 参数本来就有，只是报错里没说。

### 已知限制

- **双账号路由本身没有实盘验证。** 本仓库只有一个账号，且验证终端的 config 里没有 `BIGQMT_ACCOUNT_TYPE_MAP`，因此 `build_multi_account_rpc_service` 走的是「直接返回 primary」那条分支 —— 多账号代码路径**一次都没有被执行过**。dual-channel 收发、`SecondaryHandlersProxy` 的 account_id 注入、secondary 的 `pending` 队列被主线程 drain，这三件事目前只有代码走查和单测支撑。需要 STOCK + FUTURE 同终端的生产环境作证。
- **撤单按 account_id 路由同理**，只验证了单账号回落路径（`account_id=None` → `self.account_id`）与原行为一致。「期货委托用期货账号撤掉」这半需要双账号环境。
- 本版发布时实盘终端跑的是 0.3.18；0.3.19 的服务端改动**尚未部署验证**。

## [0.3.18] - 2026-09-04

### 新增

- **`probe_capabilities` 新增 `order_watch`：回答「#164 在这台部署上到底生效没有」**：#164 的接线在 `bigqmt_signal_trader_strategy.py` 里，而 `reload_deployment()` **刷不了顶层文件** —— 也就是说一棵代码齐全的部署树完全可能仍在跑旧的轮询路径，而**没有任何办法分辨**。

  ```json
  {"wired": true, "remarks": 0, "statuses": 1, "max_entries": 5000, "ttl_seconds": 86400.0}
  ```

  只报计数，不报 remark 和委托号 —— 那两个是委托标识。`wired` 即「重启是否已生效」。

  实盘用它验完了 #164：策略重启后手工挂撤一笔，`statuses` 0 → 1，证明 QMT 的 `order_callback` 确实在喂表。顺带查出**该功能之前根本没同步到部署**（`order_watch.py` 缺失），而 `redis_rpc.py` 的 `getattr` 兜底让它安静地退回轮询 —— 防御是对的，但功能哑了也看不出来，正是这个探针要解决的问题。

- **`BIGQMT_ACCOUNT_TYPE_MAP`：一个 QMT 进程同时服务股票和期货账号**（PR #135，@fancyfanfan）：网关的 `account_type` 原来是 init 时定死的，单账号部署没问题；一个策略实例服务两个账号时，它必须跟着**请求的 account_id** 走，否则期货账号会被当成 STOCK 查 —— 和 #92 是同一类 bug（信用账号被当 STOCK 查会返回一整行 0 且不报错）。

  ```python
  BIGQMT_ACCOUNT_TYPE_MAP = {
      "88888888": "STOCK",
      "66666666": "FUTURE",
  }
  ```

  **不配这一项时行为完全不变** —— 查不到映射就回落到网关自己的 `account_type`。实盘验证过（本终端没配该项）：`account_type` / 现金 / 持仓数 / 持仓统计 / 委托 / 成交逐项一致。

  **已知缺口**：撤单仍是单账号的 —— `OrderRef` 不带 account_id，`cancel()` 用的是网关自己的 `self.account_id`，所以双账号部署里撤期货委托会用股票账号 id 发出去。已单独开跟进。

  **双账号本身未在本仓库验证**：这里只有一个账号，该路径由报告人的生产环境作证。


### 修复

- **`get_divid_factors` 的区间请求被坍缩成只查 `end_time` 单日**（issue #165，@yucejade）：任意区间都被压成一天，而任何一天几乎都不是除权日，于是区间请求恒返回 `{}` —— 而空 dict 和「区间内没有事件」**完全分不出来**。报告人按 xtdata 区间语义做的盘前全市场复权因子同步就这样跑了近一个月：每只都空，因子表静默停更，K 线照常更新，一声不响。

  代码里原来的注释写着「xtdata SDK 也是 2 参数」——**这句是错的**，终端自带 SDK 是 `get_divid_factors(stock_code, start_time, end_time)`。

  现在先试区间形态（原生 SDK / ContextInfo），都不支持才在适配层展开。展开**不逐日遍历**：除权日恰好就是「`preClose` 与上一根 `close` 不同」的那天（那正是除权参考价的含义），所以一次日线读取就能把两年窗口收敛到几个候选日，只探测这几天。探测数有上限（40），防止筛不动时打出几百个 RPC。

  实盘验证（国金 2.1.19.0）：

  ```
  000001.SZ 20240101~20260904  ->  5 个事件（原来是 {}）
  600000.SH 20240101~20260904  ->  2 个事件（原来是 {}）
  单日 20260612                ->  0.36，与报告人自测一致
  ```

  区间结果里包含报告人**独立确认过**的 2025-10-15 中期分红（0.236）。

  **一个必须说清的限制**：扫描依赖本地日线。日线不足 2 根时无法做哪怕一次比较，此时**抛错而不是返回 `{}`** —— 否则就是把本 issue 的失败模式换个窄一点的形式重造一遍。实盘验证：`300750.SZ`（本机只有 1 根日线）现在报错并提示先 `download_history_data`，而不是静默给空。

- **`fill_data` 传不到大 QMT，停牌/无数据被静默填成 0 行且关不掉**（issue #167，@zxm9999）：报告人直接指出了断点 —— `_market_data_shapes()` 的 `big_kwargs` 没带 `fill_data`，而它是**第一个被尝试**的 shape，调用成功，参数就被无声丢弃了。

  大 QMT 是接受这个参数的（文档 5.x：`C.get_market_data_ex(fields, stock_code, period, start_time, end_time, count, dividend_type, fill_data, subscribe)`，终端自带 SDK 签名一致）。

  **后果比「参数被忽略」严重**。实盘实测（20260101~20260904，日线，15 只）：

  ```
  300750.SZ   fill_data=True -> 164 行，其中 163 行 close = 0.0
              fill_data=False ->   1 行（真实数据）
  15 只里 9 只行数不同
  ```

  也就是说：本地没有下载完整历史的代码，过去一律返回一个**结构完好、99% 是 0** 的 164 行 DataFrame，而调用方**没有办法关掉填充**。在这种帧上算收益率/均线/波动率，拿到的全是垃圾，而且不报错。

  修法：`fill_data` 单独放进一个 shape，排在原来那个不带它的 shape **之前**，而不是直接加进 `big_kwargs` —— `_call_first_supported` 只在 `TypeError` 时回落，所以签名里没有这个参数的终端仍然要能落到原来的 shape 上。`get_market_data` / `get_local_data` 同样处理。

  实盘验证：本终端接受该参数（第一个 shape 直接成功，没有回落），且 `True`/`False` 的返回**确有差异**。新增 11 个测试，修复前 9 个失败。

- **结算不再盲轮询：回调把委托号/状态推给我们了**（issue #164）：下单/撤单结算原来在 adjust 主线程上一轮一轮 `query_orders`（实测一次撤单 3.6s 打了 135 轮）。新增回调喂养的 `OrderWatchTable`（remark→委托号、委托号→状态，有界 FIFO + 24h TTL，C++ 回调线程写、adjust 线程读，普通 dict+锁）：结算先查表，命中即结算、零轮询；查不到回落原有轮询（模拟模式没有回调，轮询保留为兜底）。单测 11 个（含两条快路径零查询、回退路径、表语义/TTL/有界）。**生效需重启策略**（改了顶层 strategy 文件）。


- **按 strategy_name 过滤查委托，返回的行却 strategy_name=''**（issue #156 跟进，@kingtsi）：过滤本身有效（QMT 按策略名过滤返回 15 条），但委托行构建缺成交行早就有的「过滤兜底」——给了过滤器时，每行按构造就属于它。补上。实盘验证：`query_stock_orders(strategy_name='TEST')` 返回 2 条且 `strategy_name='TEST'`。另外新增诊断 RPC `probe_order_identity`（传 remark 返回身份链每环状态：redis 是否接线/key 名/redis 命中/进程内兜底命中），「策略名读不回」类问题以后一条调用就能定位断在哪环。


- **`download_holiday_data` / `download_his_st_data` 在大 QMT 上抛 NotImplementedError**（issue #163，@Randall-Chan）：MiniQMT 这两个是从 xtdata 服务下载假日表/ST 历史；大 QMT 终端自己维护这些数据（登录/数据更新时刷新），没有要下载的东西。现在明确回答 no-op + 说明（之前走通用兜底报 NotImplementedError，用户只能注销那两行）。实盘验证：两个调用都返回 `ok: True, downloaded: False` + 说明。客户端补上漏掉的 `download_his_st_data` 方法。

- **redis < 5.0 没有 streams，每个 tick 都在抛 `unknown command 'XADD'`**（issue #163）：事件回放流和持仓事件流都要 XADD，Windows 上常见的老 redis（3.0.x）没有这命令。现在第一次失败就学到并永久跳过 xadd（日志只说一次），pub/sub 回调不受影响（老 redis 上实时回调一直是通的）；升 redis ≥ 5.0 回放自动恢复。瞬时故障不会误触发。回归测试 6 个（修复前全失败）。


## [0.3.17] - 2026-09-03

### 修复

- **一笔委托触发两次「已报」回调，第一次还是残缺事件**（issue #161，@sumo225270）：QMT 在委托行出现时和 `m_strOrderSysID` 填上后各触发一次 order_callback（#152 的同一窗口），客户端于是看到两条一样的「已报」，第一条无委托号（order_id=0）。现在无委托号的委托事件**扣留 0.8 秒**：带号孪生到达即丢弃（只发一次完整事件），没来则由 adjust 循环补发（不丢事件）。扣留窗口可用 `exec_events_hold_presysid_seconds` 配置，设 0 恢复旧行为。实盘验证（国金 2.1.19.0）：废单路径从「50 无号 + 57 带号」两条变 1 条完整事件。**已报-已报的去重形状需开盘时段复验**（当前已过收盘，只能走废单路径）。

- **回调事件缺 `instrument_name`**（issue #161）：事件规范化没带这个字段。现在 QMT 对象自带就用自带的，没有则服务端用 ContextInfo 查一次并缓存（同一代码只查一次），委托/成交事件都带上。客户端 `order.instrument_name` / `trade.instrument_name` 直接可用。实盘验证：`name='工商银行'` ✓。

  顺带说明报告人的第三问：**QMT 界面手动下的单 strategy_name 永远为空**——手动单没有 remark，身份库无从关联，而 QMT 委托/成交行本身不携带策略名（#133）。KPI 分析建议按「remark 为空」归入手动桶。

### 新增

- **`deploy/` 一键 Windows 部署包**（PR #158，@karlthas007）：`deploy_qmt_bridge.ps1` 在全新 Windows 机器上一次完成——客户端环境（miniconda py3.13 或系统 python venv，pip/conda/Miniconda 默认清华镜像可切官方源）、服务端 4 项拷入 QMT、redis 5.0.14 注册为 Windows 服务（127.0.0.1 + 随机密码 + 192mb noeviction）、生成双侧配置；幂等可重跑，`-CheckOnly` 只读检查。附 `qmt_cli.py`（ping/资产/持仓/委托/成交/tick/kline/买卖/撤单/watch）。作者在江海证券大 QMT 实盘验证过全链路。**合并修正**：帮助文本里的示例账号改为占位符；生成的服务端配置默认 `rpc_allow_order_methods=False`（下单是显式人工决定，加 `-AllowOrders` 才开），与仓库安全默认一致；qmt_cli.py 缺配置时给明确指引而不是 ImportError。
- **`contracts.py` 兼容无 typing_extensions 的 py3.6**（PR #159，@karlthas007）：`typing.Protocol`（3.8+）缺失时先退 typing_extensions，再退纯占位基类——QMT 内嵌 python36 没有这两个库。该模块当前无调用方（latent），此修复保住全 src 的 py3.6 可导入性。补了模拟 py3.6 环境的回归测试。

### 修复

- **`get_trading_dates` 每次调用都白烧 2 秒**（issue #160，@heimo88）：他看到的是策略启动后第一次 21.6s（SDK 冷初始化），我们实盘实测发现**每次调用都 ~2.1s**——`_native_or_context` 每个调用都先让原生 xtdata SDK 去拨它在大 QMT 里永远连不上的行情服务，超时报错后才回落 ContextInfo。而「SDK 在、行情服务不在」在大 QMT 进程里是**不会自愈的永久状态**。现在原生失败按函数名记住 600 秒，窗口内直接走 ContextInfo（成功一次即清除标记）；全部 15 个 `_native_or_context` 调用点受益（`get_holidays` 等含）。回归测试 5 个（修复前 4 个失败）。**生效需同步 QMT 端并重启策略**。**已实盘验证（0.3.16 + 本条部署后）**：reload 后首次 2.5s（最后一次 SDK 实拨），之后每次 **30-46ms**。

- **`xt_trader.sync_deployment()` 从来是坏的**：它调 `self.get_deployment_info()`，而该方法只在 `BigQmtXtData` 上——trader 路径一调就 AttributeError（在部署 #160 时踩到）。改为直接走 `self.client.call("get_deployment_info")`。回归测试 2 个（修复前均失败）。


## [0.3.16] - 2026-09-03

### 修复

- **`order_stock_async` 排了队没发出去的委托随进程退出静默丢失**（issue #156，@kingtsi）：循环连发 async 下单后脚本立即退出——worker 是 daemon 线程，主线程一结束它就被掐死，队列里剩下的委托一笔都不发、没有任何报错；他的 sleep 只是在给进程续命。实盘复现（工行 100 股 ×3 深价单）：立即退出 3 笔只到 1 笔，`wait_async_orders()` + 宽限 3/3。

  修复：`stop()` 和 atexit 钩子（首次 async 下单时注册）在退出前**排干队列**——等 worker 发完已排队的每一笔（有界 5s），再给在途响应 3s 宽限让 `on_order_stock_async_response` 触发。空队列零开销；全程不抛异常。修复后按报告人形态实测：立即退出也 3/3 到达且回调齐全。注意回调本身仍要求进程存活——要在脚本里看回调，得让进程活到回调到达（或显式 `wait_async_orders()`）。

- **`test_all_apis.py` 在 zmq 部署下全挂**（issue #157，@simonfantasy）：脚本的 `_call` 只会 `call_redis_rpc`——zmq 配置下 ping 必超时、后面每个用例跟着挂，而桥本身是好的（报告人自己用 ZmqTransport 手动 ping 证明了）。现在脚本按配置构造统一调用器（zmq 走 `ZmqTransport`，信封与客户端 `call()` 一致），`redis` 改为懒导入（NO_REDIS_FLAT 无 redis 客户端库的部署也能跑）。实盘验证：本机 zmq 桥上全量 18 OK / 0 超时（`get_sector_list` 的 FAIL 是 #143 之后的诚实报错，`query_stock_position` 空为既有行为，均非本次引入）。


## [0.3.15] - 2026-09-03

### 新增

- **`rpc_default_strategy_name`：委托的「报单来源」由你决定**（issue #154，@kingtsi）：QMT 把委托的 投资备注 显示在 委托 列表的**报单来源**列里，所以那不是内部字段 —— 之前每一笔没指定策略名的委托都在那个界面上写着 `bigqmt_rpc`。

  单次调用传 `strategy_name=` 一直是生效的，缺的是**默认值** —— `bigqmt_rpc` 硬编码在三处。现在可以在配置里设一次：

  ```python
  "rpc_default_strategy_name": "",     # 留空，和手动下单一样
  ```

  空字符串在整条链路上都当作**有效答案**（`.get(key)` 而不是 `.get(key, default)`），否则它会被默认值吞回去。默认不变，现有部署不受影响。**改配置需要重启策略** —— `bigqmt_signal_trader_strategy.py` 是顶层文件，`reload_deployment()` 刷不了。

- **`describe_trade_detail_fields` 新增 `shape_fields`**：报告字段的**形状**而不是值 —— 长度、`|` 分段、以及字符类掩码（数字变 `#`、字母变 `a`、分隔符保留）。回答「这个字段里到底是什么东西」而不用把值送出 QMT。掩码是有损的，这正是重点：它带不回一个标识符。

  #154 就是这么定的案：`m_strSource` 在本终端上是 `aaaaaa_aaaaa_aaaaaa` —— **只有字母和下划线，没有数字、没有 `-`、没有 `|`、没有 `{}`**，也就是策略名，不是 MAC 或设备 GUID。



### 修复

- **无 redis 部署里 `strategy_name` 查询永远回填不上**（issue #156 / #133）：QMT 的委托/成交行根本不携带策略名（终端按它过滤、但不报告），桥靠提交时记的 redis 身份库在查询时回填 —— 但 zmq 单文件等无 redis 部署没有身份库，`strategy_name` 永远读 `''`。现在服务端同时维护一份**进程内身份日志**（提交时记 `remark -> strategy_name`，5000 条 FIFO + 24h TTL，与 redis 店同规则）：没 redis 的部署里，凡本进程提交过的委托，查询都能回填策略名。redis 仍是主店（跨重启、跨进程）。回归测试 6 个（修复前 3 个失败）。

- **zmq 传输 + redis 可达的部署里，委托/成交回调永远收不到**（issue #144，@sumo225270）：服务端发布执行事件是「**redis 优先**」——只要能建出 redis 客户端就发 redis 通道（流带短回放），连挂多次才降级到 zmq 推送通道（#145）。而客户端 `_event_loop` 是**按 transport 选的**——zmq 传输只听 zmq 推送通道。于是这类部署里每个事件都发在 redis 上，客户端却在另一个通道上听：`on_stock_order` / `on_stock_trade` 静默全丢。

  实盘复现（2026-09-02，国金 2.1.19.0，zmq 传输 + redis 可达）：当天全部委托事件都躺在 redis 流 `bigqmt:order_events:<账号>` 里（14:05 活单 50 → 撤单 54、16:40 废单 57 一条不缺），而 zmq 传输的 XtQuantTrader 回调一个都没收到。

  修复：客户端监听**每轮重连时按服务端的规则重新选通道**——redis 可达（真 ping）订 redis 四通道，否则走推送通道；服务端中途降级 redis → 下一轮客户端跟着切。修复后实盘验证：客户端订上 `bigqmt:order_events:<账号>` 等四通道，按服务端同款格式注入一条合成委托事件，**1.0 秒后 `on_stock_order` 触发**，字段解析正确。回归测试 5 个（修复前 2 个失败），#76 的 zmq 推送通道用例全部保持。

- **撤一个根本不存在的委托也报 `success=True`**（issue #151）：大 QMT 原生 `cancel` 的返回值描述的是「撤单请求有没有发出去」，不是「委托有没有被撤掉」—— 编造委托号（`bigqmt-probe-149-no-such-order` / `99999999999999`）在零可撤委托的账户上也返回成功。这是 #148 的镜像：原生返回**两个方向都不可信**。#149 只修了 falsey 一半，truthy 一半仍在快速路径上直接信。

  现在两个方向都拿委托快照结算：truthy 先做一次**即时回读** —— 命中 53/54 立即确认（快速路径不多花一次往返）；查不到、状态仍在途中才挂起等结算。到期的语义也改了：委托**查不到**报失败（`was not found`），状态 **51/52（已报待撤/部成待撤）不再报「still status 51」失败** —— 那是交易所已受理、正在途中，报「cancel accepted, still in flight」。顺带修掉一类假阴性：truthy 但委托早已成交（56）/已废（57）现在正确报失败。

  修复前 8 个新用例中 7 个失败；#148 既有用例全部保持。

- **qmt-trader CLI 一批问题**（全部带回归测试，修复前 8 个用例失败）：

  - **`cancel` 把撤单结果报反了**：网关遵循 MiniQMT 契约返回 `0`=成功 / `-1`=失败（issue #113），而 `bool(0)` 是 `False` —— 撤单**成功**被报成 `success: false`，**失败**反而报成 `true`。现在 `rc != 0` 直接以 `CANCEL_REJECTED` 报错退出；成功后回读委托行带回最终状态（写完必须回读）。
  - **全局 flag 只能放在子命令前**：`qmt.py account --table` 报 `unrecognized arguments`，而这才是最自然的写法。现在 `--table`/`--account` 放在子命令后同样生效。
  - **editable 安装下 `import xtquant` 被 site-packages 的真包遮蔽**：`_ensure_src_on_path` 只在 src 不在 sys.path 时才插入——editable 安装已把 src 放进去（但在 site-packages **之后**），于是真 xtquant 的 `__init__` 打印升级广告，污染 CLI stdout 上的 JSON 输出（`qmt.py ... | jq` 直接坏）。现在确保 src 永远挪到最前。
  - **`quote-subscribe` 首帧竞态**：首帧快照可能在 `subscribe_whole_quote` 返回前就推给回调，此时 `sub_id` 尚未绑定，回调里 `unsubscribe_quote(sub_id)` 抛 `NameError`。加 None 守卫。
  - **`kline` 统计的 high/low 名不副实**：用的是收盘价的最大/最小值，不是 K 线的最高/最低价。改用 high/low 字段（无字段时回落收盘价）。


## [0.3.14] - 2026-09-02

### 修复

- **单文件构建丢掉了 zmq 绑定地址和 transport**（issue #153，@simonfantasy）：向导里填了 QMT 机器的局域网地址，生成的 `local_config.py` 里是对的 `"zmq": {"bind_address": "tcp://0.0.0.0:15618"}`，但跑起来的 FLAT 构建打印 `zmq started bound=tcp://127.0.0.1:15618`，跨机连不上。

  报告人的判断是对的：**单文件部署从来不读 `local_config.py`** —— `_load_local_config()` 是拿构建文件顶部那个内嵌配置块**合成**出这个模块的。所以内嵌块**就是**配置，而生成它的 `render_single_file_config_block()` 少了两个 key：

  ```
  带 zmq 块 -> tcp://0.0.0.0:15618
  无 zmq 块 -> tcp://127.0.0.1:15618      <- 报告里那行
  ```

  **还牵出一个没人报的**：内嵌块连 `transport` 都没有。no-redis 的 FLAT 构建之后会强制 zmq 所以躲过去了，但 base64 的 `single_file` 构建不强制 —— 在那里选 `transport=zmq`，生成出来的服务端跑 **redis** 而客户端说 zmq，正好复现成「客户端 transport 和服务端不匹配」的 ping 超时。

  两个 key 都补上了。真正拦住这一类的是那条结构性测试：`render_server_config` 输出的每个顶层 key 都必须出现在单文件块里 —— 对单文件部署来说，配置**没有第二个来源**。

  **注意**：修的是生成器，不是已生成的文件。升级后需要**重新跑一次 `bigqmt-init`** 生成单文件。

- **qmt-trader CLI 的 `--dry-run` 会真下单**：`_ok()` 只打印不退出，dry-run 分支打印完预演后穿透到真实下单代码——`buy`/`sell`/`cancel` 三个命令全中招。SKILL.md 承诺「只打印不下单」，行为正好相反。2026-09-02 实盘事故：`buy 601398.SH 100 --dry-run` 真发出了一笔委托（仅因账户可用资金不足被打成废单，未造成成交）。补上两个 `return`，并加回归测试（修复前 4 个用例中 3 个失败）。


## [0.3.13] - 2026-09-02

### 修复

- **已到券商的委托被报成 `-1`（拒单）**（issue #152，@willzhqiang）：同步下单返回 `-1`，客户端打出 `ORDER_REJECTED`，而按同一个 remark 立刻回查，委托**就在券商那里**：`order_sysid 635093411 / status 50 REPORTED / cancelable true / 冻结 421.72`。

  报告人**没有重试**。如果重试了，就是重复下单 —— 这才是这个 bug 危险的地方，而不只是返回值不对。

  窗口在于 QMT 会先放出委托行、稍后才填 `m_strOrderSysID`。`_apply_order_lookup` 匹配到 remark 就当结算完成，哪怕委托号还是空的：回复带着 `order_sys_id=None` 发出去，客户端把它变成 `-1`。

  **委托行存在本身就证明委托已经到了券商**，所以现在的做法是继续等委托号，而不是不带委托号就作答。到期仍然没有委托号时，用能用的最响的方式说出来 —— 客户端遇到 `server_error` 会抛异常，所以它变成一个点名 remark 的异常，而不是一个静默的 `-1`：

  ```
  ORDER IS LIVE -- DO NOT RESUBMIT. passorder reached the broker and the
  order row exists (...), but QMT had still not assigned order_sys_id after
  N lookup(s) ... Find it by remark 'xxx' ... it is not a rejection.
  ```

  措辞刻意和隔壁那条「委托没进系统」区分开 —— 后者含义正相反（委托根本没到券商），而且开头就让人去查 QMT 的 `运行模式`（#122）。在这里说那句话会把人指到完全错误的方向。

  **未实盘验证**：触发这个窗口需要真实下单，本仓库不下真单。修复前 10 个新测试里有 5 个失败。

- **全推订阅把期货代码大写了，订阅从此不推**（issue #95，@lzxN / @frank0532）：`subscribe_whole_quote(["cu2610.SF"])` 只推一帧然后没了，而 `CF701.ZF` 每 250ms 一推。那一帧是订阅时的**首帧快照**（走 `get_full_tick`，保留大小写），它后面的周期订阅从来没工作过。

  看着像交易所差异，其实是大小写：

  ```
  cu2610.SF   .upper() -> CU2610.SF    ← QMT 没有这个合约
  CF701.ZF    .upper() -> CF701.ZF     ← 恰好不变，所以能用
  ```

  订阅管理器自己做了一次无条件 `.upper()`，**绕开了 `normalize_stock_code`** —— 后者从 #58 起就专门为 `.SF` / `.DF` / `.IF` / `.ZF` / `.INE` / `.GF` 保留调用方的原始大小写，正因为期货合约是小写的。所以「郑商所行、上期所不行」其实是「大写的行、小写的不行」。

  现在分开处理：裸交易所令牌（`SH` / `sz` / `if`）仍然大写，带后缀的合约走 `normalize_stock_code`，解析不了的回落而不是让整个订阅崩掉。`cu2610.SF` 和 `CU2610.SF` 现在是两个订阅 —— QMT 只有其中一个，合并等于把错的字符串发给交易所。

  **未实盘验证**：本终端无期货行情（`get_instrument_detail("IF2609.IF")` 返回 0 字段，报告人那台返回 33）。等 @lzxN 复测。

- **板块写入 API：从静默空操作改成写完回读校验**（issue #143，由 #142 @DwayneZhang 引出）：`create_sector` 在大 QMT 上「能调、返回 None、什么都不做」—— 实测板块数量前后都是 13，调用方却以为建好了。这是最坏的一种失败。

  先把三条通道枚举了一遍（`probe_capabilities` 新增 `sector_probe` 块，只读）：

  | | 有什么 |
  |---|---|
  | `ContextInfo` | `create_sector`、`get_sector`、`get_stock_list_in_sector` |
  | QMT 注入的全局函数 | **一个都没有** |
  | 原生 xtdata SDK | `add_sector`、`remove_sector`、`get_sector_list` 等，但 `无法连接行情服务！` |

  **这推翻了 issue #143 自己的说法**：`create_sector_folder` / `add_stock_to_sector` / `reset_sector_stock_list` / `remove_stock_from_sector` 不是「QMT 全局函数还没捕获」，它们在三条通道上都不存在。文档 §4.7 那个 `create_sector(parent_node, sector_name, overwrite)` 三参数签名同样不存在，所以签名保持 `(sector_name, stock_list)` —— 那才是真 SDK 给的形状，改成一个不存在的签名只是把一个错答案换成另一个。

  现在：这一族按 `add_sector` 组合实现（`add_stock_to_sector` 用读-合并-写，所以底层是覆盖式还是追加式都对），**每次写入后回读校验**，没写进去就抛错并说明原因。宁可把一次成功的写入误报成失败，也不能再让调用方以为写进去了。`create_sector_folder` 三条通道都没有，直接抛 `NotImplementedError`。

- **`get_sector_list` 不再用硬编码列表冒充真数据**（issue #143）：拿不到真实板块时它会返回 13 个常用板块名，和真列表**长得一模一样**，调用方分辨不出来，用户自建的板块永远不出现。我自己就是读了这个列表在 #130 给过一条错的建议。

  现在拿不到就抛错，错误信息里写明原因和出路；想要那 13 个名字就显式传 `allow_fallback=True` —— 它们驱动 `get_stock_list_in_sector` 仍然有效（沪深A股 5217 只，实测）。**主动要是可以的，不问就塞给你不行。**

- **回测结束时客户端超时，而不是被告知「结束了」**（issue #150，@chinapsu）：一次跑完的回测在最后抛 `TimeoutError: backtest ZMQ request timed out: next_bar`。

  告知机制本来就有 —— 状态里带 `done`，`BacktestStrategy.run()` 见到就跳出循环再调 `finish()`。信号送不到，是因为 QMT 实际调用的那个入口顺序不对：

  ```python
  def stop(ContextInfo=None):
      _RUNTIME.on_qmt_stop()      # 置 qmt_completed，唤醒等待者
      _RUNTIME.stop_server()      # ……同时把 socket 拆了
  ```

  客户端要么正停在 `next_bar` 里（唤醒了，但回复还得走 ZMQ 回去，而 socket 正在关），要么两次调用之间（`next_bar` 发进一个已经没人的端口）。就算赢了这个竞争也只是把失败推后一步 —— `run()` 紧接着还要调 `finish()`。

  `stop_server` 本身没错，它是 #109 的修复（端口留给了一个已经不存在的策略，下次跑起不来）。所以关闭流程现在两件事都做：**先把结局交给客户端，再释放端口** —— 和 `reload_deployment` 等响应队列排空再 `reset_app` 是同一个形状。等待有上限（默认 10 秒，`stop_grace_seconds` 可配），走掉的客户端不会把固定端口占死；从没连过客户端时完全不等，手动点停止不会平白多花 10 秒。

  已有测试没盖到是因为它直接调 `session.on_qmt_stop()`，**从不走模块级 `stop()`** —— 这个 issue 讲的那段拆除逻辑根本不在测试里。

- **撤单：原生返回为假时，改用委托状态确认**（issue #148，PR #149，@willzhqiang）：大 QMT 注入的 `cancel` 返回值描述的是「撤单请求有没有发出去」，不是撤单结果。@willzhqiang 的终端上实测到原生返回为假、但券商已受理且委托 67ms 内从状态 50 变成 54 —— 桥把一次成功的撤单报成了失败。

  现在原生返回为假时不再直接当失败，而是把回复挂起，在 adjust 线程上按 `order_sys_id` 精确回读委托状态：53/54（部撤/已撤）报成功，56/57（已成/废单）、查询失败、委托查不到、超时仍活跃报失败。原生返回为真仍走原来的快速路径。

  挂起复用 #44 那套 settlement 队列，不新起线程、不在 RPC 路径上 sleep —— `get_trade_detail_data` 在工作线程上返回空，所以状态回读只能在主线程做。

  **尚未修完**：原生返回为真时仍然直接采信，而本仓库终端上实测**撤一个不存在的委托也返回真**（issue #151）—— 原生返回在两个方向上都不可信。状态 51/52（已报待撤、部成待撤）也还没有单独归类，目前会一路轮询到超时后报失败。

- **大 QMT 期权 tick 快照与实时订阅兼容**：部分完整大 QMT 版本对显式
  `.SHO/.SZO` 合约的 `ContextInfo.get_full_tick` 返回空，且
  `subscribe_whole_quote` 不推送该合约。缺失的期权快照现在从
  `get_market_data_ex(period="tick", count=1)` 补齐；显式期权订阅改用
  `ContextInfo.subscribe_quote(..., result_type="list")`，并将列数组规范化为
  最新一笔五档 tick。股票、ETF 和市场代码仍走原有共享全推路径；混合组合会
  统一管理多个底层句柄，失败时回滚、退订时完整清理。

  2026-09-02 盘中在完整大 QMT 2.1.19.0 实测：`10010974.SHO` 的
  `get_full_tick` 从空结果恢复为实时五档；单期权订阅连续收到 500ms 推送；
  `510050.SH + 10010974.SHO` 混合组合同时收到 ETF 与期权；510050 202609
  期权链 IV/Greeks 仍为 28/28 有效。

  另在本仓库的国金 2.1.19.0 终端上**独立复现并验证**：修复前
  `get_full_tick(["10010974.SHO"])` 返回 `{}`，且混合请求
  `["510050.SH", "10010974.SHO"]` **只回 510050 —— 期权静默消失**，调用方拿到
  一个看起来正常的结果却少一个代码；修复后两者都返回 lastPrice / volume /
  五档。订阅初始快照同样：修复前单期权那一帧一个代码都没有，修复后期权在。
  连续推送未在本终端验证（测时为午休时段）。

## [0.3.12] - 2026-09-02

### 新增

- **`redis_enabled` 开关：可以声明「这台机器没有 redis」**（issue #147）。以前声明不了 —— `configure_runtime` 无条件下发 redis 块，而 `REDIS_HOST` / `REDIS_PORT` 有默认值（`127.0.0.1:6379`），所以「配了 redis」和「什么都没写」从配置里分辨不出来。五个使用方各自的 `if not redis_config: return None` 守卫因此全是死代码。

  ```python
  BIGQMT_REDIS_CONFIG = {
      "redis_enabled": False,     # 仅对非 redis 传输生效
      "transport": "zmq",
  }
  ```

  设 False 后整个 redis 块不再下发，委托身份库、异步下载任务、全推快照缓存、exec 事件推送**一次都不会去连**。`transport=redis` 时开关被忽略 —— 那种部署没 redis 就没有桥。

  代价（例子配置里写明了，因为否则是静默的）：查询里的 `strategy_name` 回填失效（#133）；异步下载任务和全推快照缓存不可用 —— 后两个在大 QMT 上本来就默认关闭。**委托/成交回调不受影响**，走 zmq 推送通道。

  `bigqmt_no_redis/DRYRUN_no_redis.py` 和单文件构建器现在默认带上这一项 —— 最确定会撞上这个问题的构建，恰恰是最没法表达它的。

### 修复

- **连不上的 redis 会吞掉委托/成交回调**（issue #145，@heimo88）：`_exec_event_sink` 只要 redis「available」就优先，而 available 只意味着**配了**。redis-py 是惰性连接的，配了但连不上时 client 建得出来、每次 publish 才超时 —— **回调全丢，而旁边工作正常的 zmq 推送通道一次都没被用上**，每个事件还刷一整段 traceback。

  现在：publish 失败**立刻回落到推送通道**（回调照样送达，不再丢）；连续 3 次失败降级 redis，且**只在有地方可降时才降**（降到 None 等于把吵闹的失败变成静默的失败）；traceback 限流 —— 前 3 次全量（issue #76 挣来的），之后每 50 次一行摘要。

- **两个桥接器抢同一个日志文件**（issue #144，@sumo225270）：同机同账号跑实盘桥 + 模拟桥，两个客户端都回落到 `~/.cache/bigqmt/logs/bigqmt.log`。两个进程两个句柄，Windows 永远拒绝轮转重命名，而轮转不成功意味着 `backupCount` 清理也永不执行 —— 日志无限增长。

  0.3.11 的 #139 修的是**同一进程内** handler 累积；这条是**跨进程**，进程内怎么管都够不着。日志文件名现在带进程标识：有 `BIGQMT_ACCOUNT_ID` 就用账号（跨重启稳定，轮转能接上），否则用 PID，`BIGQMT_LOG_NAME` 可以钉死。轮转失败也不再抛 —— 丢一次轮转比每写一条日志刷一段 traceback 好。

  实盘：部署后日志目录里是 `bigqmt-pid51044.log` / `bigqmt-pid76544.log` / `bigqmt-pid88484.log`，每条日志只出现 1 次；`bigqmt.log.2026-09-01` 出现了 —— **这个部署有史以来第一次轮转成功**。面板轮转报错从部署前 7 次变为部署后 0 次。

### 已知限制

- **#145 的失败降级路径未经实盘验证**：本机 redis 是通的，走不到那条分支。只有单元测试覆盖（16 个用例）。要实盘验证得停掉 redis 再下单，两件都没做。
- **`redis_enabled=False` 的效果未经实盘验证**：本机有 redis，不需要关。默认值（True）的**无回归**已验证 —— redis 块照常下发、exec 事件仍选 redis、委托身份库端到端跑通。

## [0.3.11] - 2026-09-01

### 修复

- **失败的 QMT 登录被报成启动成功**（PR #141，@willzhqiang）：FormulaServer 的 58600 端口**在登录框还开着的时候就已经在监听**。`open_qmt()` 提交凭据后立刻用这个端口判就绪，于是券商拒绝或超时的登录会被当成终端启动成功 —— 而桥根本没挂上。

  实盘复现（#140 合并后做负向路径验证时发现）：账号密码正确填入并提交、**58600 在监听**、登录框仍在、QMT 报 `200003 超时`，`XtClient` 日志里 `CProxyClient::onLogin ... status = 21`、`slot_onLoginStatus ... 错误200003,超时`。旧的就绪检查立刻返回成功。

  现在提交凭据后要等窗口从登录框形状过渡到主界面形状才算登录完成；登录框不消失则超时并抛出具体的 `QmtLauncherError`。端口就绪退回成后续的进程健康检查，不再能替代「登录完成」。

### 已知限制

- **非最大化的主界面会让成功的登录被判失败**：窗口识别用的是比例判据（<65%，见 0.3.10 的 #140），主界面如果没最大化（如 1100x700 / 1920x1200 = 0.57x0.58）会一直被当成登录框，登录成功也会抛 `QmtLauncherError`，阻断无人值守重启。

  仍然合入的理由：**修复前是「登录失败被静默报成成功」，修复后最坏是「成功被报成失败」** —— 后者声音大、消息里写明原因，前者看不见。响的失败胜过静默的错误。

  后续改进方向：判据改成相对的（拿提交前的登录窗 handle/rect 做基准，变了即成功），不依赖主界面的绝对比例。

## [0.3.10] - 2026-09-01

### 修复

- **QMT 自动登录在 DPI 缩放下点错位置**（PR #140，@willzhqiang）：**这条解释了 0.3.8 里记的那次实盘事故** —— 「账号框坐标原本打在右侧下拉箭头上，导致密码被追加进账号框」。当时挪了坐标、加了字段级像素验证，但**没找到坐标为什么会偏**。

  原因：`pyautogui` 在 import 时会开启进程 DPI 感知。**先量窗口、后 import pyautogui** 的话，`GetWindowRect` 返回逻辑坐标而 `SetCursorPos` 吃物理坐标 —— 150% 缩放下一次「安全的账号框点击」会落到几百像素之外。现在在第一次枚举窗口之前就调 `SetProcessDPIAware()`。

  登录窗识别也从绝对像素阈值（`<800 且 <600`）改成比例判据：同一个国金登录窗，DPI 虚拟化下报 832x591、DPI 感知后报 1248x886，而屏幕尺寸同步放大，**比例对缩放不变**。

  已知边界：非最大化的主界面（如 1100x700 / 1920x1200 = 0.57x0.58）会被误判成登录框，旧的绝对阈值在这里反而安全。真正的防线是其后的字段级像素验证 —— 打字落到错误字段会清空泄露并中止、绝不提交，所以最坏是一次失败的登录尝试。

### 新增

- **本地期权 IV 与希腊字母**（PR #138，@willzhqiang）：部分大 QMT 环境的 `get_option_iv` 恒返回 `0.0` 且没有希腊字母接口。新增**零依赖**的 Black-Scholes-Merton 定价、有界二分求解隐含波动率、以及 Delta/Gamma/Vega/Theta/Rho。

  - `xtdata.get_option_analytics(code)` 单合约；`xtdata.get_option_chain_analytics(code, expiry)` 整条链（一次批量取收盘价）
  - **单位显式暴露**：`vega_1pct` / `theta_per_day` / `rho_1pct` 与原始值并列 —— 不同库在这里的口径几乎总是不一样（除不除 100、除 365 还是 252），而单位错了的结果看起来完全正常
  - 无套利边界校验；坏合约保留 `analytics_error` 而不是毒化或中断整条链
  - 命令行 `qmt-trader option-greeks`
  - **纯客户端**：不改 RPC 白名单、不动原生 `get_option_iv` 语义，并有 AST 扫描的不变式测试保证服务端模块不会 import 它

  数学独立核对过（不看 PR 自带的测试，按公式另写一份参考实现）：价格与希腊字母**逐项 0.00e+00**，与 Hull 教科书公布值一致（call 4.759 / put 0.808）；隐含波动率往返在 0.05–3.0 区间误差 ~1e-12。边界（T=0 / sigma=0 / 价格越界）全部显式抛错并带上实际边界值。

- **`qmt-trader` CLI 不再被旧部署包遮蔽**（PR #138）：QMT 目录常带一份同名的旧包，原来它被 `sys.path.insert(0)` 放在最前，新的 CLI 命令会在那份旧代码上执行。改成 `append` —— 仍能发现 `local_config.py`，但不再盖住选定的包。

### 已知限制

- **期权解析部分本机复核不了实盘**：本机账户无期权权限。数学、边界、接口设计和 import 边界都逐项验过，但「在真实期权链上跑出来的数字」只有贡献者的记录：`10010975.SHO`（到期 20260923）、2026-09-01 21:11、标的 3.055、期权 0.0198、本地 IV 0.1236642706、原生 `get_option_iv` 0.0、重定价误差 1.3e-11；同次 510050 202609 链 28/28 成功。
- **DPI 登录修复由贡献者实盘验证**：本机未做真实自动登录测试（会动到实盘终端的登录流程）。贡献者在自己机器上做了两次真实 QMT 自动登录重启。

## [0.3.9] - 2026-09-01

### 新增

- **期货 / ETF 期权 opType 直通**（PR #131，close #129）：期货把「开/平、今/昨」编码在 opType 里，ETF 期权还多一个「备兑」。以前映射回 BUY/SELL 再重拼 opType 会丢掉这些 —— **平今多会变成普通卖出**，和 #103 里融资买入变普通买入同一类错误。现在 0-15（期货/股指期权/商品期权）和 50-59（ETF 期权）原样透传。

  每个值都对着官方枚举逐个核过（`docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md` 10.1）：平昨空(4)/平今空(5)/平空(8,9) 归 BUY，平昨多(1)/平今多(2)/平多(6,7) 归 SELL；备兑开仓(54) 是 SELL、备兑平仓(55) 是 BUY；56-59（行权/锁定/解锁）没有方向，要求显式传 `action`。16-22 官方未定义，不收；ETF 期权起于 50 不是 48（48/49 属组合交易）。

  **股票账号收到期货 opType 直接抛错，不回落到 23/24** —— 回落会发出一笔品种和方向都不对的真实股票单。

### 修复

- **`bigqmt-init` 生成的 ZMQ 客户端从来连不上**（PR #137，close #136，@willzhqiang）：向导和传输层各算各的端口 —— `15000 + n%1000` vs `15560 + n%100`。两者要相等得满足 `(n%1000)-(n%100) == 560`，而这个差恒为 100 的倍数，**560 不在里面**。暴力验证 0..99999：**0 个账号能对上**。而症状只是一个干巴巴的超时。

  host 也是错的：连接地址用的是 **Redis 的 host**，而服务端默认只绑 loopback、且向导写的服务端配置里根本没有 zmq 段。现在两边由同一个 `_default_zmq_address` 派生，服务端配置写 `bind_address`（同机 loopback / 跨机 0.0.0.0 并提示防火墙），向导也改问「QMT 终端地址」。

  实盘对上了：运行中的桥绑 `tcp://127.0.0.1:15563`，向导现在生成的正是这个（修复前会生成 15503）。

- **QMT 注入函数在直挂形态下不可见**（PR #134，@cnwuwil）：QMT 以 exec 挂载策略文件，`passorder` / `download_history_data` 等**只注入挂载文件的命名空间**，strategy 侧的 globals/builtins 查找看不见 —— 所有交易/下载/查询 RPC 静默失效。runtime 直挂时改为自捕获，名单收敛到 strategy 单一事实源（`_QMT_INJECTED_GLOBAL_FUNCS`）+ 三重防漂移测试。壳形态（BIGQMT_REDIS_DRYRUN）零变化，两个方向都有测试钉住。

- **none 复权读缺股不自愈**（PR #134）：大 QMT 的 raw store 非全市场铺满（实测 5225 只请求只回 8 只）。附带修掉一个更隐蔽的：`get_market_data` 返回的是 **field 键形状**（`{field: {code: [..]}}`），原实现在顶层找 code，**所有代码永远「缺失」**。判据收敛为「过半缺失才自愈」，少数常驻无数据代码（退市/停牌/无权限）不再让每次读都付下载 + 重读成本。

- **cache 禁用时下载失败仍报满进度**（PR #134）：服务端下载 RPC 被 `except: pass` 吞掉后照样返回 `{finished: total}` —— 正是 #47 定义过的假进度。禁用分支（无拉取可兜底）改为直接抛。

- **日志 handler 每次重启/reload 累积一个**（issue #139）：同一条日志被写进 `bigqmt.log` **16 遍**，QMT 面板里单实例出现 373 次。

  幂等的依据放在模块级 `_initialized` 上，而入口每次启动都 `_clear_local_modules()`（`reload_deployment()` 同样 purge）—— 模块状态归零，而 `logging.getLogger("bigqmt")` 活在 logging 的全局注册表里、活得过。**模块重置、logger 不重置，于是每启动一次就多两个 handler。**

  后果三个，一个比一个隐蔽：日志膨胀 16 倍淹掉真错误；16 个句柄占同一个文件让轮转必然 `WinError 32`（每写一条日志抛一次）；轮转从不成功导致 `backupCount` 清理永不执行、`BIGQMT_LOG_RETENTION_DAYS` 形同虚设。

  修法是把幂等依据挪到 logger 自己，并且 `removeHandler` **必须配 `handler.close()`** —— 只摘不关，文件句柄还在。

### 已知限制

- **期货 / 期权下单路径未经实盘验证**：本机账户无期货和期权权限，PR #131 的下单链路只有单元测试覆盖。透传守卫（股票账号收到期货 opType 抛错）同理未实盘触发。
- **直挂形态由贡献者验证，非本机**：PR #134 的核心主张（注入函数自捕获）由 @cnwuwil 在自己的模拟端验证（`probe_capabilities`、`get_ipo_data` 返回真实数据）；本机跑的是 DRYRUN 壳形态，只验了壳形态无回归。
- **none 复权自愈在部分终端够不着**：判据按「代码是否在返回的 key 里」判，而本机终端**给每个请求的代码都返回 key、无数据时给空帧**，因此 `missing` 恒为 0、自愈永不触发（没好处也没坏处）。贡献者的终端是真的省略 key，所以在那边工作。按行数判可同时覆盖两种，留作后续改进。
- **日志轮转恢复是推断，非实测**：句柄数从 16 降到 1 消除了 `WinError 32` 的成因，但轮转在午夜发生，本次未观察到成功轮转。

## [0.3.8] - 2026-09-01

### 新增

- **`reload_deployment` / `reload_status`：同步代码后不必重启策略**。QMT 把模块留在 `sys.modules` 里，所以以前每次改动都要人工重启（修 #133 那天重启了七次）。现在 `xt_trader.reload_deployment()` 把所有 `bigqmt_signal_trader.*` 从 `sys.modules` 清掉、重新绑定策略模块 import 时持有的引用、再跑一次 `init()` 重建对象图，约 0.8 秒。

  用 purge 而不是 `importlib.reload`：reload 必须按依赖顺序（`order_bigqmt` 在 import 时 `from ..models import OrderSnapshot`，顺序错了会静默留住旧类），purge 没有顺序问题。

  **只是「已排期」**：执行它要 `reset_app()`，那会停掉正在应答这个请求的 RPC 服务，所以回复必须先出去；真正的重载在下一个 adjust tick 上做，轮询 `reload_status()` 看结果（`ok` / `modules_purged` / 前后版本号）。

  **刷新不了 `bigqmt_signal_trader_strategy.py` 和 `BIGQMT_REDIS_DRYRUN.py`** —— QMT 自己 exec 这两个文件，模块没法 reload 自己所在的模块。改这两个仍要重启。

  实盘验证不是「跑完没报错」：把部署目录里的 `version.py` 改成探针值，reload 后 `ping` 跟着变，还原文件再 reload 又变回来。两次 reload、零重启。

- **`describe_trade_detail_fields` 诊断 RPC**：返回 QMT 自己的 ORDER / DEAL 行上**有哪些属性名**（只有名字，不返回值——行里有价格、数量、柜台编号，而这条走公共通道）。#113、#130、#133 全是「某字段缺失」，而分辨「是终端没给还是桥没转发」以前每次都要一轮部署+重启。

- **能力探测 RPC `probe_capabilities`**：只读探查当前部署暴露了哪些 QMT callable——运行时全局函数绑定（passorder/download/信用等 20 项）、ContextInfo 方法存在性、信用接口只读试调（行数/报错）。部署后跑一次即可确认这台券商 QMT 的能力边界。参考 cfquant 的 credit probe 思路。

- **docs/DEPLOY_QUICKSTART.md**：单账号部署最短路径（5 步 + 部署期常见问题表），README 快速开始入口挂载。

- **docs/LATENCY_REPORT.md**：延迟报告独立成文（传输层对比、FormulaServer 直连、下单链路各环节、方法论声明）。

### 修复

- **`query_stock_orders` / `query_stock_trades` 字段缺失**（Issue #133，@sumo225270）：委托缺 `account_type` / `instrument_name`，成交缺 `account_type`，两边 `strategy_name` 恒为空。三个字段三个不同原因：

  - `account_type` 桥根本没发过（持仓发了，但**硬编码成 2**，信用账户上就是错的——和 #92 同一类静默）。现在取部署实际配置的类型。
  - `instrument_name` 从没从行上读过。已按持仓行同样的方式读 `m_strInstrumentName`，另加 `ContextInfo.get_stock_name` 兜底（按代码缓存）。
  - `strategy_name` **QMT 根本不在行上给** —— 实盘列出全部属性：ORDER 120 个、DEAL 47 个，**都没有 `m_strStrategyName`**。它按策略过滤却从不回报。经本桥下的委托改从自己的身份库回填（下单时就记了，键是作为委托备注发出去的 `user_order_id`）；手工单没有备注，保持为空——编一个比空字符串更糟。

  顺带补齐 `xttype.XtOrder` / `XtTrade` 契约里其余漏掉的：`secu_account`、`offset_flag`、`direction`，成交多一个 `commission`。实盘发现 DEAL 行**同时**有 `m_dComssion`（QMT 自己拼错的）和 `m_dCommission`，「第一个存在的属性」可能停在 0.0，改成取第一个非零。

- **大 QMT 加载的是它自带的 `xtquant`，不是本仓库的 shim**（线上事故）：终端自带 `bin.x64/Lib/site-packages/xtquant/`，沙箱里 `from xtquant import xtconstant` 命中的是那一份——**91 个名字，本仓库 shim 有 538 个**。没有一个值不同，447 个纯粹是没有，`ACCOUNT_TYPE_DICT` 就在其中。在 import 时读它，本地测试全绿，实盘重启后 `build_app` 抛 `AttributeError`，`init` 直接死，**所有委托/持仓查询都答 `order_gateway is not configured`**，而 ping 正常、adjust 照跑，从外面看桥是活的。现在只用两份都有的常量，并把终端实际的 91 个名字快照进测试钉住。

- **`download_sector_data` 等 6 个方法缺客户端包装**（Issue #130，@happyybb / PR #132）：服务端适配器和 RPC 白名单一直都有，缺的只是 `BigQmtXtData` 上那层包装，于是直接撞 `AttributeError`。补上 `download_sector_data` / `download_cb_data` / `download_index_weight` / `download_history_contracts` / `get_stock_type` / `subscribe_l2thousand`，并加不变式测试：白名单里每个方法要么有包装、要么明确声明为 `call_method` 专用。

  **注意**：前五个在大 QMT 上仍会报错（没有可连的原生 xtdata 行情服务），改的是把看不出原因的 `AttributeError` 换成说明原因的错误。**板块数据不用下载**，`get_sector_list()` / `get_stock_list_in_sector()` 直接可读（实测 13 个板块、沪深A股 5217 只）。

  `get_stock_type` 单独处理：它不报错，但对**任何**代码都返回 `0`（实测股票/ETF/债券/期权全一样）。恒为 0 的「类型」比报错更糟——报错看得见，错的分类看不见。改成显式抛错并指向真正能用的 `get_instrument_type()`。

- **入口当普通脚本运行时毫无提示**（Issue #123，@lzxN）：日志看着像正常启动然后「结束运行」，唯一的线索是 `download globals bound=[]` 是空的、且没有 `init ok`。那说明 QMT **没注入任何 API 全局**，文件是被当普通脚本执行的，`init()` 永远不会被调用，RPC 服务不启动，外部连不上是因为根本没东西在监听。现在会直说，并点名两个已知原因：在策略编辑器界面运行、勾了「独立 python 进程」。

- **`on_account_status` 恒报 STOCK**（Issue #103，@fengzhizialex）：信用账户上也答 `stock 1`。改为报服务端实际配置的类型；客户端声明与服务端不一致时告警（客户端的 `StockAccount(id, "CREDIT")` **不会**传到 QMT 侧，静默不一致正是 #92 难查的原因）。

- **委托身份库与下载任务只在 redis 传输下可用**：两者都读 `download_job_redis_client`，而**只有 redis 传输才会建这个 client**，zmq 部署上是 `None`。后果是下单时从没记过委托身份（于是 #133 的策略名永远回填不了），以及 `submit_download_history_data` 恒报 `download jobs require a Redis client`——而干活的 `_pump_download_jobs` 每个 adjust tick 都在跑。现在两者都从 redis 配置取 client，与传输无关。

  同时**下载任务在 worker 关闭时改为拒绝提交**（大 QMT 上默认关闭，因为内嵌 xtdata SDK 没有可连的数据服务）——否则任务进了没人消费的队列，看起来成功、永远不完成。拒绝信息里说明开关名和替代做法。

- **qmt_launcher login 误输防护**（实盘事故修复）：账号框坐标原本打在右侧下拉箭头上（点它会展开账号列表），导致密码被追加进账号框；坐标改到输入框正中，且每步打完字都做字段级像素验证（账号必须进账号区、密码首字符必须进密码区、打密码期间账号区不许变），失败立即清空泄露并中止，绝不提交错误表单。

- **zmq 出站堆积硬阻塞**：大 payload（全市场快照几 MB）与冷请求共享管道时，peer 水位满会让 send_multipart 阻塞 ~200ms、拖死 router 线程。内联发送改 DONTWAIT，堵了让位进有界队列逐拍重试（超 2000 丢最旧+记日志）。

- **linkmini 模式误导**：它起的是迷你终端（无策略编辑器/ContextInfo），对本项目的桥不可用——README 与 docstring 已明确。

- **subscribe_quote 盘中推送旧快照**（Issue #104）：订阅轮询默认走 FormulaServer 直连，其快照可能滞后数小时（实盘实测 11:30 后冻结、收盘后仍停在午间数据），导致"刚完成的 bar"被推成数小时前的旧值。订阅轮询改走 RPC 桥读 QMT 实时数据（`use_formula=False`，client.call / get_market_data_ex 新增该开关，默认行为不变）。实盘验证：最新 bar 为 15:00 收盘 bar 而非 11:30 旧快照。

- **FormulaServer 快照滞后检测 + 自动回落**：intraday（tick/1m/5m/15m/30m/1h）直连回答的最新 bar 滞后超过 30 分钟（或跨日）时，本次调用**自动回落 RPC 桥拿实时数据**（不是只告警），并进入 120s 冷却期——冷却内 get_market_data_ex 直接跳过直连（不付双倍成本），到期自动重新探测（自愈）。告警同 code+period 每日一次。实盘验证：滞后检出 → 回落拿到 15:00 收盘 bar；冷却期跳过；到期恢复直连。

- **qmt_launcher 锁屏防护**：`restart_qmt` 在会话锁屏且需要自动登录时直接拒绝执行（否则关掉终端却登不回去，交易中断）；新增 `session_is_locked()` 检测。

## [0.3.7] - 2026-08-31

### 新增

- **卡顿监控：区分「桥卡住」和「桥死了」**：`zmq slow handler` 是在 handler **返回之后**计时的，所以一个阻塞住的调用在结束之前什么都不打印。实测遇到过一次 346 秒的阻塞，那期间日志只有 adjust 心跳、每 10 秒 100 拍，**看上去一切健康**——而从客户端看，卡住和死掉是同一件事：超时。

  现在有 watchdog 线程在 handler **还在跑的时候**就报：

  ```
  [bigqmt_rpc] zmq handler STILL RUNNING method=get_full_tick 6s
  thread=bigqmt-zmq-rpc queued=0 -- the bridge is blocked, not dead
  ```

  默认 20 秒触发——比实测最慢的健康调用（整市场快照 7.7s）长得多，又短于客户端 30 秒的默认超时，**所以日志会在调用方放弃之前就点名**。指数退避，一次长阻塞不会把它自己要解释的日志淹掉。`BIGQMT_REDIS_CONFIG["zmq"]["stall_warn_seconds"] = 0` 关闭。

- **启动预热**：重启后第一次 `get_financial_data` 可能要几分钟（实测 346 秒）。启动后会在**后台守护线程**上先跑一次，把这份等待提前付掉，并留下日志。

  **特意不放主线程**：启动诊断跑在 `init()` 里，把一个可能几百秒的调用加进去，会在 adjust 定时器都还没排上的时候冻住启动——比原问题更糟。`BIGQMT_REDIS_CONFIG["warm_context_data"] = False` 关闭。

  预热**会自检取到多少行**，取不到直说：

  ```
  [bigqmt_warmup] get_financial_data warm in 0.41s (159 rows)
  [bigqmt_warmup] get_financial_data returned NOTHING in 0.02s -- the probe
  did not exercise the path it is meant to warm
  ```

  这个自检是实盘验证时挣来的：第一版预热用了**空的日期区间**，被 QMT 接受、瞬间返回 `None`，日志却报 `warm in 0.00s` 的成功。一个静默空转的预热比不做更糟。

### 实盘验证

四次策略重启，逐项验证：

- ✅ **watchdog**：临时把阈值调到 5 秒，跑一个 15.94 秒的请求，日志在 6s 和 11s 各报一次（退避生效），方法名/线程/队列深度均正确，**报的时候调用还在跑**。
- ✅ **预热**：`warm in 0.00s (242 rows)`，确认真取到数。
- ❌ **预热能否挡住那 346 秒：未证明**。见下。

### 已知限制

- **预热对 346 秒的实际效果尚未证明。** 当天策略重启四次，346 秒只在第一次出现；预热现在报 0.00s，说明该调用本来就是热的——**它预热的是一个已经热的东西**。那份代价更可能在 QMT **终端**进程里，而终端当天没有重启。要验证只能等终端重启后的第一次。机制可用、自检可靠，效果待实证。
- **346 秒的根因仍未查清**：为什么一个 `ContextInfo` 调用会在后台 listener 线程上阻塞数百秒，而 QMT 自身健康、主策略线程空闲。已排除：按代码的冷缓存、按表的冷缓存、我们自己的字段翻译、以及「QMT 行情订阅重建慢」（曾据此解释，后被推翻——所依据的日志空档当天还有多处，完全正常）。watchdog 是下次复现时的眼睛。
- 其余同 0.3.6。

---

## [0.3.6] - 2026-08-31

### 修复

- **可转债的最小申报量、市场推断与报价精度**（PR #121）：三处同一个原因——代码里没有「债券」这个概念，一律按股票处理。

  | 位置 | 修复前 | 后果 |
  |---|---|---|
  | `code_utils.min_lot()` | 可转债返回 100 | `round_buy_volume` 把一手转债算成 `(10 // 100) * 100 == 0`，**单子直接废掉** |
  | `code_utils.normalize_stock_code()` | 裸 6 位码按「5/6 开头 = 沪市」 | 沪市转债 110/111/113/118/132 都是 `1` 开头，**被判到深市**，拿去下单是另一只票 |
  | `price_engine._price_precision()` | 只把 15/16/51/52 当 3 位小数 | 转债报价精度同样是 0.001，按 2 位取整会被交易所拒单 |

  是在给 [bigqmt-dashboard](https://github.com/litaolemo/bigqmt_dashboard) 接可转债交易时踩出来的。

- **直连路径对四个字段静默返回 NaN**（Issue #104）：FormulaServer 直连**接受任何字段名**，对它没有的四个字段不报错，而是返回一整列 `NaN`。实盘量的：

  ```
  field_list=[...11 个字段名...]   0.015s   preClose=nan   suspendFlag=nan
  field_list=[]                    (RPC)    preClose=9.0   suspendFlag=0
  ```

  同样的列、同样的形状、快 12 倍、数据静默错了。而这正是有人在被告知「写明字段名能走快路径」之后会做的事——客户端自己还打印了这条提示。

  `settelementPrice` / `openInterest` / `preClose` / `suspendFlag` 是**日频元数据**，不是 K 线数据，所以直连缺的正好是这四个。

  现在沿用 `dividend_type` 与 `period=tick` 已有的规矩：**这条路径答不诚实的请求，交给 RPC**。用白名单（`open/high/low/close/volume/amount/time/stime`）而不是黑名单——不认识的字段名走 RPC 只是慢，猜它「大概能供」则可能静默出错，而这个 bug 正是这么来的。

- **文件损坏时抛裸 `NameError`**（Issue #102，@huliangyu）：他的策略文件第一行是一串 200 字符的 token，不是代码。Python 把它当变量名求值，报出：

  ```
  File "...\bigqmt_signal_trader_strategy.py", line 1, in <module>
      MiFBOecYoHXT4UUBBOIr3m5aTVbA5Rbt6OnG52cfBT5EAtPG9kA7kQnEsKu...
  NameError: name 'MiFBOecYoHXT4UUB...' is not defined
  ```

  这条报错**看不出文件坏了**，所以第一反应的答复是「可能是版本问题，重新部署」——而那对这个文件无效，**换任何版本都一样报错**。

  加载器现在在 exec 失败时看一眼第一行像不像 Python，像 token 就直说，并明确指出「换版本没用」。判定**故意做窄**：40 字符以上、无空白、字符全在 base64 字母表内——满足这三条的 Python 行只可能是裸标识符，本来就是坏代码。

  两个入口脚本（`BIGQMT_REDIS_DRYRUN.py` / `BIGQMT_ZMQ_BACKTEST.py`）各内联一份；它们是引导包的入口，不能反过来 import 包里的工具。

### 已实盘验证（本版全部改动）

- ✅ **字段守卫**：六列快路径 0.016s 不变；六列 + `preClose` 回落 RPC 返回**真值 9.0**（原为 `nan`）；显式写全 11 列返回四列真值（`preClose=9.0` / `suspendFlag=0` / `openInterest=13`），与 `field_list=[]` 对照一致。1m / 5m / 1d 三周期行为一致，陌生字段名（`turnoverRate` 等）实盘确认回落 RPC。
- ✅ **损坏文件报错**：端到端跑真实加载器——正常模块照常加载，`undefined_name_here` 仍抛它自己的 `NameError`（守卫够窄），token 文件给出新消息。**并在部署到 QMT 目录后，用 QMT 里那份实际执行的入口脚本复验通过。**

### 说明

- **如果你把入口脚本改过名**（例如 `BIGQMT_REDIS_DRYRUN.py` → `BIGQMT_REDIS.py`），`sync_deployment()` **不会更新它**——它只刷新部署目录里已存在的同名文件。改过名的入口需要手动重新拷一份。

### 已知限制

同 0.3.5：回测「启动 → 停止 → 再启动」、信用委托 27/28/40 到达券商的品种、撤单返回值、订单号双形态、长列表恢复的失败路径、期货行情，均待实盘验证。

---

## [0.3.5] - 2026-08-31

### 修复

- **长代码列表恢复失败时抛出无意义的错误**（Issue #104，@frank0532 在 0.3.4 上报告）：

  ```
  File ".../xtquant_compat.py", line 1163, in get_full_tick
      raise
  RuntimeError: No active exception to reraise
  ```

  这是 0.3.1 引入恢复逻辑时留下的 bug。那个 `raise` 在 `except` 块**外面**——except 已经退出，没有活跃异常可重新抛出，于是**真正发生的超时被换成了一条毫无意义的错误**。

  而且重读那边把自己的失败原因也吞了（`except Exception: return None`），所以即便修好 `raise`，仍然没有任何地方知道恢复为什么没成功。

  现在：原始异常按**原类型**重新抛出（写 `except TimeoutError` 的调用方照旧接得住），重读失败的原因写进 warning 日志。

  > 这让报错变得有用，**并不保证超长列表一定能成功**。要全量数据，市场令牌始终更快：`get_full_tick(["SH"], types=["all"])`，7.7s 一次请求。

### 已实盘验证（本次新增）

- ✅ **`subscribe_quote` 的分周期 K 线订阅**（@frank0532 在 #104 问及）：盘中实测 `period="1m"`，210 秒内 4 次回调，bar 时间戳精确间隔 60 秒；`1m` / `5m` / `1d` 均返回 `{code: DataFrame}`，列为 `time/open/high/low/close/volume/amount`。该能力自 0.3.0 起即已具备，此前从未实盘确认过。

### 已知限制

- **`passorder` 被调用但委托不出现**：若桥的策略运行在 QMT 的**编辑器**界面，`passorder` 会静默什么都不做（QMT 文档 1.2：「编辑器里执行的下单函数不会产生实际委托」；回测/模拟信号模式同理）。这不是桥的缺陷，但表现为 `passorder submitted but order not found in system`。请确认策略在**模型交易**界面运行。
- 其余同 0.3.4：回测「启动 → 停止 → 再启动」、信用委托 27/28/40 到达券商的品种、撤单返回值、订单号双形态、期货行情，均待实盘验证。

---

## [0.3.4] - 2026-08-30

### 修复

- **回测：三个报告，两个原因**（Issue #109，@wolfeee）

  **弃用提示和 `不支持'prev_close'数据字段` 是同一个 bug。** `ContextInfo.get_history_data` 只提供 `open` / `high` / `low` / `close` / `quoter` 五个字段（API 参考 5.2），且标着【不推荐】。而 bar 提取器把它想要的**每个**字段都拿去问它——`prev_close`、`preClose`、`lastClose`、`volume`、`amount`。这些问必然失败，而且**每问一次 QMT 就往自己的日志里写一行 ERROR**，每字段 × 每周期 × 每根 K 线。

  更没道理的是：提取器本来就用上一根 K 线的收盘价填 `prev_close`，所以这些问从头到尾没有可能有收益。现在先用主推接口 `get_market_data_ex`，`get_history_data` 只问它真正有的字段。

  **停止后重启 bind 失败是真的端口泄漏。** `stop()` 只告诉引擎回测结束，从没停过 ZMQ 服务——端口继续被一个已经不存在的策略占着（回测界面的停止按钮走的正是这条路径）。而且 `stop_server()` 只设标志就返回，服务线程最多 `poll_ms` 之后才关 socket，`init()` 紧接着就 bind 下一个。端点是固定端口，没有退到随机端口的余地，这个竞态的结果只能是 EADDRINUSE。

  现在 `stop()` 会停服务，`stop_server()` 会等 socket 真的关闭。bind 失败也会保留原因——光一句 `failed to bind` 会让人往错方向查，带上 `Address in use` 才知道有一个跑着的回测要先停。

- **客户端默认 RPC 超时从 6s 提到 30s**：验证 0.3.3 部署时发现 `get_financial_data` 对着一个健康的桥超时了。同一个桥上实测：`query_orders` 1.5s、`get_asset` 1.4s、`get_financial_data` 0.8s（热）、整市场 `get_full_tick` 7.7s。

  **超时比等待更糟**：请求已经到桥那边，桥会继续做完，只是客户端不听了。下一个调用就排在一份没人要的工作后面——**一次超时繁殖出更多超时**（实测中三个调用连续各超过 45s，正是桥在消化上一轮被 6s 掐掉的请求）。不会串号：transport 按 `request_id` 匹配响应，迟到的被丢弃。

  选 30s 是因为整市场快照那条路径本来就用 30s，现在是一个数而不是两个。`timeout_seconds=` 参数与 `BIGQMT_RPC_TIMEOUT_SECONDS` 环境变量照旧优先。

  **两个配置模板也带着 `6.0`** —— example，以及 `bigqmt-init` 给每个新用户生成的那份。只改默认值的话，跑过 init 的人都拿不到这个修复。三处由测试一起钉住。

  顺带更正 `get_full_tick` 文档字符串里「client default 120s」的错误说法。

### 已实盘验证（0.3.3 部署，本次确认）

- ✅ **多股多日期财务数据**（Issue #115）：双股 + 日期区间返回 `{'000017.SZ': DataFrame(159 行), '000001.SZ': DataFrame(159 行)}`，0.16s。修复前这里是 `{'is_copy': None}`。
- ✅ **未知 order_type 的新报错**（Issue #92）：`9999` 得到指名版本的新消息；`27` **被接受**（卡在后面的 `stock_code is required`），证明信用类型在部署端已生效；无参数时保持原消息；`32` 报「无隐含买卖方向」。
- ✅ **`types=` 收窄**（Issue #104）：`['stock']` 1.13s / 2315 只，`['all']` 7.74s / 26744 只，默认 0.85s / 2315 只。

### 已知限制

- **回测修复未实盘验证**：跑在 QMT 回测进程里，需要走一遍「启动 → 停止 → 再启动」。
- **信用委托 27 / 28 / 40 到达券商的品种未验证**：RPC 层已确认接受，但是否真的作为融资买入送达券商仍需信用账户实单。
- **撤单返回值未实盘验证**：需要一笔真实可撤委托。
- **订单号形态未在实盘记录上验证**：验证当日账户无委托无成交。
- **期货行情**：本机无期货权限。

---

## [0.3.3] - 2026-08-30

### 修复

- **未知 `order_type` 的报错误导性极强**（Issue #92）：传 `order_type=27` 得到的回应是 `action or order_type is required`——可调用方明明传了。报告人因此两次回来贴同一份 traceback，间隔半小时，一字不差；两次都在检查自己的调用。

  真正的原因报错里一个字都没提：**QMT 目录里部署的那份包早于信用委托类型**。这段代码跑在 QMT 里，客户端 `pip install --upgrade` 碰不到它。

  根因是那个检查**没有区分**「没传 order_type」和「传了但不认识」，两种情况共用同一条消息。现在分开：

  - 没传 → 保持原消息 `action or order_type is required`
  - 传了但不认识 → 说清楚是**哪个值**、**哪个版本**拒绝的、以及**升级客户端没用**

  ```
  order_type 9999 is not recognised by the package deployed in QMT (0.3.3).
  Credit order types (27-32, and 40-45 special) need 0.3.1 or newer HERE, in
  the QMT python directory -- upgrading the client with pip does not change
  this file. Run xt_trader.sync_deployment(), restart the strategy, then check
  xtdata.get_deployment_info().
  ```

  消息是**纯 ASCII** 的：QMT 的日志写入会丢非 ASCII 字符（本项目遇到过中文安装路径被吞成乱码），一条乱码的报错帮不上任何人。这一点由测试钉住。

### 已知限制

- 同 0.3.2。**信用委托类型（27/28/40）仍未实盘验证**——@fengzhizialex 已确认信用账户的资产与持仓正常（Issue #92），下单类型待其完成部署后验证。

---

## [0.3.2] - 2026-08-30

### ⚠️ 破坏性变更：`cancel_order_stock` 的返回值

**撤单以前返回 `True` / `False`，现在返回 `0`（成功）/ `-1`（失败），与 MiniQMT 一致。**

```python
# 需要改
if xt_trader.cancel_order_stock(acc, order_id):      # ✗
# 改成
if xt_trader.cancel_order_stock(acc, order_id) == 0: # ✓
```

这是有意向 MiniQMT 契约靠拢。原来的 bool **把判断反过来了**：MiniQMT 的写法是 `== 0`，而 Python 里 `False == 0` 为 True，所以撤单**失败**被读成成功，**成功**被读成失败。而我们自己的异步回调早就在用 `cancel_result=0` 表示成功，同一套 API 的两半互相矛盾。

### 修复

- **返回类型与 MiniQMT 不符**（Issue #113，@tokens-lin）：报的是 `order_stock` 返回字符串而不是数字。顺着查了整个接口面，同类问题三处：

  | 接口 | MiniQMT | 修复前 |
  |---|---|---|
  | `order_stock()` | `int`（失败 -1） | `str` 合同编号 |
  | `cancel_order_stock()` | `int`（0 成功） | `bool` |
  | `XtOrder.order_id` / `XtTrade.order_id` | `int` | `str` |

  另有一个边角：委托被拒时服务端返回的是**字符串** `"-1"`，它 truthy 且永不等于 `-1`，所以废单也被读成成功。

  大 QMT 没有 int 委托编号可给——`get_trade_detail_data` 只有 `m_strOrderSysID` 字符串。所以订单号做成 int 子类，两种形态同时成立：

  ```python
  order_id = xt_trader.order_stock(acc, "600000.SH", 23, 100, 11, 10.0, "s", "")

  isinstance(order_id, int)   # True，MiniQMT 写法照常
  order_id > 0                # True
  str(order_id)               # '合同编号'，券商原始串
  xt_trader.cancel_order_stock(acc, order_id)   # 撤单送回原始串
  ```

  纯数字编号（多数券商）int 值就是那个数字；非数字的给一个稳定正数替身，撤单仍用真实串。存进数据库变成普通 int 也能撤单——客户端记最近 4096 个映射。想要字符串用 `.order_sysid`，它一直是 str。

  同样规则用于回调对象 `XtOrderError` / `XtCancelError` / `XtOrderResponse` 的 `order_id`。

  原有 4 个测试文件里 **9 处断言把旧的字符串/布尔契约写死了**，测试与代码基于同一个错误前提，所以一直全绿。这些断言已改正。

- **多股多日期财务数据只返回 `{'is_copy': None}`**（Issue #115，@jerry87n 精确定位）：QMT 的 pandas 0.22 下，`get_financial_data` 多股 **且** 多日期时返回的是 Panel。Panel 既没有 `.columns` 也没有 `.index`，一路穿过序列化层的 DataFrame 分支和 Series 分支，落到 `__dict__` 兜底——而 `vars(panel)` 是 `{'_data': ..., 'is_copy': None}`，下划线过滤后正好剩那一个。

  不报错，没数据。单股或单日期返回 DataFrame，走得通，所以藏得久。

  pandas 1.0 已删除 Panel，客户端重建不出来，因此现在返回客户端能用的形态：`{股票代码: DataFrame}`，轴标签一并带回。

### 文档

- **README 新增「与 MiniQMT 的兼容性对照」**（Issue #113 的原始诉求）：返回值对照表、int/str 双形态订单号、以及会咬人的行为差异（`types=` 默认值、`account_type` 不会从客户端传到服务端、xtconstant 与 passorder 两套编号）。

- **更正 `qmt_launcher` 一段错误描述**：README 原先称 `login` 模式用 `win32api.SendMessage`、锁屏下也能工作。代码从 #45 起就相反——用 `keybd_event` 物理输入，要求对话框在前台，`session_is_locked()` 为真时直接抛异常拒绝。要**无人值守定时重启**请用 `linkmini` / `bat` / `exe`，只有 `login` 受此限制（Issue #116）。

### 已知限制

- **撤单返回值未实盘验证**：需要一笔真实可撤委托，本机没有。类型层面单测覆盖完整。
- **Panel 修复未实盘验证**：改动跑在 QMT 内，需部署 + 重启策略后用一次多股多日期真实调用确认。
- **信用委托仍未实盘验证**（0.3.1 起）：@fengzhizialex 已确认信用账户的**资产与持仓**正常（Issue #92），但 `order_type=27/28/40` 到达券商时是否为对应品种仍待验证。
- 其余同 0.3.1。

### 部署提醒

Panel 修复在**服务端**（`redis_rpc.py` 跑在 QMT 里），只 `pip install --upgrade` 不生效——要把包拷进 QMT 的 python 目录并**重启策略**：

```python
xt_trader.sync_deployment()   # 自动拷，不碰 config 文件
```

---

## [0.3.1] - 2026-08-30

### 修复

- **信用委托类型被塌缩成普通买卖**（Issue #103）：`order_stock(acc, code, 27, ...)` 返回「不支持该类型」。有两层，第二层更危险：

  - RPC 只认 `23` / `24`，其余一律 `action or order_type is required`
  - 即使放行也没用——`submit()` 按 action 映射 opType，`BUY` 恒等于 `23`，**融资买入会被当成普通买入下出去**。一笔真实的、但下错品种的委托，比直接报错危险得多。

  数值本身是陷阱。MiniQMT 的 `order_type`（`xtconstant`）与 `passorder` 的 `opType` 是**两套编号**：

  | 含义 | xtconstant | passorder opType（API 参考 10.1） |
  |---|---|---|
  | 融资买入 … 直接还款 | 27–32 | 27–32（相同） |
  | 担保品买入 / 卖出 | — | 33 / 34 |
  | **专项两融** | **40–45** | **70–75** |

  所以 `40` 原样转发会到达 `passorder` 的**期货组合开多**。翻译不是可选项。

  所有数值**按常量名取自 `xtconstant`**，不写字面量——PR #88 正是把它们写成字面量、测试又编码了同一个错误前提，因而全绿而映射是错的。另有一条测试：`xtconstant` 若新增未覆盖的 `CREDIT_` 常量即变红，新类型是可见缺口而非静默透传。

  `直接还款`（32 / 45）移动现金而非证券，**没有买卖方向**——不猜，报错要求显式传 `action`。

- **长代码列表失败时全丢**（Issue #104）：一次 RPC 只带一个超时，装不下的列表不是降级而是全部丢失。实测 1000 个 0.42s、10000 个 2.87s、26744 个超时；报告人在 1000 附近就撞上——超时的落点取决于机器。

  现在长列表**失败或返回不全**时，改用市场令牌重读并筛选。重读**先窄后宽**：先按 `stock`（或调用方给的 `types=`）读（1.08s），不够才退到 `all`（7.4s）。

  做成兜底而非阈值切换：1000 个代码直接请求 0.42s，整市场 7.4s，无条件切换会把本来正常的请求拖慢 17 倍。短列表失败仍然抛出（小列表失败是桥坏了，不是尺寸问题）、短列表返回不全不重读（停牌/退市/未订阅本来就会缺）、无可识别后缀的代码不重读。

- **版本标记停在 0.2.16**：`pyproject` 与 CHANGELOG 已是 0.3.0，`version.py` 未跟上，于是 0.3.0 的部署把自己**报告成 0.2.16**——#103 的报告人正是照着这个数字确认版本的。`tests/test_version_stamp.py` 本就为此而写。

### 已知限制

- **信用委托未实盘验证**：会下真实的融资融券委托，本机无信用账户。@fengzhizialex 已表示可代为验证。
- **长列表兜底未实盘验证**：触发条件（超时、返回不全）均为单测模拟。
- **PyPI 曾落后于 GitHub**：0.2.10 到 0.2.15 只发了 GitHub Release、未上传 PyPI，那段时间 `pip install --upgrade` 取到的一直是 0.2.9。0.3.1 已上传。
- 其余同 0.3.0。

---

## [0.3.0] - 2026-08-29

### 新增

- **纯 ZMQ 编辑器入口 `BIGQMT_ZMQ_DRYRUN.py`**（PR #108，@amigobot）：无 redis 部署（券商白名单沙箱）专用入口，强制 ZMQ + 后台线程，自动关闭 redis 依赖功能（download_jobs/exec_events/full_tick_cache）；bootstrap 失败写入 `logs/bigqmt-bootstrap-error.log`。能力边界（无执行回报推送）已在 README 注明。
- **精简 QMT Python 兼容 fallback**：部分券商 python36.zip 裁掉 `importlib` / `logging`——REDIS_DRYRUN 注册最小 importlib 替代模块，logging_setup 降级为手写文件+stdout logger。
- **`OrderSnapshot.price_type`**：委托快照透出报价类型（m_nOrderPriceType），并补 `traded_price`；shim 新增 `xtdata.get_stock_type` 转发。

### 修复

- **纯 ZMQ 模式隐式连 Redis**（PR #108）：`publish_event`/`save_quote_subscription` 在无 redis discovery 的纯 ZMQ 下直接跳过，不再隐式建 redis 连接。
- **日线缓存日期窗口全滤光**（PR #108）：缓存为 8 位日期轴而调用方传 14 位 start_time 时字符串比较清空全部数据，现在按缓存轴精度对齐下限。
- **timeout_seconds 被吞**（PR #108）：`get_market_data_ex` 批处理与复权自愈重试路径上超时参数被丢弃，现全程透传。
- **交易日 ContextInfo fallback 用错首参**（PR #108）：SH/SZ 市场码被当证券代码传入，改为映射代表指数（000001.SH/399001.SZ）。

## [0.2.16] - 2026-08-29

纯新增，无破坏性变更。

### 新增

- **部署版本检测**：部署到 QMT 是文件拷贝，而 QMT 跨策略重跑保留 `sys.modules`——所以「忘了拷」和「拷了但没被加载」**从外部看完全一样**，此前只能靠比对文件字节和找行为变化来判断。

  启动日志现在会说明实际加载的是哪个构建：

  ```
  [bigqmt_shell] bigqmt_signal_trader 0.2.16 loaded from D:\...\python\bigqmt_signal_trader
  ```

  同一信息开放为 RPC：

  ```python
  xtdata.get_deployment_info()
  # {'version': '0.2.16', 'package_dir': ..., 'qmt_python_dir': ...,
  #  'strategy_dir': ..., 'python_version': '3.6.8'}
  ```

  `ping` 响应也带上了 `version`，客户端连接时若与自身版本不一致会**告警一次**，并说明拷贝之后仍需重启策略。

- **部署同步 `sync_deployment()`**：把客户端的包推到 QMT 的 python 目录，目标目录取自 `get_deployment_info()`，**不必硬编码路径**。

  ```python
  xt_trader.sync_deployment(dry_run=True)   # 先看会动哪些文件
  xt_trader.sync_deployment()               # 真同步
  ```

  设 `BIGQMT_AUTO_SYNC=1` 后，连接时检测到版本不一致会自动同步。**默认关闭**——往实盘终端写文件不该是「连接」的副作用，源码树里若有半成品会直接进实盘。

  | 行为 | 说明 |
  |---|---|
  | **绝不写入配置文件** | `bigqmt_signal_trader_local_config.py` / `bigqmt_signal_trader_client_config.py` 存账号与凭据；对应 `.example.py` 属文档，会更新 |
  | **不新增顶层文件** | 只刷新部署已有的模块，加上策略入口（全新部署需要） |
  | **覆盖前备份** | 留 `.bak_<时间戳>` |
  | **原子写入** | 先写临时文件再替换，中断不会留下半个模块 |

  **同步逻辑跑在客户端，不在 QMT 内。** 让交易进程盘中改写自己的代码，等于把源码树里的任何东西——包括改到一半的——直接送上实盘。每次结果都带 `restart_required`：拷贝本身不生效，必须重启策略。

### 修复

- **`__version__` 卡在 `0.2.0` 已十五个版本**，因而无法回答上述任何问题。现跟随 `pyproject.toml`，由测试钉住，并要求 `CHANGELOG` 中存在对应条目。

- **版本标记移出 `__init__.py`**：QMT 沙箱的加载器**从不执行根包**——它建一个空模块直接返回，因为根包的 eager exports 会撞 QMT 的导入白名单：

  ```python
  # QMT native allowlist rejects the root package eager exports.
  if name == "bigqmt_signal_trader":
      return module
  ```

  所以 `__init__.py` 里定义的东西**在 QMT 里不存在**——在所有测试环境都正常，唯独在它唯一需要生效的地方是隐形的。第一版正是放在那里，实盘返回 `AttributeError: module 'bigqmt_signal_trader' has no attribute 'deployment_report'`。现位于 `version.py` 子模块，测试钉住放置位置与导入写法。

### 实盘验证

部署 + 重启后：启动版本行出现；`get_deployment_info` 返回的版本与本地包一致；同步真跑一次——更新 3 个文件、跳过 51 个相同文件、**两个配置文件字节未变**、二次运行报告无操作。

### 已知限制

- **同步之后仍需手动重启策略**，无法从外部触发（`qmt_launcher` 的 `restart` 路径从未在真实环境执行过）。
- 其余同 0.2.15：本终端无期货行情权限、推送通道不可达的部署未验证、PR #88（信用委托类型）未合入、`can_close_vol` 哨兵值（#84）等。

---

## [0.2.15] - 2026-08-29

### 破坏性变更

两项，升级前请确认是否影响你的代码。

- **全市场快照默认只取股票**（Issue #104）：`get_full_tick(["SH"])` 此前返回交易所挂牌的**全部标的**，现在只返回股票。

  实测上交所 `"SH"` 共 **26744** 个标的，按名称核对后的构成是——**债券 82%**（`24浙江22`、`23山东57`、`深圳2536` 这类地方政府债，每个代码段近 1000 只）、股票 **8.7%**（2315 只）、基金/ETF 4.9%。深交所同理。

  依赖市场令牌取债券/ETF 的代码需显式传 `types=["all"]`。收窄时打印一次提示，避免只是静默变少：

  ```
  [bigqmt_market] SH narrowed to 2315 stock; pass types=['all'] for every
  instrument the exchange lists
  ```

- **`subscribe_quote` 成为真订阅**（Issue #95）：此前它把回调**调用一次**就结束——一次性取数顶着订阅的名字。这比没实现更容易误导：数据到了，然后永远等不到第二次。

  tick 周期改走已有的全推行情通道（单代码即一元 code_list，不新开端口），K 线周期改为轮询（服务端无 bar 推送机制）。**tick 订阅因此依赖推送通道，而原一次性路径不依赖**——推送通道不可达的部署会从"至少拿到一次快照"变成完全沉默。该场景未验证。

### 性能

- **全市场快照快 6.9 倍**（Issue #104）：7.44s → **1.08s**（SH），两市 5216 只 1.66s。

  瓶颈不在桥：服务端 handler 占总耗时 96%，RPC 编码仅 0.17s（14.1MB），传输+解码约 0.3s。QMT 单价严格线性、约 **0.29ms/只**（1000 只 0.42s、5000 只 1.66s、10000 只 2.87s），所以 7.4s 完全由**标的数量**解释。

  因此**在请求时收窄，而不是拿回来再过滤**——事后过滤仍要付 QMT 对每个多余标的的成本。市场令牌先解析为板块清单（FormulaServer 直连，实测 13ms，按运行缓存），只请求那些代码。

  | `types` | 板块 | 约数 |
  |---|---|---|
  | `stock`（默认） | 上证A股 / 深证A股 / 京市A股 | 2315 / 2901 / 339 |
  | `etf` / `fund` / `index` / `convertible` | 沪深ETF / 沪深基金 / 沪深指数 / 沪深转债 | 1696 / 2249 / 609 / 320 |
  | `all` | 不收窄 | 26744（SH） |

  **收窄失败一律退回全量**：类型不认识、板块查不到、板块查询抛异常、该市场无对应板块（如 HK）——都保留原令牌。丢行情比慢更糟。板块名取自实盘终端而非猜测：北交所是 `京市A股`，`北证A股` 返回 0。

### 修复

- **期货合约符号被大写化，共三处**（Issue #95）：各交易所命名规范不可互换（上期所 `rb2401`、郑商所 `AP401`），大写后是 QMT 不认识的代码——**返回空行情、不报错**，与"没有数据"无法区分。

  `#68` 当初靠**绕开** `normalize_stock_code` 解决了持仓路径，其余路径（下单、行情、全推缓存、风控）仍从这里过。三处依次是 `code_utils.normalize_stock_code`、`full_tick_cache.normalize_full_tick_codes`（在 `normalize_stock_code` **之前**又大写一次）、`market_bigqmt.normalize_market_or_stock_code`（同样在委托前大写，**使前两处的修复到不了 `get_full_tick`**——恰是本 issue 报告的路径）。

  同时泛化了返回键的映射：现在按调用方写法发送，映射改为按大写形式索引，**QMT 回显或自行规范化两种行为下都能还原**，#58 不会以任何方式复发。

- **期货交易所令牌不再抛异常**（Issue #95）：`IF` / `SF` / `DF` / `ZF` / `INE` / `GF` 此前在 `normalize_stock_code` 里以 `invalid stock code` 失败，整个交易所的期货快照无法获取。现在送达 QMT 由其回答，且**从不按股票收窄**——期货交易所只挂期货，令牌自带品种信息，无需 `types`。

- **无数据周期的订阅不再静默**（Issue #95）：本终端 `1m` / `5m` 返回空 DataFrame 而 `1d` / `tick` 有数据，订阅这类周期会得到一个**活着、正确、且永远沉默**的订阅——与坏掉的无法区分。现在说明一次（不刷屏），有数据后自动恢复安静。

### 未修复（附理由）

- **`get_market_data_ex` 的 `field_list=[]` 不走 FormulaServer 直连**（Issue #104）：报告人的观察准确（直连 0.03s vs RPC 0.97s，约 30 倍），但推论会损坏数据。空 `field_list` 意味着"全部字段"，返回 11 列，而直连只供 6 列、其余 4 列返回 `NaN`——RPC 有真实值（三只票实测 `preClose` 9.07 / 7.82 / 11.59，直连全部 `nan`）。默认路由到直连等于用 30 倍加速换真实价格静默变成 `NaN`。

  **要那 30 倍，显式写出 6 个 OHLCV 字段即可**；首次不传 `field_list` 时会在 `bigqmt.log` 记一条说明。

### 已知限制

- **本终端无期货行情权限**：合约详情 0 字段、快照 0 键、日线 0 行，九个合约四个交易所全空。因此期货令牌在有权限环境上的实际返回、以及 QMT 对小写合约回什么大小写，**均未验证**；代码对两种情况都做了处理。
- **推送通道不可达的部署未验证**：`subscribe_quote` 的 tick 路径现在依赖该通道。
- `can_close_vol` 哨兵值（#84）、PR #82 的 `traded_price` 无实盘证据、真实打新未验证、单文件构建需源码检出、`EmptyPositionProvider` 缺 `get_position_statistics`、#77 / #78 —— 同 0.2.14。
- **PR #88（信用委托类型）未合入**：其映射把 `33` / `34` 认成专项融资/融券，实为期权操作（专项信用是 `40` / `41`），另缺 9 个类型含最基本的 `28 CREDIT_SLO_SELL`。已请求修改。

---

## [0.2.14] - 2026-08-28

### 新增

- **新股申购（打新）接口**（PR #96 @ThomasAnderson01，PR #98 跟进）：`query_ipo_data` / `ipo_subscribe` / `ipo_subscribe_all` / `query_new_purchase_limit`。

  ```python
  for row in xt_trader.ipo_subscribe_all(acc, dry_run=True):   # 先看计划，不下单
      print(row)
  results = xt_trader.ipo_subscribe_all(acc)                    # 真申购
  ```

  **这是主动调用的方法，不是桥自动执行的行为。** 提交版本在 `adjust` 定时回调里无条件运行——整个 diff 没有任何开关，任何人升级后第二天 09:40 就会自动下真实委托，而且直接调 `passorder`，**绕过 `rpc_allow_order_methods=False`**：明确关掉远程下单的用户照样会被下单。改为显式接口后走既有 `order_stock` 通道，因而与其他委托一样受该开关管控。

  走既有通道还顺带修正了 `quickTrade`：被删的手写路径传 `1`，而 API 参考 1.4 明确要求**定时器/回调中下单必须传 2**（`1` 的语义是 `is_last_bar()` 为真才产生信号，在定时器回调里可能不成立，**委托会静默不发出**）。网关默认值本就是 `orderType 1101` / `prType 11` / `quickTrade 2`。

  **默认只打沪深**（市值申购、不冻结资金），北交所需冻结资金故排除，可用 `markets=("SH","SZ","BJ")` 显式打开。**申购代码认不出来一律跳过**——原实现结尾是 `return True`（"无法识别默认放行"），在一个专门排除北交所的过滤器上倾向于下单。

### 修复

- **`get_ipo_data` 的响应被清空**（实盘发现）：它返回**以申购代码为键的 dict**，却被送进 `_normalize_detail_rows`。那个函数对 dict 做 `for row in rows`——迭代的是**键**，再拿每个代码字符串去抓属性：

  ```
  QMT 返回:  {'301689.SZ': {'issuePrice': 16.0, 'maxPurchaseNum': 12000, ...}}
  归一化后:  [{}]              <- 申购代码、发行价、数量，全没了
  ```

  PR #96 修对了 `type` 参数（原将 `account_id` 传给了期望 `type` 的位置），但数据死在下一层，**所以这个 RPC 从未返回过可用数据**。

  实盘还证明了数据确实存在而非"今天没有新股"：该函数对空输入 `return []`，而服务端 `type="STOCK"` 返回 `[{}]`、`"BOND"` 返回 `[]`——非空 dict 被清空。修复后同一调用返回 `301689.SZ @ 16.0 × 12000`，与 #96 提交者当日上午实盘申购的完全一致。

  `get_new_purchase_limit` 文档（6.10）同样写明返回 dict，同样的问题，一并修。两者现走 `_call_qmt_mapping`：保留映射形状，只把值转成 JSON 安全。

- **客户端不再静默吞掉错误形状的响应**：跟进过程中一度用 `isinstance(data, dict) else {}` 归一化空值，那会把 `[{}]` 变成 `{}` = 「今天没有新股」——而当天恰好有。现在非空却形状不对会明确告警说服务端太旧。

### 文档

- README 新增「新股申购（打新）」一节，并**明确写明该接口不会自动执行**——否则读者看到"打新"容易以为装上就会自己跑。文中每个方法名、关键字参数、申购代码前缀均已对照实现核实。

### 已知限制

- **真实申购未验证**：只读与 `dry_run` 路径已在大 QMT 实盘验证（2026-08-28，`301689.SZ @ 16.0 × 12000` 计划正确、未下单），但 `dry_run=False` 会下真实委托，本仓库未执行。@ThomasAnderson01 曾用原实现于当日成功申购该股。
- **`query_new_purchase_limit` 实盘返回空 dict**：本账户无申购额度，属正常；形状已修正为 dict（此前为 list）。
- **期货合约符号大小写**（Issue #95）修复见 PR #97，**本版未合入**，待报告人确认其终端的期货数据情况。
- **信用委托类型仍会被塌缩成普通买卖**：PR #88 的映射把 `33`/`34` 认成专项融资/融券（实为期权操作，专项信用是 `40`/`41`），另缺 9 个类型含最基本的 `28 CREDIT_SLO_SELL`。已请求修改，未合入。
- **`subscribe_quote` 不是真订阅**：回调只触发一次，之后无推送。需要实时推送请用 `subscribe_whole_quote`。
- `can_close_vol` 哨兵值（#84）、单文件构建需源码检出、`EmptyPositionProvider` 缺 `get_position_statistics`、#77/#78 —— 同 0.2.13。

---

## [0.2.13] - 2026-08-27

### 修复

- **`account_type` 三个配置位置里两个静默失效**（Issue #92）：信用账户按 STOCK 查询**不会报错**——`get_trade_detail_data` 返回一行全 0 的资产。所以这个设置错了，表现就是「信用账户资产全是 0」，日志里没有任何线索。

  三个看着都合理的位置，此前只有一个生效：

  | 位置 | 修复前 |
  |---|---|
  | local config 里的 `BIGQMT_ACCOUNT_TYPE` | 生效 |
  | `BIGQMT_REDIS_CONFIG["account_type"]` | **无人读取** |
  | 改 `redis_rpc_runtime.py` 里的 `ACCOUNT_TYPE` | **被静默覆盖** |

  第三条尤其阴：解析式是 `BIGQMT_ACCOUNT_TYPE or ACCOUNT_TYPE or "STOCK"`，而随包发的 example 配置里写着 `BIGQMT_ACCOUNT_TYPE = "STOCK"`——它是真值，永远赢，所以改文件里那个常量等于白改。报告人用的正是后两条。

  后两个位置现在都认（按上表优先级），并且**解析结果在启动时打印、说明来源**，冲突会指名：

  ```
  [bigqmt_shell] account_type=CREDIT (from BIGQMT_REDIS_CONFIG['account_type'])
  [bigqmt_shell] ignored conflicting account_type from: BIGQMT_ACCOUNT_TYPE
  ```

  模块常量出厂即 `"STOCK"`，因此只有被改动过才算用户的选择——否则每个信用部署都会报一条与它的假冲突。

  **需要说清楚哪部分本来就没坏**：`account_type` 一旦解析出来，确实能正确到达 `get_trade_detail_data(account, 'CREDIT', 'ACCOUNT')`。新增 10 个测试中有 2 个覆盖该链路，它们在修复前的代码上也通过；另外 8 个会红。

### 已验证

- **PR #82 的 `traded_price` 拿到实盘证据**（0.2.11、0.2.12 两版的已知限制，现已解除）：实盘 18 笔委托、14 笔成交，逐笔核对——

  ```
  traded_price 与 price 不同的:  9 笔    (最大差 10.56)
  两者相同的:                    5 笔    (限价单按报价成交)
  已成交但 traded_price 为 0 的:  0 笔    (修复前应为全部 14 笔)
  ```

  证明 `traded_price` 是真实成交均价，而非 `price` 的副本。

### 已知限制

- **信用委托类型仍会被塌缩成普通买卖**：PR #88 试图修此问题，但其映射把 `33` / `34` 认成了专项融资买入/专项融券卖出——那两个实际是 `OPT_OPTION_SELL_CLOSE` / `OPT_OPTION_SELL_OPEN`（期权操作），专项信用是 `40` / `41`；另缺 9 个信用类型，含最基本的 `28 CREDIT_SLO_SELL`。已请求修改，本版未合入。
- **#92 的信用账户表现未实盘确认**：本机为股票账户，**无信用账户可验证「资产不再全 0」**。本版修的是配置发现与可见性，该部分完全由测试钉住，并已部署 QMT 重启验证（日志首次打印 `account_type=STOCK (from default)`，与股票账户未设置的预期一致，回归 6/6 PASS）。请 @jerry87n 在信用账户上复测。
- **`can_close_vol` 在股票账户上返回 LLONG_MAX 哨兵值**（Issue #84）。
- **单文件构建需要源码检出**：`tools/` 不随 wheel/sdist 分发；目标沙箱环境未在本机复现（本机 QMT 不拒绝 `import redis`），真实加载由 @heimo88 实测。
- **PR #81 的接口尚未补全**：`EmptyPositionProvider` 与 `PositionProvider` 协议均缺 `get_position_statistics`。
- **#77**（同终端双账户）属当前设计。**#78** 待报告人补充环境信息。

---

## [0.2.12] - 2026-08-27

### 新增

- **`bigqmt-init` 配置向导**：部署此前意味着抄两份 `.example.py`、搞清楚三十来个键里哪些真的要改、还要手工保证服务端和客户端两边一致。向导只问会变的那几项——账号、账号类型、传输方式、地址端口、Redis 凭据、是否允许远程下单、部署方式——然后**从同一组答案**生成两份配置，所以它们不可能在连接参数上对不上。选单文件部署时顺带跑对应生成器并把配置烘焙进产物，替换掉占位符。

  三项不问、直接定死：`rpc_background_threads` 恒为 `False`（`get_trade_detail_data` 离开主策略线程返回空，这不是可选项）；`rpc_allow_order_methods` 默认关，打开前明确说明含义；选无 redis 单文件会强制 `transport=zmq`，不会留下一份在无法 import redis 的文件里声称用 redis 的配置。

  密码分两类：Redis 密码是服务凭据，写进配置文件（`.example.py` 本来就这么记的），输入不回显；**QMT 登录密码完全不落盘**——`qmt_launcher` 从 `BIGQMT_LOGIN_PASSWORD` 读，这样它不会出现在 `argv` 或磁盘文件里，向导沿用该约定并在结束时说明。

- **单文件 QMT 构建生成器**（Issue #56，感谢 @heimo88）：部分券商的 QMT 是白名单 + 不能加载文件、不能 import 外部模块，只有把所有代码放进一个策略文件才能跑。`tools/build_single_file.py`（base64 内嵌）和 `tools/build_no_redis_single_file_flat.py`（明文真实代码，强制 zmq）把整个包打成一个自包含文件，运行时用自定义 import 钩子从内存解析。脚本由报告人在其券商环境实测通过。

  合入时换掉了模板里夹带的提交者个人实盘配置（真实账号、`rpc_allow_order_methods=True`、`rpc_background_threads=True`、`full_tick_cache_enabled=True`），并补上两个模板都缺的 `BIGQMT_ACCOUNT_TYPE`（#68 加的）。产物已 `.gitignore`，用时重新生成。

  flat 版把每个模块缩进进 `def _mod_N():` 再 exec——**正是让 `from X import *` 报 `SyntaxError: import * only allowed at module level` 的那个形状**。所以这个构建既依赖 0.2.11 对 #76 的修复，现在也成了它的回归守卫：整个测试套件里没有别的地方会把模块编译进函数体。

### 修复

- **负债合约查询少传一个参数**（PR #87，@ljjtim）：`get_unclosed_compacts` / `get_closed_compacts` 只传了 `accountID`，而 `docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md` 6.16 记载的签名是两参数、`accountType` 填 `'CREDIT'`。旁边三个单参数接口（`get_debt_contract` / `get_assure_contract` / `get_enable_short_contract`）未受影响，与文档一致。

- **`account_id is required` 说不清问题在哪**（Issue #90）：原来整条消息就一句 `Big QMT account_id is required`，**不说自己找过哪些模块**——所以「配置文件建了但放在当前解释器 import 不到的位置」和「压根没建配置文件」产生的报错一模一样。报告人其实已经建了那个文件。

  现在区分两种成因（没有可导入的模块 / 模块导入了但没定义 `BIGQMT_ACCOUNT_ID`——后者去查 `sys.path` 是南辕北辙），列出三条已逐一实测的解法，并点出时序陷阱：`configure()` 在模块导入时就跑了一次，之后才放好的配置不会自动生效。构造这条消息本身不会抛异常——它跑在错误路径上。

### 文档

- **QMT Python 组件前置说明**（Issue #85）：全新安装的终端 `bin.x64\` 下没有 `Lib\` 目录，也没有 `python.exe`——那是 Python 组件带来的，不是终端自带的，不要手动创建。此前文档直接假设这些路径存在，还让往 `bin.x64\Lib\site-packages` 里拷包。
- README 新增「配置向导」「单文件构建」两节；修正过期的常量计数（91 → 539，#73 之后）。

### 已知限制

- **信用委托类型仍会被塌缩成普通买卖**：PR #88 试图修这个，但其操作类型映射把 `33` / `34` 认成了专项融资买入/专项融券卖出——那两个实际是 `OPT_OPTION_SELL_CLOSE` / `OPT_OPTION_SELL_OPEN`（期权操作），专项信用是 `40` / `41`；另缺 9 个信用类型，含最基本的 `28 CREDIT_SLO_SELL`。已请求修改，本版未合入。
- **`can_close_vol` 在股票账户上返回 LLONG_MAX 哨兵值**（Issue #84），沿自 0.2.11 的 #81。
- **PR #82 的 `traded_price` 仍无实盘证据**（验证当日 0 笔委托），契约由单测钉住。
- **单文件构建未在目标沙箱环境复现**：本机 QMT 不拒绝 `import redis`。生成、编译、配置正确性由测试钉住；受限券商环境里的真实加载由 @heimo88 实测（#76 已据此关闭）。
- **PR #81 的接口尚未补全**：`EmptyPositionProvider` 与 `PositionProvider` 协议均缺 `get_position_statistics`。
- **#77**（同终端双账户）属当前设计——每账户状态存放在模块级全局，两个实例共享 `sys.modules` 即互相覆盖。**#78** 待报告人补充环境信息。

---

## [0.2.11] - 2026-08-27

### 新增

- **`query_position_statistics` 持仓统计**（PR #81，@ReCodeLife）：对齐 MiniQMT 同名接口，服务端经 `get_trade_detail_data(..., "POSITION_STATISTICS")` 提供，43 个字段同时给出 snake_case 与 `m_` 两套名字。正确加入主线程方法名单——`get_trade_detail_data` 离开主线程返回空。**实盘验证推翻了 PR 描述的一个前提**：该接口在**股票账户**上也返回数据，不限期货。6 个持仓逐行核对：`position` 与 `query_stock_positions` 的持仓量 **6/6 完全一致**，8 组 `m_` 别名与 snake_case 全部吻合，17/43 字段有值（其余 26 个是期货专属的保证金/权利金字段，股票账户为空属正常）。
- **委托回调带成交均价**（PR #82，@yuchiwang）：`traded_price`（成交均价）本就是原生 `XtOrder` 字段，但桥从未填充，导致 `on_stock_order` 在已成时拿不到成交价。服务端查询路径、回调 normalize、客户端 `_order_from_dict` 四层补齐并各带测试。提交者已实盘验证。

### 修复

- **单文件 QMT 沙箱构建无法加载：`from xtquant.xtconstant import *` 是语法错误**（Issue #76）：单文件构建把每个模块塞进函数体 exec，而 `import *` 只允许在模块级，报 `SyntaxError: import * only allowed at module level`。这是 0.2.10 里 #73 引入的——它删掉 `xtquant_compat` 中 110 个硬编码常量、改用 `import *` 兜住。

  **只导入实际用到的 3 个名字会修好语法、同时弄坏别的东西**：`import *` 拉进 534 个名字，模块自身只用 `ORDER_UNKNOWN` / `STOCK_BUY` / `STOCK_SELL`，但 `docs/XTQUANT_COMPAT_REPLACEMENT.md` 记载的「接入方式一」是 `from bigqmt_signal_trader import xtquant_compat as xtconstant`，即调用方从本模块读常量。改为显式循环回填，**539/539 全部保留**并逐个与来源比对。没有使用模块级 `__getattr__`：PEP 562 是 Python 3.7+，而 QMT 自带 3.6（`bin.x64/python36.dll`）；4 个混合大小写常量（含原生 SDK 拼写的 `OFFSET_FLAG_ClOSEYESTERDAY`）也排除了按 `.isupper()` 过滤的写法。

- **回调线程上首次导入模块失败，回调推送全丢**（Issue #76）：`exec_events` 之前是在 order/deal 回调**内部**导入的。QMT 的这些回调跑在经 `PyGILState_Ensure` 进入的 C++ 线程上，在该线程上首次 exec 一个尚未导入的模块会在 C 层失败**且不设置 Python 异常**，表现为 `SystemError: error return without exception set`。普通包部署不触发（init 期的 reload 已把它预热进 `sys.modules`，惰性导入直接命中缓存）；单文件沙箱构建里那次 `import_module` 会失败并被 `except` 吞掉，于是真的在回调线程上首载。改为模块加载期导入，走已在服务适配器模块的同一个本地 loader。

  同时修掉**让这个 bug 藏了一天的原因**：handler 只记 `str(exc)`，日志读出来就是 `error return without exception set` 然后没了。现在带异常类与完整堆栈。

- **adjust 主线程上一个未受控的调试 `print`**（#81 跟进，4c7d1cb）：PR #81 夹带了一段与其功能无关的调试输出。该文件其余 `print` 均受 `debug_log_limit`（默认 0）控制，这一处没有，因而每个响应都执行；且对**完整** payload 做 `json.dumps` 后才截断到 2000 字符。实测一个典型 `get_market_data_ex` 响应（100 支 × 240 根）序列化 2.6MB 耗时约 **30ms**、丢弃 99.92%——而它运行在 adjust 主线程上，实测 `tick_app` 11500 次调用的最大值才 29–42ms、p99.98 在 5ms 以内。

### 已知限制

- **`can_close_vol` 在股票账户上返回 LLONG_MAX 哨兵值**（Issue #84）：实盘 6 个持仓全部返回 `2^63-1`，即 QMT 对股票账户「未设置」的哨兵，被原样透传成一个真实数字；而本仓库 API 参考把 `m_nCanCloseVol` 记为 int「可平」。映射代码本身忠实转换了 QMT 给的值，问题在于哨兵未被识别。**这只有实盘数据能发现**，单测与代码审查都看不出来。
- **PR #82 的 `traded_price` 尚无实盘证据**：验证当日 0 笔委托，该字段需**有成交的委托**才能证明。契约与四层往返由单测钉住。
- **Issue #76 两项未完成验证**：报告人的单文件构建脚本依赖两个从未附带的模块（同 #56），**该构建无法在此复现**——`import *` 一项是通过「将模块源码放入函数体编译」钉住的，已确认该检查在修复前的代码上复现了报告人所报的 SyntaxError，但这不等同于跑过其真实构建；真实回调投递需实际下单才触发。已请报告人复测。
- **PR #81 的接口尚未补全**：`EmptyPositionProvider` 与 `PositionProvider` 协议均缺 `get_position_statistics`（实测 `AttributeError`，会降级为 RPC 错误响应，不会中断线程）。
- **#77**（同终端双账户）：现为一策略实例对应一账户——`_account_id` / `_rpc_service` / `_quote_subscription_service` 等每账户状态存放在模块级全局，RPC 通道亦按账户模板化，两个实例共享 `sys.modules` 即互相覆盖。属当前设计，非缺陷。**#78** 待报告人补充环境信息。

---

## [0.2.10] - 2026-08-26

### 修复

- **zmq 部署收不到任何回调推送**（Issue #76）：order/trade 事件**两端都硬绑 Redis**——服务端 `_publish_exec_event` 建不出 Redis 客户端就直接 `return`，客户端 `_event_loop` 只订阅 Redis 频道。纯 zmq 部署因此完全收不到 `on_stock_order` / `on_stock_trade` / `on_order_error`，而且是**静默的**：客户端只是连不上然后无限重试，服务端把「没有 Redis」当正常跳过。现在 exec 事件复用已有的全推行情 PUB 通道（不新开端口），Redis 仍优先（其频道带 stream 可做短重放）。报告人读代码就把这个推了出来。
- **adjust 每个 tick 都在新建 Redis 客户端**（PR #79）：`_pump_download_jobs` 每次运行都建一个新客户端，而它每个 tick 都跑——按 100ms 间隔就是**每秒 10 个**，每个带一套连接池。症状是 QMT 面板里的 `AttributeError: 'Redis' object has no attribute 'connection'`（redis-py 的 `__del__` 跑在构造未完成的对象上），一天 31 次。Python 把它吞成 `Exception ignored in`，所以**从没进过 `bigqmt.log`**。`_exec_event_redis` 早已为同样理由加过缓存，此处被漏掉；修复是复用同一个缓存 helper。
- **一行无法解析的数据搞垮整个查询**（PR #70）：#73 让 `_full_code` 遇到柜台式交易所 ID 时抛异常——信号本身对，但三个调用方的行循环都无逐行保护，异常一路抛出 `get_positions` / `query_orders` / `query_trades`。一行异常 = 整个持仓查不到；`query_orders` 外层 `except` 返回 `[]`，丢的是全部委托。现在跳过该行、其余照常返回。
- **xtquant / xtquant_compat 循环导入**（PR #74）：#73 反转常量依赖后，`xtquant/__init__` 急切加载的 `xtdata`/`xttrader` 又反向引用 `xtquant_compat`，环闭合——`import bigqmt_signal_trader` 直接失败、**27 个测试模块无法收集**。且**依赖导入顺序**（先 import xtquant 能过），这类 bug 平时测不出来。两个 shim 改为通过模块级 `__getattr__`（PEP 562）惰性解析；调用方三种写法（属性访问 / from-import / 子模块导入）全部逐项验证不变。

### 变更（PR #73，@ReCodeLife）

- **常量定义迁回 shim 侧**：`xtquant/xtconstant.py` 补全为完整实现（90 → 539 个），`xtquant_compat` 改为 `from xtquant.xtconstant import *` 并删去 142 行硬编码。对着 QMT 自带原生 SDK 逐个比对：**原生 90 个常量 0 个值被改动、0 个缺失**，新增 443 个券商扩展枚举。`xttype` 同步扩展。
- **期货持仓代码解析**（PR #68，@ReCodeLife）：裸期货合约（交易所字段为迅投简称 DF/SF/ZF）此前被送进股票归一化并抛错。改为按交易所字段分类、不猜代码形状，并**保留符号原始大小写**（`rb2401.SF` 小写 / `AP401.ZF` 大写，不可互换）。新增 `BIGQMT_ACCOUNT_TYPE` 配置（默认 `STOCK`）。

### 已知限制

- **#79 的实盘验证不充分**：修复后线上 0 次，但**重启前也是 0 次**（问题出现在前一日），所以这个 0 不构成证据。行为由单测钉住（20 次调用 → 1 个客户端，还原即变红），实盘待自然复现。
- **#76 的真实回调投递未验证**：已验证通道连通、格式正确、部署版代码端到端可投递（order/trade/order_error 三类），但真实回调需**实际下单**才触发，收盘后订阅收到 0 个事件属正常。
- **#58 的期货小写代码仍缺实盘样本**；**#56 单文件构建**未进主干（报告人的脚本依赖两个未附带模块）；**#77 / #78** 待报告人补充信息。

---

## [0.2.9] - 2026-08-24

### 修复

- **redis-py 3.5.3 兼容（PR #67 回归，Issue #71）**：`build_redis_client` 无条件传 `protocol=` 在 QMT 自带的老 redis-py 上直接 TypeError——且 QMT 策略重跑后执行事件发布器重建客户端时崩掉，事件被静默全丢（发布器构建失败现在会记日志，不再无声）。改为按版本能力（inspect.signature）条件透传；客户端 `_redis()` 同步处理。
- **async_response 没有真实 order_id**（Issue #72）：委托号异步分配、RPC 应答时通常还没有，order_id 只能回落成 remark，按 order_id 管理委托的代码会解析失败。现在 response 触发前等屏障从暂存的委托事件里学到真实委托号（bounded 2s，学不到才回落 remark）。下单仍走 wait_settlement=False 快速应答（#50/#69 的吞吐不回退）。实盘验证：`async_response order_id=xt1090519419`（真实委托号）。
- **#69 发单间隔**：0.5s 检查已在 #44 改为结算停放（不阻塞提交）——见 issue 回复，无需改动。

## [0.2.8] - 2026-08-25

### 修复

- **期货持仓代码解析错误**（PR #68，@ReCodeLife）：裸期货合约（交易所字段为迅投简称 DF/SF/ZF）被错误送进股票归一化并抛 invalid stock code。改为**按交易所字段分类，不再猜代码形状**：迅投简称（IF/SF/DF/ZF/INE/GF）拼接后缀并**保留符号原始大小写**（`rb2401.SF` 小写 / `AP401.ZF` 大写，两者不可互换）；股票/港股通走归一化；`code_utils` 补 `.HGT` / `.SGT` 后缀识别。
- **一行无法解析的数据会搞垮整个查询**（PR #70）：上一条让 `_full_code` 遇到柜台式交易所 ID 时抛异常——信号本身是对的，但三个调用方的行循环都没有逐行保护，异常会一路抛出 `get_positions` / `query_orders` / `query_trades`。**一行异常 = 整个持仓查不到**；而 `query_orders` 外层 `except` 返回 `[]`，丢的是全部委托。对交易系统而言这比它要报告的问题更危险，也与本模块「降级而非崩溃」的既定风格矛盾（POSITION 查询外的 try/except 注释即为 *degrade to empty*）。现在跳过解析不了的那一行、其余照常返回，跳过按 `(kind, exchange)` 只记一次日志。

### 新增

- **`BIGQMT_ACCOUNT_TYPE` 配置**（PR #68）：账号类型独立可配（默认 `STOCK`，另有 `CREDIT` / `FUTURE` / `OPTION`），`redis_rpc_runtime` 向后兼容读取并归一化为大写字符串后传入 `configure()`。旧配置不写此项时行为不变。

### 已知限制

- **#58 的期货小写代码仍缺实盘样本**：`get_full_tick(['rb2708.SF'])` 返回空，无法据此判断大小写还原是否生效（空结果说明该合约无数据，而非映射失败）。本版持仓侧的大小写保留由单测覆盖。
- **单文件构建（#56）未进主干**：报告人提交的 `build_no_redis_single_file_flat.py` 依赖两个未附带的模块和 `bigqmt_no_redis/` 目录，尚不能独立运行；其惰性加载架构优于此前方案（PR #62 已关闭），待依赖补齐后合入。

---

## [0.2.7] - 2026-08-24

### 新增

- **Redis 5.x 兼容**（PR #67，@sunjian710）：`build_redis_client` 透传 `protocol`（默认 2/RESP2，可用 `protocol: 3` 或 `BIGQMT_REDIS_PROTOCOL` 覆盖）——redis-py 8.x 默认 RESP3 的 HELLO 握手在 Redis 5.0 上直接报错，现在开箱即用。客户端 `BigQmtRpcClient` 的 redis_config 同样透传。
- **持仓/资产对象原生字段别名**（PR #67，@sunjian710）：`query_stock_positions`/`query_stock_asset` 返回对象新增 `m_` 前缀原生字段（`m_strStockCode`/`m_nVolume`/`m_dCash`/`m_dTotalAsset` 等），读原生 xtquant 字段名的客户端代码（如 miniqmt_redis 风格）不再 AttributeError。

### 修复

- PR #67 合入修正：`_position_object` 的 m_ 别名引用了未定义局部变量（`stock_code`/`stock_name` 只内联在 kwargs 里），持仓查询全挂——提取为局部变量并补回归测试钉住全部别名。

## [0.2.6] - 2026-08-24

### 修复

- **回调对象字段命名不一致**（Issue #65）：`on_order_error`/`on_cancel_error`/async 回报的委托号字段是 `order_sys_id`，而 `on_stock_order`/`on_stock_trade` 用 MiniQMT 规范名 `order_sysid`。全部回调对象现在同时携带两个名字（同值），另补 `order_remark`/`status`/`strategy_name`。
- **on_order_error 缺 order_remark/status**（Issue #64）：服务端事件补上 `m_strRemark`→`order_remark`/`user_order_id` 与 `m_nOrderStatus`→`status`，撤单错误事件同步补齐。柜台的拒单理由此前已在 `error_msg`（m_strCancelInfo，#60）。
- **tick 分笔下载后立读为空**（Issue #66）：两个叠加问题——①QMT 下载全局提交即返回、数据异步落地，下载后立即读读到的是落地前的空结果；②FormulaServer 快速路径不服务 tick/L2 周期却静默返回空，把 RPC 桥的正确答案挡在门外。修复：download_history_data2 分批轮询直到批内每个代码出现真实数据行（`data_wait_seconds` 超时容忍停牌/退市）；FormulaServer 对 tick/l2 周期拒绝路由、回落 RPC。实盘验证：tick 下载 0820 → 立读 1434 行全部属当日。
- **拒单回报乱序**（Issue #51 屏障补全）：服务端在应答 RPC 之前就推送废单事件，order_error/cancel_error 此前不在屏障内，会比 async_response 先到（实盘实测倒挂）。屏障现在同时扣住 order/trade/order_error/cancel_error，实盘验证顺序：async_response → 已报(50) → 废单(57) → order_error。
- **call_async 异步 RPC**（Issue #63）：`client.call_async(method, params, callback=None)` 立即返回 Future，不阻塞调用方；有界在途（64）+ 8 线程池；可选 callback 在单 dispatcher 线程按完成顺序串行派发。注意：下单类 RPC 在服务端仍由 QMT 主线程串行处理，客户端异步叠加的是往返延迟，不会并行化报单到交易所的环节。

---

## [0.2.5] - 2026-08-21

### 修复

- **get_full_tick 返回 key 被全大写**（Issue #58）：代码 upper() 后传给 QMT，返回时未映射回调用方写法。期货交易所的合约代码是小写（`rb2708.SF`、`a2609.DF`、`nr2612`），导致 `code in result` 对每一个都失败、实时五档被误判缺失。现在只还原**大小写**，不动归一化——`600000` 仍补全为 `600000.SH`，补全后缀是调用方依赖的行为。QMT 主动返回而未被请求的 key 原样透传，不丢行情。
- **柜台拒单原因没有传出**（Issue #60）：`status_msg` 与 `error_msg` 在废单时均为空，形如 `[COUNTER] 资金可用余额不足，尚需[4789.630]` 的原因完全丢失。两处缺口：`OrderSnapshot` 没有 `status_msg` 字段；`normalize_order_error_event` 只读 `m_strErrorMsg`。柜台文本实际在 `m_strCancelInfo`（官方字段表标注为「废单原因」，状态 57 明确指向它）。已贯通 `OrderSnapshot` → `order_bigqmt` → 委托事件 → `_order_from_dict`，`order_error` 事件改为优先读该字段。

### 变更（PR #59，@cnwuwil）

- **回调/成交对象对齐 MiniQMT 原生契约**：`XtTrade` 补齐 `traded_id` / `traded_time` / `traded_amount` / `strategy_name`，成交金额优先取 `m_dTradeAmount` 真值（实测限价 9.14 成交 9.06 时估算值偏高约 0.9%）；撤单响应补 `cancel_result` / `error_msg`；业务回调异常落日志不再静默吞掉。
- **回调代码补全交易所后缀**：实盘 order/deal 回调的 `m_strInstrumentID` 只带 6 位裸代码，服务端事件构建时从 `m_strExchangeID` 补全，客户端分发层按 A 股代码段推断作兜底。
- **`order_volume` 语义修正**：由剩余量 `m_nVolumeTotal`（全部成交时为 0）改为原始委托量 `m_nVolumeTotalOriginal`，对齐 `XtOrder.order_volume`。**对依赖「剩余量」的调用方是行为变化**。
- **xtdata shim 签名对齐**：`get_instrument_detail` 接受并忽略第二参数 `is_detail`；`download_history_data(2)` 透传 `dividend_type`。
- 新增官方《内置Python》API 精简参考文档，作为字段映射的依据留存。

### 实盘验证（2026-08-21 收盘后，真实账户）

- **柜台拒单原因**（#60）：29 笔委托中 15 笔废单全部带回柜台原文，`m_strCancelInfo` 字段名经实盘证实。消息不止有原因，连参数也在：
  `[COUNTER][251005][证券可用数量不足][v_stock_code=000506,v_occur_amount=100.00,p_enable_amount=0.00,...]`
- **`order_volume` 语义**（#59）：13 笔已成（状态 56）委托的 `volume` 全部为真实委托量，无一为 0。旧代码读剩余量，这 13 笔会全是 0——这是该项修复的决定性证据。委托代码的交易所后缀也已确认带全。

### 已知限制

- **#58 的期货小写代码缺实盘样本**：`get_full_tick(['rb2708.SF'])` 返回空，无法据此判断大小写还原是否生效（空结果说明该合约无数据，而非映射失败）。股票代码路径确认未被破坏，小写映射逻辑由单测覆盖。
- Issue #56（单文件、无 Redis、仅 ZMQ 的 QMT 内嵌版本）未处理。

---

## [0.2.4] - 2026-08-20

### 修复

- **qmt_launcher mode=login 打不进密码**：SendMessage 把键消息发给顶层窗口，但 Qt 对话框只在窗口持前台焦点时才把按键路由给输入框——后台进程发送等于静默丢弃。改为物理输入（keybd_event/mouse_event）：Alt 键解锁前台保护 → 置顶 + 前置 → 物理点击字段 → 打字。账号预填时先 Ctrl+A 再覆盖（否则变成追加）；密码框点击位置避开右侧虚拟键盘图标；控件坐标按窗口尺寸比例定位。已是大窗（主界面=自动登录完成）时跳过整个输入流程，避免密码打进主窗口控件。
- **登录框就绪判定**：58600 端口在登录前就监听，launcher 的端口就绪不等于"已登录可用"——docstring 已注明，建议以 RPC ping 为真正就绪信号。

### 新增

- **normalize_stock_code 支持 QMT 期权/期货后缀**（PR #57）：`.BJ/.HK/.SHO/.SZO/.SF/.DF/.IF/.ZF/.INE/.GF` 等 ContextInfo 原生后缀直接透传，不再 ValueError——ETF 期权、商品期货/期权代码可正常走 RPC。原 6 位股票码逻辑不变。
- **get_full_tick 超时参数**（PR #57）：新增 `timeout_seconds=None`，调用方传值时优先（批量查 1256+ 期权代码时 120s 默认不够），不传时保留原自动检测（全市场 30s）。

---

## [0.2.3] - 2026-08-19

### 修复

- **异步下单的 order/trade 事件先于 async_response 到达**（Issue #51）：两条回调走不同通道——`async_response` 在异步下单工作线程触发，`order`/`trade` 来自 Redis pub/sub 监听线程；服务端 `order_callback` 先推事件、后回 RPC，顺序颠倒是常态。客户端按 `order_remark` 设屏障：命中待响应委托的事件先暂存，response（或 error）触发后按到达顺序放行；10 秒超时兜底，提交失败的事件也不会被永久扣住（丢事件比顺序错乱更糟）。仅 `order_stock_async` 路径受影响，手工/同步/无 remark 委托直通。成交事件无 remark 时按委托事件学到的 `order_sys_id` 关联。已验证：9 个单测（含 4 个反向验证）+ 盘后真实 Redis 注入实测 + 盘中 3 轮真实买卖验证（async_response 均先于 order/trade 到达，买单 50→54 撤单成功，卖单无持仓 50→57 废单正确上报）。
- **重复 order_remark 导致暂存事件丢失**（Issue #51 后续）：`order_remark` 不强制唯一（网格类策略常复用同一 remark），同 remark 的第二笔下单会让 `_arm_order_barrier` 直接覆盖前一笔的屏障，暂存事件被静默丢弃；且前一笔的 response 会误放后一笔的屏障，使后一笔失去保序。改为接管旧屏障时先放行其暂存事件；`_release_order_barrier` 增加 seq 校验，只有 arm 时的那笔委托的 response 才能放行对应屏障。回归测试对任一半修复回退均失败。
- **download_history_data2 只下载当天数据**（Issue #54）：部分 QMT 版本只注入单股下载全局 `down_history_data`，而捕获列表只有 `download_history_data/2` → 下载 RPC 静默返回 False、什么都没下，读取只能看到当天数据。捕获列表补 `down_history_data`；`download_history_data2` 无批量全局时按代码循环调用单股全局（日期透传）；`_handle_download_history_data` 同步兜底。修正 DRYRUN 里恒为 False 的下载绑定诊断打印。
- **get_financial_data 表名不兼容**（Issue #52）：MiniQMT 传整表名（Balance/Income/…），大 QMT 要 `"BIGTABLE.field"` 点分字段列表。服务端适配层新增 MiniQMT→大QMT 表名映射（8 张表）+ 整表名展开为全字段点分列表；点分条目前缀重映射、未知表名透传。实盘验证：`Balance`（展开 54 字段）与 `Capital`（6 字段）均返回真实数据（首调服务端自动下载财务数据较慢，之后毫秒级）。
- **本地缓存丢时间轴导致日期窗口失效**（Issue #54 关联）：客户端规范化把 `stime` 列转成索引（MiniQMT 形态），而 local_cache 只在列里找时间轴 → `get_local_data` 的 start/end 完全不生效、按索引去重也失效。修复：缓存层识别索引形态时间轴（`write`/`read`/`covered`/count 截尾/占位行清理全链路保索引）；parquet 写盘带索引；老版写出的无时间轴缓存文件遇新版写入时自动废弃（行不可切片，留着只会污染窗口过滤）。新增 18 个读路径矩阵用例（形态 × 周期 × 窗口 × count × 复权隔离）。

## [0.2.2] - 2026-08-19

### 修复

- **server_error 污染后续查询**（Issue #43）：`_last_server_error` 是实例状态，但每个成功响应都会读取它，而只有下单路径会重置。一次静默拒绝的委托会把错误盖到之后**所有** ping 和查询上，直到下一次下单。改为在 `handle()` 中每请求清空，且清空发生在方法校验之前，因此被拒绝的方法也不会携带上一次的诊断。
- **order_remark 匹配的模糊兜底**（Issue #41）：那段 `stock_code + action` 的兜底并非用于*识别*委托，而是**告警闸门**——问题比报告描述的更严重。`order_tag` 是我们生成的唯一 id，匹配不上即真未进系统；模糊兜底唯一的作用是**压制真实告警**：账户中若有一笔无关的同股票同方向委托（手动下的或上一笔未成交的），会导致 `order_sys_id` 未回填、`server_error` 为空，客户端看到一次干净的成功，而该委托从未进入系统。已移除。
- **order_stock_async 阻塞 QMT 主线程**（Issue #44）：`_handle_submit_order` 中的 `sleep(0.5)` 在 adjust 主线程执行（下单方法不在 `listener_methods` 中，走 deferred 路径），使其余请求串行等待，吞吐上限约 2 单/秒。改为**推迟响应而非推迟工作**：提交后登记 `OrderSettlement` 并停放响应，由每次 adjust drain 重试查询，委托号就绪即在同一 tick 内回复（零 sleep）。后台线程方案不可行——`get_trade_detail_data` 在非主策略线程返回空，会把每笔都误判为静默拒绝。
- **order_stock_async 未立即返回**（Issue #50）：客户端内部同步调用 `order_stock`，阻塞整个 RPC 往返，加上 #44 后的结算等待，每笔 0.5~1 秒。服务端新增 `wait_settlement` 参数（false 时 passorder 一返回即回复，委托号由 `order_callback` 推送）；客户端提交移至工作线程，`order_stock_async` 不碰网络直接返回 seq。`on_order_error` 现在也携带 `seq`，此前无法判断是哪一笔异步委托失败。
- **未复权下载实为空跑**（Issue #47，亦是 #39 的真正原因）：服务端下载此前只在请求复权时执行，未复权路径仅调用 `get_market_data_ex` 读取已有数据，却照常通过 callback 报告 `{finished: N}`——为一件没发生的事显示进度。而 1d/tick 默认即 `dividend_type="none"`。现在所有 `dividend_type` 都执行下载。实盘验证：601398.SH 本地日线从 0 根变为有数据。
- **query_stock_orders 缺少 order_time**（Issue #48）：大 QMT 的 ORDER 行提供报单日期与时间，但三层均未读取。已贯通 `OrderSnapshot` → `order_bigqmt` → `_order_from_dict`，按 MiniQMT `XtOrder.order_time` 语义输出 Unix 秒。实盘验证：11 笔真实委托全部有值。

### 新增

- **qmt_launcher**（Issue #45）：`open` / `close` / `restart` / `status` 四个命令管理 QMT 终端。按 `bin.x64` 路径隔离（同机多实例并存时不会误关其他账户）、以 FormulaServer 端口可连接为就绪判据而非固定 sleep、窗口标题前缀匹配（不再写死版本号）、先优雅终止 20 秒后才强杀。登录路径用 `SendMessage` 投递窗口句柄，不依赖窗口置于前台。
- **get_market_data_ex 分批**（Issue #47 评论）：宽 `stock_list` 此前共用一个 RPC 超时，要么装得下要么整批丢失。改为按 100 个代码一批，单批失败只损失自身代码，全部失败才抛异常。`chunk_size=0` 恢复原行为。
- **bar driver 观测埋点**：`adjust()` 按触发来源分别计数、`tick_app` 全量耗时直方图、init 报告策略品种/周期/订阅能力。用于定位 RPC 读延迟的来源。

### 变更

- `AssetSnapshot` 补齐 `frozen_cash` / `market_value`，对齐 MiniQMT `XtAsset`；`market_value` 优先取 `m_dInstrumentValue`，仅在服务端未上报时才推导（推导会扣除冻结金额，此前未扣导致市值虚高）。
- ZMQ 传输改为精确绑定配置端口，冲突时报错而非向上扫描——端口静默漂移会让客户端连不上。

### 已知限制

- Issue #44 / #50 的实盘下单验证尚未完成（单测已量化非阻塞行为：drain < 0.2s、20 单 < 1.0s）。
- Issue #47 评论所述的 `get_market_data_ex` 超时未能复现；三组压测（300 只 × count=3、300 只 × 全历史、50 只 × 1m 全天）最慢 718ms，远在默认 6s 超时内。分批目前是防御性改动。
- RPC 读延迟受 QMT 主线程 GIL 制约，延迟 ≈ 基础 + N × `schedule_adjust_interval`。实测该间隔 200ms → 100ms 可使 p50 从 374ms 降至 172ms，代价是 CPU 占用上升。

---

## [0.2.1] - 2026-08-17

### 修复

- **正常下单误报 on_order_error(-1)**（Issue #38）：passorder 提交成功但委托号异步分配，客户端把「暂无 order_sys_id」误判为失败。服务端 `_handle_submit_order` 按唯一 `user_order_id`(remark) 匹配并回填 `order_sys_id`；顺带修掉校验代码对无 `.get()` 方法的 `OrderSnapshot` 调 `.get()` 的死代码（server_error 之前从未生效）。客户端 `call()` 不再丢弃 `server_error`，委托未进系统时转成异常，`order_stock_async` 携带真实原因回调 `on_order_error`。实盘验证：async 下单回调带真实委托号、提交阶段零误报（302 个测试通过，新增 5 个）。
- **query_stock_orders 查不到委托**（strategy_name 陷阱）：客户端别名默认 `"bigqmt_signal_trader"` 与服务端默认 `""` 不一致，改用其他策略名下单后别名查询返回空。默认改 `""`（返回全部）并对齐测试。

### 新增

- **qmt-trader skill 首次部署引导**：客户端装包、QMT 端文件同步、私有配置模板、入口启动验证、部署排错速查，零上下文也能从零跑通。
- **PyPI 发布**：`BIGQMT_REDIS_DRYRUN` 入口模块补进 py-modules，`pip install xtquant-big-convert` 即可获得完整包（wheel/sdist 均通过 twine check）。

### 变更

- README 头部加 PyPI / Python 版本 / License 徽章，新增「AI 助手 Skill：qmt-trader」专节（启用方式、命令概览、安全设计）。

---

## [0.2.0] - 2026-08-15

### 新增（Features）

- **qmt-trader skill**：统一 CLI 驱动全部 QMT API（`qmt-trader/scripts/qmt.py`），46 个子命令覆盖行情/持仓/委托/下单/撤单/财务/期权/两融/北向/龙虎榜等，含通用 `rpc` 兜底命令 + 25 个高频快捷命令。
- **异步回报回调**：`XtQuantTraderCallback` 全链路（`on_account_status` / `on_order_stock_async_response` / `on_stock_order` / `on_stock_trade` / `on_order_error` / `on_cancel_error` / `on_cancel_order_stock_async_response`），对齐 MiniQMT 原生语义，实盘验证。
- **全推行情订阅**（`subscribe_whole_quote` 真推送）：服务端引用计数管理 + PUB/SUB 数据面通道（redis/zmq）+ 客户端心跳 + 推送静默检测 + 服务端重启恢复。
- **完整 xtconstant 枚举**：91 个常量全量覆盖（账号类型/委托类型-股票期货信用期权/报价类型/委托状态/账号状态/`ORDER_TYPE_SET`），值对齐原生 MiniQMT。
- **文件日志系统**（`logging_setup.py`）：TimedRotatingFileHandler 按天轮转、保留 7 天（`BIGQMT_LOG_RETENTION_DAYS` 可配），双输出（文件 + QMT 面板），线程安全。
- **启动自动诊断**：`init()` 打印服务状态、关键函数绑定、行情链路，方便排错。
- **server_error 字段**：`submit_order` 校验委托是否进系统，静默失败时返回原因给客户端。
- **统一测试入口**：`run_all_tests.py` 分组跑全部测试（signal_trader 274 + backtest 16）。
- **端到端测试**：`test_all_apis.py` 验证真实 QMT 返回（transport 一致性/持仓空/委托空/下单未进系统/server_error）。
- **生产失败场景单元测试**：7 个测试覆盖返回空/全 0/拒绝的 QMT 边界（非 happy-path）。
- **官方交易查询函数**：`get_value_by_order_id` / `get_last_order_id` / `get_ipo_data` / `get_new_purchase_limit` / `get_history_trade_detail_data` / 融资融券 5 个 / 期权持仓 2 个 / 港股通汇率。
- **无 redis 版本**（`bigqmt_no_redis/`）：自包含 ZMQ transport + 无 redis DRYRUN，解决 QMT 沙箱 `import redis` 报错。
- **多账号使用文档**：README 加「多账号使用」章节（多策略实例 + 多 client）。
- **MiniQMT→BigQMT 转换 skill**：docs + scripts + templates（PR #37）。

### 修复（Bug Fixes）

- **QMT 自动退出**：`ZmqQuotePushChannel.stop()` 跨线程关 SUB socket 触发 Windows signaler abort → 进程崩溃。改为订阅线程自己关 socket。
- **QMT 自动退出（系列）**：`_adjust_phase` 无 except（redis 故障崩策略）、`_publish_response` 逃出、deal_callback/forward_order_event/forward_trade_event/sync_positions_app 无防护、pending 队列满（queue.Full）、init() 无防护、socket_timeout=None 永久阻塞主线程、reset_app 不清理 quote-push/whole-quote（重启泄漏）、exec 事件每次回调新建 redis client（连接池泄漏）。
- **download_history_data 下载不了**（Issue #32）：`download_history_data` 是 QMT 全局函数不是 ContextInfo 方法，改走 `qmt_api` 注入。
- **复权数据返回全 0**（front/back）：服务端需先下载原始数据 + 除权因子。下载类（`download_history_data2`）自动预下载；读取类（`get_market_data_ex`/`get_market_data`）自愈（检测全 0 → 服务端下载 → 重试）。
- **卖出方向误判**（exec_events）：QMT 回调 `m_nDirection` 恒为 48，改仲裁链（offset_flag > direction > op_type）。
- **query_orders/query_trades 返回空**：`strategy_name` 过滤不匹配，默认改 `""` 返回全部。
- **get_financial_data 返回 None**：参数顺序错误（stock_list/table_list 反了）。
- **position_events 内存无限增长**（Issue #21）：xadd 无 maxlen，加 maxlen=2000。
- **异步回调签名错误**：`on_order_stock_async_response`/`on_cancel_order_stock_async_response` 原生签名 1 参数（response 带 seq），之前传 2 参数导致 TypeError 被吞。
- **order_stock 返回 -1**：`order_stock_async` 调 `result.get()` 崩，改为触发 `on_order_error`。
- **客户端 transport 不匹配**（Issue #24）：`query_stock_asset` 返回 None 的根因是客户端 redis / 服务端 zmq 不匹配。
- **DRYRUN 硬编码路径**：`_known_qmt_python_dir` 改 sys.path 扫描（paste-run 模式）。
- **ZMQ bind 冲突提示**：加端口占用检测 + 解决步骤提示。

### 变更（Changed）

- 包发布：`pip install xtquant-big-convert`（pyproject.toml 完善元数据 + LICENSE）。
- README 重写：依赖安装分客户端/服务端、API 总览、传输层对比、FormulaServer 直连、异步回调、无 redis 版本、日志排错、多账号、复权陷阱等章节。

---

## [0.1.0] - 2026-07-02

初始版本：Big QMT Redis RPC 桥接 + MiniQMT 兼容层。

### 新增

- Redis RPC 服务（rpush/blpop/brpop）+ 可插拔传输层（redis/zmq/mysql/shm）。
- 客户端兼容层（`xtquant_compat`）：`xt_trader` / `xtdata` 方法名映射。
- 行情/持仓/委托/下单基础 RPC 接口。
- `BIGQMT_REDIS_DRYRUN.py` QMT 编辑器入口。
