**平台版本发布与健康检查修复 · 2026-10-15**

已完成修复、本地提交、推送和平台容器更新。源码提交为 `4db757e608a6e9f8989d04126dcd87550061f32a`，分支为 `feat/phase1-full-multitenancy`。

版本发布原先先读取 generation，再执行 Provider 审计和元数据读取；实时行情更新可能在此期间推进 generation，导致发布失败。现在 Provider 审计在写锁外执行，分区元数据、紧凑研究输入读取与 manifest 提交共用仓库写锁，原有一致性和未完成发布校验继续生效。盘后、维表、竞价及平台财务同步均接入该锁。

竞价快照落盘后若发布失败，返回 `publication_pending`。后续采集重试和每分钟补发布任务只处理当天已验证的本地快照；启动时也可恢复。成功的 `data_release_id` 会原子写入竞价元数据。

平台深度健康检查改为独立后台线程每 5 秒刷新，HTTP 探针异步读取结果，内部行情快照复用同一结果。启动未完成、检查结果从开始检查起超过 15 秒、依赖失败或关闭时均保持未就绪。其他服务的既有深度健康检查继续生效。

验证结果：

- 147 项定向及合入后的回归通过，覆盖发布竞争、未完成 generation 拒绝、失败恢复、重复采集防护、线程池占满、健康结果过期和依赖失败。
- 旧代码复现了竞价版本 ID 未持久化、发布失败无法恢复及健康探针线程池排队超时。
- Ruff 与原基线相比没有新增诊断；相关既有文件的 45 项历史诊断保持不变。`git diff --check` 通过。
- 平台及 API 容器健康；公网 `/health/live`、`/health/ready`、`/api/health` 均返回 200，API 的数据库、四类 Worker、公共文件、平台连接、指标缓存及计算 Broker 检查均通过。

同一生产主机、相同 6 路客户端并发，交替请求 20 次内部行情快照与 20 次健康检查，每次请求超时阈值为 4 秒：

| 指标 | 更新前 | 更新后 |
| --- | ---: | ---: |
| 健康检查失败次数 | 2 / 20 | 0 / 20 |
| 健康检查中位耗时 | 1,202.50 ms | 107.38 ms |
| 健康检查 P95 | 4,004.26 ms | 611.25 ms |
| 健康检查最大耗时 | 4,004.31 ms | 612.15 ms |
| 行情快照失败次数 | 0 / 20 | 0 / 20 |
| 行情快照中位耗时 | 2,563.37 ms | 939.83 ms |

本次为盘中短时对照，行情随交易自然变化；同时有确定性的线程池占满回归证明探针已脱离业务线程池。

今天竞价快照已由启动补发布任务恢复：

- 股票数：5,226。
- `capture_id`：`eb1521a062a44565bd81b82c68a8091a`，更新前后相同。
- `data_release_id`：`83110ac2d4e146dd8642b0c1d748a6f3`。
- 源目录、共享公共版本目录的竞价元数据和全局 manifest 一致。
- 原始竞价 Parquet 的 SHA-256 与备份一致，采集时间保持原样。

平台镜像由原线上镜像派生，仅合入本次 8 个运行时文件的修复，并保留线上已有代码差异：

`sha256:51b5d392c5c9f5de6b05146a43ece9c0724f3ca8544977bcbd67024478b334f0`

部署只替换平台容器；其余 7 个容器 ID 保持一致。环境变量按键值核验一致，挂载、运行用户、启动命令、健康检查配置、资源限额及安全配置均保持一致。

备份位于服务器 `/opt/tsp-full/backups/platform-health-20261015-4db757e`，包含原平台运行配置、全局版本文件和当天竞价分区。生效的部署覆盖文件为 `/opt/tsp-full/hotfix-platform-health-20261015-4db757e/compose.override.yml`，维护本次平台版本时应携带该覆盖文件。

恢复原平台镜像的命令已准备；本次没有执行回滚：

```bash
docker compose --project-name tsp-full \
  --env-file /opt/tsp-full/production/compose.env \
  -f /opt/tsp-full/release-2a3b395/deploy/full/compose.yml \
  -f /opt/tsp-full/hotfix-platform-health-20261015-4db757e/compose.override.yml \
  -f /opt/tsp-full/hotfix-platform-health-20261015-4db757e/compose.rollback.yml \
  up -d --no-deps --no-build platform
```

已验证当前盘中补发布及请求负载；下一交易日的 09:25 定时采集尚未实际到点运行。
