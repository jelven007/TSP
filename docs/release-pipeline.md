# 发布流水线与固定执行入口

2026-11-07发布工程候选：工具和工作流已修改，尚未提交、上线或完成四镜像真实构建。
本地应用、Worker、PostgreSQL和Compute保持用户指定的暂停。60～90分钟仍是目标，
至少三次同规模发布计时和一次冷缓存验证完成后再评估；本地工具测试不认证该目标。
版本分配规则只在[贡献规范10.1](../CONTRIBUTING.md#101-主线版本规则)定义。

## 构建与正式提升

主线 `ci.yml` 的 Version Job 分配一次版本并输出完整40位SHA；
Backend、Frontend和可复用 `docker.yml` 均检出该SHA并行执行。
Runtime 构建 `linux/amd64`、`linux/arm64`，Compute从该Runtime的索引digest构建同两架构。
`image_manifest.py` 核对角色、平台、版本、revision、统一源码树摘要及索引/platform manifest/config
三类digest，记录layer的摘要和字节数。源码树摘要为全部受跟踪文件名到SHA-256的映射经
`release.common.object_hash` 序列化后的SHA-256；不使用镜像config或引擎ID替代它。

构建阶段只写 `ci-<run_id>-<run_attempt>-runtime/compute` 候选标签。
主线版本、前后端质量门和全部构建/身份检查通过后才创建Git标签；
`docker-promote.yml` 核对Git标签、重新验证矩阵，再写应用版本（不加 `v`）和完整SHA镜像标签。
已有同名标签同digest则复用，不同digest则阻断。正式提升前检查全部目标，写后逐个核对；
Registry标签写入没有跨两个仓库的原子事务，部分失败必须按实际digest协调，再幂等续办。
`latest` 在独立串行锁内核对当前main，旧候选和tag/手动构建均不能覆盖新主线指针。
tag/手动构建独立执行后端、前端质量门；没有对应Git版本标签的手动构建不能正式提升。
桌面发布仍走已有tag和独立平台质量门；产物发布与生产切流分别签收。

构建记录基础镜像的索引digest、后端锁文件摘要、原生源码、工具链和Python ABI。
第三方依赖先从 frozen lock 导出带hash的闭包再安装，README及真实应用元数据后置；
自动改号不应改变第三方闭包。原生缓存按架构、ABI、工具链及原生源码/Cargo锁分隔；
应用版本写入uv.lock不使原生缓存失效，第三方闭包变化仍使对应安装层失效。
基础标签在每次构建中解析为digest并保存，
尚未提供跨发布的基础镜像审批/刷新目录，不能宣称不同日期重建必得同一digest。

## 私有计划与断点恢复

固定入口是[编排工具](../scripts/release/pipeline.py)，用Python标准库，不需要启动应用。
计划是受控的可执行输入：使用 argv 数组，禁止把未知来源计划直接运行。
它只编排已有命令，不生成迁移、数据库备份、健康、登录或业务验收的替代实现。
生产步骤必须接入当前已验证的备份/恢复、切换和重启命令后才能执行。

```bash
python3 scripts/release/pipeline.py --plan "$RELEASE_PRIVATE/plan.json" \
  --receipts "$RELEASE_PRIVATE/receipts"
```

`RELEASE_PRIVATE` 由执行者指定为工作区外的受控绝对路径。计划结构：

| 字段 | 实际契约 |
| --- | --- |
| `format`、`version`、`revision` | `1`、当前 `a.b.cd`、完整40位SHA |
| `environment`、`lock_dir` | 稳定环境ID、同主机所有执行者共用的受控绝对锁目录 |
| `inputs` | 绝对普通文件路径→SHA-256；包含冻结源码清单、配置/产物清单、实际执行脚本及探针文件 |
| `steps[].id`、`phase` | 唯一步骤ID、按下述阶段顺序排列 |
| `effect` | `read`或`write`；备份生成、迁移、切换、发布、删除均为write |
| `argv`、`probe`、`postcheck` | 非空字符串数组；命令返回码必须为0 |
| `timeout_seconds` | 可选正整数，默认3600秒 |

阶段固定为 `preflight → quality → artifacts → backup → switch → acceptance → retain → publish
→ cleanup → final`；可以有多个同阶段步骤，不能倒序。允许只读预检计划，但该计划通过
仅认证所列步骤，不表示整个升级通过。以下是无服务副作用的结构示例，字段取值为演示身份：

```json
{
  "format": 1,
  "version": "0.4.50",
  "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "environment": "offline-demo",
  "lock_dir": "/private/tmp/tsp-demo/locks",
  "inputs": {"/private/tmp/tsp-demo/frozen.txt": "<该文件实际SHA-256>"},
  "steps": [{
    "id": "verify-input", "phase": "preflight", "effect": "read",
    "argv": ["python3", "-c", "print('checked')"],
    "probe": ["python3", "-c", "print('{\"identity\":\"offline-demo\"}')"],
    "postcheck": ["python3", "-c", "print('{\"status\":\"passed\",\"identity\":\"offline-demo\"}')"]
  }]
}
```

probe输出JSON对象；postcheck还必须有 `status: passed`，并包含其实际验证到的版本、
产物/配置/业务边界等稳定身份，不能仅输出常量“通过”。编排器对JSON做摘要，原子写入
`receipt.json`，保存计划、前驱、探针和输出摘要、阶段状态、时间、耗时与失败分类。
只读重试保留原失败attempt及耗时，不覆盖先前失败证据。
回执默认权限0600；不记录argv或原始stdout/stderr，避免携带私有参数。
阶段命令须在受控路径保存自身详细日志；编排回执只能说明返回码和后置断言。

重新执行先重验全部inputs和已通过阶段的postcheck；输入、脚本或输出身份变化即阻断。
只读失败可以重试；write失败或遗留running不重放argv，
只有 `--reconcile` 后实际postcheck确认完成才能继续。未完成动作需人工核实并按新计划修复，
不能将 `--reconcile` 当成“强制继续”。重验失败使本轮总体回执failed，
旧阶段的通过证据仍保留。进程锁只协调同主机、同锁目录的执行者；
多主机部署须让切换在目标主机统一入口中进行，尚无分布式发布锁。

`environment: local` 的写入计划还要求用户明确恢复授权及 `--resume-local`。
默认暂停标记为 `~/.cache/tsp-dev/TSPP/local.paused`；存在标记时即使传该参数仍拒绝写入。
当前暂停要求在标记缺失时也有效，执行者不得靠重命名environment或自定义路径绕过它。

## 两版恢复与容量前置

[preflight.py](../scripts/release/preflight.py)消费受控清单并输出私有回执：

```bash
python3 scripts/release/preflight.py --manifest "$RELEASE_PRIVATE/recovery-units.json" \
  --output "$RELEASE_PRIVATE/preflight.json"
```

清单 `format: 1`，`units` 恰含当前、上一版本且当前较新；每项必须提供：

- `version`、`revision`；`source.root`实际绝对目录与`source.files`相对普通文件路径→SHA-256。
- `config`实际受控绝对配置文件路径→SHA-256，不把凭据原文加入输出。
- `images`的角色、平台、实际OCI `layout`绝对路径与索引`digest`；四个角色/平台组合齐全。
- 顶层`capacity`的实际路径、`required_bytes`、`reserve_bytes`；同文件系统所需空间求和，
  额外预留取最大值，防止多项预算重复使用同一份空闲空间。

每个布局实际读取并核验所有字节，以及版本/revision/角色/平台/digest/源码树摘要。
恢复源码清单须包含构建源码树的完整集合；热补丁恢复源与正式标签源分别登记，
不允许只核对VERSION或把部分文件清单当作全源码。工具验证清单内文件；
完整清单的采集与现场身份匹配仍由执行者负责，不能自动认证未列入文件。
清单和源码必须冻结并作为pipeline inputs绑定；实际双库backup/scratch恢复另行执行，
该预检不证明数据库可恢复，不搬移旧归档或清缓存。

[source_recovery.py](../scripts/release/source_recovery.py)生成新源码恢复集合时，记录完整tar成员
大小/hash；验证使用一次顺序解压，拒绝缺失、额外、重复、越界和非普通文件成员。
manifest仍为format-1；新增`members`字段。旧集合仍可读取并逐成员检查，
没有持久成员清单不等同对完整成员集合的独立证明。

## 按digest下载与项目缓存

[oci.py](../scripts/release/oci.py)仅支持HTTPS Registry、SHA-256及两种Linux平台：

```bash
python3 scripts/release/oci.py fetch --image "$RUNTIME_REPOSITORY" \
  --digest "$RUNTIME_INDEX_DIGEST" --platform linux/amd64 \
  --cache "$OCI_PRIVATE_CACHE" --layout "$RELEASE_PRIVATE/runtime-amd64"
python3 scripts/release/oci.py verify --digest "$RUNTIME_INDEX_DIGEST" \
  --platform linux/amd64 --layout "$RELEASE_PRIVATE/runtime-amd64"
```

repository没有tag；cache为项目专属持久目录，layout必须尚不存在。
私有认证只从 `OCI_REGISTRY_USER/OCI_REGISTRY_TOKEN`环境读取，异主机重定向不转发凭据。
index、manifest、config及压缩layer都按digest/字节数核验；完整缓存命中不联网，
下载分片使用HTTP Range续传，污染、大小不符或非法路径会拒绝，不自动掩盖错误。
回执记录实际传输字节和缓存命中。每个离线布局独立复制必要层，不因后续缓存回收失效。
工具不执行Docker load/save；引擎导入、离线导入适配、实际Docker ID和无网络原生/Compute烟测
仍是环境专属签收。只下载本架构，避免本机导入再导出作为跨机默认链路。
缓存回收只处理确认无引用的项目内容，保留当前/上一版恢复单元；不清全局共享缓存。

## 业务增量的证据边界

[business_diff.py](../scripts/release/business_diff.py)只读比较输入JSON，不访问或写入数据库：

```bash
python3 scripts/release/business_diff.py --before "$RELEASE_PRIVATE/before.json" \
  --after "$RELEASE_PRIVATE/after.json" --audit "$RELEASE_PRIVATE/audit.json" \
  --output "$RELEASE_PRIVATE/business-diff.json"
```

快照包含相同`scope`、带时区`captured_at`、`transaction: repeatable-read`以及
`rows`精确资源ID→SHA-256；新增/删除用缺失资源表达。
audit为按实际顺序采集的事件列表，每条含`resource/at/actor_id/request_id/action/before/after`，
缺失资源hash为null。仅允许时间窗口内、身份齐全、before接上一after且最终hash相同的链解释差异；
未知变化使输出blocked和非零退出。正常增量保留，不恢复旧库来凑数量相等。

当前应用 `business.audit` 主要保存request/action/target，没有完整行哈希及逐事务采集接线；
`business_transaction`默认事务也不能冒称repeatable-read。工具不认证调用者自报的隔离级别。
因此生产一致性快照导出及所有写入的完整哈希审计仍待接入，不能拿旧请求日志自动放行。
审计不足继续逐条核实并阻断未解释变化，不以宽泛白名单代替证据。

## 验证与性能签收

先 `make check`，再 `make check-release PYTHON=<冻结环境Python> RUFF=<冻结环境Ruff>`，
随后完整 `tests/tooling`；工作流使用actionlint单独校验。
规则和回归见[测试指南](testing.md#5-当前-ci-门禁)。
新增工具不改变策略、费用、账户或回测数据，不自动开展回测。
后续发布记录候选冻结→最终签收与用户端到端时间、阶段耗时/重试、实际网络字节、
冷热缓存命中及容量预检。资源扩容、测试分片、真实四镜像构建/导入、部署与业务审计接线
分别签收，未执行项保持待完成。
