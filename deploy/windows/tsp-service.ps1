#Requires -Version 5.1
<#
.SYNOPSIS
    Entry point for the TSP platform (3018) and API (3030) Windows services.

.DESCRIPTION
    NSSM runs this script as the service body. It loads the cell secrets
    explicitly instead of relying on an inherited interactive environment,
    sets the role specific variables, and finally execs uvicorn in the
    repository virtualenv.

    Kept intentionally free of non-ASCII characters: Windows PowerShell 5.1
    decodes BOM-less scripts as the active ANSI code page.

.PARAMETER Role
    platform -> owns market ingestion, 3018
    api      -> tenant HTTP surface, 3030

.PARAMETER BindHost
    Listen address. Production keeps 127.0.0.1 so only the local HTTPS
    edge can reach the application ports.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tsp-service.ps1 -Role platform

.NOTES
    Logs are captured by NSSM (AppStdout/AppStderr); this script only writes
    fatal startup errors to stderr.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('platform', 'api')]
    [string]$Role,

    [string]$SecretsFile = 'E:\pg-tsp\secrets.env',
    [string]$RepoRoot = 'E:\Projects\TSP',
    [string]$PublicMarket = 'E:\pg-tsp\public_market',
    [string]$BindHost = '127.0.0.1',

    [ValidateSet('native16gb', 'existing')]
    [string]$ResourceProfile = 'native16gb'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'tsp-resources.ps1')

$BackendDir = Join-Path $RepoRoot 'backend'
$Python = Join-Path $BackendDir '.venv\Scripts\python.exe'
$EnvFile = Join-Path $RepoRoot '.env'

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

function Write-Fatal {
    param([string]$Message)
    [Console]::Error.WriteLine(('{0:yyyy-MM-dd HH:mm:ss} tsp-service[{1}] FATAL {2}' -f (Get-Date), $Role, $Message))
}

try {
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "python interpreter not found: $Python"
    }

    $secrets = Read-SecretsFile -Path $SecretsFile
    foreach ($key in $secrets.Keys) {
        [Environment]::SetEnvironmentVariable($key, $secrets[$key], 'Process')
    }
    $resources = Get-TspResourceEnvironment -Role $Role -ResourceProfile $ResourceProfile
    foreach ($key in $resources.Keys) {
        [Environment]::SetEnvironmentVariable($key, $resources[$key], 'Process')
    }

    $env:PYTHONUNBUFFERED = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'

    if ($Role -eq 'platform') {
        # The platform writes the public market snapshot and must never run
        # the tenant API lifespan.
        $env:SERVICE_ROLE = 'platform'
        $env:TSP_PUBLIC_MARKET_DIR = $PublicMarket
        $port = 3018
    }
    else {
        $env:SERVICE_ROLE = 'api'
        $env:MULTITENANCY_ENABLED = 'true'
        $env:DATA_DIR = Join-Path $PublicMarket 'current'
        $env:PLATFORM_CONFIG_DIR = Join-Path $RepoRoot 'data\user_data'
        # Production builds are served by app.main from frontend/dist.
        $env:STATIC_DIR = Join-Path $RepoRoot 'frontend\dist'
        if (-not (Test-Path -LiteralPath (Join-Path $env:STATIC_DIR 'index.html'))) {
            throw 'frontend/dist/index.html is missing; run pnpm build first'
        }
        $port = 3030
    }

    if (-not (Test-Path -LiteralPath $PublicMarket)) {
        New-Item -ItemType Directory -Path $PublicMarket -Force | Out-Null
    }

    Set-Location -LiteralPath $BackendDir

    [Console]::Out.WriteLine(('{0:yyyy-MM-dd HH:mm:ss} tsp-service[{1}] starting uvicorn on {2}:{3}' -f (Get-Date), $Role, $BindHost, $port))

    & $Python -m uvicorn app.main:app --env-file $EnvFile `
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
