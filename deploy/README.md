# deploy/ — 一键部署包（2核4G Windows 服务器适用）

把整个 `deploy\` 文件夹拷到新服务器，按下面执行。

## 前提（脚本不负责的部分）
1. 已安装大 QMT 客户端并登录交易
2. QMT 菜单里点过 **下载 python库**（脚本会检查 `bin.x64\Lib\site-packages\xtquant`）
3. Python 二选一：
   - **miniconda/anaconda（推荐，加 `-Conda` 参数）**——已装则直接用；**没装会自动下载
     Miniconda 官方安装器静默装到 `<WorkDir>\miniconda3`**（官方源国内直连可达；
     离线机器可提前下载安装器后传 `-MinicondaUrl "C:\temp\Miniconda3-....exe"`）
   - 或系统 Python ≥ 3.10 在 PATH 上（默认走 venv）
   - 注意：`-WorkDir` 路径不要含空格（Miniconda 静默安装的限制）
4. 管理员 PowerShell

## 一键部署

```powershell
cd <deploy目录>
powershell -ExecutionPolicy Bypass -File .\deploy_qmt_bridge.ps1 `
    -QmtDir  "C:\你的券商QMT目录" `
    -Account "资金账号" `
    -WorkDir "C:\qmt_bridge" `
    -Conda `
    -Proxy   "http://127.0.0.1:7897"        # 没有代理就删掉这行
```

脚本幂等，可重复跑。**pip / conda 包源 / Miniconda 安装器默认全部走清华镜像**（国内实测 ~2MB/s；
需要换回官方源时传 `-PipIndex https://pypi.org/simple`，conda/Miniconda 同理见脚本内默认值）。
做完的事：建客户端环境（`-Conda` 走 miniconda py3.13 前缀环境，否则系统 python venv）并装包 → 拷服务端 4 项进 QMT python 目录 →
下载/解压 Redis → 注册 Windows 服务（bind 127.0.0.1 + 随机密码 + 192mb 上限）→
生成服务端/客户端配置 → 放入 `qmt_cli.py`。

**Redis 下载慢/中断**（GitHub Releases 国内直连不稳）三种出路，按速度排序：
1. `-RedisZip "C:\path\Redis-x64-5.0.14.zip"` 离线传入（最快，一次拷贝终身用）
2. `-RedisUrl "https://<镜像>/...zip"` 自定义下载源（公共 gh 加速时快时慢，建议实测后固定）
3. `-Proxy http://...` 走代理下载官方源

**离线服务器**：先在别的机器下载 Redis zip，
加参数 `-RedisZip "C:\temp\Redis-x64-5.0.14.zip"`（PyPI 下载仍需网络，或提前做好 venv 拷贝过去）。

**下单默认关闭**：生成的服务端配置 `rpc_allow_order_methods=False`，新部署只应答查询；
确认风控后加 `-AllowOrders` 重跑（或改配置为 True 并重启策略）才开放 buy/sell/cancel。

## 部署后只剩两步人工操作
1. QMT → 模型交易 → 加载 `<QMT目录>\python\BIGQMT_REDIS_DRYRUN.py` → **运行模式切"实盘"** → 启动
2. 验证（python 路径按所用路线二选一——`-Conda` 用第一行，系统 python venv 用第二行）：
   ```powershell
   & "C:\qmt_bridge\envs\bigqmt\python.exe" "C:\qmt_bridge\client\qmt_cli.py" ping
   & "C:\qmt_bridge\envs\bigqmt-client\Scripts\python.exe" "C:\qmt_bridge\client\qmt_cli.py" ping
   ```
   > PowerShell 里执行带引号的路径必须以 `&` 开头；行尾不要多引号。

## 检查状态（不写任何东西）

```powershell
powershell -File .\deploy_qmt_bridge.ps1 -QmtDir "..." -Account "..." -WorkDir "..." -CheckOnly
```

## 2核4G 内存预算
| 组件 | 占用 |
|---|---|
| 大 QMT（登录+行情） | ~1.5G（关掉不用的面板/自选股能省不少） |
| Redis（已限 192mb noeviction） | ≤200M |
| Python 策略进程（pandas+aiohttp） | ~400M |
| 系统剩余 | ~1.5G |

合计约 3.2-3.5G，可用但别再跑其他重负载。Redis 密码在 `<WorkDir>\redis\redis_password.txt`。

## 策略代码（可选）
加 `-WithStrategy -StrategySrc "C:\path\to\my_code"` 会把策略目录一起拷到 `<WorkDir>\my_code`；
注意检查 `order_config.json` 里的 `account_num` 与 `-Account` 一致。

## 细节文档
部署原理、传输层对比、排错见 [docs/USER_GUIDE.md](../docs/USER_GUIDE.md) 与 [docs/DEPLOY_QUICKSTART.md](../docs/DEPLOY_QUICKSTART.md)。
