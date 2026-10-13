param(
    [string]$RoomId,
    [switch]$Health
)
$ErrorActionPreference = 'Stop'
$workerDirectory = $PSScriptRoot
$configPath = Join-Path $workerDirectory '.env.local'
if (-not (Test-Path -LiteralPath $configPath)) {
    throw 'Create services/ai-worker/.env.local from .env.example first.'
}
foreach ($configLine in Get-Content -LiteralPath $configPath) {
    if ($configLine -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
$pythonPath = Join-Path $workerDirectory '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Install the worker first: uv sync --extra rtc (in services/ai-worker).'
}
Push-Location $workerDirectory
try {
    if ($Health) {
        & $pythonPath -m nastya_worker --health
    } elseif ($RoomId -match '^nastya_[0-9a-f]{32}$') {
        & $pythonPath -m nastya_worker --room $RoomId
    } elseif ($RoomId) {
        throw 'Pass -RoomId nastya_<32 hex characters> or -Health.'
    } else {
        & $pythonPath -m nastya_worker --watch-rooms
    }
    if ($LASTEXITCODE -ne 0) { throw 'Worker exited unsuccessfully.' }
} finally {
    Pop-Location
}
