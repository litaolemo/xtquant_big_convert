# 部署快速开始（单账号）

> 面向第一次部署的最短路径。完整安装、传输层对比、无 redis 版本等见 [README](../README.md) 的「环境要求与依赖安装」「快速开始」章节；同一台 QMT 上跑第二个账号见 README「多账号使用（股票+期货 / 普通+信用）」；排错见 README「日志与排错」。

## 前提

- 大 QMT 客户端已安装并已登录（国金/华泰等各券商版本均可）
- 一个 Redis（默认传输；同机可选 ZMQ 免 Redis，见文末）

## 第 1 步：客户端装包（1 分钟）

```powershell
pip install "xtquant-big-convert[redis]"
```

## 第 2 步：拷 4 个文件到 QMT 的 python 目录

定位包里的文件（pip 装完直接打印路径）：

```powershell
python -c "import bigqmt_signal_trader_strategy as m, os; print(os.path.dirname(m.__file__))"
```

把该目录里这 4 项复制到 QMT 的 `python` 目录（如 `D:\国金证券QMT交易端\python\`）：

```
bigqmt_signal_trader/                   （整个包目录）
bigqmt_signal_trader_strategy.py
bigqmt_signal_trader_redis_rpc_runtime.py
BIGQMT_REDIS_DRYRUN.py                  （★ 编辑器入口）
```

> 纯 ZMQ 同机部署（不想装 redis）：多拷一个 `BIGQMT_ZMQ_DRYRUN.py`，入口换成它。能力边界见 README。

## 第 3 步：写私有配置

在 QMT 的 `python` 目录新建 `bigqmt_signal_trader_local_config.py`（**不进 git**）：

```python
# coding: utf-8
BIGQMT_ACCOUNT_ID = "你的资金账号"
BIGQMT_REDIS_CONFIG = {
    "host": "Redis地址", "port": 6379, "db": 5, "password": "Redis密码",
}
```

## 第 4 步：QMT 里运行入口

QMT 策略编辑器**只加载运行 `BIGQMT_REDIS_DRYRUN.py`**。输出面板看到即成功：

```
[bigqmt_rpc] started channel=bigqmt:rpc:req:你的账号
[bigqmt_signal_trader] init ok
```

## 第 5 步：验证

```powershell
$env:BIGQMT_ACCOUNT_ID="你的资金账号"
$env:BIGQMT_REDIS_HOST="Redis地址"
$env:BIGQMT_REDIS_PORT="6379"
$env:BIGQMT_REDIS_DB="5"
$env:BIGQMT_REDIS_PASSWORD="Redis密码"

python -c "from bigqmt_signal_trader.xtquant_compat import configure, xtdata; configure(); print(xtdata.get_full_tick(['000001.SZ']))"
```

能打出五档盘口即部署成功。

## 部署后建议跑一次能力探测

不同券商 QMT 暴露的 callable 不一样（下载全局、信用接口、L2 等），一条命令列出本机能力：

```powershell
python -c "from bigqmt_signal_trader.xtquant_compat import configure, xtdata; configure(); import json; print(json.dumps(xtdata.call_method('probe_capabilities'), ensure_ascii=False, indent=2))"
```

输出三部分：`qmt_globals`（下载/信用/交易全局函数是否绑定）、`contextinfo_methods`（ContextInfo 方法存在性）、`credit_probe`（信用接口只读试调结果）。

## 常见问题（部署期 90% 的问题都在这里）

| 现象 | 原因与处理 |
|------|-----------|
| ping 超时 | 客户端和服务端 transport 不一致（一边 redis 一边 zmq），或 Redis 地址/密码/db 不一致 |
| QMT 面板报 `import redis` 被拒 | 券商沙箱白名单拦截 → 换无 redis 版本（`bigqmt_no_redis/`）或纯 ZMQ 入口 |
| 查询全空但账户有数据 | 账号没对上（服务端 `BIGQMT_ACCOUNT_ID` vs 客户端 `BIGQMT_ACCOUNT_ID`），或 QMT 不在实盘模式 |
| QMT 报错 `unexpected keyword argument 'protocol'` | QMT 自带 redis-py 3.5.3 太旧——升级桥接包到 ≥0.2.9（已修，按版本能力透传） |
| 启动面板没有任何输出 | 文件没拷全（4 项缺一不可），或加载的是 runtime 文件而不是 DRYRUN 入口 |
| 面板像正常启动然后"结束运行"，外部连不上 | 看 `download globals bound=[]` 是不是**空的**、有没有 `init ok`。空的说明 QMT 没注入任何 API 全局 —— 文件被当**普通脚本**执行，`init()` 永远不会调用，RPC 服务不启动。两个已知原因：在**策略编辑器界面**运行、勾了**「独立 python 进程」**。0.3.8 起入口会直接把这段话打出来（issue #123） |
| 改了代码但没生效 | QMT 跨重跑保留 `sys.modules`，**拷贝本身不生效**。先用 `xtdata.get_deployment_info()` 确认跑的是哪个 build，再让它重新加载 |

## 改完代码怎么生效

**0.3.8 起不用重启**：

```python
xt_trader.reload_deployment("why")   # 排期到下一个 adjust tick
xt_trader.reload_status()            # -> {'ok': True, 'modules_purged': 28,
                                     #     'version_before': ..., 'version_after': ...}
```

约 0.8 秒，期间约 1 秒的查询会超时（服务正在重建）。

**但改这两个文件仍然要重启策略**：`bigqmt_signal_trader_strategy.py`、`BIGQMT_REDIS_DRYRUN.py` —— QMT 自己 exec 它们，模块没法 reload 自己所在的模块。

## 再加一个账号

同一台 QMT 上跑第二个账号（股票+信用、股票+期货）：每个账号一份配置文件，再从 `BIGQMT_REDIS_DRYRUN.py` 另存一份入口副本，副本里加一行说明它读哪份配置：

```python
BIGQMT_LOCAL_CONFIG_MODULE = "bigqmt_signal_trader_local_config_credit"
```

两份入口分别加载运行即可。完整步骤和「怎么确认读对了配置」见 README「多账号使用（股票+期货 / 普通+信用）」。

更细的排错（日志位置、日志保留策略、启动诊断字段）见 README「日志与排错」。
