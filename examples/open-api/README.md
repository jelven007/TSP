# 开放接口示例 (Open API Examples)

外部程序接入 TSP 核心能力的可运行最小示例 — 纯 Python 标准库, 零第三方依赖。

示例调用当前 Tier A 开放面；完整多租户 Token 由管理员绑定目标租户签发，
`TSP_BASE` 应指向完整 API 或 HTTPS Edge，独立 Gateway 不承载普通 REST。
数据同步、AI 迭代、助手会话及秒级研究接口不属于这些示例的 Token 权限。
端点与能力限制见[当前实现索引](../../docs/architecture.md#capabilities)。

## 准备

1. 启动面板 (或桌面客户端), 进入 **设置 → 开放接口**, 新建 Token 并勾选所需 scope:

   | 脚本 | 所需 scope |
   | :--- | :--- |
   | `01_pull_market.py` | `read:market` |
   | `02_write_ext_data.py` | `write:ext` + `read:ext` |
   | `03_run_backtest.py` | `read:analysis` + `run:backtest` |
   | `04_events_stream.py` | 任意 (票据继承 scope) |

2. 设置环境变量后运行:

   ```bash
   export TSP_BASE=http://127.0.0.1:3018   # 面板实际地址
   export TSP_TOKEN=tsp_xxxx               # 创建 Token 时只显示一次, 立即保存

   python 01_pull_market.py
   ```

## 脚本说明

| 脚本 | 演示能力 |
   | :--- | :--- |
   | `01_pull_market.py` | 标的搜索 (拼音可用) / 日K / 市场总览 |
   | `02_write_ext_data.py` | 程序化写入扩展表行数据 + 读回验证 (数据同台分析闭环) |
   | `03_run_backtest.py` | 策略清单 / 信号回测触发与结果读取 |
   | `04_events_stream.py` | SSE 短期票据 + 事件流 (告警推送) |

## 通用约定

- 认证: `Authorization: Bearer tsp_...` 头; 与面板密码会话并行, 互不影响
- 限流: 每 Token 默认 120 次/分钟, 响应头 `X-RateLimit-Remaining` 实时可见, 超限 429 + `Retry-After`
- 错误语义: 401 Token 无效或已吊销 / 403 scope 不足或端点未开放 / 429 超限
- 机器可读契约: `GET /api/openapi.json?tier=a` — 可直接导入 Postman / 生成客户端
- 完整文档: [docs/features.md → 开放接口](../../操作说明书.md#25-开放接口与-mcp)

## 注意

- `write:ext` 只能向**已配置好的**扩展表写行数据; 表结构 (字段/模式/拉取) 属管理面, Token 不可触及
- 写入的数据会进入策略信号 / 因子 / 回测的数据面, 谨慎授予该 scope
- SSE 票据一次性且 60 秒过期, 断线重连需重新 `POST /api/events/ticket`
- 完整多租户收到 `access_revoked` 时重新权威换票，收到 `events_reset` 时重新读取
  已授权 REST 状态；续接使用最后已确认游标，不重复执行写操作
