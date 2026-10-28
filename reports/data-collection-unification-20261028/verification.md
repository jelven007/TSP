# 数据采集统一修复验证

日期：2026-10-28。应用版本保持`0.4.36`，未提交、推送或部署。

## 范围

- 数据总览删除独立「数据修正」。
- 1分K、日K离线补齐改为起止日期及全量/遗漏双模式。
- 两种模式均使用输入闭区间；遗漏先查本地，发现缺口后才解析Provider。
- 全量1分K保持32自然日、100证券分块和rustdx v2.0.4历史页定位。
- Provider取数/计算不暂停行情，只在分区写盘临界区暂停。

## 红灯证据

- 前端旧实现11项失败：仍显示修正入口、数量/月年控件和单「获取数据」按钮。
- 后端区间路由连续三次均7项失败：日期/mode未透传，非法范围仍返回200。
- 全量1分K写盘门闸测试失败：Provider取数未暂停正确，但实际写盘时门闸未激活。
- 本地优先测试失败：no-gap missing在任务创建前解析Provider。
- 第一次完整后端套件发现2项空Frame批次进度回归；保留原`persist_period`调用后，
  非空批次才进入写盘暂停门闸。

## 通过结果

- `KlineSyncConfig.test.tsx`与`Data.test.tsx`：23 passed。
- 前端全量：84 files / 453 passed；ESLint通过；`tsc -b && vite build`通过。
- `test_kline_periods_api.py`：40 passed。
- `test_repair_kline.py`与`test_rustdx_history_request_budget.py`：102 passed。
- 完整后端：4674 passed / 162 skipped；180项既有warning。
- 相关K线服务和测试Ruff：通过。
- `git diff --check`：通过。
- rustdx PyO3桥接：7 passed；Python身份/Provider/连接池等：179 passed。
- rustdx确定性核心：199 passed；文档测试55 passed、18 ignored，compile-fail 2 passed；
  rustfmt与严格Clippy通过。

既有rustdx v2.0.4融合验证包含203项上游核心单测、55项文档测试、7项桥接测试、
严格Clippy/rustfmt、274项Python聚焦回归及公开节点只读验证，见
[rustdx v2.0.4验证](../rustdx-v204-20261028/verification.md)。

## 行为核对

1. 1m/1d请求正文为`period/start_date/end_date/mode`。
2. full与missing任务记录同一闭区间。
3. missing无缺口时`requests=0`，不解析Provider；有缺口后按100证券及连续窗口请求。
4. full 1m网络取数时行情未暂停，原子分区写入时暂停并在写完后恢复。
5. 5/10/15m及1w仍只按本地基础分区生成。
6. 旧`repair_daily`及`value/unit`调用保留兼容，但数据总览不再暴露旧交互。

## 浏览器验收

系统Chrome连接当前工作区前端`127.0.0.1:5175`及既有本地后端完成登录后验证：

- 1440×1000打开1分K设置：页面宽度1440，弹窗宽度478，无横向溢出。
- 390×844打开日K设置：页面宽度390，弹窗宽度356，无横向溢出。
- 两端均有起始/结束DatePicker、全量/遗漏按钮，无「向前扩展」和顶层「数据修正」。
- 业务控制台错误和HTTP错误均为0。

截图：[桌面1分K](screenshots/data-desktop.png)、
[移动日K](screenshots/data-mobile.png)；结构化结果见
[browser-acceptance.json](browser-acceptance.json)。

## 未认证

未执行真实全市场回填、正式P95、线上ECS对比、服务重启、Docker/Windows/Linux产物
或生产部署。历史覆盖仍需按实际证券、日期和剩余缺口核对，任务成功不等于上游拥有全部历史。

本轮重新执行上游203项核心测试时，199项通过；4项直接连接公共TDX节点的测试
`tcp::basic::connection`、`tcp::ip::tests::first_server_is_connectable`、
`tcp::stock::kline::connection`、`tcp::stock::xdxr::connection`因当前外网超时失败。
排除这4项外部连通用例后的确定性核心、文档测试、桥接和Python集成均通过；
既有v2.0.4公开节点只读结果不据此改写为本轮在线成功。
