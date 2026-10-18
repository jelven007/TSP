# v0.4.8 线上升级记录

2026-10-18。火山引擎上海 ECS-XM（`118.196.108.188`）已完成 v0.4.8
单机多租户候选发布、双库 schema v5 → v6 迁移和 Edge 切流。

## 当前生产状态

- 应用版本：`0.4.8`。
- 部署提交：`9b4bc2c5edbda34b1d9330ba95ab68be0b6b87ba`。
- Runtime：
  `sha256:37ecf4b093387f4eb40602c1571be26dd1a974e3f4d7deabc571dbf90823616d`。
- Compute：
  `sha256:2b8da7515d6ca7de6d753591027de6a26a9427c6d402e4877d867af94a8ac9e6`。
- Release：`/opt/tsp-full/release-9b4bc2c5`；
  `/opt/tsp-full/current` 已原子指向该目录。
- 生产 Compose 与发布元数据已原子更新到 `/opt/tsp-full/production/`。
- 控制库和业务库均为 schema v6。
- Edge 于 2026-10-18 13:59:53 +08:00 完成切流。

`feat/single-host-compose` 候选尚未合入主线。本次部署使用经过线上候选验证的固定提交，
不能把生产成功等同于分支已完成复审和 CI 集成。

## 恢复材料

- 最终 schema v5 备份：
  `/opt/tsp-full/backups/final-v5-pre-v048-20261018T134639+0800`。
- 备份 `SHA256SUMS` 中 18 个文件均校验通过，双库 dump 已完成独立恢复验证。
- 火山引擎系统盘快照：`snap-3xescmpkka2wua8mwan6`，状态 `available`，
  永久保留。
- 旧 Release、镜像、Edge、Broker、Compose、平台配置、公共行情状态和数据库角色均
  保留在备份中。

## 验证结果

- 七个应用角色均使用固定 Runtime，全部 `healthy`，重启数均为 0。
- Broker 为 `active`；实际无网络 Compute 验证成功；无残留 Compute 容器。
- 公网 `/health/live`、`/health/ready`、`/api/auth/status` 与完整前端均返回 200。
- 双租户隔离、运行角色权限和 FORCE RLS 验证通过。
- SSE 会话、`Last-Event-ID` 私有事件补放及票据单次使用验证通过。
- 探针会话和事件已清理，线上有效会话恢复为 1，探针事件为 0。
- 切流后应用日志未发现匹配异常，Broker 无 warning 及以上日志。
- `nginx -t`、生产 Compose 解析和备份摘要复核均通过。
- 临时 SSH 公钥已从 `root` 的 `authorized_keys` 精确删除，原私钥复测无法登录；
  本地临时密钥材料已清理，未重启 ECS。

完整证据及时间线见 [deployment-result.md](./deployment-result.md)。

## 回滚约束

schema v5 旧代码不能直接连接 schema v6 数据库。回滚前必须停止新写入，保存并对账
切流后增量，再配套恢复最终双库 v5 备份或匹配的系统盘快照，同时恢复旧 Edge 路由和
Cell 目录 URL。禁止只切旧容器或丢弃新账号、会话、模拟盘成交和研究产物。

该约束已写入线上 `/opt/tsp-full/production/release.json`。没有生成会无条件覆盖
业务数据的自动回滚脚本。

## 未完成项

72 小时持续验收和完整交易日验收尚未完成，不能宣布通过。既有准备期证据继续保留在
`readiness.json`、`release-artifact.json` 和 `public-readiness-attempt.json`。
