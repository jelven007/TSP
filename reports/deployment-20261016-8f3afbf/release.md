# TSP 8f3afbf7 生产发布记录

## 发布结果

- 状态：成功
- 生产版本：`8f3afbf7ca82047d59ddb7d2b758903b38ce6eb1`
- 切流时间：2026-10-16 10:21:38 至 10:22:03（UTC+8）
- 生产地址：<https://118.196.108.188/>
- 当前 release：`/opt/tsp-full/release-8f3afbf`
- 前一版本：`/opt/tsp-full/release-414cc483`

本次上线“设置 → 系统设置 → 关于”中的独立开源版本监控。该功能监控
`shy3130/tick-stock-panel` 的正式 GitHub Release，不复用应用当前版本、检查更新或
其缓存与已读状态。

六个常驻业务容器统一使用 runtime 镜像：

`sha256:905493c93d2a94a06fdc523bea10764113077f24b163c031108a762fdd6ad2ca`

计算 Broker 沿用：

`sha256:8e2b94f16f52563ab64f49212a12391086601d29222046088e196f6bcc0c2720`

## 备份与恢复验证

生产备份位于：

`/opt/tsp-full/backups/pre-8f3afbf-20261016-102131`

备份共约 489 MB，包含旧镜像归档、旧 Compose 及 platform override、两个数据库、
Broker 代码与 systemd/env、平台配置、Nginx/TLS、快照、容器基线和 release 指针。

- control 库：schema v5，20 张业务表，临时库完整恢复成功
- data 库：schema v5，34 张业务表，临时库完整恢复成功
- 旧镜像归档 SHA-256：`399d0264c92b76acf36a7d15427dd0ae25becc684ac515679fcecc70c14d3c04`
- 回滚脚本：`rollback.py`，权限 `0700`

回滚命令：

```bash
python3 /opt/tsp-full/backups/pre-8f3afbf-20261016-102131/rollback.py --rollback /opt/tsp-full/backups/pre-8f3afbf-20261016-102131
```

本次没有 schema 迁移；回滚脚本会保存故障后的数据库并恢复旧代码、配置、镜像、
Broker 和六个服务，不会用旧数据库覆盖发布后的业务写入。

## 验收结果

- 发布前前端测试：30 个测试文件、152 项全部通过，生产构建成功
- 候选 API：深度健康 9/9，固定提交与运行镜像摘要一致
- 生产 API：发布脚本验收 9/9，切流后独立连续 3 次复检均为 9/9
- 运行状态：六个容器均使用新 runtime，重启次数均为 0，Broker 为 active
- Worker：research、realtime、notifications、scheduled 均以新 owner 健康重建
- 公网 HTTPS：工作机本地 CA、证书主机名和真实公网链路验证通过
- 静态产物：主包、路由包、系统设置包线上 SHA-256 与候选构建一致
- 浏览器：开源监控显示仓库和 `v0.3.7`；应用 `0.3.11` 更新状态独立显示
- 手动检查：GitHub Releases API 返回 HTTP 200，浏览器控制台无错误
- 稳定性：启动后六个容器及 Broker 未发现 error、exception、fatal 或 traceback 日志
- 清理：临时恢复数据库和候选容器残留均为 0，Nginx 配置检查通过

详细证据见同目录 JSON 文件。
