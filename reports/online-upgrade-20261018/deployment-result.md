# v0.4.8 火山引擎线上升级结果

执行日期：2026-10-18（北京时间）。

## 结论

火山引擎上海 ECS-XM（`118.196.108.188`）已完成 v0.4.8 单机多租户候选发布、双库
schema v5 → v6 迁移和 Edge 切流。发布完成时 API、Gateway、Platform、四类 Worker、
Compute Broker、PostgreSQL 与 Edge 均正常。

本结论只覆盖本次升级和切流回归，不代表 72 小时持续验收或完整交易日验收已通过。

## 发布身份

| 对象 | 固定身份 |
| --- | --- |
| 应用版本 | `0.4.8` |
| 部署提交 | `9b4bc2c5edbda34b1d9330ba95ab68be0b6b87ba` |
| Runtime | `sha256:37ecf4b093387f4eb40602c1571be26dd1a974e3f4d7deabc571dbf90823616d` |
| Compute | `sha256:2b8da7515d6ca7de6d753591027de6a26a9427c6d402e4877d867af94a8ac9e6` |
| Source tree | `a44841a8ae07fb29d222cbf90e65a4e80bb8363ac3a0ffbd3194628d50105290` |
| Release | `/opt/tsp-full/release-9b4bc2c5` |
| 当前指针 | `/opt/tsp-full/current` → 上述 Release |
| 生产元数据 | `/opt/tsp-full/production/release.json` |

七个应用角色使用同一 Runtime，镜像 revision 均为上述提交。Compute 由匹配源码和冻结依赖
构建，Broker 固定到上述 Compute digest。

## 备份与迁移

- 最终 schema v5 备份：
  `/opt/tsp-full/backups/final-v5-pre-v048-20261018T134639+0800`。
- `SHA256SUMS` 中 18 个文件全部重新校验为 `OK`，包括双库 dump、全局角色、Edge、
  Broker、Compose、平台配置、后端和公共行情状态。
- 双库 dump 已在独立恢复库完成恢复验证。
- 火山引擎系统盘快照：`snap-3xescmpkka2wua8mwan6`，状态 `available`，永久保留。
- 控制库 `tsp_full_candidate_control` 和业务库 `tsp_full_candidate_data` 均已迁移至
  schema v6。
- 迁移后核对了运行角色：非超级用户、无 `BYPASSRLS`；业务 RLS 表不存在启用 RLS
  但未启用 `FORCE ROW LEVEL SECURITY` 的遗漏。

## 切流与回归

切流时间为 2026-10-18 13:59:53 +08:00。Edge 配置摘要与候选配置一致，
`nginx -t` 通过，`/api/events` 与 `/api/events/ticket` 精确路由到 Gateway。

实际验证：

- 公网可信 CA 请求：`/health/live`、`/health/ready`、`/api/auth/status` 和完整前端
  HTML 均返回 200；版本为 `0.4.8`。
- 七个应用容器均为 `healthy`，重启数均为 0。
- Research、Realtime、Notifications、Scheduled 心跳健康，最终抽样延迟为 0–4 秒。
- Broker 为 `active`，Unix socket 权限为 `0660 root:tsp-runtime`，无残留 Compute
  容器。
- 实际无网络 Compute 已完成一次计算验证。
- 双租户访问和 FORCE RLS 隔离验证通过。
- SSE 临时会话认证成功；60 秒票据通过 `Last-Event-ID` 补放私有事件；同一票据第二次
  使用返回 401。临时会话和事件已精确删除，线上有效会话恢复为 1，探针事件为 0。
- 切流后七个应用角色日志未匹配到 `traceback`、`exception`、`critical`、`fatal`
  或独立 `error`，Broker 无 warning 及以上日志。
- 最终资源抽样：主机可用内存约 239 GiB，根盘 197 GiB、已用 63 GiB（34%）。
- 最终核验后已从 `root` 的 `authorized_keys` 精确删除本次临时 ED25519 公钥；等待
  5 秒未重新注入，使用该私钥重新登录返回退出码 255。本地临时私钥、公钥和
  `known_hosts` 已删除，未为此重启 ECS。

## 回滚约束

schema v5 代码不得直接连接 schema v6 数据库。回滚必须按以下顺序执行：

1. 停止所有 v0.4.8 写入者。
2. 保存并对账切流后的新增账号、会话、交易和研究产物。
3. 恢复最终双库 schema v5 备份，或恢复与其匹配的系统盘快照。
4. 同步恢复旧 Edge 路由及匹配的 Cell 目录 URL。
5. 确认数据已回到 schema v5 后，才能启动旧版本服务。

禁止只切回旧容器，也禁止直接覆盖并丢弃上线后增量。上述约束已写入线上
`production/release.json`。

## 尚未完成

- 72 小时持续验收尚未到终态，不能宣布通过。
- 完整交易日验收尚未完成。
- `feat/single-host-compose` 候选仍需独立复审、CI 和合入主线；本次部署成功不等于该
  分支已合并。
