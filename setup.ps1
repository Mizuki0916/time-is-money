# 初回セットアップ。1回だけ実行すればOK。
#   powershell -ExecutionPolicy Bypass -File C:\stock-pwa\setup.ps1
#
# やること:
#   1. Python の確認と、専用の仮想環境(.venv)にライブラリを入れる
#   2. .env を（BOM無しUTF-8で）作る
#   3. 1時間おきに動くタスクをタスクスケジューラに登録する

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "=== Time is Money セットアップ ===" -ForegroundColor Cyan
Write-Host "作業フォルダ: $root`n"

# --- 1. Python -------------------------------------------------------------
try {
    $ver = (& python --version) 2>&1
    Write-Host "[1/3] $ver を使います"
} catch {
    Write-Host "Python が見つかりません。https://www.python.org/downloads/ から入れてください。" -ForegroundColor Red
    Write-Host "（インストール時に 'Add python.exe to PATH' に必ずチェックを入れてください）" -ForegroundColor Yellow
    exit 1
}

$venv = Join-Path $root '.venv'
if (-not (Test-Path $venv)) {
    Write-Host "      仮想環境を作成中..."
    & python -m venv $venv
}
$py = Join-Path $venv 'Scripts\python.exe'

Write-Host "      ライブラリを導入中（初回は1〜2分かかります）..."
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r (Join-Path $root 'scripts\requirements.txt') --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "ライブラリの導入に失敗しました" -ForegroundColor Red; exit 1 }
Write-Host "      OK" -ForegroundColor Green

# --- 2. .env ---------------------------------------------------------------
$envPath = Join-Path $root '.env'
if (Test-Path $envPath) {
    Write-Host "[2/3] .env は既にあるので触りません"
} else {
    Write-Host "[2/3] .env を作ります"
    $key = Read-Host "      Gemini API キーを貼り付けてください（後で入れる場合は空のまま Enter）"
    $body = @"
GEMINI_API_KEY=$key
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GIT_AUTO_PUSH=0
"@
    # BOM が付くと Python 側で読めなくなるので、BOM無しUTF-8で書く
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($envPath, $body, $utf8NoBom)
    Write-Host "      作成しました: $envPath" -ForegroundColor Green
}

# --- 2b. 初回だけサンプルデータを用意 ----------------------------------------
$dataDir = Join-Path $root 'docs\data'
if (-not (Test-Path (Join-Path $dataDir 'index.json'))) {
    Write-Host "      画面確認用のサンプルデータを作ります（collect.py 実行時に本物へ置き換わります）"
    & $py (Join-Path $root 'scripts\make_sample.py') | Out-Null
} else {
    Write-Host "      既存のデータがあるので、サンプルは作りません"
}

# --- 3. タスクスケジューラ --------------------------------------------------
$taskName = 'StockDigest'
Write-Host "[3/3] 定期実行タスク '$taskName' を登録します"

try {
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "      既存のタスクを置き換えます"
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\run.ps1`"" `
    -WorkingDirectory $root

# 1時間おき + ログオン時。PCが落ちていて実行できなかった分は起動後に回収する。
# -Once + -RepetitionInterval だけだと繰り返しが効かないことがあるため、
# 日次トリガーに1時間ごとの繰り返し（期間1日）を持たせる形にする。
$start = (Get-Date).Date.AddMinutes(5)
$daily = New-ScheduledTaskTrigger -Daily -At $start
$daily.Repetition = (New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
$logon = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger @($daily, $logon) -Settings $settings `
    -Description '株系YouTube4chの新着動画を要約してPWA用データを更新する' | Out-Null

    Write-Host "      登録しました（1時間おき + ログオン時）" -ForegroundColor Green
} catch {
    # タスク登録だけ失敗しても、ここまでの準備は無駄にしない
    Write-Host "      タスク登録に失敗しました: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host "      PowerShell を「管理者として実行」して setup.ps1 をやり直すと登録できます。" -ForegroundColor Yellow
    Write-Host "      自動実行なしでも .\run.ps1 を手で叩けば動きます。" -ForegroundColor Yellow
}

Write-Host "`n=== 完了 ===" -ForegroundColor Cyan
Write-Host "次にやること:"
Write-Host "  1) 字幕チェック   " -NoNewline; Write-Host "$py scripts\check_captions.py" -ForegroundColor Yellow
Write-Host "  2) 初回の収集     " -NoNewline; Write-Host ".\run.ps1 --limit 2" -ForegroundColor Yellow
Write-Host "  3) ローカル確認   " -NoNewline; Write-Host "$py -m http.server 8000 --directory docs" -ForegroundColor Yellow
Write-Host "                    → ブラウザで http://localhost:8000"
Write-Host "  4) GitHub Pages に公開（README.md の手順4を参照）"
