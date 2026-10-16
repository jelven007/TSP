# 部署指南

本文覆盖源码部署、Docker Compose、开发模式和桌面安装包。配置项见
[配置说明](./configuration.md)，备份与恢复见[运维手册](./operations.md)，
正式放行见[发布流程](./release-operations.md)。

部署前先核对[版本基线](./version-baseline.md)。`main`、最新 `v*` 标签和当前生产
可能指向不同提交；所有部署命令必须固定 commit、tag 或镜像 digest。

## 部署方式

| 方式 | 适用场景 | 认证模式 |
| --- | --- | --- |
| 完整单机多租户 | 一期服务器、100 用户混合负载 | 管理员/租户 RBAC、持久会话 |
| 根目录 Docker Compose | 独立运行、NAS | 保留邮箱账户认证 |
| Dev 模式 | 二次开发和本地调试 | 保留邮箱账户认证 |
| 桌面安装包 | 单机直接使用 | 本地免登录 |

`main` 源码候选的完整单机多租户拓扑使用 `deploy/full/compose.yml`，启用
`MULTITENANCY_ENABLED=true`。保留完整前端与业务引擎，API 默认四进程；
独立实时网关负责 SSE，平台负责唯一行情写入，四类 Worker 分别负责研究、
实时监控/模拟盘、通知和调度。
身份与租户业务持久化到 PostgreSQL v6，公共行情按不可变版本只读共享。
部署步骤见[完整单机多租户部署](../deploy/full/README.md)，
已完成的一期发布与 100 用户验收见[实施记录](./phase1-implementation.md)。
当前生产为 `d06acc1d`/schema v5；`main` 中的实时网关和 schema v6 仍是候选，
不得直接按源码文档推断生产拓扑。

下方根目录 Compose、Dev 和桌面步骤适用于独立运行模式。

当前仓库文档默认从源码构建容器，不依赖未经当前项目验证的公共镜像标签。

## Docker Compose

### 前置要求

- Git
- Docker Engine
- Docker Compose v2
- 建议至少 4 GB 可用内存和足够的数据盘空间

### 首次部署

```bash
git clone https://github.com/jelven007/TSP.git
cd TSP
cp .env.example .env
docker compose up --build -d
```

访问 <http://localhost:3018>。

基础镜像默认从 Docker Hub 拉取。若 `registry-1.docker.io` 超时或返回
`Bad Gateway`，可通过构建参数切换拉取仓库：

```bash
BASE_IMAGE_REGISTRY=public.ecr.aws/docker docker compose build app
```

也可在 `.env` 中保存 `BASE_IMAGE_REGISTRY=public.ecr.aws/docker`。
直接构建使用 `docker build --build-arg BASE_IMAGE_REGISTRY=public.ecr.aws/docker -t tsp:local .`。
该参数只替换 Node、Python、Rust 基础镜像的仓库前缀，版本和依赖锁不变；
使用备选仓库时应核对镜像摘要。`USE_CN_MIRROR` 仅控制构建中的 npm、PyPI、
APT 和 Cargo 源，不控制 Docker Hub 拉取。

查看状态和日志：

```bash
docker compose ps
docker compose logs -f app
curl -fsS http://localhost:3018/health/live
curl -fsS http://localhost:3018/health/ready
```

Compose 会把以下内容挂载到容器：

- `./data` → `/app/data`
- `./tiers.yaml` → `/app/tiers.yaml`
- `./.env` → `/app/.env`
- 主机 Codex 配置目录 → `/root/.codex`，只读

生产环境应限制 `.env` 和 `data/` 的文件权限，并把数据目录放在有备份和容量监控
的磁盘上。

### 更新

先备份 `data/`、`.env` 和自定义插件，再执行：

```bash
git pull --ff-only
docker compose up --build -d
docker compose logs -f app
```

更新后检查健康端点、版本、数据画像、Provider 能力、策略、回测和关键调度任务。

### 停止

```bash
docker compose down
```

不要使用 `docker compose down -v`，除非已经确认卷中没有需要保留的数据。

## Dev 模式

### 前置要求

- Python 3.11+
- Node.js 20+
- [`uv`](https://docs.astral.sh/uv/)
- pnpm 9
- [Rust 编译工具链](https://www.rust-lang.org/tools/install)（源码安装默认原生桥接）

macOS / Linux：

```bash
cp .env.example .env
./dev.sh
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
.\dev.ps1
```

默认地址：

- 前端：<http://localhost:3011>
- 后端：<http://localhost:3018>

自定义端口：

```bash
BACKEND_PORT=8000 FRONTEND_PORT=5173 ./dev.sh
```

手动分别启动：

```bash
cd backend
uv sync --frozen
uv run --frozen --no-sync python -m app.plugins.rustdx.client
uv run --no-sync uvicorn app.main:app --reload --port 3018
```

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

rustdx 原生桥接是后端默认依赖，不需要额外指定 `--extra rustdx`。开发脚本在
已有 `.venv` 时也同步依赖，并在启动前校验桥接。Docker 镜像和桌面包在构建时
内置扩展并校验，目标机器不需要 Rust。旧源码环境的桥接不匹配时执行
`uv sync --frozen --reinstall-package tsp-rustdx-native` 重建。

## 桌面安装包

桌面安装包从当前仓库的
[GitHub Releases](https://github.com/jelven007/TSP/releases) 获取。
仅使用实际存在且校验通过的平台资产。

桌面版与服务端版的明确差异：

- Windows、macOS、Linux 桌面版安装后直接进入系统。
- 桌面版不注册账户、不登录、不发送注册验证码。
- 桌面版隐藏账户管理和退出登录入口。
- 服务端和 Dev 模式继续保留认证。

覆盖安装前仍应备份安装目录下的 `data/`。安装包不会主动删除运行时数据，但备份是
升级和回滚的前置条件。

## 账户初始化

服务端部署可在 `.env` 中预置首个账户：

```ini
AUTH_EMAIL='admin@example.com'
AUTH_PASSWORD='至少八位的密码'
```

两项同时配置时，首次启动会创建邮箱账户；已有账户后不会覆盖页面中维护的密码。

要开放浏览器注册，还需配置注册口令和可用 SMTP：

```ini
AUTH_REGISTRATION_SECRET='请设置注册口令'
AUTH_SMTP_HOST='smtp.example.com'
AUTH_SMTP_PORT=465
AUTH_SMTP_SECURITY='ssl'
AUTH_SMTP_USERNAME='no-reply@example.com'
AUTH_SMTP_PASSWORD='SMTP 密码或授权码'
AUTH_SMTP_FROM_ADDRESS='no-reply@example.com'
```

注册口令首次启动后以带随机盐的 PBKDF2 哈希写入 `auth.json`。确认初始化成功后，
可从 `.env` 删除明文口令并重启。SMTP 配置必须通过真实发信测试后再用于注册。

公网部署要求：

- 使用 HTTPS。
- `.env` 权限设为 `600`。
- SMTP 使用独立授权码。
- 限制反向代理请求体、超时和访问来源。
- 定期备份 `data/user_data/auth.json` 和完整 `data/`。

完整账户流程见[邮箱账户初始化](./deploy-password.md)。

## 扩展概念与行业上游

新安装的扩展概念、扩展行业默认使用内置 JSON 接口并自动拉取，
无需配置环境变量。需要替换上游时，可在 `.env` 中独立覆盖：

```ini
EXT_CONCEPT_DATA_URL='https://data.example.com/concepts'
EXT_INDUSTRY_DATA_URL='https://data.example.com/industries'
```

未配置或为空时保留内置默认接口。新建预设会在交易日
`09:15-11:30`、`13:00-15:15` 每分钟拉取，并在调度器启动时执行首轮。
旧版未使用的空默认配置会自动修复；已有用户自定义配置和主动关闭状态不被覆盖。
自动拉取的关闭入口及默认地址见[配置说明](./configuration.md#内置扩展概念与行业)。

## 老 CPU 兼容

CPU 不支持 AVX2/FMA、进程出现 `exit 132` 时，在 `.env` 设置：

```ini
BACKEND_EXTRAS=legacy-cpu
```

同时需要回测依赖：

```ini
BACKEND_EXTRAS=legacy-cpu backtest
```

然后重新执行 `docker compose up --build -d` 或启动脚本。不要用
`POLARS_SKIP_CPU_CHECK` 隐藏硬件不兼容。

## Codex CLI

Compose 默认只读挂载 `${HOME}/.codex`。Windows 的 `HOME` 可能未设置，可在
`.env` 指定：

```ini
CODEX_HOME_HOST=C:\Users\your-name\.codex
```

覆盖容器内 Codex CLI 版本：

```bash
CODEX_CLI_VERSION=0.144.3 docker compose up --build -d
```

只在可信主机启用 Codex 登录态挂载。

## 数据安全

整个 `data/` 都是运行时数据，不纳入 Git。禁止用以下命令处理升级冲突：

```text
git clean -fdx
git reset --hard
```

前者会删除被 `.gitignore` 排除的运行时数据，后者会丢弃未提交代码。遇到拉取
冲突时先停止服务、备份，再使用 `git status`、`git diff` 和非破坏性方式处理。

## 反向代理

公网部署至少需要：

- HTTPS 终止。
- 正确转发 `X-Forwarded-Proto`。
- 支持 SSE 长连接并关闭代理缓冲。
- 为回测和同步接口设置合理超时。
- 不直接暴露数据目录和配置文件。

反向代理上线后重新验证登录、SSE、回测进度、文件下载和更新检查。
