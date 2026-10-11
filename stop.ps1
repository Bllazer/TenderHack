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

function Stop-One {
    param([int]$ProcessId, [string]$Title)
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) { return }
    try {
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        # Вместе с процессом уходят его дочерние: uvicorn и vite запускают их сами.
        Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Write-Host "  остановлен $Title (pid $ProcessId)" -ForegroundColor Green
    } catch {
        Write-Warning "  не удалось остановить $Title (pid $ProcessId): $($_.Exception.Message)"
    }
}

Write-Host 'Останавливаю запущенное…' -ForegroundColor Cyan
if (Test-Path $runDir) {
    Get-ChildItem -Path $runDir -Filter '*.pid' | ForEach-Object {
        $value = (Get-Content $_.FullName -ErrorAction SilentlyContinue | Select-Object -First 1)
        $parsed = 0
        if ([int]::TryParse("$value".Trim(), [ref]$parsed) -and $parsed -gt 0) {
            Stop-One -ProcessId $parsed -Title $_.BaseName
        }
        Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
    }
}

foreach ($port in $Ports) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-One -ProcessId $_.OwningProcess -Title "порт $port"
    }
}
Write-Host 'Готово.' -ForegroundColor Cyan
