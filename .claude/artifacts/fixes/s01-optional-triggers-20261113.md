# S01 入场／出场触发器开放为可选项

2026-11-13，实际对象为 `s01 / custom_mvxxcs01`。本地已经生效，沿用 S02 的原触发器选择区，
支持取消、换选、多选内置和方向匹配的已有自定义信号，以及原信号创建入口。
多选按任一信号命中生效，各自使用保存参数；出场取最早触发事件。
保存、刷新、重新打开策略设置后保留选择，回测高级设置使用相同能力。

清空入场触发器后按基础选股结果入场，已取消的 S01 当日金额、涨幅、振幅及广度条件不继续参与。
清空出场触发器后没有信号卖出，不继续执行原 11:26 强制兜底；回测保留持仓并估值。
前日数值过滤、完整分钟要求和原排序仍在。日线信号只读前交易日完成态，
盘中入场只读 14:45 前已完成事件，出场遵守 T+1。历史冻结策略维持原语义。

## 恢复后的默认策略与参数

默认入场仍为“尾盘建仓_s01”，出场仍为“早盘清仓_s01”，全部 14 项信号参数保持不变。
前日数值过滤、原排序、14:45 确认、14:47～14:49 买入窗口均未改变。

| 信号 | 当前保存参数 |
| --- | --- |
| 尾盘建仓_s01 | 当日成交额 ≥1亿元，涨幅 2%～3%，上下振幅 ≤5.25%，有效证券 ≥1000只，上涨比例 ≥65% |
| 早盘清仓_s01 | 11:15 完整早盘判断；最后15分钟涨幅 ≤-0.3%，并满足低于VWAP ≥0.03%或最高浮盈 ≥2%且高点回落 ≥0.5% |
| 早盘清仓_s01 | 11:15起还检查3分钟跌幅 ≥0.2%且量比 ≥1.5；11:26兜底，下一完成分钟起容量成交，13:30截止 |

选股范围与状态条件、前日数值过滤、策略其他有效设置、全部信号定义及
`s02 / custom_mvxxcp88` 的源码和配置通过独立 API 读回确认保持。

## 本次真实验证

- 最终集成父源码：后端 103 项定向测试通过，包括可选／清空／多选、真实买卖订单、
  前日信号、完成分钟、T+1、旧冻结绑定和费用测试。
- 前端 10 项触发器与普通信号编辑器测试通过，TypeScript 检查和正式前端构建通过。
  集成时纳入此前已修复的普通信号编辑器测试，并保留正式源码的“信号页”名称。
- 既有行情对照 149 日、189 条默认候选与排序一致；
  70 个持仓、280 次出场前缀一致。
  清空入场得到 29751 条基础候选，只做功能核对，未运行新收益回测。
- 实际浏览器 1440／390 像素验证内置和其他信号可选、取消操作、多选保存和刷新、
  清空保存后仍可再次编辑、默认选择恢复，以及回测两个触发器页签。
- 策略页保存请求成功，页面脚本错误为 0；回测页的页面脚本错误、本地 API 错误和
  相关控制台错误均为 0。自动 GitHub 版本查询存在外部 403／CORS 错误，
  未算作本次功能通过项。策略页场景完成后关闭按钮定位超时，原始失败日志保留；
  未重做已通过场景，后续回测页单独继续验收。不是全仓测试或数据完整性背书。

## 本地安装与保留

应用版本保持 `0.4.58`，20 个源码／文档／前端测试文件增量配套安装。
两套镜像分别校验 401 个应用文件及完整构建清单，
本地应用、Worker 与配套 Compute 通过本次健康就绪检查。

| 角色 | 当前镜像 |
| --- | --- |
| Runtime | `sha256:72f6f110a44121e4f0e1c7d9769c5e662ac6ec24659de408c7b4877406505f27` |
| Compute | `sha256:6f3a6cd7bc5d43b09d0deca0711899447c391fcf74447cbd6861532c2738a5d8` |

安装前平台收盘完整性检查自然结束（其任务状态为 failed）；未取消该自然任务，
也未把它称为成功检查。重启自动补缺任务亦记录部分分钟数据源未返回，
保留失败任务和缺口明细，服务就绪不代表行情完全补齐。本次不修改其业务逻辑。
安装后核对源码哈希、运行指针、容器挂载和进程占用，清理本任务不用的旧父镜像及
集成／构建目录；数据、用户成果、未完成开发和审计证据保留。本任务部署备份 0 份。
文件系统可用字节变化为 +88297472，以实际观测为准。

本次仅本地生效，线上未更新；未递增应用版本、未提交／推送代码、未发起新收益回测。

## 页面证据

[策略多选·桌面](file:///Users/bytedance/.local/share/tsp-dev/maintenance/s01-optional-triggers-20261113/strategy-multiple-1440.png) ·
[策略多选·手机](file:///Users/bytedance/.local/share/tsp-dev/maintenance/s01-optional-triggers-20261113/strategy-multiple-390.png) ·
[清空后编辑·桌面](file:///Users/bytedance/.local/share/tsp-dev/maintenance/s01-optional-triggers-20261113/strategy-empty-1440.png) ·
[清空后编辑·手机](file:///Users/bytedance/.local/share/tsp-dev/maintenance/s01-optional-triggers-20261113/strategy-empty-390.png) ·
[默认选择](file:///Users/bytedance/.local/share/tsp-dev/maintenance/s01-optional-triggers-20261113/strategy-default-1440.png) ·
[回测入场](file:///Users/bytedance/.local/share/tsp-dev/maintenance/s01-optional-triggers-20261113/backtest-entry-1440.png) ·
[回测出场](file:///Users/bytedance/.local/share/tsp-dev/maintenance/s01-optional-triggers-20261113/backtest-exit-390.png)

[浏览器回执](file:///Users/bytedance/.local/share/tsp-dev/maintenance/s01-optional-triggers-20261113/browser-verification.json) ·
[独立定义核对](file:///Users/bytedance/.local/share/tsp-dev/maintenance/s01-optional-triggers-20261113/final-definition-verification.json) ·
[最终安装／清理回执](file:///Users/bytedance/.local/share/tsp-dev/maintenance/s01-optional-triggers-20261113/completion-receipt.json)
