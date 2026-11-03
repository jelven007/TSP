# TSP、rustdx、AIGC 本地数据清理

日期：2026-10-31。范围仅为 `/path/to/home/Documents/` 下的三个项目；
依据用户要求分析目录并清理历史无用数据。本次未提交、推送或修改应用版本。

分析基线包含37,425个文件记录，覆盖源码、报告、交付和嵌套项目；
Git、虚拟环境、node_modules 和本地 Rust 工具链另按目录占用分析。
清理前 TSP 约2.20 GiB，主要为后端虚拟环境、reports、交付和前端依赖；
rustdx 约3.38 GiB，主要为报告、Rust 工具链和原始证据；
AIGC 约293 MiB，主要为 Git、公共媒体和嵌套项目。
这些大目录中的有效数据与工具没有因为体积较大被整目录删除。

## 实际删除

| 项目 | 文件数 | 逻辑字节 | 删除文件的分配字节 | 内容 |
| --- | ---: | ---: | ---: | --- |
| TSP | 135 | 14,544,962 | 14,872,576 | 6 个 HTML 分享 ZIP、Python 字节码、测试/lint 缓存和系统元数据 |
| rustdx | 1,604 | 27,345,923 | 32,079,872 | 1,449 个打包工作副本、137 个解包文件、18 个系统元数据文件 |
| AIGC | 30 | 126,854 | 196,608 | 17 个旧探测脚本、2 个已结束调试文件、3 个已跟踪字节码和8个系统元数据文件 |
| 合计 | 1,769 | 42,017,739 | 47,149,056 | 约 40.07 MiB 逻辑数据、44.96 MiB 文件分配空间 |

逐文件路径、删除前 SHA、大小、是否跟踪及原因见
删除清单（附件保存在受控归档）；汇总见 [summary.json](summary.json)。
分配字节按删除前 `st_blocks × 512` 统计，硬链接只在最后一条链接删除时计入；
不是整盘可用空间增量，未扣除本记录占用，也不计 APFS 快照保留。

## 删除依据与保留边界

- TSP 六个分享 ZIP 内的全部352个文件均与仍保留的报告原文件逐 SHA 一致。
  用户策略、HTML、图表、原始行情、回测结果和复现脚本保持原位置。
- rustdx `final-preflight/package-workspace` 的1,449个源码/夹具文件与保留的
  `final-preflight/source.tar.gz` 同名成员逐 SHA 一致；生成的 `Cargo.lock` 不同，
  已保留在原位置。`unpacked` 的137个文件与保留的两个 `.crate` 包逐 SHA 一致。
  重新打包和解包的真实入口为 `scripts/release/preflight.py`。
  [rustdx 就地说明](file:///path/to/home/Documents/rustdx/reports/comprehensive-optimization-20261027/evidence/local-cleanup-20261031.md)
  记录恢复位置与范围。
- AIGC `scratch/` 为早期供应商/数据库连通性及演示探测；未被应用、构建、
  package scripts 或现行测试引用，历史分析也建议移出。
  `backend/.dbg/` 为已结束的空白页调试材料，没有运行时引用。
  `.gitignore` 新增这两个临时目录，已有字节码忽略规则保留。
  本次没有运行这些脚本或调用外部服务。
- TSP 缺口/不可用键清单、当前发布证据、预置策略、源码恢复归档、用户最新研究，
  rustdx 冻结原件、完整证据归档、源码 ZIP、发布包和恢复演练原件均保留。
  活跃依赖、Rust 工具链、Git 历史、身份/SMTP 配置、业务数据和 AIGC 嵌套仓库保持。
  相同内容但元数据、来源或恢复职责不同的证据没有改为硬链接。

## 本次核验

执行删除前核对候选文件当前 SHA；源码副本/解包文件/分享包逐成员核对保留原件，
并检查当前进程打开文件，候选中没有被占用的文件。

删除后核验结果：

- [完整性记录](integrity.json)：TSP 3,605、rustdx 30,736、AIGC 1,313个
  保留文件，共35,654个逐 SHA 核对，无额外缺失或字节变化。
  本次有意修改的 TSP `reports/README.md`、AIGC `.gitignore` 单独审核。
  三个仓库 HEAD 与 AIGC/clipforge 的 HEAD、干净状态均保持。
- TSP `PYTHONDONTWRITEBYTECODE=1 make check`：87个必需路径、
  35份当前 Markdown、八模块需求、0.4.39版本及 diff 空白检查通过。
  `PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_docs.py --include-reports`：
  208份 Markdown 与保留需求基线检查通过。
- TSP 工具回归：当前 PATH 未找到 `uv`，指定已有 uv 后旧 `pytest`
  启动器仍无法启动。按维护文档改用同一项目解释器：
  `PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS="-p no:cacheprovider" /path/to/home/.local/bin/uv run --project backend --frozen --no-sync python -m pytest tests/tooling -q`，
  实际146项通过，46.19秒；无需安装依赖或修改运行环境。
- rustdx `PYTHONDONTWRITEBYTECODE=1 python3 scripts/validation/check_documentation.py`：
  53份文档、1,181个链接、FR01–18检查通过。
  `PYTHONDONTWRITEBYTECODE=1 python3 scripts/validation/check_documentation_lifecycle.py --today 2026-10-31`：
  36份登记文档及源码锚点检查通过。
- AIGC `git diff --check` 及 `git check-ignore --no-index` 检查通过：
  新临时目录和原字节码路径均命中忽略规则。22个原已跟踪垃圾文件留为待提交删除。
- `.DS_Store` 由系统自动再生，完整性核验时记录到12个重新出现的元数据路径；
  其余删除路径均不存在。自动再生与新清理记录占用不计作实际用户数据变化。

历史报告中的测试计数、失败记录及采集时点清单保持原文；
这些检查只验证本地清理和文档/工具契约，不重新授予行情或生产验收。
