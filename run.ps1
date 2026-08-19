# タスクスケジューラから呼ばれる本体。手動で実行してもOK。
#   powershell -ExecutionPolicy Bypass -File C:\stock-pwa\run.ps1

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$logDir = Join-Path $root 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("run-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))

# venv があればそれを、無ければシステムの python を使う
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

"==== {0} ====" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') | Tee-Object -FilePath $log -Append

try {
    & $py (Join-Path $root 'scripts\collect.py') @args 2>&1 | Tee-Object -FilePath $log -Append
    $code = $LASTEXITCODE
} catch {
    $_.Exception.Message | Tee-Object -FilePath $log -Append
    $code = 1
}

# 30日より古いログは掃除する
Get-ChildItem $logDir -Filter 'run-*.log' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $code
