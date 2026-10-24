# 生产体系目录、需求与生命周期整合验证

日期：2026-10-23。应用版本0.4.17；源码核对HEAD：
`cc837b95f471e5b299bf5e8618332d500bff7e3a`。工作区有未提交变更，
本记录不表示已提交、发布、部署或新目标通过。

## 范围

工程目录按真实发布单元与职责整理；34份专项文档分类；需求总纲与八模块保留
203个唯一编号；生产工程、生命周期追踪、联合验收与服务运营规范已编写。
历史依据副本留档，旧离线交付保持冻结。

## 1. 完成范围

1. 保留后端、前端、MCP、原生/桌面和部署编排的实际发布边界，工程脚本、
   工具测试、专项文档与历史证据按职责归属。此前66个文件迁移与本轮34份
   专项文档迁移合计100个文件；源、目标和内容哈希均有记录。
2. 需求总纲保留角色、对象、状态、金融规则、42条NFR、容量、阶段和验收；
   161条FR分为八个业务模块。原203个编号集合完全相同，逐条产品行为、
   优先级、状态和异常规则保持，已实现条目明确归S0。
3. 增补生产工程、逐文件生命周期追踪、A–I与3.1–3.6联合验收、服务运营和
   ADR0002，同步需求、方案、开发、测试、运营与运维主入口。
4. 新增标准库文档检查器与11项失败/仓库契约测试，同步Makefile与CI。
   目录引用的后端注释、前端说明及文档链接已更新，没有新增业务能力。
5. 系统目标引用的融合方案、ClickHouse设计/SQL和用户参考图已留可检出的
   冻结副本；旧离线交付保持原内容。实现、历史测试、目标与生产状态分别记录。

## 2. 实际验证结果

| 检查 | 本次结果 | 范围与证据 |
| --- | --- | --- |
| `make check` | 通过 | 81个必需路径；81份维护Markdown链接/锚点/围栏；八模块/编号；版本0.4.17；diff空白 |
| 整个`tests/tooling/` | 111 passed，47.04秒 | 版本、主线/标签工作流、研究工具、冻结观察包与文档契约 |
| 17份受影响后端测试及5份打包/版本测试 | 234 passed、3 skipped，21.83秒 | 维护、验收、研究、导入/拷贝/根定位、构建清单、升级预检、桌面、Compose配置、版本来源 |
| 中文契约字符注释后的文档测试复核 | 11 passed，0.25秒 | 与上述111项重叠，不重复累计 |
| 前端`pnpm install --frozen-lockfile`和`pnpm build` | 通过 | 322个输入文件与工作区逐文件一致；锁文件相同；TypeScript与Vite实际构建 |
| 改动/新增Python的语法和增量Ruff | 76份可解析，零新增告警 | 使用backend项目配置，迁移文件对照HEAD原路径；现有无关告警未清理 |
| 9份后端业务源码 | 可执行AST不变 | 忽略文档字符串后与HEAD比较，变化仅文档路径/注释 |
| 迁移后的CLI `--help` | 30/30通过 | 未执行真实采集、回填、清理、通知或持续压测 |
| YAML/JSON/TOML配置 | 12份可解析 | 识别Compose原有`!reset`/`!override`；仅语法/元数据，不是Docker运行验证 |
| 需求契约对照 | 203/203保留且唯一 | 161FR、42NFR；行为、优先级、状态和异常单元格保持 |
| 文件保护 | 无已有文件意外缺失 | 开始1569路径含66个既有删除；1503个实存文件中1425哈希保持、78个授权文档/路径/门禁更新 |
| 迁移与设计素材 | 100个目标齐全，旧路径移除 | 24张既有截图与HEAD原图同哈希；12个设计输入源/副本同哈希 |
| 包含历史报告的额外链接审计 | 剩两条历史缺失链接 | 仅旧`.tools`构建清单与回归日志不存在；未伪造替代历史证据 |

完整执行范围见[check-results.json](check-results.json)，输出见
[工具测试日志](tooling-tests.txt)、[后端测试日志](backend-tests.txt)与
[前端构建日志](frontend-build.txt)。前端保留原ECharts分包超过500kB的构建提示；
本次仅调整说明/文档路径，没有扩展到无关分包优化。

## 3. 环境与验证边界

后端/工具测试在仓库外CPython3.12.15、macOS arm64隔离环境执行，
回归依赖单独安装；未据此认证完整项目冻结依赖或原生桥接安装。
后端与CLI使用隔离`DATA_DIR`、空dotenv和`POLARS_MAX_THREADS=4`，
pytest缓存与前端构建/依赖留临时目录。前端使用Node24.22.1、pnpm9.10.0和原锁文件。

3项跳过均来自`test_full_compose_config.py`：本机没有Docker Compose CLI。
没有执行真实镜像构建/容器编排、完整后端测试、前端产品浏览器联调、
Rust构建、Win10真机或安装包验收。前端说明路径变更已实际构建，
未把构建等同页面全流程验收。

万人、多节点HA、强Compute、Cell迁移、ClickHouse应用接入、
Kafka/Flink/Redis/TOS、真实数据覆盖/许可/源时钟、完整交易日及区域恢复，
都不属于本次已通过范围。二期第二步持续/交易日终态仍待，第三步未开始。
生产最后已知版本与调查继续以[版本基线](../../docs/version-baseline.md)为准；
本轮没有生产探测，历史PID/窗口不表示当前进程状态。

## 4. 历史链接限制

额外命令`python scripts/check_docs.py --include-reports`退出码1，仅报告：

- `reports/single-host/verification.md` →
  `../../.tools/builds/single-host-06/build.json`。
- 同一历史报告 →
  `../../.tools/regression-final.log`。

这些是早期忽略目录中的原始运行产物，当前工作区已缺失。
历史报告的正文、计数和命令保留原时点；本轮门禁覆盖维护文档，
不把历史链接审计写成全通过，也不重建日志冒充当时证据。

## 5. 逐文件与全生命周期追踪

[可读清单](file-inventory.md)、[完整JSON](file-inventory.json)及
[计数摘要](inventory-summary.json)从`git diff HEAD`与全部未跟踪工程文件生成。
迁移源/目标分别记录，每个路径关联需求范围及需求、方案、开发、测试、
运营、运维六类入口；删除路径指向其真实迁移目标。

[全部迁移](all-migrations.json)记录100个来源与目标；本轮专项迁移见
[34份文档映射](document-migrations.json)；原需求集合见
[requirement-ids.json](requirement-ids.json)，保持结果见
[requirement-preservation.json](requirement-preservation.json)。

[工作区保护](workspace-protection.json)、
[增量Ruff](ruff-incremental.json)、
[语法与业务边界](syntax-and-business-boundary.json)、
[配置核对](config-validation.json)、
[CLI入口](cli-results.json)和
[前端输入/锁核对](frontend-build-result.json)提供适用证据。
冻结依据来源见[设计输入清单](design-input-manifest.json)。
[原融合与系统目标交付核对](frozen-deliveries.json)确认两份旧ZIP
的23/30个成员与各自原目录逐字节一致，CRC与SHA-256已核对。

清单不纳入忽略的运行数据、凭据、依赖、缓存和交付生成物。
自身描述清单不递归计算自身哈希，由离线交付的外部哈希清单覆盖。
工作区哈希是本地内容标识，不是已提交、可部署或生产SHA。

## 6. 交付状态

目录、需求与六类生命周期文档同步及适用工程回归已完成。
本地应用版本保持0.4.17；没有提交、推送、发布、部署、改schema或迁移用户数据。
新增目标需按联合验收规范继续建设并独立签收。

相关入口：[需求总纲](../../docs/requirements.md)、
[工程目录](../../docs/reference/engineering.md#layout)、
[生命周期追踪](../../docs/reference/engineering.md#trace)、
[生产验收](../../docs/testing.md#acceptance)。
