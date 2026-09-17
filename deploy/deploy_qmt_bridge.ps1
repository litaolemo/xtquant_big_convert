#Requires -Version 5.1
<#
.SYNOPSIS
  One-shot deployment of the xtquant_big_convert Redis bridge on a fresh Windows box.
.DESCRIPTION
  Target: 2C4G Windows server with Big QMT installed and logged in.
  Idempotent - safe to re-run. Skips completed steps.
.PARAMETER QmtDir   Big QMT install root, e.g. C:\JianghaiQMT
.PARAMETER Account  Capital account id (digits), e.g. 1234567890
.PARAMETER WorkDir  Deployment root (default C:\qmt_bridge)
.PARAMETER RedisZip Offline redis zip path; skip GitHub download when provided
.PARAMETER Proxy    HTTP proxy for downloads, e.g. http://127.0.0.1:7897
.PARAMETER AllowOrders  Enable order RPCs in the generated server config (off by default)
#>
param(
    [Parameter(Mandatory=$true)][string]$QmtDir,
    [Parameter(Mandatory=$true)][string]$Account,
    [string]$WorkDir = "C:\qmt_bridge",
    [string]$RedisZip = "",
    [string]$RedisUrl = "",
    [string]$Proxy = "",
    [switch]$CheckOnly,
    [switch]$AllowOrders,
    [switch]$Conda,
    [string]$MinicondaUrl = "",
    [string]$PipIndex = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [switch]$WithStrategy,
    [string]$StrategySrc = ""
)
$ErrorActionPreference = "Stop"

function Step($m) { Write-Host ""
  Write-Host ("== " + $m) -ForegroundColor Cyan }
function Ok($m)   { Write-Host ("   [ok] " + $m) -ForegroundColor Green }
function Info($m) { Write-Host ("   .. " + $m) }

# ---- 0. preconditions -----------------------------------------------------
Step "0/7 preconditions"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $CheckOnly) { throw "Please run from an elevated (Administrator) PowerShell." }
if ($CheckOnly) {
    Info "check-only mode: reporting state, no writes"
    $items = [ordered]@{
        "QMT dir"            = Test-Path "$QmtDir\bin.x64\XtItClient.exe"
        "xtquant lib"        = Test-Path "$QmtDir\bin.x64\Lib\site-packages\xtquant"
        "server files"       = Test-Path "$QmtDir\python\BIGQMT_REDIS_DRYRUN.py"
        "server local config"= Test-Path "$QmtDir\python\bigqmt_signal_trader_local_config.py"
        "client env"         = ((Test-Path "$WorkDir\envs\bigqmt-client\Scripts\python.exe") -or (Test-Path "$WorkDir\envs\bigqmt\python.exe"))
        "redis binaries"     = Test-Path "$WorkDir\redis\Redis-x64-5.0.14\redis-server.exe"
        "redis password file"= Test-Path "$WorkDir\redis\redis_password.txt"
        "redis service"      = [bool](Get-Service RedisBigQMT -ErrorAction SilentlyContinue)
        "client config"      = Test-Path "$WorkDir\client\bigqmt_signal_trader_client_config.py"
        "qmt_cli"            = Test-Path "$WorkDir\client\qmt_cli.py"
    }
    $missing = 0
    foreach ($k in $items.Keys) {
        $mark = if ($items[$k]) { "[ok]     " } else { "[missing]"; $missing++ }
        Write-Host ("   $mark $k")
    }
    if ($missing -gt 0) { Write-Host "   $missing item(s) missing -> run without -CheckOnly (as Administrator)" }
    else { Write-Host "   deployment complete" }
    return
}
if (-not (Test-Path "$QmtDir\bin.x64\XtItClient.exe")) { throw "Big QMT not found under $QmtDir" }
if (-not (Test-Path "$QmtDir\bin.x64\Lib\site-packages\xtquant")) {
    throw "xtquant library missing. Open QMT and run its 'download python libraries' first."
}
$pyExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pyExe -and -not $Conda) { throw "System Python not found on PATH (>= 3.10), or pass -Conda to use miniconda." }
Ok "QMT found; xtquant present; python: $pyExe"

# ---- 1. client environment (conda py313 or venv) --------------------------
Step "1/7 client env (xtquant-big-convert + pandas)"
if ($Conda) {
    $condaExe = (Get-Command conda -ErrorAction SilentlyContinue).Source
    if (-not $condaExe) {
        foreach ($cand in @("$env:USERPROFILE\miniconda3\Scripts\conda.exe",
                            "$env:USERPROFILE\anaconda3\Scripts\conda.exe",
                            "C:\ProgramData\miniconda3\Scripts\conda.exe")) {
            if (Test-Path $cand) { $condaExe = $cand; break }
        }
    }
    if (-not $condaExe) {
        Info "conda not found - auto-installing Miniconda into $WorkDir\miniconda3"
        $condaHome = "$WorkDir\miniconda3".TrimEnd("\\")
        if ($condaHome.Contains(" ")) { throw "WorkDir path contains spaces; Miniconda silent install cannot handle it - choose a space-free -WorkDir." }
        $condaExe  = "$condaHome\Scripts\conda.exe"
        $installer = "$WorkDir\Miniconda3-latest-Windows-x86_64.exe"
        if (-not (Test-Path $installer)) {
            $url = if ($MinicondaUrl) { $MinicondaUrl } else { "https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-latest-Windows-x86_64.exe" }
            $dlArgs = @("-L","--max-time","900","-o",$installer,$url)
            if ($Proxy) { $dlArgs = @("-x",$Proxy) + $dlArgs }
            & curl.exe @dlArgs
            if ($LASTEXITCODE -ne 0) { throw "miniconda download failed (offline? pre-download and pass -MinicondaUrl <local exe path>, or add -Proxy http://...)" }
        }
        # silent install; /D= must be the LAST argument, unquoted, no trailing backslash
        Start-Process -FilePath $installer -ArgumentList "/InstallationType=JustMe","/AddToPath=0","/RegisterPython=0","/S","/D=$condaHome" -Wait
        if (-not (Test-Path $condaExe)) { throw "miniconda silent install failed - $condaExe missing" }
        Ok "miniconda installed: $condaHome"
    }
    $venv = "$WorkDir\envs\bigqmt"
    $vpy  = "$venv\python.exe"
    if (-not (Test-Path $vpy)) {
        New-Item -ItemType Directory -Force "$WorkDir\envs" | Out-Null
        if ($Proxy) { $env:HTTP_PROXY = $Proxy; $env:HTTPS_PROXY = $Proxy }
        # conda >= 25 requires accepting defaults-channel ToS before
        # non-interactive use; older builds lack the subcommand (ignore errors).
        foreach ($ch in @("https://repo.anaconda.com/pkgs/main",
                          "https://repo.anaconda.com/pkgs/r",
                          "https://repo.anaconda.com/pkgs/msys2")) {
            & $condaExe tos accept --override-channels --channel $ch *> $null
        }
        & $condaExe create -y -p $venv -c https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main --override-channels python=3.13
        if ($LASTEXITCODE -ne 0) { throw "conda create failed" }
    }
    Ok "conda env: $venv (python 3.13)"
} else {
    $venv = "$WorkDir\envs\bigqmt-client"
    $vpy  = "$venv\Scripts\python.exe"
    if (-not (Test-Path $vpy)) {
        New-Item -ItemType Directory -Force "$WorkDir\envs" | Out-Null
        & $pyExe -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
    }
}
& $vpy -m pip --version *> $null
if ($LASTEXITCODE -ne 0) { & $vpy -m ensurepip --upgrade }
$pkgList = & $vpy -m pip list 2>$null | Out-String
$pipArgs = @("-m","pip","install","-i",$PipIndex,"--upgrade","pip")
if ($Proxy) { $pipArgs += @("--proxy",$Proxy) }
& $vpy @pipArgs *> $null
if ($pkgList -notmatch "xtquant-big-convert") {
    & $vpy -m pip install $(if ($Proxy) { @("--proxy",$Proxy) } else { @() }) -i $PipIndex "xtquant-big-convert[redis]" pandas
    if ($LASTEXITCODE -ne 0) { throw "pip install failed (check network/proxy)" }
}
Ok "venv ready: $vpy"

# ---- 2. server files into QMT python dir ----------------------------------
Step "2/7 copy bridge server files into QMT python dir"
$src = (& $vpy -c "import bigqmt_signal_trader_strategy as m, os; print(os.path.dirname(m.__file__))").Trim()
$dst = "$QmtDir\python"
if ($src -eq $dst) {
    throw ("source resolved to the QMT python dir itself (" + $src + ") - the import picked up already-deployed files. Run the script from any other directory (e.g. the deploy folder).")
}
foreach ($item in @("bigqmt_signal_trader","bigqmt_signal_trader_strategy.py",
                    "bigqmt_signal_trader_redis_rpc_runtime.py","BIGQMT_REDIS_DRYRUN.py")) {
    Copy-Item (Join-Path $src $item) (Join-Path $dst $item) -Recurse -Force
}
Ok "copied to $dst"

# ---- 3. redis download / unzip --------------------------------------------
Step "3/7 redis 5.0.14"
$rdir = "$WorkDir\redis"
$zip  = "$rdir\Redis-x64-5.0.14.zip"
Add-Type -AssemblyName System.IO.Compression.FileSystem
function Test-RedisZip([string]$Path) {
    if (-not (Test-Path $Path)) { return $false }
    try {
        $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
        try { return [bool]($archive.Entries | Where-Object { $_.Name -eq "redis-server.exe" }) }
        finally { $archive.Dispose() }
    } catch { return $false }
}
$explicitZip = [bool]$RedisZip
if (-not $RedisZip) { $RedisZip = $zip }
if (-not (Test-RedisZip $RedisZip)) {
    # An explicitly passed archive is the caller's: never download over it.
    # The default path is ours: missing means first run, broken means an
    # interrupted download -- both go to the download below.
    if ($explicitZip) {
        if (-not (Test-Path $RedisZip)) { throw "RedisZip not found: $RedisZip" }
        throw "RedisZip is not a valid redis archive (must contain redis-server.exe): $RedisZip"
    }
    New-Item -ItemType Directory -Force $rdir | Out-Null
    if (Test-Path $RedisZip) {
        Info "removing incomplete redis archive: $RedisZip"
        Remove-Item -LiteralPath $RedisZip -Force
    }
    $downloadZip = "$zip.download"
    # Windows PowerShell 5.1 (what the README runs this with) has no
    # if-expression: `(if ...)` inside an array is parsed as a command
    # named "if". Pick the source first, then build the argument list.
    $source = "https://github.com/tporadowski/redis/releases/download/v5.0.14/Redis-x64-5.0.14.zip"
    if ($RedisUrl) { $source = $RedisUrl }
    $dlArgs = @("-L","--connect-timeout","20","--retry","3","--retry-delay","2","--max-time","1800",
      "-o",$downloadZip,$source)
    if ($Proxy) { $dlArgs = @("-x",$Proxy) + $dlArgs }
    & curl.exe @dlArgs
    if ($LASTEXITCODE -ne 0 -or -not (Test-RedisZip $downloadZip)) {
        if (Test-Path $downloadZip) { Remove-Item -LiteralPath $downloadZip -Force }
        throw "redis download failed or incomplete (pass -RedisZip <path> for offline use, -RedisUrl <mirror> for an alternate source, or add -Proxy http://...)"
    }
    Move-Item -LiteralPath $downloadZip -Destination $RedisZip -Force
}
if (-not (Test-Path "$rdir\Redis-x64-5.0.14\redis-server.exe")) {
    Expand-Archive $RedisZip "$rdir\Redis-x64-5.0.14" -Force
}
Ok "redis binaries: $rdir\Redis-x64-5.0.14"

# ---- 4. redis password / conf / service -----------------------------------
Step "4/7 redis service (bind 127.0.0.1 + password + 192mb cap)"
$pwFile = "$rdir\redis_password.txt"
if (Test-Path $pwFile) { $pw = (Get-Content $pwFile -Raw).Trim() }
else {
    $pw = -join ((48..57)+(97..122) | Get-Random -Count 20 | ForEach-Object {[char]$_})
    Set-Content $pwFile $pw -NoNewline -Encoding ASCII
}
$conf = "$rdir\Redis-x64-5.0.14\redis.windows-service.conf"
$marker = "# === bigqmt bridge additions ==="
if (-not (Select-String -Path $conf -SimpleMatch -Pattern $marker -Quiet)) {
    Add-Content $conf "`r`n$marker`r`nbind 127.0.0.1`r`nrequirepass $pw`r`nmaxmemory 192mb`r`nmaxmemory-policy noeviction"
}
$svc = Get-Service RedisBigQMT -ErrorAction SilentlyContinue
if (-not $svc) {
    & "$rdir\Redis-x64-5.0.14\redis-server.exe" --service-install $conf --service-name RedisBigQMT
    if ($LASTEXITCODE -ne 0) { throw "redis service-install failed" }
}
$svc = Get-Service RedisBigQMT
if ($svc.Status -ne "Running") { Start-Service RedisBigQMT }
Ok ("redis service: " + (Get-Service RedisBigQMT).Status)

# ---- 5. server-side config -------------------------------------------------
Step "5/7 write server-side local config"
# Order RPCs stay OFF unless -AllowOrders is given -- the repo's safety
# default: a fresh bridge answers queries only, order placement is an
# explicit human decision (qmt-trader SKILL.md 安全须知).
$allowOrdersPy = if ($AllowOrders) { "True" } else { "False" }
$localCfg = @"
# coding: utf-8
BIGQMT_ACCOUNT_ID = "$Account"
BIGQMT_ACCOUNT_TYPE = "STOCK"

BIGQMT_REDIS_CONFIG = {
    "host": "127.0.0.1",
    "port": 6379,
    "db": 5,
    "username": "",
    "password": "$pw",
    "rpc_allow_order_methods": $allowOrdersPy,
    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    "rpc_background_threads": False,
    "schedule_adjust": True,
    "schedule_adjust_interval": "100nMilliSecond",
    "full_tick_cache_enabled": False,
    "download_jobs_enabled": False,
    "exec_events_enabled": True,
    "exec_events_debug_raw_fields": False,
}
"@
Set-Content "$dst\bigqmt_signal_trader_local_config.py" $localCfg -Encoding UTF8
Ok "$dst\bigqmt_signal_trader_local_config.py"

# ---- 6. client config + CLI ------------------------------------------------
Step "6/7 client config + CLI"
$cdir = "$WorkDir\client"
New-Item -ItemType Directory -Force $cdir | Out-Null
$cliSrc = Join-Path $PSScriptRoot "qmt_cli.py"
if (-not (Test-Path $cliSrc)) { throw "qmt_cli.py must sit next to this script (deploy bundle)." }
Copy-Item $cliSrc "$cdir\qmt_cli.py" -Force
$clientCfg = @"
# coding: utf-8
BIGQMT_ACCOUNT_ID = "$Account"
BIGQMT_RPC_TIMEOUT_SECONDS = 30.0

BIGQMT_REDIS_CONFIG = {
    "host": "127.0.0.1",
    "port": 6379,
    "db": 5,
    "username": "",
    "password": "$pw",
    "transport": "redis",
    "protocol": 2,
}

BIGQMT_FULL_TICK_CACHE_CONFIG = {"enabled": False}
BIGQMT_FORMULA_SERVER_CONFIG = {"enabled": False}
BIGQMT_LOCAL_CACHE_CONFIG = {
    "enabled": True,
    "dir": r"$cdir\.bigqmt_cache",
    "fallback_rpc": False,
    "format": "auto",
}
"@
Set-Content "$cdir\bigqmt_signal_trader_client_config.py" $clientCfg -Encoding UTF8
Ok "client config + qmt_cli.py in $cdir"

# ---- 7. optional: strategy folder -----------------------------------------
if ($WithStrategy) {
    Step "7/7 strategy folder"
    if (-not $StrategySrc -or -not (Test-Path $StrategySrc)) { throw "-WithStrategy needs -StrategySrc <dir>" }
    Copy-Item "$StrategySrc\*" "$WorkDir\my_code" -Recurse -Force
    Ok "strategy copied to $WorkDir\my_code (check order_config.json account_num)"
}

# ---- done: manual steps ----------------------------------------------------
Write-Host ""
Write-Host "================ DEPLOYMENT DONE - manual steps left ================" -ForegroundColor Yellow
Write-Host @"
1. In QMT: model trading -> add strategy -> load
   $dst\BIGQMT_REDIS_DRYRUN.py
   set run mode to LIVE (实盘), start it.
   Output panel must show: [bigqmt_rpc] started channel=bigqmt:rpc:req:$Account
2. Verify from outside:
   & "$vpy" "$cdir\qmt_cli.py" ping
   & "$vpy" "$cdir\qmt_cli.py" asset
3. redis password saved at: $pwFile
4. Memory budget on 2C4G: QMT ~1.5G + redis <=192M + python ~400M. Close QMT panels you do not use.
"@ -ForegroundColor Yellow
if ($AllowOrders) {
    Write-Host "5. order RPCs: ENABLED (-AllowOrders). buy/sell/cancel will work." -ForegroundColor Yellow
} else {
    Write-Host "5. order RPCs: OFF (default). buy/sell/cancel answer ORDER_DISABLED; re-run with -AllowOrders or set rpc_allow_order_methods=True in $dst\bigqmt_signal_trader_local_config.py and restart the strategy." -ForegroundColor Yellow
}
