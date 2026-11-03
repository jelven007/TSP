# 一级菜单名称与初始化顺序调整

2026-11-03：源码调整及相关验证完成，尚未提交、合并或部署。

| 原一级菜单名称 | 新名称 | 保持的路径 |
| --- | --- | --- |
| 信号指标 | 信号 | `/signals` |
| 持仓提醒 | 持仓 | `/lots` |
| 模拟交易 | 模拟 | `/paper` |

初始化默认顺序采用当前运行配置已保存的18项顺序：

行情、环境、自选、策略、因子、信号、回测、持仓、模拟、复盘、异动、监控中心、连板梯队、个股分析、概念分析、行业分析、财务分析、数据总览。

已同步前端侧栏、菜单设置、后端`DEFAULT_NAV_ORDER`及由它派生的租户默认值。三个页面标题、助手上下文名称以及相关页面跳转说明使用新名称。业务路径、排序/隐藏偏好、批次与账户数据保持；缺偏好时返回新默认，已有自定义配置继续优先。

六类生命周期文档同步记录本次命名、顺序和兼容范围。模拟交易业务、持仓提醒功能、信号定义及其执行规则没有变更。

验证结果：

- 后端`test_preferences_cache.py`：39项通过，包含默认顺序、返回副本、不写入偏好及已有排序/隐藏项保留；新顺序断言先失败后通过。
- 前端Lots、Paper、SignalTriggerActions、StrategySettingsSignals：4个文件、18项通过；存在既有React Router升级提示。
- 前端TypeScript与Vite生产构建通过；存在既有较大chunk提示。
- 默认表核验：Layout、MenuSettings、后端常量、租户DEFAULTS的18项顺序和三个新名称一致。
- 文档检查通过：37份Markdown、8个需求模块；版本检查通过，保持0.4.46。
- Ruff增量检查：2个Python文件，原8条告警保持，新增0条；`git diff --check`通过。

原独立工作目录`TSP-menu-defaults-20261103`已受控保全，分支`feat/menu-defaults-20261103`，基线`4453ecc4`。相关入口：[菜单默认值](../backend/app/services/preferences.py)、[侧栏](../frontend/src/components/Layout.tsx)、[菜单设置](../frontend/src/pages/settings/MenuSettings.tsx)。

本次未部署到实际本地服务或线上环境，未修改受控运行偏好；原工作区并行修改与此前行情调度候选均保留。未执行回测或版本升级。
