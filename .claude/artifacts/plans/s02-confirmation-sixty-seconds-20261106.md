# S02 Confirmation Sixty Seconds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 只把尾盘确认延迟从90秒改成60秒，同步页面内容与保存，并保留原行情截止和所有其他参数。

**Architecture:** 复用当前`late_session_strength`纯计算和`custom-signals`保存入口。
共享引擎继续分别消费确认时钟与启动分钟标签。旧定义读取和历史归档保持原值。

**Tech Stack:** Python、FastAPI、Polars、React、TypeScript、Vitest、pytest、Vite。

用户已批准设计及修改范围，采用当前会话内执行，不重复请求确认。不提交或推送Git，不手动递增版本。

### Task 1: 先固定回归要求

**Files:**
- Test: `backend/tests/test_late_session_sixty_seconds.py`
- Test: `backend/tests/test_late_session_start_delay.py`
- Test: `backend/tests/test_late_session_ninety_seconds.py`
- Test: `frontend/src/lib/customSignals.test.ts`
- Test: `frontend/src/components/signals/CustomSignalDialog.test.tsx`
- Test: `frontend/src/components/signals/CustomSignalDialog.time.test.tsx`

- [x] 保存本次涉及文件的修改前副本到临时目录，保留工作区已有修改。
- [x] 新增后端真实计算回归：`evaluation_time(sig, DAY)==TARGET+timedelta(seconds=60)`、
  `bar_time(sig, DAY)==TARGET`；59秒无信号，60秒命中；60/90秒完整行情下同名单、同参考价格。
- [x] 新增或调整API回归：缺延迟的POST保存1；GET历史缺字段不补值；显式1.5历史读取保持；
  60秒精确启动14:30:20保存成功且确认14:31:20，分钟截止14:30。
- [x] 调整页面回归：确认14:31:00、60秒标签、保存延迟1、旧1.5转换后其他字段逐项相同；
  选择14:30:20后确认14:31:20；启动范围和全部数值输入保持。
- [x] 运行定向pytest和Vitest，记录因当前仍为90秒导致的预期失败。

### Task 2: 最小实现

**Files:**
- Modify: `backend/app/strategy/late_session_strength.py`
- Modify: `backend/app/api/signals.py`
- Modify: `frontend/src/lib/customSignals.ts`
- Modify: `frontend/src/components/signals/CustomSignalDialog.tsx`

- [x] 将`DEFAULT_PARAMS["evaluation_delay_minutes"]`和POST缺省保存值从1.5改为1。
- [x] 保留`delay in (0, 1, 1.5)`及历史缺省0；让60秒定义保留既有秒级启动能力。
  只调整相应秒数校验，不改变`bar_time`、`evaluate`行情截止、广度或MA5公式。
- [x] 将`LATE_SESSION_DEFAULTS.evaluation_delay_minutes`及编辑草稿固定延迟改为1。
- [x] 将页面90秒说明与旧定义保存提示改为60秒；摘要仍按原存储值计算旧定义确认时间。
- [x] 重跑受影响后端模块和前端测试，执行变更文件Ruff/ESLint及生产构建。

### Task 3: 页面、配置边界和交付验证

**Files:**
- Modify: `CONTRIBUTING.md`
- Modify: `操作说明书.md`
- Modify: `docs/requirements.md`
- Modify: `docs/architecture.md`
- Modify: `docs/secondary-development.md`
- Modify: `docs/testing.md`
- Modify: `docs/operations.md`
- Create: `reports/engineering/s02-confirmation-sixty-seconds-20261106/report.md`

- [x] 同步当前需求、架构、用户、测试、运维与二开说明，旧冻结90秒验收记录按历史保留。
- [x] 使用真实组件在桌面和窄屏验证60秒文案、14:30截止、保存及重新打开。
- [x] 对本次目标活动定义的生效操作，只改`params.evaluation_delay_minutes`，逐项比较其余配置；
  若涉及运行安装，依据会话已有授权及固定运行源构建，不混入工作区其他变更。
- [x] 检查修改前后差异、版本保持、文档检查及现有历史归档保持；记录实际安装状态。
- [x] 最终回复说明选股、买、卖、仓位和关键参数，其中仅确认延迟变化；引用实际验证结果。
