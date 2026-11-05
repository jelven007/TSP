# S02 实时结果延迟优化 Implementation Plan

> **For agentic workers:** 按已批准的顺序在当前独立工作目录执行，使用 executing-plans 和 dev-tdd；不提交、不推送、不自动部署。

**Goal:** 去除分钟完成至公开发布的300秒等待，保障实时计算，复用已预计算行情，并记录真实可见耗时。

**Architecture:** L3聚焦修改既有分钟服务、公共发布器、Broker和只读策略加载链。行情继续经过完整不可变release和租户事务；私有Python仍每任务隔离。当前线上诊断确认261秒发布等待，发布后62秒尚未拆分。

**Tech Stack:** Python、Polars、FastAPI、不可变Parquet发布、Unix socket Broker、PostgreSQL事务。

## 1. 完整分钟完成后异步发布

- [x] 在 `backend/tests/test_realtime_latency.py` 写测试：14:31成功落盘才通知发布，空轮/失败不通知，发布线程被通知唤醒、通知合并、旧release保持不变。
- [x] 运行 `python -m pytest tests/test_realtime_latency.py -q` 确认缺失行为失败。
- [x] 修改 `services/minute_refresh.py` 在完整成功落盘后调用 `publisher.request_minute_publish(completed_at)`，记录fetch/write时间；发布不在采集线程执行。
- [x] 修改 `multitenancy/publication.py`：尾盘14:25–14:40事件提前唤醒，最小5秒间隔，常规300秒和容量不足退避不变；`platform.py`先注册发布器再启动采集。
- [x] 将 `PublicBindingSynchronizer` 的轻量release检查间隔改为1秒，复用既有API绑定和Worker轮询，配置冲突/繁忙实时重试收敛到1秒。
- [x] 跑分钟、发布、绑定完整测试文件，核对快照和停止路径。

## 2. 实时优先和尾盘保留资源

- [x] 测试两个槽位有一个研究任务时实时仍可进入；等待的实时任务优先；超时和释放后计数不泄漏；并发配置减少后旧任务正常结束。
- [x] `BudgetPermits.acquire(priority=False)` 管理优先等待者和后台活动数量，使用既有50%总预算；默认策略不变。
- [x] Broker按任务种类monitor决定优先级；14:25–14:40且有效槽位≥2时预留1槽，单槽仅队列优先不突破预算。预留等待者不挤占普通16个有界排队名额。
- [x] 运行 `tests/test_strategy_concurrency.py tests/test_full_compute.py`。

## 3. 复用已发布行情和减少冷启动

- [x] 冷只读release测试：完整股票池参考窗口直接读取已发布基础enriched列，实际交易日N+1窗口，完整标的保留，不重算全套指标；两个release互不复用；无release维持旧路径。
- [x] `ScreenerService.build_daily_pool_context`仅在read_only且不可变release下直读池参考所需分区，实例内最多4项/32MiB缓存；策略技术条件的指标计算仍保留。窄表Scanner默认不透传证券状态，复用原证券状态内核最小分支，并保留成交量证据，避免改变ST/停牌统计。
- [x] `compute_worker.execute` 将仅研究需要的Backtest导入移至monitor返回之后，减少每次隔离任务冷启动。
- [x] 同输入旧/新比对11个共同参考字段及6组完整股票池/元数据，保存样本耗时；基准为完整市场组件，不宣称线上S02端到端实测。

## 4. 真实时间与验收

- [x] 增加完整分钟采集完成、release公开、Broker入队/开始/完成、Worker准备/计算/事务提交时间；结果新增可选latency，不改变entry_evaluated_at的策略计划含义。
- [x] 复用有界observability阶段计数，关联release/sequence；日志不含租户配置或凭据。
- [x] 保存成功提交后日志；失败和配置冲突不得伪装已发布结果；live latency不进入日归档，API按租户传播。
- [x] 真实隔离PostgreSQL受影响216项通过、2条既有warning；修改文件Ruff通过，另外14条既有诊断无新增。正常/并发/冷缓存组件基准通过，线上交易窗口及页面接收未验证。
- [x] 以baseline哈希仅迁移23个本次修改文件回共享工作区，所有基线及候选哈希一致；整合后真实PostgreSQL216项、Ruff及make check通过。独立补丁和报告已保存，版本保持0.4.46；未提交、推送、部署。

验收目标：数据14:31:30前可用时，确认至页面可见P95≤20秒、P99≤30秒；必须实测才能标为达成。本轮不重跑回测、不修改选股/买卖/仓位/费表，也不改实际账户资金。线上部署需完整API/Platform/Worker/Compute共同构建；本地验证不能替代线上达标。

证据：`reports/engineering/s02-latency-20261104/report.md`及同组基准、补丁、哈希和反向验证。
