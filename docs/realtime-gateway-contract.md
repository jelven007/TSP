# 单机实时网关契约

日期：2026-10-16。适用范围：完整产品单机多租户部署。

## 1. 服务边界

实时网关是单进程、单副本的 SSE 入口，只开放以下路由：

| 路由 | 用途 |
| --- | --- |
| `POST /api/events/ticket` | 使用当前 Cookie 会话或开放 API Token 换取一次性流票据 |
| `GET /api/events?ticket=...` | 消费票据并建立租户事件流 |
| `/health`、`/health/live`、`/health/ready`、`/api/health` | 存活与分项就绪状态 |

其他 `/api/*` 路由在网关返回 `404 gateway_route_not_found`。普通 HTTP API 继续由 API 服务处理。Edge 必须使用精确路径将上述两个事件路由转发至网关，并关闭 SSE 代理缓冲。

网关不挂载公共行情目录、平台配置目录或计算 Broker socket。它只持有：

- 控制库和业务库的最小运行凭据；
- 平台行情 marker 内部 Token；
- 与 API 服务一致的流票据 HMAC 密钥。

## 2. 换票与消费

客户端先调用 `POST /api/events/ticket`。服务完成权威身份、账号状态、租户成员关系、策略版本和路由 epoch 校验后，签发 `tss_` 前缀的 HMAC 票据。

票据载荷包含：

| 字段 | 约束 |
| --- | --- |
| `aud`、`jti` | 固定 audience 与随机一次性标识 |
| `sub`、`tid`、`cid` | 用户、租户与 Cell 绑定 |
| `role`、`perms` | 当前租户角色和业务权限 |
| `tok`、`scopes` | 开放 API Token 标识与原始 scope；浏览器会话为空 |
| `av`、`pv`、`re` | 账号授权版本、租户策略版本、路由 epoch |
| `prn` | `session:<会话摘要>` 或 `api:<token_id>` |
| `iat`、`exp` | 签发与到期时间 |

硬性规则：

- TTL 最大 60 秒，当前固定为 60 秒；
- 票据只能成功消费一次；
- audience、签名、时间、角色和 principal 绑定任一不合法即返回 401；
- API Token 票据必须保留原 `token_id` 和完整 `full_scopes`，不能提升权限；
- URL、响应和代理均设置 `Cache-Control: no-store`，日志不得记录完整票据。

前端每次连接或重连都重新换票，并携带最后已确认的私有事件游标。票据过期不影响已建立连接的最长授权边界：网关持续检查 claims 到期时间，超过 60 秒时发送 `access_revoked` 并关闭本次流，客户端再执行权威换票。

## 3. API Token 事件权限

浏览器 Cookie 会话按当前租户权限接收全部已定义的租户私有事件。开放 API Token 采用失败关闭规则：

| 私有事件 | 必需 scope |
| --- | --- |
| `strategy_alert` | `read:analysis` |
| `strategy_results_updated` | `read:analysis` |
| `review_progress` | `read:analysis` |
| 未知私有事件 | 不发送 |

`events_reset` 不携带业务数据，可发送给任意有效订阅者，要求客户端通过其已有权限重新读取权威 REST 状态。公共 `quotes_updated` 和 `depth_updated` marker 不含行情明细，所有有效流都可接收；实际数据读取仍经过原 API scope 与租户权限校验。

增加新的私有事件类型时，必须同时：

1. 指定开放 API scope；
2. 更新网关白名单映射；
3. 增加允许、拒绝和未知类型失败关闭测试。

## 4. 撤权与恢复

账号停用、密码变更、租户切换、成员关系或路由变更、API Token 吊销会在控制库事务内发送 `tsp_access_revoked` 通知。网关使用 PostgreSQL 专用 direct/session `LISTEN` 连接更新本地高水位：

- principal 撤权只影响对应会话或 API Token；
- user 撤权影响该用户已有流；
- tenant 撤权影响该租户已有流；
- 通知早于或等于票据签发时间时，流立即结束；
- 广播中断时由 60 秒票据硬 TTL 收敛，不能无限续期。

私有业务事件先写入 `business.full_events`，再发送 `tsp_full_events` 租户唤醒。SQL 是事实源，通知只负责唤醒：

- 同一 API 进程内，相同租户的多个标签页共用一次 SQL 读取；
- 正常情况只读取收到通知的租户；
- 每 30 秒全量补扫一次活跃订阅租户，恢复丢失通知或监听重连；
- 慢消费者队列满时丢弃排队副本并发送 `events_reset`，客户端重新读取权威 REST 状态；
- 私有游标按数据库 `sequence` 去重，公共行情 marker 只表示“有新值”，允许合并。

两个 LISTEN 连接必须直连 PostgreSQL，或经过支持 session pooling 的连接入口；不得经过 PgBouncer transaction pooling。

## 5. 单机部署限制

一次性消费记录保存在网关进程内。因此当前部署必须保持一个网关副本；进程重启会清空已消费集合，但旧票据的 HMAC 仍可能有效至原到期时间。Edge、网关与密钥的单机发布必须满足以下之一：

- 停止接收新连接，等待 60 秒后重启；
- 轮换 HMAC 密钥并同时重启签发端与网关，使旧票据立即失效。

扩展到多网关前，必须将一次性消费改为共享原子存储或粘性签发/消费协议，并重新验证重放、撤权和故障切换。当前契约不声明节点高可用。

## 6. 就绪与回退

`/health/ready` 分别报告：

- 控制库和业务库；
- 私有事件 LISTEN；
- 撤权 LISTEN；
- 平台公共行情 marker。

任一项失败时网关不就绪，但 `/health/live` 仍可用于判断进程存活。回退时先把 `/api/events` 和 `/api/events/ticket` 两个精确路由恢复至兼容 API upstream，验证并 reload Edge，再停止独立网关。普通 API、数据库 schema 和业务数据不因该回退而改变。
