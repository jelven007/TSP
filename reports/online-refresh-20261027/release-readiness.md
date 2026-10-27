# 发布身份与生产验收记录

日期2026-10-27。主流程release-build-reproducibility，高置信度；
上线单独采用production-readiness-review。范围为PR #28整合及ECS Docker升级、
用户授权清空有效六周期K线。不发布桌面平台，不变更本地运行数据。

## Build and Release Pipeline

冻结46文件 → 精确diff复审 → PR质量门 → main自动版本提交 →
同ci-revision的质量门与标签 → 固定SHA多架构Runtime → ECS同Runtime构建Compute →
manifest/config/rootfs验收 → 停写/恢复验证 → 清理/新公共发布 → 一次切换 → 线上验收。
并发版本不抢占：切换前复核main/ci-revision/tag一致，变动则重新评估；
latest不作为部署身份。PR源码85d753b，已squash合并为6fbf27f；
main自动分配v0.4.32，最终revision为4da5f30c9b5165612d78b573c142612f3fe25395。

## Verified Artifact Inputs

| 项目 | 实测结果 |
| --- | --- |
| main/tag/ci-revision | 均为4da5f30c9b5165612d78b573c142612f3fe25395 |
| main CI | 40370758749成功；后端4610通过/9跳过、前端364、工具134、原生6 |
| Docker CI | 40371792120成功，固定同revision |
| Runtime index | sha256:d4ebf07c54e20fcc5d96695520999efa58887dfd9f12d2c401f3d115f52d4059 |
| amd64 child manifest | sha256:56fd2a062c30daa409eda4d012d54fa0220ce303c89f61704039d0a8e59e77c0 |
| amd64 config digest | sha256:e46d2f8772f81ba3d3f8aa13cb2212931607e4f114cb17cdd9d497125cdded42；Docker ID另见artifacts |
| git archive SHA256 | 0d1334b5599d29611ff37ca4acd4dedf5fdcde1951d8827ebf46c4575d142845；ECS上传后核对相等 |
| ECS准备门 | Pass，Runtime/Compute已验收；623文件清单相等，native及vectorbt导入成功 |
| 数据清理/切换门 | Pass，备份及恢复已验收；允许按本次白名单清理并切换上述版本 |

注册表核对时latest仍指向旧index，本次直接使用上述不可变digest，
不依赖latest传播。Runtime实际Docker ID为amd64 child 56fd2a062c30；
Compute ID为c6e6c4765f38，完整值见artifacts.json。
离线归档477998080字节，SHA256 a835d28331751805aaac88ffd588f762ab41c7833aa2ec72bdb7c09b0ce58438，
本地与远端相等；源码清单SHA256 e4b15f2de21a1cac16062398bbffda0710215bbb6aaf914162e78d6c59b30b3b。
本次停写备份已完成：/opt/tsp-full/backups/pre-v0.4.32-4da5f30c9b51。
双库临时恢复成功，control/business schema均为6，业务核对项与停写源相等；
12目录3760文件复制并比对SHA256。旧Runtime/Compute归档586871808字节，
保存后的RootFS/labels与旧镜像相等，SHA256为
3b9e8a75839d6c04453f53459a5ad86dcf682854708399b0d7ac6766744a335a。
备份创建时全部市场写入者已停，用户当前配置已纳入停写备份。
清理只移动12个白名单目录到retained-originals并发布空current；
切换要求clear JSON成功、配置相等、同revision产物。清理或切换失败先调查，
必要时按本次匹配备份恢复，不自动重新采集。

## Pinned Inputs / Cache

| 输入 | 约束与证据 |
| --- | --- |
| 源码 | Git不可变SHA；git archive仅包含提交文件 |
| 依赖 | uv.lock、pnpm-lock.yaml、Cargo.lock；frozen/locked安装 |
| 构建工具 | 仓库Dockerfile及工作流；基础镜像构建时解析的digest由构建证据记录 |
| 输出 | 注册表index/amd64子manifest/config digest、RootFS diff_ids与build manifest |
| 缓存 | CI GHA按Docker COPY/ARG输入失效；源码/app/static版本清单校验防旧输出 |
| 缺缓存路径 | CI质量门重新安装/执行；产物必须重新hash验证，不以缓存命中代替验证 |
| 配置 | 私有配置只在受控主机；报告只记录摘要与相等性，不复制秘密 |

基础镜像标签本身不是长期可重现的digest锁；本次推广的是注册表固定已验收产物，
不在生产用标签重建Runtime。Compute从相同Runtime并按冻结依赖补充backtest。

## Artifact Availability / Rollback

| 产物 | 来源/固定方式 | 可用性 | 源不可用时 |
| --- | --- | --- | --- |
| Runtime | GHCR版本SHA/index digest，ECS固定image ID | 先验缓存/镜像源，必要时离线传输同config/rootfs | 不切换，保留旧服务 |
| Compute | 同Runtime、相同SHA与frozen backtest | ECS构建验收后固定ID | 不提升 |
| 回滚 | 0.4.31代码6b86f724与Runtime a65fa3cbbf76、匹配Compute | 已有本机镜像；停写时另留标签/备份 | 数据按本次匹配备份恢复 |

无紧急分支或质量门绕过。必要门为PR/main CI、不可变产物、配置保护、备份恢复、
精确清零及登录/API验收；桌面平台不在本次范围。

## Readiness Matrix

| 领域 | 状态/新鲜度 | 证据与提升前要求 |
| --- | --- | --- |
| 源码 | Pass，本批实测 | 后端4465、前端364、工具134；负例47失败 |
| 远程质量门 | Pass，本次CI实测 | PR #28及main成功，自动版本/tag与ci-revision相同 |
| 架构 | Pass，现网核对 | 单ECS，Platform→immutable公共文件→API/Workers→Broker/Compute，PG双库 |
| Runtime/Compute | Pass，ECS实测 | 注册表/config/rootfs/623文件manifest、原生桥与vectorbt；固定Docker ID |
| 恢复 | Pass，停写后实测 | 双库恢复临时DB并比对业务项；12目录3760文件hash相等，旧镜像归档可读 |
| 配置/权限 | Pass，现网受控摘要 | admin/Admin123与用户当前注册/找回/SMTP，除自动同步外保留 |
| 容量 | Pass，本次preflight | 可用约122GiB，新增备份与产物先预算 |
| 行情完整性 | 清零实测通过 | 12个源/current目录为空，11类画像null，初始化pending |
| 运行/监控 | 基础通过；全功能未就绪 | API/Gateway ready，非指标依赖正常，七角色/PG healthy；自动同步随后开启，分钟全功能巡检FAIL |
| 浏览器 | Pass，真实HTTPS实测 | admin登录、六周期/两模式、来源/周六、当时自动同步关闭、1440px/390px |

## Responsibility / Architecture and Fault Domains

当前会话执行代理负责本次发布、停止及回滚；验证结果和恢复路径交付仓库维护者。
线上手工/半自动路径通过现有systemd与compose执行，私有配置不进入代码仓库。
所有组件及PG共用一台ECS，主机是共同故障域；本次未新增HA或外部服务承诺。
恢复机制为受控双库备份、匹配代码/镜像及K线目录；本次实际恢复演练结果单独记录。

## SLO / Observability

本次验收目标：账户可登录，API/Gateway返回正确版本并ready；
除空K线的indicator_cache外依赖正常，七角色保持healthy；源及current K线均0。
连续两个检查周期核对容器、worker心跳与12目录。现有分钟health timer及API作为信号。
没有可验证的线上累计SLO/error-budget数字，不声称存在余额或趋势；
本次不加负载，限定一次受控停写窗口，错误即停止提升并使用匹配恢复路径。

## Safe Change / Security

先完成全部产物、停写备份和恢复，才移动12个白名单目录；不调用会额外删除业务的全量clear API。
保留非K线文件、PG业务和旧研究引用。清理、启动及首轮审计时自动同步false。
上线后02:19:29设置API成功将其开启，仅该偏好发生变化；保留随后主动设置，
记录未来自动任务可能回写。验证浏览器已在此前退出，没有写设置。
新公共current不包含K线；旧不可变研究输入和受控备份保留，不称磁盘全部销毁。
无新增外部信任边界或schema变更。秘密只在原受控配置；证据记录hash/布尔核对结果。
回滚需同时恢复匹配配置、源K线/generation/data release及代码/镜像，不只移动版本指针。

## Launch Blockers / Exceptions

产物、恢复、清理、切换、基本运行、浏览器、持续观察和末轮审计均已实测签收。
126.9秒观察中两次新巡检均确认12源/current目录为空，七角色无重启、非指标依赖正常。
首末两次业务/配置/秘密核对通过；clear、promotion、post-audit及online-watch JSON已留证，
后续设置变更单独留证。此签收不认证缺指标时的全功能巡检通过。
空K线造成指标/研究/监控/模拟交易不可用是用户本次删除要求的直接结果，
验收会如实报告；有效期至用户重新采集，刷新触发为自动同步或手工获取。
单主机无HA及累计预算未知是既有部署边界，本次无新增扩容/容灾承诺。
自动同步随后开启使外置分钟巡检的空K线例外不再生效；
full-health为FAIL，基础API仍ready且非指标依赖均正常。
验收范围明确为本次升级与当前K线清零，不宣称全功能就绪或未来持续无行情。

## Launch Watch and Handoff / Impact

切換后观察至少两个分钟检查周期：成功条件为登录、版本一致、基本ready、非指标依赖正常、
K线不回写、配置/双库业务相等；异常版本/业务/秘密变化或非预期健康错误即停止并恢复。
交付报告包含SHA/digest、备份位置、自动同步由关闭变为开启的实测及预期缺数据影响。
外部影响限既有站点用户；涉及账户配置但不改秘密，持久业务保留，
明确删除持久行情，因此数据恢复与准确清零是必要门。
