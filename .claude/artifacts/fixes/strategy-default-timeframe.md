# Bug: 分钟策略请求缺省周期被错误设为日线

> Status: FIXED
> Mode: default
> Severity: functional
> Last updated: 2026-11-01

## Symptom and expected

历史用户报告分钟策略缺省请求被设为不支持的`1d`（策略只支持`1m`）。
当前对应策略标识已统一为s02；以下为此前修复记录，测试计数属于原验证。
单策略运行未传周期时应按策略元数据采用1m，并以同一周期构建上下文和归档结果。
显式指定不支持的1d仍应拒绝。

## Reproduction

对本地`/api/screener/run_preset`提交分钟策略及2026-10-30、省略timeframe，
实际返回400和用户所报错误。
新增`backend/tests/test_strategy_default_timeframe.py`覆盖两个入口、
分钟/日线策略、缺省/null及显式非法周期。原实现连续三次10失败/4通过。

## Hypotheses and root cause

H1：请求模型默认1d而策略仅支持1m，置信高、优先1；真实请求及回归确认。
H2：策略元数据误报周期，置信中、优先2；元数据为1m且显式分钟正常，排除。
失败链为请求模型填1d→上下文构建→TenantStrategyEngine校验。
最早错误值来自两个单策略请求模型，不修改策略执行校验以掩盖问题。

## Fix

两个请求模型周期缺省改为None；路由使用显式周期或策略声明首周期，
同一解析值用于行情上下文、持久身份及日线缓存分支。
不修改交易规则、阈值、数据库schema或批量执行语义。

## Verification

- V-1：修复后14条新回归通过，含实际归档周期。
- V-2：临时撤去元数据解析后8失败/6通过，恢复修复后通过。
- V-3：新回归、历史接口、保存参数、动量日结果合计50通过。
- V-4：API既有54项Ruff提示，本次新增0项；新测试Ruff通过。
- V-5：本地API及匹配Compute采用最小补丁，保留凭据和应用0.4.39。
  原验证中的分钟策略在两个真实单跑入口省略周期均成功并归档1m，
  显式1d仍拒绝，原30个研究任务与每日条数保持一致。
- V-6：Screener及每日三Tab前端9项通过；真实浏览器加入两策略池、
  点击卡片、切换卖/选/买成功，未捕获页面运行异常。

## Pattern analysis

检索run_preset、strategies/run及前端单跑调用：
Screener卡片和设置保存已显式传入周期；旧strategyRun辅助调用不传周期，
由本次接口缺省规则覆盖。批量run-all默认日线为现有筛选语义，不扩大改动。

## Follow-ups

`test_screener_builtin_params.py`两个旧mock不接受generation/generations，
已在未修改工作区同样复现；不计入本次新增失败，不夹带修复。
API整文件既有Ruff提示未清理；本次仅验证新行没有新增提示。
文档检查发现此前Excel文件已不在工作区，其既有链接失效；
本次不恢复或重生成回测交付，不将文档全量检查称为通过。
