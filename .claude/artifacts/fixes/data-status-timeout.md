# `/api/data/status` 统计缓存并发缺陷

## Input

2026-10-28用户反馈“请求超时（30s）· /api/data/status”。
基线为v0.4.34 / 18f6f5cbea5bb651dc5fc09f289016bd01b9b6de。

## Confirmed cause

局部缓存失效递增全局世代，丢弃无关在途统计。缓存miss没有合并同键请求，
导致并发状态轮询重复扫描。并发回归先失败；恢复原实现后再次失败。
本轮没有稳定复现超过30秒的串行HTTP请求。

## Change

PublicQueryCache局部世代、按identity/generation共享计算、锁外扫描/等待和finally唤醒。
表级/前缀/全量失效后新读者不等待旧扫描；旧值不得重新缓存或覆盖新值。
保留TTL、容量、数据目录隔离、响应口径及前端timeout。
数据质量状态测试改为独立job_store夹具。

## Regression test

backend/tests/test_public_query_cache_concurrency.py：
无关总览/分钟更新保留在途日线；并发miss只计算一次；跨表/目录不阻塞；
表级/前缀/全量失效允许新世代先完成；异常释放等待者。

## Verification

隔离目录8个相关测试文件：103 passed / 14 warnings。
合入工作区3个测试文件：18 passed。
反转原实现：4 failed / 5 passed，随后恢复修复。
缓存mypy、变更Ruff、文档及diff检查通过。
实际只读行情4并发：冷读10.9秒→5.45秒，下一轮6.5–7.1秒→6–7毫秒，
关键表各4次扫描→各1次；分钟行数随后台采集变化，小样本不构成生产P95。

## Delivery boundary

代码已合入工作区，未commit/push/部署。活动采集未取消，两端运行尚未加载修复。
采集结束后的新产物发布与运行验收仍待完成。
证据见[完整记录](../../../reports/data-status-timeout-20261028/verification.md)。
