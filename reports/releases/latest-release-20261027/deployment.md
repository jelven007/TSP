# v0.4.33 两端升级完成记录

完成时间：2026-10-27T11:25:23.167481+00:00。完整机器可读证据见[deployment.json](deployment.json)。

PR [#29](https://github.com/jelven007/TSP/pull/29) 已归并 main。
不可变标签 `v0.4.33`、本地主线、线上源码和两端 Runtime/Compute
均固定到 `ded70245bf95aac467ddc53a73418c093bdae51a`。
应用版本仅由 main CI 自动递增，未手工改号。

主线 [CI](https://github.com/jelven007/TSP/actions/runs/40487265356)
全部通过：后端 4725 passed / 9 skipped；
前端 397 passed、lint/build 通过。
[Docker](https://github.com/jelven007/TSP/actions/runs/40488168868)
成功，amd64/arm64 配置字节、rootfs、文件清单和修订均验证。
版本镜像、提交镜像和 latest 指向同一索引 `sha256:a52fb3dd823b44ee549e31a4d69058dff3b30aced3669ca23344bbe7cb8211de`。
本次 `promote-latest` Job 被依赖条件跳过；核对当前主线后，
已用相同OCI索引字节手动提升latest并重新验证。
没有修改不可变版本标签或为了该操作再改源码版本；后续CI须复核该Job的跳过条件。

删除本地40个、远程15个非main分支，32个关联工作目录已保留原内容并转为detached。
两边均只保留main。原未提交内容、709份未发布文件及恢复bundle保存在受控备份，
共享工作区仍保留未发布材料。没有清理工作目录或恢复覆盖原改动。

按授权顺序先本地、后线上完成升级；切换前排空任务、停写，
双库dump已在临时库实际恢复并与来源业务对账。
本地还逐表核对控制/业务库行数；旧源码、旧镜像和配置均保留。
没有重复历史K线清空，也没有重置用户后来保存的配置。

| 环境 | 地址 | 实际版本 | 结果 |
| --- | --- | --- | --- |
| local | http://127.0.0.1:3021 | 0.4.33 | 双库恢复、配置/行情/自选保留、浏览器验收通过 |
| online | https://server.example.com | 0.4.33 | 双库恢复、配置/行情/自选保留、浏览器验收通过 |

两端均真实登录并检查1600/1440桌面和390px窄屏：
实时行情位于15分K后；数据同步左对齐约3/4、总开关位于右上；
简化名称与日K同步顺序正确，无横向溢出。
新版本盘后默认时间为交易日16:00，本地实际为
16:00；
线上已保存的15:35自定义时间按配置保留要求继续生效，
日K与盘后管道的显示均与实际配置一致。
线上首次验收使用固定16:00断言而失败；
已核对升级前备份与升级后配置完全一致，再按保留的配置重验，
没有修改生产配置以迎合断言。失败记录和配置对照均保留在受控证据中。
两端原16条自选、2个分组及策略关系保留；
恢复默认排序、完整CSV导出、关联策略弹窗和刷新重读通过，
浏览器异常、应用资源/接口的控制台错误和HTTP错误响应均为0。
外部GitHub版本检查的403/CORS网络错误单独留证，不计入应用核心功能验收；
未拦截、模拟或伪造外部返回。其实际数量见机器记录的external_update_errors。
本地依赖链接导致的字体403已通过把依赖克隆到固定运行目录修复，
原链接保留，应用源码及锁文件均未修改。
本地editable包元信息从0.4.31同步到0.4.33，仅重装应用包，
其他已安装依赖版本保持一致，原元信息已保留并重新验收。
截图和原始受控证据留在本次发布证据目录。

快速查看：[线上数据总览](https://server.example.com/data)；
[本地数据总览](http://127.0.0.1:3021/data)。
验收截图与记录保存在
[受控证据目录](file:///path/to/home/.cache/tsp-dev/TSPP/backups/latest-release-20261027/evidence)：
[线上数据同步](file:///path/to/home/.cache/tsp-dev/TSPP/backups/latest-release-20261027/evidence/browser-online/data-sync-desktop.png)、
[线上数据画像](file:///path/to/home/.cache/tsp-dev/TSPP/backups/latest-release-20261027/evidence/browser-online/data-portrait-desktop.png)、
[线上窄屏](file:///path/to/home/.cache/tsp-dev/TSPP/backups/latest-release-20261027/evidence/browser-online/data-mobile.png)。

本次验收认证升级与数据保留，不认证历史行情全量覆盖、
其他浏览器或Windows安装结果。生产健康巡检和发布维护定时器均恢复运行。
新增验收材料为发布后本地记录，未再推送主线触发额外版本递增。
