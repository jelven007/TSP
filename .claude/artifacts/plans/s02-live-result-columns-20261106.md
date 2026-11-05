# S02 Live Result Columns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现用户六项列展示、实时刷新、列表配置与紧凑布局要求。

**Architecture:** 共享三页列模型复用已有ColumnConfig及账户存储；当前页批量读取已有共享行情缓存。
稳定页签与绑定record_id的持仓草稿分开，原选股、成交和历史归档不修改。

**Tech Stack:** React、TypeScript、TanStack Query、FastAPI、Polars、Vitest、pytest、Vite、Playwright。

用户明确指示实施六项需求；当前会话执行，不重复请求设计批准。不提交或推送Git。

### Task 1：先证明回归要求

**Files:** `frontend/src/components/screener/StrategyDailyResults.live.test.tsx`、
`frontend/src/pages/Screener.test.tsx`、
`frontend/src/components/ListColumnCustomizer.loading.test.tsx`、
`backend/tests/test_screener_live_snapshot.py`。

- [x] 保存目标文件已有修改的副本；完整读取贡献与二开指南。
- [x] 实际线上查看原卖页列和列表配置按钮；新浏览器可正常打开，无错误及失败资源。
- [x] 写列测试：卖/选日期列默认隐藏；新增实时五列位置正确，买整数及千分位与建议手数正确。
- [x] 写更新测试：历史数据与现价无混用；当前页每秒查询自己的symbol集合；
  切页后停止旧范围，迟到响应不能污染；没有行情不使用旧价。
- [x] 写Screener测试：用户选择买/选后更新record_id，选中页仍不变；换策略/日期才初始化。
- [x] 写加载失败测试：动态模块失败不升级路由错误，局部反馈可关闭/重试。
- [x] 写后端真实输入测试：只读当前日缓存、按请求证券过滤、raw_close及单位和信号字段保留；
  旧无参数市场快照兼容，无当日缓存/非法范围明确处理。
- [x] 运行上述测试并记录预期失败。

### Task 2：最小数据与状态实现

**Files:** `backend/app/api/screener.py`、
`frontend/src/lib/api.ts`、`frontend/src/lib/queryKeys.ts`、
`frontend/src/lib/strategy-result-columns.ts`、
`frontend/src/lib/storage.ts`、必要时`frontend/src/lib/accountSession.ts`、
`frontend/src/components/screener/StrategyDailyResults.tsx`、
`frontend/src/pages/Screener.tsx`、
`frontend/src/components/ListColumnCustomizer.tsx`。

- [x] 扩展既有市场快照可选实时模式，仅读共享enriched当前缓存，固定字段和有界证券范围。
- [x] 前端集中API/查询键增加可选实时报价参数；旧调用兼容。
- [x] 三页默认列沿原静态顺序追加，日期列保留数据但默认隐藏；
  新行情字段使用独立键，不能覆盖历史close/change_pct/score。
- [x] 建立共享列宽与三页账户偏好，配置弹窗绑定当前页。
- [x] 当前页1000ms批量轮询；旧范围停用，过期日期/缺值保持未知，现价仅用raw_close。
- [x] 买页`volume`格式为0位小数，`amount`为0位小数且千分位；
  建议手数为有限正价格下的`floor(100000/(price*100))`。
- [x] 稳定页签键不含record_id；持仓草稿仍按原record_id防护。
- [x] 懒加载失败增加局部反馈与重试，原成功配置交互保持。

### Task 3：验证、文档与运行交付

- [x] 运行相关三页、Screener、列表配置、API测试；修复回归后执行ESLint/Ruff差异检查。
- [x] 执行生产构建；用构建后的真实组件在1440/390检查三页、每秒刷新与配置弹窗。
- [x] 同步需求、架构、用户、二开、测试、运维文档；明确历史与实时报价的日期不同。
- [x] 从实际运行源准备隔离候选，保留已部署60秒及历史读取修复；
  不把工作区其他修改打包。按会话授权完成必要线上安装并核对运行事实。
- [x] 记录原策略/信号/归档/研究与账户数据保留、实际生效状态和未验证边界。
- [x] 运行`make check`和最终差异检查；最终回复逐项交付六项变化、验证及部署状态。

最终证据见`reports/engineering/s02-live-results-20261106.md`。
实际源码/候选分别60前端、26后端通过，线上0.4.49部署/重启及双库恢复、
配置/归档/账本保持通过；真实1440/390三页验收通过。无新回测或Git提交。
