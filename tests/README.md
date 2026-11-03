# 测试目录

功能A–I、DFX3.1–3.6和签收模板见[生产验收](../docs/testing.md#acceptance)。
文档门禁和失败用例在`tests/tooling/test_documentation_contract.py`，不替代业务或产物验收。

测试按发布单元归属，仓库入口见[工程目录](../docs/reference/engineering.md#layout)。

| 位置 | 覆盖范围 | 从仓库根执行 |
| --- | --- | --- |
| `tests/tooling/` | 版本、工作流、仓库工具与研究辅助命令 | `make test-tooling` |
| `backend/tests/` | API、服务、Provider、数据、租户、回测和打包契约 | `make test-backend` |
| `frontend/src/**/*.test.*` | 页面、组件、查询与交互 | `make test-frontend` |
| `mcp-server/smoke_test.py` | 已运行 TSP 的 MCP／开放接口接入 | 按 [MCP 说明](../mcp-server/README.md)配置后执行 |
| `backend/scripts/acceptance/` | 隔离候选、数据库、SSE、恢复与持续运行验收 | 按[测试与验收](../docs/testing.md)准备环境与参数 |

依赖先按 `uv.lock`、`pnpm-lock.yaml` 安装；Makefile 测试命令不会自动同步后端依赖。
`tests/tooling/conftest.py` 为仓库 CLI 辅助模块设置本地导入路径，保留
`backend/scripts` 的 Python 包归属，避免两个脚本目录同名造成误导入。

## 按领域定位现有测试

| 行为 | 权威实现入口 | 现有测试入口 |
| --- | --- | --- |
| K线仓库和缓存依赖 | `backend/app/tickflow/repository.py` | `backend/tests/test_repository_cache_dependencies.py`、`test_repository_index.py` |
| 完整当日池和盘前调度 | `backend/app/strategy/daily_pool.py`、`services/daily_pool_schedule.py` | `backend/tests/test_daily_stock_pools.py`、`test_daily_pool_schedule.py` |
| 尾盘信号与时间窗口 | `backend/app/strategy/late_session_strength.py` | `backend/tests/test_late_session_strength.py` |
| 公共发布绑定与回收 | `backend/app/multitenancy/publication.py` | `backend/tests/test_public_binding_sync.py` |
| 策略信号选择 | `frontend/src/components/screener/SignalPicker.tsx` | 同目录`SignalPicker.test.tsx` |
| 文档链接和需求基准 | `scripts/check_docs.py` | `tests/tooling/test_documentation_contract.py` |

先按源码符号定位对应测试，既有后端测试保持原路径，前端测试继续就近维护。
新增后端专题测试沿已有领域目录组织，不按文件名猜测后批量搬迁。

回归截图与测试证据共同保存在 `reports/<分类>/<专题-日期>/`，
记录版本、环境、时间和操作；保留的证据见[报告索引](../reports/README.md)。
旧无元数据截图已按[清理记录](../reports/operations/repository-cleanup-20261024/verification.md)移出。
夹具使用小型、合成或已脱敏数据，不提交用户行情、策略、账号、密钥和生产数据库。
性能与持续运行证据必须记录规模、版本、环境和时间窗口，不能用单元测试通过代替。
