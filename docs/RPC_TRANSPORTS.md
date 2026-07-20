# 可插拔 RPC 传输层

更新时间：2026-07-02

## 目标

在 Redis RPC 之上加一层抽象，支持快速切换传输后端，按延迟/部署场景选择：

| 传输 | 同机 p50 | 跨机 | 依赖 | 适用场景 |
|------|---------|------|------|---------|
| `redis`（默认）| ~12ms | ✅ | redis-py | 生产默认，跨机也能用 |
| **`zmq`** | **~0.2ms** | ✅（tcp） | pyzmq | 同机低延迟，主优化目标 |
| `mysql` | ~50ms+ | ✅ | DBUtils + 驱动 | 兼容兜底（Redis/ZMQ 都不可用时）|
| `shm` | — | ❌ | — | 留接口未实现（需 Python 3.8+）|

切换传输**只改一个配置字段 `transport`**，业务代码（handlers / `to_jsonable` / `process_request`）零改动。

## 架构

```
业务层（不变）              BigQmtRpcHandlers / process_request / to_jsonable
                                        │  request/response dict (JSON)
                        ┌───────────────▼────────────────┐
                        │     RpcTransport 抽象接口       │
                        │  send_request / start_receiving│
                        │  send_response / stop          │
                        └───┬────────┬─────────┬─────────┘
                 ┌──────────▼┐  ┌────▼───┐  ┌──▼─────┐  ┌──────┐
                 │  Redis     │  │  ZMQ   │  │ MySQL  │  │ SHM  │
                 │  (默认)    │  │ (低延迟)│  │(兼容)  │  │(stub)│
                 └────────────┘  └────────┘  └────────┘  └──────┘
```

传输层只负责"请求/响应怎么在网络上走"，不碰业务语义。抽象接口见
`src/bigqmt_signal_trader/transports/base.py`：

- `send_request(request, timeout)` — 客户端发请求并阻塞等响应
- `start_receiving(on_request)` — 服务端开始接收，每个请求回调 `on_request`
- `send_response(request, response)` — 服务端回包（路由信息从 request 读）
- `stop()` — 释放资源

## 配置怎么切换

### 服务端（QMT 进程）

在 `bigqmt_signal_trader_local_config.py` 的 `BIGQMT_REDIS_CONFIG` 里设置 `transport` 字段：

```python
BIGQMT_REDIS_CONFIG = {
    "transport": "zmq",          # 默认 "redis"。可选: redis/zmq/mysql/shm
    "zmq": {
        "bind_address": "tcp://127.0.0.1:5560",  # Windows 同机使用 TCP 回环
    },
    "rpc_background_threads": False,
    "schedule_adjust": True,
    "schedule_adjust_interval": "100nMilliSecond",
}
```

> **注意（ZMQ）**：ZMQ 支持由 QMT 官方 `adjust` 回调排空请求。低延迟实盘建议设置
> `rpc_background_threads=False`，并把 `schedule_adjust_interval` 设置为
> `100nMilliSecond`，避免后台 Python 线程受 QMT 进程 GIL 调度影响。
> MySQL/SHM 仍会自动启用后台接收线程。
> 端口不写时按账号自动派生 `tcp://127.0.0.1:{15560 + 账号%100}`（同机回环）。
> Linux 同机也可使用 `ipc:///tmp/bigqmt_rpc.sock`。

`transport=mysql` 时可额外配置：

```python
BIGQMT_REDIS_CONFIG["mysql"] = {
    "driver": "pymysql",      # 或 mysql.connector
    "host": "127.0.0.1", "port": 3306,
    "user": "rpc", "password": "***", "database": "bigqmt_rpc",
    "pool_config": {
        "mincached": 1, "maxcached": 4,
        "maxshared": 3, "maxconnections": 8,
    },
}
```

**不指定 `transport` = `"redis"` = 完全保持现状。**

### 客户端

`BigQmtRpcClient` 同样读 `transport` 字段（从 client config 或环境变量 `BIGQMT_RPC_TRANSPORT`）：

```python
BIGQMT_REDIS_CONFIG = {
    "transport": "zmq",
    "zmq": {"connect_address": "tcp://127.0.0.1:5560"},  # 指向服务端 bind 地址
    # ...
}
```

或环境变量：`export BIGQMT_RPC_TRANSPORT=zmq`

## 各传输说明

### Redis（`transport: redis`，默认）

完全保持原有行为：
- 客户端 `RPUSH` 请求到 `bigqmt:rpc:queue:{account_id}` → `BLPOP` 响应 list
- 服务端 `brpop` 取请求 → 三路回包（`SETEX` key + `RPUSH` list + `PUBLISH` channel）
- 保留 b64 股票代码混淆编码

现有 14 个测试、所有模板字符串、配置全部不变。

### ZMQ（`transport: zmq`，低延迟）

- 服务端 ROUTER socket bind，客户端 DEALER socket connect
- 用 ZMQ 原生 identity 路由（`reply_*` 字段忽略）
- Windows 用 `tcp://127.0.0.1:port`（ZMQ 在 Windows 不支持 `ipc://`）
- Linux 同机可用 `ipc://` 更快（绕过 TCP 栈）
- 实测同机 tcp 回环：**p50 = 0.2ms**（比 Redis 快 ~60 倍）；
  在大 QMT 全终端进程内实测 ping **p50 ≈ 0.3ms**（20 次 0 个 >50ms）。

注意：ZMQ transport 的 `stop()` 由 ROUTER 接收线程自己关闭 socket（Windows
上跨线程 close socket 会触发 signaler 断言）。

> **⚠️ 在大 QMT 进程内跑 zmq 的两个必要条件（都已自动处理，勿手动关）：**
>
> 1. **`background_threads` 必须为 True** —— ZMQ 的 ROUTER 只有在后台线程里才
>    起接收循环；否则只 bind 不收包，客户端全部超时。`_build_rpc_service` 已对
>    非 redis 传输**自动置 True**，无需在 config 里写。
> 2. **`schedule_adjust` 必须保持开** —— `run_time("adjust", interval)` 是我们注册的
>    **RPC 队列 drain 定时器**(`adjust` 不是 QMT 内置回调,QMT 只自动调 init/handlebar;
>    handlebar 里 `return adjust(...)`)。deferred 档的交易查询要靠它在主线程执行;关掉就没
>    有主线程 drain 点。它也是后台线程拿 GIL 窗口的节奏源:`schedule_adjust_interval` 越小
>    inline 尾延迟越低(500ms→~490ms,100ms→热循环~100ms 但烧 CPU,**200ms 折中**)。
>    详见 `docs/BIG_QMT_REDIS_RPC.md` 的「延迟模式」。

### MySQL（`transport: mysql`，兼容兜底）

- 用 `requests` / `responses` 两张表轮询
- 通过 **DBUtils `PooledDB`** 连接池管理连接，避免频繁开关
- 跨驱动：支持 pymysql / mysql.connector / sqlite3（paramstyle 自动适配）
- `DELETE-then-INSERT` 写响应，兼容 MySQL 和 sqlite
- 延迟较高（~50ms+，受轮询间隔限制），仅作 Redis/ZMQ 不可用时的兜底

连接池配置（`pool_config`）：
```python
"pool_config": {
    "mincached": 1,      # 空闲连接数
    "maxcached": 4,      # 最大缓存连接
    "maxshared": 3,      # 最大共享连接
    "maxconnections": 8, # 最大连接数
}
```

注意：sqlite 连接线程绑定，sqlite 测试需 `check_same_thread=False` + `maxshared=0`。

### SHM（`transport: shm`，未实现）

留接口，`send_request` 会抛 `TransportError`。Python 3.8+ 的
`multiprocessing.shared_memory` 或自定义 mmap 环形缓冲区可后续实现。

## 实测延迟对比（同机）

基准脚本：`python bench_transports.py -n 100`

```
redis    n=100  min=10.86  p50=12.22  p90=14.77  p99=290.60  avg=24.99 ms
zmq      n=100  min=0.15   p50=0.21   p90=0.33   p99=20.62   avg=0.43 ms
```

## 切换检查清单

1. 服务端和客户端的 `transport` 字段**必须一致**
2. zmq：不写 `zmq` 块时端口按账号自动派生 `tcp://127.0.0.1:{15560+账号%100}`，
   两端一致；要跨机或自定义端口时才写 `connect_address`/`bind_address`
3. zmq/mysql：`background_threads` 由 `_build_rpc_service` 自动开，**不用手动配**
4. zmq（大 QMT 进程内）：**保持 `schedule_adjust` 开**（默认就是开），否则 adjust
   空转占满 GIL 饿死接收线程 → RPC 超时
5. mysql：两端连同一个数据库，schema 自动创建
6. 切回 redis：删掉 `transport` 字段或设为 `"redis"`，无需改其他配置

## 文件结构

```
src/bigqmt_signal_trader/transports/
├── __init__.py            # 导出 build_transport, RpcTransport
├── base.py                # RpcTransport 抽象基类
├── redis_transport.py     # Redis 实现（默认，零行为变更）
├── zmq_transport.py       # ZMQ ROUTER/DEALER 实现
├── mysql_transport.py     # MySQL + DBUtils 连接池
├── shm_transport.py       # 共享内存 stub
└── factory.py             # build_transport(name, config) 工厂
```

测试：`tests/bigqmt_signal_trader/test_transports.py`（9 个测试，含 ZMQ/MySQL 往返）
