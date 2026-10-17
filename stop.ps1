<#
Остановка того, что поднял start.ps1: процессы по pid-файлам в .run/
и всё, что осталось слушать порты API/интерфейса.

  .\stop.ps1
  .\stop.ps1 -Ports 8000,5173,8080
#>
param([int[]]$Ports = @(8000, 5173, 8080))

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runDir = Join-Path $root '.run'
$taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'

function Stop-One {
    param([int]$Id, [string]$Title)
    $process = Get-Process -Id $Id -ErrorAction SilentlyContinue
    if (-not $process) { return }

    if (Test-Path $taskkill) {
        # /T — вместе с дочерними процессами (uvicorn/vite часто создают их сами).
        & $taskkill /PID $Id /T /F *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  остановлен $Title (pid $Id)" -ForegroundColor Green
            return
        }
    }

    try {
        Stop-Process -Id $Id -Force -ErrorAction Stop
        Write-Host "  остановлен $Title (pid $Id)" -ForegroundColor Green
    } catch {
        Write-Warning "  не удалось остановить $Title (pid $Id): $($_.Exception.Message)"
    }
}

Write-Host 'Останавливаю запущенное…' -ForegroundColor Cyan

if (Test-Path $runDir) {
    Get-ChildItem -Path $runDir -Filter '*.pid' | ForEach-Object {
        $value = (Get-Content $_.FullName -ErrorAction SilentlyContinue | Select-Object -First 1)
        $parsed = 0
        if ([int]::TryParse("$value".Trim(), [ref]$parsed) -and $parsed -gt 0) {
            Stop-One -Id $parsed -Title $_.BaseName
        }
        Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
    }
}

foreach ($port in $Ports) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-One -Id $_.OwningProcess -Title "порт $port"
    }
}

Write-Host 'Готово.' -ForegroundColor Cyan
