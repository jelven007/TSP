# 部署目录

开发和发布边界见[生产工程](../docs/reference/engineering.md#engineering)，
生产能力按[联合验收](../docs/testing.md#acceptance)签收。
目录归档不将目标Kafka/Flink/CH/Redis/TOS等组件变成可运行部署配置。

部署配置按运行形态组织，应用源码共用同一后端包。

| 位置 | 运行形态 | 说明 |
| --- | --- | --- |
| 根 [Dockerfile](../Dockerfile)、[docker-compose.yml](../docker-compose.yml) | 独立运行 | 单容器托管前端与后端；独立模式单 API worker |
| [full/](full/README.md) | 完整单机多租户 | API、Gateway、Platform、四类 worker、Compute/Broker；含资源预算和隔离验收配置 |
| [edge/](edge/README.md) | HTTPS 边缘入口 | Nginx 路由、TLS、证书续期与 systemd |
| [cell/pg/](cell/pg/README.md) | PostgreSQL | 数据库部署和初始化；应用有序 SQL 迁移保存在 `backend/app/cell/sql/` |
| [windows/](../docs/operations.md#windows) | 原生 Windows | 完整应用单机管理员、服务启动、看门狗与资源预算 |

真实环境配置和密钥由部署者维护，不提交到仓库。资源模板随所属部署形态保存，
不再复制到顶层 `config/` 或 `infra/`。
安装器资源位于 `packaging/`，与 Linux 容器部署分别验证。

操作步骤见[部署指南](../docs/operations.md#installation)、[运维手册](../docs/operations.md)
和[发布流程](../docs/release-operations.md)。源码归档不修改现有生产目录、镜像或运行配置。
