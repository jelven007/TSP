# rustdx 三组采集路由实施计划

> Status: APPROVED
> Source: .claude/artifacts/designs/rustdx-task-affinity.md
> Mode: default
> Iterations: 2 / 3
> Baseline: 5b273174f2e2a8ac539fcea363fa02315a61ebe9
> Authorization: 用户已授权方案优化与复核通过后的源码更新；不包含发布。

## Requirements summary

以现有rustdx v2.0.4和8业务/1探测子池为基础，按三类任务分组，
候选按当日有效池序号每3项划分，完善稳定来源、组内优先级和主备恢复。
不改变数据单位、已有用户配置、单写发布、分区合并和应用版本。

## Acceptance criteria

完整继承规格AC-01～AC-15。实施及验证必须记录哪些实际通过，哪些仍有外部限制。

## RALPLAN-DR

### Principles

- 金融数据正确性先于并发与最快样本。
- 复用现有Provider、NativePool、同步服务与发布链，不引入采集子进程。
- 组配额与端点配额分离，业务/探测独立，总资源有界。
- 保留已有配置与窗口重采/逐证券跳过行为，新增原生接口按独立版本管理。
- 每项完成声明由本地TCP、原实现失败回归或真实只读证据支持。

### Decision drivers

1. 原始报文已证明跨节点分钟内容差异，以及固定连接不能冻结盘中offset。
2. 现有NativePool已提供受管TCP、源检查、取消时限与标准化边界。
3. 共享工作区有并行修改，方案必须能够隔离实施并只合入本任务差异。

### Viable options

**A：Python任务控制器组合现有原生子池（采用）。**
新增routing模块管理三组；native增加组/端点RAII额度、优先级与节点池入口；
core只增加探测分组、精确预算与取消。Provider负责完整窗口数据核对。
优点：原业务入口和PyO3单池代码继续复用，隔离测试及回退容易。
代价：Python/Native间需要明确的窗口与世代上下文，必须防止全局环境变量切站。

**B：全部路由管理移入一个Rust多组Runtime（未选）。**
重构当前RustdxClient持有三个Pool并统一管理候选、任务类别和资格。
优点：资源状态集中，跨线程一致性容易表达。
代价：需要重写849行桥接的多个方法、改变PyO3对象生命周期，
财务/业务窗口核验仍需Python域逻辑，当前收益不足以承担更大改动。

## Implementation steps

1. **保护基线及失败测试** — `backend/native/rustdx_native/src/lib.rs:164-359`、
   `backend/tests/test_rustdx_native_pool.py`、已有未跟踪`test_rustdx_stale_source.py`；
   在独立临时worktree保留当前坏会话退役修改，先复现配额/路由/锚点缺口。
2. **分级业务额度** — 新`backend/native/rustdx_native/src/quota.rs`；
   `src/lib.rs:19-63,220-359,495-555`。
   采用RAII组/端点许可包裹现有Client；socket关闭后释放；全局仍用core现有Governor。
   共享目录使用固定组及编码端点锁，不能通过新目录绕过旧进程容量协议。
3. **探测标签与关闭** — `vendor/rustdx-core/src/tcp/ip/coordinator.rs:11-195`、
   `probe.rs:97-255`、`daily.rs:35-110,190-316`、`ip.rs`。
   每组/端点1、全局3；相应调小分组worker并同步整个池超时算法；
   初始/每日测速共用同一预算；关闭取消当前刷新，不发布部分快照。
4. **原生兼容接口** — `src/lib.rs:495-849`、`src/pool.rs:39-149`。
   增加task_group、可调后台上限、队列上限、节点池快照和诊断统计；
   原参数及单池legacy组兼容。核心版本仍2.0.4，桥接及API单独升级。
5. **三组任务路由** — 新`backend/app/plugins/rustdx/routing.py`；
   `client.py:60-134,188-361`、`provider.py:296-301,817-919,1375-1430`。
   `_native_client()`保持可注入入口，返回兼容方法的统一控制器；
   实时请求标记竞价/盘口，分钟增量显式用前台准入，其他按功能归组。
   不要求旧FakeClient支持新增关键字；使用可选明确方法适配。
6. **分页与来源正确性** — `provider.py:415-556`、
   `client.py:230-302`及routing中的窗口上下文。
   在真实Client上记录/复核最新锚点；冲突重复键拒绝；新来源完整窗口与
   真实参考读取比较，短期资格按精确scope/内容hash缓存，过期或改变重验。
   不读取旧Parquet补值；参考调用也计入现有池额度和请求统计。
7. **构建身份与说明** — `Cargo.toml/Cargo.lock/pyproject.toml`、
   `backend/uv.lock`、`vendor/PATCHES.md`、`UPSTREAM.json/SOURCE-SHA256SUMS`、
   `scripts/vendor_rustdx.py`及`.env.example`。
   更新需求、架构、二开、运维、测试、用户操作说明；
   原股票官方目录、财务配置及版本文件不改变。
8. **质量门及精确合入** — 新路由/配额测试、现有rustdx与同步/能力/打包回归。
   独立环境重新构建wheel，运行全量后端与文档/工具检查；
   真实小样本只读核验，不写生产行情；确认工作区基线哈希后仅合入本任务差异。

## Workspace setup

工作区当前为dirty main。使用`/tmp`下独立worktree，基于固定HEAD，
叠加当前`src/lib.rs`及未跟踪旧会话测试作为输入基线。中间脚本/日志/缓存不进项目。
实施完成前核对共享工作区文件是否并行改变；发生差异时三方对照，不全文件覆盖。
不提交、不推送、不重置已有修改。最终规格/计划和验证报告留在用户工作区。

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| 第2名不支持当前历史窗口 | 分组仅作为候选；保留已配置参考来源，真实窗口核对，失败不认证 |
| 盘中offset原点改变 | 锚点/连续性/重复冲突检查；仅完整重采当前证券窗口一次 |
| 财务只提供目录、不提供ZIP | 文件能力实际检查，保留专站配置且计入基础组额度 |
| 配额切换双持旧/新连接 | 先关闭/排空旧组并释放RAII许可，再启用新世代 |
| 共享多进程重复扩容 | 组、端点及core全局文件额度；容量协议不同拒绝 |
| 历史回填饿死盘中刷新 | 盘中后台6，前台预留2；盘后8；有界等待 |
| 原生daily线程关闭后继续探测 | 取消刷新上下文并停止后续刷新；关闭/取消回归 |
| 窗口核验成本抵消提速 | 成本全计量；短期精确scope资格；不声称固定提速 |
| 并行源文件/报告丢失 | 临时worktree、输入哈希及精确差异合入 |

## Verification steps

- routing单测：9节点分组、资格过滤、黏性、显式配置、空候选、队列、切换世代。
- 真实本地TCP：三站24/3、逐站8/1、财务例外仍受组限、关闭归零、坏会话恢复。
- 两进程共享目录：总、组、端点额度与进程退出后的文件锁释放。
- 原实现锚点新增/源切换/冲突重复失败，新实现窗口完整重采。
- `cargo fmt --check`、bridge/core `cargo test --locked`、严格Clippy；
  vendor身份刷新后离线核验；Windows目标交叉检查适用核心。
- `uv sync --extra dev --extra cell --frozen`；安装检查；全部rustdx和邻接定向pytest；
  隔离standalone后端全量pytest；`tests/tooling`；增量Ruff和根`make check`。
- 真实只读验证：原主站同证券闭市窗口对照、实时/日K/文件索引，
  所有参考请求和错误计入，失败不丢样本；不写生产数据。

## Architect challenge

最强反方：Python控制器可能只是包装三池，原生探测和共享额度仍可绕过，
旧线程或窗口世代也可能继续工作；这样名义24/3并不约束真实socket。
因此额度必须跟随真实TCP持有，探测必须改实际Coordinator，
切换和关闭必须有真实socket及多进程证据，不能只测试Fake方法返回。

真实矛盾：严格每3项分组与保留已验证参考来源存在冲突；
财务端点例外也不符合“严格只有三个物理IP”。选择数据正确性与已存配置优先，
将三组定义为资源/任务隔离，不把未知主站强行用于历史或财务。

## Critic verdict

| Dimension | Result | Evidence/condition |
|---|---|---|
| Principle-option consistency | PASS | A复用原生TCP与业务入口 |
| Alternative exploration | PASS | B是真实native集中方案，明确API/生命周期代价 |
| Risk mitigation clarity | PASS | 风险逐项对应额度、取消、scope核验和工作区保护 |
| AC testability | PASS | AC-01～15有输入、数值上限或失败预期 |
| Verification concreteness | PASS | 本地TCP、两进程、原实现失败及冻结质量门 |
| File/line coverage | PASS | 每步均指向现有模块或明确新增文件 |

Verdict: APPROVED for implementation，非上线/性能认证。
Reservation：没有上游不可变历史版本；窗口完整核验及资格过期会增加成本。
须保留真实失败、成本及未认证范围，不能删掉慢样本来满足收益目标。

## ADR

- Decision：以Python任务控制器组合原生8+1子池，真实许可实施组/端点/总限额。
- Drivers：已证实的数据差异、旧接口复用、共享工作区兼容及可执行质量门。
- Alternatives：A采用；B因更大PyO3生命周期重构暂不选；纯改常量/环境切站因不安全拒绝。
- Why chosen：保留原数据与TCP边界，分别验证路由、实际额度和窗口正确性，
  避免把任务进程化或复制Provider。
- Consequences：原生独立API升级，增加受审计probe补丁及窗口核验成本；
  不更改应用版本或数据schema。
- Follow-ups：真实生产容量与正式P95签收属于发布阶段，本次只读小样本不替代。

## Review trail

- Planner v1：固定序号分组、每组8/1、故障自动换站。
- Architect v1：拒绝无数据资格、探测绕过、财务资格误用和全天后台占8。
- Critic v1：REVISE — 缺少窗口基准、真实共享额度、关闭取消和配置兼容。
- Planner v2：加入参考来源、精确窗口核验、native RAII/probe标签、
  盘中2/6、世代切换、legacy模式及精确工作区合入。
- Architect v2：保留核验成本和三组/三个IP的取舍。
- Critic v2：APPROVED with reservation — 性能及未验证主站不能提前认证。
