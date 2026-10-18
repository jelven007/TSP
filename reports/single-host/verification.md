# 单机容器候选验收记录

2026-10-18。本轮已完成同源构建、资源与 Worker 监督配置、隔离 Compose 功能与
故障恢复验收。结论仅适用于 ARM64 Docker、两租户历史小夹具，不代表生产容量或
长期稳定性已经签收。

## 基线与产物

- 基于 `main` v0.4.8：`bb8ea7045b613f189159e7ebd84162567ffb09e5`。
- 当时的独立分支：`feat/single-host-compose`；本节构建时尚未提交、推送、合并或生产切流。
- 构建目录：`.tools/builds/single-host-06`。
- 源码快照 SHA-256：`27a1367260aa4c64098ea3ddb84181da5d32e08696f0ca7cbfd621fc8cedc97f`。
- 三类镜像的运行文件清单 SHA-256 相同：
  `1d37daff4e8e0e52308374862960c6fd44bd86e13bab796a518a9c869bc1ce99`。
- 快照明确记录 `source_dirty=true` 与基线提交；不将未提交改动冒充为原主线产物。
  启动使用固定本地镜像 ID；没有发布 Registry 产物或签名。
- 对当前受控源码与快照再次执行校验：`changed=[] / missing=[] / added=[]`。

完整镜像 ID、架构、文件清单见
[构建记录](../../.tools/builds/single-host-06/build.json)。
部署说明见[单机验收流程](../../deploy/full/single-host.md)。

## 实施范围

L1 部署配置加最小 L3 核心修改：复用现有七角色、PostgreSQL RLS、公共行情发布和
计算协议。核心变化为 Worker 实际调度进度上报、进程监督，以及 Broker 的宿主资源
配额；没有改变行情字段、价格口径、策略算法、业务 API 或用户存储格式。

Worker 进度绑定 PID、随机实例标识与单调时钟，成功写入数据库后才记录；租约续期
不伪装调度进展，也不抑制下一次调度心跳。超时后先终止、再限时强制回收。
Broker 的 CPU、内存与并发仍由可信宿主配置控制，旧默认值保持兼容。

镜像均来自同一受控快照，派生镜像校验父文件层及源码清单。构建扫描保留前端
`components/data` 源码；运行数据和凭据继续排除。七角色只读根目录，日志轮转为
10 MiB × 3；Gateway 关闭含流票据的访问日志。

隔离预算总上限为 5,184 MiB，包含一个 1 GiB Compute；应用角色仅接内部网络。
PostgreSQL 与 Nginx 通过独立验收桥接网络提供主机回环入口。

## 实际验证

| 检查 | 结果与证据 |
| --- | --- |
| 最终定向回归 | **96 passed，0 skipped**；含真实一次性 PostgreSQL 的研究、实时、定时任务测试，[日志](../../.tools/regression-final.log) |
| 完整前端 | Docker 中 `tsc -b && vite build` 成功；真实 API 返回完整静态入口 |
| 同源镜像 | Runtime、Compute、Broker 构建与运行文件校验全部通过 |
| 十服务 Compose | 七应用角色、PostgreSQL、Broker、Nginx 启动；深度就绪、只读根目录及资源限额检查通过 |
| 两租户隔离 | HTTP 写入自己的自选股，另一租户不可见 |
| SSE | Nginx 后单次票据重用被拒绝，私有事件送达，`Last-Event-ID` 补放正确 |
| 自动重启 | API/Broker 从容器内向 PID 1 发送 SIGTERM；研究子进程 SIGKILL；各自动重启一次，业务记录保留 |
| 调度停滞 | 挂起研究子进程并注入已过期进度时间，监督器回收并自动恢复；未宣称实际等待了完整超时窗口 |
| 数据库断网 | 仅断开候选 Gateway 的数据库网络，检测不可用，重连恢复 |
| 恢复后队列 | PostgreSQL → Research → Broker → 独立 Compute 的描述任务成功 |
| 完整 Compute | 1 GiB、1 CPU、并发 1，**69.899 秒通过**；策略、策略回测、信号回测、因子、优化、步进优化、挖掘 Parquet 产物；非法导入被拒绝、下一独立任务正常 |
| 静态检查 | 本次 Python 文件按各目录适用配置执行 Ruff 通过；`git diff --check` 通过；版本校验为 `0.4.8` |
| 清理 | 所有本轮一次性 Compose、测试数据库和 Compute 容器已清理；未遗留 `tsp-candidate-*` 容器 |

[最终 Compose 报告](./compose-06b.json)，耗时 **159.756 秒**。
[完整 Compute 报告](./compute-06.json)。

实际执行的主验收命令：

```bash
python scripts/build_full_candidate.py --registry public.ecr.aws/docker \
  --output .tools/builds/single-host-06

# 在 backend 目录
python -m scripts.accept_full_compose \
  --build-dir ../.tools/builds/single-host-06 --work-dir ../.tools \
  --output ../reports/single-host/compose-06b.json

# 在仓库目录
python scripts/verify_full_compute.py \
  --image sha256:17a6ddc517027b757d729fd8153cb5ef44c588a8b91809d1b3414def8ff48192 \
  --memory-mb 1024 --cpus 1 --work-dir .tools \
  --output reports/single-host/compute-06.json
```

验收入口 SHA-256（验收脚本不属于镜像运行文件）：

- `backend/scripts/accept_full_compose.py`：
  `65d063cb62b8a0329d5cac41356040afa038c2d2d00110be51ba63ff2234dac3`。
- `backend/scripts/accept_external_sse.py`：
  `2f8ee9240243a5e81ac6c7a4ecb8ce8f420e92d665027893ff6706c7aa587913`。

前序失败报告及构建日志保留，未计为通过。其中已修正裸镜像 ID 的 BuildKit
解析、内部网络端口发布、YAML tmpfs 列表和进度心跳节流问题。
`docker kill` 被 Docker 记为人为停止，因此最终报告明确记录实际使用的退出信号，
不把手动停止后的等待误称为自动恢复。

## 当时尚未验收（历史）

- 离线夹具没有实时公共行情标记；报告为 `received=false / required=false`。
  真实标记传播、行情新鲜度及延迟须在交易日验证。
- 当前前端检查为完整构建与静态入口，没有执行真实新用户注册的浏览器全流程。
- 固定拓扑下的完整交易日、72 小时、100 个独立活跃用户持续混合负载尚未完成。
  先按真实全市场数据测量内存与查询负载，再确定长期候选资源，不能从小夹具预算
  外推生产容量。
- 原生 AMD64、Windows 真机、公网 TLS/WAN、整机恢复尚未在本轮验证。
- 当时的交易日及 72 小时观察容器保持运行；其 Docker `unhealthy` 状态与操作前一致，
  本节未核验其业务采样结果，也未将旧拓扑观察计作新拓扑证据。

生产切流仍须完成匹配拓扑的正式验收，并遵循旧平台停写、最终备份与对账、切换、
新平台启用的单写顺序。当前交付是可复核的本地候选。

## 2026-10-18 后续加固与状态

上述结论是 `9b4bc2c5` 前序候选的历史记录。后续状态以
[开发记录](./implementation.md)和[持续验收](./continuous-acceptance.md)为准：

- 火山引擎生产已完成 v0.4.8、schema v6 和七角色切流，固定提交为 `9b4bc2c5`。
- `47b1738d` 增加行情 fail-fast、公共发布完整性、验收分类、Worker/Broker、
  16 GiB 预算、双库备份恢复、保守保留和告警。
- 相关回归 `86 passed, 15 skipped`；Compose 预算在宿主解析通过；一次性
  PostgreSQL 16 双库 dump/restore 与 schema v6 校验通过。
- 原 72 小时窗口在约 9.44 小时处停止并完整保留，不作为结论。
- 新 Active/Passive 24 小时窗口为 2026-10-18 18:37:24 至
  2026-10-19 18:37:24；完整交易日任务保持 2026-10-19 09:15–15:40。
- 24 小时通过不表示 72 小时、Windows 真机或原生 AMD64 性能已签收。
