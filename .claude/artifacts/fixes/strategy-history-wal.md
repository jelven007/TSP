# Bug: 独立服务首次并发归档的WAL初始化冲突

> Status: FIXED（源码回归通过，主线与正式产物仍须验收）
> Mode: --deep
> Severity: blocker
> Last updated: 2026-10-26

## Symptom / Expected

主线CI 40252290460的后端全量出现1项失败、4507项通过、7项跳过。
`test_concurrent_successes_and_external_updates_invalidate_memory`在首次保存时
报告`sqlite3.OperationalError: database is locked`，失败点为
`PRAGMA journal_mode=WAL`。v0.4.25已由CI分配，但没有源码标签或正式发布。
预期独立服务同进程并发归档完整保存结果，继续保持WAL、FULL同步与事务。

## Reproduction

新增`test_concurrent_first_writes_initialize_wal_without_losing_results`：
屏障同步8个首次写入者，32个全新数据库，覆盖共用与多个仓库实例，
逐条校验8份不同日期的完整结果。没有固定sleep、伪造SQLite异常或吞掉失败。

macOS模块通过不能复现此问题。既有无网络Linux Compute镜像
（Python 3.11.17、SQLite 3.40.1）中，以真实SQLite执行同一测试函数，
旧实现连续三次均在WAL语句处失败；独立反向检查再次失败。
原CI使用Python 3.12.15/Linux，现象一致；不声称两个运行时字节等价。

## Hypotheses & diagnosis

| 假设 | 初始概率/预测 | 结论与证据 |
| --- | --- | --- |
| 初始化在事务锁前并发切换WAL | 40%；失败在PRAGMA而非BEGIN/INSERT | confirmed；CI及Linux三次最小复现均在同一语句退出，串行初始化后通过 |
| 结果内容、归档身份或缓存导致失败 | 25%；错误应在合并/JSON/写入或读取阶段 | eliminated（本次原因）；仅日期与字符串行的输入也在归档事务前失败 |
| 测试共享目录或其他服务竞争 | 35%；隔离临时目录后失败应消失 | eliminated（本次原因）；无网络容器、全新临时目录仍三次失败 |

## Root cause

反向调用链为并发`save`→`_local(write=True)`→WAL切换→建表→
`BEGIN IMMEDIATE`。写事务尚未开始，SQLite事务的等待超时不能保护
多个连接同时进行首次WAL转换。错误状态在连接初始化阶段引入。
真实差异由SQLite/平台调度暴露，不作为无证据的“CI随机失败”重跑处理。

## Fix

同进程的`StrategyResults`实例共用初始化锁，串行执行WAL、FULL和建表，
随后按原流程进入写事务。锁不覆盖策略计算或完整归档事务，不新增API、
schema、依赖、配置或磁盘格式，不改变PostgreSQL租户路径。
既有独立服务仍按单应用进程运行；此进程锁不提供跨进程首次转换协调。

## Verification

- V-1：Linux旧实现三次连续失败，错误和CI一致。
- V-2：Linux修正后共用/多个实例连续三轮通过，共1536份结果逐条读回。
- V-3：独立镜像换回旧实现，保留新测试，重新报告WAL锁冲突。
- V-4：macOS归档仓库与历史API模块`47 passed`。
- V-5：诊断标记仅在外部临时运行器中，生产源码没有调试输出。
- V-6：主线CI、Linux正式Runtime和桌面产物仍另行验收。

## Regression test / Defense-in-depth

新增屏障回归覆盖真实并发首次初始化及不同实例共享同一数据库的入口；
逐条读回验证没有丢失结果，不仅检查线程返回或历史行数。
继续运行旧任务顺序、去重、缓存重建、分页、删除和写失败保留成功的模块测试。

## Pattern analysis / RCA

实际`git grep -n 'journal_mode=WAL'`只命中该初始化点。
`StrategyResults`实例由screener API及归档保存辅助函数创建，单独实例锁不能
覆盖多个实例；因此使用进程共享锁。没有修改其他SQLite或市场文件写入路径。
原WAL初始化于2026-10-25提交`b4bcd8a8`引入。本地及多次CI未暴露此调度，
现有四线程单次测试不足以稳定复现；新增屏障、多次全新库及Linux反向验证预防回归。
多进程首次打开同一全新SQLite不属于此次单应用进程验收，不宣称已协调。

## 六类生命周期与发布

| 领域 | 本次影响 |
| --- | --- |
| 需求 | 既有FR-STRATEGY-05可靠归档要求，不新增编号 |
| 方案 | 同进程初始化串行，原SQLite/PostgreSQL事务及缓存契约保留 |
| 开发 | 仓库初始化锁与真实并发测试，无接口或依赖变更 |
| 测试 | Linux三次复现、三轮通过、反向失败，macOS47项模块回归 |
| 运营 | 失败质量门不发布v0.4.25；修正经主线自动分配后继版本 |
| 运维 | 现有服务保留，升级不清库或删除WAL，旧代码/配置可回退 |

对应手册更新为架构、测试与运维。后继版本、标签及实际升级结果由最终交付记录核对。
