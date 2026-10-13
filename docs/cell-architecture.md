# Cell 分组与权限隔离：前端及业务接入

本实现包含方案 v2 的 API、数据库基础链路，以及工作空间前端、策略运行、告警和模拟盘。入口为
`app.cell.main:create_app --factory`，位于 `backend/app/cell/`。
每个 Cell 固定配置自己的 ID 和业务数据库；多个 API 副本可运行在同一 Cell。
身份目录数据库独立配置，保存用户、会话、租户、成员、固定 placement 和 Token。
新增 Cell 不改变既有租户映射。

## 功能范围

| 已接入 | 实现 |
| --- | --- |
| 身份 | 邮箱密码登录、服务端哈希会话、退出、数据库共享的登录失败限流 |
| 租户目录 | 只列出当前用户所属工作空间及其 Cell 地址、状态和 routing epoch |
| 权限 | owner/admin/member/viewer；每次请求校验成员、租户状态、Cell 和版本 |
| 成员管理 | owner/admin 管理成员；仅 owner 可授予/修改 owner；保护最后一个 owner |
| Token | 固定租户、创建者、权限、有效期、policy version；明文只返回一次 |
| 自选 | 租户自选列表、批量添加、清空/删除、分组创建/修改/删除/排序、多组归属 |
| 前端 | 独立 Cell 构建；登录、目录刷新、工作空间切换、自选、策略参数表单、告警收件箱、模拟账户与订单 |
| 策略 | 平台内置日线策略目录、租户参数/版本、冻结输入、自选股票池、SQL 持久化任务、取消与运行结果 |
| 告警 | 价格/涨跌幅规则、持久化冷却与行情序号去重、已读状态；策略完成和模拟成交写入收件箱 |
| 模拟盘 | 独立虚拟账户、买卖订单、现金/持仓占用、后续行情撮合、股票 T+1、费用、涨跌停、撤单、冻结和跨日到期 |
| 共享行情 | 受平台服务 Token 保护的只读报价/历史门面；Cell 不直接占用行情源连接 |
| 数据库 | 复合租户主键/外键、强制 RLS、事务内上下文、Cell 归属和 epoch 双重检查 |
| 运维 | 显式 schema 初始化、固定分组建租户、旧账号/自选导入、状态调整 |
| 审计 | 成员与 Token 变化、工作空间状态、自选写入与业务数据在同事务记录 |
| 调度 | 事务触发唤醒、活跃工作索引、租户租约、跨副本 Cell 配额、批次报价合并 |
| 外发通知 | 邮件/Webhook、事务 Outbox、重试/死信、版本与权限复核、投递记录 |
| 自动交易 | 每日内置选股、持久化交易信号、模拟下单、持仓/预算/日配额、T+1 止盈止损 |

租户自有密钥、偏好、完整回测、SSE/WS、券商交易与旧全局业务数据
尚未迁入此入口。旧全局业务路径在 Cell API 中返回 404。旧 `app.main:app`、桌面入口与线上部署
保持原有行为；**仅运行旧入口不获得租户隔离**。不要把旧业务接口通过代理混入新的
多租户域名，否则会绕开这里的隔离边界。

已完成单机 10,000 租户调度压测，详见下方实测边界；尚未完成生产交易时段容量验收。Cell 的数据集群应独立部署；
测试使用同一 PostgreSQL 服务中的多个独立 database，仅验证路由和数据隔离，
不代表已经验收独立故障域。

## 请求与授权

登录：`POST /api/auth/login`，JSON 为 `{"email":"…","password":"…"}`。
响应返回 `tsp_cs_…` 会话。使用 `Authorization: Bearer …` 调用
`GET /api/tenants` 获取当前工作空间目录。

所有业务路径显式包含工作空间：

```text
/api/tenants/{tenant_id}/watchlist
/api/tenants/{tenant_id}/watchlist/items/{symbol}
/api/tenants/{tenant_id}/watchlist/items/{symbol}/groups
/api/tenants/{tenant_id}/watchlist/groups
/api/tenants/{tenant_id}/watchlist/groups/reorder
/api/tenants/{tenant_id}/watchlist/groups/{group_id}
/api/tenants/{tenant_id}/members
/api/tenants/{tenant_id}/members/{user_id}
/api/tenants/{tenant_id}/tokens
/api/tenants/{tenant_id}/tokens/{token_id}
/api/tenants/{tenant_id}/strategy-templates
/api/tenants/{tenant_id}/strategies
/api/tenants/{tenant_id}/strategies/{strategy_id}
/api/tenants/{tenant_id}/strategies/{strategy_id}/run
/api/tenants/{tenant_id}/strategy-jobs
/api/tenants/{tenant_id}/strategy-jobs/{job_id}/cancel
/api/tenants/{tenant_id}/monitor-rules
/api/tenants/{tenant_id}/alerts
/api/tenants/{tenant_id}/alerts/{alert_id}/read
/api/tenants/{tenant_id}/paper/accounts
/api/tenants/{tenant_id}/paper/accounts/{account_id}
/api/tenants/{tenant_id}/paper/accounts/{account_id}/freeze
/api/tenants/{tenant_id}/paper/accounts/{account_id}/orders
/api/tenants/{tenant_id}/paper/accounts/{account_id}/orders/{order_id}
/api/tenants/{tenant_id}/notification-channels
/api/tenants/{tenant_id}/notification-deliveries
/api/tenants/{tenant_id}/notification-deliveries/{delivery_id}/retry
/api/tenants/{tenant_id}/trade-automations
/api/tenants/{tenant_id}/trade-intents
```

业务请求同时携带 `X-Routing-Epoch`。它是客户端观察到的目录版本，不能替代授权。
客户端传入的 tenant ID、epoch 和其他身份头均不被当作已授权事实。服务端重新查询
真实身份、成员和 placement，再创建不可变 `TenantContext`。

| 角色 | 自选读取 | 自选修改 | 成员管理 | 创建本人 Token |
| --- | --- | --- | --- | --- |
| owner | 是 | 是 | 是，可管理 owner | 是 |
| admin | 是 | 是 | 是，不能修改/授予 owner | 是 |
| member | 是 | 是 | 否 | 是 |
| viewer | 是 | 否 | 否 | 否 |

四种业务均开放 `read` / `write` scope：
`watchlist`、`strategies`、`alerts`、`paper`。上表的自选权限同样适用于其他三种业务。
Token 无法管理成员或创建其他 Token。
Token 权限取申请权限与创建者当前角色权限的交集。成员变更或租户状态变更推进
`policy_version`，已有 Token 失效，需重新创建；用户会话则在下一次请求重新读取角色。
退出使该会话在所有 Cell 中失效。

常见错误：缺凭据 401，无成员/角色不足 403，错误 Cell 或旧 epoch 409，
缺少 epoch/输入不合法 422，数据库连接不可用 503。
客户端收到路由错误应重新拉取自己的目录，不能静默向旧入口重试。

权限校验事务在本次短业务事务结束前保持成员与租户锁，成员撤销等待已开始的
请求完成；撤销提交后的请求重新检查权限。业务 SQL 超时设为 10 秒，
连接池等待默认 5 秒。本批允许短请求同时占用身份和业务两条数据库连接，
尚未实现 v2 的独立身份服务、授权副本、缓存与撤销广播。

登录失败按邮箱摘要在数据库中统计，15 分钟内最多 10 次失败。
生产入口仍需提供全局/IP 限流、请求体限制和 TLS；本批未实现注册邮件、
密码重置或 OIDC。失效会话、过期 Token、登录尝试、已完成任务、告警和审计保留应由运维按保留期清理。

## 前端和业务执行

`VITE_CELL_MODE=true` 构建 `CellApp`，启动后先从身份服务获取工作空间目录，再使用目录里的
Cell origin、tenant ID 和 routing epoch 发请求。默认身份服务与前端同域；分离部署时构建设置
`VITE_CELL_AUTH_ORIGIN`，各 Cell 通过 `TSP_CELL_CORS_ORIGINS` 显式允许前端 origin。
Cell origin 必须使用 HTTPS，本地回环开发例外。前端随 API 镜像交付，访问根路径即可登录。

每个会话和工作空间拥有独立 React Query 缓存，不使用旧全局持久化缓存。工作空间、角色、
策略授权版本、路由地址或 epoch 变化会销毁当前页面状态、取消旧请求并丢弃迟到结果。
只读成员隐藏写入操作，后端仍独立校验权限。当前 Cell 页面提供四类业务的常用操作；
成员/Token 管理及自选分组的完整编辑仍通过 API 操作。

策略目录由完整 backend 运维环境的 `sync-strategies` 发布，API 不动态加载策略代码。
仅开放受信任平台内置日线策略，不接受用户 Python、文件路径、扩展或任意 SQL。
参数根据平台元数据验证；修改策略必须携带旧 `version`，冲突返回 409。
每租户最多 100 个配置、1 个排队/执行中的任务、1000 只自选作为运行池。
提交冻结策略版本、模板代码哈希、股票池、观察日期、提交者及 Token ID。
任务的 idempotency key 在租户内唯一；同请求返回原任务，不同请求复用编号返回 409。

策略 Worker 使用 `FOR UPDATE SKIP LOCKED` 领取任务并写入 execution epoch 与租约。
计算在独立进程执行，进程不继承数据库、AI 或行情密钥环境变量；运行前及结果提交前
重新复核创建者成员权限、policy/routing version 和提交 Token 的存续/权限。
配置后续编辑不改变已排队的冻结版本；模板代码改变会使旧任务失败，需要显式保存并重新提交。
超时/崩溃的租约到期后标记 interrupted，旧执行不能提交；任务不会自动重跑。
取消后的结果也不能写入。进程墙钟/CPU 默认 60 秒，Linux 虚拟地址空间上限 8 GiB，
输入 JSON 上限 64 MiB，Worker 容器默认 2 GiB/2 CPU。Numba 缓存写入临时目录。
虚拟地址空间与实际驻留内存不同；实际内存由部署 cgroup 限制，裸进程部署也须配置该限制。
这是受信任内置策略的进程隔离，不能作为用户代码安全沙箱。

实时 Worker 与策略 Worker 分开运行。监控规则只支持价格及小数涨跌幅，例如 `0.05` 表示 5%。
每租户最多 100 条规则；默认 300 秒冷却。规则版本、最近行情序号、触发时间均写入 PostgreSQL，
Worker 重启不重发同一行情。暂停/删除及已读操作按租户隔离。
收件箱和投递状态使用短轮询。邮件/Webhook 经独立 Worker 投递；短信和 SSE 尚未接入。

模拟盘订单只使用后续有效行情整笔撮合，不模拟真实盘口、排队量、部分成交或券商订单。
每租户最多 20 个账户。提交校验资金/持仓占用和 100 股整数手；股票当日买入不可当日卖出，
成交台账记录交易日并按先买先释放的批次计算可卖数量。账户锁串行化下单、撤单和撮合，
防止重复成交、超卖及并发现金透支；成交台账对运行角色只授予 SELECT/INSERT。
撮合计入原系统的滑点、最低佣金和卖出印花税规则，触及涨跌停不成交。
订单必须使用比提交参考更新的行情序号且报价时间不早于订单时间；跨过北京时间日期的
未成交订单到期并释放占用，冻结账户同样到期。行情不可用时同编号重试仍能取回既有订单。
下一开盘/收盘撮合、权益曲线和旧模拟盘导入尚未实现。

### 调度、通知和自动交易

Schema v3 新增 `scheduler` 元数据表和最小 `SECURITY DEFINER` 函数。
业务表 INSERT/UPDATE/DELETE 在同一事务内唤醒对应租户，回滚不会留下孤立任务。
运行角色不能直接读写调度表，只能调用 `claim` / `finish`；该接口属于受信任平台，
不向用户暴露。调度表不保存业务正文、行情、收件地址或密钥。
`due_at,tenant_id` 排序、`SKIP LOCKED` 和一租户/类型一租约实现公平领取。
唤醒版本阻止领取后的新业务被完成确认覆盖；租约所有者和期限阻止旧 Worker 释放新租约。
无业务租户停驻，暂停租户每 60 秒复核，恢复后继续运行。
跨副本共享槽位预算为 realtime 32、strategies 4、notifications 8、automation 8；
调整预算须由运维修改 `scheduler.slots`，不能仅靠增加副本绕过预算。
空队列等待 250ms，有待处理批次立即继续；报价按本批证券并集每 1000 只合并。
Schema v4 将订阅读取并行化、规则状态按租户批量 upsert，Worker 复用线程池。
事务身份/超时设置合并为一次 SQL。实时间隔改为 1 秒，Compose 实时 Worker
默认 32 并发、身份/业务各 16 连接；其他 Worker 保持独立小池。
这是 PostgreSQL 持久化调度，尚未引入 JetStream 或跨 Cell 行情推送。

v4 的容量按实际规则证券数计费：每个启用规则的证券引用 1 单位，
每个待成交订单 4 单位，每条自动交易规则的最大持仓每只 4 单位。
默认每 Cell 200 单位、每租户 100 单位、1000 个登记租户。
触发器在容量行锁内核算，跨 API 副本并发不能超额；超额写入返回
`429 cell_capacity_exceeded` 并整体回滚。暂停、撤单等减少负载的操作始终允许。
旧 Cell 升级保留已有负载，超额时拒绝进一步增加，需生成拆分方案。
这是预留容量模型，不是不同硬件下的绝对 SLA；策略历史成本仍须单独评估。

`select-cell` 以预期负载和租户配额的最大利用率选新 Cell，排除 draining/disabled；
`provision-tenant --realtime-units` 在目录 Cell 锁内原子检查容量。
`sync-cell-load` 将业务负载快照同步到目录预留，`cell-load` 导出实际负载，
`split-plan` 对输入租户按负载从高到低分配并输出 JSON，不改变既有路由或移动业务数据。
目录快照可能滞后，业务库准入是最后一道容量限制。同步负载/配置容量时须暂停建租户。
已有租户在线换组及跨库搬迁工具仍未实现。

```bash
.venv/bin/python -m app.cell.admin cell-capacity --cell-id cell-a \
  --max-tenants 1000 --realtime-units 200 --max-tenant-units 100
.venv/bin/python -m app.cell.admin sync-cell-load --cell-id cell-a
.venv/bin/python -m app.cell.admin select-cell --realtime-units 10
.venv/bin/python -m app.cell.admin cell-load --output /tmp/cell-load.json
.venv/bin/python -m app.cell.admin split-plan --input /tmp/cell-load.json --output /tmp/cell-split.json
```

单机 v4 实测：200 个活跃租户、各一规则、20ms 模拟报价延迟，连续 30 轮，
最慢轮 1.477 秒，60 秒生产式 250ms 空闲等待下最大刷新间隔 1.342 秒；
每租户至少刷新 50 次，前台 1144 次请求无错误。20 租户各十规则、首轮触发 200 告警，
最慢轮 0.236 秒、最大刷新间隔 1.163 秒。
这些是同一专用 PostgreSQL 上的受控样本；不同证券、真实平台网络、
大量交易台账、长期压力和多 Cell 同时运行未据此验收。
万级全活跃需按实测权重规划约 50 个 200 单位 Cell，并以独立资源验证；
50 个 Cell 的总体吞吐量不是本次单机实测结果。

告警插入与通知 Outbox 在同一业务事务提交；渠道默认关闭，启用只影响之后产生的提醒。
仅 owner/admin 的会话可配置渠道。邮件使用运维配置的全局 SMTP（TLS 或 STARTTLS），
密码仅进入通知 Worker。Webhook 仅允许运维设置的 HTTPS origin，不跟随重定向。
渠道修改取消旧待投递记录；投递前重新复核启用版本、管理员成员、policy/routing version。
网络操作保持短授权/渠道锁，撤权或暂停等待当前有界投递结束。
投递使用独立进程，总时长最多 8 秒；网络单次读写期限 5 秒，Webhook 不读取响应正文。
子进程仅经 stdin 接收本次投递配置，不继承数据库/平台密钥环境。
错误按 5/10/20/40/80 秒退避，最多 6 次，永久错误/超限进入 dead，管理员可手动重试。
崩溃后的 sending 租约到期可重领。每条投递保持相同 `Idempotency-Key` / 邮件 Message-ID；
外部成功与数据库确认不具备分布式原子性，因此是至少一次投递，接收端必须去重。
Message-ID 本身不能保证所有邮箱去重。运维必须控制允许 origin 的 DNS 和网络出口；
这里未实现租户 Webhook 签名密钥/KMS 或自有 SMTP。

自动交易只进入虚拟模拟账户，默认关闭，每账户最多一条规则。创建需要会话且同时
具有策略/模拟盘写权限；可配置策略、北京时间运行时刻、每单预算、最多持仓、
止损/止盈小数和每日下单上限。每日调度要求平台当日新鲜交易报价，周末/休市不会
仅因系统日期触发；日线历史观察截止设为前一自然日，网关只返回截止前存在的交易日数据。
同日运行 key 唯一，冻结策略版本及提交者。策略结果只对其冻结股票池生成交易信号，
信号 24 小时有效；下单参考行情必须晚于信号时间，成交行情还必须晚于订单参考序号。
同信号订单编号固定，事务串行消费防止重复下单。
持仓限制包含待成交买单，同证券不重复买入。预算包含滑点/佣金，成交跳价超过预算
使订单到期。卖单只释放 T+1 可卖数量；止盈止损每日每证券一条信号，待下一报价撮合，
仍受涨跌停、费用、资金与每日订单配额约束。规则修改/暂停或创建者权限变化取消
待下单信号与自动订单；已成交持仓保留，由用户管理。规则暂停不取消手动订单。

共享行情门面位于旧平台入口的 `/internal/cell-market/quotes` 与 `/history`，使用
`TSP_PLATFORM_MARKET_TOKEN` 校验 Bearer Token，复用已有标准行情服务和 Repository。
生产代理应仅向 Cell 内网开放该路径。Cell 设置相同服务 Token 及 `TSP_CELL_MARKET_URL`，
该 URL 为平台 origin。报价保留原始 source time，不以请求时间更新新鲜度；
超过 15 秒、明显未来、停牌或非交易时段的行情不参与评估。
下单额外要求当日有效涨跌停元数据；告警无需该元数据。
历史按所需窗口裁剪、排除未来日期、将非有限指标值转换为 null，返回内容摘要 release ID。
该摘要标识读取的观察快照，当前没有长期不可变行情发布仓库及完整回测重放保证。

## 数据库和部署

环境变量：

| 变量 | 用途 |
| --- | --- |
| `TSP_CELL_ID` | 当前 Cell，如 `cell-a` |
| `TSP_CELL_CONTROL_DSN` | 身份目录数据库的运行角色连接 |
| `TSP_CELL_DATA_DSN` | 当前 Cell 业务数据库的运行角色连接 |
| `TSP_CELL_POOL_SIZE` | 每个池的上限，默认 4；一个 API 副本共有两个池 |
| `TSP_CELL_POOL_TIMEOUT` | 等待连接秒数，默认 5 |
| `TSP_CELL_SESSION_TTL_SECONDS` | 会话期限，默认 86400 秒 |
| `TSP_CELL_MARKET_URL` | 共享行情平台 origin |
| `TSP_CELL_MARKET_TOKEN` | 平台只读服务 Token；不下发至前端 |
| `TSP_CELL_STATIC_DIR` | Cell 前端构建目录；API 镜像已配置 |
| `TSP_CELL_CORS_ORIGINS` | JSON origin 数组，默认 `[]` |
| `TSP_CELL_WORKER_KIND` | `realtime` / `strategies` / `notifications` / `automation` |
| `TSP_CELL_WORKER_TIMEOUT` | 内置策略进程期限，默认 60 秒 |
| `TSP_CELL_WORKER_CONCURRENCY` | 单 Worker 并发默认 2，受 Cell 槽位上限约束 |
| `TSP_CELL_REALTIME_INTERVAL` | 活跃实时租户复查间隔默认 1 秒，配合容量准入控制 |
| `TSP_CELL_SMTP_HOST/PORT/FROM` | 全局邮件服务，默认端口 465 |
| `TSP_CELL_SMTP_USER/PASSWORD/SECURITY` | Worker 邮件认证，tls / starttls |
| `TSP_CELL_WEBHOOK_ORIGINS` | 运维允许的 HTTPS origin JSON 数组，默认 `[]` |

配置不读取旧 `.env` 或 `DATA_DIR`。启动拒绝超级用户、BYPASSRLS、业务表所有者
及其成员身份作为运行角色；拒绝错误的 Cell 业务库或未启用 FORCE RLS 的租户表。

身份运行角色属于可信控制层，可读取全局身份和 placement，
具有会话/成员/Token 的受限 DML 权限；业务运行角色只能访问所在 Cell。
RLS 防止 SQL 遗漏租户条件，仍信任应用鉴权并注入上下文。
运行凭据不能交给用户代码、SQL、策略沙箱或扩展。

每个事务通过参数化 `set_config('app.tenant_id', tenant_id, true)` 注入上下文，
提交/回滚后失效。租户业务表使用 `USING` 和 `WITH CHECK`。
业务角色没有 TRUNCATE、DDL 或所有者权限。迁移角色单独配置并只在运维进程使用。

先由数据库管理员创建数据库和运行角色，分别在身份集群及每个 Cell 集群执行。
示例角色定义：

```sql
CREATE ROLE tsp_control_runtime LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
CREATE ROLE tsp_cell_a_runtime LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
```

使用数据库管理工具为角色设置随机密码和连接权限。运行角色不得成为迁移/所有者
角色的成员。库和表的所有者是迁移角色，业务角色由脚本授予最小表权限。
本批因 PostgreSQL 行锁需要 UPDATE 权限，身份表额外授予 `UPDATE(id)`，
业务绑定表授予 `UPDATE(tenant_id)`；实际路由/角色授权仍由可信服务控制。

在 backend 环境安装可选依赖；已有开发环境可使用增量安装保留其他 extras：

```bash
uv pip install --python .venv/bin/python 'psycopg[binary,pool]>=3.2,<4'
```

设置 `TSP_CONTROL_ADMIN_DSN` 和 `TSP_DATA_ADMIN_DSN` 为各数据库的迁移角色，
然后在 `backend/` 运行：

```bash
.venv/bin/python -m app.cell.admin migrate --kind control --runtime-role tsp_control_runtime
.venv/bin/python -m app.cell.admin migrate --kind data --runtime-role tsp_cell_a_runtime --cell-id cell-a
.venv/bin/python -m app.cell.admin register-cell --cell-id cell-a --api-url https://cell-a.example.com
.venv/bin/python -m app.cell.admin sync-strategies
.venv/bin/python -m app.cell.admin create-user --email owner@example.com
.venv/bin/python -m app.cell.admin provision-tenant --tenant-id workspace-001 --name 团队 \
  --cell-id cell-a --owner-id <create-user 返回的 user_id>
```

密码通过交互输入，自动化可使用 `TSP_BOOTSTRAP_PASSWORD`。
初始化不会在 API 启动时执行。Migration v1/v2/v3/v4 保存 checksum，旧版本自动追加后续版本，
重复执行不重复建表，版本/校验和不一致时失败；下一版结构须添加新迁移。

建租户先写 `moving` 目录，再写对应 Cell 的绑定，成功后激活。
中途失败不会开放业务，可用相同参数重试。已有租户不能通过此命令换 Cell、
改 owner 或重置 epoch。在线迁移/换组工具尚未实现。

使用运行角色启动：

```bash
.venv/bin/python -m uvicorn app.cell.main:create_app --factory --host 127.0.0.1 --port 3020
```

独立镜像/Compose：

```bash
docker build -f deploy/cell/Dockerfile -t tsp-cell-api:local .
docker build -f deploy/cell/Worker.Dockerfile -t tsp-cell-worker:local .
docker compose -p tsp-cell-a -f deploy/cell/compose.yml --profile worker up -d
```

Compose 使用外部 PostgreSQL，不挂载旧数据目录或旧环境文件；管理员 DSN 不传入 API。
API 镜像包含 Cell API、纯费用函数和 Cell 前端，依赖从项目锁文件提取，非 root、只读运行，
不包含旧入口、行情驱动或策略加载器。Worker 镜像使用锁定完整 backend Python 依赖，
只启动 Cell Worker，不启动旧采集/调度；数据库与平台服务凭据不传入计算子进程。
旧数据 Parquet 导入和策略目录同步在完整 backend 运维环境执行。
生产用 TLS 代理转发到 loopback 端口；多 Cell 使用不同 compose project 和端口。
探针为 `/health/live` 和 `/health/ready`。

## 旧数据迁移

先取得停写后的 `data/user_data` 一致副本。账号 ID 和 PBKDF2 密码摘要原样导入，
身份冲突立即中止。旧纯密码模式先在旧系统绑定邮箱。
所有旧账号只导入身份；成员关系逐个显式添加，只有指定 owner 获得原共享自选的权限。
会话、旧全局 Token、SMTP/AI/交易凭据以及其他业务数据不自动迁移。

```bash
.venv/bin/python -m app.cell.admin import-users --directory /path/to/offline-user_data --owner-id <旧账号ID>
.venv/bin/python -m app.cell.admin provision-tenant --tenant-id legacy --name 原工作空间 \
  --cell-id cell-a --owner-id <旧账号ID>
.venv/bin/python -m app.cell.admin tenant-status --tenant-id legacy --status suspended --actor-id <操作人ID>
.venv/bin/python -m app.cell.admin import-legacy --directory /path/to/offline-user_data \
  --tenant-id legacy --owner-id <旧账号ID>
.venv/bin/python -m app.cell.admin tenant-status --tenant-id legacy --status active --actor-id <操作人ID>
```

目标必须为空，导入不会覆盖。保留原自选添加时间，旧单值 `group_id` 转为多值归属。
管理端即使能绕过 RLS，也在目标检查中显式过滤 tenant。
身份与业务分别提交，不声称跨库原子性；失败保持租户停用，
应核对两库结果后再激活，禁止直接覆盖重试。

## 验证

真实 PostgreSQL 测试创建随机命名的运行角色和三个临时 database：
一个目录库、两个 Cell 业务库。结束后清除这些测试资源。
仅对专用测试服务运行：

```bash
TSP_TEST_POSTGRES_DSN=<专用PostgreSQL管理员连接> \
  .venv/bin/python -m pytest tests/test_cell_isolation.py tests/test_cell_business.py tests/test_cell_scheduler.py \
  tests/test_cell_notifications.py tests/test_cell_automation.py tests/test_cell_market.py -q
.venv/bin/ruff check app/cell tests/test_cell* ../deploy/cell/select_requirements.py
```

本地验证包含真实 PostgreSQL 隔离/权限/迁移与业务回归、真实策略子进程、
非 root/只读 API 及 Worker 容器、前端测试与 Cell 构建。
前一阶段验收：后端 106 项回归、2 项容器测试、前端 121 项测试通过。
本阶段额外包含调度、通知和自动交易回归，以及 10,000 租户真实 PostgreSQL 压测。
本阶段最终验证：93 项后端定向回归、2 项容器烟测、122 项前端测试通过。
v4 容量/刷新优化另通过 98 项后端定向回归、122 项前端测试与构建；
容量超限前端提示已同步。两项容器烟测使用已有依赖镜像覆盖最新 Cell 源码通过；
完整镜像构建受 Docker Hub TLS 超时影响，需在发布环境重跑。
API/Worker 镜像构建、Compose 配置、Ruff、ESLint 和 diff 检查通过。
`.github/workflows/cell.yml` 对 Cell 变更启动真实 PostgreSQL、
构建两类镜像并运行同一套检查及前端回归。

本地容器烟测需额外设置 `TSP_TEST_CELL_IMAGE`、`TSP_TEST_CELL_WORKER_IMAGE`、`TSP_TEST_POSTGRES_CONTAINER`
以及可选 `TSP_TEST_DOCKER_CONTEXT`，运行 `tests/test_cell_container.py`。
烟测临时 API 容器共享专用测试 PostgreSQL 容器的网络，
结束后清理；不需要暴露新的 API 端口。

覆盖跨租户同证券、跨租户分组、两 Cell 并发、固定 placement、
错误 Cell/epoch、四角色、最后一个 owner、Token 作用域/撤销/降权、
租户停用、退出、无上下文 RLS、跨租户写入、复合外键、
连接提交/回滚重用、DDL/TRUNCATE 禁止、错误数据库身份、
旧密码兼容、旧数据导入、未迁移路由关闭、业务只读角色、策略 Token 撤销复核、
告警冷却、订单幂等、资金占用、并发撮合/撤单/卖单、冻结、跨日到期及成交台账不可改写。
浏览器实际验证了登录、自选添加、策略配置/运行、告警规则/收件箱、模拟账户/下单/成交，
以及两工作空间四类业务数据隔离；桌面视图无横向溢出。

## 后续生产阶段

这批交付未切换线上入口，也未自动搬迁旧业务数据。单机实测（PostgreSQL 17.11、
macOS/Colima、本地 TCP、32 Worker 并发、每池 16 连接、每租户一规则/一证券）：
10,000 空闲租户索引调度 P95 0.989ms，对照遍历所有租户 74.552 秒；
10,000 活跃租户首轮 60.451 秒、165.42 租户/秒、313 次合并报价调用；
前台自选接口 962 次、0 错误、P95 24.843ms。200 活跃租户补测首轮 1.670 秒。
报价为进程内模拟数据，无真实上游网络或策略计算，无触发告警写放大。
本轮是调度吞吐量/隔离验证，不是万级单 Cell 两秒刷新承诺；200 样本也不是生产配额。
需按活跃订阅和策略成本分组，并对多规则、独立主机、长期周期、故障和交易时段验证。
可复现工具（随机独立测试数据库/角色，结束自动清理）：

```bash
TSP_TEST_POSTGRES_DSN=<专用测试PostgreSQL> PYTHONPATH=. \
  .venv/bin/python scripts/benchmark_cell.py --tenants 10000 --baseline --output /tmp/cell-capacity.json
```

CI 用 200 租户、5 轮、20ms 模拟延迟验证压测工具并保存 JSON。策略队列 10,000 租户领取/释放约 9.7 秒，
不包含策略计算；4 个 Cell 策略槽位按真实单任务耗时决定排队延迟。
进入容量验收前，需要 Collector 独立部署、共享行情发布仓库、事件/任务网关、
身份服务/每 Cell 授权副本、PgBouncer、备份灾备和交易时段压力测试。
这些工作与租户密钥、完整回测及券商交易接入共同构成后续生产阶段。
