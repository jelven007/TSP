# 早盘清仓双阈值与双时间 Implementation Plan

> 在本会话逐项执行，使用已授权的内联执行、TDD和新鲜验证；不提交、不推送、不新跑收益回测或ECS部署。

用户2026-11-03补充：后续若另获授权运行资金对照回测，只使用20万、50万、100万元三档；
历史结果和本计划原验证范围保持，不因资金档更新自动新增回测。

**Goal:** 当前早盘清仓可设置上涨/下跌阈值、10:30止损和10:45兜底，容量不足的余股利用真实后续分钟成交。

**Architecture:** 基于正式v0.4.45加当前已运行的截止失败账本补丁建立独立工作目录。
信号模块统一负责涨跌及时间阶段；现有连续资金撮合只延长真实卖出窗口和最终失败检查。
旧振幅冻结输入继续读取，当前目标信号明确转换为新参数，历史研究结果不改写。

**Tech Stack:** Python/Polars/FastAPI，React/TypeScript/Vite，现有隔离Compute与租户SQL配置。

---

工作目录：`/private/tmp/tsp-morning-exit-dual-time-20261102`。
基底提交：`4c3ad9b694da0ec2b5ac6f3118e8b86d7cd2ebeb`，版本保持0.4.45。
开始时358个应用源码文件与当前本地运行源逐项相同。主工作区已有修改保留。

### 1. 信号契约先红后绿

**完成**：新旧参数、原始价和日期口径、止损／兜底阶段均通过；实时调用链拒绝更早昨收。

文件：`backend/tests/test_morning_liquidation_returns.py`、
`backend/app/strategy/morning_liquidation.py`。

- [ ] 加入新定义`params={"time":"10:30:00","fallback_time":"10:45:00","rise_pct":6,"fall_pct":2}`。
- [ ] 断言raw_close=10.6、昨收10时精确触发上涨；9.8精确触发下跌；高低价扩大但现价10不触发。
- [ ] 断言新鲜实时报价、未来/陈旧报价排除、持仓日期/T+1、10:30止损及10:45独立兜底。
- [ ] 运行`backend/.venv/bin/python -m pytest tests/test_morning_liquidation_returns.py -q`，保存预期失败。
- [ ] 新参数使用有限正数0<阈值≤100及兜底晚于止损校验；混合振幅与涨跌参数明确拒绝。
- [ ] 新信号使用当前原始分钟价/有效实时价相对原始昨收；旧amplitude_pct定义维持原计算。
- [ ] 同命令转绿，连同`tests/test_morning_liquidation.py`复核旧输入兼容。

### 2. 连续资金真实分片窗口

**完成**：真实后续分钟续卖、兜底容量、失败余股／现金、禁止晚于兜底再入场和累计费用通过。

文件：`backend/tests/backtest/test_morning_returns_backtest.py`、
`backend/app/backtest/morning_backtest.py`。

- [ ] 构造9:31至10:45有效分钟，10:30及10:31各容量100股，10:32容量200股，400股应在10:32完成。
- [ ] 构造仅10:45容量足够的成交，断言兜底完成及真实分片累计费用。
- [ ] 构造10:45容量仍不足，断言余股和现金保留且不执行后续买入；旧单截止错误行为保持。
- [ ] 运行`backend/.venv/bin/python -m pytest tests/backtest/test_morning_returns_backtest.py -q`，记录失败。
- [ ] 最早触发仍在止损时间前定位，撮合上界取fallback_time或旧time；最终余股检查同阶段移至兜底。
- [ ] 兜底阶段只针对未清数量记录信号；不取消真实容量、单次滑点或累计费用；复牌例外保持。
- [ ] 同命令转绿，再跑`tests/backtest/test_morning_backtest.py`。

### 3. 监控和信号回放

**完成**：提前触发、止损再触发和兜底独立去重；回放展示三个阶段，实际成交只记真实分片。

文件：`backend/app/strategy/monitor.py`、
`backend/app/services/morning_liquidation_replay.py`、对应新增测试。

- [ ] 同证券止损通知后，兜底阶段应再出现一次；同阶段重复扫描不重复；买入批次保持。
- [ ] 回放保留早期涨跌触发及独立兜底，不让日终观察的兜底覆盖历史最早触发。
- [ ] 涨跌参考日取精确前交易日；缺前日价格不可用更早日替代。
- [ ] 运行新增回归并记录失败，按现有信号键及服务输出结构最小修复后转绿。

### 4. 表单、默认值与摘要

**完成**：四项可独立设置；涨8%／跌2%及自定秒级时间的实际保存／重开通过，已恢复默认值。

文件：`frontend/src/lib/api.ts`、`frontend/src/lib/customSignals.ts`、
`frontend/src/components/signals/CustomSignalDialog.tsx`、已有信号选择摘要与相关测试。

- [ ] 新建断言止损10:30、兜底10:45、涨6/跌6；分别保存上涨8、下跌2和两个自定时间后重新打开保持。
- [ ] 断言非法时间关系/涨跌阈值被拒绝，旧振幅信号编辑转换不把amplitude_pct继续提交。
- [ ] 运行`pnpm test src/components/signals/CustomSignalDialog.test.tsx src/lib/customSignals.test.ts`并保存失败。
- [ ] 使用既有time键兼容，新增可选fallback_time/rise_pct/fall_pct；摘要区分旧振幅与新涨跌配置。
- [ ] 同测试转绿；运行受影响SignalPicker/SignalTriggerActions测试、受影响ESLint及`pnpm build`。

### 5. 本地生效与签收

**完成**：最终源码／前端／Compute匹配，本地两库备份实际恢复验证、八角色、重启登录和参数保持。

- [ ] 基于当前本地运行源派生新目录，覆盖已验证的必要文件，受控.env逐字保持；Compute同步源码与静态清单。
- [ ] 用新Compute实际运行合成信号/资金分片回归及清单验证；不生成新的真实资金收益结论。
- [ ] 确认任务空闲，备份双库并恢复验证；更新匹配的API/Platform/Worker/Compute/前端运行指针。
- [ ] 按租户revision栅栏只更新目标早盘信号到time原值、fallback_time10:45、rise_pct6、fall_pct6。
- [ ] 实际登录、保存/读取、重启保持、桌面/手机表单和信号选择器均验证；其他信号、策略与账本签名保持。
- [ ] 同步六类生命周期文档、根工作区签收报告及验证JSON；最终列明参数、测试和本地/线上状态。

## 执行验收

- [x] 后端229项、SQL51项、前端25项、Compute127项，lint无新增发现及前端构建通过。
- [x] 新默认止损10:30、兜底10:45、涨幅6%、跌幅6%，按现价相对原始昨收；目标信号已生效。
- [x] 真实桌面1440／手机390、非法时间拦截、保存重开、选择器摘要、刷新及重启持久化通过。
- [x] 六类生命周期文档、API和版本基线、[签收报告](../../../reports/morning-liquidation-dual-time-20261102.md)
  及[验证JSON](../../../reports/morning-liquidation-dual-time-20261102/verification.json)齐备。
- [x] 主工作区已有改动、用户其余配置及账本保持；未新增行情收益回测、提交、推送或ECS部署。

上方分组内的小项保留原计划记录；完成状态以分组完成说明和本节实际验收为准。
