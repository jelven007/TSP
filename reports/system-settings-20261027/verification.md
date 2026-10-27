# 系统设置修复验证

2026-10-27，工作区基线ded70245 / v0.4.33；本轮只修改源码、测试与文档。

“并发策略”首次显示推荐数字，支持1～999手工输入、失焦/Enter保存及恢复推荐；
保存数超过资源/部署上限时显示实际生效数。进入策略页自动运行已删除，旧配置不再启用。
每策略由用户手动运行和启停监控；外链标题改“个股外链”；
开源区域只显示仓库链接、最新版本号与发布时间。

## 执行结果

| 验证 | 结果 |
| --- | --- |
| 旧实现连续3次复现 | 前端17失败/13通过；后端4失败/26通过 |
| 隔离反向验证 | 恢复旧目标代码失败；恢复修复后前端32/后端30项通过 |
| 后端最终受影响模块 | 6文件93项通过 |
| 前端最终受影响模块 | 7文件65项通过 |
| 前端构建、ESLint | 通过 |
| Ruff增量 | 资源模块/测试通过；settings/preferences的57项历史告警未新增 |
| 文档、目录、版本与diff检查 | 根make check通过，版本0.4.33 |
| Chrome真实组件交互 | 999保存/刷新保留、1000拒绝、推荐恢复、保存失败、手动运行和监控启停通过 |
| 布局 | 桌面1440×1000、窄屏390×844浅深色无水平溢出，pageerror为0 |

后端执行`uv run --frozen --no-sync pytest`，以工作区backend为PYTHONPATH；
测试包括`test_strategy_concurrency`、`test_full_compute`、`test_full_platform`、
`test_screener_run_all_progressive`、`test_strategy_run_queue`、`test_openapi_contract`。
前端执行`pnpm test`：System、Screener、openSourceMonitor、openSourceRelease、
StrategyCard、DefaultStrategyParamsDialog、Monitoring；随后`pnpm build`和`pnpm lint`。

浏览器使用现有Playwright环境，API与GitHub发布请求均拦截。
捕获到Chrome阻止本地Vite HMR WebSocket的消息，以及主动模拟的503保存失败；
没有应用运行时异常或框架错误遮罩，热更新链路不在本轮验证结论内。

## 界面证据

- [修改前桌面深色](before-desktop-dark.png)
- [修改后桌面浅色](after-desktop-light.png)
- [修改后桌面深色](after-desktop-dark.png)
- [修改后窄屏深色](after-mobile-dark.png)
- [修改后窄屏浅色](after-mobile-light.png)

完整调用链、兼容范围与复现见[根因记录](../../.claude/artifacts/fixes/system-settings.md)。
需求/架构/开发/测试/运营/运维文档及操作说明已同步；
没有更新运行副本或线上，没有提交/推送或递增版本。
