# 数据总览布局验证 · 2026-10-27

本次仅更新工作区源码。实时行情移入数据画像、数据同步内置总开关并占宽屏3/4，
同步列表增加日K并简化名称。未提交、推送、更新真实本地运行副本或线上，也未改版本、
后端调度、数据库、用户配置或行情数据。

源码基于`4da5f30c9b5165612d78b573c142612f3fe25395` / `0.4.32`。
隔离工作树叠加用户既有未提交修改；同步前逐文件检查基线SHA256，保留原修正日期、
离线生成、自选关系及其他工作区修改。代码清单与哈希见[file-manifest.json](file-manifest.json)。

## 需求与实现

| 用户要求 | 实现与验收 |
| --- | --- |
| 实时行情放到画像15分K后，后续卡片后移 | 新增quotes卡片键，默认可见；旧六周期自定义排序只插入该键，不移动其他相对位置 |
| 总开关放进自动数据任务右上，参考行情卡片样式 | 改为带aria-checked的滑动开关；保留加载/错误/pending禁用及现有写API |
| 整体左对齐、宽度3/4 | xl四列中同步占三列、存储占一列，顶部对齐；窄屏纵向 |
| 自动数据任务→数据同步 | 卡片标题及可访问名称一致更新 |
| 启动数据完整性自检→数据完整性 | 更新列表名称，保留启动后30秒的原执行说明 |
| 集合竞价快照→集合竞价，秒级行情归档→秒级行情 | 更新列表名称，保留原采集窗口与开关依赖 |
| 日K周期同步紧接15分K周期同步 | 新增展示行，复用1d.enabled/time；与盘后管道共用现有任务，不增加调度 |
| 盘后自动采集周期交易日16:00 | 日K及盘后管道默认均为交易日16:00；保存过的自定义时间优先 |
| 财务指标更新→财务指标，数据源能力检查→数据源能力 | 更新列表名称，保留财务能力门控及能力探测独立运行 |

关闭自动同步不改变实时行情独立开关。隐藏实时行情卡片不停止行情服务，
轮询编辑继续调用原quote-interval入口。默认ETF仍隐藏，新保存的quotes拖拽位置保留。

## 自动验证

工具环境：macOS、Node22.16.0、项目pnpm、现有node_modules与Vitest3.2.7；
生产构建使用项目TypeScript/Vite配置。

| 命令 | 实际结果 |
| --- | --- |
| pnpm test src/pages/Data.test.tsx src/components/data/ScheduleOverview.test.tsx src/components/data/PageSettingsModal.test.tsx（实现前） | RED，16失败/6通过；缺quotes、日K与新同步容器。原修正终态缓存2项仍通过 |
| pnpm test（实现后） | PASS，80文件/397项，0失败 |
| pnpm lint | PASS，exit0 |
| pnpm build | PASS，TypeScript与Vite构建成功；保留既有大chunk提醒 |
| pnpm exec eslint src/components/data/ScheduleOverview.tsx、git diff --check（最终缩进后） | PASS，exit0 |
| 根make check PYTHON=backend/.venv/bin/python | PASS：87必要路径、31Markdown、8需求模块、版本0.4.32及diff空白检查 |
| python scripts/check_docs.py --include-reports、git diff --check（最终报告后） | PASS：122Markdown、8需求模块，报告/截图链接及diff空白检查通过 |
| 同步SHA256核对 | PASS：16项任务文件与已测试源码一致；其余46个基线文件原字节保留；清单哈希全部匹配 |

新增行为回归覆盖默认/旧/新卡片顺序、显隐及恢复默认，
日K默认和保存时间、日K/总开关暂停，
真实Data页面中的卡片位置、pending禁用、成功刷新preferences和dataStatus、
失败保留原值并提示、偏好加载失败禁用。

前端全量中若干既有Router测试输出v7升级提醒，未失败；
本次未运行后端全量测试，因为没有修改后端接口、服务或调度。

## 浏览器验证

环境：隔离真实Data页面、Router和Query上下文；`http://127.0.0.1:3201`。
Browser工具未提供（Browser plugin not available），使用现有Playwright与本机Chrome，
不安装新依赖。所有`/api/`请求由本地fixtures拦截，未访问真实账户或生产数据。
桌面1440×1100，手机390×844，浅深色均检查。

流程：数据总览加载→核对画像顺序和同步布局→保存总开关→保存失败→
独立行情开关/间隔→页面设置隐藏/重开/鼠标拖拽→刷新→旧排序迁入→手机浅深色。
最终脚本exit0，11组检查通过，0失败；结构化结果见
[browser-results.json](browser-results.json)。
同步卡片宽812px，存储260px、列间隔16px，满足812=3×260+2×16；
二者顶部y104一致，同步左边x176与现有内容栅格左边一致。
总开关在同步右上角；默认实时行情紧接15分K。
保存中禁用、成功状态刷新、500错误保留旧值并提示，
实时行情独立关闭及间隔改为5秒、卡片隐藏/重开、鼠标拖动后刷新保留、
旧六周期排序插入均通过。390px手机浅深色scrollWidth=390，没有横向溢出。
页面身份、非空、无框架覆盖及控制台检查均通过；
仅包含测试主动注入的500/503网络错误，无相关运行错误或警告。

初次临时harness未从frontend目录启动，Tailwind没有加载content；
改为正确工作目录后核对布局。旧截图不作为布局证据。
其后修正临时fixture的实时开关字段及弹窗完整标题定位，不改生产代码掩盖测试错误。

| 截图 | 证明内容 |
| --- | --- |
| [桌面浅色](after-desktop-light.png)、[桌面深色](after-desktop-dark.png) | 同步3/4布局、右上滑动开关、任务名称/日K/16:00及画像位置 |
| [数据画像桌面](portrait-desktop-dark.png) | 15分K之后的实时行情及其他卡片后移 |
| [手机浅色首屏](mobile-light-viewport.png) | 390×844原尺寸、总开关及纵向布局 |
| [手机浅色全页](after-mobile-light.png)、[手机深色全页](after-mobile-dark.png) | 窄屏完整列表与画像，无横向溢出 |
| [手机浅色画像](portrait-mobile-light.png)、[手机深色画像](portrait-mobile-dark.png) | 窄屏卡片顺序及行情卡片 |

## 文件与生命周期

| 文件 | 责任 |
| --- | --- |
| frontend/src/pages/Data.tsx | 卡片迁入画像、同步3/4布局与开关接线 |
| frontend/src/components/data/ScheduleOverview.tsx | 同步标题/开关、简化名称、共享日K配置展示 |
| frontend/src/components/data/PageSettingsModal.tsx | quotes键、默认位置与旧排序兼容 |
| frontend/src/components/data/QuoteConfigCard.tsx | 移入画像的行情开关可访问语义 |
| frontend/src/pages/Data.test.tsx | 位置与开关成功/失败/pending/加载失败，保留旧修正缓存回归 |
| frontend/src/components/data/ScheduleOverview.test.tsx | 名称、日K顺序/时间和开关依赖 |
| frontend/src/components/data/PageSettingsModal.test.tsx | 默认/旧/新排序、显隐、重开/恢复默认 |
| docs/requirements.md | 产品范围与验收入口 |
| docs/architecture.md | 查询、mutation与日K共享调度契约 |
| docs/secondary-development.md | L3边界与旧配置兼容 |
| 操作说明书.md | 用户入口、布局、名称与画像默认顺序 |
| docs/testing.md | 回归矩阵与本次实际证据入口 |
| docs/operations.md | 入口变化、保存配置优先及运行边界 |
| docs/release-operations.md | 支持入口与升级核对项 |
| docs/version-baseline.md | 源码候选、版本不加号及未部署事实 |
| docs/reference/engineering.md | 需求与六类生命周期映射 |

## 范围限制

397项是本次工作区前端结果，不代替主线CI、后端全量或真实调度执行。
本次不证明历史数据完整、交易日采集运行或本机3021/3028及ECS已应用页面变化。
旧配置与行情数据未迁移；需更新运行环境时使用现有部署流程并保留用户保存值。
