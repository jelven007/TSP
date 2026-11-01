# 第二批主线整合与两端升级验收

验收时间：2026-10-27T16:45:33.695513+00:00。

最新 63 个发布文件已通过 [PR #30](https://github.com/jelven007/TSP/pull/30) 集成远程 main。
主线 CI 自动递增为 **v0.4.34**；源码、自动标签、CI 和两端运行均固定到
`18f6f5cbea5bb651dc5fc09f289016bd01b9b6de`。按用户要求，先同步主线并删除分支，再本地升级，最后线上升级。

| 项目 | 实际结果 |
| --- | --- |
| 线上 | [https://server.example.com](https://server.example.com)，v0.4.34，6 项功能 ready |
| 本地 | [http://127.0.0.1:3021](http://127.0.0.1:3021)，v0.4.34，6 项功能 ready |
| 分支 | 删除本地 4 个、远程 1 个；两端 Git 仅 main |
| PR/main | 实际执行的质量门成功，主线 CI 自动递增并创建标签 |
| Docker | build-and-push 成功；附属质量任务和 promote-latest skipped；完成 latest 提升后再次核验注册表身份 |
| 前端 | 83 文件，437 测试通过，lint/build 通过 |
| 后端 | 4731 passed、12 skipped、180 warnings |
| 工具/类型/原生桥接 | 134 passed；定向 mypy 与原生桥接通过 |
| Ruff | 本批新增 0，历史 57 项保持 |

两端升级前均完成双库备份及真实恢复比对。自选 16 条、分组 2 个、策略 ID、
行情行数、账户与受控配置经升级前后核对保留。管理员登录验收通过。
本地日K时间为 16:00，线上为
15:35，均与升级前保存值一致。
本地运行目录的 1727 个源码文件逐项核对 Git blob。
线上 7 个服务容器、PostgreSQL、计算 broker 与定时器均通过检查。

本地 Platform 的 readiness 和各项健康检查正常。

浏览器验收覆盖同步分类收起/展开、日K历史数量和单位、日期与时分弹层、全部/未分组/
自定义分类导出、策略关联与排序、策略页无自动运行请求、系统设置并发输入范围、
浅深色桌面与 390px 窄屏。没有触发获取、修正或策略计算任务，没有更改已保存设置。
应用页面错误和失败请求为 0；外部更新探测单独保留原始证据。

外部 GitHub 更新探测仍出现 403 和网络/CORS 错误，本地记录
32 条、线上记录
35 条（同一请求可能产生多条记录）。
这项自动更新探测未验收成功；本次固定提交的主线、镜像与两端运行版本已独立核验。

浏览器环境为已安装 Chrome + Playwright；Browser plugin not available。
桌面检查 1600×1000 与 1440×1000，窄屏检查 390×844。
窄屏截图等待导航动画结束，并通过现有控件收起设置菜单；这些操作只影响独立验收会话。

| 页面验收项 | 实际结果 |
| --- | --- |
| 页面身份与非空 | 数据总览、自选、策略和系统设置均有可见标题及真实内容 |
| 错误覆盖层与控制台 | 无 Vite 错误覆盖层，应用错误和失败请求为 0 |
| 交互 | 分类展开、弹层退出、CSV导出、排序恢复和菜单收起通过 |
| 配置与数据 | 自选、分组、关联策略、保存时间和并发设置读回一致 |
| 截图 | 两端桌面、浅深色和窄屏证据已保存 |

- [local 系统设置](screenshots/local-settings-desktop-light.png) · [窄屏](screenshots/local-settings-mobile-light.png) · [同步分类](screenshots/local-data-sync-collapsed-desktop.png)
- [online 系统设置](screenshots/online-settings-desktop-light.png) · [窄屏](screenshots/online-settings-mobile-light.png) · [同步分类](screenshots/online-data-sync-collapsed-desktop.png)

原工作区修改保存于 Git stash，已发布的新增原件移至受控备份；未发布研究材料保留。
工作树内容在解除分支引用前核对，全部 Git 引用另存可验证 bundle。
旧镜像、源码和配置以及双库恢复证据分别留存，未重新初始化数据库。

本地备份：`/path/to/home/.cache/tsp-dev/TSPP/backups/complete-release-20261027-2/local-pre-18f6f5cbea5b`。
线上备份：`/opt/tsp-full/backups/pre-v0.4.34-18f6f5cbea5b`。
完整受控证据及 SHA256 清单：`/path/to/home/.cache/tsp-dev/TSPP/backups/complete-release-20261027-2/evidence`。
本报告与 JSON 为部署后本地签收，不再推送以免触发额外版本递增。

12 个跳过测试、历史 Ruff 告警及未执行的 Windows 桌面产物验收仍按原边界记录；
不据此声称全部平台或所有历史行情已经初始化完成。
