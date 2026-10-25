# 完整产品单机多租户部署

生产开发与六类文档追踪见[工程规范](../../docs/reference/engineering.md#engineering)、
[本地变更映射](../../docs/reference/engineering.md#trace)。
当前单机多角色与目标多节点分别验收，回退保护切流后增量，完整目标见
[生产验收](../../docs/testing.md#acceptance)。

本目录是完整产品单机多租户部署入口，沿用原 `app.main`、完整前端和原业务引擎。
一期于 2026-10-14 完成生产切流与 100 用户验收，见
[一期实施记录](../../reports/document-consolidation-20261024/historical-evidence.md#phase1)。
2026-10-25本批升级前实测新ECS `118.196.108.188`/`https://shanao.asia`
已运行0.4.21/7823b84b/schema v6，七角色及PostgreSQL健康；最新发布状态见
[本批记录](../../reports/current-state-release-20261025/verification.md)。
旧实例inode修复和双库恢复见[历史结果](../../reports/online-timeout-20261021/result.md)。
候选升级继续按下述流程执行，不能与旧实例同时
写一个行情目录；候选数据、数据库及监听端口必须与生产分离。

## 运行组成

- `platform`：唯一行情写入者、盘前/盘后与分钟任务、盘口和扩展/财务同步。禁止加载旧账户、私有因子/策略、全局监控、模拟交易、挖掘及复盘任务。
- `api`：默认四个 HTTP 进程，完整前端、持久身份和租户业务；公共行情与平台配置只读。管理员数据/设置操作经过服务端权限验证后转发至平台。
- `gateway`：单进程实时入口，仅开放 `/api/events`、`/api/events/ticket` 和健康端点；本地验签一次性租户票据，监听撤权和私有事件通知，并转发平台行情 marker，不挂载公共行情、平台配置或计算 socket。
- `research`、`realtime`、`notifications`、`scheduled`：独立任务进程，分别处理受限研究、监控与模拟交易、通知 outbox、租户周度挖掘和每日复盘。
- 主机计算 Broker：固定计算镜像、每租户一个，全机并发按策略预算及配置限制；仅 Broker 有 Docker 权限。API/Platform/Worker 只挂载 Unix socket。计算容器无网络、无凭据、UID 10001、只读公共文件。
- PostgreSQL 16：控制库和业务库使用不同无继承关系的非 owner、非超级用户、非 BYPASSRLS 角色，均迁移到 schema v6。

## 小规格单机候选

从最新主线开始的统一容器候选使用[单机验收流程](single-host.md)。
`small-host.env` 是约 8 GiB Docker 主机上的小夹具功能验收预算，不能据此声称
可承载全市场多年行情或 100 个持续活跃用户。默认生产配置的既有资源上限保留；
可通过各角色 `TSP_*_MEMORY`、`TSP_*_CPUS`、连接池和并发参数独立调整。
`production-16g.env` 是单 Compute、36 个数据库连接上界的 16 GiB 正式预算；
应用角色 8704 MiB，连同 Compute、PostgreSQL、Broker/Edge 和宿主保留共
15744 MiB。更改 Worker 数、连接池或 Compute 并发后必须重新验算。

四类 Worker 由 `app.multitenancy.worker_process` 监督实际调度循环。
本实例心跳包含 PID、随机实例标识和单调时钟；只有成功写入数据库后才记录进展。
研究/通知 120 秒、实时 420 秒、定时批次 660 秒无进展时，监督进程先发送终止信号，
25 秒后仍未退出则强制回收，并以非零状态退出，交由 Compose 重启。
首次启动最多等待 120 秒。定时任务租约续期不刷新本地进展。
API 深度就绪使用同一组按类型区分的 freshness，不会在监督器仍判定健康时因固定
120 秒阈值提前撤出。
监督器检查的是调度循环，单个研究任务继续由原计算超时、取消和数据库租约管理。
所有角色日志按每文件 10 MiB、最多 3 个文件轮转。

`tsp-compute-broker.service` 支持在私密 `broker.env` 配置
`TSP_COMPUTE_CAPACITY`、`TSP_COMPUTE_MEMORY_MB`、`TSP_COMPUTE_CPUS` 和
`TSP_COMPUTE_QUEUE_SECONDS`；模板默认为 16、4096、2、2。其中 16 是部署并发
上限，实际默认“自动”按主机内存/CPU各25%的策略预算估算推荐值。
管理员在“设置 → 系统设置 → 策略页”保存并发数，所有租户共用；
单任务额度按原限额与总预算均分值中的较小值施加 Docker 限制。
并发满最多等待该短窗口，等待者最多16个。健康检查实际启动无网络沙箱并缓存60秒；
失败日志保留最多8 KiB stderr尾部，计算请求不能覆盖宿主配置。
旧的小主机预算文件继续作为部署上限，不能从新模板自动推定容量。

`TSP_COMPUTE_CONFIG_FILE` 默认 `/opt/tsp-full/platform-config/preferences.json`，
须对应 `TSP_PLATFORM_CONFIG_DIR` 中 Platform 实际写入的文件。目录自定义时在
`broker.env` 同步路径；Broker只读配置，Platform通过只读socket查询执行主机
资源。隔离Compose将候选配置目录只读挂给Broker；不要单独bind偏好文件，
否则原子替换后可能仍读到旧inode。保存后新任务生效，旧任务继续收尾并占用旧额度；
详细计算、失败提示与单进程边界见[运维配置](../../docs/operations.md#strategy-concurrency)。

该ECS实际由`tsp-full.service`和`operations/start_full.py`管理启动顺序，
Platform目录同步完成、刷新快照且业务功能完整后才创建消费者；
不要只用Compose单独重启平台绕过现有顺序保护。Broker升级须同步确切Compute镜像、
源码与平台偏好路径，保留已有容量上限。无schema变化时不重跑迁移或初始化。
管理员/注册/SMTP受控配置保留用户指定及随后主动修改的值；现有部署开关适配
已核验的预置要求，差异在发布记录注明，不将真实授权码写入Git。

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

注册与通知共用传输：显式 `AUTH_SMTP_*` → 已保存的平台 SMTP → `TSP_CELL_SMTP_*`
→ 未配置。必须通过受控配置提供邮箱传输及授权码，源码不包含实际凭据。
目的地使用本租户渠道，授权码不下发到页面。Webhook 的允许 origin 使用 `TSP_CELL_WEBHOOK_ORIGINS`。

平台配置与数据目录必须可由 UID 10001 读写；API 挂载同一配置目录只读。迁移目录前保存原所有权和文件权限；候选目录调整权限不得触碰原生产目录。公共版本文件中不会发布 `user_data`、环境文件或数据源凭据，扩展数据配置的 `pull` 信息也不进入公共版本。

平台与 API/Worker 均须设置可写的 `NUMBA_CACHE_DIR=/tmp/numba`，不能让 UID 10001 在镜像源码目录写计算缓存。主机须存在 GID 10001 的组，Broker 的 systemd `Group=10001` 与 socket 权限、容器挂载权限使用同一 GID。

完整镜像的国内镜像构建先从未修改的冻结锁导出依赖，再用 `--require-hashes` 安装，最后执行冻结同步。`PYPI_LOCK_MIRROR` 只改变下载链路，不改变版本或文件哈希；仅设置普通索引变量不能替换冻结锁内的官方下载地址。

## 初始化与启动

非 Windows 默认开启完整多租户，未指定服务角色时使用 `api`；Windows 默认单机管理员。
初始化自动预置 `admin/Admin123` 和注册口令 `天王盖地虎`，注册与密码找回共用该口令。
`AUTH_REGISTRATION_SECRET` 可在首次自定义，已有 SQL 口令不会被重置。
多租户初始化依赖下面的双库、schema 与内部服务准备，默认开关不替代这些资源。

1. 备份两个 PostgreSQL 库、原 `data/user_data`、策略/研究/模拟交易目录、平台配置、镜像及 Nginx 配置，记录可恢复路径。
2. 对独立候选库执行原 `app.cell.admin migrate --target-version 6`，分别指定 control/business runtime 角色并登记 Cell。API 首次启动使用已有运行权限自动创建缺失的 `admin/Admin123` 和注册口令，开放注册与密码找回，不重置已改密码/口令；`bootstrap-full-identity` 仍可用于提前初始化。API 不执行建库或 schema 迁移。
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

同一计算任务固定挂载一个发布版本，后续数据发布不能改变其历史输入。
`scripts.operations.maintain_full_host` 从业务库研究任务提取受保护 release ID，同时保留
`current`、最近版本和最小存龄；缺少 DSN 或 PostgreSQL 容器/库名时拒绝清理 release。
Docker 维护只清理到期 BuildKit 缓存和悬空镜像，不使用 `image prune -a`，不会
删除带 tag 的回滚镜像。命令默认 dry-run，复核 JSON 清单后才传 `--apply`。

公共版本每次创建目录会消耗 inode，即使文件使用硬链接也会增长。持续运行的
单机须安装 `systemd/tsp-public-release-maintenance.{service,timer}`，每五分钟
保留 `current`、最近 30 份、至少六小时及全部研究引用。安装前先用相同参数 dry-run；
查询失败会终止清理。该任务不清理 Docker 镜像和原行情。

文件版本发布间隔为五分钟；实时行情和盘口继续通过独立行情桥接刷新。每次创建
版本前按完整文件/目录清单估算空间，保留至少 10% 或 100,000 个 inode，以及
1 GiB 字节空间。容量不足时保留上一份不可变版本，不创建半成品；健康检查报告
inode 余量。无有效历史版本的首次启动仍须成功完成发布。
API 在快照至少五秒未更新且 `current` 变化时按需切换仓库，即使盘后行情
marker 流仍连接，也不会长期读取已到期目录。

将 `backend/scripts/operations/maintain_full_host.py` 安装到
`/opt/tsp-full/maintenance/maintain_full_host.py`，在
`/etc/tsp-full/public-release-maintenance.env` 填入实际目录、PostgreSQL 容器和业务库：

```dotenv
TSP_PUBLIC_MARKET_DIR=/opt/tsp-full/public
TSP_POSTGRES_CONTAINER=tsp-pg
TSP_BUSINESS_DATABASE=BUSINESS_DB
```

无需复制数据库密码；脚本在现有 PostgreSQL 容器中用 owner 查询，避免 runtime
角色的 RLS 隐藏其他租户引用。容器/库名缺失或查询结果非法时拒绝删除。

```bash
python3 /opt/tsp-full/maintenance/maintain_full_host.py \
  --release-root /opt/tsp-full/public --postgres-container tsp-pg \
  --business-database BUSINESS_DB --keep-releases 30 --min-release-age-hours 6
install -m 644 deploy/full/systemd/tsp-public-release-maintenance.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tsp-public-release-maintenance.timer
systemctl start tsp-public-release-maintenance.service
systemctl status tsp-public-release-maintenance.service --no-pager
df -i /opt/tsp-full/public
```

维护路径与 Compose 实际挂载保持一致。五分钟发布和六小时存龄约保留 72 份普通
版本；近期 30 份和研究引用另行保护。研究引用持续累积时需扩容或通过产品删除
不再需要的研究任务，不能绕过引用强删。CLI 原按天保留模式仍兼容。

停写后的双库备份使用 `scripts.operations.backup_full_host`。它先生成两个 custom-format dump，
再分别恢复到临时数据库并读取 schema 版本，全部成功后才原子发布备份目录：

```bash
cd backend
python -m scripts.operations.backup_full_host \
  --postgres-container tsp-pg --control-database CONTROL_DB --data-database DATA_DB \
  --output /opt/tsp-full/backups/final-YYYYMMDD-HHMMSS \
  --include platform-config=/opt/tsp-full/platform-config \
  --include edge=/opt/tsp-full/edge --confirm-quiesced

TSP_DATA_ADMIN_DSN='postgresql://...' python -m scripts.operations.maintain_full_host \
  --release-root /opt/tsp-full/public --prune-docker-cache
# 复核输出后追加 --apply。
```

持续观察器在 `alerts.jsonl` 写入去重的 `firing`/`resolved` 事件，覆盖深度健康、
容器退出/重启及磁盘低于 1 GiB；该文件应由主机告警采集器转发。

完整导入保存逐条目标内容散列和源文件清单。最终增量仍要求租户暂停，且先核对候选中的上次导入记录；候选业务编辑、源快照变化、缺失文件或模拟交易台账改写都会阻止覆盖。模拟交易按原成交台账核对现金与持仓；原始业务文件可在研究任务历史中导出。旧会话、验证码、全局 Token 和平台凭据不进入租户归档。导入账本仅离线管理员可用。

若旧 Cell 数据含不同模型的策略/交易活动，导入会拒绝继续，需先进行明确的域转换；不能将其标记为已迁移或用完整产品默认值替代。候选与正式库均应保留导入摘要、源快照与内容对账报告。

## 切流与恢复门槛

发布前必须完成旧数据/产物对账、注册与邮件、管理员改密、全部 18/19 业务页面关键操作、角色与数据隔离、真实模型、交易窗口与跨日结算，以及 100 个独立活跃身份混合负载。

切流前暂停旧写入者，最终增量复制并重新对账、发布公共版本，确认 API 与 Gateway 深度健康后，将普通 Nginx upstream 从旧 `app:3018` 改为 `tsp-full-api:3018`，并将两个事件精确路径指向 `tsp-full-gateway:3018`，沿用已签发的本地 CA HTTPS。执行 `nginx -t` 后仅 reload，保存切流时间和镜像/配置/数据版本。

仅回退实时网关时，先把两个事件精确路径恢复到原 API upstream 并执行 `nginx -t`/reload，再停止 Gateway；旧 API 事件端点保持兼容。完整恢复时先阻止候选写入，保存切流后的两个数据库及私有产物，恢复原 Nginx、镜像和备份，再启动唯一旧行情写入者。不能让两个版本同时写共享目录，也不能恢复旧快照后直接丢弃切流后新注册账号或业务写入；恢复需要对这部分增量明确对账。
