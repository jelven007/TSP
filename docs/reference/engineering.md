# 工程目录、构建与协作参考

核对日期：2026-11-15。当前文档归属见[文档中心](../README.md)，
源码、发布和运行身份见[版本基线](../version-baseline.md)。

<a id="layout"></a>
<a id="layout-项目目录与工程边界"></a>
## 1. 仓库布局与文件归属

<a id="layout-1-完整目录"></a>
```text
TSP/
  AGENTS.md / CONTRIBUTING.md / README.md / LICENSE / VERSION
  操作说明书.md
  Makefile / Dockerfile / docker-compose.yml / dev.sh / dev.ps1 / update.ps1
  .env.example / tiers.yaml / .github/ / .trae/skills/
  backend/
    app/                  API、服务、Provider、仓库、指标、策略、因子、回测、租户
    app/cell/sql/          按序迁移；随应用打包
    app/extensions/       真实受控扩展入口
    app/custom/           自定义模块与模板
    native/rustdx_native/  固定 Rust 核心与 PyO3 桥接
    scripts/              acceptance/performance/operations/research/diagnostics/demos
    tests/                就近领域回归、backtest、fixtures
    pyproject.toml / uv.lock
  frontend/
    src/                  pages/components/lib/extensions/custom 及就近测试
    public/               当前静态资源
    package.json / pnpm-lock.yaml / vite.config.ts / tsconfig*.json
  mcp-server/             独立 MCP 包
  deploy/                 full/edge/cell/windows 模板
  packaging/              桌面构建与安装资源
  scripts/                工程检查、版本、发布、升级、恢复及分类工具
  tests/tooling/          仓库工具与文档契约
  examples/               可运行数据源、策略和开放 API 示例
  brand/                  当前品牌源文件和平台图标
  docs/                   六类主文档、专题参考、需求基准和当前计划
  deliverables/
    research/             用户 Excel、冻结输入和详细账本
    audit/                当前发布、数据、恢复及必要历史证据
  data/                   应用运行数据；不提交
```

<a id="layout-2-文件归属与依赖方向"></a>
API／页面调用业务服务，业务服务调用领域计算、仓库及 Provider；
不在页面、脚本和 API 中复制另一套业务计算。多个运行角色复用后端源码，
不因进程独立拆成重复源码包。
两处 scripts 包的导入与执行目录分别按[仓库工具](../../scripts/README.md)和
[后端工具](../../backend/scripts/README.md)说明，不合并 Python 包。
后端平铺测试与前端就近测试保持可发现，不按名字盲目移动。

<a id="layout-4-开发与检查入口"></a>
```bash
make check
make test-tooling
make test-backend
make test-frontend
make build-frontend
```

命令须先准备冻结依赖；测试隔离见[测试指南](../testing.md)。
工程检查不要求 reports、旧截图或历史过程目录存在。

<a id="layout-5-与后续规模化需求的关系"></a>
当前为模块化单体及单机多角色实现。
新数据底座、多节点和万人容量属于[阶段计划](../planning/phase2-implementation-plan.md)，
只有量测或明确需求触发后建设，不提前假设已接入。

<a id="layout-6-兼容与维护"></a>
移动目录同步导入、CLI、CI、构建清单、Markdown 和动态加载路径。
公开锚点继续保留，旧路径的冻结证据不改写；
本次只将必要材料按哈希迁移，应用数据与未提交源码保持。

<a id="layout-7-当前内容保留规则"></a>
保留判据、职责与复核周期统一见[工程维护](../maintenance.md)。
缓存按冻结锁重建；当前源码、配置、业务数据、唯一开发工作和必要证据受保护。
临时过程文件不进入工程目录。

<a id="engineering"></a>
<a id="engineering-生产系统工程规范"></a>
## 2. 从需求到生产的工程闭环

<a id="engineering-1-工程与运行边界"></a>
源码目录不是运行目录。初始化只按受控配置执行，真实口令、SMTP授权码
不进入源码、文档、报告或日志。运行状态以实际进程、挂载与新健康回执核对。

<a id="engineering-2-需求到生产的工作闭环"></a>
每项行为改变沿需求 ID、设计、开发、测试、运营、运维完整追踪。
先理解调用链和现有测试；修复定位根因并用适用回归验证。
现行规则只维护一份，历史测试计数和未安装候选不充当当前验收。

<a id="engineering-3-后续目标的实现落点"></a>
目标契约标记“目标”或“按需扩展”，不能虚构已有 API。
二次开发先查[真实扩展入口](../secondary-development.md)，
必要核心变更保持聚焦和兼容验证。

<a id="engineering-4-配置锁文件与数据迁移"></a>
配置模板、锁文件、运行保存值与历史冻结值分别核对。
数据库迁移有序新增，禁止修改旧迁移校验和；
当前行情、账户、持仓、账本、研究快照和用户策略保留。
初始化、升级、重启不回写用户已主动修改的参数。

<a id="engineering-5-目录文档与ci门禁"></a>
需求编号基准为[requirement-ids.json](../requirement-ids.json)，
保留164个FR、42个NFR与八模块边界。目录、文档、版本及空白检查在根 Makefile。
CI定义见[ci.yml](../../.github/workflows/ci.yml)；生产认证按同一固定身份。

<a id="engineering-6-发布恢复与增量保护"></a>
升级、恢复须匹配源码、schema、Runtime／Compute和用户配置。
本地只保留当前正式运行版本、零部署备份；
线上保留当前版本和最近一份完整且恢复验证通过的备份。
在用恢复点先保护，验收后清退。

<a id="build"></a>
<a id="build-构建清单与离线升级备份"></a>
## 3. 构建身份与离线升级材料

<a id="build-构建身份"></a>
构建清单由[scripts/build_manifest.py](../../scripts/build_manifest.py)生成，
记录版本、完整SHA、文件摘要和构建环境。升级检查入口为
[scripts/upgrade_check.py](../../scripts/upgrade_check.py)。
第三方核心、原生桥接和独立MCP包版本不随主应用自动改号。
当前正式身份及发布证据见[版本基线](../version-baseline.md)。

<a id="build-git-二开升级材料"></a>
二开升级前核对基点、目标提交、暂存与工作区差异、三方源码，
只受控保全独有工作，不将缓存或旧工程整份复制当作恢复方案。
源文件、配置、依赖锁和构建清单必须匹配，文本预演不代替实际构建测试。

<a id="build-已安装产物的差异备份"></a>
已安装产物先验证其清单和原生组件，记录允许变化与保护集合。
以源码重新构建正式产物；Runtime、Compute与前端同身份验收。
不直接叠加未知旧目录。所需恢复点按用户保留数量管理。
源码恢复入口见[source_recovery.py](../../scripts/release/source_recovery.py)。

<a id="collaboration"></a>
<a id="collaboration-多人协作与版本管理"></a>
## 4. 多人协作与远程权限

<a id="collaboration-开发与合并"></a>
main为唯一集成线；功能分支通过PR合并，遵守
[贡献规范](../../CONTRIBUTING.md#10-pr-提交要求)。
本地迭代、提交和功能分支推送不递增版本；
远程main集成后CI自动递增一次，不手动改号、不移动冻结标签。
不覆盖其他贡献者的未提交工作，合并以实际验证为准。

<a id="collaboration-远程规则与权限"></a>
仓库保护定义在[.github/rulesets](../../.github/rulesets)。
按最小权限分配读、写、复审与发布责任；不以功能验证授权发布或发消息。
提交签名、hooks和远程保护按仓库既有规则执行。

<a id="trace"></a>
<a id="trace-本地变更与全生命周期追踪"></a>
## 5. 需求与生命周期追踪

<a id="trace-1-状态与责任"></a>
需求定义及状态只在[需求手册](../requirements.md)维护。
六类主入口见[文档地图](../README.md)，职责、事实源、复核周期和失效条件见
[维护表](../maintenance.md#1-文档归属)。

<a id="trace-2-全部文件的追踪方式"></a>
文件清理记录原身份、摘要、删除／移动理由和新位置；
移动复验哈希，删除前复验运行引用，数据与配置独立核对。
Git历史继续保存，不用删除旧报告代替删改已提交历史。

<a id="trace-3-本次完成与未完成"></a>
清理边界及核验方法见[资料清理](../maintenance.md#3-清理边界)。
当前未完成工作见[待验收项](../planning/current-validation.md)；
用户回测和必要审计分别保留，旧结果不因整理被改写。

<a id="trace-4-后续维护规则"></a>
行为变更立即同步唯一主章节；不往六类文档反复追加逐日修复流水。
完成任务后清理临时下载、截图、输出和可重建缓存。
冻结历史附件仅保留必要且不可替代的成果与证据。

<a id="decisions"></a>
## 6. 工程决策记录

持久工程决策进入对应架构、接口或运维主章节，记录适用边界和真实依据。
一次性选择、失效提案及完成的步骤清单不单独长期留在根工程。
