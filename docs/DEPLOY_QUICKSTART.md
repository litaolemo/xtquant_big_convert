# 部署快速开始（单账号）

> 面向第一次部署和升级已有部署的最短路径。传输层对比、多账号、无 redis 版本的能力边界见 [README](../README.md)；排错见 README「日志与排错」。

整条链路只有三件东西：**QMT 侧的服务端**（一个策略文件加一个包，跑在 QMT 进程里）、**外部的客户端**（pip 装的包）、**中间的 Redis**（或同机 ZMQ）。部署就是把服务端放进 QMT、让两边的连接参数一致。

## 前提

- 大 QMT 客户端已安装并已登录（国金/华泰等各券商版本均可）
- 一个 Redis；同机可选 ZMQ 免 Redis
- 客户端机器上有 Python 3.8 以上。**QMT 自带的是 Python 3.6.8**，服务端代码就跑在它里面，这一点在升级时会用到

## 先选部署方式

`bigqmt-init` 会问这个问题，先想好：

| 方式 | 放进 QMT 的是什么 | 什么时候选 |
|---|---|---|
| `package`（默认） | 一个包目录 + 三个顶层 `.py`，共 4 项 | 常规选择。升级时可以 `reload_deployment` 热更新，不用重启策略 |
| `single_file` | 一个 `BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py`，包和配置都 base64 内嵌在里面 | QMT 目录不方便放多个文件、或想一个文件拷来拷去 |
| `single_file_no_redis` | 一个 `BIGQMT_DRYRUN_NO_REDIS_FLAT_ALL_IN_ONE.py`，包以明文平铺进去，传输强制 ZMQ | 券商沙箱拒绝 `import redis` 时 |

单文件模式的代价：包内嵌在文件里，升级只能整个文件替换再重启策略，`reload_deployment` 刷不动它。

## 第 1 步：客户端装包

```powershell
pip install "xtquant-big-convert[redis]"
```

这一步装的是**客户端**。服务端代码也在这个包里，下一步会把它取出来。

## 第 2 步：用 `bigqmt-init` 生成配置

不要手抄 `.example.py`。在**能写到 QMT 的 python 目录的机器上**跑：

```powershell
bigqmt-init
```

它逐个问下面这些，括号里是默认值：

| 问题 | 说明 |
|---|---|
| 资金账号 | 服务端和客户端会写同一个，对不上是「查询全空」的头号原因 |
| 账号类型（STOCK） | STOCK / CREDIT / FUTURE / STOCK_OPTION / HUGANGTONG / SHENGANGTONG。信用账户选 CREDIT，选错查出来是整行 0 |
| 传输方式（redis） | redis 或 zmq。两边由同一组答案生成，不会一边 redis 一边 zmq |
| Redis 地址 / 端口 / db / 用户名 / 密码 | 选 redis 才问。密码输入不回显，会写进配置文件 |
| 允许远程下单/撤单？（否） | 打开前它会警告：任何能连上这条通道的程序都可以下单。首次部署先留 `否`，验证通过再打开 |
| 部署方式（package） | 见上表 |
| **QMT 的 python 目录（回车则写到当前目录）** | **这里最容易出错。** 填 QMT 安装目录下的 `python`，如 `D:\国金证券QMT交易端\python`。直接回车会写到你当前所在的目录，服务端找不到配置 |
| 客户端配置写到哪个目录（回车则当前目录） | 你外部程序的目录，回车通常没问题 |

跑完它写出这些文件：

| 文件 | 写到哪 | 谁用 |
|---|---|---|
| `bigqmt_signal_trader_local_config.py` | QMT 的 python 目录 | 服务端 |
| `bigqmt_signal_trader_client_config.py` | 你指定的客户端目录 | 客户端 |
| `BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py` 或 `BIGQMT_DRYRUN_NO_REDIS_FLAT_ALL_IN_ONE.py` | QMT 的 python 目录 | 只有单文件模式才生成，配置已烘焙进去 |

已存在的文件会先问再覆盖，`--force` 跳过询问。

**向导不做的事（这是它最容易被误解的地方）：** `package` 模式下它**只写配置，不拷包**。跑完它会打印一句「把 src/ 下的包同步到 QMT 的 python 目录」——如果你是 pip 装的，没有 `src/`，按下面第 3 步找文件。单文件模式则已经把文件生成到位，第 3 步跳过。

几个不问、直接定死的：

- `rpc_background_threads` 按传输选（redis `True`、zmq `False`），选反了差 4~37 倍
- 选了 `single_file_no_redis` 会把传输改成 zmq，不会留下一份声称用 redis 的配置

> 生成的文件带账号和凭据，**不要提交到版本库**。QMT 登录密码不落盘——`qmt_launcher` 从环境变量 `BIGQMT_LOGIN_PASSWORD` 读。

## 第 3 步：拷服务端文件到 QMT（`package` 模式）

单文件模式跳过本步。

先找到 pip 装的包在哪：

```powershell
python -c "import bigqmt_signal_trader_strategy as m, os; print(os.path.dirname(m.__file__))"
```

把该目录里这 4 项复制到 QMT 的 `python` 目录（和第 2 步写配置的是同一个目录）：

```
bigqmt_signal_trader/                   整个包目录
bigqmt_signal_trader_strategy.py
bigqmt_signal_trader_redis_rpc_runtime.py
BIGQMT_REDIS_DRYRUN.py                  编辑器里加载的入口
```

4 项缺一不可。少了包目录报 `No module named bigqmt_signal_trader`；少了入口面板没有任何输出。

> 纯 ZMQ 同机部署（不想装 redis）：多拷一个 `BIGQMT_ZMQ_DRYRUN.py`，入口换成它。

## 第 4 步：QMT 里运行入口

QMT 策略编辑器**加载并运行 `BIGQMT_REDIS_DRYRUN.py`**（单文件模式是 `..._ALL_IN_ONE.py`），运行模式切到**实盘**。输出面板看到这两行即成功：

```
[bigqmt_shell] bigqmt_signal_trader 0.3.37 loaded from D:\...\python\bigqmt_signal_trader
[bigqmt_rpc] started channel=bigqmt:rpc:req:你的账号
```

第一行的版本号和目录**就是实际加载的**，以后升级看它。

两个会让它看起来启动了、其实没起来的坑：在**策略编辑器界面**直接运行、勾了**「独立 python 进程」**。两种情况下 QMT 不注入任何 API 全局，文件被当普通脚本执行完就结束，`init()` 永远不会调用。面板里 `download globals bound=[]` 是空的就是这个。

## 第 5 步：验证

在客户端机器上：

```powershell
python -c "from bigqmt_signal_trader.xtquant_compat import configure, xtdata; configure(); print(xtdata.get_deployment_info())"
```

它回答的是**服务端**在跑哪个版本、从哪个目录加载：

```
{'version': '0.3.37', 'package_dir': 'D:\\...\\python\\bigqmt_signal_trader', 'python_version': '3.6.8', ...}
```

版本对得上，再拉一次行情：

```powershell
python -c "from bigqmt_signal_trader.xtquant_compat import configure, xtdata; configure(); print(xtdata.get_full_tick(['000001.SZ']))"
```

能打出五档盘口即部署成功。

## 部署后建议跑一次能力探测

不同券商 QMT 暴露的 callable 不一样（下载全局、信用接口、L2 等），一条命令列出本机能力：

```powershell
python -c "from bigqmt_signal_trader.xtquant_compat import configure, xtdata; configure(); import json; print(json.dumps(xtdata.call_method('probe_capabilities'), ensure_ascii=False, indent=2))"
```

输出三部分：`qmt_globals`（下载/信用/交易全局函数是否绑定）、`contextinfo_methods`（ContextInfo 方法存在性）、`credit_probe`（信用接口只读试调结果）。

## 升级已有部署（`package` 模式）

客户端升级只是 `pip install -U`。服务端升级是文件拷贝，有几个地方会踩坑，按这个顺序做，收盘后做。

**1. 看清线上现在是什么。** 先问一遍：

```powershell
python -c "from bigqmt_signal_trader.xtquant_compat import configure, xtdata; configure(); print(xtdata.get_deployment_info()['version'])"
```

**2. 确认线上没有你没记住的本地改动。** 把线上的包和它声称的那个版本的 tag 逐文件比对。**先把换行符归一化**：Windows 上的文件多半是 CRLF，git 里是 LF，不归一化会看到每一行都不同，把正常状态误判成大量私改。

```powershell
# 在仓库 clone 里，对每个 .py：tr -d '\r' 后比 sha256
git show v0.3.34:src/bigqmt_signal_trader/redis_rpc.py | tr -d '\r' | sha256sum
tr -d '\r' < D:\...\python\bigqmt_signal_trader\redis_rpc.py | sha256sum
```

对不上的文件再和后面几个 tag 比。线上经常是「基线版本 + 几个文件手动更新到了更新的版本」这种混搭，只要每个文件都能对上某个 tag，就是干净的，可以覆盖。对不上任何 tag 的文件才是本地改动，停下来搞清楚。

**3. 确认三个顶层文件有没有变。** `bigqmt_signal_trader_strategy.py`、`bigqmt_signal_trader_redis_rpc_runtime.py`、`BIGQMT_REDIS_DRYRUN.py` 是 QMT 自己 exec 的，`reload_deployment` 刷不动它们。新版本里这三个和线上相同，就走热更新；有一个不同，就得重启策略。

**4. 用 QMT 自带的 Python 3.6.8 编译一遍要变的文件。** 仓库 `pyproject` 声明的是 3.8 以上，但服务端跑在 3.6.8 里。`bin.x64\pythonw.exe` 就是它，**没有控制台，报错看不见**，所以让它把结果写进文件：

```python
# py36check.py
import sys, py_compile
out = ["python " + sys.version.split()[0]]
for path in sys.argv[1:]:
    try:
        py_compile.compile(path, doraise=True); out.append("OK   " + path)
    except Exception as exc:
        out.append("FAIL %s -> %s" % (path, exc))
open(r"C:\temp\py36result.txt", "w").write("\n".join(out))
```

```powershell
& "D:\...\bin.x64\pythonw.exe" py36check.py "新版本\bigqmt_signal_trader\xtquant_compat.py" "新版本\bigqmt_signal_trader\exec_events.py"
type C:\temp\py36result.txt
```

第一行会印出 `python 3.6.8`，证明跑的确实是 QMT 那个解释器。f-string 的 `=` 说明符、海象运算符、`dataclasses` 这些在 3.6 都不存在，有一个 FAIL 就别拷。

**5. 备份。** 整个包目录拷到 QMT 的 `python` 目录**之外**，比如 `D:\...\_deploy_backup_20260911_173558`。放在 `python` 目录里 QMT 会扫到。

**6. 拷贝新包，清 `__pycache__`。** 只拷 `.py`，逐字节校验一遍。`__pycache__` 里是旧字节码，QMT 的 3.6 和你机器上的 3.13 生成的还不兼容，必须清。

**7. 热更新。** 三个顶层文件没变的前提下：

```python
xt_trader.reload_deployment("deploy 0.3.37")   # 排期到下一个 adjust tick，立刻返回
xt_trader.reload_status()                       # 1~2 秒后看这个
```

`reload_status` 回 `{'ok': True, 'version_before': '0.3.34', 'version_after': '0.3.37', 'modules_purged': 31, 'seconds': 1.15}` 就是成了。期间约 1 秒的查询会超时。

**8. 验证。** `get_deployment_info()` 的版本变了、终端进程 PID 没变（说明没重启）、日志里没有新的 ERROR、拉一次行情和持仓正常。

**回滚**：把备份目录拷回去，再 `reload_deployment` 一次。

## 常见问题（部署期 90% 的问题都在这里）

| 现象 | 原因与处理 |
|------|-----------|
| ping 超时 | 客户端和服务端 transport 不一致（一边 redis 一边 zmq），或 Redis 地址/密码/db 不一致。用 `bigqmt-init` 一次生成两份就不会 |
| 服务端找不到配置 | `bigqmt-init` 问「QMT 的 python 目录」时直接回车了，配置写到了当前目录。把 `bigqmt_signal_trader_local_config.py` 挪到 QMT 的 python 目录 |
| QMT 面板报 `import redis` 被拒 | 券商沙箱白名单拦截 → 重跑 `bigqmt-init` 选 `single_file_no_redis`，或纯 ZMQ 入口 |
| 查询全空但账户有数据 | 账号没对上（服务端 `BIGQMT_ACCOUNT_ID` vs 客户端），或 QMT 不在实盘模式 |
| 信用账户查出来整行 0 | `bigqmt-init` 的账号类型选了 STOCK，应选 CREDIT |
| QMT 报错 `unexpected keyword argument 'protocol'` | QMT 自带 redis-py 3.5.3 太旧——升级桥接包到 ≥0.2.9（已修，按版本能力透传） |
| 启动面板没有任何输出 | 文件没拷全（4 项缺一不可），或加载的是 runtime 文件而不是 DRYRUN 入口 |
| 面板像正常启动然后"结束运行"，外部连不上 | 看 `download globals bound=[]` 是不是**空的**、有没有 `init ok`。空的说明 QMT 没注入任何 API 全局——文件被当**普通脚本**执行。两个已知原因：在**策略编辑器界面**运行、勾了**「独立 python 进程」**。0.3.8 起入口会直接把这段话打出来（issue #123） |
| 改了代码但没生效 | QMT 跨重跑保留 `sys.modules`，**拷贝本身不生效**。先用 `get_deployment_info()` 确认跑的是哪个 build，再 `reload_deployment` |
| `reload_deployment` 后版本没变 | 改的是三个顶层文件之一，热更新刷不动，重启策略 |
| 升级后策略报 `SyntaxError` | 新代码用了 3.7 以上的语法，QMT 里是 3.6.8。升级前用 `bin.x64\pythonw.exe` 编译一遍 |

## 改完代码怎么生效

**0.3.8 起不用重启**：

```python
xt_trader.reload_deployment("why")   # 排期到下一个 adjust tick
xt_trader.reload_status()            # -> {'ok': True, 'modules_purged': 28,
                                     #     'version_before': ..., 'version_after': ...}
```

约 1 秒，期间约 1 秒的查询会超时（服务正在重建）。

**但改这三个文件仍然要重启策略**：`bigqmt_signal_trader_strategy.py`、`bigqmt_signal_trader_redis_rpc_runtime.py`、`BIGQMT_REDIS_DRYRUN.py`——QMT 自己 exec 它们，模块没法 reload 自己所在的模块。单文件部署整个都是这种情况。

更细的排错（日志位置、日志保留策略、启动诊断字段）见 README「日志与排错」。
