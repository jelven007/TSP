#Requires -Version 5.1
<#
.SYNOPSIS
    Legacy multitenant watchdog (paused for the Windows single-admin runtime).

.DESCRIPTION
    Polls every <IntervalSeconds> and restarts a worker through
    `tsp-full.ps1 restart -Components <kind>` when either condition fails:

      * the supervisor pid recorded in <RunDir>\<kind>.pid is not alive, or
      * <RunDir>\workers\<kind>\progress.json is missing, stale
        (older than the worker's MAX_AGE) or reports healthy=false.

    A per-kind cooldown prevents restart thrashing, so a worker that crashes
    immediately does not get relaunched on every single poll; the cooldown
    expiry is logged so the failure stays visible.

.PARAMETER Once
    Run a single check-and-restart pass and exit (useful for smoke tests).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\tsp-watchdog.ps1 -Once

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\tsp-watchdog.ps1
#>
[CmdletBinding()]
param(
    [ValidateSet('research', 'realtime', 'notifications', 'scheduled')]
    [string[]]$Components = @('research', 'realtime', 'notifications', 'scheduled'),

    [int]$IntervalSeconds = 30,
    [int]$CooldownSeconds = 120,

    [string]$RunDir = 'E:\pg-tsp\run',
    [string]$LogDir = 'E:\pg-tsp\logs',
    [string]$FullScript = '',
    [string]$RepoRoot = 'E:\Projects\TSP',
    [string]$SecretsFile = 'E:\pg-tsp\secrets.env',
    [string]$PublicMarket = 'E:\pg-tsp\public_market',

    [ValidateSet('native16gb', 'existing')]
    [string]$ResourceProfile = 'native16gb',

    [switch]$Once
)

$ErrorActionPreference = 'Stop'
Write-Warning 'Windows multitenancy is paused. Disable this watchdog task; supervise the standalone service with NSSM.'
return

. (Join-Path $PSScriptRoot 'tsp-resources.ps1')

if ([string]::IsNullOrEmpty($FullScript)) {
    $FullScript = Join-Path $PSScriptRoot 'tsp-full.ps1'
}

# Kept in sync with worker_process.MAX_AGE.
$MaxAge = @{ research = 120; realtime = 420; notifications = 120; scheduled = 660 }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir 'watchdog.log'
$LastRestart = @{}

function Write-WatchdogLog {
    param([string]$Message)
    $line = ('{0} {1}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $Message)
    Write-Host $line
    Add-Content -LiteralPath $LogFile -Value $line -Encoding utf8
}

function Test-SupervisorAlive {
    param([string]$Kind)
    $pidFile = Join-Path $RunDir ("$Kind.pid")
    if (-not (Test-Path -LiteralPath $pidFile)) { return $false }
    $value = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$value)) { return $false }
    return $null -ne (Get-Process -Id $value -ErrorAction SilentlyContinue)
}

# Returns $null when healthy, otherwise a human-readable failure reason.
function Get-HeartbeatProblem {
    param([string]$Kind)
    $progressFile = Join-Path (Join-Path (Join-Path $RunDir 'workers') $Kind) 'progress.json'
    if (-not (Test-Path -LiteralPath $progressFile)) { return 'heartbeat missing' }
    $age = ((Get-Date) - (Get-Item -LiteralPath $progressFile).LastWriteTime).TotalSeconds
    if ($age -gt $MaxAge[$Kind]) {
        return ('heartbeat stale ({0:N0}s > {1}s)' -f $age, $MaxAge[$Kind])
    }
    try {
        if ((Get-Content -LiteralPath $progressFile -Raw | ConvertFrom-Json).healthy -ne $true) {
            return 'heartbeat reports unhealthy'
        }
    } catch {
        return 'heartbeat unreadable'
    }
    return $null
}

function Invoke-Restart {
    param([string]$Kind)
    $now = Get-Date
    $last = $LastRestart[$Kind]
    if ($last -and ($now - $last).TotalSeconds -lt $CooldownSeconds) {
        $wait = [math]::Ceiling($CooldownSeconds - ($now - $last).TotalSeconds)
        Write-WatchdogLog ("[hold ] {0} restart suppressed, cooldown {1}s remaining" -f $Kind, $wait)
        return
    }
    $LastRestart[$Kind] = $now
    Write-WatchdogLog ("[heal ] restarting {0}" -f $Kind)
    # Do not pipe the helper's output: the python supervisors it starts inherit
    # the pipe's write handle and never close it, so the pipeline would block
    # forever and the watchdog would stop polling. Redirect to files instead.
    $outLog = Join-Path $LogDir ("watchdog-restart-{0}.out.log" -f $Kind)
    $errLog = Join-Path $LogDir ("watchdog-restart-{0}.err.log" -f $Kind)
    try {
        $arguments = @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $FullScript,
            'restart', '-Components', $Kind, '-RunDir', $RunDir, '-LogDir', $LogDir,
            '-RepoRoot', $RepoRoot, '-SecretsFile', $SecretsFile,
            '-PublicMarket', $PublicMarket, '-ResourceProfile', $ResourceProfile
        )
        $quoted = @($arguments | ForEach-Object { ConvertTo-TspProcessArgument $_ })
        $helper = Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
            -ArgumentList $quoted
        # WaitForExit(int) waits for the process handle only. The parameterless
        # WaitForExit() would also wait for stream EOF, which never arrives
        # because the python supervisors inherit the redirected handle.
        if (-not $helper.WaitForExit(120000)) {
            Write-WatchdogLog ("[fail ] {0} restart helper timed out" -f $Kind)
            return
        }
        foreach ($line in @(Get-Content -LiteralPath $outLog -ErrorAction SilentlyContinue)) {
            Write-WatchdogLog ('        ' + $line)
        }
        foreach ($line in @(Get-Content -LiteralPath $errLog -ErrorAction SilentlyContinue)) {
            Write-WatchdogLog ('        ' + $line)
        }
        if ($null -ne $helper.ExitCode -and $helper.ExitCode -ne 0) {
            Write-WatchdogLog ("[fail ] {0} restart exit code {1}" -f $Kind, $helper.ExitCode)
        }
    } catch {
        Write-WatchdogLog ("[fail ] {0} restart threw: {1}" -f $Kind, $_.Exception.Message)
    }
}

function Invoke-CheckPass {
    foreach ($kind in $Components) {
        if (-not (Test-SupervisorAlive -Kind $kind)) {
            Write-WatchdogLog ("[down ] {0} supervisor is not running" -f $kind)
            Invoke-Restart -Kind $kind
            continue
        }
        $problem = Get-HeartbeatProblem -Kind $kind
        if ($problem) {
            Write-WatchdogLog ("[down ] {0} {1}" -f $kind, $problem)
            Invoke-Restart -Kind $kind
        }
    }
}

Write-WatchdogLog ('watchdog started: components={0} interval={1}s cooldown={2}s' -f ($Components -join ','), $IntervalSeconds, $CooldownSeconds)

if ($Once) {
    Invoke-CheckPass
    Write-WatchdogLog 'watchdog single pass complete'
    return
}

while ($true) {
    try {
        Invoke-CheckPass
    } catch {
        Write-WatchdogLog ("[fail ] check pass threw: {0}" -f $_.Exception.Message)
    }
    Start-Sleep -Seconds $IntervalSeconds
}
