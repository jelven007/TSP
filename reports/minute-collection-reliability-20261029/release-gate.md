# 分钟修复发布门禁

2026-10-29。用户已授权提交、推送并按正式流程升级本地与ECS。
PR归并、正式产物、本地及ECS升级、恢复、深健康、实际任务及正常重启均已通过；
同一OCI索引的latest提升及v0.4.38正式Release发布已完成，独立执行记录及回读核验通过。
负责执行、观察和回退：当前发布会话；故障反馈与最终签收：仓库维护者。

## Build and Release Pipeline

PR34固定`8d1b5069e92054695af1043c460597cb732101fc` →
PR质量门 → squash归并 → main CI自动递增一次 →
读取成功CI的`ci-revision` → 同SHA标签及双架构镜像 →
冻结锁补装cell/backtest → 隔离验证 → 本地 → ECS → 实际任务/重启验收。

| 构建控制 | 当前证据与执行规则 |
| --- | --- |
| 输入 | clean独立worktree；应用版本由main CI分配；pnpm/uv/Cargo冻结锁；Rust 1.99、maturin 1.9.6、uv 0.12.17、Codex 0.144.3 |
| 清洁/生成物 | 主线归档；源码文件逐blob核对；前端由固定源码构建，禁止借用旧dist |
| 产物身份 | OCI index/子manifest/config digest、版本和SHA标签、build-manifest、原生wheel哈希；完整部署层另外记录不可变镜像ID |
| 并行发布 | 切换前核对远程main与目标SHA；若出现新发布则停止该切换并重新评估，不用latest替代版本身份 |
| 依赖可用性 | 下载校验后保留两架构离线镜像及源码归档；取不到固定产物时保留旧服务 |
| 缓存 | 冻结锁、源码COPY和完整运行清单校验；缓存命中不代替运行文件核对及业务回归；新原生源码强制产生新的源码指纹 |
| 候选/分支 | 一个短期修复分支，正常PR门禁；不采用紧急豁免、不手工递增 |
| 检查 | PR/main后端全量、前端test/lint/build、Windows原生；发布清单、依赖导入、恢复、深度健康、登录与真实分钟任务为必要项 |
| 回退 | 旧v0.4.37/f4d8c11b源码及固定镜像、停写双库与行情/config备份；无schema变更，只回退代码时保留切换后业务增量 |

## Readiness Matrix

| Domain | Status | Evidence | Freshness/Drift Note | Blocker? |
| --- | --- | --- | --- | --- |
| 责任 | Pass | 用户授权和本记录的执行/回退/维护者边界 | 本轮 | 否 |
| 架构 | Pass | 单写Platform、API/Gateway、四Worker、Broker/Compute、双PG | 源码/部署手册已核对 | 否 |
| 运行 | Pass | 两端v0.4.38深健康、六功能、实际任务及正常重启通过；ECS七角色同镜像健康 | 固定5b273174；本轮实测 | 否 |
| 安全变更 | Pass | 最小源码变更，无schema/初始化/ETF迁移；两架构正式产物、完整Runtime/Compute及候选无网核验通过 | 41文件审查READY及本轮产物 | 否 |
| 容量 | Pass | 本地可用236GiB；ECS可用85GiB，平台行情5.5GiB；保持8业务+1探测 | 本轮实际测量 | 否 |
| 恢复 | Pass | 两端control20/business28表停写恢复与逐表计数、业务摘要及行情/config哈希通过 | 本轮重新恢复；schema v6/v6 | 否 |
| 安全/完整性 | Pass | 正式SHA、两架构清单及wheel身份通过；两端秘密、配置、自选及用户偏好保持 | 重启后再次核对；未新增权限 | 否 |

## Responsibility

| Area | Owner | Escalation Path | Ready? | Evidence |
| --- | --- | --- | --- | --- |
| 发布/回退/观察 | 当前发布会话 | 维护者及本会话用户反馈 | 是 | 2026-10-29授权 |
| 常规运营及签收 | 仓库维护者 | 既有运维日志、健康计时器、本报告 | 沿既有路径 | docs/release-operations.md |

## Architecture And Fault Domains

| Component | Production Dependency | Fault Domain | Map/Diagram Reference |
| --- | --- | --- | --- |
| Platform | 当前实际Provider、可写平台行情与配置 | 单ECS主机；本地独立环境 | deploy/full/README.md |
| API/Gateway/四Worker | 公共不可变版本、双PG、平台、Broker | 同ECS各进程；Gateway独立监听 | docs/architecture.md |
| Broker/Compute | Docker、无网络计算镜像、只读行情 | 同执行主机预算 | deploy/full/production-16g.env |

## SLOs

| Journey | SLO/Target | Current Evidence | Launch Risk | Response |
| --- | --- | --- | --- | --- |
| 登录/业务 | 发布验收目标：深度健康及六功能ready、现有admin登录 | 两端新版本启动与重启后均通过 | 无新可用性承诺 | 任一不通过则回退 |
| 分钟采集 | 发布验收目标：实际任务终态、键与源值正确、剩余缺口真实 | 两端100证券逐值对照及常驻真实任务、重启后CSV核验通过 | 单次样本不认证全市场SLA | 保留5,520历史键及同日各6个未返回键 |
| 错误预算 | 既有单机部署无可计算的长期SLO/错误预算台账 | 不虚构剩余预算或burn趋势 | 维持现有部署范围 | 串行两端升级、现场观察并设停止条件 |

## Observability

| Signal | Dashboard/Alert | Missing-Signal Behavior | Owner | Ready? |
| --- | --- | --- | --- | --- |
| 健康/任务/池 | /health/ready、持久任务、systemd/应用日志、既有健康timer、原生池统计 | 不以容器running作为签收 | 当前发布会话 | Pass；重启后健康timer自然触发成功 |

## Safe Change

| Change Path | Rollout/Abort/Rollback Evidence | Rehearsed? | Gap |
| --- | --- | --- | --- |
| 两端串行切换 | 固定产物/源 → 停写备份 →恢复验证 → systemd既有启动顺序 → 深健康/实际任务 | 上批流程已读，本轮重新验证 | 并行补采任务须自然终态 |
| 回退 | 保留旧镜像、源及完整受控备份；无schema变化，不重置配置/用户增量 | 本轮两端停写恢复通过 | 无新增阻断项 |

## Availability

| Fault-Domain Independence | Static Capacity Under Loss | Recovery Mechanism | Drill Result |
| --- | --- | --- | --- |
| 既有单ECS，无跨区域自动故障转移；本地不作为线上容灾 | 无额外故障域备用容量；本次不扩容、不承诺HA | 固定旧镜像及受控备份人工恢复 | 本轮两端双库及文件恢复通过 |

## Security

| Area | Evidence | Residual Risk | Exception Or Blocker | Owner |
| --- | --- | --- | --- | --- |
| 凭据/权限 | 沿既有非owner数据库角色、只读公共目录和无网Compute；配置不输出 | 既有部署信任边界保持 | 两端身份及配置核验通过 | 当前发布会话 |
| 身份 | 用户预置及随后保存配置保留；验证现有Admin123登录 | 不重跑bootstrap，不外发邮件 | 配置字节和语义对账 | 当前发布会话 |

## Launch Blockers

| Blocker | Artifact Or Path | Required Detail | Due Date |
| --- | --- | --- | --- |
| PR/main质量门、固定镜像 | GitHub PR34及Actions | 全部必要检查成功、CI实际SHA/标签/digest对应 | 已通过 |
| 恢复及新版本验收 | 受控WORK/local和ECS evidence | 双库临时恢复、逐表/文件对账；深健康、登录、任务和重启通过 | 已通过 |
| 活跃任务 | 两端实际jobs及ECS独立repair容器 | 停写前已自然退出；本轮实际任务终态与重启结果保持 | 已通过 |

## Exceptions

| Exception | Risk | Compensating Control | Owner | Expiry | Accepted By |
| --- | --- | --- | --- | --- | --- |
| 无新增发布豁免 | 沿用现有单机/人工回退边界，不宣称HA、长期SLO或历史全部完整 | 串行切换、恢复实测、保留旧产物、现场观察 | 当前发布会话 | 本轮签收 | 用户已授权既有正式升级范围 |

## Launch Watch And Handoff

| Watch Window | Success Check | Abort Check | Responder Handoff |
| --- | --- | --- | --- |
| 每端切换后至真实分钟任务及一次正常重启完成，随后覆盖至少一个既有健康timer周期 | 新SHA/版本、六功能、admin登录、分钟行与remaining/CSV、8+1、配置持久 | 健康失败、数据键/源值错误、配置被改、活跃任务丢失、来源身份不符 | 当前发布会话执行/回退，维护者接收deployment报告 |

## Advisory Posture And Impact

| Dimension | Applies? | Posture | Advisory Boundary |
| --- | --- | --- | --- |
| 外部承诺 | 固定公开源码/镜像；不新增服务承诺 | 通过PR质量门后可归并构建 | 生产切换另验收 |
| 用户关键流程 | 登录与分钟数据 | 必要检查通过才切换 | 用户已有授权 |
| 敏感数据 | 账号、SMTP、租户业务 | 受控备份、不输出秘密 | 不改变权限契约 |
| 持久状态 | 双库、行情和配置 | 全备份/恢复核验 | 不迁移schema或初始化 |
| 影响范围 | 当前本地及单ECS | 串行、有限实际任务 | 不扩展全市场性能承诺 |

## Prioritized Follow-Up Routes

| Route | Reason | Priority |
| --- | --- | --- |
| 正式产物身份/运行恢复 | 本轮必要门禁全部通过，最后文档/证据归档 | 当前交接 |
| 历史源缺口 | 三四月5,520键继续真实缺失；不伪造价格 | 独立数据覆盖记录 |

## 主线质量门与候选进展（过程记录）

下列“待验证/仍保留旧服务”为各阶段当时状态；当前签收以上表及后文最终验收为准。

PR34已归并：`30d177c2a7903aed98b5e4b54fe0a1bc9ba7792b`。
成功main CI为40787761848，实际固定版本提交为
`5b273174f2e2a8ac539fcea363fa02315a61ebe9` / `v0.4.38`；
后端4,803 passed、9 skipped、173 warnings，321.08秒。
Docker工作流40788719301已成功，正式OCI索引为
`sha256:80f329fda646cc128b4957be7c2d93bfa0c721057971a0f1aa3a185d8ee6333f`。
版本与完整SHA标签、amd64/arm64子manifest及config的哈希和源码身份通过；
生产切换仍须完成候选无网/样本及停写恢复验证。latest未自动提升，将在两端验收后
按独立发布记录提升同一索引，不重建镜像。

本地固定源码已逐blob核对1,930文件。macOS正式wheel完成后，
隔离100证券样本全量17.577秒、遗漏14.366秒，均504,000唯一键，
内容SHA256与原基准相同；无缺口0.529秒、零请求。
历史缺口1.287秒返回remaining=5,520；应用CSV按证券/交易日汇总，
实测23行、每行240个缺失键及正确首末时间。初版运维断言误以为CSV逐分钟展开，
已按源码契约修正并以新隔离目录完整重跑通过；生产代码没有因该断言改变。
这份应用摘要CSV与专项报告中的5,520行逐键明细属于不同格式。

本机实际构建工具为Python3.12.15、Rust/Cargo1.99.1、maturin1.9.6、
uv0.13.5、Node24.22.1、pnpm9.10.0；Docker声明的uv0.12.17、
Codex0.144.3、Rust1.99镜像及LinuxCPython3.11保持原正式流程。
两平台wheel不要求二进制相同，锁文件、源码指纹、OCI身份和业务结果分别核对。
本条实际测量修正上表中未区分平台的工具版本概括，不把声明版本当成本机实测值。

amd64与arm64正式下载已通过压缩层、config原字节、rootfs及revision核验。
本地完整Runtime/Compute已构建并完成无网导入和清单核验；
Runtime为`sha256:5a80bd71cbd90da4a8eaeb7343728939910c5991e0427ab8c688a7ee57576736`，
Compute为`sha256:25acd538cfe0d3bd9496b85f33e5baad941a0aa8960ba6f1ffd6fcdc814a4129`。
本轮运维脚本初次将裸镜像ID作为Docker构建FROM参数，被解析成仓库名而失败；
已改用固定本机标签，并在构建前后核对同一镜像ID，随后完整构建通过。
初次失败日志保留，正式源码和锁文件未因运维参数修正改变。
本地停写备份已在独立transition receipt后启动；恢复通过前不启动新版本。

本地备份现已通过：control20表、business28表恢复计数及业务摘要一致，
schema v6/v6，行情/config完整副本哈希核验通过。独立local-start receipt后，
v0.4.38常驻启动成功；1,930个源码文件、wheel安装字节、后端元数据、
各角色CWD、原16只自选/2组、业务设置、秘密和用户偏好对账通过。
原72,373,518分钟行及各周期/指数计数在切换审计时保持；
六功能、登录、七指数目录和日/周/月K查询通过。真实任务与正常重启继续验收。

ECS离线传输的478,866,432字节镜像全SHA256通过，原始config和rootfs保持。
完整Runtime为`sha256:5c9b38b92f100e69fe46a33202b67963129083cf1566d1ed155ac63b81c64bb9`，
Compute为`sha256:a517c7d6abe6fc332cb394a29e59853f0ece9d805cbfe4d12b58fcf43cb44845`；
两者无网导入和源码清单核验通过。正式候选100证券样本全量12.687秒、
遗漏11.878秒，504,000唯一键及规范CSV哈希与本地一致；无缺口0.809秒零请求，
历史样本0.487秒如实返回5,520键和23行摘要。ECS仍保留旧常驻服务，
待本地实际任务及重启通过后才进入停写恢复和升级。
新增真实缺口CSV核验先在已取得的5,520键/23行文件通过，错误计数5,519被拒绝；
最终还将读取重启后的常驻环境缺口CSV，并与持久任务结果对账。

本地完整重启与再次登录/六功能通过，两个实际任务的终态、结果和结束时间保持。
闭市任务`1d035949c4`端到端4.051秒、零行/零缺口/零请求；
10月28日任务`24e37cab2e`为598.129秒、补301,470行、499逻辑请求、remaining=6。
6个未返回分钟键的CSV重启后实际存在，6行摘要计数与任务结果一致，SHA256为
`f88f92a5ecf7e18c0b318f4b8828b2327cb6a28230e00330f0fd80079559f5f0`。
初次物理文件核验误用消费者`.env`的公共目录；按已核对的launcher Platform写目录
`data`修正运维核验路径后通过，没有改动运行数据或生产代码。
598秒来自5,222证券总体中实际存在301,476个缺口的单日任务，
与隔离100证券同月样本规模不同；不把样本十几秒推断为全市场耗时。
本地必要升级验收已全部通过，可以按已授权串行顺序进入ECS停写备份。

本地main已同步固定提交，10个CI版本文件更新；原2,768个其余文件字节保留，
dirty/untracked和索引另存受控归档与Git bundle。此前`missing-5520.csv`的
5,521行CRLF副本已保留受控归档；当前文件由并行会话规范化后与正式Git blob
逐字节一致，所有价格/缺口字段没有变化。2026-10-29本轮最新工作区核验中，
布局、文档、版本、完整diff及排除此CSV的diff检查均通过。
其他会话的前端和运维文档修改继续保留；成功CI和正式产物均使用固定提交。

## 两端最终验收与发布决定

ECS已加载固定5b273174提交，七角色、PostgreSQL及Broker健康。
现有管理员登录、六功能、七指数日/周/月查询、自选/分组及用户配置在启动与重启后通过。
真实闭市任务`ed67a0eeb1`耗时4.148秒、零请求；10月28日任务`150e8ae25b`
耗时41.179秒，补2,315行、27逻辑请求，remaining=6。
本地及ECS的6行缺口CSV均在重启后的实际Platform写目录读取，计数及文件SHA256一致；
两端同六证券未返回键保持，不据任务成功声称全部补齐。
ECS原118,448,923分钟行及各周期行数在升级审计时保持，任务新增行另记。
正常重启在08:32:18完成，既有健康timer于08:33:15自然触发并成功退出。
初次运维核验误把timer单调时间的人类可读时长当作整数；
改为读取systemd D-Bus的uint64微秒后，触发及服务启动均晚于重启，差12,402微秒，
通过自然触发断言。没有手工触发健康service或改变生产配置。
两端必要门禁已全部通过，无新增发布豁免；按已有授权发布同一索引与已有v0.4.38标签。
latest提升后回读index与amd64/arm64身份均一致；v0.4.38正式Release于08:35:07发布，
非draft、非prerelease。最终交付与仍缺历史键见[deployment.md](deployment.md)。

## Pipeline Map

ECS停写备份完成，恢复临时库后control20表、business28表逐表计数及业务摘要
与源库一致，schema保持v6/v6。12,861个行情文件共5,755,312,814字节
已复制并核对全哈希；平台/私有配置、旧源码、运维单元及旧Runtime/Compute均保留。
备份路径为`/opt/tsp-full/backups/pre-v0.4.38-5b273174f2e2`，
行情副本摘要为`4cf10f17629030c6d67430e62d5c36c5688eb40685b7240e272af23e56b1da82`。
原活跃采集、独立repair和研究任务已自然结束；现可按独立online-promote记录，
使用既有systemd Broker→完整服务启动顺序切换到固定5b273174提交。

| Stage | Input | Builder/Runner | Output | Record |
| --- | --- | --- | --- | --- |
| 质量门 | CI固定5b273174提交 | main CI40787761848 | v0.4.38标签及ci-revision | main-ci.json及原始日志 |
| 产物 | 同提交归档/锁 | 官方双架构Docker及macOS构建 | OCI、平台wheel、前端dist | registry、native-build、source-verification |
| 完整部署 | 固定官方平台镜像与冻结依赖 | 各执行架构cell/Compute | 不可变完整镜像ID | artifacts及无网清单核验 |
| 升级 | 同版本已验收产物 | 本地后ECS串行 | 常驻进程/镜像、实际任务 | 分阶段receipt、backup、audit、restart |

## Pinned Inputs

| Input | Pin | Integrity Check | Update Path |
| --- | --- | --- | --- |
| 源码 | 5b273174f2e2a8ac539fcea363fa02315a61ebe9 | 1,930 Git blob、源码归档SHA256 | main质量门自动分配 |
| 依赖 | 三类冻结锁；cell/backtest导出require-hashes | 不改锁、不重新选版本 | 另走普通PR |
| 工具/生成物 | build-inputs实际工具版本与控制脚本指纹 | 新native源码编译；新dist构建 | 同批记录实际输入 |

## Artifact Identity

| Artifact | Source Revision | Build Metadata | Provenance | Verification |
| --- | --- | --- | --- | --- |
| OCI双架构 | 完整固定SHA | index/child/config digest及清单 | 官方Docker40788719301 | 成功后逐hash、标签、架构、无网导入核验 |
| Mac wheel | 同SHA | cb140a0ca9d8df3fda1fdd5d5ef2f894c6bbc9feddc9bd563b8af838b000fec4 | native-build输入清单 | 隔离真实服务和来源值核对 |
| 完整层 | 同SHA | runtime/Compute不可变ID | 固定OCI加冻结可选依赖 | 清单与源一致、cell/vectorbt导入、无网 |

## Distribution Metadata

| Package Or Manifest | Ref Pin | Revision Pin | Verification Command | Drift Response |
| --- | --- | --- | --- | --- |
| 正式镜像 | v0.4.38与完整SHA | OCI revision标签 | registry_check及saved-image身份核验 | 不用latest替代，失败保留旧服务 |
| 本地安装 | 精确wheel及editable元数据 | 固定源码目录/指纹 | metadata、源码与实际CWD审计 | 回退旧包、指针和Compute配置 |

## Inflight Release Ordering And Supersession

| Release Candidate | Precedes | Supersedes | Allowed Parallelism | Decision Record |
| --- | --- | --- | --- | --- |
| PR34固定版本 | 本地验收再ECS | 原v0.4.37 | 只并行构建/下载/隔离测试 | 每次停写和切换前CAS核对远程main |

## Deploy/Scale Artifact Dependencies

| Dependency | Source | Pinning | Mirror/Cache | Unavailable-Source Behavior |
| --- | --- | --- | --- | --- |
| OCI | 官方GHCR | index、平台manifest及config | 本机完整hash缓存、离线归档、ECS旧归档差量 | 下载失败不停止旧服务 |
| 完整可选依赖 | PyPI镜像传输 | 冻结锁与require-hashes | uv已校验包缓存 | 候选构建不通过则不切换 |
| 回退入口 | 旧正式源码/镜像和新停写备份 | 完整SHA、镜像ID、文件hash | 两端受控备份 | 恢复核验失败自动恢复原服务 |

## Release Branch/Train/Candidate Policy

| Policy | Rule | Exception | Owner |
| --- | --- | --- | --- |
| 功能分支 | 短期单问题分支，经PR归并 | 无紧急豁免 | 当前发布会话/维护者 |
| 版本集合 | 新版及五个历史源码恢复点 | 不移动或删除不可变标签 | 维护者 |

## Build Cache And Invalidation

| Cache | Key | Invalidation Trigger | Poisoning Defense |
| --- | --- | --- | --- |
| OCI/blob | 内容digest | 任一内容变化 | 全hash核验后才复用 |
| Cargo/前端 | 源码和冻结锁 | 最新native源码/COPY变更 | 强制新编译、固定源码新dist，不借用旧产物 |
| 部署完整层 | 固定基础镜像ID和冻结依赖 | 官方ID或锁变化 | 完整层ID另记；清单与原正式文件集合一致 |

## Cache Hermeticity Evidence

| Cache | Declared Inputs | Stale-Output Detection | Miss-Path Check | Release Trust Decision |
| --- | --- | --- | --- | --- |
| Mac native | 全部输入SHA256及锁 | source_inputs与wheel hash | 本轮编译tsp-rustdx-native及核心 | 已通过样本，安装前再核对 |
| OCI/完整层 | 固定源码/锁及官方config | build-manifest changed/missing/added | 本轮新源码镜像及无网导入验证 | 正式产物及两端升级身份通过 |

## Release Checks

| Check | Required Or Advisory | Evidence | Failure Response |
| --- | --- | --- | --- |
| PR/main全量、前端、Windows、标签 | 必须 | 成功CI与PR receipt | 不绕过检查 |
| 来源/无网/清单/真实样本 | 必须 | 两架构候选artifacts及smoke | 不切换 |
| 恢复、登录、配置、任务、重启 | 必须 | 每端停写备份及切换后audit/http/live-task/restart | 停止下一端升级并回退当前端 |
| 工作区CSV | 记录差异 | CRLF历史副本归档保留；当前字节等于正式blob，完整diff检查通过 | 保留其他会话修改，最终重新核验 |

## Promotion And Rollback Traceability

| Promotion | Artifact | Target | Rollback Target | Latent-Harm Selection Rule |
| --- | --- | --- | --- | --- |
| 本地 | Mac wheel、固定源/dist及arm64Compute | 本机既有常驻角色 | v0.4.37/f4d8c11b源、旧包、镜像与备份 | 无schema变化；代码回退保留业务增量 |
| ECS | amd64完整Runtime/Compute | 既有七角色+Broker | 同一旧正式SHA的镜像/源及备份 | 数据问题先保留新水位，不直接覆盖业务库 |

## Clean Install Verification

| Install Path | Clean Environment | Expected Version Or Revision | Verification Evidence | Failure Response |
| --- | --- | --- | --- | --- |
| 产物 | 隔离源码、wheel target、无网容器 | v0.4.38/固定完整SHA | 真实Provider、唯一键、逐值与remaining | 保留旧常驻服务 |
| 常驻升级 | 停写验证后的既有数据与配置 | 相同版本/身份 | 深健康、登录、实际任务、正常重启 | 按备份与旧代码回退 |

## Emergency Branch Policy

| Bypass | Scope | Expiry | Reviewer Escalation |
| --- | --- | --- | --- |
| 无 | 本轮采用完整质量门及串行升级 | 本轮结束 | 维护者 |

## ECS 部署最终验收

2026-10-29 08:33 北京时间，用户指定的线上 0.4.38 部署已通过必要验收。
本记录上文的待验证状态为分阶段执行时的历史快照，最终生产证据见
[线上部署记录](deployment-online.md)及[脱敏验收 JSON](deployment-online.json)。

控制库20表、业务库28表实际恢复逐表对账，旧源/镜像/行情/config备份保持；
固定Runtime七角色及Compute配置、双库v6、六功能和公网登录通过。
原118,448,923分钟行保持，真实单日遗漏任务41.179秒补2,315行，
remaining=6如实列入CSV；闭市任务4.148秒零请求。
完整重启后任务终态、结果、结束时间、6行缺口CSV、配置和再次登录均通过，
最终分钟行118,451,238。08:33:15自然健康timer触发成功、退出码0，
覆盖本轮切换观察窗口。备份为`/opt/tsp-full/backups/pre-v0.4.38-5b273174f2e2`。

本次仅完成既有ECS固定版本部署，不将历史源缺口或全市场1秒SLA标为通过；
公开Release和可变latest不属于本次线上切换验收结果。
