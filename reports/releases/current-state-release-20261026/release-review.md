# 构建与生产发布复核

核对日期：2026-10-26。用户已授权仓库合并、新版本发布和本地/既有ECS原地升级。
主复核为release-build-reproducibility，上线复核为production-readiness-review；
覆盖既有单ECS范围，不新增容量或SLA承诺。各门只有实测后才能从待验证改为通过。

## Build and release pipeline

50文件冻结→六类文档核对→全量质量门→精确暂存审查/提交→PR/CI/完整复审→
main自动版本/标签→同ci-revision的Runtime→固定Runtime构建Compute→
本地完整环境升级→线上停写双库备份/临时恢复→完整启动/HTTPS/登录/功能核对。

| Pinned input / artifact | Required check | Availability / cache / unavailable behavior |
| --- | --- | --- |
| 源码 | base 8ae79f17、逐文件散列、最终完整ci-revision | 独立工作目录；不从脏工作区或latest部署；主线有新提交时复核身份 |
| Python/JS/Rust | uv.lock、pnpm-lock、Cargo.lock及工作流工具链 | 冻结依赖；本地复用既有依赖环境，仅测试源码隔离；正式产物由CI重建 |
| Runtime | CI质量门通过、双架构、OCI revision/version、build-manifest | 固定digest下载/保存；源不可用则保留旧服务，不换用latest |
| Compute | 固定Runtime、冻结backtest依赖、原生桥、无网络执行 | arm64本地与amd64生产各建一次，以确切ID部署 |
| 缓存/生成文件 | Docker COPY覆盖app/static/锁/原生包；前端重新生产构建 | 旧app/static先移除，manifest校验防残留；行为输入变化不复用测试结果 |
| 业务数据/配置 | schema v6、停写双库dump及临时恢复摘要、配置0600/散列 | 不重跑种子，不替换管理员/口令/SMTP；旧数据不用来覆盖新增业务 |
| Broker/启动器 | 固定Compute、既有资源限额、Platform目录/缓存先于消费者 | 保留启动器/配置/旧镜像；平台偏好目录挂载读取更新后的文件 |

支持最新及最近五个历史源码版本，保持标签/Git历史不可变；离线恢复集合提供提交和哈希。
本批串行发布；旧构建不能提升覆盖新主线。构建停滞保持既有服务。
短期整合分支合并后结束；紧急修复同样固定身份、补门禁并回归main。
回退默认选择本次升级前已验证的Runtime/Compute/源码/启动器组合；
若故障来自更早变更，再依据历史验证选择较早已知健康版本，不能只回退latest指针。

## Readiness Matrix

| Domain | Status | Evidence | Freshness / drift | Release condition |
| --- | --- | --- | --- | --- |
| 架构/契约 | Pass | 六类手册与[核对记录](verification.md) | 2026-10-26源码核对 | 独立周期、租户事务、历史兼容，未新增schema |
| 质量/行为 | 待验证 | 本批后端/前端/工具/原生/隔离web/static | 只认可本次冻结源树结果 | 零阻断，差异回归通过 |
| 本地运行 | 待升级验证 | 八服务、固定Compute、登录及历史接口 | 升级前0.4.23/8ae79f17 | 新版本完整运行且用户配置保留 |
| 正式产物 | 待CI | 自动版本/标签、ci-revision、digest、manifest | 升级前主线0.4.23/生产0.4.22 | 镜像/桌面全部固定同SHA |
| 恢复/安全变更 | 切换前必须验证 | 一致双库备份、两个临时库恢复及摘要 | 升级前重新执行 | 未完成不切换；保留旧代码与受控配置 |
| 权限/隔离/秘密 | 测试及升级后核对 | RLS/写权限/回滚、原生无网络Compute、配置权限 | 当前全量及上线烟测 | 登录、角色、旧业务、凭据均有效 |
| 运维/依赖 | 升级后核对 | API/Gateway深度健康、全部functions、systemd/timer | 单ECS七角色及PG升级前健康 | Platform先完成目录/缓存，消费者后启动 |
| 可用性/容量 | Follow-up | 单故障域、既有资源限额、同机备份 | 既有边界，本批不扩大 | 不承诺异机恢复或容量；扩容前独立验证 |
| 运营观察 | Follow-up | 24小时/完整交易日、邮件/模型/Windows真机 | 新版本重新计时 | 2026-10-27复核持续健康；首个完整交易日另验收 |

## Responsibility

| Area | Owner | Escalation / evidence |
| --- | --- | --- |
| 当前发布/回退 | 用户授权的本次执行者 | 门禁失败停止该阶段，依据原镜像/代码/配置回退 |
| 上线后响应 | 用户的ECS维护者 | 深度健康、systemd、业务/API日志与运维手册，次日观察交接 |

## Architecture and fault domains

Nginx/TLS→API/Gateway→控制/业务PostgreSQL；Platform→公共不可变发布/行情缓存→
API/四类Worker；Worker→Broker Unix socket→无网络、只读市场、UID10001的Compute。
各角色、数据库与公共市场位于同一ECS/磁盘。该拓扑与资源预算保持既有状态。
关键依赖升级前健康；切换后必须验证每个functions ready，不仅验证端口或容器running。

## SLOs and observability

登录、公共读取、研究/监控/模拟交易以HTTPS和深度健康核对。
既有系统未签收SLA/error budget，不能虚构剩余预算或burn趋势；
当前升级只沿既有资源与服务范围，完整启动烟测后持续观察，故障按固定组合回退。
每分钟健康timer必须检查status和全部functions；容器退出/重启与磁盘/inode
由原维护timer和日志记录。未建立对外告警转发，2026-10-27由维护者复核。

## Safe change and availability

备份后临时恢复验证控制/业务库摘要，删除临时恢复库不触碰生产库。
升级更换固定镜像和源码，保留原市场/业务/用户配置。原schema兼容，无在线数据迁移。
同schema回退不自动恢复旧库；确需数据恢复时先备份升级后的增量并对账。
单主机/区域故障无静态故障容量或异机恢复签收；本批不宣称改变该架构。

## Security

参数化SQL、租户事务/RLS、业务写权限及同源保护由现有调用链和本批全量测试核对。
报告只记录身份/摘要/散列，不含真实邮箱授权码或登录Cookie。
升级保留现有admin配置、注册/找回口令及SMTP，不生成随机替代值。
Compute用固定产物、无网络、只读市场与原资源硬限额；身份和manifest不符停止推广。

## Launch blockers / follow-ups

| Item | Required detail / due | Stop condition |
| --- | --- | --- |
| 质量、PR复审、CI和正式产物 | 各阶段当次证据 | 任一失败停止该阶段并修复 |
| 备份/恢复、配置、启动/登录 | 切换前备份恢复；切换后立即核对 | 不一致即停止推广并恢复原运行组合 |
| 页面设置原报错 | 重现错误/访问地址/触发条件，再补回归 | 正常路径通过不能声明原故障已修复 |
| 历史预置、邮件/模型、交易日/容量/异机恢复/Windows真机 | 2026-10-27运营接收列缺口，各项扩大承诺前完成 | 不把烟测、部分行情或100%当全部初始化 |

Exception register：本批没有新增风险例外或扩大服务承诺。
现有未签收可用性、负载、邮件/模型等边界列为Follow-up，不把用户升级授权写作签收。

## Launch watch and handoff

完整启动窗口成功条件：版本/SHA/digest一致、管理员登录、全部functions ready、
旧业务/行情可读、配置保留。身份错误、登录失败、功能不ready或恢复摘要不匹配时停止。
随后24小时及首个完整交易日继续观察；本次执行者负责切换故障，
ECS维护者接收次日健康、资源、任务状态及剩余预置清单。

## Advisory posture and impact

| Dimension | Applies | Boundary |
| --- | --- | --- |
| External commitment | 既有公开服务 | 不新增SLA/容量/交易结果承诺 |
| Customer-criticality | 登录/研究/监控/模拟交易 | 完整用户路径必须核对 |
| Data sensitivity | 租户业务和凭据 | 不发布配置或真实业务明细 |
| State durability | 原业务与策略归档 | 一致备份、实际恢复和增量保护 |
| Blast radius | 一个既有ECS及本地 | 不扩大资源或部署到其他实例 |

执行姿态：逐阶段完成已授权升级，只有证据齐备的阶段可声明通过。
