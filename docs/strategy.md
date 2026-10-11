# 策略指南

策略是选股引擎、回测、监控的基础。本文介绍策略体系与三种扩展方式。

完整策略开发规范(AI 生成与手写)见 [`backend/app/strategy/prompts/strategy-guide.md`](../backend/app/strategy/prompts/strategy-guide.md)。

---

## 内置策略

**22 个公开内置策略**,每个策略一个独立 Python 文件,通过 `matrix_native` 矩阵协议执行(`backend/app/strategy/builtin/`):

| 类型        | 代表策略                                                 |
| :---------- | :------------------------------------------------------- |
| 趋势 / 形态 | 均线多头 · MA 金叉 · MACD 金叉放量 · 布林突破 · 均线粘合突破 · 平台整理突破 · 放量创60日新高 |
| 量价 / 涨停 | 量价齐升 · 高换手拉升 · 连板股 · 逼近涨停 · 涨停基因活跃股 |
| 反转 / 波动 | 超跌反弹 · 超跌反转 · 新低反转 · 低波动龙头 · 均线回踩反弹 · 缩量回踩 · 强势高开 · MACD 零下回升 · 长下影反击 · RSI 中轴回踩 |

2026-10-11 精简了三个重复变体：

| 已删除 ID | 家族代表 | 原因 |
| :--- | :--- | :--- |
| `trend_breakout` | `breakout_new_high_60d` | 同为60日收盘新高加放量，主要区别是门槛、评分与基础过滤 |
| `limit_up_momentum` | `consecutive_limit_ups` | 连板阈值变体；代表策略的 `min_boards` 可表达首板与多板研究 |
| `broken_board_recovery` | `consecutive_limit_ups` | 实际仅筛放量涨停，没有实现断板及反包历史结构 |

代表策略默认参数不变，旧 ID 不自动重定向。历史回测记录保留；重跑已删除策略需使用原版本源码。
升级前应检查自定义叠加策略、监控和模拟盘自动规则是否引用旧 ID，显式解除或改写后再热重载，
避免不同条件被静默替换。两种 RSI 超跌、两种均线回踩以及不同突破机制仍分别保留，
因为其实际候选与触发时点存在明显差异。策略保留不等于已经验证可盈利。

### 买入资格与退出优先

- `exclude_st=true` 在信号日筛选，并在实际成交日按历史证券状态再次复核。
  即使信号后停牌、直到复牌才成交，也使用复牌日状态；已持仓证券仍可估值和卖出。
- `exclude_new_days=N` 按正式上市日期计算自然日年龄，上市当天为 0，
  年龄达到 N 才允许入场。设为 0/空值或关闭基础过滤可禁用；开启时上市日期缺失不放行。
  上市日期取证券维表的正式 IPO 日期，不能用行情窗口起点或旧 ST 回填记录的首条行情日期。
  此项仅适用于股票，ETF/指数自动中和。
- 同一根信号 K 线上入场和退出同时成立时，退出优先，先删除入场再评分、延迟成交。
  该规则用于矩阵策略、Polars 策略及叠加回测；退出通知继续保留。
- 成交日复核仅使用证券资格，不用当天收盘价、成交额等未来信息复核次日开盘买点。
  分钟回放中的价格指标仍来自 T-1，证券资格使用 T 日状态。
- 历史 ST 状态不完整时，兼容模式仍保留原有回退并报告治理缺口；
  `strict_research=true` 拒绝状态覆盖不足的回测。成交资格拦截计入 `execution.buy_ineligible`。
  矩阵磁盘缓存升级为 v7，上市日期变更会使缓存失效。
  回测配置记录 `entry_policy_version`；旧规则产生的选股缓存和“今日曾命中”缓存自动失效。

内置目录 `backend/app/strategy/builtin/` 还包含一个仅供挖掘 worker 使用的受控因子排名研究模板。它不出现在普通选股列表，也不能通过普通策略 API 直接运行或保存 override；挖掘结果发布时会生成独立策略。详见 [因子与策略挖掘](./mining.md)。

内置目录 `backend/app/strategy/builtin/` 由项目维护,**AI 生成的策略不会落入此目录**。

---

## 扩展策略的三种方式

### 🎛️ 方式一:自定义信号(不写代码)

在选股页 UI 上用 `字段 + 操作符 + 阈值` 组合,编译成 Polars 表达式热加载。适合:

- 快速验证一个简单的筛选思路(如 `RSI < 30 AND 量比 > 2`)
- 不熟悉 Python 但想自定义筛选条件

底层实现在 `backend/app/strategy/custom_signals.py`。

### 🤖 方式二:AI 生成

一句话描述思路,LLM 读取精简运行时指南生成完整策略文件:

1. **配置 AI 接口**(留空即关闭,见 [configuration.md → AI](./configuration.md#ai可选)):
   ```ini
   AI_PROVIDER=openai_compat
   AI_BASE_URL=https://api.deepseek.com/v1
   AI_API_KEY=sk-...
   AI_MODEL=deepseek-chat
   ```
2. 在选股页打开「AI 策略生成器」,用自然语言描述你的策略思路
3. 前端流式接收生成代码,后端经 `ast` 安全校验(禁止 import os/sys/subprocess 等危险模块)后返回结果
4. 保存后落入 `data/strategies/ai/`,文件名/ID 用 `ai_` 前缀

生成策略相关提示词位于 `backend/app/strategy/prompts/`:

- `strategy-guide-compact.md` — AI 运行时精简指南(用于降低长请求超时概率)
- `strategy-guide.md` — 完整策略开发规范(供人工开发和详细参考)
- `strategy-builder-step2.md` — 步骤 2 提示词模板(修改已有策略)
- `strategy-example.md` — 从零创建强势反包策略的三步演示

> 💡 **文件与范围铁律**:AI 生成的策略只生成一个 `.py` 文件,只 `import polars as pl`,绝不修改 `backend/`、`docs/`、`frontend/` 等现有文件。

生成之后的持续改进(假设—验证—判定闭环),见 [AI 策略迭代指南](./strategy-iteration.md)。

### 📝 方式三:自定义编写 / 代码迁移

可以在选股页「自定义编写」中直接编辑策略代码并保存,新建自定义策略会落入 `data/strategies/custom/`,文件名/ID 用 `custom_` 前缀。也可以手动把已有策略改写为 Polars 文件后放入该目录,引擎会自动发现。

手写策略需遵循 [`strategy-guide.md`](../backend/app/strategy/prompts/strategy-guide.md) 的文件结构(META / basic_filter / scoring / ENTRY_SIGNALS / filter 等),完整规范见该文档。

---

## 策略文件结构(简述)

一个策略 `.py` 文件通常包含:

| 部分 | 作用 |
| :--- | :--- |
| `META` | 策略元信息(名称、参数、方向等),用户可在 UI 调整阈值 |
| `basic_filter(df, params)` | 模式 A:单日过滤,返回 `pl.Expr` |
| `filter_history(df, params)` | 模式 B:历史窗口过滤,返回 `pl.DataFrame`(配 `LOOKBACK_DAYS`) |
| `scoring` | 评分权重,总和 = 1.0 |
| `ENTRY_SIGNALS` / `EXIT_SIGNALS` | 进出场信号列(回测用) |

完整字段说明与示例见 [`strategy-guide.md`](../backend/app/strategy/prompts/strategy-guide.md)。

---

## 新增内置策略(贡献者)

如果你想为项目贡献一个内置策略:在 `backend/app/strategy/builtin/` 参照现有文件实现 `StrategyDef`,引擎会自动发现并加载。欢迎提交 PR。
