# 构建与生产升级复核

复核日期：2026-10-25。范围为本批源码整合、现有本地环境及
`https://shanao.asia` / `118.196.108.188` 的原地版本升级。
用户已明确授权推送、合并及升级。本报告不扩大生产容量、SLA或业务签收。

## Build and release pipeline

冻结153个起始文件→补齐生命周期文档与测试夹具→完整质量门→独立分支提交→
本地候选Compute重建和完整重启→推送/PR复审/合并→主线CI自动版本/标签→
相同CI revision的Runtime和Compute→本地确切版本同步→生产停写备份/临时恢复→
固定产物完整启动→深度健康、HTTPS、登录、历史与配置核对。

| Pinned input / artifact | Required check | Availability / cache / unavailable behavior |
| --- | --- | --- |
| 源码 | 完整commit、Git tree及逐文件范围；最终使用ci-revision | 不把分支头或latest当产物身份，CI失败则不升级 |
| Python / JS / Rust | 现有uv.lock、pnpm-lock和Cargo.lock；无依赖变更 | 冻结安装，缓存只复用相同输入；不改锁绕过下载失败 |
| Runtime | CI成功SHA、architecture、OCI revision、build-manifest逐项校验 | 正式镜像从该CI后续构建取digest；下载失败恢复确切旧产物，拒绝替代latest |
| Compute | 原Compute.Dockerfile、固定Runtime、冻结backtest闭包 | 本地arm64与生产amd64分别记录；同架构产物只构建一次，部署用确切ID |
| 本地候选Compute | 与既有冻结依赖相同的本地Runtime层及候选app/版本 | 本地API/Vite来自同一冻结工作区；该检查不作为正式Runtime产物签收 |
| 配置和业务数据 | 原配置权限/受控散列、双库schema v6及恢复摘要 | 不改密码/注册/SMTP，不重跑种子初始化；停写时保存一致备份 |
| 启动器/Broker | 平台目录任务/缓存刷新→消费者；配置目录读取 | Broker保留旧上限；平台偏好原子替换后通过目录读取新配置 |

构建缓存按Docker COPY内容和冻结锁失效；清单核验防止旧模块残留。
前端在本次源树实际重新构建；Compute明确先移除旧app层再复制候选。
本批无并发发布；主线出现更新时重新核对CI revision，禁止旧构建覆盖新产物。
短期整合分支合并后结束；紧急分支也必须固定SHA、补质量门并回归main，
不维持未归并的第二条生产线。

## Readiness Matrix

| Domain | Status | Evidence | Freshness / drift | Release condition |
| --- | --- | --- | --- | --- |
| 需求与架构 | Pass | 六类手册、真实调用链及本批范围 | 2026-10-25核对 | 不把目标或旧实例验收写成新ECS事实 |
| 源码质量 | 检查后记录 | [验证记录](verification.md) | 本批重跑 | 后端/前端/工具/原生/静态检查无阻断失败 |
| 本地完整环境 | 产物后检查 | 八进程、真实数据库/Compute与登录历史 | 升级前仍是旧进程/Compute | 候选完整启动通过后再推送 |
| 生产产物 | CI后检查 | ci-revision、镜像digest、build-manifest | 升级前0.4.21/7823b84b | CI和产物身份全部通过 |
| 恢复 | 切换前检查 | 停写双库dump、两个临时库实际恢复和业务摘要 | 初始备份不代替升级前备份 | 备份及恢复成功后再更换运行产物 |
| 安全/隔离 | Pass及上线复核 | RLS、事务回滚、失效身份、配置不输出；固定沙箱 | 当前测试；上线后再核对 | 登录、租户隔离、凭据保留和文件权限通过 |
| 运维健康 | 上线后检查 | API/Gateway status=ok、所有functions ready、维护timer | 当次实际运行状态 | 失败停止推广并恢复已保留旧代码/配置 |
| 容量、邮件、模型、交易日 | Follow-up | 本批明确未签收范围 | 原限制继续有效 | 不扩大用户/资源上限，不对未验收能力作承诺 |

## Responsibility

| Area | Owner | Escalation path | Evidence |
| --- | --- | --- | --- |
| 本次发布与回退 | 当前用户授权会话执行者 | 失败先停止切换，按产物/代码/配置回退 | 用户请求、Git/CI和部署记录 |
| 后续维护与响应 | 用户的ECS维护者 | systemd日志、深度健康、备份及云主机诊断 | operations手册和服务器evidence |

## Architecture and fault domains

| Component | Production dependency | Fault domain | Reference |
| --- | --- | --- | --- |
| Nginx→API / Gateway | 本机端口、TLS和持久身份 | 同一ECS | architecture、deploy/full |
| Platform→公共不可变版本→API/Worker | 官方目录、Parquet发布及内部Token | 同一磁盘/ECS | operations与平台启动器 |
| 四类Worker→Broker→无网络Compute | Unix socket、固定Docker产物、全局策略预算 | 同一执行主机 | compose和Broker配置 |
| 控制/业务PostgreSQL | 非owner运行角色、schema v6、RLS | 同一ECS | 双库备份与临时恢复 |

## SLOs and observability

| Journey / signal | Current evidence / target | Response / bounded follow-up |
| --- | --- | --- |
| 登录、公共读取、研究/监控/交易功能 | 当次HTTPS和深度健康通过；无已签收SLA/error budget | 不外推SLA；失败恢复旧版本，部署后持续观察 |
| 深度健康 | 每分钟timer，status与全部functions须通过 | 缺失/失败使unit非零并留日志，由ECS维护者响应 |
| 容器退出/重启、磁盘/inode | Docker状态及公共版本维护timer | 原保留策略继续有效，失败停止清理 |
| 24小时与完整交易日 | 既有观察尚未结束；新版本重新开始记录 | 次日复核采样，首个交易日另签收；无对外告警转发 |

## Safe change and availability

| Change / recovery | Mechanism | Evidence requirement / gap |
| --- | --- | --- |
| 升级 | 先构建核验，停写备份，切换固定产物，按既有顺序完整启动 | 不在线覆盖运行镜像源码，不清空归档，不重导入业务 |
| 同schema代码回退 | 保留旧Runtime/Compute/源码/启动器/配置，停止后恢复运行组合 | 不用旧数据库覆盖升级后增量；恢复数据前另保存并对账 |
| 主机/区域故障 | 同机备份与旧产物 | 单故障域，无异机恢复/静态故障容量签收；本批未改变可用性架构 |

## Security

| Area | Evidence | Residual risk / owner |
| --- | --- | --- |
| 输入/持久化 | 参数化SQL、租户事务/RLS、有效日期/版本身份、失败保留 | 历史随业务增长需维护者监测容量 |
| 凭据 | 源码/报告扫描；真实授权码只在受控环境，配置0600 | 不输出环境文件和登录Cookie |
| 隔离计算 | 无网络、UID10001、只读市场、内存/CPU硬限额 | 本地单机内存估算与生产Docker硬限额分别说明 |

## Launch blockers / follow-ups

| Item | Required detail | Due / stop condition |
| --- | --- | --- |
| 未完成的质量/产物/本地/备份检查 | 对应实际成功证据 | 各阶段执行前完成；任何失败暂停该阶段并修复 |
| 真邮件/模型、交易日、容量与异机恢复 | 各项独立验收，不从烟测推导 | 下一运营接收复核列未完成项；扩大服务承诺前完成 |

本批没有新增需用户确认的例外；现有单ECS、同机备份、未签收SLA及负载范围
继续明确标识。用户已授权在该既有范围升级，不能把授权写作这些目标已经验收。

## Launch watch and handoff

| Watch window | Success | Abort | Responder handoff |
| --- | --- | --- | --- |
| 完整启动及本次烟测窗口；随后24小时观察 | 正式SHA/版本/资源、登录、所有functions、日期记录和原配置一致 | 身份错误、无法登录、功能不ready、备份/恢复不匹配 | 当前执行者处理切换故障；ECS维护者接收次日观察和未完成预置项 |

## Advisory posture and impact

| Dimension | Applies | Boundary |
| --- | --- | --- |
| External commitment | 既有公开服务 | 不新增SLA/容量/交易结果承诺 |
| Customer-criticality | 登录、研究、监控与模拟交易 | 必须检查完整用户路径 |
| Data sensitivity | 租户业务与凭据 | 不发布真实配置和私有结果 |
| State durability | 新策略归档及原业务 | 停写一致备份、恢复核验、增量保护 |
| Blast radius | 一个既有ECS及本地环境 | 不向其他实例或用户扩大资源预算 |

结论：按逐阶段门禁执行已授权升级；只有实际通过的阶段可声明完成。
