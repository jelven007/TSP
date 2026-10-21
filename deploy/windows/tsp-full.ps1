#Requires -Version 5.1
<#
.SYNOPSIS
    Inspect or stop legacy Windows multitenant workers.

.DESCRIPTION
    Starts, stops, restarts and reports the four Compose workers
    (research / realtime / notifications / scheduled) through the official
    entry point `python -m app.multitenancy.worker_process <kind>`.

    Every supervisor is spawned with Start-Process so it owns its own console:
    reusing or closing the launching terminal no longer kills the workers
    (see docs/windows10-full-run-plan.md, P0-4).

    The platform (3018) and API with static frontend (3030) are only probed
    for liveness. They start through tsp-service.ps1; this script does not
    restart them.

.PARAMETER Action
    start | stop | status | restart.

.PARAMETER Components
    Worker kinds to act on; defaults to all four.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\tsp-full.ps1 start

.NOTES
    Heartbeats : <RunDir>\workers\<kind>\progress.json and owner.json
    Pid files  : <RunDir>\<kind>.pid
    Logs       : <LogDir>\<kind>.out.log and <kind>.err.log
    Environment: parsed from <SecretsFile>, plus DATA_DIR=<PublicMarket>\current
                 and unbuffered UTF-8 Python output.
                `--directory` is always passed explicitly: the worker_process
                 default (/tmp/tsp-worker) is meaningless on Windows.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'status', 'restart')]
    [string]$Action = 'status',

    [ValidateSet('research', 'realtime', 'notifications', 'scheduled')]
    [string[]]$Components = @('research', 'realtime', 'notifications', 'scheduled'),

    [string]$SecretsFile = 'E:\pg-tsp\secrets.env',
    [string]$RunDir = 'E:\pg-tsp\run',
    [string]$LogDir = 'E:\pg-tsp\logs',
    [string]$PublicMarket = 'E:\pg-tsp\public_market',
    [string]$RepoRoot = 'E:\Projects\TSP',
    [string]$Python = '',

    [ValidateSet('native16gb', 'existing')]
    [string]$ResourceProfile = 'native16gb'
)

$ErrorActionPreference = 'Stop'
if ($Action -in @('start', 'restart')) {
    throw 'Windows multitenancy is paused. Use tsp-service.ps1 -Role standalone; old workers support stop/status only.'
}
. (Join-Path $PSScriptRoot 'tsp-resources.ps1')

$BackendDir = Join-Path $RepoRoot 'backend'
if ([string]::IsNullOrEmpty($Python)) {
    $Python = Join-Path $BackendDir '.venv\Scripts\python.exe'
}

# Kept in sync with worker_process.MAX_AGE.
$MaxAge = @{ research = 120; realtime = 420; notifications = 120; scheduled = 660 }

$ObservedServices = @(
    [pscustomobject]@{ Name = 'platform'; Port = 3018 }
    [pscustomobject]@{ Name = 'api';      Port = 3030 }
)

function Read-SecretsFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "secrets file not found: $Path"
    }
    $map = @{}
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $text = $line.Trim()
        if ($text.Length -eq 0 -or $text.StartsWith('#')) { continue }
        $index = $text.IndexOf('=')
        if ($index -lt 1) { continue }
        $key = $text.Substring(0, $index).Trim()
        $value = $text.Substring($index + 1).Trim()
        if ($value.Length -ge 2) {
            $first = $value[0]
            $last = $value[$value.Length - 1]
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        $map[$key] = $value
    }
    return $map
}

function Get-WorkerEnvironment {
    param([hashtable]$Secrets, [string]$Kind)
    $environment = @{}
    foreach ($key in $Secrets.Keys) { $environment[$key] = $Secrets[$key] }
    $resources = Get-TspResourceEnvironment -Role $Kind -ResourceProfile $ResourceProfile
    foreach ($key in $resources.Keys) { $environment[$key] = $resources[$key] }
    $environment['SERVICE_ROLE'] = 'api'
    $environment['MULTITENANCY_ENABLED'] = 'true'
    $environment['DATA_DIR'] = Join-Path $PublicMarket 'current'
    $environment['PLATFORM_CONFIG_DIR'] = Join-Path $RepoRoot 'data\user_data'
    $environment['PYTHONUNBUFFERED'] = '1'
    $environment['PYTHONIOENCODING'] = 'utf-8'
    $environment['PYTHONUTF8'] = '1'
    return $environment
}

# Applies the environment to this process only for the duration of $Script,
# so an interactive session is not polluted with cell credentials.
function Invoke-WithEnvironment {
    param([hashtable]$Environment, [scriptblock]$Script)
    $previous = @{}
    foreach ($key in $Environment.Keys) {
        $previous[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $Environment[$key], 'Process')
    }
    try {
        & $Script
    } finally {
        foreach ($key in $previous.Keys) {
            [Environment]::SetEnvironmentVariable($key, $previous[$key], 'Process')
        }
    }
}

function Test-PortListening {
    param([int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect('127.0.0.1', $Port)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-WorkerPaths {
    param([string]$Kind)
    return @{
        Heartbeat = Join-Path (Join-Path $RunDir 'workers') $Kind
        PidFile   = Join-Path $RunDir ("$Kind.pid")
        OutLog    = Join-Path $LogDir ("$Kind.out.log")
        ErrLog    = Join-Path $LogDir ("$Kind.err.log")
    }
}

function Get-ManagedProcess {
    param([string]$PidFile)
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $raw = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    $value = 0
    if (-not [int]::TryParse($raw, [ref]$value)) { return $null }
    return Get-Process -Id $value -ErrorAction SilentlyContinue
}

# A fresh supervisor must not adopt the previous instance's heartbeat files.
function Clear-WorkerHeartbeat {
    param([string]$Kind)
    $paths = Get-WorkerPaths -Kind $Kind
    Remove-Item -LiteralPath (Join-Path $paths.Heartbeat 'progress.json') -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $paths.Heartbeat 'owner.json') -Force -ErrorAction SilentlyContinue
}

function Start-TspWorker {
    param([string]$Kind, [hashtable]$Environment)
    $paths = Get-WorkerPaths -Kind $Kind
    $existing = Get-ManagedProcess -PidFile $paths.PidFile
    if ($existing) {
        Write-Host ("[skip ] {0} already running (pid {1})" -f $Kind, $existing.Id)
        return
    }
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "python interpreter not found: $Python"
    }
    New-Item -ItemType Directory -Force -Path $paths.Heartbeat | Out-Null
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $arguments = @('-m', 'app.multitenancy.worker_process', $Kind, '--directory', $paths.Heartbeat)
    $quoted = @($arguments | ForEach-Object { ConvertTo-TspProcessArgument $_ })
    Invoke-WithEnvironment -Environment $Environment -Script {
        $process = Start-Process -FilePath $Python -ArgumentList $quoted -WorkingDirectory $BackendDir `
            -WindowStyle Hidden -RedirectStandardOutput $paths.OutLog -RedirectStandardError $paths.ErrLog -PassThru
        Set-Content -LiteralPath $paths.PidFile -Value $process.Id -Encoding ascii
        Write-Host ("[start] {0} supervisor pid {1}" -f $Kind, $process.Id)
    }
}

function Stop-TspWorker {
    param([string]$Kind)
    $paths = Get-WorkerPaths -Kind $Kind
    $managed = Get-ManagedProcess -PidFile $paths.PidFile
    if (-not $managed) {
        Remove-Item -LiteralPath $paths.PidFile -Force -ErrorAction SilentlyContinue
        Write-Host ("[stop ] {0} not running" -f $Kind)
        return
    }
    $supervisor = $managed.Id
    # /T removes the supervised worker together with its supervisor: Windows has
    # no graceful signal, so the supervisor's own finally block never runs.
    & taskkill.exe /PID $supervisor /T /F 2>&1 | Out-Null
    Remove-Item -LiteralPath $paths.PidFile -Force -ErrorAction SilentlyContinue
    Write-Host ("[stop ] {0} pid {1}" -f $Kind, $supervisor)
}

function Get-WorkerStatusRow {
    param([string]$Kind)
    $paths = Get-WorkerPaths -Kind $Kind
    $managed = Get-ManagedProcess -PidFile $paths.PidFile
    $pidValue = 0
    $running = $false
    if ($managed) {
        $pidValue = $managed.Id
        $running = $true
    }
    $healthy = $null
    $age = $null
    $progressFile = Join-Path $paths.Heartbeat 'progress.json'
    if (Test-Path -LiteralPath $progressFile) {
        $age = [math]::Round(((Get-Date) - (Get-Item -LiteralPath $progressFile).LastWriteTime).TotalSeconds, 1)
        try {
            $healthy = (Get-Content -LiteralPath $progressFile -Raw | ConvertFrom-Json).healthy
        } catch {
            $healthy = $null
        }
    }
    return [pscustomobject]@{
        Kind     = $Kind
        Pid      = $pidValue
        Running  = $running
        Healthy  = $healthy
        AgeSec   = $age
        MaxAge   = $MaxAge[$Kind]
    }
}

function Show-WorkerStatus {
    $rows = @()
    foreach ($kind in $Components) { $rows += Get-WorkerStatusRow -Kind $kind }
    $rows | Format-Table -AutoSize
    $bad = @($rows | Where-Object {
        -not $_.Running -or $_.Healthy -ne $true -or $null -eq $_.AgeSec -or
        $_.AgeSec -lt 0 -or $_.AgeSec -gt $_.MaxAge
    })
    if ($bad.Count -gt 0) {
        Write-Host ("{0} of {1} workers are not running with a fresh heartbeat" -f $bad.Count, $rows.Count)
    } else {
        Write-Host "All workers are running with a fresh heartbeat."
    }
}

function Show-ObservedServices {
    Write-Host ''
    Write-Host 'Observed services (probe only, started outside this script):'
    foreach ($service in $ObservedServices) {
        $state = 'down'
        if (Test-PortListening -Port $service.Port) { $state = 'listening' }
        Write-Host ("  {0,-9} 127.0.0.1:{1}  {2}" -f $service.Name, $service.Port, $state)
    }
}

function Start-SelectedWorkers {
    $secrets = Read-SecretsFile -Path $SecretsFile
    foreach ($kind in $Components) {
        $environment = Get-WorkerEnvironment -Secrets $secrets -Kind $kind
        Start-TspWorker -Kind $kind -Environment $environment
    }
}

switch ($Action) {
    'status' {
        Show-WorkerStatus
        Show-ObservedServices
    }
    'start' {
        Start-SelectedWorkers
        Show-ObservedServices
    }
    'stop' {
        foreach ($kind in $Components) { Stop-TspWorker -Kind $kind }
    }
    'restart' {
        foreach ($kind in $Components) { Stop-TspWorker -Kind $kind }
        Start-Sleep -Seconds 2
        foreach ($kind in $Components) { Clear-WorkerHeartbeat -Kind $kind }
        Start-SelectedWorkers
        Show-ObservedServices
    }
}
