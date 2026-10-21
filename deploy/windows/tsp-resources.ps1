#Requires -Version 5.1
# Resource hints for native services; these are not Windows Job Object limits.
# Dot-source this file before starting Python so native libraries see the limits.
function Get-TspResourceEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('standalone', 'platform', 'api', 'research', 'realtime', 'notifications', 'scheduled')]
        [string]$Role,

        [ValidateSet('native16gb', 'existing')]
        [string]$ResourceProfile = 'native16gb'
    )

    if ($ResourceProfile -eq 'existing') { return @{} }

    $budgets = @{
        standalone = 4096
        platform = 3072
        api = 1536
        research = 512
        realtime = 768
        notifications = 256
        scheduled = 512
    }
    return @{
        TSP_MEMORY_BUDGET_MB = [string]$budgets[$Role]
        POLARS_MAX_THREADS = '2'
        NUMBA_NUM_THREADS = '2'
        OPENBLAS_NUM_THREADS = '1'
        OMP_NUM_THREADS = '1'
        MKL_NUM_THREADS = '1'
        NUMEXPR_NUM_THREADS = '1'
        POLARS_COLLECT_PERMITS = '2'
        POLARS_COLLECT_BACKGROUND_PERMITS = '1'
        STRATEGY_RUN_ALL_WORKERS = '1'
        TSP_CELL_POOL_SIZE = '2'
        TSP_CELL_WORKER_CONCURRENCY = '1'
    }
}

# Start-Process joins ArgumentList with spaces on Windows PowerShell 5.1.
# Quote according to CommandLineToArgvW, including paths ending in backslashes.
function ConvertTo-TspProcessArgument {
    param([AllowEmptyString()][string]$Value)
    return '"' + [regex]::Replace(
        [regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1'
    ) + '"'
}
