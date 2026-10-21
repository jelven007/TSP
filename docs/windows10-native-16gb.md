# Windows 10 22H2 x64 / 16 GB 单机管理员版本

Windows 原生版本暂停多租户，保留完整应用与管理员个人空间。默认用户名为
`admin`，首次密码为 `Admin123`。本说明同时适用于源码运行和 Windows 冻结桌面包。
Linux / Docker 多租户模式以及 macOS、Linux 桌面包的免登录行为保持原样。
版本沿用分支基线，集成到远程 main 后再由 CI 递增。

## 账户与个人空间

- 首次启动创建 `DATA_DIR/user_data/local_admin.json`，只存储加盐密码哈希。
  该文件已存在时不会覆盖账号、重置密码或重新生成个人空间身份。
- 登录后可在「设置 → 账户」查看管理员角色、个人空间，修改密码或退出登录。
  改密会撤销该账号全部会话；重启后仍使用新密码。
- 关闭注册、邮箱找回、旧账户迁移与租户切换；后端同样拒绝相关写接口。
  `AUTH_EMAIL`、`AUTH_PASSWORD`、`AUTH_REGISTRATION_SECRET` 不覆盖本地管理员。
- 单机已有自选、策略、回测、监控和设置继续使用原 `DATA_DIR` 下的本地存储。
  个人空间是管理员使用完整业务功能的本地空间，不创建 PostgreSQL 租户。
- 旧 `user_data/auth.json` 保留不动。账号文件损坏时启动失败，须恢复备份，
  不会自动回退初始密码。备份及回滚应包含整个 `data/`。

默认密码用于首次进入；开始保存个人数据前，在账户页面修改密码。
源码服务脚本和桌面版均默认只监听本机。

## 启动与升级

使用原生 Python 3.11+、Node/pnpm 和 rustdx 构建所需 Rust 工具。
按仓库安装流程完成 `uv sync --extra dev --extra backtest --frozen` 与
`pnpm install --frozen-lockfile`、`pnpm build`。

源码服务示例（在仓库根目录运行，路径按实际安装调整）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\tsp-service.ps1 `
  -RepoRoot E:\Projects\TSP -DataDir E:\TSPData -ResourceProfile native16gb
```

打开 `http://127.0.0.1:3030`。省略 `DataDir` 时使用 `<RepoRoot>/data`。
脚本从仓库 `.env` 读取业务配置，强制 `SERVICE_ROLE=standalone`、
`LOCAL_ADMIN_MODE=true`、`MULTITENANCY_ENABLED=false`，运行一个 uvicorn worker
并托管完整 `frontend/dist`。不需要 PostgreSQL、多租户 Broker 或四类常驻 worker。
独立启动 API、行情同步、监控、通知和定时任务使用既有单机调用链；
研究任务沿用单机回测、优化、walk-forward、挖掘进程。

Windows 桌面安装包双击启动，使用安装目录旁的 `data/`，窗口打开后先登录。
桌面入口会在加载数值库前应用同一组 4096 MiB、线程和任务并发默认值；
启动环境中显式设置的同名变量优先，不会被覆盖。

从旧 Windows 多租户部署切换时：

1. 备份数据库、公共行情、单机数据目录、服务参数及 edge 配置。
2. 停止并禁用旧 `TSPWorkerWatchdog`（含计划任务），执行旧 worker 的
   `tsp-full.ps1 stop`；停止 `TSPApi`、`TSPPlatform` 和旧 Vite 服务。
3. 解除新单机服务对平台、PostgreSQL 的 `DependOnService` 依赖，只托管一个
   `tsp-service.ps1 -Role standalone` 进程。`-Role api` 是兼容别名；
   `-Role platform`、`tsp-full.ps1 start/restart` 均拒绝运行。
   旧 watchdog 入口只提示停用后退出，`tsp-full.ps1 stop/status` 仍可清理旧进程。
4. 为单机选择可写数据目录。禁止将 `public_market/current` 或 release 目录直接
   作为写目录；脚本拒绝 `PublicMarket` 下的路径。保留旧数据库和公共快照，
   不自动导入、合并或删除租户私有数据。需要的旧租户资产须单独导出后导入。
5. 启动新服务，检查登录、个人空间、数据源能力与 `/health/ready`，
   再验证真实业务任务。使用 edge 时确认其 upstream 指向新单机服务。

配置层的 `LOCAL_ADMIN_MODE` 在原生 Windows 强制开启。若旧 `.env` 保留
`SERVICE_ROLE=platform/api/gateway`，直接运行 Python 会明确报错；先完成停写切换，
再改成 `standalone`。仅有旧 `MULTITENANCY_ENABLED=true` 时会被单机模式强制关闭。
WSL2 / Docker 内的 Linux 不受 Windows 平台默认值影响。

回滚时先停止单机进程，再恢复旧服务配置、对应数据库、目录与路由。不得让两个
运行模式同时写同一数据目录。新管理员身份文件可随备份保留，旧版本不会读取它。

## 16 GB 资源控制

原生服务默认 `native16gb`，单进程预算提示为 **4096 MiB**：

| 项目 | 设置 |
| --- | --- |
| Polars / Numba | 最多 2 线程 |
| BLAS / OpenMP / MKL / NumExpr | 1 线程 |
| 策略批量执行 | 1 worker |
| Polars collect | 前台总计 2，后台最多 1 |
| 小内存 DuckDB | 最多 2 线程，单连接缓冲上限为有效预算的 1/8，最多 512 MiB |
| 回测面板缓存 | 字节 LRU / TTL，单缓存最多 256 MiB，超大面板不驻留 |
| 历史指标 | 按有效预算分批，不截断日期、标的或字段 |

有效预算取显式值、物理内存与适用 cgroup 限制的最小值。预算不等于 Windows
Job Object 硬限制，也不是整棵进程树 RSS 保证；多个连接、任务进程和临时矩阵仍可
增加内存。16 GB 机器应避免同时提交多个重计算任务，并保留系统分页文件和 SSD 空间。
`-ResourceProfile existing` 保留已有资源环境配置。

既有 Provider、金融口径、Parquet 契约、磁盘矩阵缓存、generation 和 SSE 流程不变。
此次为 L1 启动配置及 L3 核心账号/缓存调整，未新增平行业务引擎。
单机模式不使用多租户 Compute，原生租户沙箱的实现缺口留待恢复多租户时处理。

此前合成缓存基准（macOS arm64，Python 3.11.17，Polars 1.44.1）：
固定 1536 MiB 预算、2 线程、三个 `1,000,000 × 16` 面板，各运行三次。
缓存持有数据中位数从 256,000,000 降至 128,000,000 字节；
峰值 RSS 从 633,815,040 降至 615,497,728 字节；
耗时从 0.2353 增至 0.2508 秒，六次校验和一致。
这只反映该缓存场景，不代表 Windows 整机内存下降 50% 或吞吐提升。

## 验证与边界

后端定向入口：

```powershell
cd backend
uv run --frozen --no-sync pytest tests/test_local_admin.py tests/test_auth_accounts.py tests/test_auth_env_bootstrap.py tests/test_auth_runtime_mode.py tests/test_desktop_packaging.py -q
$env:PYTHONPATH = (Get-Location).Path
& ..\deploy\windows\test-resources.ps1 -Python (Resolve-Path .venv\Scripts\python.exe).Path
```

前端运行 `pnpm test src/pages/LocalAdmin.test.tsx src/pages/Auth.test.tsx
src/components/TenantSelector.test.tsx src/components/AccountScopeBoundary.test.tsx
src/lib/accountSession.test.ts src/lib/auth-api.test.ts` 和 `pnpm build`。
Windows Server 2022 CI 覆盖原生资源、脚本及账号回归；桌面 release smoke 检查
默认登录、匿名访问拒绝、个人空间、退出后会话失效和完整 API 路由。
macOS / Linux 打包 smoke 继续验证免登录。

2026-10-22 整合 PR 首轮 Windows Server 2022 / PowerShell 5.1 CI 已执行，
资源子进程成功，但测试脚本将 `ConvertFrom-Json` 的数组输出再次包入 `@(...)`，
导致参数数量断言失败。调整为直接接收 JSON 数组，并在 Python 子进程内增加逐项
参数断言，覆盖空字符串、引号、中文及末尾反斜杠；以重跑终态验证修正。

本轮没有接入 Win10 22H2 真机，未执行安装包构建。
不能据本地跨平台测试或 Server 2022 CI 宣称 Windows 全功能实机验收完成。真机还需覆盖真实数据
同步、选股、回测、优化、walk-forward、挖掘、监控、通知、调度、取消和重启恢复，
并记录系统提交量、进程树峰值内存、换页及耗时。
