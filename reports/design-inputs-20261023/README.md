# 系统目标设计输入归档

2026-10-23 将既有离线交付中的设计依据保存为可随仓库检出和规范包携带的副本：
[原融合技术方案](fused/融合技术方案.md)、[原交付验证](fused/交付验证.md)、
[原证据索引](fused/evidence/baseline.md)、[ClickHouse八表设计](clickhouse/设计说明.md)、
[单机SQL](clickhouse/01_schema.sql)、[集群SQL](clickhouse/02_cluster.sql)、
[查询](clickhouse/03_queries.sql)、[可选投影](clickhouse/04_optional_projection.sql)、
[质量检查](clickhouse/05_quality_checks.sql)和[用户参考原图](fused/assets/reference-TSP.png)。

副本内容与原文件字节一致。来源路径/哈希由
[本次整合验证](../production-system-consolidation-20261023/verification.md)留档。
历史文件的版本、表设计状态和测试计数保留原时点；不作为当前应用集成或容量证据。
当前统一目标以[系统目标技术方案](../../docs/architecture.md#target)为准。

原`deliverables/`离线HTML/ZIP及其配套资源保持冻结，本归档仅复制正文、SQL、
必要验证数据与用户原图；不把生成的浏览器运行库纳入应用源码或运行镜像。
