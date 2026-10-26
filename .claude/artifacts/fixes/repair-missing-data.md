# Bug: K线修正缺少仅获取遗漏数据的路径

> Status: FIXED
> Mode: default
> Severity: functional
> Last updated: 2026-10-26

## Symptom / Expected

数据总览的修正入口只有单一获取动作，按所选日期重新拉取并覆盖已有值。
应拆成获取全量数据、获取遗漏数据；遗漏先检查证券、日期及分钟内部缺口，
仅请求缺失部分，已有及并发写入的记录保持原值。

## Reproduction

入口：数据总览→修正数据→K线修正。
回归在 `backend/tests/test_repair_kline.py` 和
`frontend/src/components/data/RepairDailyPanel.test.tsx`。
修改生产代码前，后端新增预期失败19项、前端预期失败4项。
隔离副本恢复旧实现后，单股日K/内部分钟缺口用例连续三次均失败；
双按钮与mode提交用例连续三次均失败。恢复当前实现后分别2项、1项通过。
反向验证没有stash或覆盖原工作区。

## Hypotheses & diagnosis

| 假设 | 初始优先级/置信度 | 结论 | 证据 |
| --- | --- | --- | --- |
| 获取方式没有进入服务，所有提交都按全量执行 | 1 / 高 | confirmed | Panel仅调用repairDaily(startDate, period)，API及服务没有mode，日K直接run_now、其他周期sync_period |
| 已有缺口算法仅使用最新日期，无法识别内部缺口 | 2 / 中 | eliminated | 修正调用链没有缺口规划器，最新日期只用于界面参考；没有可复用的遗漏取数路径 |

## Root cause / Fix

修正入口、请求与服务原来只表达周期及起点，未区分全量与遗漏。
原落盘为upsert，直接复用它会把已有记录重拉并覆盖。
新增可选mode，缺省full保持旧调用；L3局部接线复用原Provider、股票池快照、
周期标准化、分区锁、原子替换、任务和共享弹窗。

`services/kline_gaps.py`按日/周扫描键、每100证券检测缺口；
分钟只合并连续缺失标签，休市、上市前、未形成区间排除，未知日历拒绝猜测。
返回值仅保留本次缺口，锁内only_missing再次排除已有值。
日K仅以本地因子重算受影响证券，重建kline_enriched视图并失效daily缓存，
收尾刷新仓库及广播。真实重算用例曾发现漏刷新视图，补齐后通过。
10分钟只补具备完整十根本地1分钟的桶，不请求远程。
无缺口零请求成功；未补齐保留已补数据并报告剩余，不能冒充完整。

## Verification

- V-1：上述后端/前端反向验证3/3失败，恢复后通过。
- V-2：受影响13个后端测试文件，205 passed。含真实enriched、并发先补、
  100证券上限、取消、部分返回/失败、分钟/周/10分钟边界及原全量兼容。
- V-3：数据组件、Data页及Modal回归，11文件47 passed；pnpm build、pnpm lint通过。
- V-4：新模块Ruff通过，五个已有文件对比HEAD新增告警0；历史告警仍为API44、kline_sync36。
- V-5：本机Chrome/Playwright隔离接口验证full/missing参数、禁用/失败重试、关闭重开；
  1440×1000及390×844浅深色按钮可见、可点击、无越界，pageerror为0。
- V-6：make check通过，版本仍0.4.30；未提交、推送或部署。

## Pattern analysis

生产调用点通过git grep检查run_repair_daily及repairDaily。
API是本次用户入口；data_integrity的自动自愈继续缺省全量，用于既有异常分区修复，
不应被改为只补记录。离线历史sync_period也保留全量用途，未扩大当前按钮拆分范围。

## Limits / Follow-ups

检测的是缺记录，不是已有行的字段异常。停牌或上游无记录仍提示未补齐。
未知年份仅在有覆盖证据的源日历内判断；不把工作日当成交易日。
浏览器采用真实组件及隔离响应，不证明真实上游历史覆盖。生产运行未更新。
日K任务取消后可能保留已写原始数据；需要时通过既有Enriched重算恢复派生数据。
