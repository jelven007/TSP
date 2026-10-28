# 并发策略使用一半硬件资源

2026-10-28，基线18f6f5cb / v0.4.34。用户要求将并发策略资源上限调整为硬件配置的一半。

共享资源预算`strategy_resources.BUDGET_RATIO`从0.25改0.5，
独立模式与Compute Broker的策略总内存、CPU预算均为执行主机硬件的50%。
推荐值与实际生效数沿现有公式重新估算。16 GiB主机的策略内存预算为8 GiB。
页面通过资源响应`budget_ratio`显示百分比，未升级服务返回25%时如实显示；
原1～999输入、用户已保存并发值、部署容量、租户限制及旧任务资源记账保持原契约。

## 验证

- 实现修改前，后端预算回归9失败/24通过，前端50%显示用例1失败/14通过。
- 实现修改后，后端6文件93项通过，前端System16项通过。
  后端包含并发预算、Compute/Platform、渐进批跑、策略队列和OpenAPI契约。
- `pnpm build`、`pnpm lint`及资源模块/测试Ruff通过；`make check`通过。
- 本机Chrome与Playwright验证真实`StrategyConcurrencyControl`，API拦截：
  50%内存/CPU展示、16并发下每任务512 MiB、恢复推荐、保存3及刷新保留通过。
  测试主机为16 GiB/32 vCPU，单任务参考限额为1024 MiB/1 vCPU，推荐8；
  此数值只属于测试配置，实际推荐值随主机和部署限额变化。
- 桌面1440×1000、窄屏390×844浅深色无水平溢出，pageerror为0。

Browser plugin不可用，使用已有Playwright；Chrome阻止Vite HMR WebSocket，
不影响控件加载与交互，本次不验证热更新或真实Docker负载。
旧比例截图使用HEAD控件及25%资源响应；新截图使用当前共享控件与50%响应。

## 界面证据

- [修改前](before.png)
- [桌面浅色](desktop-light.png)、[桌面深色](desktop-dark.png)
- [窄屏浅色](mobile-light.png)、[窄屏深色](mobile-dark.png)

已同步需求、架构、二开/工程、测试、运营、运维、部署说明和操作说明。
未提交、推送、递增版本或更新运行副本/线上；其他工作区修改保留。
上线须同步Compute/独立后端并确认响应`budget_ratio=0.5`。
