# 集成示例

可执行样例统一放在 `examples/`，协议规范保存在
[接入参考](../docs/reference/integrations.md)。样例使用合成数据或调用显式配置的实例。

| 目录 | 用途 | 前置条件 |
| --- | --- | --- |
| [custom-data-source](custom-data-source/README.md) | 本地 HTTP 数据源的日K、复权与实时接口 | 按后端冻结锁安装 FastAPI、Uvicorn；端口3021空闲 |
| [open-api](open-api/README.md) | 外部行情查询、扩展数据、回测和事件流 | 已运行的 TSP、对应 scope 的 Token；按样例说明设置环境 |

从仓库根运行 HTTP 样例：

```bash
uv run --project backend --frozen --no-sync python examples/custom-data-source/mock_server.py
```

不要把真实 Token、行情导出、账号或运行配置保存到此目录。
MCP 是独立发布包，接入冒烟入口仍见 [mcp-server](../mcp-server/README.md)。
