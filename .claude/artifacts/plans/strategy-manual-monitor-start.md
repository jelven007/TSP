# 策略手动监控启动 Implementation Plan

**Goal:** 选择策略只读取保存结果，开启监控时以最近可用交易日执行。

**Architecture:** 在已有页面事件中分离选择与执行。监控保存成功后调用现有
单策略mutation；共享日期源、监控CRUD、缓存键及迟到响应隔离保持。

**Tech Stack:** React18、TypeScript、TanStack Query、Vitest、Vite。

执行沿本次用户明确要求在当前会话完成；本地前端副本在备份后更新，
不另行提交、发布或升级线上。

## 1. 回归测试先失败

修改`frontend/src/pages/Screener.test.tsx`，保留真实卡片和查询状态机，
在API边界模拟规则列表／保存／删除及执行响应。覆盖选择、加入池、新建、
已有监控、默认日期、保存失败／等待、重复点击、取消和迟到执行。
修改`StrategyCard.test.tsx`核对“待计算”提示指向监控按钮。

关键断言：

```ts
await act(async () => button('分钟样例').click())
expect(run).not.toHaveBeenCalled()
await act(async () => host.querySelector<HTMLButtonElement>(
  'button[title="开启策略监控"]')!.click())
expect(run).toHaveBeenCalledWith('minute', undefined, '2026-10-23', undefined, 'stock', '1m')
```

运行`pnpm test src/pages/Screener.test.tsx src/components/screener/StrategyCard.test.tsx`，
确认因为选择自动执行／监控不执行／错误提示而失败，排除测试配置错误。

## 2. 最小实现

修改`Screener.tsx`：选择事件不调用mutation；删除S02页面补算effect与新增池／策略
后的自动执行；monitor事件捕获最新日期、更新视图，保存成功后单策略执行。
以同步Set门闩及UI pending状态阻止重复操作；失败toast和finally释放。
修改`StrategyCard.tsx`：独立监控pending禁用、待计算提示改为开启监控。

```ts
const date = dataStatus.data?.enriched?.latest_date
if (!existingRuleId && !date) { toast('交易日数据尚未就绪，请稍后重试', 'error'); return }
await api.monitorRuleSave(rule)
run.mutate({ id: strategyId, date, timeframe, asset: assetType })
```

重复定向测试直至通过，再运行相关策略组件测试、`pnpm lint`和`pnpm build`。

## 3. 文档与浏览器

同步`docs/{requirements,architecture,testing,release-operations,operations,secondary-development}.md`
及`操作说明书.md`，说明选择、监控显式执行、最近本地可用交易日及现有手動重载边界。
隔离前端通过真实卡片验证选择零POST、开启规则后一次正确日期执行、日期显示及错误状态；
保留浏览器控制台／截图证据在工作区外。

运行`python3 scripts/check_docs.py --include-reports`、`git diff --check`和版本一致性检查。
将本次必要文件以修改前哈希守卫回写用户工作区，保留其余既有修改。
核对现有本地前端源码与修改前工作区一致，备份原dist及两处源码，
原子替换新构建并保留旧hash资源供已打开页面加载；验证实际返回新index、
所有服务PID和受控配置保持。

## 本次执行结果

选择／监控回归先在旧实现失败，修改后工作区30项定向测试通过；
最新并行修改合并后的前端全量525项／98文件、Lint和构建通过，
229份Markdown与版本检查通过。隔离浏览器在1440／390宽度和标准／详细卡片下
验证选择零执行、开启后的日期／周期、保存失败／重试、重复点击和取消。
实际本地管理员会话选择策略也确认零执行请求。

仅更新现有本地前端副本，原构建备份、已有进程及受控配置保持；
本次两处源码之外不覆盖另一个任务已更新的前端功能。版本保持0.4.41，
未提交、推送或升级线上；未新运行回测、未在真实账户写监控规则。
