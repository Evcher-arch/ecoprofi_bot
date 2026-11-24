# Запускать из любого каталога: powershell -File tools/miniapp-probe/run.ps1
$ErrorActionPreference = 'Stop'
$root = Join-Path $PSScriptRoot '.'
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) { throw 'Создайте tools/miniapp-probe/.env по .env.example' }
foreach ($line in Get-Content $envFile) {
  if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
  $name, $value = $line -split '=', 2
  [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), 'Process')
}
if (-not $env:MINIAPP_APP_SECRET) { throw 'MINIAPP_APP_SECRET пуст' }
if (-not $env:MINIAPP_PUBLIC_ORIGIN.StartsWith('https://')) { throw 'MINIAPP_PUBLIC_ORIGIN должен быть HTTPS-origin' }
$python = Join-Path $root '.venv/Scripts/python.exe'
& $python -I -m uvicorn server:app --app-dir $root --host 127.0.0.1 --port 8787 --no-access-log
