# 二期第一步：单机关键功能迭代实施记录

日期：2026-10-16
原始执行范围：本地实现与验证，当时不含提交、推送、候选部署或生产切流。

归档状态：相关实现和本记录现已进入 `main`；候选部署和生产切流仍未执行。当前
版本分类见[版本基线](../../docs/version-baseline.md)。

## 1. 结果

第一步约定的实时网关、公共行情按需读模型、短期流授权、跨进程撤权、私有事件恢复、前端换票重连和分项就绪状态已在单机代码中完成。当前状态为：

**本地实现与一次性 PostgreSQL 百人功能验收完成；候选主机网络容量、真实交易时段观测和发布/回退演练待执行。**

本阶段没有改变产品角色、RLS 隔离、业务权限、金融数据口径和原开放 API Token 的 HTTP scope。完整协议见 [单机实时网关契约](../../docs/reference/integrations.md#gateway)。

## 2. 已实现内容

### 2.1 独立实时网关

- 新增 `SERVICE_ROLE=gateway` 单进程入口，只开放事件流、换票和健康路由。
- Edge 和候选 Nginx 对 `/api/events`、`/api/events/ticket` 使用精确路由；其他请求仍进入完整 API。
- Gateway 不挂载公共行情文件、平台配置或计算 socket，独立设置 1 CPU、1 GiB 上限。
- 就绪检查拆分为双数据库、私有事件 LISTEN、撤权 LISTEN 和平台行情 marker。

### 2.2 短期票据与撤权

- 使用共享 HMAC 密钥签发 `tss_` 一次性票据，硬 TTL 为 60 秒。
- 票据绑定用户、租户、Cell、角色、权限、授权版本、策略版本、路由 epoch 和 principal。
- Cookie 会话绑定会话摘要；开放 API Token 绑定 `token_id` 并继承完整 `full_scopes`。
- API Token 私有事件按 scope 白名单过滤，未知事件失败关闭；`read:market` 不能订阅分析类私有事件。
- Token 吊销、密码/账号授权变化、租户切换等通过事务内 PostgreSQL NOTIFY 广播；广播中断时由 60 秒 TTL 收敛。

### 2.3 私有事件恢复

- 私有事件先写 `business.full_events`，通知只作为租户级唤醒信号。
- 相同租户的多个订阅者合并为一次 SQL 读取，不再固定每 500 ms 扫描所有订阅租户。
- 通知丢失或监听重连时每 30 秒补扫活跃订阅租户。
- 慢消费者队列满时发送 `events_reset`，前端重新读取权威 REST 状态，不宣称保留了被覆盖的完整事件历史。

### 2.4 公共行情读模型

- Platform 新增内部 SSE marker，只发送 `quotes_updated`、`depth_updated`，不复制行情明细。
- Gateway 只转发 marker；API 进程收到 marker 后增加 generation。
- API 仅在实际业务请求需要且 generation 过期时拉取一次公共快照，同一进程并发刷新受锁保护。
- marker 断开时保留最多 5 秒的按请求惰性刷新后备；启动时完成一次初始快照。

### 2.5 前端与运行状态

- 多租户前端在每次连接和重连前调用换票端点，并保留最后私有事件游标。
- 重复私有 sequence 被忽略；`events_reset` 触发查询缓存失效和权威状态重取。
- 退出、切租户和授权变化沿用账户 scope 清理旧订阅与私有缓存。
- API 与 Gateway 分别报告功能就绪，研究/通知等后台状态不再与实时网关监听状态混成单一进程存活。

## 3. 热路径基准

基准脚本：`backend/scripts/benchmark_full_quote_bridge.py`
原始结果：[quote-bridge-benchmark.json](quote-bridge-benchmark.json)

参数：4 个 API worker、60 个 1 Hz marker、每个 marker 后每个活跃 worker 20 次业务请求、每次快照解码 4 个 frame。启动刷新不计入结果。

| 场景 | 快照拉取/marker | frame 解码/marker | 说明 |
| --- | ---: | ---: | --- |
| 旧固定 1 Hz 模型 | 4 | 16 | 理论模型，4 worker 无条件轮询 |
| 新模型，无活跃请求 | 0 | 0 | 实测 |
| 新模型，1 个活跃 worker | 1 | 4 | 实测，20 次请求合并为一次刷新 |
| 新模型，4 个活跃 worker | 4 | 16 | 实测，最坏活跃情况不高于旧模型 |

该基准证明空闲 API worker 不再持续拉取和解码全市场快照，并证明同一 worker 的请求可合并；它不是生产容量或端到端时延结论。

## 4. 验证结果

| 验证 | 结果 |
| --- | --- |
| 新增票据、网关、就绪、定向事件唤醒测试 | 通过 |
| 真实 PostgreSQL：票据、管理、平台、监控联合回归 | `37 passed` |
| 后端全量（包含真实 PostgreSQL） | `3264 passed, 5 failed, 7 skipped`；5 项均为下述已知基线/契约漂移 |
| 本地一次性 PostgreSQL 百人功能验收 | 100/100 登录；5 轮共 500/500 工作流；私有事件与公共 marker 均 100/100 |
| 前端完整单测 | `155 passed` |
| 前端生产构建 | 通过 |
| 前端 lint | `0 errors`，保留 `77` 项既有 warning |
| 定向 Ruff | 通过 |
| Compose 配置展开 | 通过 |
| 事务内撤权 NOTIFY 手工验证 | 通过 |

初次后端全量结果为 `3258 passed, 10 failed, 7 skipped`。其中 5 项由本次实现引起，已修复并由上述真实 PostgreSQL 联合回归覆盖：

- 4 项精简监控测试的 `quote_service` 替身没有 `ensure_fresh`；
- 1 项旧测试仍断言开放 API Token 不可换取租户流票据。

初次全量中的其余 5 项已独立复现为当前分支基线/契约漂移，不属于本次实时网关改动：

- `test_auto_mining`：返回 503；
- `test_enriched_incremental_memory[False]`：存在多个 `quote_time_basis`；
- `test_watchlist_ocr`：实际新增 3 项，旧预期为 2；
- 2 项 OpenAPI 契约快照漂移：新增模拟盘接口，当前 SHA 为 `464e425b9a87f77e3bf5bcbd2a0dc10be906b40fcc8da2a5ba58d931dac910fd`。

最终全量从初次 10 项失败降为上述 5 项，确认本次实现引入的失败已全部消除。本记录不把仍存在的基线失败写成全量测试通过。

### 4.1 本地百人功能验收

验收器：`backend/scripts/accept_phase2_step1.py`
原始结果：[local-100-user-acceptance.json](local-100-user-acceptance.json)

脚本只接受可创建和删除数据库/角色的一次性 PostgreSQL 根 DSN。它自动创建两库及最小运行角色，完成后强制删除全部临时数据库与角色，不读取生产数据。

本轮使用 100 个独立用户及个人租户、32 条双库连接池并执行 5 轮并发工作流。每位用户每轮完成自选写入、按租户回读、策略目录读取、60 秒流换票及重放拒绝。

| 指标 | 结果 |
| --- | ---: |
| 100 人并发登录 | 100/100；总耗时 1.092 秒；P95 477.038ms |
| 第 1 轮工作流 | 100/100；P95 1,659.499ms |
| 第 2 轮工作流 | 100/100；P95 1,698.705ms |
| 第 3 轮工作流 | 100/100；P95 1,934.581ms |
| 第 4 轮工作流 | 100/100；P95 1,517.921ms |
| 第 5 轮工作流 | 100/100；P95 1,426.298ms |
| 私有事件 LISTEN/RLS 隔离 | 100/100；一次租户定向轮询读取 100 个租户；598.040ms |
| 公共行情 marker 广播 | 100/100；147.208ms |
| 真实控制库 LISTEN 撤权 | 通过；23.917ms |

该结果验证独立身份、RLS、私有写读、策略读取、换票、一次性消费、事件按租户唤醒及撤权链路。HTTP 通过进程内 ASGI 执行，未经过 Nginx、容器网络或真实平台行情，因此属于功能与数据库并发证据，不是候选主机容量、SSE 长连接容量或交易时段时延签收。

复现命令：

```bash
cd backend
TSP_TEST_POSTGRES_DSN='<一次性 PostgreSQL 根 DSN>' \
  uv run python -m scripts.accept_phase2_step1 \
  --users 100 --rounds 5 --pool-size 32 \
  --output ../reports/phase2-step1-20261016/local-100-user-acceptance.json
```

## 5. 部署与回退准备

- `deploy/full/compose.yml` 已增加独立 Gateway 和回环端口 `3031`。
- `deploy/edge/https.conf` 与候选 Nginx 已增加精确事件路由和 SSE 禁缓冲配置。
- `deploy/full/README.md` 已补共享密钥、LISTEN direct/session 连接、启动顺序、深度就绪和实时网关单独回退步骤。
- 当前没有 schema 变更。仅回退 Gateway 时，先恢复两个事件路由至兼容 API upstream，再停止 Gateway，不影响普通 API 和业务数据。

## 6. 尚未执行

以下内容必须在候选环境完成后，才能完成第一步的部署签收：

- 100 个独立活跃用户经过 Nginx、容器网络、独立 Gateway/API 进程的完整业务回归与 SSE 长连接容量；
- 真实交易时段的行情新鲜度、SSE 重连、慢消费者和资源曲线；
- 撤权广播中断、Gateway/API 重启、Platform marker 中断演练；
- 候选环境 `nginx -t`、切流与单独回退演练；
- 生产备份、发布与恢复验证。

本机没有 Nginx 可执行文件，也没有可用的 Nginx 镜像，因此只完成了配置审阅和 Compose 展开，未执行 `nginx -t`。本次执行结束时按要求未提交、未推送、未部署生产；其后代码与记录已归并到 `main`，生产状态未因此改变。
