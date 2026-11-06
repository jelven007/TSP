# 正式镜像首次提升的 Registry 判定修复

主线 CI [42301734916](https://github.com/jelven007/TSP/actions/runs/42301734916)
已完成 v0.4.51 的前后端质量门、Runtime/Compute 两架构构建和身份矩阵，
并创建源码标签；正式镜像提升失败，生产仍为 v0.4.50。失败的 CI、标签和候选镜像
保留为审计，不作为正式部署产物。

## 原因与修复

Buildx 查询尚不存在的版本标签时实际返回 `ERROR: <tag>: not found`。
旧 `promote.inspect` 只识别 `manifest unknown`，因而在首次写入前阻断。
同一标签的 Registry API 实际返回 HTTP 404，JSON 错误代码为 `MANIFEST_UNKNOWN`；
Runtime/Compute 的版本标签及完整 SHA 标签均尚未存在，没有发生部分提升。

修复复用现有 `Registry` 的 HTTPS、认证及重定向约束。只有 HTTP 404 且非空错误列表
全部为 `MANIFEST_UNKNOWN` 才确认标签缺失；权限、网络、仓库不存在、混合或非法错误
仍阻断。已存在标签读取实际 manifest 字节并核对其 SHA-256 与响应 digest；
所有目标预检、写前复查、写后 digest 验证和冲突拒绝保持。

## 验证

- 原工具的新增回归：1 failed、6 passed；失败为首次发布误报 `registry lookup failed`。
- 修复后定向回归：15 passed，0.07 秒；包括首次提升、重试幂等、冲突先阻断、
  HTTP 401/403/404/500、网络超时、错误 JSON、混合错误和实际 manifest digest 校验。
- 修改范围 Ruff 通过；真实 Registry 只读查询正确确认两个版本标签尚不存在。
- 完整工具矩阵：238 passed，57.86 秒；发布前置门：120 passed，24.67 秒，
  目录/文档/版本/diff/Ruff 同时通过；
  这些工具结果不冒充主线发布通过。

## 范围与发布边界

对应 NFR-04/08/10/16，只修复发布工具判定，不改变策略、费用、账户或数据库 schema。
新分支沿主线版本规则保持 v0.4.51；集成到 main 后由 CI 分配后继版本并重新签收。
六类文档与本记录互相引用。正式镜像、生产部署、双库实际恢复、业务差异和重启验收
必须继续分别完成；本地产品保持用户指定暂停。本次没有新增收益回测。
