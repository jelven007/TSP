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

回归截图与测试证据共同保存在 `reports/<专题-日期>/`，
记录版本、环境、时间和操作；保留的证据见[报告索引](../reports/README.md)。
旧无元数据截图已按[清理记录](../reports/repository-cleanup-20261024/verification.md)移出。
夹具使用小型、合成或已脱敏数据，不提交用户行情、策略、账号、密钥和生产数据库。
性能与持续运行证据必须记录规模、版本、环境和时间窗口，不能用单元测试通过代替。
