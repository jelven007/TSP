# 开始使用 Tick Stock Platform（TSP）

核对日期：2026-10-30。应用版本以根目录 [VERSION](../VERSION) 为准。
先选运行模式，再准备对应依赖；模式详细契约见[运维手册](operations.md#1-运行边界)。
持仓提醒支持选中命名批次后整批删除；确认范围及操作步骤见
[持仓提醒操作](../操作说明书.md#112-持仓提醒)。已有数据和初始化配置无需重置。

## 1. 选择运行模式

| 模式 | 适用场景 | 必需条件 |
| --- | --- | --- |
| 独立模式 | 本地开发、单机共享空间 | 后端与前端；显式设置 `MULTITENANCY_ENABLED=false` |
| Windows 单机管理员 | 原生源码或 Windows 桌面包 | `SERVICE_ROLE=standalone`；本机管理员与个人空间 |
| 完整单机多租户 | 私有业务隔离与服务端部署 | PostgreSQL 控制/业务双库、API、Platform、Gateway、Worker、Compute/Broker |
| 桌面安装包 | 使用已验收的目标平台产物 | 对应 Release 安装包；认证规则仍由包的实际运行模式决定 |

非 Windows 源码环境默认开启多租户，角色默认 `api`；只复制 `.env.example`
还不足以启动完整多租户。Windows 强制单机管理员模式，禁止把旧 Platform/Gateway
继续指向同一单机数据目录。

## 2. 获取源码与准备环境

源码开发需要 Git、Python 3.11+、Node.js 20+、uv、pnpm 9 和 Rust 工具链。
Python 和前端依赖分别锁定在 [uv.lock](../backend/uv.lock) 与
[pnpm-lock.yaml](../frontend/pnpm-lock.yaml)。正式镜像或桌面包已包含原生桥接，
目标机通常不需要 Rust。

```bash
git clone https://github.com/jelven007/TSP.git
cd TSP
cp -n .env.example .env
```

已有 `.env` 时保留并按需要编辑。真实 SMTP 授权码、AI 密钥和内部服务令牌
只放受控环境或运行配置。

## 3. 本地源码开发

单机开发在 `.env` 中设置：

```dotenv
MULTITENANCY_ENABLED=false
SERVICE_ROLE=standalone
DATA_DIR=./data
```

macOS/Linux 执行：

```bash
./dev.sh
```

Windows PowerShell 执行：

```powershell
.\dev.ps1
```

默认前端地址为 `http://localhost:3011`，后端为 `http://localhost:3018`。
实际端口以启动输出为准。启动脚本处理依赖与原生桥接；手动开发和可选 extras
见[安装章节](operations.md#installation)。

老 CPU 不支持 AVX2/FMA 时，在配置中使用 `BACKEND_EXTRAS=legacy-cpu`。
`backtest` extra 提供可选 vectorbt；未安装它不应使默认 Matrix 主流程失效。
开发验证需要 `dev` extra：

```bash
uv sync --project backend --frozen --extra dev
pnpm --dir frontend install --frozen-lockfile
```

## 4. 独立模式 Docker

确认 `.env` 已显式关闭多租户，再从仓库根执行：

```bash
docker compose up --build -d
docker compose logs --tail=100 app
```

访问 `http://localhost:3018`。根 Compose 将 `./data` 挂载至 `/app/data`，
容器内 `DATA_DIR` 固定为该目录。停止服务：

```bash
docker compose down
```

数据位于宿主机挂载目录。升级前按[备份与恢复](operations.md#8-备份)准备，
正式环境使用固定提交和已验收的镜像 digest。

## 5. 完整多租户部署

按 [deploy/full](../deploy/full/README.md) 完成：

1. 准备 PostgreSQL 控制库与业务库，执行有序 schema 迁移并使用各自运行角色。
2. 配置私密环境、公共行情目录、平台配置目录、租户产物目录和内部服务认证。
3. 启动平台及相关基础服务，确认公共行情快照就绪，再启动读端与业务 Worker。
4. 核对 API、Gateway、Platform 与 Compute/Broker 的匹配版本及角色权限。
5. 按[健康检查](operations.md#4-健康检查)检查功能就绪，再用业务账户验证隔离。

HTTPS 边缘配置见[deploy/edge](../deploy/edge/README.md)，数据库模板见
[deploy/cell/pg](../deploy/cell/pg/README.md)。当前发布的 schema 身份和运行证据见
[版本基线](version-baseline.md)，不能从模板默认值推断生产已完成迁移。

## 6. 第一次登录与初始化

全新安装未指定其他初始化账户时，按初始化配置预置管理员。
用户已指定的管理员、注册/找回口令和 SMTP 配置优先；已有账户及后续修改不因重启重置。
Windows 单机模式关闭注册、找回与租户切换。

首次向导及后续设置：

1. 在数据源设置核对能力矩阵。默认 rustdx，七项能力分别路由；
   未配置可用能力时先处理能力问题。
2. 等待官方证券目录同步，确认有效股票快照。
3. 同步日 K、除权因子及所需财务数据，确认 Enriched 指标已生成。
4. 需要分钟研究时明确选择 1 分 K 起止日期；多年历史需要独立任务及覆盖核对。
5. 用本地 1 分 K 生成 5/10/15 分 K，用本地日 K 生成周 K。
6. 可选配置 AI 与通知；邮箱验证需要可用 SMTP。

任务完成后核对实际证券数、日期范围、警告及剩余缺口。分钟源有保留期限，
不能把任务成功或页面显示 100% 当作全部行情初始化完成。

## 7. 开始日常工作

推荐顺序为行情 → 自选 → 策略筛选 → 因子/回测 → 监控或模拟观察 → AI/复盘。
自选一股可属于多组、关联多策略；策略关联本身不会自动开启监控。
筛选结果按日期、稳定策略 ID、版本和参数保存，历史查看不自动触发重算。

股票支持 1m/5m/10m/15m/1d/1w；指数页固定七只，支持日/周/月 K 和分时。
指数默认读取可用的完整本地历史，显式日期范围优先；可见根数仍受实际数据限制。
详细操作见[操作说明书](../操作说明书.md)。

## 8. 检查安装或文档改动

```bash
make check
```

这检查工程目录、文档、需求编号、版本一致性和 diff 空白。
服务启动后的接口与业务检查按[测试指南](testing.md)执行。
修改代码前完整阅读 [CONTRIBUTING](../CONTRIBUTING.md)，二次开发再阅读
[扩展指南](secondary-development.md)。
