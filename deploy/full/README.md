# 完整产品单机多租户部署

本目录是完整产品单机多租户部署入口，沿用原 `app.main`、完整前端和原业务引擎。
一期于 2026-10-14 完成生产切流与 100 用户验收，见
[一期实施记录](../../docs/phase1-implementation.md)。后续候选升级仍按下述隔离验收流程执行。不能与旧单进程版本同时写同一个行情目录。候选数据、数据库及监听端口必须与生产分离；完整验收后才切流。

## 运行组成

- `platform`：唯一行情写入者、盘前/盘后与分钟任务、盘口和扩展/财务同步。禁止加载旧账户、私有因子/策略、全局监控、模拟盘、挖掘及复盘任务。
- `api`：默认四个 HTTP 进程，完整前端、持久身份和租户业务；公共行情与平台配置只读。管理员数据/设置操作经过服务端权限验证后转发至平台。
- `gateway`：单进程实时入口，仅开放 `/api/events`、`/api/events/ticket` 和健康端点；本地验签一次性租户票据，监听撤权和私有事件通知，并转发平台行情 marker，不挂载公共行情、平台配置或计算 socket。
- `research`、`realtime`、`notifications`、`scheduled`：独立任务进程，分别处理受限研究、监控与模拟盘、通知 outbox、租户周度挖掘和每日复盘。
- 主机计算 Broker：固定计算镜像、每租户一个/全机四个容器；仅 Broker 有 Docker 权限。API/Worker 只挂载 Unix socket。计算容器无网络、无凭据、UID 10001、只读公共文件。
- PostgreSQL 16：控制库和业务库使用不同无继承关系的非 owner、非超级用户、非 BYPASSRLS 角色，均迁移到 schema v6。

## 小规格单机候选

从最新主线开始的统一容器候选使用[单机验收流程](./single-host.md)。
`small-host.env` 是约 8 GiB Docker 主机上的小夹具功能验收预算，不能据此声称
可承载全市场多年行情或 100 个持续活跃用户。默认生产配置的既有资源上限保留；
可通过各角色 `TSP_*_MEMORY`、`TSP_*_CPUS`、连接池和并发参数独立调整。

四类 Worker 由 `app.multitenancy.worker_process` 监督实际调度循环。
本实例心跳包含 PID、随机实例标识和单调时钟；只有成功写入数据库后才记录进展。
研究/通知 120 秒、实时 420 秒、定时批次 660 秒无进展时，监督进程先发送终止信号，
25 秒后仍未退出则强制回收，并以非零状态退出，交由 Compose 重启。
首次启动最多等待 120 秒。定时任务租约续期不刷新本地进展。
API 原有 120 秒数据库 Worker 就绪判定保留，因此慢批次期间可能先撤出 API 就绪。
监督器检查的是调度循环，单个研究任务继续由原计算超时、取消和数据库租约管理。
所有角色日志按每文件 10 MiB、最多 3 个文件轮转。

`tsp-compute-broker.service` 支持在私密 `broker.env` 配置
`TSP_COMPUTE_CAPACITY`、`TSP_COMPUTE_MEMORY_MB`、`TSP_COMPUTE_CPUS`；
默认仍为 4、4096、2。计算请求不能覆盖这些宿主配置。资源预算须包含
“最大并发 × 每个计算容器内存”，不能只计算常驻服务。

## 配置与目录

Compose 通过以下变量选择资源，私密值应置于权限 `0600` 的环境文件，不进入 Git：

| 变量 | 内容 |
| --- | --- |
| `TSP_FULL_IMAGE` | 以原 Dockerfile、`BACKEND_EXTRAS=cell` 构建的确切完整镜像版本；Compute 层会按冻结锁补装 `backtest` extra |
| `TSP_FULL_ENV_FILE` | API/Gateway/Worker 私密环境文件，包含 `TSP_CELL_ID`、两个 runtime DSN、市场网关、流票据密钥和邮件配置 |
| `TSP_PLATFORM_ENV_FILE` | 平台环境文件，仅有供应商/系统配置和两个内部网关 Token，无数据库 DSN |
| `TSP_PLATFORM_DATA_DIR` | 平台可写原行情目录；候选阶段使用独立复制件 |
| `TSP_PLATFORM_CONFIG_DIR` | 平台配置：`preferences.json`、`secrets.json`、`data_sources/`；不包含旧用户业务文件 |
| `TSP_PUBLIC_MARKET_DIR` | 独立公共版本目录，包含 `releases/<id>/` 与原子 `current` 链接 |
| `TSP_COMPUTE_SOCKET_DIR` | 主机 Broker socket 目录，默认 `/run/tsp-full` |
| `TSP_FULL_API_PORT` | 候选 API 仅监听主机回环，默认 3030 |
| `TSP_FULL_GATEWAY_PORT` | 候选实时网关仅监听主机回环，默认 3031 |
| `TSP_FULL_PG_NETWORK` | PostgreSQL 外部 Docker network，默认 `tsp-pg_default` |
| `TSP_FULL_EDGE_NETWORK` | HTTPS Edge 外部 Docker network，默认 `app_default`；API/Gateway 分别使用 `tsp-full-api`、`tsp-full-gateway` 别名 |

API/Gateway/Worker 文件中设 `TSP_CELL_MARKET_URL=http://platform:3018`、`TSP_CELL_MARKET_TOKEN`；其值必须与平台的 `TSP_PLATFORM_MARKET_TOKEN` 一致。两端的 `TSP_PLATFORM_ADMIN_TOKEN` 也必须一致，并与市场 Token 不同。

`TSP_CELL_STREAM_TICKET_SECRET` 是 API 与 Gateway 共享的 HMAC 密钥，至少 32 字节，必须由密码学安全随机源独立生成，不能复用数据库密码、平台 Token 或注册口令。所有 API/Gateway 实例必须使用同一个值；轮换时先停止 Edge 新连接，同时更新并重启 API 与 Gateway，避免新票据在旧网关验签失败。密钥为空或过短时服务启动失败。

注册邮件使用原 `AUTH_SMTP_*` 配置，未设置时读取平台 SMTP 设置。通知复用该传输配置，目的地使用本租户渠道。原 `TSP_CELL_SMTP_*` 仅为未配置原邮件服务时的通知后备。Webhook 的允许 origin 使用 `TSP_CELL_WEBHOOK_ORIGINS`。

平台配置与数据目录必须可由 UID 10001 读写；API 挂载同一配置目录只读。迁移目录前保存原所有权和文件权限；候选目录调整权限不得触碰原生产目录。公共版本文件中不会发布 `user_data`、环境文件或数据源凭据，扩展数据配置的 `pull` 信息也不进入公共版本。

平台与 API/Worker 均须设置可写的 `NUMBA_CACHE_DIR=/tmp/numba`，不能让 UID 10001 在镜像源码目录写计算缓存。主机须存在 GID 10001 的组，Broker 的 systemd `Group=10001` 与 socket 权限、容器挂载权限使用同一 GID。

完整镜像的国内镜像构建先从未修改的冻结锁导出依赖，再用 `--require-hashes` 安装，最后执行冻结同步。`PYPI_LOCK_MIRROR` 只改变下载链路，不改变版本或文件哈希；仅设置普通索引变量不能替换冻结锁内的官方下载地址。

## 初始化与启动

1. 备份两个 PostgreSQL 库、原 `data/user_data`、策略/研究/模拟盘目录、平台配置、镜像及 Nginx 配置，记录可恢复路径。
2. 对独立候选库执行原 `app.cell.admin migrate --target-version 6`，分别指定 control/business runtime 角色；登记 Cell 后运行 `bootstrap-full-identity`。初始化只创建缺失的 `admin/Admin123`，不会重置已改密码。
3. 对明确暂停的 legacy 租户执行 `import-full-product --directory <原data的一致备份> --tenant-id <id> --owner-id <原owner> --report <摘要路径>`，随后执行 `verify-full-import --directory <同一备份> --tenant-id <id> --report <对账路径>`。该命令覆盖账号与完整业务；仅 `import-full-accounts` 或旧 v4 导入不够。旧通知名称须通过 `--channel-map '{"email":"legacy-email"}'` 明确绑定至该租户的已有渠道，实际值以对账结果为准。
4. 将平台偏好、密钥和 `data_sources/` 从旧目录复制到独立平台配置目录，完成数量/内容对账。保留管理员维护的全局菜单配置；个人布局/列配置和租户定时/通知配置分别导入其 SQL 归属。保留旧文件用于恢复。
5. 启动独立 `platform` 并等待首个完整公共版本。版本发布逐项复制新文件、复用上一不可变版本的相同文件，并在数据世代或文件清单变化时放弃本次发布。
6. 以完整镜像构建 `Compute.Dockerfile`（传入 `RUNTIME_IMAGE`）。该层使用冻结锁补装
   `backtest` extra，并执行 `vectorbt` 导入检查；否则公开的 `signal_backtest`
   计算类型会在运行时失败。配置 `/etc/tsp-full/broker.env` 的确切
   `TSP_COMPUTE_IMAGE` 和主机 `TSP_PUBLIC_MARKET_DIR`。主机 Python 需 3.11+，
   项目代码部署到 `/opt/tsp-full/backend`；Broker 本身只需标准库。
7. 确认主机 GID 10001 已存在；若不存在，使用 `groupadd --gid 10001 tsp-runtime` 创建。安装 `tsp-compute-broker.service`，准备 `/opt/tsp-full/python/bin/python` 后启动 Broker，再启动 Gateway、API 和四个 Worker。
8. API 的 `/health/ready` 必须通过数据库、公共文件、平台连接、指标缓存、计算 Broker 及四类 Worker 心跳。Gateway 的 `/health/ready` 必须分别通过双库、私有事件 LISTEN、撤权 LISTEN 和平台 marker 流。只看容器运行或 `/health/live` 不算功能就绪。

```bash
docker compose -p tsp-full-candidate -f deploy/full/compose.yml config --quiet
docker compose -p tsp-full-candidate -f deploy/full/compose.yml up -d platform
docker compose -p tsp-full-candidate -f deploy/full/compose.yml up -d gateway
docker compose -p tsp-full-candidate -f deploy/full/compose.yml up -d research realtime notifications scheduled api
```

Edge 只将精确路径 `/api/events` 和 `/api/events/ticket` 路由至 Gateway，其余请求继续进入 API。`/api/events/ticket` 先由权威 Cookie/API Token 身份核验签发 60 秒一次性票据；Gateway 消费后本地校验，并通过 PostgreSQL 专用 direct/session `LISTEN` 连接接收撤权和私有事件唤醒。该连接不能经过 PgBouncer 事务池。通知丢失时私有事件最多 30 秒全扫追赶；撤权广播失效时已有流由 60 秒硬 TTL 收敛。

同一计算任务固定挂载一个发布版本，后续数据发布不能改变其历史输入。当前不自动删除旧版本；验收与上线时记录占用，清理前须确认无排队任务或 API 引用该版本。二期再实施引用计数和自动保留策略。

完整导入保存逐条目标内容散列和源文件清单。最终增量仍要求租户暂停，且先核对候选中的上次导入记录；候选业务编辑、源快照变化、缺失文件或模拟盘台账改写都会阻止覆盖。模拟盘按原成交台账核对现金与持仓；原始业务文件可在研究任务历史中导出。旧会话、验证码、全局 Token 和平台凭据不进入租户归档。导入账本仅离线管理员可用。

若旧 Cell 数据含不同模型的策略/交易活动，导入会拒绝继续，需先进行明确的域转换；不能将其标记为已迁移或用完整产品默认值替代。候选与正式库均应保留导入摘要、源快照与内容对账报告。

## 切流与恢复门槛

发布前必须完成旧数据/产物对账、注册与邮件、管理员改密、全部 18/19 业务页面关键操作、角色与数据隔离、真实模型、交易窗口与跨日结算，以及 100 个独立活跃身份混合负载。

切流前暂停旧写入者，最终增量复制并重新对账、发布公共版本，确认 API 与 Gateway 深度健康后，将普通 Nginx upstream 从旧 `app:3018` 改为 `tsp-full-api:3018`，并将两个事件精确路径指向 `tsp-full-gateway:3018`，沿用已签发的本地 CA HTTPS。执行 `nginx -t` 后仅 reload，保存切流时间和镜像/配置/数据版本。

仅回退实时网关时，先把两个事件精确路径恢复到原 API upstream 并执行 `nginx -t`/reload，再停止 Gateway；旧 API 事件端点保持兼容。完整恢复时先阻止候选写入，保存切流后的两个数据库及私有产物，恢复原 Nginx、镜像和备份，再启动唯一旧行情写入者。不能让两个版本同时写共享目录，也不能恢复旧快照后直接丢弃切流后新注册账号或业务写入；恢复需要对这部分增量明确对账。
