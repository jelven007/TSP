# 普通信号编辑器两条失败测试修复

> Status: FIXED
> Mode: --quick
> Severity: functional
> Last updated: 2026-11-13

## 范围

维护 `frontend/src/components/signals/CustomSignalDialog.test.tsx` 的两条已有测试，
修复其直接被测组件 `CustomSignalDialog.tsx`。本轮新增用例0条，维护用例2条，
30个原断言经TypeScript语法树比对全部保持。
其他已有工作区改动保留；本轮未写入策略定义、账户配置或行情数据。

## 缺陷分析

1. 工作区组件在合并保存值后强制 `evaluation_delay_minutes=1.5`，
   缺少确认延迟秒数控件，保存和说明仍使用90秒，且启动截止固定为14:58。
   默认常量、用户当前要求和相邻测试均要求默认30秒、范围30～900秒、
   30秒整数倍及保留已保存的合法值。
2. 组件调用 `api.customSignalSave` 时未传 `createOnly`，
   新建预设又使用固定标识，可能通过更新接口覆盖已有同标识信号。
   接口封装及相邻创建测试要求新建保护，编辑保持原标识更新。

两条原测试连续三次各以相同断言失败，排除了偶发时序问题。
补齐测试的卸载、QueryClient清理与mock恢复后，两条测试仍以同样原因失败，
证明测试预期与隔离问题不是实现缺陷的替代解释。

## 修复

- [参数初始化与旧无延迟兼容](file:///Users/bytedance/Documents/TSP/frontend/src/components/signals/CustomSignalDialog.tsx#L62-L85)：保留已保存延迟，缺省及旧零延迟使用当前默认值。
- [保存校验及新建保护](file:///Users/bytedance/Documents/TSP/frontend/src/components/signals/CustomSignalDialog.tsx#L130-L149)：校验30秒倍数和收盘界限，并传入 `createOnly: !editing`。
- [新建预设标识](file:///Users/bytedance/Documents/TSP/frontend/src/components/signals/CustomSignalDialog.tsx#L196-L204)：未填写标识时生成独立合法标识，保留用户输入及编辑原标识。
- [秒数控件与说明](file:///Users/bytedance/Documents/TSP/frontend/src/components/signals/CustomSignalDialog.tsx#L289-L323)：默认、显示、保存换算和确认时间保持一致。
- [测试资源清理](file:///Users/bytedance/Documents/TSP/frontend/src/components/signals/CustomSignalDialog.test.tsx#L15-L21)：使用afterEach，即使断言失败也卸载并清理缓存、mock。

相关实现依据当前已验证的本地正式源码恢复，只移入本次缺失的逻辑；
本轮未重新部署或递增版本。

## 维护的用例

- `creates and edits the late-session entry preset with all configurable defaults`
  （测试文件第23行）：默认值、保存、已保存值重载、旧定义与启动时间边界。
- `saves the morning exit preset and reloads configurable times and returns`
  （测试文件第91行）：新建保护、双时间与涨跌幅参数、保存后重载。

未新增重复用例。相邻现有测试覆盖30～900秒、非法秒数、收盘边界、
多次新建不覆盖原定义、重名错误、用户自填标识、编辑、S01参数及秒级时间。

## 验证结果

| 检查 | 本次真实结果 |
| --- | --- |
| 原失败测试复现 | 连续3次均2条失败 |
| 补齐测试清理后的复现 | 2条相同失败 |
| 实现修复后的原测试 | 2/2通过 |
| 临时反转强制90秒及createOnly两处修复 | 2条重新失败 |
| 恢复后的全部信号模块测试 | 7个文件，29/29通过 |
| 两个修改文件的ESLint | 退出码0 |
| `pnpm build` | TypeScript与Vite构建通过 |
| `git diff --check`（修改文件） | 退出码0 |
| 原断言语法树比对 | 30/30保持 |

测试命令均通过外层超时执行：

```sh
perl -e 'alarm shift; exec @ARGV' 120 pnpm exec vitest run src/components/signals/CustomSignalDialog.test.tsx
perl -e 'alarm shift; exec @ARGV' 300 pnpm exec vitest run src/components/signals
pnpm exec eslint src/components/signals/CustomSignalDialog.tsx src/components/signals/CustomSignalDialog.test.tsx
pnpm build
```

临时反向验证仅改动两处本轮关键修复，随后恢复；未stash或重置其他工作。
React Router未来版本提示及现有图表分块超过500kB的构建提示不计为失败。
本轮未执行全仓测试或重新进行浏览器/部署验收，不把局部结果扩大为全仓验证。
用户未要求覆盖率门槛，项目相关约定也未设置该卡口，覆盖率阶段按技能规则跳过。
单测技能的Step1～Step7已顺序执行，utree flush已执行。

## 同类模式检查

在信号组件与直接API封装范围检索
`evaluation_delay_minutes: 1.5 as const|启动后90秒|customSignalSave\(`，
修复后两处匹配分别为禁止90秒文案的测试断言和已带createOnly的正确保存调用；
该范围内未发现需另行修改的同类实现。不扩大全仓审查。

原始父文件、逐次测试日志、精确任务补丁与文件哈希保存在本地受控维护目录
`/Users/bytedance/.local/share/tsp-dev/maintenance/signal-editor-tests-20261113`。
