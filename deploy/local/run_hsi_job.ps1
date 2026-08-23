<#
.SYNOPSIS
  恒指月度数据采集作业：按需拉起 OpenD -> 等端口就绪 -> 跑同步脚本 -> 收尾关掉 OpenD

.DESCRIPTION
  给 Windows 计划任务用。设计要点：
  - OpenD 由本脚本拉起并在结束时关闭；若运行前端口已通，说明是你自己开着的，
    本脚本不碰它（既不重启也不关闭）
  - 同步脚本自身有"本月已记录则跳过""交割日未到则跳过"的保护，每天跑无副作用
  - 全程写日志到 hsi_job.log，计划任务静默运行时靠它排查

.PARAMETER TestOnly
  不跑采集，只验证 OpenD 能否登录并取到恒指报价。首次配置时用。

.PARAMETER KeepOpenD
  跑完不关闭 OpenD。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File run_hsi_job.ps1 -TestOnly
  powershell -ExecutionPolicy Bypass -File run_hsi_job.ps1
#>
param(
  [switch]$TestOnly,
  [switch]$KeepOpenD,
  [string]$OpenDDir    = "$env:APPDATA\Futu_OpenD",
  [string]$ProjectDir  = "E:\AI\GitHub\atm_premium_monitor",
  [string]$Python      = "C:\Python314\python.exe",
  [int]$ApiPort        = 11111,
  [int]$WaitSeconds    = 120
)

$ErrorActionPreference = "Continue"
$LogFile = Join-Path $ProjectDir "deploy\local\hsi_job.log"

function Write-Log($msg) {
  $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
  Write-Output $line
  Add-Content -Path $LogFile -Value $line -Encoding utf8
}

function Invoke-WithTimeout($script, $extraArgs, $timeoutSec = 300) {
  # futu SDK 连不上会无限重试，必须从外面掐断，否则计划任务永远不结束
  $so = Join-Path $env:TEMP "hsi_job_out.txt"
  $se = Join-Path $env:TEMP "hsi_job_err.txt"
  Remove-Item $so, $se -ErrorAction SilentlyContinue
  $argList = @($script) + $extraArgs
  $pp = Start-Process -FilePath $Python -ArgumentList $argList -WorkingDirectory $ProjectDir `
                      -NoNewWindow -PassThru -RedirectStandardOutput $so -RedirectStandardError $se
  if (-not $pp.WaitForExit($timeoutSec * 1000)) {
    Write-Log "python 超过 $timeoutSec 秒未结束，强制结束"
    Stop-Process -Id $pp.Id -Force -ErrorAction SilentlyContinue
  }
  foreach ($f in @($so, $se)) {
    Get-Content $f -Encoding UTF8 -ErrorAction SilentlyContinue |
      Where-Object { $_ -notmatch "_connect_sync|network_manager|on_connect" } |
      ForEach-Object { Write-Log "  $_" }
  }
}

function Test-Port($port) {
  try {
    $c = New-Object Net.Sockets.TcpClient
    $c.Connect("127.0.0.1", $port); $c.Close(); return $true
  } catch { return $false }
}

Write-Log "===== 作业开始 (TestOnly=$TestOnly) ====="

# 用图形版 Futu_OpenD.exe：它靠"记住密码 + 自动登录"完成无人值守登录。
# 命令行版 open-d\windows\FutuOpenD.exe 走的是 FutuOpenD.xml 里的 login_pwd_md5，
# 而图形版并不往那里写凭据（实测该字段为空），命令行版会因登录失败关掉监听端口。
$exe = Join-Path $OpenDDir "Futu_OpenD.exe"
$startedByUs = $false
$proc = $null

if (Test-Port $ApiPort) {
  Write-Log "端口 $ApiPort 已通，复用现有 OpenD（结束时不会关闭它）"
} else {
  if (-not (Test-Path $exe)) { Write-Log "找不到 OpenD: $exe"; exit 1 }
  Write-Log "拉起 OpenD: $exe"
  $proc = Start-Process -FilePath $exe -WorkingDirectory $OpenDDir -PassThru
  $startedByUs = $true

  $ready = $false
  for ($i = 1; $i -le $WaitSeconds; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Port $ApiPort) { Write-Log "第 $i 秒：端口 $ApiPort 就绪"; $ready = $true; break }
    if ($proc.HasExited) { Write-Log "OpenD 意外退出，退出码 $($proc.ExitCode)"; break }
  }
  if (-not $ready) {
    Write-Log "等待 $WaitSeconds 秒仍未就绪，放弃本次作业"
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
    exit 1
  }
  # 端口通了不代表登录完成（自动登录要走网络），多留一点时间
  Start-Sleep -Seconds 20
}

try {
  if ($TestOnly) {
    Write-Log "验证登录：拉取恒指报价"
    Invoke-WithTimeout "deploy\local\opend_selftest.py" @()
  } else {
    Write-Log "运行同步脚本"
    Invoke-WithTimeout "deploy\local\sync_hsi.py" @("--upload")
  }
} finally {
  if ($startedByUs -and -not $KeepOpenD) {
    if ($proc -and -not $proc.HasExited) {
      Write-Log "关闭 OpenD (PID $($proc.Id))"
      Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
  }
  Write-Log "===== 作业结束 ====="
}
