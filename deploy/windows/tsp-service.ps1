#Requires -Version 5.1
<#
.SYNOPSIS
    Entry point for the TSP single-admin Windows service (3030).

.DESCRIPTION
    NSSM runs one standalone process with the full frontend, market ingestion
    and personal workspace. PostgreSQL and multitenant workers are not used.

    Kept intentionally free of non-ASCII characters: Windows PowerShell 5.1
    decodes BOM-less scripts as the active ANSI code page.

.PARAMETER Role
    standalone -> default; api is a compatibility alias.
    platform   -> rejected; stop the old platform service before upgrading.

.PARAMETER BindHost
    Listen address. Production keeps 127.0.0.1 so only the local HTTPS
    edge can reach the application ports.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tsp-service.ps1

.NOTES
    Logs are captured by NSSM (AppStdout/AppStderr); this script only writes
    fatal startup errors to stderr.
#>
[CmdletBinding()]
param(
    [ValidateSet('standalone', 'platform', 'api')]
    [string]$Role = 'standalone',

    # Retained for existing NSSM command lines; cell secrets are no longer read.
    [string]$SecretsFile = 'E:\pg-tsp\secrets.env',
    [string]$RepoRoot = 'E:\Projects\TSP',
    [string]$PublicMarket = 'E:\pg-tsp\public_market',
    [string]$DataDir = '',
    [string]$BindHost = '127.0.0.1',

    [ValidateSet('native16gb', 'existing')]
    [string]$ResourceProfile = 'native16gb'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'tsp-resources.ps1')

function Write-Fatal {
    param([string]$Message)
    [Console]::Error.WriteLine(('{0:yyyy-MM-dd HH:mm:ss} tsp-service[{1}] FATAL {2}' -f (Get-Date), $Role, $Message))
}

try {
    if ($Role -eq 'platform') {
        throw 'Windows multitenancy is paused. Stop the old platform/workers/watchdog; run one standalone service.'
    }
    $BackendDir = Join-Path $RepoRoot 'backend'
    $Python = Join-Path $BackendDir '.venv\Scripts\python.exe'
    $EnvFile = Join-Path $RepoRoot '.env'
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "python interpreter not found: $Python"
    }

    $resources = Get-TspResourceEnvironment -Role standalone -ResourceProfile $ResourceProfile
    foreach ($key in $resources.Keys) {
        [Environment]::SetEnvironmentVariable($key, $resources[$key], 'Process')
    }

    $env:PYTHONUNBUFFERED = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'

    if ([string]::IsNullOrEmpty($DataDir)) {
        $DataDir = Join-Path $RepoRoot 'data'
    }
    $DataDir = [System.IO.Path]::GetFullPath($DataDir)
    $marketRoot = [System.IO.Path]::GetFullPath($PublicMarket).TrimEnd('\')
    if ($DataDir.Equals($marketRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $DataDir.StartsWith($marketRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'DataDir must be a writable personal directory, outside PublicMarket release snapshots.'
    }
    $env:SERVICE_ROLE = 'standalone'
    $env:LOCAL_ADMIN_MODE = 'true'
    $env:MULTITENANCY_ENABLED = 'false'
    $env:DATA_DIR = $DataDir
    $env:TICKFLOW_ENV_FILE = $EnvFile
    $env:STATIC_DIR = Join-Path $RepoRoot 'frontend\dist'
    if (-not (Test-Path -LiteralPath (Join-Path $env:STATIC_DIR 'index.html'))) {
        throw 'frontend/dist/index.html is missing; run pnpm build first'
    }
    $port = 3030

    Set-Location -LiteralPath $BackendDir

    [Console]::Out.WriteLine(('{0:yyyy-MM-dd HH:mm:ss} tsp-service[{1}] starting uvicorn on {2}:{3}' -f (Get-Date), $Role, $BindHost, $port))

    & $Python -m uvicorn app.main:app `
        --host $BindHost --port $port --workers 1 `
        --proxy-headers --forwarded-allow-ips 127.0.0.1 `
        --no-access-log
    $code = $LASTEXITCODE
    Write-Fatal "uvicorn exited with code $code"
    exit $code
}
catch {
    Write-Fatal $_.Exception.Message
    exit 1
}
