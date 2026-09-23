# 限制清单与不可转场景判定

每条给出：判定方法 → 影响 → 处理方案。"文件桥方案"指一种通用兜底架构：策略主体留在外部 Python 进程（任意 Python 版本、任意依赖），大QMT内只跑一个轻量桥脚本，两边通过共享目录的 JSON 文件交换指令/状态/行情。落地步骤见 D 节。

## A. 硬性环境限制（所有策略都受约束）

### A1. Python 3.6 语法上限
**判定**：analyze_strategy.py 会扫描。常见违例：walrus `:=`（3.8）、f-string `{x=}`（3.8）、`dataclasses`（3.7）、`asyncio.run`（3.7）、位置仅参数 `/`（3.8）、`match`（3.10）、`dict |` 合并（3.9）、`functools.cached_property`（3.8）。
**处理**：等价改写（walrus 拆两行、dataclass 改普通类、cached_property 改手工缓存）。f-string 本身 3.6 支持，可保留。

### A2. GBK 编码
**判定**：源码含 emoji、生僻字、特殊符号时 GBK 编不出去（check_converted.py 会报）。
**处理**：替换为 GBK 兼容字符；文件必须以 GBK 落盘且首行 `#coding:gbk`。**用 scripts/to_gbk.py 转存，不要用编辑器直接改 GBK 文件**（极易产生 mojibake）。读写外部文件时显式指定 `encoding`，py3.6 在 GBK 环境下 `open()` 默认 GBK。

### A3. 第三方库受限
**判定**：analyze_strategy.py 列出非标准库 import。
内置自带：**NumPy / Pandas / SciPy / Statsmodels / Patsy / TA_Lib**（版本旧，pandas 是 0.x~1.0 时代，无 `df.itertuples` 新参数等高版本特性，`pd.append` 可用）。
**处理**：
- `requests` 等纯 Python 库：多数客户端可用；若报 `Module xxx not in whitelist!` → 券商开了白名单，找券商开通。
- 自装库：本机装 Python 3.6 到 `C:\Python36`，pip 装 **py3.6 兼容版本**，客户端"设置-模型设置"指向该环境（详见迅投官方 [常见问题](https://dict.thinktrader.net/innerApi/question_answer.html) 的第三方库导入指引）。
- torch/tensorflow/akshare 等重型或不兼容 py3.6 的库：**不可转** → 方案①模型推理留在外部进程算好信号，落地文件/HTTP，内置端只读信号执行交易；方案②整体走文件桥。

### A4. 单线程禁阻塞
**判定**：threading/multiprocessing/asyncio import、`time.sleep`、阻塞 IO 重试循环、`while True`。
**影响**：客户端所有策略共用一个 Python 线程，阻塞会卡死全部策略（包括别的策略）。
**处理**：sleep 等待→状态机+下轮定时器检查；并行计算→不可转（外部算好喂进来）；网络请求设短超时且容忍失败。

### A5. ContextInfo 变量回滚
**判定**：原策略若把状态存 self/全局，转换时有人习惯写 `C.xxx = ...` —— 禁止。
**影响**：ContextInfo 随 K线深拷贝回滚，盘中存的状态会丢，且拖慢运行。
**处理**：所有可变状态放模块级 `class G: pass; G = G()` 实例（模板已内置）。

## B. 架构性差异（需要重构的场景）

### B1. 多账户单进程
**判定**：代码里多个 `StockAccount` / 账户列表循环下单。
**影响**：内置策略一个实例绑一个账户（界面选定）。
**处理**：
- 账户数少：每个账户建一个策略交易实例（同一份代码，界面分别选账户）。代码里不要写死账户，全用注入的 `account`/`accountType`。
- 需要跨账户协同（资金调度/对冲腿）：**不可转** → 文件桥方案，外部进程统一调度多个客户端。
- 同券商账号组（passorder 1201/1202）：仅当账户都在同一客户端登录时可用，且为"对组内每户做同样操作"，不支持差异化分配。

### B2. 跨券商/多客户端
**判定**：多个 QMT path、多 session。
**处理**：**不可转**（一个内置策略只活在一个客户端里）→ 每客户端部署各自内置策略（互相独立），或文件桥统一调度。

### B3. 7x24 守护/盘后任务
**判定**：apscheduler 配置了夜间任务、开机自启动逻辑、AutoLogin。
**影响**：内置策略只在客户端运行期间活着；客户端通常夜间关闭/清算期掉线。
**处理**：盘中逻辑转内置；盘后选股/数据下载留外部脚本（Windows 计划任务），结果以文件（如 csv 票池）喂给内置策略读取——常见的"URL/文件票池"模式即属此类，保留即可（改为本地路径或确认客户端能访问该 URL）。

### B4. 委托回报驱动的复杂状态机
**判定**：`on_order_stock_async_response` 用 seq 关联、回报里立刻连锁下单。
**影响**：passorder 无 seq；回报推送只在实盘模式有效且与策略同线程。
**处理**：改 userOrderId（投资备注）关联 + order_callback/轮询双轨对账（模板已含）。连锁下单逻辑放回调里可行但要轻量；稳妥做法是回调只改状态，统一由定时器主循环决策下单。

### B5. Level-2 / 高频依赖
**判定**：`get_l2_quote`、逐笔委托/成交、500ms 以内轮询。
**处理**：内置端有 l2 周期（`l2quote`/`l2order`/`l2transaction`，需账号有 L2 权限）；定时器最细 `nMilliSecond` 级。但所有策略共线程，高频策略相互挤占，延迟敏感型（>1次/秒决策、微秒级要求）**不建议转** → 评估后保留外接或文件桥+外部高性能进程。

### B6. 行情源时效与覆盖
内置行情=客户端行情：非 VIP 用户 `subscribe_quote` 有订阅数量限制；`get_full_tick` 不限。跨市场数据（港股通标的行情等）取决于客户端行情权限。原策略若依赖 xtdata VIP 全推，转换后用 `C.get_full_tick(批量列表)` + 秒级定时器近似。

## C. 业务行为差异（容易踩坑）

| # | 差异 | 应对 |
|---|---|---|
| C1 | `get_trade_detail_data` 读本地缓存（柜台推送 50ms~6s 刷新），下单后立查查不到 | 不要"下单→sleep→查"；按状态机轮询，同标的有待报单时禁止加单 |
| C2 | 交易日切换/行情重连时客户端会**自动重启所有运行中策略**（init 重跑） | init 必须幂等；持久状态可落盘 JSON（绝对路径），init 时恢复；当日已执行标志要带日期 |
| C3 | 模拟信号模式 passorder 不实际下单、回调不触发 | 验证流程先模拟看信号，再实盘小单 |
| C4 | handlebar 盘中每个主图 tick 都触发（不分周期） | 定时器型策略 handlebar 留空直接 return；K线型用 `C.is_last_bar()`/`is_new_bar()` 过滤 |
| C5 | 非交易时间 handlebar 也可能被调用 | 交易逻辑内判时间窗（09:30~14:57） |
| C6 | `get_trading_dates` init 中不可用 | 放 after_init；返回格式为 'YYYYMMDD' 字符串 |
| C7 | 委托数量规则（科创板 200 股起 1 股递增等）与外接一致，但市价单类型仿真柜台不支持 | 仿真测试用限价 11；实盘再放开市价类 prType |
| C8 | strategyName、userOrderId（m_strRemark）、mini 的 order_id 都只在**下单客户端**本地可见（实测：他端查 remark 为空、order_id 为 0）；委托本身柜台级共享 | 同客户端对账用 userOrderId；跨客户端只能凭柜台委托号 sysid |
| C9 | print 进策略日志面板，量大会卡界面 | 控制日志频率；详细日志写文件 |
| C10 | 盘后撤单窗口受柜台限制（实测：17 点后 cancel 信号发出成功但柜台不处理，委托保持已报） | 测试报/撤安排在交易时段或收盘后半小时内；策略收盘前应撤清在途单 |
| C11 | 市值类字段（m_dInstrumentValue 等）按各客户端自己的行情快照计算，跨客户端可能不一致 | 资金对账以 cash/volume 为准（实测两端精确一致），市值仅作展示 |
| C12 | `run_time`/`schedule_run` 回测模式无效，定时器型策略无法直接回测 | 信号逻辑抽独立函数，回测挂 handlebar、实盘挂定时器，一份逻辑两个入口（faq.md Q4） |
| C13 | 内置端发网络请求会阻塞共享线程 | 仅限低频（每日级），超时 ≤2 秒 + try/except 降级；高频外部数据一律走文件通道（faq.md Q1） |

## D. 完全不可转换 → 直接给文件桥方案

满足任一条即建议放弃纯内置转换，采用文件桥（策略零改动）：
1. 重型 ML 推理/重度第三方依赖且无法降级 py3.6
2. 跨账户、跨客户端、跨券商统一调度
3. 策略与 Web 服务/数据库/消息队列深度耦合
4. 需要外部进程级容灾（策略进程独立于客户端存活）

文件桥落地步骤（自行实现一个桥脚本，约 200~300 行）：
1. 在大QMT新建一个内置策略作为"桥"：用 template_timer.py 骨架，`run_time` 1秒循环；指定一个共享目录 `BRIDGE_DIR`
2. 桥脚本每轮做两件事：扫描 `BRIDGE_DIR/cmd/*.json` 指令文件（含 buy/sell/cancel 及参数）→ 调 `passorder`/`cancel` 执行后删除指令文件；把 `get_trade_detail_data` 的委托/持仓/资产 + `get_full_tick` 行情序列化写入 `BRIDGE_DIR/state/orders|positions|asset|quotes.json`，并每秒刷新 `heartbeat.json`（时间戳）供外部判活
3. 外部策略把原 xtquant 调用替换为读写桥目录 JSON：下单=写指令文件，查询=读状态文件，并校验心跳新鲜度
4. 注意原子写（先写临时文件再 rename）、GBK/UTF-8 编码显式声明、指令文件带唯一序号防重放
5. 该模式已在实盘（含两融账户）验证过报/撤单与行情回传链路可行

## 实测验证记录（模拟 mini + 大QMT 双端同账户交叉验证）

以下断言已实弹核验，可直接信赖：
- xtconstant 15 项常量（买卖 23/24、FIX_PRICE=11、LATEST_PRICE=5、委托状态 48~57/255）与映射表一致
- `xc.CREDIT_BUY/CREDIT_SELL` 实际值 = 23/24（与 STOCK_BUY 同值）→ 印证两融转换必须显式改 33/34
- 大QMT内置 `passorder`（11参/prType=11/quickTrade=2/userOrderId）→ `get_trade_detail_data('order')` 按 `m_strRemark` 命中，`m_strOrderSysID` 回传，状态 50已报 → `cancel(sysid)` 信号发出成功
- 同账户跨客户端：mini 可见大QMT 所下委托（同 sysid 同状态），但 remark 为空、order_id 为 0 → C8 结论
- 资产/持仓字段两端精确一致：`cash↔m_dAvailable`、`total_asset↔m_dBalance`、`volume↔m_nVolume`、`can_use_volume↔m_nCanUseVolume`、`avg_price↔m_dOpenPrice`；市值字段两端不一致（各自行情快照）→ C11 结论
- 17 点后柜台不再处理撤单（15:38 同流程撤单成功）→ C10 结论

## E. 部署细节（转换完成后）

1. **新建策略**：大QMT → 模型/策略 → 新建Python策略 → 粘贴 GBK 代码 → 保存编译（看输出面板无报错、中文无乱码）
2. **新建策略交易**：选模型 + 资金账号（类型务必选对 STOCK/CREDIT）+ 周期（定时器型选日线最省）+ 主图代码任意（如 000001.SH）
3. **运行模式**：模拟信号 → 观察 ≥1 个时段 → 实盘交易
4. **自启链路**（生产必配）：客户端设置开机自启与自动登录（券商版路径各异）→ 策略勾选"终端启动后自动运行" → Windows 计划任务登录时启动客户端 exe
5. **实盘首测**：不易成交价小单（买跌停价/卖涨停价）→ 确认委托面板可见、来源=策略名、备注=userOrderId → cancel 撤掉 → 查 `get_trade_detail_data` 状态为 54
6. **回滚预案**：策略交易界面一键停止；停止前手动撤清在途单（stop 回调里不能撤单）
