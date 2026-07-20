# QMT 原生 ZMQ 回测桥接

## 1. 目标和边界

正式模式是在 **QMT 回测进程内部**运行一个 ZMQ 服务，把 QMT 当前回测 Bar、
账户、持仓、委托和成交桥接给外部策略。QMT 是唯一的行情推进器、回测引擎、
账户系统和撮合器。

`BIGQMT_ZMQ_BACKTEST.py` 与现有实盘 RPC 入口完全分离：

- 不导入或修改 `bigqmt_signal_trader`；
- 使用独立端口 `tcp://127.0.0.1:16662`；
- 只允许 `ContextInfo.do_back_test=true` 的 QMT 回测上下文；
- 协议固定返回 `live_ready=false`；
- ZMQ 后台线程只接收请求和排队，不直接调用 QMT API；
- `passorder`、撤单和账户查询只在 QMT `handlebar` 回调线程执行。

项目仍保留端口 `16661` 的 CSV 独立回测工具，用于脱离 QMT 的协议测试。该工具
使用本地 `BacktestEngine/SimulatedBroker`，不是 QMT 原生回测服务，二者不能混用。

## 2. 运行时序

1. QMT 加载 `BIGQMT_ZMQ_BACKTEST.py` 并调用 `init(ContextInfo)`。
2. 入口确认当前是 QMT 回测模式，绑定 QMT 注入的 `passorder`、`cancel` 和
   `get_trade_detail_data`，然后启动 ZMQ 服务。
3. QMT 调用 `handlebar` 时，服务发布当前 Bar，并等待外部策略完成这一 Bar 的决策。
4. 外部策略调用 `submit_order` 或 `cancel_order`；ZMQ 线程只把命令放入当前 Bar 队列。
5. 外部策略调用 `next_bar` 后，QMT 回调线程排空命令并调用 QMT API，然后把控制权
   交还 QMT。QMT 自己撮合并推进下一根 Bar。
6. QMT 的 `order_callback`、`deal_callback` 以及账户查询结果会进入 ZMQ 状态；
   QMT 调用 `stop/after_backtest` 后，下一次 `next_bar` 返回 `done=true`。

外部策略超时不释放当前 Bar 时，桥接会抛出超时错误并停止继续下单，避免 QMT
静默跑完整段历史而外部策略没有参与。

## 3. QMT 端安装与配置

同步以下内容到正在运行的 QMT `python` 目录：

```text
src/bigqmt_backtest/
src/BIGQMT_ZMQ_BACKTEST.py
```

编辑 `BIGQMT_ZMQ_BACKTEST.py` 顶部配置：

```python
BACKTEST_ZMQ_CONFIG = {
    "bind_endpoint": "tcp://127.0.0.1:16662",
    "run_id": "",                    # 空值会按启动时间生成
    "account_id": "你的QMT回测账号",
    "account_type": "STOCK",
    "strategy_name": "ZMQ_BACKTEST",
    "combo_type": 1101,
    "quick_trade": 2,
    "market_price_type": 5,
    "limit_price_type": 11,
    "bar_wait_timeout_seconds": 60,
    "require_qmt_backtest": True,
}
```

入口是 GBK/ASCII；`bigqmt_backtest` 包使用 UTF-8。在 QMT 中创建回测任务并且只加载
`BIGQMT_ZMQ_BACKTEST.py`，不要使用“启动本地 Python”。启动日志会打印实际
`run_id`、端口和账号。

`account_id` 必须填写，服务会调用 `ContextInfo.set_account(account_id)`，外部订单最终
通过以下 QMT 原生接口提交：

```text
passorder(23/24, 1101, account_id, symbol, price_type, price, quantity,
          strategy_name, quick_trade, client_order_id, ContextInfo)
```

撮合价格、成交时间、手续费、资金和持仓均以 QMT 回测结果为准，桥接不再计算第二套
结果。

## 4. 外部策略启动

安装客户端包：

```powershell
python -m pip install -e .
```

启动 QMT 回测后，在外部 Python 运行：

```powershell
python examples/zmq_backtest_strategy.py `
  --endpoint tcp://127.0.0.1:16662 `
  --symbol 600000.SH `
  --fast 5 `
  --slow 20
```

没有传 `--run-id` 时，客户端先调用 `describe` 发现 QMT 本次运行的 `run_id`。同一运行
只允许首个调用 `start` 的 `client_id` 控制。

## 5. 协议

ZMQ 使用 `REQ/REP`，请求包含：

```json
{
  "schema_version": 1,
  "request_id": "唯一幂等键",
  "run_id": "qmt-native-20260719-120000",
  "client_id": "strategy-a",
  "method": "start",
  "params": {}
}
```

| 方法 | QMT 原生模式含义 |
|---|---|
| `ping` / `describe` | 探活、发现 `run_id` 和确认 `engine_owner=QMT` |
| `start` | 外部策略挂接并等待 QMT 第一根 Bar；不启动第二个引擎 |
| `submit_order` | 把订单意图排入当前 Bar，返回 `QUEUED` |
| `cancel_order` | 把撤单意图排入当前 Bar |
| `next_bar` | 释放当前 Bar；QMT 线程执行命令并等待 QMT 下一根 Bar |
| `state` | 返回 QMT 缓存的资金、持仓和当前 Bar |
| `history` | 返回 QMT 已经发布给外部策略的历史 Bar，不泄露未来数据 |
| `orders` / `fills` | 返回 QMT 委托/成交查询及回调归一化结果 |
| `finish` | 外部策略解除挂接；QMT 回测报告仍由 QMT 生成 |

响应包含 `execution_backend=QMT_NATIVE`、`execution_mode=QMT_BACKTEST` 和
`live_ready=false`。相同 `request_id` 的重试返回缓存响应。

## 6. CSV 独立模式

仅当不启动 QMT、需要验证协议或外部策略逻辑时使用：

```powershell
python -m bigqmt_backtest.server `
  --data examples/backtest_bars.example.csv `
  --config examples/backtest_config.example.json `
  --run-id demo-001 `
  --bind tcp://127.0.0.1:16661
```

该模式响应 `execution_backend=LOCAL_SIM`，本地产出 `result.json`、委托、成交、资金
曲线等证据。它不会调用 QMT，也不能代表 QMT 原生撮合结果。
