# TSP 生产环境构建与部署方案（Windows 10 原生）

> 适用版本：v0.4.9（schema v6，多租户 full-product）
> 文档类型：Runbook / 方案（**尚未执行**，命令与配置需按实际域名、路径替换后落地）
> 关联文档：[Windows 10 完整运行方案](./windows10-full-run-plan.md)、[部署指南](./deployment.md)、[运维手册](./operations.md)、[配置说明](./configuration.md)、[完整单机多租户部署](../deploy/full/README.md)

> **更新**：保留本页历史部署命令供参考，当前 Win10 22H2 x64 / 16 GB 资源配置、
> 实现状态和验证要求见[原生资源优化](./windows10-native-16gb.md)。
> “Compute 错误可接受”已不满足当前全功能目标；原生隔离计算后端仍为阻断项。

---

## 0. 结论与适用范围

本方案把当前「Vite 开发服务器 + 五项 Windows 服务」升级为可交付的生产形态。核心变化只有三点：

1. 前端由 `pnpm dev` 改为 **`pnpm build` 产出 `frontend/dist`**，由 `TSPApi`（3030）直接静态托管，`TSPFrontend` 退场。
2. `TSPPlatform`（3018）与 `TSPApi`（3030）**只监听 `127.0.0.1`**，对外只暴露新增的 `TSPEdge`（Caddy，80/443）。
3. 保留 `TSPPostgres`、`TSPWorkerWatchdog` 与四类 worker 的现有托管方式，不改变 worker 契约。

**明确边界**：现有原生部署缺少 Compute 传输与隔离后端，因此 `/health/ready`
返回 `ready=false`。本页部署命令不能补齐此能力，不能作为全功能验收结果。
当前原生目标需实现 Windows 隔离计算，不能用 Linux / WSL2 的验证替代。

---

## 1. 目标形态

| 组件 | 服务名 | 监听 | 对外 | 职责 |
| --- | --- | --- | --- | --- |
| PostgreSQL 16.9 | `TSPPostgres` | `127.0.0.1:5432` | 否 | 双库 `tsp_control` / `tsp_data_local` |
| 平台 | `TSPPlatform` | `127.0.0.1:3018` | 否 | 唯一行情写入者 + 公共版本发布 |
| API + 静态前端 | `TSPApi` | `127.0.0.1:3030` | 否 | 租户 HTTP 业务 + `frontend/dist` |
| HTTPS Edge | `TSPEdge`（新增） | `0.0.0.0:80/443` | **是** | TLS 终止、反向代理、缓存头 |
| Worker 看门狗 | `TSPWorkerWatchdog` | 无监听 | 否 | 4 类 worker 自愈 |
| 前端开发服务器 | `TSPFrontend` | 停用 | 否 | 保留配置用于应急回滚 |

```
浏览器 ── HTTPS:443 ──> TSPEdge (Caddy)
                          │  /api/*, /health/live, 静态与 SPA 深链
                          ▼
                    TSPApi 127.0.0.1:3030
                     ├── app.main 挂载 frontend/dist
                     ├── 租户业务 /api/*
                     └── 平台客户端 ──> TSPPlatform 127.0.0.1:3018
                                             └── 写 public_market
                    TSPWorkerWatchdog ──> 4 类 worker
```

**关键事实**（代码依据）：

- [config.py](../backend/app/config.py#L182-L183)：`static_dir` 非 frozen 时默认为 `<项目根>/frontend/dist`，可被环境变量 `STATIC_DIR` 覆盖。
- [main.py](../backend/app/main.py#L689-L708)：`static_dir` 存在时挂载 `/assets`，其余未匹配路径回退 `index.html`，且 `index.html` 带 `Cache-Control: no-store, must-revalidate`。因此**生产不需要独立前端进程**，也不要用 `vite preview` 代替。
- 前端所有请求使用同源相对路径 `/api/...`，无 `import.meta.env` 注入，构建产物与部署域名无关。
- 容器拓扑中 `deploy/full/compose.yml` 为 API 使用 4 个 uvicorn 进程；**Windows 原生建议保持 1 个进程**，原因见 §10.3。

---

## 2. 构建方案

### 2.1 固定输入

构建必须可追溯、可复现。每次生产构建记录以下输入，产物与输入一一对应：

| 输入 | 固定方式 | 记录内容 |
| --- | --- | --- |
| 源码 | 固定 commit 或 tag，工作区干净 | `git rev-parse HEAD` |
| Python 依赖 | `backend/uv.lock` | 文件 SHA-256 |
| 前端依赖 | `frontend/pnpm-lock.yaml` | 文件 SHA-256 |
| Node / pnpm | Node 20+，`pnpm@9.10.0`（`frontend/package.json` 的 `packageManager`） | `node --version`、`pnpm --version` |
| Python | `>=3.11`（当前 3.12） | `python --version` |
| uv | 与 lockfile 兼容的版本 | `uv --version` |

构建前检查：

```powershell
git -C E:\Projects\TSP status --porcelain      # 必须为空
git -C E:\Projects\TSP rev-parse HEAD
node --version                                  # >= v20
pnpm --version                                  # 9.10.0
cd E:\Projects\TSP\backend
uv --version
```

pnpm 版本对齐（仅首次或版本漂移时执行）：

```powershell
corepack enable
corepack prepare pnpm@9.10.0 --activate
pnpm --version
```

### 2.2 构建顺序与停机要求

Windows 下 `.venv` 文件被运行中的服务占用时，`uv sync` 可能因文件锁失败；`frontend/dist` 写入时也不应与正在验证的版本混淆。**构建前先停应用服务，保留 `TSPPostgres`**：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File E:\Projects\TSP\deploy\windows\tsp-full.ps1 stop
Stop-Service TSPWorkerWatchdog
Stop-Service TSPApi
Stop-Service TSPPlatform
# TSPPostgres 保持运行；TSPFrontend 在正式切流后不再需要
```

### 2.3 后端构建

> 顺序：先执行 §2.6 的测试（需要 dev 依赖），再执行本节固化生产环境。

```powershell
cd E:\Projects\TSP\backend
uv sync --frozen --no-dev
if ($LASTEXITCODE -ne 0) { throw 'uv sync failed' }

.\.venv\Scripts\python.exe -m app.plugins.rustdx.client
if ($LASTEXITCODE -ne 0) { throw 'rustdx native bridge check failed' }

.\.venv\Scripts\python.exe -c "import app.main; print('import ok')"
if ($LASTEXITCODE -ne 0) { throw 'app.main import failed' }
```

说明：

- `--frozen` 严格按 `uv.lock` 安装，不重新求解。
- `--no-dev` 排除 `[dependency-groups].dev`（pytest / ruff / mypy），生产运行时不需要。
- `tsp-rustdx-native` 是默认路径依赖（`[tool.uv.sources]`），`uv sync` 会本地构建并安装，**必须**执行桥接自检；桥接失败时前端行情能力不可用。
- 不安装 `backtest` / `desktop` / `legacy-cpu` extras，除非目标机器明确需要。

### 2.4 前端构建

```powershell
cd E:\Projects\TSP\frontend
pnpm install --frozen-lockfile
if ($LASTEXITCODE -ne 0) { throw 'pnpm install failed' }

pnpm build
if ($LASTEXITCODE -ne 0) { throw 'pnpm build failed' }
```

`pnpm build` 等价于 `tsc -b && vite build`，类型检查失败即构建失败；输出目录 `frontend/dist`，`sourcemap: false`。

构建前保留上一版产物用于回滚：

```powershell
$dist = 'E:\Projects\TSP\frontend\dist'
if (Test-Path -LiteralPath $dist) {
    Remove-Item -LiteralPath "$dist.prev" -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item -LiteralPath $dist -Destination "$dist.prev" -Recurse
}
```

### 2.5 产物清单与校验

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$out = "E:\pg-tsp\build\$stamp"
New-Item -ItemType Directory -Force -Path $out | Out-Null

git -C E:\Projects\TSP rev-parse HEAD | Set-Content "$out\commit.txt"
(Get-FileHash E:\Projects\TSP\backend\uv.lock -Algorithm SHA256).Hash |
    Set-Content "$out\uv.lock.sha256"
(Get-FileHash E:\Projects\TSP\frontend\pnpm-lock.yaml -Algorithm SHA256).Hash |
    Set-Content "$out\pnpm-lock.sha256"
(Get-FileHash E:\Projects\TSP\frontend\dist\index.html -Algorithm SHA256).Hash |
    Set-Content "$out\index.html.sha256"
Get-ChildItem E:\Projects\TSP\frontend\dist -Recurse -File |
    Get-FileHash -Algorithm SHA256 |
    Select-Object Path, Hash |
    Export-Csv "$out\dist.sha256.csv" -NoTypeInformation
```

产物定义（可直接交付/回滚的最小集合）：

| 产物 | 路径 |
| --- | --- |
| 前端静态站点 | `frontend\dist\`（含 `index.html`、`assets\*`） |
| 后端虚拟环境 | `backend\.venv\`（由 `uv sync --frozen --no-dev` 生成） |
| 构建清单 | `E:\pg-tsp\build\<stamp>\` |
| 运行配置 | `E:\Projects\TSP\.env`、`E:\pg-tsp\secrets.env`（不入库、单独备份） |

### 2.6 构建门禁

按 [CONTRIBUTING.md](../CONTRIBUTING.md) 第 9 节执行受影响测试。测试需要 dev 依赖，**必须在 `uv sync --frozen --no-dev` 之前、或在独立 CI/开发环境中执行**（生产 venv 不含 pytest）：

```powershell
cd E:\Projects\TSP\backend
uv sync --frozen                    # 含 dependency-groups.dev，仅用于测试
uv run --frozen pytest tests/test_multitenancy_worker_process.py tests/test_full_schedules.py -q
uv run --frozen ruff check app tests

cd E:\Projects\TSP
git diff --check
```

测试通过后再执行 §2.3 的 `uv sync --frozen --no-dev` 固化生产 venv。前端任何源码改动都必须通过 §2.4 的 `pnpm build`。

---

## 3. 运行配置改造

### 3.1 `deploy/windows/tsp-service.ps1`

当前脚本已默认监听 `127.0.0.1`、显式设置 `STATIC_DIR`、固定单 Uvicorn worker，
并加载 `native16gb` 资源配置（[tsp-service.ps1](../deploy/windows/tsp-service.ps1)）。
下列片段保留原生产接线说明，完整参数以脚本为准：

```powershell
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('platform', 'api')]
    [string]$Role,

    [string]$SecretsFile = 'E:\pg-tsp\secrets.env',
    [string]$RepoRoot = 'E:\Projects\TSP',
    [string]$PublicMarket = 'E:\pg-tsp\public_market',
    [string]$BindHost = '127.0.0.1'
)
```

在 `else`（api）分支补充静态目录：

```powershell
$env:SERVICE_ROLE = 'api'
$env:DATA_DIR = Join-Path $PublicMarket 'current'
$env:PLATFORM_CONFIG_DIR = Join-Path $RepoRoot 'data\user_data'
$env:STATIC_DIR = Join-Path $RepoRoot 'frontend\dist'
```

uvicorn 启动参数改为：

```powershell
& $Python -m uvicorn app.main:app --env-file $EnvFile `
    --host $BindHost --port $port `
    --proxy-headers --forwarded-allow-ips 127.0.0.1 `
    --no-access-log
```

要点：

- `--proxy-headers --forwarded-allow-ips 127.0.0.1` 只信任本机 Edge 写入的 `X-Forwarded-*`。
- `--no-access-log` 避免访问日志重复落盘；审计交给 Edge 与 NSSM 轮转日志（如需要访问日志，去掉该参数）。
- 不启用 `--reload`，不使用 `--workers > 1`（见 §10.3）。
- 脚本注释与输出保持 ASCII，避免 PowerShell 5.1 按 ANSI 代码页解码 BOM-less UTF-8 中文出错。
- NSSM 的 `AppParameters` 不需要改：`-Role platform|api` 不变，`BindHost` 使用默认值。

### 3.2 环境变量核对

生产必须确认以下变量来自 `E:\pg-tsp\secrets.env` 与 `E:\Projects\TSP\.env`，且与服务角色一致：

| 变量 | platform | api | 说明 |
| --- | --- | --- | --- |
| `SERVICE_ROLE` | `platform` | `api` | 由脚本设置 |
| `MULTITENANCY_ENABLED` | — | `true` | api 与 worker 必须开启 |
| `TSP_CELL_SCHEMA_VERSION` | — | `6` | 与迁移后的 schema 一致 |
| `TSP_CELL_ID` | — | `local`（本机） | 租户单元标识 |
| `DATA_DIR` | — | `<PublicMarket>\current` | api 只读公共版本 |
| `PLATFORM_CONFIG_DIR` | — | `<RepoRoot>\data\user_data` | 平台配置只读 |
| `TSP_PUBLIC_MARKET_DIR` | `<PublicMarket>` | — | 平台唯一写入口 |
| `TSP_CELL_MARKET_URL` | — | 指向 `http://127.0.0.1:3018` | 绑定 loopback 后必须仍是本机地址 |
| `STATIC_DIR` | — | `<RepoRoot>\frontend\dist` | 本方案新增 |

`TSP_CELL_MARKET_TOKEN` 与 `TSP_PLATFORM_MARKET_TOKEN` 必须一致；`TSP_PLATFORM_ADMIN_TOKEN` 必须不同。不要把 `.env`、`secrets.env` 放进 `frontend/dist` 或任何 Web 可读目录。

### 3.3 停用 `TSPFrontend`

正式切流后停用但不删除，保留回滚能力：

```powershell
& C:\Tools\nssm\nssm.exe stop TSPFrontend
& C:\Tools\nssm\nssm.exe set TSPFrontend Start SERVICE_DISABLED
Get-Service TSPFrontend
```

回滚时恢复：

```powershell
& C:\Tools\nssm\nssm.exe set TSPFrontend Start SERVICE_AUTO_START
Start-Service TSPFrontend
```

---

## 4. HTTPS Edge

### 4.1 选型

| 方案 | 适用 | 说明 |
| --- | --- | --- |
| **Caddy 2（推荐）** | 单机 Windows | 单文件、自动 HTTPS、SSE 免缓冲配置简单，配合 NSSM 注册服务 |
| Nginx for Windows | 需要与 Linux Edge 配置保持同构 | 需手工管理证书与重载；Windows 版生产支持弱于 Linux |

本方案以 Caddy 为例；仓库已有的 Linux Nginx Edge 资产见 [deploy/edge](../deploy/edge/README.md)，不要直接套用到 Windows。

### 4.2 安装

```powershell
New-Item -ItemType Directory -Force -Path C:\Tools\caddy | Out-Null
# 从 Caddy 官方发行包解压 caddy.exe 到 C:\Tools\caddy\caddy.exe
& C:\Tools\caddy\caddy.exe version
```

固定版本并记录 SHA-256，升级前先在非生产端口 `caddy validate`。

### 4.3 Caddyfile

保存为 `C:\Tools\caddy\Caddyfile`。**公网域名 + ACME**：

```caddyfile
{
	admin 127.0.0.1:2019
	email ops@example.com
}

tsp.example.com {
	encode zstd gzip

	@immutable path /assets/*
	header @immutable Cache-Control "public, max-age=31536000, immutable"

	@internal path /health/ready /api/health
	respond @internal 404

	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "strict-origin-when-cross-origin"
		-Server
	}

	reverse_proxy 127.0.0.1:3030 {
		flush_interval -1
		health_uri /health/live
		health_interval 30s
		health_timeout 5s
	}
}
```

**内网 IP + 本地 CA**：

```caddyfile
{
	admin 127.0.0.1:2019
	storage file_system C:/Tools/caddy/data
}

https://192.168.1.10 {
	tls internal

	encode zstd gzip

	@immutable path /assets/*
	header @immutable Cache-Control "public, max-age=31536000, immutable"

	@internal path /health/ready /api/health
	respond @internal 404

	reverse_proxy 127.0.0.1:3030 {
		flush_interval -1
		health_uri /health/live
		health_interval 30s
		health_timeout 5s
	}
}
```

配置说明：

| 规则 | 目的 |
| --- | --- |
| `flush_interval -1` | 立即冲刷响应，保证 `/api/events`、`/api/intraday/*`、`/api/mining/*` 等 SSE 不被缓冲 |
| `health_uri /health/live` | 只探测进程存活；`/health/ready` 在 Windows 恒 503，不能作为 upstream 健康判据 |
| `/assets/*` 长缓存 | 构建产物文件名带 hash，可安全 `immutable` |
| `index.html` 不额外加缓存头 | 后端已返回 `no-store`，Edge 不要覆盖 |
| 屏蔽 `/health/ready`、`/api/health` | 避免对外暴露内部依赖状态；运维从本机 3030 检查 |
| `-Server` | 去掉版本指纹 |
| 未设置请求体上限 | 如需限制上传，增加 `request_body { max_size 256MB }`，数值按实际业务确认 |

校验并热加载：

```powershell
& C:\Tools\caddy\caddy.exe validate --config C:\Tools\caddy\Caddyfile --adapter caddyfile
& C:\Tools\caddy\caddy.exe reload --config C:\Tools\caddy\Caddyfile --adapter caddyfile
```

### 4.4 证书

**公网域名（推荐）**：Caddy 自动申请与续期，要求 80/443 可达。证书与账户数据默认存放在运行账户的 `%AppData%\Caddy`；以 LocalSystem 运行时位于 `C:\Windows\System32\config\systemprofile\AppData\Roaming\Caddy`，建议在全局配置中用 `storage file_system` 固定到 `C:\Tools\caddy\data`，便于备份。

**内网 IP**：`tls internal` 生成本地 CA 与 IP SAN 证书。分发根证书：

```
C:\Tools\caddy\data\pki\authorities\local\root.crt
```

- 客户端导入「受信任的根证书颁发机构」后重启浏览器；Firefox 需单独在「证书颁发机构」中导入。
- 通过可信渠道核对 SHA-256 指纹后再分发；**绝不分发根 CA 私钥**。
- 根证书有效期长、站点证书较短，Caddy 自动轮换；叶证书轮换不要求客户端重新导入。
- 未导入根证书前浏览器会告警，不要通过关闭校验规避。

### 4.5 注册 `TSPEdge` 服务

```powershell
$Nssm  = 'C:\Tools\nssm\nssm.exe'
$Caddy = 'C:\Tools\caddy\caddy.exe'

& $Nssm install TSPEdge $Caddy
& $Nssm set TSPEdge AppParameters 'run --config C:\Tools\caddy\Caddyfile --adapter caddyfile'
& $Nssm set TSPEdge AppDirectory 'C:\Tools\caddy'
& $Nssm set TSPEdge Start SERVICE_AUTO_START
& $Nssm set TSPEdge DependOnService TSPApi
& $Nssm set TSPEdge AppExit Default Restart
& $Nssm set TSPEdge AppRestartDelay 5000
& $Nssm set TSPEdge AppThrottle 10000
& $Nssm set TSPEdge AppStdout 'E:\pg-tsp\logs\edge-service.out.log'
& $Nssm set TSPEdge AppStderr 'E:\pg-tsp\logs\edge-service.err.log'
& $Nssm set TSPEdge AppRotateFiles 1
& $Nssm set TSPEdge AppRotateOnline 1
& $Nssm set TSPEdge AppRotateBytes 10485760
& $Nssm set TSPEdge AppKillProcessTree 1
& $Nssm set TSPEdge ObjectName LocalSystem
& $Nssm set TSPEdge Description 'TSP production HTTPS edge (Caddy)'

Start-Service TSPEdge
Get-Service TSPEdge
```

### 4.6 防火墙

只放行 Edge 端口；应用端口绑定 loopback 后不再接受外部连接，防火墙规则作为第二道防线：

```powershell
New-NetFirewallRule -DisplayName 'TSP Edge HTTP' -Direction Inbound -Protocol TCP `
    -LocalPort 80 -Action Allow -Profile Domain,Private
New-NetFirewallRule -DisplayName 'TSP Edge HTTPS' -Direction Inbound -Protocol TCP `
    -LocalPort 443 -Action Allow -Profile Domain,Private
```

公网 ACME 场景需将 80 放行到 `Public` profile。**不要**为 3012/3018/3030 创建放行规则。

---

## 5. 首次上线 Runbook

### 5.1 前置检查

```powershell
Get-Service TSPPostgres, TSPPlatform, TSPApi, TSPFrontend, TSPWorkerWatchdog | Format-Table Name, Status, StartType
Get-Content E:\pg-tsp\secrets.env | ForEach-Object { ($_ -split '=')[0] } | Where-Object { $_ -and -not $_.StartsWith('#') }
Test-Path E:\Projects\TSP\.env
Test-Path E:\Projects\TSP\frontend\dist
```

确认：

- `TSPPostgres` 正在运行，双库可连接，schema 版本为 6。
- `E:\Projects\TSP\.env` 与 `E:\pg-tsp\secrets.env` 均为最小权限（仅 Administrators / SYSTEM 可读）。
- 服务器已开启开发者模式或服务账户具备创建目录符号链接的权限（`publication.py` 发布 `current` 需要）。
- 服务器时间同步正常（TLS 与行情时点依赖）。

### 5.2 备份（切流前强制）

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = "E:\pg-tsp\backup\pre-prod-$stamp"
New-Item -ItemType Directory -Force -Path $backup | Out-Null

$env:PGPASSWORD = '<control admin password>'
& E:\pg-tsp\pgsql\bin\pg_dump.exe -h 127.0.0.1 -p 5432 -U <control_admin> -Fc `
    -d tsp_control -f "$backup\tsp_control.dump"

$env:PGPASSWORD = '<data admin password>'
& E:\pg-tsp\pgsql\bin\pg_dump.exe -h 127.0.0.1 -p 5432 -U <data_admin> -Fc `
    -d tsp_data_local -f "$backup\tsp_data_local.dump"
Remove-Item Env:\PGPASSWORD

Compress-Archive -Path E:\pg-tsp\public_market -DestinationPath "$backup\public_market.zip"
Compress-Archive -Path E:\Projects\TSP\data\user_data -DestinationPath "$backup\user_data.zip"
Copy-Item E:\Projects\TSP\.env "$backup\app.env"
Copy-Item E:\pg-tsp\secrets.env "$backup\secrets.env"
```

备份目录应位于不同物理磁盘或已挂载的备份卷，并验证可解压、`pg_restore --list` 可读。

### 5.3 构建

按 §2 执行。构建期间应用服务保持停止。

### 5.4 数据库迁移 / 初始化

仅在版本升级或首次部署时执行；日常构建不需要。

```powershell
cd E:\Projects\TSP\backend
$env:TSP_CONTROL_ADMIN_DSN = 'postgresql://<control_admin>:<pwd>@127.0.0.1:5432/tsp_control'
$env:TSP_DATA_ADMIN_DSN    = 'postgresql://<data_admin>:<pwd>@127.0.0.1:5432/tsp_data_local'

.\.venv\Scripts\python.exe -m app.cell.admin migrate `
    --kind control --runtime-role <control_runtime_role> --target-version 6
.\.venv\Scripts\python.exe -m app.cell.admin migrate `
    --kind data --runtime-role <data_runtime_role> --cell-id local --target-version 6

Remove-Item Env:\TSP_CONTROL_ADMIN_DSN
Remove-Item Env:\TSP_DATA_ADMIN_DSN
```

- 迁移 CLI 会校验 schema 文件 SHA-256；不要修改 `backend/app/cell/sql/` 下文件。
- `--runtime-role` 必须是实际运行角色，不能用 owner/超级用户替代。
- 迁移为**前向**变更，回滚只能恢复 §5.2 的数据库备份。
- 首次部署还需按 [deploy/full/README.md](../deploy/full/README.md) 的「初始化与启动」执行 `bootstrap-full-identity` 与租户开通；本机已有 `admin` 账户时不要重复初始化。

### 5.5 切换

```powershell
# 1. 应用新脚本与新静态产物后，先起后端
Start-Service TSPPlatform
Start-Service TSPApi
Start-Service TSPWorkerWatchdog

# 2. 停用开发前端
& C:\Tools\nssm\nssm.exe stop TSPFrontend
& C:\Tools\nssm\nssm.exe set TSPFrontend Start SERVICE_DISABLED

# 3. 起 Edge
Start-Service TSPEdge
```

### 5.6 启停顺序

**启动**（SCM 会按 `DependOnService` 自动带起前置服务）：

```
TSPPostgres -> TSPPlatform -> TSPApi -> TSPEdge / TSPWorkerWatchdog
```

**停止**（顺序不可颠倒）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File E:\Projects\TSP\deploy\windows\tsp-full.ps1 stop
Stop-Service TSPWorkerWatchdog
Stop-Service TSPEdge
Stop-Service TSPApi
Stop-Service TSPPlatform
Stop-Service TSPPostgres
```

必须先停 worker：NSSM 的 `AppKillProcessTree` 不会清理看门狗通过 `Start-Process` 拉起的 supervisor，直接停 `TSPWorkerWatchdog` 会长期停在 `STOP_PENDING`。

### 5.7 上线后验证

```powershell
# Edge 与 TLS
curl.exe -sk -o NUL -w "%{http_code}`n" https://tsp.example.com/
curl.exe -sk -o NUL -w "%{http_code}`n" https://tsp.example.com/strategies   # SPA 深链应 200
curl.exe -sk -o NUL -w "%{http_code}`n" https://tsp.example.com/health/live # 200
curl.exe -sk -o NUL -w "%{http_code}`n" https://tsp.example.com/health/ready # 404（被 Edge 屏蔽）

# 后端真实状态（本机）
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:3030/health/live   # 200
curl.exe -s http://127.0.0.1:3030/health/ready
# 期望：workers / databases / public_files / platform / indicator_cache = ok
#       Compute 未实现时为 error、ready=false；这代表全功能验收失败

# 前端静态资源
curl.exe -skI https://tsp.example.com/assets/<实际文件名>.js | Select-String 'cache-control'
# 期望：public, max-age=31536000, immutable

# 外部不应命中应用端口
Test-NetConnection <本机内网IP> -Port 3030   # TcpTestSucceeded 应为 False
```

浏览器侧：

- 登录 `admin` 账户，确认租户选择、行情、K 线、策略页面可用。
- 打开 DevTools → Network，确认 `/api/events` 为 `text/event-stream` 且长时间保持连接、不被代理断开。
- 上传/导入、通知、定时任务各执行一次。
- 四类 worker 心跳持续刷新：`curl.exe -s http://127.0.0.1:3030/api/health`。

---

## 6. 日常运维

### 6.1 日志

| 组件 | 日志 |
| --- | --- |
| `TSPPlatform` | `E:\pg-tsp\logs\platform-service.out.log` / `.err.log` |
| `TSPApi` | `E:\pg-tsp\logs\api-service.out.log` / `.err.log` |
| `TSPEdge` | `E:\pg-tsp\logs\edge-service.out.log` / `.err.log` |
| `TSPWorkerWatchdog` | `E:\pg-tsp\logs\watchdog-service.out.log` / `.err.log` |
| Worker | `E:\pg-tsp\logs\<kind>.out.log` / `.err.log` |
| PostgreSQL | `E:\pg-tsp\logs\postgres.log` |

NSSM 均启用 `AppRotateFiles=1`、`AppRotateOnline=1`、`AppRotateBytes=10485760`。轮转文件不在备份范围，但应纳入磁盘容量监控。

### 6.2 自愈

- `TSPEdge`：`AppExit Default Restart` + 5s 延迟，绑定失败/证书错误会持续重试；重复失败时检查 `edge-service.err.log` 与 80/443 占用。
- `TSPApi` / `TSPPlatform`：同上；启动失败常见原因是 secrets 缺失、DSN 不可达、端口被占。
- 四类 worker：由 `TSPWorkerWatchdog` 每 30s 检查 PID 与 `progress.json`，冷却 120s 后重启。

### 6.3 监控

最低监控项：

| 指标 | 采集方式 | 告警阈值 |
| --- | --- | --- |
| Edge 存活 | `https://<host>/health/live` | 连续 3 次非 200 |
| API 存活 | `http://127.0.0.1:3030/health/live` | 连续 3 次非 200 |
| 深度就绪 | `http://127.0.0.1:3030/health/ready` | 除 `compute_broker` 外任一非 `ok` |
| worker 心跳 | `/api/health` 的 4 项 | 任一非 `ok` 或 `age_seconds` 接近阈值 |
| 磁盘 | `E:` 可用空间 | 低于 15% |
| 证书有效期 | Edge 证书 | 剩余 < 21 天（ACME 应已自动续期） |

### 6.4 证书轮换

- ACME：Caddy 自动续期；确认 80/443 持续可达，定期检查 `caddy` 日志中的续期结果。
- 内网 CA：Caddy 自动轮换站点证书；根证书到期前需重新签发并重新分发。
- 轮换后执行 `caddy validate` 与 `caddy reload`，并复测 `/health/live`、SSE 与登录。

---

## 7. 备份与恢复

### 7.1 备份范围

| 对象 | 路径 | 方式 |
| --- | --- | --- |
| 控制库 | `tsp_control` | `pg_dump -Fc` |
| 业务库 | `tsp_data_local` | `pg_dump -Fc` |
| 公共行情版本 | `E:\pg-tsp\public_market` | 目录归档（含 `releases/` 与 `current`） |
| 平台配置 | `E:\Projects\TSP\data\user_data` | 目录归档 |
| 运行配置 | `E:\Projects\TSP\.env`、`E:\pg-tsp\secrets.env` | 文件复制，加密保管 |
| Edge 配置与证书 | `C:\Tools\caddy\Caddyfile`、`C:\Tools\caddy\data` | 目录归档，CA 私钥单独加密 |

Linux/Docker 生产使用仓库的 `python -m scripts.backup_full_host`；**Windows 原生没有等价入口**，按本节命令执行。

### 7.2 恢复

```powershell
# 1. 停止应用（保留 PostgreSQL）
powershell -NoProfile -ExecutionPolicy Bypass -File E:\Projects\TSP\deploy\windows\tsp-full.ps1 stop
Stop-Service TSPWorkerWatchdog
Stop-Service TSPEdge
Stop-Service TSPApi
Stop-Service TSPPlatform

# 2. 恢复数据库（确认目标库可被覆盖）
$env:PGPASSWORD = '<admin password>'
& E:\pg-tsp\pgsql\bin\pg_restore.exe -h 127.0.0.1 -p 5432 -U <admin> `
    --clean --if-exists -d tsp_control "$backup\tsp_control.dump"
& E:\pg-tsp\pgsql\bin\pg_restore.exe -h 127.0.0.1 -p 5432 -U <admin> `
    --clean --if-exists -d tsp_data_local "$backup\tsp_data_local.dump"
Remove-Item Env:\PGPASSWORD

# 3. 恢复目录与配置
Expand-Archive "$backup\public_market.zip" -DestinationPath E:\pg-tsp -Force
Expand-Archive "$backup\user_data.zip" -DestinationPath E:\Projects\TSP\data -Force
Copy-Item "$backup\app.env" E:\Projects\TSP\.env -Force
Copy-Item "$backup\secrets.env" E:\pg-tsp\secrets.env -Force

# 4. 按 §5.6 启动并验证
```

恢复后必须重新核对 `current` 链接指向、文件属主与 ACL，以及 schema 版本与代码版本是否匹配。

---

## 8. 升级与回滚

### 8.1 升级流程

1. 记录当前 commit、`frontend/dist` 哈希、schema 版本。
2. 按 §5.2 做一次备份。
3. 按 §5.6 停止应用（保留 PostgreSQL）。
4. `git fetch` 后切换到目标 tag/commit；**不要**在服务运行中执行 `uv sync`。
5. 按 §2.3 / §2.4 重新构建；保留 `dist.prev`。
6. 执行 §5.4 的离线迁移（`--target-version` 按发布说明）。
7. 按 §5.6 启动，执行 §5.7 验证。

### 8.2 回滚流程

| 变更类型 | 回滚方式 |
| --- | --- |
| 仅前端 | 用 `frontend\dist.prev` 覆盖 `dist`，重启 `TSPApi` |
| 后端代码/依赖 | `git checkout <上一版本>` → `uv sync --frozen --no-dev` → 重启 |
| Edge 配置 | 恢复 `Caddyfile` 备份 → `caddy validate` → `caddy reload` |
| 数据库已迁移 | **不能降级**；停止应用，按 §7.2 恢复升级前备份，再部署旧代码 |
| 前端托管不可用 | 恢复 `TSPFrontend` 为 `SERVICE_AUTO_START` 并启动，临时回到开发服务器形态 |

回滚后必须重新执行 §5.7，重点确认登录、SSE、worker 心跳与行情写入。

---

## 9. 验收清单

| # | 项目 | 判定 |
| --- | --- | --- |
| 1 | 构建可追溯 | commit、`uv.lock`、`pnpm-lock.yaml`、`dist` 哈希均已归档 |
| 2 | 构建门禁 | `pnpm build` 通过；受影响后端测试通过；`git diff --check` 干净 |
| 3 | 服务状态 | `TSPPostgres`、`TSPPlatform`、`TSPApi`、`TSPEdge`、`TSPWorkerWatchdog` 均 `Running / Automatic` |
| 4 | 端口收口 | 3018/3030/3012 仅本机可达；外部仅 80/443 |
| 5 | TLS | 无证书告警；HSTS 生效；HTTP 自动跳转 HTTPS |
| 6 | 静态托管 | `/` 返回 `index.html`；`/strategies` 等深链返回 200；`/assets/*` 长缓存 |
| 7 | API | 登录、租户选择、行情、K 线、策略、监控接口通过 |
| 8 | SSE | `/api/events`、`/api/intraday/*`、`/api/mining/*` 长连接稳定，无缓冲积压 |
| 9 | Worker | 4 类 worker `healthy=true`，`age_seconds` 远低于阈值 |
| 10 | 自愈 | 手工终止一个 worker supervisor，冷却后自动恢复 |
| 11 | 重启 | 整机重启后无需登录，服务按依赖顺序自动拉起 |
| 12 | 备份 | 双库 dump 可 `pg_restore --list`；目录归档可解压 |
| 13 | 回滚 | `dist.prev` 与上一 commit 可恢复；`TSPFrontend` 可临时启用 |
| 14 | 完整就绪 | 原生隔离 Compute 实现后，`/health/ready` 所有检查均 `ok`；当前仍被 Compute 缺失阻断 |

---

## 10. 风险与明确边界

### 10.1 现有 `compute_broker` 的 Windows 实现缺口

- `ComputeExecutor` 硬编码 `socket.AF_UNIX`（`multitenancy/executor.py`），Windows CPython 无此常量。
- `ComputeBroker` 继承 `socketserver.ThreadingUnixStreamServer`，该类型在无 `AF_UNIX` 的平台不存在。
- 计算沙箱依赖 `docker run --network none --read-only ...`，本机无容器运行时。

结果：该部署 `/health/ready` 为 503、`ready=false`，经计算通道的能力
（回测、优化、挖掘、监控计算等）不可用。缺少 `AF_UNIX` 时客户端现在明确返回
503；原生全功能要求另行补齐受控 IPC、沙箱和资源回收，不能使用无沙箱计算替代。

### 10.2 其他平台拓扑参考（不属于本轮原生验收）

走 Linux / WSL2 + Docker 正式生产拓扑：

- [deploy/full/compose.yml](../deploy/full/compose.yml)：platform、gateway、api、四类 worker、PostgreSQL、Edge 七角色。
- [deploy/full/Compute.Dockerfile](../deploy/full/Compute.Dockerfile) 与 `deploy/full/tsp-compute-broker.service`：主机计算 Broker。
- [deploy/edge](../deploy/edge/README.md)：HTTPS Edge 与本地 CA。
- [deploy/full/production-16g.env](../deploy/full/production-16g.env)：16 GiB 单机资源预算。
- [deploy/full/single-host.md](../deploy/full/single-host.md)：单机验收流程。

该拓扑与本文的 Windows 原生方案**不能同时写同一个行情目录**，候选环境必须使用独立数据目录、数据库与端口。

### 10.3 其他风险

| # | 风险 | 处置 |
| --- | --- | --- |
| R1 | Windows 上 uvicorn 多进程会重复建立数据库监听与轮询，放大资源且无独立 Gateway | 保持 `--workers 1`；需要横向扩展时按 Docker Gateway 拓扑部署 |
| R2 | `subprocess.terminate()` 在 Windows 为硬杀，worker 无优雅关闭 | 依赖看门狗「重启即恢复」；停止前先 `tsp-full.ps1 stop` |
| R3 | 看门狗以 `LocalSystem` 拉起 worker，与手工启动的同名 worker 可能短暂并存 | 单实例由 PID 文件与 `progress.json` 的 `instance` 校验保证；运维统一用服务启停 |
| R4 | Caddy 以 LocalSystem 运行时证书目录与用户态不同 | 用 `storage file_system C:\Tools\caddy\data` 固定路径并纳入备份 |
| R5 | `index.html` 若被中间层缓存，升级后浏览器仍引用旧 assets | 不在 Edge 覆盖 index 的 `Cache-Control`，保留后端 `no-store` |
| R6 | `.env` / `secrets.env` 权限过宽导致凭据泄露 | ACL 仅允许 `Administrators` 与 `SYSTEM`；不放入 Web 根目录 |
| R7 | 数据库迁移不可回退 | 升级前强制备份；回滚依赖数据库恢复，不执行降级脚本 |

---

## 附录 A：本方案涉及的文件级变更

| 文件 | 变更 | 状态 |
| --- | --- | --- |
| `deploy/windows/tsp-service.ps1` | `BindHost` 默认 `127.0.0.1`；api 显式 `STATIC_DIR`；单 worker、受控 proxy headers 与原生资源配置 | 代码已实现；Windows 真机待验证 |
| `docs/windows10-production-deployment.md`（本文件） | 新增生产构建与部署方案 | 已生成 |
| `docs/README.md` | 「部署与配置」索引增加本方案 | 已更新 |
| `docs/windows10-full-run-plan.md` | 第 8 节末尾指向本方案 | 已更新 |
| Windows 服务配置 | 新增 `TSPEdge`；`TSPFrontend` 设为 `SERVICE_DISABLED` | 待实施（NSSM 命令见 §4.5、§3.3） |
| `C:\Tools\caddy\Caddyfile` | 新增 Edge 配置 | 待实施 |
| 防火墙 | 新增 80/443 入站规则 | 待实施 |

本方案不修改任何产品代码，不改变 Linux/容器行为，不递增应用版本号（合并 `main` 后由 CI 递增）。
