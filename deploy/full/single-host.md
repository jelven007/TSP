# 单机容器候选实施与验收

基线：`main` v0.4.8，`bb8ea7045b613f189159e7ebd84162567ffb09e5`。
本次属于部署配置调整及最小核心修改：复用七角色、数据库 RLS、公共版本发布和
计算协议；核心变化仅限 Worker 进程监督和 Broker 宿主资源配额。
不递增应用版本，不切换现有流量，不与现有平台共享写目录。

## 1. 固定构建输入

从独立工作目录执行：

```bash
python3 scripts/build_full_candidate.py --output .tools/builds/candidate
```

可用 `--registry public.ecr.aws/docker` 选择基础镜像仓库。
构建工具复制受控源码范围并校验复制前后文件清单，包含 `deploy/full`，
随后从同一快照构建 Runtime、Compute 和 Broker。
Runtime 包含完整前端与 `cell` 依赖，Compute 补齐冻结的 `backtest` 依赖，
Broker 仅增加 Docker CLI。Compute 不继承 HTTP healthcheck。

输出包含源码、各镜像运行文件清单、构建日志、镜像 ID、架构和 `build.json`。
未提交工作区记录 `base_commit`、`source_dirty=true` 和完整文件哈希；
镜像的提交字段保持未知，不能冒充基线提交原样构建。
候选启动使用镜像 ID，三类产物的源码文件清单必须一致。
本地镜像 ID 不等于已推送 Registry digest，也不等于发布签名。

## 2. 当前小规格预算

| 组件 | 内存上限 MiB | 并发说明 |
| --- | ---: | --- |
| Platform | 1024 | 唯一行情写入者，Polars 2 线程 |
| API | 1024 | 1 个 HTTP 进程 |
| Gateway | 256 | 1 个流进程 |
| Research | 256 | 1 个任务 |
| Realtime | 384 | 1 个租户任务 |
| Notifications | 256 | 1 个投递任务 |
| Scheduled | 384 | 1 个批次 |
| PostgreSQL | 384 | 80 连接上限，64 MiB shared_buffers |
| Broker | 128 | 只接收受限计算协议 |
| Edge | 64 | 仅主机回环 HTTP |
| 一次性 Compute | 1024 | 全机 1 个，不额外使用 swap |
| 合计 | 5184 | CPU 上限允许时间共享，不是专用核预留 |

每个 API/Gateway/Worker 的每库连接池上限为 2，六进程池上限合计 24；
Gateway LISTEN 等专用连接及验收管理连接另计。80 是 PostgreSQL 拒绝新连接的
边界，不代表业务容量已验证。启动脚本要求 Docker 总内存至少再留 2 GiB；
仍应在启动前检查已有容器、Docker VM 和宿主实际内存。

该预算仅供小夹具功能验证。真实全市场数据规模、查询峰值、计算 RSS 和宿主
余量必须实测后再制定生产预算。已有交易日、72 小时观察继续保护，
其结果只对应原拓扑。

## 3. 独立 Compose 故障验收

准备本地 PostgreSQL 16 和 Nginx 镜像，然后从 `backend` 执行：

```bash
uv run --frozen python -m scripts.accept_full_compose \
  --build-dir ../.tools/builds/candidate \
  --work-dir ../.tools \
  --output ../reports/single-host/compose.json
```

脚本叠加 `compose.yml`、`compose.isolated.yml` 和 `small-host.env`，
使用独立项目名、目录、端口、网络、数据库及随机凭据。
七角色全部运行真实应用入口；行情输入为确定性历史小夹具，网络禁止供应商出口。
应用角色只连接内部网络；PostgreSQL 与 Nginx 另接验收桥接网络，
用于发布主机回环端口（Docker 不发布仅内部网络的容器端口）。
覆盖文件使用 Compose 的 `!reset` 清除 API/Gateway 主机端口，
要求 Docker Compose 2.24 或更新版本。
Broker 独占 Docker socket，Compute 仍无网络、无凭据且只读挂载固定公共版本。
只有此一次性候选的 Broker 可以清理其独占 socket 卷内的遗留 socket。

验证范围：

- 七角色深度就绪、完整前端静态入口和镜像一致性。
- 两个租户的真实 HTTP 读写隔离。
- Nginx 后的私有事件、一次性票据与断线补放；离线历史夹具不产生实时公共
  行情标记，标记传播与延迟留到交易日验证，报告不会将其计为通过。
- API、研究子进程、Broker 故障后的自动重启及业务记录保留。
- 挂起研究调度子进程并注入过期时间戳，验证监督器限时回收；不把它记录为
  实际等待完整心跳超时窗口。
- Gateway 数据库网络断开后的不可用检测及恢复。
- 恢复后通过 PostgreSQL 队列、Research、Broker 和独立 Compute 完成真实任务。

结束时仅停止并删除本次候选资源；故障也保留报告。不要对已有部署运行此脚本。
若需要检查 1 GiB 预算内完整计算能力，再执行：

```bash
python3 scripts/verify_full_compute.py --image <本次Compute镜像ID> \
  --memory-mb 1024 --cpus 1 --work-dir .tools \
  --output reports/single-host/compute.json
```

## 4. 正式长时验收

前三步通过后，使用固定产物和适配真实数据的资源预算启动长期候选，
重新执行完整交易日、72 小时观察和 100 个独立活跃用户的持续混合负载。
本地历史小夹具、100 条同账户 SSE 连接、ARM 主机仿真或容器显示 healthy
均不能替代这些验收。原生 AMD64、Windows 真机、公网 TLS/WAN 与生产主机
恢复另行签收。当前入口会清理一次性环境，不能直接用作长时部署。

生产切流仍遵守“旧平台停写 → 最终一致备份与对账 → 路由切换 → 新平台启用”，
保留 Edge 配置、双库和私有增量的恢复路径。单机容器化不提供宿主故障高可用。
