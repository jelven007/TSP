# TSP 414cc483 生产发布记录

## 发布结果

- 状态：成功
- 生产版本：`414cc48379cd6ecdb664d373b22d37dcdac5b29a`
- 切流时间：2026-10-15 20:37:03 至 20:37:26（UTC+8）
- 生产地址：<https://118.196.108.188/>
- 当前 release：`/opt/tsp-full/release-414cc483`
- 前一版本：`/opt/tsp-full/release-2a3b395`

本次统一上线 research/overlay、模拟盘风控与诊断、管理员租户命名，以及
`4db757e6`、`ddb5f799` 两次 platform 热修。六个常驻业务容器现已使用同一
runtime 镜像：

`sha256:8ddc0f4ad866985b2038d83bf30f961209cd692f460f17f5ce406a83d92f2c6e`

计算 Broker 使用：

`sha256:8e2b94f16f52563ab64f49212a12391086601d29222046088e196f6bcc0c2720`

## 备份与恢复验证

生产备份位于：

`/opt/tsp-full/backups/pre-414cc483-20261015-203656`

备份共约 497 MB，包含旧镜像归档、旧 Compose 及 platform override、两个数据库、
Broker 代码与 systemd/env、平台配置、Nginx/TLS、快照、容器基线和 release 指针。

- control 库：schema v5，20 张业务表，临时库完整恢复成功
- data 库：schema v5，34 张业务表，临时库完整恢复成功
- 旧镜像归档：`old-images.tar`，已保存 SHA-256
- 回滚脚本：`rollback.py`

代码回滚命令：

```bash
python3 /opt/tsp-full/backups/pre-414cc483-20261015-203656/rollback.py --rollback /opt/tsp-full/backups/pre-414cc483-20261015-203656
```

回滚脚本会先保存故障后的数据库，再恢复旧代码、配置、混合镜像、Broker 和六个服务；
本次无 schema 迁移，因此不会用旧数据库覆盖上线后的业务写入。

## 验收结果

- 后端定向回归：411 项通过
- 前端测试：10 项通过，生产构建成功
- 候选 API：深度健康 9/9，登录、租户、模拟盘与策略 API 通过
- 生产 API：深度健康 9/9，platform/API 健康，四类 Worker 心跳全部重建
- 公网 HTTPS：本地 CA、证书主机名和真实公网链路校验通过
- 新接口：`/api/paper/rounds`、`/api/paper/auto_skips` 等已出现在 OpenAPI
- 业务校验：管理员可见 2 个租户，22 个策略均含 `overlay_filter`
- 静态产物：API 与 Paper 页面 chunk 的线上 SHA-256 与候选一致
- 边缘代理：`tsp-full-api` alias 存在，`nginx -t` 通过
- 稳定性复检：公网连续 3 次 9/9 就绪，六个容器重启次数均为 0
- 清理核验：Broker 无 error 日志，临时恢复数据库残留数为 0

管理员空间当前没有模拟盘账户，因此真实持仓估值为空；live/daily_close/cost 分支已由
定向单测覆盖。

## 发布过程说明

首次切流时，ECS 通过自身公网 IP 访问 443 超时，自动保护逻辑按预期恢复旧版本并重新
达到 9/9 就绪。确认这是云主机公网回环限制后，服务器内验收改为直连本机 Nginx，
真实公网验收则从工作机独立执行；第二次切流与全部验收成功。

详细证据见同目录 JSON 文件。
