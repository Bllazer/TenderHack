<#
Запуск проекта локально: бекенд (FastAPI + поиск по КТРУ) и фронтенд (Vite/React).

  .\start.ps1                 # разработка: API :8000 + интерфейс :5173
  .\start.ps1 -Preview        # собранная версия: API :8000 + статика :8080
  .\start.ps1 -BackendOnly    # только API
  .\start.ps1 -Gpu            # не скрывать GPU (torch в .venv всё равно CPU-сборка)
  .\start.ps1 -Reinstall      # пересобрать backend/.venv и переустановить фронтенд
  .\start.ps1 -SkipInstall    # ничего не скачивать, только запустить уже готовое

Первый запуск скачивает Python-зависимости в backend\.venv и, если нужно,
Node.js и pnpm в .tools\. Для этого нужен интернет — один раз.
Логи и pid-файлы: .run\   Остановка: .\stop.ps1
Если python/node/pnpm уже установлены в нестандартном месте, укажите их через
$env:PYTHON_BIN, $env:NODE_BIN, $env:PNPM_BIN.
#>
[CmdletBinding()]
param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 0,
    [switch]$Preview,
    [switch]$BackendOnly,
    [switch]$Gpu,
    [switch]$SkipInstall,
    [switch]$Reinstall,
    [int]$TimeoutSeconds = 300
)

$ErrorActionPreference = 'Stop'
# В PowerShell 5.1 прогресс-бар Invoke-WebRequest замедляет скачивание в разы.
$ProgressPreference = 'SilentlyContinue'
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
    # Старые версии .NET могут не знать Tls12 — тогда остаётся системное значение.
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $root 'backend'
$frontendDir = Join-Path $root 'frontend'
$runDir = Join-Path $root '.run'
$toolsDir = Join-Path $root '.tools'
$venvDir = Join-Path $backendDir '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$uvVersion = '0.13.1'
$fallbackNodeVersion = 'v24.22.0'
$pnpmCommand = $null

New-Item -ItemType Directory -Force -Path $runDir | Out-Null
New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null

function Write-Step {
    param([string]$Text)
    Write-Host "`n$Text" -ForegroundColor Cyan
}

function Write-Detail {
    param([string]$Text)
    Write-Host "  $Text" -ForegroundColor DarkGray
}

function Write-Ok {
    param([string]$Text)
    Write-Host "  $Text" -ForegroundColor Green
}

function Write-Note {
    param([string]$Text)
    Write-Host "  $Text" -ForegroundColor Yellow
}

function Invoke-NativeStream {
    # Запускает native-команду, показывает stdout+stderr как обычный текст и
    # возвращает код. Нужен из-за PowerShell 5.1: он превращает stderr native-
    # программ в ErrorRecord, а при ErrorActionPreference=Stop — обрывает скрипт.
    param([string]$FilePath, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $FilePath @Arguments 2>&1 | ForEach-Object { Write-Host $_ }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Show-LogTail {
    param([string]$Name)
    foreach ($stream in @('out', 'err')) {
        $path = Join-Path $runDir "$Name.$stream.log"
        if (-not (Test-Path $path)) { continue }
        $tail = Get-Content $path -Tail 25 -ErrorAction SilentlyContinue
        if (-not $tail) { continue }
        Write-Host "--- $Name.$stream.log ---" -ForegroundColor DarkGray
        foreach ($line in $tail) { Write-Host "  $line" -ForegroundColor DarkGray }
    }
}

function Get-PythonVersion {
    param([string]$Exe)
    if (-not $Exe -or -not (Test-Path $Exe)) { return $null }
    try {
        $value = & $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $value) { return $null }
        return "$value".Trim()
    } catch {
        return $null
    }
}

function Test-PythonSupported {
    param([string]$Version)
    if (-not $Version) { return $false }
    $parts = $Version.Split('.')
    if ($parts.Count -lt 2) { return $false }
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    return ($major -gt 3) -or ($major -eq 3 -and $minor -ge 12)
}

function Resolve-Python {
    $candidates = New-Object System.Collections.Generic.List[string]

    if ($env:PYTHON_BIN) { $candidates.Add($env:PYTHON_BIN) }

    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { $candidates.Add($command.Source) }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py -and $py.Source) {
        foreach ($version in @('3.13', '3.12')) {
            try {
                $path = & $py.Source "-$version" -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $path) { $candidates.Add("$path".Trim()) }
            } catch {
                # Версии может не быть — это нормально, пробуем следующую.
            }
        }
    }

    foreach ($candidate in $candidates) {
        $version = Get-PythonVersion $candidate
        if (Test-PythonSupported $version) { return (Resolve-Path $candidate).Path }
    }

    throw "Не найден Python 3.12+. Установите его или задайте путь в `$env:PYTHON_BIN."
}

function Test-BackendPython {
    param([string]$Exe)
    $version = Get-PythonVersion $Exe
    return Test-PythonSupported $version
}

function Test-BackendDeps {
    param([string]$Exe)
    if (-not (Test-Path $Exe)) { return $false }
    & $Exe -c "import fastapi, uvicorn, pydantic_settings, numpy, pymorphy3, torch, transformers" 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Resolve-Uv {
    if ($env:UV_BIN -and (Test-Path $env:UV_BIN)) { return (Resolve-Path $env:UV_BIN).Path }

    $local = Join-Path $toolsDir 'uv\uv.exe'
    if (Test-Path $local) {
        & $local --version *> $null
        if ($LASTEXITCODE -eq 0) { return $local }
    }

    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { return $command.Source }

    if ($SkipInstall) { throw "uv не найден, а -SkipInstall запрещает установку." }

    Write-Detail "скачиваю uv $uvVersion..."
    $zip = Join-Path $toolsDir "uv-$uvVersion.zip"
    $extract = Join-Path $toolsDir 'uv-extract'
    Invoke-WebRequest -Uri "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip" `
        -OutFile $zip -UseBasicParsing
    if (Test-Path $extract) { Remove-Item $extract -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $extract -Force
    $found = Get-ChildItem $extract -Filter 'uv.exe' -Recurse | Select-Object -First 1
    if (-not $found) { throw "В архиве uv не найден uv.exe." }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $local) | Out-Null
    Copy-Item $found.FullName $local -Force
    Remove-Item $zip, $extract -Recurse -Force -ErrorAction SilentlyContinue

    & $local --version *> $null
    if ($LASTEXITCODE -ne 0) { throw "Скачанный uv не запускается: $local" }
    return $local
}

function Install-BackendWithPip {
    param([string]$BasePython)
    $previous = $ErrorActionPreference
    # pip пишет прогресс в stderr; при ErrorActionPreference=Stop PowerShell 5.1
    # принимает это за ошибку и обрывает установку. Код возврата проверяем сами.
    $ErrorActionPreference = 'Continue'
    try {
        if (-not (Test-Path $venvPython)) {
            Write-Detail "создаю backend\.venv..."
            & $BasePython -m venv $venvDir
            if ($LASTEXITCODE -ne 0) { throw "Не удалось создать backend\.venv." }
        }
        & $venvPython -m pip --version *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Detail "в venv нет pip — добавляю через ensurepip..."
            & $venvPython -m ensurepip --upgrade | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "Не удалось добавить pip в backend\.venv." }
        }

        Write-Detail "ставлю backend-зависимости через pip (torch CPU, затем проект)..."
        & $venvPython -m pip install --upgrade pip --disable-pip-version-check | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "pip не смог обновиться." }
        & $venvPython -m pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.6,<3" | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Не удалось установить torch (CPU)." }
        Push-Location $backendDir
        try {
            & $venvPython -m pip install -e ".[search]" | Out-Host
            $code = $LASTEXITCODE
        } finally {
            Pop-Location
        }
        if ($code -ne 0) { throw "pip install завершился с кодом $code." }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Install-Backend {
    param([string]$BasePython)

    if ($Reinstall -and (Test-Path $venvDir)) {
        Write-Detail "удаляю старое backend\.venv..."
        Remove-Item $venvDir -Recurse -Force
    }

    $useUv = $true
    try {
        $uv = Resolve-Uv
    } catch {
        Write-Note "uv недоступен ($($_.Exception.Message)); использую pip."
        $useUv = $false
    }

    if ($useUv) {
        Write-Detail "ставлю backend-зависимости (uv sync --frozen --extra search)..."
        Push-Location $backendDir
        try {
            $code = Invoke-NativeStream -FilePath $uv -Arguments @(
                'sync', '--frozen', '--extra', 'search', '--no-dev', '--python', $BasePython)
        } finally {
            Pop-Location
        }
        if ($code -ne 0) {
            Write-Note "uv sync завершился с кодом $code; пробую pip."
            $useUv = $false
        }
    }

    if ($useUv -and -not (Test-BackendDeps $venvPython)) {
        Write-Note "uv sync прошёл, но зависимости не импортируются; пробую pip."
        $useUv = $false
    }

    if (-not $useUv) {
        Install-BackendWithPip -BasePython $BasePython
    }

    if (-not (Test-BackendDeps $venvPython)) {
        throw "Backend-зависимости не установились. Проверьте интернет и лог выше."
    }
    return $venvPython
}

function Resolve-Node {
    if ($env:NODE_BIN -and (Test-Path $env:NODE_BIN)) { return (Resolve-Path $env:NODE_BIN).Path }

    $command = Get-Command node -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { return $command.Source }

    $candidates = @(
        (Join-Path $toolsDir 'node\node.exe'),
        (Join-Path $env:ProgramFiles 'nodejs\node.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'nodejs\node.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\nodejs\node.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return (Resolve-Path $candidate).Path }
    }
    return $null
}

function Install-PortableNode {
    if ($SkipInstall) { throw "Node.js не найден, а -SkipInstall запрещает установку." }

    $version = $fallbackNodeVersion
    try {
        $index = Invoke-RestMethod 'https://nodejs.org/dist/index.json' -TimeoutSec 30
        $lts = $index | Where-Object { $_.lts -and ($_.files -contains 'win-x64-zip') } | Select-Object -First 1
        if ($lts -and $lts.version) { $version = $lts.version }
    } catch {
        Write-Note "не удалось узнать последнюю LTS Node.js, беру $version."
    }

    $zipName = "node-$version-win-x64.zip"
    $url = "https://nodejs.org/dist/$version/$zipName"
    $zip = Join-Path $toolsDir $zipName
    $extract = Join-Path $toolsDir 'node-extract'

    Write-Detail "Node.js не найден — скачиваю $version в .tools\node (около 30 МБ)..."
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing

    $expected = $null
    try {
        $sums = (Invoke-WebRequest -Uri "https://nodejs.org/dist/$version/SHASUMS256.txt" -UseBasicParsing).Content
        foreach ($line in ($sums -split "`n")) {
            if ($line -match '^([0-9a-fA-F]{64})\s+(\S+)$' -and $Matches[2] -eq $zipName) {
                $expected = $Matches[1].ToLowerInvariant()
                break
            }
        }
    } catch {
        Write-Note "не удалось скачать SHA256 для Node.js: $($_.Exception.Message)"
    }
    if ($expected) {
        $actual = (Get-FileHash -Path $zip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) {
            Remove-Item $zip -Force -ErrorAction SilentlyContinue
            throw "Контрольная сумма Node.js не совпала — скачивание отменено."
        }
    }

    if (Test-Path $extract) { Remove-Item $extract -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $extract -Force
    $inner = Get-ChildItem $extract -Directory | Select-Object -First 1
    if (-not $inner) { throw "Неожиданный архив Node.js: нет каталога с файлами." }

    $nodeDir = Join-Path $toolsDir 'node'
    if (Test-Path $nodeDir) { Remove-Item $nodeDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $nodeDir | Out-Null
    Get-ChildItem $inner.FullName -Force | Copy-Item -Destination $nodeDir -Recurse -Force
    Remove-Item $zip, $extract -Recurse -Force -ErrorAction SilentlyContinue

    $node = Join-Path $nodeDir 'node.exe'
    if (-not (Test-Path $node)) { throw "После распаковки не найден node.exe." }
    return $node
}

function Get-RequiredPnpmVersion {
    try {
        $package = Get-Content (Join-Path $frontendDir 'package.json') -Raw | ConvertFrom-Json
        if ($package.packageManager -match '^pnpm@(.+)$') { return $Matches[1] }
    } catch {
        # Ниже есть версия по умолчанию.
    }
    return '11.25.0'
}

function Resolve-Pnpm {
    if ($env:PNPM_BIN -and (Test-Path $env:PNPM_BIN)) { return (Resolve-Path $env:PNPM_BIN).Path }

    $command = Get-Command pnpm -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { return $command.Source }

    if ($env:PNPM_HOME) {
        $exe = Join-Path $env:PNPM_HOME 'pnpm.exe'
        if (Test-Path $exe) { return $exe }
    }

    $local = Join-Path $toolsDir 'pnpm\bin\pnpm.cjs'
    if (Test-Path $local) { return $local }

    return $null
}

function Install-PortablePnpm {
    param([string]$Node)
    if ($SkipInstall) { throw "pnpm не найден, а -SkipInstall запрещает установку." }

    $version = Get-RequiredPnpmVersion
    $tgz = Join-Path $toolsDir "pnpm-$version.tgz"
    $extract = Join-Path $toolsDir 'pnpm-extract'

    Write-Detail "pnpm не найден — скачиваю pnpm $version в .tools\pnpm..."
    Invoke-WebRequest -Uri "https://registry.npmjs.org/pnpm/-/pnpm-$version.tgz" -OutFile $tgz -UseBasicParsing

    $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
    if (-not $tar) { $tar = Get-Command tar -ErrorAction SilentlyContinue }
    if (-not $tar) { throw "Для распаковки pnpm нужен tar.exe (входит в Windows 10/11)." }

    if (Test-Path $extract) { Remove-Item $extract -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $extract | Out-Null
    $code = Invoke-NativeStream -FilePath $tar.Source -Arguments @('-xzf', $tgz, '-C', $extract)
    if ($code -ne 0) { throw "tar не смог распаковать pnpm." }

    $package = Join-Path $extract 'package'
    if (-not (Test-Path $package)) { throw "Неожиданный архив pnpm: нет каталога package." }

    $target = Join-Path $toolsDir 'pnpm'
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    Move-Item $package $target
    Remove-Item $tgz, $extract -Recurse -Force -ErrorAction SilentlyContinue

    $pnpm = Join-Path $target 'bin\pnpm.cjs'
    if (-not (Test-Path $pnpm)) { throw "После распаковки не найден pnpm.cjs." }

    & $Node $pnpm --version *> $null
    if ($LASTEXITCODE -ne 0) { throw "Скачанный pnpm не запускается: $pnpm" }
    return $pnpm
}

function New-PnpmCommand {
    param([string]$Node, [string]$Pnpm)
    $extension = [System.IO.Path]::GetExtension($Pnpm).ToLowerInvariant()
    if ($extension -eq '.cjs' -or $extension -eq '.js' -or $extension -eq '.mjs') {
        return [pscustomobject]@{ Node = $Node; Path = $Pnpm; Kind = 'js' }
    }
    return [pscustomobject]@{ Node = $null; Path = $Pnpm; Kind = 'native' }
}

function Get-PnpmVersionText {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($pnpmCommand.Kind -eq 'js') {
            $value = & $pnpmCommand.Node $pnpmCommand.Path '--version'
        } else {
            $value = & $pnpmCommand.Path '--version'
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { throw "pnpm не запускается: $($pnpmCommand.Path)" }
    return "$value".Trim()
}

function Invoke-Pnpm {
    param([string[]]$Arguments)
    if ($pnpmCommand.Kind -eq 'js') {
        $code = Invoke-NativeStream -FilePath $pnpmCommand.Node -Arguments (@($pnpmCommand.Path) + $Arguments)
    } else {
        $code = Invoke-NativeStream -FilePath $pnpmCommand.Path -Arguments $Arguments
    }
    if ($code -ne 0) {
        throw "pnpm $($Arguments -join ' ') завершился с кодом $code."
    }
}

function Get-PnpmLaunch {
    param([string]$Mode)
    $arguments = @($Mode, '--host', '127.0.0.1', '--port', "$FrontendPort")
    if ($pnpmCommand.Kind -eq 'js') {
        # Start-Process не умеет аккуратно квотить массив аргументов,
        # поэтому путь к pnpm.cjs квотим сами и передаём одной строкой.
        return [pscustomobject]@{
            File = $pnpmCommand.Node
            Arguments = @("`"$($pnpmCommand.Path)`"") + $arguments
        }
    }
    return [pscustomobject]@{ File = $pnpmCommand.Path; Arguments = $arguments }
}

function Get-HttpStatus {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -Uri $Url -TimeoutSec 5 -UseBasicParsing
        return [int]$response.StatusCode
    } catch {
        $response = $_.Exception.Response
        if (-not $response -and $_.Exception.InnerException) {
            $response = $_.Exception.InnerException.Response
        }
        if ($response) { return [int]$response.StatusCode }
        return $null
    }
}

function Test-Frontend {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -Uri $Url -TimeoutSec 5 -UseBasicParsing
        if ($response.StatusCode -ne 200) { return $false }
        return ($response.Content -match 'id="root"' -and
            ($response.Content -match '/src/main' -or $response.Content -match '/assets/'))
    } catch {
        return $false
    }
}

function Wait-Service {
    param(
        [string]$Url,
        [string]$Title,
        [int]$Seconds,
        $Process
    )
    $deadline = (Get-Date).AddSeconds($Seconds)
    $lastStatus = $null
    while ((Get-Date) -lt $deadline) {
        if ($Process -and $Process.HasExited) {
            Write-Warning "  $Title завершился с кодом $($Process.ExitCode) до готовности."
            return $false
        }
        $status = Get-HttpStatus $Url
        if ($status -eq 200) {
            Write-Ok "готово: $Title -> $Url"
            return $true
        }
        if ($status -eq 503) {
            Write-Warning "  $Title отвечает 503 (сервис поднялся, но не готов)."
            return $false
        }
        if ($status) { $lastStatus = $status }
        Start-Sleep -Seconds 2
    }
    Write-Warning "  не дождался $Title ($Url) за $Seconds с (последний статус: $lastStatus)."
    return $false
}

function Start-Logged {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkDir
    )
    $stdout = Join-Path $runDir "$Name.out.log"
    $stderr = Join-Path $runDir "$Name.err.log"
    # Отдельное (скрытое) окно консоли: сервисы не умрут, когда закроется
    # терминал, из которого запустили start.ps1 или start.cmd.
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $WorkDir `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -WindowStyle Hidden
    Set-Content -Path (Join-Path $runDir "$Name.pid") -Value $process.Id -Encoding ascii
    Write-Detail "запущен $Name (pid $($process.Id)), лог: $stdout"
    return $process
}

Write-Step "Проект: $root"

# --- backend ------------------------------------------------------------------
Write-Step 'Backend'
$python = Resolve-Python
Write-Detail "Python: $python ($(Get-PythonVersion $python))"

if ($Reinstall -or -not (Test-BackendPython $venvPython) -or -not (Test-BackendDeps $venvPython)) {
    if ($SkipInstall) {
        throw "Backend-окружение не готово (backend\.venv). Уберите -SkipInstall или выполните .\start.ps1 без него."
    }
    $backendPython = Install-Backend -BasePython $python
} else {
    $backendPython = $venvPython
    Write-Detail "backend-окружение готово: $venvPython"
}

$backendUrl = "http://127.0.0.1:$BackendPort/api/health/ready"
$backendStatus = Get-HttpStatus $backendUrl
if ($backendStatus -eq 200) {
    Write-Note "API уже отвечает на порту $BackendPort — использую его."
} elseif ($backendStatus -eq 503) {
    throw "На порту $BackendPort уже работает API, но он не готов (503). Остановите его через .\stop.ps1 и запустите снова."
} else {
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:HF_HUB_OFFLINE = '1'
    $env:TRANSFORMERS_OFFLINE = '1'
    if (-not $Gpu) { $env:CUDA_VISIBLE_DEVICES = '-1' }

    $backend = Start-Logged -Name 'backend' -FilePath $backendPython -WorkDir $backendDir -Arguments @(
        '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$BackendPort")
    $ready = Wait-Service -Url $backendUrl -Title 'API' -Seconds $TimeoutSeconds -Process $backend
    if (-not $ready) {
        Show-LogTail 'backend'
        throw "API не поднялся. Исправьте ошибку из лога .run\backend.*.log и запустите снова."
    }
}

if ($BackendOnly) {
    Write-Host "`nAPI: http://127.0.0.1:$BackendPort/api/docs" -ForegroundColor Cyan
    return
}

# --- frontend -----------------------------------------------------------------
Write-Step 'Frontend'

if ($FrontendPort -eq 0) { $FrontendPort = if ($Preview) { 8080 } else { 5173 } }
$mode = if ($Preview) { 'preview' } else { 'dev' }
$frontendUrl = "http://127.0.0.1:$FrontendPort/"

if (Test-Frontend $frontendUrl) {
    Write-Note "интерфейс уже отвечает на порту $FrontendPort — использую его."
} else {
    $node = Resolve-Node
    if (-not $node) {
        $node = Install-PortableNode
        Write-Ok "Node.js установлен: $node"
    } else {
        Write-Detail "Node.js: $node"
    }

    $pnpmPath = Resolve-Pnpm
    if (-not $pnpmPath) {
        $pnpmPath = Install-PortablePnpm -Node $node
        Write-Ok "pnpm установлен: $pnpmPath"
    }

    # pnpm запускает vite через дочерний процесс, который ищет node в PATH.
    # Для локального .tools\node это обязательно, иначе будет "'node' is not recognized".
    $nodeDir = Split-Path -Parent $node
    if ($env:PATH -notlike "*$nodeDir*") { $env:PATH = "$nodeDir;$env:PATH" }

    $pnpmCommand = New-PnpmCommand -Node $node -Pnpm $pnpmPath
    Write-Detail "pnpm: $(Get-PnpmVersionText)"

    if ($Reinstall -or -not (Test-Path (Join-Path $frontendDir 'node_modules'))) {
        Write-Detail 'ставлю зависимости фронтенда (pnpm install --frozen-lockfile)...'
        Push-Location $frontendDir
        try { Invoke-Pnpm @('install', '--frozen-lockfile') } finally { Pop-Location }
    }

    if ($Preview) {
        Write-Detail 'собираю фронтенд (pnpm build)...'
        Push-Location $frontendDir
        try { Invoke-Pnpm @('build') } finally { Pop-Location }
    }

    $launch = Get-PnpmLaunch -Mode $mode
    $frontend = Start-Logged -Name 'frontend' -FilePath $launch.File -WorkDir $frontendDir -Arguments $launch.Arguments
    $frontendReady = Wait-Service -Url $frontendUrl -Title 'интерфейс' -Seconds 120 -Process $frontend
    if (-not $frontendReady) {
        Show-LogTail 'frontend'
        throw "Интерфейс не поднялся. Исправьте ошибку из лога .run\frontend.*.log и запустите снова."
    }
}

Write-Host "`nОткрывайте:" -ForegroundColor Cyan
Write-Host "  интерфейс : http://127.0.0.1:$FrontendPort/"
Write-Host "  API       : http://127.0.0.1:$BackendPort/api/docs"
Write-Host "  проверка  : http://127.0.0.1:$BackendPort/api/health/ready"
Write-Host "Остановить: .\stop.ps1`n" -ForegroundColor DarkGray
