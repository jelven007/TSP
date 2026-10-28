# 数据采集统一修复

## 问题

- 数据总览同时存在顶层「数据修正」和K线卡片离线补齐，两套入口发生行为漂移。
- 1分K、日K卡片仍按数量/月年向前扩展，不能保证两台环境获取相同日期范围。
- `/api/kline/sync_period`没有消费起止日期和full/missing模式。
- missing任务在本地缺口扫描前解析Provider，可能被无关的上游状态提前阻断。
- 全量1分K没有证明实时行情只在分区写盘期间暂停。

## 根因

`KlineSyncConfig`仍调用旧`period/value/unit`契约，基础周期和
`repair_daily`区间任务分属两个启动入口。缺口服务本身已经先扫描本地并延迟取数，
但API在建任务前做了Provider门控。`sync_period`写盘也未进入线程本地暂停门闸。

## 修复

- 删除数据总览顶层修正按钮、状态和弹窗。
- 1m/1d卡片使用统一DatePicker的起止日期及全量/遗漏双按钮。
- `sync_period`与兼容`repair_daily`复用`_start_kline_range_job`。
- missing只在发现首个缺口后调用`require_period_provider`。
- 1m全量保持32自然日/100证券分块，Provider取数后仅在`persist_period`临界区暂停行情。
- 保留5/10/15m、1w本地生成和旧API兼容，不修改Provider协议、Parquet schema或版本。

## 回归

- 前端定向23项、全量453项、ESLint、TypeScript/Vite生产构建通过。
- K线周期API 40项通过。
- 修正与rustdx历史请求预算102项通过。
- 完整后端4674项通过、162项按平台条件跳过；首次全量发现的2项空批次进度回归已修复。
- 相关K线服务及测试Ruff通过，`git diff --check`通过。
- 系统Chrome在1440×1000及390×844验证1分K/日K设置，无旧入口、无横向溢出或业务错误。
- rustdx桥接7项、Python集成179项、确定性核心199项及55+2项文档测试通过；
  4项公共TDX直连测试本轮因外网超时未通过。

## 边界

未提交、推送、部署、重启服务或获取真实全市场数据。rustdx v2.0.4身份与既有融合
验证沿`reports/rustdx-v204-20261028/verification.md`，正式发布仍由main CI分配版本。
