<#
Запуск проекта локально: бекенд (FastAPI + поиск по КТРУ) и фронтенд (Vite/React).

  .\start.ps1                 # разработка: API :8000 + интерфейс :5173
  .\start.ps1 -Preview        # собранная версия: API :8000 + статика :8080
  .\start.ps1 -BackendOnly    # только API
  .\start.ps1 -Gpu            # считать эмбеддинги на GPU (по умолчанию CPU — стабильнее)

Логи и pid-файлы: .run/   Остановка: .\stop.ps1
Если python/pnpm не в PATH, укажите их через $env:PYTHON_BIN, $env:NODE_BIN, $env:PNPM_BIN.
#>
[CmdletBinding()]
param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 0,
    [switch]$Preview,
    [switch]$BackendOnly,
    [switch]$Gpu,
    [switch]$SkipInstall,
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $root 'backend'
$frontendDir = Join-Path $root 'frontend'
$runDir = Join-Path $root '.run'
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

function Resolve-Tool {
    param([string]$Name, [string]$Fallback)
    # Явно указанный путь важнее PATH: у системного python может не быть зависимостей проекта.
    if ($Fallback -and (Test-Path $Fallback)) { return $Fallback }
    $found = Get-Command $Name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    throw "Не найден $Name — установите его или задайте путь через переменную окружения."
}

function Test-Endpoint {
    param([string]$Url)
    try { return (Invoke-WebRequest -Uri $Url -TimeoutSec 5 -UseBasicParsing).StatusCode -eq 200 }
    catch { return $false }
}

function Wait-Endpoint {
    param([string]$Url, [string]$Title, [int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Endpoint $Url) { Write-Host "  готово: $Title -> $Url" -ForegroundColor Green; return $true }
        Start-Sleep -Seconds 2
    }
    Write-Warning "  не дождался $Title ($Url) за $Seconds с — смотрите лог в .run/"
    return $false
}

function Start-Logged {
    param([string]$Name, [string]$FilePath, [string[]]$Arguments, [string]$WorkDir)
    $stdout = Join-Path $runDir "$Name.out.log"
    $stderr = Join-Path $runDir "$Name.err.log"
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $WorkDir `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -NoNewWindow
    Set-Content -Path (Join-Path $runDir "$Name.pid") -Value $process.Id -Encoding ascii
    Write-Host "  запущен $Name (pid $($process.Id)), лог: $stdout" -ForegroundColor DarkGray
    return $process
}

Write-Host "Проект: $root" -ForegroundColor Cyan

# --- бекенд -------------------------------------------------------------------
$python = Resolve-Tool -Name 'python' -Fallback $env:PYTHON_BIN
if (Test-Endpoint "http://127.0.0.1:$BackendPort/api/health/ready") {
    Write-Host "  API уже отвечает на порту $BackendPort" -ForegroundColor Yellow
} else {
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:HF_HUB_OFFLINE = '1'
    $env:TRANSFORMERS_OFFLINE = '1'
    if (-not $Gpu) { $env:CUDA_VISIBLE_DEVICES = '-1' }
    Start-Logged -Name 'backend' -FilePath $python -WorkDir $backendDir -Arguments @(
        '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$BackendPort") | Out-Null
    Wait-Endpoint -Url "http://127.0.0.1:$BackendPort/api/health/ready" -Title 'API' -Seconds $TimeoutSeconds | Out-Null
}

if ($BackendOnly) {
    Write-Host "`nAPI: http://127.0.0.1:$BackendPort/api/docs" -ForegroundColor Cyan
    return
}

# --- фронтенд -----------------------------------------------------------------
$pnpm = Resolve-Tool -Name 'pnpm' -Fallback $env:PNPM_BIN
$pnpmPrefix = @()
if ($pnpm -like '*.mjs' -or $pnpm -like '*.js') {
    $node = Resolve-Tool -Name 'node' -Fallback $env:NODE_BIN
    # node нужен не только нам, но и самому pnpm: он ищет его в PATH у дочернего процесса.
    $nodeDir = Split-Path -Parent $node
    if ($env:PATH -notlike "*$nodeDir*") { $env:PATH = "$nodeDir;$env:PATH" }
    $pnpmPrefix = @($node, $pnpm)
} else {
    $pnpmPrefix = @($pnpm)
}
function Invoke-Pnpm {
    param([string[]]$Arguments)
    & $pnpmPrefix[0] @($pnpmPrefix[1..($pnpmPrefix.Count - 1)]) @Arguments
}

$mode = if ($Preview) { 'preview' } else { 'dev' }
if ($FrontendPort -eq 0) { $FrontendPort = if ($Preview) { 8080 } else { 5173 } }

if (-not (Test-Path (Join-Path $frontendDir 'node_modules')) -and -not $SkipInstall) {
    Write-Host '  ставлю зависимости фронтенда (pnpm install)...' -ForegroundColor DarkGray
    Push-Location $frontendDir
    try { Invoke-Pnpm @('install') } finally { Pop-Location }
}
if ($Preview) {
    Write-Host '  собираю фронтенд (pnpm build)...' -ForegroundColor DarkGray
    Push-Location $frontendDir
    try { Invoke-Pnpm @('build') } finally { Pop-Location }
}

Start-Logged -Name 'frontend' -FilePath $pnpmPrefix[0] -WorkDir $frontendDir `
    -Arguments (@($pnpmPrefix[1..($pnpmPrefix.Count - 1)]) + @($mode, '--host', '127.0.0.1', '--port', "$FrontendPort")) | Out-Null
Wait-Endpoint -Url "http://127.0.0.1:$FrontendPort/" -Title 'интерфейс' -Seconds 120 | Out-Null

Write-Host "`nОткрывайте:" -ForegroundColor Cyan
Write-Host "  интерфейс : http://127.0.0.1:$FrontendPort/"
Write-Host "  API       : http://127.0.0.1:$BackendPort/api/docs"
Write-Host "  проверка  : http://127.0.0.1:$BackendPort/api/health/ready"
Write-Host "Остановить: .\stop.ps1`n" -ForegroundColor DarkGray
