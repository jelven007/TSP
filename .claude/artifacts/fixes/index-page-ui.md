# Bug: 指数页修正终态不收起且K线控制偏离个股详情

> Status: FIXED
> Mode: default
> Severity: functional
> Last updated: 2026-10-28

## Symptom

- 数据修正任务完成后一直显示。
- K线数量与按钮放在同一边框内，日期另起一排，最大根数未使用图表实际可用根数状态。

## Expected

终态保留5秒后自动收起；K线控制复用个股详情的默认60根、实际上限、
独立数量文本和同行日期布局。

## Reproduction

`frontend/src/pages/Indices.test.tsx`新增两个用例。修复前连续3次均为
2 failed / 3 passed：按钮组文本含`3 / 3 根`且日期不在同一工具组，
终态没有注册5秒清理。

## Hypotheses & diagnosis

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| 后端任务终态未返回 | 排除 | 测试及页面都收到`succeeded`与100% |
| 指数页遗漏终态清理 | 确认 | effect仅失效查询，没有`setJobId(null)`定时器 |
| 图表未提供实际根数 | 排除 | `StockDailyKChart`已有available/visible回调，个股详情已使用 |
| 指数页自行拼装工具栏 | 确认 | 数量直接拼在按钮容器，日历位于独立下一行 |

## Root cause

指数扩展时复用了图表数据组件，但没有复用个股详情的调用方状态与工具栏布局；
任务终态逻辑也只复制了查询失效部分，漏掉数据总览的5秒清理。

## Fix

- `Indices.tsx`保存`availableBars/currentBars`，缩放按实际上限计算，周期切换重置。
- 数量移出按钮边框，起止日历放入同一个不可拆分日期组。
- 终态完成查询失效后注册可清理的5秒定时器，且只清除对应任务ID。

## Verification

- V-1：修复前连续3次2 failed / 3 passed。
- V-2：修复后指数页5项通过。
- V-3：前端全量84文件、453测试通过，lint/build通过。
- V-4：Playwright生产构建验证1600×1000与390×844，真实ECharts非空，
  60→90→130封顶、日历同行、任务自动消失、无溢出/控制台错误。

## Regression test

- `uses actual available bars and keeps the count outside the buttons with both calendars inline`
- `automatically hides the completed repair task after five seconds`

## Pattern analysis

搜索`ActiveJobCard`与`onAvailableBarsChange`：数据总览和个股详情分别已有正确模式；
指数页是唯一缺少对应调用方状态与终态清理的页面，本次直接对齐，未抽象新组件。
