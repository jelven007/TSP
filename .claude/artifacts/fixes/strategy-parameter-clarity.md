# 策略参数作用域与保存修正

日期：2026-11-01；本地源码变更，应用版本仍为0.4.39。
来源：[设计及AC-1～AC-6](../designs/strategy-parameter-clarity.md)；
对应FR-STRATEGY-06/08、NFR-06，L3局部配置/API/页面修正。

## 原因与改动

- 页头默认值是账户在当前浏览器的新建模板，重命名并与当前策略设置分开。
- 共享数值过滤编辑器及单位，补齐总市值、上市天数，目标/ST独立。
- 文件读取保留null：清空取消原限制，缺省继承；校验有效范围后才保存。
- PATCH按字段合并basic_filter，批量仅发送选中策略的数值过滤及启用状态；
  确认冻结输入和目标，部分失败保留成功结果并显示具体名单，不隐式保存模板。
- 旧格式切换数值开关时先固定原有效板块/ST范围，避免把旧绑定行为带入新编辑器；
  已有目标格式继续原样保留。
- 详情完整合并退出覆盖并提供可选原始可编辑快照；恢复只修改草稿，可关闭取消。
  异步旧详情不得覆盖新策略；通用退出输入百分数，专用分钟参数与单位契约保留。
- API保存校验退出组合，前端保存刷新策略清单与已有相关查询，历史不自动重算。

## Verification / Regression test

测试先复现：9项后台回归失败（null、范围丢失、非法保存、退出回显），
8项前端回归失败（入口/字段、草稿恢复、旧请求、选择批量）；另复现
移动止盈非法组合未拒绝和模板缺目标独立声明，再进行对应修正。

在独立worktree的backend目录执行，避免共享venv的editable安装导入用户根目录旧源码：

```bash
TICKFLOW_ENV_FILE=/dev/null MULTITENANCY_ENABLED=false SERVICE_ROLE=standalone \
  ./.venv/bin/python -m pytest \
  tests/test_strategy_saved_params.py tests/test_strategy_detail_signals.py \
  tests/test_strategy_targets.py tests/test_strategy_overlay.py tests/test_strategy_scoring.py \
  tests/test_composite_override.py tests/test_strategy_cache.py tests/test_s01_strategy.py \
  tests/test_minute_strategy.py tests/test_openapi_contract.py \
  tests/test_strategy_build_stream.py tests/test_ai_strategy_meta_normalize.py \
  tests/test_composite_strategy_api.py -q
```

结果：175通过；13项既有datetime.utcnow弃用提示。
覆盖清空保存/缓存重读/实际运行、字段PATCH保留范围与风险、默认快照、
非法基础范围/有效继承范围/退出组合、目标/评分/叠加兼容和分钟接口。

2026-11-01清理后，命令中的退役专属测试已移除；175项为原验证计数，
当前清理验证另见`reports/strategy-removal-20261101.md`。

前端定向Vitest六文件26通过：
DefaultStrategyParamsDialog、StrategySettingsClarity、StrategyTargets、
StrategyParameterUnits、strategyParams、strategyOverrides。
本次改动的11个前端文件ESLint通过；`pnpm build`通过（TypeScript及Vite）。

隔离浏览器使用真实新低反转/s01定义，API写入仅测试内存：
浏览器插件556px真实点击验证非法拦截、清空保存重开、选择批量/锁定、
恢复草稿取消不写及分钟专用编辑器；无运行错误。
插件没有viewport调整工具，补用独立Playwright/本机Chrome验证
1440/390px×浅/深色×模板/普通设置/分钟设置12个布局，
无横向溢出，模板保存和止盈设置保存重开一致，无pageerror。

合并前核对发现8个目标文件有并行更新，已逐项读取增量；
以任务补丁合入并保留策略删除/历史级联更新，`git apply --check`通过。
用户工作区复验上述14个后台文件，加策略删除/代码保存兼容测试，共184通过；
前端加并行的StrategyDelete回归，共7文件28通过；11个改动文件lint与构建通过。
`check_repository.py`（87个必需路径）、`check_version.py`（0.4.39）、
`check_docs.py`（37份Markdown、8个需求模块）及`git diff --check`通过。
独立worktree未携带会话报告/运行配置，其旧链接检查不用于最终文档结论。
六类生命周期、二次开发/API和用户操作说明已同步。

## 边界与恢复

AC-1～AC-6按上述组件、配置及浏览器证据验证。没有执行完整系统全量测试、
真实租户SQL业务配置写入、实际账户参数修改、正式行情回测或部署。
选股、买卖、仓位及费用规则无变动；历史回测不代表本次新运行。
版本文件未递增，未提交、推送或替换正在运行的服务。

临时依赖/预览fixture不进入用户工作区；只回写相对会话前基线的任务增量，
回写前对原文件哈希核对并逐项合并非重叠的并行更新。上线须前端/API/Worker/Compute配套，
回退旧读取代码前检查null取消限制的语义，保留用户策略覆盖及浏览器模板备份。
