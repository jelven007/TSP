# Full目录刷新后公共行情缓存无法恢复

Status: FIXED（源码回归；正式产物及双端部署另行签收）

## 现象与预期

Full平台启动预热成功后，09:00目录任务再次清空股票派生缓存。实时行情关闭时，
公共快照一直为空；即使手动预热完成，同一公共版本的API消费者也可能继续保留冷快照。
目录刷新后应自动后台回暖，完成时通知消费者；快照请求不应重算历史。

## 复现与根因

`test_full_platform.py::test_platform_lifecycle_rewarms_catalog_with_quotes_disabled`
在原代码连续三次失败，触发真实`refresh_current_instruments`，断言后台预热被启动。
旧代码反向验证再次失败，两种成功非空/成功空输入均覆盖。

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| 目录失效后的平台生命周期缺回调 | confirmed | Repository清缓存后调用`_on_refresh_done`，Full未注册；真实测试在等待回暖时失败 |
| 公共文件未完整发布导致消费者拒绝更新 | eliminated（本次现象） | 相同发布身份下手动预热可恢复Platform行数，API仍保留冷快照；缺少内存更新通知 |

反向链路：API公共绑定→Full只读snapshot→Repository股票缓存→目录刷新失效。
失效源已有回调，缺口在Full lifespan接线；独立主应用已有自己的刷新回调。

## 修复与边界

Full先注册QuoteService与Repository回调再启动预热。目录/同步刷新沿已有
`ensure_enriched_warmup`启动有界单飞后台任务；完成后失效公共快照并发送
`quotes_updated`标记。成功空数据也通知，不因此递归预热。指标就绪只认实际非空缓存。
关闭及启动失败资源清理均移除回调。不引入私有策略、矩阵预热、数据库迁移或Provider接口。

## 验证

- V-1：修复后两项生命周期回归通过。
- V-2：只移除生产修复，测试再次两项失败；恢复后通过。
- V-3：受影响9个模块，真实隔离PostgreSQL：146项通过、无跳过、20条既有Polars弃用warning。
- V-4：Ruff无诊断；快照冷热读不重算、重复目录刷新单飞、成功空通知、完成时通知及关闭回调释放均覆盖。
- 正式主线CI、同SHA产物和双端目录刷新现场验收在发布交付回执记录，不将本次合成测试冒充运行部署。

## 模式检查

搜索`_on_refresh_done`：独立主应用有注册及关闭清理；Repository负责通知；
缺口仅为Full平台接线。本次不改独立主应用或快照请求路径。
