# Bug: 自选CSV导入反序及管理分组重复导入入口

> Status: FIXED
> Mode: --quick
> Severity: functional
> Last updated: 2026-10-27
> 源码基线: 4da5f30c9b5165612d78b573c142612f3fe25395 / 0.4.32

## Symptom / Expected

CSV导入后的股票顺序与文件完全相反。期望所选证券按文件首现顺序去重并整体置顶，
重新勾选及重新加载保持该顺序。管理自选分组弹窗移除CSV文件选择、导入按钮与说明；
主页面批量导入、新建分组及分组导出继续可用。

## Reproduction

CSV代码依次为600036、515880，解析候选保持正序，批量保存后变成515880、600036。
文件模式回归两例、前端重新勾选两例及分组入口一例在修复前连续三次失败。
真实临时PostgreSQL两例也复现反序，覆盖only_new与既有跨组成员。

## Hypotheses & diagnosis

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| CSV解析或页面字段排序反转 | 排除 | 解析候选正序；批量API测试在不经过页面时已反序 |
| 顺序处理每项并置顶 | 确认 | 文件rows.insert(0)与SQL递减position都使末项排在最前 |
| 勾选Set的插入顺序改变提交顺序 | 确认 | 取消再勾选首项及补选已有证券的前端测试出现顺序偏移 |

## Root cause / Fix

反序首次发生于两种存储的add_batch。首现去重后倒序执行既有置顶写入，
最终保存与返回为输入正序，未涉及成员保持原序。前端从候选数组过滤勾选项。
管理分组移除CSV状态/控件及文件参数，页面普通新建直接调用watchlistGroupCreate。

修改点为backend/app/services/watchlist.py、backend/app/multitenancy/watchlist.py、
frontend/src/components/WatchlistImportDialog.tsx、WatchlistGroups.tsx和pages/Watchlist.tsx。
沿现有锁、SQL事务、revision与自选/增强查询失效，无新增存储字段或接口。
单股置顶、多组成员及only_new保留；旧groups/import-csv后端接口保持兼容，
历史列表不自动翻转。CSV顺序表示默认展示；用户主动字段排序继续生效。

## Verification

| 项 | 命令/验证 | 实际结果 |
| --- | --- | --- |
| V-1 | 文件模式显式MULTITENANCY_ENABLED=false；uv run --frozen --no-sync pytest tests/test_watchlist_csv.py tests/test_watchlist_groups.py tests/test_watchlist_enriched_join.py tests/test_watchlist_ocr.py tests/test_watchlist_upload_size_limit.py tests/test_strategy_targets.py -q | 102通过 |
| V-2 | 临时双库/角色；uv run --frozen --no-sync pytest tests/test_full_identity.py tests/test_full_strategy_targets.py -q | 40通过；不修改实际账户库 |
| V-3 | pnpm test：WatchlistImportDialog、WatchlistGroups、StrategyTargets、Watchlist、Modal、Screener及watchlist-columns | 26通过 |
| V-4 | pnpm build；针对本轮React文件的pnpm exec eslint | 均通过；构建有既有大chunk提示 |
| V-5 | Ruff租户服务及SQL测试；文件服务/CSV测试与HEAD逐项比较诊断 | 前者通过；后者无新增告警，既有47项减少到46项 |
| V-6 | 在临时源码副本换回HEAD生产代码，保留新测试；再验证当前代码 | 旧文件2例、旧SQL2例、旧前端3例失败；当前对应例通过；未stash或回退共享工作区 |
| V-7 | Chrome构建产物预览，1440×1000与390×844，隔离API/自选数据 | 上传→重新勾选→选分组→导入，请求及默认行序一致；重载保持；管理分组无CSV输入，窄屏新建可用；无页面/console错误及横向溢出 |
| V-8 | make check | 87项必需路径、31份文档、版本一致性及diff空白检查通过，版本仍0.4.32 |

浏览器截图为manager-desktop.png、import-desktop.png、order-desktop.png及
manager-mobile.png，保存在本机临时验证目录。浏览器使用普通Playwright：
本会话未提供可调用的Browser插件工具。3071仅验证构建产物，API拦截数据不认证生产。
原工作区3022的Vite依赖缓存出现重复React hook错误，改用已通过构建的产物验证；
隔离SSE不保持真实长连接，截图的断线提示属于测试环境。

初次宽测试使用macOS默认多租户配置，文件专用mock产生24项上下文错误；
按测试职责显式选择文件模式后102项全部通过，不改变用户运行配置。

## Regression test

- backend/tests/test_watchlist_csv.py：CSV解析/批量API/重读、未知项、重复代码、既有多组成员。
- backend/tests/test_full_strategy_targets.py：输入正序、only_new、revision、租户隔离。
- backend/tests/test_full_identity.py：既有批量顺序契约更新为输入正序。
- frontend/src/components/WatchlistImportDialog.test.tsx：重新勾选及补选既有成员。
- frontend/src/components/screener/StrategyTargets.test.tsx：管理分组无CSV且普通新建仍可用。

## Pattern analysis / Follow-ups

搜索rows.insert(0)及min(position)-1：当前两种批量入口已修复。
单股move_to_top两处为明确置顶，不属于反序缺陷。
旧create_group(symbols=...)两处仍使用历史逐项置顶，仅留给兼容后端接口，
管理分组已不调用；未扩大到旧接口排序迁移。Cell兼容入口按加入日期排序，
不参与主页面CSV确认链路，本次不修改。

## 运行与交付范围

源码保持0.4.32，未提交、推送或部署。本机实际frontend3021/API3028仍来自
v0.4.31-6b86f7249406发布目录，不能把工作区修改或预览验证称为实际运行副本已升级。
既有K线改动、账户、策略、SMTP及真实自选数据均保留。需求/架构/开发/测试/
用户操作/运维及版本分类文档同步本轮契约。
