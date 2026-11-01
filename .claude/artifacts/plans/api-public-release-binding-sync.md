# 闲置API进程公共发布绑定同步修复方案

Status: IMPLEMENTED_LOCAL_CANDIDATE_ACCEPTANCE_PENDING
User approval: 用户“按方案开始实施修复”
Design review: 单代理内分别执行Planner、Architect、Critic审视，最终方案通过内部审视
Source: 用户“根据根因生成修复方案，同步闲置进程的发布绑定”
Mode: deliberate；生产故障修复的方案阶段
Iterations: 2 / 3
Last updated: 2026-11-01

目标：让每个Full API进程在没有业务请求时及时更新公共发布绑定，并确保旧发布在
仍被读取期间不会被回收。用户已批准实施，内部类型、接线及测试已落入工作区。
下文设计保留原方案用语；实际实现、偏差、验证及未完成验收以
[实施记录](../../../reports/api-public-release-binding-implementation-20261101.md)为准。
生产原始证据见[根因调查](../../../reports/api-readiness-root-cause-20261101.md)，
本次没有修改受控配置或生产服务状态，未提交/推送，应用版本不递增。

## 1. 需求、范围与验收条件

已确认4个API进程中3个绑定定时维护删除的旧目录，60次采样中45次503。
问题由按需绑定更新与发布回收之间的生命周期缺口造成。
本次属于L3核心局部修改，复用PublicQuoteBridge、PublicMarket、DataStore、
现有发布清单、保留候选算法及进程生命周期；不增加扩展注册接口。

本次范围是Full API的发布同步、绑定读取、旧发布使用期保护、保留任务配合及相关测试。
保持现有业务请求的按需行情刷新、Platform采集、四类Worker心跳、研究固定输入、
HTTP路径和200/503含义。公共清单增加共享锁使用约定，不修改清单内容或格式。
不新增数据库表、Provider路由、前端界面、用户配置项或部署自动升级行为。

| 编号 | 可验证条件 |
| --- | --- |
| AC-1 | 4个独立API进程没有业务请求、甚至没有健康请求，current变更后全部自动更新绑定；候选环境正常负载下25秒内完成 |
| AC-2 | current不变时，连续12轮巡检及任意marker计数增长均不增加`PublicMarket.snapshot()`调用或frame decode计数 |
| AC-3 | 同步失败、锁忙和候选构建中断均保留此前完整绑定；不安装半成品或晚到的旧候选 |
| AC-4 | 就绪检查取得同一已提交绑定中的仓库、行情快照和租约引用；准备新绑定期间探活无需等待大快照获取 |
| AC-5 | current暂时变化但旧绑定有效时允许至多30秒的同步宽限；持续失配、巡检超过30秒没有成功检查、当前入口失效或绑定清单不可用时，`public_files`为error、HTTP503 |
| AC-6 | 回收遇到任意共享读锁时跳过对应发布；读者释放后，符合原保留条件的发布才可删除；研究引用和current仍无条件保护 |
| AC-7 | 旧仓库仍被在途请求引用时，关闭新旧绑定或GC均不提前关闭其DuckDB连接或清单锁；资源终结不保留初始仓库至进程退出 |
| AC-8 | 重复start/close不生成重复巡检线程；停止后不提交新候选；没有存活读者时进程退出释放所有锁 |
| AC-9 | 清单缺失、非法ID、越界路径、非普通文件、锁能力缺失均明确诊断且拒绝无保护删除；合法旧清单无须迁移或添加文件 |
| AC-10 | 既有相关模块回归及新增复现、并发、回收测试通过；候选环境无新增探活超时，正常负载下探活p95≤500 ms、最大耗时小于现有6秒客户端期限 |
| AC-11 | 生产修复验收覆盖至少6小时保留边界及真实发布切换/回收；需逐进程确认，不能只用随机负载均衡请求宣称4进程一致 |

25秒收敛和探活延迟是上线验收目标，不是当前实现保证。
30秒为状态宽限/诊断预算，不把它描述为可以强行终止任意同步Python操作的硬期限。
若候选环境达不到目标，停止上线并记录阶段耗时，调整方案后重新验证。

## 2. RALPLAN-DR

### Principles

1. 同步由API生命周期负责，不能依赖用户流量或探活触发。
2. 巡检只检查发布身份；实时行情的marker不会让闲置巡检拉取大快照。
3. 新绑定准备成功后再提交；失败保留旧绑定并报告实际状态。
4. 发布回收必须与真正的文件读者协作；不能以“进程已切换”推断旧请求已结束。
5. 复用已有接口与清单，限制修改范围，保留历史数据和运行配置。

### Decision drivers

- 消除3/4进程旧绑定和间歇503，同时保留真实不可用时的503。
- 控制快照解码、仓库重建及探活延迟，避免以后台轮询增加采集/CPU压力。
- 保护仍被使用的目录，尽量不引入数据库迁移和另一套任务租约。

### Viable options

**方案A：每进程发布巡检 + 现有清单文件共享锁（推荐）**

每5秒比较current与已提交绑定。仅发布身份变化时准备新绑定，仍复用现有刷新锁
协调业务请求。API持有不可变`.public_release.json`文件上的共享`flock`；
保留任务删除前取得同一文件的非阻塞排他锁。

优点：解决纯闲置进程；无新表、锁文件或清单格式；锁随进程/文件描述符释放；
可以用ECS现有只读API挂载打开文件取得共享锁。
代价：要求回收者与容器看到同一文件锁语义；需显式管理仓库生命周期，
而非仅更新`app.state.repo`后立即关闭旧库。
适用范围为当前单机ECS的本地文件系统，跨主机共享存储必须另行验证。

**方案B：每进程巡检 + 数据库登记读者租约和回收隔离**

API在控制库登记绑定、租约及使用中的旧发布，后台续约；保留任务联查研究引用
和读者租约。过期读者还需被隔离，避免租约到期后继续读已回收目录。

优点：可显式查询多主机读者，适合无法共享可靠文件锁的部署。
代价：新增schema、权限、租约续期、时钟/过期隔离和故障恢复路径；
读者暂停或数据库短暂失败时必须处理过期后读取边界，不能仅设置TTL。
本次不选：当前故障在同一ECS文件系统内，新增持久化租约成本高于方案A。
该方案是真实替代路径，在文件锁能力验证失败时才重新评估。

**不采用的应急替代**

只在`/health/ready`调用`ensure_fresh()`可缓解探活，但不能满足没有任何请求的AC-1，
还会把大快照和仓库构建放进6秒探活期限。
只重启或加长保留时间无法约束无限期闲置读者，不能作为根因修复。

## 3. 推荐设计

### 3.1 巡检及生命周期

在`backend/app/multitenancy/`拟新增`public_binding_sync.py`，
定义内部`PublicBindingSynchronizer`，通过组合接入Full API lifespan，
不继承或替换整个主应用。
默认间隔5秒，测试可通过构造参数缩短间隔；本次不新增环境变量。

职责与拟新增方法：

| 方法 | 行为 |
| --- | --- |
| `start()` | 幂等启动一个daemon线程；初始绑定已完成后、进入API可服务阶段前启动 |
| `check_once()` | 严格解析current，记录检查时刻；身份相同直接结束；身份不同调用桥接的发布专用同步 |
| `snapshot()` | 返回短锁保护的诊断副本，不执行网络或文件全量检查 |
| `close()` | 设置停止事件，唤醒等待，停止新候选；在关闭Market与仓库资源前收尾 |

循环使用`stop_event.wait(5)`，异常按固定、有限错误类别记录并在下轮重试；
不快速重试、不积累任务队列、不在event callback里解码快照。
首次启动由现有同步`quotes.refresh()`建立有效绑定，再启动巡检。

诊断至少包含：`running`、`last_checked_age_seconds`、
`binding_current`、`mismatch_age_seconds`、`last_error_kind`。
使用monotonic测量年龄。锁忙不能清零失配年龄；
只有current与已提交绑定相同才清零。停止或超过30秒没有成功检查必须可见。

正常收敛预算：下一轮检测≤5秒，Platform HTTP各阶段沿现有15秒超时，
剩余准备/提交在候选正常负载下完成，使总收敛≤25秒。
现有HTTP超时并非整个操作的硬截止；准备阶段在关键边界检查累计耗时，
超过30秒丢弃候选并使就绪诊断失败，不强行杀死正在执行的Python线程。

### 3.2 只在发布变化时同步

在PublicQuoteBridge拟新增`sync_public_release()`。
巡检的current检查在刷新锁外进行一次，取得刷新锁后再次检查已提交绑定，
避免业务请求已更新后仍重复下载。
后台使用非阻塞方式尝试现有`refresh_lock`：锁忙则本轮跳过并保留诊断。
业务请求继续沿原`ensure_fresh()`/`refresh()`接线，不新增第二个HTTP客户端。
锁的取得只由最外层入口负责；后台已经持有refresh_lock时不能再调用会取得同一锁的
`refresh()`。共用的内部准备逻辑不重新取刷新锁。
全路径锁序固定为refresh_lock→update_lock；捕获视图/marker只取短更新锁，
不得在持更新锁时反向等待刷新锁。

不能每5秒无条件调用`ensure_fresh()`：其marker落后判断会让不断增长的
`update_generation`触发全量快照，违反AC-2。

概念流程如下；步骤名称为拟实现逻辑，不是当前已存在API：

```text
current严格解析
  → 与已提交绑定比较
  → 相同：仅更新时间，结束
  → 不同：尝试刷新锁
      → 锁忙：记录pending，结束
      → 二次比较
      → 获取一份Platform快照
      → 取得其发布共享锁并验证清单
      → 构建候选仓库及相关状态
      → 再验证current、快照身份、停止状态
      → 短更新锁提交
```

current在准备期间再次变化时丢弃候选，下轮追赶最新版本；
不得把基于B构造的结果覆盖已经提交的C。
Platform的1秒快照缓存可能短暂返回此前发布，这属于可重试失配，
不安装“仓库B、快照A”的混合候选。

### 3.3 准备、提交和一致读取

将`quote_bridge.py:_update()`拆分为内部准备与提交阶段，
拟新增`PreparedPublicBinding`和已提交绑定的紧凑引用视图。

准备阶段持有刷新锁，在短更新锁外完成：
快照解码、发布ID和目录边界校验、共享租约获取、DataStore及KlineRepository初始化、
证券/指数缓存、快照安装和SectorMonitorService构造。
任何异常关闭候选自身资源，旧已提交仓库、快照及租约保持。
此完整候选构建只适用于发布身份切换。同发布的业务行情刷新继续复用原仓库，
不能因此在每次marker更新时重新创建DataStore；frame和Depth内容先解析/校验，
随后沿原缓存更新契约安装。本次不改变同发布实时刷新为仓库重建。

提交阶段在`update_lock`内仅替换准备完成的引用、世代及计数，
更新原有bridge/depth/state兼容别名；锁内没有网络、清单4MB解析或仓库全量初始化。
不得在提交中再次构造可能失败的服务。
提交后将资源所有权转给已提交绑定，再在短锁外广播更新。
广播异常可以单独记录，但不能因其失败关闭已经公开、仍被读者使用的候选资源。

Full就绪检查新增使用桥接的内部`binding_snapshot()`：
短锁内捕获同一已提交视图中的repo/store、快照、lease和同步诊断，
在锁外读取该视图的清单并进行原有数据库、Worker和Compute检查。
捕获的repo引用保证本次检查期间旧资源不被终结。
有该视图时不再使用路由提前取得、可能已过期的`data_dir`；
无真实桥接的既有测试替身保留原参数路径。

此保证覆盖桥接提交及就绪读取。原有`app.state`兼容别名是多个赋值，
不能宣称所有业务路由跨多次字段读取都已具备事务式版本快照。
本次不批量重写所有路由；已有在途请求持有的旧仓库继续有效。
若实现发现某个直接读多个别名的路由受此新增并发影响，须在该调用点补最小捕获，
并更新方案/回归范围，不能掩盖为“全局原子切换”。

### 3.4 共享读锁与回收协议

拟新增`backend/app/multitenancy/public_reader.py`，
定义内部`PublicReleaseLease`。锁对象就是已有、每个发布独立且不再修改的清单，
不额外创建`.lock`文件，不改清单内容或checksum。

读者顺序：

1. 严格校验发布目录位于公共根的`releases`内，ID合法。
2. 以只读方式打开清单；支持时使用`O_NOFOLLOW`，`fstat`确认普通文件。
3. 非阻塞获取`LOCK_SH`。获取失败返回固定错误类别，不在探活线程等待。
4. 在持锁状态下读取并验证清单ID、路径仍存在及文件身份，随后构建/读取仓库。
5. lease与DataStore生命期绑定，所有引用释放后关闭DuckDB连接及清单FD。

回收者顺序：

1. 沿原逻辑查询研究引用并生成候选。
2. 只读打开候选清单，非阻塞尝试`LOCK_EX`。
3. 忙、缺失、权限不足、非普通文件或锁能力不可验证时跳过，输出固定原因。
4. 持排他锁重新核对current、近期集合、研究引用及候选路径身份；
   锁忙以外的研究查询失败仍按现有行为终止清理。
5. 条件仍满足才在持锁状态下`rmtree()`，随后关闭FD。

读者若先打开FD但在排他删除后才取得共享锁，步骤4的路径/清单验证必须拒绝旧目录，
关闭FD并重新解析current。仅取得已被unlink文件的锁不能证明目录仍可读取。

`maintain_full_host.py`保持可由宿主系统Python单独运行，只使用标准库实现回收侧
排他锁守卫，不引入app/数据库驱动导入依赖。
宿主与API对同一清单使用同一锁协议，两端约定由跨进程测试约束。
没有可靠`flock`能力的平台保留原读取能力，但本次维护不能宣称读者安全，
应跳过受影响目录删除并明确诊断。

### 3.5 资源与退出

lease终结回调不允许捕获DataStore自身造成引用环；
可以以独立DuckDB连接和lease作为终结参数。
旧store仍有在途引用时不关闭，不人为把旧绑定保存到无限增长历史列表。
GC延后可能暂缓目录回收，这是安全取舍，需观测而非强制释放。

修正lifespan启动阶段局部`repo/store`及回调对初始仓库的保留：
首次刷新后用`app.state.repo`创建Sector服务，释放不再需要的初始局部引用。
资源回调应关闭当前桥接所有权，不以捕获初始store的闭包保留旧租约至退出。

退出先禁止候选提交并通知巡检停止，再关闭marker/Market阻塞I/O并等待收尾，
最后释放进程拥有的绑定。等待预算20秒；
超时不强制释放仍被线程/请求使用的连接或锁，记录固定诊断，
最终由服务进程退出释放FD。保留现有40秒容器停止宽限。
测试须覆盖超时路径，不能仅以正常close通过宣称所有阻塞都可强行取消。

### 3.6 就绪和观测

`public_files`保留原状态及HTTP映射，拟增加以下无敏感值诊断：
`binding_current`、`sync_age_seconds`、`last_error_kind`和`reader_lock_held`。
30秒以内的同步pending且旧绑定/清单有效时仍可就绪；持续失配、入口损坏、
停止、超龄巡检或绑定不可读返回error。不是仅检查current存在即恢复200。

错误类别限定为`current_unavailable`、`invalid_release`、`reader_lock_busy`、
`snapshot_unavailable`、`snapshot_release_mismatch`、`prepare_failed`、`sync_stalled`、
`stopped`；不输出异常原文、地址、FD/PID、完整清单或受控配置。

复用`public.refresh`及`public.refresh_wait`阶段指标。
后台锁忙只增加有界计数；若增加`public.binding_check`、`public.binding_prepare`、
`public.binding_commit`，须同步`observability.py:_RUNTIME_STAGES`允许列表和测试。
不能假设任意新stage名已可用，也不以release ID或用户身份作为指标标签。

## 4. 实施任务

勾选只表示实现和对应已执行验证；候选负载及生产6小时验收单列，不计为已完成。

### Task 1：复现和生命周期基线

Files：现有`backend/tests/test_function_health.py`、`test_full_platform.py`、
`test_full_host_maintenance.py`；拟新增`backend/tests/test_public_binding_sync.py`、
`test_public_reader_lease.py`。

- [x] 将已证实的临时复现转为入仓测试：旧绑定初始200，正常保留后current有效，
  无业务请求时最终自动绑定current并返回200；变更前必须稳定失败3/3。
- [x] 为巡检定义可注入时钟及一次检查入口；使用Event/条件等待代替固定长sleep。
- [x] 补`test_unchanged_release_does_not_fetch_on_markers`：12轮、marker递增，
  snapshot calls和decode计数都等于开始值。
- [x] 补资源引用测试，记录初始store被lifespan保留的现状，明确期望释放条件。

Run（backend目录）：

```bash
uv run --frozen --no-sync python -m pytest tests/test_public_binding_sync.py tests/test_public_reader_lease.py -q
```

预期：新增能力断言在修复前失败；不是依赖真实数据库或当前用户目录的失败。

### Task 2：清单共享租约

Create：`backend/app/multitenancy/public_reader.py`。
Modify：[桥接准备路径](../../../backend/app/multitenancy/quote_bridge.py#L101-L126)、
[初始仓库路径](../../../backend/app/multitenancy/lifespan.py#L66-L87)。
Tests：`test_public_reader_lease.py`、`test_full_platform.py`。

- [x] 实现`PublicReleaseLease.acquire(release_dir)`及幂等`close()`，按3.4节验证后持锁。
- [x] 先取得租约再构建DataStore；失败关闭候选自己的FD/连接。
- [x] 用弱引用终结将lease关联到store；在途引用和终结回调不相互循环保留。
- [x] 验证两读者可同时持共享锁，第三进程排他锁失败；一个读者释放后仍不可删除，
  最后一读者释放后才成功；只读打开文件也必须参与真实Linux锁测试。
- [x] 覆盖目录删除后迟到取得锁、清单替换、越界和非法ID。

### Task 3：桥接准备/提交分离

Modify：[PublicQuoteBridge](../../../backend/app/multitenancy/quote_bridge.py#L60-L147)。
Tests：`test_full_platform.py`、`test_public_binding_sync.py`、`test_closing_price_cache.py`。

- [x] 引入内部候选及已提交视图，复用frame decode、证券缓存和Depth/Sector初始化。
- [x] 将可能失败/耗时的步骤移到短更新锁外，完成后才替换引用。
- [x] 新增`sync_public_release()`，先比较发布再尝试refresh_lock，再次比较后获取快照。
- [x] 新增`binding_snapshot()`，返回保持repo/lease引用的一致视图。
- [x] 在current B→C或停止事件发生后拒绝晚到B；候选失败原绑定逐字段保持。
- [x] 保持业务`ensure_fresh()`、行情marker及已有计数含义，重复触发同一发布最多一次
  成功提交；不会由不同入口并行构建多个同版本仓库。

### Task 4：每进程巡检接线

Create：`backend/app/multitenancy/public_binding_sync.py`。
Modify：[Full lifespan](../../../backend/app/multitenancy/lifespan.py#L62-L130)、
`quote_bridge.py:start/close`。
Tests：`test_public_binding_sync.py`。

- [x] 按3.1节实现start/check_once/snapshot/close、5秒默认间隔及30秒诊断预算。
- [x] 每个uvicorn子进程各自实例化；不把对象放在主进程或模块共享单例。
- [x] 初始refresh成功后启动，API接收请求前完成所有依赖接线。
- [x] 消除初始repo/store局部保留和使用旧repo重建Sector服务的问题。
- [x] 用条件式测试证明没有任何HTTP请求时仍更新；锁忙、请求同步抢先更新、
  异常重试、重复start/close及退出时晚提交均覆盖。

### Task 5：一致就绪读取

Modify：[Full health](../../../backend/app/multitenancy/health.py#L69-L128)；
必要时调整[路由参数捕获](../../../backend/app/api/routes.py#L77-L98)。
Tests：`test_function_health.py`、`test_health.py`、`test_public_binding_sync.py`。

- [x] 从已提交binding_snapshot捕获清单目录、快照和lease；文件解析在短锁外。
- [x] 合并同步诊断到public_files，保持原market/function依赖规则及HTTP含义。
- [x] 模拟prepare被Event阻塞，旧有效绑定在宽限内探活仍返回200且不等待prepare。
- [x] 使用注入时钟跨过30秒，断言持续失配/停止/巡检失活均503；
  true数据库/Platform/清单故障原断言继续通过。
- [x] 旧Fake没有binding_snapshot时使用兼容参数路径；不让替身偶然测试绕过真实
  lifespan必须启动同步器的集成断言。

### Task 6：回收排他守卫

Modify：[maintain_full_host.py](../../../backend/scripts/operations/maintain_full_host.py#L157-L185)。
Tests：`test_full_host_maintenance.py`、`test_public_reader_lease.py`。

- [x] 删除前打开候选清单、非阻塞排他锁及持锁二次检查。
- [x] 保留原候选、研究引用、current、最近30份/6小时语义。
- [x] 报告增加有界skip原因；研究引用查询错误仍终止，锁忙只跳过对应目录。
- [x] 标准库单脚本运行回归：复制维护脚本至临时目录、用系统Python启动仍可dry-run，
  不要求app包或新数据库驱动。
- [x] 用两个真实子进程证明持共享锁时维护不能删除；解锁后才移除；
  回收者先获得排他锁的迟到读者必须验证失败并重试current。

### Task 7：文档、可观测性与回归

Modify：`backend/app/services/observability.py`（仅需要新stage时）、
`docs/operations.md`、`docs/testing.md`、`deploy/full/README.md`、
`docs/secondary-development.md`、`docs/version-baseline.md`、相关报告索引。

- [x] 替换“按需刷新保证闲置进程不会长期读旧目录”的不准确说明，
  明确巡检、共享锁、宽限及非支持文件系统行为。
- [x] 更新需求/架构/测试/运维/发布映射，全部新增接口标明内部L3实现，
  不扩大公共扩展API。
- [x] 补有界指标允许列表及日志脱敏测试，保留原public.refresh计数可比性。
- [x] 执行第6节回归和失败反向证明；没有本轮命令证据时不写“修复通过”。
- [x] 本地和功能分支不加应用版本，不自动提交/推送；集成main后由CI分配版本。

仍待独立验收：

- [ ] 代表性候选4进程25秒收敛、探活p95≤500 ms及最大<6秒。
- [ ] 固定产物上线后逐进程观察真实发布/回收，覆盖至少6小时。

## 5. Workspace setup

实施前运行`git status --short`、`git branch --show-current`，读取届时最新CONTRIBUTING
和二开指南。当前目录位于`work/s01-strategy-support`，有大量其他未提交工作；
本轮只写方案及索引，不能将该目录当作干净修复分支。

本次已使用独立工作树，从固定主线提交`bac72d9a83c03890cbb954e0e13782b8421d6073`开始，
本地必要的未提交修复只有确认相关后逐文件带入，不执行stash/restore覆盖用户修改。
分支为`work/api-public-release-binding-sync`。11个源码/测试文件在目标哈希确认未变化
后聚焦同步共享工作区，既有并行修改保留；没有生成commit或推送。

## 6. 验证步骤

统一在隔离临时DATA_DIR下，`TICKFLOW_ENV_FILE=/dev/null`，
`MULTITENANCY_ENABLED=false`、`SERVICE_ROLE=standalone`、
`POLARS_MAX_THREADS=4`运行单元测试，不能读取真实用户配置。

受影响模块命令（backend目录，新增测试在Task 1创建后）：

```bash
uv run --frozen --no-sync python -m pytest \
  tests/test_public_binding_sync.py tests/test_public_reader_lease.py \
  tests/test_function_health.py tests/test_health.py \
  tests/test_multitenancy_worker_process.py tests/test_full_platform.py \
  tests/test_full_host_maintenance.py tests/test_closing_price_cache.py -q
uv run --frozen --no-sync ruff check \
  app/multitenancy/public_reader.py app/multitenancy/public_binding_sync.py \
  app/multitenancy/quote_bridge.py app/multitenancy/lifespan.py \
  app/multitenancy/health.py scripts/operations/maintain_full_host.py \
  tests/test_public_binding_sync.py tests/test_public_reader_lease.py
```

新增缺陷测试先3/3红，再完整绿；只临时禁用本次同步接线及回收守卫后应重新红，
恢复后再绿。使用隔离工作树中的聚焦修改完成反向验证，不stash共享工作区。
80个或其他计数不能预写为结果；原调查79项通过只属于原调查窗口。

根目录：

```bash
PYTHONDONTWRITEBYTECODE=1 make check
PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_docs.py --include-reports
git diff --check
```

### Expanded test plan

- **Unit**：current不变零下载，失配检测/宽限、错误枚举、候选丢弃、资源终结、
  幂等退出。Event及注入时钟控制竞争顺序。
- **Integration**：真实PublicPublisher→PublicQuoteBridge→Full就绪；
  真实读锁/回收子进程，new current与old in-flight并存，清单迟到锁及GC延后。
- **E2E**：独立候选部署4个API进程，除探活无业务流量；观察发布变更到各进程提交，
  运行原保留条件回收，跨6小时边界。按受控进程观测确认，不新增公开PID诊断接口。
- **Observability**：记录阶段耗时、snapshot_fetches/frame_decodes增量、skip计数，
  以及绑定当前/失配年龄。无变化12轮快照增量为0；确认探活p95和最大耗时满足AC-10。

## 7. 风险、Pre-mortem与上线顺序

| 风险 | 缓解及阻断条件 |
| --- | --- |
| 新后台线程与请求同时刷新 | 同一refresh_lock内二次比较；准备在短更新锁外；旧请求lease继续有效 |
| 新绑定下载很慢 | 旧绑定在宽限内保留；30秒失配如实503；候选负载不达25秒收敛时禁止上线 |
| 文件系统不提供一致锁语义 | 候选阶段做宿主↔API只读挂载锁互斥验证，失败则不启用删除，并重新评估方案B |
| 初始store/回调引用泄漏 | 终结不捕获store，释放初始局部，测试GC和关闭FD；回收跳过数持续增长需要调查 |
| 旧版API不持读锁 | 升级窗口暂停公共发布维护，全部API进程升级并确认持锁后才恢复 |
| 原app.state多个别名不是单引用 | 最低保证就绪/桥接统一视图；复核受影响直接读调用点，不虚构全业务请求事务性 |
| 清单文件被换inode | 读者/回收者持锁后复核身份；发布清单仍不可变，非协作外部改写不视为受支持操作 |

Pre-mortem：

1. **Scenario：巡检成为全市场轮询。**
   Trigger：后台直接调用ensure_fresh，行情marker每秒增长。
   Mitigation：仅比较发布身份；无变化12轮零下载回归是阻断项。
2. **Scenario：API切换后旧请求读文件失败。**
   Trigger：维护看到旧目录不再current即删除，或切换立即关闭旧库。
   Mitigation：store生命期持共享锁，维护排他删除；在途引用测试及真实锁互斥必过。
3. **Scenario：探活被候选构建卡住。**
   Trigger：大快照解析/仓库注册仍位于update_lock内，GET等待同一锁。
   Mitigation：完整prepare在锁外，GET只捕获引用；慢prepare的并发探活验收。
4. **Scenario：混合版本上线时仍复发。**
   Trigger：旧API不持lease而维护已恢复。
   Mitigation：暂停维护→固定候选产物→所有子进程更新确认→dry-run→恢复维护；
   有一个旧进程未确认就不能完成上线。

后续部署必须先完成候选产物和检查，再以具体产物及结果执行上线审批；
用户已批准实施源码修复，尚未授权生产部署、重启、提交或推送。

升级建议顺序：保存受控配置校验基线和回滚产物；短暂停止公共发布维护定时器；
发布/回收者与API采用已验收版本，确认所有API持共享锁并能闲置同步；
先dry-run，再恢复维护，观察至少6小时。
暂停窗口先核对磁盘/inode余量；不能暂停维护后无人跟进造成新的空间故障。

回滚时先暂停维护，再回退已保留产物和对应脚本；用户配置和业务数据保持。
回退旧API后缺陷仍存在，不能把回滚视为修复完成，维护恢复条件需要重新确认。

## 8. ADR

- **Decision**：选每进程5秒发布身份巡检，并用现有不可变清单共享锁保护API读者，
  回收按同文件排他锁和原研究保护条件执行。
- **Drivers**：消除流量依赖、控制全量快照成本、避免新增DB/schema和读者TTL隔离。
- **Alternatives**：方案A chosen；方案B因当前单机部署和复杂度 rejected。
  请求触发探活刷新、延长保留及重启因不能满足AC-1 rejected。
- **Why chosen**：针对已证明的生命周期断层同时补上同步与使用期回收协作，
  复用现有客户端、锁、不可变清单及候选算法。不会无条件提高数据采集频率。
- **Consequences**：增加每进程一个轻量线程和每活跃仓库一个清单FD；
  GC延后会推迟旧目录回收。需要宿主/容器同文件锁验证及协调升级窗口。
- **Follow-ups**：跨主机文件系统或不能可靠flock的部署另行设计；
  全业务API跨多次字段读取的请求级统一视图不是本次承诺。

## 9. Planner、Architect、Critic审视记录

### Planner draft v1

优先最小修复：后台每5秒调用ensure_fresh，并让health捕获更新后目录；
认为原6小时存龄足够覆盖读取。备选使用数据库读者租约。

### Architect challenge v1

最强反驳：ensure_fresh对marker落后也触发快照，闲置进程会重新高频解码；
6小时存龄不约束无限期请求或原仓库引用。自动更新同时引入更频繁的并发提交。
关键张力是最少修改与真实资源安全，及快速跟进与探活延迟。
应将巡检收窄到发布身份，加入实际共享使用期保护，拆分准备/提交。

### Critic verdict v1: REVISE

AC-2无法满足；AC-6缺少真正读者保护；候选耗时/短锁和旧store资源释放没有明确边界。
拒收以上三项，以实际清单文件锁和同一提交视图补齐。

### Planner draft v2

形成第3–7节：发布专用巡检、清单共享锁、回收排他守卫、候选准备和
health短锁捕获；加入只读锁能力验证、GC保留和协调回滚。

### Architect challenge v2

文件锁是协作协议，不能保护非协作旧API；单代理短锁提交也不能让所有app.state
字段自动成为跨业务请求事务。以协调升级和限定一致读取保证应对，
不新增强制中止读者或通用版本管理框架。

### Critic verdict v2: APPROVED_FOR_PLAN

| 维度 | 状态 | 判断 |
| --- | --- | --- |
| Principle-option consistency | 通过 | 巡检解耦业务，保留真实失败和读者安全 |
| Alternative exploration | 通过 | 文件协作锁与DB租约有明确适用差异 |
| Risk mitigation clarity | 通过 | 混合版本、慢准备、GC、锁能力均有验证/阻断条件 |
| AC testability | 通过 | 条件、时间预算、计数、锁互斥均可直接断言 |
| Verification concreteness | 通过 | 新测试名、模块命令、生产逐进程观察及6小时边界明确 |
| File/line coverage | 通过 | 7个实施任务均列出现有或拟新增文件，核心接线有位置 |
| Pre-mortem | 通过 | 四类具体失败触发及补救 |
| Expanded tests | 通过 | Unit、Integration、E2E、Observability分开定义 |

Reservations：

- Task 3兼容别名不构成所有业务路由的请求级版本事务；实施须复核直接读调用点。
- Task 2跨宿主/容器文件锁语义尚未实测，结果是上线阻断条件。
- 25秒收敛和500 ms探活p95尚未验证；只能由候选实测证明，不能拿原调查耗时代替。

以上通过是本轮**单代理方案内部审视**，不是独立人员复审、用户批准或代码验收。
最终迭代2/3。后续实施如偏离ADR，应先更新本方案并说明原因。
