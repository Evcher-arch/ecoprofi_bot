# Перенос рабочего контура ПК -> VPS, сторона ПК (specs/005-vps-migration, T026, T027, T032).
# Запуск из корня проекта в Windows PowerShell 5.1+:
#   powershell -ExecutionPolicy Bypass -File deploy/migrate-pc.ps1 -Step <шаг>
# Шаги (строго по порядку):
#   check     — состояние сервисов и индексатора, ничего не меняет
#   freeze    — T026: остановить писателей и вход, db остаётся работать
#   snapshot  — T027: дамп, SHA256, количества строк и версии в out/ (не в Git)
#   finalize  — T032: только после пустого diff на VPS (migrate-vps.sh verify) — стоп db и автозапуска
#   resume    — откат до точки невозврата: вернуть сервисы ПК
# Никогда не выполняется down -v: том ecoprofi_db-data хранится не меньше 14 дней (T050).
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('check', 'freeze', 'snapshot', 'finalize', 'resume')]
    [string]$Step,
    # Разрешить freeze в окне планового прохода 07:00/19:00 МСК (не рекомендуется).
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Out = Join-Path $Root 'out'
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Invoke-Native {
    # Запуск внешней команды с проверкой кода возврата.
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Команда завершилась с кодом ${LASTEXITCODE}: $File $($Arguments -join ' ')" }
}

function Get-Msk { [DateTime]::UtcNow.AddHours(3) }

function Write-Log {
    param([string]$Text)
    $line = "$((Get-Msk).ToString('yyyy-MM-dd HH:mm:ss')) МСК  $Text"
    Write-Host $line
    New-Item -ItemType Directory -Force $Out | Out-Null
    [IO.File]::AppendAllText((Join-Path $Out 'migration-pc.log'), $line + "`n", $Utf8)
}

function Show-State {
    Invoke-Native docker @('compose', 'ps', '--all', '--format', 'table {{.Service}}\t{{.State}}\t{{.Status}}')
}

switch ($Step) {
    'check' {
        Show-State
        Write-Host "`n--- indexer status ---"
        Invoke-Native docker @('compose', '--profile', 'indexer', 'run', '--rm', 'indexer', 'python', '-m', 'ecoprofi.indexer', 'status')
    }

    'freeze' {
        $msk = Get-Msk
        $minutes = $msk.Hour * 60 + $msk.Minute
        $nearRun = ($minutes -ge 6 * 60 + 30 -and $minutes -le 8 * 60 + 30) -or ($minutes -ge 18 * 60 + 30 -and $minutes -le 20 * 60 + 30)
        if ($nearRun -and -not $Force) {
            throw "Сейчас $($msk.ToString('HH:mm')) МСК — окно планового прохода (07:00/19:00 ±30 мин, проход идёт до часа). Перенос начинать вне окна или с -Force."
        }
        Write-Host "--- indexer status до остановки ---"
        Invoke-Native docker @('compose', '--profile', 'indexer', 'run', '--rm', 'indexer', 'python', '-m', 'ecoprofi.indexer', 'status')
        Invoke-Native docker @('compose', '--profile', 'miniapp', '--profile', 'tunnel', '--profile', 'daemon',
            'stop', 'vk-adapter', 'indexer-daemon', 'bot', 'miniapp-tunnel', 'miniapp-gateway')
        Write-Log 'freeze: остановлены vk-adapter, indexer-daemon, bot, miniapp-tunnel, miniapp-gateway; db работает'
        Show-State
    }

    'snapshot' {
        $running = (& docker compose ps --status running --services) -split "`r?`n" | Where-Object { $_ }
        if ($LASTEXITCODE -ne 0) { throw 'docker compose ps не выполнен' }
        $writers = $running | Where-Object { $_ -in @('bot', 'vk-adapter', 'indexer-daemon') }
        if ($writers) { throw "Писатели ещё работают: $($writers -join ', '). Сначала -Step freeze." }
        if ($running -notcontains 'db') { throw 'Сервис db не запущен' }

        New-Item -ItemType Directory -Force $Out | Out-Null
        $dump = Join-Path $Out 'ecoprofi-migration.dump'
        Invoke-Native docker @('compose', 'exec', '-T', 'db', 'pg_dump', '-U', 'ecoprofi_owner', '-d', 'ecoprofi', '-Fc', '-f', '/tmp/ecoprofi.dump')
        Invoke-Native docker @('compose', 'cp', 'db:/tmp/ecoprofi.dump', $dump)
        Invoke-Native docker @('compose', 'exec', '-T', 'db', 'pg_restore', '--list', '/tmp/ecoprofi.dump') | Out-Null
        Invoke-Native docker @('compose', 'exec', '-T', 'db', 'rm', '-f', '/tmp/ecoprofi.dump')

        $hash = (Get-FileHash $dump -Algorithm SHA256).Hash.ToLower()
        $size = (Get-Item $dump).Length
        # Формат sha256sum: на VPS проверяется командой sha256sum -c.
        [IO.File]::WriteAllText("$dump.sha256", "$hash  ecoprofi-migration.dump`n", $Utf8)

        # Файлы сверки формируются внутри контейнера: кодировка PowerShell не портит diff на VPS.
        Invoke-Native docker @('compose', 'cp', 'deploy/table-counts.sql', 'db:/tmp/counts.sql')
        Invoke-Native docker @('compose', 'exec', '-T', 'db', 'sh', '-c',
            'psql -U ecoprofi_owner -d ecoprofi -v ON_ERROR_STOP=1 -tA -f /tmp/counts.sql > /tmp/counts.txt')
        Invoke-Native docker @('compose', 'cp', 'db:/tmp/counts.txt', (Join-Path $Out 'counts-pc.txt'))
        Invoke-Native docker @('compose', 'exec', '-T', 'db', 'sh', '-c',
            # Без двойных кавычек: Windows PowerShell 5.1 искажает их в аргументах внешних команд.
            'psql -U ecoprofi_owner -d ecoprofi -tA -c ''SELECT version()'' -c ''SELECT $$vector $$ || extversion FROM pg_extension WHERE extname = $$vector$$'' > /tmp/versions.txt')
        Invoke-Native docker @('compose', 'cp', 'db:/tmp/versions.txt', (Join-Path $Out 'versions-pc.txt'))

        Write-Log "snapshot: $dump, $size байт, SHA256 $hash"
        Write-Host "`n--- out/versions-pc.txt ---"; Get-Content (Join-Path $Out 'versions-pc.txt')
        Write-Host "`n--- out/counts-pc.txt ---"; Get-Content (Join-Path $Out 'counts-pc.txt')
        Write-Host "`nДамп содержит закрытые данные: не коммитить, не пересылать в мессенджеры."
    }

    'finalize' {
        Invoke-Native docker @('compose', 'stop', 'db')
        $ids = (& docker ps -aq --filter 'label=com.docker.compose.project=ecoprofi') -split "`r?`n" | Where-Object { $_ }
        if ($ids) { Invoke-Native docker (@('update', '--restart=no') + $ids) | Out-Null }
        Write-Log 'finalize: рабочий контур — VPS; db ПК остановлен, автозапуск снят; том ecoprofi_db-data сохранён'
        Write-Host 'Команды индексатора на ПК больше не выполнять (включая status): они продлили бы пару доступа на второй площадке.'
        Show-State
    }

    'resume' {
        Invoke-Native docker @('compose', '--profile', 'miniapp', '--profile', 'daemon', 'up', '-d')
        Write-Log 'resume: сервисы ПК возвращены, рабочий контур — ПК'
        Show-State
    }
}
