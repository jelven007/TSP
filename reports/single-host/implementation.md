# 单机容器化可靠性开发记录

日期：2026-10-18。基线版本：v0.4.8。主实现提交：

- `9b4bc2c5`：单机七角色 Compose、Worker Supervisor、同源 Runtime/Compute/Broker。
- `47b1738d`：行情读路径、公共发布、验收分类、资源、Broker、备份和观测加固。
- `72e9aded`：24 小时持续验收窗口和证据入口。

本批没有手工递增应用版本。合入远程 `main` 后由 CI 自动分配一次后继版本。

## 1. 必须修复

| 问题 | 实现 |
| --- | --- |
| API 空日线触发同步 Provider，拖住请求并级联 503 | `api/kline.py` 在非 standalone 角色快速返回 `market_history_unavailable`；API 行情代理读超时改为 12 秒，管理代理保留 300 秒 |
| 公共版本可能发布最新原始分区但缺少 enriched 标的 | `PublicPublisher.validate_daily_coverage()` 对 stock/ETF/index 最新日期执行结构化 Parquet symbol 子集校验，失败保留旧 `current` |
| 持续验收把同租户旧 marker 当成跨租户泄漏 | 新增 `consistency_violations`；同租户旧轮次/错误 symbol 归一致性，跨租户 marker 才归隔离；旧状态文件用 `setdefault` 兼容 |
| 旧 72 小时窗口已被缺陷污染 | 原窗口和遥测保留，新版镜像重新部署后，从 2026-10-18 18:37:24 起执行独立 24 小时 Active/Passive，完整交易日观察继续保留 |

## 2. 建议修复

| 领域 | 实现 |
| --- | --- |
| Worker 健康 | Supervisor 与 API readiness 共用 `MAX_AGE`，并返回实际年龄和阈值 |
| 16 GiB 预算 | 新增 `deploy/full/production-16g.env`；应用 8,704 MiB、单 Compute 2,048 MiB、双库连接上界 36 |
| Compute Broker | 健康检查运行真实无网络沙箱并缓存成功结果；执行/等待队列有界；保留 8 KiB stderr 尾部；健康超时同步放宽到 25 秒 |
| 备份恢复 | `scripts.backup_full_host` 在停写确认后生成双库 custom dump，逐库恢复到临时数据库并核对 schema，全部成功才原子发布备份目录 |
| 磁盘保留 | `scripts.maintain_full_host` 保护 current、最近版本和研究任务引用；缺少业务库 DSN 时拒绝删 release；Docker 只清理过期 BuildKit 缓存和悬空镜像，默认 dry-run |
| 监控告警 | 持续观察器写入去重的 `firing`/`resolved` 事件，覆盖深度健康、容器重启/退出和低于 1 GiB 的磁盘余量 |

## 3. 兼容性

- 没有数据库 schema、行情字段、策略算法、回测撮合或前端 API 变更。
- standalone 日线缺失仍可在线补数；只有多租户生产角色改为 fail-fast。
- 管理操作继续允许长耗时；只有公共行情读代理使用短超时。
- 公共发布门槛只在存在最新原始分区时生效；轻量夹具没有原始分区时保持兼容。
- 新验收字段对旧 `status.json` 兼容；旧窗口不重写。
- 维护脚本默认不删除数据，必须显式 `--apply`。

## 4. 部署状态

火山引擎生产已于 2026-10-18 部署 `9b4bc2c5`、v0.4.8、schema v6，详见
[线上升级结果](../online-upgrade-20261018/deployment-result.md)。`47b1738d`
当前在隔离候选中运行，不能据此宣称生产已经包含后续加固。

当前 24 小时窗口和启动证据见
[持续验收记录](./continuous-acceptance.md)。24 小时成功不等同 72 小时认证。
