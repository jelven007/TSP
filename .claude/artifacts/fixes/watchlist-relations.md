# 自选关系与清单操作验证

日期：2026-10-27。契约：[设计记录](../designs/watchlist-relations.md)。
工作区基线`4da5f30c` / 0.4.32；保留已有CSV顺序、K线及研究修改，
初次源码验收未提交、推送、部署或加版本。随后用户授权的本地更新及快速刷新修复见
[真实运行记录](../../../reports/local-watchlist-update-20261027/verification.md)；
运行与源码类别以[版本基线](../../../docs/version-baseline.md)为准。

## 需求与实现

- 全部每证券一行：清单提供顺序及关系，行情按symbol合并；名称标签同时展示多个分组和策略，
  表格、个股卡片及分组卡片共享展示。
- 恢复默认排序：清除共享排序hook的字段排序，按已保存的清单顺序展示；不清分组或筛选，
  不重写历史顺序。分组卡片自身涨跌幅榜单沿原定义。
- 导出全部：点击读取当前空间完整清单，首现去重；UTF-8 BOM、CRLF、股票代码单列，
  保留前导零和交易所后缀。与分组导出复用同一下载函数。空清单/失败不下载，
  挂载和账户范围保护忽略迟到结果。
- 股票多策略：`strategy_ids`稳定ID列表，经`PUT /api/watchlist/{symbol}/strategies`保存，
  当前租户公开可见策略校验；名称取全周期独立目录。空数组解除关联；
  关联不启动监控或更改策略范围。
- 文件缺列读作空数组、读不写盘、首次写回备份且不覆盖原bak；SQL复用租户documents，
  批量读取、同事务更新revision及删除清理；重导入/移组/置顶保留；
  完整文件转SQL业务导入同步转换并对账。无需新增schema迁移。

## 自动验证

开发顺序有实际RED记录：初始4个后端测试失败（字段及set_strategies缺失、路由404），
排序reset测试失败；关系/导出组件未实现导致前端导入失败；
文件转SQL迁移测试先失败（缺少关联文档），实施后通过。

已完成：

- `MULTITENANCY_ENABLED=false uv run --frozen --no-sync pytest tests/test_watchlist_relations.py tests/test_watchlist_groups.py tests/test_watchlist_csv.py tests/test_watchlist_enriched_join.py tests/test_watchlist_ocr.py tests/test_watchlist_upload_size_limit.py tests/test_strategy_targets.py tests/test_market_scope.py tests/test_openapi_contract.py -q`：116通过。
- 独立临时PostgreSQL双库/非绕过RLS角色：
  `pytest tests/test_full_strategy_targets.py tests/test_full_legacy_import.py tests/test_full_identity.py tests/test_scheduled_full_backup.py -q`：56通过。
  DSN仅从受控临时配置读入进程环境，未输出；测试库及角色随fixture清理。
  含真实TenantStrategyEngine的所属/外国/草稿策略校验、未登录拒绝、跨租户关系、
  Cell删除/清空清理、备份及完整导入对账。
- 最终`pnpm test`：80文件、390项通过（含迟到策略保存保护）。
- 最终`pnpm build`与`pnpm lint`：均通过；保留已有ECharts大包及React Router升级提示。
- Ruff全文件存在58个历史诊断；临时脚本`tspp-watchlist-ruff-20261027.py`按git新增行及新文件检查，
  本轮变更行0诊断，退出0；不声称历史文件全量Ruff已清零。
- `make check`：87必需路径、31文档及8需求模块通过，版本0.4.32一致，`git diff --check`通过。

## 浏览器验证

`http://127.0.0.1:3071/watchlist`；Chrome，通过Playwright调用现有浏览器安装。
Browser plugin not available。生产构建的独立preview，全业务API及外部版本查询使用隔离fixture，
未写真实用户自选。1440×1000及390×844：

| 检查 | 结果 |
| --- | --- |
| 页URL、标题及“自选”主体 | PASS |
| 非空白/框架错误覆盖层 | PASS |
| 同证券行显示两个分组和两个策略 | PASS |
| 故意逆序行情仍按清单顺序 | PASS |
| 字段排序→恢复默认，当前分组保留 | PASS |
| 选中具体组时下载全部，实际文件BOM/CRLF及代码单列 | PASS |
| 双策略多选保存→刷新保留，解除全部→保存清除 | PASS |
| 窄屏Modal边界、卡片关系和编辑入口、页面无横向溢出 | PASS |
| 控制台和页面运行错误 | PASS，0错误 |

脚本及截图位于受控临时目录中的`tspp-watchlist-relations-qa-20261027.cjs`和
`tspp-watchlist-relations-qa-20261027/`，最终构建后再次完整执行通过。
证据：`result.json`、实际`全部自选股.csv`、`relations-desktop.png`、
`strategy-desktop.png`、`strategy-mobile.png`和`cards-mobile.png`。
窄屏视觉核对后将关系标签置于代码/名称下，避免与右上角分组按钮重叠；最终截图已复核。
SSE拦截连接闭合导致预览底部断线提示，属于fixture，不认证真实服务连接。
此前QA脚本使用自定义表头名称并误假设刷新保留分组，已改用实际“现价”及刷新回全部的现有行为。
外部GitHub版本检查曾返回403，隔离fixture补齐后无该错误；未改应用网络策略。

## 交付边界

初次是工作区实现及隔离验证；随后本机3021/3028已更新自选补丁并真实验证，
ECS没有加载本轮未提交改动。最新运行状态见上述本地更新记录。
仅在Chrome及所列viewport实测；浏览器API为fixture，实际持久化/隔离另由真实PostgreSQL测试证明。
没有清理真实自选、行情或凭据，也没有替换管理员或SMTP等用户配置。
