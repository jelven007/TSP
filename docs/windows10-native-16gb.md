# Windows 10 22H2 x64 / 16 GB 原生资源优化

本轮以原生 Windows 为目标，保留完整应用、数据源、研究、监控、通知和租户契约。
不以关闭功能、缩短研究数据窗口或跳过 Compute 健康检查作为验收手段。
开发基线为 `04b374d0`，版本号沿用分支基线，不提前递增。
变更分级为 L1 启动资源配置与 L3 核心缓存/批次修改；已有扩展插槽不能表达
内存预算，直接在原调用链接入，不新增业务引擎。

**当前状态：资源优化代码已实现；原生完整多租户验收仍被隔离计算后端缺失阻断。**
本轮没有 Windows 10 真机连接，也未运行 Windows CI。macOS 测试和 Windows Server
CI 均不能替代 Windows 10 的全功能验收。历史 Windows 文档中的“不可能实现”
应理解为旧实现的限制，不能作为操作系统能力结论。

## 已实现的资源控制

`deploy/windows/tsp-resources.ps1` 提供 `native16gb` 配置，服务脚本默认应用：

| 角色 | 进程预算提示 MiB | DuckDB 单连接上限 MiB | 面板缓存上限 MiB |
| --- | ---: | ---: | ---: |
| platform | 3072 | 384 | 256 |
| api | 1536 | 192 | 192 |
| research | 512 | 64 | 64 |
| realtime | 768 | 96 | 96 |
| notifications | 256 | 32 | 32 |
| scheduled | 512 | 64 | 64 |

六类角色的规划预算合计 **6656 MiB（6.5 GiB）**。这是批次与缓存的预算提示，
不是整棵进程树的硬内存限制；Python 对象、临时矩阵、多个仓库连接、监督进程与
第三方库均可能额外占用内存。每个缓存和数据库连接分别计费，不能把表中数字当成
整机占用保证。剩余约 9.5 GiB 还要承担 Windows、浏览器、PostgreSQL、文件缓存和
尚未落地的计算沙箱，必须实测。

- Polars、Numba 最多 2 线程；BLAS、OpenMP、MKL、NumExpr 为 1 线程。
- 小于 8 GiB 的有效进程预算下，DuckDB 最多 2 线程，缓冲上限为预算的 1/8，
  最多 512 MiB。未配置预算的大内存环境保持原 DuckDB 设置。
- 有效预算取显式 `TSP_MEMORY_BUDGET_MB`、物理内存与适用 cgroup 限制的最小值。
  原生 Windows 不需要 cgroup。非法或非正预算会明确报错。
- 小内存历史指标按目标行数分批，去除强制至少 50 个标的的下限；日期、标的和字段
  不截断，全量重建仍沿用暂存、校验和发布流程。
- 回测面板缓存增加字节上限（最多 256 MiB），保留 LRU、TTL 和同键请求共享。
  超大面板完整返回但不驻留；命中率可能降低，后续请求需要重算。
- 每个 API/worker 的控制库、业务库连接池各最多 2 条；单 API 加四类 worker
  的池上界为 `5 × 2 × 2 = 20`。通知监听、运维和其他独立连接另计。
- 四类 worker 每类并发为 1，策略批量执行保持串行；各 worker 仍独立运行，
  此设置不能替代 Compute 的全局容量闸。
- API 明确 `--workers 1`，托管完整 `frontend/dist`；静态产物缺失时启动报错。
  生产无需 Vite 常驻，前端构建与后端安装仍按原部署流程执行。
- 看门狗把资源配置和自定义路径继续传给重启脚本，支持含空格的进程参数；
  worker 显式设置 `SERVICE_ROLE=api`、多租户开关和平台配置目录。

这些修改不迁移数据库、不改金融字段和 API 成功响应，不切换 Provider。涉及缓存为
回测面板 LRU；指标批次与 DuckDB 缓冲只改变计算资源。磁盘矩阵缓存、数据
generation、SSE 和前端查询失效契约沿用原实现。

## 使用与回滚

在停止旧应用进程、备份配置并完成静态构建后，以当前受控服务账户运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\tsp-service.ps1 -Role platform -ResourceProfile native16gb
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\tsp-service.ps1 -Role api -ResourceProfile native16gb
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\tsp-full.ps1 start -ResourceProfile native16gb
```

路径参数继续接受 `RepoRoot`、`SecretsFile`、`PublicMarket`，worker/watchdog
还接受 `RunDir`、`LogDir`。三个服务脚本及看门狗必须使用同一配置。脚本在 secrets
导入后覆盖资源变量，避免旧大机器的并发配置悄悄覆盖本轮预算；身份、DSN 和
schema 版本仍从现有 secrets 读取。

如需恢复手工资源参数，改用 `-ResourceProfile existing` 并重启对应进程。
它保留 secrets/环境中的资源配置；若其中设置过 `TSP_MEMORY_BUDGET_MB`，
需要同时调整。代码回滚使用前一提交并重建依赖，不需要修改数据或 schema。
本轮未操作目标机器服务或凭据。

## 原生计算后端的阻断项

现有 `ComputeExecutor → ComputeBroker → compute_worker` 使用 Unix socket、
Linux 容器、容器路径和 POSIX 管道轮询。目标 Python 缺少 `AF_UNIX` 时，
现在会返回明确 503，不再泄漏为 `AttributeError`；这不代表计算功能已补齐。

完整原生实现还必须完成并在 Windows 10 验证：

1. 经过身份校验的本地 IPC；不把未认证 TCP 端口当成租户边界。
2. Windows 10 支持的 AppContainer 等无网络、无凭据隔离，公共行情只读、
   每任务私有目录、可信运行时代码与 DLL 访问控制。
3. Job Object 的内存、进程数、子进程树约束和 kill-on-close；
   Job Object 本身不能替代文件、网络和凭据隔离。
4. 取消、断连、超时、broker 重启后先回收全部子进程再释放任务许可；
   全局单重任务和每租户容量限制。
5. 移除 worker 对 `/market`、`/tmp` 和特权符号链接的依赖，同时保留全部计算种类、
   私有因子/信号注册表和固定行情版本。
6. 所有业务计算种类、恶意文件访问/网络访问、资源耗尽和清理路径的真机回归。

本轮没有提交未经目标平台验证的沙箱实现，没有用普通子进程替换安全边界。
完整原生验收仍不通过；`compute_broker=error` 不能列为通过条件。

## 验证入口

资源与 Windows PowerShell 5.1 的路径传递检查：

```powershell
cd backend
uv sync --extra dev --frozen
$env:PYTHONPATH = (Get-Location).Path
& ..\deploy\windows\test-resources.ps1 -Python (Resolve-Path .venv\Scripts\python.exe).Path
uv run --frozen --no-sync pytest tests/test_resource_budget.py tests/test_datastore_memory_budget.py tests/test_compute_unavailable.py tests/backtest/test_panel_cache_budget.py tests/test_enriched_full_rebuild.py tests/test_enriched_incremental_memory.py tests/test_enriched_refresh_memory.py tests/test_pipeline_capacity.py tests/test_multitenancy_worker_process.py tests/backtest/test_worker_process.py tests/test_full_schedules.py -q
```

`windows-native.yml` 提供上述 Windows Server 2022 回归；它只覆盖资源与已有原生
兼容逻辑，不提供 Win10、沙箱隔离或全功能认证。

同规模缓存基准：

```text
python scripts/benchmark_memory_budget.py --before <旧工作树/backend> --after <新工作树/backend>
```

同一解释器交替运行前后版本各三次，固定 `1536 MiB` 预算、2 个 Polars 线程、
3 个 `1,000,000 × 16` 数值面板，检查结果一致并采样 RSS。
这是合成缓存驻留基准，不代表全市场回测或整机峰值。

2026-10-21 在 macOS arm64、Python 3.11.17、Polars 1.44.1 的实际结果
（三个独立进程样本的中位数）：

| 指标 | 基线 | 修改后 |
| --- | ---: | ---: |
| 缓存持有的数据字节数 | 256,000,000 | 128,000,000 |
| 进程峰值 RSS 字节数 | 633,815,040 | 615,497,728 |
| 结束 RSS 字节数 | 587,530,240 | 583,221,248 |
| 三个面板总耗时秒 | 0.2353 | 0.2508 |

六次运行的校验和一致。缓存持有数据减少 50%，进程 RSS 改善较小且样本波动明显，
不能推导为整机内存降低 50%；耗时中位数增加约 6.6%，不宣称吞吐提升。
Python/native 分配器可能保留已释放的内存，字节淘汰不保证操作系统立即回收 RSS。

同日后端实际执行（`backend` 目录，`POLARS_MAX_THREADS=2 PYTHONPATH=.`）：

```text
python -m pytest tests/backtest tests/test_resource_budget.py tests/test_datastore_memory_budget.py tests/test_compute_unavailable.py tests/test_full_compute.py tests/test_enriched_full_rebuild.py tests/test_enriched_incremental_memory.py tests/test_enriched_refresh_memory.py tests/test_pipeline_capacity.py tests/test_repository_cache_dependencies.py tests/test_get_daily_cache_reuse.py tests/test_enriched_history_window_cache.py tests/test_multitenancy_worker_process.py tests/test_full_schedules.py -q -rs
```

结果 **734 passed、6 skipped、38 warnings，118.13 秒**。6 项跳过均为
`test_full_schedules.py` 缺少临时 PostgreSQL 服务的集成用例，不能计为通过。
首次运行发现新增线程上限遗漏 `os` 导入，修正后以上最终回归无失败。
DuckDB 线程限制与缺少 `AF_UNIX` 的测试均先复现原失败，再验证修复。

新增 Python 文件及 executor 的 Ruff 检查通过；修改的历史核心文件 Ruff 告警
与基线同为 162 条，无新增告警。工作流 YAML 解析、Windows 脚本 ASCII 编码、
文档链接、`git diff --check` 和版本一致性检查通过。未修改前端源码，
本轮未执行前端构建、Windows PowerShell 运行或 Windows CI。

Windows 10 最终还需记录真实数据版本、数据量、空闲/并发任务时 RSS、系统提交量、
换页和耗时；覆盖完整 UI、新用户与租户隔离、行情同步、选股、回测、优化、
walk-forward、挖掘、监控、通知、调度、取消和重启恢复。必须等原生 Compute
实现与验收完成后，才能宣称“全部功能可用”。
