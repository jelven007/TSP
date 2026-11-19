# 验证证据与历史报告

仓库不保存已结束的过程计划、修复流水、升级日志、重复导出或临时截图。
已清理材料中的历史日期、安装状态和测试计数不能认证当前版本或运行状态。
用户本地的当前成果、Excel、冻结业务结果、未完成开发和不可变审计记录保留；
清理过程文件不删除应用中的回测任务、数据库或交易账本。

需求编号基线移至 [requirement-ids.json](requirement-ids.json)，内容保持原冻结集合。
[需求手册](requirements.md)仍是唯一功能定义；检查继续拒绝缺失编号、重复定义、
无效模块、状态和本地链接。目录移除不关闭文档或需求门禁。

当前发布过程见[发布流水线](release-pipeline.md)，身份核对见
[版本基线](version-baseline.md)，验收要求见[测试手册](testing.md)。
最终用户成果归 [deliverables](../deliverables/README.md)，必要证据归其 audit 目录。
未完成设计、计划和缺陷记录可在本地 `.claude/artifacts/` 维护，结束后将有效契约
归入对应手册并清理过程材料。保留范围和检查方法见[维护规则](maintenance.md)。
