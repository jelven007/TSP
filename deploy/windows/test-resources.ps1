#Requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Python)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'tsp-resources.ps1')

foreach ($path in (Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.ps1')) {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $path.FullName, [ref]$tokens, [ref]$parseErrors
    ) | Out-Null
    if ($parseErrors.Count -gt 0) { throw ($parseErrors | Out-String) }
}

$previous = @{}
$testDir = Join-Path ([System.IO.Path]::GetTempPath()) ('tsp resource ' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $testDir | Out-Null
try {
    $source = @'
import json, os, sys, tempfile
from pathlib import Path
from app.config import Settings
from app.cell.config import CellSettings
from app.resource_budget import effective_memory_bytes
from app.tickflow.repository import DataStore

settings = Settings(_env_file=None)
cell = CellSettings(id="test", control_dsn="unused", data_dsn="unused")
assert cell.pool_size == 2 and cell.worker_concurrency == 1
assert settings.polars_collect_permits == 2
assert settings.polars_collect_background_permits == 1
assert settings.strategy_run_all_workers == 1
budget = int(os.environ["TSP_MEMORY_BUDGET_MB"]) * 1024**2
assert 0 < effective_memory_bytes() <= budget
with tempfile.TemporaryDirectory() as directory:
    store = DataStore(Path(directory), read_only=True)
    try:
        assert store.db.execute("select current_setting('threads')").fetchone()[0] <= 2
        assert store.db.execute("select sum(i) from range(1000) t(i)").fetchone()[0] == 499500
    finally:
        store.db.close()
expected = ["C:\\path with spaces\\", 'a"b', "", "C:\\\u6d4b\u8bd5\\"]
assert sys.argv[2:] == expected, f"process arguments changed: {sys.argv[2:]!r}"
Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding="utf-8")
'@
    $probe = Join-Path $testDir 'check resources.py'
    [System.IO.File]::WriteAllText($probe, $source)
    $output = Join-Path $testDir 'result.json'
    $unicodePath = 'C:\' + [char]0x6D4B + [char]0x8BD5 + '\'
    $samples = @('C:\path with spaces\', 'a"b', '', $unicodePath)
    foreach ($role in @('standalone', 'platform', 'api', 'research', 'realtime', 'notifications', 'scheduled')) {
        $resources = Get-TspResourceEnvironment -Role $role
        foreach ($key in $resources.Keys) {
            if (-not $previous.ContainsKey($key)) {
                $previous[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
            }
            [Environment]::SetEnvironmentVariable($key, $resources[$key], 'Process')
        }
        $arguments = @($probe, $output) + $samples
        $quoted = @($arguments | ForEach-Object { ConvertTo-TspProcessArgument $_ })
        $child = Start-Process -FilePath $Python -ArgumentList $quoted -Wait -PassThru -NoNewWindow
        if ($child.ExitCode -ne 0) { throw "resource check failed: $role" }
        # Windows PowerShell 5.1 emits the JSON array as one pipeline object.
        $actual = Get-Content -LiteralPath $output -Raw | ConvertFrom-Json
        if ($actual.Count -ne $samples.Count) { throw 'argument count mismatch' }
        for ($i = 0; $i -lt $samples.Count; $i++) {
            if ($actual[$i] -cne $samples[$i]) { throw "argument mismatch: $i" }
        }
    }
    if ((Get-TspResourceEnvironment -Role api -ResourceProfile existing).Count -ne 0) {
        throw 'existing profile must not override resource settings'
    }
    foreach ($action in @('start', 'restart')) {
        $fullScript = Join-Path $PSScriptRoot 'tsp-full.ps1'
        $argsList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $fullScript, $action)
        $quoted = @($argsList | ForEach-Object { ConvertTo-TspProcessArgument $_ })
        $errorLog = Join-Path $testDir "$action.err"
        $child = Start-Process powershell.exe -ArgumentList $quoted -Wait -PassThru -NoNewWindow -RedirectStandardError $errorLog
        if ($child.ExitCode -eq 0 -or (Get-Content $errorLog -Raw) -notmatch 'multitenancy is paused') {
            throw "legacy worker action was not rejected: $action"
        }
    }
    $serviceScript = Join-Path $PSScriptRoot 'tsp-service.ps1'
    $argsList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $serviceScript, '-Role', 'platform')
    $quoted = @($argsList | ForEach-Object { ConvertTo-TspProcessArgument $_ })
    $errorLog = Join-Path $testDir 'platform.err'
    $child = Start-Process powershell.exe -ArgumentList $quoted -Wait -PassThru -NoNewWindow -RedirectStandardError $errorLog
    if ($child.ExitCode -eq 0 -or (Get-Content $errorLog -Raw) -notmatch 'multitenancy is paused') {
        throw 'legacy platform service was not rejected'
    }
    Write-Output 'Native resource profiles, SQL and process argument checks passed.'
} finally {
    foreach ($key in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previous[$key], 'Process')
    }
    Remove-Item -LiteralPath $testDir -Recurse -Force
}
