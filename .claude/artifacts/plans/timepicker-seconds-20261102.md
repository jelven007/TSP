# 公共时间控件秒级模式 Implementation Plan

> 按本会话已授权范围逐项执行；保留现有隔离工作目录，不提交或创建子代理。

**Goal:** 止损与兜底复用公共时间控件，24小时制显示和保存HH:mm:ss。

**Architecture:** `TimePicker`新增可选`showSeconds`，默认时分模式保持。
`CustomSignalDialog`的两个早盘时间字段启用秒列，现有参数及校验不变。

**Tech Stack:** React、TypeScript、Vitest、现有Modal／Picker弹层与Vite。

## 1. 先红后绿

- [x] `frontend/src/components/TimePicker.test.tsx`覆盖秒确认、补零、重开、Escape、旧时分及清空／禁用。
- [x] `frontend/src/components/signals/CustomSignalDialog.time.test.tsx`覆盖公共按钮、两个时间精确保存与非法关系。
- [x] 运行`PATH="/Users/bytedance/.local/bin:$PATH" pnpm test src/components/TimePicker.test.tsx src/components/signals/CustomSignalDialog.time.test.tsx`，记录新能力缺失的失败。

## 2. 最小实现和兼容验证

- [x] `TimePickerProps`增加`showSeconds?: boolean`；可选秒列、初值补零、秒选中／滚动、确认格式保持。
- [x] 两个早盘原生时间输入替换成`TimePicker showSeconds`，`time/fallback_time`值直接传递。
- [x] 更新旧对话框测试中的输入定位及Modal mock的弹层容器；金额和策略条件断言保持。
- [x] 跑TimePicker、DatePicker、两个已有CustomSignalDialog测试、新时间测试、SignalTriggerActions及StrategySettingsSignals。
- [x] 受影响ESLint与`pnpm build`通过；不重复无关后端收益测试。

## 3. 本地和交付

- [x] 从当前实际运行源派生新目录，只更新公共控件／表单及静态资源，后端文件hash保持。
- [x] 构建匹配Compute静态清单，任务空闲后可逆切换，受控配置和11组用户状态保持。
- [x] 实际登录，桌面1440／手机390选择时分秒、确认／Escape、保存重开／刷新。
- [x] 真实周期同步页面仍显示HH:mm；不保存其配置，不新增交易或收益任务。
- [x] 同步六类生命周期说明及简短验收记录，交付可点击文件链接。

实际结果：先红6失败／2通过，最终相关前端42项、ESLint、构建和`make check`通过。
实际两时间秒值保存重开／刷新和原时分控件通过；重启登录及11组状态签名保持。
核验期间另有1条成功研究记录新增，原记录逐行签名保持，本次未发起研究任务。
报告及14文件增量补丁见`reports/timepicker-seconds-20261102.md`。
