# TSP - pull latest code and restart backend + frontend (Windows / PowerShell)
#
# Usage:
#   .\update.ps1
#
# Equivalent to: git pull --ff-only  then  .\dev.ps1
# dev.ps1 already frees the backend/frontend ports, so any running instance is replaced.

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Log-Info($m) { Write-Host "[update] $m" -ForegroundColor DarkGray }
function Log-Ok  ($m) { Write-Host "[update] $m" -ForegroundColor Green }
function Log-Err ($m) { Write-Host "[update] $m" -ForegroundColor Red }

# git may not be on PATH on a fresh Windows install; fall back to common paths.
$git = 'git'
if (-not (Get-Command $git -ErrorAction SilentlyContinue)) {
    $candidates = @(
        "$env:ProgramFiles\Git\cmd\git.exe",
        "${env:ProgramFiles(x86)}\Git\cmd\git.exe",
        "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe"
    ) | Where-Object { $_ -and (Test-Path $_) }
    $git = $candidates | Select-Object -First 1
    if (-not $git) { Log-Err 'git not found; install Git or add it to PATH'; exit 1 }
}

Push-Location $Root
try {
    Log-Info 'fetching latest...'
    & $git fetch origin --prune

    $branch = (& $git rev-parse --abbrev-ref HEAD).Trim()
    $behind = (& $git rev-list --count "HEAD..origin/$branch").Trim()
    if ([int]$behind -gt 0) {
        Log-Info "$behind new commit(s) on origin/$branch, fast-forwarding..."
        & $git pull --ff-only origin $branch
        if ($LASTEXITCODE -ne 0) {
            Log-Err 'git pull failed (local changes or diverged history); resolve then retry'
            exit 1
        }
        Log-Ok "updated $branch"
    } else {
        Log-Ok 'already up to date'
    }
} finally {
    Pop-Location
}

Log-Info 'restarting backend + frontend...'
& (Join-Path $Root 'dev.ps1')
