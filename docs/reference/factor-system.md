# 因子平台：实现、技术契约与后续设计

核对日期：2026-10-23；合并日期：2026-10-24。注册、DSL、组合与服务端统计已有实现；完整历史宇宙、中性化、自动衰减和自动权重仍是目标。当前请求响应以[factors API](../../backend/app/api/factors.py)与[契约测试](../../backend/tests/test_factor_api.py)为准，不能照目标示例导入不存在的模块。

- [1. 当前实现与使用闭环](#factor-current)
- [2. 后续技术设计](#factor-target)
- [3. 兼容与验证](#factor-validation)

<a id="factor-current"></a>
## 1. 当前实现与使用闭环

<a id="factor-当前实现对照2026-10-23"></a>
### 当前实现对照（2026-10-23）

| 层 | 已有实现与事实源 | 尚未实现/与原提案的区别 |
| --- | --- | --- |
| L-REG | [registry.py](../../backend/app/factors/registry.py)：77 项内置目录，元数据/依赖/预热兼容视图与私有注册上下文 | 原 `expr_factory` 完整 schema 未原样实现；单位默认 `none`，不按猜测认证单位 |
| L-DSL | [dsl.py](../../backend/app/factors/dsl.py)：25 个算子、编译校验及两阶段求值 | 当前为单文件；不能导入提案中的 `dsl/` 子包 |
| L-CMP | [store.py](../../backend/app/factors/store.py) 与评分物化：自定义/复合定义、版本、生命周期、组合权重 | 没有独立 `composite.py` 模块；自动 ICIR 权重模式未交付 |
| L-INF | [stats_v2.py](../../backend/app/backtest/stats_v2.py)：Newey-West、BH-FDR、DSR，已接批量检验 | 没有提案中的 `factors/stats.py`；统计显著性不认证可交易收益 |
| L-UNI / L-NEU | 复用现有证券/资产过滤、历史状态与财务时点处理 | 通用 `UniverseSpec`、完整行业/规模中性化与提案模块未实现 |
| L-MET | 已有 stats_v2 与回测统计，各路径保留明确口径 | 提案统一 `factors/metrics.py` 及完整 metrics_v2 schema 不作为现有接口 |
| 页面与生命周期 | `/factors` 的检验、因子库、编辑器、组合、挖掘；draft/active/watch/retired | 无巡检 tab 和后台衰减定时任务 |
| 数据可信度 | 已有证券 SCD2、财务观察版本、固定研究输入与未知比例 | 完整历史退市池、原财务修订可得时间及历史成分未认证 |

当前 HTTP 请求/响应以 [factors API](../../backend/app/api/factors.py) 及
[factor API 测试](../../backend/tests/test_factor_api.py)为准；
页面、参数与研究边界见[总体方案](#factor-current)、
[研究配置](research.md#research)、[当前功能索引](../architecture.md#capabilities)。
下列章节保留原设计依据和路线图，不覆盖上述现行契约。

<a id="factor-product-1-架构总览两线模型"></a>
### 1. 架构总览：两线模型

<a id="factor-product-11-模型"></a>
#### 1.1 模型

```text
研究线（发现问题）                     交易线（执行战法）
┌─────────────────────────┐          ┌─────────────────────────┐
│ 因子库(注册表)            │          │ 策略引擎(战法)            │
│   ↑ 检验(IC/IR/多空)      │          │   时机/买卖规则/仓位/风控   │
│   ↑ 公式DSL(自定义因子)    │          │   ↑ 评分选股               │
│   ↑ 组合因子(加权合成)     │          │   ↑ 验证(鲁棒性/步进)       │
│ 挖掘(自动搜索因子组合)     │          │ 回测(策略绩效)             │
└───────────┬─────────────┘          └───────────↑─────────────┘
            │        唯一桥梁：策略 scoring 配置          │
            └────────── 因子(组合)权重 ──────────────────┘
```

- **因子层**回答"此刻全市场谁排前面"（截面排序）；**战法层**回答"何时进、买几只、多重仓、何时出、怎么止损"。
- 两线的数值计算通过**评分配置**结合：`{"scoring": {"cf_hotmom": 0.6, ...}}`。因子不会"变成"策略，因子给策略供货。
- 【现状】桥已存在：`backend/app/strategy/scoring.py:13` `VIRTUAL_SCORING_DEPENDENCIES`（35 个虚拟因子依赖声明）、`scoring_value_expr`（按名取值表达式）、`mining.py:342` 挖掘候选产物本身就是 `scoring = dict(zip(factor_names, weights))`。
- 当前研究线已统一到 `/factors`；`/backtest?tab=factor` 与 `/mining`
  仅作兼容跳转。四条因子/策略桥已实现，不应把上面的概念示意当作唯一交互入口。

<a id="factor-product-12-各能力现状对照"></a>
#### 1.2 各能力现状对照

| 能力 | 现状 | 本方案落点 |
| --- | --- | --- |
| 因子检验（批量/单因子） | 已实现 Rank IC、分层、多空、分年/环境与服务端统计；内置目录 77 项，动态因子另计 | `/factors?tab=inspect` |
| 因子注册 | `factors/registry.py`，兼容导出 `FACTOR_COLUMNS`；动态私有注册隔离 | `/factors?tab=library` |
| 公式配置 | `factors/dsl.py`，25 个算子；校验、试算和草稿保存 | `/factors?tab=editor` |
| 自动挖掘 | 受控组合搜索与嵌套验证，显式保存/发布，周度默认关闭 | `/factors?tab=mining`；[挖掘口径](research.md#mining) |
| 因子→策略 | 评分、单因子排名策略、触发器与归因四条桥；复合因子使用共享物化路径 | `/factors?tab=composite` 及策略/回测 |
| 生命周期管理 | draft/active/watch/retired 已实现；自动衰减/巡检未交付 | 无巡检 tab |

<a id="factor-ui"></a>
### 当前页面、配置与生命周期

`/factors?tab=inspect|library|editor|composite|mining` 为五个现有入口；
`/backtest?tab=factor`、`/mining` 与旧挖掘链接只作保参兼容跳转。
候选通过共享抽屉保存和载入复测；操作步骤统一见[操作说明书第10章](../../操作说明书.md#10-因子)。

| 步骤 | 当前行为与约束 |
| --- | --- |
| 选择与检验 | 77项内置目录，动态自定义/复合/扩展字段另计；预设替换选择，手动改选回自定义；当前按股票计算 |
| 判读 | IC方向是当前样本结果；Newey-West、BH-FDR及服务端判读不认证未来收益；理论多空不等于可执行做空 |
| 编辑 | 25算子按时序/截面/工具分组；双语字段插入；替换非初始公式先确认；校验返回位置、依赖与预热 |
| 试算与保存 | 公式最长2,000字符，试算默认40日、范围20–120；创建/改公式前以最近40交易日检查非全空；失败保留草稿 |
| 组合 | 2–8个已知成员，权重非零，拒绝未知、自引用和循环；复用共享物化路径；没有自动ICIR配权 |
| 生命周期 | draft/active/watch/retired由用户显式选择；公式变化升版本并回草稿，元数据变化保留状态；没有自动检验门禁或后台衰减 |
| 删除与引用 | 内置只读；发现引用默认拒删并返回引用；有权强制删除不自动修复下游 |
| 四条桥 | 因子生成排名策略、策略创建因子信号、AI信号使用因子白名单、回测入场快照归因；矩阵排名类暂无归因 |

加载、运行、无数据、失败和无权限分别展示；运行保留上一份结果，失败不冒充零值。
因子目录、预设数量和接口字段以运行时注册表为准，不保留旧61项线框作为第二份事实源。

<a id="factor-target"></a>
## 2. 后续技术设计

以下保留原后端设计中的 schema、公式、数据集、缓存与性能约束，**统一标为设计参考，并非当前 API**。其中“现状”和代码行号对应`main@2ce8b4b1`（2026-09-04）；实现差异以上一节为准。新增接口沿用当前角色/租户/scope，不沿用早期单密码授权假设。性能预算是待实现验收线，旧模块路径只表达拟建职责。

<a id="factor-spec-0-设计目标"></a>
### 0. 设计目标

1. **因子定义单一事实源**：公式、元数据、计算、测试同处一地，可审计、可版本化。
2. **研究结论可辩护**：宇宙可解释、风险调整显式、统计检验完备、指标口径唯一。
3. **策略接入零摩擦**【用户核心诉求】：因子研究成果（含用户自定义因子、复合因子、挖掘产物）以统一形态被策略评分、选股、回测、监控四端消费，一处定义、处处生效。
4. **不推倒重来**：挖掘框架（purge/embargo/嵌套样本外）、enriched 列体系、虚拟评分机制全部保留，只做补层和收口。

---

<a id="factor-spec-1-分层总览与现状映射"></a>
### 1. 分层总览与现状映射

| 层 | 目标模块 | 现状代码 | 动作 | 分级 |
| --- | --- | --- | --- | --- |
| L-REG 因子注册表 | `app/factors/registry.py`【设计】 | `backtest/factor.py:36` FACTOR_COLUMNS + `strategy/scoring.py` 虚拟因子 + `indicators/pipeline.py` ENRICHED_COLUMNS | 三处合一收口 | L3（重构） |
| L-DSL 表达式层 | `app/factors/dsl/`【设计】 | 无（`strategy/custom_signals.py` 白名单模式可借鉴） | 新增 | L2 |
| L-UNI 宇宙构建 | `app/factors/universe.py`【设计】 | 无（tradable/limit_up_locked/listing_date 素材已存在） | 新增 | L2 |
| L-NEU 风险调整 | `app/factors/neutralize.py`【设计】 | 无（`get_index_daily`、行业 preset、`share_capital.py` 素材已存在） | 新增 | L2 |
| L-INF 统计检验 | `app/factors/stats.py`【设计】 | 无 | 新增 | L2 |
| L-MET 指标统一 | `app/factors/metrics.py`【设计】 | `backtest/engine.py` 三种 Sharpe（:2899/:2993/:3116） | 收敛 + 版本化 | L3（热点） |
| L-CMP 复合因子→策略 | `app/factors/composite.py`【设计】 | `strategy/scoring.py` 虚拟评分字段机制【现状·已可用】 | 扩展既有机制 | L2→L3 接线 |
| 数据契约 | provider dataset 声明 | 无 ST 历史/退市股/点时行业 | 新增 dataset | L1（YAML）+provider 实现 |

模块落点说明：新建 `app/factors/` 包而不是塞进 `backtest/`，因为因子目录、宇宙、中性化被选股（`strategy/`）、回测（`backtest/`）、挖掘（`backtest/mining.py`）三方消费，放任一方都会造成反向依赖（违反 CONTRIBUTING 2.3 模块边界）。

---

<a id="factor-spec-2-因子注册表l-reg"></a>
### 2. 因子注册表（L-REG）

<a id="factor-spec-21-现状问题"></a>
#### 2.1 现状问题

因子元数据目前分散在四处，互相漂移无感知：

| 位置 | 内容 | 缺陷 |
| --- | --- | --- |
| `backtest/factor.py:36-109` | 62+ 因子目录（id/label/group/desc） | desc 是自然语言，与计算无绑定 |
| `strategy/scoring.py:13-51` | VIRTUAL_SCORING_DEPENDENCIES | 虚拟因子的依赖声明，但与因子目录是两套清单 |
| `strategy/scoring.py:80+` | `scoring_value_expr` | 虚拟因子的 Polars 表达式，硬编码 if-else 分发 |
| `strategy/scoring.py:53-66` | `_ROLLING_SCORING_WARMUP` | 预热窗口第三套清单 |

<a id="factor-spec-22-factorspec-完整-schema设计"></a>
#### 2.2 FactorSpec 完整 schema【设计】

```python
@dataclass(frozen=True)
class FactorSpec:
    id: str                      # 全局唯一，^f_[a-z0-9_]{1,40}$；内置因子保持现有列名不变（如 momentum_20d）
    version: int                 # 因子语义版本；公式变更必须 +1，进缓存键
    label: str                   # 中文显示名
    group: str                   # 展示分组（沿用现有：动量/均线偏离/超买超卖/趋势/波动率/量价/…）
    kind: Literal["base", "virtual", "composite", "custom"]
                                # base: 已物化在 enriched parquet
                                # virtual: 按需由 base 列编译计算（如 ma5_bias）
                                # composite: 复合因子（见 §8）
                                # custom: 用户 DSL 因子（见 §3）
    expr_factory: Callable[[frozenset[str]], pl.Expr | None] | None
                                # virtual/custom 的计算：输入可用列集合，依赖不完整返回 None（fail-closed）
    formula_text: str            # 人类可读公式；virtual 由表达式自动生成，base 手写并配特征化测试锁定
    dependencies: frozenset[str] # 展开到 enriched base 列（自递归展开 composite/custom 依赖）
    direction: Literal["high", "low", "none"]
                                # 预期信号方向；进复合因子默认权重与 UI 排序展示
    unit: Literal["ratio", "pct", "score", "count", "days", "currency", "none"]
                                # 单位口径，UI 格式化与 sanity check 用（禁止"数值<1 乘 100"启发式）
    warmup_bars: int             # 历史窗口需求（交易日数）；对齐 _ROLLING_SCORING_WARMUP 语义
    pit: bool                    # 是否点时数据依赖（财务因子 = True）
    pit_source: Literal["financial_announce", "share_capital_announce", "none"]
    asset_types: frozenset[Literal["stock"]]
    incremental_safe: bool       # 盘中增量路径（pipeline.py:1795）能否复算；False 则盘中不含该列
    scale_free: bool             # 跨标的可比（可直接截面排序）；如 atr_14 原值 = False，atr_pct = True
    null_policy: Literal["keep", "drop_row"]
                                # 研究路径默认 keep（不填零，沿用 fundamentals.py 纪律）
    stability: Literal["stable", "experimental", "deprecated"]
    tags: tuple[str, ...]        # 风格标签："momentum"/"value"/"size"/"lottery"/"liquidity"/…
```

注册表 API（仅内部 Python 接口，不新增 HTTP）：

```python
register_factor(spec)                      # 启动期注册；重复 id 且 version 未增 → 拒绝启动（fail-closed）
get_factor(fid) -> FactorSpec
all_factors(asset_type=None, stable_only=False) -> list[FactorSpec]
factor_dependencies(fids) -> frozenset[str] # 递归展开
factor_value_exprs(available_cols) -> dict[str, pl.Expr | None]
```

<a id="factor-spec-23-迁移策略设计"></a>
#### 2.3 迁移策略【设计】

1. **特征化测试先行**：固定样本（≥50 只股票 × 含除权日、停牌日、涨跌停日的窗口）快照当前全部 62+ 因子在 enriched 与 `scoring_value_expr` 两条路径的输出，重构后断言逐位一致。扩展 `backend/tests/backtest/test_factor_library_v2.py`。
2. `VIRTUAL_SCORING_DEPENDENCIES`、`scoring_value_expr` 的 if-else 分发、`_ROLLING_SCORING_WARMUP` 逐一改读注册表，**函数签名不变**（`scoring.py` 对外契约保持）。
3. `FACTOR_COLUMNS` 改由注册表生成，`factor.py` 对外常量保留为兼容别名。
4. desc 公式与 `formula_text` 不一致处，以特征化测试输出的实际计算为准修正文档。

<a id="factor-spec-24-因子分类学与补全清单设计"></a>
#### 2.4 因子分类学与补全清单【设计】

现有 11 组保留；补全以下专业常用因子（标注数据依赖，缺数据不注册、不静默）：

| 族 | 建议新增 | 公式要点 | 依赖 |
| --- | --- | --- | --- |
| 动量 | 特质动量 `f_idio_mom_20d` | 个股日收益对基准收益回归残差的 20 日累计 | 指数日K（已有） |
| 动量 | 52 周新高接近度 `f_near_high_52w` | close / 250 日最高 close − 1 | 已有 |
| 反转 | 短期反转 `f_rev_5d` | −momentum_5d（direction=low 的语义化封装） | 已有 |
| 波动 | 已实现波动偏度差、高低频波动分解 | 简化：`f_vol_ratio_short_long` = vol_5d/vol_60d | 已有 |
| 波动(条件) | 条件波动率 `f_ewma_vol` | RiskMetrics EWMA(λ=0.94) 条性日波动年化；比等权 rolling_std 对近端冲击响应更快，低成本低争议 | 已有（`ewm_std` 向量化） |
| 波动(条件) | 波动的波动 `f_vol_of_vol_60d` / 波动区制 `f_vol_regime` | 波动率的滚动 std / EWMA 波动 ÷ 长期波动；区分"高波市场"与"波动突变"，A 股风格切换敏感因子 | 已有 |
| 波动(条件) | GARCH(1,1) 条件波动 `f_garch_vol` | **标记 experimental、按需实现**：逐 symbol 递归拟合与全向量化管线冲突，若引入必须走 `numba_runtime.py`【现状】路径或 numba/arch 依赖，先以 EWMA 交付（日频下 EWMA ≈ GARCH 的 90% 价值） | 已有 + numba |
| 量价 | 量价背离 `f_pv_divergence_20d` | −vol_price_corr_20d 语义化 | 已有 |
| 流动性 | 非流动性变化 `f_amihud_chg` | amihud_20d / amihud_60d − 1 | 已有 |
| 规模 | 流通市值对数 `f_log_float_mv` | ln(历史流通股本 × raw_close)【点时股本，share_capital.py 已有】 | 已有 |
| 价值 | `f_ep_latest`、`f_ep_ttm` | 1/PE 口径（E/P 比 PE 统计性质更好）；ttm 需财务四表滚动 | 财务（已有）；ttm 需扩展 |
| 质量 | 应收/存货增速差、商誉/净资产 | 财务表字段 | 财务（需字段核对） |
| 涨停 | 首板/连板区分、炸板后回封率 | 基于 consecutive_limit_ups、炸板列 | 已有 |
| 财务 | SUE（盈余惊喜） | (E_t − E_{t-4}) / σ(ΔE, 4期)，公告日口径 | 财务历史（已有 `_merge_report_history`） |

---

<a id="factor-spec-3-因子表达式层l-dsl"></a>
### 3. 因子表达式层（L-DSL）

<a id="factor-spec-31-语法与算子表设计"></a>
#### 3.1 语法与算子表【设计】

表达式 = `expr ::= operand | expr op expr | func(expr[, expr[, const]])`；中缀 + 函数调用，无变量赋值、无循环。

**操作数**：基准列（open/high/low/close/volume/amount/turnover_rate/prev_close/raw_close）、白名单指标列（注册表中 base 因子）、已注册因子 id（virtual/composite/custom，递归内联展开）、数值常量。

**时序算子**（`over("symbol")`，窗口 n ∈ [2, 512]，全部只向后看）：

| 算子 | 语义 | Polars 编译 |
| --- | --- | --- |
| `ts_mean(x,n)` / `ts_std(x,n)` / `ts_sum(x,n)` | 滚动均值/样本标准差/求和 | `rolling_mean/std/sum(n)` |
| `ts_max(x,n)` / `ts_min(x,n)` | 滚动极值 | `rolling_max(n)` / `rolling_min(n)` |
| `ts_delta(x,n)` | x − ts_delay(x,n) | `x - x.shift(n)` |
| `ts_delay(x,n)` | n 期前的值（n ∈ [1, 512]，**禁止负数**——负数即未来函数，编译期报错） | `x.shift(n)` |
| `ts_rank(x,n)` | 当期值在滚动窗口内的分位 | `rolling_rank(n)`【已验证：polars 1.40.1 存在且行为正确，[§17](../../reports/document-consolidation-20261024/historical-evidence.md#factor-history-17-验证附录本设计的验证记录)】 |
| `ts_zscore(x,n)` | (x − ts_mean)/ts_std | 组合表达式 |
| `ts_corr(x,y,n)` / `ts_cov(x,y,n)` | 滚动相关/协方差 | 顶层函数 `pl.rolling_corr(x,y,window_size=n)` / `pl.rolling_cov`【已验证：Expr 上无此方法，必须走顶层函数，[§17](../../reports/document-consolidation-20261024/historical-evidence.md#factor-history-17-验证附录本设计的验证记录)】 |
| `ts_quantile(x,n,q)` | 滚动分位（q ∈ (0,1) 常量） | `rolling_quantile` |
| `decay_linear(x,n)` | 线性衰减加权均值（近端权重大） | 手写权重组合表达式 |

**v1 不提供的时序算子及原因**：`ts_argmax/ts_argmin`——Polars 无向量化实现（`rolling_map` 为逐窗 Python 回调，违反向量化约束，已验证 Expr 无 `rolling_arg_max`）；"距极值天数"类需求以具体因子的组合表达式实现（如 `distance_to_high_60d` 模式），确有高频需求再经 numba 扩展。

**截面算子**（按日期分组，逐日横截面）：

| 算子 | 语义 | 说明 |
| --- | --- | --- |
| `rank(x)` | 横截面百分位排名 ∈ (0,1] | null 不参与排名 |
| `zscore(x)` | 横截面 (x−μ)/σ | σ=0 → null |
| `winsorize(x,k)` | 截尾至 μ±kσ（k ∈ [1,6] 常量，默认 3） | 截面口径 |

**算术/工具**：`+ − * /`（除零 → null）、`log abs sign sqrt min max power(x,c) clamp(x,lo,hi)`、三元 `if_else(cond, a, b)`、比较与逻辑 `> >= < <= == != and or not`（产出布尔，配合 if_else）。

<a id="factor-spec-32-校验规则编译期全部强制设计"></a>
#### 3.2 校验规则（编译期全部强制）【设计】

1. 标识符必须在基准列/白名单/已注册因子内，否则报错（防注入，沿用 `custom_signals.py` 白名单哲学）。
2. `ts_delay`/`ts_delta` 的 n ≥ 0；任何窗口 n ∈ [2, 512]；`power` 指数 |c| ≤ 4；AST 深度 ≤ 12；表达式 token 数 ≤ 200。
3. 常量折叠后若产生 `x/0` 类静态除零 → 编译失败。
4. 依赖列集合 = 递归展开；warmup_bars = max(各 ts 算子窗口)；超出即注册表标记，研究 UI 提示所需历史长度。
5. 产出类型必须为数值或布尔（布尔经 `cast` 视为 0/1）。
6. **禁止未来引用的总闸**：所有时序算子 shift 语义已内建，语法层不存在负 shift；code review checklist 补一条"新增算子必须只向后看"。

<a id="factor-spec-33-编译流水线设计"></a>
#### 3.3 编译流水线【设计】

`text → tokenizer → Pratt 解析 → AST → 语义检查(§3.2) → 依赖/预热推导 → Polars Expr 工厂`。产出缓存（表达式文本 → 编译产物 LRU，键含依赖列版本）；编译失败返回结构化错误（位置 + 原因），不抛裸异常。

<a id="factor-spec-34-形式文法ebnf与错误码目录设计"></a>
#### 3.4 形式文法（EBNF）与错误码目录【设计】

```ebnf
expr        = or_expr ;
or_expr     = and_expr { "or" and_expr } ;
and_expr    = cmp_expr { "and" cmp_expr } ;
cmp_expr    = add_expr [ (">" | ">=" | "<" | "<=" | "==" | "!=") add_expr ] ;
add_expr    = mul_expr { ("+" | "-") mul_expr } ;
mul_expr    = unary { ("*" | "/") unary } ;
unary       = "-" unary | primary ;
primary     = NUMBER | IDENT | func_call | "(" expr ")" ;
func_call   = IDENT "(" [ arglist ] ")" ;
arglist     = expr { "," expr } ;
(* IDENT：基准列/白名单指标列/已注册因子 id/算子名；NUMBER：十进制与负号经 unary 处理 *)
```

运算符优先级由产生式层级固定（or < and < 比较 < 加减 < 乘除 < 一元负号 < 原子），与 Python/JS 语义一致，降低用户迁移成本。

**错误码目录**（编译与运行校验的唯一错误词汇表，API/编辑器/UI 共用）：

| 码 | 含义 | 触发 |
| --- | --- | --- |
| E001 | 未知标识符 | IDENT 不在白名单/注册表 |
| E002 | 未知函数 | 函数名不在算子表 |
| E003 | 参数数量/类型不符 | 算子签名不匹配（含常量参数位置） |
| E004 | 窗口越界 | n∉[2,512] 或 q∉(0,1) |
| E005 | 负 shift | ts_delay/ts_delta 的 n<0 |
| E006 | 嵌套深度超限 | AST 深度>12 |
| E007 | 规模超限 | token 数>200 |
| E008 | 静态除零 | 常量折叠检出分母恒 0 |
| E009 | 产出类型非法 | 非数值/布尔 |
| E010 | power 指数越界 | \|c\|>4 |
| E011 | winsorize k 越界 | k∉[1,6] |
| E012 | 循环引用 | 因子依赖成环（含自定义因子链） |
| E013 | 依赖列不可用 | 面板缺列（运行时） |
| E014 | 语法错误 | 解析失败（附位置） |
| E015 | 预热不足 | warmup > 研究窗口（运行时） |
| E016 | 常量表达式 | 无任何标识符，拒绝保存 |

错误响应统一结构：`{"code": "E001", "message": "未知标识符: clos", "position": {"offset": 12, "line": 1}, "detail": {...}}`。

<a id="factor-spec-35-用户因子生命周期与存储设计"></a>
#### 3.5 用户因子生命周期与存储【设计】

- 存储路径：`data/user_data/custom_factors/*.json`（对齐 custom_signals 目录约定），schema：

```json
{
  "id": "uf_my_rev",              // ^uf_[a-z0-9_]{1,40}$，前缀与 csg_ 同哲学
  "version": 1,
  "label": "我的反转因子",
  "formula": "rank(-ts_sum(change_pct, 5))",
  "direction": "low",
  "description": "5 日累计涨幅的截面倒数",
  "created_at": "2026-09-04T00:00:00",
  "updated_at": "2026-09-04T00:00:00"
}
```

- 生命周期：草稿（编辑器内试算，不落盘）→ 保存（编译通过 + 试算有非空输出才可保存，fail-closed）→ 引用（策略 scoring / 因子研究 / 复合因子）→ 版本化（公式变更 version+1，旧结果按 version 键隔离）→ 删除（有引用时列出引用方并二次确认，对齐策略删除的 fail-closed 要求）。
- 加载失败的单个文件只禁用该因子并提示，不影响启动与其他因子（对齐 plugins 隔离要求，CONTRIBUTING 第 4 节）。

---

<a id="factor-spec-4-宇宙构建l-uni"></a>
### 4. 宇宙构建（L-UNI）

<a id="factor-spec-41-universespec-完整-schema设计"></a>
#### 4.1 UniverseSpec 完整 schema【设计】

```python
@dataclass(frozen=True)
class UniverseSpec:
    exclude_suspended: bool = True          # 停牌（tradable 矩阵口径，matrix.py:1476+）
    exclude_limit_locked: bool = True       # 调仓时点一字涨停不可买入者（buy_limit_up 口径）
    exclude_st: bool = False                # 非点时（今日名称），开启时报告中必须出现降级注记
    min_listing_days: int = 0               # 次新剔除；listing_date 已入库未使用（api/data.py:765）
    min_amount_quantile: float | None = None  # 流动性过滤：当日成交额截面分位下限 (0,1)
    cap_quantile_range: tuple[float, float] | None = None  # 市值分位区间；依赖 f_log_float_mv
    max_names: int | None = None            # 截面数量上限；排序键由研究上下文显式传入（因子值或复合分，不隐式默认），用于微型宇宙研究
```

<a id="factor-spec-42-执行语义设计"></a>
#### 4.2 执行语义【设计】

- 按日生成 `universe[date] -> set[symbol]`，**每个过滤条件独立短路、独立计数**，产出 `filter_stats`（每日各过滤器剔除数），研究报告展示"宇宙从 5200 → 4980 → 4890"漏斗。
- `universe_id = sha256(canonical_json(spec))[:12]`，进一切下游缓存键。
- 过滤顺序固定（先便宜的列过滤，后需联表的），顺序本身进 canonical_json。
- **as_of 语义**：宇宙内一切判定只用当日及以前数据。ST 例外必须显式标注 `degraded: ["st_not_point_in_time"]` 并在报告 UI 渲染黄条。

<a id="factor-spec-43-数据缺口降级矩阵设计"></a>
#### 4.3 数据缺口降级矩阵【设计】

| 过滤器 | 数据缺失时行为 |
| --- | --- |
| exclude_suspended / exclude_limit_locked | 素材必在（enriched 必算列）；缺失 = 数据本身异常 → fail 报错 |
| exclude_st | instruments 名称缺失 → 过滤器跳过 + 降级注记（不静默假装过滤了） |
| min_listing_days | listing_date 缺失的标的视为"不满足"剔除（保守），计数展示 |
| cap_quantile_range | 历史股本缺失标的退出该过滤（不参与分位），降级注记 |
| 退市股（未来） | 依赖新 dataset（[§9](#factor-spec-9-数据契约扩展provider-dataset)）；无数据源时 universe 定义退化为"当前上市 ∪ 本地历史"，报告中永久注记幸存者偏差警示 |

---

<a id="factor-spec-5-风险调整与基准l-neu"></a>
### 5. 风险调整与基准（L-NEU）

<a id="factor-spec-51-neutralizationspec设计"></a>
#### 5.1 NeutralizationSpec【设计】

```python
@dataclass(frozen=True)
class NeutralizationSpec:
    benchmark: str | None = "000001.SH"    # 上证指数（index_const.py:12【现状】核心四只之一）；扩展指数见 §9
    return_basis: Literal["raw", "excess"] = "excess"   # IC/分层收益口径
    method: Literal["none", "industry_demean", "industry_zscore", "regression_industry_size"] = "none"
    winsorize_sigma: float | None = 3.0    # 因子值截面截尾；None = 不截尾
    # neutralization_id = sha256(canonical_json)[:12]，进缓存键
```

<a id="factor-spec-52-方法规格设计"></a>
#### 5.2 方法规格【设计】

- 超额收益：`r_ex = r_stock − r_bench`（基准同日收益；基准停市日沿用最近交易日，日历由数据轴驱动）。
- `industry_demean`：`f' = f − mean_ind(f)`（THS 行业一级，ext preset 快照）。
- `industry_zscore`：组内标准化 `f' = (f − μ_ind)/σ_ind`（σ=0 组 → null）。
- `regression_industry_size`：`f ~ 1 + 行业哑变量 + log_float_mv` 的残差（逐日 OLS，Polars 表达式实现，n<30 或共线 → 回退 demean + 注记）。
- 固定管线顺序：`宇宙过滤 → winsorize → 中性化 → 标准化(zscore 或 rank)`；顺序进 spec 哈希。
- **行业快照局限**（当前归属回填历史）写入 `degraded` 注记并在报告显示；点时行业表到位后（[§9](#factor-spec-9-数据契约扩展provider-dataset)）仅切换数据源，spec 不变。

<a id="factor-spec-53-报告口径并列设计"></a>
#### 5.3 报告口径并列【设计】

IC 报告同时输出三列：`原始 / 超额 / 超额+中性化`，默认排序以最后一列为准——旧结论可查，新结论更严，不静默替换。

---

<a id="factor-spec-6-统计检验l-inf"></a>
### 6. 统计检验（L-INF）

**依赖原则**：后端当前无 scipy/statsmodels（已验证，[§17](../../reports/document-consolidation-20261024/historical-evidence.md#factor-history-17-验证附录本设计的验证记录)），运行时保持零新增第三方依赖——NW/BH-FDR/DSR 全部以 numpy 手写实现（各约 20-40 行）；statsmodels 仅允许加入 uv dev 依赖组用于测试对拍，不进运行时 import。

<a id="factor-spec-61-统计量精确定义设计"></a>
#### 6.1 统计量精确定义【设计】

| 统计量 | 定义 | 备注 |
| --- | --- | --- |
| Rank IC | 逐日 Spearman(factor_t, fwd_ret_{t→t+h})，现有口径不变（factor.py:740-750） | — |
| IC t 值（朴素） | `t = mean(IC) / (std(IC, ddof=1)/√N)` | 仅作对照展示 |
| IC t 值（NW） | Newey-West HAC 稳健标准误，滞后 `L = h`（h 日前瞻收益使 IC 序列存在 h−1 阶移动平均自相关） | **主口径**；numpy 手写 Bartlett 核加权，测试用固定黄金参考向量 + 可选 dev 组 statsmodels 对拍 |
| ICIR | mean(IC)/std(IC)，已有 | — |
| IC 自相关 & 半衰期 | ACF(1..10)；半衰期 = ACF 首次 < 0.5 的滞后（线性插值）；无收敛 → null | 换手率预期管理 |
| 分层单调性 | Spearman(组序号, 组均超额收益) + 线性趋势斜率 t 值 | 判定"梯子是否成立" |
| 多空 t 值 | 顶组−底组日超额收益序列的 NW t | 滞后 = 调仓周期的收益重叠阶数 |
| BH-FDR q 值 | 对 optimizer/mining 排行榜全体 p 值（每行 = 其 OOS/IS 最优组合的 IC 或收益 t 值双尾 p）做 Benjamini-Hochberg，q_i = min_{j≥i}(N·p_j/j) 单调化 | 排行榜级，不进单因子报告 |
| Deflated Sharpe | Bailey-López de Prado：以试验次数 N（挖掘 trial 预算已计数，mining.py:1041+）与偏度峰度校正 SR₀，DSR = Φ((SR−SR₀)·√(T−1) / √(1−γ̂₃SR+((γ̂₄−1)/4)SR²)) | 挖掘晋升报告展示"考虑搜索后的置信" |
| 覆盖率/换手率 | 已有（factor.py:1123-1166），补充宇宙过滤后口径 | — |

<a id="factor-spec-62-显著性标注约定设计"></a>
#### 6.2 显著性标注约定【设计】

|t| < 1.645 无标注；≥1.645 `*`(10%)；≥1.96 `**`(5%)；≥2.576 `***`(1%)。报告 UI 图标化，q ≥ 0.10 的挖掘候选禁止晋升（现有晋升门槛 mining.py:26-31 之上叠加，未达标给出具体差值）。

<a id="factor-spec-63-版本命名规则与报告完整字段设计"></a>
#### 6.3 版本命名规则与报告完整字段【设计】

**方法论版本命名**（单一规则，全文档统一）：

- `factor_v3` = 本设计交付的因子研究方法论（三口径 + 统计检验 + 宇宙/中性化 spec 进键）；现有 `factor_v2`（factor.py:112【现状】）结果按旧版本读取展示，不重算。
- `metrics_v2` = [§7](#factor-spec-7-绩效指标统一l-met) 指标统一后的口径；与 `factor_v3` 独立演进，报告分别携带。
- 因子个体 `version`（FactorSpec）与研究方法论版本正交：因子公式变更不改方法论版本，反之亦然。

**IC 研究报告完整字段定义**（`POST /api/factor-research/ic` 响应，[§10](#factor-spec-10-api-契约设计) 示例为其节选）：

```text
methodology_version: str                      # "factor_v3"
factor_id / factor_version: str / int
universe_id / neutralization_id: str          # 两 spec 哈希
universe: object                              # 回显生效 UniverseSpec
neutralization: object                        # 回显生效 NeutralizationSpec
date_range: {start, end, rebalance, n_groups}
ic: {mean, std, icir, t_naive, t_newey_west, nw_lag, significance,
     half_life_days|null, acf: float[10], win_rate, coverage, n_days}
ic_decay: [{horizon, ic_mean, icir}]          # 沿用现有 1/3/5 日结构
ic_yearly: [{year, ic_mean, icir, n_days}]    # 沿用现有结构
ic_by_basis: {raw, excess, excess_neutralized} × {mean, t_newey_west}
monotonicity: {spearman, trend_t, verdict}    # verdict ∈ 成立/弱/不成立
groups: [{group, excess_return_annual, nav, turnover, n_names,
          t_stat, avg_name_count}]            # 每组含 t 值
long_short: {annual_return, t_newey_west, max_drawdown, executable_short: false}
turnover_top_group: float
costs: {commission_pct, stamp_tax_pct, slippage_bps, round_trip}
universe_funnel: [{date, raw, after_suspended, after_limit_locked,
                   after_st|null, after_new_listing|null, after_filters}]
degraded: [str]                               # 如 st_not_point_in_time / industry_snapshot
warnings: [str]                               # 非降级类提示（预热边界、覆盖不足等）
```

新增字段全部带默认值，历史（factor_v2）缓存结果缺字段时前端显示为空，不报错。

---

<a id="factor-spec-7-绩效指标统一l-met"></a>
### 7. 绩效指标统一（L-MET）

<a id="factor-spec-71-唯一口径设计"></a>
#### 7.1 唯一口径【设计】

- **Sharpe**：净值曲线日收益 `r_t = nav_t/nav_{t−1} − 1`，`Sharpe = mean(r)/std(r, ddof=1) × √A`；无风险利率参数 `rf_annual`（默认 0，单位/年，日化按 A 折算）。引擎三种旧口径（逐笔/仅卖出日/净值）收敛为净值口径；前两者字段保留一个版本周期并标 `deprecated_mode`。
- **年化天数 A**：默认 243（近五年 A 股实际均值区间），`metrics_methodology_version = "metrics_v2"`；报告展示口径徽章。
- **年化收益**：`(nav_T/nav_0)^(A/n_bars) − 1`（统一按 K 线数折算，废除 365.25 自然日混用，engine.py:2871-2874 收敛）。
- **MaxDD**：现有算法（峰值下限 1.0）不变。
- **基准相对新增**：`excess_annual`、`tracking_error = std(r−r_b)×√A`、`information_ratio = mean(r−r_b)/std(r−r_b)×√A`、`beta/alpha`（OLS，rf 处理同上）、`excess_win_rate`。
- `engine.py`/`strategy.py`/`factor.py` 全部改 import `app/factors/metrics.py`，禁止本地重算（Ruff 检查加入 noqa 禁用清单之外无豁免）。

---

<a id="factor-spec-8-复合因子与策略接入l-cmp核心章节"></a>
### 8. 复合因子与策略接入（L-CMP）——核心章节

<a id="factor-spec-81-现有桥现状已可用设计的锚点"></a>
#### 8.1 现有桥（【现状·已可用】，设计的锚点）

- 策略配置：`"scoring": {"factor_name": weight}` + `scoring_directions` 覆盖高低方向（`builtin/*.py` 均此形态）。
- `scoring.py`：虚拟因子按需编译 Polars 表达式（`scoring_value_expr`）、依赖展开（`scoring_dependencies`）、预热推导（`scoring_warmup_bars`）。
- 回测矩阵按 `score` 排序建仓（`engine.py` `max_positions` + `score_min/max`）。
- 挖掘产物 = 因子排名组合（mining.py），候选库 `candidates.py` 已有 `factor`/`strategy` 双形态。

<a id="factor-spec-82-factorcompositespec设计"></a>
#### 8.2 FactorCompositeSpec【设计】

```python
@dataclass(frozen=True)
class FactorCompositeSpec:
    id: str                      # ^cf_[a-z0-9_]{1,40}$；策略 scoring 里以 "cf_xxx" 引用
    version: int
    label: str
    factors: tuple[CompositeMember, ...]   # 1..10 个成员
    transform: Literal["rank", "zscore"] = "rank"   # 成员标准化方式（截面）
    weighting: Literal["manual", "equal", "icir", "max_ic"] = "manual"
    auto_weight_window: int = 504           # 自动权重的滚动窗口（交易日）
    direction: Literal["high", "low"] = "high"

@dataclass(frozen=True)
class CompositeMember:
    factor: str                  # 任意已注册因子 id（含 uf_/cf_ 前缀，禁止自引用，环检测）
    weight: float | None         # manual 模式必填；自动模式忽略
    direction_override: Literal["high", "low"] | None = None
```

**计算管线（顺序固定，进 spec 哈希）**：

```
宇宙(可选, 默认不过滤以兼容现有策略)
→ 各成员因子值（注册表展开，含 warmup 检查）
→ winsorize(3σ, 可关)
→ 截面 transform（rank/zscore）
→ 方向统一（low → 取负）
→ 加权求和（manual 权重归一化校验 |Σw−1|<1e-9；
   icir → w_i ∝ max(ICIR_i, 0)，ICIR 取 (t−auto_weight_window, t−1] 窗口——**权重只用于过去，严禁 t 日数据参与 t 日权重**；
   max_ic → 同窗口 mean(rank IC) 单调权重）
→ 输出复合分 cf_xxx（rank 基础下近似 ∈ [−1,1]，文档声明分布性质）
```

**接入策略（零引擎改动）**：

- 复合因子注册进注册表（kind="composite"），`scoring_value_expr` 机制天然支持：策略写 `"scoring": {"cf_hotmom": 0.6, "vol_ratio_5d": 0.2, "amount": 0.2}` 即生效；依赖/预热自动递归展开进矩阵构建，选股/回测/监控三端无需感知"这是复合因子"。
- 挖掘产物一键导出为 CompositeSpec：mining 的排名组合本来 = factors+weights，导出即 `cf_mined_<run>`，闭合"挖掘 → 复合因子 → 策略"回路。
- 前置校验：成员因子任一 warmup 超研究窗口 → 启动期注册成功但使用时返回明确"预热不足"错误（不产出半截分数）。

**策略侧引用形态（确切 JSON）**——复合因子编辑器"导出 scoring 片段"产出，直接粘贴进策略配置：

```json
{
  "scoring": {"cf_hotmom": 0.6, "vol_ratio_5d": 0.2, "amount": 0.2},
  "scoring_directions": {"cf_hotmom": "high", "amount": "high"}
}
```

复合因子与普通字段混用、权重语义不变；`scoring_dependencies`/`scoring_warmup_bars` 自动递归展开（`scoring.py:91-104`【现状】机制不动，仅数据源换成注册表）。

<a id="factor-spec-83-一致性契约设计"></a>
#### 8.3 一致性契约【设计】

同一 `cf_xxx` 在**因子研究（IC/分层）、选股、回测、监控**四端必须逐位同值——单测直接断言四路径对同一 (date,symbol) 的输出相等。这是 CONTRIBUTING 5.3"同一候选集和排序方向"的推广。

<a id="factor-spec-84-监控端数据流澄清设计"></a>
#### 8.4 监控端数据流澄清【设计】

监控不重算复合因子：`monitor.py:1314-1322`【现状】消费的是**策略结果缓存**里的 `result.scores`（`score_min/score_max` 过滤）。因此复合因子进监控的路径 = 策略执行时算好分 → 结果缓存 → 监控读缓存。**实时行情线程零新增计算**（CONTRIBUTING 6.3 硬约束）。推论：修改复合因子定义后，必须走策略参数变更的既有失效链路（重算策略结果缓存 → 监控实例刷新），该链路已存在（CONTRIBUTING 5.1），设计只复用不新造。

<a id="factor-spec-85-自定义复合因子的盘中行为设计"></a>
#### 8.5 自定义/复合因子的盘中行为【设计】

- 选股（盘后批量）：`incremental_safe=True` 的成员因子照常参与当日计算。
- `incremental_safe=False` 成员（若有）：当日选股对该因子返回"预热/路径不足"的明确不可计算状态（对齐 CONTRIBUTING 5.1"空值不得伪装成零分"），UI 标注原因；**不降级用部分成员算半截复合分**。
- 盘中增量路径（`pipeline.py:1795` `compute_enriched_today`【现状】）：自定义与复合因子默认不进入（[§12](#factor-spec-12-缓存与性能设计) 缓存策略），分时选股若引用则同样返回不可计算状态，盘后恢复。

---

<a id="factor-spec-9-数据契约扩展provider-dataset"></a>
### 9. 数据契约扩展（provider dataset）

新增 dataset 声明（capabilities.py 注册表 + 对应 provider 实现，均【设计】）：

| dataset | 内容 | 解锁能力 | 无数据源时 |
| --- | --- | --- | --- |
| `st_history` | 点时风险警示状态 | 历史涨跌停幅度修正、宇宙 ST 点时过滤 | 涨跌停用当前名推断 + 注记（现状） |
| `delisted_kline` | 退市标的日 K + 退市维表 | 幸存者偏差修复（宇宙回补） | 报告永久幸存者注记 |
| `industry_pit` | 点时行业归属 | 中性化升级为点时 | 行业快照 + 注记（现状） |
| `index_ext` | 扩展指数日 K | 基准升级（当前限核心四只，index_const.py:12-15【现状】） | 基准限核心四只 |

各 dataset 完整字段 schema（provider 归一后落 Parquet，命名对齐现有 normalized 契约）：

**`st_history`**（分区 `data/parquet/st_history/`，按年）：

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| symbol | str | 标准代码（如 600000.SH） |
| flag_date | date | 状态生效日（戴帽/摘帽公告后的首个交易日） |
| st_flag | bool | true=风险警示（ST/*ST），false=摘帽；行区间语义：自 flag_date 起至下一条记录 |
| flag_type | str | "ST" / "*ST" / "摘帽"；缺失填 "ST" |
| source | str | provider 标识 |

查询语义：`st_at(symbol, t) = flag_date ≤ t 的最后一条记录的 st_flag`（asof-backward）。同步：全量快照 + 增量 append，`(symbol, flag_date)` 去重幂等（对齐 kline_sync:358-367【现状】模式）。

**`delisted_kline`**：K 线部分复用 `daily` dataset 完整 schema（symbol/date/OHLC/volume/amount/…）；另需维表 `delisted_instruments`：

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| symbol / name | str | 代码/退市前简称 |
| list_date / delist_date | date | 上市/退市日 |
| delist_reason | str | "面值"/"财务"/"重组"/"主动"/"其他"；缺失填 "其他" |

宇宙回补语义：`as_of=t 的可交易池 = instruments(上市≤t<退市) ∪ delisted(上市≤t<退市)`；`_resolve_universe`（daily_pipeline.py:92-125【现状】）扩展为两源合并。

**`industry_pit`**（分区 `data/parquet/industry_pit/`）：

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| symbol | str | 标准代码 |
| effective_date | date | 归属生效日 |
| industry_l1 / industry_l2 | str | 一级/二级行业名（如 计算机/软件开发，对齐 market_mainline.py:35【现状】的两级口径） |
| source | str | provider（如 ths） |

查询语义：asof-backward join（同 fundamentals.py:107-114【现状】模式）；effective_date 缺失 = 供应商不提供历史，整表降级为快照并触发 [§5](#factor-spec-5-风险调整与基准l-neu) 注记。

**`index_ext`**：schema 与 `kline_index_daily` 完全一致（symbol/date/OHLC/volume/amount），仅标的白名单扩展（默认建议：000300.SH 沪深300、000905.SH 中证500、000852.SH 中证1000、000985.SH 中证全指）；白名单由 preset 配置声明，不进代码硬编码（对齐 ext_presets 模式）。

**能力注册示例**（provider 侧 `plugin.yaml` datasets 声明，对齐 `docs/reference/integrations.md#plugin` 契约）：

```yaml
datasets:
  st_history:
    enabled: true
    description: 点时风险警示状态（戴帽/摘帽区间）
  delisted_kline:
    enabled: true
    description: 退市标的日K + 退市维表
  industry_pit:
    enabled: false          # 供应商无历史归属时声明 false，不注册能力
    description: 点时行业归属
```

能力矩阵（`capabilities.py` 注册表）同步各 dataset 的展示元数据与路由偏好字段；provider 未声明 = 该能力全局不可用，研究路径按 [§4.3](#factor-spec-43-数据缺口降级矩阵设计) 降级矩阵处理，不静默。

---

<a id="factor-spec-10-api-契约设计"></a>
### 10. API 契约【设计】

新路由前缀 `/api/factors`（薄层，重计算在 services/factors_research.py 编排层）：

| 端点 | 方法 | 请求要点 | 响应要点 |
| --- | --- | --- | --- |
| `/api/factors/catalog` | GET | asset_type, group, stability 过滤 | 因子清单（含 formula_text/warmup/direction/pit/scale_free/usage_count） |
| `/api/factors/validate` | POST | formula 文本 | 编译错误（位置+原因）或成功（依赖/预热推导） |
| `/api/factors/preview` | POST | formula + symbols + date_range | 试算表格（最新 5 日 × 前 20 标的）+ 非空率 |
| `/api/factors/custom` | GET/POST/DELETE | [§3.4](#factor-spec-34-形式文法ebnf与错误码目录设计) JSON | CRUD；删除带引用清单 |
| `/api/factor-research/ic` | POST | factor_id, universe_id/UniverseSpec, NeutralizationSpec, start/end, rebalance, n_groups | [§6](#factor-spec-6-统计检验l-inf) 全套统计 + 三口径并列 + 漏斗 filter_stats + degraded 注记 |
| `/api/factor-research/composite` | GET/POST/DELETE | CompositeSpec | CRUD + 一键"作为评分字段试策略"跳转链接 |
| `/api/factor-research/universes` | GET/POST | UniverseSpec 存档 | 命名宇宙 CRUD（研究配置复用） |

全部响应新增字段带默认值；错误响应含 `code/message/detail`，不泄漏内部栈（CONTRIBUTING 第 8 节）。SSE 进度复用现有回测 SSE 模式（长任务：批量 IC 扫描）。

**鉴权（原设计参考）**：新路由经 `api/routes.py` 注册；当前必须复用模式对应的账户、租户事务和scope，不采用早期应用级部署口令假设。原设计的认证背景见（`docs/operations.md#email-accounts` 模式），不引入独立权限模型。写操作（自定义因子/复合因子/宇宙存档 CRUD）只落 `data/user_data/`，路径校验沿用策略目录的防穿越规则（CONTRIBUTING 5.1 删除策略 fail-closed 要求同样适用）。

**并发**：批量 IC 扫描与复合分批量计算走 `services/heavy_job_limiter.py`【现状】限流，SSE 进度事件结构复用回测现有契约；用户取消走现有回测 worker 取消机制。

**核心端点示例**（其余端点仅为原设计示例）：

`GET /api/factors/catalog?group=动量&asset_type=stock` →

```json
{
  "factors": [
    {
      "id": "momentum_20d", "version": 1, "label": "20日动量", "group": "动量",
      "kind": "base", "formula_text": "20个交易日累计收益率",
      "direction": "high", "unit": "ratio", "warmup_bars": 20,
      "pit": false, "scale_free": true, "stability": "stable",
      "tags": ["momentum"], "usage_count": 7, "custom": false
    }
  ],
  "total": 61, "degraded": []
}
```

`POST /api/factors/validate` `{"formula": "rank(ts_delta(close, -5))"}` →

```json
{"ok": false, "errors": [{"code": "E005", "message": "负 shift: ts_delay 的 n 必须 ≥ 0（负数即未来函数）", "position": {"offset": 18, "line": 1}, "detail": {"n": -5}}]}
```

`POST /api/factor-research/ic`：

```json
{
  "factor_id": "momentum_20d",
  "universe": {"exclude_suspended": true, "exclude_limit_locked": true, "exclude_st": true, "min_listing_days": 60},
  "neutralization": {"benchmark": "000001.SH", "return_basis": "excess", "method": "industry_demean", "winsorize_sigma": 3.0},
  "start": "2023-01-01", "end": "2025-12-31",
  "rebalance": "monthly", "n_groups": 5
}
```

响应（节选，完整字段见 [§6](#factor-spec-6-统计检验l-inf)）：

```json
{
  "methodology_version": "factor_v3",
  "universe_id": "a1b2c3d4e5f6", "neutralization_id": "9f8e7d6c5b4a",
  "ic": {"mean": 0.031, "icir": 0.42, "t_naive": 2.9, "t_newey_west": 1.87, "nw_lag": 1,
          "significance": "*", "half_life_days": 4, "acf": [0.21, 0.08, ...], "coverage": 0.97},
  "ic_by_basis": {"raw": {"mean": 0.041, "t_newey_west": 2.2}, "excess": {"mean": 0.031, "t_newey_west": 1.87},
                   "excess_neutralized": {"mean": 0.019, "t_newey_west": 1.02}},
  "monotonicity": {"spearman": 0.9, "trend_t": 2.4, "verdict": "成立"},
  "universe_funnel": [{"date": "2025-12-31", "raw": 5412, "after_suspended": 5390, "after_limit_locked": 5320, "after_st": 5180, "after_new_listing": 5090}],
  "degraded": ["st_not_point_in_time", "industry_snapshot"],
  "turnover_top_group": 0.31, "costs_round_trip": 0.0013
}
```

---

<a id="factor-spec-12-缓存与性能设计"></a>
### 12. 缓存与性能【设计】

| 缓存 | 键 | 失效 |
| --- | --- | --- |
| 因子 IC 报告 | `fr:ic:{factor_id}:{v}:{universe_id}:{neu_id}:{methodology}:{range_hash}:{rebalance}:{n_groups}` | enriched generation 变更或键任一分量变 |
| 复合因子定义 | `fr:cmp:{id}:{spec_hash}` | 定义编辑 |
| 复合分值（研究期） | `fr:cmpv:{cmp_spec_hash}:{matrix_generation}` | 矩阵重建 |
| DSL 编译产物 | 进程内 LRU（表达式文本 → Expr） | 进程重启 |
| 宇宙快照 | `fr:uni:{universe_id}:{matrix_generation}` | 矩阵重建 |

约束：复合/自定义因子**默认不物化进 enriched parquet**（避免用户定义污染核心管道与增量路径）；只在研究/评分请求期计算并按上表缓存。`incremental_safe=False` 的因子盘中路径直接缺失而非降级计算（对齐 pipeline 增量路径现有行为）。中性化逐日截面计算全部 Polars 表达式化；统计层 O(N·G) 极小。禁止任何新增逻辑进入实时行情线程（CONTRIBUTING 6.3）。

**性能预算（实现验收线，超线必须先优化再合入）**：

| 操作 | 预算 | 基准场景 |
| --- | --- | --- |
| 单因子 IC 全报告（含三口径+t 值+分层） | ≤ 现有报告耗时 × 1.3 | 全 A 股 × 3 年日线（现有 `factor.py` 同窗基线，PR 里附前后数据，CONTRIBUTING 6.3） |
| DSL 编译（含校验） | ≤ 5ms/表达式 | 深度 12、token 200 上限样例 |
| 复合因子单期截面计算 | ≤ 成员因子独立计算耗时之和 × 1.2 | 10 成员 × 全 A 股 |
| 宇宙过滤全期 | ≤ 全期 IC 计算的 10% | 同上基准 |
| 因子目录接口 | ≤ 50ms | 全量 62+ 因子元数据 |
| 相关性探索器 | ≤ 现有 mining 同规模秩相关耗时 × 1.1 | 10 因子 × 1 年 |

---

<a id="factor-spec-13-测试矩阵最低要求清单设计"></a>
### 13. 测试矩阵（最低要求清单）【设计】

| 模块 | 必测 |
| --- | --- |
| 注册表重构 | 特征化快照（62+ 因子两条计算路径逐位一致）；重复 id/未增版本拒绝启动 |
| DSL | 每算子黄金用例（含 null/除零/σ=0/全常数）；负 shift 编译失败；深度/窗口/白名单越界拒绝；与手写 Polars 等价性；注入样例（`__import__`、列名穿越）拒绝 |
| 宇宙 | 每过滤器独立单测（构造含 ST/停牌/涨停锁死/次新/微额的合成面板）；漏斗计数；降级注记触发 |
| 中性化 | 合成数据数值断言（demean/zscore/回归残差 vs statsmodels 对拍）；行业缺组回退；基准停市日 |
| 统计 | NW t：黄金参考向量（离线计算硬编码期望值）+ dev 组 statsmodels 对拍（可选）；FDR：BH 已知 p 向量解析解；DSR：已构造解析例（对称正态收益 + 已知试验数）；单调性边界（平梯/倒梯） |
| 指标 | Sharpe/年化/超额/IR 已知序列解析解；243 口径回归 |
| 复合因子 | 权重归一；**自动权重无未来函数**（t 日权重不随 t 日数据变化——篡改 t 日数据断言权重不变）；四端同值断言（[§8.3](#factor-spec-83-一致性契约设计)）；环引用拒绝 |
| API | 成功/空数据/编译错误/预热不足/无权限 |
| 缓存 | 键覆盖测试（改 spec 必换键）；generation 失效 |
| 前端 | pnpm build + 五态检查 |

---

<a id="factor-validation"></a>
## 3. 兼容与验证

注册与评分重构必须以固定样本逐位对照，保留已有因子ID和旧结果读取。
因子版本、方法论版本、输入版本分别管理；三口径/指标变化必须显式标记，
不能直接以设计中的 `metrics_v2` 替换当前结果。

宇宙和中性化接入时同步候选配置白名单，验证保存→载入后的 spec 哈希不变；
缓存键覆盖日期、证券、定义、预热、输入和环境。已有用户文件与研究证据不因回退而删除，
不能将“revert代码”视为已经验证数据兼容。

后续按真实需求建设宇宙、中性化、自动权重与巡检：
自动权重只使用 t−1 及以前的窗口；滚动63日IC/IR与连续126日观察仅为原衰减方案，
不是已运行任务。门槛变化需产品、研究和测试共同记录依据。
完整历史退市池、点时行业与财务修订时间仍依赖来源证据。
不新增因子市场、跨市场引擎、在线学习或第二套计算器。

测试矩阵见上面的技术设计测试节；统一运行命令与放行标准见[测试手册](../testing.md)。
2026-09-04的61因子/Polars验证及2026-09-05扩充记录保留在
[历史依据](../../reports/document-consolidation-20261024/historical-evidence.md#factor-history)，
不代表本次重跑。
