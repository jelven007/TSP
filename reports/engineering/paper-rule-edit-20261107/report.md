> 脱敏历史正文副本；执行日期、版本和测试计数沿原记录。原始附件及用户文件保持，本次统一发布的验证与部署另行签收。

# 模拟账户自动跟单规则修改验收

本任务需求：模拟页面账户中的自动跟单规则支持修改。
源码基线4453ecc4，任务分支work/paper-rule-edit-20261107；
开发继承共享工作区当前源码，版本号没有递增。

2026-11-08后续按用户授权已安装本地和线上0.4.52，快照`paper-rule-edit-20261108`。
两端实际组合候选各204项后端/13项前端及双库恢复、真实页面和完整重启复验通过；
后续现场身份及配置/账本保留见
[双端安装验收](../../operations/paper-rule-edit-20261108/report.md)。
下文“未安装”保留2026-11-07源码验收阶段的历史含义。

## 用户可见行为

展开账户，在自动跟单规则行点击“修改”，以共享720px弹窗编辑原参数。
支持名称、策略/监控来源、方向、金额/权益比例、订单类型、冷却天数、
最大持仓数、候选顺序、单股/总仓上限及策略风控。
保存成功刷新规则、账户对比、订单与概览；取消丢弃草稿，错误保留输入可重试。
保存期间禁用输入、重复提交、关闭按钮、Escape与遮罩关闭。
修改停用规则保持停用，不增加规则、不改规则编号与创建时间。

选股算法、本次之外的买卖撮合、账户费用与资金保持原规则。自动方向仍按策略
买入信号/入池买入，卖出信号/出池全平；T+1、待卖占用、冷却、仓位限制继续执行。
调整跟随目标、方向或订单类型，沿原save_auto_rule逻辑取消旧退出待办及关联待卖单；
只改名称/买入金额保留原全平待办；关闭策略风控沿原机制取消风控待办。
已有成交、持仓与入场冻结保护位不重写，本任务未运行收益回测。

## 实现与兼容

L3核心局部接线，未引入新插件接口或SQL迁移。
`PUT /api/paper/auto_rules/{rule_id}?account=...`查当前租户/账户已有规则，
复用AutoRuleModel与PaperAutoRuleResponse；省略的可选配置保留原值。
服务端保护id/created_at/enabled，启停仍走独立接口。
不存在返回404，配置错误返回400/422；文件模式用原锁和原子规则写入，
Full模式复用paper_operation写事务及RLS，SQL失败整体回滚。
Tier A新增PUT端点，开放接口范围只增加该项，旧创建/启停/删除保持。

## 实际验证

| 验证 | 实际结果 |
| --- | --- |
| 旧实现反证 | 8项后端更新用例失败：缺领域函数/HTTP405；5项编辑UI用例失败：无修改按钮 |
| 弹窗反证 | 内联版本2项失败：没有dialog及保存期间关闭保护 |
| 后端 | 181项通过，包含paper自动/风险/T+1/权益/撮合/API/对比/周期仲裁/开放契约及17项真实PostgreSQL |
| 前端 | Paper 8项、Modal 5项，共13项通过 |
| 构建/lint | pnpm build、定向ESLint、Ruff及任务diff空白检查通过 |
| 浏览器 | Chromium 1440×1000浅/深色、390×844：回填、真实PUT保存、刷新持久化、取消、503失败重试、账户切换、移动端校验及保存 |

后端命令：
`pytest tests/test_full_paper.py tests/test_paper_auto.py tests/test_paper_api.py
tests/test_paper_trading.py tests/test_paper_arena.py tests/test_paper_risk_policy.py
tests/test_paper_t1_available_at_order.py tests/test_paper_corporate_actions.py
tests/test_paper_settle_price_time.py tests/test_strategy_portfolio_arbitration.py
tests/test_openapi_contract.py -q`。Full测试提供独立临时PostgreSQL DSN，
不使用本地应用库或生产库。
前端命令：`pnpm test src/pages/Paper.test.tsx src/components/Modal.test.tsx`、
`pnpm exec eslint src/pages/Paper.tsx src/pages/Paper.test.tsx`、`pnpm build`。

Browser工具能读页面但视口0×0，点击报“coordinates outside viewport”。
改用临时Playwright环境及已缓存Chromium，不改项目依赖。
隔离服务使用实际Paper页面和paper路由，目录/行情状态只用验收夹具；
503由测试注入，其余保存使用真实新接口。页面错误与意外控制台错误为零。
移动弹窗x=16、宽358，位于390px视口内，正文滚动、底部按钮可见。
构建有Browserslist旧缓存与既有大chunk提示；后端有既有Polars sortedness提示，
这些提示没有导致测试或构建失败。

页面证据：原规则列表、修改弹窗、深色弹窗、390px编辑（受控原始附件）；
结构化结果：browser-result.json（受控原始附件）。

## 生命周期与运行状态

需求FR-PAPER-07、设计/计划、架构、二开契约、测试、用户操作、运营、运维、
工程映射同步。本次完成源码和隔离联调，未提交、推送或安装到本地/线上运行环境。
回写共享工作区后再次通过181项后端、13项前端、构建、Ruff、ESLint、
`scripts/check_docs.py`（44份Markdown、8模块、保留需求基线）及全仓`git diff --check`。
本任务九个源码/测试文件与独立工作目录逐字节一致；运维文档中并行更新的
S02已安装状态保留。临时API、Vite和PostgreSQL验收服务已关闭。
运行版本是否具备入口须以实际安装身份核对，不能把临时验收当作生产验收。
后续安装须同时带入前端、API与自动执行领域源码，保留用户保存配置和交易历史；
回退代码不会自动撤销已保存参数，也不应重放历史委托。

范围限制：未测试真实监控事件持续写入与运行版本切换；
未做全仓质量背书或历史收益回测。现有专用分钟容量策略自动委托限制仍保持。
