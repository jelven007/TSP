# 周K生成混合Parquet schema修复验证

日期：2026-10-28。基线：`1c630cc2` / v0.4.36。
本轮未改版本、未提交、未推送、未部署，也未执行真实周K写盘。

## 问题与根因

日K历史分区只有标准OHLCVA；实时更新分区会增加`quote_ts/quote_time_basis`。
Polars 1.44的裸`scan_parquet`默认严格使用首文件schema：

- 旧分区在前，新分区报`extra column ... quote_ts`。
- 新分区在前，旧分区报`did not find column quote_ts`。

周K“生成数据”先扫描所有基础分区枚举证券，再按范围读取日K聚合，
两处都绕过了仓库已有的兼容读取助手，因此用户任务在写盘前失败。

## 修复

`backend/app/services/kline_periods.py`：

- 证券枚举使用`scan_parquet_compat`，兼容增量列变化。
- 日K范围使用`scan_daily_parquet`，采用标准日Kschema、缺列插入、
  额外列忽略及允许的整数到浮点转换。

没有改动周KOHLCVA公式、分区目录、任务接口、视图、缓存或Provider。
无需清理/迁移10月27日分区，`quote_ts`继续服务日K完整性判断。

## 自动验证

| 验证 | 结果 |
| --- | --- |
| 修复前新测试，连续3次 | 每次2 failed / 35 passed |
| 周K周期模块 | 37 passed |
| 周K生成API与进度 | 39 passed |
| 共享日Kschema及模拟交易读取 | 5 passed |
| 周K修正筛选用例 | 2 passed |
| 进程内恢复严格扫描 | 2 failed；恢复修复后2 passed |
| Ruff | passed |

整份`test_repair_kline.py`另有2个基线环境失败：测试默认数据源为rustdx，
而干净HEAD worktree没有当前工作区未提交的rustdx插件升级；与本次2行读取接线无关。
对应周K修正用例单独通过。

## ECS实际数据只读核对

`/app/data/kline_daily/date=2026-10-26/part.parquet`没有`quote_ts`；
10月27日分区包含`quote_ts/quote_time_basis`，与用户报错一致。
在现有ECS容器内调用共享兼容扫描并仅内存聚合：

- 读取10,430行；
- 覆盖5,215证券；
- 生成5,215条、日期为2026-10-26的周K内存结果。

该检查没有调用`persist_period`、没有创建任务、没有修改线上文件。

## 兼容与部署

无API、前端、Provider、账号schema、配置或行情文件变化。
修复需由执行周K任务的Platform加载新后端源码后生效；旧失败任务可保留，
升级后重新点击“生成数据”。本轮未验证部署后真实任务终态和全部周K覆盖。
